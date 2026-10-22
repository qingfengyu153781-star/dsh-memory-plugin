# -*- coding: utf-8 -*-
"""_guard · 决策过程闸 g_decision —— 核不了"方案好不好"，但核得了"决策站不站得住"

## 为什么有这道闸（2026-10-03 用户追问）

用户问：「…」

**我上一轮说"价值判断核不了" —— 那是偷懒的借口。**
"结论唯一正确"确实没有判据，但**决策过程有没有站住**，是可核的：

| 可核的 | 判据 |
|---|---|
| **有没有给过比选** | 只给一个方案就宣布"更好" ⇒ 没比过 |
| **比选判据是什么** | "感觉更好/更干净" ⇒ 没判据 |
| **判据可否复算** | 能不能拿数据说话（数字/阈值/对照） |
| **是否与已拒绝清单冲突** | 用户拒过的又被搬上来 ⇒ 违规（表里镜8 实证） |
| **是否问了该问的** | 有歧义没问就动手 ⇒ 违规（表里"我没让你修这个表"实证） |
| **假设有没有写明** | 把假设当事实用 ⇒ 病根 7 |

## 一句话
**核的不是"你选对了没有"，是"你有没有认真选"。**

## 用法
```python
from gates.g_decision import DecisionGate
dg = DecisionGate(cfg)
rep = dg.check_text(方案文本)
# rep["ok"] / rep["rows"]（逐项判据与理由）
```

跨项目：判据词表与已拒绝清单来自 config.decision；本文件不含项目专有内容。
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
    # ① 宣布结论的写法
    "conclusion_pats": [
        r"因此(?:我)?(?:决定|选择|采用|建议)", r"建议(?:采用|用|选)", r"我(?:决定|选择|采用)",
        r"最优(?:方案|解)", r"更好的(?:方案|做法)", r"结论[:：]", r"决定[:：]",
        r"方案\s*[A-Z甲乙丙一二三1-3](?:更|最好|胜出|可行)",
        r"采用方案", r"选用方案", r"选定方案", r"决定采用", r"建议采用",
        r"综上", r"故选", r"所以选", r"最终(?:选|采用|决定)",
        # ★★ 2026-10-15 形态判据（修"触发词过窄"的假根治；用户要求检查同类问题）★★
        #   病根：上面全是**字面动词**清单 ⇒ 换个说法就整段跳过。
        #   实测（对抗样本 `/tmp/adv_decision.md`）：
        #     「经过一番考虑，我们**打算用 B 方案**，它更省事。**最终就按 B 走**。」
        #     ⇒ 原清单一条都不匹配 ⇒ 报「无方案决策声明，跳过」，**exit 0 放行**。
        #   修法：按"**选择类动词 ＋ 选择对象**"的**形态**触发，不再穷举措辞。
        r"(?:倾向|打算|准备|拟用|拟采用|暂定|敲定|拍板|就用|改成|改用|决定|选择|选用|选定|采用)"
        r"[^\n。]{0,14}(?:方案|做法|路线|口径|版本|模型|库|工具|架构|设计)",
        r"(?:用|选|采用|按|走|定)\s*方案\s*[A-Z甲乙丙一二三四1-4]",
        r"最终\s*(?:就)?\s*(?:按|用|选|采用|走)",
        r"方案\s*[A-Z甲乙丙一二三四1-4][^\n。]{0,12}(?:更|最|胜出|可行|省|快|稳|好)",
    ],
    # ② 比选证据
    "compare_pats": [
        r"方案\s*[A-Z甲乙丙一二三1-3]", r"备选", r"候选", r"对比", r"权衡",
        r"取舍", r"vs\.?|VS", r"二选一", r"三选一", r"另一种",
    ],
    # ③ 判据证据（可复算的）
    "criteria_pats": [
        r"判据", r"标准[:：]", r"评价维度", r"评估维度", r"指标",
        r"优点|缺点|代价|收益", r"成本", r"风险",
    ],
    # ④ 可复算物（数字/阈值/对照）
    "numeric_pats": [r"\d", r"[≤≥<>]", r"对照", r"实测"],
    # ⑤ 已拒绝清单（用户明说拒过的）
    # ★ 2026-10-04（wiregates）：不再留空 —— 原为 `[]` ⇒ `hit_rej` 恒空 ⇒ **D4 恒 True**，
    #   永远打印「…」，**给用户"核过了"的假安心**（比 B2 报 None 更危险）。
    #   **单一真相源**：与 g_behavior 的 B2 共用同一份清单（同一件事只存一处，
    #   避免"改了一处、另一处还是空的"——这正是病根 6「不扫同类」）。
    #   下列条目逐条带记忆库 citation；**核不到出处的一律不填**。
    "rejected": [
        # 本机记录（序号已隐去）—— 用户 2026-09-12 明令废止分步出图
        "两步出图", "分步出图",
        # 本机记录（序号已隐去）（铁律 2）—— "搬了变冷约束"被骂两次
        "变冷", "变凉", "皮肤凉", "眼神失神", "冷蓝", "光线变冷", "动作变慢",
        # 本机记录（序号已隐去）（铁律 4）—— 原文点名"把已拒绝的『变冷』/『音色变』混进选项"
        "音色变",
    ],
    # ⑥ 该问未问：有歧义标记却没提问记录
    "ambiguity_pats": [
        r"可能(?:是|指)", r"也许", r"不清楚", r"不确定", r"有两种理解", r"我理解为",
        r"如果.{0,8}的话", r"假设",
    ],
    "asked_pats": [r"已(?:向|与)用户确认", r"用户(?:说|确认|选择|拍板)", r"经确认", r"澄清"],
    "max_missing": 1,   # 允许缺 1 项（给简报留口子）
}


def _cfg_of(cfg: ProjectConfig) -> dict:
    d = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    over = cfg.get("decision") or {}
    for k, v in over.items():
        if isinstance(v, list) and isinstance(d.get(k), list):
            d[k] = list(d[k]) + [x for x in v if x not in d[k]]
        else:
            d[k] = v
    return d


class DecisionGate:
    NAME = "g_decision"

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self.K = _cfg_of(cfg)
        lg = (cfg.get("decision") or {}).get("ledger", "_guard_ledger/decision.jsonl")
        self.ledger = cfg.resolve(lg)
        self.ledger.parent.mkdir(parents=True, exist_ok=True)

    def check_text(self, text: str, *, strict: bool = False) -> dict:
        K = self.K
        rows: list[dict] = []

        has_concl = bool(re.search("|".join(K["conclusion_pats"]), text))
        if not has_concl:
            rep = {"gate": self.NAME, "ok": True, "rows": [],
                   "summary": "决策过程闸：无方案决策声明，跳过"}
            self._log(rep)
            return rep

        # D1 有没有给过比选
        n_cmp = len(re.findall("|".join(K["compare_pats"]), text))
        rows.append({
            "id": "D1", "name": "给了比选",
            "ok": n_cmp >= 1,
            "detail": (f"提到 {n_cmp} 处备选/对比" if n_cmp >= 1 else
                       "**只有一个方案就宣布结论** —— 没比过，凭什么说它更好？"),
        })

        # D2 有没有判据
        n_cri = len(re.findall("|".join(K["criteria_pats"]), text))
        rows.append({
            "id": "D2", "name": "有判据",
            "ok": n_cri >= 1,
            "detail": (f"提到 {n_cri} 处判据/维度" if n_cri >= 1 else
                       "**没写判据** —— 『感觉更好/更干净』不是判据，无法复核"),
        })

        # D3 判据可否复算
        n_num = len(re.findall("|".join(K["numeric_pats"]), text))
        rows.append({
            "id": "D3", "name": "判据可复算",
            "ok": n_num >= 2,
            "detail": (f"含 {n_num} 处数字/阈值/对照" if n_num >= 2 else
                       f"**判据不可复算**（只有 {n_num} 处数字） —— "
                       "没有量化的判据，换个人会得出相反结论"),
        })

        # D4 是否撞已拒绝清单
        # ★ 2026-10-04（wiregates）通用判据（Lead 定，全闸适用）：
        #   **"无法核" 必须报 None，不许报 True；"核过了" 和 "没得核" 是两种结论。**
        #   原实现 `"ok": not hit_rej` 在清单为空时 `hit_rej == []` ⇒ 恒 True
        #   ⇒ 报告里显示「…」，**用户以为核过了，其实一次都没比**。
        rej_items = [r for r in K["rejected"] if r]
        hit_rej = [r for r in rej_items if r in text]
        if not rej_items:
            rows.append({
                "id": "D4", "name": "不撞已拒绝清单", "ok": None,
                "detail": "**无法核**：已拒绝清单为空 ⇒ 不是『未命中』，是『没得比』"
                          "（空清单报 True = 假安心）",
            })
        else:
            rows.append({
                "id": "D4", "name": "不撞已拒绝清单",
                "ok": not hit_rej,
                "detail": (f"未命中已拒绝项（清单 {len(rej_items)} 条）" if not hit_rej else
                           f"**搬回了用户已拒绝的**：{'、'.join(hit_rej[:3])} —— "
                           "表里镜8 实证：『我拒绝的东西再搬上来』是最严重的违规之一"),
            })

        # D5 有歧义是否问过
        n_amb = len(re.findall("|".join(K["ambiguity_pats"]), text))
        n_ask = len(re.findall("|".join(K["asked_pats"]), text))
        rows.append({
            "id": "D5", "name": "有歧义先问",
            "ok": (n_amb == 0) or (n_ask >= 1),
            "detail": (f"歧义 {n_amb} 处，确认 {n_ask} 处" if (n_amb == 0 or n_ask >= 1) else
                       f"**{n_amb} 处歧义但 0 处确认** —— 自己在替用户拍板"
                       "（表里实证：『我没让你修这个表』就是这么来的）"),
        })

        # D6 假设有没有写明
        has_assume = bool(re.search(r"假设|前提|若.{0,6}则|假定", text))
        has_concl2 = has_concl
        rows.append({
            "id": "D6", "name": "假设写明",
            "ok": has_assume or n_amb == 0,
            "detail": ("已写明假设/前提" if has_assume else
                       "未写明假设（若本方案无假设可忽略）"),
        })

        # ★ 2026-10-04（wiregates）：`not r["ok"]` 会把 `ok=None`（**无法核**）也当失败
        #   ⇒ 一旦 D4 诚实报 None，闸就会因"没得比"而拦人 —— 那是**误杀**。
        #   三条腿必须分清：False=不过（拦）；None=无法核（只提示，不拦）；True=过。
        miss = [r for r in rows if r["ok"] is False]
        unk = [r for r in rows if r["ok"] is None]
        # D6 是软项（无假设时允许缺）
        # ★ D1/D2/D3 是**硬项**（2026-10-03 修）：
        #   "没比选 / 没判据 / 判据不可复算" 正是病根 1「判据量错对象」的核心，
        #   若允许它们被 max_missing 的额度吸收，闸门就形同虚设
        #   （实测：'有判据但不可复算' 曾因额度=1 而放行）。
        HARD = {"D1", "D2", "D3"}
        hard_miss = [r for r in miss if r["id"] in HARD]
        soft_miss = [r for r in miss if r["id"] not in HARD and r["id"] != "D6"]
        ok = (len(hard_miss) == 0) and (len(soft_miss) <= int(K["max_missing"]))
        rep = {
            "gate": self.NAME, "ok": ok, "rows": rows,
            "missing": [r["id"] for r in hard_miss],
            "unverifiable": [r["id"] for r in unk],
            "summary": (f"决策过程闸：{len(rows)-len(miss)-len(unk)}/{len(rows)} 项站得住"
                        + ("" if not unk else
                           f"；{len(unk)} 项**无法核**（清单为空 ⇒ 不是核过了）："
                           + "、".join(r["id"] for r in unk))
                        + ("" if ok else "；不站的：" + "；".join(r["detail"] for r in hard_miss))),
        }
        self._log({**rep, "rows": None})
        return rep

    def check_file(self, p: str | Path, **kw) -> dict:
        p = Path(p)
        rep = self.check_text(p.read_text(encoding="utf-8", errors="replace"), **kw)
        rep["file"] = str(p)
        return rep

    def require(self, text: str, **kw) -> dict:
        rep = self.check_text(text, **kw)
        if not rep["ok"]:
            raise DecisionFailed(rep["summary"])
        return rep

    def _log(self, rec: dict) -> None:
        try:
            with self.ledger.open("a", encoding="utf-8") as f:
                f.write(json.dumps({**rec, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                   ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            # ★ D6 修复（2026-10-04）：不静默吞 —— 台账写失败必须可见。
            sys.stderr.write(
                f"[g_decision] 台账写入失败（不静默吞）：{self.ledger} :: "
                f"{type(e).__name__}: {e}\n")


class DecisionFailed(RuntimeError):
    """决策过程站不住 —— 不许据此宣布方案。"""


def demo(cfg: ProjectConfig) -> int:
    g = DecisionGate(cfg)
    fails = 0
    print("### 决策过程闸 · 负例自检（都是真实发生过的形态）\n")

    cases = [
        ("只给一个方案就宣布更好（无判据）",
         "我决定采用方案 A。它看起来更干净、更好维护。", True),
        ("有比选但没判据",
         "方案 A 与方案 B 对比后，我选择 A，感觉更合适。", True),
        ("有判据但不可复算",
         "方案 A 与 B 对比：A 的判据是更清晰，所以选 A。", True),
        ("搬回用户已拒绝的方案", None, True),   # 下面特判
        ("有歧义却没问",
         "用户说的『问题』可能是指文件缺陷，也可能是指内容问题。我决定按文件缺陷处理。", True),
    ]
    for name, text, should_block in cases:
        if text is None:
            rej = g.K["rejected"]
            if not rej:
                print("  （跳过『已拒绝清单』—— config 里没配）")
                continue
            text = f"我建议采用 {rej[0]}，它更简单。"
        r = g.check_text(text)
        hit = (not r["ok"])
        good = (hit == should_block)
        if not good:
            fails += 1
        print(f"  {'✓✓ 拦下' if hit else '—— 放行'}  {name}")
        for row in r["rows"]:
            if not row["ok"]:
                print(f"         {row['id']} {row['name']}：{row['detail'][:78]}")
    print()

    print("### 正例：认真的决策过程应放行")
    good = """# 方案比选

用户已确认目标是降低返工率（经确认）。

## 备选
- 方案 A：加结构闸
- 方案 B：加人工评审
- 方案 C：不加，只改文档

## 评价维度与判据
| 维度 | 判据 | A | B | C |
|---|---|---|---|---|
| 拦截率 | 对 14 种攻击的识破率 | 100% | 60% | 0% |
| 成本 | 实现工时 | 4 h | 12 h | 1 h |

## 假设
假设：错误主要发生在产出一侧（若上游理解错，闸门拦不住）。该假设已写入文档。

## 结论
采用方案 A：识破率 100% 对 0%，成本 4 h 低于 B。代价是不覆盖上游理解错误。
"""
    r2 = g.check_text(good)
    if r2["ok"]:
        print(f"  ✓ 放行（{r2['summary'][:70]}）")
    else:
        print(f"  ✗ 误杀：{r2['summary'][:150]}")
        fails += 1
    print()

    # ★ 2026-10-15 负例（防"触发词再被收窄"复发）：**换了说法的决策**
    #   旧版 `conclusion_pats` 全是字面动词（"因此我决定"/"结论："/"最终选"…），
    #   下面这句"打算用 B 方案 … 最终就按 B 走"一个字都不匹配 ⇒ 旧版报"无方案决策声明，跳过"、
    #   exit 0 **放行**。改成形态判据（选择类动词 + 选择对象）后必须拦下。
    reworded = "经过一番考虑，我们打算用 B 方案，它更省事。最终就按 B 走。"
    r3 = g.check_text(reworded)
    hit = r3["ok"] is False
    print(f"  {'✓✓拦下' if hit else '✗✗漏放'}  形态判据负例（换说法的决策）：{reworded[:26]}")
    if not hit:
        fails += 1
    #   正例（不许误杀）：**没做选择**的普通报告必须照旧跳过
    r4 = g.check_text("我读了 3 个文件，把现状记录如下。没有做任何取舍。")
    if r4["ok"] is True:
        print("  ✓ 不误杀：无选择动作的报告照旧跳过")
    else:
        print(f"  ✗ 误杀：{r4['summary'][:120]}")
        fails += 1
    print()

    print(f"### 自检结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[g_decision] stdout 重设为 utf-8 失败（继续跑）：{e}\n")
    from guard import load as load_cfg

    ap = argparse.ArgumentParser(prog="g_decision")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--text-file")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    g = DecisionGate(cfg)
    if a.demo:
        sys.exit(demo(cfg))
    if a.text_file:
        rep = g.check_file(a.text_file)
        if a.json:
            print(json.dumps(rep, ensure_ascii=False, indent=1))
        else:
            print(rep["summary"])
            for r in rep["rows"]:
                print(("  ✓ " if r["ok"] else "  ✗ ") + f"{r['id']} {r['name']}：{r['detail']}")
        sys.exit(0 if rep["ok"] else 1)
    print("用法：--demo / --text-file <方案>")
