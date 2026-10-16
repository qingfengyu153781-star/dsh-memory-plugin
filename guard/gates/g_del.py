# -*- coding: utf-8 -*-
"""_guard · 删除依赖清零闸 g_del —— 治「删除前不看谁在用」（拆书复盘病根 7，四撞红线）

## 为什么必须做成闸
复盘实证（<会话> 主线 G）——同型红线一年内撞了 4 次：
  · 11:17 rm -rf 五目录 ⇒ Device or resource busy（Ep09 正被另一会话解码，对方批次被腰斩）
    —— 且 dl-novel 事前已预警"请确认它已停再删"，Lead 自己的裁定也写明条件，仍未等确认（P-52-2）
  · 02:51 删 Ep10：全工作区唯一副本、三条线在跑、两分钟前刚下令对它抽帧核对（P-33）
  · 11:17 删小圆素材 ⇒ dl-short 的裁决二复核被直接阻塞；Lead 自认"我又犯了"（§9 二次复发 P-56-1）
  · 09:03 请示删已拆素材 ⇒ 直接触发 09:11 暴怒（P-38-9）
文档层（AGENTS.md 红线"归档/移动共享目录不看谁正在写"）复犯 ⇒ 不根治。
根治＝删除决定**写盘时**物理过闸（hook 自动路），CLI 模式做 pending 实对账。

## 三件套声明（删除决定文本里缺任一 ⇒ 拦）
  ① 全队 pending 已清零（附证据：谁核的/何时）
  ② 唯一副本核查（非唯一｜唯一但无任何线引用）
  ③ 对照物核查（跨话对照/剧场版对照/后续复验确认不再需要该素材）

## CLI 实对账（删除前必跑）
  g_del.py --targets "pathA;pathB" --pending-file <全队 pending 文本>
  ⇒ pending 文本里任何「正在/解码/复核/抽帧/拉片/待裁」+ 目标路径共现 ⇒ 拦（谁在用列名）

## demo 负例＝复盘真实事件
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
GATES_DIR = Path(__file__).resolve().parent

# ★ 删除动词：**不加 `\b`** —— `\b` 是 `\w` 边界，而中文在 re 里算 `\w`，
#   `\b删除\b` 在"本批删除已拆素材"这种前后都是中文的句子里**匹配不上**（实测漏拦）。
# ★ 补入"以腾空间为由"的形态（`先删`/`腾磁盘`/`腾空间`/`释放空间`）——
#   2026-10-15 实测漏拦句正是「本批**先删**已拆素材**腾磁盘**（当时 pending 挂着…）」：
#   旧判据连"这是删除决定"都没认出来，直接 skip ⇒ 三件套检查根本没跑。
DEL_VERBS = re.compile(
    r"rm\s+-rf|删除|删掉|先删|该删|清理|清空|移除|腾磁盘|腾空间|腾出空间|释放磁盘|释放空间",
    re.I)
NEED1 = re.compile(r"全队\s*pending\s*已清零|pending\s*清单\s*归零|无任何线(?:在用|引用)", re.I)
NEED2 = re.compile(r"唯一副本核查|非唯一副本|唯一(?:副本)?\s*但\s*(?:无任何线|零引用)", re.I)
NEED3 = re.compile(r"对照物核查|跨话对照(?:已确认)?不?再?需要|复验(?:确认)?不?再?需要|对照(?:需求)?已清", re.I)
# pending 共现判据（CLI 实对账用）：活动动词与目标名同现
ACTIVE_PAT = re.compile(r"(正在|解码中?|复核|抽帧|拉片|待裁|待验|排队|在跑|挂着)")
NAME_EXTRACT = re.compile(r"[/\\]?([^/\\]+?)(?:\.(?:mkv|mp4|avi|txt|md|json))?$")

def check_decision_text(text: str) -> dict:
    """hook 自动路：删除决定文本过闸（缺三件套 ⇒ ok=False）。"""
    if not DEL_VERBS.search(text):
        return {"ok": True, "summary": "非删除决定文本，不拦", "mode": "skip"}
    missing = []
    if not NEED1.search(text):
        missing.append("①全队 pending 已清零（证据）")
    if not NEED2.search(text):
        missing.append("②唯一副本核查")
    if not NEED3.search(text):
        missing.append("③对照物核查（跨话/剧场版/复验不再需要）")
    if missing:
        return {"ok": False,
                "summary": "删除决定缺三件套声明：%s —— 当年四撞红线全是这个形态（busy 腰斩/唯一副本/§9 二次复发/裁决二被阻塞）。补齐声明并实跑 g_del --targets --pending-file 对账后再删。" % "、".join(missing),
                "missing": missing}
    return {"ok": True, "summary": "删除决定三件套齐全（另须实对账：g_del --targets --pending-file）"}

def check_pending(targets: list[str], pending_text: str) -> dict:
    """CLI 实对账：活动动词+目标名在 pending 里共现 ⇒ 拦。"""
    conflicts = []
    for t in targets:
        base = Path(t).name or t
        m = NAME_EXTRACT.match(base)
        key = (m.group(1) if m else base).strip()
        if not key:
            continue
        for line in pending_text.splitlines():
            if key in line and ACTIVE_PAT.search(line):
                conflicts.append({"target": t, "key": key, "line": line.strip()[:120]})
                break
    if conflicts:
        return {"ok": False, "summary": "删除被拦：%d 个目标正在被使用（pending 共现）——当年 11:17 就是这么腰斩别人的解码批次" % len(conflicts),
                "conflicts": conflicts}
    return {"ok": True, "summary": "pending 无共现占用（另须三件套声明 + 唯一副本核查通过）"}

# ── 类式 API（hook 自动路）──
class DelGate:
    NAME = "g_del"

    def __init__(self, cfg=None):
        self.cfg = cfg or {}

    def check_text(self, text: str) -> dict:
        try:
            return check_decision_text(text)
        except Exception as e:  # noqa: BLE001
            return {"ok": None, "summary": "g_del 异常（%s）——未核，不假装在跑" % e}

# ── demo（负例＝复盘真实事件）──
def cmd_demo() -> int:
    NEG = [
        ("rm -rf dl_trix_full 等 5 个小圆源目录——理由：报告已全部落盘（当时 dl-short 正用 Trix 1080p 做裁决二复核，P-56-1）",
         "文本层：缺三件套（当年 §9 二次复发原句）"),
        ("删除 Ep10 素材——已拆完按规范删（当时它全工作区唯一副本、三条线在跑、2 分钟前刚下令抽帧核对，P-33）",
         "文本层：缺三件套（唯一副本原句）"),
        ("本批先删已拆素材腾磁盘（当时 pending 挂着剧场版对照需 TV 全集，P-48-5）",
         "文本层：缺三件套（先删后补矛盾原句）"),
    ]
    POS = [
        "删除 dl_ep03 临时目录：全队 pending 已清零（2026-10-07 list_agents+grep 核）；唯一副本核查：非唯一（另有 batch1 存档）；对照物核查：跨话对照已确认不再需要（§15 登记）",
    ]
    PENDING_NEG = [
        ("dl_trix_full", "dl-short：我正在用 dl_trix_full/Trix 1080p 素材做第 9 话裁决二复核，还差 2 项未回"),
        ("Ep10", "deep-madoka-movie：Ep10 切点表抽帧核对进行中（第 3 批）"),
    ]
    ok = True
    print("== g_del --demo（负例＝复盘真实事件）==")
    for text, why in NEG:
        r = check_decision_text(text)
        mark = "✓拦" if r["ok"] is False else "✗漏！"
        if r["ok"] is not False:
            ok = False
        print("  %s ｜『%s』｜ %s" % (mark, text[:38], why))
    for text in POS:
        r = check_decision_text(text)
        mark = "✓过" if r["ok"] is True else "✗误拦"
        if r["ok"] is not True:
            ok = False
        print("  %s ｜『%s』｜ 三件套齐全正例" % (mark, text[:38]))
    for tgt, pend in PENDING_NEG:
        r = check_pending([tgt], pend)
        mark = "✓拦" if r["ok"] is False else "✗漏！"
        if r["ok"] is not False:
            ok = False
        print("  %s ｜CLI对账：目标 %s vs pending『%s』" % (mark, tgt, pend[:30]))
    r = check_pending(["dl_ep03_temp"], "全队 idle；无任何活动动词共现")
    mark = "✓过" if r["ok"] is True else "✗误拦"
    if r["ok"] is not True:
        ok = False
    print("  %s ｜CLI对账正例：无共现" % mark)
    print("== 结果：%s ==" % ("✅ demo 全绿（复盘三起真实删除决定 + 两起 pending 占用全部拦住）" if ok else "🔴 有漏拦——回炉"))
    return 0 if ok else 1

def main():
    args = sys.argv[1:]
    if not args or args[0] in ("--demo", "demo"):
        return cmd_demo()
    if args[0] == "--text-file":
        text = Path(args[1]).read_text(encoding="utf-8")
        r = check_decision_text(text)
        print(r["summary"] if r["ok"] else "🔴 " + r["summary"])
        return 0 if r["ok"] else 1
    if args[0] == "--targets":
        try:
            targets = [t.strip() for t in args[1].split(";") if t.strip()]
            pf = args[args.index("--pending-file") + 1]
            pending = Path(pf).read_text(encoding="utf-8")
        except (IndexError, ValueError):
            print("用法：--targets 'a;b' --pending-file <文件>")
            return 2
        r = check_pending(targets, pending)
        if r["ok"]:
            print("✓ " + r["summary"])
        else:
            print("🔴 " + r["summary"])
            for c in r["conflicts"]:
                print("   目标 %s ← %s" % (c["target"], c["line"]))
        return 0 if r["ok"] else 1
    print("用法：g_del.py [--demo | --text-file F | --targets 'a;b' --pending-file F]")
    return 2

if __name__ == "__main__":
    sys.exit(main())
