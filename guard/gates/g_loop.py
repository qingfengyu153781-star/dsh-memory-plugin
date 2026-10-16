# -*- coding: utf-8 -*-
"""_guard · 熔断闸 g_loop —— **自动续跑跑到该停的时候，必须停**

## 为什么有这道闸（2026-10-04 立，从《联网学习Blender建模》会话来）

**实证时间线（该会话）**：

| 时刻 | 事件 |
|---|---|
| 10-03 19:36 | 用户敲 `/goal <学建模原文>`，`maxGoalRounds: 256` |
| 10-04 02:46 | 用户说「…」 |
| 10-04 09:16 → 11:25 | **自动续跑 turn_45~54，共 10 轮 / 2 小时 9 分，用户全程不在场** |
| 10-04 11:21 | 一次上下文压缩（用户发话前 4 分钟） |
| 10-04 11:25 | 用户说「…」 |
| 10-04 11:26 | 用户补一句「…」，随后 goal 被 pause |

**⇒ 用户 02:46 已经明确表达不满，之后 8.5 小时仍全自动跑。**
**平均每 10 分钟自动产生一轮，52 轮里 36% 被事后判定为"原地打转"。**

**这次复盘还实测到同类（就在护栏层自己身上）**：
`_guard_ledger/action_sequence.jsonl` 里 `launch@9876` **连续 4 次失败仍在重试**；
那段会话里 `img_scan` 被截断的同一份输出**连犯 6 次**；
工具警告"主线程重活建议改走 headless"**吃了 5 次、0 次改正**。

## 熔断判据（4 条，全部可从台账判定，不靠语义理解）

  **A · 同动作连败**：同一 `key` 连续失败 ≥N 次（默认 3）⇒ 熔断
     （实证：`launch@9876` 连败；`launches: 20` —— 连猜 5 轮全错）

  **B · 判据钝化**：同一判据连续 ≥N 轮输出完全相同 ⇒ 熔断
     （与 `g_criteria` 的钝化检测同源；这里用于"该不该继续自动跑"的决策）

  **C · 无新产物**：连续 ≥N 轮没有产生任何新文件 / 新模型 / 新图 ⇒ 熔断
     （实证：turn_47/49/53/54 四轮判定为原地打转）

  **D · 用户已表达不满未解决** ⇒ 禁止继续自动扩产 ★最重要的一条
     （实证：用户 02:46 说"不像现实"，之后仍自动跑 8.5 小时）

## 关键设计：**只报"该停"，不自己停**

本闸**不直接掐断运行**（那属于宿主/编排器的职权，闸门越权会出乱子）。
它做的是：**在过闸时判出"早就该熔断了"，把这件事变成可见的、必须回答的记录**。
⇒ 与 `g_criteria` 的钝化检测**分工**：本闸管"该不该继续跑"，那道闸管"判据本身还灵不灵"。

## 与"不许误杀"的平衡

  · 台账为空 / 没有自动续跑迹象 ⇒ `ok=None`（未核，不拦）
  · 单件任务（本来就不循环）⇒ 未核
  · 只有"连续失败 **同一个** 动作"才算；不同动作各失败一次不算

## 用法
```bash
python _guard/gates/g_loop.py --demo                       # 负例自检
python _guard/gates/g_loop.py --actions <action_seq.jsonl>  # 查同动作连败
python _guard/gates/g_loop.py --rounds <rounds.jsonl>       # 查无新产物/钝化
```
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402

DEFAULTS: dict = {
    "actions_ledger": "_guard_ledger/action_sequence.jsonl",
    "rounds_ledger": "_guard_ledger/rounds.jsonl",

    "max_same_fail": 3,        # A · 同动作连续失败上限
    "max_stale_rounds": 5,     # B · 钝化轮数上限
    "max_barren_rounds": 3,    # C · 连续无产物轮数上限

    # D · 用户不满信号（出现后不许继续自动扩产）
    "user_discontent_pats": [
        r"不合格", r"不像现实", r"太丑", r"不行", r"重做", r"返工",
        r"你(?:怎么|为什么)", r"不对", r"错了", r"垃圾",
    ],
    # 视为"有产物"的迹象
    "artifact_pats": [
        r"\.blend\b", r"\.png\b", r"\.md\b", r"\.json\b", r"新增", r"落盘", r"生成",
    ],
}


def _cfg_of(cfg: ProjectConfig) -> dict:
    d = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    over = cfg.get("loop_gate") or {}
    for k, v in over.items():
        if isinstance(v, list) and isinstance(d.get(k), list):
            d[k] = list(d[k]) + [x for x in v if x not in d[k]]
        else:
            d[k] = v
    return d


class LoopGate:
    """熔断闸。四判：同动作连败 / 判据钝化 / 无新产物 / 用户不满未解决。"""

    NAME = "g_loop"

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self.K = _cfg_of(cfg)
        self.actions = cfg.resolve(self.K["actions_ledger"])
        self.rounds = cfg.resolve(self.K["rounds_ledger"])

    # ---------------------------------------------------------------- 读
    @staticmethod
    def _read(p: Path) -> list[dict]:
        if not p.exists():
            return []
        out = []
        for ln in p.read_text(encoding="utf-8", errors="replace").split("\n"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out

    # ---------------------------------------------------------------- 读档案推轮
    # ★ 判据词（与 g_criteria 的 pass_pats 同源）：工具输出里这些行 = 一轮的"判据结论"
    VERDICT_PATS = [r"验收通过", r"验收合格", r"检查通过", r"校验通过",
                    r"残留\s*\*{0,2}\s*0", r"零残留", r"无残留",
                    r"达标", r"合格", r"✅\s*通过"]

    @staticmethod
    def _read_lines(p: Path) -> list[str]:
        """读会话 jsonl / .zstd 存档为行列表。

        ★ zstd 写法沿用 g_brief 修正后的 **buf 引用**模式：
          `copy_stream` 返回 (读,写) 计数元组，直接 `.getvalue()` 会 AttributeError
          （2026-10-04 实测踩过 ⇒ .zstd 存档恒读不出）。必须先留 BytesIO 引用再取值。
        """
        if not p.exists():
            return []
        if p.suffix == ".zstd":
            import zstandard as _zstd
            import io as _io
            buf = _io.BytesIO()
            with p.open("rb") as fi:
                _zstd.ZstdDecompressor().copy_stream(fi, buf)
            return buf.getvalue().decode("utf-8", "replace").split("\n")
        return p.read_text(encoding="utf-8", errors="replace").split("\n")

    def rounds_from_archive(self, session_jsonl: Path) -> list[dict]:
        """从会话存档推「每轮一行」的 rounds 台账 —— g_loop B/C 判据的**真数据源**。

        ★ 为什么需要它（2026-10-04 根治"挂名未活"）：B（判据钝化）/ C（无产物轮）
          原先依赖 `rounds.jsonl` 台账，但**没有任何写入方** ⇒ 永远「未核」。
          本方法让任何会话存档（含历史案卷）都能直接推出每轮数据：
          · 一轮 = 一个 turn（data.turn）；
          · artifacts = 该轮 write/edit 工具调用真正落盘的文件路径；
          · verdict = 该轮工具输出里**最后一条**命中判据词的行
            （案发现场物证：那句「验收通过：**N KB / N 篇**，媒体残留 **0**」
            就住在 tool/result 里，被复制了 29 次）。
        """
        import re as _re
        turns: dict[int, dict] = {}
        cur_turn: int | None = None
        # ★ 归属规则（实测档序，2026-10-04）：turn/start(N) **先于**该轮的 user/message
        #   ，而 user/message **没有
        #   data.turn 字段**（keys=content/id/role/source）⇒ 挂到 cur_turn（最近开
        #   始的轮）。注入（agent-instructions/runtime-context）的 kind != 'user' 不算。
        def _row(t: int) -> dict:
            return turns.setdefault(t, {"turn": t, "artifacts": [], "verdict": "",
                                        "verdict_all": [], "has_user": False})
        for ln in self._read_lines(session_jsonl):
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            d = o.get("data") or {}
            typ = o.get("type")
            if typ == "turn/start":
                t = d.get("turn")
                if isinstance(t, int):
                    cur_turn = t
                    _row(t)
                continue
            if typ == "user/message":
                s = d.get("source") if isinstance(d.get("source"), dict) else None
                if (s or {}).get("kind") == "user" and cur_turn is not None:
                    _row(cur_turn)["has_user"] = True
                continue
            t = d.get("turn")
            if not isinstance(t, int):
                continue
            row = _row(t)
            if typ == "tool/call":
                if (d.get("name") or "") in ("write", "edit"):
                    try:
                        args = json.loads(d.get("arguments") or "{}")
                        fp = args.get("file_path")
                        if fp:
                            row["artifacts"].append(str(fp))
                    except Exception:  # noqa: BLE001
                        pass
            elif typ in ("tool/result", "assistant/message"):
                # ★ 抽取源必须含 assistant/message（2026-10-04 案发现场实测）：
                #   那句「媒体残留 **0**」42 次里有 41 次住在 **AI 自己的回复**里
                #   （贴检查输出当进度汇报），tool/result 里只有 1 次。
                #   只扫 tool/result ⇒ 恰好漏掉主战场。
                c = (d.get("message") or {}).get("content") or d.get("content")
                if isinstance(c, list):
                    for b in c:
                        txt = (b.get("text") or "") if isinstance(b, dict) else ""
                        for line in txt.split("\n"):
                            if any(_re.search(p, line) for p in self.VERDICT_PATS):
                                line = line.strip()[:120]
                                row["verdict"] = line
                                # ★ 收**全部**命中行（不只最后一条）：一轮里常有多条
                                #   检查行混排，只留最后一条会丢掉钝化片段的出现记录。
                                if len(row["verdict_all"]) < 6:
                                    row["verdict_all"].append(line)
        return [turns[k] for k in sorted(turns)]

    # ---------------------------------------------------------------- A
    def check_same_fail(self, rows: list[dict] | None = None) -> dict:
        """A · 同一动作连续失败 ≥N 次 ⇒ 该停了，别再重试。"""
        rows = rows if rows is not None else self._read(self.actions)
        if not rows:
            return {"ok": None, "status": "skip", "detail": "动作台账为空 ⇒ 未核"}
        n = int(self.K["max_same_fail"])
        runs: dict[str, int] = {}
        for r in rows:
            key = str(r.get("key") or r.get("action") or "?")
            if r.get("ok") is False:
                runs[key] = runs.get(key, 0) + 1
            else:
                runs[key] = 0
        # ★★ 末尾连败语义（2026-10-04 自查修正，防陈年数据误杀）：
        #   原实现取**全史最大连败** ⇒ 历史上连败过 4 次、但早被一次成功打断的动作，
        #   会**永远**拦住之后的每一份报告 —— 那不是"该停没停"，是拿旧伤当现症。
        #   正确语义：**只有"现在仍处在失败循环里"（台账末尾就是 ≥N 连败）才拦**。
        #   runs[] 在扫描结束时保存的正是每个 key 的**末尾连败数**（成功会清零）。
        out = [{"key": k, "run": v} for k, v in runs.items() if v >= n]
        out.sort(key=lambda x: -x["run"])
        return {"ok": not out, "status": "checked", "hits": out,
                "detail": (f"动作台账 {len(rows)} 条；**末尾**仍连败 ≥{n} 次的动作 {len(out)} 个"
                           + (f"（最严重：{out[0]['key']} 连败 {out[0]['run']} 次）"
                              if out else "（历史连败已被成功打断的不算）"))}

    # ---------------------------------------------------------------- B/C
    def check_rounds(self, rows: list[dict] | None = None,
                     user_text: str = "") -> dict:
        """B · 钝化轮 / C · 无产物轮 / D · 用户不满未解决。"""
        rows = rows if rows is not None else self._read(self.rounds)
        problems = []
        # D · 用户不满 —— 与台账无关，只看用户文本（最重要）
        if user_text:
            for ln in user_text.split("\n"):
                for p in self.K["user_discontent_pats"]:
                    if re.search(p, ln):
                        problems.append(
                            f"★ 用户已表达不满（『{ln.strip()[:40]}』）⇒ "
                            "**禁止继续自动扩产**，必须先停下来对齐，再决定是否继续")
                        break
                else:
                    continue
                break
        if not rows:
            if problems:
                return {"ok": False, "status": "checked", "problems": problems,
                        "summary": "熔断：✗ " + problems[0][:90]}
            return {"ok": None, "status": "skip", "detail": "轮次台账为空 ⇒ 未核"}

        # C · 连续无产物（★2026-10-04 修正语义：**只在无人值守段内累计**）
        #   对照组实测误杀：本会话近几轮是纯分析（只读不写文件）⇒ 也被算"打转"。
        #   但**只读轮 ≠ 打转轮** —— 区别在用户在不在场：
        #   · 案发现场的打转 = 自动续跑段（turn 45–54，用户全程不在）内连续无产物；
        #   · 正常分析轮之间有用户发言（有方向、在推进）⇒ 不算。
        #   ⇒ 打转计数遇到 `has_user`（该轮有真人发言）即清零。
        n_barren = int(self.K["max_barren_rounds"])
        barren_run = 0
        for r in rows:
            if r.get("has_user"):
                barren_run = 0          # 用户在本轮发过话 ⇒ 有方向，打转计数清零
                continue
            blob = json.dumps(r, ensure_ascii=False)
            has_art = any(re.search(p, blob) for p in self.K["artifact_pats"])
            barren_run = 0 if has_art else barren_run + 1
            if barren_run >= n_barren:
                problems.append(
                    f"无人值守段内连续 {barren_run} 轮没有产生任何新产物"
                    "（落盘/新增/生成）⇒ 疑似原地打转，该停下来报回来")
                break

        # B · 钝化轮（★2026-10-04 二次升级：**片段级**位置对齐，案发现场实测逼出）：
        #   案发现场的判据行长这样——「知识库 **N KB**；媒体残留 **0**；旁边任务未受影响」
        #   一行里混着**工作指标**（KB 每轮在变）和**钝化结论**（残留 0 恒定）。
        #   · 整行比较 ⇒ 前缀数字在变 ⇒ 永不同形 ⇒ 漏网（第一次实测就这么漏的）；
        #   · 片段比较（按 ；切开）⇒「媒体残留 #」片段连续 40 轮出现且数字恒为 0 ⇒ 抓住。
        #   判钝化的片段 = 自身含判据词 + 连续 ≥N 轮出现 + **片段内数字无任何变化**；
        #   数字在变的片段（如「知识库 # KB」）= 判据在工作 ⇒ 不算钝化。
        n_stale = int(self.K["max_stale_rounds"])
        import re as _re2
        FRAG_PATS = list(self.VERDICT_PATS) + [r"通过"]
        frag_turns: dict[str, list] = {}      # 片段形状 -> [(turn, 数字元组)]
        for r in rows:
            lines = r.get("verdict_all") or ([r["verdict"]] if r.get("verdict") else [])
            for v in lines:
                for frag in _re2.split(r"[；;]\s*", str(v)):
                    frag = frag.strip()
                    if not frag or not any(_re2.search(p, frag) for p in FRAG_PATS):
                        continue
                    s2 = _re2.sub(r"\d+(?:\.\d+)?", "#", frag)
                    shape = _re2.sub(r"[#\s\*：:，,。/|（）()]+", "", s2)
                    if not shape:
                        continue
                    frag_turns.setdefault(shape, []).append(
                        (r.get("turn"), tuple(_re2.findall(r"\d+(?:\.\d+)?", frag))))
        for shape, entries in frag_turns.items():
            if len(entries) < n_stale:
                continue
            ts = sorted(t for t, _ in entries)
            run = 1
            for i in range(1, len(ts)):
                run = run + 1 if (ts[i] - ts[i - 1]) <= 2 else 1
                if run >= n_stale:
                    digits = [d for _, d in entries]
                    cols = [{d[c] for d in digits if c < len(d)}
                            for c in range(max((len(d) for d in digits), default=0))]
                    changed = sum(1 for col in cols if len(col) > 1)
                    if changed > 0:
                        continue      # 片段里的数字在变 ⇒ 这个判据在工作 ⇒ 不算钝化
                    problems.append(
                        f"判据片段『{shape[:40]}』连续 {run} 轮出现且数字恒定"
                        "⇒ 已钝化，继续跑不产生信息")
                    break

        ok = not problems
        return {"ok": ok, "status": "checked", "problems": problems,
                "summary": ("熔断：✓ 未发现该停未停" if ok
                            else "熔断：✗ " + problems[0][:110])}


def demo(cfg: ProjectConfig | None = None) -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    g = LoopGate.__new__(LoopGate)
    g.K = dict(DEFAULTS)
    fail = 0

    print("=" * 74)
    print("g_loop 负例自检 —— 用《联网学习Blender建模》会话与护栏层的真实记录")
    print("=" * 74)

    print("\n### 负例 1：『launch@9876』末尾仍连败 4 次（现在就在失败循环里）")
    rows = [{"key": "launch@9876", "ok": True}] + [{"key": "launch@9876", "ok": False}] * 4
    r = g.check_same_fail(rows)
    print(f"  判定：ok={r['ok']}  {r['detail']}")
    for h in r.get("hits", []):
        print(f"    ✗ 动作 {h['key']} 连续失败 {h['run']} 次")
    if r["ok"] is False:
        print("  ✓✓ 拦住（连败不该无限重试）")
    else:
        print("  ✗✗ 没拦住"); fail += 1

    print("\n### 正例 0：连败已被成功打断（历史旧伤不算现症）⇒ 不拦")
    rows0 = [{"key": "launch@9876", "ok": False}] * 4 + [{"key": "launch@9876", "ok": True}]
    r0 = g.check_same_fail(rows0)
    print(f"  台账：F F F F T（连败 4 次后成功了一次 ⇒ 循环已解）")
    print(f"  判定：ok={r0['ok']}  {r0['detail']}")
    if r0["ok"] is True:
        print("  ✓ 放行（不许拿陈年连败永远拦后续报告）")
    else:
        print("  ✗✗ 误杀 —— 历史连败被成功打断后仍拦 ⇒ 陈年数据误杀"); fail += 1

    print("\n### 负例 2：用户说了『你这椅子不像现实』之后仍在自动跑")
    r2 = g.check_rounds([], user_text="你这椅子不像现实的")
    print(f"  判定：ok={r2['ok']}")
    for p in r2.get("problems", []):
        print(f"    ✗ {p[:100]}")
    if r2["ok"] is False:
        print("  ✓✓ 拦住（用户不满后不许继续自动扩产）")
    else:
        print("  ✗✗ 没拦住 —— 最重要的一条失效"); fail += 1

    print("\n### 负例 3：连续 3 轮无新产物（原地打转）")
    rows3 = [{"turn": i, "note": "又查了一次路灯尺寸，仍未找到"} for i in range(1, 5)]
    r3 = g.check_rounds(rows3)
    print(f"  判定：ok={r3['ok']}")
    for p in r3.get("problems", []):
        print(f"    ✗ {p[:100]}")
    if r3["ok"] is False:
        print("  ✓✓ 拦住（原地打转该报回来）")
    else:
        print("  ✗✗ 没拦住"); fail += 1

    print("\n### 负例 4：判据连续 5 轮输出完全相同")
    rows4 = [{"turn": i, "verdict": "媒体残留 0", "artifact": "新增 x.md"}
             for i in range(1, 7)]
    r4 = g.check_rounds(rows4)
    print(f"  判定：ok={r4['ok']}")
    for p in r4.get("problems", []):
        print(f"    ✗ {p[:100]}")
    if r4["ok"] is False:
        print("  ✓✓ 拦住（钝化 = 继续跑不产生信息）")
    else:
        print("  ✗✗ 没拦住"); fail += 1

    print("\n### 正例 1：正常推进（有产物、判据在变）⇒ 放行")
    rows5 = [{"turn": i, "verdict": f"面数 {40+i} 个通过", "artifact": f"新增 m{i}.blend"}
             for i in range(1, 7)]
    r5 = g.check_rounds(rows5)
    print(f"  判定：ok={r5['ok']}  {r5.get('summary', '')[:70]}")
    if r5["ok"] is True:
        print("  ✓ 放行（该跑就跑，不误杀）")
    else:
        print("  ✗✗ 误杀 —— 正常推进被判熔断"); fail += 1

    print("\n### 正例 2：台账为空 ⇒ 未核")
    r6 = g.check_same_fail([])
    print(f"  判定：ok={r6['ok']}（None=未核）  {r6['detail'][:50]}")
    if r6["ok"] is None:
        print("  ✓ 未核，不拦")
    else:
        print("  ✗✗ 误杀"); fail += 1

    print("\n### 正例 3：不同动作各失败一次 ⇒ 不算连败")
    rows7 = [{"key": "a", "ok": False}, {"key": "b", "ok": False},
             {"key": "c", "ok": False}]
    r7 = g.check_same_fail(rows7)
    print(f"  判定：ok={r7['ok']}  {r7['detail']}")
    if r7["ok"] is True:
        print("  ✓ 放行（不是同一动作反复失败）")
    else:
        print("  ✗✗ 误杀"); fail += 1

    print("\n### 正例 4：只读分析轮，但每轮有用户发言 ⇒ 不算打转（C 判据修正的正例）")
    rows8 = [{"turn": i, "has_user": True, "note": "纯分析，无新文件"} for i in range(1, 5)]
    r8 = g.check_rounds(rows8)
    print(f"  判定：ok={r8['ok']}  {r8.get('summary', '')[:60]}")
    if r8["ok"] is True:
        print("  ✓ 放行（用户在场 = 有方向；只读 ≠ 打转）")
    else:
        print("  ✗✗ 误杀 —— 有用户发言的只读轮被判打转"); fail += 1

    print("\n### 负例 5：无人值守段内连续无产物 ⇒ 该拦（C 判据的案发现场形态）")
    rows9 = [{"turn": i, "has_user": False, "note": "自动续跑，无新文件"} for i in range(1, 5)]
    r9 = g.check_rounds(rows9)
    print(f"  判定：ok={r9['ok']}")
    if r9["ok"] is False:
        print("  ✓✓ 拦住（无人值守 + 零产物 = 原地打转）")
    else:
        print("  ✗✗ 没拦住"); fail += 1

    print("\n" + "=" * 74)
    print("自检结果：" + ("全部通过" if fail == 0 else f"有 {fail} 项失败"))
    print("=" * 74)
    return 1 if fail else 0


def _utf8_stdio() -> None:
    """GBK 终端防崩（同 g_criteria：直连必崩 / 管道静默丢符号，统一包 UTF-8）。

    ★ 用 reconfigure 而不是再包一层 TextIOWrapper：原地改、幂等 ——
      若这里包新 wrapper，demo() 里再包一层时，旧 wrapper 被 GC 会**关掉底层
      buffer**，随后打印全崩（ValueError: I/O operation on closed file，
      2026-10-04 实测踩过）。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(prog="g_loop")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--actions")
    ap.add_argument("--rounds")
    ap.add_argument("--user-text")
    ap.add_argument("--session", help="会话存档（.jsonl/.zstd）⇒ 推每轮 rounds 再查 B/C")
    a = ap.parse_args()
    if a.demo:
        return demo()
    from guard import load as load_cfg
    g = LoopGate(load_cfg(a.project, a.project_root))
    rc = 0
    if a.session:
        rows = g.rounds_from_archive(Path(a.session))
        r = g.check_rounds(rows)
        print(f"档案推轮：{len(rows)} 轮（来自 {Path(a.session).name}）")
        print(r.get("summary") or r.get("detail"))
        for p in r.get("problems", []):
            print("  ✗ " + p)
        rc = 0 if r["ok"] is not False else 1
    elif a.actions:
        r = g.check_same_fail(g._read(Path(a.actions)))
        print("同动作连败：" + r["detail"])
        for h in r.get("hits", []):
            print(f"  ✗ {h['key']} 连败 {h['run']} 次")
        rc = 0 if r["ok"] is not False else 1
    elif a.rounds or a.user_text:
        rows = g._read(Path(a.rounds)) if a.rounds else []
        ut = Path(a.user_text).read_text(encoding="utf-8", errors="replace") if a.user_text else ""
        r = g.check_rounds(rows, ut)
        print(r.get("summary") or r.get("detail"))
        for p in r.get("problems", []):
            print("  ✗ " + p)
        rc = 0 if r["ok"] is not False else 1
    else:
        print("用法：--demo / --actions <jsonl> / --rounds <jsonl> [--user-text <文件>]")
    return rc


if __name__ == "__main__":
    sys.exit(main())
