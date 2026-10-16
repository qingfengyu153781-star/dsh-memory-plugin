# -*- coding: utf-8 -*-
"""_guard · 静默宽 except 闸 g_silent —— 让「吞掉异常」可数、可拦、只防新增

## 治的哪条规矩（R-54，2026-10-14 立）

**R-54**：我们自己代码里的「静默宽 except」（`except Exception` / 裸 `except`，
且 handler 体内没有任何 `warn/log/print/raise/audit/error` 动作）**必须可数**，
且**只许减不许增**——存量登记进基线，新增即红。

## 为什么必须做成闸（这条是本工作区头号病的温床）

本工作区点名的头号病是「**静默失效**」：东西在跑、不抛错、日志也有，
但结果全错或全丢，而没人知道。判据是「**失败不改变任何可见状态**」——
而 `except Exception: pass` 正是它的最朴素形态。

已实证的两次：
  · 注意力编排器 **97/97 静默失败**，一整天零提示（2026-10-04 复盘）；
  · `dsh-rag-memory/bin/mem.py` 本次实测 **11 处**静默宽 except。

**在此之前没有任何东西在数它**：52 条规矩里没有一条能机械地把静默 except 列出来。
本闸把「可数」这一步机器化——不负责逐处定性（那要人判语义），
负责保证「**不会再变多**」，并把清单随时摆出来。

## 来源与移植

审计核 `tools/silent_except_lint.py` 移植自 **lingshu**（FuRongJun-1999）的
`tools/silent_except_lint.py`（MIT），保持**原样不动**（便于与上游 diff）；
判据口径 = 其 issue #170 统计脚本逐字（AST 数 `ast.ExceptHandler`）。
本仓的用法集中在 `tools/silent_scan_roots.json`（配置；R-56 起路径不进代码）。

## 判据（三态，不许含糊）

| 情形 | 判定 | 理由 |
|---|---|---|
| 某文件静默数 > 基线，或出现基线外新文件 | **红（退出 1）** | 新增静默吞错 |
| 扫描面里出现**解析失败**文件 | **红（退出 1）** | 审计面不完整 ⇒ 计数不可信（工具 v2 fail-closed） |
| 每文件静默数 ≤ 基线 | 绿（退出 0） | 只防新增 |

★「解析失败也算红」是刻意抄上游的 fail-closed 语义：否则新文件只要带上语法错，
就能整文件逃出审计而守卫恒绿（上游实测复现过这条）。

## 用法

    python _guard/gates/g_silent.py            # 跑全部登记根（不过则退 1）
    python _guard/gates/g_silent.py --json     # 确定性 JSON
    python _guard/gates/g_silent.py --demo     # 负例自检（造静默/加记录/语法错三种）
    python _guard/tools/silent_except_lint.py --repo-root <根> <面> --update-baseline
                                               # 需要动基线时，用上游工具本体跑

## 边界（如实登记）

- **不判语义**：只看"handler 体里有没有可检索的记录动作"（子串启发式）。
  把 `logging` 别名成 `lg.info(...)` 会被**少算**；handler 里恰好有个叫
  `errors` 的变量会被**多算**（上游已知局限 L1，照抄未改）。
- **不判异常类型够不够窄**：工具只数"有没有记录动作"，不判 `except Exception`
  是否该收窄（那要逐处人判）。
- **基线是快照，不是承诺**：计上升时先判因——新增的真缺陷 ⇒ 改代码；
  刻意静默且已写明理由 ⇒ 更新基线并在基线文件「登记面」追加一行带日期的记录，
  **不要只改数字**。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # _guard/gates
GUARD = HERE.parent                             # _guard
sys.path.insert(0, str(GUARD))
from guard import ProjectConfig  # noqa: E402

TOOLS = GUARD / "tools"
LINT_PATH = TOOLS / "silent_except_lint.py"

#: 扫描根**配置**（R-56 纪律：通用层不许写死具体项目路径 ⇒ 路径全部从这里读）。
#: 文件：`tools/silent_scan_roots.json`（绝对路径只允许出现在该配置里，不进代码）。
#: ★扫描面**显式列出**（不要把实验/备份目录扫进来——`_guard/_audit_lead/probe/g_fake.py`
#:   是刻意写坏的假闸件，扫它会触发 fail-closed 红，那是噪声不是缺陷）。
ROOTS_CONFIG = TOOLS / "silent_scan_roots.json"


def load_scan_roots() -> list[dict]:
    """从配置读扫描根清单（缺失/格式错 ⇒ 抛错，**不静默返回空**——空面会被误当绿）。"""
    if not ROOTS_CONFIG.exists():
        raise FileNotFoundError("扫描根配置缺失：%s（fail-closed）" % ROOTS_CONFIG)
    data = json.loads(ROOTS_CONFIG.read_text(encoding="utf-8"))
    return [
        {"name": r["name"], "root": Path(r["root"]),
         "paths": list(r["paths"]), "baseline": Path(r["baseline"])}
        for r in data.get("roots", [])
    ]


def _load_lint():
    """按路径加载审计核（文件名不是包内模块，故用 spec 加载）。"""
    if not LINT_PATH.exists():
        raise FileNotFoundError("审计核缺失：%s（g_silent 无法工作，fail-closed）" % LINT_PATH)
    spec = importlib.util.spec_from_file_location("silent_except_lint", LINT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def scan_root(cfg: dict) -> dict:
    """跑一个登记根的审计，返回结构化结果（不抛异常，失败即 ok=False）。"""
    out = {"name": cfg["name"], "root": str(cfg["root"]), "ok": False,
           "exit_code": None, "silent_total": None, "baseline_total": None,
           "over": [], "unparsable": [], "stale": [], "note": ""}
    root = Path(cfg["root"])
    if not root.is_dir():
        out["note"] = "扫描根不存在（未跳过——如实报，不当绿）"
        return out
    baseline = Path(cfg["baseline"])
    if not baseline.exists():
        out["note"] = "基线文件缺失：%s" % baseline
        return out
    try:
        lint = _load_lint()
    except Exception as exc:                       # pragma: no cover - 环境问题
        print("g_silent: 加载审计核失败（审计面不完整，不得当绿）：%r" % (exc,),
              file=sys.stderr)
        out["note"] = "加载审计核失败：%r" % (exc,)
        return out
    argv = (["--repo-root", str(root), "--baseline", str(baseline), "--json", "--quiet"]
            + list(cfg["paths"]))
    try:
        code, _text, payload = lint.run(argv)
    except SystemExit as exc:                      # argparse 出错
        out["note"] = "审计核用法错误（SystemExit %s）" % exc.code
        return out
    out["exit_code"] = code
    out["silent_total"] = payload.get("actual_total")
    out["baseline_total"] = payload.get("baseline_total")
    out["over"] = payload.get("new_over_baseline") or []
    out["unparsable"] = payload.get("unparsable") or []
    out["stale"] = payload.get("stale_baseline_entries") or []
    out["ok"] = (code == 0)
    if code == 2:
        out["note"] = "用法/路径错误（退出 2）——审计没跑成，不得当绿"
    return out


class SilentGate:
    """静默宽 except 闸。"""

    def __init__(self, cfg: ProjectConfig | None = None):
        self.cfg = cfg

    def check(self, roots: list | None = None) -> dict:
        roots = roots if roots is not None else load_scan_roots()
        results = [scan_root(c) for c in roots]
        bad = [r for r in results if not r["ok"]]
        return {
            "ok": not bad,
            "gate": "g_silent",
            "rule": "R-54 静默宽 except 只防新增（超基线 / 解析失败 ⇒ 红）",
            "results": results,
            "failing": [r["name"] for r in bad],
            "silent_total": sum(r["silent_total"] or 0 for r in results),
            "baseline_total": sum(r["baseline_total"] or 0 for r in results),
        }

    def render(self, rep: dict) -> str:
        lines = []
        for r in rep["results"]:
            mark = "绿" if r["ok"] else "红"
            lines.append("[%s] %s  静默 %s / 基线 %s" %
                         (mark, r["name"], r["silent_total"], r["baseline_total"]))
            for o in r["over"]:
                lines.append("     超出：%s 静默 %s > 基线 %s（超 %s）"
                             % (o["file"], o["silent"], o["baseline"], o["excess"]))
            for u in r["unparsable"]:
                lines.append("     解析失败（fail-closed）：%s" % u)
            if r["note"]:
                lines.append("     注：%s" % r["note"])
        return "\n".join(lines)


# ------------------------------------------------------------------ 负例自检
def demo(cfg: ProjectConfig | None = None) -> int:
    """造三种情形验闸真的会动：静默新增 / 补了记录动作 / 语法错文件。"""
    lint = _load_lint()
    base = Path(tempfile.mkdtemp(prefix="silent_demo_"))
    try:
        (base / "pkg").mkdir()
        src = base / "pkg" / "a.py"
        baseline = base / "baseline.txt"
        ok = True

        def ask() -> int:
            code, _t, _p = lint.run(["--repo-root", str(base),
                                     "--baseline", str(baseline), "--quiet", "--json",
                                     "pkg"])
            return code

        # ① 静默宽 except（无记录动作）⇒ 必须红
        src.write_text("def f():\n    try:\n        return 1\n"
                       "    except Exception:\n        pass\n", encoding="utf-8")
        baseline.write_text("# empty\n", encoding="utf-8")
        r1 = ask()
        print("  ① 静默 except（基线 0）      -> 退出 %d（应 1）" % r1)
        ok &= (r1 == 1)

        # ② 同一处补上记录动作 ⇒ 计数归 0 ⇒ 必须绿
        src.write_text("import logging\nlog = logging.getLogger(__name__)\n"
                       "def f():\n    try:\n        return 1\n"
                       "    except Exception:\n        log.warning('x')\n",
                       encoding="utf-8")
        r2 = ask()
        print("  ② 该处补 log.warning         -> 退出 %d（应 0）" % r2)
        ok &= (r2 == 0)

        # ③ 扫描面里出现解析失败文件 ⇒ fail-closed 必须红
        (base / "pkg" / "broken.py").write_text("def f(:\n    pass\n", encoding="utf-8")
        r3 = ask()
        print("  ③ 扫描面含语法错文件         -> 退出 %d（应 1，fail-closed）" % r3)
        ok &= (r3 == 1)

        # ④ 基线覆盖既有静默数 ⇒ 只防新增 ⇒ 必须绿
        (base / "pkg" / "broken.py").unlink()
        src.write_text("def f():\n    try:\n        return 1\n"
                       "    except Exception:\n        pass\n", encoding="utf-8")
        baseline.write_text("pkg/a.py\t1\n", encoding="utf-8")
        r4 = ask()
        print("  ④ 基线登记该处 = 1           -> 退出 %d（应 0，只防新增）" % r4)
        ok &= (r4 == 0)

        print("  demo 结论：%s" % ("全过（闸真的会动）" if ok else "有负例未按预期——闸坏了"))
        return 0 if ok else 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _log(rec: dict) -> None:
    try:
        ledger = GUARD / "ledger"
        ledger.mkdir(exist_ok=True)
        with (ledger / "g_silent.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:                       # 记录失败不静默：打到 stderr
        print("g_silent: 台账写入失败（审计记录丢失，需人工看一眼）：%r" % (exc,),
              file=sys.stderr)


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(prog="g_silent.py", description="静默宽 except 闸（R-54）")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true", help="负例自检")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    cfg = None
    if not args.demo:
        try:
            from guard import load as load_cfg      # 与其它闸同款入口（g_mem 亦此）
            cfg = load_cfg(args.project, args.project_root)
        except Exception as exc:
            print("g_silent: 项目配置加载失败，退到默认（不影响判据）：%r" % (exc,),
                  file=sys.stderr)
            cfg = None

    if args.demo:
        rc = demo(cfg)
        _log({"demo": True, "ok": rc == 0})
        return rc

    gate = SilentGate(cfg)
    rep = gate.check()
    text = gate.render(rep)
    _log({"ok": rep["ok"], "silent_total": rep["silent_total"],
          "baseline_total": rep["baseline_total"], "failing": rep["failing"]})
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, sort_keys=True))
    elif not args.quiet:
        print(text)
        print("结论：%s" % ("绿（无新增静默宽 except）" if rep["ok"] else "红（见上）"))
    else:
        print(text)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
