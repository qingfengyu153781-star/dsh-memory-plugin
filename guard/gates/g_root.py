# -*- coding: utf-8 -*-
"""_guard · 根治病根闸 v1 —— 把《问题总表》7 个病根做成机器拦得住的检查

## 为什么有这个文件

《问题总表》（4,888 条问题）归纳出**7 个病根**。它们的共同点是：
**把「我做的动作」当成了「我要的结果」** —— 过程全绿，目标可以完全不达标。

表里自己反复证明：**「写了断言但不 raise ＝ 没有断言」**（行 49299）、
**「断言只记录不拦停」**（行 30110）。
所以根治的唯一有效形式 = **可执行、会拦停的闸门**，不是再写一份文档。

## 七道闸（与病根一一对应）

| 闸 | 病的根 | 拦什么 |
|---|---|---|
| `B1_target` | 判据量"过程量"不量"目标量" | 判据声明里没有"要保证的量"，或该量不锚在最终产物 |
| `B2_cite`   | 引用会过期 | 出现 md5/行号/计数而**无取数时刻** |
| `B3_reopen` | 没落盘/被求值覆盖 | 报"已改"却无 **reopen 后实测值** |
| `B4_ruler`  | 口径不统一 | 数字旁**缺量具四问**（帧/归零遍/算法/文件集） |
| `B5_scope`  | 范围不全却说"没有" | 出现"不存在/没有"而**无扫描范围声明** |
| `B6_sweep`  | 改一处不扫同类 | 有改动却**无同类扫描的命中数** |
| `B7_verify` | 未核实就下结论 | 报告里"看到"与"实测"混为一谈 |

## 用法

```python
from gates.g_root import RootGate
g = RootGate(cfg)
report = g.check_text(open("报告.md").read(), kind="report")
if not report["ok"]:
    raise RuntimeError(report["summary"])   # ← 必须 raise，否则等于没闸
```

命令行：
```bash
python gates/g_root.py --text-file 报告.md          # 单文件
python gates/g_root.py --demo                        # 负例自检（证明拦得住）
python gates/g_root.py --audit "D:/path/*.md"        # 批量
```

跨项目：阈值与关键词来自 `config.root_gate`，本文件不含项目专有内容。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402


# --------------------------------------------------------------------------
# 默认阈值（可被 config.root_gate 覆盖）
# --------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    # B2：引用必须带取数时刻的"可变物"
    #
    # ★★ 2026-10-04 Lead 修（同词不同义 · 判据量错对象 · 本族第五例）：
    #   原词表里的裸 `指纹` 会把 **"指纹文件"** 当成 **"md5 指纹"**。
    #   实测（`AGENTS.md:583`）：
    #       「从当前目录往上找**指纹**文件（≥2 命中即认定），不用传项目根」
    #   —— 这里的"指纹"是**项目识别用的标记文件**（`projects/*.yaml` 的 `fingerprint`），
    #   **与 md5 无关、也不会过期** ⇒ 被判"引用无取数时刻" ⇒ 误杀。
    #   修法：`指纹` 必须与**凭据语境**同现才计入（`指纹` + `md5`/`哈希`/`校验`/`= 值`）；
    #         单独出现的"指纹文件"不算。
    #   同批收紧：`\bL\d{2,4}\b` 原会匹配**任何** L+2~4 位数字（如 `L100`），
    #         而它想抓的是"第 217 行"这种**行号引用** ⇒ 改为必须带 `行/L=` 语境。
    "volatile_pats": [
        r"\bmd5\b", r"\bMD5\b", r"哈希(?:\s*值)?",
        r"指纹\s*[:：=]\s*\S+",              # ★ 收紧：指纹=值 才算凭据
        r"第\s*\d+\s*行", r"行号\s*[:：=]?\s*\d+",
        r"(?:行|L)\s*[:：=]\s*\d{2,4}",       # ★ 收紧：带语境的行号引用
    ],
    # ★ 仅当"计数"被当作**凭据**引用时才需取数时刻；
    #   "命中 3 处""共 3 帧"是当次量具结果，不算过期引用（否则误杀所有量测报告）。
    # ★★ 2026-10-04 修（不可能命中的正则）：
    #   原文 4 条正则里 **3 条在 40 份真实语料/377,454 字符上零命中**（`总计\s*\d+`、
    #   `合计\s*\d+`、`(?:问题|条目|记录|文件)数\s*(?:为|=|＝)\s*\d+`），
    #   第 4 条也只在 ≥5 个实体时命中 ⇒ 等于**这条检查在实际报告上永不触发**。
    #   真实报告里的计数写法实测为：「共 40 条规则」「命中 3 处」「27 个对象」
    #   「共 4,888 条问题」。修法：换成实测命中的形态（并去掉"必须 ≥ 5 个实体"的死条件）。
    "count_cite_pats": [
        r"共\s*\d+\s*(?:个|条|处|份|张|项|次|种)",
        r"(?:问题|条目|记录|文件|规则|闸门|缺陷)数(?:量)?\s*(?:为|=|＝|:|：)\s*\d+",
        r"总计\s*\d+|共计\s*\d+|合计\s*\d+|\d+\s*条(?:问题|记录|规则)",
    ],
    # 取数时刻的合格写法
    # ★★ 2026-10-04 修（不可能命中的正则）：原 `@\s*\d` / `版本\s*[vV]?\d` /
    #   `截至第\s*\d+\s*轮` 三条在真实语料上零命中；而"实测于/取数于/快照于 + 时间"
    #   这类真实写法原先只覆盖了 `实测于` 一种 ⇒ 其余全被判"无取数时刻"（误杀）。
    "stamp_pats": [
        r"\d{4}-\d{2}-\d{2}", r"\d{2}:\d{2}", r"截至", r"取数", r"实测于",
        r"取数于", r"快照于", r"采集于", r"@\s*\d", r"版本\s*[vV]?\d", r"rev=",
        r"第\s*\d+\s*轮", r"时点", r"时间戳",
    ],
    # B3：声明"改过/完成"的动词
    # ★★ 2026-10-15 形态判据（修"触发词过窄"的假根治；用户要求检查同类问题）★★
    #   病根：原清单是**字面动词**（已改/已修改/已更新/已完成/已修复/已生效/改好了）。
    #   实测（对抗样本 /tmp/claim_fix.md）：报告写「这个问题**已根治**」
    #   ⇒ B3 报「未出现『已改/完成』类声明，跳过」⇒ **整条根因判据根本不跑**。
    #   修法：按「**已/都/均/经 + 改动类动词**」的**形态**触发（与 g_xcheck X5 同款）。
    "claim_pats": [
        r"(?:已|都|均|经)[^\n。]{0,4}?"
        r"(?:改|修|更新|完成|做完|做掉|处理|解决|落地|根治|生效|上线|部署|清|删|搞定|跑通)",
        r"(?:改|修|清|删)(?:好|掉|完)了",
    ],
    # B3：回读证据的合格写法
    # ★ 2026-10-15 同样按**形态**放宽：这条判据的**意图**是"改动声明必须附**独立复验**的实测值"
    #   （治「内存态达标一律算通过」）。原清单只认 `reopen/重开/复读/回读/独立进程`，
    #   而"另起一个进程重跑脚本/在安装副本上实测/换一条计算通路复算"同样满足意图却全部落空
    #   ⇒ 会把**真做了复验**的报告误判成"无回读证据"。这里补齐这些形态。
    "reopen_pats": [
        r"reopen", r"重开", r"复读", r"回读", r"独立进程", r"open_mainfile", r"重新打开",
        r"另起(?:一个)?(?:进程|脚本|通路)", r"重跑", r"再跑(?:一遍|一次)", r"重新(?:跑|执行|生成)",
        r"实测", r"复算", r"复验", r"换一?条(?:通路|路径|算法)", r"在(?:真实|安装)副本上",
    ],
    # B4：量具四问
    "ruler_keys": ["用什么帧", "归零", "算法", "文件集", "阈值"],
    # B5：断言"没有"的动词
    #
    # ★★ 2026-10-04 Lead 修（判据量错对象 · 本族第六例）：
    #   原词表把**裸 `不存在`** 当成"说了没有"。实测 17 份真实文档，它的命中**没有一处**
    #   是 B5 想治的那种结论（"我说没有某样东西，却没说我扫了哪里"）：
    #     · 「**不存在**『最多 2 个』」        —— 在讲**规矩冲突**
    #     · 「更早的本地已**不存在**（不是没抽）」 —— **恰恰作了澄清**
    #     · 「`lib/orchestrator/` **不存在**」  —— 在**定位 bug**
    #     · 「调用了**不存在**的 markdown 工具」  —— 在**报现象**
    #   ⇒ B5 要抓的是「**结论性的"没有"**」（"全库没有"/"文件里查无"），
    #     不是叙述里出现"不存在"三个字。
    #   修法：要求**结论性语境** —— 出现"全/所有/任何/整个/通篇/一处也没有"这类**全称量词**，
    #         或"查无/零命中/未找到/找不到"这类**检索结论动词**；
    #         裸"不存在"**只有在带全称范围时**才算。
    # B5：**候选句召回**词表（宽召回，宁多勿漏 —— 判定交给 b5_scope 的句级判据）
    #
    # ★★★ 2026-10-04 Lead 定稿（第三版，前两版都在**调词表**上打转 ⇒ 必须换判据形状）
    #
    #   前两版为什么失败：
    #     · v1 裸 `不存在` ⇒ 误杀「讲规矩冲突」「定位 bug」「作澄清」；
    #     · v2 加全称量词/检索动词 ⇒ 又误杀「找不到就新建」「崩溃栈里没有任何文件操作」，
    #       而收紧"任何"之后「所有会话里都没有」又漏了。
    #   ⇒ 根因：**"叙述"与"断言"不是词的区别，是句子的区别** —— 词表永远调不准。
    #
    #   ⇒ 定稿：本表**只管召回**（宁可多召回，交给下一步判），
    #     真正的判定在 `b5_scope()`：**看这一句是不是"下了『没有』的结论"**。
    "absence_pats": [
        r"不存在", r"没有", r"查无", r"零命中", r"未找到", r"找不到",
        r"不含", r"无此", r"无该", r"没有该", r"空",
    ],
    # B5：**叙述句**特征 —— 出现这些 ⇒ 这句在"讲过程/报现象"，不是在"下结论" ⇒ 不算 B5
    #   实测来自真实误杀样本：
    #     · 「找不到就新建语义化文件夹」   → 动作叙述（"就<动词>"）
    #     · 「崩溃栈里没有任何文件操作」   → 报现象（"<范围>里 没有 <现象>"）
    #     · 「更早的本地已不存在（不是没抽）」→ 澄清（括号补充）
    #     · 「调用了不存在的 markdown 工具」→ 报现象
    "absence_narrative_pats": [
        r"找不到就", r"找不到[，,]",            # 动作叙述
        r"[里中内]没有\S{0,10}(?:操作|动作|痕迹|记录|变化|异常)",  # 报现象
        r"（[^）]*）|\([^)]*\)",                 # 含括号补充（澄清）
        r"不存在[　 ]*『",                   # 引用某说法（讲冲突）
        # ★ 具体指认（"X 不存在"，X 是路径/文件名/标识符）⇒ 报现象，不是全局结论
        r"[A-Za-z_][\w./\-]*[　 ]*(?:文件夹|目录|文件|路径)?[　 ]*不存在",
        r"[一-鿿/]{2,}[　 ]*不存在[　 ]*（",       # "…不存在（原因）"
    ],
    # B5：**结论句**特征 —— 出现这些 ⇒ 这是在"下『没有』的结论" ⇒ 必须报范围
    #   这些是 B5 真正要治的形态（病根 5：读不到 ≠ 没有）
    "absence_conclusion_pats": [
        r"(?:全|所有|整个|通篇|统统|全部)[^，。；\n]{0,6}(?:都)?\s*(?:没有|无|不存在)",
        r"一处也(?:没有|无)", r"一个也(?:没有|无)",
        r"零命中", r"查无", r"未找到",
        r"没(?:有)?(?:找到|发现|查到|搜到|检索到)",
        r"不存在[。；！？\n]?$",
        r"(?:文件|目录|库|清单|表|索引|记录|条目)(?:里|中)?\s*(?:并)?(?:不存在|没有|无)",
        # ★ 2026-10-04 Lead 收紧：泛用否定（"没有任何地方报红"）与**范围**无关，
        #   不算 B5。B5 治的是"说某范围内没有 X"⇒ 要求**可查证宾语**。
        r"没有(?:该|这个|此)\s*(?:文件|目录|项|函数|字段|变量|方法|工具|记录|条目|模块|配置)",
        r"(?:全|所有|整个|通篇|全部)[^，。；\n]{0,8}(?:都)?\s*(?:没有|无|不含)",
    ],
    # B5：范围声明
    "scope_pats": [r"扫描范围", r"搜了", r"查了", r"检索范围", r"已扫", r"覆盖范围", r"grep 了"],
    # B6：改动声明
    "change_pats": [r"已改", r"已替换", r"已删除", r"已重命名", r"已统一", r"已修正"],
    # B6：同类扫描
    "sweep_pats": [r"同类", r"全量\s*grep", r"已扫全", r"命中\s*\d+\s*处", r"其余\d*处", r"无其它"],
    # B7：把"看到/推断"当"实测"的词
    "guess_pats": [r"应该是", r"大概", r"估计", r"看起来", r"似乎", r"推测", r"凭印象", r"想当然", r"我认为"],
    # B7：实测词
    "measure_pats": [r"实测", r"回读", r"打印", r"量得", r"读数为", r"测得", r"复算"],
    # B1：判据必须声明"要保证的量"
    "criterion_pats": [r"判据", r"闸门", r"断言", r"验收标准"],
    "goal_pats": [r"要保证", r"目标是", r"该保证", r"确保", r"目的是", r"指的是"],
    # 阈值
    "min_ctx_chars": 120,    # 判定时的上下文窗口
    "span_lines": 6,         # 一条声明向后看多少行算"同段"
}


def _cfg_of(cfg: ProjectConfig) -> dict[str, Any]:
    d = dict(DEFAULTS)
    over = cfg.get("root_gate") or {}
    for k, v in over.items():
        if isinstance(v, list) and isinstance(d.get(k), list):
            d[k] = list(d[k]) + [x for x in v if x not in d[k]]
        elif isinstance(v, dict) and isinstance(d.get(k), dict):
            d[k].update(v)
        else:
            d[k] = v
    return d


class RootGate:
    """七病根闸。`check_text()` 返回结构化报告；`ok=False` 必须被 raise。"""

    NAME = "g_root"

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self.K = _cfg_of(cfg)
        lg = (cfg.get("root_gate") or {}).get("ledger", "_guard_ledger/root_gate.jsonl")
        self.ledger = cfg.resolve(lg)
        self.ledger.parent.mkdir(parents=True, exist_ok=True)

    # ---------------- 工具 ----------------
    @staticmethod
    def _lines(text: str) -> list[str]:
        return text.split("\n")

    def _hits(self, text: str, pats: list[str]) -> list[tuple[int, str]]:
        """返回 [(行号, 命中的行原文)]，行号从 1 开始。

        ★ 关键：**排除"元行"** —— 引用块（> ）、表格单元格里的引文、代码块、
          以及行内含「坑/病根/教训/错在/反面/被斥」等**讨论标志**的行。
        理由：报告里大量出现「系统里到处是"已完成"的证据」这类**批判性引用**，
        若把它当作"宣称完成"，闸门就会误杀所有"讨论病根"的文档（实测踩过）。
        """
        out = []
        rx = re.compile("|".join(pats)) if pats else None
        if rx is None:
            return out
        in_code = False
        for i, l in enumerate(self._lines(text), 1):
            s = l.strip()
            if s.startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                continue
            # 引用块 / 表格行：通常是引用别人的话或列表，不作声明
            if s.startswith(">"):
                continue
            if s.startswith("|"):
                continue
            # 含"讨论标志"的行 = 在讨论这个坑，不是在犯这个坑
            if re.search(r"坑|病根|教训|错在|反面|被斥|禁止|不许|误判|误杀|应该避免|防再犯", l):
                continue
            # ★★ 2026-10-04 新增「元话语」过滤（治本族第四例 · 判据量错对象）：
            #   实测（task-6 正例回归）：`AGENTS.md` / `_guard/README.md` / 复盘 被 B2/B5 误拦，
            #   根因是 `_hits` 把**"在讲某概念/在描述某工具输出"**当成了**"在引用某可变物"**：
            #     · AGENTS.md:515-516 「`_guard\projects\<名>.yaml`（指纹/标准/断言…）」
            #       「找**指纹文件**自动识别」 ← 在**定义什么是指纹文件**，没有引用任何具体指纹
            #     · README.md:178 「把『md5』写成『校验值』…**换说法就绕过**」 ← 在**描述这个坑**
            #     · 复盘:71 「哈希自洽」 ← 在**转述工具输出**，不是在宣称自己的量测
            #   ⇒ 这三类都是**元话语**（谈论指标/构成清单/假设推演），不是**声明**。
            #   判据（任一命中即跳过本行）：
            #     ① 构成/包含式列举：`（指纹/标准/断言/…）`、`数字/哈希/路径/…，任一即可`
            #     ② 概念定义：`叫什么`/`指的是`/`所谓`/`这类`/`形如`/`即以`/`任缺`/`任一`
            #     ③ 假设/疑问/条件：`如果`/`若`/`假如`/`是否`/`能否`/`？`/`吗`
            if re.search(r"[（(][^）)]*(?:指纹|哈希|md5|行号|计数)[^）)]*[）)]"
                         r"|(?:指纹|哈希|md5|行号|计数)\s*[、/／和与]\s*\S"
                         r"|(?:数字|哈希|路径|文件名|命令|帧号|版本号|指纹)[^\n]{0,20}[、/／][^\n]{0,12}(?:任一|任缺|均可|之一)"
                         r"|叫做|指的是|所谓|这类|这种写法|形如|即以|任一即可|任缺"
                         r"|换说法|换个说法|写成|改写成"
                         r"|如果|假如|若是|就算|哪怕|是否|能否|会不会"
                         r"|自洽|一致|相符|吻合", l):
                continue
            # 定义句 / 原则句 / 章节标题：在**定义**概念，不是在**宣称**自己的状态
            if re.match(r"^\s*(?:#{1,6}\s|\*\*定义\*\*|定义\s*[:：]|>\s|-\s*\*\*定义)", l):
                continue
            if re.search(r"(?:^|[；;。])\s*[^，。；]{0,6}\s*(?:＝|=|≠)\s*(?:判成|当成|视为|被当成|被当作)", l):
                continue
            # 修法/规范/清单段：在写"应该怎么做"，不是在宣称"我已经做到了"
            if re.search(r"修法|共性|规范|纪律|应当|必须|不许|阈值|判据是|清单|步骤\s*[0-9一二三]|立\"|第[一二三四五]步", l):
                continue
            if rx.search(l):
                out.append((i, l))
        return out

    def _span(self, lines: list[str], ln: int, back: int = 0) -> str:
        """取第 ln 行附近的一段（同段），用于判断"有没有配套声明"。"""
        a = max(0, ln - 1 - back)
        b = min(len(lines), ln - 1 + self.K["span_lines"])
        return "\n".join(lines[a:b])

    # ---------------- 七道检查 ----------------
    def b1_target(self, text: str) -> dict:
        """判据必须声明"要保证的量"，且该量应锚在最终产物上。

        病根 1 的形态：「所有判据都在量『我设的键有没有生效』，
        没有一条在量『画面上看起来对不对』」（行 1731）。
        """
        lines = self._lines(text)
        # ★ 只认"判据**声明**"（判据/闸门/断言 + 冒号或"＝"/"为"），
        #   不认"判据遍/该判据"这类**引用**——否则"判据遍不归零"会被误当声明而误杀。
        decl_pats = [r"判据\s*[:：]", r"闸门\s*[:：]", r"断言\s*[:：]", r"验收标准\s*[:：]",
                     r"判据\s*=", r"判据为", r"断言为"]
        crit = self._hits(text, decl_pats)
        if not crit:
            return {"gate": "B1_target", "ok": True, "detail": "未出现判据声明，跳过"}
        missing = []
        for ln, _ in crit:
            seg = self._span(lines, ln, back=1)
            if not re.search("|".join(self.K["goal_pats"]), seg):
                missing.append(ln)
        ok = len(missing) == 0
        return {
            "gate": "B1_target", "ok": ok,
            "detail": (f"{len(crit)} 处判据全部声明了目标量" if ok
                       else f"{len(missing)}/{len(crit)} 处判据未声明『要保证的量』，行 {missing[:8]}"),
            "lines": missing[:20],
        }

    def b2_cite(self, text: str) -> dict:
        """可变物引用必须带取数时刻。

        病根 2：「行号与 md5、计数一样会过期」（行 48768）。
        """
        lines = self._lines(text)
        vol = self._hits(text, self.K["volatile_pats"]) + self._hits(text, self.K.get("count_cite_pats", []))
        if not vol:
            return {"gate": "B2_cite", "ok": True, "detail": "未出现可变物引用，跳过"}
        bad = []
        for ln, _ in vol:
            seg = self._span(lines, ln, back=2)
            if not re.search("|".join(self.K["stamp_pats"]), seg):
                bad.append(ln)
        ok = len(bad) == 0
        return {
            "gate": "B2_cite", "ok": ok,
            "detail": (f"{len(vol)} 处可变物引用均带取数时刻" if ok
                       else f"{len(bad)}/{len(vol)} 处引用无取数时刻，行 {bad[:8]}"),
            "lines": bad[:20],
        }

    def b3_reopen(self, text: str) -> dict:
        """报"已改/完成"必须附 reopen 后的实测值。

        病根 3：「内存态达标一律不算通过」（行 48244）。
        """
        lines = self._lines(text)
        claim = self._hits(text, self.K["claim_pats"])
        if not claim:
            return {"gate": "B3_reopen", "ok": True, "detail": "未出现『已改/完成』类声明，跳过"}
        bad = []
        for ln, _ in claim:
            seg = self._span(lines, ln, back=2)
            if not re.search("|".join(self.K["reopen_pats"]), seg):
                bad.append(ln)
        ok = len(bad) == 0
        return {
            "gate": "B3_reopen", "ok": ok,
            "detail": (f"{len(claim)} 处『已改』声明均带回读证据" if ok
                       else f"{len(bad)}/{len(claim)} 处声明无 reopen 回读证据，行 {bad[:8]}"),
            "lines": bad[:20],
        }

    def b4_ruler(self, text: str, ruler_keys: list[str] | None = None) -> dict:
        r"""量具四问：帧 / 归零遍 / 算法 / 文件集。

        病根 4：「同一对主体/背景：4.55（不过）vs 41.65（过）」（行 48069）。

        ★ 空文件/无数字的报告跳过 —— 没有量测就没有量具可问。
          （实测踩过：空文件被 B4 拦下 = 误杀；其余六道都会"没内容就跳过"，
            本道原先漏了这个前置，是与其它闸不一致。）

        ★★ 2026-10-04 修（硬缺陷 · 判据量错对象 · 本族第三例）：
          原文前置条件是 `has_numbers = bool(re.search(r"\d", text))` ——
          **把"文档里有没有数字"当成了"这份报告做没做过量测"**。
          实测（全量 4070 份 .md）：
              B4 不过 3938/4070 = **96.8%**；仅因 B4 被拦 2063/4070 = **50.7%**
            `AGENTS.md`（行号/字节数）、`SKILLS_INDEX.md`（技能编号）、
            复盘（日期/帧号）、`Blender建模知识库/*`（章节号）——
            **它们天然含数字，但从不报量测** ⇒ 必然误杀。
          且 `kind=report/article/auto` 三种全部照拦（`article` 只逃生 B1/B3/B6）。
          ⇒ 与刚修好的 g_lang（"结论明确"量成字数）、g_struct（体裁量成文件名）**同源**。

          修法（治本）：前置条件改为**"有量测/交付语境"**，而不是裸 `\d`：
            ① 出现量测动词（实测/测得/量得/读数/均值/占比/帧数/md5/渲染…）；或
            ② 出现"带单位的物理量 + 比较断言"（`4.55 vs 41.65`、`≥8`、`±0.02 m`）；
          含数字但**无量测语境** ⇒ 跳过（与 B1/B2/B3/B5/B6/B7 的"没内容就跳过"语义一致）。
          ★ 注意：**不是放宽 B4** —— 真做过量测的报告照样要答上四问（见 demo 负例）。
        """
        keys = ruler_keys or self.K["ruler_keys"]
        if not text.strip():
            return {"gate": "B4_ruler", "ok": True,
                    "detail": "空报告，无量具可问，跳过"}

        # 量测语境 ①：量测/交付动词（这些词出现 ⇒ 文档在报"我量过"）
        #
        # ★★ 2026-10-04 二次修（Lead 复核发现，本族**第四次**复发）：
        #   上一版把 `口径|阈值|判据|指标|算法|归零|掩膜|量具` 也算作"量测语境"，
        #   但它们在**说明性文档里是普通名词**，不代表"我量过"：
        #     实测 `SKILLS_INDEX.md`（能力索引）**唯一命中词是「口径」**
        #     ⇒ 被判为"量测报告" ⇒ 要求答上量具四问 ⇒ **误杀**。
        #   这与本函数上一版修的是**同一个病**（判据量的是"有没有那个词"，
        #   而不是"这份文档是不是在报告一次量测"），只是换了个词表位置复发。
        #   ⇒ 收紧口径：只保留**动作性**的词（"我做了量测"的动词/名词化结果），
        #     把"描述性名词"（口径/判据/指标/算法…）**移出**触发条件 ——
        #     它们在真正的量测报告里必然伴随 `实测/测得/读数/均值` 等硬词。
        measure_ctx = re.search(
            r"实测|测得|量得|量测|读数|读数为|均值|平均值|中位数"
            r"|帧数|帧率|采样|样本数|误差|偏差|方差|标准差|像素|分辨率"
            r"|md5|哈希|校验值|指纹"
            r"|已渲|渲染帧|渲染了|接触表|出片"
            r"|对比度|亮度|色偏|面积比|IoU|重合度"
            r"|复算|逐帧|探针|量具四问",
            text, re.I,
        )
        # 量测语境 ②：带单位的物理量 + 比较断言（"4.55 vs 41.65" 这种形态）
        # ★★ 2026-10-04 修（实测踩到）：原连字符模式把**日期** `2026-10-03`
        #   当成了"两个数的比较" ⇒ 纯章节/日期文档被误判为量测报告
        #   （实测 `# 建模知识库 / ## 10 基础操作 / 2026-10-03 更新，共 12 节` 被 B4 拦）。
        #   修法：① 先抹掉日期时间串；② 连字符两侧**必须带小数点**
        #        （真实比较读数形如 4.55 vs 41.65；而日期是整数-整数）。
        scrubbed = re.sub(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?", " ", text)
        # ★★ 2026-10-04 二次修（Lead）：原 `[≥≤<>±]\s*\d` 让 `> 1`（Markdown 引用块里
        #   极常见的写法）也算"比较断言" ⇒ 说明性文档被误判为量测报告。
        #   ⇒ 要求**带小数或带单位**，即真的在读一个物理量的数。
        unit_cmp = re.search(
            r"[-+]?\d+(?:\.\d+)?\s*(?:mm|cm|km|kg|ms|fps|px|°|度|%|％)"
            r"|[-+]?\d+\.\d+\s*(?:vs|VS|对比|至|~|～|—|-)\s*[-+]?\d+\.\d+"
            r"|\d+\.\d+\s*(?:m|s)\b"
            r"|[≥≤±]\s*\d+\.\d+"                      # ★ 收紧：必须带小数点
            r"|[<>]\s*\d+(?:\.\d+)?\s*(?:mm|cm|kg|ms|px|%|％)",   # ★ 收紧：必须带单位
            scrubbed, re.I,
        )
        if not (measure_ctx or unit_cmp):
            return {"gate": "B4_ruler", "ok": True,
                    "detail": "含数字但**无任何量测语境**（无实测/测得/均值/帧数/单位比较等）"
                              "⇒ 不是量测报告，无量具可问，跳过"}

        present = [k for k in keys if k in text]
        need = max(2, len(keys) - 1)   # 至少答上大半
        ok = len(present) >= need
        return {
            "gate": "B4_ruler", "ok": ok,
            "detail": (f"量具四问答上 {len(present)}/{len(keys)}：{'、'.join(present)}" if ok
                       else f"量具四问只答上 {len(present)}/{len(keys)}（需 ≥{need}）：{'、'.join(present) or '无'}"),
        }

    def b5_scope(self, text: str) -> dict:
        """说"没有"必须同时说"扫了什么范围"。

        病根 5：「读不到 ≠ 没有」（行 8697）。

        ★★★ 2026-10-04 Lead 重写（第三次，前两次都在**调词表**上打转 ⇒ 必须换判据形状）

        **前两次为什么失败**（记录在此，防再犯）：
          · 第一版：裸 `不存在` 命中 ⇒ 把"讲规矩冲突""定位 bug""作澄清"全误杀；
          · 第二版：加全称量词/检索动词 ⇒ 又把「找不到就新建」「崩溃栈里没有任何文件操作」
            这类**叙述句**误杀；收紧"任何"后，「所有会话里都没有」又漏了。
          ⇒ **在词表上找平衡永远调不准** —— 因为"叙述"与"断言"**不是词的区别，是句子的区别**。

        **正解：判"这一句是不是结论性断言"，而不是判"有没有某个词"。**

        B5 要治的形态是【**下了"没有"的结论**，却没交代范围】。这种句子有共同特征：
          ① **含**"没有/不存在/查无/零命中"这类**否定存在**的词；
          ② **不含**"叙述性动作主体"（我/他/脚本/流程 在 做 某事）——
             因为那是**在讲过程**，不是在**下结论**；
          ③ 该否定**指向一个可作为查证对象的东西**（文件/记录/条目/字段/项…）
             或**句末就是那个否定**。

        ⇒ 判据：句子里有否定存在词 **且** 不像叙述句 **且** 否定后有可查证宾语或到句末。
        """
        lines = self._lines(text)
        cand = self._hits(text, self.K["absence_pats"])
        if not cand:
            return {"gate": "B5_scope", "ok": True,
                    "detail": "未出现『不存在/没有』类候选断言，跳过"}

        narr = self.K.get("absence_narrative_pats") or []
        concl = self.K.get("absence_conclusion_pats") or []
        scope = self.K["scope_pats"]

        ab, bad = [], []
        for ln, raw in cand:
            seg = self._span(lines, ln, back=2)

            # ── 句级判据（Lead 定稿，2026-10-04）──────────────────────────
            # B5 要治的是【**下了"没有"的结论**，却没交代范围】。
            # 判"这一句是不是结论"，而不是判"有没有某个词"：
            #   ① 有**结论特征** ⇒ 是断言；
            #   ② 有**叙述特征**且无结论特征 ⇒ 是讲过程/报现象 ⇒ **不算**；
            #   ③ 两者都无 ⇒ 保守：**不算**（宁不拦，不误杀真实文档）。
            #      —— 这条取舍的理由：B5 是"软提醒"性质，误杀真实文档的代价
            #         （17 份全被拦）远大于漏过一句（`g_root` 另有 B1/B3/B6 兜底）。
            is_concl = bool(re.search("|".join(concl), seg)) if concl else False
            is_narr = bool(re.search("|".join(narr), seg)) if narr else False
            if not is_concl or is_narr:
                continue

            ab.append(ln)
            if not re.search("|".join(scope), seg):
                bad.append(ln)

        if not ab:
            return {"gate": "B5_scope", "ok": True,
                    "detail": (f"召回 {len(cand)} 处『没有』类表述，"
                               f"经**句级判据**判定**均非结论性断言** ⇒ 跳过")}
        ok = len(bad) == 0
        return {
            "gate": "B5_scope", "ok": ok,
            "detail": (f"{len(ab)} 处**结论性**『没有』断言均带扫描范围" if ok
                       else f"{len(bad)}/{len(ab)} 处**结论性**『没有』断言无扫描范围，行 {bad[:8]}"),
            "lines": bad[:20],
        }

    def b6_sweep(self, text: str) -> dict:
        """改一处必须扫同类，并报命中数。

        病根 6：「改一处不扫同类」（行 31172）。
        """
        lines = self._lines(text)
        ch = self._hits(text, self.K["change_pats"])
        if not ch:
            return {"gate": "B6_sweep", "ok": True, "detail": "未出现改动声明，跳过"}
        bad = []
        for ln, _ in ch:
            seg = self._span(lines, ln, back=2)
            if not re.search("|".join(self.K["sweep_pats"]), seg):
                bad.append(ln)
        ok = len(bad) == 0
        return {
            "gate": "B6_sweep", "ok": ok,
            "detail": (f"{len(ch)} 处改动均附同类扫描" if ok
                       else f"{len(bad)}/{len(ch)} 处改动无同类扫描，行 {bad[:8]}"),
            "lines": bad[:20],
        }

    def b7_verify(self, text: str) -> dict:
        """报告里"我看到的"与"我实测的"必须分开；推断不许当实测。

        病根 7：「归因必须给分层实测……不许拿目视读数当机制结论」（行 2283）。
        """
        guess = self._hits(text, self.K["guess_pats"])
        measure = self._hits(text, self.K["measure_pats"])
        if not guess:
            return {"gate": "B7_verify", "ok": True,
                    "detail": f"未出现推断类措辞，跳过（实测标记 {len(measure)} 处）"}
        # 有推断词、却完全没有实测证据 ⇒ 拦
        if not measure:
            return {"gate": "B7_verify", "ok": False,
                    "detail": f"出现 {len(guess)} 处推断措辞但全文 0 处实测证据，行 {[l for l, _ in guess[:8]]}",
                    "lines": [l for l, _ in guess[:20]]}
        ratio = len(guess) / max(1, len(measure))
        ok = ratio <= 1.0
        return {
            "gate": "B7_verify", "ok": ok,
            "detail": (f"推断 {len(guess)} 处／实测 {len(measure)} 处（比 {ratio:.2f} ≤ 1.0）" if ok
                       else f"推断 {len(guess)} 处／实测 {len(measure)} 处（比 {ratio:.2f} > 1.0：推断多于实测）"),
        }

    # ---------------- 总检 ----------------
    def check_text(self, text: str, *, kind: str = "auto", strict: bool = False) -> dict:
        """跑七道闸。

        kind: report / plan / article / memo / auto
          · report / plan —— 交付报告，跑全部七道
          · article（纯文章/情报稿）—— 只跑 B2/B4/B5，避免误杀
          · memo（记忆/索引/说明类）—— **只诊引用与范围，不问量具**（见下）
          · auto —— 同 report（保守：不自动放宽）

        ★★ 2026-10-04 新增 `memo`（治"对记忆/索引文件重复要求交付栏目"）：
          实测背景：`AGENTS.md` / `SKILLS_INDEX.md` / `_guard/README.md` / 复盘
          这类**记忆·索引·说明**文档，`g_doc` 已经在管它们的健康度
          （注入上限/最高优先级位置/索引塞详情…）。
          `g_root` 不该对**同一批文件**再要求"量具四问/同类扫描"这些**交付栏目** ——
          那是两类完全不同的文档职责，混在一起必然误杀。
          ⇒ memo 语义：**只诊 B2（引用会过期）与 B5（范围不全说没有）**，
             这两条对记忆文件**确实适用**（引用会过期、说"没有"要报范围）；
             不问量具（B4）、不问回读（B3）、不问同类扫描（B6）。
        """
        if kind == "memo":
            checks = [self.b2_cite(text), self.b5_scope(text)]
        elif kind == "article":
            checks = [self.b2_cite(text), self.b4_ruler(text), self.b5_scope(text)]
        else:
            checks = [
                self.b1_target(text), self.b2_cite(text), self.b3_reopen(text),
                self.b4_ruler(text), self.b5_scope(text), self.b6_sweep(text),
                self.b7_verify(text),
            ]

        failed = [c for c in checks if not c["ok"]]
        # ★★ 2026-10-04 修（硬缺陷 · 恒假分支）：
        #   原文 `HARD = {"B1","B3","B6","B7"}` 且 `hard_failed = 只有 HARD 才进` ⇒
        #   **B2_cite / B4_ruler / B5_scope 永远不可能出现在 hard_failed 里**。
        #   实测后果（真实文本）：一份"七病根全犯"的闸门验证报告
        #   （宣称"七道闸全过、无遗漏、可交付"，无回读/无同类扫描/无量具/无范围）
        #   在本闸下 **放行 6/7**，唯一不过项是 B4 —— 而 B4 不是硬拦 ⇒ 整套判 ok=True。
        #   病根形态：**判据量错对象** —— 声称"七病根闸"，实际只有 4 条能拦停。
        #   修法：默认**七道全硬拦**（这才是"七病根闸"该有的语义）；
        #         要放宽必须显式配置 `root_gate.soft_gates`（不再是代码里写死的静默降级）。
        default_hard = {c["gate"] for c in checks}
        soft = set((self.K.get("soft_gates") or []) if isinstance(self.K.get("soft_gates"), list)
                   else [])
        HARD = default_hard - soft
        # 兼容旧写法：配置里若显式给了 hard_gates，则以它为准（并集语义，防止旧配置变宽）
        legacy_hard = self.K.get("hard_gates")
        if isinstance(legacy_hard, list) and legacy_hard:
            HARD = set(legacy_hard)
        hard_failed = [c for c in failed if c["gate"] in HARD] if not strict else failed
        ok = len(hard_failed) == 0 if not strict else len(failed) == 0

        n_pass = sum(1 for c in checks if c["ok"])
        summary = (f"根治病根闸 {n_pass}/{len(checks)} 通过"
                   + ("" if ok else "；不过项：" + "；".join(c["detail"] for c in (hard_failed or failed))))
        rep = {
            "gate": "g_root", "ok": ok, "strict": strict, "kind": kind,
            "passed": n_pass, "total": len(checks),
            "checks": checks, "failed": [c["gate"] for c in failed],
            "hard_failed": [c["gate"] for c in hard_failed],
            "summary": summary,
        }
        self._log(rep)
        return rep

    def check_file(self, p: str | Path, **kw) -> dict:
        p = Path(p)
        rep = self.check_text(p.read_text(encoding="utf-8", errors="replace"), **kw)
        rep["file"] = str(p)
        return rep

    def _log(self, rep: dict) -> None:
        try:
            import time
            with self.ledger.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "ok": rep["ok"], "passed": rep["passed"], "total": rep["total"],
                    "failed": rep["failed"], "file": rep.get("file", ""),
                }, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            # ★★ 2026-10-04 修（静默吞异常）：原为 `pass` ⇒ **台账写失败无人知晓**，
            #   于是"闸门跑过了"这件事本身没有任何留痕，事后无法复核。
            #   修法：至少打到 stderr（不抛，避免因台账问题阻断主流程，但绝不无声）。
            import sys as _sys
            print(f"[g_root] ⚠ 台账写入失败（本次判定仍有效，但未留痕）："
                  f"{type(e).__name__}: {e} → {self.ledger}", file=_sys.stderr)

    def require(self, text: str, **kw) -> dict:
        """跑闸，不过就 raise —— 这是唯一能让闸门"拦停"的入口。"""
        rep = self.check_text(text, **kw)
        if not rep["ok"]:
            raise RootGateFailed(rep["summary"])
        return rep


class RootGateFailed(RuntimeError):
    """根治病根闸未通过 —— 不许宣布完成/交付。"""


# --------------------------------------------------------------------------
# 负例自检：证明"拦得住"
# --------------------------------------------------------------------------
def demo(cfg: ProjectConfig) -> int:
    g = RootGate(cfg)
    fails = 0
    print("### 根治病根闸 · 负例自检（每条都是《问题总表》的真实形态）\n")

    cases = [
        ("B1 判据量错对象",
         "本镜判据：肩 0→−44°→0、肘 0→−27°→0。全部通过。",
         "B1_target"),
        ("B2 引用会过期",
         "该文件 md5 为 8d8a08c4，第 217 行写着 S17 三版陈设。已核对无误。",
         "B2_cite"),
        ("B3 没落盘当已改",
         "已修改 27 个对象的 hide_render=True，存盘成功。",
         "B3_reopen"),
        ("B5 范围不全说没有",
         "源稿没有提到四幕式结构，该概念零命中。",
         "B5_scope"),
        ("B7 推断当实测",
         "这段应该是渲染层抖动导致的，看起来像 TAA 采样问题，大概改一下参数就好。",
         "B7_verify"),
    ]
    for name, text, expect in cases:
        rep = g.check_text(text, kind="report")
        hit = expect in rep["failed"]
        mark = "✓✓ 拦住" if hit else "✗✗ 漏了"
        if not hit:
            fails += 1
        print(f"  {mark}  {name}")
        bad = [c for c in rep["checks"] if not c["ok"]]
        for c in bad:
            print(f"         → {c['detail']}")
    print()

    print("### 正例：合规报告应放行")
    good = """本报告全部读数均为实测。
### 量具口径
- 用什么帧：出片态最终灯光的正常渲染帧（f01/f12/f24）
- 归零：掩膜遍归零 World 取 alpha；判据遍不归零
- 算法：|主体均值 − 背景均值| ≥ 8
- 文件集：_previs/_shots/S07_*.png，共 3 帧
### 改动与回读
已修改 hide_render=True；改＋存盘即退 → 独立进程 reopen 复读 = True（实测值）。
同类扫描：全量 grep hide_render，命中 3 处，其余 2 处一并改净，无其它残留。
### 范围
扫描范围：全部 88 个 .blend 文件；"不存在"结论仅就本范围内成立。
### 证据分离
我看到的：画面里人形可辨。
我实测的：主体均值 42.59 / 背景均值 49.57，差 6.98（< 8，不过）。
出处：md5 34533610ab77…（实测于 2026-10-03 11:20）。
"""
    rep = g.check_text(good, kind="report")
    if rep["ok"]:
        print("  ✓ 放行：" + rep["summary"])
    else:
        print("  ✗ 误杀：" + rep["summary"])
        fails += 1
    print()

    # ★ 2026-10-15 负例（防"触发词再被收窄"复发）：
    #   B3 的触发条件原来是字面动词清单（已改/已更新/已完成/已修复/已生效/改好了）。
    #   实测：只写「这个问题**已根治**」⇒ 报"未出现『已改/完成』类声明，跳过"
    #   ⇒ **整条根因判据根本不跑**。现在按形态判据（已/都/均/经 + 改动类动词）⇒ B3 必须到达。
    #   这里**直接单测 B3 本身**（不经过 check_text）—— 免得被 B4 量具四问的其它要求带偏判据。
    print("### 负例：只写『已根治』且**没有**独立复验证据 ⇒ B3 必须到达并拦下")
    bad_claim = "这个问题已根治，改动见 diff。"
    rowb = g.b3_reopen(bad_claim)
    arrived = "跳过" not in str(rowb.get("detail", ""))
    hit = rowb.get("ok") is False
    print(f"  {'✓✓到达并拦下' if (arrived and hit) else '✗✗漏放'}  B3：{str(rowb.get('detail'))[:90]}")
    if not (arrived and hit):
        fails += 1
    #   正例（不许误杀）：带**独立复验**证据的同一句声明 ⇒ 必须放过
    ok_claim = "这个问题已根治。另起一个进程重跑脚本实测：5/5 通过。"
    rowk = g.b3_reopen(ok_claim)
    if rowk.get("ok") is True and "跳过" not in str(rowk.get("detail", "")):
        print("  ✓ 不误杀：声明带独立复验证据 ⇒ B3 放过")
    else:
        print(f"  ✗ 误杀：{str(rowk.get('detail'))[:90]}")
        fails += 1
    #   正例（不许误杀）：**没有**改动声明的文本 ⇒ B3 应当跳过（保持原行为）
    rown = g.b3_reopen("我读了 3 个文件，把现状记录下来了。")
    if "跳过" in str(rown.get("detail", "")):
        print("  ✓ 不误杀：无改动声明 ⇒ B3 跳过")
    else:
        print(f"  ✗ 误触发：{str(rown.get('detail'))[:90]}")
        fails += 1
    print()

    print(f"### 自检结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        # 2026-10-04：原为 `pass`（静默）。切 UTF-8 失败不影响判定，但仍要可诊断，
        #   否则"本来就没切"与"切失败了"分不开（后者会让 ✓/✗ 变乱码却无人知晓）。
        print(f"[警告] stdout 切 UTF-8 失败（不影响判定，但输出符号可能乱码）："
              f"{type(e).__name__}: {e}；可设 PYTHONIOENCODING=utf-8 重跑", file=sys.stderr)
    from guard import load as load_cfg

    ap = argparse.ArgumentParser(prog="g_root")
    ap.add_argument("--project", help="可省：默认从当前目录自动识别")
    ap.add_argument("--project-root", help="可省：默认从当前目录自动识别")
    ap.add_argument("--text-file", help="要检查的文件")
    ap.add_argument("--kind", default="report",
                    choices=["report", "plan", "article", "memo", "auto"],
                    help="memo=记忆/索引/说明类（只诊引用与范围，不问量具）")
    ap.add_argument("--strict", action="store_true", help="严格模式：任一不过即拦")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--demo", action="store_true", help="负例自检")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)

    if a.demo:
        sys.exit(demo(cfg))

    g = RootGate(cfg)
    if a.text_file:
        rep = g.check_file(a.text_file, kind=a.kind, strict=a.strict)
        if a.json:
            print(json.dumps(rep, ensure_ascii=False, indent=1))
        else:
            print(rep["summary"])
            for c in rep["checks"]:
                print(("  ✓ " if c["ok"] else "  ✗ ") + c["gate"] + "：" + c["detail"])
        sys.exit(0 if rep["ok"] else 1)
    print("用法：--demo 自检 / --text-file <文件> 检查")
