# -*- coding: utf-8 -*-
"""_guard · 记忆库管道卫生闸 g_mem_hygiene —— 治「…」

## 治的哪条规矩（2026-10-15 新增 · R-58）

**R-58**：往对话里塞内容的**非真人来源**（`plugin:*` / `time-context` / `runtime-context` /
`agent-instructions` / `tool-jobs`）**不得**被记忆库登记成"用户原话"（`kind='user_text'`）。

## 为什么要做成闸（这是 2026-10-15 实测出来的病，不是假想）

**现象**：注意力编排器注入的 `<memory_recall>` / `<attention_orchestration>` 块、
运行环境快照、时间上下文、AGENTS.md 注入块 —— **全都被当成"用户说过的话"收进了索引**。

**证据（清洗前，同一把尺子实测）**：

| 量 | 实测 |
|---|---|
| `kind='user_text'` 且文本以注入标签开头的原子 | **3 704** |
| 其中 `<memory_recall` | 2 687 |
| `<attention_orchestration` | 326 |
| 运行环境快照 / 时间上下文 | 3 137 / 2 620 |
| `chunks.user_text` 含注入标签 | **83 464** |
| 真人 `user_text` 原子占"user_text 总数" | 24 360 → **只剩 6 933（29%）** |

**病根**：`src/memlib/dsh.py` 的 `user/message` 分支**只看记录类型、不看 `data.source.kind`**，
而原始记录里**本来就带** `{"kind": "plugin:dsh-memory-plugin"}` 这类标记（可核）。

**后果（为什么它值得一道闸）**：注入块里写着用户这一轮的**逐字 query**
⇒ 它是"与后续任何相关提问字面重合度最高"的一类文本 ⇒ 召回时稳定排前
⇒ **编排器把上一轮自己的输出当证据、再注入一遍**（自噬闭环；台账里 18.6% 的"出处"就是这么来的），
而"出处"字段填的甚至是**上一次的注入块本身**。

## 判据（每条都可复算）

| id | 判据 | 量的对象 |
|---|---|---|
| H1 | `USER_SOURCE_KINDS == {"user"}`（**fail-closed 白名单**存在） | 代码 |
| H2 | `plugin_injection ∈ pipeline.NO_CHUNK`（挡在检索之外） | 代码 |
| H3 | 行为负例：插件块 fixture ⇒ 落成 `plugin_injection`，**不是** `user_text` | 真跑抽取器 |
| H4 | 行为正例：真人 fixture ⇒ 仍是 `user_text`（**不误杀**） | 真跑抽取器 |
| H5 | 行为：注入块**不得出现在** `pipeline.fragments()` 的切片里 | 真跑切片器 |
| H6 | 现库：`kind='user_text'` 且以注入标签开头的原子 **== 0** | 现库（`--db`，慢） |
| H7 | 现库：`plugin_injection` 原子 **> 0**（**挡而不丢**，可审计） | 现库（`--db`） |

**★ 为什么默认只跑 H1–H5**：H6/H7 要在 32 万行的 `atoms` 上做 `LIKE` 全表扫（实测数秒），
不适合挂在每次写操作的收尾上。H1–H5 是**毫秒级**，而且**恰好是能第一时间发现回归的那几条**——
污染来自抽取器，抽取器一退化 H3/H5 立刻红。H6/H7 作为兜底，手工/CI 时用 `--db` 跑。

## 用法

    python _guard/gates/g_mem_hygiene.py --demo        # 负例自检（闸真的会拦）
    python _guard/gates/g_mem_hygiene.py               # 行为判据（毫秒级）
    python _guard/gates/g_mem_hygiene.py --db          # 追加现库判据（慢）

退出码：0 = 过；1 = 有判据不过（含"测不到 ⇒ 未核"的 fail-closed）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from guard import ProjectConfig  # noqa: E402
from tool_paths import expand_path  # noqa: E402  ★ 展开 <WORKSPACE>/<HOME>

CONFIG_REL = "tools/memory_hygiene.json"

# 注入标签（与 plugin 侧 `isOwnInjectionText` 的 INJECT_TAGS 对齐）
INJECT_TAGS = ("<memory_recall", "<attention_orchestration", "<goal_round",
               "<memory-orchestrator-alert")


def load_paths(cfg: ProjectConfig) -> dict:
    """读扫描面配置。**本地路径不进代码**（R-56：通用层不得写死具体项目）。"""
    p = Path(__file__).resolve().parent.parent / CONFIG_REL
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        # ★ 不静默吞：配置坏了却当"没配置"跑下去，就是又一处"失败不改变可见状态"
        print("[g_mem_hygiene] 扫描面配置解析失败（按未配置处理，判据会走 fail-closed）：%r"
              % (e,), file=sys.stderr)
        return {}


class HygieneGate:
    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self.paths = load_paths(cfg)
        self.mem_root = (expand_path(self.paths["mem_root"])
                         if self.paths.get("mem_root") else None)

    # ---------------------------------------------------------------- 载入被测代码
    def _load_memlib(self):
        """导入记忆库的抽取器与切片器。导不进来 ⇒ 抛异常（调用方判"未核"，fail-closed）。"""
        if self.mem_root is None:
            raise RuntimeError("配置缺 mem_root（%s）" % CONFIG_REL)
        src = str(self.mem_root / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        from memlib import dsh, pipeline  # type: ignore
        return dsh, pipeline

    # ---------------------------------------------------------------- H1–H5
    def check_behavior(self, dsh_mod=None, pipeline_mod=None) -> list[dict]:
        rows: list[dict] = []
        try:
            if dsh_mod is None or pipeline_mod is None:
                dsh_mod, pipeline_mod = self._load_memlib()
        except Exception as e:  # noqa: BLE001
            # ★ 载不进来**必须可见**：判据量不了就报"未核"，绝不因为"读不到"而放绿
            print("[g_mem_hygiene] 载不进抽取器（不静默；判据按未核处理）：%r" % (e,),
                  file=sys.stderr)
            rows.append({"id": "H0", "rule": "记忆库管道卫生", "ok": None,
                         "detail": "**载不进抽取器**（%r）⇒ 不判绿（fail-closed）；"
                                   "先确认 %s 可导入" % (e, CONFIG_REL)})
            return rows

        wl = getattr(dsh_mod, "USER_SOURCE_KINDS", None)
        rows.append({"id": "H1", "rule": "记忆库管道卫生", "ok": wl == {"user"},
                     "detail": "真人来源白名单 USER_SOURCE_KINDS=%r（须恰为 {'user'}，"
                               "取不到 kind 一律不算真人）" % (wl,)})

        nchunk = getattr(pipeline_mod, "NO_CHUNK", set())
        rows.append({"id": "H2", "rule": "记忆库管道卫生",
                     "ok": "plugin_injection" in nchunk,
                     "detail": "NO_CHUNK=%r（须含 'plugin_injection'：存得下、可审计、"
                               "但不进 chunks、检索不到）" % (sorted(nchunk),)})

        # 三条 fixture：真人 / 插件块 / 运行环境块
        fixture = [
            {"type": "user/message", "seq": 1, "time": 1,
             "data": {"content": [{"type": "text", "text": "真人提问：他的功能是什么"}],
                      "source": {"kind": "user"}}},
            {"type": "user/message", "seq": 2, "time": 2,
             "data": {"content": [{"type": "text",
                                   "text": '<memory_recall mode="digest" source="dsh-memory-plugin">注入块正文'}],
                      "source": {"kind": "plugin:dsh-memory-plugin"}}},
            {"type": "user/message", "seq": 3, "time": 3,
             "data": {"content": [{"type": "text", "text": "Current runtime context. This snapshot supersedes."}],
                      "source": {"kind": "runtime-context"}}},
        ]
        atoms, meta, _ = dsh_mod.extract_atoms(fixture, {"file": "<fixture>", "version": 1})
        kinds = [a["kind"] for a in atoms]

        rows.append({"id": "H3", "rule": "记忆库管道卫生",
                     "ok": kinds.count("plugin_injection") == 2,
                     "detail": "插件块+运行环境块须落成 plugin_injection ×2，实测 kinds=%r" % (kinds,)})
        rows.append({"id": "H4", "rule": "记忆库管道卫生",
                     "ok": kinds.count("user_text") == 1,
                     "detail": "真人消息须仍是 user_text ×1（不误杀），实测 kinds=%r" % (kinds,)})

        frags = pipeline_mod.fragments(atoms)
        leaked = [f for f in frags if any(t in f[1] for t in INJECT_TAGS)
                  or "runtime context" in f[1].lower()]
        rows.append({"id": "H5", "rule": "记忆库管道卫生", "ok": not leaked,
                     "detail": "注入块**不得**出现在可检索切片里，实测泄漏 %d 片" % len(leaked)})
        return rows

    # ---------------------------------------------------------------- H6–H7
    def check_db(self) -> list[dict]:
        """现库兜底判据（慢：全表 LIKE 扫）。"""
        import sqlite3
        db = (self.mem_root / "index" / "memory.sqlite") if self.mem_root else None
        if db is None or not db.exists():
            return [{"id": "H6", "rule": "记忆库管道卫生", "ok": None,
                     "detail": "找不到现库（%s）⇒ 不判绿（fail-closed）" % db}]
        con = sqlite3.connect(str(db))
        try:
            cond = " OR ".join(["text LIKE ?"] * len(INJECT_TAGS))
            args = tuple(t + "%" for t in INJECT_TAGS)
            n_bad = con.execute(
                "SELECT COUNT(*) FROM atoms WHERE kind='user_text' AND (%s)" % cond, args
            ).fetchone()[0]
            n_keep = con.execute(
                "SELECT COUNT(*) FROM atoms WHERE kind='plugin_injection'"
            ).fetchone()[0]
            n_human = con.execute(
                "SELECT COUNT(*) FROM atoms WHERE kind='user_text'"
            ).fetchone()[0]
        finally:
            con.close()
        return [
            {"id": "H6", "rule": "记忆库管道卫生", "ok": n_bad == 0,
             "detail": "现库 user_text 注入块须为 0，实测 **%d**（真人 user_text 共 %d 条）"
                       % (n_bad, n_human)},
            {"id": "H7", "rule": "记忆库管道卫生", "ok": n_keep > 0,
             "detail": "现库须留下 plugin_injection 供审计（挡而不丢），实测 **%d**" % n_keep},
        ]

    # ---------------------------------------------------------------- 汇总
    def check(self, with_db: bool = False) -> dict:
        rows = self.check_behavior()
        if with_db and all(r["ok"] is not None for r in rows):
            rows += self.check_db()
        bad = [r for r in rows if r["ok"] is False]
        unknown = [r for r in rows if r["ok"] is None]
        if bad:
            summary = "记忆库管道卫生：**%d 项不过** ⇒ 注入块又会被当用户原话入库" % len(bad)
        elif unknown:
            summary = "记忆库管道卫生：%d 项**未核**（fail-closed，不判绿）" % len(unknown)
        else:
            summary = "记忆库管道卫生：%d 项全过（真人 %s / 注入块挡在检索外）" % (
                len(rows), "是 user_text" if any(r["id"] == "H4" for r in rows) else "—")
        return {"ok": not bad and not unknown, "summary": summary, "rows": rows}

    def render(self, rep: dict) -> str:
        lines = [rep["summary"]]
        for r in rep["rows"]:
            mark = "✓" if r["ok"] is True else ("✗" if r["ok"] is False else "?")
            lines.append("  %s %s %s" % (mark, r["id"], r["detail"]))
        return "\n".join(lines)


def demo(cfg: ProjectConfig) -> int:
    """负例自检：不仅"能跑"，还要证明**旧写法真的会被拦下**。"""
    print("### 记忆库管道卫生闸 g_mem_hygiene 演示")
    g = HygieneGate(cfg)
    fails = 0

    print("\n### 1. 真实形态：H1–H5 逐条")
    try:
        dsh_mod, pipe_mod = g._load_memlib()
    except Exception as e:  # noqa: BLE001
        print("  ✗ 载不进抽取器：%r" % (e,))
        print("\n### 自检结果：1 项失败")
        return 1
    for r in g.check_behavior(dsh_mod, pipe_mod):
        mark = "✓" if r["ok"] is True else ("✗" if r["ok"] is False else "?")
        print("  %s %s %s" % (mark, r["id"], r["detail"]))
        if r["ok"] is not True:
            fails += 1

    print("\n### 2. 负例：把白名单改回旧行为（= 不看 source.kind）⇒ 闸必须真的拦")
    saved = dsh_mod.USER_SOURCE_KINDS
    try:
        dsh_mod.USER_SOURCE_KINDS = {"user", "plugin:dsh-memory-plugin", "runtime-context"}
        rows = g.check_behavior(dsh_mod, pipe_mod)
        h3 = [r for r in rows if r["id"] == "H3"]
        h4 = [r for r in rows if r["id"] == "H4"]
        hit = (h3 and h3[0]["ok"] is False) or (h4 and h4[0]["ok"] is False)
        print("  %s 白名单放开后 H3/H4 判定 = %s / %s"
              % ("✓✓拦下" if hit else "✗✗ 没拦住（闸是摆设）",
                 h3[0]["ok"] if h3 else "?", h4[0]["ok"] if h4 else "?"))
        if not hit:
            fails += 1
    finally:
        dsh_mod.USER_SOURCE_KINDS = saved

    print("\n### 3. 负例：把 plugin_injection 从 NO_CHUNK 拿掉 ⇒ 必须红")
    saved_nc = pipe_mod.NO_CHUNK
    try:
        pipe_mod.NO_CHUNK = set(saved_nc) - {"plugin_injection"}
        rows = g.check_behavior(dsh_mod, pipe_mod)
        h2 = [r for r in rows if r["id"] == "H2"]
        h5 = [r for r in rows if r["id"] == "H5"]
        hit = (h2 and h2[0]["ok"] is False) and (h5 and h5[0]["ok"] is False)
        print("  %s NO_CHUNK 去掉后 H2=%s / H5=%s"
              % ("✓✓拦下" if hit else "✗✗ 没拦住",
                 h2[0]["ok"] if h2 else "?", h5[0]["ok"] if h5 else "?"))
        if not hit:
            fails += 1
    finally:
        pipe_mod.NO_CHUNK = saved_nc

    print("\n### 4. 正例：恢复原状 ⇒ 全过（不误杀）")
    rows = g.check_behavior(dsh_mod, pipe_mod)
    print("  %s %s" % ("✓ 全过" if all(r["ok"] is True for r in rows) else "✗ 误杀",
                       " / ".join("%s=%s" % (r["id"], r["ok"]) for r in rows)))
    if not all(r["ok"] is True for r in rows):
        fails += 1

    print("\n### 自检结果：%s" % ("全部通过" if fails == 0 else "%d 项失败" % fails))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        print("[g_mem_hygiene] stdout 重设为 utf-8 失败（继续跑）：%r" % (e,), file=sys.stderr)
    from guard import load as load_cfg

    ap = argparse.ArgumentParser(prog="g_mem_hygiene")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--db", action="store_true", help="追加现库判据（慢）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    if a.demo:
        sys.exit(demo(cfg))
    gt = HygieneGate(cfg)
    rep = gt.check(with_db=a.db)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(gt.render(rep))
    sys.exit(0 if rep["ok"] else 1)
