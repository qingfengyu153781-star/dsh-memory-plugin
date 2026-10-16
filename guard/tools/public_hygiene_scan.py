#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""public_hygiene_scan · 对外面卫生扫描器（凭据 / 本机绝对路径 / PII / 运行台账 / 项目专有名）

## 治的哪条规矩（R-55，2026-10-15 立）

**R-55**：任何**对外面**（要推 GitHub 的东西、要交付给别人的产物、要引入/固化进我们仓的
外部件）在定稿前必须过一遍本扫描——**凭据、本机绝对路径字面量、隐私信息不得出现**；
命中即红，要放过必须在同行业写 `hygiene-allow` 标记并说明理由。

## 来源

lingshu（FuRongJun-1999）的**边界章程**（`docs/architecture.md` §6）与**引入双清单**
（内容政策清单 + 隐私清单。它每引入一件都要"机械扫描：无本机路径 / 无凭据 / 无邮箱 / 无本地 IP"）。
**它那份扫描是人肉的**（写在 `docs/intake/*/AUDIT.md` 里），没有工具——本器把这一步机器化。

## ★★ 2026-10-16 根治记录：本器自己曾**整类瞎掉**（假绿）

**现象**：本器对 `dsh-memory-plugin` 报「0 命中」，而该仓当时**已发布**着
`<盘符>:\\<工作区>\\...`、`<盘符>:\\Users\\<名>\\...`、`示例项目A/示例项目B` 项目内容、以及约 1300 行运行台账。
**报"干净"而实际满仓** —— 这正是本器要治的「假绿」，出在仪器自己身上。

**三层根因（逐层实测，不是推断）**：

| 层 | 病根 | 为什么"看不出来" |
|---|---|---|
| ① | `PATH-WIN` 正则写的是 `[A-Za-z]:[\\\\/](?:Users 或 agent)[\\\\/]` —— **只认单反斜杠**；而 JS/JSON 源码里写的是 `'<盘符>:\\<工作区>\\...'`（**文件里是双反斜杠**） | 正则不报错、就是不命中 ⇒ **量错对象**：它量的是"自然书写形态"，而代码里的路径是**转义形态** |
| ② | 只扫**行内容**，**没有"路径级"规则** ⇒ `_guard_ledger/*.jsonl`、`ledger/*.jsonl` 这类**整类运行台账**被跟踪发布也照绿 | 台账里每一行单看都"没凭据"（`{"demo": true, "ok": true}`）⇒ 内容规则天然抓不到 |
| ③ | **别的项目名**完全不在规则表里 | 项目名是**本地专有词表**，本器却当"没有就不存在" ⇒ 静默放过 |

**修法**：① 正则改 `{1,2}` 同时覆盖转义与自然形态；② 新增**路径级**规则 `RT-*`；
③ 新增 `PROJ-NAME`，词表放**本地 gitignored 配置**（`hygiene_forbidden.json`）——
**把项目名写进公开仓 = 用扫描器再泄露一次**；④ 新增 `--selftest`：**仪器必须能证明自己抓得到**
（每种规则喂一条负例，抓不到就红）。

## 判据（命中即红）

| 类 | 规则 | 例子 |
|---|---|---|
| 凭据 | `CRED-*` | AWS `AKIA…` / `sk-…` / `ghp_…` / `github_pat_…` / `xox?-…` / 私钥块 / `api_key="…"` 赋值字面量 |
| 本机路径 | `PATH-*` | `<盘符>:\\Users\\<名>\\…`、`<盘符>:\\<工作区>\\…`、`/cygdrive/<盘符>/…`，**含双反斜杠转义形态** |
| 运行台账 | `RT-*` | **路径**命中 `_guard_ledger/**`、`ledger/*.jsonl`、`_guard_vault/**` ⇒ 整类不得进对外面 |
| 项目专有名 | `PROJ-NAME` | 读本地 `hygiene_forbidden.json` 的词表；命中即红 |
| 隐私 | `PII-*` | 邮箱、内网 IP（10./192.168./172.16-31.） |

**豁免**：同一行含 `hygiene-allow`（或 `nocheck`）即放过——**且必须写理由**
（`# hygiene-allow: 这是公开的示例邮箱`）。豁免是**行级、显式、可 grep**的，
不设"全局白名单文件"（那样会静默积累）。

## ★ 输出纪律：**打完码再给人看**

命中内容一律**中间打码**（`sk-abcd…wxyz` → `sk-a…yz`）——
否则"扫描报告"本身就成了泄露面。原文只在 `--raw` 下给出，且提示不要外发。

## 用法

    python _guard/tools/public_hygiene_scan.py <路径...> [--json] [--raw]
    python _guard/tools/public_hygiene_scan.py <路径...> --quiet     # 只打摘要
    python _guard/tools/public_hygiene_scan.py --selftest            # ★ 仪器自证（每规则一条负例）

退出码：0 无命中 / 1 有命中 / 2 用法错误（含"没给路径"——**不当绿**）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

#: (规则号, 说明, 正则)
RULES = [
    ("CRED-AWS", "AWS Access Key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("CRED-SK", "OpenAI/DeepSeek 风格 API key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("CRED-GH", "GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("CRED-GHPAT", "GitHub fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("CRED-SLACK", "Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("CRED-PRIVKEY", "私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("CRED-ASSIGN", "凭据赋值字面量", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key)\b"
        r"\s*[:=]\s*['\"][^'\"\s]{8,}['\"]")),
    # ★★ 修 ①：`{1,2}` 必须给——源码里的路径是**转义形态**（`<盘符>:\\<工作区>\\`），
    #    只写单反斜杠会整类漏掉（2026-10-16 实测：满仓本机路径报 0 命中）。
    ("PATH-WIN", "本机绝对路径（盘符+用户/工作区段，含转义形态）", re.compile(
        r"[A-Za-z]:[\\/]{1,2}(?:Users|agent|deepseekzmd)[\\/]{1,2}", re.I)),
    ("PATH-CYG", "MSYS 盘符路径", re.compile(r"/cygdrive/[a-z]/", re.I)),
    # ★★ 2026-10-16 补：**指向本机私有记录的指针**也算泄露。
    #   起因：卫生扫描全绿之后，仓里仍留着本机会话 id 与 DB 序号 —— 它们不含凭据、
    #   不是路径，却**指向私有记录**。（用户原话那一类无法用正则判，见 README §7。）
    ("REF-SESSION", "本机会话 id 指针",
     re.compile(r"\bsession-[0-9a-fA-F]{6,}\b")),
    ("PII-MAIL", "邮箱地址", re.compile(
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("PII-IP", "内网 IP", re.compile(
        r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")),
]

#: ★★ 修 ②：**路径级**规则——量"这个文件该不该出现在对外面"，而不是量它某一行写了什么。
#: 台账/快照/版本库是**运行时副产物**：每行单看都干净（`{"demo": true, "ok": true}`），
#: 内容规则天然抓不到 ⇒ 必须按**路径**判。（2026-10-16 实测：1300 行运行台账已随公开仓发布。）
PATH_RULES = [
    ("RT-LEDGER", "运行台账（副产物，不进对外面）", re.compile(r"(^|/)_guard_ledger/")),
    ("RT-LEDGER", "运行台账（副产物，不进对外面）", re.compile(r"(^|/)ledger/[^/]*\.jsonl$")),
    ("RT-VAULT", "版本库快照（副产物，不进对外面）", re.compile(r"(^|/)_guard_vault/")),
    ("RT-BACKUP", "备份文件（副产物，不进对外面）", re.compile(r"(\.bak|_bak_|\.orig|\.rej)(\.|$)")),
]

#: ★★ 修 ③：项目专有名的词表**不进公开仓**——本地配置（gitignored）。
#: 为什么：把项目名写进规则表 = 用扫描器**再泄露一次**（这是"治泄露的东西自己泄露"）。
FORBIDDEN_FILE = Path(__file__).resolve().parent / "hygiene_forbidden.json"

#: 同行出现即豁免（**必须写理由**：`# hygiene-allow: 原因`）
ALLOW_MARKERS = ("hygiene-allow", "nocheck")

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
             ".mypy_cache", ".pytest_cache", "_bak", ".idea"}
#: 跳过**备份/临时**文件：它们不是交付内容（★但交付打包前要确保没把它们一起带走——
#: 实测踩过：改文件时生成的 `*._bak_YYYYMMDD_HHMM` 被扫成"命中"，掩盖了真实状态）。
#: ★ 注意：这里"跳过"的是**扫描噪音**；而"备份文件被跟踪进仓"是 `RT-BACKUP` 管的另一回事
#:   （路径级规则只在**显式给到该路径**时判，不被本行跳过影响——见 `_iter_files` 的 `skip_noise`）。
SKIP_FILE_PATS = ("._bak_", ".bak", "~", ".orig", ".rej", ".pyc", ".tmp")
#: 精确文件名跳过：`.env` 是**本机私密配置**（按定义不发布，且已被 .gitignore 忽略），
#: 不属于"对外面"——扫它只会产生噪音（实测 qoder-proxy/.env 有 3 处本机路径）。
#: ★`.env.example`（模板，会随仓发布）**不跳**，必须扫。
SKIP_EXACT_NAMES = {".env"}

#: 凭据的**占位符 / 测试假值**白名单：这些形态是正当的，不该报。
#: 实测假阳性三例：qoder-proxy README 的 `your-secret-custom-key`、
#: 离线测试用的 `sk-dead-endpoint-for-offline-test`、`expired-token-for-probe`。
#: ★对**所有 `CRED-*` 规则**生效；这些模式足够特异（`your-`/`<`/`-for-test`/`dead-`…），
#:   误漏真 key 的概率远低于假阳性对闸可信度的伤害。
PLACEHOLDER_PATS = ("your-", "your_", "yourkey", "<", "xxx", "***", "changeme",
                    "placeholder", "example", "dummy", "redacted", "todo",
                    "secret-custom-key", "${", "{{",
                    "-for-test", "-for-probe", "for-offline", "dead-", "expired-",
                    "fake", "-sample", "test-key", "test-token")

#: 邮箱白名单（明确不需要报的**示例/占位**域名）
MAIL_OK_DOMAINS = ("example.com", "example.org", "example.net", "test.local", "invalid")

MAX_EXCERPT = 120


def load_forbidden(path: Path | None = None) -> list[str]:
    """读本地项目名词表（不存在 ⇒ 空表，不报错；**不静默当"没有"**——见 selftest 第 ⑤ 例）。"""
    p = path or FORBIDDEN_FILE
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    names = data.get("names") if isinstance(data, dict) else data
    return [str(n) for n in (names or []) if str(n).strip()]


def _mask(s: str) -> str:
    """中间打码：只留首尾各 3 字符。"""
    s = s.strip()
    if len(s) <= 8:
        return s[:2] + "…"
    return s[:3] + "…" + s[-3:]


def _is_text_file(p: Path) -> bool:
    try:
        with p.open("rb") as fh:
            chunk = fh.read(4096)
    except OSError:
        return False
    return b"\x00" not in chunk


def _is_noise(name: str) -> bool:
    """噪音文件（备份/临时）：**内容不扫**，但**路径级规则照样判**。

    ★ 这两个是两件事，别混（实测踩过）：
      · "扫它内容是噪音"（旧逻辑）⇒ 顺手把整条路径规则也跳过了 ⇒ `.bak` 被跟踪也照绿；
      · "它出现在对外面里就是错的" ⇒ 该红，得由 `RT-BACKUP` 判。
    """
    return name in SKIP_EXACT_NAMES or any(pat in name for pat in SKIP_FILE_PATS)


def _iter_files(paths: list[str]) -> list[Path]:
    """目录遍历（**含**噪音文件；噪音的取舍在 `scan()` 里分别处理）。"""
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for fn in sorted(filenames):
                    out.append(Path(dirpath) / fn)
        # 不存在的路径由调用方判（fail-closed）
    return out


def _repo_root(p: Path) -> Path | None:
    cur = p if p.is_dir() else p.parent
    for cand in [cur, *cur.parents]:
        if (cand / ".git").exists():
            return cand
    return None


def _tracked_files(paths: list[str]) -> list[Path]:
    """★ 对外面的**权威口径**：`git ls-files`（**已跟踪**）——本地未跟踪的备份不该算红。

    为什么必须是已跟踪：判"这个仓对外发布了什么"只有 `git ls-files` 说得准；
    目录遍历会把本机 `*.bak` 当成命中，**红得不是地方**（实测：改文件的备份被扫红，
    真正的发布内容反而被噪音淹没）。
    """
    import subprocess
    out: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        p = Path(raw).resolve()
        root = _repo_root(p)
        if root is None:
            continue
        try:
            res = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                                 capture_output=True, check=True)
        except Exception:
            continue
        base = p if p.is_dir() else p.parent
        for name in res.stdout.decode("utf-8", "replace").split("\0"):
            if not name:
                continue
            f = (root / name).resolve()
            try:
                f.relative_to(base)
            except ValueError:
                continue
            if f not in seen:
                seen.add(f)
                out.append(f)
    return out


def scan_path_rules(path: Path) -> list[dict]:
    """★ 路径级规则：不读内容，只判"这个文件该不该出现在对外面"。"""
    hits: list[dict] = []
    norm = str(path).replace("\\", "/")
    for rule_id, desc, rx in PATH_RULES:
        if rx.search(norm):
            hits.append({
                "file": norm, "line": 0, "rule": rule_id, "desc": desc,
                "excerpt": "（路径级命中；该文件属运行副产物，整类不进对外面）",
            })
    return hits


def scan_file(path: Path, *, raw: bool = False, ignore_rules: tuple = (),
              forbidden: tuple = ()) -> list[dict]:
    hits: list[dict] = []
    if not _is_text_file(path):
        return hits
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return hits
    for lineno, line in enumerate(text.splitlines(), 1):
        if any(m in line for m in ALLOW_MARKERS):
            continue
        for rule_id, desc, rx in RULES:
            if rule_id in ignore_rules:            # ★ 面级忽略（见 g_public 的登记面格式）
                continue
            for m in rx.finditer(line):
                token = m.group(0)
                if rule_id == "PII-MAIL" and any(
                        token.lower().endswith("@" + d) for d in MAIL_OK_DOMAINS):
                    continue
                if rule_id.startswith("CRED-") and any(
                        p in token.lower() for p in PLACEHOLDER_PATS):
                    continue                       # 占位符 / 测试假值，正当
                hits.append({
                    "file": str(path).replace("\\", "/"),
                    "line": lineno,
                    "rule": rule_id,
                    "desc": desc,
                    "excerpt": token if raw else _mask(token),
                })
        # ★ 项目专有名（本地词表驱动）
        if "PROJ-NAME" not in ignore_rules:
            for name in forbidden:
                if name and name in line:
                    hits.append({
                        "file": str(path).replace("\\", "/"),
                        "line": lineno,
                        "rule": "PROJ-NAME",
                        "desc": "别的项目专有名（本地词表 hygiene_forbidden.json）",
                        "excerpt": name if raw else _mask(name),
                    })
    return hits


def scan(paths: list[str], *, raw: bool = False, ignore_rules: tuple = (),
         forbidden: tuple | None = None, tracked_only: bool = False) -> dict:
    names = tuple(load_forbidden() if forbidden is None else forbidden)
    files = _tracked_files(paths) if tracked_only else _iter_files(paths)
    hits: list[dict] = []
    for f in files:
        hits.extend(scan_path_rules(f))            # ★ 先判路径级（整类副产物；**噪音也算**）
        if _is_noise(f.name):
            continue                               # 内容不扫（避免噪音淹没真命中）
        hits.extend(scan_file(f, raw=raw, ignore_rules=ignore_rules, forbidden=names))
    by_rule: dict[str, int] = {}
    for h in hits:
        by_rule[h["rule"]] = by_rule.get(h["rule"], 0) + 1
    return {
        "paths": list(paths),
        "files_scanned": len(files),
        "hits": hits,
        "hit_total": len(hits),
        "by_rule": dict(sorted(by_rule.items())),
        "exit_code": 1 if hits else 0,
        "raw": raw,
        "forbidden_names": len(names),
        "tracked_only": tracked_only,
    }


def render(rep: dict) -> str:
    lines = []
    if not rep["hits"]:
        lines.append("对外面卫生：无命中（扫描 %d 个文件；项目名词表 %d 条）"
                     % (rep["files_scanned"], rep.get("forbidden_names", 0)))
        return "\n".join(lines)
    lines.append("对外面卫生：命中 %d 处（扫描 %d 个文件）"
                 % (rep["hit_total"], rep["files_scanned"]))
    lines.append("按规则：%s" % ", ".join("%s=%d" % (k, v) for k, v in rep["by_rule"].items()))
    lines.append("")
    for h in rep["hits"][:60]:
        lines.append("  %s:%d  [%s %s]  %s"
                     % (h["file"], h["line"], h["rule"], h["desc"], h["excerpt"]))
    if rep["hit_total"] > 60:
        lines.append("  …另有 %d 处（用 --json 取全量）" % (rep["hit_total"] - 60))
    lines.append("")
    lines.append("处置：① 真凭据 ⇒ 立刻吊销并改环境变量/密钥库，**不要只改文件**；")
    lines.append("      ② 若确属公开示例 ⇒ 同行补 `# hygiene-allow: <理由>`；")
    lines.append("      ③ 本机路径 ⇒ 改成仓内相对路径或占位符；")
    lines.append("      ④ 运行台账/备份 ⇒ **取消跟踪并 gitignore**（整类副产物不进仓）；")
    lines.append("      ⑤ 别的项目名 ⇒ 改成中性示例（词表在本地 hygiene_forbidden.json）。")
    return "\n".join(lines)


# ------------------------------------------------------------------ ★ 仪器自证
#: (用例名, 文件名, 内容, 期望命中的规则号或 None=期望干净, 是否路径级)
SELFTEST_CASES = [
    ("凭据 AWS（真形态）", "a.txt", 'AWS = "AKIA' + '3K7QZ9WX2MPL4NVT"\n', "CRED-AWS", False),
    ("凭据 sk-（真形态）", "b.txt", 'k = "sk-' + 'abcdefghijklmnopqrstuvwxyz123456"\n', "CRED-SK", False),
    ("凭据占位符 ⇒ 正当放过", "c.txt", 'k = "sk-your-key-placeholder-1234567"\n', None, False),
    ("邮箱示例域名 ⇒ 放过", "d.txt", 'mail = "nobody@example.com"\n', None, False),
    ("邮箱真域名 ⇒ 命中", "e.txt", 'mail = "someone@' + 'qq.com"\n', "PII-MAIL", False),
    # ★★ 回归例：**双反斜杠转义形态**——2026-10-16 整类漏掉的正是它
    ("本机路径（自然形态）", "f.txt", 'p = "D:' + '\\agent\\x.txt"\n', "PATH-WIN", False),
    ("本机路径（**转义**形态）", "g.txt", 'p = "D:' + '\\\\agent\\\\x.txt"\n', "PATH-WIN", False),
    ("用户目录（转义形态）", "h.txt", 'p = "C:' + '\\\\Users\\\\someone\\\\x"\n', "PATH-WIN", False),
    ("cygwin 盘符路径", "i.txt", 'p = "/cygdrive/' + 'd/agent/x"\n', "PATH-CYG", False),
    ("行级豁免 ⇒ 放过", "j.txt", 'AWS = "AKIA' + '3K7QZ9WX2MPL4NVT"  # hygiene-allow: 公开示例\n',
     None, False),
    # ★★ 回归例：**路径级**——台账/备份整类副产物
    ("运行台账（路径级）", "_guard_ledger/path.jsonl", '{"demo": true, "ok": true}\n',
     "RT-LEDGER", True),
    ("工具层台账（路径级）", "ledger/g_public.jsonl", '{"demo": true, "ok": true}\n',
     "RT-LEDGER", True),
    ("备份文件（路径级）", "x.py.bak", 'print(1)\n', "RT-BACKUP", True),
    # ★★ 回归例：**项目专有名**（词表由调用方注入，公开仓里不放真名）
    ("别的项目专有名", "k.txt", "本案按 ZZZ_PROJ 的断言执行\n", "PROJ-NAME", False),
    # ★★ 回归例：**会话 id 指针**（不含凭据也不是路径，却是泄露）
    #   ★ 用**字符串拼接**写样本，否则本器自己的规则会命中本文件（与凭据夹具同一手法）
    ("会话 id 指针", "m.txt", "出处：会话 session-" + "eeef4dba 第 3 轮\n", "REF-SESSION", False),
]


def selftest() -> int:
    """★ 仪器自证：每条规则喂一条负例，**抓不到就红**。

    为什么必须有：本器曾整类瞎掉（双反斜杠 / 路径级 / 项目名）却一直报 0 命中。
    **"报干净"本身必须可被证伪** —— 所以自检要能证明"该抓的抓得到"，
    而不是只证明"能跑"。
    """
    import shutil
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="hygiene_selftest_"))
    ok = True
    print("对外面卫生扫描器 · 自证（%d 例）" % len(SELFTEST_CASES))
    try:
        for name, rel, content, want, is_path in SELFTEST_CASES:
            target = base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            rep = scan([str(base)], forbidden=("ZZZ_PROJ",))
            got = sorted({h["rule"] for h in rep["hits"] if h["file"].endswith(rel)})
            if want is None:
                good = not got
            else:
                good = want in got
            ok &= good
            print("  %s %-28s 期望 %-9s 实得 %s"
                  % ("✓" if good else "✗", name, want or "无命中", got or "无命中"))
            # 清掉，保证每例独立
            if target.exists():
                target.unlink()
    finally:
        shutil.rmtree(base, ignore_errors=True)
    # ⑤ 词表缺失必须**显式报"0 条"**（不静默当"没有项目名这回事"）
    n = len(load_forbidden())
    print("  词表：hygiene_forbidden.json 加载 %d 条%s"
          % (n, "" if n else "（★本地未配置 ⇒ PROJ-NAME 面**空跑**，这是可知的边界，不是绿）"))
    print("  自证结论：%s" % ("通过（每种规则都抓得到负例）" if ok else "未通过 —— 扫描器有盲区，不当绿"))
    return 0 if ok else 1


def _warn_uncommitted(paths: list[str]) -> None:
    """★ `--tracked` 只看**索引**（`git ls-files`）：新加但**还没 `git add`** 的文件不在扫描面内。

    实测踩过（2026-10-16）：工作区扫描报"无命中"，而 `git archive HEAD` 一验就有 4 处 ——
    因为那 4 处所在的样本对当时**还没入库**。⇒ 有未提交改动时**显式提醒**：
    发布前请对 `git archive HEAD` 导出的树再扫一遍（那才是真正要发出去的内容）。
    """
    import subprocess
    for raw in paths:
        p = Path(raw).resolve()
        root = _repo_root(p)
        if root is None:
            continue
        try:
            res = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                                 capture_output=True, check=True)
        except Exception:
            continue
        dirty = [x for x in res.stdout.decode("utf-8", "replace").splitlines() if x.strip()]
        if dirty:
            print("★ 提醒：仓里有 %d 条未提交改动 —— `--tracked` 只看**索引**，"
                  "新加但未 add 的文件不在扫描面内。**发布前请对 `git archive HEAD` 再扫一遍**"
                  "（那才是真正发出去的内容）。" % len(dirty), file=sys.stderr)
            return


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="public_hygiene_scan.py",
        description="对外面卫生扫描：凭据 / 本机绝对路径 / PII / 运行台账 / 项目专有名（命中即红）")
    ap.add_argument("paths", nargs="*", help="要扫的文件或目录")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--raw", action="store_true", help="输出原文（★ 不要外发）")
    ap.add_argument("--quiet", action="store_true", help="只打摘要")
    ap.add_argument("--selftest", action="store_true", help="★ 仪器自证：每规则一条负例")
    ap.add_argument("--forbidden", help="逗号分隔的项目名词表（覆盖本地配置）")
    ap.add_argument("--tracked", action="store_true",
                    help="★ 对外面权威口径：只扫 `git ls-files` 已跟踪文件"
                         "（本地未跟踪的备份不该算红；台账/备份被跟踪则必红）")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()

    if not a.paths:
        print("用法错误：必须显式给路径（本器不猜对外面；没给路径=没扫，不当绿）",
              file=sys.stderr)
        return 2
    missing = [p for p in a.paths if not Path(p).exists()]
    if missing:
        print("路径不存在：%s" % ", ".join(missing), file=sys.stderr)
        return 2

    forb = tuple(x.strip() for x in a.forbidden.split(",") if x.strip()) if a.forbidden else None
    if a.tracked:
        _warn_uncommitted(a.paths)
    rep = scan(a.paths, raw=a.raw, forbidden=forb, tracked_only=a.tracked)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, sort_keys=True, indent=1))
    elif a.quiet:
        print("对外面卫生：命中 %d 处 / 扫描 %d 个文件（%s）"
              % (rep["hit_total"], rep["files_scanned"], rep["by_rule"] or "无"))
    else:
        print(render(rep))
    return rep["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
