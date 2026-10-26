# -*- coding: utf-8 -*-
"""brain —— 把「灵枢脑」（md_cg 认知图）**融合进本记忆库**（不是做成第二个插件）

## 为什么是"融合"而不是"装两个插件"

两套记忆各自往对话里注入，就会长出我们查了一整天的那个病：**自己注入、自己召回、自己当证据**
（自噬闭环）。所以本模块的定位是：

| | 独立插件（已撤） | 本模块（融合） |
|---|---|---|
| 工具面 | 多出 `lingshu_cg`/`lingshu_stg` 等一整套 | **不新增工具面**，走本库既有入口 |
| 注入 | 自己再往对话里塞一块【灵枢最近记忆】 | 由 `mem.py recall --json` 的 `brain` 段返回，**与本库命中渲染进同一个 `<memory_recall>` 块** |
| 数据根 | `~/.dsh/.dsh-memory/data/mdcg` | **本库内** `brain_root/` |
| 包来源 | profile 的 node_modules（DSH 插件依赖） | 本仓 `vendor/dsh-memory/`（MIT，随仓走） |

## 分工（两套记忆各管一段）

- 本库（RAG）管「**原始会话可溯源检索**」：切片 + FTS，答"哪一轮说过什么"。
- 脑（认知图）管「**经审的结论**」：CCG 卡 + 验证态，答"这条结论**还成不成立**"。

## 边界（如实标注）

- 脑包缺失 / 连不上 / 超时 ⇒ 一律返回 `{"status": "unavailable", "reason": ...}`，
  **显式降级**，绝不静默、也绝不假装召回成功（本库 `*_not_ready` 同款口径）。
- 数据根下的 `_keys.json` 等由脑自己管理；本模块**不读凭据、不打印凭据**。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import paths as _paths

#: 代码根。★ 用 `paths.CODE_ROOT`（abspath），**不用** `Path.resolve()`：
#: pnpm 的 `node_modules/<pkg>` 是符号链接，`resolve()` 会指进 `.pnpm/<hash>/`
#: 内容寻址仓库（只读、升级即被 prune），于是 `BRAIN_ROOT` 会往包管理器目录里写数据。
ROOT = Path(_paths.CODE_ROOT)                              # 代码根（找 vendor）
#: 脑包位置（可经 env 覆盖：搬仓/换版本时不用改代码）
VENDOR = Path(os.environ.get("DSH_BRAIN_VENDOR")
              or (ROOT / "vendor" / "dsh-memory"))
#: 脑数据根 —— 挂在**数据根**下，不挂在代码根下（代码可来自安装包，数据必须在用户那里）
BRAIN_ROOT = Path(_paths.DATA_ROOT) / "brain_root"
DEFAULT_TIMEOUT = float(os.environ.get("DSH_BRAIN_TIMEOUT", "20"))
#: 写入凭据（**仓外**）：脑的角色制令牌。路径可经 env 覆盖。
#: ★ 只读这个文件、只经 env 传给子进程 ⇒ **绝不打印、绝不入仓**。
TOKEN_FILE = Path(os.environ.get("DSH_BRAIN_TOKEN_FILE")
                  or (Path.home() / ".mdcg" / "dsh_rag_token.txt"))


def _token() -> Optional[str]:
    try:
        if TOKEN_FILE.is_file():
            t = TOKEN_FILE.read_text(encoding="utf-8").strip()
            return t or None
    except OSError:
        return None
    return None


# --------------------------------------------------------------------- 可用性
def available() -> tuple[bool, str]:
    """脑包在不在（不启动进程，纯文件检查）。返回 (可用, 原因)。"""
    if not VENDOR.is_dir():
        return False, "脑包缺失：%s（应为随仓 vendor/dsh-memory）" % VENDOR
    if not (VENDOR / "md_cg" / "mcp_server.py").is_file():
        return False, "脑包不完整：缺 md_cg/mcp_server.py"
    if not (VENDOR / "utf8_boot.py").is_file():
        return False, "脑包不完整：缺 utf8_boot.py"
    return True, "ok"


# --------------------------------------------------------------------- 客户端
class BrainError(RuntimeError):
    pass


class Brain:
    """一个 stdio MCP 会话（短命：用完 close）。协议见灵枢 `world/brain_store.py`（实测 23/23）。"""

    def __init__(self, root: Optional[str] = None, python: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT):
        ok, why = available()
        if not ok:
            raise BrainError(why)
        self.root = str(root or BRAIN_ROOT)
        os.makedirs(self.root, exist_ok=True)
        self.python = python or sys.executable
        self.timeout = timeout
        self._id = 0
        self._closed = False
        self.server_info: Dict = {}
        env = {
            "PYTHONPATH": str(VENDOR),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "MDCG_ROOT": self.root,
            "MDCG_STG_STATE": "1",
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "TEMP": os.environ.get("TEMP", ""),
            "TMP": os.environ.get("TMP", ""),
            "USERPROFILE": os.environ.get("USERPROFILE", ""),
            "HOMEDRIVE": os.environ.get("HOMEDRIVE", ""),
            "HOMEPATH": os.environ.get("HOMEPATH", ""),
            "APPDATA": os.environ.get("APPDATA", ""),
            "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
        }
        tok = _token()
        if tok:
            env["MDCG_TOKEN"] = tok          # 只经 env 传；不落日志、不进返回值
        else:
            # ★ 无凭据 ⇒ 脑会以只读访客身份运行：**读得到、写不了**。这是显式降级，
            #   不是故障；调用方从 write 的回执里能看到 AccessDenied 的原因。
            print("[brain] 未找到写入凭据（%s）⇒ 本次为**只读访客**模式（写操作会被脑拒绝）"
                  % TOKEN_FILE, file=sys.stderr)
        self._p = subprocess.Popen(
            [self.python, "-X", "utf8", "-m", "md_cg.mcp_server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, encoding="utf-8", errors="replace", cwd=str(VENDOR))
        self._tail = ""
        self._lock = threading.Lock()
        threading.Thread(target=self._drain, daemon=True).start()
        init = self._exchange({"jsonrpc": "2.0", "method": "initialize",
                               "params": {"protocolVersion": "2024-11-05",
                                          "capabilities": {},
                                          "clientInfo": {"name": "dsh-rag-memory",
                                                         "version": "0.1"}}})
        self.server_info = (init.get("result") or {}).get("serverInfo") or {}
        if not self.server_info:
            raise BrainError("脑侧握手无 serverInfo：%r" % (init,))
        self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # ---- 低层 ----
    def _drain(self) -> None:
        try:
            while True:
                chunk = self._p.stderr.buffer.read1(4096)     # noqa: SLF001
                if not chunk:
                    return
                self._tail = (self._tail + chunk.decode("utf-8", "replace"))[-2000:]
        except (OSError, ValueError, AttributeError):
            return

    def _rpc(self, obj: dict) -> None:
        self._p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._p.stdin.flush()

    def _read(self) -> dict:
        while True:
            line = self._p.stdout.readline()
            if not line:
                raise BrainError("脑进程关闭：" + self._tail[-400:])
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                return msg

    def _exchange(self, obj: dict) -> dict:
        with self._lock:
            if self._closed:
                raise BrainError("脑侧连接已关闭")
            self._id += 1
            obj["id"] = self._id
            box: list = []
            t = threading.Thread(target=lambda: (self._rpc(obj), box.append(self._read())),
                                 daemon=True)
            t.start()
            t.join(self.timeout)
            if t.is_alive():
                self.close()
                raise BrainError("脑侧 MCP %s 超时（%.0fs）：%s"
                                 % (obj.get("method"), self.timeout, self._tail[-200:]))
            return box[0]

    def call(self, name: str, arguments: dict) -> dict:
        """tools/call → 解析 content[0].text 的 JSON。"""
        msg = self._exchange({"jsonrpc": "2.0", "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})
        if msg.get("error"):
            raise BrainError("MCP error：%r" % (msg["error"],))
        res = msg.get("result") or {}
        text = ((res.get("content") or [{}])[0]).get("text", "")
        if res.get("isError"):
            raise BrainError("工具 %s 返回 isError：%s" % (name, text[:300]))
        try:
            return json.loads(text)
        except ValueError as e:
            raise BrainError("工具 %s 返回非 JSON：%s" % (name, text[:300])) from e

    # ---- 业务面（都薄，便于单独换） ----
    def status(self) -> dict:
        return self.call("cg", {"op": "status"})

    def info(self) -> dict:
        return self.call("cg", {"op": "info"})

    def recall(self, query: str, k: int = 5) -> dict:
        """按 token 预算召回（脑自己的 RRF 多路融合）。实测返回形如
        `{"pack":[{...}], "tokens_used":N, "budget":N, "meta":{...}}`。"""
        return self.call("mdcg_recall", {"query": query, "k": int(k)})

    def search(self, query: str, k: int = 5) -> dict:
        return self.call("mdcg_search", {"query": query, "k": int(k)})

    def remember(self, content: str, **kw) -> dict:
        return self.call("mdcg_remember", dict(content=content, **kw))

    def recent(self, limit: int = 5) -> dict:
        return self.call("cg", {"op": "recent", "limit": int(limit)})

    def recent_events(self, limit: int = 5) -> dict:
        """最近事件（脑的会话摄取窗口）。"""
        return self.call("cg", {"op": "recent", "action": "recent_events",
                                "limit": int(limit)})

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._p.terminate()
        except OSError:
            pass
        try:
            self._p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                self._p.kill()
            except OSError:
                pass


# --------------------------------------------------------------------- 门面
def with_brain(fn, timeout: float = DEFAULT_TIMEOUT):
    """开一次脑会话跑 fn，保证关闭；任何失败都折算成**显式降级**结果。"""
    ok, why = available()
    if not ok:
        return {"status": "unavailable", "reason": why}
    b = None
    t0 = time.time()
    try:
        b = Brain(timeout=timeout)
        out = fn(b)
        if isinstance(out, dict):
            out.setdefault("status", "ok")
            out["ms"] = int((time.time() - t0) * 1000)
            out["server"] = b.server_info
        return out
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "reason": "%s: %s" % (type(exc).__name__, exc),
                "ms": int((time.time() - t0) * 1000)}
    finally:
        if b is not None:
            b.close()


def recall_for_injection(query: str, k: int = 3, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """给注入面用：只取"结论类"命中，并**只带可读字段**（正文摘录 + 验证态 + id）。

    ★ 只报 `verification_state` 里**不是 expired/doubted** 的条目（存疑/过期的结论
      不该再当证据注入——那正是本库 `g_root`「引用过期」要治的病）。
    """
    def _f(b: Brain) -> dict:
        raw = b.recall(query, k=max(k * 3, 8))
        items = (raw.get("pack") or raw.get("items") or raw.get("hits")
                 or raw.get("results") or [])
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            fm = it.get("frontmatter") or {}
            # ★ 两个"state"别混：`verification_state`＝**这条结论还成不成立**；
            #   `state`＝**四态资格判定**（ACCEPT/REJECT/DEFER/BLINDSPOT）。
            #   混用会把"未验证"标成"DEFER"——判据量错对象。
            vst = fm.get("verification_state") or it.get("verification_state") or "unverified"
            if vst in ("expired", "doubted"):
                continue                      # 过期/存疑的结论不当证据注入（治"引用过期"）
            content = it.get("content") or it.get("text") or ""
            title = it.get("title") or ""
            if not title:
                m = re.search(r"^#\s*功能名[:：]\s*(.+)$", content, re.M)
                title = (m.group(1).strip() if m else content.strip().splitlines()[0][:60]
                         if content.strip() else "")
            out.append({
                "id": it.get("id") or it.get("node_id"),
                "title": title,
                "layer": fm.get("layer") or it.get("layer") or "",
                "verification_state": vst,
                "qualification": it.get("state") or "",      # 四态资格（单独一栏，不冒充验证态）
                "not_applicable": (fm.get("non_applicable_conditions") or [])[:2],
                "snippet": content[:400],
            })
            if len(out) >= k:
                break
        return {"items": out, "n": len(out), "root": b.root,
                "tokens_used": raw.get("tokens_used")}
    return with_brain(_f, timeout=timeout)


# --------------------------------------------------------------------- 探针
def _probe() -> int:
    print("=== 脑包可用性 ===")
    ok, why = available()
    print("  available =", ok, "|", why)
    if not ok:
        return 1
    print("  vendor =", VENDOR)
    print("  root   =", BRAIN_ROOT)
    with_r = with_brain(lambda b: {"server": b.server_info, "brain_status": b.status()})
    print("=== 连接与巡检 ===")
    print(json.dumps(with_r, ensure_ascii=False)[:600])
    if with_r.get("status") != "ok":
        return 1
    for nm, fn in (("info", lambda b: b.info()),
                   ("recent", lambda b: b.recent(3)),
                   ("recall", lambda b: b.recall("记忆 融合", 3))):
        r = with_brain(fn)
        print("--- %s → status=%s%s" % (nm, r.get("status"),
                                        ("  reason=" + str(r.get("reason"))) if r.get("reason") else ""))
        print("    " + json.dumps(r, ensure_ascii=False)[:400])
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(prog="memlib.brain")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--recall", default="")
    ap.add_argument("--k", type=int, default=3)
    a = ap.parse_args()
    if a.recall:
        print(json.dumps(recall_for_injection(a.recall, a.k), ensure_ascii=False, indent=1))
        sys.exit(0)
    sys.exit(_probe())
