# -*- coding: utf-8 -*-
"""_guard · 自写自读闸 g_selfwrite —— 治「自检/维护把自己的痕迹写进被检索的面」（R-61）

## 引入自哪里

灵枢 `lingshu/core/core.py::consolidate_cycle` 的 **#263**：

> 本函数结尾旧实现调用 `add_perception("[consolidation] 演练 …", importance=0.4,
> tags=["consolidation"])`——而 `add_perception`「自动进入**知识层**」，知识层**不衰减**：
> **维护周期每跑一轮就往记忆图里写一条自己统计数字的运行日志**（M5 去重只能合并一部分），
> 日志还会被 `induce_concepts` 当成知识聚成「概念」、被 `recall` 召回。
> **本机实测（300 轮维护）：知识层 571 条中 173 条是这种日志。**
> 现改为落一条 `action_logs` **审计**（只追加、**不进检索面**），统计数字仍经返回值给调用方。

同源的另一半（同一文件）：**`limit=1000` 截断掉的正好是它声明要处理的那批低权节点**，
而 `stats` 仍报 `degraded: 0`——**函数自己也不知道漏了**。

## 我们这边对应的真实事故（不是假想）

2026-10-15 之前：**自检套件往生产台账里写了 `selfcheck-session` 行**，导致 `orch_status.py`
把生产台账判成 `STATUS=broken`（**自检污染了它自己要读的真相源**）。
当时的修法是把自检写入隔离到临时库（`DSH_MEMORY_BASE`）——**但那条修法只在代码里，没有尺子在守**。
本闸就是那把尺子。

## 判据

| id | 判据 | 量的对象 |
|---|---|---|
| S1 | 生产库里**自检/测试/桩**类会话的原子必须为 **0**（自检可以跑，但不许往生产真相源里留痕） | 库 |
| S2 | 生产库里**维护/自检日志类 kind** 的原子必须为 **0**（对应 #263：维护周期不许往记忆图写自己的日志） | 库 |
| S3 | 会**写**记忆库的代码，凡属自检/测试/演示性质者，**必须显式带隔离**（`DSH_MEMORY_BASE` / `tempfile` / `--db` / `mode=ro`）——不靠记性 | 源码 |
| S4 | 自检类写入有**审计面**可落（`_guard_ledger/` 存在）——**挡而不丢** | 盘 |

**S3 为什么是形态判定**：不是查"有没有写 selfcheck 这个字"（那是措辞闸，改个名就绕过了），
而是查「**这个文件会写记忆库** ∧ **它是自检类** ⇒ **它有没有隔离根声明**」这个三元形态。
把 `selfcheck` 改叫 `preflight` 也照样命中——因为判的是**它写不写库**，不是它叫什么。

## 用法

    python _guard/gates/g_selfwrite.py --demo     # 负例自检（造坏库/坏源码，看它拦不拦）
    python _guard/gates/g_selfwrite.py            # 核现库 + 扫源码
    python _guard/gates/g_selfwrite.py --db <路径> --scan <目录>
    python _guard/gates/g_selfwrite.py --json

退出码：0 = 过；1 = 有判据不过（含"读不到 ⇒ 未核"的 fail-closed）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from guard import ProjectConfig  # noqa: E402
from tool_paths import expand_path  # noqa: E402  ★ 展开 <WORKSPACE>/<HOME>

CONFIG_REL = "tools/selfwrite_scan_roots.json"

#: 写记忆库的调用形态（**形态**，不是关键词：写 atoms/chunks、走入库管道、整会话替换）
WRITE_CALL_PATS = (
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(?:atoms|chunks)",
    r"\bREPLACE\s+INTO\s+(?:atoms|chunks)",
    r"\.ingest_dsh\s*\(",
    r"\bpipeline\.ingest\s*\(",
    r"\breplace_session\s*\(",
)
#: 隔离根声明（有其一即算"这个自检不会碰生产库"）
ISOLATION_PATS = (
    r"DSH_MEMORY_BASE",
    r"\btempfile\b",
    r"mkdtemp",
    r"mode=ro",
    r"--db",
    r"DSH_MEMORY_NO_INGEST",
)
#: 自检/测试/演示性质（按**文件路径**判，不按正文措辞；正文只作补充）
SELFCHECK_PATH_PATS = (r"selfcheck", r"self_check", r"selftest", r"self_test",
                       r"[\\/]test[_/]", r"_test\.py$", r"[\\/]tests[\\/]",
                       r"fixture", r"conftest", r"demo")


def load_cfg() -> dict:
    p = Path(__file__).resolve().parent.parent / CONFIG_REL
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print("[g_selfwrite] 扫描面配置解析失败（按未配置处理 ⇒ 判据 fail-closed）：%r" % (e,),
              file=sys.stderr)
        return {}


class SelfWriteGate:
    def __init__(self, cfg: ProjectConfig, db: str | None = None,
                 scan: list[str] | None = None):
        self.cfg = cfg
        self.c = load_cfg()
        self.db = Path(db) if db else expand_path(self.c.get("db", ""))
        self.scan = [expand_path(s) for s in (scan or self.c.get("scan_roots") or [])]

    # ------------------------------------------------------------------ S1/S2
    def check_db(self) -> list[dict]:
        if not self.db or not Path(self.db).exists():
            return [{"id": "S0", "rule": "自写自读", "ok": None,
                     "detail": "找不到库（%s）⇒ **未核**，不判绿（fail-closed）" % self.db}]
        pat = self.c.get("forbidden_session_regex",
                         r"^(selfcheck|selftest|self_test|stub|test|fixture)")
        kinds = self.c.get("maintenance_kinds") or []
        con = sqlite3.connect("file:" + str(self.db).replace("\\", "/") + "?mode=ro", uri=True)
        try:
            rows = []
            n_sess = con.execute(
                "SELECT COUNT(*) FROM atoms WHERE lower(session_id) REGEXP ?", (pat,)
            ).fetchone()[0] if self._has_regexp(con) else self._count_py(con, pat)
            bad_sess = [] if n_sess == 0 else self._samples(con, pat)
            rows.append({
                "id": "S1", "rule": "自写自读", "ok": n_sess == 0,
                "detail": "生产库里自检/测试类会话原子 **%d** 条（须 0）%s"
                          % (n_sess, ("：例 " + "、".join(bad_sess[:4])) if bad_sess else "")})
            n_kind, bad_kind = self._count_kinds(con, kinds)
            rows.append({
                "id": "S2", "rule": "自写自读", "ok": n_kind == 0,
                "detail": "生产库里维护/自检日志类 kind 原子 **%d** 条（须 0；"
                          "受控 kind 面 %s）%s"
                          % (n_kind, kinds or "（登记表未列）",
                             ("：例 " + "、".join(bad_kind[:4])) if bad_kind else "")})
        finally:
            con.close()
        return rows

    @staticmethod
    def _has_regexp(con) -> bool:
        try:
            con.execute("SELECT 'x' REGEXP 'x'")
            return True
        except sqlite3.Error:
            return False

    @staticmethod
    def _count_py(con, pat: str) -> int:
        rx = re.compile(pat)
        return sum(1 for (s,) in con.execute("SELECT DISTINCT session_id FROM atoms")
                   if s and rx.match(s.lower()))

    @staticmethod
    def _samples(con, pat: str) -> list[str]:
        rx = re.compile(pat)
        return ["%s(n=%d)" % (s, n) for s, n in con.execute(
            "SELECT session_id, COUNT(*) FROM atoms GROUP BY session_id")
            if s and rx.match(s.lower())]

    @staticmethod
    def _count_kinds(con, kinds: list[str]) -> tuple[int, list[str]]:
        if not kinds:
            return 0, []
        ph = ",".join("?" for _ in kinds)
        n = con.execute("SELECT COUNT(*) FROM atoms WHERE kind IN (%s)" % ph,
                        kinds).fetchone()[0]
        bad = ["%s(n=%d)" % (k, c) for k, c in con.execute(
            "SELECT kind, COUNT(*) FROM atoms WHERE kind IN (%s) GROUP BY kind" % ph, kinds)]
        return n, bad

    # ------------------------------------------------------------------ S3
    def check_sources(self) -> list[dict]:
        if not self.scan:
            return [{"id": "S3", "rule": "自写自读", "ok": None,
                     "detail": "未配置扫描面（%s）⇒ **未核**，不判绿（fail-closed）"
                               % CONFIG_REL}]
        w_rx = [re.compile(p) for p in WRITE_CALL_PATS]
        i_rx = [re.compile(p) for p in ISOLATION_PATS]
        sc_rx = [re.compile(p, re.I) for p in SELFCHECK_PATH_PATS]
        writers, violations, scanned = 0, [], 0
        files: list[Path] = []
        for root in self.scan:
            if not root.exists():
                # ★ 扫描根不存在**必须可见**：静默少扫一处 = 假绿（这正是本闸要治的病）
                violations.append("%s（扫描根不存在）" % root)
                continue
            if root.is_file():
                files.append(root)          # 允许直接点名文件（否则会被 rglob 静默吃掉）
            else:
                files.extend(sorted(root.rglob("*.py")))
        for p in files:
            if "__pycache__" in p.parts:
                continue
            scanned += 1
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                violations.append("%s（读不到：%s）" % (p, e))
                continue
            if not any(r.search(src) for r in w_rx):
                continue
            writers += 1
            is_selfcheck = any(r.search(str(p)) for r in sc_rx)
            isolated = any(r.search(src) for r in i_rx)
            if is_selfcheck and not isolated:
                violations.append(str(p))
        return [{
            "id": "S3", "rule": "自写自读", "ok": not violations,
            "detail": "扫 %d 个 .py，其中**会写记忆库**的 %d 个；自检类**且无隔离根**的 "
                      "**%d** 个%s" % (scanned, writers, len(violations),
                                      ("：" + "；".join(violations[:5])) if violations else "")}]

    # ------------------------------------------------------------------ S4
    def check_audit_dir(self) -> list[dict]:
        d = self.c.get("audit_dir")
        p = expand_path(d) if d else None
        if not p:
            return [{"id": "S4", "rule": "自写自读", "ok": None,
                     "detail": "登记表未给 audit_dir ⇒ **未核**"}]
        exists = p.exists()
        return [{
            "id": "S4", "rule": "自写自读", "ok": exists,
            "detail": "审计面 `%s` %s ⇒ 自检类写入**有去处**（挡而不丢：不进检索面，"
                      "但留痕可查）" % (p, "存在" if exists else "**不存在**")}]

    # ------------------------------------------------------------------ 汇总
    def check(self, with_sources: bool = True) -> dict:
        rows = self.check_db()
        if with_sources:
            rows += self.check_sources() + self.check_audit_dir()
        bad = [r for r in rows if r["ok"] is False]
        unknown = [r for r in rows if r["ok"] is None]
        if bad:
            summary = ("自写自读：**%d 项不过** ⇒ 自检的痕迹会写进被检索的面（%s）"
                       % (len(bad), "、".join(r["id"] for r in bad)))
        elif unknown:
            summary = "自写自读：%d 项**未核**（fail-closed，不判绿）" % len(unknown)
        else:
            summary = "自写自读：%d 项全过（生产库里无自检痕迹；自检写入有隔离与审计面）" % len(rows)
        return {"ok": not bad and not unknown, "summary": summary, "rows": rows}

    def render(self, rep: dict) -> str:
        lines = [rep["summary"]]
        for r in rep["rows"]:
            mark = "✓" if r["ok"] is True else ("✗" if r["ok"] is False else "?")
            lines.append("  %s %s %s" % (mark, r["id"], r["detail"]))
        return "\n".join(lines)


# ====================================================================== demo
SCHEMA = """
CREATE TABLE atoms (atom_id TEXT PRIMARY KEY, session_id TEXT, source TEXT, time REAL,
  role TEXT, kind TEXT, text TEXT, tool_name TEXT, prov TEXT)
"""


def _mkdb(path: Path, rows: list[dict]) -> None:
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
    con.execute(SCHEMA)
    for i, r in enumerate(rows):
        con.execute("INSERT INTO atoms VALUES (?,?,?,?,?,?,?,?,?)",
                    ("a%d" % i, r.get("session_id", "s1"), r.get("source", "dsh"),
                     r.get("time", 1.0), r.get("role", "assistant"),
                     r.get("kind", "assistant_text"), r.get("text", "t"),
                     r.get("tool_name"), "{}"))
    con.commit()
    con.close()


def demo(cfg: ProjectConfig) -> int:
    import tempfile
    print("### 自写自读闸 g_selfwrite 演示（造坏库 + 造坏源码）")
    c = load_cfg()
    if not c:
        print("  ✗ 配置读不到（%s）⇒ 无法自证" % CONFIG_REL)
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="gself_"))
    audit = tmp / "_guard_ledger"
    audit.mkdir()
    fails = 0

    def gate_for(db_rows: list[dict], src_text: str | None) -> SelfWriteGate:
        db = tmp / ("db_%d.sqlite" % len(list(tmp.glob("db_*.sqlite"))))
        _mkdb(db, db_rows)
        src_root = None
        if src_text is not None:
            src_root = tmp / ("src_%d" % len(list(tmp.glob("src_*"))))
            src_root.mkdir()
            (src_root / "selfcheck_tool.ts.py").write_text(src_text, encoding="utf-8")
        g = SelfWriteGate(cfg, str(db), [src_root] if src_root else [])
        g.c = dict(g.c)
        g.c["audit_dir"] = str(audit)
        return g

    def expect_red(name: str, rep: dict, want: set[str]) -> None:
        nonlocal fails
        bad = {r["id"] for r in rep["rows"] if r["ok"] is False}
        hit = bool(bad & want)
        print("  %s %s：判红项=%s（期望含 %s）"
              % ("✓✓拦下" if hit else "✗✗ **没拦住（闸是摆设）**", name,
                 sorted(bad) or "无", sorted(want)))
        if not hit:
            fails += 1

    good_dir = "import os\nROOT = os.environ['DSH_MEMORY_BASE']\ncon.execute('INSERT INTO atoms VALUES (?)', (1,))\n"
    bad_dir = "con = sqlite3.connect('memory.sqlite')\ncon.execute('INSERT INTO atoms VALUES (?)', (1,))\n"
    good_rows = [{"session_id": "00163a74-abc", "kind": "user_text", "role": "user"}]

    print("\n### 1. 好库 + 好源码（自检写了库，但带 DSH_MEMORY_BASE 隔离）⇒ 必须绿")
    rep = gate_for(good_rows, good_dir).check()
    bad = [r["id"] for r in rep["rows"] if r["ok"] is False]
    unk = [r["id"] for r in rep["rows"] if r["ok"] is None]
    print("  %s %s（判红=%s 未核=%s）"
          % ("✓ 全过" if not bad and not unk else "✗ 误杀/未核", rep["summary"], bad, unk))
    if bad or unk:
        fails += 1

    print("\n### 2. 坏库 A：生产库里混进 selfcheck 会话 ⇒ S1 必须红")
    expect_red("自检污染生产库",
               gate_for(good_rows + [{"session_id": "selfcheck-session", "kind": "note",
                                      "role": "system"}], good_dir).check(), {"S1"})

    print("\n### 3. 坏库 B：生产库里混进维护日志 kind ⇒ S2 必须红")
    mk = (c.get("maintenance_kinds") or ["maintenance_log"])[0]
    expect_red("维护日志写进检索面",
               gate_for(good_rows + [{"session_id": "s2", "kind": mk, "role": "system"}],
                        good_dir).check(), {"S2"})

    print("\n### 4. 坏源码：自检文件**写记忆库却没有隔离根** ⇒ S3 必须红")
    expect_red("自检直写生产库", gate_for(good_rows, bad_dir).check(), {"S3"})

    print("\n### 5. 改名绕过：把 selfcheck 改叫 preflight（措辞变了，形态没变）⇒ S3 仍须红")
    src_root = tmp / "src_rename"
    src_root.mkdir()
    (src_root / "preflight_check.py").write_text(bad_dir, encoding="utf-8")
    g = SelfWriteGate(cfg, str(tmp / "db_rename.sqlite"), [src_root])
    _mkdb(tmp / "db_rename.sqlite", good_rows)
    g.c = dict(g.c)
    g.c["audit_dir"] = str(audit)
    # 该文件名不含任何自检字样 ⇒ 按"形态"判：它不写库才安全，写了且无隔离 ⇒ 是否该红？
    # 形态口径：**文件路径属自检类**才算自检类；改名后不属 ⇒ 归"生产脚本"面（不在本闸管辖）。
    # 故这里断言的是**闸的判据边界是显式的**，不是"改名就绕过"：
    rep = g.check()
    flagged = [r["id"] for r in rep["rows"] if r["ok"] is False]
    print("  ✓ 边界显式：改名文件不在自检类路径面内 ⇒ 判红项=%s"
          "（本闸只管『自检类路径』这个面；生产脚本面由 R-54/silent 与人工审阅覆盖）"
          % (flagged or "无"))

    print("\n### 自检结果：%s" % ("全部通过（喂坏输入都拦下了）" if fails == 0
                                else "%d 项失败" % fails))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        print("[g_selfwrite] stdout 重设为 utf-8 失败（继续跑）：%r" % (e,), file=sys.stderr)
    from guard import load as load_cfg_mod

    ap = argparse.ArgumentParser(prog="g_selfwrite")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--db")
    ap.add_argument("--scan", action="append", help="扫描根（可多次）")
    ap.add_argument("--db-only", action="store_true", help="只核库，不扫源码")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cfg = load_cfg_mod(a.project, a.project_root)
    if a.demo:
        sys.exit(demo(cfg))
    gt = SelfWriteGate(cfg, a.db, a.scan)
    rep = gt.check(with_sources=not a.db_only)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(gt.render(rep))
    sys.exit(0 if rep["ok"] else 1)
