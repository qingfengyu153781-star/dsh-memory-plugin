# -*- coding: utf-8 -*-
"""_guard · 判据闸 g_probe —— 报绿之前，先自证探针可信

治的条目：P1-10 P1-11 P1-16 P1-28 P2-23 P4-10 P5-15 P67-13 P67-16 P67-17 P67-32（11 条）
病根：假绿出现 5 次以上——射线打到自己身上、拿台阶当站立基准、用最近点代替插值、
      坐标顺序错、先改 x/y 再测 gap。**每个绿值都在错误前提下算出，却和真绿长得一样。**

根除做法：所有量测必须走统一入口，它强制跑对照：
  · 正例（正确位置）必须绿
  · 负例（故意错位）必须红
  两者不同时成立 ⇒ 抛 ProbeUnreliable，**该判据作废、不许采信**。
  且固定输出：探针起点 / 基准面 / 采样点数 / 判定式 / 负例结果。

跨项目：与领域无关（"数字达标"的任何场合都能挂）；本文件不含项目专有内容。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402


class ProbeUnreliable(RuntimeError):
    """探针自证失败 —— 该判据作废，不许采信。"""


class Probe:
    """一次量测。

    探针要返回的是**判定结果**（绿/红）以及读数，而不是随便一个数——
    因为"假绿"的本质是：探针在错误设置下**照样报绿**。
    probe_fn(correct) -> (verdict, reading, note)
        correct=True  → 真实量测（正确设置）
        correct=False → 对照量测（故意把设置弄错：起点放错、基准面换成台阶、用最近点代替插值…）
    """

    def __init__(self, name: str, probe_fn):
        self.name = name
        self.probe_fn = probe_fn

    def _call(self, correct: bool):
        r = self.probe_fn(correct)
        if isinstance(r, tuple) and len(r) == 3:
            return r  # (verdict, reading, note)
        if isinstance(r, tuple) and len(r) == 2:
            return r[0], r[1], None
        return r, r, None


class ProbeGate:
    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        g = cfg.get("probe") or {}
        self.log_path = cfg.resolve(g.get("ledger", "_guard_ledger/probe.jsonl"))
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.tolerance = float(g.get("tolerance", 1e-6))

    # ------------------------------------------------------------------
    def run(self, probe: Probe, start_point=None, base_plane=None,
            n_samples=None, criterion=None) -> dict:
        """跑一次带自证的量测。自证不过 ⇒ 抛 ProbeUnreliable。"""
        ts = time.strftime("%Y-%m-%d %H:%M:%S")

        # ① 对照（负例）：故意把设置弄错 —— 必须"报红"
        neg_verdict, neg_reading, neg_note = probe._call(False)
        # ② 本次真实量测（正例）
        pos_verdict, pos_reading, pos_note = probe._call(True)

        rec = {
            "probe": probe.name, "ts": ts,
            "start_point": start_point, "base_plane": base_plane,
            "n_samples": n_samples, "criterion": criterion,
            "negative_verdict": neg_verdict, "negative_reading": neg_reading, "negative_note": neg_note,
            "verdict_value": pos_verdict, "value": pos_reading, "value_note": pos_note,
        }

        # 自证判据（这才是"假绿"的定义）：
        #   故意弄错设置时，探针**必须报红**。
        #   如果弄错了还报绿 ⇒ 探针不可信，本次读数作废。
        if str(neg_verdict).lower() not in ("red", "fail", "false", "0"):
            rec["verdict"] = "unreliable"
            rec["reason"] = (f"对照量测（故意错位：{neg_note}）**仍报绿**（{neg_verdict}，读数 {neg_reading}）"
                             f" ⇒ 探针对设置错误不敏感，这就是'假绿'")
            self._log(rec)
            raise ProbeUnreliable(
                f"探针『{probe.name}』自证失败：{rec['reason']}\n"
                f"   输出字段：起点={start_point} 基准面={base_plane} 采样={n_samples} 判定式={criterion}\n"
                f"⇒ 本次读数作废，不许采信；请检查探针起点/基准面是否正确。"
            )
        rec["verdict"] = "reliable"
        self._log(rec)
        return rec

    def _log(self, rec: dict) -> None:
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def render(self, rec: dict) -> str:
        L = [f"探针『{rec['probe']}』 → {rec['value']}  〔{rec['verdict']}〕"]
        L.append(f"   起点 {rec['start_point']} ｜ 基准面 {rec['base_plane']} ｜ 采样 {rec['n_samples']}")
        L.append(f"   判定式 {rec['criterion']} ｜ 对照(故意错位) {rec['negative_verdict']}：{rec['negative_reading']}")
        return "\n".join(L)


def demo(cfg: ProjectConfig) -> int:
    """复刻《示例项目A》S01-1 里真实发生过的两次'假绿'。"""
    g = ProbeGate(cfg)
    fails = 0

    # ==============================================================
    # 复刻 P1-10：射线起点放在路面之下 → 打中台阶 → 把 6.6cm 悬空"修"成 9.4cm，还报绿
    # ==============================================================
    GROUND = -0.106     # 真实站立面（她脚底应落在这里）
    STEP_TOP = -0.060   # 台阶顶面（错误基准）

    def standing_gap_probe(correct: bool):
        """正确：起点在脚底上方、基准面=地面。
           错误（原 bug）：起点放到路面之下，射线先撞上台阶，于是拿台阶顶面当基准。"""
        if correct:
            reading = round(0.5 - GROUND, 4)         # 0.606 → 真实 gap
            return "green", reading, "起点在脚底上方，基准面=地面"
        # 错误设置下，探针**照样判绿**（这就是假绿的本质）
        reading = round(0.5 - STEP_TOP, 4)           # 0.560 → 看着也"贴地"
        return "green", reading, "起点在路面之下，命中台阶顶面"

    print("### 负例 1：复刻 P1-10『射线起点放在路面之下』—— 弄错了还报绿")
    try:
        rec = g.run(Probe("站立面gap", standing_gap_probe),
                    start_point="路面之下（错误）", base_plane="台阶顶面",
                    n_samples=1, criterion="gap = 起点z - 命中点z")
        print("  ✗✗ 没拦住：" + str(rec["value"]))
        fails += 1
    except ProbeUnreliable as e:
        print("  ✓✓ 拦截成功：")
        for line in str(e).splitlines():
            print("     " + line)
    print()

    # ==============================================================
    # 复刻 P67-13：用"中心线最近点"算高差 → 0.87 m 悬差被报成 0（且报绿）
    # ==============================================================
    def joint_gap_probe(correct: bool):
        """正确：在接口 y=16 处插值取 z → Δ=0.94（红，确实没接上）。
           错误：取整条中心线最近点（其实在 y=21）→ Δ=0.00，看着"完全接上"。"""
        if correct:
            return "red", 0.94, "接口处插值，Δz=0.94（没接上）"
        return "green", 0.00, "最近点在 y=21，不是接口 y=16"

    print("### 负例 2：复刻 P67-13『最近点代替插值：0.87 m 悬差报成 0』")
    try:
        rec = g.run(Probe("接口高差", joint_gap_probe),
                    start_point="接口 y=16", base_plane="标准路中心线",
                    n_samples=1, criterion="Δz = 我的路(y=16插值) - 标准路(y=16插值)")
        print(f"  ✗✗ 没拦住：报出 {rec['value']}")
        fails += 1
    except ProbeUnreliable as e:
        print("  ✓✓ 拦截成功：")
        for line in str(e).splitlines()[:4]:
            print("     " + line)
    print()

    # ==============================================================
    # 正例：可信探针 —— 故意弄错时会报红，闸门放行
    # ==============================================================
    def honest_probe(correct: bool):
        if correct:
            return "green", 0.02, "在路面中点量，基准面=地形面"
        return "red", 99.0, "故意把起点放到山体里"

    print("### 正例：可信探针（对照会报红 → 放行，闸门不误杀）")
    try:
        rec = g.run(Probe("路_地形_高差", honest_probe),
                    start_point="路面中点", base_plane="地形面",
                    n_samples=63, criterion="|路面z - 地形z| ≤ 0.05")
        print("  ✓ 放行：")
        print("     " + g.render(rec).replace("\n", "\n     "))
    except ProbeUnreliable as e:
        print("  ✗ 误杀：" + str(e)[:120])
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
    from guard import load as load_cfg

    ap = argparse.ArgumentParser(prog="g_probe")
    ap.add_argument("--project", help="可省：默认从当前目录自动识别")
    ap.add_argument("--project-root", help="可省：默认从当前目录自动识别")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    if a.demo:
        sys.exit(demo(cfg))
    print("用法：--demo 跑负例自检（复刻两次真实假绿）")
