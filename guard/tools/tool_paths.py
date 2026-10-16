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
#: 本机覆盖（**gitignored**）：`{"workspace": "<本机工作区根>"}`。
#: 为什么要有：`<WORKSPACE>` 是"本机事实"，不能写进公开仓；但读取器又必须能解析它
#: （2026-10-16 实测：`tool_paths.json` 写着 `<WORKSPACE>` 而读取器不展开 ⇒
#:  返回字面量 `<WORKSPACE>/dsh-rag-memory/bin/mem.py` 这个**不存在的路径** ⇒
#:  g_xcheck 拿不到记忆库 CLI ⇒ 负例只到"未验"、判不出红。**声明了机制却没实现 = 假根治**。）
LOCAL_CONFIG = Path(__file__).resolve().parent / "tool_paths.local.json"
_cache: dict | None = None


def workspace_root() -> Path:
    """`<WORKSPACE>` 的取值（**本机工作区根**）。解析顺序：

    ① 环境变量 `DSH_WORKSPACE`（最高优先，便于临时切仓）
    ② 本地覆盖 `tools/tool_paths.local.json` 的 `workspace`（**gitignored**）
    ③ 兜底：`guard/` 的**父目录**——本机 `_guard` 就在 `<WORKSPACE>/_guard`，
       对外发布件也在 `<仓根>/guard` ⇒ 同一套推导在两种布局下都对。

    ★ 三级都不命中时不静默：调用方拿到的路径若不存在，**自己会显式报错**（见 tool_path）。
    """
    v = os.environ.get("DSH_WORKSPACE")
    if v:
        return Path(v)
    if LOCAL_CONFIG.exists():
        try:
            w = json.loads(LOCAL_CONFIG.read_text(encoding="utf-8")).get("workspace")
            if w:
                return Path(w)
        except Exception as exc:                   # 读不到必须可见（不许静默回落）
            print("tool_paths: 本地覆盖读取失败（%s）：%r" % (LOCAL_CONFIG, exc),
                  file=sys.stderr)
    return Path(__file__).resolve().parent.parent.parent      # tools -> guard -> 仓根


def expand_path(p: str | os.PathLike) -> Path:
    """展开路径占位符：`<WORKSPACE>` / `<HOME>`。

    **为什么要它**：占位符是"通用件里不写本机路径"的唯一办法；
    但占了位就必须有人**填**——只占不填等于写了个不存在的路径（那是静默失效）。
    """
    s = str(p)
    s = s.replace("<WORKSPACE>", str(workspace_root()))
    s = s.replace("<HOME>", str(Path.home()))
    return Path(s)


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
    返回值已展开 `<WORKSPACE>` / `<HOME>`（未展开的占位符路径**必然不存在**，
    调用方据此报错即可，不会"看起来能用"）。
    """
    item = _load().get(key) or {}
    env_name = env or item.get("env") or ""
    if env_name:
        v = os.environ.get(env_name)
        if v:
            return Path(v)
    p = item.get("path")
    return expand_path(p) if p else None
