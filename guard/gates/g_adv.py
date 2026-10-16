# -*- coding: utf-8 -*-
"""_guard · 对抗电池闸 g_adv —— 把「假报错 / 假根治 / 假通过」变成**每次收尾都在跑**的判据（R-59）

## 为什么它必须是一道闸（而不是一个脚本）

用户 2026-10-15 明令：「**做，保证以后不会出现假报错、假根治、假通过**」。

脚本会被人忘掉，闸不会 —— 所以：
  · 本闸登记进 `guard_cli.GATE_FILES` 与 `cmd_selftest`（否则"闸坏了没人知道"，见 R-57）；
  · `hook.py` 在 `Hook.after` 里用 `--changed <改到的闸>` 调它 —— **改动哪道闸，就重跑那道闸的样本对**；
  · 覆盖面写死在判据里：**支持 `--text-file` 的闸必须有样本对**（缺 ⇒ 红），
    其余闸必须登记在 `DEMO_ONLY` 里并写明原因（**不许"没样本＝通过"** —— 那正是"没扫≠没问题"的老病）。

## `--demo` 干什么（selftest 每轮都跑它）

  ① **覆盖面**：文本类闸的样本对是否齐全、自检类闸是否都登记了原因；
  ② **负例自证（关键）**：临时造一个"**坏**样本对"——must_block 写成**完全合规**的文本 ⇒
     电池**必须**把它判成"假通过"；判不出来 ⇒ 本闸红。"闸的闸"必须自己能拦。
  ③ **正例**：真样本对全跑 ⇒ 必须全过。

## 用法

    python _guard/gates/g_adv.py --demo                 # 自检（覆盖 + 负例自证 + 真样本全跑）
    python _guard/gates/g_adv.py --full                 # 全量电池（含所有 --demo 类闸）
    python _guard/gates/g_adv.py --changed a.py,b.py    # 只跑改到的闸（收尾自动路）
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402

GUARD = Path(__file__).resolve().parent.parent
TOOLS = GUARD / "tools"
BATTERY = TOOLS / "adversarial_battery.py"


def _battery(args: list[str], timeout: int = 300) -> tuple[int, dict]:
    r = subprocess.run([sys.executable, str(BATTERY), "--json", *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, cwd=str(GUARD.parent))
    try:
        return r.returncode, json.loads(r.stdout or "{}")
    except Exception as e:  # noqa: BLE001
        # ★ 不静默：电池的输出解析不了 ⇒ 必须**看得见**（否则"解析失败"会被当成"没发现问题"）
        print(f"[g_adv] 电池输出无法解析（判据按未核处理）：{e!r}", file=sys.stderr)
        return r.returncode, {"ok": False, "rows": [],
                              "raw": (r.stdout or r.stderr or "")[:400]}


class AdvGate:
    NAME = "g_adv"

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg

    def check(self) -> dict:
        rc, rep = _battery([])
        bad = [r for r in (rep.get("rows") or []) if not r.get("ok")]
        return {"gate": self.NAME, "ok": rc == 0 and not bad,
                "n_pairs": rep.get("n_pairs"), "n_demos": rep.get("n_demos"),
                "bad": bad, "rc": rc}

    def check_changed(self, changed: list[str]) -> dict:
        gates = [c for c in changed if Path(str(c)).stem.startswith("g_") or "gates/" in str(c).replace("\\", "/")]
        if not gates:
            return {"gate": self.NAME, "ok": True, "n_pairs": 0, "n_demos": 0, "bad": [],
                    "detail": "本次改动没有闸文件 ⇒ 无需重跑样本对"}
        rc, rep = _battery(["--changed", ",".join(gates)])
        bad = [r for r in (rep.get("rows") or []) if not r.get("ok")]
        return {"gate": self.NAME, "ok": rc == 0 and not bad, "bad": bad, "rc": rc,
                "detail": f"重跑了 {len(gates)} 道改动的闸"}

    def render(self, rep: dict) -> str:
        if rep.get("ok"):
            if rep.get("n_pairs") is None:
                return f"对抗电池：绿（{rep.get('detail') or '本次无改动闸'}）"
            return (f"对抗电池：绿（样本对 {rep.get('n_pairs')} 道 / 自检类 {rep.get('n_demos')} 道 ——"
                    f" 无假通过/假根治/假报错/盲区）")
        lines = [f"对抗电池：红（{len(rep.get('bad') or [])} 项不合格）"]
        for b in (rep.get("bad") or [])[:12]:
            lines.append(f"  ✗ [{b.get('kind', '?')}] {b.get('gate', '?')}：{str(b.get('detail'))[:110]}")
        return "\n".join(lines)


def _coverage(rows: list[dict]) -> None:
    for r in rows:
        if r.get("kind") == "coverage":
            mark = "✓" if r["ok"] else "✗"
            print(f"  {mark} {r['gate']:16} {r['detail'][:110]}")


def demo(cfg: ProjectConfig) -> int:
    g = AdvGate(cfg)
    fails = 0
    print("### 对抗电池闸 · 自检（覆盖 + 负例自证 + 真样本全跑）\n")

    # ---- ① 覆盖面 ----
    print("### ① 覆盖面（缺样本 ⇒ 红；自检类必须登记原因）")
    rc, rep = _battery(["--coverage"])
    cov = [r for r in (rep.get("rows") or []) if r.get("kind") == "coverage"]
    _coverage(cov)
    if rc != 0 or any(not r["ok"] for r in cov):
        print("  ✗✗ 覆盖面有缺口 ⇒ 有闸没有样本对，也没登记原因")
        fails += 1
    else:
        print(f"  ✓ 覆盖面完整（文本类闸都有样本对；自检类均登记了原因）")
    print()

    # ---- ② 负例自证：造一个"坏样本对"（must_block 写成完全合规的文本）⇒ 电池必须判它假通过 ----
    print("### ② 负例自证：坏样本对（must_block 写成合规文本）⇒ 电池必须报『假通过』")
    sys.path.insert(0, str(TOOLS))          # 电池在 _guard/tools 下
    import adversarial_battery as B  # noqa: E402
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "decision"
        d.mkdir(parents=True, exist_ok=True)
        (d / "must_block.md").write_text(
            "比选：方案 A 识破率 100%、成本 4 h；方案 B 0%、9 h。判据：识破率 ≥95%。最终采用方案 A。\n",
            encoding="utf-8")
        (d / "must_pass.md").write_text("无内容。\n", encoding="utf-8")
        (d / "expect.json").write_text('{"must_block_output_contains": "决策"}', encoding="utf-8")
        rows = B.check_pair("decision", GUARD / "gates" / "g_decision.py", d)
        flagged = [r for r in rows if r.get("kind") == "must_block" and not r["ok"]]
        print(f"  {'✓✓拦下' if flagged else '✗✗ 没拦住（本闸自己是摆设）'}  "
              f"坏样本被判：{flagged[0]['detail'][:70] if flagged else '（没报问题）'}")
        if not flagged:
            fails += 1
    print()

    # ---- ③ 真样本对全跑 ----
    print("### ③ 真样本对全跑（必须全过）")
    rc3, rep3 = _battery([])
    bad = [r for r in (rep3.get("rows") or []) if not r.get("ok")]
    for b in bad[:8]:
        print(f"  ✗ [{b.get('kind')}] {b.get('gate')}：{str(b.get('detail'))[:100]}")
    if rc3 != 0 or bad:
        print(f"  ✗✗ 有 {len(bad)} 项不合格")
        fails += 1
    else:
        print(f"  ✓ 全过（样本对 {rep3.get('n_pairs')} 道 / 自检类 {rep3.get('n_demos')} 道）")
    print()

    print(f"### 自检结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        print(f"[g_adv] stdout 重设为 utf-8 失败（继续跑）：{e!r}", file=sys.stderr)
    from guard import load as load_cfg

    ap = argparse.ArgumentParser(prog="g_adv")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--changed", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    if a.demo:
        sys.exit(demo(cfg))
    gt = AdvGate(cfg)
    if a.changed:
        rep = gt.check_changed([x for x in a.changed.split(",") if x.strip()])
    elif a.full:
        rc, r2 = _battery([])
        rep = {"gate": gt.NAME, "ok": rc == 0 and not [r for r in (r2.get("rows") or []) if not r.get("ok")],
               "n_pairs": r2.get("n_pairs"), "n_demos": r2.get("n_demos"),
               "bad": [r for r in (r2.get("rows") or []) if not r.get("ok")]}
    else:
        rep = gt.check()
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(gt.render(rep))
    sys.exit(0 if rep.get("ok") else 1)
