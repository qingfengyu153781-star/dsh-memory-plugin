# -*- coding: utf-8 -*-
"""_guard · 要求锚闸 g_brief —— 治「越做越精细、方向越跑越偏」

## 为什么有这道闸（2026-10-04 立，用户点破）

**用户原话**：「…」

**实测三条结论**（全部可复跑）：
  ① 闸本身没问题 —— 41 条规矩 **41/41 有闸文件、41/41 在自动路上**；
  ② 注意力编排器在工作 —— 282 条记录 / 179 成功；
  ③ **41 条规矩里，"管用户要求有没有被执行"的 = 0 条。** ← 病根在这里

**结构性盲区（一句话）**：
  > 闸能保证「你做的这件事**做得对不对**」，
  > 保证不了「…」。

**实证代价（示例项目A会话 <会话>，142 轮）**：
  · 构件层面几乎全合规（有倒角、有收缝、GAP=0、无 6 面盒）；
  · 但整座山的方向从头错到尾 —— **42 轮返工后整座山被用户要求删除**（5,279 件 / 280,934 面）；
  · 用户 8 条要求里 R1（依剧本分镜）/R6（要求精细）/R8（要看到）三条**全程未达标**，
    而**没有任何一道闸在任一轮把这件事拦下来**。

## 这道闸治什么

**把"用户要求"变成一份机器可对账的锚**（`BRIEF.md`），并在两个点上强制对账：

  · **开轮**：把用户原话 + goal 落成 BRIEF（**逐字抄**，不许转述）
  · **收尾**：凡出现「完成 / 达标 / 已交付 / 通过」字样 ⇒ 必须逐条对账；
    **BRIEF 里有未覆盖项 ⇒ 拦下**，不许宣布完成。

## 判据（三条，都可机器判）

  B1 **原话逐字**：BRIEF 里每条要求必须能在会话原文里逐字命中（防"转述成用户希望…"）
  B2 **未覆盖不许说完成**：报告若声明完成，而 BRIEF 有 `状态≠已兑现` 的条目 ⇒ 红
  B3 **声称"用户选了 X"必须指到轮次**：出现「…」等措辞时，
     必须给出 `turn=N` / `seq=N` / 逐字原话；给不出 ⇒ 红
     （实证：示例项目A seq=… 写「**你选了「白模级」**」，而该决定实际在 seq=… —— 
      **有出处但两次引用都没写**；同类措辞在会话里反复出现且从不带出处）

## 用法
```bash
python _guard/gates/g_brief.py --demo                        # 三条判据自检（正例+负例）
python _guard/gates/g_brief.py --init --session <会话.jsonl> \\
       --goal "<goal 原文>" --out BRIEF.md                   # 开轮：落锚
python _guard/gates/g_brief.py --text-file <报告> --brief BRIEF.md   # 收尾：对账
```

跨项目：不含示例项目A专有内容；BRIEF 路径与关键词可在 `_guard/projects/<名>.yaml` 覆盖。
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

DEFAULTS = {
    # BRIEF 落点（相对项目根）
    "brief_path": "_guard_ledger/BRIEF.md",
    # 对账台账
    "ledger": "_guard_ledger/brief.jsonl",
    # 「宣布完成」的触发词 —— 出现这些就必须对账
    #
    # ★ 2026-10-04 用真实语料回归时补：**原名单全部漏检**。
    #   示例项目A会话里 4 次真实"自宣交付"用的都是**标题式措辞**
    #   （`# 交付：示例项目A世界（白模）` / `## 本轮做了什么`），
    #   **一个"已完成/达标"都没写** —— 它写的是"交付"这个动作名。
    #   ⇒ 名单必须覆盖：① 状态词 ② **动作名词**（交付/提交/成果/验收）③ 表格型"证据"栏。
    "done_pats": [
        r"已完成", r"已交付", r"达标", r"通过验收", r"全部完成", r"收工",
        r"目标(?:已)?达成", r"全部兑现", r"可以交付", r"MVP\s*完成",
        # ② 动作名词式（真实语料里最常出现的形态）
        r"^#+\s*交付", r"^#+\s*成果", r"^#+\s*验收", r"^#+\s*提交",
        r"\n#+\s*交付", r"\n#+\s*成果", r"\n#+\s*验收",
        r"交付物\s*[:：]", r"交付目录", r"本轮交付",
        # ③ 表格型：把"目标要求"逐项标成有证据
        r"目标(?:四项|三项|全部)的证据", r"目标的证据",
        r"验收(?:通过|结论)", r"判据图.{0,6}(?:通过|✓)",
    ],
    # 「…」的措辞 —— 出现这些必须带出处
    "cite_user_pats": [
        r"你(?:先前)?选(?:了|的)", r"按(?:你|您)的(?:要求|意思|选择)",
        r"用户(?:已)?(?:选择|决定|裁定|确认)", r"你(?:说|要)过",
        r"遵照(?:你|您)的", r"依(?:你|您)的",
    ],
    # 出处的形态：turn=N / seq=N / dsh:session...#turn=N / 逐字引号
    "provenance_pats": [
        r"turn\s*[=＝]\s*\d+", r"seq\s*[=＝]\s*\d+",
        r"dsh:[0-9a-zA-Z-]{6,}#turn=\d+",
        r"[「\"][^」\"]{8,}[」\"]",          # 逐字引号 ≥8 字
    ],
    # BRIEF 条目的状态词
    "status_done": ["已兑现", "已覆盖", "已完成", "达标", "done", "✓"],
    "status_open": ["未兑现", "未覆盖", "未完成", "未达标", "open", "✗", "待办", "违反"],
}


def _cfg_of(cfg: ProjectConfig) -> dict:
    d = dict(DEFAULTS)
    for k in ("done_pats", "cite_user_pats", "provenance_pats",
              "status_done", "status_open"):
        d[k] = list(DEFAULTS[k])
    over = cfg.get("brief") or {}
    for k, v in over.items():
        if isinstance(v, list) and isinstance(d.get(k), list):
            d[k] = list(d[k]) + [x for x in v if x not in d[k]]
        else:
            d[k] = v
    return d


# ══════════════════════════════════════════════════════════════════════════
# BRIEF 解析 / 生成
# ══════════════════════════════════════════════════════════════════════════

BRIEF_ROW = re.compile(
    r"^\|\s*(?P<id>[A-Za-z]?-?\d+)\s*\|\s*(?P<req>.+?)\s*\|\s*(?P<status>[^|]*?)\s*\|"
    r"\s*(?P<ev>[^|]*?)\s*\|?\s*$")


class BriefFailed(RuntimeError):
    """要求锚对账未通过 —— **不许宣布完成**（由 hook 抛出，阻断落盘）。"""


class BriefGate:
    NAME = "g_brief"

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self.K = _cfg_of(cfg)
        self.brief_path = cfg.resolve(self.K["brief_path"])
        self.ledger = cfg.resolve(self.K["ledger"])
        self.ledger.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 解析
    def parse_brief(self, path: Path | None = None) -> list[dict]:
        """读 BRIEF.md 的要求表。行格式：
        `| R1 | 要求原文（逐字） | 已兑现/未兑现 | 证据 |`
        """
        p = path or self.brief_path
        if not p.exists():
            return []
        rows = []
        for line in p.read_text(encoding="utf-8", errors="replace").split("\n"):
            if not line.strip().startswith("|"):
                continue
            if re.match(r"^\|\s*[-: ]+\|", line):        # 分隔行
                continue
            m = BRIEF_ROW.match(line.strip())
            if not m:
                continue
            rid, req, status, ev = (m.group("id"), m.group("req"),
                                    m.group("status"), m.group("ev"))
            if req.strip() in ("要求（原文逐字）", "要求", "用户要求"):
                continue                                  # 表头
            rows.append({"id": rid.strip(), "req": req.strip(),
                         "status": status.strip(), "evidence": ev.strip()})
        return rows

    def is_open(self, row: dict) -> bool:
        s = (row.get("status") or "").lower()
        if any(k.lower() in s for k in self.K["status_open"]):
            return True
        if any(k.lower() in s for k in self.K["status_done"]):
            return False
        return True                                       # 状态不明 ⇒ 按未兑现算（保守）

    # ---------------------------------------------------------------- B1
    def check_verbatim(self, rows: list[dict], corpus: str) -> dict:
        """B1：每条要求必须能在原文里**逐字**命中 —— 防"转述成用户希望…"。"""
        res = []
        for r in rows:
            # 逐字判据：取要求里最长的 8 字连续片段，看是否出现在原文
            txt = re.sub(r"\s+", "", r["req"])
            hit = False
            for n in (12, 10, 8):
                if len(txt) < n:
                    continue
                for i in range(0, len(txt) - n + 1, max(1, n // 2)):
                    if txt[i:i + n] in corpus:
                        hit = True
                        break
                if hit:
                    break
            res.append({**r, "verbatim": hit,
                        "detail": ("逐字命中原文" if hit else
                                   "**原文里找不到逐字对应** ⇒ 疑似转述/脑补，不是用户原话")})
        bad = [x for x in res if not x["verbatim"]]
        return {"ok": not bad, "rows": res, "n_bad": len(bad)}

    # ---------------------------------------------------------------- B2
    def check_completion(self, text: str, rows: list[dict]) -> dict:
        """B2：凡**本报告自己**宣布完成，BRIEF 不许有未兑现项。

        ★ 关键区分（本闸第一次上真实语料就踩到的假阳性）：
          报告里出现「达标」不等于**本报告在宣布自己达标** ——
          它可能在**描述历史**（"Lead 宣布精细达标，12 分钟后被骂穿"）
          或**否定**（"最终判词：未达标"）。
          实测：示例项目A `REPORT.md` 3 处触发词**全是**这两种情况，
          若不过滤 ⇒ **误杀**（正是本闸要防的"量错对象"）。

          判据：命中处出现 **否定词** 或 **引述标记** ⇒ 不算"宣布完成"。
        """
        NEG = (r"未达标|没达标|不达标|未完成|没完成|未交付|未兑现|不能交付|不许|不得|"
               r"失败|推翻|判废|撤销|被骂|骂穿|打回|退回|否认|不是|并非|无法|"
               r"声称|宣称|自宣|曾|一度|当时|原本|号称|自称")
        QUOTE = r"[「『\"']|引述|原话|转述|据"
        claims = []
        for p in self.K["done_pats"]:
            for m in re.finditer(r"[^\n。；]{0,40}" + p + r"[^\n。；]{0,40}", text):
                seg = m.group(0)
                # ★ 往前多看 30 字：否定/引述常在触发词之前（"最终判词：未达标"）
                i = max(0, m.start() - 30)
                ctx = text[i:m.end()]
                if re.search(NEG, ctx) or re.search(QUOTE, ctx):
                    continue                      # 否定或引述 ⇒ 不是"本报告宣布完成"
                claims.append(seg)
        claims = list(dict.fromkeys(claims))[:20]
        opened = [r for r in rows if self.is_open(r)]
        if not claims:
            return {"ok": True, "claims": [], "n_claim": 0, "open": opened,
                    "detail": "报告未宣布完成 ⇒ 不触发对账"}
        if opened:
            ids = "、".join(r["id"] for r in opened[:12])
            return {"ok": False, "claims": claims, "n_claim": len(claims), "open": opened,
                    "detail": f"宣布完成（{len(claims)} 处），但 BRIEF 里 **{len(opened)} 条未兑现**（{ids}）"
                              " ⇒ **不许宣布完成**；须逐条写出未覆盖项"}
        if not rows:
            # ★ 无 BRIEF 却有完成声明 ⇒ 由 check_report 升级为红（此处如实标注）
            return {"ok": True, "claims": claims, "n_claim": len(claims), "open": [],
                    "no_brief": True,
                    "detail": f"宣布完成/交付 {len(claims)} 处，但**无 BRIEF 锚** ⇒ 无从对账"}
        return {"ok": True, "claims": claims, "n_claim": len(claims), "open": [],
                "detail": f"宣布完成 {len(claims)} 处，BRIEF 全部已兑现 ✓"}

    # ---------------------------------------------------------------- B3
    def check_user_citation(self, text: str) -> dict:
        """B3：声称"用户选了/用户要求"，必须指到轮次或逐字原话。"""
        hits = []
        for p in self.K["cite_user_pats"]:
            hits += re.findall(r"[^\n。；]{0,60}" + p + r"[^\n。；]{0,60}", text)
        hits = list(dict.fromkeys(hits))[:20]
        rows = []
        for h in hits:
            has = any(re.search(p, h) for p in self.K["provenance_pats"])
            rows.append({"claim": h[:80], "ok": has,
                         "detail": ("带出处" if has else
                                    "**声称用户意图却无出处** ⇒ 必须给 turn=N/seq=N 或逐字原话")})
        bad = [r for r in rows if not r["ok"]]
        return {"ok": not bad, "rows": rows, "n_bad": len(bad), "n_claim": len(rows)}

    # ---------------------------------------------------------------- 汇总
    def check_report(self, text: str, brief: Path | None = None,
                     corpus: str = "", allow_unseeded: bool = False,
                     session_id: str = "") -> dict:
        # ★ 修正⑤：报告侧也按会话取锚 —— 无显式 brief 时优先用本会话的锚文件
        if brief is None and (session_id or "").strip():
            brief = self.session_brief_path(session_id)
        rows = self.parse_brief(brief)
        # ★★★ 2026-10-04 根治（用户：「…」）：
        #   原实现的漏洞不在"漏了哪个判据"，而在**"无锚"这个状态被奖励**：
        #     · 无锚 + 未宣布完成 ⇒ b2.ok=True ⇒ 整体绿 ⇒ 写得越含糊越安全
        #     · 无锚 + 宣布完成   ⇒ 才拦（no_brief_but_claims）
        #   实测物证：`brief.jsonl` 39 次 report 里**绝大多数 n_rows=0**（无锚），
        #   同一分钟内 ok=True(n_rows=0) 与 ok=False(n_rows=39) 交替出现 ——
        #   **"有没有锚"取决于谁在跑，不取决于事实**。
        #   ⇒ 根治不是"再加一条判据"，而是**让"无锚"不可能长期存在**：
        #     ① 无锚时判 **"未核"（ok=False）**，不再默认绿；
        #     ② 由 hook 在**会话首轮自动落锚**（见 hook.py `_brief_autoseed`），
        #        不依赖"谁记得跑 --init"——那正是 AGENTS.md 反复批判的"靠记性"。
        #   豁免：显式传 `allow_unseeded=True`（由 hook 在"确认是空会话/非交付轮"时使用）。
        #   ★★ 同日三修（全量自检抓出误杀）：B0 也必须遵守**三态**。
        #   原实现 `b0_ok = bool(rows)` ⇒ **无锚一律判红** ——
        #   实测误杀：「本轮只做了地形，尚未完成，无交付。」（无锚、也没宣布完成）
        #   ⇒ 把"这道闸本轮没有对象"记成了"这份报告不合格"。
        #   正确语义：**无锚 = 未核**；只有在"无锚 **且** 宣布了完成"时才判红
        #   （那正是"没有锚就想宣布做完"—— 必须拦）。
        b0_ok = bool(rows)
        b0_detail = (f"锚已就绪（{len(rows)} 条）" if rows else
                     "**无锚**：`BRIEF.md` 不存在或为空 ⇒ 本闸本轮**未核**（不算通过）；"
                     "若本报告宣布完成，则升级为拦。修法：hook 会在会话首轮自动落锚；"
                     "或手动 `g_brief.py --init --session <jsonl>`")
        b1 = self.check_verbatim(rows, corpus) if (rows and corpus) else {
            # ★ 2026-10-04 修「跳过记成通过」的假绿（实测：detail 写"未核"而 ok=True）。
            #   跳过 ≠ 通过。未核就是未核，不许当绿。
            #
            # ★★ 同日再修（全量自检抓出）：**"未核"也不等于"不通过"** ——
            #   原实现 `ok: False` ⇒ 任何没提供语料的调用**一律判红**，
            #   实测误杀：「本轮只做了地形，尚未完成，无交付。」（既没 BRIEF 也没语料）
            #   ⇒ **把"没做这件事"和"做错了这件事"混为一谈**，
            #     而本项目自己的纪律写得明明白白：「**未核 ≠ 不通过**」（[库 P152 同族]）。
            #   正确三态：ok=True（核过且过）/ ok=False（核过且不过）/ **ok=None（未核）**。
            #   ⇒ 未核时**不参与 ok 总判定**，但**在 summary 里显式写出"未核"**，
            #     并且如果报告宣布了完成 ⇒ 由下面的 no_brief_but_claims 分支接管判红。
            "ok": None, "rows": [], "n_bad": 0, "skipped": True,
            "detail": "未提供原文语料 ⇒ B1 未核（**不算通过，也不判为不通过**）"}
        # ★★ 关键：**无论有没有 BRIEF，都必须先算"宣布完成了几处"** ——
        #   原实现写成 `check_completion(...) if rows else {ok:True,...}`，
        #   导致"无 BRIEF"时**根本没调用**完成声明检测 ⇒ n_claim 恒 0
        #   ⇒ 下游的"无锚却宣布完成"升级分支**永远不触发**。
        #   这与 g_behavior 的 `mx=max(mx,2)`、g_vision 的"前置 return 挡住机位判据"
        #   **是同一族缺陷：判据永不到达**。修法＝无条件先算，再按有无 rows 分流。
        b2 = self.check_completion(text, rows)
        if not rows:
            b2["detail"] = ("**无 BRIEF** ⇒ 无法对账 —— 开轮时未落锚，本闸失去对象"
                            + (f"；但报告已宣布完成/交付 {b2.get('n_claim', 0)} 处"
                               if b2.get("n_claim") else ""))
        b3 = self.check_user_citation(text)
        # ★★ 2026-10-04 端到端实测抓出的**静默失效**（本闸存在的理由就是治这个）：
        #   原实现"无 BRIEF ⇒ ok=True 放行" ⇒ **没落锚反而更安全** ——
        #   它奖励了"不对账"，惩罚了"老实落锚"（落锚后一旦有未兑现就红）。
        #   实测物证：hook 默认找 `_guard_ledger/BRIEF.md`，样本落在别的名字上，
        #   于是**真实的 4 次自宣交付全部静默放行**（闸报"✓ 无 BRIEF"）。
        #   修法：**"无 BRIEF 且报告宣布完成" ⇒ 判红** ——
        #   你要宣布完成，就必须先有锚；没有锚 = 没对账 = 不许宣布完成。
        no_brief_but_claims = (not rows) and b2.get("n_claim", 0) > 0
        if no_brief_but_claims:
            b2["ok"] = False
            b2["detail"] = (f"宣布完成/交付（{b2['n_claim']} 处），但**开轮没有落 BRIEF 锚** "
                            "⇒ 无法证明这些要求被兑现过 ⇒ 不许宣布完成。"
                            "先跑 `g_brief.py --init --session <jsonl>` 落锚再对账。")
        # ★ 三态语义（2026-10-04 第三次修）：**无锚 = 未核，不是"不合格"**
        #   · 无锚 + 未宣布完成 ⇒ **未核**（b0_ok=None，不参与总判定，但 summary 写明）
        #   · 无锚 + 宣布了完成 ⇒ **拦**（由 no_brief_but_claims 判红）
        #   为什么不再用 `allow_unseeded` 豁免：三态本身就把它表达清楚了 ——
        #   不需要一个"后门开关"，也就没有"拿开关跳过闸门"的风险
        #   （原设计担心的"没落锚反而更安全"，现由 no_brief_but_claims 硬拦堵住）。
        if b0_ok:
            pass                                   # 有锚 ⇒ 已核
        elif no_brief_but_claims:
            b0_ok = False                          # 无锚 + 宣布完成 ⇒ 红
        else:
            b0_ok = None                           # 无锚 + 未宣布完成 ⇒ 未核
        # ★ 三态合并（2026-10-04 全量自检抓出的误杀）：
        #   ok=True  核过且过   / ok=False 核过且不过 / ok=None **未核**
        #   `None` 不参与总判定（未核 ≠ 不通过，[库 P152 同族]），
        #   但 summary 里必须显式写"未核" —— 不许把"没做这件事"记成"做错了这件事"。
        def _pass(v):
            return v is not False          # None（未核）按"不拦"处理，但下面会写清楚

        ok = _pass(b0_ok) and _pass(b1.get("ok")) and b2["ok"] and b3["ok"]
        if b1.get("skipped"):
            ok = ok and not b2.get("n_claim", 0)   # 未核 + 宣布完成 ⇒ 仍拦（不许拿"没语料"当免罪）
        # ★ 三态汇总标签（写进 summary，别让人以为"绿=核过"）
        tri = ("核过" if (b0_ok is True) else
               ("拦" if b0_ok is False else "未核"))
        parts = []
        mark = {"核过": "✓", "拦": "✗", "未核": "○"}[tri]
        parts.append("B0 " + mark + " " + b0_detail[:60])
        if rows:
            parts.append(f"BRIEF {len(rows)} 条（未兑现 {sum(1 for r in rows if self.is_open(r))}）")
        else:
            parts.append("**无 BRIEF**（开轮未落锚）"
                         + ("，且报告宣布完成 ⇒ 拦" if no_brief_but_claims else ""))
        parts.append("B2 " + ("✓" if b2["ok"] else "✗ ") + b2["detail"][:70])
        parts.append("B3 " + ("✓ 用户意图引用带出处" if b3["ok"]
                              else f"✗ {b3['n_bad']}/{b3['n_claim']} 处无出处"))
        rep = {"gate": self.NAME, "ok": ok, "n_rows": len(rows),
               "no_brief_but_claims": no_brief_but_claims,
               "b0": {"ok": b0_ok, "detail": b0_detail},
               "b1": {k: v for k, v in b1.items() if k != "rows"},
               "b2": {k: v for k, v in b2.items() if k != "claims"},
               "b3": {k: v for k, v in b3.items() if k != "rows"},
               "summary": "要求锚： " + "｜".join(parts)}
        self._log({"kind": "report", "ok": ok,
                   "n_rows": len(rows), "n_open": sum(1 for r in rows if self.is_open(r)),
                   "b3_bad": b3["n_bad"]})
        return rep

    # ---------------------------------------------------------------- 落锚
    # ---------------- 会话标识（★修正⑤：按会话隔离，治「多对话串台」） ----------------
    @staticmethod
    def _derive_session_id(session_jsonl: Path, explicit: str = "") -> str:
        """推导会话 id：① 显式参数 ② 存档父目录名（session-xxx / 裸 UUID）。"""
        sid = (explicit or "").strip()
        if sid:
            return sid
        try:
            name = session_jsonl.parent.name
        except Exception:  # noqa: BLE001
            return ""
        import re as _re
        if _re.fullmatch(r"session-[0-9a-fA-F-]{10,}", name) or _re.fullmatch(r"[0-9a-fA-F-]{20,}", name):
            return name
        return ""

    def session_brief_path(self, session_id: str) -> Path:
        """★ 每会话一个锚文件：_guard_ledger/BRIEF_<session_id>.md。"""
        sid = (session_id or "").strip()
        if not sid:
            sid = "default"
        safe = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in sid)
        return self.brief_path.parent / f"BRIEF_{safe}.md"

    @staticmethod
    def _read_session_lines(session_jsonl: Path):
        """读会话 jsonl 行；.zstd 存档自动解压（★修正⑤-c：
        旧版把压缩文件当文本读 ⇒ 每行 json.loads 全失败 ⇒ 恒 0 条 ⇒ 自动落锚永远锚不上）。"""
        if not session_jsonl.exists():
            return
        if session_jsonl.suffix == ".zstd":
            # ★ 2026-10-04 修 zstd 活 bug（实测抓出）：`copy_stream` 返回 (读,写) 字节计数
            #   **元组**，旧代码对元组调 `.getvalue()` ⇒ 恒 AttributeError ⇒ .zstd 存档
            #   **永远读不出** ⇒ 落锚恒 0 条。修法：BytesIO 先留引用，copy_stream 后取值。
            #   ★ 同时撤掉 try/except 吞异常：解压失败必须**抛出去**（init 端退出非 0），
            #   不许 stderr 叫一声然后当 0 条继续走（那正是"失败被记成成功"）。
            import zstandard as _zstd
            import io as _io
            buf = _io.BytesIO()
            with session_jsonl.open("rb") as fi:
                _zstd.ZstdDecompressor().copy_stream(fi, buf)
            raw = buf.getvalue()
            if not raw.strip():
                raise ValueError(f"zstd 解压结果为空：{session_jsonl}")
            for line in raw.decode("utf-8", "replace").split("\n"):
                yield line
            return
        with session_jsonl.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                yield line

    def init_brief(self, session_jsonl: Path, goal: str, out: Path | None = None,
                   session_id: str = "") -> dict:
        """开轮：从会话里抽出**真人原话**，逐字落成 BRIEF_<会话id>.md（★按会话隔离）。

        ★ 2026-10-04 修正⑤（用户实测报「…」）：
          旧版写**全局唯一** BRIEF.md —— 任何会话落的锚都会被注入别的会话
          （实测物证：本轮注入块的 A1 行是**示例项目A会话**的答复 —— 那是审计
          输出文本被误收，不是本会话的答复）。
          新版：**每会话一个 BRIEF_<session_id>.md**；注入端按会话 id 取自己的锚，
          找不到 ⇒ 不注入（**取错锚比无锚更坏**）。

        ★ 关键：只认 `source.kind == 'user'` ——
          示例项目A会话 663 条 user/message 里只有 35 条是真人原话，
          其余是 goal / team-message / runtime-context 注入。
          **把注入当用户原话，是"要求被曲解"的第一大来源。**

        ★ 修正⑤-b：问答答复必须是**可解析的 {"answers":[…]} JSON 载荷** ——
          旧版只做子串匹配 '"answers"'，实测两条假阳性：
          A1 = 审计输出里引用的示例项目A答复（**串台物证**）；
          A2 = 读 hook.py 的工具结果（源码里恰好含 '"answers"' 字样，自我命中）。
        """
        sid = self._derive_session_id(session_jsonl, session_id)
        if out is None:
            out = self.session_brief_path(sid)
        utts = []
        for line in self._read_session_lines(session_jsonl):
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") != "user/message":
                continue
            d = o.get("data") or {}
            s = d.get("source") if isinstance(d.get("source"), dict) else None
            if s is None and isinstance(d.get("message"), dict):
                s = d["message"].get("source")
            if (s or {}).get("kind") != "user":
                continue
            c = d.get("content") or (d.get("message") or {}).get("content")
            txt = "".join(b.get("text", "") for b in c
                          if isinstance(b, dict) and b.get("type") == "text") \
                if isinstance(c, list) else ""
            if txt.strip():
                utts.append({"seq": o.get("seq"), "text": txt.strip()})
        # 另抓 ask_user_question 的真实答复（★修正⑤-b：严格 JSON 判定，防引用文本假阳性）
        answers = []
        for line in self._read_session_lines(session_jsonl):
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") != "tool/result":
                continue
            c = ((o.get("data") or {}).get("message") or {}).get("content")
            if not isinstance(c, list):
                continue
            for b in c:
                t = (b.get("text") or "") if isinstance(b, dict) else ""
                if '"answers"' not in t:
                    continue
                try:
                    j = json.loads(t)
                    if isinstance(j, dict) and isinstance(j.get("answers"), list):
                        answers.append({"seq": o.get("seq"), "raw": t[:400]})
                except Exception:
                    continue          # 引用文本/源码片段 ⇒ 不是真答复载荷

        # ★ 修正⑤-d：重落时**保留既有行的状态/证据**（治"刷新把已兑现重置回未兑现"）——
        #   匹配键 = req 列全文（seq+原话），行序由存档决定 ⇒ 前缀稳定 ⇒ 逐行对得上。
        prev: dict[str, tuple[str, str]] = {}
        try:
            if out.exists():
                for row in self.parse_brief(out):
                    st = row.get("status") or ""
                    if st and "未兑现" not in st:
                        prev[row.get("req", "")] = (st, row.get("evidence") or "—")
        except Exception:  # noqa: BLE001
            prev = {}

        lines = ["# BRIEF · 用户要求锚（逐字，不许转述）", "",
                 f"> 生成时刻：{time.strftime('%Y-%m-%d %H:%M:%S')}",
                 f"> 会话：`{session_jsonl.name}`",
                 f"> 会话锚：`{sid or '（未识别——仅手动对账用，不会被注入端自动命中）'}`"
                 "（★本锚只属于该会话；注入端按 BRIEF_<会话id>.md 取自己的锚）",
                 "> **★ 口径**：只收 `source.kind=='user'` 的真人原话 + `ask_user_question` 的真实答复。",
                 "> **注入内容（goal/runtime-context/team-message）一律不算用户原话。**", ""]
        if goal:
            lines += ["## 〇、Goal（系统记录的目标）", "", f"> {goal}", ""]
        lines += ["## 一、用户原话（真人输入，逐字）", "",
                  "| # | seq | 原话（逐字照抄） | 状态 | 证据 |",
                  "|---|---|---|---|---|"]
        for i, u in enumerate(utts, 1):
            t = u["text"].replace("|", "\\|").replace("\n", " ")
            st, ev = prev.get(f"{u['seq']} | {t}", ("未兑现", "—"))
            lines.append(f"| R{i} | {u['seq']} | {t} | {st} | {ev} |")
        if answers:
            lines += ["", "## 二、问答答复（`ask_user_question` 真实答复，逐字）", "",
                      "| # | seq | 答复（逐字） | 状态 | 证据 |",
                      "|---|---|---|---|---|"]
            for i, a in enumerate(answers, 1):
                t = a["raw"].replace("|", "\\|").replace("\n", " ")
                st, ev = prev.get(f"{a['seq']} | {t}", ("未兑现", "—"))
                lines.append(f"| A{i} | {a['seq']} | {t} | {st} | {ev} |")
        lines += ["", "---", "",
                  "**收尾对账口径**：凡报告里出现「完成/达标/已交付/通过验收」，",
                  "必须先把上表逐条改成「已兑现」并填证据；**有「未兑现」⇒ 不许宣布完成**。",
                  "（闸：`python _guard/gates/g_brief.py --text-file <报告> --brief <本文件> --corpus <会话.jsonl>`）"]
        out.parent.mkdir(parents=True, exist_ok=True)
        # ★ 2026-10-04 修「0 条也落空锚 + EXIT=0」的假绿（实测：zstd 解压失败时
        #   旧代码照样写出 923B 纯表头锚、CLI 报「已落盘」退出 0 —— 失败被记成成功）。
        #   0 条 = 没有可对账的对象 ⇒ **不落盘**，返回 empty 标记由调用方判失败。
        if not utts and not answers:
            rec = {"kind": "init", "utts": 0, "answers": 0, "out": str(out),
                   "session_id": sid, "empty": True,
                   "why": "抽出 0 条 ⇒ 空锚无对账对象 ⇒ 不落盘（防假绿）"}
            self._log(rec)
            return rec
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rec = {"kind": "init", "utts": len(utts), "answers": len(answers),
               "out": str(out), "session_id": sid}
        self._log(rec)
        return rec

    def _log(self, rec: dict) -> None:
        try:
            with self.ledger.open("a", encoding="utf-8") as f:
                f.write(json.dumps({**rec, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                   ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[g_brief] 台账写入失败（不静默吞）：{self.ledger} :: "
                             f"{type(e).__name__}: {e}\n")


# ══════════════════════════════════════════════════════════════════════════
def demo(cfg: ProjectConfig) -> int:
    g = BriefGate(cfg)
    fails = 0
    print("### 要求锚闸 g_brief · 自检（正例须放行、负例须拦下）\n")

    # ---------------- B2：完成 vs 未兑现 ----------------
    print("### ① B2「有未兑现项 ⇒ 不许宣布完成」")
    rows = [{"id": "R1", "req": "我要看到完整的精细的示例项目A剧本的世界", "status": "未兑现", "evidence": "—"},  # purity-allow: 夹具/样本（闸自检的输入数据或真实语料，非代码依赖）
            {"id": "R2", "req": "山要巍峨嶙峋", "status": "未兑现", "evidence": "—"}]
    bad = "本世界已完成交付，山体达标，全部完成。"
    r = g.check_completion(bad, rows)
    print(f"  负例（未兑现却宣布完成）→ {'✓拦下' if not r['ok'] else '✗放行'} ：{r['detail'][:80]}")
    if r["ok"]:
        fails += 1
    # ★ 假阳性回归（本闸第一次上真实语料就踩到）：描述历史 / 否定 都不算"宣布完成"
    for txt, label in [
        ("seq=1755 Lead 宣布「精细达标」→ 12 分钟后被用户骂穿。", "描述历史（引述+被骂）"),
        ("最终判词：❌ 未达标。", "否定（未达标）"),
        ("我方自宣已完成交付，但随后被推翻。", "引述+推翻"),
    ]:
        r = g.check_completion(txt, rows)
        print(f"  正例（{label}）→ {'✓放行' if r['ok'] else '✗误杀'} ：{r['detail'][:60]}")
        if not r["ok"]:
            fails += 1
    rows_ok = [dict(x, status="已兑现", evidence="seq=10689 实测") for x in rows]
    r = g.check_completion(bad, rows_ok)
    print(f"  正例（全部已兑现后宣布）→ {'✓放行' if r['ok'] else '✗误杀'} ：{r['detail'][:80]}")
    if not r["ok"]:
        fails += 1
    r = g.check_completion("本轮只做了地形，尚未完成。", rows)
    print(f"  正例（未宣布完成）→ {'✓放行' if r['ok'] else '✗误杀'} ：{r['detail'][:80]}")
    if not r["ok"]:
        fails += 1
    # ★★ 静默失效回归（端到端实测抓出的真缺陷，必须长期挂在这）：
    #   "无 BRIEF 锚却宣布完成" 以前**静默放行**（n_claim 恒 0，升级分支永不到达）
    #   ⇒ 没落锚反而更安全，奖励"不对账"。这条负例就是钉住这个回归。
    rep_nb = g.check_report("# 交付：某某世界\n\n## 目标四项的证据\n\n全部达标。", brief=None)
    got = rep_nb["ok"]
    print(f"  负例（无 BRIEF 锚却宣布完成）→ {'✓拦下' if not got else '✗静默放行（回归！）'}"
          f" ：n_claim={rep_nb['b2'].get('n_claim')}")
    if got:
        fails += 1
    rep_ok = g.check_report("本轮只做了地形，尚未完成，无交付。", brief=None)
    print(f"  正例（无锚但也未宣布完成）→ {'✓放行' if rep_ok['ok'] else '✗误杀'}")
    if not rep_ok["ok"]:
        fails += 1
    print()

    # ---------------- B3：用户意图引用必须有出处 ----------------
    print("### ② B3「声称用户意图必须带出处」")
    for txt, expect in [
        ("你选了「白模级」，所以我按白模做。", False),
        ("你选了「白模级」（turn=4 / seq=1780），所以我按白模做。", True),
        ("用户已确认重建县城，山上不动。", False),
        ('用户已确认：「重建山脚那一片，山上不动」（seq=3648）。', True),
        ("本轮我按自己的判断推进。", True),
    ]:
        r = g.check_user_citation(txt)
        got = r["ok"]
        mark = "✓" if got == expect else "✗"
        if got != expect:
            fails += 1
        print(f"  {mark} [{'放行' if got else '拦下'}] {txt[:44]}")
    print()

    # ---------------- B1：逐字 vs 转述 ----------------
    print("### ③ B1「要求必须逐字命中原文」")
    corpus = "研究示例项目A剧本和分镜用插件控制blender建造完整的世界建模，我要看到完整的精细的示例项目A剧本的世界"  # purity-allow: 夹具/样本（闸自检的输入数据或真实语料，非代码依赖）
    r = g.check_verbatim([{"id": "R1", "req": "我要看到完整的精细的示例项目A剧本的世界",  # purity-allow: 夹具/样本（闸自检的输入数据或真实语料，非代码依赖）
                           "status": "未兑现", "evidence": "—"}], corpus)
    print(f"  正例（逐字）→ {'✓通过' if r['ok'] else '✗误杀'}")
    if not r["ok"]:
        fails += 1
    r = g.check_verbatim([{"id": "R2", "req": "用户希望世界做得比较完整并且比较精细一些",
                           "status": "未兑现", "evidence": "—"}], corpus)
    print(f"  负例（转述）→ {'✓拦下' if not r['ok'] else '✗放行'}")
    if r["ok"]:
        fails += 1
    print()

    # ---------------- 真实语料回归：示例项目A ----------------
    print("### ④ 真实语料回归（示例项目A会话，若在盘上）")  # purity-allow: 夹具/样本（闸自检的输入数据或真实语料，非代码依赖）
    sj = Path(r"<WORKSPACE>\_wu_audit\cache\session.jsonl")
    if sj.exists():
        out = Path(r"<WORKSPACE>\_guard_ledger\BRIEF_示例项目A_样本.md")  # purity-allow: 夹具/样本（闸自检的输入数据或真实语料，非代码依赖）
        rec = g.init_brief(sj, "（见 REPORT.md §1）", out)
        print(f"  抽出真人原话 {rec['utts']} 条 / 问答答复 {rec['answers']} 条 → {out.name}")
        print(f"  判据：663 条 user/message 里应只认 35 条（用户原话）+ 4 条（问答）")
        ok = (rec["utts"] == 35 and rec["answers"] == 4)
        print(f"  {'✓' if ok else '✗'} 计数核对：utts={rec['utts']}（应 35）、answers={rec['answers']}（应 4）")
        if not ok:
            fails += 1
    else:
        print("  （未找到示例项目A会话副本，跳过）")  # purity-allow: 夹具/样本（闸自检的输入数据或真实语料，非代码依赖）
    print()

    print(f"### 自检结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    from guard import load as load_cfg

    ap = argparse.ArgumentParser(prog="g_brief")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--init", action="store_true", help="开轮：落 BRIEF 锚（默认按会话隔离）")
    ap.add_argument("--session", help="会话 jsonl 或 .zstd 存档（用于抽真人原话）")
    ap.add_argument("--session-id", default="", help="会话 id（缺省从存档父目录名推导）")
    ap.add_argument("--goal", default="", help="goal 原文")
    ap.add_argument("--out", help="BRIEF 输出路径（缺省 = _guard_ledger/BRIEF_<会话id>.md）")
    ap.add_argument("--brief", help="收尾对账用的 BRIEF")
    ap.add_argument("--corpus", help="B1 用的原文语料（会话 jsonl）")
    ap.add_argument("--text-file", help="要检查的报告")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    g = BriefGate(cfg)
    if a.demo:
        sys.exit(demo(cfg))
    if a.init:
        if not a.session:
            print("--init 需要 --session <会话.jsonl 或 .zstd>")
            sys.exit(2)
        sid = g._derive_session_id(Path(a.session), a.session_id)
        out = Path(a.out) if a.out else g.session_brief_path(sid)
        rec = g.init_brief(Path(a.session), a.goal, out, session_id=a.session_id)
        if rec.get("empty"):
            print(f"✗ 未落盘：{rec.get('why')}—— 检查 --session 路径/格式是否正确")
            sys.exit(2)
        print(f"BRIEF 已落盘：{out}")
        print(f"  会话锚：{rec.get('session_id') or sid or '（未识别）'}"
              f"｜真人原话 {rec['utts']} 条 / 问答 {rec['answers']} 条")
        sys.exit(0)
    if a.text_file:
        corpus = Path(a.corpus).read_text(encoding="utf-8", errors="replace") if a.corpus else ""
        brief = Path(a.brief) if a.brief else None
        rep = g.check_report(Path(a.text_file).read_text(encoding="utf-8", errors="replace"),
                             brief, corpus)
        print(rep["summary"])
        if not rep["ok"]:
            b2 = rep["b2"]
            if not b2.get("ok"):
                print("  ✗ " + b2.get("detail", ""))
            b3 = rep["b3"]
            if not b3.get("ok"):
                print(f"  ✗ {b3.get('n_bad')}/{b3.get('n_claim')} 处声称用户意图却无出处")
        sys.exit(0 if rep["ok"] else 1)
    print("用法：--demo / --init --session <jsonl> [--goal ...] [--out BRIEF.md] "
          "/ --text-file <报告> [--brief BRIEF.md] [--corpus <jsonl>]")
