"""Coverage reconciliation: what DSH actually has on disk vs what is in the index.

Outputs `reports/coverage.json` and `reports/COVERAGE.md`. The point is to be able
to answer "did you really take everything?" with numbers and a list of anything
that is missing, plus the reason.
"""
import collections
import json
import os
import time

from . import dsh, store
from .pipeline import DB_PATH, REPORT_DIR, RAW


def _day(ms):
    if not ms:
        return None
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ms / 1000))
    except Exception:
        return None


def build(con, write=True):
    entries = dsh.discover_sessions()
    disk = {e["session_id"]: e for e in entries}
    idx = {r[0]: {"chunks": r[1], "atoms": r[2], "first": r[3], "last": r[4], "title": r[5],
                  "source": r[6]}
           for r in con.execute("""SELECT session_id, n_chunks, n_atoms, first_time, last_time,
                                   title, source FROM sessions""")}
    src_rows = con.execute("""SELECT path, session_id, size, mtime, status, n_atoms, n_chunks,
                              n_records, is_backup, note FROM sources WHERE source='dsh'""").fetchall()

    dsh_idx = {sid: v for sid, v in idx.items() if v["source"] == "dsh"}
    missing = sorted(set(disk) - set(dsh_idx))
    orphan = sorted(set(dsh_idx) - set(disk))
    empty = sorted(sid for sid in disk if sid in dsh_idx and dsh_idx[sid]["chunks"] == 0)

    # per-file accounting
    files_indexed, files_failed = 0, []
    for row in src_rows:
        if row[4] == "ok":
            files_indexed += 1
        else:
            files_failed.append({"path": row[0], "status": row[4], "note": row[9]})

    disk_files = sum(len(items) for v in disk.values() for items in v["versions"].values()) \
        + sum(len(v["backups"]) for v in disk.values())
    disk_live = sum(len(items) for v in disk.values() for items in v["versions"].values())
    disk_bytes = sum(it["size"] for v in disk.values()
                     for items in v["versions"].values() for it in items)

    # activity timeline from atoms (NOT session start days: a long session active over
    # several days would otherwise look like a "gap")
    days_by_source = collections.defaultdict(collections.Counter)
    for src, t in con.execute("SELECT source, time FROM atoms WHERE time IS NOT NULL"):
        d = _day(t)
        if d:
            days_by_source[src or "?"][d] += 1
    days = days_by_source.get("dsh", collections.Counter())
    dsh_idx_days = collections.Counter()
    for v in dsh_idx.values():
        d = _day(v["first"])
        if d:
            dsh_idx_days[d] += 1
    span_days = sorted(days)
    gaps = []
    if span_days:
        cur = time.mktime(time.strptime(span_days[0], "%Y-%m-%d"))
        end = time.mktime(time.strptime(span_days[-1], "%Y-%m-%d"))
        while cur <= end:
            d = time.strftime("%Y-%m-%d", time.localtime(cur))
            if days.get(d, 0) == 0:
                gaps.append(d)
            cur += 86400

    # raw archive accounting
    raw_sessions = 0
    for e in entries:
        p = os.path.join(RAW, "dsh", e["group"], e["session_id"], "atoms.jsonl")
        if os.path.exists(p):
            raw_sessions += 1

    hist = con.execute("SELECT kind, COUNT(*) FROM atoms GROUP BY kind ORDER BY 2 DESC").fetchall()
    chunks_by_source = con.execute(
        "SELECT source, COUNT(*), SUM(n_chars) FROM chunks GROUP BY source").fetchall()
    by_source = {}
    for sid, v in idx.items():
        b = by_source.setdefault(v["source"] or "?", {"sessions": 0, "chunks": 0, "atoms": 0})
        b["sessions"] += 1
        b["chunks"] += v["chunks"] or 0
        b["atoms"] += v["atoms"] or 0

    rep = {
        "generatedAt": int(time.time() * 1000),
        "dshStore": dsh.SESSIONS_ROOT,
        "disk": {"sessionDirs": len(entries), "liveFiles": disk_live, "allFiles": disk_files,
                 "compressedBytes": disk_bytes},
        "index": {"sessions": len(idx), "dshSessions": len(dsh_idx),
                  "sessionsWithChunks": sum(1 for v in idx.values() if (v["chunks"] or 0) > 0),
                  "sessionsEmpty": empty, "bySource": by_source,
                  "atomsByKind": dict(hist), "chunksBySource": [
                      {"source": r[0], "chunks": r[1], "chars": r[2]} for r in chunks_by_source]},
        "files": {"indexed": files_indexed, "failed": files_failed},
        "rawArchive": {"sessions": raw_sessions},
        "reconciliation": {
            "missingFromIndex": missing,
            "inIndexButNotOnDisk": orphan,
            "sessionsWithNoConversation": empty,
            "timelineDays": dict(sorted(days.items())),
            "sessionStartDays": dict(sorted(dsh_idx_days.items())),
            "timelineGaps": gaps,
            "activityBySource": {s: {"days": len(c), "first": min(c), "last": max(c),
                                     "atoms": sum(c.values())} if c else {}
                                 for s, c in days_by_source.items()},
        },
    }
    if write:
        os.makedirs(REPORT_DIR, exist_ok=True)
        with open(os.path.join(REPORT_DIR, "coverage.json"), "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=1)
        with open(os.path.join(REPORT_DIR, "COVERAGE.md"), "w", encoding="utf-8") as fh:
            fh.write(_markdown(rep))
    return rep


def _markdown(r):
    L = []
    A = L.append
    A("# DSH 记忆库 · 覆盖对账清单")
    A("")
    A("- 生成时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    A("- DSH 会话存储: `%s`" % r["dshStore"])
    A("")
    A("## 一、磁盘实际有多少（源头）")
    A("")
    A("| 指标 | 数值 |")
    A("|---|---|")
    A("| 会话目录 | %d |" % r["disk"]["sessionDirs"])
    A("| 当前版本文件（v0/v3/v4…） | %d |" % r["disk"]["liveFiles"])
    A("| 含历史备份文件总数 | %d |" % r["disk"]["allFiles"])
    A("| 压缩体积 | %.1f MB |" % (r["disk"]["compressedBytes"] / 1048576))
    A("")
    A("## 二、库里实际收了多少（索引）")
    A("")
    A("| 指标 | 数值 |")
    A("|---|---|")
    A("| 入库会话数 | %d |" % r["index"]["sessions"])
    A("| 其中 DSH 会话 | %d |" % r["index"]["dshSessions"])
    A("| 有对话内容的会话 | %d |" % r["index"]["sessionsWithChunks"])
    A("| 空会话（只有会话头，无对话） | %d |" % len(r["index"]["sessionsEmpty"]))
    A("| 原始档落盘会话数 | %d |" % r["rawArchive"]["sessions"])
    A("| 已索引源文件 | %d |" % r["files"]["indexed"])
    A("| 解析失败文件 | %d |" % len(r["files"]["failed"]))
    A("")
    A("按来源:")
    A("")
    A("| 来源 | 会话 | atom | 切片 |")
    A("|---|---|---|---|")
    for s, v in sorted(r["index"]["bySource"].items()):
        A("| %s | %d | %d | %d |" % (s, v["sessions"], v["atoms"], v["chunks"]))
    A("")
    A("## 三、对账结论")
    A("")
    rc = r["reconciliation"]
    A("- 磁盘有、库里没有的会话: **%d 个**%s" % (
        len(rc["missingFromIndex"]),
        "：" + ", ".join(rc["missingFromIndex"][:20]) if rc["missingFromIndex"] else "（无）"))
    A("- 库里有、磁盘已不存在的会话: **%d 个**%s" % (
        len(rc["inIndexButNotOnDisk"]),
        "（说明源被清理过，我们仍保留）" if rc["inIndexButNotOnDisk"] else "（无）"))
    A("- 有会话头但没有对话内容的空会话: %d 个%s" % (
        len(rc["sessionsWithNoConversation"]),
        "（DSH 里就是空会话，非漏收）" if rc["sessionsWithNoConversation"] else ""))
    A("- 时间轴上无任何活动（无 atom）的日期: %s" % (", ".join(rc["timelineGaps"]) if rc["timelineGaps"] else "无（无整段空白）"))
    A("- 时间轴口径: 按 **atom 实际发生时间**统计（不是按会话创建日），跨天长会话不会造成假空白")
    A("")
    A("### 各来源时间跨度")
    A("")
    A("| 来源 | 活跃天数 | 起 | 止 | atom |")
    A("|---|---|---|---|---|")
    for s, v in sorted(rc.get("activityBySource", {}).items()):
        if v:
            A("| %s | %d | %s | %s | %d |" % (s, v["days"], v["first"], v["last"], v["atoms"]))
    A("")
    A("### DSH 每日活动量（atom 数）")
    A("")
    A("| 日期 | atom | 当日新开会话 |")
    A("|---|---|---|")
    for d, n in sorted(rc["timelineDays"].items()):
        A("| %s | %d | %d |" % (d, n, rc.get("sessionStartDays", {}).get(d, 0)))
    A("")
    A("### atom 类型分布（库里存了什么）")
    A("")
    A("| 类型 | 条数 |")
    A("|---|---|")
    for k, v in sorted(r["index"]["atomsByKind"].items(), key=lambda kv: -kv[1]):
        A("| `%s` | %d |" % (k, v))
    A("")
    if r["files"]["failed"]:
        A("### 解析失败的文件")
        A("")
        for f in r["files"]["failed"]:
            A("- `%s` — %s (%s)" % (f["path"], f["status"], f.get("note")))
        A("")
    return "\n".join(L)
