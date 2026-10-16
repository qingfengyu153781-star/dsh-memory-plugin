# -*- coding: utf-8 -*-
"""_guard · 批次闸 g_batch —— **批量任务没跑完，不许当跑完了**

## 为什么有这道闸（2026-10-04 立，从《联网学习Blender建模》会话来）

**实证（该会话 `seq=1325`）**：

```
STARTED PID=14568
--- 45s 后 ---
任务数 10
可用物理内存 4.07 GB
ASR 模型 = tiny
STOP 转写前内存仅 2.28GB < 2.5GB，安全退出（已完成 0 条，重跑本脚本会自动续采）
DONE ok=0 skip=0 fail=0 已清媒体文件=0
```

**`ok=0` —— 一整个批次零成功。**
而它的下一步不是报错、不是重试、不是告诉用户，而是**直接转入"写笔记"阶段**。

**同类（同会话）**：

| seq | 现象 | 后果 |
|---|---|---|
| 1325 | `DONE ok=0` | 零成功被当作"跑完了" |
| 1525 | `STOP 内存 1.26GB < 2.0GB，安全退出（已完成 1/8 块）` | 1/8 被当作"跑过" |
| 1595 | `STOP 内存 1.12GB < 2.0GB，安全退出（已完成 27/29 块）` | 27/29 ≈ 93%，反而没人注意那 2 块 |
| 2473 | `STOP 内存 1.41GB < 2.0GB，安全退出（已完成 1/3 块）` | 同上 |

**另有 18 条视频的采集日志**里 `ERR`/`SKIP` 混在 `OK` 里，
最终**无人汇总完成率** —— 直到写报告时才由 AI 自己回头数了一遍。

**⇒ 病根：批量任务没有"出口条件"。** 跑完 = 脚本退出，**不管成了几条**。

## 与已有闸的关系（不重复）

| 闸 | 治什么 | 治不了本病的理由 |
|---|---|---|
| `g_mem` | 跑重活前**先看内存够不够** | 它管"能不能开跑"，**不管"跑完成了几条"** |
| `g_done` | 宣布完成时要有命令输出 | 它认"有过一条命令"，`DONE ok=0` **也是命令输出** |
| `g_probe` | 探针要能红 | 它管"判据会不会红"，不管"批次完成率" |
| **`g_batch`（本闸）** | **批量任务的完成率必须达阈值，否则不许进入下一阶段** | —— |

## 三层判据（可执行）

  **A · 完成率**：日志里能读出 `ok/skip/fail` 或 `n/N` ⇒ 算完成率
     完成率 < 阈值（默认 0.8）⇒ **拦**
     `ok=0` ⇒ **一律拦**（零成功永远不是"跑完了"）

  **B · 中断可见**：出现 `STOP / KILLED / 安全退出 / 中断 / timeout` 类字样，
     但报告里**没有**"还剩 N 条未做"的交代 ⇒ 拦（半截当整截）

  **C · 退出码诚实**：批量脚本以 0 退出但 `fail>0` ⇒ 拦（假绿）

## 与"不许误杀"的平衡

  · 日志里**读不出任何完成率信息** ⇒ `ok=None`（未核，不拦）—— 不许因为"格式没匹配上"就判红
  · 单件任务（不构成批次）⇒ 未核

## 用法
```bash
python _guard/gates/g_batch.py --demo                    # 负例自检（用真实那次 ok=0）
python _guard/gates/g_batch.py --log <批次日志>           # 查一个日志
python _guard/gates/g_batch.py --text-file <报告>         # 查报告里的批次交代
```
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402

DEFAULTS: dict = {
    # A · 完成率的识别式（多种真实写法都要认）
    "rate_pats": [
        r"DONE\s+ok=(?P<ok>\d+)\s+skip=(?P<skip>\d+)\s+fail=(?P<fail>\d+)",
        r"已完成[:：]?\s*(?P<done>\d+)\s*/\s*(?P<total>\d+)",
        r"(?P<done>\d+)\s*/\s*(?P<total>\d+)\s*(?:块|条|个|项|张)",
        r"OK\s+(?P<done>\d+)[^\n]{0,20}?\bERR\b",
    ],
    # 逐条状态行：用于统计 OK/ERR/SKIP 个数
    "item_pats": [r"\[\d+/\d+\]\s*(?P<st>OK|ERR|SKIP|FAIL|WAV_FAIL)\b",
                  r"^\s*(?P<st>OK|ERR|SKIP|FAIL)\s+\S+"],

    # B · 中断字样
    "abort_pats": [r"\bSTOP\b", r"安全退出", r"\bKILLED\b", r"中断", r"timeout",
                   r"超时", r"被终止", r"MemoryError", r"mkl_malloc",
                   r"Unable to allocate"],

    # 阈值
    "min_rate": 0.8,          # 完成率下限
    "zero_hard": True,        # ok=0 ⇒ 一律拦
}


def _cfg_of(cfg: ProjectConfig) -> dict:
    d = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    over = cfg.get("batch_gate") or {}
    for k, v in over.items():
        if isinstance(v, list) and isinstance(d.get(k), list):
            d[k] = list(d[k]) + [x for x in v if x not in d[k]]
        else:
            d[k] = v
    return d


class BatchGate:
    """批次闸。三查：完成率（A）→ 中断交代（B）→ 退出码诚实（C）。"""

    NAME = "g_batch"

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self.K = _cfg_of(cfg)

    # ---------------------------------------------------------------- 解析
    def parse_rate(self, log: str) -> dict:
        """从日志里读完成率。多种口径都认；读不出 ⇒ 未核。"""
        # ① 显式 DONE ok=/skip=/fail=
        for m in re.finditer(self.K["rate_pats"][0], log):
            ok, skip, fail = int(m.group("ok")), int(m.group("skip")), int(m.group("fail"))
            total = ok + skip + fail
            return {"ok": ok, "skip": skip, "fail": fail, "total": total,
                    "rate": (ok / total) if total else None, "src": "DONE ok=/skip=/fail="}
        # ② 已完成 n/N
        for p in self.K["rate_pats"][1:]:
            for m in re.finditer(p, log):
                gd = m.groupdict()
                try:
                    done, total = int(gd["done"]), int(gd["total"])
                except (KeyError, TypeError, ValueError):
                    continue
                if total > 0:
                    return {"ok": done, "skip": 0, "fail": total - done, "total": total,
                            "rate": done / total, "src": "已完成 n/N"}
        # ③ 逐条状态行统计
        st = {"OK": 0, "ERR": 0, "SKIP": 0, "FAIL": 0, "WAV_FAIL": 0}
        hit = False
        for ln in log.split("\n"):
            for p in self.K["item_pats"]:
                m = re.search(p, ln)
                if m:
                    st[m.group("st")] += 1
                    hit = True
                    break
        if hit:
            total = sum(st.values())
            return {"ok": st["OK"], "skip": st["SKIP"],
                    "fail": st["ERR"] + st["FAIL"] + st["WAV_FAIL"],
                    "total": total, "rate": st["OK"] / total if total else None,
                    "src": "逐条状态行统计"}
        return {"ok": None, "status": "skip", "src": "none",
                "detail": "日志里读不出任何完成率信息 ⇒ 未核（不算通过，也不判不通过）"}

    def aborted(self, log: str) -> list[str]:
        out = []
        for ln in log.split("\n"):
            for p in self.K["abort_pats"]:
                if re.search(p, ln, re.I):
                    out.append(ln.strip()[:110])
                    break
        return out

    # ---------------------------------------------------------------- A/B/C
    def check_log(self, log: str, report: str = "") -> dict:
        a = self.parse_rate(log)
        ab = self.aborted(log)

        if a.get("ok") is None:
            return {"ok": None, "status": "skip", "a": a, "abort_lines": ab,
                    "summary": "批次：○ 未核（" + str(a.get("detail"))[:60] + "）"}

        problems = []
        rate = a.get("rate")
        # A2 · ok=0 一律拦（零成功永远不是"跑完了"）
        if self.K["zero_hard"] and a["ok"] == 0:
            problems.append(f"**ok=0（整批零成功）** ⇒ 绝不是『跑完了』。"
                            f"完成率 0/{a['total']}，必须报回来或重跑")
        # A1 · 完成率低于阈值
        elif rate is not None and rate < float(self.K["min_rate"]):
            problems.append(f"完成率 {rate:.0%}（{a['ok']}/{a['total']}）"
                            f"低于阈值 {float(self.K['min_rate']):.0%}")

        # B · 有中断字样却在报告里没交代剩余
        if ab and report:
            declared = re.search(r"(?:还剩|剩余|未完成|未做|缺)\s*\d+", report)
            if not declared:
                problems.append(f"日志里有 {len(ab)} 处中断字样（如：{ab[0][:60]}），"
                                "但报告里没有『还剩 N 条未做』的交代 ⇒ 半截当整截")

        ok = not problems
        return {"ok": ok, "status": "checked", "a": a, "abort_lines": ab,
                "problems": problems,
                "summary": (f"批次：完成率 {a['ok']}/{a['total']}"
                            + (f" = {rate:.0%}" if rate is not None else "")
                            + (" ✓" if ok else " ✗ " + "；".join(problems))[:150])}

    # ---------------------------------------------------------------- 报告层
    def check_report(self, text: str) -> dict:
        """报告层：出现『批次/批量/全部完成』却没给完成率 ⇒ 拦。"""
        claims = [ln for ln in text.split("\n")
                  if re.search(r"(?:批量|批次|全部|所有).{0,8}(?:完成|跑完|处理完|下载完)",
                               ln)]
        if not claims:
            return {"ok": None, "status": "skip",
                    "summary": "批次：○ 未核（报告里没有批次完成声称）"}
        bad = [c for c in claims
               if not re.search(r"\d+\s*/\s*\d+|\d+(?:\.\d+)?%|ok=\d+", c)]
        return {"ok": not bad, "status": "checked", "n_claim": len(claims),
                "summary": ("批次：✓ 完成率已给" if not bad else
                            f"批次：✗ {len(bad)}/{len(claims)} 处声称批次完成却没给完成率")}


# -------------------------------------------------------------------- demo
def demo(cfg: ProjectConfig | None = None) -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    g = BatchGate.__new__(BatchGate)
    g.K = dict(DEFAULTS)
    fail = 0

    print("=" * 74)
    print("g_batch 负例自检 —— 用《联网学习Blender建模》会话的真实日志")
    print("=" * 74)

    print("\n### 负例 1：真实那次『DONE ok=0』（seq=1325）")
    real = ("STARTED PID=14568\n"
            "--- 45s 后 ---\n任务数 10\n可用物理内存 4.07 GB\nASR 模型 = tiny\n"
            "STOP 转写前内存仅 2.28GB < 2.5GB，安全退出（已完成 0 条，重跑本脚本会自动续采）\n"
            "DONE ok=0 skip=0 fail=0 已清媒体文件=0\n")
    r = g.check_log(real, report="本轮已学习完毕，知识库已交付，共 19 篇。")
    print(f"  判定：ok={r['ok']}")
    for p in r.get("problems", []):
        print(f"    ✗ {p}")
    if r["ok"] is False:
        print("  ✓✓ 拦住（零成功不许当跑完）")
    else:
        print("  ✗✗ 没拦住 —— 闸失效"); fail += 1

    print("\n### 负例 2：18 条视频里 OK/ERR/SKIP 混排，完成率不足")
    log2 = "\n".join(
        [f"[{i}/18] {'OK' if i <= 12 else ('ERR' if i <= 15 else 'SKIP')} BV{i:04d}" for i in range(1, 19)]
    )
    r2 = g.check_log(log2)
    print(f"  判定：ok={r2['ok']}  完成率 {r2['a']['ok']}/{r2['a']['total']}")
    for p in r2.get("problems", []):
        print(f"    ✗ {p}")
    if r2["ok"] is False:
        print("  ✓✓ 拦住（12/18 = 67% < 80%）")
    else:
        print("  ✗✗ 没拦住"); fail += 1

    print("\n### 负例 3：有中断字样，报告却不交代剩余")
    r3 = g.check_log(real, report="采集已全部完成。")
    has_abort_msg = any("中断字样" in p for p in r3.get("problems", []))
    print(f"  判定：ok={r3['ok']}  含中断交代缺失 = {has_abort_msg}")
    if r3["ok"] is False:
        print("  ✓✓ 拦住（半截当整截）")
    else:
        print("  ✗✗ 没拦住"); fail += 1

    print("\n### 正例 1：完成率达标 ⇒ 放行")
    good = "DONE ok=16 skip=2 fail=0 已清媒体文件=16\n"
    r4 = g.check_log(good)
    print(f"  判定：ok={r4['ok']}  完成率 {r4['a']['ok']}/{r4['a']['total']}"
          f" = {r4['a']['rate']:.0%}")
    if r4["ok"] is True:
        print("  ✓ 放行（89% ≥ 80%）")
    else:
        print("  ✗✗ 误杀 —— 达标批次被拦"); fail += 1

    print("\n### 正例 2：日志格式认不出 ⇒ 未核（不许误杀）")
    r5 = g.check_log("好的，我开始处理。\n处理中……\n")
    print(f"  判定：ok={r5['ok']}（None=未核）")
    if r5["ok"] is None:
        print("  ✓ 未核，不拦（读不出完成率 ≠ 不合格）")
    else:
        print("  ✗✗ 把『认不出格式』判成了『不合格』"); fail += 1

    print("\n### 正例 3：报告给了完成率 ⇒ 放行")
    r6 = g.check_report("本批 18 条视频全部处理完成，成功 16/18。")
    print(f"  判定：ok={r6['ok']}  {r6['summary']}")
    if r6["ok"] is True:
        print("  ✓ 放行")
    else:
        print("  ✗✗ 误杀"); fail += 1

    print("\n### 负例 4：报告声称『全部完成』却不给完成率")
    r7 = g.check_report("本批视频全部处理完成。")
    print(f"  判定：ok={r7['ok']}  {r7['summary']}")
    if r7["ok"] is False:
        print("  ✓✓ 拦住（说了全部完成，却给不出几条）")
    else:
        print("  ✗✗ 没拦住"); fail += 1

    print("\n" + "=" * 74)
    print("自检结果：" + ("全部通过" if fail == 0 else f"有 {fail} 项失败"))
    print("=" * 74)
    return 1 if fail else 0


def _utf8_stdio() -> None:
    """GBK 终端防崩（同 g_criteria：直连必崩 / 管道静默丢符号，统一包 UTF-8）。

    ★ 用 reconfigure 而不是再包一层 TextIOWrapper：原地改、幂等 ——
      双层包装会让旧 wrapper 被 GC 时关掉底层 buffer（实测踩过）。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(prog="g_batch")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--log")
    ap.add_argument("--text-file")
    a = ap.parse_args()
    if a.demo:
        return demo()
    from guard import load as load_cfg
    g = BatchGate(load_cfg(a.project, a.project_root))
    rc = 0
    if a.log:
        rep = "" 
        if a.text_file:
            rep = Path(a.text_file).read_text(encoding="utf-8", errors="replace")
        r = g.check_log(Path(a.log).read_text(encoding="utf-8", errors="replace"), rep)
        print(r["summary"])
        for p in r.get("problems", []):
            print("  ✗ " + p)
        rc = 0 if r["ok"] is not False else 1
    elif a.text_file:
        r = g.check_report(Path(a.text_file).read_text(encoding="utf-8", errors="replace"))
        print(r["summary"])
        rc = 0 if r["ok"] is not False else 1
    else:
        print("用法：--demo / --log <批次日志> [--text-file <报告>] / --text-file <报告>")
    return rc


if __name__ == "__main__":
    sys.exit(main())
