# -*- coding: utf-8 -*-
"""_guard · 语言闸 g_lang —— 用中文交流（用户明令，2026-10-03）

规矩：**与用户的一切交流一律用中文**（正文、表格、结论、报错说明、交付说明）。
      代码/命令/文件路径/API 名/专有名词可保留原文——那是标识符，不是"交流"。

为什么值得做成闸门：
  · 这条已经被用户明令过不止一次（本工作区口径），但**靠记性会漂**：
    模型默认语言受输入影响，一段英文工具输出之后很容易跟着漂成英文。
  · 所以把它做成**可检查的判据**：产出文本里"非中文正文"的比例超限即判不过。

判据（可打印）：
  · 中文字符占比 = 汉字数 / (汉字数 + 拉丁字母词数)
  · 正文门限：≥ 0.60（含代码块与路径的整篇），纯说明段落应 ≥ 0.85
  · 白名单不计入：代码块 ```…```、行内 `code`、URL、文件路径、已登记的技术标识符

跨项目：门限与白名单来自 config.language；本文件不含项目专有内容。
"""
from __future__ import annotations

import re
import sys
import time
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402

CJK = re.compile(r"[\u4e00-\u9fff]")
LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
# 不算"交流语言"的：代码块、行内代码、URL、Windows/Unix 路径
STRIP_PATTERNS = [
    re.compile(r"```.*?```", re.S),        # 代码块
    re.compile(r"`[^`]*`"),                 # 行内代码
    re.compile(r"https?://\S+"),            # URL
    re.compile(r"[A-Za-z]:\\[^\s，。；）)]*"),  # Windows 路径
    re.compile(r"(?:/[\w.\-]+){2,}"),       # Unix 路径
]


class LanguageViolation(RuntimeError):
    """产出语种不合规 —— 打回重写，不许交付。"""


class LangGate:
    def __init__(self, cfg: ProjectConfig):
        lang = cfg.language or {}
        self.required = str(lang.get("required", "zh")).lower()
        self.threshold = float(lang.get("min_cjk_ratio", 0.60))
        self.strict_threshold = float(lang.get("min_cjk_ratio_prose", 0.85))
        self.allowlist = list(lang.get("allowlist") or [])
        g = cfg.get("lang") or {}
        self.log_path = cfg.resolve(g.get("ledger", "_guard_ledger/lang.jsonl"))
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def _strip(self, text: str) -> str:
        out = text
        for p in STRIP_PATTERNS:
            out = p.sub(" ", out)
        for w in self.allowlist:
            out = out.replace(w, " ")
        # 表格分隔线、标点
        out = re.sub(r"[-|:>\s]{2,}", " ", out)
        return out

    def ratio(self, text: str) -> dict:
        body = self._strip(text)
        cjk = len(CJK.findall(body))
        latin = len(LATIN_WORD.findall(body))
        denom = cjk + latin
        r = (cjk / denom) if denom else 1.0
        return {"cjk_chars": cjk, "latin_words": latin, "ratio": round(r, 4)}

    # ------------------------------------------------------------------
    # ★★ 2026-10-04 修（判据量错对象 · 死配置）：
    #   档头明写「纯说明段落应 ≥ 0.85」，`__init__` 也读了 `strict_threshold=0.85`，
    #   但 **`check()` / `enforce()` 从头到尾只用 `threshold`(0.60)**
    #   ⇒ `strict_threshold` 是一条**读了从不用的死配置**，
    #     档头承诺的"纯说明段更严"**从未生效**（文件说判 A，代码只量 B）。
    #   实测形态：一份"中文标题 + 英文结论段"的汇报，整篇 ratio=0.5556 被 0.60 拦下，
    #   但若正文更长（稀释分母）就能以"整篇达标"的名义把**整段英文说明**带过去。
    #   修法：把"整篇"与"纯说明段"两个口径**都真的量**，取更严者。
    def prose_ratio(self, text: str) -> dict:
        """量**纯说明段落**（去掉代码块/表格/列表/标题行）的中文占比。

        为什么要单独量：整篇比例会被大段中文正文稀释 ——
        一段 200 字的英文"结论/说明"夹在 2000 字中文里，整篇照样 >0.9，
        但它恰恰是"与用户交流"的那部分。整篇达标 ≠ 每段达标（判据量错对象）。
        """
        kept = []
        in_code = False
        for line in text.split("\n"):
            s = line.strip()
            if s.startswith("```"):
                in_code = not in_code
                continue
            if in_code or not s:
                continue
            if s.startswith(("#", "|", "-", "*", ">", "+")) or re.match(r"^\d+[.)]\s", s):
                continue
            kept.append(line)
        body = self._strip("\n".join(kept))
        cjk = len(CJK.findall(body))
        latin = len(LATIN_WORD.findall(body))
        denom = cjk + latin
        r = (cjk / denom) if denom else 1.0
        return {"cjk_chars": cjk, "latin_words": latin, "ratio": round(r, 4),
                "n_lines": len(kept)}

    def check(self, text: str, stage: str = "产出") -> dict:
        m = self.ratio(text)
        p = self.prose_ratio(text)
        # 两个口径都要过：整篇看总量，纯说明段看"人真正读的那部分"
        ok_whole = m["ratio"] >= self.threshold
        # 纯说明段样本太少（<40 字）时该口径无统计意义，不作数（避免误杀短产出）
        prose_enough = (p["cjk_chars"] + p["latin_words"]) >= 40
        ok_prose = (p["ratio"] >= self.strict_threshold) if prose_enough else True
        ok = ok_whole and ok_prose
        rec = {"stage": stage, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "required": self.required, "threshold": self.threshold,
               "strict_threshold": self.strict_threshold,
               "measured": m, "prose": p,
               "ok_whole": ok_whole, "ok_prose": ok_prose, "prose_enough": prose_enough,
               "ok": ok}
        self._log(rec)
        return rec

    def enforce(self, text: str, stage: str = "产出") -> dict:
        rec = self.check(text, stage)
        if not rec["ok"]:
            m, p = rec["measured"], rec["prose"]
            if not rec["ok_whole"]:
                why = (f"整篇中文字符占比 {m['ratio']:.1%}（下限 {self.threshold:.0%}）"
                       f"—— 汉字 {m['cjk_chars']} 个、拉丁词 {m['latin_words']} 个")
            else:
                why = (f"**纯说明段落**中文字符占比 {p['ratio']:.1%}"
                       f"（下限 {self.strict_threshold:.0%}）"
                       f"—— 该段汉字 {p['cjk_chars']} 个、拉丁词 {p['latin_words']} 个"
                       f"（整篇 {m['ratio']:.1%} 达标，但被中文正文稀释了）")
            raise LanguageViolation(
                f"[{stage}] 语言闸不过：{why}。\n"
                f"   规矩：与用户的一切交流一律用中文（代码/路径/API 名可保留原文）。\n"
                f"⇒ 打回重写，不许交付。"
            )
        return rec

    def _log(self, rec: dict) -> None:
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def demo(cfg: ProjectConfig) -> int:
    g = LangGate(cfg)
    fails = 0

    samples = [
        ("负例 1：整段英文汇报（漂了）",
         "The first shot has been rendered successfully. All five gates passed. "
         "The camera elevation is now correct and the character walks up the mountain road. "
         "I have verified the contact sheet and every frame looks fine. Ready to deliver.",
         False),
        ("负例 2：一半英一半中（更常见）",
         "我已经完成了第一镜的重建。The mesh has been regenerated with 6720 faces. "
         "相机仰角已修正为 +6.84 度。The road is now aligned to the terrain and no longer floating.",
         False),
        ("正例 1：中文汇报（含中文主体）",
         "第一镜已重建完成：山体主坡 6720 面，相机站在山上、仰角 +6.84 度（符合分镜的『略仰』），"
         "她垂手背对镜头沿山道往上走，背上挂着旧酒壶。接触表 5 帧已拼好，每格 960×540，"
         "五道闸门全过，可以交付。",
         True),
        ("正例 2：中文正文 + 代码块/路径（不该误杀）",
         "已把快照挂到写操作上，回档是一条命令：\n"
         "```bash\npython guard_cli.py vault-rollback 12\n```\n"
         "文件落在 `D:\\agent\\_guard\\gates\\g_vault.py`，回档后自动比对 md5，一致才算成功。",
         True),
        # ★★ 2026-10-04 新增（治"整篇达标就放行"这个真实缺口）：
        #   形态 = 大段中文正文把一整段英文结论"稀释"掉 ⇒ 整篇 ratio 达标、照样交付。
        #   出处：本闸档头自己写的「纯说明段落应 ≥ 0.85」——
        #   原文`strict_threshold` 读了从不用 ⇒ 这条承诺是空的（判据量错对象）。
        ("负例 3：中文正文稀释掉一整段英文结论（整篇达标、纯说明段不达标）",
         "本镜已完成重建。山体主坡 6720 面，相机站在山上、仰角 +6.84 度，符合分镜的『略仰』。"
         "她垂手背对镜头沿山道往上走，背上挂着旧酒壶。接触表 5 帧已拼好，每格 960×540。"
         "石磴每级四角 gap 0.015 米，路与地形高差 0.02 米，都在公差内。地形面已经归零，"
         "路面中点的读数复算过两遍，结果一致。人物占画高 0.22，件在框率 1.0，帧数 120，"
         "时长 5.0 秒。以上读数全部来自实测，不是估计。渲染用的帧号和文件夹对得上，"
         "交付前又核对了一次接触表，五格都能看清人形与山道的相对位置关系。\n\n"
         "Here is the summary of what was done in this shot. The character mesh was rebuilt "
         "from the blockout stage and the camera elevation was adjusted to match the original "
         "storyboard intent of a slightly upward angle. The road surface was realigned to the "
         "terrain so the previous floating issue is completely gone. All five gates passed and "
         "I have verified the contact sheet frame by frame before declaring this shot done.",
         False),
    ]

    for title, text, should_pass in samples:
        print(f"### {title}")
        try:
            rec = g.enforce(text, stage=title)
            got = "放行"
            print(f"  → {got}：整篇 {rec['measured']['ratio']:.1%}"
                  f"（汉字 {rec['measured']['cjk_chars']} / 拉丁词 {rec['measured']['latin_words']}）"
                  f"｜纯说明段 {rec['prose']['ratio']:.1%}")
            if not should_pass:
                print("  ✗✗ 没拦住（该拦的放行了）")
                fails += 1
        except LanguageViolation as e:
            got = "拦下"
            print("  ✓✓ 拦截成功：" + str(e).splitlines()[0])
            if should_pass:
                print("  ✗ 误杀（该放行的被拦了）")
                fails += 1
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

    ap = argparse.ArgumentParser(prog="g_lang")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--text-file", help="检查一个文本文件的语言合规性")
    a = ap.parse_args()
    cfg = load(a.project, a.project_root)
    if a.demo:
        sys.exit(demo(cfg))
    if a.text_file:
        t = Path(a.text_file).read_text(encoding="utf-8", errors="replace")
        try:
            r = LangGate(cfg).enforce(t, stage=a.text_file)
            print(f"语言闸通过：中文占比 {r['measured']['ratio']:.1%}")
            sys.exit(0)
        except LanguageViolation as e:
            print(str(e))
            sys.exit(2)
    print("用法：--demo 负例自检 / --text-file <文件> 检查某份产出")
