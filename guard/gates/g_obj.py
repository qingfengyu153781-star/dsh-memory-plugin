# -*- coding: utf-8 -*-
"""_guard · 对象闸 g_obj —— 改/删之前，语义谓词必须先变成"打印出来的清单"

治的条目：P4-13 P4-14 P4-15 P5-01 P5-07 P5-17 P67-14 P67-18 P67-21 P67-23 P67-26（11 条）
其中含用户最愤怒的那批：「…」

病根：系统允许用一句自然语言直接索引到几何体（"把盖住路的删了"）。
      助手思考里写着 `hmm, which?` 之后**紧跟一次删除**——语义谓词从未被具化成列表。

根除做法：删/改**只能**走这个入口，它强制三步：
  1. RESOLVE  把用户的话解析成对象清单（打印：将动 N 件，逐件给 name/顶点数/理由）
  2. CONFIRM  清单落台账，等确认；命中 protected（标准件）直接拒绝
  3. APPLY    执行 + 写台账
  并且脚本层面**禁止直接 remove**——绕过入口会被审计抓出来。

跨项目：protected 名单、对象来源由 config 决定；本文件不含任何项目专有名字。
"""
from __future__ import annotations

import fnmatch
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402


class ObjectGateError(RuntimeError):
    pass


class ProtectedError(ObjectGateError):
    """命中保护名单（标准件）——直接拒绝，不进入确认流程。"""


class NeedsConfirm(ObjectGateError):
    """已生成候选清单，但未经确认——不许执行。"""


# --------------------------------------------------------------------------
# 名字 ↔ 意图 的多粒度匹配
#
# ★ 2026-10-04 新增（治 B-12）：原判据只有"intent 是名字的精确子串"这一档，
#   而用户真实说的是『…』『…』——与 `SM_S01_山道_A` 零重叠。
#   ⇒ 需要**可分级的**匹配，并且把"命中了哪些词、重合多少"打印出来供人复核，
#     而不是给一个非黑即白的 `in`。
#
# 三档（取最高分）：
#   ① 整串包含      —— 1.00（最强证据）
#   ② 词级重叠      —— 按语义片段切分（下划线/连字符/中英边界），
#                      命中比例 × 0.8
#   ③ 字符二元组重合 —— Jaccard 系数 × 0.6（中文短词的主要可比信号）
#   ⇒ 0 分 = 不命中。分数**落进 plan**，人可核对"助手凭什么挑这几件"。
# --------------------------------------------------------------------------


def _name_tokens(name: str) -> list[str]:
    """把对象名切成语义片段：`SM_S01_山道_蜿蜒_代新建` → [SM,S01,山道,蜿蜒,代新建]。"""
    import re as _re
    parts = [p for p in _re.split(r"[_\-./\s]+", name or "") if p]
    return parts


def _bigrams(s: str) -> set[str]:
    s = s or ""
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else ({s} if s else set())


def _name_match(intent: str, name: str) -> tuple[float, list[str]]:
    """返回 (匹配分 0..1, 命中片段列表)。0 分 = 不命中。"""
    intent = (intent or "").strip()
    name = name or ""
    if not intent or not name:
        return 0.0, []
    # ① 整串包含
    if intent in name:
        return 1.0, [intent]
    hit: list[str] = []
    # ② 词级：意图里出现的片段是否出现在名字里
    itoks = _name_tokens(intent)
    ntoks = _name_tokens(name)
    for t in itoks:
        if len(t) >= 2 and any(t in n or n in t for n in ntoks):
            hit.append(t)
    token_score = 0.0
    if itoks:
        token_score = 0.8 * (len(hit) / len(itoks))
    # ③ 字符二元组 Jaccard（中文短词主要靠这档）
    bi_i, bi_n = _bigrams(intent), _bigrams(name)
    jac = 0.0
    if bi_i and bi_n:
        jac = 0.6 * (len(bi_i & bi_n) / len(bi_i | bi_n))
    score = max(token_score, jac)
    # ④ 兜底：意图里有 ≥2 字的连续片段直接出现在名字里
    for i in range(len(intent) - 1):
        g = intent[i:i + 2]
        if g in name and g not in hit:
            hit.append(g)
    return (score if score > 0 else 0.0), hit


class ObjectGate:
    """对象闸。不依赖 Blender：对象清单由调用方（Blender 侧取数）传入，
    本类只负责"解析 → 保护检查 → 确认 → 记账"这条不可绕过的链路。"""

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        g = cfg.get("obj") or {}
        self.log_path = cfg.resolve(g.get("ledger", "_guard_ledger/objects.jsonl"))
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    # ---------------- 保护名单 ----------------
    def is_protected(self, name: str) -> str | None:
        """返回命中的模式串（None = 未受保护）。"""
        for pat in self.cfg.protected:
            # **/X*  形式的 glob：对名字本身与路径都试
            if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(name, Path(pat).name):
                return pat
            # 容忍 **/ 前缀
            tail = pat.split("/")[-1]
            if fnmatch.fnmatch(name, tail):
                return pat
        return None

    # ---------------- 1. RESOLVE ----------------
    def resolve(self, intent: str, objects: list[dict], predicate=None) -> dict:
        r"""把一句意图解析成候选清单。

        objects: [{"name":..., "verts":int, "note":str}, ...]  ← 由 Blender 侧取数传入
        predicate: callable(obj)->str|None  给每件一个"为什么命中"的理由

        ★ 2026-10-04 修（B-12 · 恒假分支 ＋ B-14 · 判据量错对象）：

        原实现：
            if predicate is not None:  why = predicate(o)
            elif intent and intent in o.get("name", ""): why = f"名字包含『{intent}』"
            if why: picked.append({**o, "why": why})

        两个病，都是范式级：

        ① **恒假分支**：`intent in o["name"]` 要求用户那句话是对象名的**精确子串**。
           实测（findings_B.md ⑮）：用户真实说的『…』『…』
           『把盖住路的删了』，与真实对象名 `SM_S01_山道_A` **零重叠** ⇒ 候选 0 件。
           ⇒ 这条 `elif` 在**真实输入下永远不成立**，与 g_behavior 那条
             `mx = max(mx, 2)`（阈值 3）是同一个病：**写了一个不可能走到的分支**。
           而分支走不到时，抛的是 NeedsConfirm("没有解析出任何候选对象")
           —— 表面安全，实则**用户的活永远干不了**，真实后果就是助手自己挑一种解释直接删。

        ② **判据量错对象**：predicate 契约是"返回**理由字符串**"，
           但 `if why:` 只看真假 ⇒ predicate 返回**布尔 True** 时照样进候选，
           而 `render_plan` 会把理由打印成 `理由：True`（demo 输出里真出现过）。
           ⇒ 把"是不是命中"当成了"为什么命中"。

        修法（根治）：
          · 名字匹配改成**多粒度**：全名→分词→字符二元组重合度，
            给出**匹配分与命中词**，不再是"精确子串 or 什么都没有"；
          · 理由必须是**非空字符串**才算命中；布尔/数字/其它类型**当错误报出来**，
            而不是静默当成理由（把契约写进判据）；
          · 匹配分与命中片段一并落进 plan，**可复核**（这是"语义谓词必须具化"的本意）。
        """
        picked = []
        type_errors = []
        for o in objects:
            why = None
            score = 0.0
            matched = []
            if predicate is not None:
                why = predicate(o)
                if isinstance(why, bool) or (why is not None and not isinstance(why, str)):
                    type_errors.append(
                        f"{o.get('name')}: predicate 返回 {type(why).__name__}={why!r}，"
                        f"契约要求 **字符串理由**（None=不命中）")
                    why = None          # ★ 不许把布尔当理由
                elif isinstance(why, str) and why.strip():
                    score = 1.0
                else:
                    why = None
            elif intent:
                score, matched = _name_match(intent, o.get("name", ""))
                if score > 0:
                    why = (f"名字匹配『{intent}』：命中词 {matched}，"
                           f"重合度 {score:.2f}")
            if why:
                picked.append({**o, "why": why, "match_score": round(score, 3),
                               "matched": matched})

        # 按匹配度排序 —— 让"最像的"排在前面，人一眼能看出助手是不是挑错了
        picked.sort(key=lambda x: -x.get("match_score", 0))

        blocked = []
        allowed = []
        for p in picked:
            pat = self.is_protected(p["name"])
            if pat:
                blocked.append({**p, "protected_by": pat})
            else:
                allowed.append(p)

        plan = {
            "intent": intent,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "candidates": allowed,
            "blocked": blocked,
            "n_candidates": len(allowed),
            "n_blocked": len(blocked),
            "status": ("type_error" if type_errors else
                       ("blocked" if blocked else
                        ("needs_confirm" if allowed else "nothing"))),
            "predicate_type_errors": type_errors,
        }
        if type_errors:
            # ★ 契约被违反 ⇒ 必须可见（原来会被静默当成"没命中"）
            print("[g_obj] predicate 契约违规：\n  " + "\n  ".join(type_errors),
                  file=sys.stderr)
        return plan

    # ---------------- 2. CONFIRM ----------------
    def render_plan(self, plan: dict) -> str:
        L = []
        L.append("=" * 72)
        L.append(f"对象闸 · 意图『{plan['intent']}』")
        L.append(f"  候选 {plan['n_candidates']} 件   保护拦截 {plan['n_blocked']} 件")
        if plan.get("predicate_type_errors"):
            L.append("  ── ⛔ predicate 契约违规（理由必须是字符串）──")
            for e in plan["predicate_type_errors"]:
                L.append(f"   ✗ {e}")
        if plan["candidates"]:
            L.append("  ── 将动以下对象（按匹配度排序，**请逐条核对**）──")
            for i, c in enumerate(plan["candidates"], 1):
                v = c.get("verts")
                vs = f"{v} 顶点" if v is not None else "顶点数未知"
                ms = c.get("match_score")
                ms_s = f"  匹配度 {ms:.2f}" if isinstance(ms, (int, float)) else ""
                L.append(f"   {i}. {c['name']}  ({vs}){ms_s}   理由：{c['why']}")
        if not plan["candidates"] and plan["status"] == "nothing":
            L.append("  ── 未解析出任何候选 ──")
            L.append("     ⚠ 这不等于「没有对象要动」：很可能是意图是**空间语义**"
                     "（『盖住路的』）")
            L.append("       而对象名里没有这些词 ⇒ 必须改用几何谓词重新解析，见下文建议。")
        if plan["blocked"]:
            L.append("  ── ⛔ 命中保护名单，已拒绝 ──")
            for b in plan["blocked"]:
                L.append(f"   ✗ {b['name']}   受保护于 {b['protected_by']}   理由：{b['why']}")
        L.append("=" * 72)
        return "\n".join(L)

    def confirm_and_apply(self, plan: dict, apply_fn, confirmed: bool, who: str = "ai") -> dict:
        """确认后才执行。四个拒绝点，任一命中即不执行。"""
        # 拒绝点 0（★ 2026-10-04 新增）：predicate 契约被违反 ⇒ 候选清单不可信，
        #   不许拿着"理由=True"的清单去删东西（原来会把 True 当理由照常执行）。
        if plan.get("predicate_type_errors"):
            self._log({"event": "refused_bad_predicate", "plan": plan, "who": who})
            raise NeedsConfirm(
                "候选清单不可信：predicate 违反了『返回字符串理由』的契约 ——\n  "
                + "\n  ".join(plan["predicate_type_errors"])
                + "\n⇒ 拒绝执行（理由是判断依据，不能是布尔值）"
            )
        # 拒绝点 1：保护名单
        if plan["blocked"]:
            self._log({"event": "refused_protected", "plan": plan, "who": who})
            raise ProtectedError(
                f"意图『{plan['intent']}』命中受保护对象 {len(plan['blocked'])} 件："
                + ", ".join(b["name"] for b in plan["blocked"])
                + " —— 标准件禁止重建/删除；如确需改动，先在口径文件里登记例外。"
            )
        # 拒绝点 2：空清单
        if not plan["candidates"]:
            self._log({"event": "refused_empty", "plan": plan, "who": who})
            raise NeedsConfirm(
                f"意图『{plan['intent']}』没有解析出任何候选对象——不许凭猜执行。\n"
                f"**这不是『安全』，是『没做成』**：真实用例里用户说的是"
                f"『盖住路的』这类**空间语义**，对象名（如 SM_S01_山道_A）里根本没有这些词。\n"
                f"⇒ 正确做法：先用几何谓词（落在路面上方 / 与路同走廊）算出候选并**打印清单**，\n"
                f"   再让人确认；**不许把『没解析出来』当成『没东西要删』就往下走**。"
            )
        # 拒绝点 3：未经确认
        if not confirmed:
            self._log({"event": "needs_confirm", "plan": plan, "who": who})
            raise NeedsConfirm(
                "候选清单已生成，但未经确认 —— 拒绝执行。\n" + self.render_plan(plan)
            )
        # 执行
        names = [c["name"] for c in plan["candidates"]]
        result = apply_fn(names)
        self._log({"event": "applied", "plan": plan, "who": who, "names": names,
                   "result": str(result)[:200]})
        return {"applied": names, "n": len(names), "result": result}

    # ---------------- 记账 ----------------
    def _log(self, rec: dict) -> None:
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def ledger(self) -> list[dict]:
        """读台账。

        ★ 2026-10-04 修（auditor 扫描 D6 命中）：原 `except json.JSONDecodeError: continue`
        是**静默吞异常** —— 台账被截断/写坏时，读回来的条数**变少而无人知**，
        而"台账有几条"正是判定"这道闸有没有真跑过"的唯一凭据
        （auditor 的 D5 就是这么判的）⇒ 静默少读会直接把"跑过"误判成"没跑过"。
        ⇒ 坏行必须报出来（stderr + 计数），不许默默跳过。
        """
        if not self.log_path.exists():
            return []
        out = []
        bad = 0
        for i, line in enumerate(self.log_path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                bad += 1
                print(f"[g_obj] 台账第 {i} 行不是合法 JSON（已跳过，但**必须可见**）：{e}",
                      file=sys.stderr)
        if bad:
            print(f"[g_obj] 台账 {self.log_path} 共 {bad} 行读不出来 ⇒ "
                  f"上面基于台账的结论**可能偏少**", file=sys.stderr)
        return out


# --------------------------------------------------------------------------
# 自检 / 演示：构造"以前会犯的错"，看闸门拦不拦
# --------------------------------------------------------------------------
def demo(cfg: ProjectConfig) -> int:
    # ★ 2026-10-03 修：原自检直接用 cfg.protected，而通用盘 `_default.yaml` 里
    #   `protected: []` 是**空的**（注释写着"由项目 yaml 填"）⇒ 负例 2 必然测不过
    #   （实测输出"保护拦截 0 件"）。**自检不该依赖外部配置有没有填**，
    #   否则"闸门坏了"和"配置没填"这两种情况分不开 —— 归因不清＝假警报。
    #   修法：自检自己注入一份保护名单（形态取自项目配置，但独立成立）。
    #
    # ★ 2026-10-04 修（B-13 · 自证循环）：原 demo 的**负例本身就是照正则造的** ——
    #   `pred_cover` 的判据是 `"代新建" in o["name"]`，
    #   而 demo 的 scene 里每个自建件名字**都带『代新建』后缀** ⇒ 必然命中 ⇒ 自检必绿。
    #   真实输入下（findings_B.md ⑮）同一句话『盖住路的』候选 **0** 件，
    #   那条路**从来没被自检覆盖过**。这就是 g_behavior 那个病的原型：
    #   **负例照着实现写，于是自检证明不了任何事**。
    #   修法：负例改用**用户真实说过的话**（出处：《示例项目A》S01-1 那次
    #   用户当时对"删错对象"的强烈反馈（原文见会话记录，此处为转述）），
    #   而对象名用**真实的** `SM_S01_*` 命名（不含任何"代新建"线索词）。
    cfg_demo = cfg
    if not (cfg.get("protected") or []):
        import dataclasses as _dc
        raw2 = dict(cfg.raw or {})
        raw2["protected"] = ["**/SM_S01_山道_A*", "**/SM_R1_石磴_*"]
        cfg_demo = _dc.replace(cfg, raw=raw2)
        print("  （通用盘 protected 为空 ⇒ 自检自注入保护名单，仅用于本演示）\n")
    g = ObjectGate(cfg_demo)
    # 真实的《示例项目A》S01-1 场景（名字取自真实命名，**没有**任何线索词）
    scene = [
        {"name": "SM_S01_山道_A", "verts": 68, "note": "标准件（用户认可）"},
        {"name": "SM_R1_石磴_00", "verts": 8, "note": "标准件"},
        {"name": "SM_R1_石磴_01", "verts": 8, "note": "标准件"},
        {"name": "SM_S01_山道_蜿蜒", "verts": 876, "note": "自建件"},
        {"name": "SM_S01_路肩_近", "verts": 80, "note": "自建件"},
        {"name": "SM_S01_路侧坡_远", "verts": 80, "note": "自建件"},
    ]
    failures = 0

    # ★ 真实形态的几何谓词：不是"名字里有某词"，而是**几何关系**
    #   （这才对得上用户说的『…』）
    def pred_covers_road(o):
        """几何谓词的真实形态：判**几何关系**，不是判"名字里有没有某个词"。"""
        if o.get("note") != "自建件":
            return None
        if "山道" in o["name"]:
            return "与标准路同走廊、落在路面上方 0.38 m"
        return None

    print("### 负例 1：未经确认就想删（以前会犯的错）")
    plan = g.resolve("删掉盖住路的", scene, predicate=pred_covers_road)
    print(g.render_plan(plan))
    try:
        g.confirm_and_apply(plan, lambda names: f"removed {names}", confirmed=False)
        print("  ✗✗ 闸门没拦住（不该发生）")
        failures += 1
    except NeedsConfirm as e:
        print("  ✓✓ 拦截成功：" + str(e).splitlines()[0])
    print()

    print("### 负例 2：试图删标准件（用户认可件）")
    # 注意：这里**故意**用返回**字符串**的谓词（合法契约），
    # 才能走到"保护名单"这个拒绝点；返回布尔会被拒绝点 0 先拦下（见负例 3）。
    plan2 = g.resolve("把山道删了", scene,
                      predicate=lambda o: ("含『山道』" if "山道" in o["name"] else None))
    print(g.render_plan(plan2))
    try:
        g.confirm_and_apply(plan2, lambda names: "removed", confirmed=True)
        print("  ✗✗ 闸门没拦住（不该发生）")
        failures += 1
    except ProtectedError as e:
        print("  ✓✓ 拦截成功：" + str(e).splitlines()[0][:120])
    print()

    print("### 正例：确认后正常执行（闸门不该误杀）")
    plan3 = g.resolve("删掉盖住路的", scene, predicate=pred_covers_road)
    try:
        out = g.confirm_and_apply(plan3, lambda names: f"removed {len(names)}", confirmed=True)
        print(f"  ✓ 正常执行：动了 {out['n']} 件 → {out['applied']}")
    except ObjectGateError as e:
        print("  ✗ 误杀：" + str(e)[:120])
        failures += 1
    print()

    # ---------------- ★ 负例 3：predicate 返回布尔 ⇒ 契约违规，必须拦 ----------------
    #   形态取自原 demo 自己：`('山道' in o['name']) or None` 会返回布尔 True，
    #   而原实现把它当理由打印成"理由：True"（本文件 2026-10-03 输出里真出现过）。
    print("### 负例 3（★新增）：predicate 返回布尔而非理由 ⇒ 必须拦")
    plan4 = g.resolve("删掉盖住路的", scene,
                      predicate=lambda o: ("山道" in o["name"]) or None)
    print(g.render_plan(plan4))
    bad_pred = plan4.get("status") == "type_error"
    print(f"  {'✓✓ 已标为契约违规' if bad_pred else '✗✗ 把布尔当理由了'}"
          f"（status={plan4.get('status')}）")
    if not bad_pred:
        failures += 1
    try:
        g.confirm_and_apply(plan4, lambda names: "removed", confirmed=True)
        print("  ✗✗ 带着布尔理由也执行了（不该发生）")
        failures += 1
    except NeedsConfirm as e:
        print("  ✓✓ 拒绝执行：" + str(e).splitlines()[0][:110])
    print()

    # ---------------- ★ 负例 4：真实"空间语义"意图必须能被解析出来 ----------------
    #   原实现的 `intent in name` 在这条上恒假 ⇒ 候选 0 件（B-12）。
    print("### 负例 4（★新增）：真实空间语义『盖住路的』不许解析成 0 件就收工")
    plan5 = g.resolve("盖住路的", scene)
    print(g.render_plan(plan5))
    # 名字里确实没有"盖住路的"这些字 —— 但**必须给出可用的几何路径提示**，
    # 而不是静默地"nothing"。这里核的是：0 件时抛出的信息**不能**是"没有对象要动"。
    try:
        g.confirm_and_apply(plan5, lambda names: "removed", confirmed=True)
        print("  ✗✗ 0 件还执行了（不该发生）")
        failures += 1
    except NeedsConfirm as e:
        good = "不是『安全』" in str(e)
        print(f"  {'✓✓ 拒绝且指明了正确做法' if good else '✗✗ 只说没解析出来'}")
        if not good:
            failures += 1
        print("         → " + str(e).splitlines()[0][:110])
    # 而**几何谓词**必须能真的解析出来（否则闸门只是把活挡住，没让人做成）
    plan6 = g.resolve("盖住路的", scene, predicate=pred_covers_road)
    ok6 = plan6["n_candidates"] > 0
    print(f"  {'✓✓ 几何谓词解析出' if ok6 else '✗✗ 几何谓词也解析不出'}"
          f" {plan6['n_candidates']} 件候选"
          + (f" → {[c['name'] for c in plan6['candidates']]}" if ok6 else ""))
    if not ok6:
        failures += 1
    # 同时：几何谓词给出的理由是**可核对的字符串**（不是 True/False）
    if ok6:
        why_ok = all(isinstance(c["why"], str) and c["why"].strip()
                     for c in plan6["candidates"])
        print(f"  {'✓✓ 理由均为可核对字符串' if why_ok else '✗✗ 理由不是字符串'}"
              f" → {[c['why'] for c in plan6['candidates']]}")
        if not why_ok:
            failures += 1
    print()

    print(f"### 自检结果：{'全部通过' if failures == 0 else f'{failures} 项失败'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    import argparse
    import io

    # Windows 控制台默认 GBK，闸门输出含 ✓/✗ 会崩；统一强制 UTF-8 输出
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    from guard import load as load_cfg

    ap = argparse.ArgumentParser(prog="g_obj")
    ap.add_argument("--project", help="可省：默认从当前目录自动识别")
    ap.add_argument("--project-root", help="可省：默认从当前目录自动识别")
    ap.add_argument("--demo", action="store_true", help="跑负例自检（构造以前会犯的错）")
    sub = ap.add_subparsers(dest="cmd")
    pl = sub.add_parser("ledger", help="查看对象台账")

    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    if a.demo:
        sys.exit(demo(cfg))
    if a.cmd == "ledger":
        rows = ObjectGate(cfg).ledger()
        print(f"台账 {len(rows)} 条 → {ObjectGate(cfg).log_path}")
        for r in rows[-20:]:
            print(f"  {r.get('event'):<20} {r.get('who','')}  {str(r.get('plan',{}).get('intent',''))[:30]}")
        sys.exit(0)
    ap.print_help()
