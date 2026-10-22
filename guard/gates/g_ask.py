# -*- coding: utf-8 -*-
"""_guard · 开工前提问闸 g_ask —— 不理解为或有疑问 ⇒ **必须先问，绝对不开工**

## 为什么有这道闸（2026-10-04 立，用户明令）

**用户原话**：
> 「…」

## 与已有的 g_intent 什么关系（**不是重复，是补缺口**）

| 闸 | 治什么 | 触发条件 |
|---|---|---|
| `g_intent` | **指代歧义** —— "这个/那个"指哪个对象 | 有指代词 **且** 上下文同类候选 ≥2 |
| **`g_ask`（本闸）** | **该问却没问就开工** —— 包括两类：<br>① **不理解**（读不通 / 有多种合理解读 / 缺少必需信息）<br>② **有疑问**（我发现疑点但选择"先做着看"） | **任一**命中即触发，<u>不需要</u>"候选 ≥2"这个前置 |

**关键区别**：`g_intent` 要求"候选 ≥2"才算歧义 —— 这留下一个大洞：
**只有一个候选、但那个候选我其实没把握** ⇒ `g_intent` 放行 ⇒ 我照样开工。
而本项目最贵的一次偏离（示例项目A 42 轮造山全废）**恰恰不是指代歧义**，
是"用户说了要巍峨嶙峋，我读懂了字面，但**没问清楚"什么样的才算巍峨"**"。

## 实证代价（这条闸要防的具体事）

示例项目A会话 `<会话>`：
- 用户 seq=…「…」
  —— 这句**本身就是用户在告诉我"你理解错了"**；
- 而在此之前的 20+ 轮里，**Lead 一次都没问过"你说的巍峨具体是什么样"**，
  而是自己按"坡度分布双峰直方图"去猜 —— 猜了 20 多轮；
- 直到很晚才承认"改前改后都不对，因为方法本身错了"，随后整座山被删。

**⇒ 20 多轮的代价，起因是"该问的时候没问，选择先做着看"。**

## 三层判据（可执行）

  **A · 该问信号**：本轮输入/我的计划里出现**该问的信号** ⇒ 必须先问
     · 读不通的词（用户自造术语 / 无出处的尺寸口径 / 指代不明的目标）
     · "我先按…理解""大概是…""应该是…""先做着看""边做边确认"
     · 用户明确说过"不懂就问""有疑问就问我"
  **B · 疑点未清**：我识别出了疑点（`doubts`），却**没有任何一次提问记录** ⇒ 拦
  **C · 无据开工**：动手前没有 `ask.jsonl` 里的 resolve 记录 ⇒ 视为"没做理解直接开工" ⇒ 拦

## 与"不许滥问"的平衡（**这条同样重要**）

用户要的是"**有疑问就问**"，不是"什么都问"。本项目另一条已立的纪律 [库 P82/96/97]：
**答案已经在剧本/台账/文档里写着的问题，不许拿去问用户**（示例项目A实测至少 5 次犯这个，
每次都被用户骂"我不是一直让你参考剧本吗"）。

⇒ 所以本闸**同时判两个方向**：
  · **该问没问** ⇒ 拦（本闸主旨）
  · **该自己查的去问** ⇒ 也拦（`asked` 里若有条目能被 `self_check_sources` 命中 ⇒ 拦，
    提示"先去查 <来源>，查不到再问"）

## 用法
```bash
python _guard/gates/g_ask.py --demo         # 负例自检（用真实那两件事）
python _guard/gates/g_ask.py --note "把山重做一遍" --plan-file <计划.md>
python _guard/gates/g_ask.py --log-ask "问：你说的巍峨具体指什么？" --answer "…"
```

跨项目：触发词与来源清单可在 `_guard/projects/<名>.yaml` 的 `ask_gate` 段覆盖。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402

DEFAULTS: dict = {
    "ledger": "_guard_ledger/ask.jsonl",

    # ── A · 「该问」的信号（出现即必须先问）─────────────────────────
    # 1) 我自己在"猜"的措辞 —— 这是最诚实的自曝信号
    "guessing_pats": [
        r"我先按.{0,12}理解", r"大概(?:是|指)", r"应该是", r"估计是", r"我猜",
        r"先做(?:着|起)看", r"边做边(?:确认|看|调)", r"按我的理解",
        r"如果不(?:对|是).{0,8}(?:再|就)(?:改|调|说)", r"先按.{0,10}(?:来|走)",
        r"暂(?:按|定)", r"默认(?:按|用|取)",
        # ★★ 2026-10-15 形态扩面（用户：「…」）★★
        #   实测逃逸：「…」。我**按自己的理解**直接开工了」
        #   ⇒ 原清单只有「按**我的**理解」（"我的" ≠ "自己的"）⇒ 一个字都不匹配 ⇒ **放行**。
        #   "换个人称/换个动词"就逃逸 —— 与 `/d/agent` 只认两个字面量同族 ⇒ 按**形态**补。
        r"按(?:我)?自己的?理解", r"我理解为", r"自行(?:判断|决定|开工|处理)",
        r"没(?:有)?(?:再)?(?:确认|问)(?:就|便)?(?:开工|动手|开始|执行)",
        r"先(?:做|干)(?:了)?再说",
    ],
    # 2) 需求本身"读不通"的信号
    "unclear_pats": [
        r"什么(?:样|才|算)", r"怎么(?:算|才)", r"到什么程度", r"标准是(?:什么|多少)",
        r"要(?:多|几)(?:大|高|长|宽|深|久)", r"具体(?:是|指|要)",
        r"不太(?:确定|清楚|明白)", r"没(?:看|读)懂", r"有(?:歧义|多种解读)",
    ],
    # 3) 用户点名"要问"的信号（用户以前说过"不懂就问"）
    "user_ask_signals": [
        r"不懂就(?:问|说)", r"有疑问就(?:问|说)", r"不确定就(?:问|说)",
        r"先问(?:我|清楚)", r"问我", r"有(?:问题|疑问).{0,6}(?:问|提)",
    ],
    # ── B · 「我不该问、该自己查」的来源（防滥问）──────────────────
    # 这些地方写着答案的问题，不许拿去问用户（[库 P82] 的闸化）
    "self_check_sources": [
        "07_场景设计.md", "06_分镜表.md", "03_场次表.md", "09_建模清单.md",
        "00_世界坐标总图", "验收判据", "台账", "AGENTS.md", "SKILLS_INDEX",
        "记忆库", "dsh-rag-memory",  # purity-allow: 夹具/样本（闸自检的输入数据或真实语料，非代码依赖）
    ],
    # ── 阈值 ──────────────────────────────────────────────────────
    "min_doubts_when_asking": 0,   # doubts 非空但 asked 为空 ⇒ 拦
}


def _cfg_of(cfg: ProjectConfig) -> dict:
    d = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    over = cfg.get("ask_gate") or {}
    for k, v in over.items():
        if isinstance(v, list) and isinstance(d.get(k), list):
            d[k] = list(d[k]) + [x for x in v if x not in d[k]]
        else:
            d[k] = v
    return d


class AskGateError(RuntimeError):
    """该问没问就开工 —— 绝对不许开工。"""


class MustAsk(AskGateError):
    """命中「该问」信号 ⇒ 先提问，拿到答复前不许动手。"""


class MustSelfCheck(AskGateError):
    """把该自己查的问题拿去问用户 ⇒ 先去查来源，查不到再问。"""


class AskGate:
    """开工前提问闸。三步：scan（扫信号）→ ask（提问）→ gate（放行/拦）。"""

    NAME = "g_ask"

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self.K = _cfg_of(cfg)
        self.ledger = cfg.resolve(self.K["ledger"])
        self.ledger.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- A
    def scan(self, text: str, *, doubts: list[str] | None = None) -> dict:
        """扫本轮输入 / 我的计划，找「该问」的信号。

        【闸的精髓】**这是自曝机制** ——
          我只要在计划里写出"我先按…理解""应该是…""先做着看"，
          就等于自己承认"我在猜"，闸立刻拦下要求提问。
          **诚实写计划 ⇒ 闸生效；含糊写计划 ⇒ 也拦（因为 C 判据要求有 resolve 记录）。**
        """
        t = text or ""
        hits: dict[str, list[str]] = {}
        for name, key in (("我在猜", "guessing_pats"),
                          ("需求读不通", "unclear_pats"),
                          ("用户要求过要问", "user_ask_signals")):
            found = []
            for p in self.K[key]:
                found += [m.group(0) for m in re.finditer(p, t)]
            if found:
                hits[name] = sorted(set(found))
        ds = list(doubts or [])
        must = bool(hits) or bool(ds)
        reasons = []
        for k, v in hits.items():
            reasons.append(f"{k}：命中「{'、'.join(v[:4])}」⇒ 说明这件事我**没有把握**")
        if ds:
            reasons.append(f"我自己列出了 {len(ds)} 条疑点（{'；'.join(ds[:3])}）⇒ 有疑点就不许开工")
        rec = {"gate": self.NAME, "event": "scan", "text": t[:2000],
               "hits": hits, "doubts": ds, "must_ask": must, "reasons": reasons,
               "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._log(rec)
        return rec

    # ---------------------------------------------------------------- B
    def gate(self, scan_rec: dict, *, asked: list[str] | None = None,
             answered: bool = False) -> dict:
        """放行判定。

        asked    : 我实际提了哪些问题（逐条原文）
        answered : 用户是否已答复
        """
        aq = list(asked or [])
        reasons = []
        if scan_rec.get("must_ask") and not aq:
            reasons.append("命中「该问」信号，但**一次都没问** ⇒ 绝对不许开工")
        if scan_rec.get("must_ask") and aq and not answered:
            reasons.append(f"已提问 {len(aq)} 条，但**用户还没答复** ⇒ 不许开工")
        if scan_rec.get("doubts") and not aq:
            reasons.append(f"自己都列出了 {len(scan_rec['doubts'])} 条疑点却没问 ⇒ 拦")
        ok = not reasons
        rec = {"gate": self.NAME, "event": "gate", "asked": aq,
               "answered": bool(answered), "ok": ok, "reasons": reasons,
               "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._log(rec)
        if not ok:
            raise MustAsk(self.question_of(scan_rec, aq))
        return rec

    def question_of(self, scan_rec: dict, asked: list[str]) -> str:
        """把"该问"变成**具体的问题清单**（不是一句"我该问什么"）。"""
        lines = ["按你定的规矩：**不理解或有疑问就必须先问，绝对不开工**。", ""]
        for r in scan_rec.get("reasons", []):
            lines.append(f"  · {r}")
        if scan_rec.get("doubts"):
            lines += ["", "  我列出的疑点："]
            lines += [f"    - {d}" for d in scan_rec["doubts"][:8]]
        if asked:
            lines += ["", "  已提的问题（等答复）："] + [f"    - {q}" for q in asked[:8]]
        lines += ["", "  ★ 但**回答用户前先自检**：这些问题的答案是不是已经写在"]
        lines.append(f"    {'、'.join(self.K['self_check_sources'][:6])} 里了？")
        lines.append("    写着答案的、该自己查的，不许拿去问用户（这条同样是硬规矩）。")
        return "\n".join(lines)

    # ---------------------------------------------------------------- C
    def check_asked(self, asked: list[str]) -> dict:
        """防**滥问**：我提的问题里，有没有"答案早就在文档里"的？

        [库 P82] + 示例项目A实测：用户至少 5 次骂"我不是一直让你参考剧本和分镜吗"。
        判据：问题文本里出现 `self_check_sources` 里的来源名，或疑问点能对到这些来源
              ⇒ 先自己去查，查不到再问。
        """
        rows = []
        for q in asked:
            src = [s for s in self.K["self_check_sources"] if s in q]
            rows.append({"q": q[:100], "self_answerable": bool(src), "sources": src})
        bad = [r for r in rows if r["self_answerable"]]
        return {"ok": not bad, "rows": rows, "n_bad": len(bad)}

    # ---------------------------------------------------------------- 报告级
    def check_text(self, text: str) -> dict:
        """报告/计划级检查：出现"我在猜"措辞却没有对应的提问记录 ⇒ 拦。

        这条让闸**在产出侧也生效** —— 即"事后也能抓出'当时该问没问'"。
        """
        rep_scan = self.scan(text)                      # 注意：会写一条 scan 台账
        asked = self.asked_in(text)
        reasons = []
        if rep_scan["must_ask"] and not asked:
            reasons.append("文本里有「" + "、".join(
                list(rep_scan["hits"].keys())) + "」信号，但**没有任何提问记录**")
        ok = not reasons
        rep = {"gate": self.NAME, "ok": ok, "hits": rep_scan["hits"],
               "asked": asked, "reasons": reasons,
               "summary": ("开工前提问："
                           + ("✓ 无'该问未问'信号" if ok
                              else "✗ " + "；".join(reasons))[:200])}
        self._log({"event": "report", "ok": ok, "n_asked": len(asked)})
        return rep

    @staticmethod
    def asked_in(text: str) -> list[str]:
        """从文本里抽"我提了什么问题"（问号句 + `问：` 标记）。"""
        out = []
        for m in re.finditer(r"(?:^|\n)\s*(?:问|提问|已问)\s*[:：]\s*(.+)", text):
            out.append(m.group(1).strip()[:120])
        if not out:
            out = [m.group(0).strip()[:120]
                   for m in re.finditer(r"[^\n。]{6,80}[？?]", text)][:10]
        return out

    # ---------------------------------------------------------------- 便捷入口
    def before_work(self, note: str, *, plan: str = "",
                    doubts: list[str] | None = None) -> dict:
        """**开工前调它**。不解或有疑问 ⇒ raise MustAsk。"""
        scan_rec = self.scan(f"{note}\n{plan}", doubts=doubts)
        if scan_rec["must_ask"]:
            raise MustAsk(self.question_of(scan_rec, []))
        return scan_rec

    def _log(self, rec: dict) -> None:
        try:
            with self.ledger.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[g_ask] 台账写入失败（不静默吞）：{self.ledger} :: "
                             f"{type(e).__name__}: {e}\n")


# --------------------------------------------------------------------------
def demo(cfg: ProjectConfig) -> int:
    g = AskGate(cfg)
    fails = 0
    print("### 开工前提问闸 g_ask · 负例自检（全部来自真实发生过的事）\n")

    # ── 负例 1：示例项目A 20+ 轮造山 —— 该问没问就开工
    print("### 负例 1：用户要「巍峨嶙峋」，我没问「什么样的算巍峨」就开始改（真实，代价 42 轮）")
    plan = ("我先按『坡度分布双峰直方图』理解用户要的巍峨："
            "缓坡≥25%、中坡≤15%、陡崖≥25%。先做着看，出图不对再调。")
    rec = g.scan(plan, doubts=["用户没有给出巍峨的量化标准"])
    print(f"  扫出信号：{list(rec['hits'].keys())}｜疑点 {len(rec['doubts'])} 条")
    try:
        g.gate(rec)
        print("  ✗✗ 没拦住 —— 这正是真实发生的那 42 轮"); fails += 1
    except MustAsk as e:
        print("  ✓✓ 拦住。它会要求先问：")
        for ln in str(e).split("\n")[:6]:
            print("      " + ln)
    print()

    # ── 正例 1：问了、也被答复了 ⇒ 放行
    print("### 正例 1：先问了「巍峨具体指什么」并拿到答复 ⇒ 放行")
    try:
        g.gate(rec, asked=["你说的『巍峨嶙峋』具体指什么？是层峦叠嶂的远山剪影，还是近处可见岩理的崖壁？"],
               answered=True)
        print("  ✓ 放行")
    except MustAsk as e:
        print(f"  ✗ 误杀：{str(e)[:60]}"); fails += 1
    print()

    # ── 负例 2：问了但用户还没答复 ⇒ 也不许开工
    print("### 负例 2：提了问题但用户还没答复 ⇒ 同样不许开工")
    try:
        g.gate(rec, asked=["巍峨具体指什么？"], answered=False)
        print("  ✗✗ 没拦住"); fails += 1
    except MustAsk:
        print("  ✓✓ 拦住（等答复，不许自己先动）")
    print()

    # ── 负例 3：防滥问 —— 答案在剧本里却拿去问用户（示例项目A实测 ≥5 次）
    print("### 负例 3：答案已写在 07_场景设计.md 里，却拿去问用户（真实，被骂 ≥5 次）")
    r = g.check_asked(["07_场景设计.md 里写的丹房位置到底是哪个？你定一下"])
    got = r["ok"]
    print(f"  {'✗✗ 没拦住' if got else '✓✓ 拦住'}：可自查来源={r['rows'][0]['sources']}")
    if got:
        fails += 1
    r = g.check_asked(["你说的『精细』到什么程度算够？这个剧本里没写"])
    print(f"  {'✓ 放行（真该问）' if r['ok'] else '✗ 误杀'}")
    if not r["ok"]:
        fails += 1
    print()

    # ── 正例 2：干净的计划，无猜无疑 ⇒ 放行
    print("### 正例 2：计划明确、无疑点 ⇒ 放行")
    clean = "按 07_场景设计.md 第 283 行：道观 6×5 步、丹房在观南 3 步。尺寸已从原文取到，无疑点。"
    rec2 = g.scan(clean)
    try:
        g.gate(rec2)
        print("  ✓ 放行（未命中任何『该问』信号）")
    except MustAsk as e:
        print(f"  ✗ 误杀：{str(e)[:70]}"); fails += 1
    print()

    # ── 报告级：事后也能抓
    print("### 负例 4：报告级事后抓（产出侧同样生效）")
    txt = "我先按自己的理解去改了山形，估计是大致差不多就行。"
    rep = g.check_text(txt)
    print(f"  {'✓ 拦住' if not rep['ok'] else '✗ 没拦住'}：{rep['summary'][:90]}")
    if rep["ok"]:
        fails += 1
    print()

    print(f"### 自检结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    from guard import load as load_cfg

    ap = argparse.ArgumentParser(prog="g_ask")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--note", help="开工动作说明")
    ap.add_argument("--plan-file", help="计划文本文件")
    ap.add_argument("--text-file", help="报告/计划（报告级检查）")
    ap.add_argument("--ask", action="append", default=[], help="我提的问题（可多次）")
    ap.add_argument("--answered", action="store_true", help="用户已答复")
    ap.add_argument("--check-asked", action="store_true", help="只查'该自己查却拿去问'")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    g = AskGate(cfg)
    if a.demo:
        sys.exit(demo(cfg))
    if a.check_asked:
        r = g.check_asked(a.ask)
        print(("✓ 没有'该自己查却拿去问'的问题" if r["ok"]
               else f"✗ {r['n_bad']} 条该自己查："))
        for row in r["rows"]:
            if row["self_answerable"]:
                print(f"  · {row['q'][:70]} → 先查：{'、'.join(row['sources'])}")
        sys.exit(0 if r["ok"] else 1)
    if a.text_file:
        rep = g.check_text(Path(a.text_file).read_text(encoding="utf-8", errors="replace"))
        print(rep["summary"])
        sys.exit(0 if rep["ok"] else 1)
    plan = Path(a.plan_file).read_text(encoding="utf-8", errors="replace") if a.plan_file else ""
    if a.note or plan:
        rec = g.scan(f"{a.note or ''}\n{plan}")
        try:
            g.gate(rec, asked=a.ask, answered=a.answered)
            print("✓ 开工前提问闸：放行")
            sys.exit(0)
        except MustAsk as e:
            print(str(e))
            sys.exit(1)
    print("用法：--demo / --note <动作> [--plan-file <计划>] [--ask <问题>]... [--answered] "
          "/ --text-file <报告> / --check-asked --ask <问题>")
