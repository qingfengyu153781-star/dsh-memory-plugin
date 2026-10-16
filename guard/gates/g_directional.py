# -*- coding: utf-8 -*-
"""_guard · 方向性偏差闸 g_directional —— 检索有没有系统性丢掉「矛盾的另一侧」（R-65）

## 判据来源（学自 hive-memory-bench，署名）

hive-memory-bench（符荣峻，CC BY 4.0 / 代码 MIT）的公开结论之一：
> **压缩丢的不是随机信息，是「对面那一块」**——抽样按"与提问的相似度"选段，
> 而矛盾的另一侧恰恰与提问词面不重叠（提问说「反对权威」，另一侧写的是
> 「唯有强大的王者…才是自由的前提」）⇒ **压缩系统性地丢掉"对面那一块"**。
> 依据：[`docs/压缩的代价就是因果丢失_v1.0.md`]，工具 `systems/压缩代价.py`（可跑在自建语料上）。

**为什么本仓需要它**：我们的检索也是"按词面相似度选段"（FTS/chunk 打分），
所以**同一个偏差我们一样会犯**；而本工作区的病根之一正是「**检索只带回支持自己的那半**」，
随后报告就写成"已根治"（2026-10-15 我本人犯过：断言"删除=去重、内容没丢"，
实测 371 条无副本 ⇒ 对面那一块根本没进检索）。

## 判据（三态，不许只报覆盖率）

| id | 判据 | 量的对象 |
|---|---|---|
| D1 | 给定「矛盾对」（a＝题面侧 / b＝**对面那一侧**），**两侧存活率**各是多少 | 检索片段 × 逐字引文 |
| D2 | **方向性偏差**：`存活率(b) < 存活率(a) − tol` ⇒ **红**（说明选段有方向性偏差） | 两个存活率之差 |
| D3 | 任一侧 "存活" 必须**逐字**（归一化后子串），不许按关键词/近义算 | 逐字引文 |

## 用法

    python _guard/gates/g_directional.py --pairs pairs.json --retrieval 检索片段目录/文件 [--tol 0.2] [--json out.json]
    python _guard/gates/g_directional.py --selftest        # 负例自证：只保 a 侧必须红

`pairs.json` 两种写法：`[{"qid":"q1","a":"…逐字…","b":"…逐字…"}]` 或 `{"q1":{"a":"…","b":"…"}}`；
`--retrieval` 目录时每题一个文件（与 qid 同名），文件形如 `{"hits":[{"text":…}]}` 或纯文本。

退出码：0＝无方向性偏差；1＝检出偏差（或用法/输入错误）。
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402

ZW = ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff")


def norm(s: str) -> str:
    """归一化：去零宽字符 + 压掉所有空白。**两侧同口径**，否则存活率不可比。"""
    s = str(s or "")
    for z in ZW:
        s = s.replace(z, "")
    return re.sub(r"\s+", "", s)


def load_pairs(p: str) -> dict:
    d = json.loads(Path(p).read_text(encoding="utf-8"))
    if isinstance(d, list):
        return {x["qid"]: {"a": x.get("a"), "b": x.get("b") or x.get("far")} for x in d}
    return {k: {"a": v.get("a"), "b": v.get("b") or v.get("far")} for k, v in d.items()}


def load_retrieval(root: str) -> dict:
    """返回 {qid/文件名: 归一化片段文本}；单文件则键为 `_all`。"""
    out = {}
    p = Path(root)
    files = sorted(p.glob("**/*")) if p.is_dir() else [p]
    for f in files:
        if not f.is_file():
            continue
        t = f.read_text(encoding="utf-8", errors="replace")
        try:
            d = json.loads(t)
            hits = d.get("hits") if isinstance(d, dict) else d
            t = "".join((h.get("text") if isinstance(h, dict) else str(h)) or "" for h in hits or [])
        except Exception:  # noqa: BLE001
            pass
        out[f.stem] = norm(t)
    if p.is_file():
        out["_all"] = out.pop(p.stem)
    return out


class DirectionalGate:
    NAME = "g_directional"

    def __init__(self, cfg: ProjectConfig | None = None):
        self.cfg = cfg

    def check(self, pairs: dict, retrieval: dict, tol: float = 0.2) -> dict:
        rows, a_hit, b_hit, n = [], 0, 0, 0
        for qid, pr in pairs.items():
            ctx = retrieval.get(qid) or retrieval.get("_all") or ""
            a, b = norm(pr.get("a")), norm(pr.get("b"))
            if not a or not b:
                rows.append({"qid": qid, "ok": None, "why": "对里缺 a 或 b"})
                continue
            n += 1
            oa, ob = a in ctx, b in ctx          # D3：**逐字**，不做关键词/近义
            a_hit += 1 if oa else 0
            b_hit += 1 if ob else 0
            rows.append({"qid": qid, "a": oa, "b": ob,
                         "verdict": "两侧都在" if (oa and ob) else ("只保 a 侧（对面丢了）" if oa
                                                                    else ("只保 b 侧" if ob else "两侧都不在"))})
        if not n:
            return {"ok": False, "rows": rows, "summary": "方向性闸：**没有可判的对**（pairs 为空）"}
        # ★ 自检抓出的假通过（2026-10-16）：**两侧都不在**时 bias=0 ⇒ 原实现判绿。
        #   但"一对都没验上"根本不是"没有方向性偏差"——那是**什么都没验**。
        #   同族教训：0 样本 / 全 SKIP 不得算通过（hive 的腿 9 与我们的 g_integ 都栽过）。
        unverified = sum(1 for r in rows if r.get("verdict") == "两侧都不在")
        ra, rb = a_hit / n, b_hit / n
        bias = ra - rb
        if unverified == n:
            return {"ok": False, "rows": rows, "n": n, "unverified": unverified,
                    "alive_a": ra, "alive_b": rb, "bias": bias, "tol": tol,
                    "summary": ("方向性闸：对 %d 组，**没有任何一对被真验**（a/b 两侧都不在检索片段里）"
                                "⇒ 判红：这不是「无偏差」，是「没验」" % n)}
        ok = bias <= tol
        extra = ("；另有 %d 组**两侧都没验上**（已计入红/绿之外的读数，需人工看）" % unverified
                 if unverified else "")
        summary = ("方向性闸：对 %d 组 ｜ a 侧存活 %.0f%% / **b 侧（对面那一侧）存活 %.0f%%** "
                   "｜ 偏差 %.0fpp（容差 %.0fpp）⇒ %s%s"
                   % (n, ra * 100, rb * 100, bias * 100, tol * 100,
                      "绿" if ok else "**红：检索有方向性偏差**", extra))
        return {"ok": ok, "rows": rows, "n": n, "unverified": unverified,
                "alive_a": ra, "alive_b": rb, "bias": bias, "tol": tol, "summary": summary}

    def render(self, rep: dict) -> str:
        lines = [rep["summary"]]
        for r in rep.get("rows") or []:
            if r.get("verdict"):
                lines.append("  · %s：%s" % (r["qid"], r["verdict"]))
            else:
                lines.append("  · %s：%s" % (r["qid"], r.get("why")))
        return "\n".join(lines)


# ====================================================================== demo
def demo(cfg: ProjectConfig | None = None) -> int:
    """负例自证：**只保 a 侧**必须红；两侧都保必须绿；两侧都不在也要红。"""
    print("### 方向性偏差闸 g_directional 演示")
    g = DirectionalGate(cfg)
    fails = 0
    pairs = {"q1": {"a": "唯有人民才是历史的创造者", "b": "唯有强大的王者才是自由的前提"}}
    cases = [
        ("只保 a 侧（对面丢了）", {"q1": norm("……唯有人民才是历史的创造者……")}, False),
        ("两侧都在", {"q1": norm("唯有人民才是历史的创造者 ／ 唯有强大的王者才是自由的前提")}, True),
        ("两侧都不在", {"q1": norm("毫不相关的一段话")}, False),
        ("b 侧改写（不算逐字）", {"q1": norm("唯有人民才是历史的创造者 ／ 强大的王者带来自由")}, False),
    ]
    for name, ret, want_ok in cases:
        rep = g.check(pairs, ret, tol=0.2)
        good = rep["ok"] == want_ok
        print("  %s %-18s ⇒ %s" % ("✓" if good else "✗✗", name, rep["summary"]))
        if not good:
            fails += 1
    print("\n### 自检结果：%s" % ("全部通过（只保一侧/两侧都不在/改写冒充 都被判红）"
                                if fails == 0 else "%d 项失败" % fails))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        print("[g_directional] stdout 重设为 utf-8 失败（继续跑）：%r" % (e,), file=sys.stderr)
    from guard import load as load_project

    ap = argparse.ArgumentParser(prog="g_directional")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--pairs", help="矛盾对 JSON（a=题面侧，b=对面那一侧）")
    ap.add_argument("--retrieval", help="检索片段：目录（每题一文件）或单文件")
    ap.add_argument("--tol", type=float, default=0.2, help="可容忍的方向性偏差（默认 0.2）")
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--selftest", "--demo", action="store_true", dest="selftest",
                    help="负例自检（--demo 为别名：rules/电池 按 --demo 调）")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(demo(load_project(a.project, a.project_root)))
    if not (a.pairs and a.retrieval):
        print("需要 --pairs 与 --retrieval（或用 --selftest）")
        sys.exit(1)
    rep = DirectionalGate(load_project(a.project, a.project_root)).check(
        load_pairs(a.pairs), load_retrieval(a.retrieval), tol=a.tol)
    print(DirectionalGate().render(rep))
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    sys.exit(0 if rep["ok"] else 1)
