# -*- coding: utf-8 -*-
"""_guard · 集成回归闸 g_integ —— 改动命中声明的面 ⇒ **自动跑**该项目的集成回归（R-64）

## 为什么要有它（起因是一条真实的漏）

2026-10-15 把灵枢脑**融合**进 `dsh-memory-plugin` 后，我写了端到端回归
`plugin-src/dsh-memory-plugin/test/brain_fusion.mjs`（10 条断言：融合接上没？块是不是**一个**？
验证态有没有被四态资格冒充？脑不可用时有没有**写明原因**？）。

写完当天我是**手敲** `node test/brain_fusion.mjs` 跑通的 —— 到了第二天复核才发现：
该项目的 `package.json` 里 `test` 脚本是
`selfcheck && inject && orch_regress`，**唯独没有这条融合回归**。
⇒ 也就是说：**这条回归下次会靠人记得跑**，忘了就等于没有。

这跟本工作区已经反复栽过的坑是**同一个形态**：
「**规矩写下了 ≠ 有东西在跑它**」（`_guard` 的存在理由就是这句）。
所以不补文档、补闸：**改动命中声明面 ⇒ 自动跑；不命中 ⇒ 显式写"未跑"（不许静默当绿）**。

## 判据

| id | 判据 | 量的对象 |
|---|---|---|
| I1 | 声明的每个集成项，其 `cmd` 退出码必须 **0** | 真实命令的退出码 |
| I2 | 改动文件命中某集成项的 `patterns` ⇒ 该项**必须真跑**（跑不了 ⇒ 红，不许当"跳过"） | 改动清单 × 声明面 |
| I3 | 未命中的集成项 ⇒ 报告里显式写 **skipped + 原因**（"未跑"≠"通过"） | 报告文本 |
| I4 | 命令超时 ⇒ 红（并写明超时秒数），不许无限等 | 墙钟 |

## 配置

`_guard/tools/integrations.json`：

```json
{"integration": [{"name": "插件全量回归", "patterns": ["plugin-src/"],
                  "cwd": "D:\\\\...", "cmd": ["npm", "test"], "timeout_seconds": 600}]}
```

## 用法

    python _guard/gates/g_integ.py --changed "a.py;b.js"   # 命中即跑
    python _guard/gates/g_integ.py --all                   # 全跑
    python _guard/gates/g_integ.py --demo                  # 自证：坏命令必红、好命令必绿

退出码：0 = 全过（含"未命中"）；1 = 有集成项失败/超时/该跑却跑不了。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from guard import ProjectConfig  # noqa: E402
from tool_paths import expand_path  # noqa: E402  ★ 展开 <WORKSPACE>/<HOME>

HERE = Path(__file__).resolve().parent.parent
CFG = HERE / "tools" / "integrations.json"


def load_cfg() -> dict:
    try:
        if CFG.is_file():
            return json.loads(CFG.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        # ★ 配置坏了 ⇒ **不能当"没有集成项"**（那会静默放行）。显式报出来。
        print("[g_integ] 配置读不出（%s）：%r ⇒ 本次判**红**（不许静默放行）" % (CFG, exc),
              file=sys.stderr)
        return {"_broken": str(exc), "integration": []}
    return {"integration": []}


def _matched(patterns: list[str], changed: list[str]) -> list[str]:
    """改动清单里命中声明面的项（大小写不敏感、分隔符归一）。"""
    norm = [str(c).replace("\\", "/").lower() for c in (changed or [])]
    out = []
    for p in patterns or []:
        q = str(p).replace("\\", "/").lower()
        for c in norm:
            if q in c:
                out.append(c)
    return sorted(set(out))


class IntegGate:
    NAME = "g_integ"

    def __init__(self, cfg: ProjectConfig | None = None):
        self.cfg = cfg
        self.reg = load_cfg()

    def check(self, changed: list[str] | None = None, force_all: bool = False) -> dict:
        items = self.reg.get("integration") or []
        if self.reg.get("_broken"):
            return {"ok": False, "rows": [], "summary":
                    "集成回归闸：**配置坏了**（%s）⇒ 判红，不许当'没有集成项'" % self.reg["_broken"]}
        if not items:
            return {"ok": True, "rows": [], "ran": 0, "skipped": 0,
                    "summary": "集成回归闸：未声明集成项（配置 %s 为空）" % CFG.name}
        rows, bad, ran, skipped = [], [], 0, 0
        for it in items:
            name = it.get("name") or "(未命名)"
            hit = _matched(it.get("patterns") or [], changed or [])
            if not (force_all or hit):
                skipped += 1
                rows.append({"name": name, "ok": None, "skipped": True,
                             "detail": "未命中（改动 %d 个文件都没落在声明的面上）" % len(changed or [])})
                continue
            cmd = it.get("cmd") or []
            if not cmd:
                bad.append("%s：声明了集成项但没给 cmd" % name)
                rows.append({"name": name, "ok": False, "detail": "缺 cmd"})
                continue
            ran += 1
            tmo = float(it.get("timeout_seconds", 600))
            try:
                argv = [str(x) for x in cmd]
                if os.name == "nt":
                    # ★ Windows：`npm` 实际是 `npm.cmd`，**CreateProcess 直调不到**
                    #   （实测 FileNotFoundError）。先 which 解析，再经 cmd.exe 起。
                    argv[0] = shutil.which(argv[0]) or argv[0]
                    r = subprocess.run(subprocess.list2cmdline(argv), shell=True,
                                       cwd=str(expand_path(it["cwd"])) if it.get("cwd") else None, capture_output=True,
                                       text=True, encoding="utf-8", errors="replace",
                                       timeout=tmo)
                else:
                    r = subprocess.run(argv, cwd=str(expand_path(it["cwd"])) if it.get("cwd") else None,
                                       capture_output=True,
                                       text=True, encoding="utf-8", errors="replace",
                                       timeout=tmo)
            except subprocess.TimeoutExpired:
                # ★ 不静默：失败既进报告（bad/rows），也打 stderr —— 只进报告不够
                #   （"只往返回体里塞一个字段"正是静默失效的形态）。
                print("[g_integ] %s **超时**（%.0fs）：%s" % (name, tmo, cmd), file=sys.stderr)
                bad.append("%s：**超时**（%.0fs）" % (name, tmo))
                rows.append({"name": name, "ok": False, "detail": "超时 %.0fs" % tmo})
                continue
            except Exception as exc:  # noqa: BLE001
                print("[g_integ] %s 跑不起来：%r" % (name, exc), file=sys.stderr)
                bad.append("%s：跑不起来（%r）" % (name, exc))
                rows.append({"name": name, "ok": False, "detail": "跑不起来：%r" % (exc,)})
                continue
            ok = r.returncode == 0
            tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-4:]
            rows.append({"name": name, "ok": ok, "rc": r.returncode, "cmd": cmd,
                         "hit": hit[:3], "tail": tail})
            if not ok:
                bad.append("%s：退出码 %d ← %s" % (name, r.returncode, " / ".join(tail)[-300:]))
                print("[g_integ] %s 失败（rc=%d）：\n  %s" % (name, r.returncode, "\n  ".join(tail)),
                      file=sys.stderr)
        summary = ("集成回归闸：跑 %d / 跳过 %d / 失败 %d —— %s"
                   % (ran, skipped, len(bad), "绿" if not bad else "红"))
        if not bad:
            for row in rows:
                if row.get("skipped"):
                    summary += "；「%s」**未跑**（%s）" % (row["name"], row["detail"])
        return {"ok": not bad, "rows": rows, "ran": ran, "skipped": skipped,
                "bad": bad, "summary": summary}

    def render(self, rep: dict) -> str:
        lines = [rep["summary"]]
        for row in rep.get("rows") or []:
            if row.get("ok") is True:
                lines.append("  ✓ %s：rc=0" % row["name"])
            elif row.get("ok") is False and not rep.get("bad"):
                lines.append("  ✗ %s：%s" % (row["name"], row.get("detail") or "失败"))
        for b in rep.get("bad") or []:
            lines.append("  ✗ " + b)
        return "\n".join(lines)


# ====================================================================== demo
def demo(cfg: ProjectConfig | None = None) -> int:
    """自证：**好命令必绿、坏命令必红、未命中必须显式写"未跑"**。"""
    import tempfile
    print("### 集成回归闸 g_integ 演示")
    g = IntegGate(cfg)
    fails = 0

    # ① 未命中 ⇒ 不该跑，且报告要写"未跑"
    saved = g.reg
    g.reg = {"integration": [{"name": "假项目", "patterns": ["src/never/"],
                              "cwd": ".", "cmd": ["python", "-c", "print('should-not-run')"],
                              "timeout_seconds": 30}]}
    rep = g.check(changed=["README.md"])
    ok1 = rep["ok"] and rep["ran"] == 0 and rep["skipped"] == 1 and "未跑" in rep["summary"]
    print("  %s 未命中 ⇒ 不跑，且报告写明「未跑」：%s" % ("✓" if ok1 else "✗", rep["summary"]))
    if not ok1:
        fails += 1

    # ② 命中 + 好命令 ⇒ 绿
    g.reg = {"integration": [{"name": "好命令", "patterns": ["x.py"],
                              "cwd": ".", "cmd": ["python", "-c", "print('ok')"],
                              "timeout_seconds": 30}]}
    rep = g.check(changed=["a/x.py"])
    ok2 = rep["ok"] and rep["ran"] == 1
    print("  %s 命中 + 好命令 ⇒ 绿（ran=%d）" % ("✓" if ok2 else "✗", rep["ran"]))
    if not ok2:
        fails += 1

    # ③ 命中 + 坏命令 ⇒ **必须红**
    g.reg = {"integration": [{"name": "坏命令", "patterns": ["x.py"],
                              "cwd": ".", "cmd": ["python", "-c", "import sys; sys.exit(3)"],
                              "timeout_seconds": 30}]}
    rep = g.check(changed=["a/x.py"])
    ok3 = (not rep["ok"]) and rep["ran"] == 1
    print("  %s 命中 + 坏命令（rc=3）⇒ %s" % ("✓✓拦下" if ok3 else "✗✗ **没拦住**", rep["summary"]))
    if not ok3:
        fails += 1

    # ④ 命中 + 超时 ⇒ **必须红**（不许无限等）
    g.reg = {"integration": [{"name": "慢命令", "patterns": ["x.py"], "cwd": ".",
                              "cmd": ["python", "-c", "import time; time.sleep(30)"],
                              "timeout_seconds": 2}]}
    rep = g.check(changed=["a/x.py"])
    ok4 = (not rep["ok"]) and "超时" in " ".join(rep.get("bad") or [])
    print("  %s 命中 + 超时 ⇒ %s" % ("✓✓拦下" if ok4 else "✗✗ **没拦住**", rep["summary"]))
    if not ok4:
        fails += 1

    # ⑤ 配置坏了 ⇒ 必须红（不许当"没有集成项"静默放行）
    g.reg = {"_broken": "假装的解析错误", "integration": []}
    rep = g.check(changed=["x"])
    ok5 = not rep["ok"]
    print("  %s 配置坏了 ⇒ %s" % ("✓✓拦下" if ok5 else "✗✗ **没拦住**", rep["summary"]))
    if not ok5:
        fails += 1

    g.reg = saved
    print("\n### 自检结果：%s" % ("全部通过（坏命令/超时/坏配置都拦下）" if fails == 0
                                else "%d 项失败" % fails))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        print("[g_integ] stdout 重设为 utf-8 失败（继续跑）：%r" % (e,), file=sys.stderr)
    from guard import load as load_project       # ★ 别叫 load_cfg：会覆盖本模块的 load_cfg()

    ap = argparse.ArgumentParser(prog="g_integ")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--changed", default="", help="分号/逗号分隔的改动文件清单")
    ap.add_argument("--all", action="store_true", help="全部跑（不看改动面）")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.demo:
        sys.exit(demo(load_project(a.project, a.project_root)))
    ch = [x for x in a.changed.replace(",", ";").split(";") if x.strip()]
    rep = IntegGate(load_project(a.project, a.project_root)).check(changed=ch, force_all=a.all)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(IntegGate().render(rep))
    sys.exit(0 if rep["ok"] else 1)
