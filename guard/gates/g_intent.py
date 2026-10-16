# -*- coding: utf-8 -*-
"""_guard · 意图闸 g_intent —— 动手之前，先把"我以为你要我做什么"变成可确认的清单

## 为什么有这道闸（2026-10-03 用户点破）

用户原话：「**既然这些闸门能够帮助理解我说的话为什么你前面还是会理解错我的意思？甚至不问我？**」

**实测的错**（真实发生，不是假设）：
  · 用户说「先把**主表遇到的问题**根治了再看吧」
  · 我理解成「主表**这个文件**的缺陷（NUL 字节 / 字段不齐 / 编号乱）」→ 出了一整套修复方案
  · 用户回：「**我没让你修这个表**我要修的是**表里内容体现的问题**」
  · ⇒ 我把"表遇到的问题"读成了"表这个文件的问题"，而用户要的是"表里记录的那些问题"

**关键点：已有的八道闸一道都拦不住它。**
  八道闸全在**产出侧**（报告写完、要落盘时）；
  而这个错发生在**动手前**——方向就错了，产出侧再严也没用。

**这正是 g_obj 的病根，只不过对象从"几何体"换成了"用户的意图"**：
  g_obj 治「自然语言直接索引到几何体（删错件）」；
  g_intent 治「自然语言直接索引到**任务**（做错事）」。

## 根除做法（照抄 g_obj 的三步，一步不许跳）

  1. RESOLVE  把用户的话解析成**任务清单**：逐条给「对象 / 打算做什么 / 我不确定的地方」
  2. CONFIRM  清单落台账；**有歧义就必须等确认**（无歧义才可放行）
  3. APPLY    才动手

## 硬约束（可执行判据）

  · 用户话里出现**指代词**（这个/那个/它/该/此/这些/那些/问题/东西/情况）
    且上下文里**同类候选 ≥2 个** ⇒ **必须问**，不许自己挑一个
  · **不确定栏为空** ⇒ 视为"没做 RESOLVE"，同样拦下
    （表里病根 7：「未核实就下结论」；空着的不确定栏 = 没核实）
  · 命令里出现**范围待定词**（这些/那些/等/之类/全部/其它）而没说边界 ⇒ 拦

跨项目：触发词与阈值来自 config.intent_gate；本文件不含任何项目专有内容。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402


# --------------------------------------------------------------------------
DEFAULTS: dict = {
    # 指代词：出现即说明"指向哪个对象"没被说清
    "deixis_pats": [
        r"这个", r"那个", r"该(?!文件|行|处)", r"此(?!外)", r"它", r"这些", r"那些",
        r"这种", r"那种", r"上述", r"前面(?:说|提)的", r"刚才",
    ],
    # 语义空壳名词：单独出现时不知道指哪个具体东西
    "vague_nouns": [
        r"问题", r"东西", r"情况", r"部分", r"内容", r"地方", r"方面",
        r"事情", r"活儿", r"那块", r"那边",
    ],
    # 范围待定词
    "scope_pats": [
        r"这些", r"那些", r"等(?!级|于|待)", r"之类", r"全部", r"其它", r"其他", r"所有",
    ],
    # 明确要求"问"的信号（用户主动说"有歧义就问我"）
    "ask_signals": [r"问我", r"先问", r"不确定.*问", r"有疑问.*问"],
    # 阈值
    "min_candidates": 2,      # 同类候选 ≥2 才算歧义
    "min_uncertain": 1,       # 不确定栏至少写几条（0 ⇒ 视为没做 RESOLVE）
}


def _cfg_of(cfg: ProjectConfig) -> dict:
    d = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    over = cfg.get("intent_gate") or {}
    for k, v in over.items():
        if isinstance(v, list) and isinstance(d.get(k), list):
            d[k] = list(d[k]) + [x for x in v if x not in d[k]]
        else:
            d[k] = v
    return d


class IntentGateError(RuntimeError):
    """意图闸未通过 —— 不许动手。"""


class NeedsClarify(IntentGateError):
    """用户的话有歧义 —— 必须先问，不许自己挑一种解释执行。"""


class IntentGate:
    """意图闸。三步不可绕过：resolve → confirm → apply。"""

    NAME = "g_intent"

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self.K = _cfg_of(cfg)
        lg = (cfg.get("intent_gate") or {}).get("ledger", "_guard_ledger/intent.jsonl")
        self.ledger = cfg.resolve(lg)
        self.ledger.parent.mkdir(parents=True, exist_ok=True)

    # ---------------- 第一步：RESOLVE ----------------
    def resolve(self, user_said: str, *, candidates: list[str] | None = None,
                uncertain: list[str] | None = None, my_reading: str = "") -> dict:
        """把用户的话解析成任务清单。

        candidates: 上下文里的**同类候选**（同一个词可能指的东西，逐条列出）
        uncertain : **我不确定的地方**（必填；空 ⇒ 拦，见模块 docstring）
        my_reading: 我打算怎么理解这句话（一句话人话）
        """
        cands = list(candidates or [])
        unc = list(uncertain or [])

        # 1) 指代词 / 空壳名词
        dej = [(m.group(0), m.start()) for p in self.K["deixis_pats"]
               for m in re.finditer(p, user_said)]
        vague = [(m.group(0), m.start()) for p in self.K["vague_nouns"]
                 for m in re.finditer(p, user_said)]
        scope = [(m.group(0), m.start()) for p in self.K["scope_pats"]
                 for m in re.finditer(p, user_said)]

        reasons: list[str] = []
        # 2) 歧义判定：有指代词或空壳名词，且候选 ≥ 阈值 ⇒ 必须问
        if (dej or vague) and len(cands) >= int(self.K["min_candidates"]):
            words = sorted({w for w, _ in (dej + vague)})
            reasons.append(
                f"话里有指代/空壳词「{'、'.join(words)}」，而同类候选有 {len(cands)} 个"
                f"（{'、'.join(cands[:5])}）⇒ 指哪个没被说清"
            )
        # 3) 范围待定
        if scope and len(cands) >= int(self.K["min_candidates"]):
            words = sorted({w for w, _ in scope})
            reasons.append(f"范围词「{'、'.join(words)}」没给边界（候选 {len(cands)} 个）")
        # 4) 不确定栏为空 ⇒ 视为没做 RESOLVE
        if len(unc) < int(self.K["min_uncertain"]):
            reasons.append(
                "『我不确定的地方』栏为空 —— 空栏 = 没做 RESOLVE"
                "（真无疑问就显式写『无』，但要先真想过一遍）"
            )

        rec = {
            "gate": self.NAME, "user_said": user_said.strip(),
            "my_reading": my_reading.strip(),
            "candidates": cands, "uncertain": unc,
            "deixis": sorted({w for w, _ in dej}),
            "vague": sorted({w for w, _ in vague}),
            "scope": sorted({w for w, _ in scope}),
            "must_clarify": bool(reasons), "reasons": reasons,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._log({**rec, "event": "resolve"})
        return rec

    # ---------------- 第二步：CONFIRM ----------------
    def require_clarify(self, rec: dict) -> None:
        """有歧义就 raise —— 这是让闸门"拦得住"的入口。"""
        if rec.get("must_clarify"):
            raise NeedsClarify(self.question_of(rec))

    def question_of(self, rec: dict) -> str:
        """把歧义变成**一个选择题**（不是开放式提问——选项题最省用户力气）。"""
        cands = rec.get("candidates") or []
        lines = ["你先定一下方向，我不敢自己挑：", ""]
        q = rec.get("user_said", "")
        lines.append(f"  你说的是：「{q}」")
        if rec.get("my_reading"):
            lines.append(f"  我原本以为：{rec['my_reading']}")
        lines.append("")
        for i, c in enumerate(cands, 1):
            lines.append(f"  {i}. {c}")
        lines.append("")
        lines.append("  （回 1/2/…，或直接说你要的是哪个）")
        return "\n".join(lines)

    def confirm(self, rec: dict, *, answer: str = "", confirmed: bool = False) -> dict:
        """用户确认后才放行。answer = 用户的回答（记台账，供追溯）。"""
        if rec.get("must_clarify") and not (confirmed and answer.strip()):
            raise NeedsClarify(self.question_of(rec))
        rec2 = {**rec, "event": "confirm", "answer": answer.strip(),
                "confirmed": bool(confirmed), "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._log(rec2)
        return rec2

    # ---------------- 第三步：APPLY ----------------
    def apply(self, rec: dict, what: str) -> dict:
        """真正动手前调它（写台账）。未确认 ⇒ 拦。"""
        if not rec.get("confirmed"):
            raise IntentGateError("未确认就动手 —— 先走 resolve →（有歧义则）confirm")
        row = {"event": "apply", "what": what, "my_reading": rec.get("my_reading", ""),
               "answer": rec.get("answer", ""), "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._log(row)
        return row

    def _log(self, rec: dict) -> None:
        # ★ D6 修复（2026-10-04）：原 `except Exception: pass` ⇒ 台账写不进去无声无息，
        #   闸坏了没人知道（= 假绿）。现在写失败必须落到 stderr。
        try:
            with self.ledger.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(
                f"[g_intent] 台账写入失败（不静默吞）：{self.ledger} :: "
                f"{type(e).__name__}: {e}\n")


# --------------------------------------------------------------------------
# 自检：用 2026-10-03 那次真实误解做负例
# --------------------------------------------------------------------------
def demo(cfg: ProjectConfig) -> int:
    g = IntentGate(cfg)
    fails = 0
    print("### 意图闸 · 负例自检（用的是真实发生过的那次误解）\n")

    # ── 负例 1：真实那次 —— "主表遇到的问题"
    print("### 负例 1：把『主表遇到的问题』读成『表这个文件的问题』")
    rec = g.resolve(
        "先把主表遇到的问题根治了再看吧",
        candidates=["主表**文件**的缺陷（NUL 字节 / 字段不齐 / 编号乱）",
                    "主表**里记录的**那些问题（4,888 条，7 个病根）"],
        uncertain=[],                       # ← 当时我一个都没写（这就是病）
        my_reading="修 主表.md 的 NUL 字节与字段",
    )
    try:
        g.require_clarify(rec)
        print("  ✗✗ 没拦住 —— 这正是真实发生的那次"); fails += 1
    except NeedsClarify as e:
        print("  ✓✓ 拦住。它会问你：")
        print("      " + str(e).replace("\n", "\n      ")[:400])
    print()

    # ── 负例 2：不确定栏空着
    print("### 负例 2：不确定栏为空（= 没做 RESOLVE）")
    rec2 = g.resolve("改一下那个东西", candidates=["A 报告", "B 脚本"],
                     uncertain=[], my_reading="改 B")
    if rec2["must_clarify"]:
        print("  ✓✓ 拦住：" + rec2["reasons"][0]); 
    else:
        print("  ✗✗ 没拦住"); fails += 1
    print()

    # ── 负例 3：范围词没边界
    print("### 负例 3：范围词没给边界（『这些都改了』）")
    rec3 = g.resolve("把这些都改了", candidates=["8 道闸门的 README", "两处 config", "hook.py"],
                     uncertain=["不清楚『这些』包不包括 _default.yaml"], my_reading="改全部")
    if rec3["must_clarify"]:
        print("  ✓✓ 拦住：" + [r for r in rec3["reasons"] if "范围" in r or "指代" in r][0])
    else:
        print("  ✗✗ 没拦住"); fails += 1
    print()

    # ── 正例：指令明确、候选唯一、不确定栏非空
    print("### 正例：指令明确应放行")
    rec4 = g.resolve(
        "把 g_root 闸门接进 hook.py 的写报告流程",
        candidates=["hook.py 的报告闸"],      # 只有 1 个候选 ⇒ 不算歧义
        uncertain=["无（候选唯一，路径已给）"],
        my_reading="在 hook.py 加 guard_report()，写入前过闸",
    )
    if rec4["must_clarify"]:
        print("  ✗ 误杀：" + "；".join(rec4["reasons"])); fails += 1
    else:
        ok = g.confirm(rec4, answer="就按你说的做", confirmed=True)
        g.apply(ok, "改 hook.py")
        print("  ✓ 放行 → confirm → apply 全链走通")
    print()

    print(f"### 自检结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[g_intent] stdout 重设为 utf-8 失败（继续跑）：{e}\n")
    from guard import load as load_cfg

    ap = argparse.ArgumentParser(prog="g_intent")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--said", help="用户原话（解析它）")
    ap.add_argument("--candidates", help="同类候选项，分号分隔")
    ap.add_argument("--uncertain", help="我不确定的地方，分号分隔")
    ap.add_argument("--reading", default="", help="我打算怎么理解（一句话）")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    if a.demo:
        sys.exit(demo(cfg))

    g = IntentGate(cfg)
    if a.said:
        rec = g.resolve(a.said,
                        candidates=[x for x in (a.candidates or "").split(";") if x.strip()],
                        uncertain=[x for x in (a.uncertain or "").split(";") if x.strip()],
                        my_reading=a.reading)
        if rec["must_clarify"]:
            print(g.question_of(rec))
            sys.exit(1)
        print("✓ 无歧义，可放行：" + rec["my_reading"])
        sys.exit(0)
    print("用法：--demo 自检 / --said '用户原话' --candidates 'A;B' --uncertain '…'")
