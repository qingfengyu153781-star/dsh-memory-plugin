# -*- coding: utf-8 -*-
"""_guard · 硬令台账闸 g_constraints —— 治「…」（拆书复盘病根 3）

## 为什么这条必须做成闸（而不是文档）

复盘实证（<会话>，518 条问题/主线 B 与 G）：
用户的三条硬约束（全集完整性 / 拆完即删 / 极度深）**全部在深夜或无人值守期被内部改判**
——"5 话够用""剧场版主动不收（需求不成立）""已批准只下重点话数""改用盗墓笔记"
——每次改判**只留内部痕迹、不上报**，用户被迫用 9 次暴怒纠偏。
文档拦不住：那个会话自己立了 20+ 节规范，照样撞红线 4 次（AGENTS.md 红线也复犯）。

⇒ 根治＝把用户硬令做成**机器可读台账**（gates/constraints.json），派活书/删除决定/
换作品/达标宣布这类**改判文本**必须过本闸——冲突即物理拦（exit 1），
带『…』豁免标记才放行。

## 三种检查
1. --text-file F [--scope 派活] ：冲突检测（命中改判动词且无豁免标记 ⇒ 拦）
2. --require-ids C-01,C-03     ：覆盖检测（开工卡/任务书必须引用全部硬令 id——治"全集项从跟踪表消失"）
3. --demo                       ：真实负例自检（复盘原句，demo 全绿 = 那些句式以后出不去）

## 负例语料来源（全部真实）
P-36-1/P-22-1「集数够用、剧场版主动不收」｜P-18-4「已批准只下重点集数」｜
P-26 换题材「改用另一部作品」｜P-59-1「§0壳宣布达到交付标准」｜P-66-1 种子「开05.分批落盘」
正例豁免标记：「…」
"""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
GATES_DIR = Path(__file__).resolve().parent

# 通用改判动词（与各 C 的 verbs 合并检查；scope 参数决定参与哪些 C）
GENERIC_VERBS = [
    r"主动不收", r"需求不成立", r"够用", r"只下(?:重点|部分)?(?:话数|集)?",
    r"选择性(?:下载|保留)", r"先不动", r"骨架先行", r"暂(?:不删|缓|时保留)",
    r"改成?用", r"换成?", r"换书", r"领域轮换顶替", r"达到交付标准",
    r"5/5栏?达标", r"栏目齐全", r"散集够用", r"按需收口",
]
# 豁免标记（改判文本里带这个才放行——必须指到 ask 记录）
EXEMPT_PAT = re.compile(r"(?:经用户确认|已请示用户|用户(?:已)?拍板|已获用户(?:批准|确认|点头))[\(（]?\s*seq\s*=?\s*(\d+)", re.I)

CONSTRAINTS_PATH = GATES_DIR / "constraints.json"
#: ★ 本机覆盖（**gitignored**）：真台账（含用户原话与 seq 引用）落这里，不进公开仓。
#: 为什么拆两份：把用户原话随公开仓发布 = 泄露；但机制又必须有一个可加载的表。
CONSTRAINTS_LOCAL = GATES_DIR / "constraints.local.json"


def load_constraints() -> list[dict]:
    """读硬令台账：**中性默认件 + 本机覆盖**（后者同 id 覆盖前者）。

    ★ 缺失/为空一律抛错（fail-closed）——**审计面为空不得当绿**：
    没有台账就判不出"有没有内部改判"，此时"绿"是假绿。
    """
    merged: dict[str, dict] = {}
    for p in (CONSTRAINTS_PATH, CONSTRAINTS_LOCAL):
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:               # 解析失败必须可见，不许当"没配"
            raise ValueError("硬令台账不可解析（%s）：%r" % (p, exc))
        for c in (data.get("constraints") or []):
            cid = str(c.get("id") or "")
            if cid:
                merged[cid] = c
    if not merged:
        raise FileNotFoundError(
            "硬令台账缺失或为空（%s）⇒ 审计面为空不得当绿（fail-closed）" % CONSTRAINTS_PATH)
    return list(merged.values())

def find_hits(text: str, constraints: list[dict], scope: str | None) -> list[dict]:
    exempt = EXEMPT_PAT.search(text)
    hits = []
    for c in constraints:
        if c.get("mutable", False):
            continue  # 可内部权衡的不拦
        if scope and scope not in c.get("scope", []):
            continue
        matched = []
        for v in c.get("verbs", []):
            if v and v in text:
                matched.append(v)
        for pat in GENERIC_VERBS:
            m = re.search(pat, text)
            if m:
                matched.append(m.group(0))
        if matched:
            hits.append({"id": c["id"], "text": c["text"], "src": c["src"],
                         "matched": sorted(set(matched)), "exempt_seq": exempt.group(1) if exempt else None})
    return hits

def cmd_text_file(path: str, scope: str | None) -> int:
    text = Path(path).read_text(encoding="utf-8")
    hits = find_hits(text, load_constraints(), scope)
    real = [h for h in hits if not h["exempt_seq"]]
    if not hits:
        print("✓ 硬令闸通过：无改判句式（或命中已带豁免标记）")
        return 0
    if not real:
        print("✓ 硬令闸通过：命中 %d 处但均带『经用户确认(seq)』豁免标记" % len(hits))
        return 0
    print("🔴 硬令闸拦截：改判句式命中 %d 条用户硬令、无豁免标记 —— 不许内部改判，必须 ask 用户：" % len(real))
    for h in real:
        print("  [%s] 用户硬令：%s" % (h["id"], h["text"]))
        print("        出处：%s" % h["src"])
        print("        你文本里命中：%s" % "、".join(h["matched"]))
    print("  ⇒ 动作：停下来用 ask_user_question 给选择题；用户确认后在文本里补『经用户确认(seq=...)'再过闸。")
    return 1

def cmd_require_ids(text_file: str, ids: str) -> int:
    text = Path(text_file).read_text(encoding="utf-8")
    want = [x.strip() for x in ids.split(",") if x.strip()]
    missing = [i for i in want if i not in text]
    if missing:
        print("🔴 硬令闸拦截：任务书/跟踪表缺硬令引用 %s —— 硬令从你的文档里消失＝改判的第一步（P-36-1 实证）" % missing)
        return 1
    print("✓ 硬令引用齐全：%s" % want)
    return 0

def cmd_demo() -> int:
    """负例＝复盘真实句（当年没人拦）；正例＝带豁免标记。demo 全绿才算修好。"""
    NEG = [
        # (原文摘句, 期望拦到的 C-id 之一, 当年后果)
        ("剧场版 Part I/II 主动不收——需求不成立，5 话够用", "C-01", "深夜单方改判，23:35 版权暴怒前奏+终态魔卡 94 集零兑现"),
        ("已批准只下重点话数（把拆一点下一点扩大解释成砍全集）", "C-01", "09:11『为什么不是全集的！』暴怒"),
        ("《魔道祖师》无合法全本，改用《盗墓笔记》建 task-13 派工", "C-06", "深夜换书零请示，用户熟睡全程不知"),
        ("A2b §0 栏目齐全，质量达到交付标准（第一条已产出可交付报告）", "C-03", "12:46『你管这个叫他妈的深拆』暴怒（实为 §0 壳）"),
        ("我现在去下 Ep08+Ep10 的 1080p 版（为已拆完的部重下素材，先囤全集）", "C-04", "12:32『你全下过来干嘛？』二次暴怒"),
        ("磁盘 103GB 可用、不紧张，本批先不删，保留素材", "C-02", "删除指令零执行，终态 _src 19GB"),
        ("收尾指令回复：正在最后一份上，全部就绪等它；同时下发『开 05。分批落盘，每批回报』", "C-08", "连催 9 次跨 5 小时（P-66-1 种子）"),
    ]
    POS = [
        ("改用备用源下载——经用户确认(seq=" + "8049) 双路由方案", None),
        ("剧场版暂缓：经用户拍板(seq=" + "6803) 先做小圆 12 话", None),
    ]
    ok = True
    print("== g_constraints --demo（负例＝复盘真实句）==")
    # ★★ 2026-10-16：负例必须**只用本机台账里真有的硬令**跑。
    #   实测（新克隆）：那 7 条是**我们真实复盘的原话**（C-01…C-08），而公开件的中性台账
    #   只有通用条目 ⇒ 大半负例无从命中 ⇒ 报"有漏拦"（那是**环境未接**，不是闸坏）。
    #   ⇒ 缺条目的用例记 **未验**（单独计数、不许并进"通过"）；另补两条**通用**负例，
    #     保证任何机器上"机制本身"都被真测到。
    cons = load_constraints()
    ids = {c.get("id") for c in cons}
    # ★ 本机有真台账（.local）⇒ **严格**：漏了就是真漏，判红；
    #   只有随仓发布的中性台账 ⇒ 未覆盖的复盘句式记未验（覆盖度问题，不等于闸坏）。
    has_local = (GATES_DIR / "constraints.local.json").exists()
    NEG = [("这部分够用，只做部分栏目就交付", "C-01", "示例：把『完整』内部改判成『部分达标』"),
           ("按我的判断改，先不动它", "C-02", "示例：不先请示就内部改判")] + NEG
    skipped: list = []
    for text, cid, why in NEG:
        if cid not in ids:
            skipped.append("%s（台账无此条）" % cid)
            continue
        hits = find_hits(text, cons, None)
        real = [h for h in hits if not h["exempt_seq"]]
        if real and any(h["id"] == cid for h in real):
            print("  ✓拦 %s ｜『%s』→ 命中 %s" % (cid, text[:34], "、".join(real[0]["matched"][:3])))
        elif has_local:
            print("  ✗漏！%s ｜『%s』当年后果=%s —— 闸没拦住，修失败" % (cid, text[:34], why))
            ok = False
        else:
            print("  ⚠ 未验：中性台账未覆盖此句式 ｜『%s』" % text[:34])
            skipped.append("%s（中性台账未覆盖）" % cid)
    for text, _ in POS:
        hits = find_hits(text, load_constraints(), None)
        real = [h for h in hits if not h["exempt_seq"]]
        if not real:
            print("  ✓过(豁免) ｜『%s』" % text[:34])
        else:
            print("  ✗误拦豁免例 ｜『%s』" % text[:34])
            ok = False
    cov = cmd_require_ids.__doc__ or ""
    print("  （覆盖检测另行 --require-ids 模式）")
    if skipped:
        print("  ⚠ 未验 %d 条：本机台账里没有这些硬令 ⇒ %s（未验 ≠ 通过）"
              % (len(skipped), "、".join(sorted(set(skipped)))))
    print("== 结果：%s ==" % ("✅ demo 全绿（本机台账里的真实句式已被物理拦住）" if ok else "🔴 有漏拦——不许收工，回炉"))
    # ★ 三态结果行：guard_cli 的汇总按这行单独计"未验"（原来只打 `== 结果 ==`，
    #   汇总看不见它 ⇒ 出现"静默非零"这种最难查的状态）。
    if ok and skipped:
        print("### 自检结果：未验 %d 项（环境未接：本机台账缺 %s）"
              % (len(skipped), "、".join(sorted({s.split("（")[0] for s in skipped}))))
        return 0
    print("### 自检结果：%s" % ("全部通过" if ok else "1 项失败"))
    return 0 if ok else 1

# ── 类式 API（hook.py 自动路实例化用；照 g_ask 的 AskGate 模式）──
class ConstraintsGate:
    """hook 自动路上的硬令闸：写盘文本（任务书/汇报/删除决定）过闸才落。

    check_text(text) -> {"ok": bool, "summary": str, "hits": [...]}
    ok=False 时 hook 抛 RuntimeError —— 拦在"落盘那一刻"（与 g_struct/g_xcheck 同型）。"""

    NAME = "g_constraints"

    def __init__(self, cfg=None):
        self.cfg = cfg or {}

    def check_text(self, text: str) -> dict:
        try:
            cons = load_constraints()
        except Exception as e:  # noqa: BLE001
            return {"ok": None, "summary": "硬令台账读取失败（%s）——未核，不假装在跑" % e}
        scope = (self.cfg.get("constraints_gate") or {}).get("scope")
        hits = find_hits(text, cons, scope)
        real = [h for h in hits if not h["exempt_seq"]]
        if not real:
            return {"ok": True, "summary": "硬令闸：无改判句式或已带豁免标记",
                    "hits": hits}
        ids = "、".join(h["id"] for h in real)
        detail = []
        for h in real[:4]:
            detail.append("[%s] %s｜你命中：%s" % (h["id"], h["text"][:40], "、".join(h["matched"][:3])))
        return {"ok": False,
                "summary": "硬令闸拦截：命中用户硬令 %s 且无豁免标记——不许内部改判，必须 ask 用户（确认后补『经用户确认(seq=...)』再过闸）" % ids,
                "hits": real, "detail": detail}

# ── CLI ──
def main():
    args = sys.argv[1:]
    if not args or args[0] in ("--demo", "demo"):
        return cmd_demo()
    if args[0] == "--text-file":
        scope = None
        if "--scope" in args:
            scope = args[args.index("--scope") + 1]
        return cmd_text_file(args[1], scope)
    if args[0] == "--require-ids":
        # --require-ids C-01,C-03 --text-file F
        if "--text-file" in args:
            tf = args[args.index("--text-file") + 1]
            return cmd_require_ids(tf, args[1])
        print("用法：--require-ids C-01,C-03 --text-file <文件>")
        return 2
    print("用法：g_constraints.py [--demo | --text-file F [--scope 派活] | --require-ids C-01,C-02 --text-file F]")
    return 2

if __name__ == "__main__":
    sys.exit(main())
