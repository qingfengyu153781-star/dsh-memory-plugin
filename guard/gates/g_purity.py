# -*- coding: utf-8 -*-
"""_guard · 通用层纯净性闸 g_purity —— `_guard` 是跨项目通用层，不许沾具体项目

## 治的哪条规矩（R-56，2026-10-15 立）

**R-56**：`_guard/gates/*.py` 与 `_guard/*.py`（跨项目通用层）的**代码**里
不许出现具体项目的内容——具体项目的名字、指向它的绝对路径。
路径一律从**配置**读（`tools/*.json`、`projects/*.yaml`），代码里用 `HERE`/`GUARD` 相对定位。

## 为什么必须做成闸

`_guard` 的价值在于「**任何项目都不改代码就能用**」（`g_mem` 的注释里就写着
"跨项目：阈值来自 config.mem；本文件不含项目专有内容"）。但这条**没有东西在跑**——
一旦有人图省事把 `Path(r"<WORKSPACE>\<某项目>")` 写进闸里，通用性当场破，而且**没人会发现**
（在本机跑永远是对的，换机/换项目才炸）。

实证（本条闸的起因）：**2026-10-14 我自己在 `g_silent.py` 里写死了
`Path(r"<WORKSPACE>\dsh-rag-memory")`** 作为扫描根——正是这条纪律要防的形态。
已改为从 `tools/silent_scan_roots.json` 读。

## 来源

lingshu（FuRongJun-1999）的 `tools/import_direction_lint.py`（依赖方向机械化审计）
＋ 它的**受控面纪律**（`docs/architecture.md` §6.2：受控面由启动期快照或显式配置声明，
**不随判定时的 cwd 变化**；以及「项目根由参数传、不写死」这一取向）。

## 判据（★ 只扫代码，不扫注释/docstring）

| 情形 | 判定 |
|---|---|
| **字符串字面量 / import** 命中 `tools/purity_deny.txt` 里的正则 | **红（退出 1）** |
| 同一处（行）带 `purity-allow: <理由>` | 放过（行级、显式、可 grep） |
| 禁止清单为空 / 配置缺失 | **红**（审计面不完整，不当绿） |

**为什么排除 docstring 与注释**：要量的是"**代码依赖**"，不是"文本提及"——
件头写「本件移植自 lingshu」是**注明出处**（正当），而
`Path(r"<WORKSPACE>\lingshu")` 才是依赖。判据必须量对对象。

## 用法

    python _guard/gates/g_purity.py            # 扫通用层
    python _guard/gates/g_purity.py --json
    python _guard/gates/g_purity.py --demo     # 4 个负例自检
    python _guard/guard_cli.py purity
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # _guard/gates
GUARD = HERE.parent                             # _guard
sys.path.insert(0, str(GUARD))
from guard import ProjectConfig  # noqa: E402

TOOLS = GUARD / "tools"
DENY_PATH = TOOLS / "purity_deny.txt"
#: ★ 本机覆盖（**gitignored**）：真项目名/真路径落这里，不进公开仓
#: （否则"治泄露的清单"自己就把项目名发布了 —— 2026-10-16 实测教训）。
DENY_LOCAL = TOOLS / "purity_deny.local.txt"
#: 存量基线（每文件允许的命中数）——「只防新增」，抄 lingshu 四个 lint 的共同模式
BASELINE_PATH = TOOLS / "purity_baseline.txt"


def _load_baseline() -> dict:
    """读基线（文件缺失 ⇒ 空表，语义 = 每文件 0 处）。"""
    if not BASELINE_PATH.exists():
        return {}
    out: dict = {}
    for line in BASELINE_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split("\t")
        if len(parts) >= 2 and parts[1].strip().isdigit():
            out[parts[0].strip()] = int(parts[1])
    return out


def _write_baseline(counts: dict) -> None:
    """写基线（★只写计数，不改源码；登记纪律写在文件头）。"""
    lines = [
        "# _guard · 通用层纯净性基线（R-56 / g_purity）",
        "# 格式：<仓内相对路径>\t<允许命中数>；`#` 注释与空行跳过；未登记文件按 0 计。",
        "# 语义：**只防新增**——存量登记在此，新增文件或某文件超出即红。",
        "#",
        "# 判因纪律（抄 lingshu）：计数上升先判因——",  # purity-allow: 文档文本，非代码依赖
        "#   · 新增的**真依赖** ⇒ 改代码（路径挪进 tools/*.json、projects/*.yaml）；",
        "#   · 确属**夹具/样本**（负例库、测试输入） ⇒ 在**该行**加 `purity-allow: <理由>`",
        "#     （行级、显式、可 grep），**不要**为变绿抬高这个数。",
        "#",
        "# 首建读数（2026-10-15）：35 个通用层文件 / 23 处存量命中。",
        "# 已知待整改（**真依赖**，非夹具）——明细见",
        "#   `_guard/工程纪律_引入自lingshu_20261014.md` §「存量分类」：",  # purity-allow: 文档文本，非代码依赖
        "#   gates/g_xcheck.py（写死跨项目绝对路径）、gates/g_vision.py（写死本机凭据文件路径）",
        "",
    ]
    for f in sorted(counts):
        lines.append("%s\t%d" % (f, counts[f]))
    BASELINE_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

#: 扫描面：通用层代码（**显式列出**，不整棵树盲扫——_audit*/_bak*/_tmp* 是实验与备份）
GENERIC_FILES: list[Path] = sorted(
    [p for p in (GUARD / "gates").glob("*.py")] +
    [p for p in GUARD.glob("*.py")]
)

ALLOW_MARKER = "purity-allow"


def load_deny() -> list[tuple[str, re.Pattern]]:
    """读禁止清单：**中性默认件 + 本机覆盖**（两份并起来）。

    缺失或为空 ⇒ 抛错（不静默放行）——但判据里的"空"指**两份合起来**为空，
    这样公开仓（只有中性件）与开发机（中性件 + .local）都能跑。
    """
    rules: list[tuple[str, re.Pattern]] = []
    seen: set[str] = set()
    exists = False
    for p in (DENY_PATH, DENY_LOCAL):
        if not p.exists():
            continue
        exists = True
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or s in seen:
                continue
            seen.add(s)
            rules.append((s, re.compile(s)))
    if not exists:
        raise FileNotFoundError("禁止清单缺失：%s（fail-closed）" % DENY_PATH)
    if not rules:
        raise ValueError("禁止清单为空（%s）——审计面为空不得当绿" % DENY_PATH)
    return rules


def _docstring_positions(tree: ast.AST) -> set[tuple[int, int]]:
    """收集 docstring 节点位置（这些**不算代码**）。"""
    pos: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                pos.add((body[0].lineno, body[0].col_offset))
    return pos


def scan_file(path: Path) -> list[dict]:
    """只扫**代码**里的字符串字面量与 import 模块名。"""
    hits: list[dict] = []
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except (SyntaxError, OSError) as exc:
        return [{"file": str(path).replace("\\", "/"), "line": 0, "rule": "PARSE-FAIL",
                 "token": "", "note": "解析失败（审计面不完整，按红处理）：%r" % (exc,)}]
    lines = src.splitlines()
    doc_pos = _docstring_positions(tree)
    deny = load_deny()

    def _check(pos_lineno: int, col: int, token: str, kind: str) -> None:
        line_text = lines[pos_lineno - 1] if 0 < pos_lineno <= len(lines) else ""
        if ALLOW_MARKER in line_text:
            return
        for pat, rx in deny:
            if rx.search(token):
                hits.append({"file": str(path).replace("\\", "/"), "line": pos_lineno,
                             "rule": kind, "token": token[:80], "pattern": pat})

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if (node.lineno, node.col_offset) in doc_pos:
                continue                       # docstring：注明出处等文本，不算代码
            _check(node.lineno, node.col_offset, node.value, "STR")
        elif isinstance(node, ast.ImportFrom) and node.module:
            _check(node.lineno, node.col_offset, node.module, "IMPORT")
        elif isinstance(node, ast.Import):
            for al in node.names:
                _check(node.lineno, node.col_offset, al.name, "IMPORT")
    return hits


class PurityGate:
    """通用层纯净性闸。"""

    def __init__(self, cfg: ProjectConfig | None = None):
        self.cfg = cfg

    def check(self, files: list[Path] | None = None) -> dict:
        targets = list(files) if files else GENERIC_FILES
        try:
            load_deny()
        except (FileNotFoundError, ValueError) as exc:
            return {"ok": False, "gate": "g_purity", "scanned": 0, "hits": [],
                    "note": "禁止清单不可用（不当绿）：%r" % (exc,)}
        hits: list[dict] = []
        for f in targets:
            hits.extend(scan_file(f))
        baseline = _load_baseline()
        per_file: dict = {}
        for h in hits:
            per_file[h["file"]] = per_file.get(h["file"], 0) + 1
        over = {f: (n, baseline.get(f, 0)) for f, n in per_file.items()
                if n > baseline.get(f, 0)}
        return {"ok": not over, "gate": "g_purity", "scanned": len(targets),
                "hits": hits, "per_file": per_file, "over": over, "note": ""}

    def render(self, rep: dict) -> str:
        if rep.get("note"):
            return "通用层纯净性：%s" % rep["note"]
        if rep["ok"]:
            return "通用层纯净性：绿（扫 %d 个通用层文件，无超基线项）" % rep["scanned"]
        over = rep.get("over") or {}
        lines = ["通用层纯净性：红（扫 %d 个文件；超基线 %d 个文件 / 命中 %d 处）"
                 % (rep["scanned"], len(over), len(rep["hits"]))]
        for f, nb in sorted(over.items()):
            lines.append("  超基线：%s 命中 %d > 基线 %d" % (f, nb[0], nb[1]))
        for h in rep["hits"][:40]:
            if h.get("rule") == "PARSE-FAIL":
                lines.append("  %s  %s" % (h["file"], h["note"]))
            else:
                lines.append("  %s:%d  [%s] 命中 `%s`  ← %s"
                             % (h["file"], h["line"], h["rule"], h.get("pattern"),
                                (h.get("token") or "")[:60]))
        lines.append("  处置：把路径挪进配置（tools/*.json、projects/*.yaml），"
                     "代码里改用 HERE/GUARD 相对定位；确属必须 ⇒ 该行加 `purity-allow: <理由>`")
        return "\n".join(lines)


# ------------------------------------------------------------------ 负例自检
def demo(cfg: ProjectConfig | None = None) -> int:
    base = Path(tempfile.mkdtemp(prefix="purity_demo_"))
    try:
        ok = True
        f = base / "t.py"
        # ★ 夹具用**运行期拼接**造出项目名/路径 —— 本文件 AST 里不出现完整字面量，
        #   否则"扫描器扫自己"会自命中（那是噪声，不是依赖）。
        name = "lings" + "hu"
        proj_path = "D:" + "\\" + "agent" + "\\" + name

        # ① 代码里写死具体项目路径 ⇒ 必须红
        f.write_text('P = r"%s\\tools"\n' % proj_path, encoding="utf-8")
        h1 = scan_file(f)
        print("  ① 代码写死项目路径        -> 命中 %d（应 ≥1）" % len(h1))
        ok &= len(h1) >= 1

        # ② 同一处带 purity-allow ⇒ 放过
        f.write_text('P = r"%s\\tools"  # purity-allow: 兼容旧路径\n' % proj_path,
                     encoding="utf-8")
        h2 = scan_file(f)
        print("  ② 同行 purity-allow       -> 命中 %d（应 0）" % len(h2))
        ok &= len(h2) == 0

        # ③ docstring 里注明出处 ⇒ **不算代码**，不该命中（判据量对对象）
        f.write_text('"""本件移植自 %s（上游作者）。"""\nX = 1\n' % name, encoding="utf-8")
        h3 = scan_file(f)
        print("  ③ docstring 注明出处      -> 命中 %d（应 0）" % len(h3))
        ok &= len(h3) == 0

        # ④ 代码里字符串写项目名 / 动态 import ⇒ 必须红
        f.write_text('import importlib\nimportlib.import_module("%s.core")\n' % name,
                     encoding="utf-8")
        h4 = scan_file(f)
        print("  ④ 代码里字符串写项目名    -> 命中 %d（应 ≥1）" % len(h4))
        ok &= len(h4) >= 1

        print("  demo 结论：%s" % ("全过（闸真的会动）" if ok else "有负例未按预期——闸坏了"))
        return 0 if ok else 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _log(rec: dict) -> None:
    try:
        ledger = GUARD / "ledger"
        ledger.mkdir(exist_ok=True)
        with (ledger / "g_purity.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:                       # 记录失败不静默
        print("g_purity: 台账写入失败：%r" % (exc,), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="g_purity.py", description="通用层纯净性闸（R-56）")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--baseline", metavar="FILE", help="基线文件（默认 tools/purity_baseline.txt）")
    ap.add_argument("--update-baseline", action="store_true", help="把当前每文件命中数写回基线")
    a = ap.parse_args(argv)

    cfg = None
    if not a.demo:
        try:
            from guard import load as load_cfg
            cfg = load_cfg(a.project, a.project_root)
        except Exception as exc:
            print("g_purity: 项目配置加载失败，退到默认：%r" % (exc,), file=sys.stderr)

    if a.demo:
        rc = demo(cfg)
        _log({"demo": True, "ok": rc == 0})
        return rc

    if a.baseline:
        globals()["BASELINE_PATH"] = Path(a.baseline)
    gate = PurityGate(cfg)
    rep = gate.check()
    if a.update_baseline:
        _write_baseline(rep.get("per_file") or {})
        print("已写入基线：%s（%d 个文件）" % (BASELINE_PATH, len(rep.get("per_file") or {})))
        return 0
    _log({"ok": rep["ok"], "scanned": rep.get("scanned"), "hits": len(rep.get("hits", []))})
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, sort_keys=True))
    else:
        print(gate.render(rep))
        if not a.quiet:
            print("结论：%s" % ("绿" if rep["ok"] else "红（见上）"))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
