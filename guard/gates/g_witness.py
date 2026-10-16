# -*- coding: utf-8 -*-
"""_guard · 独立见证者闸 g_witness —— 核验器**不得与被检对象同源**（R-68）

## 判据来源（学自 dsh-bounty 的思路，代码为自研；该仓 license=None，不可照搬）

dsh-bounty 的账本不变量自审件里明写：**「本文件不 import bounty_board，id 公式独立重实现
——当独立见证者，避免同错同绿」**。这是"判据与被检对象同源 ⇒ 一起错一起绿"的**机械对策**。

**为什么本仓需要它**：我们的 `g_prov` 读 `memlib` 写的库、`g_syntax` 用与产物同一套
解析器判断产物、`g_integ` 跑的还是被检对象自己的测试——**同源判据给不出独立证据**。
它不会让判据变准，但能让"**这是自证**"这件事无法被隐藏。

## 判据

| id | 判据 | 量的对象 |
|---|---|---|
| W1 | 核验器**不得 import 被检对象模块**（含 `importlib.import_module("…")` 字面量） | AST 静态扫描 |
| W2 | 核验器必须**自带自证入口**（`--demo` 或 `--selftest`），否则"没有负例自证的核验器不可信" | 源码字符串 |
| W3 | 声明了 `subject_outputs` 却**只读它、无第二来源** ⇒ 未验（不等于通过） | 配置声明 |

## 用法

    python _guard/gates/g_witness.py            # 读 tools/witness.json 逐对判
    python _guard/gates/g_witness.py --demo     # 负例自证（检查器 import 被检对象 ⇒ 必红）

`tools/witness.json`：
```json
{"pairs": [{"checker": "gates/g_prov.py", "subject": ["src/memlib/dsh.py"],
            "subject_outputs": ["index/memory.sqlite"], "why": "…"}]}
```
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402

HERE = Path(__file__).resolve().parent.parent
CFG = HERE / "tools" / "witness.json"


def load_cfg() -> dict:
    try:
        if CFG.is_file():
            return json.loads(CFG.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print("[g_witness] 配置读不出（%s）：%r ⇒ 判红（不许静默放行）" % (CFG, exc),
              file=sys.stderr)
        return {"_broken": str(exc), "pairs": []}
    return {"pairs": []}


def imported_names(src: str) -> set:
    """静态取出 import 目标（含 `importlib.import_module("x")` 的字面量）。"""
    out = set()
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return {"__syntax_error__:%s" % exc.lineno}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            if n.module:
                out.add(n.module)
        elif isinstance(n, ast.Call):
            f = n.func
            name = getattr(f, "attr", "") or getattr(f, "id", "")
            if name in ("import_module", "__import__") and n.args:
                a = n.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    out.add(a.value)
    return out


class WitnessGate:
    NAME = "g_witness"

    def __init__(self, cfg: ProjectConfig | None = None):
        self.cfg = cfg
        self.reg = load_cfg()

    def check(self) -> dict:
        if self.reg.get("_broken"):
            return {"ok": False, "rows": [], "summary":
                    "独立见证者闸：**配置坏了**（%s）⇒ 判红" % self.reg["_broken"]}
        pairs = self.reg.get("pairs") or []
        if not pairs:
            return {"ok": True, "rows": [], "skipped": True,
                    "summary": "独立见证者闸：未声明核验器/被检对象对（配置为空）⇒ 判据未跑"}
        rows, bad, unknown = [], [], 0
        for pr in pairs:
            ck = (HERE / pr["checker"]).resolve()
            if not ck.is_file():
                bad.append("%s：核验器文件不存在" % pr["checker"])
                rows.append({"checker": pr["checker"], "ok": False, "why": "文件不存在"})
                continue
            src = ck.read_text(encoding="utf-8", errors="replace")
            imps = imported_names(src)
            subjects = [s for s in (pr.get("subject") or [])]
            hit = [s for s in subjects
                   if s in imps or any(s.split("/")[-1] in i for i in imps for s in subjects)]
            has_demo = ("--demo" in src) or ("--selftest" in src)
            outs = pr.get("subject_outputs") or []
            row = {"checker": pr["checker"], "imports_subject": hit, "has_demo": has_demo,
                   "subject_outputs": outs}
            if hit:
                bad.append("%s：**import 了被检对象**（%s）⇒ 同源判据，给不出独立证据"
                           % (pr["checker"], ", ".join(hit)))
                row["ok"] = False
            elif not has_demo:
                bad.append("%s：**没有自证入口**（--demo/--selftest）⇒ 核验器不可信"
                           % pr["checker"])
                row["ok"] = False
            elif outs:
                unknown += 1
                row["ok"] = None
                row["why"] = ("声明了被检产物 %s：**本闸不判它是否唯一依据**（需人工声明第二来源）"
                              % ", ".join(outs))
            else:
                row["ok"] = True
            rows.append(row)
        summary = ("独立见证者闸：对 %d 组 ｜ 违规 %d ｜ 未验 %d —— %s"
                   % (len(pairs), len(bad), unknown, "绿" if not bad else "红"))
        return {"ok": not bad, "rows": rows, "bad": bad, "unverified": unknown,
                "summary": summary}

    def render(self, rep: dict) -> str:
        lines = [rep["summary"]]
        for r in rep.get("rows") or []:
            if r.get("ok") is True:
                lines.append("  ✓ %s：未 import 被检对象，且带自证入口" % r["checker"])
            elif r.get("ok") is False:
                lines.append("  ✗ %s：%s" % (r["checker"], r.get("why") or "违规"))
            else:
                lines.append("  ⚠ %s：%s" % (r["checker"], r.get("why") or "未验"))
        return "\n".join(lines)


# ====================================================================== demo
def demo(cfg: ProjectConfig | None = None) -> int:
    import tempfile
    print("### 独立见证者闸 g_witness 演示")
    d = Path(tempfile.mkdtemp(prefix="gwitness_"))
    (d / "subject.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    # ① 同源：核验器 import 被检对象 ⇒ 必红
    (d / "bad_checker.py").write_text(
        "import subject\n\ndef main():\n    return subject.f()\n\nif __name__ == \"__main__\":\n    print(main())\n",
        encoding="utf-8")
    # ② 独立 + 带自证入口 ⇒ 绿
    (d / "good_checker.py").write_text(
        "def main():\n    return 0\n\nif __name__ == \"__main__\":\n    import sys\n    sys.exit(0 if \"--demo\" in sys.argv else main())\n",
        encoding="utf-8")
    # ③ 独立但没自证入口 ⇒ 红
    (d / "no_demo_checker.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    fails = 0
    saved = WitnessGate(cfg).reg
    for name, checker, want in (("核验器 import 被检对象", "bad_checker.py", False),
                                ("独立且带自证入口", "good_checker.py", True),
                                ("独立但无自证入口", "no_demo_checker.py", False)):
        g = WitnessGate(cfg)
        g.reg = {"pairs": [{"checker": str(d / checker), "subject": ["subject"]}]}
        rep = g.check()
        good = rep["ok"] == want
        print("  %s %-22s ⇒ %s" % ("✓" if good else "✗✗", name, rep["summary"][:70]))
        if not good:
            fails += 1
    g = WitnessGate(cfg)
    g.reg = {"pairs": [{"checker": str(d / "good_checker.py"), "subject": ["subject"],
                        "subject_outputs": ["out.db"]}]}
    rep = g.check()
    good = (rep["ok"] is True) and rep.get("unverified") == 1
    print("  %s %-22s ⇒ %s" % ("✓" if good else "✗✗", "声明被检产物 ⇒ 记未验", rep["summary"][:70]))
    if not good:
        fails += 1
    WitnessGate(cfg).reg = saved
    print("\n### 自检结果：%s" % ("全部通过（同源/无自证必红，独立+自证才绿，声明产物记未验）"
                                if fails == 0 else "%d 项失败" % fails))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        print("[g_witness] stdout 重设为 utf-8 失败（继续跑）：%r" % (e,), file=sys.stderr)
    from guard import load as load_project

    ap = argparse.ArgumentParser(prog="g_witness")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--selftest", "--demo", action="store_true", dest="selftest")
    a = ap.parse_args()
    g = WitnessGate(load_project(a.project, a.project_root))
    if a.selftest:
        sys.exit(demo(load_project(a.project, a.project_root)))
    rep = g.check()
    print(g.render(rep))
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    sys.exit(0 if rep["ok"] else 1)
