# -*- coding: utf-8 -*-
"""转义安全的源码改写器 —— 取代「用 bash heredoc 拼 .py」（R-66 的执行件）

## 为什么存在（4 次事故，逐条有据）

2026-10-16 一天里，我**4 次**用 bash heredoc / `python - <<EOF` 去改 `.py`，而 shell 与 python 的
**双重转义**把 `\\n` 变成真换行 ⇒ 写出的文件里字符串字面量断了行：

```
print("[hook] 失败：\n" + tail)     ←  我想要的
print("[hook] 失败：                 ←  实际写进去的（换行真的断了）
" + tail)
```
每一次都是"**跑起来才发现**"（其中一次当场把 `hook.py` 写成 `SyntaxError`）。
⇒ 这不是记性问题，是**工具选错了**：拿 shell 拼源码，转义规则叠了四层（bash→python→字符串→正则）。

## 用法（把"要改什么"写成 JSON，用文件工具落盘 —— 全程没有 shell 转义参与）

```json
{
  "file": "<WORKSPACE>/_guard/hook.py",
  "edits": [
    {"old": "要替换的原文（必须在文件里唯一）", "new": "替换成什么（可含真换行/引号，原样写入）"}
  ]
}
```

    python _guard/tools/safe_patch.py spec.json [--dry-run]

**它保证的事**：① 每处 `old` 必须**恰好命中一次**（0 次或多次 ⇒ 拒改，不猜）
② 改前自动备份（同名 `.bak_<ts>`）③ 改后**当场体检**（`.py`→compile / `.json`→json.loads / `.js`→node --check）
④ 体检不过 ⇒ **回滚**并报错（不留下坏文件）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


def verify(p: Path) -> tuple[bool, str]:
    """改后体检：与 g_syntax 同口径（.py compile / .json json.loads / .js node --check）。"""
    try:
        suf = p.suffix.lower()
        if suf == ".py":
            compile(p.read_text(encoding="utf-8", errors="replace"), str(p), "exec")
        elif suf == ".json":
            json.loads(p.read_text(encoding="utf-8"))
        elif suf in (".js", ".mjs", ".cjs"):
            r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60)
            if r.returncode != 0:
                return False, (r.stderr or r.stdout).strip()[-200:]
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc).splitlines()[-1][:200]


def main() -> int:
    ap = argparse.ArgumentParser(prog="safe_patch")
    ap.add_argument("spec", help="补丁 JSON（file + edits[{old,new}]）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    spec = json.loads(Path(a.spec).read_text(encoding="utf-8"))
    p = Path(spec["file"])
    if not p.is_file():
        print("文件不存在：%s" % p, file=sys.stderr)
        return 1
    src = p.read_text(encoding="utf-8")
    out = src
    for i, e in enumerate(spec.get("edits") or [], 1):
        old, new = e.get("old") or "", e.get("new") or ""
        hits = out.count(old)
        if hits != 1:
            print("第 %d 处 old 命中 %d 次（必须恰好 1 次）⇒ 拒改，不猜：%.60r" % (i, hits, old),
                  file=sys.stderr)
            return 1
        out = out.replace(old, new, 1)
    if out == src:
        print("无实际变化（edits 为空或 new 与 old 相同）")
        return 0
    if a.dry_run:
        print("dry-run：将改动 %d 处（未落盘）" % len(spec.get("edits") or []))
        return 0
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = p.with_name(p.name + ".bak_%s" % ts)
    shutil.copy2(p, bak)
    p.write_text(out, encoding="utf-8")
    ok, why = verify(p)
    if not ok:
        shutil.copy2(bak, p)                   # ★ 体检不过 ⇒ 回滚，不留坏文件
        print("体检不过 ⇒ **已回滚**：%s\n  原因：%s" % (p, why), file=sys.stderr)
        return 1
    print("已改 %s（%d 处）｜备份 %s｜体检：%s" % (p, len(spec.get("edits") or []), bak.name, why))
    return 0


if __name__ == "__main__":
    sys.exit(main())
