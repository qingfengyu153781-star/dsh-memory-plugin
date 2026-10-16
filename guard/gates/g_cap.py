# -*- coding: utf-8 -*-
"""_guard · 能力闸 g_cap —— 让 AI 知道自己手上有哪些插件/技能，并在该用时主动用

用户明令（2026-10-03）：**要让它知道自己有什么插件，并且主动去用**，不是等人点名。

关键设计（决定它是"活的"还是"摆设"）：
  · 能力清单**当场从运行时/磁盘算出**，不读手抄名单（手抄的必然过期）
  · 每个条目都带**来源**（哪个目录/哪个接口读出来的）→ 可追溯、可核验
  · 使用时用 **新鲜度检查**：清单超过 N 分钟未刷新即拒绝判定（防用过期清单）
  · 给出"该用没用"的判据：识别任务里的**触发关键词**，若命中却没加载对应能力 ⇒ 报红

跨项目：能力来源在 config.capability.sources；本文件不含项目专有内容。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402


class CapabilityMiss(RuntimeError):
    """有该用的能力却没用 —— 打回。"""


class CapabilityGate:
    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        g = cfg.get("capability") or {}
        self.sources = list(g.get("sources") or [])
        self.max_age = int(g.get("max_age_s", 600))
        self.ledger = cfg.resolve((cfg.get("cap") or {}).get("ledger", "_guard_ledger/cap.jsonl"))
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = self.ledger.parent / "capability_snapshot.json"
        self._cache: dict | None = None
        self._cache_at: float = 0.0

    # ------------------------------------------------------------------
    # 现场采集（每个条目都带来源）
    # ------------------------------------------------------------------
    # pnpm/npm 的**真身**在这些隐藏目录里（顶层只留符号链接）⇒ 必须钻进去，
    #   否则"装了 16 个插件"的能力清单只会扫出顶层那 6 个（**范围不全说没有**，B5 的同型病）。
    VENDOR_DIRS = (".pnpm", "node_modules")
    SKIP_DIRS = (".cache", ".bin", ".modules.yaml", ".pnpm-workspace-state-v1.json")

    def _scan_dir(self, path: str, kind: str, pattern: str = "*", _depth: int = 0) -> list[dict]:
        p = Path(path)
        out = []
        if not p.exists() or _depth > 4:
            return out
        for child in sorted(p.glob(pattern)):
            name = child.name
            # ① 供应商目录（.pnpm / node_modules）：钻进去找真实包
            if child.is_dir() and name in self.VENDOR_DIRS:
                out += self._scan_dir(str(child), kind, pattern, _depth + 1)
                continue
            # ② 其它隐藏项：跳过（.cache/.bin 等不是能力）
            if name.startswith("."):
                continue
            if name in self.SKIP_DIRS:
                continue
            # ③ npm scope 目录（@xxx）是容器不是插件 → 钻进去
            if name.startswith("@") and child.is_dir():
                out += self._scan_dir(str(child), kind, pattern, _depth + 1)
                continue
            # ④ pnpm 真身形如 `dsh-office@0.1.1` ⇒ 去掉版本号后缀，还原包名
            disp = re.sub(r"@[\d][^@/]*$", "", name) if "@" in name[1:] else name
            #   作用域包 `@scope+name@1.0.0`（.pnpm 里的写法）→ `@scope/name`
            m = re.match(r"^(@[^+@/]+)\+([^@/]+)@[\d]", name)
            if m:
                disp = f"{m.group(1)}/{m.group(2)}"
            if not disp:
                continue
            desc = self._describe(child)
            out.append({"kind": kind, "name": disp, "source": str(p),
                        "desc": desc, "is_dir": child.is_dir(),
                        "path": str(child)})
        return out

    @staticmethod
    def _describe(child: Path) -> str:
        """尽量取一句人话描述；无则留空（不拿 name: xxx 这种 front-matter 充数）。"""
        for cand in ("package.json", "SKILL.md", "README.md"):
            f = child / cand
            if not f.is_file():
                continue
            try:
                txt = f.read_text(encoding="utf-8", errors="replace")[:6000]
            except Exception as e:  # noqa: BLE001
                # ★★ 2026-10-04 修（静默吞异常）：原为 `continue` ⇒ 读不了也不说，
                #   结果是"没描述"与"读失败"分不开 —— 能力清单会静默变空壳。
                print(f"[g_cap] ⚠ 读描述失败 {f}：{type(e).__name__}: {e}", file=sys.stderr)
                continue
            if cand == "package.json":
                try:
                    import json as _j
                    d = (_j.loads(txt).get("description") or "").strip()
                    if d:
                        return d[:100]
                except Exception as e:  # noqa: BLE001
                    # 同上：JSON 坏了要能看出来，不许无声当成"没描述"
                    print(f"[g_cap] ⚠ package.json 解析失败 {f}：{type(e).__name__}: {e}",
                          file=sys.stderr)
                continue
            # Markdown：跳过 YAML front-matter 与纯 name:/description: 行
            body = txt
            if body.lstrip().startswith("---"):
                parts = body.split("---", 2)
                if len(parts) >= 3:
                    body = parts[2]
            for line in body.splitlines():
                s = line.strip()
                if not s or s.startswith(("#", "-", "*", "|", "```", "---")):
                    continue
                if re.match(r"^(name|description|title|version)\s*[:：]", s, re.I):
                    continue
                if len(s) >= 6:
                    return s[:100]
        return ""

    def scan(self, force: bool = False) -> dict:
        now = time.time()
        if self._cache and not force and (now - self._cache_at) < self.max_age:
            self._cache["cache_age_s"] = round(now - self._cache_at, 1)
            return self._cache

        items: list[dict] = []
        for s in self.sources:
            kind = s.get("kind", "unknown")
            if s.get("path"):
                items += self._scan_dir(s["path"], kind)
            elif s.get("runtime"):
                # 运行时接口类能力：登记为"由运行时提供"，并尝试导入探测
                items.append({"kind": kind, "name": s["runtime"], "source": f"runtime:{s['runtime']}",
                              "desc": "由运行时接口提供（调用前需先列一次）", "is_dir": False})
            elif s.get("list"):
                for nm in s["list"]:
                    items.append({"kind": kind, "name": nm, "source": "config:list", "desc": "", "is_dir": False})

        snap = {"scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "scanned_ts": now, "cache_age_s": 0.0,
                "n": len(items), "items": items}
        self._cache, self._cache_at = snap, now
        self.snapshot_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        return snap

    # ------------------------------------------------------------------
    # 新鲜度
    # ------------------------------------------------------------------
    def freshness(self, snap: dict | None = None) -> dict:
        snap = snap or self.scan()
        age = time.time() - snap["scanned_ts"]
        return {"age_s": round(age, 1), "max_age_s": self.max_age,
                "fresh": age <= self.max_age, "scanned_at": snap["scanned_at"]}

    # ------------------------------------------------------------------
    # 该用没用
    # ------------------------------------------------------------------
    def coverage(self, task: str, used: list[str] | None) -> dict:
        """给定任务描述与本次实际用到的能力名，判断"命中却没用"。"""
        snap = self.scan()
        fresh = self.freshness(snap)
        used = used or []
        # 触发词 → 能力名关键字（通用规则，跨项目适用）
        # ★★ 2026-10-04 修（自证循环 + 真实任务漏检）：
        #   ① 自证循环：demo 负例文本 "用 Blender 做第一镜白模预演，渲一版出来" 里
        #      的 `Blender`/`白模`/`预演` **就是下面第 3 条正则的字面量** ⇒
        #      "自检通过"只证明"我认出我自己写的词"，不证明真实任务能命中。
        #   ② 真实漏检（实测）：`帮我算一下这个月的成本和补贴`（明显的 Excel/财务任务）
        #      ⇒ expected=[] **放行**；`用那个三维软件搭个场景` ⇒ 也放行。
        #      根因：触发词要求**写出工具名/术语**（Excel/Blender/建模），
        #      而人真实说话时说的是**意图**（"算成本和补贴""搭个场景"）。
        #   修法：触发词按**意图**扩充（不按工具名），并补"口语化说法"一列。
        TRIGGERS = [
            (r"生图|出图|首帧|尾帧|提示词|参考图|三视图|大头照|画一?[张个幅]|配图|插画", ["generate", "image", "生图", "画图"]),
            (r"视频|成片|运镜|动画|mp4|短片|剪辑|镜头连|成片|分镜动", ["video", "视频"]),
            (r"Blender|白模|预演|建模|场景件|三维|3D|搭个场景|搭场景|模型|blend", ["blender", "wushannv", "previs"]),
            (r"PPT|路演|幻灯片|演示|汇报稿|讲稿|做一?[份个]?\s*(?:ppt|演示)", ["ppt", "slide", "idesign", "ippt"]),
            (r"Excel|表格|财务|模型|算账|成本|补贴|预算|营收|定价|算一下|核算|对账|报表|xlsx", ["excel", "office", "xlsx"]),
            (r"Word|文档|报告|docx|写一?[份个篇]|成文", ["office", "docx", "word"]),
            (r"检索|记忆|历史对话|上次|之前说过|以前说|找一下.*(?:结论|记录)|回忆", ["memory", "recall", "rag"]),
            (r"可视化|图表|看板|画个图|图看看|展示出来", ["visualize", "图表"]),
            (r"深度研究|调研|竞品|文献|查一?下.*资料|市场.*(?:分析|调研)", ["research", "deep-research"]),
        ]
        expected: list[str] = []
        hit_kinds: list[str] = []
        for pat, keys in TRIGGERS:
            if re.search(pat, task, re.I):
                hit_kinds.append(pat)
                for it in snap["items"]:
                    nm = it["name"].lower()
                    if any(k.lower() in nm for k in keys):
                        expected.append(it["name"])
        expected = sorted(set(expected))
        used_l = [u.lower() for u in used]
        missed = [e for e in expected if not any(e.lower() in u or u in e.lower() for u in used_l)]

        # ★★ 2026-10-04 新增（治"扫描范围不全说没有"长在闸自己身上 · 病根 5）：
        #   实测：任务「一条算本月开支与补贴的请求」命中了财务/Excel 触发词，
        #   但本机能力清单里**根本没有任何 excel/office 类条目** ⇒ expected=[] ⇒ 静默放行。
        #   而"本机没装该能力"与"这个任务不需要能力"是**两回事**：
        #   前者要报"能力缺口"（据此去补装/换路子），后者才该放行。
        #   ⇒ 判据补一条：**触发词命中但 expected 为空** ⇒ 显式报缺口，不再当成"无关任务"。
        gap = bool(hit_kinds) and not expected
        ok = bool(fresh["fresh"]) and not missed and not gap
        rec = {"task": task[:80], "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "freshness": fresh, "expected": expected, "used": used,
               "missed": missed, "capability_gap": gap,
               "triggered": [k[:40] for k in hit_kinds], "ok": ok}
        self._log(rec)
        return rec

    def enforce(self, task: str, used: list[str] | None) -> dict:
        rec = self.coverage(task, used)
        if not rec["freshness"]["fresh"]:
            raise CapabilityMiss(
                f"能力清单不新鲜（{rec['freshness']['age_s']}s > {self.max_age}s）—— "
                f"禁止用过期的能力清单判定。请先重新扫描。"
            )
        if rec["missed"]:
            raise CapabilityMiss(
                f"这项任务命中了 {len(rec['missed'])} 个**该用却没加载**的能力：{rec['missed']}\n"
                f"   任务：{task[:70]}\n"
                f"   本机已装且相关的能力见：{self.snapshot_path}\n"
                f"⇒ 规矩：先查能力清单，命中的主动加载再动手（别等人点名）。"
            )
        if rec.get("capability_gap"):
            raise CapabilityMiss(
                f"**能力缺口**：这项任务的触发词命中了（{rec['triggered'][:2]}），"
                f"但本机能力清单里**没有任何相关能力**（expected=[]）。\n"
                f"   任务：{task[:70]}\n"
                f"   ⇒ 这不是『任务与能力无关』，而是『本机没有这个能力』。\n"
                f"   请二选一并报回来：① 先装/加载对应能力再动手；"
                f"② 明确说明改用别的手段（并说明为什么够用）。\n"
                f"   禁止沉默地当无事发生 —— 那正是『范围不全却当成没有』（病根 5）。"
            )
        return rec

    def _log(self, rec: dict) -> None:
        with open(self.ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def render(self, snap: dict | None = None) -> str:
        snap = snap or self.scan()
        f = self.freshness(snap)
        L = [f"能力清单（{snap['n']} 项，扫描于 {snap['scanned_at']}，新鲜度 {f['age_s']}s/{self.max_age}s）"]
        by_kind: dict[str, list[dict]] = {}
        for it in snap["items"]:
            by_kind.setdefault(it["kind"], []).append(it)
        for kind, its in by_kind.items():
            L.append(f"  【{kind}】{len(its)} 项")
            for it in its[:12]:
                d = f" — {it['desc']}" if it["desc"] else ""
                L.append(f"     · {it['name']}{d}")
            if len(its) > 12:
                L.append(f"     … 另 {len(its)-12} 项")
        return "\n".join(L)


def demo(cfg: ProjectConfig) -> int:
    g = CapabilityGate(cfg)
    fails = 0

    print("### 能力清单（当场从磁盘/运行时算出，非手抄）")
    snap = g.scan(force=True)
    print(g.render(snap))
    print()

    # 可追溯性：每条都能指回来源
    print("### 可追溯性抽查")
    for want in ("dsh-blender-plugin", "dsh-po06", "dsh-memory-plugin"):
        hit = next((i for i in snap["items"] if i["name"] == want), None)
        if hit:
            print(f"  ✓ {want} ← 读自 {hit['source']}")
        else:
            print(f"  ⚠ {want} 未在清单中（可能未装）")
    print()

    print("### 负例 1：任务明显要用 Blender 能力，却一个都没加载")
    try:
        g.enforce("用 Blender 做第一镜白模预演，渲一版出来", used=[])
        print("  ✗✗ 没拦住"); fails += 1
    except CapabilityMiss as e:
        print("  ✓✓ 拦截成功：")
        for line in str(e).splitlines()[:3]:
            print("     " + line)
    print()

    print("### 负例 2：要生图，却没加载生图能力")
    try:
        g.enforce("给出这一镜的首帧提示词与参考图", used=["dsh-po06"])
        print("  ✗✗ 没拦住"); fails += 1
    except CapabilityMiss as e:
        print("  ✓✓ 拦截成功：")
        for line in str(e).splitlines()[:3]:
            print("     " + line)
    print()

    # ★★ 2026-10-04 新增（治自证循环）：
    #   上面两条负例的文本**含闸自己正则的字面量**（"Blender"/"白模"/"预演"/"首帧"/"参考图"），
    #   等于"照答案出题" ⇒ 全绿也不证明真实能拦。
    #   本节用**真实口语任务**（不含任何工具名术语，只有意图）做负例 —— 出处：
    #   《问题总表》反复出现的形态「用户说的是要什么，AI 找的是叫什么」。
    print("### 负例 3（破自证循环）：真实口语任务，全文不含任何工具名")
    ORAL = [
        ("帮我算一下这个月的成本和补贴", "财务/Excel 类"),
        ("用那个三维软件搭个场景", "3D 类"),
        ("把这几个片段接起来出个短片", "视频类"),
    ]
    for task, kind in ORAL:
        try:
            g.enforce(task, used=[])
            print(f"  ✗✗ 没拦住（{kind}）：{task}")
            fails += 1
        except CapabilityMiss as e:
            n = str(e).splitlines()[0]
            print(f"  ✓✓ 拦截成功（{kind}）：{task}")
            print(f"     {n}")
    print()

    print("### 负例 4：能力清单不新鲜必须拒绝判定（不许用过期清单）")
    snap = g.scan(force=True)
    snap["scanned_ts"] = time.time() - (g.max_age + 60)   # 人为做旧
    g._cache, g._cache_at = snap, time.time()              # 让缓存命中这份旧快照
    try:
        g.enforce("用 Blender 做第一镜白模预演", used=[])
        print("  ✗✗ 没拦住"); fails += 1
    except CapabilityMiss as e:
        print("  ✓✓ 拦截成功：" + str(e).splitlines()[0])
    g._cache = None   # 复原
    print()

    print("### 正例：任务命中的能力都用上了（不该误杀）")
    try:
        # 把"命中清单"里每一项都真的加载（这正是规矩要求的动作）
        need = g.coverage("用 Blender 做第一镜白模预演", used=[])["expected"]
        rec = g.enforce("用 Blender 做第一镜白模预演", used=need)
        print(f"  ✓ 放行：命中 {len(rec['expected'])} 项，全部已加载 → {rec['expected']}")
    except CapabilityMiss as e:
        print("  ✗ 误杀：" + str(e)[:140]); fails += 1
    print()

    print("### 正例：任务与能力无关（不该乱拦）")
    try:
        rec = g.enforce("把这句中文润色一下", used=[])
        print(f"  ✓ 放行：无需特定能力（命中 {len(rec['expected'])} 项）")
    except CapabilityMiss as e:
        print("  ✗ 误杀：" + str(e)[:140]); fails += 1
    print()
    print(f"### 自检结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        # 2026-10-04：原为 `pass`（静默）。切 UTF-8 失败不影响判定，但仍要可诊断，
        #   否则"本来就没切"与"切失败了"分不开（后者会让 ✓/✗ 变乱码却无人知晓）。
        print(f"[警告] stdout 切 UTF-8 失败（不影响判定，但输出符号可能乱码）："
              f"{type(e).__name__}: {e}；可设 PYTHONIOENCODING=utf-8 重跑", file=sys.stderr)
    from guard import load

    ap = argparse.ArgumentParser(prog="g_cap")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--list", action="store_true", help="只列能力清单")
    a = ap.parse_args()
    cfg = load(a.project, a.project_root)
    if a.demo:
        sys.exit(demo(cfg))
    if a.list:
        print(CapabilityGate(cfg).render())
        sys.exit(0)
    print("用法：--list 列能力清单 / --demo 负例自检")
