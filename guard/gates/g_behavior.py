# -*- coding: utf-8 -*-
"""_guard · 行为闸 g_behavior —— 把"以前只在文档里写着的 4 条"变成可跑的检查

## 治的哪 4 条（2026-10-03 用户明令「…」）

这 4 条在 `rules.yaml` 里长期挂 `gate: none`（**裸规矩**）——
意思是**写了、但没有任何东西在跑它** ⇒ 跟没写一样。

| 规矩 | 原状态 | 现在怎么核 |
|---|---|---|
| **R-12** 解不出来就报回来，不许硬调参数凑 | 裸 | **B1**：同一目标连续调参 ≥N 次仍无果 ⇒ 拦，要求回来复命 |
| **R-13** 用户已拒绝的方案绝不再搬上来 | 裸 | **B2**：维护"已拒绝清单"，文本命中即拦 |
| **R-27** 看不了图时别用像素统计下语义结论 | **表述已过时** | **B3**：改用"`read_image` 优先"；查"用像素统计下语义结论" |
| **R-28** 用户说"有"就是有，不许二次反驳 | 裸 | **B4**：检测"用户说有 + 报告写无/第N次核对"这类句式 |

## 为什么这 4 条能做闸（原来以为"纯行为约束"）

**原来的借口是"难以机器判定"** —— 但拆开看，**每一条都有可判定的形态**：
  · R-12：不是判"该不该继续"，是判"**试了几次还没报回来**" —— **次数是可数的**
  · R-13：不是判"这个方案好不好"，是判"**它是否出现在已拒绝清单里**" —— **清单是可查的**
  · R-27：不是判"该用哪个工具"，是判"**结论是不是靠像素统计得出的**" —— **句式是可匹配的**
  · R-28：不是判"谁对"，是判"**用户已说『…』之后是否还在反驳**" —— **话序是可查的**

## R-27 的表述修正（2026-10-03）
原文：「看不了图时用官方视觉线路，不要用像素统计瞎判断」
**已过时** —— `read_image` 当天已打通（见 AGENTS.md「read_image 打通全过程」）。
**新表述**：「**能看图就直接 `read_image`**；**不许用像素统计代替看图下语义结论**」。

跨项目：清单/阈值来自 config.behavior；本文件不含项目专有内容。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402

DEFAULTS = {
    # ── B1（R-12）：连续调参次数上限 ──
    "max_param_attempts": 3,
    # ★ 2026-10-04 重写（wiregates）：原实现只认「第 N 次尝试/调整」「调了 N 次参数」这类
    #   **显式编号句式** —— 而 2026-10-04 真实循环语料（corpus/blender_loop.txt）里
    #   **一次都没出现**这种句式（它写的是「反复 launch 了 20 次」「连猜 5 轮」「仍然连不上」）。
    #   ⇒ 原 demo 用例与真实语料**句式不重合** ⇒ demo 全绿、真实那次不响（D3 自证循环）。
    #
    #   新判据分两类，**都认真实形态**：
    #     (a) 显式计数：第 N 次 / 重试 N 次 / launch 了 N 次 / 猜了 N 轮 —— **取 N 的最大值**
    #     (b) 无进展措辞：再试/又试/还是连不上/仍然连不上/我陷入循环/换个端口再 —— **计次数**
    #   两条合并：max(显式计数上限, 无进展措辞命中数)。
    "param_attempt_pats": [
        # ── (a) 显式计数的真实形态 ──
        r"第\s*(\d+)\s*次(?:尝试|调整|试)",
        r"(?:重试|再试|试了|试过|尝试|连|调了|整了|跑了|启动|launch(?:ed)?|猜了|猜)\s*(\d+)\s*(?:次|轮|遍)",
        r"(\d+)\s*(?:次|轮|遍)(?:都)?(?:失败|没用|不行|没解决|没成功)",
        r"(?:launch(?:es)?|launched)\D{0,6}(\d+)",
        r"\"launches\"\s*:\s*(\d+)",
    ],
    # ── (b) 无进展措辞：这些**本身就是一次"试了没用"**，与有没有写编号无关 ──
    "no_progress_pats": [
        r"再试(?:一次|试)?", r"又试", r"又调(?:整)?了", r"又改成", r"再次(?:尝试|调整|重试)",
        r"还是(?:连不上|不行|失败|没|不)", r"仍然(?:连不上|不行|失败|无|没有)",
        r"依旧(?:不行|失败|连不上)", r"依然(?:不行|失败|连不上)",
        r"我(?:陷入|进入)了?(?:死)?循环", r"一直(?:在)?(?:猜|试|反复)",
        r"换个(?:端口|参数|方法|写法)再", r"换了(?:个)?(?:端口|参数|方法)再",
        r"重复(?:试|调)", r"反复(?:launch|启动|试|调|跑)",
    ],
    # 报回来的写法（有这些 ⇒ 已复命，不算硬凑）
    # ★★ 2026-10-04 收紧（wiregates，Lead 实测"两个字就能绕过"后收口）：
    #
    #   判据口径：**"复命" = 明确停下 + 上交给人。**
    #             **"提到根因 / 感到卡住" 都是循环中的常态，不算复命。**
    #
    #   原表里有 `根因` / `卡住` / `我一直在猜` / `这正是我的问题` / `应该先读·查` ——
    #   这些词在**"正在循环中"**的文本里本来就会出现（循环中的人正在分析、
    #   正在说"根因可能是…"）。⇒ 实测：一段真循环**末尾加"根因。"两个字就放行**。
    #   **闸问的是「…」，不是「…」。**
    #
    #   真实语料复核（corpus/blender_loop.txt）：命中的 `报回`/`解不出`
    #   来自**引用 AGENTS.md 规矩的句子**（"这正是 AGENTS.md 那条
    #   「解不出来就报回来，不许硬调参数凑」**要治的行为**"）——
    #   **那是事后复盘在描述规矩，不是"停下来报给用户"** ⇒ 属**假复命**。
    #   ⇒ 因此不能只认关键词，必须**同时**要求"停止/上交"的动作语义。
    "reported_pats": [
        r"报回(?:来|去|给)", r"请示", r"需要你(?:拍板|确认|决定)",
        r"无法解出", r"解不出", r"做不了", r"超出现有能力",
        r"已?(?:停止|停下|停手)", r"上交", r"交给(?:你|用户)(?:决定|判断|拍板)",
        r"我(?:先)?停(?:下来|下)", r"不再(?:试|调|猜)",
    ],
    # ★ 「提到自我诊断」的词：**只有与"停止/上交"同时出现才算复命**
    #   （单独出现 ⇒ 只是循环中的常态，不算复命）
    "self_diag_pats": [
        r"根因", r"卡住", r"受阻", r"我一直在猜", r"这正是我的问题",
        r"应该(?:先|去)(?:读|查)",
    ],
    # ── B2（R-13）：已拒绝清单 ──
    # ★ 2026-10-04：不再是 `[]`（原来是空的 ⇒ 恒 ok=None ⇒ 这道判据全程睡觉）。
    #   下列条目**逐条带记忆库 citation**，见每行注释；无 citation 的一律不填。
    "rejected": [
        # 出处：本机记录（序号已隐去）—— 用户 2026-09-12 明令废止：
        # 「拒绝两步出图！跟我们做出来的完全违和绝对不能在…」
        "两步出图", "分步出图",
        # 出处：本机记录（序号已隐去）（铁律 2）—— 用户只让写"人物A的脸从形态1变成形态3"，
        # 已被明确拒绝过多次的"变冷"类修饰：绝不加 变冷/变凉/皮肤凉/眼神失神/温度/体温/苍白/冷蓝/光线变冷/动作变慢
        "变冷", "变凉", "皮肤凉", "眼神失神", "冷蓝", "光线变冷", "动作变慢",
        # 出处：本机记录（序号已隐去）（铁律 4）—— 用户原话「…」
        # 原文点名"把用户已拒绝的『…』/『…』等内容混进选项 — 错"
        "音色变",
    ],
    # 这些是"再搬上来"的信号
    # ★ 2026-10-04 扩充（wiregates）：原表只认「建议采用/可以考虑/改回/还是用…」，
    #   漏掉了最常见的真实形态 —— **"建议加上/加一点/补上/改成/换成…"**。
    #   实测漏报用例："建议在提示词里加一点偏冷的光线，让气质更冷。"
    #     → 原表 0 命中 ⇒ 放行；而「…」正是那条本机记录里用户骂过两次的拒绝项。
    "resubmit_pats": [r"建议(?:采用|用|选|加|上|补|改成|换成|引入)",
                      r"可以考虑", r"方案\s*[A-Z甲乙丙一二三]", r"改回", r"恢复原来",
                      r"还是用", r"再次?提出", r"当作选项",
                      r"(?:加|加上|加一点|补上|补一点|引入|采用|启用|恢复|套上)[^。\n]{0,20}",
                      r"(?:改|换)(?:成|为)[^。\n]{0,20}"],
    # ── B3（R-27）：像素统计冒充看图 ──
    # ★★ 2026-10-15 形态扩面（用户：「…」）★★
    #   实测逃逸：文本「画面我核过了：**像素均值** 0.0316，所以画面对得上，没问题。」
    #   ⇒ 原清单只认"像素**统计**"这个词 ⇒ 命中为空 ⇒ **整条 B3 不触发、放行**。
    #   而"像素均值 / 整镜 mean / p95"正是我们自己复盘里的写法
    #   （AGENTS.md：「拿『整镜 mean ≥0.15』当『看得出在动』⇒ 它量的是画面有多少纹理」）
    #   ⇒ 判据漏掉的恰好是**本机真实用法**。
    "pixel_pats": [
        r"像素统计", r"肤色占比", r"边缘密度", r"直方图(?:分析|显示)",
        r"亮度分布说明", r"RGB\s*[（(]?\s*\d+\s*,\s*\d+\s*,\s*\d+",
        r"占比\s*[\d.]+%.*(?:说明|判断|证明)",
        r"像素(?:均值|平均值|值|占比|分布)",
        r"(?:整镜|全图|画面|图像)[^\n。]{0,6}(?:均值|统计|分布|mean|p95|标准差)",
        r"(?:均值|mean|p95|标准差|方差|覆盖率)[^\n。]{0,10}(?:看得出|说明|意味着|所以|因此|推断)",
    ],
    # 语义结论的写法
    # ★ 同样过窄（原 4 条只认 特写/中景/手/脸/镜头 等少量词）⇒ 按形态补「图像对象 + 状态断言」
    "semantic_pats": [
        r"(?:说|说明|判断|证明|意味着|表示).*(?:是|像|属于).*(?:特写|中景|全景|近景)",
        r"判断为.*(?:手|脸|人|物|场景)", r"据此(?:判断|认定)",
        r"这说明.*(?:镜头|景别|构图|姿态)",
        r"(?:画面|图像|截图|渲染图|视口|镜头|主体|人物|内容)[^\n。]{0,12}"
        r"(?:对得上|对得起来|没问题|正常|一致|正确|通过|看得出|入画|完整|清晰|无误)",
    ],
    # 合法用法（量色值/判色偏 / **真的看了具体那张图**）—— 这些不算"用统计量替代看图"
    # ★ 2026-10-15：我自己扩面时把这条漏了，`must_pass` 当场被判红（**假报错**）——
    #   判据的实质是"统计量**替代**看图"，所以"给了具体图/帧号 + 统计量作辅助"必须放过。
    #   判据只认**可核的图像凭据**（文件名/帧号/read_image），不认"我看了"这种自述（可被否定句/装样子绕过）。
    "pixel_ok_pats": [
        r"色偏", r"色值", r"判色", r"偏色", r"色差",
        r"read_image", r"\.(?:png|jpg|jpeg|webp)\b", r"\bf\d{3,}\b", r"帧\s*\d+",
    ],
    # ── B4（R-28）：用户说有之后还反驳 ──
    "user_says_yes_pats": [r"用户(?:说|确认|回复)(?:了)?[：: ]*[「\"]?(?:有|是|对|存在)",
                           r"我看到(?:了)?有", r"用户指出有"],
    "refute_pats": [r"但我(?:再|又)?(?:次)?核(?:对|查)", r"第\s*[二2]次核对",
                    r"实测(?:确认)?(?:并)?(?:没有|无)", r"仍然(?:为|是)?(?:否|无|没有)",
                    r"与我(?:的)?核对(?:结果)?(?:冲突|不符)"],
}


def _cfg_of(cfg: ProjectConfig) -> dict:
    d = {}
    for k, v in DEFAULTS.items():
        d[k] = list(v) if isinstance(v, list) else v
    over = cfg.get("behavior") or {}
    for k, v in over.items():
        if isinstance(v, list) and isinstance(d.get(k), list):
            d[k] = list(d[k]) + [x for x in v if x not in d[k]]
        else:
            d[k] = v
    return d


class BehaviorGate:
    NAME = "g_behavior"

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self.K = _cfg_of(cfg)
        lg = (cfg.get("behavior") or {}).get("ledger", "_guard_ledger/behavior.jsonl")
        self.ledger = cfg.resolve(lg)
        self.ledger.parent.mkdir(parents=True, exist_ok=True)

    # ---------- B1（R-12）：连续调参无果 ----------
    def b1_param_grind(self, text: str) -> list[dict]:
        """★ 2026-10-04 重写：原实现是 `mx = max(mx, 2)`（死代码，阈值 3 ⇒ 永远不触发），
        且只认显式编号句式 ⇒ 真实循环语料（「反复 launch 了 20 次」「仍然连不上」）一次不响。

        新实现是**真实计数器**：把"试了几次"从两类真实形态里数出来，取最大。
        """
        k = self.K
        # ── (a) 显式计数：取所有捕获到的数字的最大值 ──
        explicit = 0
        for pat in k["param_attempt_pats"]:
            for m in re.finditer(pat, text):
                for g in m.groups():
                    if g and str(g).isdigit():
                        explicit = max(explicit, int(g))
            # 无捕获组的 pattern：按"命中次数"计
            if "(" not in pat:
                explicit = max(explicit, len(re.findall(pat, text)))
        # ── (b) 无进展措辞：按"同一次尝试"聚簇后计数 ──
        # ★ 2026-10-04 修正（防误杀，两轮）：
        #   反例（我自己的正例测试抓到）："再试了一次，还是不行；又调了一次，通过了。"
        #   这是**2 次尝试**，但旧写法朴素相加 = 3 ≥ 阈值 ⇒ **误杀正常重试**。
        #   · 只做"区间重叠合并"不够：`再试`[0,2) / `还是不行`[6,10) / `又调了`[11,14)
        #     **互不重叠** ⇒ 仍数成 3。
        #   · 正解：**按句读聚簇** —— 同一分句（由 `；。！？，` 等切分）里的多个无进展
        #     措辞，说的是**同一次**尝试；跨分句才算新的一次。
        #   判据：两个命中之间若**不含分句边界**，归为同一次。
        spans = []
        for pat in k.get("no_progress_pats", []):
            for m in re.finditer(pat, text):
                spans.append((m.start(), m.end()))
        spans.sort()
        BOUNDARY = re.compile(r"[；;。！!？?\n]")
        vague = 0
        cur_end = -1
        for s, e in spans:
            if cur_end < 0 or s >= cur_end and BOUNDARY.search(text[cur_end:s]):
                vague += 1
            cur_end = max(cur_end, e)
        mx = max(explicit, vague)
        if mx < int(k["max_param_attempts"]):
            return []
        # ★ 收口（2026-10-04）：复命 = **明确停下 + 上交给人**。
        #   · `reported_pats` 只含"停止/上交"语义 ⇒ 单独命中即可算复命；
        #   · `self_diag_pats`（根因/卡住/我一直在猜…）**单独出现不算** ——
        #     那是循环中的常态（循环中的人正在分析）。只有当它与"停止/上交"
        #     **同时出现**时才提升为复命。
        #
        # ★★ 第三层收口（真实语料暴露的**假复命**）：
        #   `blender_loop.txt` 里命中的 `报回`/`解不出` 出自**引用规矩的句子**：
        #     「这正是 AGENTS.md 那条「解不出来就报回来，不许硬调参数凑」**要治的行为**」
        #   —— 那是**事后复盘在描述规矩**，不是"我停下来报给用户了"。
        #   判据：把**引号/书名号内的内容**剔除后再匹配（引用 ≠ 自述）。
        #   ⇒ 先剥掉 `「…」` `『…』` `"…"` `《…》` `` `…` `` 里的内容，再找复命动作。
        stripped = re.sub(r"[「『“\"《][^」』”\"》]{0,60}[」』”\"》]", "", text)
        stripped = re.sub(r"`[^`]{0,80}`", "", stripped)
        stopped = [p for p in k["reported_pats"] if re.search(p, stripped)]
        diag = [p for p in k.get("self_diag_pats", []) if re.search(p, stripped)]
        reported = bool(stopped)
        return [{
            "id": "B1", "rule": "R-12", "ok": reported,
            "detail": (f"检测到**连续尝试/调参 {mx} 次**"
                       f"（阈值 {k['max_param_attempts']}；"
                       f"显式计数 {explicit}、无进展措辞 {vague}）"
                       + (f"，且已复命 ✓（停止/上交动作：{stopped[:2]}）" if reported else
                          "，但**没有复命** ⇒ 这是在硬试/硬调参数凑；"
                          "应当停下，把『人话/场景/资产哪里不对』报给用户，不许硬凑"
                          + ("（注：文中虽提到" + "、".join(d[:8] for d in diag[:2])
                             + "，但那只是**循环中的自我分析**，不是"
                             "「停下并上交给人」⇒ 不算复命）" if diag else "")
                          + "（实证：launch 20 次、端口一次没开，全程『一直在猜修法』）")),
        }]

    # ---------- B2（R-13）：搬回已拒绝的方案 ----------
    def b2_rejected(self, text: str) -> list[dict]:
        rej = [r for r in self.K["rejected"] if r]
        if not rej:
            # ★ 通用判据（Lead 2026-10-04 定，wiregates 采纳）：
            #   **"无法核" 必须报 None，不许报 True；"核过了" 和 "没得核" 是两种结论。**
            #   这里保持 None（诚实报"无法核"），但**默认清单已不再为空**（见 DEFAULTS）。
            return [{"id": "B2", "rule": "R-13", "ok": None,
                     "detail": "**无法核**：已拒绝清单为空（config.behavior.rejected）"
                               " ⇒ 不是『未命中』，是『没得比』"}]
        hit = []
        for r in rej:
            for m in re.finditer(re.escape(r), text):
                ctx = text[max(0, m.start() - 120): m.end() + 120]
                # ★ 排除"否定语境"（2026-10-03 实测误判）：
                #   「不用两步出图」「拒绝分步出图」「不再用…」是在**遵守**规矩，
                #   不能因为它提到了那个词就判违规。
                if re.search(r"不(?:用|要|再|采|选|做)|拒绝|禁止|避免|别|勿|放弃|排除",
                             ctx[max(0, m.start() - 60 - max(0, m.start() - 120)):m.end() + 60]):
                    continue
                if any(re.search(p, ctx) for p in self.K["resubmit_pats"]):
                    hit.append(r)
                    break
        return [{
            "id": "B2", "rule": "R-13", "ok": not hit,
            "detail": ("未搬回已拒绝项 ✓" if not hit else
                       f"**搬回了用户已拒绝的**：{'、'.join(hit[:3])} —— "
                       "表里镜8 实证：『我拒绝的东西再搬上来』是最严重的违规之一"),
        }]

    # ---------- B3（R-27）：像素统计冒充看图 ----------
    def b3_pixel_as_vision(self, text: str) -> list[dict]:
        px = [m.group(0) for m in re.finditer("|".join(self.K["pixel_pats"]), text)]
        if not px:
            return []
        # 合法用法（判色偏/色值）不算违规
        if re.search("|".join(self.K["pixel_ok_pats"]), text):
            legit = True
        else:
            legit = False
        sem = bool(re.search("|".join(self.K["semantic_pats"]), text))
        bad = sem and not legit
        return [{
            "id": "B3", "rule": "R-27", "ok": not bad,
            "detail": ("用像素统计下**语义结论** ⇒ 违规；"
                       "**能看图就直接 `read_image`**，"
                       "像素统计只能判『色偏没偏』，判不了『这是手还是中景』"
                       if bad else
                       ("像素统计用于色值/色偏判断 ✓（合法用法）" if legit
                        else f"提到像素统计（{len(px)} 处）但未用于下语义结论 ✓")),
        }]

    # ---------- B4（R-28）：用户说有之后还反驳 ----------
    def b4_refute_user(self, text: str) -> list[dict]:
        if not re.search("|".join(self.K["user_says_yes_pats"]), text):
            return []
        hit = [m.group(0) for m in re.finditer("|".join(self.K["refute_pats"]), text)]
        return [{
            "id": "B4", "rule": "R-28", "ok": not hit,
            "detail": ("用户说『有』之后仍在反驳 ⇒ 违规。"
                       "**用户是最终裁判**：说『有』就是有，不许拿第二次核对去推翻"
                       if hit else
                       "用户说『有』之后未再反驳 ✓"),
        }]

    # ---------- 总检 ----------
    def check_text(self, text: str) -> dict:
        rows = (self.b1_param_grind(text) + self.b2_rejected(text)
                + self.b3_pixel_as_vision(text) + self.b4_refute_user(text))
        if not rows:
            rep = {"gate": self.NAME, "ok": True, "rows": [],
                   "summary": "行为闸：未触发任何检查项，跳过"}
            self._log(rep)
            return rep
        # 只把 ok=False 当不过；ok=None（无法核）只提示
        bad = [r for r in rows if r["ok"] is False]
        unk = [r for r in rows if r["ok"] is None]
        rep = {
            "gate": self.NAME, "ok": not bad, "rows": rows,
            "summary": (f"行为闸：核了 {len(rows)} 项"
                        + ("" if not bad else "；**不过**：" +
                           "；".join(r["detail"] for r in bad))
                        + (f"；{len(unk)} 项无法核（未配置）" if unk else "")),
        }
        self._log({**rep, "rows": None})
        return rep

    def check_file(self, p: str | Path) -> dict:
        p = Path(p)
        rep = self.check_text(p.read_text(encoding="utf-8", errors="replace"))
        rep["file"] = str(p)
        return rep

    def require(self, text: str) -> dict:
        rep = self.check_text(text)
        if not rep["ok"]:
            raise BehaviorFailed(rep["summary"])
        return rep

    def _log(self, rec: dict) -> None:
        # ★ D6 修复（2026-10-04）：原 `except Exception: pass` ⇒ 台账写不进去**无声无息**。
        #   闸坏了没人知道 = 假绿。现在写失败必须落到 stderr。
        try:
            with self.ledger.open("a", encoding="utf-8") as f:
                f.write(json.dumps({**rec, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                   ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(
                f"[g_behavior] 台账写入失败（不静默吞）：{self.ledger} :: "
                f"{type(e).__name__}: {e}\n")


class BehaviorFailed(RuntimeError):
    """行为闸未通过 —— 这是"以前只在文档里写着"的那 4 条，现在会拦了。"""


# --------------------------------------------------------------------------
def demo(cfg: ProjectConfig) -> int:
    g = BehaviorGate(cfg)
    # ★ 2026-10-04（wiregates）：删掉原来的"临时塞一个假清单"
    #   `g.K["rejected"] = ["两步出图", "降低分辨率", "拆成两段硬切"]` —— 三条里：
    #     · 「…」有 citation（本机记录，用户明令废止）→ 真拒绝项 ✓
    #     · 「…」**核不到**任何"用户已拒绝"的出处 → 不填
    #     · 「拆成两段硬切」不但核不到拒绝依据，AGENTS.md 复盘表还明载
    #       「…」是用户**认可**的做法
    #       ⇒ **填进真清单会误杀**
    #   ⇒ 结论：**demo 里的默认值不是真实口径**，不能当清单用。
    #     现在 demo 直接用 DEFAULTS 里那份**逐条带 citation** 的真清单。
    fails = 0
    print("### 行为闸 · 实例自检（4 条曾经的裸规矩）\n")
    print(f"  （已拒绝清单 {len(g.K['rejected'])} 条，逐条带记忆库 citation；"
          f"阈值 max_param_attempts={g.K['max_param_attempts']}）\n")

    cases = [
        # ── B1：负例用**真实语料形态**（corpus/blender_loop.txt），不再照正则出题 ──
        ("B1", "R-12 真实循环形态：反复 launch 20 次 / 仍然连不上（改前放行）",
         "反复 launch 了 20 次都失败，一直在猜修法。猜了 5 轮、launch 了 20 次都没解决。"
         "仍然连不上。再试一次。换个端口再试。还是连不上。我陷入循环了。", True),
        ("B1", "R-12 显式编号句式（原句式，仍要能拦）",
         "第 1 次调整 root_z=0.9，不行。第 2 次调整 0.85，还不行。第 3 次调整 0.88，仍不达标。"
         "第 4 次再调 0.86……", True),
        ("B1", "R-12 调参后报回来了（应放行）",
         "试了 4 次参数都不对，无法解出 —— 报回来：这不是参数问题，是场景设定有问题。", False),
        ("B1", "R-12 只试 2 次（未到阈值，应放行）",
         "再试了一次，还是不行；又调了一次，通过了。", False),
        # ── B2：负例用**有 citation 的真拒绝项** ──
        ("B2", "R-13 搬回已拒绝的『两步出图』（citation: 本机记录）",
         "建议采用两步出图，它更稳。", True),
        ("B2", "R-13 搬回已拒绝的『变冷』约束（citation: 本机记录 铁律2）",
         "建议在提示词里加一点变冷的光线，让气质更冷。", True),
        ("B2", "R-13 否定语境（应放行）",
         "建议采用一步出图 + 身份锚，不用两步出图。", False),
        # ── B3 / B4 原样保留（本次未改判据）──
        ("B3", "R-27 用像素统计下语义结论",
         "肤色占比达 38%，边缘密度高，据此判断这是手部特写。", True),
        ("B3", "R-27 像素统计判色偏（合法，应放行）",
         "测了 RGB 134,99,89，与设定色值有偏差，属色偏。", False),
        ("B4", "R-28 用户说有之后仍反驳",
         "用户说有了。但我再次核对，实测并没有，与我的核对结果冲突。", True),
        ("B4", "R-28 未反驳（应放行）",
         "用户说有了。以用户判断为准，我不再复核。", False),
    ]
    for tag, name, text, expect in cases:
        r = g.check_text(text)
        rows = [x for x in r["rows"] if x["id"] == tag]
        if not rows:
            print(f"  —— 未触发   {name}")
            continue
        row = rows[0]
        hit = row["ok"] is False
        exp = bool(expect) if expect is not None else None
        mark = "✓✓拦下" if hit else "——放行"
        flag = ""
        if exp is not None and hit != exp:
            flag = "  ✗ 期望不符"
            fails += 1
        print(f"  {mark}  {name}{flag}")
        if hit:
            print(f"           {row['detail'][:100]}")
    print()

    print("### 正例：正常的报告不该被误伤")
    ok_doc = """# 报告
本次把 root_z 调到 0.86，实测 0.865 达标（实测于 2026-10-03）。
用 read_image 看了 f48 帧，画面里人形完整。
用户确认过目标（用户说：可以）。
"""
    r = g.check_text(ok_doc)
    print(f"  {'✓ 放行' if r['ok'] else '✗ 误杀'}：{r['summary'][:80]}")
    if not r["ok"]:
        fails += 1
    print()
    print(f"### 自检结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        # ★ D6：reconfigure 失败在旧 Windows 控制台属常见，但**不许静默**——留一行痕。
        sys.stderr.write(f"[g_behavior] stdout 重设为 utf-8 失败（继续跑）：{e}\n")
    from guard import load as load_cfg

    ap = argparse.ArgumentParser(prog="g_behavior")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--text-file")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    g = BehaviorGate(cfg)
    if a.demo:
        sys.exit(demo(cfg))
    if a.text_file:
        rep = g.check_file(a.text_file)
        if a.json:
            print(json.dumps(rep, ensure_ascii=False, indent=1))
        else:
            print(rep["summary"])
            for row in rep["rows"]:
                m = "✓" if row["ok"] is True else ("✗" if row["ok"] is False else "?")
                print(f"  {m} {row['id']} [{row['rule']}] {row['detail']}")
        sys.exit(0 if rep["ok"] else 1)
    print("用法：--demo / --text-file <文本>")
