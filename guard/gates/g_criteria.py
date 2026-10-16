# -*- coding: utf-8 -*-
"""_guard · 判据闸 g_criteria —— **判据必须量对对象，且不许钝化**

## 为什么有这道闸（2026-10-04 立，从《联网学习Blender建模》会话来）

**那段会话赔掉的东西**：15 小时 50 分 / 3004 万 input token / 55 轮，
用户最后一句话是「…」。而在这 15 小时里，**验收判据一直是绿的**。

### 病灶原文

```python
BASE  = (ROOT/_tmp_video_learn, ROOT/Blender建模知识库)   # ← 只扫这两个目录
MEDIA = ('*.mp4','*.mkv','*.flv','*.webm','*.avi','*.mov',
         '*.m4a','*.wav','*.mp3','*.aac','*.part','*.ts')  # ← 只认这些后缀
```

用户说的是「…」
—— 要的是「**别占空间**」。
判据量的是「**这两个目录里有没有 mp4 后缀**」。

### 后果（2026-10-04 实测）

| 判据说的 | 磁盘真实 |
|---|---|
| 「媒体残留 **0** ✅」（连续 **29** 轮打印同一句话） | `_tmp_video_learn` = **907 文件 / 72.2 MB**（比知识库本体 28.6 MB 还大 2.5 倍） |

**两者都对，互不矛盾** —— 因为判据量的不是用户要的那个量。
**这就是「…」：绿灯是真的，只是它答的不是用户问的问题。**

### 为什么必须单独成闸（它跟已有闸都不是一回事）

| 已有闸 | 治什么 | 治不了本病的理由 |
|---|---|---|
| `g_brief` | 方向对不对（要求锚 / 未兑现） | 它管"有没有对账"，不管"对账用的尺子对不对" |
| `g_probe` | 探针自证（正例绿+负例红） | 它要求"探针会红"，但**不检查探针量的是不是目标量** |
| `g_root` | 病根归纳（量具要有身份证） | 是**报告措辞层**软闸，抓不到"脚本里写死的判据" |

**⇒ 本闸补的是：判据（尺子）与实际要保证的量（对象）之间的对齐。**

## 三层判据（可执行，全部不靠语义理解）

  **A · 量纲对齐**：报告里每处「通过 / 达标 / 验收 OK / 残留 0」，
     必须能配上 ① 对应哪条用户要求（BRIEF 编号或原话片段）
              ② 量的什么量（字节 / 个数 / 比例 / 米）
              ③ 扫描范围（根路径 / 全盘）
     三缺一 ⇒ 拦（"没声明范围的绿"等于没量）

  **B · 钝化检测** ★本闸最重要的判据：
     同一判据**连续 ≥N 轮输出完全相同**（默认 N=5）⇒ 判「**判据已钝化**」。
     理由：**一个永远为真的判据不产生信息**。
     实测物证：那段会话里 `验收通过：**273.3 KB / 18 篇**，媒体残留 **0**。`
     这句话被复制了 29 次，唯一变化的是篇数与字节数 —— **"媒体残留 0" 一动不动**。

  **C · 口径一致**：同一份报告里，同一个量出现两个不同数值/范围 ⇒ 拦
     （治"自己在 A 处说全盘扫过、B 处其实只扫了两个目录"）

## 与"不许误杀"的平衡（同样重要）

**未核 ≠ 不通过**（本项目管理铁律，[库 P152 同族]）：
  · 报告里**没有**任何"通过/达标"字样 ⇒ 本闸**无对象** ⇒ `ok=None`（未核，不拦）
  · 不是交付轮、没跑验收 ⇒ 同样未核
⇒ **只有"宣称通过却给不出量纲/范围"或"判据钝化"才判红。**

## 用法
```bash
python _guard/gates/g_criteria.py --demo                      # 负例自检（用真实那件事）
python _guard/gates/g_criteria.py --text-file <报告>           # 查报告里的判据声明
python _guard/gates/g_criteria.py --ledger <台账.jsonl> --check-stale
python _guard/gates/g_criteria.py --record "媒体残留=0|scope=<WORKSPACE>|unit=个"
```

跨项目：阈值与触发词可在 `_guard/projects/<名>.yaml` 的 `criteria_gate` 段覆盖。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402

DEFAULTS: dict = {
    "ledger": "_guard_ledger/criteria.jsonl",

    # ── A · 触发词：哪句话算"在宣称验收通过" ──────────────────────
    "pass_pats": [
        r"验收通过", r"验收合格", r"检查通过", r"校验通过", r"已通过验收",
        r"残留\s*\*{0,2}\s*0", r"零残留", r"无残留",
        r"达标", r"合格", r"全部通过", r"均通过", r"✅\s*通过",
    ],

    # ── A · 三要素：声明里必须出现的东西 ──────────────────────────
    # ① 量的什么量（单位）
    "unit_pats": [
        r"\d+(?:\.\d+)?\s*(?:MB|KB|GB|B)\b", r"\d+\s*(?:个|条|处|项|篇|张|帧|对)",
        r"\d+(?:\.\d+)?\s*%", r"\d+(?:\.\d+)?\s*(?:mm|cm|m|米|毫米)\b",
        r"pair_count", r"IoU", r"非流形", r"面数", r"三角面",
    ],
    # ② 扫描范围（查了哪里）
    "scope_pats": [
        r"全盘", r"全工作区", r"整个工作区", r"[A-Z]:[\\/]", r"_tmp_\w+",
        r"find\s+", r"os\.walk", r"rglob", r"glob\(", r"扫描(?:了|范围)",
        r"du\s+-sh", r"目录\s*[:：]",
    ],
    # ③ 对应用户要求（哪条 BRIEF / 哪句原话）
    "anchor_pats": [
        r"\bR\d+\b", r"\bA\d+\b", r"BRIEF", r"要求锚",
        r"用户(?:原话|要求)", r"对应要求",
    ],

    # ── B · 钝化检测 ─────────────────────────────────────────────
    "stale_n": 5,               # 同一判据连续 N 轮完全相同 ⇒ 判钝化
    # ★ 归一化开关：**默认关**（2026-10-04 自检抓出误杀后定的）
    #   曾默认 True（抹掉数字再比），结果把「面数 40 / 41 / 42 …」判成钝化 ——
    #   而那正是"判据在工作"的样子。**数值在变 = 判据有信息**，不该判钝化。
    #   ⇒ 正确语义：钝化 = **整行文字一字不差地重复**（含数字）。
    "normalize_digits": False,
}


def _cfg_of(cfg: ProjectConfig) -> dict:
    d = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    over = cfg.get("criteria_gate") or {}
    for k, v in over.items():
        if isinstance(v, list) and isinstance(d.get(k), list):
            d[k] = list(d[k]) + [x for x in v if x not in d[k]]
        else:
            d[k] = v
    return d


class CriteriaError(RuntimeError):
    """判据不合格 —— 不许拿它宣布通过。"""


class CriteriaGate:
    """判据闸。三查：量纲对齐（A）→ 钝化（B）→ 口径一致（C）。"""

    NAME = "g_criteria"

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self.K = _cfg_of(cfg)
        self.ledger = cfg.resolve(self.K["ledger"])
        self.ledger.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 工具
    @staticmethod
    def _norm(line: str, drop_digits: bool) -> str:
        """把一行判据归一化成"形状"，用于比较是否雷同。

        ★ 为什么要把数字抹掉：
          实测那句 `验收通过：**273.3 KB / 18 篇**，媒体残留 **0**。`
          每轮**字节数和篇数都在变**，只有"媒体残留 0"一动不动。
          若不做归一化，就会因为"数字变了"而判成"没钝化" —— 正好漏掉真正的钝化。
        """
        s = line.strip()
        if drop_digits:
            s = re.sub(r"\d+(?:\.\d+)?", "#", s)
        s = re.sub(r"\s+", " ", s)
        return s

    def _lines_with(self, text: str, pats: list[str]) -> list[str]:
        out = []
        for ln in text.split("\n"):
            for p in pats:
                if re.search(p, ln, re.I):
                    out.append(ln)
                    break
        return out

    # ---------------------------------------------------------------- A
    def check_dimension(self, text: str) -> dict:
        """A · 量纲对齐：宣称通过 ⇒ 必须给 ① 单位 ② 范围 ③ 要求锚。"""
        claims = self._lines_with(text, self.K["pass_pats"])
        if not claims:
            # ★ 未核，不是不通过（不许误杀"这轮没做验收"）
            return {"ok": None, "status": "skip", "n_claim": 0, "bad": [],
                    "detail": "报告里没有『通过/达标』类声称 ⇒ 本闸无对象（未核）"}
        bad = []
        for c in claims:
            has_unit = any(re.search(p, c, re.I) for p in self.K["unit_pats"])
            has_scope = any(re.search(p, c, re.I) for p in self.K["scope_pats"])
            has_anchor = any(re.search(p, c, re.I) for p in self.K["anchor_pats"])
            miss = [n for n, v in (("单位", has_unit), ("范围", has_scope),
                                   ("要求锚", has_anchor)) if not v]
            if len(miss) >= 2:          # 缺两项以上才算"说不清"（缺一项可从上下文补）
                bad.append({"line": c[:110], "missing": miss})
        detail = (f"宣称通过 {len(claims)} 处，其中 {len(bad)} 处说不出"
                 "『量的什么+范围多大+对应用户哪条要求』")
        return {"ok": not bad, "status": "checked", "n_claim": len(claims),
                "bad": bad, "detail": detail}

    # ---------------------------------------------------------------- B
    def check_stale(self, entries: list[dict] | None = None) -> dict:
        """B · 钝化检测：同一判据连续 N 轮完全相同 ⇒ 判「已钝化」。

        ★ 本闸最重要的一条。一个**永远为真**的判据**不产生信息** ——
          它从"检查"退化成"仪式"。
        """
        rows = entries if entries is not None else self.read_ledger()
        if not rows:
            return {"ok": None, "status": "skip", "detail": "台账为空 ⇒ 未核"}
        n = int(self.K["stale_n"])
        drop = bool(self.K["normalize_digits"])
        # 按"判据名"分组，看每组的归一化形状是否连续雷同
        groups: dict[str, list[str]] = {}
        for r in rows:
            key = (r.get("name") or "?").strip()
            groups.setdefault(key, []).append(self._norm(str(r.get("line") or ""), drop))
        stale = []
        for key, seq in groups.items():
            if len(seq) < n:
                continue
            run = 1
            for i in range(1, len(seq)):
                run = run + 1 if seq[i] == seq[i - 1] else 1
                if run >= n:
                    stale.append({"name": key, "run": run, "line": seq[i][:100]})
                    break
        # ★ 第二条通路：**结论词恒定**检测（治"数字在变、结论一动不动"）
        #   实测物证：`验收通过：**273.3 KB / 18 篇**，媒体残留 **0**。`
        #   29 轮里字节数/篇数每轮都变（所以整行比较抓不到），
        #   但**结论部分"媒体残留 0"一字不变** —— 那才是钝化的实质。
        verdicts: dict[str, list[str]] = {}
        for r in rows:
            key = (r.get("name") or "?").strip()
            v = self._verdict(str(r.get("line") or ""))
            if v:
                verdicts.setdefault(key, []).append(v)
        for key, seq in verdicts.items():
            if len(seq) < n:
                continue
            # ★ 排除"数值真的在动"的判据：那正是**判据在工作**的样子，不是钝化。
            #   实测误杀（自检抓出）：`面数 40 / 41 / 42 …` 结论形状同，
            #   但数值单调变化 ⇒ 必须放行。
            raw = [str(r.get("line") or "") for r in rows
                   if (r.get("name") or "?").strip() == key]
            if self._varying(raw):
                continue
            run = 1
            for i in range(1, len(seq)):
                run = run + 1 if seq[i] == seq[i - 1] else 1
                if run >= n:
                    if not any(s["name"] == key for s in stale):
                        stale.append({"name": key, "run": run,
                                      "line": f"结论恒定：{seq[i][:80]}"})
                    break
        detail = (f"台账 {len(rows)} 条 / {len(groups)} 个判据名；"
                 f"连续 ≥{n} 次完全相同的判据 {len(stale)} 个")
        return {"ok": not stale, "status": "checked", "stale": stale, "detail": detail}

    @staticmethod
    def _varying(lines: list[str]) -> bool:
        """这一组判据的**数值是否真的在动**。

        ★ 判据（走对了三轮才对，记下来防再犯）：
          · **整行**数字在变 ⇒ 判据在工作 ⇒ 不判钝化
            （`面数检查：40 个，通过` → `41 个` → `42 个`）
          · **整行**数字恒定，而**结论形状**雷同 ⇒ 钝化
            （`…**200.3 KB**…媒体残留 0` → `…**210.3 KB**…媒体残留 0`：
              注意这行**整行数字也在变**，所以不能只看整行！）

        ⇒ 两者冲突，唯一的解法是**看"变化的是不是判据本身关注的那个量"**。
          这里采用可判定的近似：**把数字按位置对齐后，若存在某一位置的数字恒定
          而其他位置在变 ⇒ 那个恒定的数就是"结论值"** ⇒ 判钝化（交回 False）。
          若**所有**位置的数字都在变 ⇒ 判据在工作（返回 True）。
        """
        seqs = [re.findall(r"\d+(?:\.\d+)?", ln) for ln in lines]
        seqs = [s for s in seqs if s]
        if len(seqs) < 2:
            return False
        width = max(len(s) for s in seqs)
        # 逐位置看：哪些位置的数字在整组内发生了变化
        changed = 0
        stable = 0
        for i in range(width):
            col = {s[i] for s in seqs if i < len(s)}
            if len(col) > 1:
                changed += 1
            elif col:
                stable += 1
        # 全部位置都在变 ⇒ 判据在工作
        # 有位置恒定（且至少有位置在变）⇒ 那个恒定量就是"没动过的结论" ⇒ 钝化
        return changed > 0 and stable == 0

    @staticmethod
    def _verdict(line: str) -> str:
        """抽出判据的**结论部分**（去掉数字与单位），用于"结论是否恒定"检测。

        `验收通过：**273.3 KB / 18 篇**，媒体残留 **0**。`
          ⇒ 抹掉数字/单位后 ⇒ `验收通过：# KB / # 篇，媒体残留 #。`
        **连续 N 轮这个形状不变 ⇒ 结论没动过 ⇒ 钝化。**
        而 `面数 40 个 / 41 个 / 42 个` 抹掉后虽然也同形，
        但它的**数值在单调变化** ⇒ 由 `_varying()` 排除，不判钝化。
        """
        s = re.sub(r"\d+(?:\.\d+)?", "#", line)
        s = re.sub(r"[#\s\*：:，,。/|]+", "", s)
        return s

    # ---------------------------------------------------------------- C
    def check_consistency(self, text: str) -> dict:
        """C · 口径一致：同一个量在同一份报告里给了两个范围 ⇒ 拦。"""
        scopes = self._lines_with(text, self.K["scope_pats"])
        if len(scopes) < 2:
            return {"ok": None, "status": "skip", "detail": "范围声明 <2 处 ⇒ 未核"}
        # 抽出所有"路径形态"的范围，看是否互不包含（可疑的窄化）
        roots = set()
        for ln in scopes:
            for m in re.finditer(r"[A-Z]:[\\/][^\s，。；,;）)]*", ln):
                roots.add(m.group(0).rstrip("\\/"))
        nested_conflict = []
        for a in roots:
            for b in roots:
                if a != b and a.startswith(b) and len(b) < 12:
                    nested_conflict.append((a, b))
        detail = (f"范围声明 {len(scopes)} 处，抽出根路径 {len(roots)} 个"
                 + (f"，其中 {len(nested_conflict)} 对存在'窄化'嫌疑" if nested_conflict else ""))
        return {"ok": not nested_conflict, "status": "checked",
                "roots": sorted(roots), "conflicts": nested_conflict, "detail": detail}

    # ---------------------------------------------------------------- 汇总
    def check_report(self, text: str) -> dict:
        a = self.check_dimension(text)
        # B 只在调用方要求时查（它依赖台账，不是文本属性）
        c = self.check_consistency(text)

        def _pass(v):
            return v is not False          # None（未核）不参与总判定

        ok = _pass(a["ok"]) and _pass(c["ok"])
        tri = ("核过" if a["ok"] is True else ("拦" if a["ok"] is False else "未核"))
        mark = {"核过": "✓", "拦": "✗", "未核": "○"}[tri]
        parts = [f"A {mark} {a['detail'][:70]}", f"C {('✓' if c['ok'] else '○')} {c['detail'][:60]}"]
        return {"gate": self.NAME, "ok": ok, "a": a, "c": c,
                "summary": "判据量纲： " + "｜".join(parts)}

    # ---------------------------------------------------------------- 台账
    def read_ledger(self) -> list[dict]:
        if not self.ledger.exists():
            return []
        out = []
        for ln in self.ledger.read_text(encoding="utf-8", errors="replace").split("\n"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out

    def record(self, name: str, line: str, **kw) -> dict:
        rec = {"name": name, "line": line, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        rec.update(kw)
        with self.ledger.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec


# -------------------------------------------------------------------- demo
def demo(cfg: ProjectConfig | None = None) -> int:
    """负例自检：用《联网学习Blender建模》会话里那件真事。"""
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    cfg = cfg or ProjectConfig.__new__(ProjectConfig)
    try:
        cfg.data = {}
        cfg.root = Path(r"<WORKSPACE>")
    except Exception:  # noqa: BLE001
        pass
    g = CriteriaGate.__new__(CriteriaGate)
    g.K = dict(DEFAULTS)
    g.ledger = Path(r"<WORKSPACE>/_guard_ledger/criteria.jsonl")

    print("=" * 74)
    print("g_criteria 负例自检 —— 用《联网学习Blender建模》会话的真实判据")
    print("=" * 74)

    fail = 0

    print("\n### 负例 1：那句被复制了 29 次的『媒体残留 0』")
    real = "验收通过：**273.3 KB / 18 篇**，媒体残留 **0**。"
    r = g.check_dimension(real)
    print(f"  原文：{real}")
    print(f"  判定：ok={r['ok']}  {r['detail']}")
    for b in r["bad"]:
        print(f"    ✗ 缺 {b['missing']}：{b['line'][:70]}")
    if r["ok"] is False:
        print("  ✓✓ 拦住（说不出量的什么/范围多大/对应哪条要求）")
    else:
        print("  ✗✗ 没拦住 —— 闸失效"); fail += 1

    print("\n### 负例 2：钝化检测 —— 同一判据连续 6 轮雷同")
    rows = [{"name": "媒体残留",
             "line": f"验收通过：**{200+i*10}.3 KB / 19 篇**，媒体残留 **0**。"}
            for i in range(6)]
    rs = g.check_stale(rows)
    print(f"  台账 6 条（数字每轮都变，只有『媒体残留 0』不动）")
    print(f"  判定：ok={rs['ok']}  {rs['detail']}")
    for s in rs["stale"]:
        print(f"    ✗ 判据『{s['name']}』连续 {s['run']} 次完全相同 ⇒ 已钝化")
    if rs["ok"] is False:
        print("  ✓✓ 拦住（永远为真的判据不产生信息）")
    else:
        print("  ✗✗ 没拦住 —— 钝化检测失效"); fail += 1

    print("\n### 正例 1：合规的判据声明（单位+范围+要求锚齐全）")
    good = "R4 达标：对 D:\\agent 全盘 du -sh，总量 72.2 MB（阈值 5 MB），未达标。"
    r2 = g.check_dimension(good)
    print(f"  原文：{good}")
    print(f"  判定：ok={r2['ok']}  {r2['detail']}")
    if r2["ok"] is True:
        print("  ✓ 放行")
    else:
        print("  ✗✗ 误杀 —— 正例被拦"); fail += 1

    print("\n### 正例 2：本轮没做验收 ⇒ 未核（不许误杀）")
    r3 = g.check_dimension("本轮整理了地形草稿，尚未完成，无交付。")
    print(f"  判定：ok={r3['ok']}（None=未核）  {r3['detail']}")
    if r3["ok"] is None:
        print("  ✓ 未核，不拦（未核 ≠ 不通过）")
    else:
        print("  ✗✗ 把『没做』判成了『做错』"); fail += 1

    print("\n### 正例 3：结论随数值一起变 ⇒ 判据在工作（不算钝化）")
    rows2 = [{"name": "面数", "line": f"面数检查：{40+i} 个，通过"} for i in range(6)]
    rs2 = g.check_stale(rows2)
    print(f"  台账：面数检查：40 个，通过 / 41 个，通过 / … / 45 个，通过")
    print(f"  判定：ok={rs2['ok']}  {rs2['detail']}")
    if rs2["ok"] is True:
        print("  ✓ 放行（结论里的数值每轮都在变 = 判据有信息）")
    else:
        print("  ✗✗ 误杀 —— 变化的判据被判钝化"); fail += 1

    print("\n### 负例 3：结论恒定但前缀数字在变（最隐蔽的钝化）")
    rows3 = [{"name": "残留", "line": f"验收通过：**{200+i*10}.3 KB / 19 篇**，媒体残留 0。"}
             for i in range(6)]
    rs3 = g.check_stale(rows3)
    print(f"  台账：…**200.3 KB**…媒体残留 0 / …**210.3 KB**…媒体残留 0 / …（6 轮）")
    print(f"  判定：ok={rs3['ok']}  {rs3['detail']}")
    if rs3["ok"] is False:
        print("  ✓✓ 拦住（前缀在变、结论一动不动 = 已钝化）")
    else:
        print("  ✗✗ 没拦住 —— 最隐蔽的钝化漏了"); fail += 1

    print("\n" + "=" * 74)
    print("自检结果：" + ("全部通过" if fail == 0 else f"有 {fail} 项失败"))
    print("=" * 74)
    return 1 if fail else 0


def _utf8_stdio() -> None:
    """GBK 终端防崩（2026-10-04 实测两态）：

    · 直连控制台：gbk + errors='strict' ⇒ 打印 ✗/○/✓ **必崩**（UnicodeEncodeError）
    · 管道：gbk + errors='replace' ⇒ 不崩，但符号**静默丢成 '?'**
      （更阴 —— 闸报了红，人看到的却是 "B0 ?"，还以为没拦）
    ⇒ 统一改 reconfigure 成 UTF-8 + replace：不崩、且符号按 UTF-8 正确输出。
    ★ 用 reconfigure 而不是再包一层 TextIOWrapper：原地改、幂等 ——
      双层包装会让旧 wrapper 被 GC 时关掉底层 buffer
      （ValueError: I/O operation on closed file，2026-10-04 实测踩过）。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(prog="g_criteria")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--text-file")
    ap.add_argument("--ledger", help="台账 jsonl（覆盖默认）")
    ap.add_argument("--check-stale", action="store_true", help="查钝化")
    ap.add_argument("--record", help='记一条判据："name|line"，或用 --name/--line')
    ap.add_argument("--name")
    ap.add_argument("--line")
    a = ap.parse_args()

    if a.demo:
        return demo()

    from guard import load as load_cfg
    cfg = load_cfg(a.project, a.project_root)
    g = CriteriaGate(cfg)
    if a.ledger:
        g.ledger = Path(a.ledger)

    if a.record or (a.name and a.line):
        name, line = (a.name or "?"), (a.line or "")
        if a.record and "|" in a.record:
            name, line = a.record.split("|", 1)
        elif a.record:
            line = a.record
        g.record(name, line)
        print(f"已记录判据：{name} = {line}")
        return 0

    if a.check_stale:
        rs = g.check_stale()
        print("钝化检测：" + rs["detail"])
        for s in rs.get("stale", []):
            print(f"  ✗ 判据『{s['name']}』连续 {s['run']} 次完全相同 ⇒ 已钝化：{s['line']}")
        return 0 if rs["ok"] is not False else 1

    if a.text_file:
        rep = g.check_report(Path(a.text_file).read_text(encoding="utf-8", errors="replace"))
        print(rep["summary"])
        if rep["ok"] is False:
            for b in rep["a"].get("bad", []):
                print(f"  ✗ 缺 {b['missing']}：{b['line']}")
        return 0 if rep["ok"] is not False else 1

    print("用法：--demo / --text-file <报告> / --check-stale / --record \"名|内容\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
