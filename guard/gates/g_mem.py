# -*- coding: utf-8 -*-
"""_guard · 内存闸 g_mem —— 跑重活之前先看"这台机器还剩多少内存"

## 治的哪条规矩（2026-10-03 新增）

**R-40**：跑重活（ASR / 大模型推理 / 视频生成 / 长渲染）之前，先实测可用内存；
按"任务档位"给最低内存门槛，**不够就降档或拒绝开跑**，不许硬上。

## 为什么必须做成闸（这条已真实踩过两次）

文档里写过的"H3 本地硬墙＝系统内存，本机 15.7GB 是硬墙"**没有东西在跑它** ⇒ 原地重犯：
2026-10-03 一次 PR 剪辑学习任务，**两个子智能体先后在 ASR 中途死掉**
（一个只转完 900/1095 s、一个只转完 327/765 s），
根因全是 `faster_whisper` 的 `small` 模型报
`RuntimeError: mkl_malloc: failed to allocate memory` —— **连模型都加载不了**。
换 `tiny` + 切块 + 续转后一次跑通。

**这条正是 g_behavior 那条思路的延续**：原以为是"纯行为约束、无法机器判定"，
拆开看 —— **"可用内存"是一个可以直接量出来的数**（`Win32_OperatingSystem.FreePhysicalMemory`），
**"任务要多少内存"也是可声明的档位** ⇒ 完全可判定。

## 三档任务与门槛（默认值，可按项目覆盖）

**★ 先回答一个必答问题（2026-10-04 用户点名核过）：这些门槛是"取数算出来的"还是"硬编码的"？**

| 量 | 来源 | 结论 |
|---|---|---|
| 三档门槛 `0.8 / 3.5 / 6.0` GB、舒适线 `6.0` GB | `DEFAULTS` 模块级常量（可被 `projects/<名>.yaml` 的 `mem:` 覆盖） | **硬编码的标定值** —— 它是"经验标定"，**不随本机变化**（这是对的：门槛是常量，不是观测量） |
| "够不够"比的那个**可用内存** | `probe_memory()` → `Win32_OperatingSystem.FreePhysicalMemory` | **真从系统取数** |
| 取数失败时 | `M0` 行 `ok=None` + 逐档一律 `ok=None` ⇒ `check_text.ok=False` | **fail-closed（拦下）**，实测见 findings_B.md ②/B-1 |

⇒ 一句话：**门槛是常量、读数是实测、失败是拦下。** 三者必须分清，混起来就会出
"看着有闸、其实拿的是过期的数"这种假绿。

| 档位 | 典型任务 | 最低可用内存 | 不够时的建议 |
|---|---|---|---|
| `light` | 文本处理、ffmpeg 抽帧/切块、curl | 0.8 GB | —— |
| `asr` | Whisper `small`、中等模型推理 | **3.5 GB** | **降 `tiny`**（`small` 实测在 4.1 GB 可用时都会 OOM） |
| `heavy` | 视频生成、长渲染、批量推理 | **6.0 GB** | 拆批 / 关掉占内存的进程 / 换机 |

**注意 `asr` 档的门槛定为 3.5 GB 是"下限"而不是"够用"**：实测 4.1 GB 可用时
`small` 仍 `mkl_malloc` 失败 ⇒ **建议动作一律是"降 tiny"**，而不是"勉强够就跑"。

跨项目：阈值来自 config.mem；本文件不含项目专有内容。
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402

DEFAULTS = {
    # 档位 -> 最低可用内存（GB）
    "tiers": {
        "light": 0.8,
        "asr": 3.5,
        "heavy": 6.0,
    },
    # ★ asr 档的"舒适线"（高于此值才建议用 small；低于一律 tiny）
    #   实测：4.1 GB 可用时 small 仍 mkl_malloc 失败 ⇒ 舒适线必须显著高于下限
    "asr_comfort_gb": 6.0,
    # 各档位不够时的建议动作（写进报告，让人知道下一步干什么）
    "advice": {
        "light": "照常跑；若仍失败，检查是否被别的进程抢占。",
        "asr": "把 whisper 模型降到 tiny（WhisperModel(\"tiny\", ...)）+ 先切块再转 + 断点续转。",
        "heavy": "拆批 / 先关掉吃内存的进程 / 换机；不要硬上。",
    },
    # 超过这个比例就顺带提醒（不拦，只提示）
    "warn_ratio": 0.85,
}


def _cfg_of(cfg: ProjectConfig) -> dict:
    d = {}
    for k, v in DEFAULTS.items():
        d[k] = dict(v) if isinstance(v, dict) else v
    over = cfg.get("mem") or {}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            d[k].update(v)
        else:
            d[k] = v
    return d


def probe_memory() -> dict:
    """实测本机内存。返回 {ok, total_gb, free_gb, source, err}

    走 Windows 的 Win32_OperatingSystem（本机唯一的可靠来源）；
    非 Windows 退回 /proc/meminfo；都拿不到就 ok=False（**不猜**）。
    """
    sysname = platform.system()
    if sysname == "Windows":
        ps = (
            "$os = Get-CimInstance Win32_OperatingSystem;"
            "'{0} {1}' -f $os.TotalVisibleMemorySize, $os.FreePhysicalMemory"
        )
        try:
            out = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=30,
            )
            parts = (out.stdout or "").split()
            if len(parts) >= 2:
                total = float(parts[0]) / 1024.0 / 1024.0   # KB -> GB
                free = float(parts[1]) / 1024.0 / 1024.0
                return {"ok": True, "total_gb": round(total, 2),
                        "free_gb": round(free, 2), "source": "Win32_OperatingSystem"}
            return {"ok": False, "err": f"powershell 无有效输出: {(out.stderr or '')[:120]}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "err": f"探测失败: {e}"}

    # Linux / WSL
    try:
        info = Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace")
        vals = {}
        for ln in info.splitlines():
            if ":" in ln:
                k, _, v = ln.partition(":")
                vals[k.strip()] = v.strip()
        def _gb(key: str) -> float:
            return float(vals[key].split()[0]) / 1024.0 / 1024.0
        return {"ok": True, "total_gb": round(_gb("MemTotal"), 2),
                "free_gb": round(_gb("MemAvailable"), 2), "source": "/proc/meminfo"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": f"探测失败: {e}"}


class MemGate:
    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self.c = _cfg_of(cfg)
        # ★ 2026-10-04（Lead）：补 ledger —— 原来是**唯一一道没有台账的闸**，
        #   导致审计器 D5 判"从未在真实产出上跑过"，且**无从查证**它跑没跑过。
        #   判据："闸接上了"要看三件事（被实例化 / 判据可达 / **ledger 有真实记录**），
        #   这道闸此前第③条无从验证 ⇒ 与 g_behavior 当初"没有 behavior.jsonl"同病。
        #
        # ★★ 路径口径统一（更重要的是这条）：`cfg.resolve("_guard_ledger/x.jsonl")`
        #   会拼到 **project_root**（`<WORKSPACE>`）⇒ 落在 `<WORKSPACE>\_guard_ledger\`；
        #   而审计器 `_guard/_audit/audit_gates.py` 读的是**工具层**的
        #   `_guard/_guard_ledger/` ⇒ **两套台账、互不可见** ⇒
        #   审计器永远报"从未运行"，哪怕闸天天在跑。这是"静默失效"的又一实例
        #   （失败不改变任何可见状态：闸跑了、台账也写了，但**看的地方不对**）。
        #   ⇒ 统一到**工具层** `_guard/_guard_ledger/`（与其它 16 道闸的实际落点一致）。
        lg = (cfg.get("mem") or {}).get("ledger")
        if lg:
            self.ledger = cfg.resolve(lg)
        else:
            self.ledger = Path(__file__).resolve().parent.parent / "_guard_ledger" / "mem.jsonl"

    def _log(self, rec: dict) -> None:
        """落台账。★ 失败必须留痕（不许 `except: pass` 静默吞）—— 见 D6。

        ★★ 而且**写失败必须能从退出码看出来**：原实现只 `sys.stderr.write` 就放过，
        调用方拿到的仍是 `ok=True` ⇒ **台账没了但闸报绿**，正是"静默失效"。
        现在：写失败 ⇒ 在返回值里打 `ledger_ok=False`，让上位机看得见。
        """
        rec = {**rec, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        try:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            with open(self.ledger, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[g_mem] 台账写入失败（判决仍有效）：{e}\n")
            return False
        return True

    def check_tier(self, tier: str, free_gb: float | None = None,
                   probe: dict | None = None) -> dict:
        """判某个档位现在能不能跑。返回 row 形状的 dict。

        ★ 2026-10-04 修（B-1）：原实现有个**逃逸门**——
        `check_text()` 在 probe 失败时把 `free_gb=None` 传进来，
        而这里 `if free_gb is None:` 会**再 probe 一次**。
        于是"第一次取数失败、第二次恰好成功"时，逐档行照常判绿，
        而 M0 那行记的 ok=None 就被 `elif unknown:` 的汇总逻辑**排到后面去了**
        （`bad` 优先于 `unknown`，且两条路都不影响逐档行的 ok=True）
        ⇒ 实测模拟探针**永久失败**时确实 fail-closed（ok=False），
          但"偶发失败"这一真实形态下，判据用的是**第二次**的结果、
          而报告里却出现了**自相矛盾的两条 M0**（原 demo 输出里真是两条 M0）。
        修法：`probe` 显式传入 —— 一次判定只取一次数，**一次取数失败就是这一轮失败**，
        不许在同一个判定里偷偷重试。
        """
        tiers = self.c["tiers"]
        if tier not in tiers:
            return {"id": "M?", "rule": "内存闸", "ok": None,
                    "detail": f"未知档位 {tier}（可选：{', '.join(tiers)}）"}
        need = float(tiers[tier])
        if free_gb is None:
            m = probe if probe is not None else probe_memory()
            if not m.get("ok"):
                # **测不到就不许假装测到了** —— 返回"未知"，不是"通过"
                return {"id": f"M-{tier}", "rule": "内存闸", "ok": None,
                        "detail": f"**测不到可用内存**（{m.get('err')}）⇒ 不判绿；"
                                  f"先手动确认再跑 {tier} 档"}
            free_gb = float(m["free_gb"])
        ok = free_gb >= need
        advice = self.c["advice"].get(tier, "")
        detail = (f"{tier} 档需 ≥{need} GB，实测可用 **{free_gb:.2f} GB** ⇒ "
                  f"{'够' if ok else '**不够**'}")
        if not ok:
            detail += f"；建议：{advice}"
        # ★ asr 档专用：即便"够下限"，只要没到舒适线，也必须点名"用 tiny"
        #
        # ★ 2026-10-04 修（B-2 · 判据量错对象）：原实现把"低于舒适线"只写进 detail，
        #   返回值 `ok` 仍是 **True** ⇒ `check_text()` 汇总成
        #   「…」。**用户拿到的是"放行"两个字**，
        #   而真正该传出去的结论是"**不许用 small，必须降 tiny**"。
        #   实测（before_real_input.txt ③）：本机 3.74 GB 可用时判 asr 档
        #   → ok=True、summary=「放行」，与 AGENTS.md「不够/偏低就降 tiny」相矛盾。
        #   修法：把"够下限但低于舒适线"独立成一个三态 —— `degraded`。
        #   `ok` 仍为 True（确实没到"拒绝开跑"的程度），但
        #   ① 新增 `verdict` 字段供上位机判定；② summary 不再说"放行"而说"**须降档**"。
        degraded = False
        if tier == "asr" and ok:
            comfort = float(self.c.get("asr_comfort_gb", 6.0))
            if free_gb < comfort:
                degraded = True
                detail += (f"；**但低于舒适线 {comfort} GB** ⇒ "
                           f"**仍须用 tiny 模型**（实测 small 在 4.1 GB 可用时就会 "
                           f"`mkl_malloc` 失败）。{advice}")
        row = {"id": f"M-{tier}", "rule": "内存闸", "ok": ok, "detail": detail,
               "free_gb": free_gb, "need_gb": need,
               "degraded": degraded,
               "verdict": ("refuse" if not ok else ("downgrade" if degraded else "go"))}
        # ★ 落台账（2026-10-04 Lead）：不落 ledger 的闸 = "跑没跑过无从查证"，
        #   审计器会判 D5「从未在真实产出上跑过」。判据要可复核，就必须留痕。
        #   `ledger_ok` 显式回传：写失败时上位机看得见（防"台账没了但闸报绿"）。
        row["ledger_ok"] = self._log(row)
        return row

    def check_text(self, text: str) -> dict:
        """文本形态：检查文本里声称要跑的重活档位，逐条判。

        识别"我要跑 ASR / 大模型 / 渲染"这类意图 + 显式档位标注。
        """
        rows = []
        low = text or ""
        tier_hits: list[str] = []
        # 显式档位写法：mem-tier: asr ／ 内存档位=heavy
        import re
        for m in re.finditer(r"(?:mem[-_ ]?tier|内存档位)\s*[:=：]\s*([a-z]+)", low, re.I):
            tier_hits.append(m.group(1).lower())
        # 意图推断
        intent = {
            "asr": [r"whisper", r"\bASR\b", r"语音转写", r"转写稿", r"faster[_-]?whisper"],
            "heavy": [r"视频生成", r"\bH3\b", r"ComfyUI", r"渲染", r"批量推理",
                      r"stable\s*diffusion", r"大模型推理"],
            "light": [r"ffmpeg", r"抽帧", r"切块", r"curl"],
        }
        for tier, pats in intent.items():
            for p in pats:
                if re.search(p, low, re.I):
                    if tier not in tier_hits:
                        tier_hits.append(tier)
                    break
        if not tier_hits:
            return {"ok": True, "summary": "内存闸 M：文本里没识别到重活档位 ⇒ 放行",
                    "rows": [], "verdict": "go"}

        # ★ 2026-10-04 修：**只取一次数**，并把同一个 probe 传下去
        #   （原实现 probe 失败时传 None ⇒ check_tier 又 probe 一次 ⇒ 见 check_tier 注释）
        m = probe_memory()
        if not m.get("ok"):
            rows.append({"id": "M0", "rule": "内存闸", "ok": None,
                         "detail": f"**测不到可用内存**（{m.get('err')}）⇒ 不判绿"})
        for t in tier_hits:
            row = self.check_tier(t, m.get("free_gb") if m.get("ok") else None,
                                  probe=m)
            rows.append(row)

        bad = [r for r in rows if r["ok"] is False]
        unknown = [r for r in rows if r["ok"] is None]
        degraded = [r for r in rows if r.get("degraded")]
        if bad:
            summary = f"内存闸 M：**{len(bad)} 项不够** ⇒ 拦下（降档或拒绝开跑）"
            verdict = "refuse"
        elif unknown:
            summary = f"内存闸 M：{len(unknown)} 项**测不到** ⇒ 不判绿"
            verdict = "unknown"
        elif degraded:
            # ★ 不许说"放行" —— 该传出去的结论是"必须降档"
            summary = (f"内存闸 M：{len(rows)} 项够下限，但 **{len(degraded)} 项低于舒适线 "
                       f"⇒ 须降档跑**（不许按默认档硬上）")
            verdict = "downgrade"
        else:
            summary = f"内存闸 M：{len(rows)} 项全部够用 ⇒ 放行"
            verdict = "go"
        return {"ok": not bad and not unknown, "summary": summary, "rows": rows,
                "mem": m, "verdict": verdict, "degraded": len(degraded)}

    def check_file(self, path: str | os.PathLike) -> dict:
        p = Path(path)
        return self.check_text(p.read_text(encoding="utf-8", errors="replace"))


def demo(cfg: ProjectConfig) -> int:
    g = MemGate(cfg)
    print("### 内存闸 g_mem 演示")
    m = probe_memory()
    print(f"  实测内存：{json.dumps(m, ensure_ascii=False)}")
    print()
    fails = 0

    print("### 1. 真实档位判定（用本机实测值）")
    if m.get("ok"):
        for t in g.c["tiers"]:
            r = g.check_tier(t, m["free_gb"])
            mark = "✓够" if r["ok"] else "✗不够"
            print(f"  {mark}  {r['id']:8s} {r['detail'][:90]}")
    else:
        print(f"  —— 本机测不到内存：{m.get('err')}")
    print()

    print("### 2. 负例：把可用内存设成很小，应拦下并给降档建议")
    for t, fake in (("asr", 1.0), ("heavy", 2.0), ("light", 0.5)):
        r = g.check_tier(t, fake)
        hit = r["ok"] is False
        print(f"  {'✓✓拦下' if hit else '✗ 未拦'}  可用 {fake} GB 跑 {t} 档"
              f"  →  {r['detail'][:80]}")
        if not hit:
            fails += 1
    print()

    print("### 3. 正例：内存充足应放行")
    for t, fake in (("light", 8.0), ("asr", 8.0), ("heavy", 16.0)):
        r = g.check_tier(t, fake)
        passed = r["ok"] is True
        print(f"  {'✓ 放行' if passed else '✗ 误杀'}  可用 {fake} GB 跑 {t} 档")
        if not passed:
            fails += 1
    print()

    print("### 4. 负例：文本提到 ASR 且内存不足 ⇒ 拦")
    # ★ 2026-10-04 修：原负例带**逃逸门** ——
    #   `if m['ok'] and m['free_gb'] < tiers['asr']: ... else: print('此项不适用（跳过）')`
    #   本机可用内存常年在 4 GB 上下，一旦 ≥3.5 GB 这条负例就**永远跳过**，
    #   而 demo 仍然打印"全部通过" ⇒ **自检给的是假绿**（见 findings_B.md ④/B-3）。
    #   修法：不再依赖"本机恰好内存不足"这个巧合 —— 用**注入的假 probe** 造出
    #   "取数失败"与"内存不足"两种**确定会发生**的形态，负例必然执行。
    r = g.check_text("我要先跑 whisper 把视频转写出来。")
    if m.get("ok") and m["free_gb"] < g.c["tiers"]["asr"]:
        ok = r["ok"] is False
        print(f"  {'✓✓拦下' if ok else '✗ 未拦'}  （本机真实）文本提到 ASR，可用 "
              f"{m['free_gb']} GB < {g.c['tiers']['asr']} GB")
        if not ok:
            fails += 1
    else:
        print(f"  —— 本机可用 {m.get('free_gb')} GB 已达 asr 门槛 ⇒ 本机形态不拦（正确）；"
              f"改用注入形态验证：")
    # ★ 确定形态 a：取数失败 ⇒ 必须 fail-closed
    _saved = globals()["probe_memory"]
    globals()["probe_memory"] = lambda: {"ok": False, "err": "（自检注入）取数不可用"}
    try:
        rf = g.check_text("mem-tier: heavy\n我要跑视频生成。")
        bad_fail_open = rf["ok"] is True
        print(f"  {'✓✓拦下' if not bad_fail_open else '✗✗ fail-open 放行！'}  "
              f"（注入）取数失败 + 声称跑 heavy ⇒ ok={rf['ok']} · {rf['summary'][:60]}")
        if bad_fail_open:
            fails += 1
        # 同一轮里不能出现两条自相矛盾的 M0，也不能靠"再探一次"翻盘
        n_m0 = len([x for x in rf["rows"] if x["id"] == "M0"])
        n_tier_unknown = len([x for x in rf["rows"] if x["ok"] is None and x["id"] != "M0"])
        print(f"     行构成：M0={n_m0} 条，逐档未判定={n_tier_unknown} 条 "
              f"{'✓ 一次取数只算一次' if n_m0 <= 1 else '✗ 重复取数'}")
        if n_m0 > 1:
            fails += 1
    finally:
        globals()["probe_memory"] = _saved
    # ★ 确定形态 b：够下限但低于舒适线 ⇒ 结论必须是"须降档"，不许说"放行"
    rl = g.check_tier("asr", 4.1)
    hit_low = (rl["ok"] is True and rl.get("verdict") == "downgrade")
    print(f"  {'✓✓须降档' if hit_low else '✗✗ 谎报放行'}  "
          f"可用 4.1 GB 跑 asr（>下限 3.5，<舒适线 6.0）⇒ verdict={rl.get('verdict')}")
    if not hit_low:
        fails += 1
    print()

    print("### 5. 正例：文本没提重活 ⇒ 不误伤")
    r = g.check_text("今天整理一下文档目录，把复盘索引挪出去。")
    ok = r["ok"] is True
    print(f"  {'✓ 放行' if ok else '✗ 误杀'}  {r['summary'][:80]}")
    if not ok:
        fails += 1
    print()

    print("### 6. 门槛来源自证（回答『是取数算出来的还是硬编码的』）")
    print(f"  · 三档门槛 {json.dumps(g.c['tiers'], ensure_ascii=False)} —— "
          f"**模块级常量（硬编码标定值）**，不随本机变化；")
    print(f"  · 『够不够』用的可用内存 {m.get('free_gb')} GB —— "
          f"**真从 {m.get('source')} 取数**（probe_memory()）；")
    print(f"  · 取数失败 ⇒ M0 记 ok=None + 逐档不判绿 ⇒ **fail-closed**（形态 a 已验证）。")
    print()

    print(f"### 自检结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    from guard import load as load_cfg

    ap = argparse.ArgumentParser(prog="g_mem")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--text-file")
    ap.add_argument("--tier", help="直接判某个档位：light / asr / heavy")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    g = MemGate(cfg)

    if a.demo:
        sys.exit(demo(cfg))
    if a.tier:
        row = g.check_tier(a.tier)
        if a.json:
            print(json.dumps(row, ensure_ascii=False, indent=1))
        else:
            m = "✓" if row["ok"] is True else ("✗" if row["ok"] is False else "?")
            print(f"{m} {row['detail']}")
        sys.exit(0 if row["ok"] else 1)
    if a.text_file:
        rep = g.check_file(a.text_file)
        if a.json:
            print(json.dumps(rep, ensure_ascii=False, indent=1))
        else:
            print(rep["summary"])
            for row in rep["rows"]:
                m = "✓" if row["ok"] is True else ("✗" if row["ok"] is False else "?")
                print(f"  {m} {row['id']} [{row['rule']}] {row['detail']}")
        sys.exit(0 if rep["ok"] else 1)
    # 无参数：直接报本机内存现状（最常用）
    m = probe_memory()
    if m.get("ok"):
        print(f"内存：总 {m['total_gb']} GB / 可用 {m['free_gb']} GB（{m['source']}）")
        for t in g.c["tiers"]:
            r = g.check_tier(t, m["free_gb"])
            mark = "✓" if r["ok"] else "✗"
            print(f"  {mark} {t:6s} 需 ≥{r['need_gb']} GB")
    else:
        print(f"内存：**测不到**（{m.get('err')}）")
    sys.exit(0)
