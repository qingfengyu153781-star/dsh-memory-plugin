# -*- coding: utf-8 -*-
"""_guard · 规矩台账 rules.yaml 的装载与自证（把 AGENTS.md 的每条规矩接到闸上）

## 为什么有这个东西（2026-10-03 用户点破）

用户原话：「…」

**问题的实质**：
  · AGENTS.md 是**靠读到 + 记住**生效的 —— 换会话 / 重开发 / 上下文被压缩，就会漂
  · 用户已经实测过："写进 AGENTS.md 在重开发的时候依旧会不记得"
  · 所以规矩不能只写在文档里，**每条规矩都必须有个东西在跑**

**这份台账做的事**：
  把 AGENTS.md 里的每条规矩，逐条登记成 `rule → gate → 检查动作`。
  然后**机器来核对**：哪些规矩有闸（绿）、哪些是"裸的"（红）。

**关键设计（决定它是"活的"还是"摆设"）**：
  · `check` 必须是**可执行的**（能跑、有退出码），不是一句形容词
  · 定期跑 `verify`，**裸规矩红了就要去补闸**，不是补文档
  · 每条规矩带 `src`（AGENTS.md 的行号）—— 规矩改了能被发现
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402

HERE = Path(__file__).resolve().parent


def load_rules(path: Path | None = None) -> dict:
    """读 rules.yaml（极简 YAML 子集解析：够用即可，不引第三方依赖）。"""
    p = path or (HERE / "rules.yaml")
    if not p.exists():
        return {"rules": []}
    txt = p.read_text(encoding="utf-8")
    rules, cur = [], None
    for raw in txt.split("\n"):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^\s*-\s+id:\s*(.+)$", line)
        if m:
            cur = {"id": m.group(1).strip()}
            rules.append(cur)
            continue
        m = re.match(r"^\s+(\w+):\s*(.*)$", line)
        if m and cur is not None:
            k, v = m.group(1), m.group(2).strip()
            if v.startswith("[") and v.endswith("]"):
                v = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
            elif (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            cur[k] = v
    return {"rules": rules}


# ══════════════════════════════════════════════════════════════════════════
# ★ P0-1/P0-2/P0-3 修复（2026-10-04，Lead 亲自坐实并修）
#
# **病根**：这份台账报「40 条规矩 / 40 条已接闸 / 覆盖率 100%」，
#   实际只量到「**闸文件存在** + `--demo` 返回 0」——**判据量错对象**（病根 1）。
#
# 三条实测硬缺陷（证据：`_guard/_audit_lead/probe/_probe_rules.py`，
# `_guard/_audit/corpus/REAL_CASES.md` 语料 5）：
#
#   ① **自证条款**：原 `status = "✓" if (has_gate and (selftest_ok in (True, None))) else "✗"`
#      —— `selftest_ok is None` **也算 ✓**。实测：一个**空壳闸**
#      （`print('all good'); sys.exit(0)`）能被认证为"已接闸"。
#      `--no-run` 时 selftest_ok 恒 None ⇒ status 恒 ✓ ⇒ **退化为"只看文件在不在"**。
#
#   ② **死代码**：原 `for argv in ([file,"--demo"], [file,"list"])`
#      —— 实测 **18/18 道闸 `--demo` 全部 rc=0**，循环在第一步就 break，
#      **`list` 分支永远走不到**。（与 `g_behavior` 的 `max(mx,2)` 同族）
#
#   ③ **措辞脆弱**：原靠字符串匹配 `"unrecognized arguments"` / `"invalid choice"`
#      来区分"参数不支持"与"自检失败"—— 认的是 **argparse 的英文原文**。
#      任何闸改用 `parse_known_args`、自定义 `error()`、或中文提示，
#      两个串都为假 ⇒ 走 `selftest_ok=False` ⇒ 该闸被**误判「✗ 闸坏了」**。
#      （textgates/wiregates 两侧独立指出，属"当前未触发的潜伏缺陷"）
#
# ★ 修法：把"三层"改成**四层**，且每层各自诚实取值：
#   ① 登记了 gate 名      ② 闸文件存在
#   ③ 自检真的跑过（**要求 exit 0 且 stdout 有内容**，不认措辞）
#   ④ ★ **在自动路上**（hook.py 是否实例化该 Gate 类）—— 新增，这才是"接上了"的实质
#   且 **未核 ⇒ None，不许当 ✓**。
# ══════════════════════════════════════════════════════════════════════════

def _mounted_gates() -> set[str]:
    """④ 新增层：扫 hook.py，看哪些 Gate 类被**实例化**（=真在自动路上）。

    实测口径（Lead 复核过）：hook.py import 了 8 个 Gate 类，
    其中 DocGate/IntentGate/RootGate/StructGate/VisionGate/XCheckGate 真被调用
    ⇒ **17 道闸里只有 6 道在自动路上**（覆盖率 35%，而旧口径报 100%）。
    """
    import ast as _ast
    hooked: set[str] = set()
    hp = HERE / "hook.py"
    if not hp.exists():
        return hooked
    try:
        src = hp.read_text(encoding="utf-8", errors="replace")
        tree = _ast.parse(src)
    except SyntaxError:
        return hooked

    # ① 收集 hook.py 里被调用的**名字**（类实例化 or 函数调用）
    called = set()
    for n in _ast.walk(tree):
        if isinstance(n, _ast.Call):
            f = n.func
            nm = f.id if isinstance(f, _ast.Name) else (f.attr if isinstance(f, _ast.Attribute) else "")
            if nm:
                called.add(nm)
    # ② hook.py 里 `from gates.X import A, B` 的形态也要认
    #    （`guard_vault` 用 `from gates.g_vault import Vault`，Vault 不含 "Gate"）
    imported_from_gates: dict[str, set[str]] = {}
    for n in _ast.walk(tree):
        if isinstance(n, _ast.ImportFrom) and n.module and n.module.startswith("gates."):
            mod = n.module.split(".")[-1]
            imported_from_gates.setdefault(mod, set()).update(a.name for a in n.names)

    # ③ 逐个闸文件：它的**类名或模块级函数名**只要出现在调用集合里 ⇒ 判"在自动路上"
    for p in (HERE / "gates").glob("*.py"):
        if p.stem.startswith("_"):
            continue
        try:
            t = _ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        exported: set[str] = set()
        for n in _ast.walk(t):
            if isinstance(n, _ast.ClassDef):
                exported.add(n.name)                       # 任意类名（含 Vault）
            elif isinstance(n, _ast.FunctionDef) and n.col_offset == 0:
                exported.add(n.name)                       # 模块级函数（含 annotate）
        # ★ 判据：hook.py **调用**了它（用 called 集合），或从该模块 import 后又调用
        # ★ 2026-10-14 收窄（治假绿）：**只认类名撞车，不认模块级函数名**。
        #   实测证据：新闸 g_silent 的模块级 `_log` 与 hook.py 里的 `_log` 调用撞车
        #   ⇒ 交集非空 ⇒ 被判"在自动路上"；rules 于是打印"53 条规矩全部接线完整、
        #   都在自动路上"，却与本文件自述的"17 道闸里只有 6 道在自动路上（覆盖率 35%）"
        #   直接矛盾。通用名（`_log`/`demo`/`main`/`scan_root`）撞车概率极高，
        #   不能当挂载证据。现口径：**类名**匹配（`XxxGate`/`Vault` 等，特异）
        #   ＋ 下方 `from gates.X import N` 且 N 被调用（显式导入路径）。
        #   上方 exported 的「模块级函数名」收集**保留但不再采信**（免得误删有用信息）。
        _class_names = {n.name for n in _ast.walk(t) if isinstance(n, _ast.ClassDef)}
        if _class_names & called:
            hooked.add(p.stem)                             # g_xxx.py
            hooked.add(p.stem[2:] if p.stem.startswith("g_") else p.stem)   # 别名
        # `from gates.X import Name` 且 Name 被调用
        imp = imported_from_gates.get(p.stem, set())
        if imp & called:
            hooked.add(p.stem)
            hooked.add(p.stem[2:] if p.stem.startswith("g_") else p.stem)
    return hooked


def verify(cfg: ProjectConfig | None = None, *, run_checks: bool = True) -> dict:
    """核对每条规矩的闸是否真的在。

    **四个层次（缺一层都不算"接上了"）**：
      ① 规矩登记了 gate 名
      ② 该 gate 的**文件真实存在**
      ③ 该 gate 的**自检真的跑过**（exit 0 且 stdout 非空）
      ④ ★ 该 gate **在自动路上**（`hook.py` 真实例化了它的 Gate 类）
    **未核（None）一律不算 ✓** —— 这是 2026-10-04 修掉的自证条款。
    """
    rules = load_rules()["rules"]
    gate_dir = HERE / "gates"        # HERE = _guard/，闸门在 _guard/gates/
    mounted = _mounted_gates()

    # ★ 性能修复（2026-10-04）：原实现对**每条规矩**都起一次子进程跑 `--demo`
    #   ⇒ 40 条规矩 = 40 次冷启动（实测 >120 s，命令超时）。
    #   同一道闸被多条规矩引用（g_root 被 7 条引用），重复跑纯属浪费。
    #   ⇒ 改成**按闸缓存**：每道闸只跑一次自检。
    _selftest_cache: dict[str, bool | None] = {}

    def _selftest_once(gate_name: str, gate_file) -> bool | None:
        if gate_name in _selftest_cache:
            return _selftest_cache[gate_name]
        val: bool | None = None
        if run_checks and gate_file is not None:
            try:
                rr = subprocess.run([sys.executable, str(gate_file), "--demo"],
                                    capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=120)
                # exit 0 且确实产出了自检内容 ⇒ 才算"自检跑过"
                val = (rr.returncode == 0 and bool((rr.stdout or "").strip()))
            except Exception:  # noqa: BLE001
                val = False
        _selftest_cache[gate_name] = val
        return val

    rows, naked, broken, unmounted = [], [], [], []
    for r in rules:
        rid = r.get("id", "?")
        gate = r.get("gate", "")
        chk = r.get("check", "")

        gate_file = None
        if gate and gate != "none":
            cand = gate_dir / f"{gate}.py"
            gate_file = cand if cand.exists() else None

        has_gate = bool(gate_file)
        # ③ 自检：按闸缓存，只跑一次（P0 性能修复）
        selftest_ok = _selftest_once(gate, gate_file) if has_gate else None

        # ④ 在不在自动路上
        is_mounted = gate in mounted if gate and gate != "none" else None

        # ★ 判据（P0-1 修）：None 不再算 ✓。
        #   status 三态：✓ 全过 / ✗ 有硬伤 / ⚠ 未核或不在自动路
        reasons = []
        if not has_gate or gate == "none":
            naked.append(rid); reasons.append("无闸文件")
        if selftest_ok is False:
            broken.append(rid); reasons.append("自检不过")
        if is_mounted is False:
            unmounted.append(rid); reasons.append("不在自动路（只能手敲）")
        if not reasons:
            status = "✓" if selftest_ok is True else "⚠"
        elif any(x in reasons for x in ("无闸文件", "自检不过")):
            status = "✗"
        else:
            status = "⚠"

        rows.append({
            "id": rid, "rule": r.get("rule", ""), "gate": gate or "(无)",
            "has_file": has_gate, "selftest_ok": selftest_ok,
            "mounted": is_mounted,
            "check": chk, "src": r.get("src", ""), "status": status,
            "reasons": reasons,
        })

    total = len(rows)
    wired = sum(1 for x in rows if x["status"] == "✓")
    # ★ 两个口径分开报，别再合成一个"100%"
    mounted_rows = [x for x in rows if x["mounted"] is True]
    rep = {
        "total": total, "wired": wired, "naked": naked, "broken": broken,
        "unmounted": unmounted,
        "coverage": round(wired / total, 3) if total else 0.0,
        # ★ 实质口径：既自检过、又在自动路上
        "automounted": len(mounted_rows),
        "automounted_coverage": round(len(mounted_rows) / total, 3) if total else 0.0,
        "rows": rows,
    }
    return rep


def report(cfg: ProjectConfig | None = None, *, run_checks: bool = True) -> int:
    rep = verify(cfg, run_checks=run_checks)
    print("=" * 78)
    print("AGENTS.md 规矩 → 闸 的接线核对")
    print("=" * 78)
    # ★ P0-1 修复核心：**两个口径分开报**，不再合成一个"100%"。
    #   旧输出「已接闸 40 ｜ 覆盖率 100%」量的是"文件存在 + --demo rc=0"，
    #   与"闸真在自动路上"（实测 6/17）差得极远 ⇒ 正是"判据量错对象"。
    print(f"  规矩总数 {rep['total']}")
    print(f"  ① 有闸文件 + 自检跑过  ：{rep['wired']}  ({rep['coverage']:.0%})")
    print(f"  ② ★ 且在自动路上（实质）：{rep['automounted']}  "
          f"({rep['automounted_coverage']:.0%})   ← hook.py 真会调到它")
    print()
    print("  说明：② 才是『接上了』的实质口径。① 高而 ② 低 ⇒ 闸只能手敲，不在路上。")
    print()
    for x in rep["rows"]:
        mark = x["status"]
        gate = x["gate"]
        extra = ""
        if not x["has_file"] and gate != "(无)":
            extra = "  ← 闸文件不存在！"
        elif x["selftest_ok"] is False:
            extra = "  ← 自检不过！"
        elif x.get("mounted") is False:
            extra = "  ← 不在自动路（只能手敲）"
        elif x["selftest_ok"] is None:
            extra = "  ← 未核自检（--no-run）"
        print(f"  {mark} {x['id']:<8} {gate:<10} {x['rule'][:40]}{extra}")
    print()
    if rep["naked"]:
        print(f"  ⚠ 裸规矩（登记了但没有闸）：{len(rep['naked'])} 条")
        for rid in rep["naked"]:
            print(f"      · {rid}")
    if rep["broken"]:
        print(f"  ✗ 闸坏了（自检不过）：{rep['broken']}")
    if rep.get("unmounted"):
        print(f"  ⚠ 不在自动路（只能手敲）：{len(rep['unmounted'])} 条规矩")
        for rid in rep["unmounted"]:
            print(f"      · {rid}")
    if not rep["naked"] and not rep["broken"] and not rep.get("unmounted"):
        print("  ✓ 每条规矩都有会跑的闸，且都在自动路上（接线完整）")
    # ★ P0-1：`--no-run` 时**不许**返回 0（那等于"未核也满分"）
    if not run_checks:
        print("\n  ⚠ --no-run：只查了文件在不在，**未核自检、未核挂载** ⇒ 本次结论不可当覆盖率用。")
        return 2
    return 0 if (not rep["naked"] and not rep["broken"] and not rep.get("unmounted")) else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    from guard import load as load_cfg

    ap = argparse.ArgumentParser(prog="rules")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--no-run", action="store_true", help="只查文件在不在，不跑自检")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    if a.json:
        print(json.dumps(verify(cfg, run_checks=not a.no_run), ensure_ascii=False, indent=1))
        sys.exit(0)
    sys.exit(report(cfg, run_checks=not a.no_run))
