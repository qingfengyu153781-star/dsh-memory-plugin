"""Local browsing console for the memory library.

`mem.py console --port 8848` serves a small single-page app (stdlib only, no CDN)
that can search, filter, and read back any ingested conversation with citations.
`mem.py console --build` writes a standalone catalogue HTML.
"""
import html
import json
import os
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import store
from .normalize import tokenize

PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DSH 记忆库 · 检索台</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--panel2:#1e222b;--fg:#e6e9ef;--dim:#9aa4b2;--line:#2a2f3a;
--accent:#5b9dff;--user:#7ee2a8;--asst:#8fc7ff;--tool:#e2c185;--sys:#c39bff;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);z-index:5}
h1{font-size:16px;margin:0;font-weight:600;letter-spacing:.5px}
h1 span{color:var(--accent)}
input,select,button{background:var(--panel2);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:7px 10px;font-size:13px;font-family:inherit}
input:focus,select:focus{outline:1px solid var(--accent)}
button{cursor:pointer}
button:hover{border-color:var(--accent)}
#q{width:420px}
main{display:grid;grid-template-columns:340px 1fr;gap:0;height:calc(100vh - 62px)}
#left{border-right:1px solid var(--line);overflow:auto;padding:10px}
#right{overflow:auto;padding:14px 18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:8px;cursor:pointer}
.card:hover{border-color:var(--accent)}
.hit{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px}
.hit h3{margin:0 0 6px;font-size:14px;font-weight:600}
.cite{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;color:var(--dim);word-break:break-all}
.score{float:right;color:var(--accent);font-family:ui-monospace,monospace}
pre{white-space:pre-wrap;word-break:break-word;background:#11141a;border:1px solid var(--line);border-radius:8px;padding:10px;margin:8px 0 0;font:12.5px/1.55 ui-monospace,Consolas,"Microsoft YaHei",monospace;max-height:420px;overflow:auto}
mark{background:#ffd76a;color:#111;border-radius:3px}
.dim{color:var(--dim)} .small{font-size:12px}
.tag{display:inline-block;padding:1px 7px;border-radius:999px;border:1px solid var(--line);font-size:11px;margin-right:6px;color:var(--dim)}
.u{color:var(--user)} .a{color:var(--asst)} .t{color:var(--tool)} .s{color:var(--sys)}
.atom{border-left:3px solid var(--line);padding:6px 0 6px 12px;margin:8px 0}
.atom.user{border-color:var(--user)} .atom.assistant{border-color:var(--asst)}
.atom.tool{border-color:var(--tool)} .atom.system{border-color:var(--sys)}
.atom .head{font-size:11.5px;color:var(--dim);font-family:ui-monospace,monospace}
.atom .body{white-space:pre-wrap;word-break:break-word;font-size:13px;max-height:300px;overflow:auto}
.atom .body.clip{max-height:180px;overflow:hidden;position:relative}
.atom .body.clip:after{content:"";position:absolute;bottom:0;left:0;right:0;height:40px;background:linear-gradient(transparent,var(--bg))}
.more{font-size:11.5px;color:var(--accent);cursor:pointer}
#status{color:var(--dim);font-size:12px}
</style></head>
<body>
<header>
  <h1>DSH <span>记忆库</span></h1>
  <input id="q" placeholder="检索任意历史细节…（支持中文/英文/文件名/命令）" autocomplete="off">
  <button id="go">搜索</button>
  <select id="source"><option value="">全部来源</option></select>
  <select id="k"><option value="8">8 条</option><option value="20" selected>20 条</option><option value="50">50 条</option></select>
  <input id="since" type="date" title="起始日期"><input id="until" type="date" title="结束日期">
  <span id="status"></span>
</header>
<main>
  <div id="left"><div class="small dim" style="padding:6px">会话列表<br>点击查看完整对话记录</div><div id="sessions"></div></div>
  <div id="right"><div id="out" class="dim">输入关键词开始检索；或从左侧挑一个会话翻阅原始记录。</div></div>
</main>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt=t=>t?new Date(t).toLocaleString('zh-CN',{hour12:false}):'-';
function hl(text,q){ if(!q) return esc(text);
  const terms=[...new Set(q.split(/\s+/).filter(x=>x.length>1))].slice(0,8)
    .map(t=>t.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'));
  if(!terms.length) return esc(text);
  return esc(text).replace(new RegExp('('+terms.join('|')+')','gi'),'<mark>$1</mark>');
}
async function api(p){const r=await fetch(p);return r.json();}
async function loadStats(){
  const s=await api('/api/stats');
  $('#status').textContent=`会话 ${s.sessions} · 切片 ${s.chunks} · atom ${s.atoms} · ${(s.chunk_chars/1e6).toFixed(1)}M 字 · 库 ${s.dbSizeMB||0}MB`;
  const src=$('#source'); (s.sources||[]).forEach(x=>{const o=document.createElement('option');o.value=x;o.textContent=x;src.appendChild(o);});
}
async function loadSessions(grep){
  const rows=await api('/api/sessions?limit=400'+(grep?('&grep='+encodeURIComponent(grep)):''));
  $('#sessions').innerHTML=rows.map(r=>`<div class="card" onclick="showSession('${r.session_id}')">
    <div style="font-weight:600">${esc(r.title||r.label||r.session_id.slice(0,24))}</div>
    <div class="small dim">${esc(r.session_id.slice(0,22))} · ${esc(r.source)} · ${r.origin||'-'} · ${fmt(r.last_time)}<br>
    user ${r.n_user} · chunk ${r.n_chunks} · ${(r.n_chars/1000).toFixed(0)}k字</div></div>`).join('')||'<div class="dim small">无</div>';
}
async function doSearch(){
  const q=$('#q').value.trim(); if(!q) return;
  $('#out').innerHTML='<div class="dim">检索中…</div>';
  const p=new URLSearchParams({q,k:$('#k').value});
  if($('#source').value)p.set('source',$('#source').value);
  if($('#since').value)p.set('since',$('#since').value);
  if($('#until').value)p.set('until',$('#until').value);
  const d=await api('/api/search?'+p);
  $('#status').textContent=`命中 ${d.hits.length} 条（候选 ${d.candidates}）`;
  $('#out').innerHTML=d.hits.map(h=>`<div class="hit">
    <div class="score">${h.score}</div>
    <h3>${esc(h.title||h.label||h.session_id.slice(0,30))}</h3>
    <div class="cite">${esc(h.citation)}</div>
    <div class="small dim" style="margin-top:4px">
      <span class="tag">${esc(h.kind)}</span><span class="tag">turn ${h.turn??'-'}</span>
      <span class="tag">${esc(h.roles||'')}</span>
      <a href="#" onclick="showSession('${h.session_id}',${h.turn??'null'});return false">打开该会话 ↗</a>
    </div>
    <pre>${hl(h.text,q)}</pre></div>`).join('')||'<div class="dim">没有命中。换个说法，或用“精确短语”检索。</div>';
}
async function showSession(id,turn){
  $('#out').innerHTML='<div class="dim">读取会话…</div>';
  const d=await api('/api/session?id='+encodeURIComponent(id)+(turn?('&turn='+turn):''));
  const s=d.session;
  $('#out').innerHTML=`<div class="hit"><h3>${esc(s.title||s.label||s.session_id)}</h3>
    <div class="cite">${esc(s.session_id)} · ${esc(s.source)} · origin=${esc(s.origin||'-')} · 父会话=${esc(s.parent_session||'-')}<br>
    工作目录 ${esc(s.cwd||'-')} · ${fmt(s.first_time)} → ${fmt(s.last_time)} · atom ${s.n_atoms} · 切片 ${s.n_chunks} · 版本 ${esc(s.versions||'')}</div>
    <div class="small dim" style="margin-top:6px">文件: ${esc((d.files||[]).join(', '))}</div></div>
    <div>${d.atoms.map(a=>`<div class="atom ${esc(a.role||'system')}">
      <div class="head">${esc(a.kind)} · turn ${a.turn??'-'} · seq ${a.seq??'-'} · ${fmt(a.time)} ${a.tool_name?('· '+esc(a.tool_name)):''}</div>
      <div class="body clip" onclick="this.classList.toggle('clip')">${esc(a.text)}</div></div>`).join('')}</div>`;
}
$('#go').onclick=doSearch; $('#q').addEventListener('keydown',e=>{if(e.key==='Enter')doSearch();});
$('#source').onchange=()=>{if($('#q').value.trim())doSearch();};
$('#k').onchange=()=>{if($('#q').value.trim())doSearch();};
$('#since').onchange=$('#until').onchange=()=>{if($('#q').value.trim())doSearch();};
let t; $('#sessions').addEventListener('input',()=>{clearTimeout(t);t=setTimeout(()=>loadSessions($('#q').value.trim()),250);});
loadStats(); loadSessions('');
window.showSession=showSession;
</script></body></html>
"""


def _clean(text):
    """控制台也是"出答案"的一路，统一过清洗（raw/ 与索引不动，见 memlib.clean）。"""
    try:
        from memlib import clean as _c
        return _c.clean_text(text)
    except Exception:
        return text


def _hits(con, q, k, source=None, since=None, until=None):
    since_ms = until_ms = None
    for val, is_end in ((since, False), (until, True)):
        if val:
            try:
                t = time.strptime(val, "%Y-%m-%d")
                since_ms = int(time.mktime(t) * 1000) + (86399000 if is_end else 0)
            except ValueError:
                pass
    rows = store.search(con, q, k=k, source=source or None, since=since_ms, until=until_ms,
                        explain=True)
    out = []
    for h in rows:
        meta = con.execute("SELECT title, label, origin, parent_session, cwd FROM sessions WHERE session_id=?",
                           (h["session_id"],)).fetchone() or (None,) * 5
        prov = h.get("prov")
        if isinstance(prov, str):
            try:
                prov = json.loads(prov)
            except Exception:
                prov = {}
        out.append({
            "session_id": h["session_id"], "title": meta[0], "label": meta[1], "origin": meta[2],
            "parent": meta[3], "cwd": meta[4], "source": h.get("source"), "kind": h.get("kind"),
            "turn": h.get("turn"), "roles": h.get("roles"), "score": h.get("score"),
            "time_start": h.get("time_start"),
            "citation": "%s:%s#turn=%s seq=%s..%s %s" % (
                h.get("source"), h["session_id"], h.get("turn"), h.get("seq_start"), h.get("seq_end"),
                time.strftime("%Y-%m-%d %H:%M", time.localtime((h.get("time_start") or 0) / 1000))),
            "text": _clean(h["text"]), "explain": h.get("explain"),
            "files": (prov or {}).get("files"),
        })
    return out


class Handler(BaseHTTPRequestHandler):
    con = None
    db_path = None
    quiet = True

    def log_message(self, *a):
        if not self.quiet:
            super().log_message(*a)

    def _db(self):
        """Threaded server -> one short-lived connection per request."""
        if self.db_path:
            return store.connect(self.db_path)
        return self.con

    def _close(self, con):
        if self.db_path and con is not None:
            try:
                con.close()
            except Exception:
                pass

    def _send(self, obj, status=200, ctype="application/json; charset=utf-8"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        g = lambda k, d=None: (qs.get(k) or [d])[0]
        con = self._db()
        try:
            if u.path in ("/", "/index.html"):
                return self._send(PAGE.encode("utf-8"), ctype="text/html; charset=utf-8")
            if u.path == "/api/search":
                q = g("q", "")
                if not q.strip():
                    return self._send({"hits": [], "candidates": 0})
                k = int(g("k", "20"))
                return self._send({"hits": _hits(con, q, k, g("source"), g("since"), g("until")),
                                   "candidates": k})
            if u.path == "/api/sessions":
                rows = store.session_list(con, limit=int(g("limit", "200")), grep=g("grep"))
                return self._send(rows)
            if u.path == "/api/session":
                sid = g("id", "")
                row = con.execute("""SELECT session_id, source, group_slug, cwd, origin,
                    parent_session, title, label, first_time, last_time, n_atoms, n_user,
                    n_assistant, n_tool, n_chunks, versions, dir FROM sessions WHERE session_id=?""",
                    (sid,)).fetchone()
                if not row:
                    return self._send({"error": "not found"}, 404)
                cols = ["session_id", "source", "group_slug", "cwd", "origin", "parent_session",
                        "title", "label", "first_time", "last_time", "n_atoms", "n_user",
                        "n_assistant", "n_tool", "n_chunks", "versions", "dir"]
                ses = dict(zip(cols, row))
                return self._send({"session": ses, "files": json.loads(ses.get("versions") or "[]"),
                                   "atoms": store.session_atoms(con, sid,
                                                                limit=int(g("limit", "2000")))})
            if u.path == "/api/stats":
                st = store.stats(con)
                st["sources"] = list(st.get("sessions_by_source", {}).keys())
                if self.db_path and os.path.exists(self.db_path):
                    st["dbSizeMB"] = round(os.path.getsize(self.db_path) / 1048576, 1)
                return self._send(st)
            if u.path == "/api/grep":
                rows = store.grep(con, g("q", ""), k=int(g("k", "30")), scope=g("scope", "chunks"))
                return self._send({"rows": [list(r) for r in rows]})
            return self._send({"error": "not found"}, 404)
        except Exception as e:
            return self._send({"error": "%s: %s" % (type(e).__name__, e)}, 500)
        finally:
            self._close(con)


def serve(con, port=8848, open_browser=True, quiet=True):
    Handler.con = con
    Handler.quiet = quiet
    row = con.execute("PRAGMA database_list").fetchone()
    Handler.db_path = row[2] if row and row[2] else None
    ThreadingHTTPServer.daemon_threads = True
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % port
    print("DSH 记忆库检索台: %s  (Ctrl+C 退出)" % url)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()


def build_static(con, path):
    rows = store.session_list(con, limit=100000)
    st = store.stats(con)
    cards = []
    for r in rows:
        cards.append(
            '<div class="card" data-k="%s">'
            '<div style="font-weight:600">%s</div>'
            '<div class="small dim">%s · %s · %s · user %s · chunk %s · <span class="mono">%s</span></div>'
            '</div>' % (
                html.escape((r["title"] or "") + " " + (r["label"] or "") + " " + r["session_id"]).lower(),
                html.escape(r["title"] or r["label"] or r["session_id"]),
                html.escape(str(r["source"])), html.escape(str(r["origin"])),
                time.strftime("%Y-%m-%d %H:%M", time.localtime((r["last_time"] or 0) / 1000)),
                r["n_user"], r["n_chunks"], html.escape(r["session_id"])))
    doc = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>DSH 记忆库 · 会话总目录</title><style>
body{margin:0;background:#0f1115;color:#e6e9ef;font:14px/1.6 "Segoe UI","Microsoft YaHei",sans-serif}
header{padding:16px 20px;border-bottom:1px solid #2a2f3a;position:sticky;top:0;background:#0f1115}
input{width:420px;background:#1e222b;color:#e6e9ef;border:1px solid #2a2f3a;border-radius:8px;padding:8px 10px}
.grid{padding:14px 20px;display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:8px}
.card{background:#171a21;border:1px solid #2a2f3a;border-radius:10px;padding:10px 12px}
.dim{color:#9aa4b2}.small{font-size:12px}.mono{font-family:ui-monospace,Consolas,monospace}
</style></head><body>
<header><b>DSH 记忆库 · 会话总目录</b>
<div class="dim small" style="margin:6px 0">会话 %d · 切片 %d · atom %d · 覆盖 %s → %s</div>
<input id="f" placeholder="按标题/会话号过滤…"></header>
<div class="grid" id="g">%s</div>
<script>
const cards=[...document.querySelectorAll('.card')];
document.getElementById('f').addEventListener('input',e=>{const v=e.target.value.toLowerCase();
 cards.forEach(c=>{c.style.display=c.dataset.k.includes(v)?'':'none';});});
</script></body></html>""" % (
        st["sessions"], st["chunks"], st["atoms"],
        time.strftime("%Y-%m-%d", time.localtime((st["first_time"] or 0) / 1000)),
        time.strftime("%Y-%m-%d", time.localtime((st["last_time"] or 0) / 1000)),
        "\n".join(cards))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path
