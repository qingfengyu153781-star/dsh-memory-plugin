# -*- coding: utf-8 -*-
"""_guard · 视觉标注 vannot —— 让人眼判，必须先告诉他"看哪儿"

## 为什么有这个东西（2026-10-03 用户点名）

用户原话：「**细线级细节走人眼的话我希望加入一个功能就是给图片上框出来
不然人也不知道你指的地方是哪里**」

**问题**：报告写「请人眼确认姐姐脸上的机械细线」—— 但中景图 1920×1080 里，
"脸上"是哪一块？人打开图**根本不知道往哪儿看**。这是一条无效指令。

**治法**：凡要人眼判的，必须给出**带标注的图**：
  · 在图上**画框**（红框 + 编号）
  · 给出**坐标与尺寸**（可复算）
  · 可选**放大裁切**（细线级细节在中景里看不清，放大才看得见）
  · 编号与问题一一对应（问 3 个地方就标 ①②③）

## 实测教训（表里 P 系列，本文件的设计依据）
  · **放大也可能白搭**：低对比亚像素细节"放大只是把看不清放大"
    → 所以标注图必须**同时给原图带框 + 放大图**，且**明显提示"看不清就说看不清"**
  · **细线级细节模型读不出来，人眼定**：模型答"没有"不等于真没有

## 用法
```python
from gates.vannot import annotate, Region
# 让模型先给粗略位置，再标出来给人眼定
regions = [Region(x=812, y=330, w=180, h=140, label="1", note="左脸疑似机械线")]
out = annotate("shot_f48.png", regions, out_dir="_guard/_anno")
# out = {"marked": "...带框图.png", "crops": ["...放大1.png"], "manifest": {...}}
```

跨项目：样式与阈值来自 config.vannot；本文件不含项目专有内容。
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore
except ImportError:  # pragma: no cover
    Image = None  # type: ignore


BOX_COLOR = (255, 40, 40)          # 红框（醒目）
LABEL_BG = (255, 40, 40)
TEXT_COLOR = (255, 255, 255)
CROP_PAD = 1.6                     # 裁切放大时四周留边倍数
MIN_CROP = 220                     # 放大图最短边（太小看不清）


@dataclass
class Region:
    """要人眼看的一块区域。

    x,y,w,h 用**原图像素坐标**（左上角 + 宽高）。
    label 是图上的编号（1/2/3… 或 A/B/C）。
    note 是这块要看什么（一句话）。
    """
    x: int
    y: int
    w: int
    h: int
    label: str = "1"
    note: str = ""

    def box(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.w, self.y + self.h)


@dataclass
class AnnoResult:
    src: str = ""
    marked: str = ""
    crops: list[str] = field(default_factory=list)
    regions: list[dict] = field(default_factory=list)
    ok: bool = False
    detail: str = ""


def _font(size: int = 26):
    for name in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
                 "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def annotate(image: str | Path, regions: list[Region], *,
             out_dir: str | Path = "_guard/_anno",
             do_crop: bool = True,
             max_edge: int = 1600) -> AnnoResult:
    """在图上画框 + 编号，并（可选）输出放大裁切图。

    返回 AnnoResult：marked = 带框图；crops = 逐区域的放大图。
    """
    res = AnnoResult(src=str(image))
    if Image is None:
        res.detail = "**无法标注**：Pillow 不可用（装 pillow 后重试）"
        return res
    p = Path(image)
    if not p.exists():
        res.detail = f"图片不存在：{p}"
        return res
    if not regions:
        res.detail = "**未给任何区域** —— 没有框，人不知道往哪儿看（这就是本条要治的病）"
        return res

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    im = Image.open(p).convert("RGB")
    W, H = im.size

    # 越界区域先裁到图内（并记录，避免"框在图外"这种无声错误）
    clean: list[Region] = []
    for r in regions:
        x0, y0 = max(0, r.x), max(0, r.y)
        x1, y1 = min(W, r.x + r.w), min(H, r.y + r.h)
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        clean.append(Region(x0, y0, x1 - x0, y1 - y0, r.label, r.note))
    if not clean:
        res.detail = "**所有区域都在图外** —— 坐标给错了（请核对原图尺寸）"
        return res

    # ① 带框图（超长边则缩，但保证框还看得见）
    marked_im = im.copy()
    d = ImageDraw.Draw(marked_im)
    lw = max(3, min(W, H) // 240)
    fs = max(20, min(W, H) // 40)
    font = _font(fs)
    for r in clean:
        d.rectangle(r.box(), outline=BOX_COLOR, width=lw)
        # 编号贴在框左上角外侧（框贴边时贴内侧）
        tx = r.x if r.y >= fs + 8 else r.y and r.x
        ty = r.y - fs - 6 if r.y - fs - 6 > 0 else r.y + 4
        label = str(r.label)
        try:
            tw = d.textlength(label, font=font)
        except Exception:  # noqa: BLE001
            tw = fs
        d.rectangle((tx, ty, tx + tw + 10, ty + fs + 6), fill=LABEL_BG)
        d.text((tx + 5, ty + 2), label, fill=TEXT_COLOR, font=font)

    if max(marked_im.size) > max_edge:
        marked_im.thumbnail((max_edge, max_edge))
    marked_path = out / (p.stem + "_标注.png")
    marked_im.save(marked_path)
    res.marked = str(marked_path)

    # ② 逐区域放大裁切（细线级细节在中景里看不清）
    if do_crop:
        for r in clean:
            cx0 = max(0, int(r.x - r.w * (CROP_PAD - 1) / 2))
            cy0 = max(0, int(r.y - r.h * (CROP_PAD - 1) / 2))
            cx1 = min(W, int(r.x + r.w + r.w * (CROP_PAD - 1) / 2))
            cy1 = min(H, int(r.y + r.h + r.h * (CROP_PAD - 1) / 2))
            crop = im.crop((cx0, cy0, cx1, cy1))
            # 放大到最短边 ≥ MIN_CROP
            sc = max(1.0, MIN_CROP / max(1, min(crop.size)))
            if sc > 1.0:
                crop = crop.resize((int(crop.width * sc), int(crop.height * sc)),
                                   Image.LANCZOS)
            cp = out / f"{p.stem}_放大{r.label}.png"
            crop.save(cp)
            res.crops.append(str(cp))

    res.regions = [asdict(r) for r in clean]
    res.ok = True
    res.detail = (f"已标注 {len(clean)} 个区域：{res.marked}"
                  + (f"（含 {len(res.crops)} 张放大图）" if res.crops else ""))
    return res


def question_sheet(image: str | Path, regions: list[Region],
                   questions: dict[str, str] | None = None) -> str:
    """生成给**人**看的一页纸：图在哪、框在哪、要判什么。

    用户点名要的正是这个 —— 光给图不行，得说清"编号 ↔ 问题 ↔ 坐标"。
    """
    p = Path(image)
    lines = [f"# 请人眼判读：{p.name}", ""]
    if not regions:
        lines.append("**警告**：未给任何区域 —— 无框的请求是无效的，人不知道往哪儿看。")
        return "\n".join(lines)
    lines.append("| 编号 | 坐标(左上 x,y) | 尺寸 | 要看什么 |")
    lines.append("|---|---|---|---|")
    for r in regions:
        q = (questions or {}).get(str(r.label), r.note or "（未写）")
        lines.append(f"| **{r.label}** | {r.x},{r.y} | {r.w}×{r.h} | {q} |")
    lines += [
        "",
        "## 怎么判",
        "1. 先看**带框图**（红框就是位置，编号与上表对应）",
        "2. 再看**放大图**（细线级细节在中景里看不清，放大才看得见）",
        "3. **看不清就说看不清** —— 别勉强给结论（放大可能只是把『看不清』放大）",
        "",
        "## 为什么要你判（模型判不了这类）",
        "细线/接缝/颗粒/虹膜反光属**亚像素级低对比**细节，视觉模型读不出来；",
        "它答『没有』**不等于真没有** —— 所以这类必须人眼定。",
    ]
    return "\n".join(lines)


def demo(cfg=None) -> int:
    fails = 0
    print("### 视觉标注 · 实例自检\n")
    if Image is None:
        print("  （无 Pillow，跳过）")
        return 0

    tmp = Path(r"<WORKSPACE>\_guard\_anno_test")
    tmp.mkdir(parents=True, exist_ok=True)
    # 造一张"中景人像"假图：有头/身，头上有几条细线
    im = Image.new("RGB", (1280, 720), (70, 74, 82))
    d = ImageDraw.Draw(im)
    d.rectangle((560, 300, 720, 660), fill=(120, 118, 112))       # 身体
    d.ellipse((585, 190, 695, 310), fill=(196, 172, 150))          # 头
    for i in range(4):                                             # 脸上的细线
        y = 230 + i * 12
        d.line((605, y, 675, y + 3), fill=(30, 30, 30), width=1)
    src = tmp / "fake_midshot.png"
    im.save(src)

    print("### ① 不给区域（应明确拒绝并说明理由）")
    r0 = annotate(src, [], out_dir=tmp)
    if not r0.ok and "不知道往哪儿看" in r0.detail:
        print(f"  ✓ 拒绝：{r0.detail[:70]}")
    else:
        print(f"  ✗ 应拒绝却通过了"); fails += 1
    print()

    print("### ② 给区域（应画框 + 出放大图）")
    regions = [Region(595, 195, 100, 110, label="1", note="脸部：是否有机械细线")]
    r1 = annotate(src, regions, out_dir=tmp)
    if r1.ok and Path(r1.marked).exists() and r1.crops and Path(r1.crops[0]).exists():
        print(f"  ✓ 带框图：{Path(r1.marked).name}")
        print(f"  ✓ 放大图：{[Path(c).name for c in r1.crops]}")
    else:
        print(f"  ✗ 失败：{r1.detail[:80]}"); fails += 1
    print()

    print("### ③ 越界区域（应裁进图内或明确报错）")
    r2 = annotate(src, [Region(5000, 5000, 100, 100, label="9")], out_dir=tmp)
    if not r2.ok and "图外" in r2.detail:
        print(f"  ✓ 明确报错：{r2.detail[:60]}")
    else:
        print(f"  ✗ 应报错"); fails += 1
    print()

    print("### ④ 给人看的一页纸（编号↔坐标↔问题）")
    sheet = question_sheet(src, regions, {"1": "脸上有没有黑色机械细线？"})
    has_all = all(k in sheet for k in ["595,195", "100×110", "机械细线", "看不清就说看不清"])
    print("  " + ("✓ 编号/坐标/问题/免责俱全" if has_all else "✗ 内容不全"))
    if not has_all:
        fails += 1
        print(sheet[:400])
    print()

    print(f"### 自检结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}")
    print(f"（测试图与标注留在 {tmp}，可打开看）")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(prog="vannot")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--image", help="要标注的图")
    ap.add_argument("--boxes", help="区域，格式 x,y,w,h,label,note;x,y,w,h,label,note")
    ap.add_argument("--out-dir", default="_guard/_anno")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.demo:
        sys.exit(demo())
    if a.image and a.boxes:
        regs = []
        for part in a.boxes.split(";"):
            f = part.split(",")
            if len(f) >= 4:
                regs.append(Region(int(f[0]), int(f[1]), int(f[2]), int(f[3]),
                                   f[4] if len(f) > 4 else "1",
                                   f[5] if len(f) > 5 else ""))
        r = annotate(a.image, regs, out_dir=a.out_dir)
        if a.json:
            print(json.dumps(asdict(r), ensure_ascii=False, indent=1))
        else:
            print(r.detail)
            for c in r.crops:
                print("  放大：" + c)
        sys.exit(0 if r.ok else 1)
    ap.print_help()
