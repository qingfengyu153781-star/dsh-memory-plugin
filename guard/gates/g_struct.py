# -*- coding: utf-8 -*-
"""_guard · 结构声明闸 g_struct —— 不查"用了什么词"，只查"有没有那一栏"

## 为什么有这道闸（治本，2026-10-03）

**实测出的病**（`case_test.py --t3`）：`g_root` 的识破率 **0/7** ——
把「已改」写成「处理完毕」、把「md5」写成「校验值」、改用英文、塞进代码块……
**换个说法就绕过了**。

**根因（不是"词表不够"）**：
> `g_root` 问的是「**你有没有说『已改』这个词**」。
> 只要不用那个词，它就看不见 —— 这是**关键词匹配**，不是**理解**。

**为什么"加词表"治不了本**：
> 表里 4,888 条问题有 **3,510 种说法** —— **人就是会换着说法犯错**。
> 加词表 = 打地鼠，且永远落后一步。（这正是表里"病根 6：改一处不扫同类"的形态）

## 治本做法：把"说了什么词"换成"有没有那一栏"

不再问"你有没有说『已改』"，改问：

    「你的报告里**有没有 `## 回读证据` 这一栏**？栏里**有没有 `实测值:` 这个字段**？」

- **换词没用** —— 要求的是**结构**（章节标题 + 字段名），不是词
- **漏写立刻暴露** —— 不是"你措辞不对"，而是"你根本没有这一栏"
- **可机械判定** —— 章节存在性 + 字段存在性，二进制结果，无歧义

## 七栏（与 7 个病根一一对应）

| 栏 | 治的病根 | 必备字段 |
|---|---|---|
| `## 判据` | B1 判据量错对象 | `要保证的量:` |
| `## 出处` | B2 引用会过期 | `实测于:` |
| `## 回读证据` | B3 没落盘/被覆盖 | `实测值:` |
| `## 量具口径` | B4 口径不统一 | `算法:` `文件集:` |
| `## 扫描范围` | B5 范围不全 | `范围:` |
| `## 同类扫描` | B6 不扫同类 | `命中:` |
| `## 证据分离` | B7 未核实 | `我实测的:` |

## 与 g_root 的分工
  · `g_root`   —— **软闸**：扫描措辞，给提示（可被绕过，但能发现"忘了写"）
  · `g_struct` —— **硬闸**：查结构，**绕不过**（要么有那一栏，要么没有）

跨项目：栏目名与字段名来自 config.struct_gate；本文件不含项目专有内容。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402


# 七栏定义：栏目名（可多个别名）+ 必备字段 + 对应的病根
SECTIONS: list[dict] = [
    {
        "id": "S1", "root": "B1 判据量错对象",
        "names": ["判据", "验收判据", "判据定义", "要保证的量", "验收标准"],
        "fields": ["要保证的量"],
        "why": "判据必须声明『我要保证的那个量』，否则会变成只量过程量（表行 1731）",
    },
    {
        "id": "S2", "root": "B2 引用会过期",
        "names": ["出处", "引用", "来源与出处", "出处与取数时刻"],
        "fields": ["实测于", "取数时刻", "截至"],
        # ★ any-of：三者是**同义别名**（写明取数时点即可），不是"三个都要"。
        "fields_mode": "any",
        "why": "md5/行号/计数都是『活的』，必须带取数时刻（表行 48768）",
    },
    {
        "id": "S3", "root": "B3 没落盘/被求值覆盖",
        "names": ["回读证据", "回读", "复读证据", "reopen 证据"],
        "fields": ["实测值"],
        "why": "报『已改』必须附 reopen 后从盘上读回的值（表行 48244）",
    },
    {
        "id": "S4", "root": "B4 口径不统一",
        "names": ["量具口径", "口径", "量具说明"],
        "fields": ["算法", "文件集"],
        "why": "量具要有身份证，否则同一对象两人算出相反结论（表行 48069）",
    },
    {
        "id": "S5", "root": "B5 范围不全说没有",
        "names": ["扫描范围", "检索范围", "覆盖范围"],
        "fields": ["范围"],
        "why": "说『没有』必须同时说『我扫了什么』（表行 8697）",
    },
    {
        "id": "S6", "root": "B6 改一处不扫同类",
        "names": ["同类扫描", "全量扫描", "同类检查"],
        "fields": ["命中"],
        "why": "改一处必须扫同类并报命中数（表行 31172）",
    },
    {
        "id": "S7", "root": "B7 未核实就下结论",
        "names": ["证据分离", "实测与推断", "我看到的与我实测的"],
        "fields": ["我实测的"],
        "why": "『我看到的』与『我实测的』必须分开（表行 2283）",
    },
    {
        # ★ 引入出处（R-62）：灵枢 lingshu `core.py` 每处修复都带一段
        #   「边界（削掉的判别力，如实标注）」——写明这次**没**解决什么、代价是什么。
        #   ★ 出处写在这里（注释），**不写进下面的字符串**：通用层代码的字符串字面量
        #     不许出现具体项目名（R-56 / g_purity 当场抓到过一次）。
        "id": "S8", "root": "B8 谎报完整（只报修好的，不报没修的）",
        "names": ["边界", "遗留与风险", "遗留问题", "本次未修", "没修什么",
                  "已知缺口", "削掉的判别力", "本次未做"],
        "fields": ["未修", "遗留", "未做", "未扫", "盲区", "缺口"],
        # ★ any-of：这 6 个是**别名**（写其一即可），不是"全都要"。
        #   2026-10-15 修：旧实现一律按 all-of 判 ⇒ 一份只写"未修："的合格报告
        #   被判"缺 5 个字段"（**假报错**）。R-62 的规矩原文就是「其一」。
        "fields_mode": "any",
        "why": "每修一处必写「**边界（削掉的判别力，如实标注）**」——写明这次**没**解决什么、"
               "代价是什么。只报修好的、不报没修的，就是「谎报完整」。",
    },
]

DEFAULTS: dict = {
    # 哪些报告必须有全部七栏（否则只要求与内容相关的栏）
    "full_report_markers": [r"改动", r"修复", r"验收", r"回读", r"交付", r"完成报告"],
    # 按内容自动要求"相关栏"：命中这些词 ⇒ 该栏必需
    "trigger": {
        "S1": [r"判据", r"阈值", r"验收", r"≥", r"≤"],
        "S2": [r"md5", r"指纹", r"哈希", r"行号", r"L\d{2,4}"],
        "S3": [r"已改", r"已修改", r"已更新", r"已修复", r"已生效", r"处理完毕", r"改完"],
        "S4": [r"\d", r"量", r"测", r"均值", r"占比", r"px"],
        "S5": [r"不存在", r"没有", r"未见", r"零命中", r"查无", r"无此"],
        "S6": [r"已改", r"已替换", r"已删除", r"已统一", r"处理完毕"],
        "S7": [r"推断", r"推测", r"似乎", r"看起来", r"应该"],
        # ★ S8（R-62）：**完成类断言**一出现，就必须写"这次没修什么"。
        #   只报修好的 = 谎报完整（灵枢每处修复必附「边界（削掉的判别力）」的反面）。
        #   ★ 触发词只收**完成断言**（已根治/已修复/处理完毕…），**不收**「已删除/已替换/已统一」
        #     这类动作动词——那样会把普通陈述也拉进来，变成**假报错**（误杀）。
        "S8": [r"已根治", r"已修复", r"已改完", r"已全部修", r"处理完毕",
               r"全部改完", r"修完", r"零残留", r"彻底解决"],
    },
    "max_skip_ratio": 0.30,   # 允许最多 30% 的必需栏缺失（给短报告留口子）
    # ★ 兜底：成果类文档即使没有触发词，也必须有的核心栏
    #   （治"需要 0 栏 ⇒ 全过"这个漏洞；见 check_text 里的说明）
    "core_sections": ["S3", "S5"],
}


def _cfg_of(cfg: ProjectConfig) -> dict:
    d = {k: (json.loads(json.dumps(v)) if isinstance(v, (dict, list)) else v)
         for k, v in DEFAULTS.items()}
    over = cfg.get("struct_gate") or {}
    for k, v in over.items():
        d[k] = v
    return d


class StructGate:
    """结构声明闸：查'有没有那一栏'，不查'用了什么词'。"""

    NAME = "g_struct"

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self.K = _cfg_of(cfg)
        lg = (cfg.get("struct_gate") or {}).get("ledger", "_guard_ledger/struct.jsonl")
        self.ledger = cfg.resolve(lg)
        self.ledger.parent.mkdir(parents=True, exist_ok=True)

    # ---------------- 结构解析 ----------------
    @staticmethod
    def parse_sections(text: str) -> dict[str, str]:
        """把 markdown 按 `## 标题` 切成 {标题: 正文}。

        ★ 只认**真正的章节标题**（行首 `#` + 空格），不认代码块/引用块里的伪标题。
          —— 这是"结构"与"措辞"的分界：塞进代码块骗不过结构检查。
        """
        out: dict[str, str] = {}
        cur, buf = None, []
        in_code = False
        for line in text.split("\n"):
            s = line.strip()
            if s.startswith("```"):
                in_code = not in_code
                if cur is not None:
                    buf.append(line)
                continue
            if in_code:
                if cur is not None:
                    buf.append(line)
                continue
            m = re.match(r"^(#{2,4})\s+(.+?)\s*$", line)
            if m:
                if cur is not None:
                    out[cur] = "\n".join(buf).strip()
                cur, buf = m.group(2).strip(), []
            elif cur is not None:
                buf.append(line)
        if cur is not None:
            out[cur] = "\n".join(buf).strip()
        return out

    @staticmethod
    def _norm(s: str) -> str:
        """标题归一：去序号/标点/空格/加粗标记/emoji，只留汉字字母数字。"""
        s = re.sub(r"\*\*|__|`", "", s)
        s = re.sub(r"^[\s\d\.、\-—·]+", "", s)
        s = re.sub(r"[\s\u3000]+", "", s)
        s = re.sub(r"[^\w\u4e00-\u9fff]", "", s)
        return s.lower()

    def find_section(self, sections: dict[str, str], sec: dict) -> tuple[str, str] | None:
        """找栏目：标题归一后**包含**任一别名即算命中（比精确匹配宽容，比词表严格）。

        ★ 2026-10-15 修（自查时撞出来的，属**判据量错对象**）：同义栏可能**不止一个**
          ——报告里既有 §1.5「诚实边界（A 线）」又有 §「边界（这次没做的）」，两者都
          含别名「边界」。旧实现返回**第一个**命中项 ⇒ 若第一个不含必备字段，就报
          「缺字段」，而真正合格的那一栏**从头到尾没被看过**（假红 = 假报错）。
          现：优先返回**含必备字段**的命中项；都不含才返回第一个（保住"缺字段"语义）。
        """
        wants = [self._norm(n) for n in sec["names"]]
        hits: list[tuple[str, str]] = []
        for title, body in sections.items():
            t = self._norm(title)
            for w in wants:
                if w and w in t:
                    hits.append((title, body))
                    break
        if not hits:
            return None
        for title, body in hits:                      # 先找"字段齐全"的那一栏
            fp = self.field_present(body, sec.get("fields") or [])
            if (sec.get("fields_mode") == "any" and fp) or \
               (sec.get("fields_mode") != "any" and len(fp) == len(sec.get("fields") or [])):
                return title, body
        return hits[0]

    def field_present(self, body: str, fields: list[str]) -> list[str]:
        """栏目正文里有没有必备字段（字段名后接 : 或 ：）。"""
        found = []
        for f in fields:
            if re.search(re.escape(f) + r"\s*[:：]", body):
                found.append(f)
        return found

    # ---------------- 实质内容判据（治本第二层） ----------------
    # 问题：光有栏目不够 —— 空壳（`- 实测值：`）和废话（`- 实测值：没问题`）也能过。
    # 治法：**每栏必须写出"可被独立核对的东西"**。
    #   不要求特定写法（那就成模板了），只要求"含可核对物" ——
    #   数字 / 哈希 / 路径 / 文件名 / 命令 / 帧号 / 版本号，任一即可。
    # 为什么这不算模板：它约束的是**信息量**（有没有可查的东西），不是**句式**。
    #   可以用任何语言、任何结构、任何措辞 —— 只要有真东西在里面。
    VERIFIABLE = [
        (r"\d", "数字"),
        (r"\b[0-9a-fA-F]{8,}\b", "哈希/ID"),
        (r"[A-Za-z]:\\|/[\w\-]+/[\w\-]+|\.(?:py|md|blend|png|json|yaml|mp4|csv)\b", "路径/文件名"),
        (r"\b(?:python|node|grep|ffmpeg|blender|pwsh|curl)\b", "命令"),
        (r"\bf\d{1,4}\b|帧\s*\d+", "帧号"),
        (r"\bv?\d+\.\d+(?:\.\d+)?\b", "版本号"),
    ]

    def _has_verifiable(self, body: str) -> list[str]:
        """这一栏里有没有"可被独立核对的东西"？返回命中的类别。"""
        return [name for pat, name in self.VERIFIABLE if re.search(pat, body)]

    def _field_value(self, body: str, field: str) -> str:
        """取 `字段：` 后面那一段。"""
        m = re.search(re.escape(field) + r"\s*[:：]\s*(.*)", body)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _is_placeholder(val: str) -> bool:
        """占位/废话判定：空、极短、或纯套话。"""
        v = re.sub(r"[\s\-—·。，,、;；]+", "", val)
        if not v or len(v) <= 2:
            return True
        return bool(re.fullmatch(
            r"(?:没问题|都看了|都好了|一切正常|正常|搞定了|妥了|完成|已核实|通过|无异常|同上|见上|无|NA)+",
            v, re.I))

    def _looks_like_report(self, text: str, sections: dict[str, str]) -> bool:
        """判断"这是不是一份成果类文档"——**不依赖触发词**。

        ★ 2026-10-03 修正一：原先设了「有标题 + 正文 ≥ 80 字」的门槛，
          实测被 `## ✅ 已完成的改动 / 全部都弄好了，存档没问题。`（仅 22 字）绕过。
          **教训：短报告恰恰最容易藏问题** —— 越短越可能"啥也没交代就报完成"。
        ★ 2026-10-03 修正二：加**体裁排除** —— README / AGENTS / 索引 / 规范类
          文档不是"成果报告"，不该被硬套七栏（实测：它们被 need=7 missing=7 全拦，
          属误杀。这正是"分不清体裁"那条病根，不能自己再犯一次）。
        """
        if not text.strip():
            return False

        # ① 体裁排除：说明/索引/规范/手册类 —— 它们描述"是什么"，不宣称"我做完了"
        if self._is_reference_doc(text):
            return False

        body_len = len(re.sub(r"\s", "", text))
        # ② 明确的完成叙事 —— **不设长度门槛**（再短也要交代清楚）
        if re.search(r"完成|搞定|弄好|做好了|改好|处理完|存档|保存|没问题|弄完|妥了|就好了", text):
            return True
        # ③ 有标题 + 够长 + 有"做过事"的痕迹 ⇒ 也算成果文档（防"换个说法描述完成"）
        has_title = bool(re.match(r"^#{1,3}\s+\S", text.lstrip()))
        if has_title and body_len >= 80 and re.search(r"已|检查过|核对过|更新|改动", text):
            return True
        if body_len >= 120 and re.search(r"已|检查过|核对过|更新|改动", text):
            return True
        return False

    @staticmethod
    def _is_reference_doc(text: str, filename: str = "") -> bool:
        """体裁排除：这份文档是"说明书/索引/规范/记忆文件"，还是"成果报告"？

        判据（任一成立即判为参考类，不做成果报告要求）：
          · **文件名**含 README / AGENTS / INDEX / 索引 / 规范 / 手册 / 指南 / 速查 / SKILL
            （最可靠：文件名是人起的，且不会因为内容变化而漂）
          · 开头 6 行里的标题含这些词
          · 有"用法/安装/配置/参数/接口/示例/FAQ"这类手册型章节
          · 代码块 ≥ 6 且无明确完成叙事
          · 表格行 ≥ 15 且够长且无完成叙事（索引/清单型）
        """
        # ① 文件名 —— 最可靠的一层
        #   实测补充（2026-10-03）：真实语料里大量被拦的是**调研报告/问题汇总/
        #   场景设计/交接报告**这类文档 —— 它们不是"改动交付报告"，
        #   却被触发词套上了"回读证据/同类扫描"要求，属误杀。
        #   治法：把已知的非交付体裁写进排除名单（**宁可少拦，不要误杀**）。
        #
        # ★★ 2026-10-04 根治（治"枚举式名单必然漏" · 病根 6）：
        #   实测：`经验复盘/` 下 13 份 `复盘_*.md` 被拦 7 份（need≥3 的全拦）。
        #   根因：名单里有 `记录/纪要/笔记/总结`，**唯独漏了『复盘』** ——
        #   而"复盘"是本机最常见的文档体裁之一。
        #   ⇒ 病根不是"漏了一个词"，而是**枚举式名单这个做法本身**：
        #     列举"哪些不是交付报告"永远列不完（下次还会漏"回顾/反思/教训/手记…"）。
        #   ⇒ 改成**体裁判定**：先问"这份文档是不是在**事后叙述**"，
        #     再看"它是不是在**宣称交付**"。两者取交集才要求七栏。
        #     · 事后叙述标记（文件名或首标题命中）⇒ 参考类，跳过七栏
        #     · 交付声明标记（正文里真的在报"已改/已修复/回读/实测值"）
        #       ⇒ 即使文件名像复盘，也仍按交付报告要求（防"改个名绕过"）
        narrative_doc = bool(filename) and bool(re.search(
            r"复盘|回顾|反思|教训|手记|随记|日志|周记|月报|记录|纪要|笔记|总结"
            r"|调研|汇总|清单|台账|交接|transcript|digest"
            r"|场景设计|分镜|镜表|设计文档|人物表|场次表|pars|pipeline|fact"
            r"|索引|规范|手册|指南|速查|备忘|说明|声明"
            r"|readme|agents?\.md|index|skill"
            r"|^t_\d+|^out_problem|^out_skill|问题库|逐个展开|质检|验收工程",
            filename, re.I))
        if narrative_doc:
            # ★★ 2026-10-04 三次迭代的最终判据（每条都有实测教训）：
            #   一修：加 `复盘` 到名单 —— 打补丁，不合格。
            #   二修：改成"事后叙述体裁则放行，除非它真在报交付" —— 13/13 放行，
            #         但**两个负例被放行**（过矫）：
            #         实测 `# 复盘\n\n已修复…可以交付。` 起名 `复盘_测试.md` 就整份绕过。
            #   三修（现在）：**文件名是可控的，不能当体裁证据**。判据改为看**正文体征**：
            #         · 交付报告结构（七栏 / `实测值：`）⇒ 是交付报告
            #         · 否则必须**正文真像事后叙述**（够长 + 有叙述型章节）才算参考类
            #         · 都不满足（如一句话的"复盘"）⇒ 按交付报告要求（宁可拦，不放假交付过）
            if StructGate._has_delivery_structure(text):
                return False
            if StructGate._is_narrative_body(text):
                return True
            return False   # 文件名像复盘、但正文既不是交付报告也不是真叙述 ⇒ 不豁免

        head = "\n".join(text.split("\n")[:6])
        # ② 标题特征（体裁：说明/索引/规范/记忆）
        if re.search(r"readme|索引|规范|手册|指南|速查|备忘|api\s*文档|接口文档|说明文档|工作区记忆|工作区级记忆",
                     head, re.I):
            return True
        # ②b 首标题是"事后叙述"体裁，且正文真像叙述（同样要求正文体征）
        #     实测：`# 复盘 · 2026-10-01 · …` / `# 复盘：2026-08-25 …`
        if re.search(r"复盘|回顾|反思|教训|手记|随记|日志|纪要|笔记|总结", head):
            if not StructGate._has_delivery_structure(text) \
                    and StructGate._is_narrative_body(text):
                return True
        # ③ 结构特征：手册型章节
        manual_secs = len(re.findall(r"^#{2,4}\s*(?:用法|安装|快速开始|配置|参数|接口|示例|常见问题|FAQ|Usage|Install|Quick)",
                                     text, re.M))
        if manual_secs >= 1:
            return True
        # ④ 代码块密集（手册/规范特征）
        if text.count("```") >= 6 and not StructGate._claims_delivery(text):
            return True
        # ⑤ 纯清单/索引型
        table_rows = len(re.findall(r"^\s*\|.*\|\s*$", text, re.M))
        if table_rows >= 15 and len(re.sub(r"\s", "", text)) > 500 \
                and not StructGate._claims_delivery(text):
            return True
        return False

    # 交付声明标记：正文里**真的在宣称"我改好了/我测过了"**
    #   —— 用于把"事后叙述型文档（复盘/记录/纪要）"与"改动交付报告"分开。
    #   为什么这样判：体裁看**有没有交付动作**，不看文件名列举了几个词。
    DELIVERY_CLAIM = re.compile(
        r"已改|已修复|已修改|已更新|已生效|已统一|已替换|已删除|已重命名"
        r"|回读|复读|reopen|实测值|实测于|存盘成功|已落盘|改完|处理完毕"
    )

    # 交付报告**结构**标记：这份文档是不是按"交付报告"的样子在写？
    #   —— 与"正文里出现了交付动词"是两回事：
    #      复盘会写"当时已修复了 X"（叙述），但不会写「## 回读证据」这一栏（交付结构）。
    #   判据：出现任一下列交付类栏目名 / 回读证据句式。
    DELIVERY_STRUCT = re.compile(
        r"^#{2,4}\s*(?:回读证据|复读证据|reopen\s*证据|实测证据"
        r"|量具口径|扫描范围|同类扫描|证据分离|口径|判据)"
        r"|实测值\s*[:：]|reopen\s*后|回读后",
        re.I | re.M,
    )

    @staticmethod
    def _claims_delivery(text: str) -> bool:
        return bool(StructGate.DELIVERY_CLAIM.search(text))

    @staticmethod
    def _has_delivery_structure(text: str) -> bool:
        """这份文档是否具备"交付报告的结构"（而不只是叙述里提到改动）。"""
        return bool(StructGate.DELIVERY_STRUCT.search(text))

    # ---- "事后叙述"的内容体征（不认文件名，只认正文长得像不像复盘） ----
    #   为什么必须看正文：实测 `# 复盘\n\n已修复…可以交付。` 起名 `复盘_测试.md`
    #   就能整份绕过 ⇒ **文件名是可控的，不能当体裁证据**。
    NARRATIVE_MIN_CHARS = 800     # 复盘/回顾这类文档必有实质篇幅（实测 13 份均 >2.6 KB）
    # 叙述型章节：**不强求章节名**（枚举章节名 = 又一次"列举式名单"，必然漏）。
    #   实测教训：`复盘_示例报告_01_视觉与景别.md` 用的是 `### 症状` 这类
    #   **无序号的三级标题**，原先"序号+顿号"的正则一条都命中 ⇒ 误判非叙述。
    #   正解：只要求"有多个标题把长文切成了段落"，以及"标题/正文在讲经过、原因、教训"。
    NARRATIVE_SECTIONS = re.compile(
        r"^#{2,4}\s*(?:[一二三四五六七八九十\d]+[、.）)]|\S)",
        re.M,
    )
    # 叙述语气（出现过即认为在"讲发生过的事"，而不是在"下达交付声明"）
    NARRATIVE_TONE = re.compile(
        r"经过|当时|后来|原因|根因|症状|现象|教训|经验|对照|对比|时间线"
        r"|事实|推断|处置|对策|改进|结论|总结|回顾|复盘|背景|问题"
    )

    @staticmethod
    def _has_narrative_body(text: str) -> bool:
        """有实质叙述（不是一句话就交差的壳）。"""
        return len(re.sub(r"\s", "", text)) >= StructGate.NARRATIVE_MIN_CHARS

    @staticmethod
    def _is_narrative_body(text: str) -> bool:
        """正文长得像"事后叙述文档"：

        ① 有实质篇幅（不是一句话交差）；
        ② 出现过叙述语气词（经过/当时/根因/教训…）；
        ③ **不是**一段没有结构的流水账 —— 分节形式二选一即可：
           · 有 ≥3 个 markdown 标题，**或**
           · 有 ≥4 个粗体小标题 / 分隔线把内容切块（实测：
             `复盘_示例报告_01_视觉与景别.md` 用 `**…**` 分节，**一个 # 都没有**）

        ★ 全部是**内容体征**，不看文件名、不枚举章节名 —— 枚举必漏（实测踩过两次：
          先是漏 `复盘` 这个词，后是漏 `### 症状` 这种无序号标题）。
        """
        if not StructGate._has_narrative_body(text):
            return False
        if not StructGate.NARRATIVE_TONE.search(text):
            return False
        n_head = len(StructGate.NARRATIVE_SECTIONS.findall(text))
        if n_head >= 3:
            return True
        # 无 markdown 标题时的替代分节体征：粗体小标题 / 分隔线
        n_bold = len(re.findall(r"^\s*(?:\*\*[^*\n]{2,40}\*\*|[-=]{3,})\s*$", text, re.M))
        if n_bold >= 4:
            return True
        # ★★ 第三条体征（实测补）：**条目列表型 / 索引型**文档
        #   实测：`复盘_示例报告_0X_*.md` 三份是"本文件含 4 条 + 逐条列出"的**索引件**
        #   （从 AGENTS.md 的「复盘索引」段迁出的），既没有 markdown 标题、
        #   也没有粗体分节（n_sections=0 / n_bold=1）⇒ 被前两条判据漏掉。
        #   但它们**明显不是交付报告**（没有任何"我改好了"的交付结构）。
        #   体征：≥5 个列表条目 且 有日期/编号式条目头。
        n_items = len(re.findall(r"^\s*(?:[-*+]|\d+[.)])\s+\S", text, re.M))
        has_dated = bool(re.search(r"\d{4}-\d{2}-\d{2}", text))
        return n_items >= 5 and has_dated

    # ---------------- 检查 ----------------
    def check_text(self, text: str, *, strict: bool = False, filename: str = "") -> dict:
        sections = self.parse_sections(text)

        # ★ 体裁闸（最前置）：说明/索引/规范类文档不是"成果报告"，
        #   不该被硬套七栏。实测踩过：README/AGENTS/SKILLS_INDEX 被 need=7 全拦 ——
        #   这正是"分不清体裁"那条病根，不能自己再犯。
        if self.K.get("genre_exclude", True) and self._is_reference_doc(text, filename):
            rep = {
                "gate": self.NAME, "ok": True, "need": 0, "missing": 0, "ratio": 0.0,
                "rows": [], "genre": "reference",
                "summary": "结构声明闸：判为说明/索引/规范类文档，非成果报告，跳过七栏要求",
            }
            self._log(rep)
            return rep

        # 需要哪些栏：按内容触发
        need = []
        for sec in SECTIONS:
            pats = self.K["trigger"].get(sec["id"], [])
            if any(re.search(p, text) for p in pats):
                need.append(sec)

        # ★ 兜底（治本的关键一环）：**"需要 0 栏"本身就是漏洞** ——
        #   实测踩过：`## ✅ 已完成的改动\n全部都弄好了` 里没有触发词 ⇒ 需要 0 栏 ⇒ 全过。
        #   病根：**"我看不出你在宣称什么"不等于"你没宣称"** ——
        #   这正是表里"范围不全却当成没有"（B5）的同型错误，长在了闸门自己身上。
        #
        # ★ 2026-10-03 二修（治规避）：原先只在 `not need` 时才补核心栏 ——
        #   但对抗者可以"**只写会被触发的栏**"（避开"没有/判据"等触发词），
        #   让 need 变小、miss 也随之变小，比率就低了。实测：
        #   只写 `## 判据`(空) + `## 回读证据`(空) ⇒ need=2 miss=2，看似"100%"，
        #   但若他再写得巧一点就能压到 30% 阈值以下。
        #   治法：**成果类文档一律并入核心栏**（不管 need 已经是多少），
        #   让"少写几栏"不再能降低分母。
        if self._looks_like_report(text, sections):
            genre_report = True
            core_ids = self.K.get("core_sections", ["S3", "S5"])
            have = {s["id"] for s in need}
            need += [s for s in SECTIONS if s["id"] in core_ids and s["id"] not in have]
        else:
            genre_report = False

        rows, missing = [], []
        for sec in need:
            hit = self.find_section(sections, sec)
            if hit is None:
                rows.append({"id": sec["id"], "root": sec["root"], "ok": False,
                             "state": "缺栏", "detail": f"缺 `## {sec['names'][0]}` 栏 —— {sec['why']}",
                             "fields_found": []})
                missing.append(sec)
                continue
            title, body = hit
            found = self.field_present(body, sec["fields"])
            need_all = (sec.get("fields_mode") != "any")
            ok_fields = (len(found) == len(sec["fields"])) if need_all else bool(found)
            if not ok_fields:
                lack = [f for f in sec["fields"] if f not in found]
                rows.append({"id": sec["id"], "root": sec["root"], "ok": False,
                             "state": "缺字段",
                             "detail": f"`{title}` 缺字段 {'、'.join(lack)} —— {sec['why']}",
                             "fields_found": found})
                missing.append(sec)
                continue

            # ★ 第二层：字段有了，**里面有没有可被独立核对的东西**？
            #   治"空壳/废话也能过"（实测：`- 实测值：` 与 `- 实测值：没问题` 都曾放行）。
            #   注意：这不要求句式，只要求信息 —— 任何写法都行，只要有真东西。
            if self.K.get("require_substance", True):
                vals = {f: self._field_value(body, f) for f in sec["fields"]}
                empty = [f for f, v in vals.items() if self._is_placeholder(v)]
                body_verifiable = self._has_verifiable(body)
                if empty and not body_verifiable:
                    rows.append({"id": sec["id"], "root": sec["root"], "ok": False,
                                 "state": "空壳",
                                 "detail": (f"`{title}` 的 {'、'.join(empty)} 是空壳/套话，"
                                            f"且本栏无任何可核对物（数字/路径/哈希/命令/帧号任缺）"
                                            f" —— {sec['why']}"),
                                 "fields_found": found, "verifiable": []})
                    missing.append(sec)
                    continue

            rows.append({"id": sec["id"], "root": sec["root"], "ok": True,
                         "state": "完整",
                         "detail": f"`{title}` 含 {'、'.join(found)}"
                                   + (f"（可核对物：{'、'.join(self._has_verifiable(body))}）"
                                      if self._has_verifiable(body) else ""),
                         "fields_found": found,
                         "verifiable": self._has_verifiable(body)})

        total, miss = len(need), len(missing)
        ratio = miss / total if total else 0.0
        # ★★ 2026-10-04 修（死代码 · 比例阈值不可达）：
        #   原文只有 `ok = ratio <= max_skip_ratio(0.30)` 一条判据。
        #   但上面 `core_sections` 兜底会把 S3+S5 **强制并入** need ⇒
        #   成果类文档一旦缺栏，`miss` 至少为 2（S3、S5 都没写），而 `total` 最大 7
        #   ⇒ 可达的 ratio 只有 {0} ∪ [0.286(需 total=7 且 miss=2 … 实测落点 ≥0.5), 1.0]。
        #   实测：一份"只写一栏"的报告 need=1 miss=1 ratio=1.0；
        #   一份"全犯"的报告 need=3 miss=3 ratio=1.0。
        #   ⇒ **`0 < ratio <= 0.30` 这段"给短报告留口子"的分支永远走不到**：
        #      注释承诺的"允许最多 30% 缺失"是一个**恒假的宽松条款**，
        #      实际行为是"缺任何一栏就拦"（比注释严格得多）——注释与代码不是一回事。
        #   修法（治本，不是把阈值调大）：
        #     ① 阈值语义改为**同时**受两道判据约束：比例 + 绝对缺失数上限
        #        （比例对短报告失真：3 栏缺 1 栏 = 33% 就被拦，而 7 栏缺 1 栏 = 14% 放行，
        #         同样是"缺一栏"，后果却不同 ⇒ 这本身就是"判据量错对象"）；
        #     ② 核心栏（S3 回读证据 / S5 扫描范围）**不参与比例豁免** ——
        #        它们是"有没有交代清楚"的底线，缺了就该拦，不许被 30% 吞掉。
        core_ids = set(self.K.get("core_sections", ["S3", "S5"]) if genre_report else [])
        core_missing = [m for m in missing if m["id"] in core_ids]
        soft_missing = [m for m in missing if m["id"] not in core_ids]
        max_soft = self.K.get("max_skip_sections")
        if not isinstance(max_soft, int):
            # 由比例换算成"允许缺几栏"，至少 0：短报告不再被比例误伤
            max_soft = max(0, int(float(self.K["max_skip_ratio"]) * max(1, total)))
        ok = (not core_missing) and len(soft_missing) <= max_soft
        rep = {
            "gate": self.NAME, "ok": ok, "need": total, "missing": miss,
            "ratio": round(ratio, 3), "rows": rows,
            "core_missing": [m["id"] for m in core_missing],
            "soft_missing": [m["id"] for m in soft_missing],
            "max_soft_missing": max_soft,
            "sections_found": list(sections.keys())[:40],
            "summary": (f"结构声明闸：需要 {total} 栏，缺 {miss} 栏（{ratio:.0%}）"
                        + ("" if ok else "；缺：" + "、".join(
                            f"{r['id']}({r['state']})" for r in rows if not r["ok"]))),
        }
        self._log({**rep, "rows": None})
        return rep

    def check_file(self, p: str | Path, **kw) -> dict:
        p = Path(p)
        rep = self.check_text(p.read_text(encoding="utf-8", errors="replace"),
                              filename=p.name, **kw)
        rep["file"] = str(p)
        return rep

    def require(self, text: str, **kw) -> dict:
        rep = self.check_text(text, **kw)
        if not rep["ok"]:
            raise StructGateFailed(rep["summary"] + "\n缺失明细：\n" +
                                   "\n".join("  · " + r["detail"] for r in rep["rows"] if not r["ok"]))
        return rep

    def _log(self, rec: dict) -> None:
        try:
            with self.ledger.open("a", encoding="utf-8") as f:
                f.write(json.dumps({**rec, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                   ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            # ★★ 2026-10-04 修（静默吞异常）：原为 `pass` ⇒ 台账写失败无人知晓，
            #   于是"闸门跑过了"这件事没有任何留痕，事后无法复核。
            #   修法：打到 stderr（不抛，避免阻主流程，但绝不无声）。
            print(f"[g_struct] ⚠ 台账写入失败（判定仍有效，但未留痕）："
                  f"{type(e).__name__}: {e} → {self.ledger}", file=sys.stderr)

    # ---------------- 生成模板（治本的另一半：给人一个照着写的壳） ----------------
    def template(self, *, only: list[str] | None = None) -> str:
        """输出一份**照抄就能过闸**的报告模板。

        治本的关键：光有闸不够，还得让人知道"应该长什么样"。
        """
        out = ["# 报告", ""]
        for sec in SECTIONS:
            if only and sec["id"] not in only:
                continue
            out.append(f"## {sec['names'][0]}")
            for f in sec["fields"]:
                out.append(f"- {f}：")
            out.append(f"<!-- 治：{sec['root']} —— {sec['why']} -->")
            out.append("")
        return "\n".join(out)


class StructGateFailed(RuntimeError):
    """结构声明闸未通过 —— 报告缺必需栏目，不许交付。"""


# --------------------------------------------------------------------------
def demo(cfg: ProjectConfig) -> int:
    g = StructGate(cfg)
    fails = 0
    print("### 结构声明闸 · 对比测试（治「换词绕过」）\n")

    # ── 核心对照：同一份内容，一份用"标准词"、一份用"同义词"
    print("### 对照 1：g_root 被同义词绕过，g_struct 能否拦住？")
    # 这份报告"内容上有问题"（改了东西但没回读），但刻意不用 g_root 的词表
    sneaky = """# 工作报告

## 处理记录
所有对象的可见性都处理完毕了，存档也成功了。

## 说明
涉及 27 处改动，都已经生效。
"""
    from gates.g_root import RootGate  # noqa: E402
    rg = RootGate(cfg)
    r1 = rg.check_text(sneaky, kind="report")
    s1 = g.check_text(sneaky)
    print(f"  内容：改了 27 处但没回读，且刻意避开 g_root 的词表")
    print(f"  g_root   ：{'放行（被骗过）' if r1['ok'] else '拦下'}")
    print(f"  g_struct ：{'放行' if s1['ok'] else '拦下'}")
    if not s1["ok"]:
        for r in s1["rows"]:
            if not r["ok"]:
                print(f"       → {r['detail']}")
    else:
        print("  ✗✗ g_struct 也没拦住"); fails += 1
    print()

    # ── 对照 2：结构完整（用任何词都行）应放行
    print("### 对照 2：结构完整（措辞随意）应放行")
    complete = """# 工作报告

## 判据
- 要保证的量：可见性开关真的落盘了

## 出处
- md5 abc123（实测于 2026-10-03 12:40）

## 回读证据
- 实测值：reopen 后 27 个对象全为 True

## 量具口径
- 算法：逐对象读 hide_render
- 文件集：S17.blend

## 扫描范围
- 范围：全部 40 个 .blend

## 同类扫描
- 命中：3 处，其余 2 处已改净

## 证据分离
- 我实测的：27/27 为 True
"""
    s2 = g.check_text(complete)
    if s2["ok"]:
        print(f"  ✓ 放行（{s2['need']} 栏齐备）")
    else:
        print(f"  ✗ 误杀：{s2['summary']}"); fails += 1
    print()

    # ── 对照 3：把内容塞进代码块伪装（g_struct 不认伪标题）
    print("### 对照 3：把栏目塞进代码块（伪标题）应仍判缺栏")
    fake = """# 报告

```
## 回读证据
- 实测值：True
```

## 处理记录
已改 27 处。
"""
    s3 = g.check_text(fake)
    if not s3["ok"]:
        print(f"  ✓ 拦下：{s3['summary'][:80]}")
    else:
        print("  ✗✗ 被伪标题骗过"); fails += 1
    print()

    print("### 附：照抄就能过闸的模板")
    print("-" * 60)
    print(g.template(only=["S3", "S5"]))
    print("-" * 60)
    print(f"\n### 自检结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}")
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

    ap = argparse.ArgumentParser(prog="g_struct")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--text-file")
    ap.add_argument("--template", action="store_true", help="输出照抄就能过闸的报告模板")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    g = StructGate(cfg)
    if a.demo:
        sys.exit(demo(cfg))
    if a.template:
        print(g.template())
        sys.exit(0)
    if a.text_file:
        rep = g.check_file(a.text_file)
        if a.json:
            print(json.dumps(rep, ensure_ascii=False, indent=1))
        else:
            print(rep["summary"])
            for r in rep["rows"]:
                print(("  ✓ " if r["ok"] else "  ✗ ") + f"{r['id']} {r['root']}：{r['detail']}")
        sys.exit(0 if rep["ok"] else 1)
    print("用法：--demo / --template / --text-file <报告>")
