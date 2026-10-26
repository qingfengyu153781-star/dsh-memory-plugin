#!/usr/bin/env python
"""mem — DSH unified RAG memory library CLI.

    python bin/mem.py ingest                 # 增量入库（只处理变化过的会话文件）
    python bin/mem.py ingest --full          # 全量回填
    python bin/mem.py recall "关键词 问题"    # 检索，带来源引用
    python bin/mem.py ask "问题"              # 组装证据包（给 agent 用）
    python bin/mem.py show <session> --turn 12
    python bin/mem.py sessions --grep 漫剧
    python bin/mem.py grep "精确短语"
    python bin/mem.py coverage | stats | verify
    python bin/mem.py watch --interval 60     # 常驻增量扫盘
    python bin/mem.py console --port 8848     # 本地检索台
"""
import argparse
import io
import json
import os
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

#: 不写 `__pycache__`。为什么：装进 `node_modules/<插件>/backend/` 之后，
#: 字节码缓存会落进包管理器目录（只读或升级即丢），也让「包内容对账」多出噪音。
#: 一行代价，换掉两类麻烦。
sys.dont_write_bytecode = True

from memlib import coverage as coverage_mod          # noqa: E402
from memlib import dsh, paths, pipeline, store       # noqa: E402
from memlib.normalize import tokenize                 # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DB = pipeline.DB_PATH


def human_time(ms, fmt="%Y-%m-%d %H:%M"):
    if not ms:
        return "-"
    try:
        return time.strftime(fmt, time.localtime(ms / 1000))
    except Exception:
        return str(ms)


def parse_day(s, end=False):
    if not s:
        return None
    try:
        t = time.strptime(s, "%Y-%m-%d")
    except ValueError:
        try:
            n = int(s)
            return n
        except ValueError:
            return None
    ms = int(time.mktime(t) * 1000)
    return ms + (86399000 if end else 0)


def excerpt(text, terms, width=760):
    """Show the most query-relevant window of a chunk."""
    if len(text) <= width:
        return text
    low = text.lower()
    pos = -1
    for t in sorted(terms, key=len, reverse=True):
        p = low.find(t.lower())
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    if pos < 0:
        return text[:width] + " …"
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def _pid_alive(pid):
    """Is this pid still running? (Windows: OpenProcess probe; POSIX: signal 0.)"""
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _acquire_lock(path, stale_ms=300000):
    """Single-writer guard: concurrent ingests (plugin + cron + manual + tests) would
    fight over SQLite. The lock records pid+time and is reclaimed immediately when the
    owner is dead, or after `stale_ms` when it cannot be judged."""
    payload = "%d %d" % (os.getpid(), int(time.time() * 1000))
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, payload.encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            parts = open(path, encoding="utf-8", errors="replace").read().split()
            ts = int(parts[1]) if len(parts) > 1 else int(os.path.getmtime(path) * 1000)
            pid = int(parts[0]) if parts else -1
            alive = _pid_alive(pid) if len(parts) > 1 else True
            if not alive or (time.time() * 1000 - ts) > stale_ms:
                os.remove(path)
                return _acquire_lock(path, stale_ms)
        except Exception:
            pass
        return False
    except Exception:
        return True          # never block ingestion because of a lock bug


def cmd_ingest(args):
    lock = os.path.join(pipeline.INDEX_DIR, ".ingest.lock")
    os.makedirs(pipeline.INDEX_DIR, exist_ok=True)
    if not _acquire_lock(lock):
        print(json.dumps({"skipped": "another ingest is already running", "lock": lock},
                         ensure_ascii=False))
        return 0
    try:
        return _cmd_ingest_inner(args)
    finally:
        try:
            os.remove(lock)
        except Exception:
            pass


def _cmd_ingest_inner(args):
    con = store.connect(DB)
    t0 = time.time()
    if args.source in ("all", "dsh"):
        def prog(i, n, res):
            if args.quiet:
                return
            if "error" in res:
                print("[%d/%d] FAIL %s: %s" % (i, n, res.get("session_id"), res["error"]))
            else:
                print("[%d/%d] %s atoms=%s chunks=%s%s" % (
                    i, n, res["session_id"][:28], res.get("atoms"), res.get("chunks"),
                    (" +%d from older versions" % res["kept_from_older"]) if res.get("kept_from_older") else ""))
        r = pipeline.ingest_dsh(con, full=args.full, use_backups=not args.no_backups,
                                write_raw=not args.no_raw, progress=prog, limit=args.limit)
        print(json.dumps({"dsh": r["planned"], "done": r["done"], "skipped": r["unchanged_skipped"],
                          "failed": len(r["failed"]), "atoms": r["totals"]["atoms"],
                          "chunks": r["totals"]["chunks"], "chars": r["totals"]["chars"],
                          "seconds": round(time.time() - t0, 1)}, ensure_ascii=False))
        for f in r["failed"][:20]:
            print("  FAILED", f["session_id"], f["error"])
    if args.source in ("all", "staged"):
        r2 = pipeline.ingest_staged(con)
        print(json.dumps({"staged_sessions": r2["done"]}, ensure_ascii=False))
    if args.coverage:
        coverage_mod.build(con)
        print("coverage ->", os.path.join(pipeline.REPORT_DIR, "COVERAGE.md"))
    # ★ 2026-10-15：**入库收尾自动核验**（R-60/q6 的自动化收口）
    #   为什么放这儿：核验的定义是「事后重开原档比对」——入库刚写完，正是"事后"的
    #   第一时刻；而"要人记得跑 verify-atoms"是这条链上**唯一还靠记性**的环（已被点出）。
    #   代价有界：只核 `verify_method IS NULL`（即本次新增）且最多 limit 条。
    #   ★ 它**不阻断入库**：失败只打 stderr（入库成功与否不受影响）。
    if not getattr(args, "no_verify", False):
        pend = con.execute("SELECT COUNT(*) FROM atoms WHERE verify_method IS NULL"
                           ).fetchone()[0]
        if pend:
            try:
                cmd_verify_atoms(argparse.Namespace(
                    method="raw_reopen", limit=getattr(args, "verify_limit", 8000),
                    apply=True, db=None))
            except Exception as _e:  # noqa: BLE001
                print("[ingest] 收尾核验失败（**不阻断入库**，请手工跑 verify-atoms）：%r" % (_e,),
                      file=sys.stderr)
        else:
            print("[ingest] 收尾核验：无需核验（没有未核验原子）")
    con.close()


def _display(text, raw=False):
    """出答案这一路统一过一遍清洗（raw/ 与索引文本一个字不动，这是可重算的投影层）。

    规则见 src/memlib/clean.py：转义/JSON 残渣、控制字符、不可见字符、超长 base64、
    重复符号长串 → 清或折叠；**代码块、路径、公式里的符号一律不碰**（判定为源码就跳过）。
    `--raw` 可关掉看原文。
    """
    if raw or not text:
        return text
    try:
        from memlib import clean
        return clean.clean_text(text)
    except Exception:
        return text


def _fmt_hit(i, h, terms, full=False, raw=False):
    prov = h.get("prov")
    if isinstance(prov, str):
        try:
            prov = json.loads(prov)
        except Exception:
            prov = {}
    src = h.get("source") or "dsh"
    cite = "%s:%s#turn=%s seq=%s..%s %s" % (
        src, h["session_id"], h.get("turn"), h.get("seq_start"), h.get("seq_end"),
        human_time(h.get("time_start")))
    lines = ["%2d) score=%.3f  %s" % (i, h.get("score", 0), cite)]
    ses = h.get("_session") or {}
    if ses.get("title"):
        lines.append("    标题: %s" % ses["title"])
    if isinstance(prov, dict) and prov.get("files"):
        lines.append("    来源文件: %s" % ", ".join(prov["files"]))
    lines.append("    类型=%s 角色=%s 权重=%s" % (h.get("kind"), h.get("roles"), h.get("weight")))
    if h.get("explain"):
        e = h["explain"]
        lines.append("    打分: 覆盖%.2f 用户字段%.2f 短语%.1f fts%.2f 新鲜度%.2f 命中词=%s" % (
            e["coverage"], e["user_coverage"], e["phrase"], e["fts"], e["recency"],
            ",".join(e["terms_hit"][:12])))
    body = _display(h["text"], raw) if full else excerpt(_display(h["text"], raw), terms)
    for ln in body.split("\n"):
        lines.append("    | " + ln[:400])
    return "\n".join(lines)


DEFAULT_MIN_SCORE = 3.2      # 低于它：判定"库里没有"（实测真实细节题最低 3.28，硬拦 0%）
DEFAULT_STRONG_SCORE = 4.0   # 高于它**且**词覆盖率≥60% 才算"有证据"


def atom_fallback(con, query, k=5):
    """切片检索判 none/weak 时的兜底：回到 atoms 原文做"去空格子串"精确回查。

    为什么需要：atoms 存的是**全文**，而切片对超长 tool_result 只索引了头部
    （TOOL_HEAD=600 / TOOL_FULL=6000 / TOOL_CHUNK_CAP=6）。实测（2026-09-30）4 条失败
    全部落在工具结果深处（长表格 / ^^^ 断言 / 乱码文件名），切片搜不到但原文里有。
    只对 6~80 字的"细节型"查询启用，且只在主检索判 none/weak 时触发，不拖慢正常路径。
    """
    q = re.sub(r"\s+", "", query)
    if len(q) < 6 or len(q) > 80:
        return []
    try:
        rows = con.execute(
            "SELECT atom_id, session_id, turn, seq, time, kind, tool_name, text "
            "FROM atoms WHERE REPLACE(REPLACE(text, ' ', ''), char(10), '') LIKE ? "
            "ORDER BY COALESCE(time,0) DESC LIMIT ?", ("%" + q + "%", k)).fetchall()
    except Exception:
        return []
    out = []
    for aid, sid, turn, seq, ts, kind, tool, text in rows:
        if not text:
            continue
        pos = text.replace(" ", "").replace("\n", "").find(q)
        approx = max(0, pos - 120) if pos >= 0 else 0
        quote = text[approx:approx + 600]
        out.append({
            "level": "atom",
            "citation": "%s:%s#turn=%s seq=%s @%s" % ("dsh", sid, turn, seq, human_time(ts)),
            "session_id": sid, "turn": turn, "kind": kind, "tool": tool,
            "score": None, "quote": quote,
            "why": "切片未命中，由 atoms 原文精确回查命中（工具结果全文只在 atoms 里）",
        })
    return out


def want_atom_fallback(verdict, hits, query):
    """要不要退到 atoms 原文精确回查？

    两种情况都要：
      ① 主检索判 none/weak（分数不够）；
      ② **主检索虽然分高，但返回的前几条里根本没有你要的那句原文** —— 这正是
         "拿具体细节去问、却答不出那句话"的真实成因（token 覆盖够高但原文不在里面）。
    只在 6~80 字的"细节型"查询上启用，避免每次自然语言提问都全表扫 atoms。
    """
    if verdict != "strong":
        return True
    q = re.sub(r"\s+", "", query)
    if len(q) < 6 or len(q) > 80:
        return False
    for h in hits[:3]:
        if q in re.sub(r"\s+", "", h.get("text") or ""):
            return False
    return True


def confidence(hits, min_score=None, strong_score=None):
    """三态置信度闸门（阈值 2026-09-30 当场标定：
    真实细节题 top-1 分数 min 3.28 / 中位 9.17；库里不存在的问题最高可到 5.01。
    单靠分数分不干净 → 追加"问题里的词是否真出现在片段里"的覆盖率判据。）"""
    min_score = DEFAULT_MIN_SCORE if min_score is None else min_score
    strong_score = DEFAULT_STRONG_SCORE if strong_score is None else strong_score
    if not hits:
        return "none", 0.0, "库里没有任何命中。"
    top = hits[0]
    score = top.get("score") or 0.0
    ex = top.get("explain") or {}
    cov = max(ex.get("coverage", 0.0), ex.get("user_coverage", 0.0), ex.get("title_coverage", 0.0))
    if score < min_score:
        return ("none", score,
                "库里没有与该问题足够相关的内容（最高分 %.2f < %.2f）。**不要基于这些片段作答**，"
                "如实说“查不到”；可换更具体的关键词，或用 `mem.py grep --scope atoms` 精确检索原文。"
                % (score, min_score))
    if score >= strong_score and cov >= 0.6:
        return "strong", score, None
    if score >= strong_score and cov < 0.6:
        return ("weak", score,
                "命中了高分片段，但问题里的词只有 %.0f%% 出现在片段里（阈值 60%%）——**弱相关，不确定**："
                "作答前必须自己核对下面原文，或换更精确的关键词再问。" % (cov * 100))
    return ("weak", score,
            "最高分 %.2f 只到了“弱相关”区间（<%.1f）——**不确定**：这些片段只是词面相近，"
            "作答前请自己核对原文；答不上就如实说“查不到”。" % (score, strong_score))


def cmd_recall(args):
    con = store.connect(DB)
    since, until = parse_day(args.since), parse_day(args.until, end=True)
    hits = store.search(con, args.query, k=args.k, session=args.session, source=args.source,
                        since=since, until=until, roles=args.role, kinds=args.kind,
                        turn=args.turn, explain=True)
    ses_ids = {h["session_id"] for h in hits}
    ses = {}
    for sid in ses_ids:
        row = con.execute("SELECT title, label, origin, parent_session, cwd, first_time, last_time,"
                          " n_user, n_chunks FROM sessions WHERE session_id=?", (sid,)).fetchone()
        if row:
            ses[sid] = {"title": row[0], "label": row[1], "origin": row[2], "parent": row[3],
                        "cwd": row[4], "first": row[5], "last": row[6], "n_user": row[7], "n_chunks": row[8]}
    for h in hits:
        h["_session"] = ses.get(h["session_id"], {})
    raw_out = getattr(args, "raw", False)
    for h in hits:
        h["text"] = _display(h["text"], raw_out)          # 出答案这一路统一清洗
    verdict, top, note = confidence(hits, args.min_score, args.strong_score)
    sufficient = verdict == "strong"
    # ★ 2026-10-15：**脑（认知图）融合进本入口**——同一次 recall、同一个返回体。
    #   为什么放在这里而不是另开一个插件：两套记忆各自注入会长出"自己注入→自己召回→
    #   自己当证据"的自噬闭环。融合后：本库管「原始会话可溯源」，脑管「经审的结论」，
    #   由本块的 brain 段一并返回，**由调用方渲染进同一个 <memory_recall> 块**。
    #   `--no-brain` 可关（脑不可用时也会自动降级为 status=unavailable + 原因）。
    brain_sec = None
    if not getattr(args, "no_brain", False):
        try:
            from memlib import brain as _brain
            brain_sec = _brain.recall_for_injection(args.query, k=getattr(args, "brain_k", 3))
        except Exception as _e:  # noqa: BLE001
            # ★ 不静默：脑段取不到 ⇒ 既进返回体（status/reason），也打 stderr。
            #   只往返回体里塞一个字段是不够的——那正是"静默失效"的形态。
            print("[mem.recall] 脑段取不到（本库命中照常返回）：%r" % (_e,), file=sys.stderr)
            brain_sec = {"status": "unavailable", "reason": "适配器异常：%r" % (_e,)}
    if args.json:
        out = {"query": args.query, "k": args.k, "sufficient": sufficient,
               "verdict": verdict, "topScore": top, "minScore": args.min_score,
               "strongScore": args.strong_score, "note": note,
               "hits": [{k: v for k, v in h.items() if k != "tok"} for h in hits]}
        if brain_sec is not None:
            out["brain"] = brain_sec
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        terms = sorted(set(tokenize(args.query)))
        print("查询: %s  (命中 %d 条 / 库内 %d 切片)" % (
            args.query, len(hits), con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]))
        if note:
            print("")
            print("⚠ [%s] %s" % (verdict, note))
        print("")
        for i, h in enumerate(hits, 1):
            print(_fmt_hit(i, h, terms, full=args.full))
            print("")
    con.close()


def cmd_ask(args):
    """Assemble an evidence pack: the exact quotes + citations an agent needs.

    带置信度闸门：最高分低于阈值时明确说"查不到"，不拿语义相近的片段糊过去。
    """
    con = store.connect(DB)
    hits = store.search(con, args.query, k=args.k, session=args.session, source=args.source,
                        since=parse_day(args.since), until=parse_day(args.until, end=True),
                        explain=True)
    verdict, top, note = confidence(hits, args.min_score, args.strong_score)
    fb = atom_fallback(con, args.query) if want_atom_fallback(verdict, hits, args.query) else []
    raw_out = getattr(args, "raw", False)
    for e in fb:
        e["quote"] = _display(e["quote"], raw_out)
    for h in hits:
        h["text"] = _display(h["text"], raw_out)          # 出答案这一路统一清洗
    if fb:
        verdict, sufficient = "strong", True
        note = ("切片检索判为 %s；**atoms 原文精确回查命中 %d 条**（你要的那句原话只在 atoms 全文里，"
                "超长工具结果只索引头部）——以这几条为准，引用见下。"
                % ("查不到" if top < args.min_score else "弱相关", len(fb)))
    sufficient = verdict == "strong"
    pack = {"question": args.query, "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sufficient": sufficient, "verdict": verdict, "topScore": top,
            "minScore": args.min_score, "strongScore": args.strong_score, "note": note,
            "atomFallback": len(fb), "evidence": []}
    for e in fb:
        pack["evidence"].append({
            "citation": e["citation"], "level": "atom", "why": e["why"],
            "session": {"id": e["session_id"], "title": None, "origin": None,
                        "parent": None, "cwd": None, "last": None},
            "score": None, "quote": e["quote"], "raw_source": None,
        })
    for h in hits:
        row = con.execute("SELECT title, origin, parent_session, cwd, last_time FROM sessions"
                          " WHERE session_id=?", (h["session_id"],)).fetchone() or (None,) * 5
        pack["evidence"].append({
            "citation": "%s:%s#turn=%s seq=%s..%s @%s" % (
                h.get("source"), h["session_id"], h.get("turn"), h.get("seq_start"),
                h.get("seq_end"), human_time(h.get("time_start"))),
            "session": {"id": h["session_id"], "title": row[0], "origin": row[1],
                        "parent": row[2], "cwd": row[3], "last": human_time(row[4])},
            "score": h.get("score"),
            "quote": h["text"][:2000],
            "raw_source": (h.get("prov") if isinstance(h.get("prov"), dict) else {}).get("files"),
        })
    if args.json:
        print(json.dumps(pack, ensure_ascii=False, indent=1))
    else:
        print("# 证据包: %s" % args.query)
        if not sufficient:
            print("")
            print("⚠ **查不到**：%s" % note)
            print("（下面片段只是词面相近的候选，不作为答案依据）")
        print("（下面每条都可溯源到具体会话/轮次；回答时请引用 citation）")
        print("")
        for i, e in enumerate(pack["evidence"], 1):
            print("## 证据 %d  [score %.3f]" % (i, e["score"] or 0))
            print("citation: %s" % e["citation"])
            print("会话标题: %s | cwd=%s | 最后活动 %s" % (e["session"]["title"], e["session"]["cwd"],
                                                        e["session"]["last"]))
            print("```")
            print(e["quote"])
            print("```")
            print("")
    con.close()


def cmd_show(args):
    con = store.connect(DB)
    sid = args.session
    row = con.execute("""SELECT session_id, source, group_slug, cwd, origin, parent_session, title,
                         label, first_time, last_time, n_atoms, n_user, n_assistant, n_tool, n_chunks,
                         versions, dir FROM sessions WHERE session_id=?""", (sid,)).fetchone()
    if not row:
        rows = con.execute("SELECT session_id FROM sessions WHERE session_id LIKE ? LIMIT 10",
                           ("%" + sid + "%",)).fetchall()
        print("未找到该会话。相近: %s" % [r[0] for r in rows])
        return
    cols = ["session_id", "source", "group", "cwd", "origin", "parent", "title", "label",
            "first", "last", "atoms", "user", "assistant", "tool", "chunks", "versions", "dir"]
    info = dict(zip(cols, row))
    print("会话: %s" % info["session_id"])
    print("  标题: %s | 来源: %s | origin=%s | 父会话=%s" % (info["title"], info["source"],
                                                          info["origin"], info["parent"]))
    print("  工作目录: %s" % info["cwd"])
    print("  时间: %s → %s | atom=%s (用户%s/助手%s/工具%s) 切片=%s" % (
        human_time(info["first"]), human_time(info["last"]), info["atoms"], info["user"],
        info["assistant"], info["tool"], info["chunks"]))
    atoms = store.session_atoms(con, sid, turn=args.turn, kind=args.kind,
                               limit=args.limit, offset=args.offset)
    print("  atom 显示 %d 条%s:" % (len(atoms), (" (turn=%s)" % args.turn) if args.turn else ""))
    print("")
    for a in atoms:
        head = "[%s] %s" % (a["kind"], human_time(a["time"]))
        print("--- atom#%s %s turn=%s seq=%s %s" % (a["atom_id"], head, a["turn"], a["seq"],
                                                    a["tool_name"] or ""))
        print(a["text"] if args.full else a["text"][:args.width])
    con.close()


def cmd_sessions(args):
    con = store.connect(DB)
    rows = store.session_list(con, limit=args.limit, offset=args.offset, source=args.source,
                              grep=args.grep)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        print("%-38s %-8s %-9s %-16s %6s %6s %7s  %s" % (
            "session", "source", "origin", "last", "user", "chunks", "chars", "title/label"))
        for r in rows:
            print("%-38s %-8s %-9s %-16s %6s %6s %7s  %s" % (
                r["session_id"][:38], r["source"], r["origin"], human_time(r["last_time"]),
                r["n_user"], r["n_chunks"], r["n_chars"], (r["title"] or r["label"] or "")[:44]))
    con.close()


# ---------------------------------------------------------------------------
# session-domains：本会话"真实活动过的项目目录"（2026-10-16）
# ---------------------------------------------------------------------------
# 为什么需要它（实证）：注意力编排器把**别的项目**的记录当成
#   「本项目必须常驻的事实」注入了 —— 某个会话收到的是**另一个项目**（用户明令的红线）
#   的进度报告与审计文件。
#
# 判据为什么**不能只看"用户自己说过的话"**（这条被实测推翻，差点做成假闸）：
#   该会话的 user_text / assistant_text 里**一个项目路径都没有** —— 路径全在
#   tool_call（51+15+9…）/ tool_result（105+50+45…）/ assistant_reasoning 里。
#   ⇒ 只看用户话 = 取不到线索 = 闸永远 noClue = 形同不存在（"判据量错了对象"）。
# 所以：扫**本会话全部原子**（排除插件注入块本身与框架样板，防自我循环），数出项目目录指纹。
# 字符集排除两种分隔符与引号/括号 ⇒ 只取**一级目录**（含引号会截出 `_coldstart_data"` 这种假名）
#
# ★ 工作区根名不再写死：旧版正则里硬编码 `agent`，换台机器/换个工作区就一条都认不出来
#   （对外等于功能缺失）。现在由 `DSH_PROJECT_ROOTS` 指定，逗号分隔，默认 `agent`。
_PROJECT_ROOTS = [s.strip() for s in
                  (os.environ.get("DSH_PROJECT_ROOTS") or "agent").split(",")
                  if s.strip()]
_DOMAIN_RE = re.compile(
    r"[A-Za-z]:[\\/](?:%s)[\\/]([^\s\\/\"'`)\]}>，。；：！？,;]+)"
    % "|".join(re.escape(s) for s in _PROJECT_ROOTS),
    re.I,
)
# 与插件侧 core.js 的 normDomain 同口径（两处必须一致：`伴学智能体_2026` ⇒ `伴学智能体`）
_DOMAIN_NOISE = {"plugin_injection", "system_text", "title", "todo"}


def _norm_domain(raw):
    s = re.sub(r"[_-]?20\d\d(?:[-_]\d\d)?$", "", raw or "", flags=re.I)
    return s.strip("_- \t").lower()


# 口径信号词（**定方向**的话 vs 临时指令）：出现在用户原话里 ⇒ 更像"本项目口径"
_KEYPOINT_HINTS = ("目标", "方向", "口径", "定位", "改成", "变成", "转变", "必须是",
                   "不要", "不准", "为准", "以后", "本项目", "我们做的是", "本体",
                   "核心是", "本质", "路线")


def _keypoint_score(s):
    """给一条用户原话打"口径性"分（可解释、可复算；不引入模型）。"""
    sc = sum(1 for h in _KEYPOINT_HINTS if h in s)
    if 20 <= len(s) <= 160:
        sc += 1                      # 口径句多在中等长度；超长多为贴文档
    return sc


def cmd_session_domains(args):
    con = store.connect(DB)
    rows = con.execute(
        "SELECT kind, text FROM atoms WHERE session_id LIKE ?", (args.session + "%",)).fetchall()
    cnt = {}
    raw_of = {}
    for kind, text in rows:
        if kind in _DOMAIN_NOISE:
            continue
        for m in _DOMAIN_RE.finditer(text or ""):
            raw = m.group(1).rstrip("\\/")
            key = _norm_domain(raw)
            if not key:
                continue
            cnt[key] = cnt.get(key, 0) + 1
            raw_of.setdefault(key, raw)
    top = sorted(cnt.items(), key=lambda kv: -kv[1])[:args.k]

    # ★★ 2026-10-17：**本会话自己定过的口径**（用户原话，带真实出处）★
    #   实证（「眼睛」项目 2026-10-16）：
    #     用户 21:10 定下「从让模型学会剪辑 → 让模型正确认知 3d 空间、真正理解人类看到的画面」；
    #     23:33 用户说「叙述总体目标」⇒ 候选池**6 条全是别会话的日志、本会话口径一条没有**
    #     ⇒ 判官判"无待办"⇒ **空手 ⇒ 记忆 0 注入** ⇒ 模型把本体目标(3D)写成"落笔式临摹"，
    #     用户当场发火「3d部分呢你他妈吃了？」。
    #   病根：检索是**全库字面相似度**，泛化 query（"叙述总体目标"）必然召回别会话垃圾，
    #         而本会话早前定下的口径字面不重合 ⇒ **进不了候选** ⇒ 判官根本看不到。
    #   修法：把本会话**用户原话里带口径的几条**（按"口径性"打分，见 _keypoint_score）
    #         连同**真实出处**（`dsh:<会话>#turn=N`）一起给编排器 —— 由它作为候选送判官。
    #
    #   ★ 为什么不是"取最近 N 条"（第一版就是这么写的，实测不合格）：
    #     在「眼睛」会话上取最近 4 条，拿到的是 t19–t22 的**任务指令**（"整理 blender 文件作附件"），
    #     而 21:10 那条**定目标的话**（t2/t3）根本没进来 ⇒ 治不了 t24 那个洞。
    #     ⇒ 按"口径信号词 + 中等长度"打分后排序，让**定方向的话**优先于临时指令。
    kp = []
    if getattr(args, "keypoints", 0):
        cands = []
        for turn, t, text in con.execute(
                "SELECT turn, time, text FROM atoms WHERE session_id LIKE ? "
                "AND kind IN ('user_text','spliced_user') ORDER BY time DESC LIMIT 400",
                (args.session + "%",)):
            s = (text or "").strip()
            if len(s) < 20 or len(s) > 300:          # 太短无口径；太长（贴文档）不是口径句
                continue
            if any(s.startswith(tag) for tag in ("<memory_recall", "<attention_orchestration",
                                                 "<system-reminder", "<goal_round")):
                continue
            cands.append((_keypoint_score(s), t or 0, turn, s))
        # 口径分高的在前；同分按"新的在前"
        cands.sort(key=lambda x: (-x[0], -x[1]))
        seen_txt = set()
        for sc, t, turn, s in cands:
            k = re.sub(r"\s+", "", s)[:120]          # 同一句被记两遍（user/spliced）⇒ 去重
            if k in seen_txt:
                continue
            seen_txt.add(k)
            kp.append({"turn": turn, "time": t, "text": s, "score": sc})
            if len(kp) >= args.keypoints:
                break

    out = {
        "session_id": args.session,
        "scanned": len(rows),
        "domains": [{"key": k, "name": raw_of[k], "count": v} for k, v in top],
        "keypoints": kp,
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print("会话 %s：扫 %d 条原子（已排除插件注入/框架样板）" % (args.session, len(rows)))
        if not out["domains"]:
            print("  （该会话的原子里没有任何 `<工作区根>\\<目录>` 形态的项目路径；"
                  "工作区根由 DSH_PROJECT_ROOTS 指定，当前 = %s）"
                  % ",".join(_PROJECT_ROOTS))
        for d in out["domains"]:
            print("  %6d  %s" % (d["count"], d["name"]))
        if kp:
            print("  本会话用户口径（可作候选，出处真实）：")
            for x in kp:
                print("    dsh:%s#turn=%s  %s" % (args.session[:12], x["turn"],
                                                  x["text"].replace("\n", " ")[:90]))
    con.close()
    return 0


def cmd_grep(args):
    con = store.connect(DB)
    rows = store.grep(con, args.pattern, k=args.k, scope=args.scope, session=args.session,
                      regex=args.regex)
    print("精确匹配 %d 条 (scope=%s)" % (len(rows), args.scope))
    for r in rows:
        if args.scope == "chunks":
            cid, sid, src, turn, s0, s1, t, text = r
            print("\n--- %s:%s turn=%s seq=%s..%s %s" % (src, sid, turn, s0, s1, human_time(t)))
        else:
            aid, sid, src, turn, seq, t, kind, text = r
            print("\n--- %s atom#%s %s turn=%s seq=%s %s" % (src, aid, kind, turn, seq, human_time(t)))
        print(excerpt(_display(text, getattr(args, "raw", False)), [args.pattern], 800))
    con.close()


def cmd_reindex(args):
    con = store.connect(DB)
    t0 = time.time()
    print("rebuilding FTS index from chunks (tokenizer change / repair)…")
    n = store.reindex_fts(con, progress=lambda k: print("  %d chunks…" % k))
    print(json.dumps({"reindexed": n, "seconds": round(time.time() - t0, 1)}, ensure_ascii=False))
    con.close()


def cmd_paths(args):
    """自证：代码根 / 数据根各是哪儿、数据根是怎么定出来的、库在不在。

    为什么要有这条命令：路径解析曾经是"猜错也返回成功"，出问题时**无从判断**
    插件到底在看哪个库。把它做成一条能跑的命令，就不必靠读代码猜。
    """
    d = paths.describe()
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return 0
    print("代码根   :", d["codeRoot"], "（在 node_modules 内）" if d["insideNodeModules"] else "")
    print("数据根   :", d["dataRoot"])
    print("数据根来源:", d["dataRootSource"],
          {"env": "← DSH_MEMORY_DATA 指定",
           "sibling-of-code": "← 从代码根上溯找到（含 index/ 或 raw/）",
           "default": "← 每用户默认 ~/.dsh/memory-library（该目录可能还没有数据）"}.get(d["dataRootSource"], ""))
    print("库文件   :", d["dbPath"], "存在" if d["dbExists"] else "**不存在**（首次 ingest 会新建）")
    print("raw/     :", d["raw"])
    print("reports/ :", d["reportDir"])
    print()
    print("改法：设 DSH_MEMORY_DATA=<你的记忆库目录>，或在插件目录写 local.config.json 的 memoryBase")
    return 0


def cmd_stats(args):
    con = store.connect(DB)
    st = store.stats(con)
    st["dbPath"] = DB
    st["dbSizeMB"] = round(os.path.getsize(DB) / 1048576, 1) if os.path.exists(DB) else 0
    st["walSizeMB"] = round(os.path.getsize(DB + "-wal") / 1048576, 1) if os.path.exists(DB + "-wal") else 0
    st["first_time_h"] = human_time(st["first_time"])
    st["last_time_h"] = human_time(st["last_time"])
    print(json.dumps(st, ensure_ascii=False, indent=1, default=str))
    con.close()


def cmd_coverage(args):
    con = store.connect(DB)
    rep = coverage_mod.build(con)
    print(json.dumps({"diskSessions": rep["disk"]["sessionDirs"],
                      "indexedSessions": rep["index"]["sessions"],
                      "dshSessions": rep["index"]["dshSessions"],
                      "withContent": rep["index"]["sessionsWithChunks"],
                      "missing": len(rep["reconciliation"]["missingFromIndex"]),
                      "gaps": rep["reconciliation"]["timelineGaps"],
                      "report": os.path.join(pipeline.REPORT_DIR, "COVERAGE.md")},
                     ensure_ascii=False, indent=1))
    con.close()


def cmd_verify(args):
    con = store.connect(DB)
    problems = []
    orphan = con.execute("""SELECT COUNT(*) FROM chunks c LEFT JOIN sessions s
                            ON s.session_id=c.session_id WHERE s.session_id IS NULL""").fetchone()[0]
    if orphan:
        problems.append("orphan chunks: %d" % orphan)
    empty = con.execute("SELECT COUNT(*) FROM chunks WHERE text IS NULL OR LENGTH(text)=0").fetchone()[0]
    if empty:
        problems.append("empty chunks: %d" % empty)
    bad_hash = 0
    for cid, txt, h in con.execute("SELECT chunk_id, text, text_hash FROM chunks"):
        from memlib.normalize import sha1
        if sha1(txt) != h:
            bad_hash += 1
    if bad_hash:
        problems.append("chunk hash mismatch: %d" % bad_hash)
    n_chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    try:
        n_fts = con.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    except Exception as e:
        n_fts = "n/a (%s)" % e
    if isinstance(n_fts, int) and n_fts != n_chunks:
        problems.append("fts rows %d != chunks %d" % (n_fts, n_chunks))
    mismatch = con.execute("""SELECT COUNT(*) FROM sessions s WHERE s.n_chunks !=
                              (SELECT COUNT(*) FROM chunks c WHERE c.session_id=s.session_id)""").fetchone()[0]
    if mismatch:
        problems.append("sessions with wrong n_chunks: %d" % mismatch)
    atom_orphan = con.execute("""SELECT COUNT(*) FROM atoms a LEFT JOIN sessions s
                                 ON s.session_id=a.session_id WHERE s.session_id IS NULL""").fetchone()[0]
    if atom_orphan:
        problems.append("orphan atoms: %d" % atom_orphan)
    dup = con.execute("""SELECT COUNT(*) FROM (SELECT session_id, chunk_id FROM chunks
                        GROUP BY session_id, text_hash HAVING COUNT(*)>1)""").fetchone()[0]
    out = {"chunks": n_chunks, "ftsRows": n_fts, "atoms": con.execute("SELECT COUNT(*) FROM atoms").fetchone()[0],
           "sessions": con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
           "duplicateChunkTexts": dup, "problems": problems,
           "verdict": "OK" if not problems else "PROBLEMS"}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    con.close()
    return 0 if not problems else 1


def cmd_watch(args):
    con = store.connect(DB)
    rounds = 0
    while True:
        r = pipeline.ingest_dsh(con, full=False, use_backups=not args.no_backups, write_raw=True)
        print("[%s] ingested %d sessions (%d atoms, %d chunks, %d failed) | unchanged=%d" % (
            time.strftime("%H:%M:%S"), r["done"], r["totals"]["atoms"], r["totals"]["chunks"],
            len(r["failed"]), r["unchanged_skipped"]))
        sys.stdout.flush()
        rounds += 1
        if args.once:
            break
        time.sleep(args.interval)
    con.close()


def cmd_repair_provenance(args):
    """来源面修复（R-60 的数据侧）：schema 迁移 + 补 tool_name + 补观测位。

    默认 **dry-run**：只报"会改多少"，一个字节都不写。`--apply` 才写。
    每一步都打 before/after 读数（"报了多少就改了多少"必须能对账）。
    """
    from memlib import store as _store
    db = args.db or pipeline.DB_PATH
    con = _store.connect(db)          # connect 内部会跑 migrate（幂等补列/建索引）
    print("库：%s" % db)
    print("schema_version =", con.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])

    before_missing = con.execute(
        "SELECT count(*) FROM atoms WHERE kind='tool_result' "
        "AND (tool_name IS NULL OR trim(tool_name)='')").fetchone()[0]
    backfillable = con.execute("""
      SELECT count(*) FROM atoms r
      WHERE r.kind='tool_result' AND (r.tool_name IS NULL OR trim(r.tool_name)='')
        AND r.call_id IS NOT NULL AND trim(r.call_id)<>''
        AND EXISTS (SELECT 1 FROM atoms c
                    WHERE c.session_id=r.session_id AND c.call_id=r.call_id
                      AND c.kind='tool_call' AND c.tool_name IS NOT NULL
                      AND trim(c.tool_name)<>'')""").fetchone()[0]
    before_pos = con.execute(
        "SELECT count(*) FROM atoms WHERE observation_position IS NULL "
        "OR trim(observation_position)=''").fetchone()[0]
    print("① tool_result 缺 tool_name：%d（其中可按 call_id 回填 %d）"
          % (before_missing, backfillable))
    print("② 缺 observation_position：%d" % before_pos)
    before_tb = con.execute("SELECT count(*) FROM atoms WHERE time_basis IS NULL").fetchone()[0]
    print("③ time_basis 未显式：%d（v3：把「没时间戳」从沉默的 NULL 变成 "
          "record/source_absent 显式状态）" % before_tb)

    if not args.apply:
        print("\n[dry-run] 未写**数据**。加 --apply 执行回填。")
        print("         ★ 注意：schema 迁移（补列/建索引）是幂等的、**在 dry-run 里也会做**——"
              "否则本命令量不到 observation_position 的缺口。它只改表结构，不改任何既有行的值。")
        con.close()
        return 0

    t0 = time.time()
    con.execute("BEGIN")
    cur = con.execute("""
      UPDATE atoms SET tool_name = (
        SELECT c.tool_name FROM atoms c
        WHERE c.session_id = atoms.session_id AND c.call_id = atoms.call_id
          AND c.kind='tool_call' AND c.tool_name IS NOT NULL AND trim(c.tool_name)<>''
        LIMIT 1)
      WHERE kind='tool_result' AND (tool_name IS NULL OR trim(tool_name)='')
        AND call_id IS NOT NULL AND trim(call_id)<>''
        AND EXISTS (SELECT 1 FROM atoms c
                    WHERE c.session_id=atoms.session_id AND c.call_id=atoms.call_id
                      AND c.kind='tool_call' AND c.tool_name IS NOT NULL
                      AND trim(c.tool_name)<>'')""")
    n_tool = cur.rowcount
    # 观测位：从 sessions 表 join 出「源·会话来源·工作区」（与会话粒度一致）
    cur = con.execute("""
      UPDATE atoms SET observation_position = (
        SELECT s.source || '·' || COALESCE(NULLIF(trim(s.origin),''),'unknown') ||
               CASE WHEN COALESCE(trim(s.cwd), trim(s.dir), '') <> ''
                    THEN '·' || COALESCE(NULLIF(trim(s.cwd),''), trim(s.dir)) ELSE '' END
        FROM sessions s WHERE s.session_id = atoms.session_id)
      WHERE (observation_position IS NULL OR trim(observation_position)='')
        AND session_id IN (SELECT session_id FROM sessions)""")
    n_pos = cur.rowcount
    # ③ time_basis 显式化（v3）：它是**从 time 派生**的，不是编造 ——
    #    `record`＝取自原始记录、`source_absent`＝源头就没有逐条时间戳。
    #    目的：让「我们没读到」与「源头没有」一眼可分（null 是沉默的缺口）。
    cur = con.execute("""
      UPDATE atoms SET time_basis = CASE WHEN time IS NULL THEN 'source_absent'
                                         ELSE 'record' END
      WHERE time_basis IS NULL""")
    n_tb = cur.rowcount
    con.commit()
    after_missing = con.execute(
        "SELECT count(*) FROM atoms WHERE kind='tool_result' "
        "AND (tool_name IS NULL OR trim(tool_name)='')").fetchone()[0]
    after_pos = con.execute(
        "SELECT count(*) FROM atoms WHERE observation_position IS NULL "
        "OR trim(observation_position)=''").fetchone()[0]
    after_tb = con.execute("SELECT count(*) FROM atoms WHERE time_basis IS NULL").fetchone()[0]
    con.execute("INSERT INTO ingest_log(ts, source, session_id, action, detail) "
                "VALUES(?,?,?,?,?)",
                (int(time.time() * 1000), "repair", "-", "repair-provenance",
                 "tool_name +%d→缺%d / observation_position +%d→缺%d / time_basis +%d→缺%d"
                 % (n_tool, after_missing, n_pos, after_pos, n_tb, after_tb)))
    con.commit()
    print("\n[apply] 用时 %.1fs" % (time.time() - t0))
    print("① tool_name：补 %d 条 ⇒ 仍缺 %d（原缺 %d）" % (n_tool, after_missing, before_missing))
    print("② observation_position：补 %d 条 ⇒ 仍缺 %d（原缺 %d）" % (n_pos, after_pos, before_pos))
    print("③ time_basis：显式化 %d 条 ⇒ 仍缺 %d（原缺 %d）" % (n_tb, after_tb, before_tb))
    con.close()
    return 0


def _open_lines(path):
    """按行产出原文（支持 `.zstd` 会话档）；返回 (可迭代, 备注) 或 (None, 原因)。

    ★ `.zstd` 走**流式解压逐行读**（`zstandard.stream_reader` + TextIOWrapper），
      不把整档解压进内存。原先用"解压后 >64 MB 就不核"的护栏，实测把 **68,242 条**
      原子挡在核验之外（大会话档）——护栏本身没问题，**问题是它让我以为"核完了"**。
      改成流式后这个上限不需要了（峰值内存与档无关）。
    ★ 形态护栏：有的源的 `prov.file` **根本不是逐行 JSON**（实测：cursor 的
      `state.vscdb` 是 **sqlite 库**）⇒ 按行比对这套方法对它不适用，显式记
      `source_not_jsonl` 并计入报告，**不混进"漂移"**。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError as e:  # noqa: BLE001
        return None, "open_error:%s" % e
    # ★ 按**内容魔数**判形态，不按扩展名：DSH 的备份档名是
    #   `session.v3.jsonl.zstd.bak-20260919-120530` —— **不以 `.zstd` 结尾**。
    #   旧实现按扩展名分支 ⇒ 把 zstd 压缩档当纯文本打开 ⇒ 读出二进制垃圾 ⇒
    #   JSON 解析失败、被记成 `source_unreadable`（实测 61 条就是这么来的）。
    #   教训同 R-59：**按形状/措辞判定会漏**，要按形态判定。
    ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
    if head.startswith(ZSTD_MAGIC):
        zstd = getattr(dsh, "zstd", None)
        if zstd is None:
            return None, "source_no_zstd_module"
        try:
            fh = open(path, "rb")
            reader = zstd.ZstdDecompressor().stream_reader(fh)
            # newline="\n"：**关掉 universal newlines**，与入库侧 `split("\n")` 逐字对齐
            #   （实测同一档：universal=43,057 行 vs split=43,058 行 ⇒ 行号会错位）。
            return io.TextIOWrapper(reader, encoding="utf-8", errors="replace",
                                    newline="\n"), None
        except Exception as e:  # noqa: BLE001
            return None, "read_error:%s" % e
    if head.startswith(b"SQLite format 3"):
        return None, "source_not_jsonl:sqlite"
    try:
        # 同上：newline="\n" 与入库侧 `split("\n")` 逐字对齐
        fh = open(path, encoding="utf-8", errors="replace", newline="\n")
    except OSError as e:  # noqa: BLE001
        return None, "open_error:%s" % e
    return fh, None


def _record_blobs(rec):
    """把一条原始记录碾成**两种可比形态**，返回 (字符串叶子拼接, 原始 JSON 串)。

    ★ 为什么要两种：口径错会让"其实一致"误判成"漂移"，而**假漂移比不核还坏**
      （会让人去修根本没坏的东西）。本命令的比对口径校准了三轮：
      第 1 轮直接拿 `json.dumps(rec)` 比 ⇒ 转义面误判 **79%** 为漂移；
      第 2 轮改抽字符串叶子 ⇒ 降到 26%，但 `tool_call` 的正文是**工具输入的 JSON**
      （`{"command":...}`），抽叶子会把键丢掉 ⇒ 又误判；
      第 3 轮：叶子拼接 + 原始 JSON 串（并让正文侧也做一次转义）⇒ 两形态任一命中即算一致。
    """
    buf = []

    def walk(x):
        if isinstance(x, str):
            buf.append(x)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)

    walk(rec)
    leaves = re.sub(r"\s+", "", " ".join(buf))
    raw = re.sub(r"\s+", "", json.dumps(rec, ensure_ascii=False))
    return leaves, raw


def _confirmed(want, leaves, raw):
    """正文是否**在**这条原始记录里（三种形态任一命中即算一致）。"""
    if not want:
        return False
    w = re.sub(r"\s+", "", want)            # 空白全去：json.dumps 会在 ":" 后加空格，
    if w in leaves or w in raw:             # 而正文常是紧凑 JSON ⇒ 只归一"个数"仍对不上
        return True
    esc = w.replace('"', '\\"')             # 正文是 JSON ⇒ 原记录里是转义形态
    return esc in raw or esc in leaves


def _parts(text):
    """把正文切成"可单独核对的分片"，并**剥掉抽取器的模板包装**。

    用途：派生/拼接类原子（`plugin_injection` 把 AGENTS.md 与环境块拼一起、
    `todo` 由多条拼成）**整段**天然不是原记录的连续子串，但**每一片**都该能找到。
    用"所有分片都能找到"作为**弱于连续、强于不核**的一档证据。

    ★ 2026-10-15 补：只按 `\\n` 切还不够——抽取器会**按模板拼**正文，模板本身
      在原档里没有，于是分片也找不到。实测这类占"没验上"的绝大多数：
      `todo` 251（`- [pending] 内容`）、`command` 90（`/名字 参数`）、
      `tool_result` 111（多 content 块按 `\\n` 拼）。故这里剥掉常见模板包装：
      · 列表标记 `- [xxx] `（todo）
      · 行首 `/`（command 的斜杠命令）
      · 再把"斜杠命令"拆成 名字 / 参数 两片
    """
    out = []
    for seg in (text or "").split("\n"):
        s = seg.strip()
        if not s:
            continue
        # 剥列表标记：- [pending] 内容 / - [done] 内容
        s = re.sub(r"^-\s*\[[^\]]*\]\s*", "", s)
        # 斜杠命令：/name args ⇒ 名字与参数各自成片（原档里是分开的字段）
        m = re.match(r"^/(\S+)\s*(.*)$", s)
        if m:
            for piece in (m.group(1), m.group(2)):
                p = re.sub(r"\s+", "", piece)
                if len(p) >= 4:          # 名字/参数各自成片（原档里是分开的字段）
                    out.append(p)
            continue
        s = re.sub(r"\s+", "", s)
        # 阈值 8（不是 12）：中文 8 字已足够有辨识度；**分片越多、要求"全找到"就越严**
        if len(s) >= 8:
            out.append(s)
    return out


def _confirmed_parts(segs, leaves, raw):
    """所有分片是否都能在记录里找到。

    ★ 允许只有 1 片：剥掉模板后的单片段（如 `todo` 只有一条、`/cmd args` 拆成的名字）
      照样是**有辨识度的**证据；要求 ≥2 片会把这类一律打成"没验上"。
      分片本身已有长度门槛（≥8 / 斜杠件 ≥4）⇒ 不靠"片数"兜底，靠"每片都要找到"。
    """
    if not segs:
        return False
    for s in segs:
        esc = s.replace('"', '\\"')
        if not (s in leaves or s in raw or esc in raw or esc in leaves):
            return False
    return True


_STAGED_CACHE: dict = {}
_staged_cache: dict = {}   # 同名小写别名（下面用这个）


def _staged_index() -> dict:
    """会话 → 采集件路径（`staged/<source>/<session>.atoms.jsonl`）。"""
    idx = {}
    root = os.path.join(pipeline.ROOT, "staged")
    if not os.path.isdir(root):
        return idx
    for src in os.listdir(root):
        d = os.path.join(root, src)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.endswith(".atoms.jsonl"):
                idx[fn[:-len(".atoms.jsonl")]] = os.path.join(d, fn)
    return idx


def _staged_path(session_id):
    global _STAGED_IDX
    try:
        return _STAGED_IDX.get(str(session_id))
    except NameError:
        _STAGED_IDX = _staged_index()
        return _STAGED_IDX.get(str(session_id))


def cmd_brain_status(args):
    """脑（认知图）状态：只读巡检 + 服务信息 + 最近事件。**融合后的统一入口**。"""
    from memlib import brain
    ok, why = brain.available()
    if not ok:
        print(json.dumps({"status": "unavailable", "reason": why}, ensure_ascii=False, indent=1))
        return 1
    r = brain.with_brain(lambda b: {"brain_status": b.status(), "info": b.info(),
                                    "recent": b.recent(getattr(args, "limit", 5))},
                         timeout=float(getattr(args, "timeout", 20)))
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r.get("status") == "ok" else 1


def cmd_brain_remember(args):
    """把一条**结论**写成 CCG 卡存进脑（`text` 类**必须**带六要素，缺了会被脑拒绝并列出缺失项）。"""
    from memlib import brain
    text = args.content
    if args.file:
        text = io.open(args.file, encoding="utf-8").read()
    tags = [t for t in (args.tags or "").split(",") if t.strip()]
    # ★ 正路：**分区靠 `domain:<名>` 标签**（脑的 route_key 只认它、或 condition_space 的
    #   观测位；都没有则落 orphan）。实测：不写 domain ⇒ 只有 1 个桶，脑的 info 会如实报
    #   「单桶：条件路由本就不可用」；写上 domain ⇒ 桶数随之增长（实测 1 → 3）。
    if getattr(args, "domain", ""):
        tags = ["domain:" + args.domain] + [t for t in tags if not t.startswith("domain:")]
    kw = {"content_kind": args.kind}
    if tags:
        kw["tags"] = tags
    r = brain.with_brain(lambda b: b.remember(text, **kw))
    print(json.dumps(r, ensure_ascii=False, indent=1))
    if r.get("status") == "ok" and r.get("ok"):
        print("[brain] 已写入 id=%s（root=%s）" % (r.get("id"), brain.BRAIN_ROOT))
        return 0
    return 1


def cmd_brain_recall(args):
    """按 query 取**经审的结论**（注入面口径：验证态 + 不适用条件，过期/存疑的不给）。"""
    from memlib import brain
    r = brain.recall_for_injection(args.query, k=args.k)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r.get("status") == "ok" else 1


def _purge_targets(con, keep_nameless=False):
    """要删的原子：**任何证据通路都核对不上**的那些（用户 2026-10-16 裁决：无效的删掉不留）。

    范围（逐条可复算）：
      · `verify_status IN ('source_not_jsonl','text_not_found')` —— 原档与采集件**都核不上**
      · 无名 `tool_result`（除非 --keep-nameless）—— 原档无对应 tool/call，且正文是"调用被中断"的提示语
    保留：`staged_ok`（能在采集件里核上）、一切 `reopened_*` / `cross_confirmed`。
    """
    where = ["verify_status IN ('source_not_jsonl','text_not_found')"]
    if not keep_nameless:
        where.append("(kind='tool_result' AND (tool_name IS NULL OR trim(tool_name)=''))")
    q = "SELECT atom_id, session_id FROM atoms WHERE " + " OR ".join(where)
    return [tuple(r) for r in con.execute(q)]


def cmd_purge_unverified(args):
    """删掉**核对不上**的原子，并**同步重建受影响会话的切片**（否则 recall 还会从 chunks 捞出内容）。

    安全三件套：①默认 dry-run ②`--apply` 时先**导出留底** JSONL ③再**整库备份**（可用 --no-db-backup 关）。
    ★ 为什么必须重建切片：`recall` 查的是 `chunks`（按会话派生），只删 atoms 等于没删干净。
    """
    con = store.connect(DB)
    tgt = _purge_targets(con, keep_nameless=args.keep_nameless)
    by_sess = {}
    for aid, sid in tgt:
        by_sess.setdefault(sid, []).append(aid)
    print("待删原子 = %d 条，涉及会话 %d 个" % (len(tgt), len(by_sess)))
    rows = con.execute(
        "SELECT verify_status, count(*) FROM atoms WHERE atom_id IN (%s) GROUP BY 1"
        % ",".join(str(a) for a, _ in tgt) if tgt else "SELECT '',0").fetchall()
    for r in rows:
        print("   %-20s %d" % r)
    for sid, ids in sorted(by_sess.items(), key=lambda x: -len(x[1]))[:6]:
        n_all = con.execute("SELECT count(*) FROM atoms WHERE session_id=?", (sid,)).fetchone()[0]
        print("   %s 删 %d / 会话内 %d" % (sid[:40], len(ids), n_all))
    if not tgt:
        print("没有可删的（无核对不上的原子）")
        return 0
    if not args.apply:
        print("\n[dry-run] 未删任何东西。加 --apply 执行；`--keep-nameless` 可保留那 1 条无名提示。")
        con.close()
        return 0

    ts = time.strftime("%Y%m%d_%H%M%S")
    bdir = os.path.join(pipeline.ROOT, "backups")
    os.makedirs(bdir, exist_ok=True)
    # ① 导出留底（被删的行原样存 JSONL，可按 atom_id 回插）
    exp = os.path.join(bdir, "purged_atoms_%s.jsonl" % ts)
    cols = [r[1] for r in con.execute("PRAGMA table_info(atoms)")]
    with io.open(exp, "w", encoding="utf-8") as fh:
        for aid, sid in tgt:
            row = con.execute("SELECT %s FROM atoms WHERE atom_id=?" % ",".join(cols),
                              (aid,)).fetchone()
            fh.write(json.dumps(dict(zip(cols, row)), ensure_ascii=False) + "\n")
    print("① 已导出留底：%s（%d 行，可按 atom_id 回插）" % (exp, len(tgt)))
    # ② 整库备份（一致性快照，走 sqlite backup API）
    if not args.no_db_backup:
        bpath = os.path.join(bdir, "memory_before_purge_%s.sqlite" % ts)
        t0 = time.time()
        dst = sqlite3.connect(bpath)
        with dst:
            con.backup(dst)
        dst.close()
        print("② 已整库备份：%s（%.1fs，%.0f MB）"
              % (bpath, time.time() - t0, os.path.getsize(bpath) / 1048576.0))
    # ③ 逐会话：过滤后重建切片（原子替换）
    n_del = n_chunk_before = n_chunk_after = 0
    for sid, ids in by_sess.items():
        srow = con.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
        if srow is None:
            continue
        scols = [r[1] for r in con.execute("PRAGMA table_info(sessions)")]
        meta = dict(zip(scols, srow))
        source_files = json.loads(meta.get("files") or "[]")
        acols = [r[1] for r in con.execute("PRAGMA table_info(atoms)")]
        keep = []
        for row in con.execute("SELECT %s FROM atoms WHERE session_id=? ORDER BY seq"
                               % ",".join(acols), (sid,)):
            a = dict(zip(acols, row))
            if a["atom_id"] in ids:
                continue
            keep.append({
                "seq": a["seq"], "time": a["time"], "turn": a["turn"], "step": a["step"],
                "role": a["role"], "kind": a["kind"], "text": a["text"], "hash": a["text_hash"],
                "tool_name": a["tool_name"], "call_id": a["call_id"], "note": a["note"],
                "prov": json.loads(a["prov"]) if a["prov"] else None,
                "extra_from_version": a["extra_from_version"],
                "observation_position": a["observation_position"],
                "verify_status": a["verify_status"], "verify_method": a["verify_method"],
                "verify_ref": a["verify_ref"], "time_basis": a["time_basis"]})
        n_chunk_before += con.execute("SELECT count(*) FROM chunks WHERE session_id=?",
                                      (sid,)).fetchone()[0]
        m2 = dict(meta)
        times = [x["time"] for x in keep if x.get("time")]
        m2["first_time"] = min(times) if times else meta.get("first_time")
        m2["last_time"] = max(times) if times else meta.get("last_time")
        m2["n_user"] = sum(1 for x in keep if x["kind"] in ("user_text", "spliced_user"))
        m2["n_assistant"] = sum(1 for x in keep
                               if x["kind"] in ("assistant_text", "assistant_reasoning"))
        m2["n_tool"] = sum(1 for x in keep if x["kind"] in ("tool_call", "tool_result"))
        chunks = pipeline.build_chunks(m2, keep)
        con.execute("BEGIN")
        store.replace_session(con, m2, keep, chunks, source_files)
        con.commit()
        n_chunk_after += len(chunks)
        n_del += len(ids)
    store.log(con, "purge", "-", "purge-unverified",
              "删原子 %d 条 / 会话 %d 个；导出 %s；chunks %d→%d"
              % (n_del, len(by_sess), os.path.basename(exp), n_chunk_before, n_chunk_after))
    con.commit()
    print("③ 已删 %d 条；受影响会话切片 %d → %d" % (n_del, n_chunk_before, n_chunk_after))
    left = con.execute("SELECT count(*) FROM atoms WHERE verify_status IN "
                       "('source_not_jsonl','text_not_found')").fetchone()[0]
    print("④ 复核：仍核对不上的原子 = %d；无名 tool_result = %d"
          % (left, con.execute("SELECT count(*) FROM atoms WHERE kind='tool_result' "
                               "AND (tool_name IS NULL OR trim(tool_name)='')").fetchone()[0]))
    con.close()
    return 0


def _norm_for_match(s):
    """归一化文本，用于与 Cursor bubble 的正文比对。

    实测踩过的坑：我们的 atom 正文常被**包上标签**（如 `<user_query>\\nConfigure Display Language\\n</user_query>`），
    而 Cursor bubble 里是**裸文本**（`Configure Display Language`）⇒ 直接整段比对 **0 命中**。
    故这里剥掉尖括号标签再压空白。
    """
    s = re.sub(r"</?[A-Za-z_][A-Za-z0-9_\- ]*>", "", s or "")
    return re.sub(r"\s+", "", s)


def _cursor_bubble_times():
    """从 Cursor 的 `state.vscdb` 读**真时间**：`bubbleId:<composerId>:<bubbleId>` 的 `createdAt`。

    为什么要它：Cursor 的 `agent-transcripts/*.jsonl` **逐条没有时间戳**（适配器文档自述），
    但 Cursor 自己的 bubble 记录**有** `createdAt`（实测 481 条全是 ISO 串；其中 89 条带 text）。
    ⇒ 缺失的时间戳**不是没有源**，是我们没去取。
    返回 {(composer, 归一文本): [epoch_ms,...]}（另建 ("", norm) 的全局键作兜底）。
    """
    db = os.path.join(os.environ.get("APPDATA", ""), "Cursor", "User", "globalStorage",
                      "state.vscdb")
    if not os.path.isfile(db):
        return None, "找不到 Cursor state.vscdb：%s" % db
    import datetime
    con = sqlite3.connect("file:" + db.replace("\\", "/") + "?mode=ro", uri=True, timeout=20)
    idx, n_bubble, n_text = {}, 0, 0
    for key, val in con.execute("SELECT key, value FROM cursorDiskKV "
                                "WHERE key LIKE 'bubbleId:%'"):
        try:
            o = json.loads(val)
        except Exception:  # noqa: BLE001
            continue
        n_bubble += 1
        t = o.get("createdAt")
        txt = o.get("text")
        if not t:
            continue
        # 正文候选：① 普通消息的 text ② 工具调用的 params/rawArgs（Cursor 把工具参数放这里）
        cands = []
        if isinstance(txt, str) and txt.strip():
            cands.append(txt)
        tfd = o.get("toolFormerData")
        if isinstance(tfd, dict):
            for f in ("params", "rawArgs"):
                v = tfd.get(f)
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False)
                if isinstance(v, str) and v.strip():
                    cands.append(v)
        if not cands:
            continue
        try:
            dt = datetime.datetime.strptime(t.replace("Z", ""), "%Y-%m-%dT%H:%M:%S.%f")
        except ValueError:
            continue
        ms = int(dt.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
        parts = key.split(":")
        composer = parts[1] if len(parts) > 1 else ""
        got = False
        for ct in cands:
            norm = _norm_for_match(ct)
            if len(norm) < 6:
                continue
            got = True
            idx.setdefault((composer, norm), []).append(ms)
            idx.setdefault(("", norm), []).append(ms)
        if got:
            n_text += 1
    con.close()
    return idx, "读了 %d 条 bubble，其中 %d 条有可比对正文（text 或工具 params/rawArgs）" \
        % (n_bubble, n_text)


def cmd_fix_cursor_time(args):
    """把 cursor 源**缺失的时间戳**补成 Cursor 自己记录的真时间（`bubbleId.createdAt`）。

    判据（不许编造）：只在**同会话内文本唯一命中**时写入；多处命中或命中不到 ⇒ **保持 NULL**，
    并如实报"补不上"的条数。写 `time_basis='cursor_bubble_createdAt'` 标明来源。
    """
    con = store.connect(DB)
    idx, note = _cursor_bubble_times()
    print("[fix-cursor-time] %s" % note)
    if idx is None:
        con.close()
        return 1
    rows = con.execute("SELECT atom_id, session_id, kind, text FROM atoms "
                       "WHERE source='cursor' AND time IS NULL").fetchall()
    print("待补时间的 cursor 原子 = %d 条" % len(rows))
    plan, ambiguous, missing = [], 0, 0
    for aid, sid, kind, text in rows:
        norm = _norm_for_match(text)
        cand = idx.get((sid, norm)) or idx.get(("", norm)) or []
        if not cand and len(norm) >= 12:
            # 退一步：包含判定（我们的正文比 bubble 多包了标签/多拼了片段时）
            for (comp, bnorm), times in idx.items():
                if comp not in ("", sid) or len(bnorm) < 12:
                    continue
                if bnorm in norm or norm in bnorm:
                    cand = cand + times
        uniq = sorted(set(cand))
        if len(uniq) == 1:
            plan.append((aid, sid, uniq[0], norm[:40]))
        elif len(uniq) > 1:
            ambiguous += 1
        else:
            missing += 1
    print("  可补（同会话内文本唯一命中）= %d ｜ 多处命中（不敢写）= %d ｜ 采集侧没有 = %d"
          % (len(plan), ambiguous, missing))
    for aid, sid, ms, s in plan[:3]:
        print("   样本 atom=%s -> %s  (%s)" % (aid, time.strftime("%Y-%m-%d %H:%M:%S",
                                                                time.localtime(ms / 1000.0)), s))
    if not args.apply:
        print("\n[dry-run] 未写任何东西。加 --apply 执行（写入 time + time_basis，并重建受影响会话切片）。")
        con.close()
        return 0
    sessions = sorted({sid for _, sid, _, _ in plan})
    con.execute("BEGIN")
    for aid, sid, ms, _s in plan:
        con.execute("UPDATE atoms SET time=?, time_basis='cursor_bubble_createdAt' "
                    "WHERE atom_id=?", (ms, aid))
    con.commit()
    # 时间变了 ⇒ 该会话切片的 time_start/time_end 要跟着重算（注入面显示的就是它）
    rebuilt = 0
    for sid in sessions:
        srow = con.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
        if srow is None:
            continue
        scols = [r[1] for r in con.execute("PRAGMA table_info(sessions)")]
        meta = dict(zip(scols, srow))
        source_files = json.loads(meta.get("files") or "[]")
        acols = [r[1] for r in con.execute("PRAGMA table_info(atoms)")]
        keep = []
        for row in con.execute("SELECT %s FROM atoms WHERE session_id=? ORDER BY seq"
                               % ",".join(acols), (sid,)):
            a = dict(zip(acols, row))
            keep.append({
                "seq": a["seq"], "time": a["time"], "turn": a["turn"], "step": a["step"],
                "role": a["role"], "kind": a["kind"], "text": a["text"], "hash": a["text_hash"],
                "tool_name": a["tool_name"], "call_id": a["call_id"], "note": a["note"],
                "prov": json.loads(a["prov"]) if a["prov"] else None,
                "extra_from_version": a["extra_from_version"],
                "observation_position": a["observation_position"],
                "verify_status": a["verify_status"], "verify_method": a["verify_method"],
                "verify_ref": a["verify_ref"], "time_basis": a["time_basis"]})
        times = [x["time"] for x in keep if x.get("time")]
        m2 = dict(meta)
        m2["first_time"] = min(times) if times else meta.get("first_time")
        m2["last_time"] = max(times) if times else meta.get("last_time")
        chunks = pipeline.build_chunks(m2, keep)
        con.execute("BEGIN")
        store.replace_session(con, m2, keep, chunks, source_files)
        con.commit()
        rebuilt += 1
    store.log(con, "fix", "-", "fix-cursor-time",
              "补时间 %d 条 / 会话 %d 个；多处命中 %d、采集侧没有 %d；来源 bubbleId.createdAt"
              % (len(plan), rebuilt, ambiguous, missing))
    con.commit()
    left = con.execute("SELECT count(*) FROM atoms WHERE time IS NULL").fetchone()[0]
    print("已补 %d 条；重建切片会话 %d 个。复核：仍无时间戳 = %d 条（多处命中/采集侧没有的那部分）"
          % (len(plan), rebuilt, left))
    print("  time_basis 分布 =", con.execute("SELECT time_basis, count(*) FROM atoms "
                                            "GROUP BY 1").fetchall())
    con.close()
    return 0


def cmd_verify_atoms(args):
    """来源核验（R-60 的 q6）——两条真能跑的核验通路，结果落 verify_* 三列。

    ① `cross_source`：同一 `text_hash` 在 **≥2 个不同 source** 都出现过 ⇒ 交叉确认。
    ② `raw_reopen`：重开 `prov.file`、取第 `prov.line` 行、解析 JSON，看该原子正文
       是否**在**这条原始记录里 ⇒ 与原始记录一致 / 已漂移。支持 `.zstd` 会话档。
    默认 **dry-run**（只报读数）；`--apply` 才写回。

    ★ 口径校准花了 4 轮（21% → 74% → 76% → 100%），全因**比对口径**而不是库漂：
      ① 直接跟 `json.dumps(rec)` 比 ⇒ JSON 转义面误判 79% 为漂移；
      ② 改抽字符串叶子 ⇒ 74%，但 `tool_call` 的正文是工具输入的紧凑 JSON，抽叶子丢键；
      ③ 叶子 + 原始串 + 正文侧转义 ⇒ 76%（`json.dumps` 在 `":"` 后加空格，仍对不上）；
      ④ **两侧一律去掉全部空白** ⇒ 100%。
      教训写在判据里：粗口径的尺子会让 9.8 万条好记录被当成"漂移"去修。
    """
    from memlib import store as _store
    db = args.db or pipeline.DB_PATH
    con = _store.connect(db)
    now = int(time.time() * 1000)

    if args.method in ("cross_source", "both"):
        rows = con.execute(
            "SELECT text_hash, count(DISTINCT source) ns, group_concat(DISTINCT source) "
            "FROM atoms WHERE text_hash IS NOT NULL GROUP BY text_hash "
            "HAVING ns >= 2").fetchall()
        if args.apply and rows:
            con.execute("BEGIN")
            for h, _ns, srcs in rows:
                con.execute(
                    "UPDATE atoms SET verify_status='cross_confirmed', "
                    "verify_method='cross_source', verify_ref=?, verified_at=? "
                    "WHERE text_hash=? AND COALESCE(verify_status,'unverified')"
                    "  IN ('unverified','cross_confirmed')",
                    ("sources:" + (srcs or "")[:120], now, h))
            con.commit()
        print("① cross_source：命中 %d 个哈希（≥2 个来源共现）" % len(rows))

    if args.method in ("raw_reopen", "both"):
        sql = ("SELECT atom_id, text, prov, kind, session_id FROM atoms WHERE prov IS NOT NULL "
               "AND verify_method IS NULL")
        if args.limit:
            sql += " LIMIT %d" % int(args.limit)
        # ★ 单遍流式，不整档读进内存：先把"要核的行"按文件归堆，再逐档扫一遍只取需要的行。
        want_by_file = {}
        ok = {"ok": 0, "parts_ok": 0, "staged_ok": 0, "text_not_found": 0, "missing": 0,
              "bad": 0, "no_line": 0, "too_large": 0, "not_jsonl": 0}
        nf_kinds = {}
        plans = []
        n_atoms = 0
        for atom_id, text, prov, kind, sid in con.execute(sql):
            n_atoms += 1
            try:
                d = json.loads(prov)
            except Exception as e:  # noqa: BLE001
                # ★ 不静默：prov 解析不了 ⇒ 这条原子**无法核验**，必须可见地计入"读不了"
                print("[verify-atoms] prov 解析失败（该条计 source_unreadable）：%.120r ← %r"
                      % (prov, e), file=sys.stderr)
                ok["bad"] += 1
                continue
            # ★ `json.loads("null")` / `json.loads("[]")` **不抛异常**，返回 None / list。
            #   旧写法直接 `d.get(...)` ⇒ AttributeError，被上层的"不阻断入库"吞成一行
            #   stderr 提示（2026-10-26 沙箱实测：空库 + staged 种子必现）。
            #   这不是"极端输入"：staged 导入的原子本来就可能没有 provenance。
            if not isinstance(d, dict):
                ok["no_line"] += 1
                continue
            f, ln = d.get("file"), d.get("line")
            # ★ "prov 只到文件级"（无行号）与"文件没了"是**两件事**，不混一桶：
            #   混了就会把「dsh 源本就没有行号」报成"文件缺"，读数一错结论全错。
            if not f or not ln:
                ok["no_line"] += 1
                continue
            want_by_file.setdefault(f, {}).setdefault(int(ln), []).append(
                (atom_id, re.sub(r"\s+", "", text or ""), kind, _parts(text), sid))
        t0 = time.time()

        def _emit(f, i, hits, leaves=None, raw=None, unreadable=False):
            for aid, w, kd, segs, _sid in hits:
                if unreadable:
                    ok["bad"] += 1
                    plans.append((aid, "source_unreadable", "raw_reopen", "%s:%s" % (f, i)))
                elif _confirmed(w, leaves, raw):
                    ok["ok"] += 1
                    plans.append((aid, "reopened_ok", "raw_reopen", "%s:%s" % (f, i)))
                elif _confirmed_parts(segs, leaves, raw):
                    # 分片核验：每一片都能在原记录里找到（派生/拼接类的合理弱证据）
                    ok["parts_ok"] += 1
                    plans.append((aid, "reopened_parts_ok", "raw_reopen", "%s:%s" % (f, i)))
                else:
                    # ★ 这里**不叫"漂移"**：本判据只证明了"正文不是该行的子串"，
                    #   **没有**证明内容变了——派生/多字段拼接的原子（如 todo 由多条
                    #   拼成、deliverable 由路径拼成）天然不是任一字段的连续子串。
                    #   把"没验上"说成"漂移"＝**假报错**（会让人去修根本没坏的东西）。
                    ok["text_not_found"] += 1
                    nf_kinds[kd] = nf_kinds.get(kd, 0) + 1
                    plans.append((aid, "text_not_found", "raw_reopen", "%s:%s" % (f, i)))

        for f, lines_wanted in want_by_file.items():
            remaining = sum(len(v) for v in lines_wanted.values())
            hi = max(lines_wanted)
            fh, why = _open_lines(f)
            if fh is None:
                if why.startswith("source_too_large"):
                    bucket, status = "too_large", "source_too_large"
                elif why.startswith("source_not_jsonl"):
                    bucket, status = "not_jsonl", "source_not_jsonl"
                else:
                    bucket, status = "missing", "source_missing"
                # ★ 兜底档（第三档证据）：`prov.file` 不是**逐行文本**（如 cursor 的
                #   `state.vscdb` 是 sqlite 库）时，改与**采集件**
                #   `staged/<source>/<session>.atoms.jsonl` 比对——它含原子正文，
                #   能证「库里的内容 == 采集件的内容」（**上游原档仍不可核**，如实标注）。
                #   这比一句"格式不适用"多一层真证据，又不冒充"原档已核"。
                staged_ok_ids, rest = [], []
                for i, hits in lines_wanted.items():
                    for t in hits:
                        sp = _staged_path(t[4])
                        blob = None
                        if sp:
                            if sp not in _staged_cache:
                                try:
                                    _staged_cache[sp] = re.sub(
                                        r"\s+", "", open(sp, encoding="utf-8",
                                                        errors="replace").read())
                                except OSError as e:  # noqa: BLE001
                                    print("[verify-atoms] 采集件读不了（%s）← %r" % (sp, e),
                                          file=sys.stderr)
                                    _staged_cache[sp] = None
                            blob = _staged_cache[sp]
                        if blob and (t[1] in blob or t[1].replace('"', '\\"') in blob):
                            ok["staged_ok"] += 1
                            plans.append((t[0], "staged_ok", "staged_reopen", sp or ""))
                        else:
                            rest.append((i, t))
                for i, t in rest:
                    ok[bucket] += 1
                    plans.append((t[0], status, "raw_reopen", "%s:%s" % (f, i)))
                continue
            try:
                it = enumerate(fh, 1) if isinstance(fh, list) else enumerate(fh, 1)
                closer = None if isinstance(fh, list) else fh
                try:
                    for i, line in it:
                        hits = lines_wanted.get(i)
                        if not hits:
                            if i > hi:
                                break
                            continue
                        try:
                            leaves, raw = _record_blobs(json.loads(line))
                            _emit(f, i, hits, leaves, raw)
                        except Exception as e:  # noqa: BLE001
                            # ★ 不静默：这一行读不成 JSON ⇒ 该条判"读不了"，
                            #   但"读不了"与"漂移"是两件事（前者是我们没读到，后者是内容变了）
                            print("[verify-atoms] 原始行解析失败（%s:%s）← %r" % (f, i, e),
                                  file=sys.stderr)
                            _emit(f, i, hits, unreadable=True)
                        remaining -= len(hits)
                        if remaining <= 0:
                            break
                finally:
                    if closer is not None:
                        closer.close()
            except OSError:
                for i, hits in lines_wanted.items():
                    for aid, _w, _kd, _segs, _sid in hits:
                        ok["missing"] += 1
                        plans.append((aid, "source_missing", "raw_reopen", "%s:%s" % (f, i)))
        print("② raw_reopen：送入核验 %d 条，落判 %d 条（%.1fs，涉及文件 %d 个）⇒ "
              "一致 %d / 分片一致 %d / 采集件一致 %d / **没验上**（text_not_found，不等于漂移）%d / "
              "文件缺 %d / 读不了 %d / 无行号 %d / 档过大 %d / 非逐行形态 %d"
              % (n_atoms, len(plans), time.time() - t0, len(want_by_file),
                 ok["ok"], ok["parts_ok"], ok["staged_ok"], ok["text_not_found"], ok["missing"],
                 ok["bad"], ok["no_line"], ok["too_large"], ok["not_jsonl"]))
        if nf_kinds:
            print("   ↳ 没验上的 kind 分布（前 6）：%s —— **派生/拼接类**天然不是任一字段的"
                  "连续子串，故本判据对它们只有「在不在」的弱证据，不判「变了没有」"
                  % sorted(nf_kinds.items(), key=lambda kv: -kv[1])[:6])
        if args.apply and plans:
            con.executemany(
                "UPDATE atoms SET verify_status=?, verify_method=?, verify_ref=?, "
                "verified_at=? WHERE atom_id=?", [(s, m, r, now, i) for i, s, m, r in plans])
            con.commit()

    if not args.apply:
        print("\n[dry-run] 未写库。加 --apply 把上述结果写回 verify_status/verify_method/verify_ref。")
    else:
        n = con.execute("SELECT count(*) FROM atoms WHERE verify_status IS NOT NULL "
                        "AND verify_status<>'unverified'").fetchone()[0]
        print("\n[apply] 已核验原子数 = %d" % n)
    con.close()
    return 0


def cmd_orchestrate_status(args):  # noqa: ARG001
    """注意力编排器自检（2026-10-04 新增）。

    用户问「注意力功能今天有没有效果」时，我不得不去翻 decisions.jsonl 才发现
    **96/96 全失败、且一整天零提示**。这条命令把那次排查固化下来。
    """
    import runpy
    runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "orch_status.py"),
                   run_name="__main__")
    return 0


def cmd_console(args):
    from memlib import console as console_mod
    con = store.connect(DB)
    if args.build:
        p = console_mod.build_static(con, os.path.join(pipeline.ROOT, "console", "index.html"))
        print("static console ->", p)
    if not args.no_serve:
        console_mod.serve(con, args.port, open_browser=not args.no_browser)
    con.close()


def main():
    ap = argparse.ArgumentParser(prog="mem", description="DSH unified RAG memory library")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="入库（默认增量，--full 全量回填）")
    p.add_argument("--full", action="store_true")
    p.add_argument("--source", default="all", choices=["all", "dsh", "staged"])
    p.add_argument("--no-backups", action="store_true", help="不读 .bak-* 历史备份文件")
    p.add_argument("--no-raw", action="store_true", help="不写 raw/ 原始档")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--coverage", action="store_true", help="入库后刷新覆盖对账")
    p.add_argument("--quiet", action="store_true")
    # ★ 入库收尾自动核验（默认开；这是 R-60/q6 那条"还靠记性"的环的自动化收口）
    p.add_argument("--no-verify", action="store_true",
                   help="跳过入库收尾的自动核验（默认**会**核本次新增）")
    p.add_argument("--verify-limit", type=int, default=8000,
                   help="收尾核验最多核多少条（默认 8000，约 2-4 秒）")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("brain-status", help="脑（认知图，融合件）状态：巡检 + 服务信息 + 最近事件")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--timeout", type=float, default=20)
    p.set_defaults(func=cmd_brain_status)

    p = sub.add_parser("brain-remember", help="把一条结论写成 CCG 卡存进脑（text 类须带六要素）")
    p.add_argument("content", nargs="?", default="", help="正文；也可用 --file")
    p.add_argument("--file", default="", help="从文件读正文（优先于位置参数）")
    p.add_argument("--kind", default="text",
                   help="content_kind：text|code|work_done|work_wip|...（默认 text，须带 CCG 六要素）")
    p.add_argument("--tags", default="", help="逗号分隔")
    p.add_argument("--domain", default="",
                   help="大域名（自动写成 `domain:<名>` 标签）——**分区键**：不写就全落同一个桶，"
                        "脑会如实报「单桶：条件路由本就不可用」")
    p.set_defaults(func=cmd_brain_remember)

    p = sub.add_parser("brain-recall", help="取经审的结论（注入面口径：带验证态/不适用条件）")
    p.add_argument("query")
    p.add_argument("--k", type=int, default=3)
    p.set_defaults(func=cmd_brain_recall)

    p = sub.add_parser("purge-unverified",
                       help="删掉**任何证据通路都核对不上**的原子（默认 dry-run；含切片重建+留底）")
    p.add_argument("--apply", action="store_true", help="真删（默认只演练）")
    p.add_argument("--keep-nameless", action="store_true",
                   help="保留那条无名 tool_result（原档无对应 tool/call 的中断提示）")
    p.add_argument("--no-db-backup", action="store_true",
                   help="跳过整库备份（默认**会**先备份，可回滚）")
    p.set_defaults(func=cmd_purge_unverified)

    p = sub.add_parser("fix-cursor-time",
                       help="把 cursor 源缺失的时间戳补成 Cursor 自己记录的 `bubbleId.createdAt`")
    p.add_argument("--apply", action="store_true", help="真写（默认只演练）")
    p.set_defaults(func=cmd_fix_cursor_time)

    p = sub.add_parser("recall", help="检索并给出出处")
    p.add_argument("query")
    p.add_argument("--k", type=int, default=8)
    # ★ 脑（认知图）融合开关：默认开——同一次 recall 就带回「经审的结论」段
    p.add_argument("--no-brain", action="store_true",
                   help="不带脑（认知图）的结论段（默认**会**带）")
    p.add_argument("--brain-k", type=int, default=3,
                   help="脑段最多给几条结论（默认 3）")
    p.add_argument("--min-score", type=float, default=3.2,
                   help="置信度闸门：最高分低于它 → 明确报“查不到”（默认 3.2）")
    p.add_argument("--strong-score", type=float, default=4.0,
                   help="达到它且词覆盖率≥60% 才算“有证据”（默认 4.0，2026-09-30 二维标定）")
    p.add_argument("--raw", action="store_true",
                   help="不清洗：直接看原文（默认会自动过滤转义残渣/控制符/不可见字符等噪声）")
    p.add_argument("--session")
    p.add_argument("--source")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--role")
    p.add_argument("--kind")
    p.add_argument("--turn", type=int)
    p.add_argument("--json", action="store_true")
    p.add_argument("--explain", action="store_true")
    p.add_argument("--full", action="store_true", help="显示完整切片而不是摘录")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("ask", help="组装带引用的证据包")
    p.add_argument("query")
    p.add_argument("--k", type=int, default=6)
    p.add_argument("--min-score", type=float, default=3.2,
                   help="置信度闸门：最高分低于它 → 明确报“查不到”（默认 3.2）")
    p.add_argument("--strong-score", type=float, default=4.0,
                   help="达到它且词覆盖率≥60% 才算“有证据”（默认 4.0，2026-09-30 二维标定）")
    p.add_argument("--raw", action="store_true",
                   help="不清洗：直接看原文（默认会自动过滤转义残渣/控制符/不可见字符等噪声）")
    p.add_argument("--session")
    p.add_argument("--source")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--json", action="store_true")
    p.add_argument("--explain", action="store_true")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("show", help="看某个会话的原始记录")
    p.add_argument("session")
    p.add_argument("--turn", type=int)
    p.add_argument("--kind")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--width", type=int, default=600)
    p.add_argument("--full", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("sessions", help="列出会话")
    p.add_argument("--limit", type=int, default=60)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--source")
    p.add_argument("--grep")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("grep", help="全文精确检索（含未切片的原文）")
    p.add_argument("pattern")
    p.add_argument("--scope", default="chunks", choices=["chunks", "atoms"])
    p.add_argument("--session")
    p.add_argument("--k", type=int, default=20)
    p.add_argument("--regex", action="store_true")
    p.add_argument("--raw", action="store_true", help="不清洗，直接看原文")
    p.set_defaults(func=cmd_grep)

    p = sub.add_parser("stats", help="库统计")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("paths", help="自证：代码根/数据根在哪、数据根怎么定出来的")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_paths)

    p = sub.add_parser("reindex", help="重建全文索引（改分词器/修复索引）")
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser("coverage", help="覆盖对账")
    p.set_defaults(func=cmd_coverage)

    p = sub.add_parser("verify", help="完整性自检")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("watch", help="常驻增量入库")
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--once", action="store_true")
    p.add_argument("--no-backups", action="store_true")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("console", help="本地检索台（HTTP）")
    p.add_argument("--port", type=int, default=8848)
    p.add_argument("--build", action="store_true", help="另外导出一份静态 HTML")
    p.add_argument("--no-serve", action="store_true")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_console)

    # ★ 2026-10-04：注意力编排器自检
    #   起因：用户问「注意力功能今天有没有效果」，一查发现 **96/96 全失败且一整天无提示**。
    #   有了这条命令，"它到底有没有在干活"一眼可见，不用再去翻 decisions.jsonl。
    p = sub.add_parser("orchestrate-status", help="注意力编排器自检（有没有在干活）")
    p.set_defaults(func=cmd_orchestrate_status)

    # ★ 2026-10-16：本会话活动过的项目目录（编排器"项目闸"用：判"这条候选是不是本项目的"）
    #   实证：编排器把《伴学智能体》（老师的项目）的进度报告当成「眼睛」项目的
    #   "必须常驻的事实"注入 ⇒ 需要"本会话到底是哪个项目"这个事实（见 cmd_session_domains 注释）。
    p = sub.add_parser("session-domains",
                       help="本会话活动过的项目目录指纹（项目闸用）")
    p.add_argument("session", help="会话 id（可只给前若干位）")
    p.add_argument("--k", type=int, default=8, help="最多返回几个（按出现次数倒序）")
    p.add_argument("--keypoints", type=int, default=6,
                   help="同时返回本会话**用户原话口径**几条（0=不返回）；出处真实，可作编排器候选")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_domains)

    # ★ 2026-10-15：来源面修复（学自灵枢 provenance 六问；对应闸 g_prov / R-60）
    p = sub.add_parser("repair-provenance",
                       help="来源面修复：补 tool_name / 观测位 / schema 迁移（默认 dry-run）")
    p.add_argument("--apply", action="store_true", help="真的写库（默认只报会改多少）")
    p.add_argument("--db", default=None, help="指定库（默认 index/memory.sqlite）")
    p.set_defaults(func=cmd_repair_provenance)

    p = sub.add_parser("verify-atoms",
                       help="来源核验：重开原始记录比对（六问 q6）＋交叉来源确认")
    p.add_argument("--method", choices=["raw_reopen", "cross_source", "both"],
                   default="both")
    p.add_argument("--limit", type=int, default=0,
                   help="raw_reopen 最多核多少条（0=全部；建议先用 2000 试）")
    p.add_argument("--apply", action="store_true", help="把结果写回 verify_* 三列")
    p.add_argument("--db", default=None)
    p.set_defaults(func=cmd_verify_atoms)

    args = ap.parse_args()
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
