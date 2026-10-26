"""Ingest pipeline: DSH session store -> raw archive + atoms + chunks + SQLite index.

Two modes, one code path:
  * backfill  : walk every session directory, ingest what is new/changed
  * incremental: same call, but only files whose (size, mtime) fingerprint changed
    are re-read, so a cron/timer can run it every minute cheaply.

Nothing is ever deleted from the source store; every ingested session also gets a
verbatim gzip copy of the decompressed original plus a normalized `atoms.jsonl`
under raw/, so re-chunking never needs the original store again.
"""
import gzip
import json
import os
import time

from . import dsh, paths, store
from .normalize import norm_text, sha1, tokenize

#: 数据根（`raw/ index/ reports/ staged/ backups/ console/` 都挂在它下面）。
#: 2026-10-26 起由 `paths.py` 单独解析 —— 不再与代码根绑定。
#: 名字仍叫 `ROOT`，是为了让既有调用点（`pipeline.ROOT`）零改动。
ROOT = paths.DATA_ROOT
RAW = paths.RAW
INDEX_DIR = paths.INDEX_DIR
DB_PATH = paths.DB_PATH
REPORT_DIR = paths.REPORT_DIR

FRAG_MAX = 1100          # a single atom is split into fragments of at most this
CHUNK_MAX = 1900         # chunks are packed up to this size
TOOL_HEAD = 600          # tool results longer than TOOL_FULL are only headed in conv chunks
TOOL_FULL = 6000         # tool results up to this size get their own fully indexed chunks
TOOL_CHUNK_CAP = 6       # max full chunks per oversized tool result

KIND_PREFIX = {
    "user_text": "[用户]",
    "spliced_user": "[用户·插话]",
    "assistant_text": "[助手]",
    "assistant_reasoning": "[助手·思考]",
    "tool_call": "[工具调用]",
    "tool_result": "[工具结果]",
    "compaction_summary": "[上下文压缩摘要]",
    "goal": "[目标]",
    "command": "[命令]",
    "developer_text": "[开发者]",
    "deliverable": "[交付物]",
    "title": "[标题]",
    "system_text": "[系统提示]",
    "todo": "[待办]",
    "note": "[记录]",
    "plugin_injection": "[插件注入]",
}
KIND_WEIGHT = {
    "user_text": 1.6, "spliced_user": 1.5, "assistant_text": 1.2,
    "assistant_reasoning": 0.9, "compaction_summary": 1.3, "goal": 1.2,
    "command": 1.0, "tool_call": 0.8, "tool_result": 0.6, "developer_text": 1.0,
    "deliverable": 1.0,
}
# atoms that are searchable but never mixed into retrieval chunks
# ★ 2026-10-15：`plugin_injection` 入列 —— 插件/框架往对话里塞的机器块不再参与检索
#   （dsh.py 按 source.kind 白名单把它们与真人发言分开；见该文件 USER_SOURCE_KINDS 注释）
NO_CHUNK = {"system_text", "title", "todo", "note", "plugin_injection"}


# ------------------------------------------------------------------ 来源六问

OBS_POSITION_MAX = 160


def observation_position(source, meta):
    """观测位（来源六问 q2）——把"在哪观测到"从「导入器级」细化到**会话粒度**。

    形态：`<源>·<会话来源>·<工作区>`，如 `dsh·user·<工作区>`、`dsh·subagent·<工作区>`。

    ★ 诚实边界（哪些削掉了）：
      · 粒度**止于会话**，不是逐条记录 —— 原始记录里没有逐条的观测位字段，
        能拿到的最细位置就是"哪个源、哪类会话（user/subagent）、在哪个工作区"；
      · 灵枢 `observation_position` 的值域含物理观测位（`感知·摄像头` / `感知·屏幕`），
        本库**没有**那一层（无摄像头/屏幕采集通道）——不假装有。
    """
    src = str(source or meta.get("source") or "unknown")
    origin = str(meta.get("origin") or "unknown")
    where = str(meta.get("cwd") or meta.get("dir") or "").strip()
    pos = "%s·%s·%s" % (src, origin, where) if where else "%s·%s" % (src, origin)
    return pos[:OBS_POSITION_MAX]


def stamp_position(atoms, meta):
    """给一批原子盖上观测位（原地改）＋ 标记"时间从哪来"，返回用到的观测位。

    ★ `time_basis`（v3）：`record`＝取自原始记录；`source_absent`＝**源头就没有**
      逐条时间戳（实测 cursor 的部分 `.jsonl` 顶层只有 `{role, message}`）。
      为什么要有它：`time IS NULL` 是**沉默的缺口**——读的人分不清"我们没读到"
      与"源头没有"。与 `verify_status` 同款取向：**未知要显式**。
    """
    pos = observation_position(meta.get("source"), meta)
    for a in atoms:
        a["observation_position"] = pos
        a["time_basis"] = "record" if a.get("time") else "source_absent"
    return pos


# ------------------------------------------------------------------ fragments

def _split_text(text, size=FRAG_MAX):
    """Split on paragraph/line/sentence boundaries; never mid-word if avoidable."""
    if len(text) <= size:
        return [text]
    parts, buf = [], ""
    for block in text.split("\n"):
        if len(block) > size:
            if buf:
                parts.append(buf)
                buf = ""
            for i in range(0, len(block), size):
                parts.append(block[i:i + size])
            continue
        if len(buf) + len(block) + 1 > size:
            parts.append(buf)
            buf = block
        else:
            buf = buf + "\n" + block if buf else block
    if buf:
        parts.append(buf)
    return [p for p in parts if p.strip()]


def fragments(atoms):
    """Expand atoms into renderable fragments with their citation metadata."""
    out = []
    for a in atoms:
        kind = a["kind"]
        if kind in NO_CHUNK:
            continue
        text = a["text"]
        if kind == "tool_result" and len(text) > TOOL_FULL:
            head = text[:TOOL_HEAD]
            out.append((a, head, True))
            rest = text[TOOL_HEAD:]
            for i in range(min(TOOL_CHUNK_CAP, (len(rest) + CHUNK_MAX - 1) // CHUNK_MAX)):
                piece = rest[i * CHUNK_MAX:(i + 1) * CHUNK_MAX]
                if piece.strip():
                    out.append((a, piece, True))
            continue
        for piece in _split_text(text):
            out.append((a, piece, False))
    return out


def build_chunks(meta, atoms):
    """Pack fragments into retrieval windows, one conversation chunk = one turn-ish."""
    frags = fragments(atoms)
    chunks = []
    buf, buf_chars, buf_roles, buf_tools = [], 0, set(), set()
    cur_user = ""
    chunk_atoms, times = [], []

    def flush():
        nonlocal buf, buf_chars, buf_roles, buf_tools, chunk_atoms, times
        if not buf:
            return
        text = "\n".join(buf)
        atom_list = list(chunk_atoms)
        first, last = atom_list[0], atom_list[-1]
        kinds = {x["kind"] for x in atom_list}
        if kinds & {"user_text", "spliced_user"}:
            kind = "turn"
        elif "tool_result" in kinds and len(kinds) == 1:
            kind = "tool"
        else:
            kind = "cont"
        chunks.append({
            "kind": kind,
            "turn": first.get("turn"),
            "seq_start": first.get("seq"), "seq_end": last.get("seq"),
            "time_start": min(times) if times else None,
            "time_end": max(times) if times else None,
            "text": text,
            "user_text": cur_user,
            "roles": ",".join(sorted(x for x in buf_roles if x)),
            "tools": ",".join(sorted(buf_tools)),
            "weight": round(sum(KIND_WEIGHT.get(x["kind"], 1.0) for x in atom_list) / max(1, len(atom_list)), 3),
            "n_atoms": len(atom_list),
            "hash": sha1(text),
            "tok": " ".join(tokenize(text)),
            "user_tok": " ".join(tokenize(cur_user)),
            "title_tok": " ".join(tokenize(meta.get("title") or "")),
            "prov": {"files": meta.get("files_short")},
        })
        buf, buf_chars, buf_roles, buf_tools, chunk_atoms, times = [], 0, set(), set(), [], []

    for a, piece, truncated in frags:
        kind = a["kind"]
        if kind in ("user_text", "spliced_user") and buf_chars > 0:
            flush()
        if kind in ("user_text", "spliced_user"):
            cur_user = a["text"][:3000]
        prefix = KIND_PREFIX.get(kind, "[%s]" % kind)
        if kind == "tool_call" and a.get("tool_name"):
            prefix = "[工具调用 %s]" % a["tool_name"]
        line = "%s %s" % (prefix, piece)
        if buf_chars + len(line) > CHUNK_MAX and buf_chars > 0 and kind not in ("user_text", "spliced_user"):
            flush()
        buf.append(line)
        buf_chars += len(line) + 1
        buf_roles.add(a["role"] or "?")
        if a.get("tool_name"):
            buf_tools.add(a["tool_name"])
        chunk_atoms.append(a)
        if a.get("time"):
            times.append(a["time"])
    flush()
    return chunks


# ------------------------------------------------------------------ raw archive

def archive_raw(entry, versions_text, atoms, meta):
    """Write verbatim originals (gzip) + normalized atoms for one session."""
    sdir = os.path.join(RAW, "dsh", entry["group"], entry["session_id"])
    os.makedirs(sdir, exist_ok=True)
    written = []
    for v, text in sorted(versions_text.items()):
        if not text:
            continue
        fn = os.path.join(sdir, "original.v%d.jsonl.gz" % v)
        tmp = fn + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=3) as fh:
            fh.write(text)
        os.replace(tmp, fn)
        written.append(os.path.basename(fn))
    ap = os.path.join(sdir, "atoms.jsonl")
    tmp = ap + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": "meta", **{k: v for k, v in meta.items()
                                             if k in ("session_id", "source", "group_slug", "cwd",
                                                      "origin", "parent_session", "created_at",
                                                      "title", "label", "model", "versions")}},
                            ensure_ascii=False) + "\n")
        for a in atoms:
            fh.write(json.dumps({"t": "atom", "kind": a["kind"], "role": a["role"],
                                 "seq": a.get("seq"), "time": a.get("time"), "turn": a.get("turn"),
                                 "step": a.get("step"), "tool": a.get("tool_name"),
                                 "call": a.get("call_id"), "extra_version": a.get("extra_from_version"),
                                 "text": a["text"]}, ensure_ascii=False) + "\n")
    os.replace(tmp, ap)
    return written


# ------------------------------------------------------------------ one session

def process_dsh_session(con, entry, include_backups=True, write_raw=True, verbose=False):
    """Read all version files of one session, merge, archive, index."""
    sid = entry["session_id"]
    per_version, per_version_text, file_rows, metas = {}, {}, [], {}
    t0 = time.time()
    candidates = []
    for v, items in entry["versions"].items():
        for it in items:
            candidates.append(it)
    if include_backups:
        candidates += entry["backups"]
    canonical_path = entry["canonical"]["path"]
    for it in candidates:
        try:
            raw = dsh.read_zstd_multiframe(it["path"])
        except Exception as e:
            file_rows.append((it, None, "decode-error: %s" % e))
            continue
        text = raw.decode("utf-8", "replace")
        recs, lns, bad = dsh.parse_jsonl_with_lines(text)   # 带物理行号（q6 核验用）
        prov = {"file": it["path"], "version": it["version"], "backup": it["is_backup"]}
        atoms, meta_part, counters = dsh.extract_atoms(recs, prov, lines=lns)
        key = it["version"]
        if it["is_backup"]:
            key = -it["version"] - 1     # backups rank below every live version
        per_version.setdefault(key, []).extend(atoms)
        if not it["is_backup"]:
            per_version_text[it["version"]] = text
            metas[it["version"]] = meta_part
        file_rows.append((it, {"sha1": sha1(text), "records": len(recs), "bad": bad,
                               "atoms": len(atoms), "chars": len(text), "counters": counters}, None))

    if not per_version:
        return None
    # ordered values: live versions ascending, then backups
    ordered = {k: per_version[k] for k in sorted(per_version, key=lambda x: (x < 0, x))}
    atoms, merge_info = dsh.merge_versions(ordered)
    if not atoms and not per_version_text:
        return None

    meta = {"session_id": sid, "source": "dsh", "group_slug": entry["group"], "dir": entry["dir"]}
    for v in sorted(metas, reverse=True):          # canonical first, then older fills gaps
        for k, val in metas[v].items():
            if val is not None and meta.get(k) is None:
                meta[k] = val
    meta["versions"] = sorted(entry["versions"])
    meta["files_short"] = [os.path.basename(f["path"]) for f in
                           (entry["canonical"],) + tuple(x for x in entry["backups"][:2])]
    times = [a["time"] for a in atoms if a.get("time")]
    meta["first_time"] = min(times) if times else meta.get("created_at")
    meta["last_time"] = max(times) if times else meta.get("created_at")
    meta["n_user"] = sum(1 for a in atoms if a["kind"] in ("user_text", "spliced_user"))
    meta["n_assistant"] = sum(1 for a in atoms if a["kind"] in ("assistant_text", "assistant_reasoning"))
    meta["n_tool"] = sum(1 for a in atoms if a["kind"] in ("tool_call", "tool_result"))
    stamp_position(atoms, meta)                    # 六问 q2：观测位（会话粒度）

    chunks = build_chunks(meta, atoms)
    if write_raw:
        archive_raw(entry, per_version_text, atoms, meta)
        meta["in_raw_archive"] = 1

    store.replace_session(con, meta, atoms, chunks, [os.path.basename(f["path"]) for f in candidates])
    for it, info, err in file_rows:
        con.execute("""INSERT INTO sources(source, path, session_id, group_slug, size, mtime,
            content_sha1, version, is_backup, status, n_atoms, n_chunks, n_records, ingested_at, note)
            VALUES('dsh',?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source, path) DO UPDATE SET
              size=excluded.size, mtime=excluded.mtime, content_sha1=excluded.content_sha1,
              status=excluded.status, n_atoms=excluded.n_atoms, n_chunks=excluded.n_chunks,
              n_records=excluded.n_records, ingested_at=excluded.ingested_at, note=excluded.note""", (
            it["path"], sid, entry["group"], it["size"], it["mtime"],
            (info or {}).get("sha1"), it["version"], int(it["is_backup"]),
            "ok" if info else "error", (info or {}).get("atoms"), len(chunks),
            (info or {}).get("records"), int(time.time() * 1000), err))
    return {"session_id": sid, "atoms": len(atoms), "chunks": len(chunks),
            "chars": sum(len(a["text"]) for a in atoms), "kept_from_older": merge_info["kept_from_older"],
            "seconds": round(time.time() - t0, 2)}


def fingerprints(entry, use_backups=True):
    fps = []
    for v, items in entry["versions"].items():
        for it in items:
            fps.append((it["path"], it["size"], it["mtime"]))
    if use_backups:
        for it in entry["backups"]:
            fps.append((it["path"], it["size"], it["mtime"]))
    return tuple(sorted(fps))


def unchanged(con, entry, use_backups=True):
    fps = fingerprints(entry, use_backups)
    rows = con.execute("SELECT path, size, mtime FROM sources WHERE source='dsh' AND session_id=?",
                       (entry["session_id"],)).fetchall()
    have = tuple(sorted((r[0], r[1], r[2]) for r in rows))
    return have == fps and bool(rows)


def ingest_dsh(con, full=False, use_backups=True, write_raw=True, progress=None, limit=None):
    entries = dsh.discover_sessions()
    todo = entries if full else [e for e in entries if not unchanged(con, e, use_backups)]
    if limit:
        todo = todo[:limit]
    done, skipped, failed = [], len(entries) - len(todo), []
    for i, e in enumerate(todo, 1):
        try:
            res = process_dsh_session(con, e, include_backups=use_backups, write_raw=write_raw)
            con.commit()
            if res:
                done.append(res)
                if progress:
                    progress(i, len(todo), res)
            else:
                skipped += 1
        except Exception as ex:
            con.rollback()
            failed.append({"session_id": e["session_id"], "error": "%s: %s" % (type(ex).__name__, ex)})
            if progress:
                progress(i, len(todo), {"session_id": e["session_id"], "error": str(ex)})
    return {"planned": len(todo), "done": len(done), "unchanged_skipped": skipped,
            "failed": failed, "totals": {
                "atoms": sum(d["atoms"] for d in done),
                "chunks": sum(d["chunks"] for d in done),
                "chars": sum(d["chars"] for d in done)}}


# ------------------------------------------------------------------ staged (other sources)

def ingest_staged(con, staged_root=None, progress=None):
    """Ingest normalized atom files produced by adapters (cursor/claude/qoder/...)."""
    staged_root = staged_root or os.path.join(ROOT, "staged")
    results = []
    if not os.path.isdir(staged_root):
        return {"planned": 0, "done": 0, "sessions": []}
    for source in sorted(os.listdir(staged_root)):
        sdir = os.path.join(staged_root, source)
        if not os.path.isdir(sdir):
            continue
        for fn in sorted(os.listdir(sdir)):
            if not fn.endswith(".atoms.jsonl"):
                continue
            path = os.path.join(sdir, fn)
            st = os.stat(path)
            row = con.execute("SELECT size, mtime FROM sources WHERE source=? AND path=?",
                              (source, path)).fetchone()
            if row and row[0] == st.st_size and row[1] == int(st.st_mtime * 1000):
                continue
            try:
                meta, atoms = _read_staged(path, source)
                if not meta:
                    continue
                chunks = build_chunks(meta, atoms)
                store.replace_session(con, meta, atoms, chunks, [fn])
                con.execute("""INSERT INTO sources(source, path, session_id, group_slug, size, mtime,
                    content_sha1, version, is_backup, status, n_atoms, n_chunks, n_records,
                    ingested_at, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(source, path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,
                    status='ok', n_atoms=excluded.n_atoms, n_chunks=excluded.n_chunks,
                    ingested_at=excluded.ingested_at""",
                    (source, path, meta["session_id"], None, st.st_size, int(st.st_mtime * 1000),
                     None, None, 0, "ok", len(atoms), len(chunks), len(atoms),
                     int(time.time() * 1000), None))
                con.commit()
                results.append({"session_id": meta["session_id"], "source": source,
                                "atoms": len(atoms), "chunks": len(chunks)})
                if progress:
                    progress(len(results), 0, results[-1])
            except Exception as ex:
                con.rollback()
                results.append({"session_id": fn, "source": source, "error": str(ex)})
    return {"planned": len(results), "done": len([r for r in results if "error" not in r]),
            "sessions": results}


def _read_staged(path, source):
    meta, atoms = None, []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("t") == "meta":
                meta = {
                    "session_id": o.get("sessionId") or os.path.basename(path)[:-len(".atoms.jsonl")],
                    "source": o.get("source") or source,
                    "group_slug": o.get("workspace"), "cwd": o.get("cwd"),
                    "origin": o.get("origin") or "import", "parent_session": o.get("parentSession"),
                    "title": o.get("title"), "label": o.get("label"),
                    "created_at": o.get("createdAt"), "versions": [0],
                    "files_short": [os.path.basename(path)],
                }
            elif o.get("t") == "atom":
                text = norm_text(o.get("text") or "")
                if not text:
                    continue
                kind = o.get("kind") or "assistant_text"
                atoms.append({
                    "kind": kind, "role": o.get("role") or "assistant", "text": text,
                    "seq": o.get("seq"), "time": o.get("time"), "turn": o.get("turn"),
                    "step": o.get("step"), "tool_name": o.get("toolName"),
                    "call_id": o.get("callId"), "hash": sha1(kind + "\x00" + text),
                    "prov": o.get("provenance"),
                })
    if not meta or not atoms:
        return None, []
    times = [a["time"] for a in atoms if a.get("time")]
    meta["first_time"] = min(times) if times else meta.get("created_at")
    meta["last_time"] = max(times) if times else meta.get("created_at")
    meta["n_user"] = sum(1 for a in atoms if a["kind"] in ("user_text", "spliced_user"))
    meta["n_assistant"] = sum(1 for a in atoms if a["kind"] in ("assistant_text", "assistant_reasoning"))
    meta["n_tool"] = sum(1 for a in atoms if a["kind"] in ("tool_call", "tool_result"))
    stamp_position(atoms, meta)                    # 六问 q2：观测位（会话粒度）
    return meta, atoms
