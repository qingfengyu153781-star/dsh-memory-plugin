# -*- coding: utf-8 -*-
"""_guard · 闸挂载完整性闸 g_mount —— 每一道闸都必须「跑得到」

## 治的哪条规矩（R-57，2026-10-15 立）

**R-57**：`_guard/gates/` 下的每一道闸（`g_*.py`）必须
① 登记在 `guard_cli.GATE_FILES`（否则 CLI 跑不到它）、
② 列进 `cmd_selftest` 的自检清单（否则 `guard_cli selftest` **永远不跑它**）、
③ 登记项指向的文件真实存在。

## 为什么必须做成闸（实测盲区）

lingshu 自认过同构的洞：「**脚本式件不列入 SCRIPT_TESTS，则门禁根本看不见它**
——同族盲区已出现三次（#69/#26/#145）」。我们这边**实测**：

    2026-10-15 首测：`gates/` 有 **27** 道闸，`GATE_FILES` 只登记 **22** 个
    （缺 batch / constraints / criteria / del / loop），`selftest` 清单又少一个 vault
    ⇒ `guard_cli selftest` 实际只覆盖 **21/27** —— 另外 6 道**坏了也不会被自检发现**。

`rules.py` 覆盖的是「**规矩 → 闸**」（这条规矩有没有闸在跑），
**不覆盖**「**闸 → 可跑性**」（这道闸在不在 CLI/自检清单里）。两者是正交的，缺一不可。

## 判据（三条，任一不满足 ⇒ 红）

| # | 判据 | 说明 |
|---|---|---|
| M1 | 每个 `gates/g_*.py` 的别名都在 `GATE_FILES` 里 | 否则 **CLI 跑不到**（只能手敲 python gates/xxx.py） |
| M2 | `GATE_FILES` 的每个键都在 `cmd_selftest` 清单里 | 否则 **`selftest` 不跑它** |
| M3 | `GATE_FILES` 每个值指向的文件存在 | 否则登记是空的（跑起来必报错） |

★判据**从 `guard_cli.py` 源码解析**（AST 读 `GATE_FILES` 字面量与 `cmd_selftest` 的元组），
**不写死名单**——写死就等于把"清单"抄成第二份真相源，清单变了闸不知道。

## 用法

    python _guard/gates/g_mount.py            # 扫，有缺口即红
    python _guard/gates/g_mount.py --json
    python _guard/gates/g_mount.py --demo     # 3 个负例自检
    python _guard/guard_cli.py mount
"""
from __future__ import annotations

import argparse
import ast
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # _guard/gates
GUARD = HERE.parent                             # _guard
sys.path.insert(0, str(GUARD))
from guard import ProjectConfig  # noqa: E402

CLI_PATH = GUARD / "guard_cli.py"


def parse_cli(path: Path) -> dict:
    """从 guard_cli.py 解析：GATE_FILES 的键/值文件名、cmd_selftest 的清单。"""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    keys: set[str] = set()
    values: dict[str, str] = {}
    selftest: set[str] = set()

    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "GATE_FILES" for t in node.targets):
            if isinstance(node.value, ast.Dict):
                for k, v in zip(node.value.keys, node.value.values):
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
                        # 取表达式里最后一个字符串常量（HERE / "gates" / "g_spec.py"）
                        # 取表达式**最右侧**的字符串常量（`HERE / "gates" / "g_spec.py"`
                        #   ⇒ "g_spec.py"）。★不能用 ast.walk 的"最后一个"：BFS 顺序会
                        #   先给外层 "gates" 再给右端 "g_spec.py"，实测把 22 个登记项
                        #   全误报成"文件不存在"（假阳性）。
                        node_v = v
                        while isinstance(node_v, ast.BinOp):
                            node_v = node_v.right
                        values[k.value] = (node_v.value
                                           if isinstance(node_v, ast.Constant) else "")

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_selftest":
            for sub in ast.walk(node):
                if isinstance(sub, ast.For):
                    # 正常是元组；**单元素若写成 `("a")`（少个尾逗号），AST 里是
                    # Constant(str) 而不是 Tuple** —— 那种写法同样要认。
                    # （实测：demo 夹具用单元素清单时就踩了这条，导致"解析出空清单"。）
                    if isinstance(sub.iter, ast.Tuple):
                        items = list(sub.iter.elts)
                    elif isinstance(sub.iter, ast.Constant):
                        items = [sub.iter]
                    else:
                        items = []
                    for elt in items:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            selftest.add(elt.value)

    return {"gate_files": keys, "gate_file_names": values, "selftest": selftest}


def audit(gates_dir: Path, cli_path: Path) -> dict:
    """三条判据。返回 {ok, m1_missing, m2_missing, m3_missing, n_gates, ...}。"""
    cli = parse_cli(cli_path)
    gates = {p.stem[2:]: p for p in sorted(gates_dir.glob("g_*.py"))}   # 别名 -> 文件
    m1 = sorted(set(gates) - cli["gate_files"])          # 有闸文件但没登记
    m2 = sorted(set(cli["gate_files"]) - cli["selftest"])  # 登记了但不进自检
    m3 = sorted(k for k, fn in cli["gate_file_names"].items()
                if fn and not (gates_dir / fn).exists())
    return {
        "ok": not (m1 or m2 or m3),
        "gate": "g_mount",
        "n_gates": len(gates),
        "n_registered": len(cli["gate_files"]),
        "n_selftest": len(cli["selftest"]),
        "m1_not_registered": m1,
        "m2_not_in_selftest": m2,
        "m3_missing_file": m3,
    }


class MountGate:
    """闸挂载完整性闸。"""

    def __init__(self, cfg: ProjectConfig | None = None):
        self.cfg = cfg

    def check(self) -> dict:
        try:
            return audit(HERE, CLI_PATH)
        except Exception as exc:                   # 解析失败 ⇒ 红（审计面不完整）
            # ★ 不静默：解析失败本身就是"审计面不完整"，除了返回红，**还要在 stderr 上说一句**
            #   （R-54 的 silent 口径只认 warn/log/print/raise/audit/error 子串 ——
            #    只 return 一个红字典，在它眼里与"静默吞掉"长得一样）
            print("[g_mount] 解析 guard_cli.py 失败（审计面不完整 ⇒ 判红）：%r" % (exc,),
                  file=sys.stderr)
            return {"ok": False, "gate": "g_mount", "note": "解析 guard_cli.py 失败：%r" % (exc,),
                    "m1_not_registered": [], "m2_not_in_selftest": [], "m3_missing_file": [],
                    "n_gates": 0, "n_registered": 0, "n_selftest": 0}

    def render(self, rep: dict) -> str:
        if rep.get("note"):
            return "闸挂载完整性：红 —— %s" % rep["note"]
        head = ("闸挂载完整性：闸文件 %d / GATE_FILES %d / selftest %d"
                % (rep["n_gates"], rep["n_registered"], rep["n_selftest"]))
        if rep["ok"]:
            return head + " —— 绿（三者一致）"
        lines = [head + " —— 红"]
        if rep["m1_not_registered"]:
            lines.append("  M1 有闸但未登记 GATE_FILES（CLI 跑不到）：%s"
                         % ", ".join(rep["m1_not_registered"]))
        if rep["m2_not_in_selftest"]:
            lines.append("  M2 已登记但不在 selftest 清单（自检不跑它）：%s"
                         % ", ".join(rep["m2_not_in_selftest"]))
        if rep["m3_missing_file"]:
            lines.append("  M3 登记了但文件不存在：%s" % ", ".join(rep["m3_missing_file"]))
        lines.append("  处置：在 guard_cli.py 的 GATE_FILES 与 cmd_selftest 元组里补登；"
                     "**不要**把这条闸的名单写死成第二份真相源。")
        return "\n".join(lines)


# ------------------------------------------------------------------ 负例自检
def demo(cfg: ProjectConfig | None = None) -> int:
    base = Path(tempfile.mkdtemp(prefix="mount_demo_"))
    try:
        ok = True
        gd = base / "gates"
        gd.mkdir()
        cli = base / "guard_cli.py"

        def build(registered: list[str], selftest: list[str]) -> None:
            body = "GATE_FILES = {\n" + "".join(
                '    "%s": HERE / "gates" / "g_%s.py",\n' % (k, k) for k in registered) + "}\n"
            body += "\ndef cmd_selftest(a):\n    for name in (%s%s):\n        pass\n" % (
                ", ".join('"%s"' % s for s in selftest),
                "," if len(selftest) == 1 else "")
            cli.write_text("from pathlib import Path\nHERE = Path(__file__).parent\n" + body,
                           encoding="utf-8")

        # ① 有闸文件但未登记 ⇒ M1 必须红
        (gd / "g_a.py").write_text("X=1\n", encoding="utf-8")
        build([], [])
        r1 = audit(gd, cli)
        print("  ① 闸文件未登记 GATE_FILES -> M1=%s（应含 a）" % r1["m1_not_registered"])
        ok &= r1["m1_not_registered"] == ["a"]

        # ② 已登记但不在 selftest ⇒ M2 必须红
        build(["a"], [])
        r2 = audit(gd, cli)
        print("  ② 登记了但不在自检清单   -> M2=%s（应含 a）" % r2["m2_not_in_selftest"])
        ok &= r2["m2_not_in_selftest"] == ["a"]

        # ③ 登记了但文件不存在 ⇒ M3 必须红
        build(["a", "ghost"], ["a", "ghost"])
        r3 = audit(gd, cli)
        print("  ③ 登记了但文件不存在     -> M3=%s（应含 ghost）" % r3["m3_missing_file"])
        ok &= r3["m3_missing_file"] == ["ghost"]

        # ④ 三者一致 ⇒ 必须绿
        build(["a"], ["a"])
        r4 = audit(gd, cli)
        print("  ④ 三者一致               -> ok=%s（应 True）" % r4["ok"])
        ok &= (r4["ok"] is True)

        print("  demo 结论：%s" % ("全过（闸真的会动）" if ok else "有负例未按预期——闸坏了"))
        return 0 if ok else 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _log(rec: dict) -> None:
    try:
        ledger = GUARD / "ledger"
        ledger.mkdir(exist_ok=True)
        with (ledger / "g_mount.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:                       # 记录失败不静默
        print("g_mount: 台账写入失败：%r" % (exc,), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="g_mount.py", description="闸挂载完整性闸（R-57）")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    cfg = None
    if not a.demo:
        try:
            from guard import load as load_cfg
            cfg = load_cfg(a.project, a.project_root)
        except Exception as exc:
            print("g_mount: 项目配置加载失败，退到默认：%r" % (exc,), file=sys.stderr)

    if a.demo:
        rc = demo(cfg)
        _log({"demo": True, "ok": rc == 0})
        return rc

    gate = MountGate(cfg)
    rep = gate.check()
    _log({"ok": rep["ok"], "n_gates": rep.get("n_gates"),
          "m1": rep.get("m1_not_registered"), "m2": rep.get("m2_not_in_selftest")})
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, sort_keys=True))
    else:
        print(gate.render(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
