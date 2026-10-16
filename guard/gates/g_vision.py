# -*- coding: utf-8 -*-
"""_guard · 视觉核验 g_vision —— 图像结论必须有"看过"的证据；看不了就**明说**

## 治的三个问题（2026-10-03 用户点名）

**① 图像核验跟随当前模型**
  报告里写「我看了画面，人形完整」—— 以前 `g_xcheck` 核不了（没有可核对象）。
  治法：**真的去调视觉模型看**。模型跟当前配置走（`config.vision.model`）。

**② 模型没有识图能力 ⇒ 必须明说"我无法看图"，不许假装核过**
  用户原话：「…」
  实测教训（表里 P 系列）：视觉模型对**细线级/纹理级/亚像素级**细节读不出来
  （机械线、接缝、颗粒、虹膜反光）—— **它答"没有"不等于真没有**。
  治法：分两级 —— **大特征**可采信；**细线级**必须标"须人眼定"，不许拿模型结论判废。

**③ "真去改了但没测"也要解决**
  以前只能核"文件存在吗"，核不出"你跑没跑过"。
  治法：报告里的"实测值"必须有**带痕的来源** ——
  探针回传 / 命令输出 / 日志文件 / 台账，**手写的不算**（见 g_xcheck 的 X6/X7）。

## 用法
```python
from gates.g_vision import VisionGate
vg = VisionGate(cfg)
r = vg.check_image("shot_f48.png", "画面里有没有可辨识人形？手臂在躯干内还是外？")
# 模型没有识图能力 ⇒ r["ok"]=None 且 r["detail"] 明确写"无法看图"
```

跨项目：模型名/接口来自 config.vision；本文件不含项目专有内容。
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402

#: 本机专有路径**不进代码**（R-56）：从 tools/tool_paths.json 读（环境变量可覆盖）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from tool_paths import tool_path, expand_path  # noqa: E402


def _cred_path() -> Path:
    """凭据文件位置：环境变量 `DSH_CREDENTIALS` 优先，其次 `tools/tool_paths.json`。

    取不到 ⇒ 返回必然不存在的占位路径（下游会**显式**报"读不到"，不静默当作没 key）。
    """
    p = tool_path("credentials_yaml", "DSH_CREDENTIALS")
    return p if p else Path("__credentials_not_configured__")

DEFAULTS = {
    # 视觉线路（跟随当前在用的模型；用户 2026-09-12 换到官方线路）
    "endpoint": "https://api.deepseek.com/v1/chat/completions",
    "model": "deepseek-v4-flash-vision-exp",
    "key_name": "DEEPSEEK_API_KEY",
    "max_edge": 900,          # 缩到最长边 ≤900px（官方硬限 640000 像素）
    "max_bytes": 1000 * 1024,  # 1MB
    "timeout": 60,
    # 细线级关键词 —— 问到这些必须标"须人眼定"
    "fine_detail_pats": [
        r"机械线", r"接缝", r"颗粒", r"纹理", r"皮肤纹", r"虹膜", r"反光",
        r"细线", r"亚像素", r"文字边缘", r"划痕", r"发丝",
    ],
    # 大特征关键词 —— 这些模型可采信
    "gross_feature_pats": [
        r"几个人", r"人数", r"位置", r"姿态", r"朝向", r"脸朝", r"坐着", r"站着",
        r"有没有.*(?:人|物|家具)", r"白天|夜晚", r"在不在", r"占画面",
    ],
}


def _cfg_of(cfg: ProjectConfig) -> dict:
    d = dict(DEFAULTS)
    d["fine_detail_pats"] = list(DEFAULTS["fine_detail_pats"])
    d["gross_feature_pats"] = list(DEFAULTS["gross_feature_pats"])
    over = cfg.get("vision") or {}
    for k, v in over.items():
        if isinstance(v, list) and isinstance(d.get(k), list):
            d[k] = list(d[k]) + [x for x in v if x not in d[k]]
        else:
            d[k] = v
    return d


class NoVisionError(RuntimeError):
    """当前模型/线路没有识图能力 —— 必须**明确告诉用户**，不许假装核过。"""


class VisionGate:
    NAME = "g_vision"

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self.K = _cfg_of(cfg)
        lg = (cfg.get("vision") or {}).get("ledger", "_guard_ledger/vision.jsonl")
        self.ledger = cfg.resolve(lg)
        self.ledger.parent.mkdir(parents=True, exist_ok=True)

    # ---------------- 能力探测 ----------------
    def capability(self) -> dict:
        """当前能不能看图？（**先探能力，再决定说不说"我核过了"**）"""
        key = self._key()
        return {
            "endpoint": self.K["endpoint"],
            "model": self.K["model"],
            "has_key": bool(key),
            "can_see": bool(key),
            "detail": ("可看图" if key else
                       f"**无法看图**：凭证 {self.K['key_name']} 缺失或读不到（{_cred_path()}）"
                       " —— 任何『我看了画面』的结论都不成立"),
        }

    def _key(self) -> str:
        try:
            t = _cred_path().read_text(encoding="utf-8", errors="replace")
            m = re.search(self.K["key_name"] + r"\s*:\s*(\S+)", t)
            return m.group(1).strip() if m else ""
        except Exception:  # noqa: BLE001
            return ""

    # ---------------- 问题分级 ----------------
    def classify(self, question: str) -> str:
        """这问题属"大特征"还是"细线级"？

        ★ 关键的诚实性判据（表里 P 系列实证）：
          视觉模型对**细线级**细节读不出来，它答"没有"**不等于真没有** ——
          所以细线级问题**不许拿模型结论判废**，必须标"须人眼定"。
        """
        if any(re.search(p, question) for p in self.K["fine_detail_pats"]):
            return "fine"
        if any(re.search(p, question) for p in self.K["gross_feature_pats"]):
            return "gross"
        return "unknown"

    # ---------------- 真去看 ----------------
    def ask(self, image: str | Path, question: str) -> dict:
        cap = self.capability()
        kind = self.classify(question)
        p = Path(image)

        if not cap["can_see"]:
            r = {"ok": None, "kind": kind, "question": question, "image": str(p),
                 "answer": "", "detail": cap["detail"]}
            self._log(r) 
            return r
        if not p.exists():
            r = {"ok": False, "kind": kind, "question": question, "image": str(p),
                 "answer": "", "detail": f"图片不存在：{p}"}
            self._log(r)
            return r

        try:
            data, mime = self._prepare(p)
        except Exception as e:  # noqa: BLE001
            r = {"ok": None, "kind": kind, "question": question, "image": str(p),
                 "answer": "", "detail": f"图片预处理失败：{e}"}
            self._log(r)
            return r

        payload = {
            "model": self.K["model"],
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}},
                ],
            }],
            "max_tokens": 800,
        }
        req = urllib.request.Request(
            self.K["endpoint"], data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self._key()})
        try:
            with urllib.request.urlopen(req, timeout=int(self.K["timeout"])) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            msg = (body.get("choices") or [{}])[0].get("message") or {}
            ans = (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "").strip()
            r = {"ok": True, "kind": kind, "question": question, "image": str(p),
                 "answer": ans, "detail": "模型已看图并作答"}
        except urllib.error.HTTPError as e:
            r = {"ok": None, "kind": kind, "question": question, "image": str(p), "answer": "",
                 "detail": f"**看图失败**（HTTP {e.code}）—— 不许据此下结论"}
        except Exception as e:  # noqa: BLE001
            r = {"ok": None, "kind": kind, "question": question, "image": str(p), "answer": "",
                 "detail": f"**看图失败**：{type(e).__name__}: {str(e)[:80]} —— 不许据此下结论"}
        self._log(r)
        return r

    def _prepare(self, p: Path) -> tuple[bytes, str]:
        """缩图到限额内（官方硬限 640000 像素 / 1MB）。"""
        raw = p.read_bytes()
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        if len(raw) <= int(self.K["max_bytes"]):
            return raw, mime
        try:
            from PIL import Image  # type: ignore
            import io as _io
            im = Image.open(_io.BytesIO(raw))
            edge = int(self.K["max_edge"])
            if max(im.size) > edge:
                im.thumbnail((edge, edge))
            buf = _io.BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=85)
            return buf.getvalue(), "image/jpeg"
        except Exception:  # noqa: BLE001
            raise RuntimeError(f"图片 {len(raw)}B 超限且无法缩放（需 Pillow）")

    # ---------------- 报告级检查：图像结论必须有据 ----------------
    # ★★ 2026-10-15 修「…」的假根治（用户问："检查其他的是否有这样的假根治"）★★
    #   病根：原来只有一条**字面动词**清单 `我(看了|亲眼|亲看)|画面里|逐帧看|看图`。
    #   实测（对抗样本，见 `_orch_audit_20261015/对抗样本_违规报告.md`）：
    #     · "图像核验：**我看过**那张图了，没问题。" ⇒ 一个字都没匹配上（"看过" ≠ "看了"）
    #     · "我把那张**渲染图**看过了，挺正常的，所以这版模型可以通过。" ⇒ 同样跳过
    #   ⇒ 明明是图像结论，闸却报「报告无图像结论 … 跳过」，**exit 0 放行**。
    #   这与历史上 `/d/agent` 只认两个字面量是**同一族**：按"那一次的现象"写字面量，不按形态判。
    #   修法：加**形态判据** —— 同一行里既有**图像类对象**、又有**结论性动作** ⇒ 视为图像结论，
    #   再走"必须给看了哪张/哪一帧"的证据检查。
    _IMG_OBJ = r"(?:截图|渲染图|渲染|图片|图像|贴图|缩略图|视口|预览图|帧|画面|照片)"
    _IMG_ACT = (r"(?:我看|看过|看了|核过|核了|核对|确认|检查|对比|比对|逐帧|看图|亲眼看|"
                r"没问题|正常|清晰|正确|无误|一致|通过)")

    def _img_claims(self, text: str) -> list[str]:
        """形态判据：同一行里既有图像类对象、又有结论性动作 ⇒ 这是一条图像结论。"""
        out = []
        for line in str(text or "").splitlines():
            if re.search(self._IMG_OBJ, line) and re.search(self._IMG_ACT, line):
                out.append(line.strip()[:120])
        return out

    def check_report(self, text: str) -> dict:
        """报告里出现"我看了/画面里…"这类结论时，检查有没有**看过**的证据。"""
        claims = re.findall(r"(?:我(?:看了|亲眼|亲看)|画面里|逐帧看|看图)[^\n。]{4,80}", text)
        # ★ 形态判据兜底：字面动词没命中，但"对象+动作"齐了 ⇒ 同样当成图像结论
        for extra in self._img_claims(text):
            if extra not in claims:
                claims.append(extra)

        # ★★ P0 修复（2026-10-04，wiregates）：原实现在这里就 `if not claims: return`，
        #    把**机位判据整段挡在后面**（原 `cam_claims` 在其后 18 行）⇒
        #    **"没有图像结论" 被当成了 "没有机位结论"**。
        #    实测 A/B（同一句话）：
        #      · 不写"我看了"  → ok=True, rc=0 **放行**（机位判据根本没跑）
        #      · 写"我看了图，" → ok=False, rc=1 拦下（cam_checked=1）
        #    ⇒ 判据只在"恰好也写了图像结论措辞"时才生效，报机位的人**经常不写"我看了"**。
        #    这是**判据永不到达**（与 g_behavior 的 `mx=max(mx,2)` 同族，但更隐蔽：
        #    不是分支恒假，是整段代码被前置 return 挡住）。
        #    **根治**：两类结论**各自判定、合并结论** —— 没有图像结论 ≠ 没有机位结论。
        cap = self.capability()
        # ── 第一类：图像结论必须有"看过"的痕迹（帧号/图名/尺寸/读数）──
        rows = []
        for c in claims[:20]:
            strong = bool(re.search(r"f\d{1,4}|\.png|\.jpg|帧\s*\d+|看过.*帧", c))
            rows.append({"claim": c[:60], "ok": strong,
                         "detail": ("有帧号/图名佐证" if strong else
                                    "只写了结论，**没给看了哪张/哪一帧** ⇒ 无法复核")})
        bad = [r for r in rows if not r["ok"]]
        # ---- 第二类：机位 / 构图结论（2026-10-04 用户点名）----
        # 用户原话：「…」
        # 病根：报机位时凭感觉（"站 y=-90、50mm 看山"），不算"主体多大 / 视场要多少 / 该站多远"
        #   ⇒ 连带后果：相机贴在山面上、满屏主体、只拍到局部，却拿"图不好看"去否定几何。
        # 判据：凡出现机位/景别结论，必须同时出现 ①焦距(mm) ②距离(m) ③主体尺寸或视场(m)。
        cam_claims = re.findall(
            r"(?:机位|相机|镜头|焦距|景别|视场|取景|构图|占画面)[^\n。]{4,120}", text)
        cam_rows = []
        for c in cam_claims[:20]:
            has_f = bool(re.search(r"\d{1,3}\s*mm|焦距\s*[:：]?\s*\d", c))
            has_d = bool(re.search(r"距离|站\s*[yYzZ]?\s*[-−]?\d|\d+(?:\.\d+)?\s*m\s*(?:处|外|远|距|$)|[=＝]\s*\d+(?:\.\d+)?\s*m", c))
            has_s = bool(re.search(r"视场|主体|高\s*\d+(?:\.\d+)?\s*m|占画面|\d+(?:\.\d+)?\s*m\s*(?:高|宽|跨)", c))
            miss = [n for n, v in (("焦距", has_f), ("距离", has_d), ("主体尺寸/视场", has_s)) if not v]
            cam_rows.append({"claim": c[:70], "ok": not miss,
                             "detail": ("三要素齐（焦距/距离/主体尺寸）" if not miss
                                        else "缺 " + "、".join(miss)
                                             + " ⇒ **机位是猜的，不是解出来的**")})
        cam_bad = [r for r in cam_rows if not r["ok"]]
        ok = (len(bad) <= max(0, len(rows) // 3)) and (len(cam_bad) == 0)
        # ★ 合并结论：两类**各判各的**（这是本次修的核心 —— 原来第一类为空就整体短路）
        if not rows and not cam_rows:
            summ = "视觉核验：报告无图像结论、也无机位结论，跳过"
        else:
            parts = []
            if rows:
                parts.append(f"{len(rows)} 条图像结论"
                             + (f"，{len(bad)} 条没给看了哪张"
                                + ("；且**当前无法看图**（" + cap['detail'][:40] + "）"
                                   if not cap["can_see"] else "")
                                if bad else "，全部有帧号/图名佐证"))
            if cam_rows:
                parts.append(f"机位结论 {len(cam_rows)} 条"
                             + (f"，{len(cam_bad)} 条没给「焦距＋距离＋主体尺寸」三要素"
                                " ⇒ 机位是猜的" if cam_bad else "，三要素齐全"))
            summ = "视觉核验：" + "｜".join(parts)
        rep = {"gate": self.NAME, "ok": ok, "claims": rows, "checked": len(rows),
               "cam_claims": cam_rows, "cam_checked": len(cam_rows),
               "can_see": cap["can_see"],
               "summary": summ}
        self._log({"kind": "report", **{k: v for k, v in rep.items()
                                        if k not in ("claims", "cam_claims")}})
        return rep

    def _log(self, rec: dict) -> None:
        # ★ D6 修复（2026-10-04）：原来 `except Exception: pass` ⇒ 台账写不进去**无声无息**，
        #   闸坏了没人知道（这正是 AGENTS.md「挂名 ≠ 有效：闸坏了等于没闸」的机制来源）。
        #   现在：写失败必须**落到 stderr**，让"闸没记账"这件事可见。
        try:
            with self.ledger.open("a", encoding="utf-8") as f:
                f.write(json.dumps({**rec, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                   ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(
                f"[g_vision] 台账写入失败（不静默吞）：{self.ledger} :: "
                f"{type(e).__name__}: {e}\n")


# --------------------------------------------------------------------------
def demo(cfg: ProjectConfig) -> int:
    vg = VisionGate(cfg)
    fails = 0
    print("### 视觉核验闸 · 实例自检\n")

    cap = vg.capability()
    print("### ① 能力探测（先探能力，再决定说不说『我核过了』）")
    print(f"  模型     ：{cap['model']}")
    print(f"  有凭证   ：{cap['has_key']}")
    print(f"  能看图   ：{cap['can_see']}")
    print(f"  说明     ：{cap['detail'][:90]}")
    print()

    print("### ② 问题分级（细线级不许拿模型结论判废）")
    for q, expect in [("画面里有几个人？坐着还是站着？", "gross"),
                      ("姐姐脸上的机械细线有没有？", "fine"),
                      ("这幅画构图对不对？", "unknown")]:
        got = vg.classify(q)
        mark = "✓" if got == expect else "✗"
        if got != expect:
            fails += 1
        print(f"  {mark} 「{q}」→ {got}（应 {expect}）")
    print()

    print("### ③ 报告级：图像结论必须有据")
    bad = "我看了画面，人形完整，无穿模。画面里一切正常。"
    r1 = vg.check_report(bad)
    print(f"  无帧号的结论 → {'✓拦下' if not r1['ok'] else '✗放行'}")
    for c in r1["claims"]:
        print(f"      {'✗' if not c['ok'] else '✓'} {c['claim'][:44]} — {c['detail'][:50]}")
    if r1["ok"]:
        fails += 1
    print()

    good = "我看了 f48 帧（shot_f48.png），画面里人形完整、手臂在躯干外侧，无穿模。"
    r2 = vg.check_report(good)
    print(f"  带帧号的结论 → {'✓放行' if r2['ok'] else '✗误杀'}")
    if not r2["ok"]:
        fails += 1
    print()

    print("### ④ 真去看一张图（若无法看图，必须明说）")
    # ★★ 2026-10-16 修：这里原来写的是**字面** `<WORKSPACE>\_guard\_vtest.png` ——
    #   占位符没人展开 ⇒ `Image.save` 抛 OSError: Invalid argument ⇒
    #   演示**崩在最后一步**（自检报失败），而前面的用例都"通过"。
    #   这就是"只占位不展开 = 假根治"的第三种表现：不是漏判，是**根本跑不完**。
    img = expand_path("<WORKSPACE>/_guard/_vtest.png")
    img.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image  # type: ignore
        im = Image.new("RGB", (200, 120), (200, 60, 60))
        im.save(img)
        r3 = vg.ask(img, "这张图是什么颜色？")
        if r3["ok"] is None:
            print(f"  ⚠ **无法看图**（如实报，不假装）：{r3['detail'][:80]}")
        elif r3["ok"]:
            print(f"  ✓ 模型答：{r3['answer'][:80]}")
        else:
            print(f"  ✗ {r3['detail'][:80]}")
            fails += 1
        img.unlink(missing_ok=True)
    except ImportError:
        print("  （无 Pillow，跳过真看图测试）")
    print()
    # ★ 2026-10-15 负例（防"触发词再被收窄"复发）：
    #   这两句**都是图像结论**，但只在旧的字面动词清单之外（"看过"≠"看了"、"我"与动词不相邻）
    #   ⇒ 旧版会报"报告无图像结论，跳过"并 exit 0。修成形态判据（对象+动作）后必须拦。
    for txt in ("图像核验：我看过那张图了，没问题。",
                "我把那张渲染图看过了，挺正常的，所以这版可以通过。"):
        rr = vg.check_report(txt)
        hit = rr.get("ok") is False
        print(f"  {'✓✓拦下' if hit else '✗✗漏放'}  形态判据负例：{txt[:26]}")
        if not hit:
            fails += 1
    #   正例（不许误杀）：没提图的普通报告必须照旧跳过
    rp = vg.check_report("我检查了 3 个文件，引用 <WORKSPACE>\AGENTS.md 第 12 行。")
    if rp.get("ok") is True:
        print("  ✓ 不误杀：无图像结论的报告照旧跳过")
    else:
        print(f"  ✗ 误杀：{rp.get('summary')}")
        fails += 1
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

    ap = argparse.ArgumentParser(prog="g_vision")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--capability", action="store_true", help="只探能力")
    ap.add_argument("--image", help="要看的图")
    ap.add_argument("--ask", help="要问的问题")
    ap.add_argument("--text-file", help="检查报告里的图像结论")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    vg = VisionGate(cfg)
    if a.demo:
        sys.exit(demo(cfg))
    if a.capability:
        print(json.dumps(vg.capability(), ensure_ascii=False, indent=1))
        sys.exit(0)
    if a.text_file:
        rep = vg.check_report(Path(a.text_file).read_text(encoding="utf-8", errors="replace"))
        print(rep["summary"])
        sys.exit(0 if rep["ok"] else 1)
    if a.image and a.ask:
        r = vg.ask(a.image, a.ask)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        sys.exit(0 if r["ok"] else 1)
    print("用法：--demo / --capability / --image <图> --ask <问题> / --text-file <报告>")
