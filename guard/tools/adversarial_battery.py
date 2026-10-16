# -*- coding: utf-8 -*-
"""_guard · 对抗样本电池 —— 治「假报错 / 假根治 / 假通过」（R-59）

## 为什么要有这个（用户 2026-10-15 明令）

> 「**做，保证以后不会出现假报错、假根治、假通过**」

三道闸门层面的病，一句话说清：

| 病 | 长什么样 | 本电池怎么抓 |
|---|---|---|
| **假通过** | 明明违规，闸判绿 | `must_block` 样本 ⇒ **必须判红** |
| **假根治** | 闸在，但"要不要查"挂在**字面措辞**上 ⇒ 换个说法整段跳过 | `must_block` 样本还要求输出里出现**"到达标记"**（哪条判据真的跑了）；只判红不算过 |
| **假报错** | 明明合规，闸判红（误杀）⇒ 人就不信闸了 | `must_pass` 样本 ⇒ **必须判绿** |

实证（本电池就是为此建的，详见 `_orch_audit_20261015/假根治审计报告.md`）：
  · `g_vision`：「我看过那张图了」⇒ 旧版报"无图像结论，跳过"、**exit 0**（假通过 + 假根治）
  · `g_decision`：「打算用 B 方案…最终就按 B 走」⇒ 旧版"无方案决策声明，跳过"、**exit 0**
  · `g_xcheck`/`g_root`：「已**根治**」⇒ 台账核查/根因判据**双双不跑**（exit 0）
  —— 四处都是"按措辞触发"，四处都是同一个病。

## 两类覆盖（**缺一类就是盲区**）

* **文本类闸**（支持 `--text-file`，14 道）：每道必须有 `must_block.md` + `must_pass.md` + `expect.json`；
  缺样本 ⇒ **判红**（不许"没配样本＝通过"，那正是"没扫≠没问题"的老病）。
* **自检类闸**（其余 15 道，含 `g_mem` 这类依赖本机实测的）：跑 `--demo`，**退出码必须 0**
  （它们的 `--demo` 里本来就带"必须拦下/不许误杀"的断言）。

## 用法

    python _guard/tools/adversarial_battery.py              # 全量（约 1–2 min）
    python _guard/tools/adversarial_battery.py --json
    python _guard/tools/adversarial_battery.py --changed gates/g_vision.py,gates/g_root.py
                                                            # 只跑改到的那几道（收尾自动路用，通常 0 道）
    python _guard/tools/adversarial_battery.py --coverage    # 只查覆盖面（毫秒级）

退出码：0 = 全过；1 = 有假通过/假根治/假报错/覆盖面缺口。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

GUARD = Path(__file__).resolve().parent.parent
GATES = GUARD / "gates"
FIX = GUARD / "tools" / "adversarial"
LEDGER = GUARD / "_guard_ledger" / "adversarial.jsonl"

# 自检类闸：**故意不配文本样本**及原因（必须写明，不许默认豁免）
DEMO_ONLY = {
    "g_cap": "读本机已装插件/技能目录，样本会随机器变化",
    "g_doc": "输入是记忆文件本身（AGENTS.md 等），样本=改真文件，不能进电池",
    "g_done": "接口是 `--project`（读台账），无 --text-file",
    "g_intent": "接口是 --said/--means，语义输入不适合固定样本",
    "g_loop": "要跑真循环（有副作用）",
    "g_mem": "**依赖本机实测内存**（读数随机器变化）⇒ 只能用 --demo 里注入的假 probe",
    "g_mem_hygiene": "输入是记忆库+插件代码，样本会过时",
    "g_mount": "输入是 guard_cli.py 源码 AST",
    "g_obj": "接口是 --predicate/--list（语义谓词）",
    "g_path": "接口是 --cmd/--write-path，样本=命令串（已在 demo 覆盖 12 例）",
    "g_probe": "接口是 --subject/--measured",
    "g_public": "扫描面是登记面的真实文件",
    "g_purity": "扫描面是通用层源码本身",
    "g_silent": "扫描面是源码本身",
    "g_spec": "接口是 --spec/--measured（断言对象）",
    "g_vault": "要真写文件/建版本库",
    "g_adv": "**本闸自己就是电池**（--demo 即自检：覆盖面 + 坏样本自证 + 真样本全跑），无文本输入",
    "g_prov": "判据对象是**记忆库**（sqlite），不是文本；样本会随库变化 ⇒ 由 --demo 造 8 个库自证（好库绿 + 7 类坏库各必须红）",
    "g_selfwrite": "判据对象是**库 + 源码扫描面**，不是文本；样本=改真源码 ⇒ 由 --demo 造坏库/坏源码自证（3 类坏输入各必须红）",
    "g_syntax": "输入是**改动文件清单**（路径），不是文本内容；样本=临时目录里造好/坏文件 ⇒ 由 --demo 自证（坏 .py / 坏 .json / 坏 .js 各必须红）",
    "g_witness": "输入是**配置声明对**（核验器×被检对象）；样本=临时目录里造 import 同源/无自证入口的检查器 ⇒ 由 --demo 自证（同源必红 / 无自证必红 / 独立+自证才绿）",
    "g_clean": "输入是**仓路径**；样本=临时 git 仓（干净/造脏/白名单/非 git）⇒ 由 --demo 自证（脏必红 / 白名单放行 / 非 git 记未验）",
    "g_polarity": "输入是**声明文本 + 引文**；样本=内存里造的极性对 ⇒ 由 --demo 自证（极性反转必红 / 纯关键词覆盖必红 / 两侧都没提=未验 / 极性一致才绿）",
    "g_directional": "输入是**矛盾对 + 检索片段**（不是文本）；样本=内存里造的 a/b 引文 ⇒ 由 --demo 自证（只保一侧必红 / 两侧都不在必红 / 改写冒充必红 / 两侧都在才绿）",
    "g_integ": "输入是**改动清单 + 声明的集成项**；样本=临时声明的命令 ⇒ 由 --demo 自证（坏命令必红 / 超时必红 / 配置坏必红 / 未命中必须写明「未跑」）",
}


def discover() -> tuple[dict, dict]:
    """返回 (需要样本对的闸, 走 --demo 的闸)。"""
    pairs, demos = {}, {}
    for p in sorted(GATES.glob("g_*.py")):
        name = p.stem[2:]
        src = p.read_text(encoding="utf-8", errors="replace")
        if '"--text-file"' in src and (FIX / name).exists():
            pairs[name] = p
        else:
            demos[name] = p
    return pairs, demos


def run_gate(gate: Path, args: list[str], timeout: int = 120) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(gate), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout,
                       cwd=str(GUARD.parent))
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def check_pair(name: str, gate: Path, d: Path | None = None) -> list[dict]:
    d = d or (FIX / name)
    rows = []
    expect = {}
    try:
        expect = json.loads((d / "expect.json").read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        # ★ 不静默：期望文件读不出来 ⇒ 必须可见（判"未核"，不当通过）
        print(f"[adv] {name} 的 expect.json 读不出来（按不合格处理）：{e!r}", file=sys.stderr)
        rows.append({"gate": name, "kind": "coverage", "ok": False,
                     "detail": "缺 expect.json（无法判定期望）"})
        return rows
    marker = str(expect.get("must_block_output_contains", "") or "")

    # ① must_block：必须判红 **且** 到达标记出现（只判红不算过 —— 抓"假根治"）
    rc, out = run_gate(gate, ["--text-file", str(d / "must_block.md")])
    blocked = rc != 0
    reached = (marker == "") or (marker in out)
    rows.append({"gate": name, "kind": "must_block", "ok": blocked and reached,
                 "rc": rc, "reached": reached, "marker": marker,
                 "detail": ("判红且判据到达" if (blocked and reached) else
                            ("**判红但判据未到达**（{} 不在输出里 ⇒ 换措辞逃逸）".format(marker)
                             if blocked else "**放行了违规样本（假通过）**"))})

    # ② must_pass：必须判绿（抓"假报错/误杀"）
    rc2, out2 = run_gate(gate, ["--text-file", str(d / "must_pass.md")])
    rows.append({"gate": name, "kind": "must_pass", "ok": rc2 == 0, "rc": rc2,
                 "detail": "合规样本放行" if rc2 == 0 else "**拦下了合规样本（假报错）**："
                           + out2.strip().splitlines()[-1][:80] if out2.strip() else "**拦下了合规样本（假报错）**"})
    return rows


def check_demo(name: str, gate: Path) -> list[dict]:
    rc, out = run_gate(gate, ["--demo"])
    tail = (out.strip().splitlines() or [""])[-1][:90]
    return [{"gate": name, "kind": "demo", "ok": rc == 0, "rc": rc,
             "detail": "自检通过" if rc == 0 else "**自检失败**：" + tail}]


def main() -> int:
    ap = argparse.ArgumentParser(prog="adversarial_battery")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--coverage", action="store_true", help="只查覆盖面（毫秒级）")
    ap.add_argument("--changed", default="", help="逗号分隔的改动文件（只看其中的闸）")
    a = ap.parse_args()

    pairs, demos = discover()
    rows: list[dict] = []

    # ---- 覆盖面：每个闸必须落在"样本对"或"已登记的自检类"里 ----
    all_gates = {p.stem[2:]: p for p in sorted(GATES.glob("g_*.py"))}
    for name, p in all_gates.items():
        if name in pairs:
            continue
        key = "g_" + name
        if key in DEMO_ONLY:
            continue
        if name in demos and (FIX / name).exists():
            # 有目录但没配对（只写了一边）⇒ 也要报
            rows.append({"gate": name, "kind": "coverage", "ok": False,
                         "detail": "样本目录存在但不成对（须同时有 must_block.md / must_pass.md / expect.json）"})
            continue
        rows.append({"gate": name, "kind": "coverage", "ok": False,
                     "detail": "既没配样本对、也没登记为自检类 ⇒ 盲区（要配样本，或写进 DEMO_ONLY 并说明原因）"})

    # ---- 未登记的文本类闸（能 --text-file 却没配样本）也算缺口 ----
    #   ★ 已登记在 DEMO_ONLY 的（如 g_mem：依赖本机实测内存）不算缺口 —— 但要留下登记原因
    for name, p in all_gates.items():
        src = p.read_text(encoding="utf-8", errors="replace")
        if '"--text-file"' in src and not (FIX / name).exists() and ("g_" + name) not in DEMO_ONLY:
            rows.append({"gate": name, "kind": "coverage", "ok": False,
                         "detail": "支持 --text-file 但没有样本对 ⇒ 加 tools/adversarial/%s/" % name})

    if a.coverage:
        pass
    else:
        changed = {x.strip().replace("\\", "/") for x in a.changed.split(",") if x.strip()}
        only = {Path(x).stem[2:] for x in changed if Path(x).stem.startswith("g_")}
        targets_pairs = {k: v for k, v in pairs.items() if not only or k in only}
        targets_demos = {k: v for k, v in demos.items() if not only or k in only}
        for name, p in sorted(targets_pairs.items()):
            rows += check_pair(name, p)
        for name, p in sorted(targets_demos.items()):
            # ★ 不许自指：`g_adv` 就是本电池 —— 拿它当普通闸跑会**无限递归**
            #   （电池 → g_adv --demo → 电池 → …）。它由"覆盖面"那一段单独覆盖。
            if name == "adv":
                continue
            rows += check_demo(name, p)

    bad = [r for r in rows if not r["ok"]]
    if not a.coverage:
        try:
            LEDGER.parent.mkdir(parents=True, exist_ok=True)
            with LEDGER.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                    "n_pairs": len(pairs), "n_demos": len(demos),
                                    "n_bad": len(bad),
                                    "changed": a.changed}, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            print(f"[adv] 台账写入失败（判决仍有效）：{e}", file=sys.stderr)

    if a.json:
        print(json.dumps({"ok": not bad, "rows": rows, "n_pairs": len(pairs),
                          "n_demos": len(demos)}, ensure_ascii=False, indent=1))
    else:
        print("=" * 74)
        print(f"对抗样本电池 · 样本对 {len(pairs)} 道 / 自检类 {len(demos)} 道")
        print("=" * 74)
        for r in rows:
            if r["kind"] == "coverage" and r["ok"]:
                continue
            mark = "✓" if r["ok"] else "✗"
            print(f"  {mark} [{r['kind']:10}] {r['gate']:16} {r['detail']}")
        print(f"\n结论：{'✅ 全过（无假通过/假根治/假报错/盲区）' if not bad else '❌ %d 项不合格' % len(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        print(f"[adv] stdout 重设为 utf-8 失败（继续跑）：{e!r}", file=sys.stderr)
    sys.exit(main())
