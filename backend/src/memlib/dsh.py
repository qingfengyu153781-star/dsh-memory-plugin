"""DSH session store reader: locate session files, decode multi-frame zstd,
turn raw event records into normalized conversation atoms.

Format facts (verified 2026-09-30 on this machine):
  <DSH_HOME>/sessions/<group-slug>/<session-id>/session.v4.jsonl.zstd   <- current
                                                session.v3.jsonl.zstd   <- older
                                                session.jsonl.zstd      <- legacy v0
                                                session.v*.jsonl.zstd.bak-<ts>  <- backups
The payload is **concatenated zstd frames** (one frame per flush), so a single
one-shot decompress returns only the first record. We must stream.
"""
import io
import json
import os
import re

#: `zstandard` 是**可选**依赖：只有读会话日志（ingest / sessions / grep 原文）才需要它。
#: 2026-10-26 之前这里是**无保护的顶层 import** —— 缺这个包时整个 memlib 都 import 不了，
#: 于是连 `recall`（只查 SQLite，根本不需要解压）也跟着陪葬。
#: 现在改成软依赖：缺了就把错误留在 `ZSTD_IMPORT_ERROR` 里，
#: 由**真正用到它的那条命令**去报错，而不是让所有命令一起死。
try:
    import zstandard as zstd
    ZSTD_IMPORT_ERROR = None
except Exception as _e:                                     # pragma: no cover
    zstd = None
    ZSTD_IMPORT_ERROR = "%s: %s" % (type(_e).__name__, _e)

from .normalize import norm_text, sha1, sha1_bytes


def require_zstd(what="读取 DSH 会话日志"):
    """需要 zstandard 的命令先调它；缺依赖时给出**可照做**的修法，而不是 ImportError 栈。"""
    if zstd is not None:
        return
    raise RuntimeError(
        "缺少可选依赖 zstandard（用于%s）。安装：python -m pip install zstandard\n"
        "原始错误：%s" % (what, ZSTD_IMPORT_ERROR)
    )


#: DSH 主目录。默认取当前用户的 `~/.dsh` —— **不写死任何人的用户名**。
#: （旧写法把某个具体用户的绝对路径当默认值：既是对外泄漏，
#:   也是功能 bug —— 换台机器就指到不存在的目录。）
DSH_HOME = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
SESSIONS_ROOT = os.path.join(DSH_HOME, "sessions")

VER_RE = re.compile(r"^session\.v(\d+)\.jsonl\.zstd$")
PLAIN_RE = re.compile(r"^session\.jsonl\.zstd$")
BAK_RE = re.compile(r"\.bak-.*$")

# record types that carry conversation content
CONV_TYPES = {
    "user/message", "assistant/message", "tool/call", "tool/result",
    "agent/inbox/spliced", "compaction/summary", "session/title", "goal/change",
    "todo/write", "command/run", "developer/message", "deliverables/presented",
    "workspace/changes", "system/message", "plan/mode",
}
# ★★ 2026-10-15：**只有真人发言才算 user_text**（fail-closed 白名单）★★
# 病根（实测）：旧代码只看 `type == "user/message"`，**不看 `data.source.kind`**，
#   于是插件/框架往对话里塞的机器块被登记成 `role=user / kind=user_text`，
#   召回时被当"用户原话"，编排器把**自己上一轮的输出**当证据再注入（自噬闭环）。
# 实测取值：user（真人）／time-context／runtime-context／agent-instructions／
#   tool-jobs／plugin:dsh-memory-plugin。
# 实测污染量（清洗前）：3703 条（<memory_recall 2686／<goal_round 661／
#   <attention_orchestration 326／告警 30），横跨 491 个会话。
# 修法：不在白名单 ⇒ 单独存 `kind=plugin_injection`（role=plugin），
#   且该 kind 列在 `pipeline.NO_CHUNK` 里 ⇒ **存得下、可审计、但进不了 chunks、检索不到**。
USER_SOURCE_KINDS = {"user"}


def _source_kind(obj):
    """取消息的 `source.kind`（取不到回空串 ⇒ 判为**非**真人源，fail-closed）。"""
    if not isinstance(obj, dict):
        return ""
    src = obj.get("source")
    if not isinstance(src, dict):
        return ""
    k = src.get("kind")
    return k if isinstance(k, str) else ""


# record types deliberately not indexed as conversation (kept in the raw archive)
IGNORED_TYPES = {
    "assistant/chunk", "reasoning-chunks", "text-chunks", "tool-call-chunks",
    "step/start", "step/end", "turn/start", "turn/end", "request/header",
    "request/context", "llm/retry", "llm/retry-started", "assistant/attempt",
    "session-log-deepseek/delivery-accepted", "web/deepseek-search-llm-request",
    "session/title-llm-request", "sandbox/mode", "approval/policy",
    "permission/preset", "model/selection", "agent-preset/selected",
    "session/end-seed", "subagent/descriptor", "subagent/catalog",
    "compaction/start", "compaction/end", "compaction/prune", "approval/asked",
    "approval/decided", "session",
}


# ------------------------------------------------------------------ decoding

def read_zstd_multiframe(path, max_bytes=None):
    """Decompress a file that may contain many concatenated zstd frames."""
    require_zstd("解压会话日志 %s" % os.path.basename(path))
    dctx = zstd.ZstdDecompressor()
    out = bytearray()
    with open(path, "rb") as fh:
        reader = dctx.stream_reader(fh)
        while True:
            chunk = reader.read(1 << 22)
            if not chunk:
                break
            out += chunk
            if max_bytes and len(out) > max_bytes:
                break
    return bytes(out)


def parse_jsonl(text):
    recs, bad = [], 0
    for i, line in enumerate(text.split("\n")):
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except Exception:
            bad += 1
    return recs, bad


def parse_jsonl_with_lines(text):
    """同 `parse_jsonl`，但**带回每条记录的物理行号**（1 基）。

    ★ 为什么不复用 `parse_jsonl` 的 enumerate 下标：那里会**跳过坏行**，
      下标于是不等于物理行号；拿它当行号写进 prov，核验时会去开错的行
      —— 错行号比没有行号更坏（会伪造出"漂移"）。
    """
    recs, lns, bad = [], [], 0
    for i, line in enumerate(text.split("\n"), 1):
        s = line.strip()
        if not s:
            continue
        try:
            recs.append(json.loads(s))
            lns.append(i)
        except Exception:
            bad += 1
    return recs, lns, bad


def file_version(name):
    m = VER_RE.match(name)
    if m:
        return int(m.group(1))
    if PLAIN_RE.match(name):
        return 0
    return None


def discover_sessions(root=None):
    """Return one entry per session directory with all of its live version files."""
    root = root or SESSIONS_ROOT
    out = []
    if not os.path.isdir(root):
        return out
    for group in sorted(os.listdir(root)):
        gdir = os.path.join(root, group)
        if not os.path.isdir(gdir):
            continue
        for sid in sorted(os.listdir(gdir)):
            sdir = os.path.join(gdir, sid)
            if not os.path.isdir(sdir):
                continue
            live, backups = {}, []
            for fn in sorted(os.listdir(sdir)):
                fp = os.path.join(sdir, fn)
                if not os.path.isfile(fp):
                    continue
                st = os.stat(fp)
                base = BAK_RE.sub("", fn)
                v = file_version(base)
                if v is None:
                    continue
                item = {"name": fn, "path": fp, "size": st.st_size,
                        "mtime": int(st.st_mtime * 1000), "version": v,
                        "is_backup": ".bak-" in fn}
                if item["is_backup"]:
                    backups.append(item)
                else:
                    live.setdefault(v, []).append(item)
            if not live:
                continue
            versions = sorted(live)
            canonical = sorted(live[versions[-1]], key=lambda x: x["mtime"])[-1]
            out.append({
                "session_id": sid, "group": group, "dir": sdir,
                "versions": {v: sorted(live[v], key=lambda x: x["mtime"]) for v in versions},
                "canonical": canonical, "canonical_version": versions[-1],
                "backups": backups,
            })
    return out


# ------------------------------------------------------------- atom building

def content_to_text(content, depth=0):
    """Flatten a message/content payload to plain text. Never summarises."""
    if content is None or depth > 4:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        t = content.get("type")
        if t in ("text", "reasoning"):
            return content.get("text") or ""
        if t == "tool-result":
            return content_to_text(content.get("content"), depth + 1)
        if t == "tool-call":
            return content.get("arguments") or ""
        if t == "image":
            return "[图片]"
        if t in ("file", "document"):
            return "[文件 %s]" % (content.get("path") or content.get("name") or "")
        if "text" in content and isinstance(content["text"], str):
            return content["text"]
        return ""
    if isinstance(content, list):
        buf = []
        for p in content:
            s = content_to_text(p, depth + 1)
            if s:
                buf.append(s)
        return "\n".join(buf)
    return ""


def _atom(kind, role, text, rec, prov, **kw):
    text = norm_text(text)
    if not text:
        return None
    a = {
        "kind": kind, "role": role, "text": text,
        "seq": rec.get("seq"), "time": rec.get("time"),
        "turn": kw.get("turn"), "step": kw.get("step"),
        "tool_name": kw.get("tool_name"), "call_id": kw.get("call_id"),
        "hash": sha1(kind + "\x00" + text),
        "prov": prov,
    }
    return a


def extract_atoms(records, prov, lines=None):
    """records -> (atoms, session_meta, counters).  prov = provenance dict.

    `lines`（可选，与 records 等长）：每条记录在**原始文件里的物理行号**。
    给了它就把它并进 prov（`{file, version, backup, line}`）——有了行号，记忆库才能
    对自己做**可复现的核验**：重开该文件的该行、比对正文是否还在（来源六问 q6 的
    `raw_reopen` 通路，见 `bin/mem.py verify-atoms`）。没有行号时只能核到"文件还在"。
    """
    atoms, counters = [], {}
    meta = {}
    last_turn = last_step = None
    base_prov = prov
    #: callId → 工具名（同一次抽取里 `tool/call` 先于 `tool/result`）
    #: ★ 2026-10-15：`tool_result` 原先**不带工具名**（只带 callId），入库后要靠
    #:   `mem.py repair-provenance` 回填 8.8 万条；而**每来一条新会话又会重新缺**
    #:   ⇒ 那是"治标"。现在在抽取阶段就地记住 call→name，入库即完整；
    #:   跨版本/跨文件的遗漏再由回填补（兜底，不是主路）。
    call_names: dict = {}
    for i, rec in enumerate(records):
        # ★ 逐条 prov（带该条自己的行号）。`prov` 这个名字在循环内被覆盖是有意的：
        #   函数体内所有 `_atom(..., prov, ...)` 调用点因此自动拿到本条的行号，
        #   无需逐个改调用点（改点多 = 漏改点）。base_prov 保住原始字典。
        prov = (dict(base_prov, line=lines[i]) if (lines and i < len(lines)) else base_prov)
        t = rec.get("type")
        counters[t] = counters.get(t, 0) + 1
        if t == "session":
            meta.update({
                "session_id": rec.get("id"), "cwd": rec.get("cwd"),
                "created_at": rec.get("createdAt"), "origin": rec.get("origin") or "user",
                "parent_session": rec.get("parentSession"), "delegation_depth": rec.get("delegationDepth"),
                "agent_preset": rec.get("agentPreset"), "version": rec.get("version"),
                "is_seeded": rec.get("isSeeded"),
            })
            continue
        d = rec.get("data") or {}
        if isinstance(d.get("turn"), int):
            last_turn = d["turn"]
        if isinstance(d.get("step"), int):
            last_step = d["step"]
        turn = d.get("turn", last_turn)
        step = d.get("step", last_step)

        if t == "user/message":
            # 真人才进 user_text；插件/框架块另存 plugin_injection（不入 chunks）
            sk = _source_kind(d)
            human = sk in USER_SOURCE_KINDS
            a = _atom("user_text" if human else "plugin_injection",
                      "user" if human else "plugin",
                      content_to_text(d.get("content")), rec, prov, turn=turn, step=step)
            if a:
                if not human:
                    a["note"] = "非真人来源（不入检索）: %s" % (sk or "(无)")
                atoms.append(a)
        elif t == "agent/inbox/spliced":
            for ins in d.get("inserted") or []:
                sk = _source_kind(ins)
                human = sk in USER_SOURCE_KINDS
                a = _atom("spliced_user" if human else "plugin_injection",
                          "user" if human else "plugin",
                          content_to_text(ins.get("content")), rec, prov,
                          turn=turn, step=step)
                if a:
                    a["note"] = ("mid-turn injection (target=%s)" % d.get("target")) if human \
                        else ("非真人插话来源（不入检索）: %s" % (sk or "(无)"))
                    atoms.append(a)
        elif t == "assistant/message":
            msg = d.get("message") or {}
            for part in msg.get("content") or []:
                if not isinstance(part, dict):
                    continue
                pt = part.get("type")
                if pt == "reasoning":
                    a = _atom("assistant_reasoning", "assistant", part.get("text"), rec, prov,
                              turn=turn, step=step)
                    if a:
                        atoms.append(a)
                elif pt == "text":
                    a = _atom("assistant_text", "assistant", part.get("text"), rec, prov,
                              turn=turn, step=step)
                    if a:
                        atoms.append(a)
                # tool-call parts are captured by dedicated tool/call records
        elif t == "tool/call":
            _cid = d.get("callId")
            _nm = d.get("name")
            if _cid and _nm:
                call_names[_cid] = _nm
            a = _atom("tool_call", "tool", d.get("arguments"), rec, prov,
                      turn=turn, step=step, tool_name=_nm, call_id=_cid)
            if a:
                atoms.append(a)
        elif t == "tool/result":
            msg = d.get("message") or {}
            txt = content_to_text(msg.get("content"))
            call_id = None
            src = msg.get("source") or {}
            if isinstance(src, dict):
                call_id = src.get("callId")
            # 工具名优先取自同一次抽取里记住的 call→name（入库即完整）
            a = _atom("tool_result", "tool", txt, rec, prov, turn=turn, step=step,
                      call_id=call_id, tool_name=call_names.get(call_id))
            if a:
                if d.get("isError") or msg.get("isError"):
                    a["note"] = "error result"
                atoms.append(a)
        elif t == "system/message":
            msg = d.get("message") or {}
            a = _atom("system_text", "system", content_to_text(msg.get("content")), rec, prov,
                      turn=turn, step=step)
            if a:
                a["indexed"] = 0
                atoms.append(a)
        elif t == "developer/message":
            a = _atom("developer_text", "developer", content_to_text(d.get("content")), rec, prov,
                      turn=turn, step=step)
            if a:
                atoms.append(a)
        elif t == "compaction/summary":
            a = _atom("compaction_summary", "system", content_to_text(d.get("summary")), rec, prov,
                      turn=turn, step=step)
            if a:
                atoms.append(a)
        elif t == "session/title":
            title = d.get("title")
            if title:
                meta["title"] = title
                a = _atom("title", "system", title, rec, prov, turn=turn, step=step)
                if a:
                    a["indexed"] = 0
                    atoms.append(a)
        elif t == "goal/change":
            g = d.get("goal") or {}
            txt = g.get("objective") or ""
            if txt:
                meta["objective"] = txt
                a = _atom("goal", "system", txt, rec, prov, turn=turn, step=step)
                if a:
                    atoms.append(a)
        elif t == "todo/write":
            todos = d.get("todos") or []
            txt = "\n".join("- [%s] %s" % (x.get("status"), x.get("content")) for x in todos
                            if isinstance(x, dict))
            a = _atom("todo", "system", txt, rec, prov, turn=turn, step=step)
            if a:
                a["indexed"] = 0
                atoms.append(a)
        elif t == "command/run":
            name = d.get("name") or ""
            cmd_args = (d.get("args") or "").strip()
            a = _atom("command", "user", ("/%s %s" % (name, cmd_args)).strip(),
                      rec, prov, turn=turn, step=step)
            if a:
                atoms.append(a)
        elif t == "deliverables/presented":
            files = d.get("files") or []
            txt = "\n".join(str(f.get("path") if isinstance(f, dict) else f) for f in files)
            a = _atom("deliverable", "system", txt, rec, prov, turn=turn, step=step)
            if a:
                atoms.append(a)
        elif t == "workspace/changes":
            a = _atom("note", "system", json.dumps(d, ensure_ascii=False)[:4000], rec, prov,
                      turn=turn, step=step)
            if a:
                a["indexed"] = 0
                atoms.append(a)
        elif t == "subagent/descriptor":
            meta["label"] = d.get("label")
            meta["model"] = d.get("agentModel")
            meta["provider"] = d.get("agentProvider")
        elif t == "model/selection":
            meta.setdefault("model", d.get("model"))
        elif t == "session/end-seed":
            meta["seeded"] = True
        elif t == "sandbox/mode":
            meta.setdefault("sandbox", d.get("mode"))
    return atoms, meta, counters


def merge_versions(per_version):
    """per_version: {version: [atoms...]} -> single chronological atom list.

    The canonical (highest) version wins on duplicates; atoms that only exist in
    an older version are merged back in by timestamp. This is what makes the
    library loss-free when DSH rewrites/prunes a session log.

    ★ 2026-10-15 修（**跨版本合并会静默丢不同调用**）：去重键原本是
      `(kind, hash)`，而 `hash = sha1(kind + "\\x00" + text)` **不含调用标识** ⇒
      所有 `arguments` 为空的调用（正文都是 `{}`）共用一个 hash。实测：**438 个不同
      callId 共用同一个 hash**（涉及 20 个不同工具），跨版本合并时被合并成 1 条。
      后果不只是"少几条"：`tool_result` 找不到对应 `tool_call` ⇒ 工具名永久缺失
      （实测 133 条）、调用史被吃掉。
      修法：去重键带上 `call_id`（真实身份）。**不改 hash 公式**——改了会让新旧行
      的 hash 语义分家（cross_source 等按 hash 的判据会两边都算不准）。
    """
    if not per_version:
        return [], {"kept_from_older": 0}
    versions = sorted(per_version)
    seen = set()
    merged = []
    kept_old = 0
    for i, v in enumerate(reversed(versions)):   # newest first == canonical precedence
        is_canonical = (i == 0)
        for a in per_version[v]:
            key = (a["kind"], a["hash"], a.get("call_id"))
            # inside the canonical file repeats are real; only de-dup across files
            if not is_canonical and key in seen:
                continue
            seen.add(key)
            if not is_canonical:
                a = dict(a)
                a["extra_from_version"] = v
                kept_old += 1
            merged.append(a)
    merged.sort(key=lambda a: (a.get("time") or 0, a.get("seq") or 0))
    return merged, {"kept_from_older": kept_old}
