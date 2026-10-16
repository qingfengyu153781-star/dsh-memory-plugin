# -*- coding: utf-8 -*-
"""_guard · 口径闸 g_spec —— 标准不是"要读的文件"，是"物体必须满足的谓词"

治的条目：P1-03 P1-14 P2-06 P4-04 P5-02 P5-16 P67-04（7 条）
其中最关键的反证：助手**读了《搭建单_S01》之后照样**建出 4 条带、0.9 m 悬空的路
                  ⇒ "读"这个动作对结果没有约束力。

根除做法：把标准里的每条规格写成**可执行断言**，建完自动跑；
          任一不过 ⇒ 拒绝存盘 / 拒绝渲染 / 拒绝报完成。
          "不满足标准的对象根本进不了交付。"

跨项目：断言表来自 config.assertions，本文件不含任何项目专有量。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402


class SpecViolation(RuntimeError):
    """断言不过 —— 拒绝存盘/渲染/交付。"""


def _in_range(value, spec) -> tuple[bool, str]:
    """spec 支持三类写法，返回 (通过?, 说明)

      [lo, hi]              区间（含端点）
      {"max": x}            上界（含）—— 治"公差/不得超过"这类规格
      {"min": x}            下界（含）
      {"abs_max": x}        绝对值上界 —— 治"|gap| ≤ x"这类对称公差
      True / 数值 / 字符串   精确相等
    """
    if isinstance(spec, dict):
        if not isinstance(value, (int, float)):
            return False, f"期望数值公差 {spec}，实得非数值 {value!r}"
        checks = []
        if "abs_max" in spec:
            checks.append((abs(value) <= spec["abs_max"], f"|实得| ≤ {spec['abs_max']}（实得 |{value}|）"))
        if "max" in spec:
            checks.append((value <= spec["max"], f"≤ {spec['max']}（实得 {value}）"))
        if "min" in spec:
            checks.append((value >= spec["min"], f"≥ {spec['min']}（实得 {value}）"))
        if not checks:
            return False, f"无法识别的公差规格 {spec!r}"
        ok = all(c[0] for c in checks)
        return ok, "；".join(c[1] for c in checks)
    if isinstance(spec, list) and len(spec) == 2 and all(isinstance(x, (int, float)) for x in spec):
        lo, hi = spec
        if not isinstance(value, (int, float)):
            return False, f"期望数值区间 [{lo}, {hi}]，实得非数值 {value!r}"
        ok = lo <= value <= hi
        return ok, f"期望 [{lo}, {hi}]，实得 {value}"
    if isinstance(spec, bool):
        return (bool(value) == spec), f"期望 {spec}，实得 {value}"
    if isinstance(spec, (int, float, str)):
        ok = (value == spec)
        return ok, f"期望 {spec!r}，实得 {value!r}"
    return False, f"无法识别的断言规格 {spec!r}"


class SpecGate:
    """口径闸。实际取数由调用方传入（Blender 侧量测），本类只做断言与拒绝。"""

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        g = cfg.get("spec") or {}
        self.log_path = cfg.resolve(g.get("ledger", "_guard_ledger/spec.jsonl"))
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def check_sources(self) -> dict:
        """装载即检：标准文件是否都在（治"以为读了其实没有"）。"""
        missing = self.cfg.missing_spec_sources()
        return {"ok": not missing, "missing": missing, "checked": len(self.cfg.spec_sources)}

    def check(self, measured: dict, stage: str = "存盘前", *, allow_empty: bool | None = None) -> dict:
        """measured: {"山道.宽度_m": 1.35, ...}  ← 由 Blender 侧实测传入

        ★★ 2026-10-04 修（恒真分支 · 判据量错对象）：
          原文没有任何"断言表为空"的处理 ⇒ 通用盘 `_default.yaml` 的 `assertions: {}`
          会让本闸 **total=0 / ok=True / failed=0** —— 实测：
              g.check({"山道.宽度_m": 99.0})  ->  total=0  ok=True
              g.check({})                     ->  total=0  ok=True
          **任何东西都过**，包括明显超标的 99 米宽山道。
          这与本闸档头承诺的"不满足标准的对象根本进不了交付"**直接矛盾**：
          它量的是"我跑了几条断言"（过程量），不是"对象符不符合标准"（目标量）。
          ⇒ 空断言表 = **无断言可判**，不许当成"全过"。
          修法：默认把"断言表为空"判为**不过**（并在 detail 里说清怎么修），
                确需放行（如纯文章/非建模产出）必须**显式**传 `allow_empty=True`。
        """
        if not self.cfg.assertions:
            empty_ok = bool(self.K_empty_allowed() if allow_empty is None else allow_empty)
            out = {"stage": stage, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "total": 0, "failed": 0, "ok": empty_ok, "results": [],
                   "empty_assertions": True,
                   "detail": ("断言表为空 —— 没有任何标准可判" if not empty_ok
                              else "断言表为空（已显式放行 allow_empty=True）")}
            self._log(out)
            return out

        results = []
        for key, spec in self.cfg.assertions.items():
            if key not in measured:
                results.append({"key": key, "status": "missing",
                                "detail": "未提供实测值 —— 断言无法通过（不许默认绿）", "spec": spec})
                continue
            ok, detail = _in_range(measured[key], spec)
            results.append({"key": key, "status": "pass" if ok else "fail",
                            "detail": detail, "spec": spec, "measured": measured[key]})
        failed = [r for r in results if r["status"] != "pass"]
        out = {"stage": stage, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "total": len(results), "failed": len(failed),
               "ok": not failed, "results": results, "empty_assertions": False}
        self._log(out)
        return out

    def K_empty_allowed(self) -> bool:
        """配置里显式允许"无断言"时才算允许（默认不允许）。"""
        return bool((self.cfg.get("spec") or {}).get("allow_empty_assertions", False))

    def enforce(self, measured: dict, stage: str = "存盘前", *, allow_empty: bool | None = None) -> dict:
        """跑断言；不过则抛异常（＝拒绝存盘/渲染/交付）。"""
        out = self.check(measured, stage, allow_empty=allow_empty)
        if out.get("empty_assertions") and not out["ok"]:
            raise SpecViolation(
                f"[{stage}] 口径闸未通过：**断言表为空**（total=0）。\n"
                f"   无断言 ≠ 全过 —— 那是『没有任何标准在跑』，不是『符合标准』。\n"
                f"   ⇒ 请在项目 yaml 的 `assertions:` 下登记可执行断言；\n"
                f"      若这份产出确实不需要口径断言（如纯文章），请显式传 allow_empty=True。"
            )
        if not out["ok"]:
            raise SpecViolation(
                f"[{stage}] 口径断言未通过 {out['failed']}/{out['total']} 条：\n"
                + "\n".join(f"   ✗ {r['key']}: {r['detail']}"
                            for r in out["results"] if r["status"] != "pass")
                + "\n⇒ 拒绝继续（存盘/渲染/交付一律不做）。"
            )
        return out

    def render(self, out: dict) -> str:
        if out.get("empty_assertions"):
            return f"口径闸 [{out['stage']}]  断言表为空 —— {'放行（已显式允许）' if out['ok'] else '不过（无断言可判）'}"
        L = [f"口径闸 [{out['stage']}]  {out['total']-out['failed']}/{out['total']} 通过"]
        for r in out["results"]:
            mark = {"pass": "✓", "fail": "✗", "missing": "?"}[r["status"]]
            L.append(f"  {mark} {r['key']}: {r['detail']}")
        return "\n".join(L)

    def _log(self, rec: dict) -> None:
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def demo(cfg: ProjectConfig) -> int:
    # ★ 2026-10-03 修：原自检直接吃 cfg.assertions，而通用盘 `_default.yaml` 里
    #   `assertions: {}` 是**空的**（注释写着"由项目 yaml 填"）⇒ 三个负例全是
    #   项目专有断言（山道/石磴/人物占画高）却无断言可查 ⇒ **全放行、自检必挂**
    #   （实测输出"检查 0 份标准 / 0/0 通过"）。
    #   这与 g_obj 是**同一类病**：自检依赖外部配置有没有填，
    #   后果是"闸门坏了"与"配置没填"分不开 —— **假警报与漏检同源**。
    #   修法：自检自注入一份断言（形态取自项目配置，但独立成立）。
    cfg_demo = cfg
    if not (cfg.get("assertions") or {}):
        import dataclasses as _dc
        raw2 = dict(cfg.raw or {})
        raw2["assertions"] = {
            "山道.宽度_m": [1.30, 1.40],
            "山道.横断面带数": 1,
            "石磴.阶高_m": [0.14, 0.16],
            "石磴.踏面_m": [0.64, 0.66],
            "石磴.每级四角_gap_m": {"abs_max": 0.02},
            "路_地形_高差_m": {"abs_max": 0.05},
            "人物占画高": [0.0, 0.25],
        }
        cfg_demo = _dc.replace(cfg, raw=raw2)
        print("  （通用盘 assertions 为空 ⇒ 自检自注入断言，仅用于本演示）\n")
    g = SpecGate(cfg_demo)
    fails = 0

    print("### 装载自检：标准文件是否都在")
    src = g.check_sources()
    print(f"  检查 {src['checked']} 份标准 → {'全在' if src['ok'] else '缺：' + str(src['missing'])}")
    print()

    print("### 负例 1：S01-1 实测过的错——路做成 4 条带（'刨坑'）")
    try:
        g.enforce({"山道.宽度_m": 1.35, "山道.横断面带数": 4,
                   "路_地形_高差_m": 0.02, "人物占画高": 0.22}, stage="存盘前")
        print("  ✗✗ 没拦住"); fails += 1
    except SpecViolation as e:
        print("  ✓✓ 拦截成功：")
        for line in str(e).splitlines()[1:4]:
            print("     " + line)
    print()

    print("### 负例 2：S01-1 实测过的错——路比地形悬 0.9 m")
    try:
        g.enforce({"山道.宽度_m": 1.35, "山道.横断面带数": 1,
                   "路_地形_高差_m": 0.90, "人物占画高": 0.22}, stage="存盘前")
        print("  ✗✗ 没拦住"); fails += 1
    except SpecViolation as e:
        print("  ✓✓ 拦截成功：")
        for line in str(e).splitlines()[1:3]:
            print("     " + line)
    print()

    print("### 负例 3：缺实测值就想蒙混过关（不许默认绿）")
    try:
        g.enforce({"山道.宽度_m": 1.35}, stage="存盘前")
        print("  ✗✗ 没拦住"); fails += 1
    except SpecViolation as e:
        n = str(e).count("未提供实测值")
        print(f"  ✓✓ 拦截成功：{n} 条断言因缺实测值被判不通过（不许默认绿）")
    print()

    print("### 正例：合规的一版（闸门不该误杀）")
    try:
        out = g.enforce({"山道.宽度_m": 1.35, "山道.横断面带数": 1,
                         "石磴.阶高_m": 0.15, "石磴.踏面_m": 0.65,
                         "石磴.每级四角_gap_m": 0.015,
                         "路_地形_高差_m": 0.02, "人物占画高": 0.22}, stage="存盘前")
        print("  ✓ 通过：" + g.render(out).splitlines()[0])
    except SpecViolation as e:
        print("  ✗ 误杀：" + str(e)[:150]); fails += 1
    print()

    # ★★ 2026-10-04 新增（治"total=0 恒过"这个恒真分支）：
    #   通用盘 `_default.yaml` 的 `assertions: {}` 是空的 ⇒ 旧码 total=0 / ok=True，
    #   实测连 `{"山道.宽度_m": 99.0}`（宽 99 米的山道）都放行。
    #   本节用**空断言表**做负例：无断言 ≠ 全过。
    print("### 负例 4：断言表为空 ⇒ 不许当成『全过』（无断言≠符合标准）")
    import dataclasses as _dc2
    cfg_empty = _dc2.replace(cfg, raw={**(cfg.raw or {}), "assertions": {}})
    g_empty = SpecGate(cfg_empty)
    try:
        g_empty.enforce({"山道.宽度_m": 99.0}, stage="存盘前")
        print("  ✗✗ 没拦住 —— 空断言表把 99 米宽的山道放行了"); fails += 1
    except SpecViolation as e:
        print("  ✓✓ 拦截成功：" + str(e).splitlines()[1].strip())
    print()

    print("### 正例 5：确实不需要断言的产出（显式 allow_empty=True）不该被拦")
    try:
        out = g_empty.enforce({"任意": 1}, stage="存盘前", allow_empty=True)
        print("  ✓ 放行：" + g_empty.render(out))
    except SpecViolation as e:
        print("  ✗ 误杀：" + str(e)[:150]); fails += 1
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

    ap = argparse.ArgumentParser(prog="g_spec")
    ap.add_argument("--project", help="可省：默认从当前目录自动识别")
    ap.add_argument("--project-root", help="可省：默认从当前目录自动识别")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--sources", action="store_true", help="只做标准文件装载自检")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    if a.demo:
        sys.exit(demo(cfg))
    if a.sources:
        r = SpecGate(cfg).check_sources()
        print(f"标准 {r['checked']} 份 → {'全在 ✓' if r['ok'] else '缺失 ' + str(r['missing'])}")
        sys.exit(0 if r["ok"] else 2)
    print("用法：--demo 跑负例自检 / --sources 检查标准文件")
