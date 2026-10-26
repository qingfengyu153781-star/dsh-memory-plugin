"""自动清洗层：把"无用符号"从**出答案这条路**上滤掉。

设计原则（2026-09-30 定）：
  1. **不动 raw/，不动索引文本**——这是一个可重算的投影层（impl-1-p011 的分叉选了这一支）。
     理由：切片文本一旦被改写，"细节回原档逐字对上"的验收会当场掉下来（片段里的 `\\"` 被清掉就匹配不上了）。
  2. **宁少勿多**：代码块里的 `\\n`、路径里的反斜杠、公式里的符号都是正文，不许当噪声删。
     所以"转义反解"只在**判定为被双重转义的 JSON 残渣**时才执行，代码一律不碰。
  3. 清不掉的老实标出来（乱码替换符 U+FFFD 属于**信息已丢失**，无法还原，只收敛不编造）。
"""
import json
import re

# ---------------------------------------------------------------- 判定器
_JSON_KV = re.compile(r'"[A-Za-z_][A-Za-z0-9_]{1,30}"\s*:\s*("[^"]{0,60}"|\d+(?:\.\d+)?|\[|\{|true|false|null)')
_ESCAPE_SEQ = re.compile(r'\\[nrt"\\]')
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_C1 = re.compile(r"[\x80-\x9f]")
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
_UESC = re.compile(r"\\u([0-9a-fA-F]{4})")
_REPL = re.compile(r"\ufffd+")
_BLOB = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{120,}={0,2}(?![A-Za-z0-9+/])")
_RUNPUNCT = re.compile(r"([=\-*_~#>|])\1{9,}")          # 只收 ASCII 标点：中文破折号/间隔号可能是正文
_BLANKS = re.compile(r"\n{3,}")
# 一次性反解（UTF-8 安全）：**绝不用 unicode_escape**——那会把中文按 latin-1 解、反而造出乱码
_UNESC = re.compile(r"\\u([0-9a-fA-F]{4})|\\([nrt\"\\/])")
_UNESC_MAP = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "/": "/"}


def _unescape(m):
    if m.group(1):
        return chr(int(m.group(1), 16))
    return _UNESC_MAP[m.group(2)]

# 判定"这一段是被序列化/双重转义的 JSON 残渣"，而不是源码
_JSON_HINT = re.compile(r'"(user_text|text|content|roles|weight|prov|score|explain|terms_hit|title|role|tool_name)"\s*:')


_SCAFFOLD_KEYS = frozenset((
    "roles", "tools", "weight", "prov", "rank", "title", "via", "score", "explain",
    "terms_hit", "terms_total", "coverage", "user_coverage", "title_coverage", "partial",
    "phrase", "fts", "title_bonus", "recency", "k", "query", "hits", "session_id",
    "chunk_id", "kind", "seq_start", "seq_end", "time_start", "time_end", "n_chars",
    "minScore", "strongScore", "topScore", "sufficient", "verdict", "atomFallback",
    "generatedAt", "source", "group_slug", "turn", "seq", "roles",
))


def is_pure_scaffold_json(text):
    """整段就是一个"检索元数据/facet"JSON、里面没有一句正文 → 对人是噪声，挡掉。

    注意：只要有一个长字符串值（≥40 字，通常是 note/正文），就**不算**纯脚手架，保持原样。
    """
    if not text or len(text) < 40 or not text.lstrip().startswith("{"):
        return False
    try:
        obj = json.loads(text)
    except Exception:
        return False
    if not isinstance(obj, dict) or not obj:
        return False
    if not all(isinstance(k, str) and k in _SCAFFOLD_KEYS for k in obj):
        return False
    return not any(isinstance(v, str) and len(v) >= 40 for v in obj.values())


def looks_like_escaped_json(text):
    """是不是'被 escape 过的文本'：转义序列密集（JSON 键名是加分项，不是必需——碎片也得认）。

    2026-09-30 实测校准：真实库里带转义残渣的条目多是**被转义内容的碎片**，
    JSON 解不全，所以按"转义密度"判，而不是按"能不能解析成 JSON"判。
    """
    if not text:
        return False
    esc = len(_ESCAPE_SEQ.findall(text))
    if esc == 0:
        return False
    dense = esc >= 3 and esc * 120 >= len(text)          # 每 120 字符 ≥1 个转义 = 转义密集
    return dense or (esc >= 6 and (_JSON_KV.search(text) or _JSON_HINT.search(text)))


def looks_like_code(text):
    """像源码/脚本：有缩进代码行、def/import/函数调用、或大段带引号的语句。"""
    if not text:
        return False
    signals = 0
    if re.search(r"^\s{2,}(def |class |return |if |for |while |import |from |print\(|#)", text, re.M):
        signals += 1
    if re.search(r"\b(def|import|return|lambda|function|const|let|async|await)\b", text):
        signals += 1
    if text.count("\\n") >= 3 and re.search(r"[\"'`]\s*\)|print\(|f\"|=\s*\[", text):
        signals += 1
    return signals >= 2


# ---------------------------------------------------------------- 清洗主函数
def clean_text(text, report=None):
    """返回清洗后的展示文本。report（可选 list）里记下"改了什么"。"""
    if not text:
        return text
    log = report if report is not None else []
    s = text

    def sub(pattern, repl, why, count=None):
        nonlocal s
        hits = pattern.findall(s)
        if hits:
            s = pattern.sub(repl, s) if count is None else pattern.sub(repl, s, count=count)
            log.append((why, len(hits)))

    # 1) 一定删：ANSI 转义 / 控制字符 / 不可见字符（任何上下文都是垃圾）
    sub(_ANSI, "", "ANSI转义")
    sub(_CTRL, "", "控制字符")
    sub(_C1, "", "C1控制字符")
    sub(_INVISIBLE, "", "零宽/方向标记")

    # 2) 乱码替换符：信息已丢失，只收敛不编造
    if _REPL.search(s):
        n = len(_REPL.findall(s))
        s = _REPL.sub("�", s)
        log.append(("乱码替换符收敛(U+FFFD，信息已丢失，未编造)", n))

    # 2.5) 无转义的"纯元数据 JSON dump"：整段没有一句正文 → 挡掉（不依赖转义）
    if is_pure_scaffold_json(s):
        log.append(("整段为纯 JSON 脚手架（无转义版），已折叠", 1))
        s = "[JSON 脚手架残渣 %d 字符已省略（无可读正文）]" % len(s)

    # 3) 转义/JSON 残渣：**只在能抽出正文时才改**，否则原样不动（宁少勿多）
    #    2026-09-30 踩坑记录：早先版本无条件 unescape，把 tool-call 记录里的代码改了，
    #    还用过 unicode_escape 把中文按 latin-1 解、反而造出乱码 —— 现在一律"能抽正文才动手"。
    if looks_like_escaped_json(s) and not looks_like_code(s):
        cand = _UNESC.sub(_unescape, s)
        obj = None
        m = re.search(r"\{.*\}", cand, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
            except Exception:
                obj = None
        picked = []

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in ("user_text", "text", "content", "quote") and isinstance(v, str) and v.strip():
                        picked.append(v)
                    else:
                        walk(v)
            elif isinstance(o, list):
                for v in o[:6]:
                    walk(v)

        if obj is not None:
            walk(obj)
        if picked:
            s = "\n".join(picked)
            log.append(("从转义/JSON 残渣中抽出正文", len(picked)))
        elif obj is not None and all(isinstance(k, str) for k in obj.keys()) and \
                all(k in ("roles", "tools", "weight", "prov", "rank", "title", "via", "score",
                          "explain", "terms_hit", "terms_total", "coverage", "user_coverage",
                          "title_coverage", "partial", "phrase", "fts", "title_bonus", "recency",
                          "k", "query", "hits", "session_id", "chunk_id", "kind", "seq_start",
                          "seq_end", "time_start", "time_end", "n_chars") for k in obj.keys()):
            # 纯脚手架（检索元数据/facet dump）：没有一句正文，整段就是噪声 → 挡住
            s = "[JSON 脚手架残渣 %d 字符已省略（无可读正文）]" % len(s)
            log.append(("整段为纯 JSON 脚手架，已折叠", 1))
        else:
            # 碎片：JSON 解不全，但转义本身是噪声 → **保留反解结果**（\n→换行、\"→引号）
            s = cand
            log.append(("转义反解(碎片，未识别出 JSON 结构)", len(_UNESC.findall(s)) or 1))
    else:
        # 非转义残渣：只做无损还原（字面 \uXXXX → 字符），其余转义保持原样（可能是代码）
        if _UESC.search(s):
            n = len(_UESC.findall(s))
            s = _UESC.sub(_unescape, s)
            log.append((r"字面 \uXXXX 还原", n))

    # 4) 超长 base64/hex 块：折叠（不删整段，留标记）
    if _BLOB.search(s):
        n = len(_BLOB.findall(s))
        s = _BLOB.sub(lambda m: m.group(0)[:16] + "…[长串已省略 %d 字符]" % (len(m.group(0)) - 16), s)
        log.append(("超长 base64/hex 块折叠", n))

    # 5) 重复符号长串：收敛到 3 个（保留可视分隔作用）
    if _RUNPUNCT.search(s):
        n = len(_RUNPUNCT.findall(s))
        s = _RUNPUNCT.sub(lambda m: m.group(1) * 3, s)
        log.append(("重复符号长串收敛", n))

    # 6) 空行与行尾空白
    s = _BLANKS.sub("\n\n", s)
    s = "\n".join(ln.rstrip() for ln in s.split("\n")).strip()
    return s


# ---------------------------------------------------------------- 残余噪声审计
NOISE_RULES = {
    "转义残渣": _ESCAPE_SEQ,
    "JSON键值残渣": _JSON_KV,
    "控制字符": _CTRL,
    "不可见字符": _INVISIBLE,
    "乱码替换符": _REPL,
    "ANSI转义": _ANSI,
    r"字面\uXXXX": _UESC,
    "超长base64/hex": _BLOB,
    "重复符号长串": _RUNPUNCT,
    "GBK乱码串": re.compile(r"[ÃÂåæçèéêëìíîïðñòóô][\x80-\xbf]{2,}"),
}


def noise_of(text):
    """返回这段文本命中的噪声类别列表（清洗后用它来报'还剩哪些清不掉'）。"""
    return [k for k, rx in NOISE_RULES.items() if rx.search(text or "")]
