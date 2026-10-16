# -*- coding: utf-8 -*-
"""_guard · 极性/否定闸 g_polarity —— 拿"关键词出现"冒充"语义存活"必须判红（R-67）

## 判据来源（学自 hive-memory-bench，署名）

hive-memory-bench（符荣峻，CC BY 4.0 / 代码 MIT）的 `systems/三轴诊断.py` 在文档里**自曝口径**：
> L2′ 关键词覆盖 …… **★ 校准注意**：它只量**术语出现过没有**——对极性/否定/语序不敏感
> （把「必须」改成「不必」不会压低它）。**它不是蕴含/正确性检查**；原名「语义存活」已弃用。

它给的自证夹具（`--selftest`）就是极性反转：`L1 0/2 · L2 0/2 · **L2′ 2/2**`。

**为什么本仓需要它**：我们自己的判据里也有"关键词/要点出现即算命中"的形态
（例如采分点覆盖、`g_xcheck` 的术语核对）。一旦有人把这种读数**写成"语义存活/结论成立"**，
就会出假绿 —— 而本工作区最重的扣分面正是"把未验写成已验"。

## 判据

| id | 判据 | 量的对象 |
|---|---|---|
| P1 | 声明与引文**极性相反**（一方出现 `必须/可以/是/有`，另一方出现对应的 `不必/不可以/不是/没有`）⇒ **红** | 声明文本 × 引文 |
| P2 | 只有"关键词覆盖"作依据、**没有任何极性/否定核对** ⇒ **红**（不许拿覆盖冒充蕴含） | 依据的形态 |
| P3 | 声明与引文极性一致 ⇒ 绿；两侧都没提到该极性对象 ⇒ **未验**（不等于通过） | 三态 |

## 用法

    python _guard/gates/g_polarity.py --claim "…" --quotes quotes.json [--json out.json]
    python _guard/gates/g_polarity.py --demo        # 负例自证（极性反转必红；纯覆盖必红）

`quotes.json`：`["引文1", "引文2"]` 或 `{"quotes":[...], "coverage_only": true}`
（`coverage_only=true` 表示"依据只有关键词覆盖" ⇒ 直接按 P2 判红）。

退出码：0＝绿（极性一致）；1＝红（极性相反 / 只有覆盖 / 用法错误）；2＝未验（两侧都没提）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402

#: 极性对（正 / 反）。**同口径**：只做"正出现而反也出现"的字面核对，不做语义推理。
PAIRS = [
    ("必须", "不必"), ("必须", "无须"), ("可以", "不可以"), ("可以", "不能"),
    ("允许", "禁止"), ("是", "不是"), ("有", "没有"), ("增加", "减少"),
    ("支持", "反对"), ("真实", "虚假"), ("正确", "错误"), ("有效", "无效"),
    ("自由", "奴役"), ("公开", "保密"), ("存在", "不存在"),
]


def hits(text: str) -> set:
    t = re.sub(r"\s+", "", text or "")
    return {("%s|%s" % (a, b)) for a, b in PAIRS if a in t} , {("%s|%s" % (a, b))
                                                              for a, b in PAIRS if b in t}


class PolarityGate:
    NAME = "g_polarity"

    def __init__(self, cfg: ProjectConfig | None = None):
        self.cfg = cfg

    def check(self, claim: str, quotes: list, coverage_only: bool = False) -> dict:
        pos_c, neg_c = hits(claim)
        pos_q, neg_q = set(), set()
        for q in quotes or []:
            a, b = hits(q)
            pos_q |= a
            neg_q |= b
        # P2：只有关键词覆盖、没有任何极性核对 ⇒ 红
        if coverage_only:
            return {"ok": False, "verdict": "红", "why": "P2",
                    "summary": "极性闸：**依据只有「关键词覆盖」**（声明称语义存活/结论成立，"
                               "却没有做极性或否定核对）⇒ 判红：覆盖 ≠ 蕴含（hive 自曝口径：" 
                               "「必须→不必」不会压低覆盖读数）。"}
        # P1：声明说正、引文说反（或反之）⇒ 红
        conflict = (pos_c & neg_q) | (neg_c & pos_q)
        if conflict:
            ex = sorted(conflict)[0]
            return {"ok": False, "verdict": "红", "why": "P1",
                    "summary": "极性闸：**引文与声明极性相反**（%s：声明说「%s」，引文说「%s」）⇒ 判红。"
                               % (ex, ex.split("|")[0], ex.split("|")[1]),
                    "conflict": sorted(conflict)}
        # P3：两侧都没提到该极性对象 ⇒ 未验
        if not (pos_c | neg_c) & (pos_q | neg_q):
            return {"ok": None, "verdict": "未验", "why": "P3",
                    "summary": "极性闸：声明与引文**都没提到**极性对象 ⇒ **未验**（不等于通过）。"}
        return {"ok": True, "verdict": "绿", "why": "P3",
                "summary": "极性闸：声明与引文极性一致（口径：只做字面正/反核对，不做蕴含推理）。"}

    def render(self, rep: dict) -> str:
        return rep.get("summary", "")


# ====================================================================== demo
def demo(cfg: ProjectConfig | None = None) -> int:
    print("### 极性/否定闸 g_polarity 演示（复现 hive 的三轴校准夹具结论：覆盖对极性不敏感）")
    g = PolarityGate(cfg)
    cases = [
        ("极性一致（必须 ↔ 必须）", "写入必须过闸门", ["写入必须过闸门才落盘"], False, True),
        ("极性相反（必须 ↔ 不必）", "写入必须过闸门", ["写入不必过闸门，直接落盘"], False, False),
        ("★ 纯关键词覆盖冒充蕴含", "结论：语义存活", ["必须 过闸门 写入 落盘"], True, False),
        ("两侧都没提（未验）", "今天天气如何", ["无关的一段话"], False, None),
    ]
    fails = 0
    for name, claim, quotes, cov, want in cases:
        rep = g.check(claim, quotes, coverage_only=cov)
        good = rep["ok"] == want
        print("  %s %-24s ⇒ [%s] %s" % ("✓" if good else "✗✗", name, rep["verdict"],
                                        rep["summary"][:88]))
        if not good:
            fails += 1
    print("\n### 自检结果：%s" % ("全部通过（极性反转必红；纯覆盖必红；两侧未提=未验）"
                                if fails == 0 else "%d 项失败" % fails))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        print("[g_polarity] stdout 重设为 utf-8 失败（继续跑）：%r" % (e,), file=sys.stderr)
    from guard import load as load_project

    ap = argparse.ArgumentParser(prog="g_polarity")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--claim")
    ap.add_argument("--quotes", help="引文 JSON：[\"…\"] 或 {\"quotes\":[…],\"coverage_only\":true}")
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--selftest", "--demo", action="store_true", dest="selftest")
    a = ap.parse_args()
    gate = PolarityGate(load_project(a.project, a.project_root))
    if a.selftest:
        sys.exit(demo(load_project(a.project, a.project_root)))
    if not (a.claim and a.quotes):
        print("需要 --claim 与 --quotes（或用 --demo）")
        sys.exit(1)
    d = json.loads(Path(a.quotes).read_text(encoding="utf-8"))
    qs = d.get("quotes") if isinstance(d, dict) else d
    cov = bool(d.get("coverage_only")) if isinstance(d, dict) else False
    rep = gate.check(a.claim, qs, coverage_only=cov)
    print(gate.render(rep))
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.exit(0 if rep["ok"] is True else (2 if rep["ok"] is None else 1))
