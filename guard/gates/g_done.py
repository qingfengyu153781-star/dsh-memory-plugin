# -*- coding: utf-8 -*-
"""_guard · 完成闸 g_done —— "完成"是一条命令的输出，不是一个词

治的条目：P1-02 P1-20 P1-23 P2-03 P2-21 P4-10 P5-18 P67-30 P67-33
          ＋ 所有"自称看过其实没看"的（隐性全部）

病根：`see=false` 也能报"完成" —— 因为"完成"没有定义，所以它可以被口头宣布。
      用户三次质问「…」。

根除做法：把"完成"做成一条命令。它逐项检查，任一不过即**拒绝**：
  · 接触表存在且每格 ≥ 480x270（＝真的看了图）
  · spec 断言全绿
  · probe 自证记录存在（含对照）
  · 关键读数齐全（人物占画高 / 件在框率 / 帧数 / 时长）
只有全部通过，才输出那一行 `DELIVERED ...`。
**"完成"此后只指这一行**；没有这行，就是没完成。

跨项目：检查项来自 config.done_checks；本文件不含项目专有内容。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402


class NotDone(RuntimeError):
    """未通过完成闸 —— 不许说'完成/可以了/交付'。"""


class DoneGate:
    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        g = cfg.get("done") or {}
        self.ledger = cfg.resolve(g.get("ledger", "_guard_ledger/done.jsonl"))
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.min_cell = int(g.get("min_table_cell", 480))
        self.min_frames = int(g.get("min_table_frames", 5))

    # ---- 各项检查 ----
    def check_table(self, table_png: Path | None, cell_w: int | None, cell_h: int | None,
                    n_frames: int | None) -> dict:
        """接触表（＝"真的看了图"的物证）。"""
        if table_png is None:
            return {"name": "接触表", "ok": False,
                    "detail": "未提供接触表 —— 无物证说明你看过图（禁止只报数字）"}
        if not Path(table_png).exists():
            return {"name": "接触表", "ok": False, "detail": f"接触表不存在：{table_png}"}
        if n_frames is None or n_frames < self.min_frames:
            return {"name": "接触表", "ok": False,
                    "detail": f"抽帧数 {n_frames} < 下限 {self.min_frames}"}
        if cell_w is None or cell_h is None:
            return {"name": "接触表", "ok": False, "detail": "未提供每格像素尺寸"}
        if cell_w < self.min_cell or cell_h < int(self.min_cell * 9 / 16):
            return {"name": "接触表", "ok": False,
                    "detail": f"每格 {cell_w}x{cell_h} 小于下限（长边≥{self.min_cell}，16:9）"}
        return {"name": "接触表", "ok": True,
                "detail": f"{Path(table_png).name}：{n_frames} 帧，每格 {cell_w}x{cell_h}"}

    def check_spec(self, spec_ok: bool | None, spec_detail: str = "") -> dict:
        if spec_ok is None:
            return {"name": "口径断言", "ok": False, "detail": "未跑口径闸 —— 不许跳过"}
        return {"name": "口径断言", "ok": bool(spec_ok), "detail": spec_detail or ("全绿" if spec_ok else "有不过项")}

    def check_probe(self, probe_records: list[dict]) -> dict:
        if not probe_records:
            return {"name": "探针自证", "ok": False, "detail": "无探针自证记录 —— 判据可信性未证明"}
        bad = [r for r in probe_records if r.get("verdict") != "reliable"]
        if bad:
            return {"name": "探针自证", "ok": False,
                    "detail": f"{len(bad)} 条探针不可信：" + ", ".join(r.get("probe", "?") for r in bad)}
        return {"name": "探针自证", "ok": True, "detail": f"{len(probe_records)} 条探针均自证通过"}

    def check_readings(self, readings: dict | None) -> dict:
        required = ["人物占画高", "件在框率", "帧数", "时长"]
        if not readings:
            return {"name": "关键读数", "ok": False, "detail": f"未提供读数（需 {'/'.join(required)}）"}
        miss = [k for k in required if k not in readings]
        if miss:
            return {"name": "关键读数", "ok": False, "detail": "缺：" + ", ".join(miss)}
        return {"name": "关键读数", "ok": True,
                "detail": " ".join(f"{k}={readings[k]}" for k in required)}

    # ---- 合理性一问（2026-10-04 重写，原文有恒真分支） ----
    # ★★ 硬缺陷（实测坐实）：原判据
    #     ok = ("不会" in a or "不荒唐" in a) and "荒唐" not in a.replace("会不会觉得荒唐", "")
    #   实测 5 种回答**全部放行**，包括：
    #     '会。这个画面很荒唐，人穿进地板了。'   -> 放行   （明确说荒唐！）
    #     '荒唐。'                               -> 放行   （就俩字）
    #   根因：**把"回答里出现了『荒唐』二字"当成了"结论是荒唐"** ——
    #   而按题意，回答里几乎必然出现"荒唐"（题干就含这词），于是 `"荒唐" not in a` 恒假，
    #   整个合取式**恒假却被当成恒真用**（`ok` 永远走 True 分支）。
    #   这正是本闸要治的病根 1「判据量错对象」：它量的是"有没有这个词"，不是"结论是什么"。
    #   修法：**先判极性，再判是否自曝问题**，且不允许"否定+但是+有问题"这种自相矛盾蒙混。
    ABSURD_NEG = [
        r"不会觉得荒唐", r"不会荒唐", r"不荒唐", r"没有荒唐", r"无荒唐",
        r"不显荒唐", r"谈不上荒唐", r"不算荒唐",
        # ★ 省略式否定：题干问的就是"会不会觉得荒唐"，则"不会/没有/否"即等价于
        #   "不会觉得荒唐"。不许因为人家答得简洁就判不合格（那是判据不合理）。
        r"^不会[。．\.，,；;！!]?\s*$", r"^不会\b", r"^没有[。．\.]?\s*$",
        r"^否[。．\.]?\s*$", r"^不[。．\.]?\s*$",
    ]
    ABSURD_POS = [
        r"会觉得荒唐", r"会荒唐", r"确实荒唐", r"有点儿荒唐", r"有点荒唐",
        r"挺荒唐", r"很荒唐", r"非常荒唐", r"是荒唐的", r"荒唐的",
        r"^荒唐", r"[。；;，,]\s*荒唐",
    ]
    # 回答里自曝了问题 ⇒ 不许据此判"不荒唐"（自相矛盾即拦）
    ABSURD_SELFBAD = [
        r"穿模", r"穿进", r"穿透", r"陷进", r"悬空", r"漂浮", r"穿地", r"浮空",
        r"反物理", r"不合理", r"对不上", r"错位", r"比例失衡", r"不像",
    ]
    # ★★ 2026-10-04 修（否定语境误判 · 病根 6"改一处不扫同类"）：
    #   实测回归：正例 `不会。她走在山道上、垂手、背酒壶，比例正常，画面没有反物理的地方。`
    #   被拦，理由是"自曝问题（反物理）" —— 可 `没有反物理的地方` **恰恰是在说没问题**。
    #   根因：拿**关键词**当"自曝"，不看它处在肯定还是否定语境里。
    #   同一形态 g_behavior B2 已治过（2026-10-03：「不用两步出图」「拒绝分步出图」是在**遵守**规矩）。
    #   修法：复用其否定语境范式 —— 命中词前面若有否定词（**且中间没有转折词**），
    #         则该命中**不算自曝**。
    #   ★ 转折词必须断开语境："不会荒唐，不过人穿进地板了" 里 `不过` 之后的自曝
    #     是**真的自曝**（前面那个"不会"已被转折推翻）—— 实测踩过，故单独列出。
    SELF_IS_NOT_CONFESS = re.compile(
        r"(?:没有|没|无|不|未|非|不存在|看不到|看不出|谈不上|避免了|不会)"
        r"[^，。；！？\n]{0,6}$"
    )
    TURN_WORDS = re.compile(r"但是|不过|然而|可就是|问题是|只是|但(?![^，。]{0,3}(?:不|没|无))")

    @classmethod
    def _confessed(cls, pat: str, a: str) -> bool:
        """命中词是否处在**肯定语境**（=真的在自曝问题）。

        否定语境（"没有反物理的地方" / "无穿模" / "不存在错位"）⇒ 不算自曝。
        ★ 但**转折词会推翻前面的否定**："不会荒唐，不过人穿进地板了" ⇒ 算自曝。
        """
        for m in re.finditer(pat, a):
            head = a[:m.start()]
            if not cls.SELF_IS_NOT_CONFESS.search(head):
                return True
            # 否定语境成立还不够：若"否定词→本命中词"之间夹了转折词，否定已被推翻
            seg_start = max(0, m.start() - 12)
            seg = a[seg_start:m.start()]
            if cls.TURN_WORDS.search(seg):
                return True
        return False

    def check_absurd(self, absurd_answer: str | None) -> dict:
        """AGENTS.md 最高优先级铁律：「不懂技术的人看这个，会不会觉得荒唐？」

        ★★ 2026-10-04 二次修（治"过矫"）：第一版把"长度不够"当成"结论不明确"，
           实测误杀了两条**最标准的正答**：
             `不荒唐`                        -> 判"回答过短(3字)"
             `不会，比例正常，接触面也对`      -> 判"没有明确的『不会荒唐』结论"
           —— 这与原缺陷是**同一个病根**：判据量错对象（拿字面长度量"结论是否明确"）。
           正解：**长度只用于"既没肯定也没否定"的兜底**，
                 极性判定看**句首结论**（"不会，…"先给结论再给理由，不许因后半句否掉）。

        通过条件（三条全满足）：
          ① 给出了结论（极性可判——由否定式/肯定式命中，或句首"不会/否/合理"这类短答）
          ② 极性 = **否定**（不荒唐），不是"会/确实荒唐"
          ③ 没有自曝已知问题（"不会荒唐，不过人穿进地板了" ⇒ 自相矛盾即按荒唐处理）
        """
        if absurd_answer is None:
            return {"name": "合理性一问", "ok": False,
                    "detail": "未回答『一个不懂技术的人看这个，会不会觉得荒唐？』"}
        a = absurd_answer.strip()
        if not a:
            return {"name": "合理性一问", "ok": False, "detail": "回答为空"}

        head = a[:12]                      # 句首窗口：结论通常在最前面
        # ① 极性 —— 先看句首，再看全文（口语式"不会，比例正常…"句首即结论）
        neg_head = [p for p in self.ABSURD_NEG if re.search(p, head)]
        neg = neg_head or [p for p in self.ABSURD_NEG if re.search(p, a)]
        neg = [p for p in neg if not self._is_rhetorical(p, a)]
        # 肯定式：同样先句首后全文；排除被否定式前言覆盖的情形
        pos = [p for p in self.ABSURD_POS
               if re.search(p, a)
               and not re.search(r"不[会算]?\S*荒唐", a)
               and not re.match(r"^\s*(?:不会|没有|否|不|合理|没问题)", a)
               and not re.search(r"不[会算]?\S{0,4}(?:荒唐|荒唐的)", head)]
        selfbad = [p for p in self.ABSURD_SELFBAD if self._confessed(p, a)]

        if pos:
            return {"name": "合理性一问", "ok": False,
                    "detail": f"回答的结论是**荒唐**（命中肯定式 {pos[:2]}）⇒ 自己都判荒唐，不许交付"}
        if neg:
            if selfbad:
                return {"name": "合理性一问", "ok": False,
                        "detail": (f"一边判『不会荒唐』一边自曝问题（{'、'.join(selfbad[:3])}）"
                                   f"⇒ 自相矛盾即按荒唐处理：{a[:60]!r}")}
            return {"name": "合理性一问", "ok": True, "detail": f"（否证式）{a[:80]}"}
        # ② 兜底：既没肯定也没否定 —— 此时才用"长度/是否含结论词"判断
        if len(a) < 6:
            return {"name": "合理性一问", "ok": False,
                    "detail": f"回答过短且看不出结论（{len(a)} 字）：{a!r}"}
        if re.search(r"合理|没问题|正常|可以接受|符合现实|说得过去", a):
            # 这类是"肯定式正答"，但没有正面回答"会不会荒唐" ⇒ 只能算半个答案
            return {"name": "合理性一问", "ok": False,
                    "detail": f"只说了『{a[:40]}』，**没有正面回答会不会荒唐** —— "
                              f"请直接给结论（例：不会觉得荒唐，因为…）"}
        return {"name": "合理性一问", "ok": False,
                "detail": f"回答里没有可判定的结论：{a[:60]!r}"}

    @staticmethod
    def _is_rhetorical(pat: str, a: str) -> bool:
        """排除"反问式肯定"：如「难道不荒唐吗」「能不荒唐吗」其实是判荒唐。"""
        return bool(re.search(r"(?:难道|岂)[^，。；]{0,6}(?:不|没)", a))

    # ---- 主流程 ----
    def deliver(self, shot: str, *, md5: str, table_png=None, cell_w=None, cell_h=None,
                n_frames=None, spec_ok=None, spec_detail="", probe_records=None,
                readings=None, absurd_answer: str | None = None) -> dict:
        checks = [
            self.check_table(table_png, cell_w, cell_h, n_frames),
            self.check_spec(spec_ok, spec_detail),
            self.check_probe(probe_records or []),
            self.check_readings(readings),
        ]
        # 最高优先级那一问（AGENTS.md 铁律）：不懂技术的人看这个会不会荒唐
        checks.append(self.check_absurd(absurd_answer))

        failed = [c for c in checks if not c["ok"]]
        rec = {"shot": shot, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "checks": checks, "ok": not failed, "md5": md5}
        self._log(rec)

        if failed:
            raise NotDone(
                f"『{shot}』未通过完成闸（{len(failed)}/{len(checks)} 项不过）：\n"
                + "\n".join(f"   ✗ {c['name']}：{c['detail']}" for c in failed)
                + "\n⇒ 不许说'完成/可以了/交付'。"
            )
        line = f"DELIVERED {shot} md5={md5[:12]} frames={readings.get('帧数')} table={Path(table_png).name}"
        rec["line"] = line
        self._log({"kind": "delivered", "line": line, "shot": shot})
        return {"ok": True, "line": line, "checks": checks}

    def _log(self, rec: dict) -> None:
        with open(self.ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def demo(cfg: ProjectConfig) -> int:
    g = DoneGate(cfg)
    fails = 0
    good_table = Path(__file__).resolve().parent.parent / "ledger" / "_demo_table.png"
    good_table.write_bytes(b"\x89PNG\r\n\x1a\n" + b"demo")  # 造一个占位"接触表"

    good_probe = [{"probe": "路_地形_高差", "verdict": "reliable"},
                  {"probe": "站立面gap", "verdict": "reliable"}]
    good_readings = {"人物占画高": 0.22, "件在框率": 1.0, "帧数": 120, "时长": 5.0}
    ok_answer = "不会。她走在山道上、垂手、背酒壶，比例正常，画面没有反物理的地方。"

    print("### 负例 1：只报数字、没有接触表（等于『我没看图』）")
    try:
        g.deliver("S01-1", md5="a" * 32, spec_ok=True, probe_records=good_probe,
                  readings=good_readings, absurd_answer=ok_answer)
        print("  ✗✗ 没拦住"); fails += 1
    except NotDone as e:
        print("  ✓✓ 拦截成功：")
        for line in str(e).splitlines()[1:3]:
            print("     " + line)
    print()

    print("### 负例 2：接触表每格太小（192x108 < 480x270）")
    try:
        g.deliver("S01-1", md5="a" * 32, table_png=good_table, cell_w=192, cell_h=108, n_frames=5,
                  spec_ok=True, probe_records=good_probe, readings=good_readings, absurd_answer=ok_answer)
        print("  ✗✗ 没拦住"); fails += 1
    except NotDone as e:
        print("  ✓✓ 拦截成功：" + [l for l in str(e).splitlines() if "接触表" in l][0].strip())
    print()

    print("### 负例 3：口径断言有不过项，却想交付")
    try:
        g.deliver("S01-1", md5="a" * 32, table_png=good_table, cell_w=960, cell_h=540, n_frames=6,
                  spec_ok=False, spec_detail="山道.横断面带数=4（期望 1）",
                  probe_records=good_probe, readings=good_readings, absurd_answer=ok_answer)
        print("  ✗✗ 没拦住"); fails += 1
    except NotDone as e:
        print("  ✓✓ 拦截成功：" + [l for l in str(e).splitlines() if "口径" in l][0].strip())
    print()

    print("### 负例 4：没回答『会不会荒唐』")
    try:
        g.deliver("S01-1", md5="a" * 32, table_png=good_table, cell_w=960, cell_h=540, n_frames=6,
                  spec_ok=True, probe_records=good_probe, readings=good_readings)
        print("  ✗✗ 没拦住"); fails += 1
    except NotDone as e:
        print("  ✓✓ 拦截成功：" + [l for l in str(e).splitlines() if "合理性" in l][0].strip())
    print()

    # ★★ 2026-10-04 新增（治"只验证负例、把正例改死了"）：
    #   原判据 `("不会" in a or "不荒唐" in a) and "荒唐" not in a.replace(...)`
    #   实测 5 种回答**全部放行**（含"会。这个画面很荒唐"）——假绿。
    #   第一次修又把判据写宽成关键词匹配，**误杀正例**
    #   `不会。…画面没有反物理的地方。`（"没有反物理"恰恰是在说没问题）。
    #   ⇒ 本节同时钉住**正例必须放行 + 负例必须拦下**两条腿（g_probe 的判据本身就是这个）。
    print("### 合理性一问 · 双向对照（正例必须放行 / 负例必须拦下）")
    ABSURD_CASES = [
        ("不会。比例正常。", True, "简洁否定式正答（短 ≠ 不明确）"),
        ("不荒唐", True, "最直白的正答（曾被'长度<6'误杀）"),
        ("不会，比例正常，接触面也对", True, "口语式：先结论后理由（曾被'后半句'否掉）"),
        ("不会觉得荒唐", True, "标准正答"),
        ("不会。她走在山道上、垂手、背酒壶，比例正常，画面没有反物理的地方。",
         True, "★否定语境：'没有反物理的地方'是在说没问题（曾被当自曝误杀）"),
        ("会。这个画面很荒唐，人穿进地板了。", False, "肯定式判荒唐（改前放行）"),
        ("荒唐。", False, "两字判荒唐（改前放行）"),
        ("挺荒唐的，脚陷进地板 15cm", False, "判荒唐+自曝（改前放行）"),
        ("不会荒唐，不过人穿进地板了", False, "自相矛盾 ⇒ 按荒唐处理"),
        (None, False, "未回答"),
    ]
    for ans, should_pass, why in ABSURD_CASES:
        chk = g.check_absurd(ans)
        got = chk["ok"]
        if got != should_pass:
            print(f"  ✗✗ {'应放行' if should_pass else '应拦下'}却相反：{ans!r} —— {why}")
            fails += 1
        else:
            tail = f"  ← {why}" if not should_pass else ""
            print(f"  ✓ {'放行' if got else '拦下'}：{str(ans)[:34]!r}{tail}")
    print()

    print("### 正例：全项通过 → 输出那一行（唯一的'完成'定义）")
    try:
        out = g.deliver("S01-1", md5="46411bbb86e8" + "0" * 20, table_png=good_table, cell_w=960,
                        cell_h=540, n_frames=5, spec_ok=True, spec_detail="7/7 通过",
                        probe_records=good_probe, readings=good_readings, absurd_answer=ok_answer)
        print("  ✓ " + out["line"])
    except NotDone as e:
        print("  ✗ 误杀：" + str(e)[:150]); fails += 1
    print()
    print(f"### 自检结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    from guard import load as load_cfg

    ap = argparse.ArgumentParser(prog="g_done")
    ap.add_argument("--project", help="可省：默认从当前目录自动识别")
    ap.add_argument("--project-root", help="可省：默认从当前目录自动识别")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    if a.demo:
        sys.exit(demo(cfg))
    print("用法：--demo 跑负例自检")
