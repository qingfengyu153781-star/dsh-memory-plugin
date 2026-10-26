"""Shared helpers: text normalization, CJK-aware tokenization, hashing, time.

No third-party deps. Kept deliberately small and deterministic so that the same
tokenizer is used at index time and query time.
"""
import hashlib
import re
import unicodedata

# ---------------------------------------------------------------- text utils

_WS_RE = re.compile(r"[ \t\u00a0\u3000]+")
_NL_RE = re.compile(r"\n{3,}")


def norm_text(s):
    """Light normalization: no semantic change, only whitespace/casing hygiene."""
    if not s:
        return ""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _WS_RE.sub(" ", s)
    s = _NL_RE.sub("\n\n", s)
    return s.strip()


def sha1(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def sha1_bytes(b):
    return hashlib.sha1(b).hexdigest()


# ---------------------------------------------------------------- tokenizer

CJK_RANGES = (
    (0x3040, 0x30FF),   # kana
    (0x3400, 0x4DBF),   # CJK ext A
    (0x4E00, 0x9FFF),   # CJK unified
    (0xF900, 0xFAFF),   # compatibility ideographs
    (0x20000, 0x2FA1F),  # ext B-F
)

_ASCII_WORD = re.compile(r"[a-zA-Z0-9_+#.]{1,64}")
_ID_SEP = re.compile(r"[._+#-]+")
# words that carry no retrieval value in this corpus
STOP = {
    "the", "and", "for", "with", "that", "this", "you", "are", "was", "have", "has",
    "not", "but", "from", "they", "them", "then", "there", "here", "into", "out",
    "your", "our", "its", "it's", "will", "would", "can", "could", "should", "may",
    "一个", "这个", "那个", "我们", "你们", "他们", "什么", "怎么", "可以", "如果",
    "因为", "所以", "但是", "而且", "就是", "还是", "已经", "没有", "不是", "这种",
    "进行", "使用", "通过", "以及", "并且", "或者", "然后", "现在", "这样", "那些",
    "的话", "一些", "一下", "这里", "那里", "时候", "自己", "需要", "应该", "可能",
}


def _is_cjk(ch):
    o = ord(ch)
    for lo, hi in CJK_RANGES:
        if lo <= o <= hi:
            return True
    return False


def tokenize(text, min_cjk_run=1):
    """CJK -> character bigrams (+ unigram for isolated chars), latin -> words.

    Bigrams give exact-ish matching for multi-character Chinese terms while a
    single-char query still matches. Returns a list (order preserved, dups kept).
    """
    if not text:
        return []
    text = unicodedata.normalize("NFKC", text)
    toks = []
    run = []

    def flush():
        if not run:
            return
        s = "".join(run)
        if len(s) == 1:
            toks.append(s)
        else:
            for i in range(len(s) - 1):
                toks.append(s[i:i + 2])
            # full short run helps exact 2-4 char terms rank a touch higher
            if len(s) <= 4:
                toks.append(s)
        run.clear()

    for ch in text:
        if _is_cjk(ch):
            run.append(ch)
        else:
            flush()
            if ch.isalnum() or ch in "_+#.":
                continue
    flush()

    for m in _ASCII_WORD.finditer(text):
        w = m.group(0).lower()
        if len(w) == 1 and not w.isdigit():
            continue
        w = w.strip(".")
        if not w or w in STOP:
            continue
        toks.append(w)
        # identifiers like `session.v4.jsonl.zstd` also索引各段，保证片段查询能命中
        if len(w) >= 6 and _ID_SEP.search(w):
            for piece in _ID_SEP.split(w):
                if len(piece) >= 3 and piece not in STOP:
                    toks.append(piece)
    return [t for t in toks if t and t not in STOP]


def tokenize_unique(text, cap=4000):
    """Unique terms with per-term frequency, capped for index size sanity."""
    toks = tokenize(text)
    if not toks:
        return {}
    counts = {}
    for t in toks:
        counts[t] = counts.get(t, 0) + 1
    if len(counts) <= cap:
        return counts
    # keep the rarest-looking (longest) terms when a chunk is enormous
    items = sorted(counts.items(), key=lambda kv: (-len(kv[0]), -kv[1]))[:cap]
    return dict(items)
