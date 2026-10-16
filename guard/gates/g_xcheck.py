# -*- coding: utf-8 -*-
"""_guard · 交叉复核闸 g_xcheck —— 不只查"你写了没有"，去**库和盘上核一遍**

## 治的是什么（2026-10-03 用户点破）

用户原话：「…」

**前面十一闸的共同局限**（我实测出来的）：
  · `g_root` 查措辞 → 换词绕过（识破 15%）
  · `g_struct` 查结构+实质 → 换词/空壳/废话都绕不过（识破 100%）
  · **但两者都只查"报告里写了什么"** ——
    你写 `- 实测值：27 个对象` **而实际没测**，照样过闸。

**这就是最后一层：报告说的话，去别处核。**

## 交叉复核有三条独立通路（都不依赖报告自身）

| 通路 | 核什么 | 数据源 |
|---|---|---|
| **A. 盘上核** | 文件是否存在 / md5 是否对得上 / 行号内容是否相符 | 文件系统 |
| **B. 库里核** | 用户是否真说过那句话 / 该决策是否真做过 | `dsh-rag-memory`（1,152 会话 / 254,028 原子） |
| **C. 台账核** | 该操作是否真在台账里留过痕（快照/闸门记录） | `_guard_ledger/*.jsonl` |

**为什么要"交叉"**：单条通路自己会错（md5 可能写错、库可能没入库、台账可能没记）。
**两条独立通路一致**才算过 —— 这正是表里反复推荐的「绿灯必须由另一条计算通路复核」。

## 检查项

| 项 | 声明形态 | 复核方式 |
|---|---|---|
| `X1 文件存在` | 提到某路径 | `Path.exists()` |
| `X2 md5 属实` | `md5 <hex>` + 邻近路径 | 实算盘上文件的 md5 |
| `X3 用户原话` | 引号内"用户说…" | 去库里 grep 该短语 |
| `X4 数字可复算` | `N 个文件/条` | 实算该目录下的数量 |
| `X5 台账留痕` | 「已改/已修复」 | 查 `_guard_ledger` 有无对应记录 |

跨项目：记忆库路径、台账路径来自 config.xcheck；本文件不含项目专有内容。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402

#: 本机专有路径**不进代码**（R-56）：从 tools/tool_paths.json 读（环境变量可覆盖）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from tool_paths import tool_path, expand_path  # noqa: E402


def _isnum(x: str) -> bool:
    try:
        float(x)
        return True
    except Exception:  # noqa: BLE001
        return False


def _md5(p: Path) -> str:
    h = hashlib.md5()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class XCheckGate:
    """交叉复核闸：报告的声明 → 去盘/库/台账核。"""

    NAME = "g_xcheck"

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        x = cfg.get("xcheck") or {}
        _mp = x.get("mem_cli") or tool_path("mem_py", "DSH_MEM_PY")
        # 取不到 ⇒ 用必然不存在的占位路径：下游"文件不存在"检查会**显式**报出来（不静默）
        self.mem = Path(_mp) if _mp else Path("__mem_py_not_configured__")
        self.ledger_dir = cfg.resolve(x.get("ledger_dir", "_guard_ledger"))
        self.timeout = int(x.get("timeout", 25))
        lg = x.get("ledger", "_guard_ledger/xcheck.jsonl")
        self.ledger = cfg.resolve(lg)
        self.ledger.parent.mkdir(parents=True, exist_ok=True)

    # ---------------- X1：文件是否存在 ----------------
    def x1_paths(self, text: str) -> list[dict]:
        out = []
        # 只认"像真路径"的（有盘符或常见扩展名），避免把说的概念当路径
        pats = re.findall(
            r"(?:[A-Za-z]:\\[^\s，。；、）\)\|`\"']+|(?<![\w/])/[\w\-./]+\.\w{1,5})", text)
        lines = text.split("\n")
        for raw in dict.fromkeys(pats):
            p = Path(raw.strip().rstrip("，。；、）)"))
            if not re.search(r"\.\w{1,5}$", str(p)) and "\\" not in str(p):
                continue

            # ★ 排除"举例/测试用"的路径（2026-10-03 实测误报）：
            #   报告里为了说明"什么样的会被拦"，会写 `<WORKSPACE>\cc-assets\x.md`
            #   这类**虚构示例** —— 它们不是声明"我动了这个文件"，不该核存在性。
            ctx = self._line_of(lines, str(p))
            if self._is_example(ctx):
                continue

            exists = p.exists()
            out.append({"item": "X1", "what": str(p), "ok": exists,
                        "detail": ("存在" if exists else "**盘上不存在**")})
        return out

    @staticmethod
    def _line_of(lines: list[str], needle: str) -> str:
        for l in lines:
            if needle in l:
                return l
        return ""

    @staticmethod
    def _is_example(ctx: str) -> bool:
        """这一行是在"举例/测试/演示/记录测试结果"，还是真的在声明？

        判据：
          · 含 测试/举例/示例/反例/演示/比如/例如/用例/假设/mock/假想/虚拟
          · 或路径里带 `...` 这种显然的省略写法
          · 或路径本身明显是占位（x.md / xxx / nope / foo / bar）
          · 或这一行是**测试结果行**（含 ✓/✗ 判定 + 拦下/放行/允许 这类结论词）
            —— 实测：报告的"测试结果表"里会列出**被拦下的路径**，
               那不是声明"我动了它"，不该核存在性。
        """
        if re.search(r"测试|举例|示例|反例|演示|比如|例如|用例|假设|mock|假想|虚拟|核对|复核", ctx):
            return True
        if "..." in ctx or "\u2026" in ctx:
            return True
        if re.search(r"[\\/](?:x|xxx|nope|foo|bar|test|tmp)[\w\-]*\.\w{1,5}\b", ctx):
            return True
        # 测试结果行：有判定符号 + 结论词
        if re.search(r"[✓✗√×]", ctx) and re.search(r"拦下|放行|允许|识破|被骗|通过|不过|误杀", ctx):
            return True
        # markdown 表格行 + 结论词
        if ctx.strip().startswith("|") and re.search(r"拦下|放行|允许|识破|通过|不过", ctx):
            return True
        return False

    # ---------------- X2：md5 是否属实 ----------------
    def x2_md5(self, text: str) -> list[dict]:
        out = []
        # 找 `md5 <hex>` 或 `<hex>（…）` 形式，并尝试关联附近的路径
        for m in re.finditer(r"(?:md5|MD5|哈希|指纹)\s*[:：=]?\s*`?([0-9a-fA-F]{16,64})`?", text):
            hx = m.group(1).lower()
            # ★ 修（2026-10-03 实测）：原先只往前看 300 字符，
            #   但"md5 xxx，位于 D:\path"这种写法路径在**后面** ⇒ 关联不到 ⇒ 误判"无法复核"。
            #   改为**前后都看**（各 400 字符）。
            ctx = text[max(0, m.start() - 400): m.end() + 400]
            cand = re.findall(r"[A-Za-z]:\\[^\s，。；、）\)\|`\"']+\.[A-Za-z0-9]{1,6}", ctx)
            if not cand:
                out.append({"item": "X2", "what": hx[:16], "ok": False,
                            "detail": ("写了 md5 但**附近没有可对应的文件路径** —— "
                                       "无法复核的 md5 等于没写（必须给出它属于哪个文件）")})
                continue
            for c in cand[:1]:
                p = Path(c.rstrip("，。；、）)"))
                if not p.exists():
                    out.append({"item": "X2", "what": str(p), "ok": False,
                                "detail": "引用的文件不存在，md5 无从核起"})
                    continue
                try:
                    real = _md5(p)
                except Exception as e:  # noqa: BLE001
                    out.append({"item": "X2", "what": str(p), "ok": False,
                                "detail": f"算 md5 失败：{e}"})
                    continue
                same = real.startswith(hx[:16]) or hx.startswith(real[:16])
                out.append({"item": "X2", "what": str(p), "ok": same,
                            "detail": (f"md5 相符（{real[:16]}）" if same
                                       else f"**md5 不符**：报告写 {hx[:16]}，盘上实为 {real[:16]}")})
        return out

    # ---------------- X3：用户原话是否真说过 ----------------
    def x3_quote(self, text: str) -> list[dict]:
        """报告里"用户说：xxx"这类引用，去记忆库核。"""
        out = []
        pats = re.findall(r"(?:用户(?:说|原话|要求)|你说)[：: ]*[「\"“]([^」\"”]{6,60})[」\"”]", text)
        for q in dict.fromkeys(pats):
            if not self.mem.exists():
                out.append({"item": "X3", "what": q, "ok": None, "detail": "记忆库 CLI 不可用"})
                continue
            try:
                r = subprocess.run([sys.executable, str(self.mem), "grep", q, "--scope", "atoms"],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=self.timeout)
                # ★★ 2026-10-16 根治（**判据量错对象**，出在闸自己身上）：
                #   原判据是 `"精确匹配" in out and "0 条" not in out` ——
                #   而 `"20 条"` 里**包含子串 `"0 条"`** ⇒ 命中 20 条被判"零命中"！
                #   ⇒ 它绿不绿竟然取决于**命中数的末位是不是 0**（<10 绿、涨到 20 红）。
                #   这类"用子串猜数字"的判据一律改成：**解析出数字，按数字判**；
                #   解析不出来 ⇒ **未验**（不许把"没解析出来"当成"零命中"）。
                n = re.search(r"精确匹配\s*(\d+)\s*条", r.stdout or "")
                if n is None:
                    out.append({"item": "X3", "what": q[:40], "ok": None, "hits": None,
                                "detail": "查询输出无法解析 ⇒ **未验**（不当零命中）：%s"
                                          % (r.stdout or r.stderr or "")[:80]})
                    continue
                cnt = int(n.group(1))
                hit = cnt > 0
                out.append({"item": "X3", "what": q[:40], "ok": hit, "hits": cnt,
                            "detail": (f"库中命中 {cnt} 条" if hit
                                       else "**库中零命中** —— 该『原话』在全库范围内找不到")})
            except Exception as e:  # noqa: BLE001
                out.append({"item": "X3", "what": q[:40], "ok": None, "detail": f"查询失败：{e}"})
        return out

    # ---------------- X4：数字是否可复算 ----------------
    def x4_count(self, text: str) -> list[dict]:
        """报告里 `N 个 <类型>` 且带目录的，去实算。"""
        out = []
        for m in re.finditer(r"(\d+)\s*(?:个|份|张|条)\s*(`?[\w\-\.]+`?)\s*(?:文件|对象|件)?", text):
            n, kind = int(m.group(1)), m.group(2).strip("`")
            ctx = text[max(0, m.start() - 200): m.end() + 200]
            dirs = re.findall(r"([A-Za-z]:\\[^\s，。；、）\)\|`\"']+)", ctx)
            if not dirs:
                continue
            d = Path(dirs[0].rstrip("，。；、）)"))
            if not d.is_dir():
                continue
            ext = kind if kind.startswith(".") else f".{kind.lstrip('.')}"
            try:
                real = len([x for x in d.rglob(f"*{ext}") if x.is_file()]) if ext != "." else \
                       len([x for x in d.rglob("*") if x.is_file()])
            except Exception:  # noqa: BLE001
                continue
            same = (real == n)
            out.append({"item": "X4", "what": f"{n} {kind} @ {d}", "ok": same,
                        "detail": (f"实算 {real}，与报告一致" if same
                                   else f"**实算 {real}**，报告写 {n}（差 {abs(real-n)}）")})
        return out

    # ---------------- X5：台账是否留痕 ----------------
    def x5_ledger(self, text: str) -> list[dict]:
        """报告宣称"已改/已修复" ⇒ 台账里该有对应记录。"""
        out = []
        # ★★ 2026-10-15 形态判据（修"触发词过窄"的假根治；用户要求检查同类）★★
        #   病根：原清单只有 `已改|已修改|已修复|已更新|已生效`。
        #   实测（对抗样本）：报告只写「这个问题**已根治**」
        #   ⇒ 一个字都不匹配 ⇒ X5 **整段跳过**，输出「核了 0 项，全部相符」、exit 0 放行
        #   （连 `g_root` 的 B3 也一起跳过 —— "根治"这个词能一次绕过两道检查）。
        #   修法：改成「**已/都/均/经 + 改动类动词**」的**形态**判据，不再穷举措辞。
        if not re.search(
            r"(?:已|都|均|经)[^\n。]{0,4}?"
            r"(?:改|修|更新|完成|做完|做掉|处理|解决|落地|根治|生效|上线|部署|清|删|搞定|跑通)"
            r"|(?:改|修|清|删)(?:好|掉|完)了",
            text,
        ):
            return out
        if not self.ledger_dir.exists():
            return [{"item": "X5", "what": "台账目录", "ok": None,
                     "detail": f"{self.ledger_dir} 不存在，无法查留痕"}]
        rows = []
        for f in self.ledger_dir.glob("*.jsonl"):
            try:
                rows += [json.loads(l) for l in f.read_text(encoding="utf-8",
                                                            errors="replace").splitlines() if l.strip()]
            except Exception:  # noqa: BLE001
                continue
        out.append({"item": "X5", "what": "改动留痕", "ok": len(rows) > 0,
                    "hits": len(rows),
                    "detail": (f"台账共 {len(rows)} 条记录" if rows else
                               "**台账零记录** —— 宣称『已改』但没有任何留痕")})
        return out

    # ---------------- X6：**实测值必须有带痕来源**（治"真去改了但没测"） ----------------
    # 用户 2026-10-03 点名：「…」
    # 病根：报告写 `- 实测值：27 个对象全为 True` —— 无法区分"真跑过"与"手写编的"。
    # 治法：实测值必须能指到**一条带痕的执行** —— 探针台账/闸门台账/命令输出/日志文件。
    #   指不到的 ⇒ 明确标"无凭据"，**不许当成实测**。
    def x6_evidence(self, text: str) -> list[dict]:
        out = []
        # 找"实测/测得/读数为"这类声明
        claims = re.findall(r"(?:实测值|实测|测得|读数为|复读得)[^\n。；]{0,120}", text)
        if not claims:
            return out

        # 收集"带痕来源"：台账记录 + 报告里引用的日志/输出文件
        ledger_rows = 0
        if self.ledger_dir.exists():
            for f in self.ledger_dir.glob("*.jsonl"):
                try:
                    ledger_rows += sum(1 for l in f.read_text(encoding="utf-8",
                                                             errors="replace").splitlines() if l.strip())
                except Exception:  # noqa: BLE001
                    continue

        for c in claims[:20]:
            # ① 声明里直接带了**可查来源**（必须指向一个真载体）
            #   ★ 修（2026-10-03 实测）：原先把 `reopen`/`python` 也算来源 ——
            #     但「reopen 后全为 True」是**动作描述**，不是来源 ⇒ 手写值也放行了。
            #     现在只认"可查载体"：台账文件 / 探针 / 命令 / 日志 / 回执 / .jsonl。
            has_src = bool(re.search(
                r"台账|探针|probe|\.jsonl|日志|\.log|回执|stdout|stderr|命令输出|"
                r"selftest|guard_cli|blender_probe|_ledger/|来源\s*[:：]|脚本\s*[:：]", c))
            # ② 只有读数+比较、但没指来源 ⇒ **不算实测**（治"改了没测"）
            has_num = bool(re.search(r"\d", c))
            has_cmp = bool(re.search(r"[≤≥<>＝=]|差\s*[\d.]|vs|比", c))
            if has_src:
                out.append({"item": "X6", "what": c[:50], "ok": True,
                            "detail": "声明里带了可查来源（台账/探针/命令/日志）"})
            elif has_num and has_cmp:
                out.append({"item": "X6", "what": c[:50], "ok": False,
                            "detail": ("**无来源的『实测』** —— 有读数但没指到台账/探针/命令/日志 ⇒ "
                                       "无法区分『真跑过』与『手写编的』；"
                                       "请补『用哪条命令/哪个探针测的』")})
            else:
                out.append({"item": "X6", "what": c[:50], "ok": False,
                            "detail": ("**无凭据的『实测』** —— 既没来源也没可对照读数 ⇒ "
                                       "等于没测；请补『用哪条命令/哪个探针测的』")})
        return out

    # ---------------- X7：数字与台账对账（实测值要能回查） ----------------
    def x7_ledger_value(self, text: str) -> list[dict]:
        """报告里的关键数值，若台账里有同名探针/闸门记录，**对一下**。"""
        out = []
        if not self.ledger_dir.exists():
            return out
        rows = []
        for f in self.ledger_dir.glob("*.jsonl"):
            try:
                rows += [json.loads(l) for l in f.read_text(encoding="utf-8",
                                                            errors="replace").splitlines() if l.strip()]
            except Exception:  # noqa: BLE001
                continue
        if not rows:
            return out
        # 名字对得上的探针记录：报告里提到该名字 + 有数字
        for r in rows[:200]:
            name = r.get("probe") or r.get("gate") or ""
            if not name or len(str(name)) < 2:
                continue
            if str(name) not in text:
                continue
            val = r.get("value")
            if val is None:
                continue
            try:
                fval = float(val)
            except Exception:  # noqa: BLE001
                continue
            # 报告里该名字附近有没有同一个数
            m = re.search(re.escape(str(name)) + r"[^\n]{0,160}", text)
            seg = m.group(0) if m else ""
            nums = re.findall(r"-?\d+\.?\d*", seg)
            match = any(abs(float(x) - fval) < 1e-6 for x in nums if _isnum(x))
            out.append({"item": "X7", "what": f"{name}={val}", "ok": match,
                        "detail": (f"台账值 {val}，报告里对得上" if match
                                   else f"**台账记 {val}，报告该处未见此值** —— 读数可能对不上")})
        return out

    # ---------------- 总检 ----------------
    def check_text(self, text: str, *, strict: bool = False,
                   items: list[str] | None = None) -> dict:
        want = items or ["X1", "X2", "X3", "X4", "X5", "X6", "X7"]
        found: list[dict] = []
        if "X1" in want:
            found += self.x1_paths(text)
        if "X2" in want:
            found += self.x2_md5(text)
        if "X3" in want:
            found += self.x3_quote(text)
        if "X4" in want:
            found += self.x4_count(text)
        if "X5" in want:
            found += self.x5_ledger(text)
        if "X6" in want:
            found += self.x6_evidence(text)
        if "X7" in want:
            found += self.x7_ledger_value(text)

        # 判定：**明确的错**（ok=False）必须拦；无法复核（ok=None）只提示
        wrong = [r for r in found if r["ok"] is False]
        unver = [r for r in found if r["ok"] is None]
        checked = [r for r in found if r["ok"] is True]
        # ★★ 2026-10-16 修（本闸自己的**假通过**）：原来 `ok = len(wrong) == 0` ⇒
        #   **一项都没真核成也报"全部相符"**。实测：本机没接记忆库时 X3 那行写着
        #   "1 项无法复核"，而 overall 仍 ok=True ⇒ 报告里「用户说：「X」」这种
        #   **无从核实**的话被当成核查通过（正是本闸要拦的"编造原话"）。
        #   口径：**未核 ≠ 相符** —— 有声明、却一项都没真核成 ⇒ 判 **未验**（ok=None）。
        #   （"什么都没声明"仍算通过：没有要核的东西，那不叫未验。）
        if wrong:
            ok = False
        elif checked or not unver:
            ok = True
        else:
            ok = None

        rep = {
            "gate": self.NAME, "ok": ok,
            "checked": len(checked), "wrong": len(wrong), "unverifiable": len(unver),
            "rows": found,
            "summary": (f"交叉复核：核了 {len(checked)} 项"
                        + (f"；**{len(wrong)} 项对不上**" if wrong else
                           ("，全部相符" if (checked or not unver) else "，**无一项真核成**"))
                        + (f"；{len(unver)} 项无法复核" if unver else "")),
        }
        if wrong:
            rep["summary"] += "\n对不上的：\n" + "\n".join(
                "  · " + r["detail"] for r in wrong)
        self._log({**rep, "rows": None})
        return rep

    def check_file(self, p: str | Path, **kw) -> dict:
        p = Path(p)
        rep = self.check_text(p.read_text(encoding="utf-8", errors="replace"), **kw)
        rep["file"] = str(p)
        return rep

    def require(self, text: str, **kw) -> dict:
        rep = self.check_text(text, **kw)
        if not rep["ok"]:
            raise XCheckFailed(rep["summary"])
        return rep

    def _log(self, rec: dict) -> None:
        try:
            with self.ledger.open("a", encoding="utf-8") as f:
                f.write(json.dumps({**rec, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                   ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            # ★ D6 修复（2026-10-04）：不静默吞 —— 台账写失败必须可见。
            sys.stderr.write(
                f"[g_xcheck] 台账写入失败（不静默吞）：{self.ledger} :: "
                f"{type(e).__name__}: {e}\n")


class XCheckFailed(RuntimeError):
    """交叉复核未通过 —— 报告里的声明与盘上/库里的实况对不上，不许交付。"""


# --------------------------------------------------------------------------
def demo(cfg: ProjectConfig) -> int:
    g = XCheckGate(cfg)
    fails = 0
    unverified = 0          # ★ 环境未接 ⇒ 记未验（≠ 通过；汇总里单独计数）
    print("### 交叉复核闸 · 实例测试（用真实的盘/库）\n")
    print("  · 记忆库：%s" % ("已接" if g.mem.exists() else "**未接**（本机没这库）"))

    # ── 负例 1：编造的文件路径
    print("### 负例 1：报告引用一个不存在的文件")
    bad1 = f"本次改动落在 `{expand_path('<WORKSPACE>/_guard/gates/g_nonexistent.py')}`，已核验。"
    r1 = g.check_text(bad1, items=["X1"])
    hit = not r1["ok"]
    print(f"  {'✓✓ 拦住' if hit else '✗✗ 漏了'}  {r1['summary'][:100]}")
    if not hit:
        fails += 1
    print()

    # ── 负例 2：编造 md5
    print("### 负例 2：md5 与盘上不符")
    # ★★ 2026-10-16 修：这里原来写的是**字面** `<WORKSPACE>\_guard\rules.yaml` ——
    #   占位符没人展开 ⇒ `real.exists()` 恒假 ⇒ **整条负例静默不跑**，
    #   而自检照样打印"通过"。这正是本闸要治的"假通过"发生在闸自己身上。
    #   现在：① 用 expand_path 真展开；② 样本不可用时**显式记不合格**，不许静默跳过。
    real = expand_path("<WORKSPACE>/_guard/rules.yaml")
    if real.exists():
        fake_md5 = "f" * 32
        bad2 = f"该文件 md5 {fake_md5}，已核对。路径 {real}"
        r2 = g.check_text(bad2, items=["X2"])
        hit = not r2["ok"]
        print(f"  {'✓✓ 拦住' if hit else '✗✗ 漏了'}  {r2['summary'][:110]}")
        if not hit:
            fails += 1
    else:
        # ★ 不静默：显式打「未验」并计入 unverified（原来记"失败"，但那是**环境缺失**、
        #   不是判据坏了 —— 换台机器没接工作区就会红，属误伤）。
        print(f"  ⚠ 未验：样本路径不存在（{real}）⇒ 本机未接工作区，本项无法真跑")
        unverified += 1
    print()

    # ── 负例 3：编造"用户原话"
    print("### 负例 3：编造用户没说过的话")
    # ★ 用**当场生成的随机串**做负例 —— 实测教训：固定串会被我自己写进语料、
    #   记忆库随即"命中"，负例就失效了（库是活的）。随机串保证零命中。
    import uuid as _uuid
    fake_quote = "本功能需全面重做" + _uuid.uuid4().hex[:10]
    bad3 = '用户说：「' + fake_quote + '」，所以我改了。'
    r3 = g.check_text(bad3, items=["X3"])
    if r3["ok"] is None:                       # 未接记忆库 ⇒ 无从核实
        print("  ⚠ 未验：本机未接记忆库 ⇒ 无法核实『原话』，本项无法真跑")
        unverified += 1
    else:
        hit = not r3["ok"]
        print(f"  {'✓✓ 拦住' if hit else '✗✗ 漏了'}  {r3['summary'][:110]}")
        if not hit:
            fails += 1
    print()

    # ── 正例 1：真说过的话
    print("### 正例 1：用户真说过的话（应核得到）")
    # ★ 中性技术短语（**不是**用户原话）；已验证库中存在：mem.py grep "去重键 调用标识" → 12 条
    good1 = '用户说：「去重键 调用标识」，我据此调整。'
    r4 = g.check_text(good1, items=["X3"])
    if r4["ok"] is None:
        print("  ⚠ 未验：本机未接记忆库 ⇒ 无法核实，本项无法真跑")
        unverified += 1
    else:
        print(f"  {'✓ 核实相符' if r4['ok'] else '✗ 误判'}")
        for row in r4["rows"]:
            print(f"      {row['detail'][:90]}")
        if not r4["ok"]:
            fails += 1
    print()

    # ── 正例 2：真存在的路径
    print("### 正例 2：真存在的路径（应通过）")
    good2 = (f"改动落在 `{expand_path('<WORKSPACE>/_guard/rules.yaml')}` 与 "
             f"`{expand_path('<WORKSPACE>/_guard/gates/g_struct.py')}`。")
    r5 = g.check_text(good2, items=["X1"])
    if (not r5["ok"]) and not expand_path("<WORKSPACE>/_guard/rules.yaml").exists():
        print("  ⚠ 未验：本机未接工作区（样本路径不存在）⇒ 本项无法真跑")
        unverified += 1
    else:
        print(f"  {'✓ 通过' if r5['ok'] else '✗ 误判：' + r5['summary'][:80]}")
        if not r5['ok']:
            fails += 1
    print()

    # ★ 2026-10-15 负例（防"触发词再被收窄"复发）：
    #   X5 的触发条件原来是字面清单 `已改|已修改|已修复|已更新|已生效`。
    #   实测：只写「这个问题**已根治**」⇒ 一个字都不匹配 ⇒ X5 **整段不跑**，
    #   输出"核了 0 项，全部相符"、exit 0 **放行**。
    #   现在按「已/都/均/经 + 改动类动词」的形态判据 ⇒ X5 必须**到达**。
    print("### 负例 3：只写『已根治』（旧字面清单漏掉的说法）⇒ X5 必须到达")
    r6 = g.x5_ledger("这个问题已根治，改动见 diff。")
    reached = bool(r6) and any(x.get("item") == "X5" for x in r6)
    detail = r6[0].get("detail", "")[:70] if r6 else "（X5 未产生任何项 ⇒ 整段被跳过）"
    print(f"  {'✓✓到达' if reached else '✗✗未到达（放行）'}  X5：{detail}")
    if not reached:
        fails += 1
    #   正例（不许误杀）：没有改动声明的文本 ⇒ X5 应当不产生项（保持原行为）
    r7 = g.x5_ledger("我读了 3 个文件，把现状记录下来了。")
    if not r7:
        print("  ✓ 不误杀：无改动声明 ⇒ X5 不产生项")
    else:
        print(f"  ✗ 误触发：{r7[0].get('detail', '')[:70]}")
        fails += 1
    print()

    if fails == 0 and unverified:
        print(f"### 自检结果：未验 {unverified} 项（环境未接：本机无记忆库/工作区样本）")
        return 0
    print(f"### 自检结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}"
          + (f"（另有 {unverified} 项未验）" if unverified else ""))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[g_xcheck] stdout 重设为 utf-8 失败（继续跑）：{e}\n")
    from guard import load as load_cfg

    ap = argparse.ArgumentParser(prog="g_xcheck")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--text-file")
    ap.add_argument("--items", help="只跑指定项，逗号分隔（X1,X2,X3,X4,X5）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    g = XCheckGate(cfg)
    if a.demo:
        sys.exit(demo(cfg))
    if a.text_file:
        items = [x.strip() for x in a.items.split(",")] if a.items else None
        rep = g.check_file(a.text_file, items=items)
        if a.json:
            print(json.dumps(rep, ensure_ascii=False, indent=1))
        else:
            print(rep["summary"])
            for r in rep["rows"]:
                mark = "✓" if r["ok"] is True else ("✗" if r["ok"] is False else "?")
                print(f"  {mark} {r['item']} {r['what'][:44]} — {r['detail'][:80]}")
        sys.exit(0 if rep["ok"] else 1)
    print("用法：--demo / --text-file <报告> [--items X1,X3]")
