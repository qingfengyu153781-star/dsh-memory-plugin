# -*- coding: utf-8 -*-
"""tool_paths · 通用层的「本机专有路径」读取器（R-56）

**为什么有这个东西**：`_guard` 是跨项目通用层，代码里不许写死本机/具体项目的路径
（写死了，换项目或换机就废，而且在本机永远不报错）。

⇒ 路径集中配置在 `tools/tool_paths.json`，**逐键可用环境变量覆盖**。
⇒ 读取失败**不静默**：打 stderr 并返回 `None`，**调用方必须显式处理**
   （拿 `None` 当"路径为空"接着跑，就是静默失败）。

用法::

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    from tool_paths import tool_path
    p = tool_path("mem_py", "DSH_MEM_PY")   # 环境变量 DSH_MEM_PY 优先
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CONFIG = Path(__file__).resolve().parent / "tool_paths.json"
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception as exc:                   # 读不到必须可见
            print("tool_paths: 配置读取失败（%s）：%r" % (CONFIG, exc), file=sys.stderr)
            _cache = {}
    return _cache


def tool_path(key: str, env: str | None = None) -> Path | None:
    """取本机专有路径：**环境变量优先**，其次 `tools/tool_paths.json`。

    返回 `None` 表示"没配也没给环境变量"——**调用方必须显式处理**（不许当空路径继续跑）。
    """
    item = _load().get(key) or {}
    env_name = env or item.get("env") or ""
    if env_name:
        v = os.environ.get(env_name)
        if v:
            return Path(v)
    p = item.get("path")
    return Path(p) if p else None
