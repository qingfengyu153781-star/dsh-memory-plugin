# -*- coding: utf-8 -*-
"""_guard · 语法/格式闸 g_syntax —— 改动过的 `.py` / `.json` 必须当场过编译与解析（R-63）

## 为什么要有它（不是假想，是一天内的实测次数）

2026-10-15 一天内，我在**同一类错误**上栽了 **6 + 4** 次：

| 次数 | 形态 | 后果 |
|---|---|---|
| **6 次** | 中文串里嵌了 **ASCII 双引号**（`print("…写成"不提供"却不写原因…")`） | `SyntaxError` ⇒ 脚本跑不起来；其中 2 次让**闸自己**瘫掉 |
| **4 次** | JSON 写坏（真实换行没转义 / 中英引号混用） | `g_prov` 对登记表解析失败是 **fail-closed** ⇒ **闸自己判红** |

每一次都是"**跑起来报错才发现**"——也就是说，**只要没人跑，它就静静躺在那里**。
这不是记性问题（一天犯 10 次说明记性没用），是**没有自动检查**。

## 判据（形态判定）

| id | 判据 | 量的对象 |
|---|---|---|
| S1 | 改动过的每个 `.py` 必须 `compile()` 通过 | 源码 |
| S2 | 改动过的每个 `.json` 必须 `json.loads()` 通过 | 数据面 |
| S3 | 读不了的文件（权限/编码）⇒ **可见报红**，不许当"跳过" | 文件系统 |

**为什么用 `compile()` 而不是 `py_compile`**：等价地抓 `SyntaxError`，但**不写 `.pyc`**
（写盘会污染工作区，且本机曾因 `.pyc` 陈旧踩过坑）。

## 用法

    python _guard/gates/g_syntax.py --files a.py b.json   # 体检指定文件
    python _guard/gates/g_syntax.py --changed "a.py;b.json"
    python _guard/gates/g_syntax.py --demo                # 自证：好文件必过、坏文件必红

退出码：0 = 全过；1 = 有不合格项（含"读不了"的 fail-closed）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402


class SyntaxGate:
    NAME = "g_syntax"

    def __init__(self, cfg: ProjectConfig | None = None):
        self.cfg = cfg

    # ------------------------------------------------------------------
    def recent(self, minutes: int = 15, roots: list[str] | None = None) -> list[str]:
        """**近期写入体检**：扫最近 N 分钟内被改过的 `.py/.json/.js`（**来源无关**）。

        ★ 为什么要有它（2026-10-16 立，同日第 4 次事故之后）：我用 bash heredoc 拼 `.py` 时，
        shell 与 python 的双重转义会把 `\\n` 变成真换行 ⇒ 写坏文件（当天 4 次，全靠"跑起来才发现"）。
        `--files` 只在**我知道自己动了哪些文件**时管用；`--recent` 是兜底：不管谁用什么方式写的
        （heredoc / python / 编辑器 / 后台进程），只要落盘就体检 ⇒ 把"记性"换成"机制"。
        """
        import time as _t
        cut = _t.time() - minutes * 60
        exts = (".py", ".json", ".js", ".mjs", ".cjs")
        out = []
        for b in [Path(r) for r in (roots or [str(Path(__file__).resolve().parent.parent)])]:
            if not b.exists():
                continue
            for p in b.rglob("*"):
                try:
                    if not p.is_file() or p.suffix.lower() not in exts:
                        continue
                    if "__pycache__" in p.parts or "_guard_vault" in p.parts:
                        continue
                    if p.stat().st_mtime >= cut:
                        out.append(str(p))
                except OSError:
                    continue
        return sorted(out)

    def check(self, files: list[str]) -> dict:
        rows, bad, checked = [], [], 0
        for raw in files or []:
            p = Path(str(raw))
            if not p.is_file():
                continue                      # 不存在的路径不算改动面（由别的闸管）
            suf = p.suffix.lower()
            if suf not in (".py", ".json", ".js", ".mjs", ".cjs"):
                continue
            checked += 1
            try:
                if suf == ".py":
                    compile(p.read_text(encoding="utf-8", errors="replace"),
                            str(p), "exec")
                    rows.append({"id": "S1", "file": str(p), "ok": True})
                elif suf in (".js", ".mjs", ".cjs"):
                    # S3：JS 用 node --check（本插件是 JS，语法病不该有盲区）
                    import subprocess
                    r = subprocess.run(["node", "--check", str(p)],
                                       capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=60)
                    if r.returncode != 0:
                        raise RuntimeError((r.stderr or r.stdout or "").strip()[-200:])
                    rows.append({"id": "S3", "file": str(p), "ok": True})
                else:
                    json.loads(p.read_text(encoding="utf-8"))
                    rows.append({"id": "S2", "file": str(p), "ok": True})
            except Exception as exc:  # noqa: BLE001
                # ★ 不静默：读不了/编译不了**都是红**，不许当"跳过"
                msg = str(exc).splitlines()[-1][:200] if str(exc) else repr(exc)
                rows.append({"id": "S1" if suf == ".py" else "S2", "file": str(p),
                             "ok": False, "detail": msg})
                bad.append("%s ← %s" % (p, msg))
                print("[g_syntax] 不合格：%s ← %s" % (p, msg), file=sys.stderr)
        if bad:
            return {"ok": False, "rows": rows, "checked": checked, "bad": bad,
                    "summary": "语法/格式闸：**%d/%d 个改动文件不合格**" % (len(bad), checked)}
        return {"ok": True, "rows": rows, "checked": checked, "bad": [],
                "summary": "语法/格式闸：改动的 %d 个 .py/.json 均合格" % checked}

    def render(self, rep: dict) -> str:
        lines = [rep["summary"]]
        for b in rep.get("bad") or []:
            lines.append("  ✗ " + b)
        return "\n".join(lines)


# ====================================================================== demo
def demo(cfg: ProjectConfig | None = None) -> int:
    """负例自证：造好文件（必过）+ 造**两种坏文件**（必须都红）。"""
    import tempfile
    print("### 语法/格式闸 g_syntax 演示")
    g = SyntaxGate(cfg)
    d = Path(tempfile.mkdtemp(prefix="gsyntax_"))
    good_py = d / "good.py"
    good_py.write_text("x = 1\n", encoding="utf-8")
    good_json = d / "good.json"
    good_json.write_text('{"a": 1}', encoding="utf-8")
    # 坏 .py：**正是我今天犯的那个形态**（中文串里嵌 ASCII 双引号）
    bad_py = d / "bad.py"
    bad_py.write_text('print("写成"不提供"却不写原因")\n', encoding="utf-8")
    # 坏 .json：字符串里写了真实换行（JSON 不允许）
    bad_json = d / "bad.json"
    bad_json.write_text('{"note": "第一行\n第二行"}', encoding="utf-8")
    # 坏 .js：括号没闭合（JS 也要拦——本插件就是 JS）
    bad_js = d / "bad.js"
    bad_js.write_text("export function f( { return 1 }\n", encoding="utf-8")
    fails = 0

    rep = g.check([str(good_py), str(good_json)])
    ok = rep["ok"] and rep["checked"] == 2
    print("  %s 好文件（1 py + 1 json）⇒ %s" % ("✓ 过" if ok else "✗ 误杀",
                                              rep["summary"]))
    if not ok:
        fails += 1

    rep = g.check([str(bad_py)])
    hit = (not rep["ok"]) and rep["checked"] == 1
    print("  %s 坏 .py（中文串嵌 ASCII 引号）⇒ %s"
          % ("✓✓拦下" if hit else "✗✗ **没拦住**", rep["summary"]))
    if not hit:
        fails += 1

    rep = g.check([str(bad_json)])
    hit = not rep["ok"]
    print("  %s 坏 .json（字符串里真实换行）⇒ %s"
          % ("✓✓拦下" if hit else "✗✗ **没拦住**", rep["summary"]))
    if not hit:
        fails += 1

    rep = g.check([str(bad_js)])
    hit = not rep["ok"]
    print("  %s 坏 .js（括号没闭合）⇒ %s"
          % ("✓✓拦下" if hit else "✗✗ **没拦住**", rep["summary"]))
    if not hit:
        fails += 1

    rep = g.check([str(d / "不存在.py"), str(d / "a.txt")])
    ok = rep["ok"] and rep["checked"] == 0
    print("  %s 不存在/非目标后缀 ⇒ 不算改动面（checked=%d）"
          % ("✓ 不误判" if ok else "✗ 误判", rep["checked"]))
    if not ok:
        fails += 1

    print("\n### 自检结果：%s" % ("全部通过（喂坏输入都拦下了）" if fails == 0
                                else "%d 项失败" % fails))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        print("[g_syntax] stdout 重设为 utf-8 失败（继续跑）：%r" % (e,), file=sys.stderr)
    from guard import load as load_cfg

    ap = argparse.ArgumentParser(prog="g_syntax")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--files", nargs="*", default=[], help="要体检的文件")
    ap.add_argument("--recent", type=int, default=0,
                    help="**来源无关兜底**：体检最近 N 分钟内改过的 .py/.json/.js（0=不扫）")
    ap.add_argument("--changed", default="", help="分号或逗号分隔的改动文件清单")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    if a.demo:
        sys.exit(demo(cfg))
    files = list(a.files)
    if a.recent:
        g = SyntaxGate(cfg)
        files += [x for x in g.recent(a.recent) if x not in files]
        if not files:
            print("近 %d 分钟内没有 .py/.json/.js 被改过（无对象可体检）" % a.recent)
            sys.exit(0)
    if a.changed:
        files += [x for x in a.changed.replace(",", ";").split(";") if x.strip()]
    rep = SyntaxGate(cfg).check(files)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(SyntaxGate(cfg).render(rep))
    sys.exit(0 if rep["ok"] else 1)
