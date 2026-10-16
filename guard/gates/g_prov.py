# -*- coding: utf-8 -*-
"""_guard · 来源闸 g_prov —— 把灵枢的「来源六问」落成可跑的形态判据（R-60）

## 引入自哪里（不是我想的，是灵枢踩出来的）

灵枢 `lingshu/core/provenance.py` 的动机原话：

> `session_other` / `other` 里**混进了用户本人的真实对话**——因为写入时没有记录
> 「谁 / 在什么工具下 / 哪个会话 / 什么时间 / 验证过没有」，事后只能靠
> `tags LIKE '%dsh%'` 这类字符串猜类别，**猜错就归错桶**。

灵枢另有一条 `#217`：**来源类保留标签只能由创建时的可信路径声明，不得由后续
快照并入既有节点**（否则 `external` 知识被洗成 `user`）。

## 判据（形态判定，不靠措辞）

| id | 判据 | 量的对象 |
|---|---|---|
| P1 | 库中每个 `(source, kind, role)` 组合**都必须在登记表里显式声明**（未声明即红） | 库 × 登记表 |
| P2 | **来源洗白**双向：非真人 kind 不得落真人角色；真人 kind 必须落真人角色 | 库 |
| P3 | **声明与事实一致**（双向）：声明「提供」⇒ 缺口不超基线（**只防新增**）；声明「代理」⇒ 须写明差在哪；声明「不提供」⇒ 须写明原因，且库里确有高覆盖列时判红；**声明「排除某源」⇒ 该源在库里的缺口必须确实接近满缺**（否则排除声明过期 ⇒ 红）；另有 `mode=presence`：该列必须**每行都有显式取值**（缺口 0） | 库 × 登记表 |
| P4 | `prov` 列的**语义声明**必须与库中实际键一致 | 库 × 登记表 |
| P5 | 读数（非判据）：已核验原子数（`verify_status<>'unverified'`）与核验方法面 | 库 |

**为什么 P3 要"双向"**：只查"声明提供⇒覆盖率达标"的话，**把问项改成"不提供"就能绿**
——那是假根治。所以"不提供"要付代价：写原因、且不许与事实相反。
同理"排除某源"也要付代价：**被排除的源必须真的大量缺**，否则就是拿排除来放水。

**为什么按 `(source, kind, role)` 而不用 `(kind, role)`**：同一个 kind 在不同来源里
role 不同（实测 `dsh.tool_call=tool` / `claude_code.tool_call=assistant`、
`claude_code.assistant_text=system`）。只登记 kind 粒度会**看不见某个来源新学了一套口径**
——那就正是"静默放行新 kind"换了个马甲。

**为什么默认不做文本判据**：这道闸量的对象是**库**，不是文本。
写一段"我已核来源"的话不该让它变绿——那正是用户明令禁止的「按措辞决定要不要查」。

## 用法

    python _guard/gates/g_prov.py --demo            # 负例自检（造 9 个库，看它真拦不拦）
    python _guard/gates/g_prov.py                   # 核现库（全量）
    python _guard/gates/g_prov.py --incremental     # 只核上次之后的新原子（P1/P2）
    python _guard/gates/g_prov.py --db <路径> --json

退出码：0 = 过；1 = 有判据不过（含"读不到 ⇒ 未核"的 fail-closed）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from guard import ProjectConfig  # noqa: E402
from tool_paths import expand_path  # noqa: E402  ★ 展开 <WORKSPACE>/<HOME>

REGISTRY_REL = "tools/prov_registry.json"
STATE_REL = "_guard_ledger/prov_state.json"

QUESTIONS = (
    ("q1_who", "谁说的"),
    ("q2_position", "在哪观测"),
    ("q3_tool", "用什么工具"),
    ("q4_session", "哪个会话"),
    ("q5_time", "什么时候"),
    ("q6_verified", "验证过没有"),
)


def load_registry() -> dict:
    p = Path(__file__).resolve().parent.parent / REGISTRY_REL
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        # ★ 不静默：登记表坏了却当"没登记"跑下去 = 又一处"失败不改变可见状态"
        print("[g_prov] 登记表解析失败（按未登记处理 ⇒ 判据 fail-closed）：%r" % (e,),
              file=sys.stderr)
        return {}


def load_state() -> dict:
    p = Path(__file__).resolve().parent.parent / STATE_REL
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception as e:  # noqa: BLE001
        print("[g_prov] 增量状态读不出（按全量处理）：%r" % (e,), file=sys.stderr)
        return {}


def save_state(d: dict) -> None:
    p = Path(__file__).resolve().parent.parent / STATE_REL
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print("[g_prov] 增量状态写不下（下次仍是全量）：%r" % (e,), file=sys.stderr)


class ProvGate:
    def __init__(self, cfg: ProjectConfig, db: str | None = None):
        self.cfg = cfg
        self.reg = load_registry()
        self.db = Path(db) if db else expand_path(self.reg.get("db", ""))

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _combos(con, since: int | None = None) -> list[tuple]:
        where = " WHERE atom_id > ?" if since else ""
        args = (since,) if since else ()
        return [tuple(r) for r in con.execute(
            "SELECT source, kind, role, COUNT(*) FROM atoms" + where +
            " GROUP BY source, kind, role ORDER BY 4 DESC", args)]

    @staticmethod
    def _max_atom_id(con) -> int:
        return int(con.execute("SELECT COALESCE(MAX(atom_id),0) FROM atoms").fetchone()[0])

    @staticmethod
    def _gap(con, spec: dict, since: int | None = None) -> tuple[int, int]:
        """返回 (缺口条数, 作用域总数)。支持 applies_to_kinds / applies_to_sources / join。"""
        where, args = [], []
        if spec.get("applies_to_kinds"):
            where.append("kind IN (%s)" % ",".join("?" for _ in spec["applies_to_kinds"]))
            args += list(spec["applies_to_kinds"])
        if spec.get("applies_to_sources"):
            where.append("source IN (%s)" % ",".join("?" for _ in spec["applies_to_sources"]))
            args += list(spec["applies_to_sources"])
        if since:
            where.append("atom_id > ?")
            args.append(since)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        tot = con.execute("SELECT COUNT(*) FROM atoms" + w, args).fetchone()[0]
        exprs = []
        for col in spec.get("columns") or []:
            exprs.append("%s IS NOT NULL AND trim(CAST(%s AS TEXT)) NOT IN "
                         "('','null','{}','[]','None')" % (col, col))
        if not exprs:
            return tot, tot
        n = con.execute("SELECT COUNT(*) FROM atoms%s%s(%s)"
                        % (w, " AND " if w else " WHERE ", " AND ".join(exprs)),
                        args).fetchone()[0]
        if n:
            pass
        # 多列时取"最差"的一列（缺口最大）——避免"有一列填了就报达标"
        gaps = []
        for e in exprs:
            gaps.append(tot - con.execute(
                "SELECT COUNT(*) FROM atoms%s%s%s"
                % (w, " AND " if w else " WHERE ", e), args).fetchone()[0])
        return (max(gaps) if gaps else 0), tot

    # ------------------------------------------------------------------ P1
    def check_mapping(self, con, since: int | None = None) -> list[dict]:
        by_src = self.reg.get("source_kind_role_map") or {}
        flat = self.reg.get("kind_role_map") or {}
        undeclared = []
        n_src = 0
        for source, kind, role, n in self._combos(con, since):
            if source in by_src:
                n_src += 1
                allowed = (by_src[source] or {}).get(kind) or []
            else:
                allowed = flat.get(kind) or []
            if role not in allowed:
                undeclared.append("%s/%s+%s(n=%d)" % (source, kind, role, n))
        scope = "增量" if since else "全量"
        return [{
            "id": "P1", "rule": "来源契约（六问）", "ok": not undeclared,
            "detail": "%s：未显式声明的 (source/kind→role) 组合 **%d** 个%s；"
                      "已登记来源 %d 个 / 兜底 kind %d 个"
                      % (scope, len(undeclared),
                         ("：" + "、".join(undeclared[:6])) if undeclared else "",
                         len(by_src), len(flat)),
        }]

    # ------------------------------------------------------------------ P2
    def check_laundering(self, con, since: int | None = None) -> list[dict]:
        human = set(self.reg.get("human_kinds") or [])
        role_h = self.reg.get("human_role", "user")
        into_human, out_of_human = [], []
        for source, kind, role, n in self._combos(con, since):
            if role == role_h and kind not in human:
                into_human.append("%s/%s(n=%d)" % (source, kind, n))
            if kind in human and role != role_h:
                out_of_human.append("%s/%s+%s(n=%d)" % (source, kind, role, n))
        return [{
            "id": "P2", "rule": "来源契约（六问）",
            "ok": not into_human and not out_of_human,
            "detail": "洗白面：非真人 kind 落 `%s` 角色 **%d** 个%s；真人 kind 非 `%s` 角色 "
                      "**%d** 个%s（真人 kind 白名单 %s）"
                      % (role_h, len(into_human),
                         ("：" + "、".join(into_human[:4])) if into_human else "",
                         role_h, len(out_of_human),
                         ("：" + "、".join(out_of_human[:4])) if out_of_human else "",
                         sorted(human)),
        }]

    # ------------------------------------------------------------------ P3
    def check_questions(self, con) -> list[dict]:
        qs = self.reg.get("questions") or {}
        below, lying, no_reason, stale_excl = [], [], [], []
        note_rows = []
        n_true = n_proxy = n_false = 0
        for qid, label in QUESTIONS:
            spec = qs.get(qid) or {}
            provided = spec.get("provided")
            if provided in (True, "proxy"):
                base = int(spec.get("baseline_gap", 0))
                worst = tot = 0
                try:
                    worst, tot = self._gap(con, spec)
                except sqlite3.Error as e:
                    below.append("%s（读不到：%s）" % (label, e))
                if provided == "proxy":
                    n_proxy += 1
                    if len((spec.get("proxy_note") or "").strip()) < 8:
                        no_reason.append("%s（代理但未写明差在哪）" % label)
                else:
                    n_true += 1
                # ★ 排除面：声明"某源本就不提供"必须与现实相符
                for src, why in (spec.get("excluded_sources") or {}).items():
                    if len(str(why).strip()) < 8:
                        no_reason.append("%s（排除 %s 但没写原因）" % (label, src))
                    gap_s, tot_s = self._gap(con, dict(spec, applies_to_sources=[src]))
                    if tot_s and (tot_s - gap_s) / tot_s > 0.10:
                        stale_excl.append("%s（声明排除 %s，但该源 %d/%d 有值 ⇒ 排除过期）"
                                          % (label, src, tot_s - gap_s, tot_s))
                if spec.get("mode") == "presence":
                    # 该列必须每行都有显式取值（缺口 0）——"未知"要显式，不是留空
                    if worst > 0:
                        below.append("%s：presence 模式要求缺口 0，实测缺口 %d" % (label, worst))
                elif worst > base:
                    below.append("%s：缺口 %d > 基线 %d" % (label, worst, base))
                # ★ presence_columns：附加"必须显式"的列（与基线缺口并存，不是二选一）
                #   例：q5 的 time 允许 410 条存量缺口，但 `time_basis` **一条都不许空**
                #   ——"我们没读到"与"源头没有"必须能分开。
                for pcol in spec.get("presence_columns") or []:
                    try:
                        pgap, _ptot = self._gap(con, dict(spec, columns=[pcol],
                                                          mode=None,
                                                          presence_columns=[]))
                    except sqlite3.Error as e:
                        below.append("%s（%s 读不到：%s）" % (label, pcol, e))
                        continue
                    if pgap > 0:
                        below.append("%s：`%s` 必须显式（presence），实测缺口 %d"
                                     % (label, pcol, pgap))
                note_rows.append("%s %s：缺口 %d/基线 %d%s"
                                 % (label, "（代理）" if provided == "proxy" else "",
                                    worst, base,
                                    "；" + spec["proxy_note"] if spec.get("proxy_note") else ""))
            elif provided is False:
                n_false += 1
                reason = (spec.get("reason") or "").strip()
                if len(reason) < 8:
                    no_reason.append("%s（声明不提供但没写原因）" % label)
                for col in spec.get("probe_columns") or []:
                    try:
                        gap, tot = self._gap(con, dict(spec, columns=[col]))
                    except sqlite3.Error:
                        continue
                    if tot and (tot - gap) / tot >= 0.5:
                        lying.append("%s（声明不提供，但 %s 覆盖 %d/%d）"
                                     % (label, col, tot - gap, tot))
                note_rows.append("%s **不提供**：%s" % (label, reason or "（未写原因）"))
            else:
                no_reason.append("%s（未声明 provided）" % label)
                note_rows.append("%s 未声明" % label)
        ok = not below and not lying and not no_reason and not stale_excl
        return [
            {"id": "P3", "rule": "来源契约（六问）", "ok": ok,
             "detail": "六问：提供 %d · 代理 %d · 不提供 %d；**缺口超基线 %d**%s；"
                       "**声明与事实相反 %d**%s；**排除声明过期 %d**%s；缺原因/未声明 %d%s"
                       % (n_true, n_proxy, n_false,
                          len(below), ("：" + "；".join(below)) if below else "",
                          len(lying), ("：" + "；".join(lying)) if lying else "",
                          len(stale_excl), ("：" + "；".join(stale_excl)) if stale_excl else "",
                          len(no_reason),
                          ("：" + "；".join(no_reason)) if no_reason else "")},
            {"id": "P3.1", "rule": "来源契约（六问）", "ok": True,
             "detail": "逐问读数：" + " ｜ ".join(note_rows)},
        ]

    # ------------------------------------------------------------------ P4
    def check_prov_semantics(self, con) -> list[dict]:
        declared = (self.reg.get("prov_column_semantics") or {}).get("declared")
        keys: dict[str, int] = {}
        try:
            for (p,) in con.execute("SELECT DISTINCT prov FROM atoms LIMIT 500"):
                try:
                    for k in json.loads(p or "{}"):
                        keys[k] = keys.get(k, 0) + 1
                except Exception:  # noqa: BLE001
                    print("[g_prov] prov 行解析不了（该行的键不参与 P4 判据）：%.120r"
                          % (p,), file=sys.stderr)
                    keys["<unparsable>"] = keys.get("<unparsable>", 0) + 1
        except sqlite3.Error as e:
            return [{"id": "P4", "rule": "来源契约（六问）", "ok": None,
                     "detail": "读不到 prov 列（%r）⇒ **未核**，不判绿（fail-closed）" % (e,)}]
        verify_words = {"verify_status", "verify_methods", "verify_ref", "verified",
                        "observation_position", "observation_tool"}
        actual_verify = sorted(set(keys) & verify_words)
        if declared == "file_origin":
            ok = not actual_verify
            detail = ("`prov` 声明为**文件出处**，实测键=%s ⇒ %s"
                      % (sorted(keys), "一致" if ok
                         else "**混进验证/观测类键 %s ⇒ 声明与事实不一致**" % actual_verify))
        elif declared == "provenance_6q":
            need = {"source", "observation_position", "observation_tool",
                    "session_id", "time_window", "verify_status"}
            missing = sorted(need - set(keys))
            ok = not missing
            detail = ("`prov` 声明为**来源六问**，实测键=%s，缺 %s ⇒ %s"
                      % (sorted(keys), missing, "一致" if ok else "声明与事实不一致"))
        else:
            ok = False
            detail = ("`prov` 语义**未声明**（登记表 prov_column_semantics.declared ∈ "
                      "{file_origin, provenance_6q}）——名字叫 prov 却不定语义，"
                      "就是「名字对、语义不对」的温床")
        return [{"id": "P4", "rule": "来源契约（六问）", "ok": ok, "detail": detail}]

    # ------------------------------------------------------------------ P5（读数）
    def check_verified(self, con) -> list[dict]:
        try:
            n = con.execute("SELECT COUNT(*) FROM atoms WHERE verify_status IS NOT NULL "
                            "AND verify_status<>'unverified'").fetchone()[0]
            pend = con.execute("SELECT COUNT(*) FROM atoms WHERE verify_status IS NULL "
                               "OR verify_status='unverified'").fetchone()[0]
            methods = [r for r in con.execute(
                "SELECT verify_method, COUNT(*) FROM atoms WHERE verify_method IS NOT NULL "
                "GROUP BY 1 ORDER BY 2 DESC")]
        except sqlite3.Error as e:
            return [{"id": "P5", "rule": "来源契约（六问）", "ok": None,
                     "detail": "读不到 verify_* 列（%r）⇒ **未核**" % (e,)}]
        # ★ 未核验数**必须显式报**：核验是**后来重开原档**才做的事，不是入库时盖章
        #   （入库时盖章＝拿抽取器自己的产物证明自己，**证不了任何事**＝假通过）。
        #   所以"新原子默认未核验"是设计，不是缺陷；但**数量要一直看得见**。
        #   同时挂到 self 上，供上层（hook 的自动核验兜底）判断"值不值得跑一批"。
        self.pending = pend
        hint = ("" if pend == 0 else
                "；未核验 **%d** 条（新入库，待下次 `mem.py verify-atoms` 批次）" % pend)
        return [{"id": "P5", "rule": "来源契约（六问）", "ok": True,
                 "detail": "已核验 **%d** 条%s（「unverified」是**显式**状态，"
                           "不等于「已验证」——两码事，别混）%s"
                           % (n, ("：" + "、".join("%s=%d" % (m, c) for m, c in methods))
                              if methods else "", hint)}]

    # ------------------------------------------------------------------ 汇总
    def check(self, incremental: bool = False) -> dict:
        if not self.db or not Path(self.db).exists():
            return {"ok": False, "rows": [{
                "id": "P0", "rule": "来源契约（六问）", "ok": None,
                "detail": "找不到库（%s）⇒ **未核**，不判绿（fail-closed）" % self.db}],
                "summary": "来源契约（六问）：库读不到 ⇒ 未核（fail-closed，不判绿）"}
        con = sqlite3.connect("file:" + str(self.db).replace("\\", "/") + "?mode=ro", uri=True)
        try:
            since = None
            if incremental:
                since = int(load_state().get("last_atom_id") or 0)
            top = self._max_atom_id(con)
            rows = self.check_mapping(con, since) + self.check_laundering(con, since)
            if not incremental:
                rows += self.check_questions(con) + self.check_prov_semantics(con)
            rows += self.check_verified(con)
        finally:
            con.close()
        if not incremental:
            save_state({"last_atom_id": top, "at": time.strftime("%Y-%m-%d %H:%M:%S")})
        bad = [r for r in rows if r["ok"] is False]
        unknown = [r for r in rows if r["ok"] is None]
        if bad:
            summary = ("来源契约（六问）：**%d 项不过** ⇒ 来源面不可信（%s）"
                       % (len(bad), "、".join(r["id"] for r in bad)))
        elif unknown:
            summary = "来源契约（六问）：%d 项**未核**（fail-closed，不判绿）" % len(unknown)
        else:
            qs = self.reg.get("questions") or {}
            n_true = sum(1 for qid, _l in QUESTIONS
                         if (qs.get(qid) or {}).get("provided") is True)
            n_proxy = sum(1 for qid, _l in QUESTIONS
                          if (qs.get(qid) or {}).get("provided") == "proxy")
            n_false = sum(1 for qid, _l in QUESTIONS
                          if (qs.get(qid) or {}).get("provided") is False)
            summary = ("来源契约（六问）%s：%d 项全过（提供 %d · 代理 %d · 不提供 %d）"
                       % ("·增量" if incremental else "", len(rows),
                          n_true, n_proxy, n_false))
        return {"ok": not bad and not unknown, "summary": summary, "rows": rows,
                "incremental": incremental, "state": {"last_atom_id": top},
                "pending_unverified": getattr(self, "pending", None)}

    def render(self, rep: dict) -> str:
        lines = [rep["summary"]]
        for r in rep["rows"]:
            mark = "✓" if r["ok"] is True else ("✗" if r["ok"] is False else "?")
            lines.append("  %s %s %s" % (mark, r["id"], r["detail"]))
        return "\n".join(lines)


# ====================================================================== demo
SCHEMA = """
CREATE TABLE atoms (
  atom_id INTEGER PRIMARY KEY, session_id TEXT, source TEXT, seq INTEGER, time REAL,
  turn INTEGER, step INTEGER, role TEXT, kind TEXT, text TEXT, text_hash TEXT,
  tool_name TEXT, call_id TEXT, note TEXT, prov TEXT, extra_from_version INTEGER,
  observation_position TEXT, verify_status TEXT DEFAULT 'unverified',
  verify_method TEXT, verify_ref TEXT, verified_at INTEGER, time_basis TEXT);
CREATE TABLE sessions (session_id TEXT PRIMARY KEY, source TEXT, origin TEXT, cwd TEXT,
  dir TEXT);
"""


def _mkdb(path: Path, rows: list[dict], sessions: list[dict] | None = None) -> None:
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    for s in sessions or []:
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?)",
                    (s["session_id"], s.get("source"), s.get("origin"), s.get("cwd"),
                     s.get("dir")))
    for i, r in enumerate(rows):
        con.execute(
            "INSERT INTO atoms (atom_id, session_id, source, time, role, kind, text, "
            "tool_name, prov, observation_position, verify_status, time_basis) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (i + 1, r.get("session_id", "s1"), r.get("source", "dsh"),
             r.get("time", 1.0), r.get("role", "assistant"),
             r.get("kind", "assistant_text"), r.get("text", "t"),
             r.get("tool_name"), json.dumps(r.get("prov", {"file": "f.jsonl", "line": 1})),
             r.get("observation_position", "dsh·user·<WORKSPACE>"),
             r.get("verify_status", "unverified"),
             r.get("time_basis", "record")))
    con.commit()
    con.close()


def _good_rows() -> list[dict]:
    return [
        {"kind": "user_text", "role": "user"},
        {"kind": "plugin_injection", "role": "plugin"},
        {"kind": "tool_call", "role": "tool", "tool_name": "read"},
        {"kind": "tool_result", "role": "tool", "tool_name": "read"},
    ]


def _patch(reg: dict, **changes) -> dict:
    out = json.loads(json.dumps(reg))
    for path, val in changes.items():
        d = out
        parts = path.split(".")
        for p in parts[:-1]:
            d = d[p]
        d[parts[-1]] = val
    return out


def demo(cfg: ProjectConfig) -> int:
    """负例自检：**造坏库**，看闸真拦不拦（不靠"看代码像对的"）。"""
    import tempfile
    print("### 来源闸 g_prov 演示（造库自证）")
    reg = load_registry()
    if not reg:
        print("  ✗ 登记表读不到（%s）⇒ 无法自证" % REGISTRY_REL)
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="gprov_"))
    fails = 0

    def run(label: str, rows: list[dict], reg_patch: dict | None = None) -> dict:
        db = tmp / (label + ".sqlite")
        _mkdb(db, rows, [{"session_id": "s1", "source": "dsh", "origin": "user",
                          "cwd": "<WORKSPACE>"}])
        gate = ProvGate(cfg, str(db))
        if reg_patch:
            gate.reg = reg_patch
        return gate.check()

    def expect_red(name: str, rep: dict, want_ids: set[str]) -> None:
        nonlocal fails
        bad = {r["id"] for r in rep["rows"] if r["ok"] is False}
        hit = bool(bad & want_ids)
        print("  %s %s：判红项=%s（期望含 %s）"
              % ("✓✓拦下" if hit else "✗✗ **没拦住（闸是摆设）**",
                 name, sorted(bad) or "无", sorted(want_ids)))
        if not hit:
            fails += 1

    def expect_green(name: str, rep: dict) -> None:
        nonlocal fails
        bad = [r["id"] for r in rep["rows"] if r["ok"] is False]
        unk = [r["id"] for r in rep["rows"] if r["ok"] is None]
        ok = not bad and not unk
        print("  %s %s：%s（判红=%s 未核=%s）"
              % ("✓ 全过" if ok else "✗ 误杀/未核", name, rep["summary"], bad, unk))
        if not ok:
            fails += 1

    print("\n### 1. 好库（已声明、无洗白、缺口 0）⇒ 必须绿（不误杀）")
    expect_green("好库", run("good", _good_rows()))

    print("\n### 2. 坏库 A：注入块被写成真人（来源洗白）⇒ P2 必须红")
    expect_red("洗白库",
               run("launder", _good_rows() + [{"kind": "plugin_injection", "role": "user"}]),
               {"P2"})

    print("\n### 3. 坏库 B：出现未声明的 (source/kind→role) ⇒ P1 必须红")
    expect_red("新组合静默放行",
               run("undecl", _good_rows() + [{"kind": "brand_new_kind", "role": "user"}]),
               {"P1"})

    print("\n### 4. 坏库 C：同一 kind 但**换了来源**导致 role 变了 ⇒ P1 仍须红"
          "（证明登记是 source 粒度）")
    expect_red("换来源换口径",
               run("persrc", _good_rows() + [{"source": "cursor", "kind": "tool_result",
                                              "role": "assistant"}]),
               {"P1"})

    print("\n### 5. 坏库 D：声明 prov 是「文件出处」却混进验证类键 ⇒ P4 必须红")
    rows = _good_rows()
    rows[0]["prov"] = {"file": "f.jsonl", "verify_status": "verified"}
    expect_red("prov 语义不符", run("provsem", rows), {"P4"})

    print("\n### 6. 坏库 E：声明「工具名提供、基线 0」但这批原子没工具名 ⇒ P3 必须红（只防新增）")
    rows = _good_rows()
    rows[3]["tool_name"] = None
    expect_red("新增缺口超基线",
               run("below", rows, _patch(reg, **{"questions.q3_tool.baseline_gap": 0})), {"P3"})

    print("\n### 7. 坏库 F：声明「不提供」但库里该列覆盖 100% ⇒ P3 必须红")
    rows = [dict(r, tool_name="read") for r in _good_rows()]
    expect_red("声明低于事实",
               run("lying", rows, _patch(reg, **{
                   "questions.q3_tool": {"provided": False, "reason": "本库不记工具名",
                                         "probe_columns": ["tool_name"]}})), {"P3"})

    print("\n### 8. 坏库 G：问项写成「不提供」却不写原因 ⇒ P3 必须红")
    expect_red("缺原因想糊过去",
               run("noreason", _good_rows(),
                   _patch(reg, **{"questions.q2_position": {"provided": False,
                                                            "reason": ""}})), {"P3"})

    print("\n### 9. 坏库 H：声明「排除某源」但该源其实有值 ⇒ 排除声明过期 ⇒ P3 必须红")
    expect_red("拿排除放水",
               run("stale_excl", _good_rows(),
                   _patch(reg, **{"questions.q5_time.excluded_sources": {
                       "dsh": "声称该源不提供时间戳"}})), {"P3"})

    print("\n### 10. 坏库 I：presence 模式列留空 ⇒ P3 必须红")
    rows = _good_rows()
    rows[0]["verify_status"] = None
    expect_red("显式未知被破坏",
               run("presence", rows, _patch(reg, **{
                   "questions.q6_verified": {"provided": True,
                                             "columns": ["verify_status"],
                                             "mode": "presence"}})), {"P3"})

    print("\n### 11. 坏库 J：`time_basis` 留空（沉默的缺口）⇒ P3 必须红"
          "（证明『未知要显式』这条判据有牙）")
    rows = _good_rows()
    rows[0]["time_basis"] = None
    expect_red("时间来源不显式", run("timebasis", rows), {"P3"})

    print("\n### 自检结果：%s" % ("全部通过（喂坏输入都拦下了）" if fails == 0
                                else "%d 项失败" % fails))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        print("[g_prov] stdout 重设为 utf-8 失败（继续跑）：%r" % (e,), file=sys.stderr)
    from guard import load as load_cfg

    ap = argparse.ArgumentParser(prog="g_prov")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--db", help="指定库路径（默认取登记表里的 db）")
    ap.add_argument("--incremental", action="store_true",
                    help="只核上次之后的新原子（P1/P2；自动路用）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    if a.demo:
        sys.exit(demo(cfg))
    gt = ProvGate(cfg, a.db)
    rep = gt.check(incremental=a.incremental)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(gt.render(rep))
    sys.exit(0 if rep["ok"] else 1)
