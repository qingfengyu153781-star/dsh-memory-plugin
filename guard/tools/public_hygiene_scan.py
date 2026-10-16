#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""public_hygiene_scan · 对外面卫生扫描器（凭据 / 本机绝对路径 / PII）

## 治的哪条规矩（R-55，2026-10-15 立）

**R-55**：任何**对外面**（要推 GitHub 的东西、要交付给别人的产物、要引入/固化进我们仓的
外部件）在定稿前必须过一遍本扫描——**凭据、本机绝对路径字面量、隐私信息不得出现**；
命中即红，要放过必须在同行业写 `hygiene-allow` 标记并说明理由。

## 来源

linghu（FuRongJun-1999）的**边界章程**（`docs/architecture.md` §6）与**引入双清单**
（内容政策清单 + 隐私清单。它每引入一件都要"机械扫描：无本机路径 / 无凭据 / 无邮箱 / 无本地 IP"）。
**它那份扫描是人肉的（写在 `docs/intake/*/AUDIT.md` 里），没有工具**——本器把这一步机器化。

## 判据（命中即红）

| 类 | 规则 | 例子 |
|---|---|---|
| 凭据 | `CRED-*` | AWS `AKIA…` / `sk-…` / `ghp_…` / `github_pat_…` / `xox?-…` / 私钥块 / `api_key="…"` 赋值字面量 |
| 本机路径 | `PATH-*` | `C:\\Users\\<名>\\…`、`D:\\agent\\…`、`/cygdrive/d/…` |
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
    ("PATH-WIN", "本机绝对路径（盘符+用户/工作区段）", re.compile(
        r"[A-Za-z]:[\\/](?:Users|agent|deepseekzmd)[\\/]", re.I)),
    ("PATH-CYG", "MSYS 盘符路径", re.compile(r"/cygdrive/[a-z]/", re.I)),
    ("PII-MAIL", "邮箱地址", re.compile(
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("PII-IP", "内网 IP", re.compile(
        r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")),
]

#: 同行出现即豁免（**必须写理由**：`# hygiene-allow: 原因`）
ALLOW_MARKERS = ("hygiene-allow", "nocheck")

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
             ".mypy_cache", ".pytest_cache", "_bak", ".idea"}
#: 跳过**备份/临时**文件：它们不是交付内容（★但交付打包前要确保没把它们一起带走——
#: 实测踩过：改文件时生成的 `*._bak_YYYYMMDD_HHMM` 被扫成"命中"，掩盖了真实状态）。
SKIP_FILE_PATS = ("._bak_", ".bak", "~", ".orig", ".rej", ".pyc", ".tmp")
#: 精确文件名跳过：`.env` 是**本机私密配置**（按定义不发布，且已被 .gitignore 忽略），
#: 不属于"对外面"——扫它只会产生噪音（实测 qoder-proxy/.env 有 3 处本机路径）。
#: ★`.env.example`（模板，会随仓发布）**不跳**，必须扫。
SKIP_EXACT_NAMES = {".env"}

#: 凭据的**占位符 / 测试假值**白名单：这些形态是正当的，不该报。
#: 实测假阳性三例：qoder-proxy README 的 `your-secret-custom-key`、
#: 伴学智能体的 `sk-dead-endpoint-for-offline-test`（离线测试）、`expired-token-for-probe`。
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


def _iter_files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for fn in sorted(filenames):
                    if fn in SKIP_EXACT_NAMES:
                        continue
                    if any(pat in fn for pat in SKIP_FILE_PATS):
                        continue
                    out.append(Path(dirpath) / fn)
        # 不存在的路径由调用方判（fail-closed）
    return out


def scan_file(path: Path, *, raw: bool = False, ignore_rules: tuple = ()) -> list[dict]:
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
    return hits


def scan(paths: list[str], *, raw: bool = False, ignore_rules: tuple = ()) -> dict:
    files = _iter_files(paths)
    hits: list[dict] = []
    for f in files:
        hits.extend(scan_file(f, raw=raw, ignore_rules=ignore_rules))
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
    }


def render(rep: dict) -> str:
    lines = []
    if not rep["hits"]:
        lines.append("对外面卫生：无命中（扫描 %d 个文件）" % rep["files_scanned"])
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
    lines.append("      ③ 本机路径 ⇒ 改成仓内相对路径或占位符。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="public_hygiene_scan.py",
        description="对外面卫生扫描：凭据 / 本机绝对路径 / PII（命中即红）")
    ap.add_argument("paths", nargs="*", help="要扫的文件或目录")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--raw", action="store_true", help="输出原文（★ 不要外发）")
    ap.add_argument("--quiet", action="store_true", help="只打摘要")
    a = ap.parse_args(argv)

    if not a.paths:
        print("用法错误：必须显式给路径（本器不猜对外面；没给路径=没扫，不当绿）",
              file=sys.stderr)
        return 2
    missing = [p for p in a.paths if not Path(p).exists()]
    if missing:
        print("路径不存在：%s" % ", ".join(missing), file=sys.stderr)
        return 2

    rep = scan(a.paths, raw=a.raw)
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
