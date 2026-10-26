"""SQLite storage + FTS5 retrieval kernel for the DSH memory library.

Design notes
------------
* `atoms`  : every conversation unit, full text, never truncated. This is the
             "nothing is forgotten" layer and the target of `mem grep`.
* `chunks` : retrieval windows built from atoms, with citation metadata.
* `chunks_fts`: FTS5 (contentless, delete enabled) over *pre-tokenized* text.
             We tokenize ourselves (CJK character bigrams + latin words) so that
             Chinese matching is exact-ish and 2-character queries work, then let
             SQLite do compact storage + bm25 candidate ranking. Final ranking is
             redone in Python for transparency and tunability.
"""
import json
import os
import sqlite3
import time

from .normalize import tokenize, tokenize_unique
from .privacy import redact

SCHEMA_VERSION = 3

#: v2/v3 新增列（幂等迁移用）：表 -> [(列名, 列定义)]
#: 为什么需要它：`CREATE TABLE IF NOT EXISTS` 对**已存在**的表**不会**补列
#: ⇒ 老库（本机 4.4 GB / 32.6 万原子）跑 DDL 一个新列也拿不到，且**不报错**。
#: 这正是"声明与事实不符"那类静默失效，故必须显式迁移 + 版本记账。
MIGRATIONS = {
    "atoms": [
        ("observation_position", "TEXT"),
        ("verify_status", "TEXT DEFAULT 'unverified'"),
        ("verify_method", "TEXT"),
        ("verify_ref", "TEXT"),
        ("verified_at", "INTEGER"),
        # v3：把"没有时间戳"从 NULL（**沉默的缺口**）变成**显式状态**
        # `record`（取自原始记录）/ `source_absent`（源头就没有该字段）。
        # 与 verify_status 同款取向：未知要显式，不能靠留空。
        ("time_basis", "TEXT"),
    ],
}

DDL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sources(
  source TEXT NOT NULL, path TEXT NOT NULL, session_id TEXT, group_slug TEXT,
  size INTEGER, mtime INTEGER, content_sha1 TEXT, version INTEGER, is_backup INTEGER,
  status TEXT, n_atoms INTEGER, n_chunks INTEGER, n_records INTEGER,
  ingested_at INTEGER, note TEXT,
  PRIMARY KEY(source, path));
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY, source TEXT, group_slug TEXT, dir TEXT, cwd TEXT,
  origin TEXT, parent_session TEXT, delegation_depth INTEGER, agent_preset TEXT,
  model TEXT, label TEXT, title TEXT, objective TEXT,
  created_at INTEGER, first_time INTEGER, last_time INTEGER,
  n_atoms INTEGER, n_user INTEGER, n_assistant INTEGER, n_tool INTEGER, n_chars INTEGER,
  n_chunks INTEGER, n_indexed_chars INTEGER, versions TEXT, files TEXT,
  in_raw_archive INTEGER DEFAULT 0, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS atoms(
  atom_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, source TEXT, seq INTEGER,
  time INTEGER, turn INTEGER, step INTEGER, role TEXT, kind TEXT, text TEXT,
  text_hash TEXT, tool_name TEXT, call_id TEXT, note TEXT, prov TEXT,
  extra_from_version INTEGER,
  observation_position TEXT,
  verify_status TEXT DEFAULT 'unverified', verify_method TEXT, verify_ref TEXT,
  verified_at INTEGER, time_basis TEXT);
CREATE INDEX IF NOT EXISTS idx_atoms_session ON atoms(session_id);
CREATE INDEX IF NOT EXISTS idx_atoms_kind ON atoms(kind);
CREATE INDEX IF NOT EXISTS idx_atoms_time ON atoms(time);
CREATE INDEX IF NOT EXISTS idx_atoms_hash ON atoms(text_hash);
-- v2：F1 回填 tool_name 靠 (session_id, call_id) 自连接；无此索引时相关子查询
-- 实测 >600 s 都跑不完（98,978 行 × 全表扫）。建索引后同一查询 3 s。
CREATE INDEX IF NOT EXISTS idx_atoms_session_call ON atoms(session_id, call_id);
CREATE TABLE IF NOT EXISTS chunks(
  chunk_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, source TEXT, kind TEXT,
  turn INTEGER, seq_start INTEGER, seq_end INTEGER, time_start INTEGER, time_end INTEGER,
  n_chars INTEGER, text TEXT, user_text TEXT, roles TEXT, tools TEXT, weight REAL,
  text_hash TEXT, prov TEXT, n_atoms INTEGER);
CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_chunks_time ON chunks(time_start);
CREATE INDEX IF NOT EXISTS idx_chunks_turn ON chunks(session_id, turn);
CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(text_hash);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  tok, user_tok, title_tok, content='', contentless_delete=1,
  tokenize="unicode61 remove_diacritics 2 tokenchars '._+#-'");
CREATE TABLE IF NOT EXISTS ingest_log(
  id INTEGER PRIMARY KEY, ts INTEGER, source TEXT, session_id TEXT, action TEXT, detail TEXT);
"""


def migrate(con, verbose=True):
    """把已存在的库补到当前 SCHEMA_VERSION（幂等）。

    返回 {"added": [(表,列), ...], "version_before": int|None, "version_after": int}。
    ★ 只加列、只建索引 —— 不改写任何既有数据（数据回填是另一件事，见 bin/mem.py repair-provenance）。
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(atoms)")}
    added = []
    for table, spec in MIGRATIONS.items():
        have = {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
        for name, decl in spec:
            if name not in have:
                con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
                added.append((table, name))
    con.execute("CREATE INDEX IF NOT EXISTS idx_atoms_session_call ON atoms(session_id, call_id)")
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    before = int(row[0]) if row and str(row[0]).isdigit() else None
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),))
    con.commit()
    if verbose:
        print("[store.migrate] schema %s → %s；补列 %d 个%s"
              % (before, SCHEMA_VERSION, len(added),
                 ("：" + "、".join("%s.%s" % t for t in added)) if added else ""))
    return {"added": added, "version_before": before, "version_after": SCHEMA_VERSION}


def connect(path, create=True):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-262144")     # 256 MB page cache
    con.execute("PRAGMA mmap_size=1073741824")
    if create:
        con.executescript(DDL)
        con.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),))
        con.commit()
        # ★ 老库补列（新建库走 DDL 已含列，这里会 no-op）
        migrate(con, verbose=False)
    return con


# ------------------------------------------------------------------ writing

def replace_session(con, meta, atoms, chunks, source_files):
    """Atomically (re)write one session. Caller owns the transaction."""
    sid = meta["session_id"]
    old = [r[0] for r in con.execute("SELECT chunk_id FROM chunks WHERE session_id=?", (sid,))]
    for cid in old:
        con.execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
    con.execute("DELETE FROM chunks WHERE session_id=?", (sid,))
    con.execute("DELETE FROM atoms WHERE session_id=?", (sid,))
    con.execute("DELETE FROM sessions WHERE session_id=?", (sid,))

    con.execute("""INSERT INTO sessions(session_id, source, group_slug, dir, cwd, origin,
        parent_session, delegation_depth, agent_preset, model, label, title, objective,
        created_at, first_time, last_time, n_atoms, n_user, n_assistant, n_tool, n_chars,
        n_chunks, n_indexed_chars, versions, files, in_raw_archive, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        sid, meta.get("source"), meta.get("group_slug"), meta.get("dir"), meta.get("cwd"),
        meta.get("origin"), meta.get("parent_session"), meta.get("delegation_depth"),
        meta.get("agent_preset"), meta.get("model"), meta.get("label"), meta.get("title"),
        meta.get("objective"), meta.get("created_at"), meta.get("first_time"), meta.get("last_time"),
        len(atoms), meta.get("n_user"), meta.get("n_assistant"), meta.get("n_tool"),
        sum(len(a["text"]) for a in atoms), len(chunks),
        sum(len(c["text"]) for c in chunks),
        json.dumps(meta.get("versions")), json.dumps(source_files, ensure_ascii=False),
        int(meta.get("in_raw_archive", 0)), int(time.time() * 1000)))

    con.executemany("""INSERT INTO atoms(session_id, source, seq, time, turn, step, role, kind,
        text, text_hash, tool_name, call_id, note, prov, extra_from_version,
        observation_position, verify_status, verify_method, verify_ref, time_basis)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [
        (sid, meta.get("source"), a.get("seq"), a.get("time"), a.get("turn"), a.get("step"),
         a.get("role"), a.get("kind"), redact(a["text"]), a.get("hash"),
         a.get("tool_name"), a.get("call_id"), a.get("note"),
         json.dumps(a.get("prov"), ensure_ascii=False), a.get("extra_from_version"),
         a.get("observation_position"), a.get("verify_status") or "unverified",
         a.get("verify_method"), a.get("verify_ref"), a.get("time_basis"))
        for a in atoms])

    for c in chunks:
        cur = con.execute("""INSERT INTO chunks(session_id, source, kind, turn, seq_start, seq_end,
            time_start, time_end, n_chars, text, user_text, roles, tools, weight, text_hash, prov,
            n_atoms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            sid, meta.get("source"), c["kind"], c.get("turn"), c.get("seq_start"), c.get("seq_end"),
            c.get("time_start"), c.get("time_end"), len(c["text"]), redact(c["text"]), redact(c.get("user_text") or ""),
            c.get("roles"), c.get("tools"), c.get("weight", 1.0), c.get("hash"),
            json.dumps(c.get("prov"), ensure_ascii=False), c.get("n_atoms")))
        cid = cur.lastrowid
        con.execute("INSERT INTO chunks_fts(rowid, tok, user_tok, title_tok) VALUES(?,?,?,?)",
                    (cid, c["tok"], c.get("user_tok") or "", c.get("title_tok") or ""))


def log(con, source, session_id, action, detail=""):
    con.execute("INSERT INTO ingest_log(ts, source, session_id, action, detail) VALUES(?,?,?,?,?)",
                (int(time.time() * 1000), source, session_id, action, detail))


FTS_DDL = """CREATE VIRTUAL TABLE chunks_fts USING fts5(
  tok, user_tok, title_tok, content='', contentless_delete=1,
  tokenize="unicode61 remove_diacritics 2 tokenchars '._+#-'");"""


def reindex_fts(con, progress=None):
    """Rebuild the FTS index from `chunks` (needed when the tokenizer changes)."""
    con.execute("DROP TABLE IF EXISTS chunks_fts")
    con.execute(FTS_DDL)
    cur = con.execute("""SELECT c.chunk_id, c.text, c.user_text, COALESCE(s.title,'')
                         FROM chunks c LEFT JOIN sessions s ON s.session_id = c.session_id""")
    n = 0
    batch = []
    while True:
        rows = cur.fetchmany(2000)
        if not rows:
            break
        for cid, text, user_text, title in rows:
            batch.append((cid, " ".join(tokenize(text)), " ".join(tokenize(user_text or "")),
                          " ".join(tokenize(title or ""))))
        con.executemany("INSERT INTO chunks_fts(rowid, tok, user_tok, title_tok) VALUES(?,?,?,?)",
                        batch)
        n += len(batch)
        batch = []
        if progress and n % 20000 < 2000:
            progress(n)
    con.commit()
    return n


# ------------------------------------------------------------------ reading

def _quote(t):
    return '"%s"' % t.replace('"', " ")


def _or_query(terms):
    """OR of every token -- recall-oriented."""
    safe = [_quote(t) for t in terms if t]
    return " OR ".join(safe) if safe else None


def _phrase_query(terms):
    """Exact token sequence -- precision-oriented (tokens are bigrams/words)."""
    safe = [t.replace('"', " ") for t in terms if t]
    if len(safe) < 2:
        return None
    return '"%s"' % " ".join(safe)


# kept for backwards compatibility with tooling
def _fts_query(terms):
    return _or_query(terms)


COLS = ["chunk_id", "session_id", "source", "kind", "turn", "seq_start", "seq_end",
        "time_start", "time_end", "n_chars", "text", "user_text", "roles", "tools",
        "weight", "prov", "rank", "title"]

_SELECT = """SELECT c.chunk_id, c.session_id, c.source, c.kind, c.turn, c.seq_start, c.seq_end,
                    c.time_start, c.time_end, c.n_chars, c.text, c.user_text, c.roles, c.tools,
                    c.weight, c.prov, %s AS rank, COALESCE(s.title,'') AS title
             FROM %s LEFT JOIN sessions s ON s.session_id = c.session_id
             WHERE %s"""


def _filters(session=None, source=None, since=None, until=None, roles=None, kinds=None, turn=None):
    sql, args = [], []
    if session:
        sql.append("AND (c.session_id = ? OR c.session_id LIKE ?)")
        args += [session, "%" + session + "%"]
    if source:
        sql.append("AND c.source = ?")
        args.append(source)
    if since:
        sql.append("AND (c.time_end IS NULL OR c.time_end >= ?)")
        args.append(since)
    if until:
        sql.append("AND (c.time_start IS NULL OR c.time_start <= ?)")
        args.append(until)
    if roles:
        for r in [x.strip() for x in roles.split(",") if x.strip()]:
            sql.append("AND c.roles LIKE ?")
            args.append("%" + r + "%")
    if kinds:
        ks = [x.strip() for x in kinds.split(",") if x.strip()]
        sql.append("AND c.kind IN (%s)" % ",".join("?" * len(ks)))
        args += ks
    if turn is not None:
        sql.append("AND c.turn = ?")
        args.append(turn)
    return (" ".join(sql), args)


def _run_match(con, expr, filters, limit):
    sql = (_SELECT % ("bm25(chunks_fts, 1.0, 4.0, 2.0)",
                      "chunks_fts JOIN chunks c ON c.chunk_id = chunks_fts.rowid",
                      "chunks_fts MATCH ? " + filters[0])
           + " ORDER BY rank LIMIT ?")
    return [dict(zip(COLS, r)) for r in con.execute(sql, [expr] + filters[1] + [limit])]


def _like_scan(con, query, filters, limit=20):
    """Last-resort exact substring scan (whitespace-insensitive)."""
    q = "%" + query.strip() + "%"
    sql = (_SELECT % ("0.0", "chunks c",
                      "REPLACE(REPLACE(c.text, char(10), ' '), char(13), ' ') LIKE ? " + filters[0])
           + " LIMIT ?")
    return [dict(zip(COLS, r)) for r in con.execute(sql, [q] + filters[1] + [limit])]


def search(con, query, k=8, session=None, source=None, since=None, until=None,
           roles=None, kinds=None, turn=None, candidates=600, explain=False):
    """Return ranked chunks (list of dicts) for a natural-language/keyword query.

    Strategy: exact token-phrase first (precision), then OR-of-tokens (recall),
    then a whitespace-insensitive substring scan if both came up nearly empty.
    Final ordering is redone in Python so the score is inspectable.
    """
    terms = []
    for t in tokenize(query):
        if t not in terms:
            terms.append(t)
    if not terms:
        return []
    filt = _filters(session, source, since, until, roles, kinds, turn)
    found = {}
    ph = _phrase_query(terms)
    if ph:
        for h in _run_match(con, ph, filt, candidates):
            h["via"] = "phrase"
            found[h["chunk_id"]] = h
    if len(found) < 80:
        orx = _or_query(terms[:24])
        if orx:
            for h in _run_match(con, orx, filt, candidates):
                if h["chunk_id"] not in found:
                    h["via"] = "or"
                    found[h["chunk_id"]] = h
    if len(found) < 3 and len(query.strip()) >= 6:
        try:
            for h in _like_scan(con, query.strip(), filt, 20):
                h["via"] = "substring"
                found.setdefault(h["chunk_id"], h)
        except Exception:
            pass
    hits = list(found.values())
    if not hits:
        return []
    hits = rerank(hits, terms, query, k=k, explain=explain)
    for h in hits:
        if h.get('text'): h['text'] = redact(h['text'])
        if h.get('user_text'): h['user_text'] = redact(h['user_text'])
    return hits[:k]


def _as_ms(v):
    """把时间值归一成「毫秒数」。拿不准就返回 None（**不抛异常**）。

    为什么必须有这一层（2026-10-26 沙箱实测）：`time_end` 不总是数字 ——
    从 `staged/*.atoms.jsonl` 导入的原子，`time` 是 ISO 字符串
    （`2026-10-26T00:00:01Z`），而 `now` 是 float 毫秒 ⇒
    `now - h["time_end"]` 直接 TypeError，**整条 recall 崩掉**。
    旧库里全是数字所以从没暴露过；一个空库 + 一份 staged 种子就能打出来。
    这就是「只在别人的环境里才发作」的那类 bug。
    """
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    try:
        return float(s)
    except (TypeError, ValueError):
        pass
    # ISO 8601：`2026-10-26T00:00:01Z` / `2026-10-26T00:00:01+08:00` / 带小数秒
    try:
        import datetime as _dt
        txt = s.replace("Z", "+00:00")
        d = _dt.datetime.fromisoformat(txt)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.timestamp() * 1000.0
    except (TypeError, ValueError):
        pass
    # `2026-10-26 00:00:01` 这种空格分隔写法
    try:
        import datetime as _dt
        d = _dt.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        return d.replace(tzinfo=_dt.timezone.utc).timestamp() * 1000.0
    except (TypeError, ValueError):
        return None


def rerank(hits, terms, query, k=8, explain=False):
    """Python-side scoring on top of FTS5 bm25 candidates.

    Deterministic and inspectable: coverage of query terms, exact-phrase hit,
    which field matched, chunk weight, and a light recency nudge.
    """
    q_norm = query.strip().lower()
    q_runs = [s for s in _cjk_runs(query) if len(s) >= 2]
    ranks = sorted(h["rank"] for h in hits)
    rmin, rmax = (ranks[0], ranks[-1]) if ranks else (0.0, 0.0)
    now = time.time() * 1000
    scored = []
    for h in hits:
        tok = set(tokenize(h["text"] or ""))
        parent = set(tokenize(h["user_text"] or ""))
        title_toks = set(tokenize(h.get("title") or ""))
        all_toks = tok | parent | title_toks
        hit_terms = [t for t in terms if t in all_toks]
        coverage_all = len(hit_terms) / max(1, len(terms))
        coverage_user = sum(1 for t in terms if t in parent) / max(1, len(terms))
        coverage_title = sum(1 for t in terms if t in title_toks) / max(1, len(terms))
        # partial-word tolerance: "authorizat" should still count against "authorization"
        partial = 0
        for t in terms:
            if t in all_toks or len(t) < 4:
                continue
            if any(x.startswith(t) or t.startswith(x) and len(x) >= 4 for x in all_toks):
                partial += 1
        coverage_partial = partial / max(1, len(terms))
        text_l = (h["text"] or "").lower()
        phrase = 0.0
        if len(q_norm) >= 2 and q_norm in text_l:
            phrase += 2.0
        for run in q_runs:
            if run.lower() in text_l:
                phrase += 0.6
        phrase = min(phrase, 3.0)
        r = h["rank"]
        fts_norm = 0.0 if rmax == rmin else 1.0 - (r - rmin) / (rmax - rmin)
        recency = 0.0
        _te = _as_ms(h.get("time_end"))
        if _te is not None:
            age_days = max(0.0, (now - _te) / 86400000.0)
            recency = 1.0 / (1.0 + age_days / 30.0)
        via = h.get("via")
        # a session whose *title* fully matches the query is "about" the query even when
        # the words never appear in the body (LLM-generated titles behave like this)
        title_bonus = 0.8 if coverage_title >= 0.999 and len(terms) <= 10 else 0.0
        score = (3.0 * coverage_all + 2.5 * coverage_user + 2.0 * coverage_title
                 + 1.5 * coverage_partial
                 + 1.6 * phrase + 1.2 * fts_norm + 0.4 * float(h.get("weight") or 1.0)
                 + 0.25 * recency + (1.5 if via == "phrase" else 0.0) + title_bonus)
        h["score"] = round(score, 4)
        if explain:
            h["explain"] = {"coverage": round(coverage_all, 3), "user_coverage": round(coverage_user, 3),
                            "title_coverage": round(coverage_title, 3),
                            "partial": round(coverage_partial, 3),
                            "phrase": round(phrase, 2), "fts": round(fts_norm, 3), "via": via,
                            "title_bonus": title_bonus,
                            "weight": h.get("weight"), "recency": round(recency, 3),
                            "terms_hit": hit_terms, "terms_total": len(terms)}
        scored.append(h)
    scored.sort(key=lambda x: -x["score"])
    return scored


def _cjk_runs(s):
    out, cur = [], []
    for ch in s:
        o = ord(ch)
        if 0x3400 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF or 0x20000 <= o <= 0x2FA1F:
            cur.append(ch)
        else:
            if len(cur) >= 2:
                out.append("".join(cur))
            cur = []
    if len(cur) >= 2:
        out.append("".join(cur))
    return out


def grep(con, pattern, k=50, scope="chunks", session=None, regex=False):
    """Exact substring / regex search across chunk text (or atom text)."""
    table = "chunks" if scope == "chunks" else "atoms"
    cols = ("chunk_id, session_id, source, turn, seq_start, seq_end, time_start, text"
            if table == "chunks" else
            "atom_id, session_id, source, turn, seq, time, kind, text")
    sql = "SELECT %s FROM %s WHERE " % (cols, table)
    args = []
    if regex:
        sql += "text REGEXP ?"
    else:
        sql += "text LIKE ?"
        pattern = "%" + pattern + "%"
    args.append(pattern)
    if session:
        sql += " AND session_id = ?"
        args.append(session)
    sql += " LIMIT ?"
    args.append(k)
    rows = con.execute(sql, args).fetchall()
    idx = 7 if table == 'chunks' else 7
    return [tuple(redact(v) if (i == idx and isinstance(v, str)) else v for i, v in enumerate(r)) for r in rows]


def session_list(con, limit=200, offset=0, source=None, grep=None, order="last_time"):
    sql = ["SELECT session_id, source, group_slug, origin, parent_session, title, label, cwd,",
           "first_time, last_time, n_user, n_assistant, n_tool, n_atoms, n_chunks, n_chars, versions",
           "FROM sessions WHERE 1=1"]
    args = []
    if source:
        sql.append("AND source = ?")
        args.append(source)
    if grep:
        sql.append("AND (title LIKE ? OR label LIKE ? OR session_id LIKE ? OR cwd LIKE ?)")
        args += ["%" + grep + "%"] * 4
    sql.append("ORDER BY %s DESC LIMIT ? OFFSET ?" % order)
    args += [limit, offset]
    cols = ["session_id", "source", "group_slug", "origin", "parent_session", "title", "label",
            "cwd", "first_time", "last_time", "n_user", "n_assistant", "n_tool", "n_atoms",
            "n_chunks", "n_chars", "versions"]
    out = [dict(zip(cols, r)) for r in con.execute(" ".join(sql), args)]
    for it in out:
        if it.get('text'): it['text'] = redact(it['text'])
        if it.get('note'): it['note'] = redact(it['note'])
    return out


def session_atoms(con, session_id, turn=None, kind=None, limit=10000, offset=0):
    sql = ["SELECT atom_id, seq, time, turn, step, role, kind, tool_name, text, note, prov",
           "FROM atoms WHERE session_id = ?"]
    args = [session_id]
    if turn is not None:
        sql.append("AND turn = ?")
        args.append(turn)
    if kind:
        sql.append("AND kind = ?")
        args.append(kind)
    sql.append("ORDER BY COALESCE(time,0), COALESCE(seq,0) LIMIT ? OFFSET ?")
    args += [limit, offset]
    cols = ["atom_id", "seq", "time", "turn", "step", "role", "kind", "tool_name", "text", "note", "prov"]
    return [dict(zip(cols, r)) for r in con.execute(" ".join(sql), args)]


def stats(con):
    out = {}
    q = lambda s, *a: con.execute(s, a).fetchone()[0]
    out["sessions"] = q("SELECT COUNT(*) FROM sessions")
    out["sessions_by_source"] = dict(con.execute(
        "SELECT source, COUNT(*) FROM sessions GROUP BY source").fetchall())
    out["atoms"] = q("SELECT COUNT(*) FROM atoms")
    out["atoms_by_kind"] = dict(con.execute(
        "SELECT kind, COUNT(*) FROM atoms GROUP BY kind ORDER BY 2 DESC").fetchall())
    out["atom_chars"] = q("SELECT COALESCE(SUM(LENGTH(text)),0) FROM atoms")
    out["chunks"] = q("SELECT COUNT(*) FROM chunks")
    out["chunk_chars"] = q("SELECT COALESCE(SUM(LENGTH(text)),0) FROM chunks")
    out["first_time"] = q("SELECT MIN(first_time) FROM sessions")
    out["last_time"] = q("SELECT MAX(last_time) FROM sessions")
    out["db_bytes"] = os.path.getsize(con.execute("PRAGMA database_list").fetchone()[2]) \
        if con.execute("PRAGMA database_list").fetchone()[2] else 0
    return out
