# -*- coding: utf-8 -*-
"""_guard · 整洁闸 g_clean —— 跑完不许把仓/工作区搞脏（R-69）

## 判据来源（学自 dsh-bounty 的思路，代码自研；该仓 license=None，不可照搬）

dsh-bounty 的 CI（`.github/workflows/review-check.yml`）里有一条：
**「跑完不许把仓搞脏」——`git status --porcelain` 必须为空**。
含义：生成器 / 闸 / 探针跑完之后，**工作区必须回到干净态**；否则说明有东西被悄悄改写
（产物没提交、临时文件乱落、闸自己改了被检对象）——而那正是"**被求值覆盖**"病根的温床。

**为什么本仓需要它**：我们的 `_guard` 自己就频繁写台账/快照/备份（`_guard_ledger/`、
`_guard_vault/`、`_tmp/`），一旦某次写到了受版本管理的文件上，**没有任何判据会报**。

## 判据

| id | 判据 | 量的对象 |
|---|---|---|
| C1 | 声明的每个仓，`git status --porcelain` 必须为空 | 工作区状态 |
| C2 | 允许清单（如生成物目录）里的路径**不算脏**，但必须**显式声明** | 白名单 |
| C3 | 非 git 仓 / `git` 不可用 ⇒ **未验**（不许当干净） | 环境 |

## 用法

    python _guard/gates/g_clean.py                # 读 tools/clean.json
    python _guard/gates/g_clean.py --demo          # 负例自证（造一个脏目录 ⇒ 必红）

`tools/clean.json`：`{"repos": ["D:\\\\agent\\\\_guard"], "allow": ["_guard_ledger/", "_tmp/"]}`
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402

HERE = Path(__file__).resolve().parent.parent
CFG = HERE / "tools" / "clean.json"


def load_cfg() -> dict:
    try:
        if CFG.is_file():
            return json.loads(CFG.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print("[g_clean] 配置读不出（%s）：%r ⇒ 判红" % (CFG, exc), file=sys.stderr)
        return {"_broken": str(exc), "repos": []}
    return {"repos": [str(HERE.parent)]}          # 缺省：本工作区


class CleanGate:
    NAME = "g_clean"

    def __init__(self, cfg: ProjectConfig | None = None):
        self.cfg = cfg
        self.reg = load_cfg()

    def check(self) -> dict:
        if self.reg.get("_broken"):
            return {"ok": False, "rows": [], "summary":
                    "整洁闸：**配置坏了**（%s）⇒ 判红" % self.reg["_broken"]}
        allow = self.reg.get("allow") or []
        rows, bad, unknown = [], [], 0
        for repo in self.reg.get("repos") or []:
            r = Path(repo)
            if not (r / ".git").exists():
                unknown += 1
                rows.append({"repo": repo, "ok": None, "why": "不是 git 仓 ⇒ 未验"})
                continue
            try:
                p = subprocess.run(["git", "status", "--porcelain"], cwd=str(r),
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=120)
            except Exception as exc:  # noqa: BLE001
                print("[g_clean] git 跑不起来（%s）：%r" % (repo, exc), file=sys.stderr)
                unknown += 1
                rows.append({"repo": repo, "ok": None, "why": "git 跑不起来 ⇒ 未验"})
                continue
            if p.returncode != 0:
                unknown += 1
                rows.append({"repo": repo, "ok": None, "why": "git 退出码 %d ⇒ 未验" % p.returncode})
                continue
            dirty = [ln for ln in (p.stdout or "").splitlines() if ln.strip()]
            dirty = [ln for ln in dirty
                     if not any(a.replace("\\", "/") in ln.replace("\\", "/") for a in allow)]
            rows.append({"repo": repo, "ok": not dirty, "dirty": dirty[:20],
                         "n": len(dirty)})
            if dirty:
                bad.append("%s：%d 条未提交改动（前 5：%s）"
                           % (repo, len(dirty), " / ".join(d[:60] for d in dirty[:5])))
                print("[g_clean] %s 脏了 %d 条：\n  %s" % (repo, len(dirty),
                                                          "\n  ".join(dirty[:10])),
                      file=sys.stderr)
        if not rows:
            return {"ok": False, "rows": [], "summary": "整洁闸：**没有可判的仓**（配置为空）"}
        summary = ("整洁闸：仓 %d 个 ｜ 脏 %d ｜ 未验 %d —— %s"
                   % (len(rows), len(bad), unknown, "绿" if not bad else "红"))
        return {"ok": not bad, "rows": rows, "bad": bad, "unverified": unknown,
                "summary": summary}

    def render(self, rep: dict) -> str:
        lines = [rep["summary"]]
        for r in rep.get("rows") or []:
            if r.get("ok") is True:
                lines.append("  ✓ %s：干净%s" % (r["repo"], "" if not r.get("n") else "（%d）" % r["n"]))
            elif r.get("ok") is False:
                lines.append("  ✗ %s：%d 条未提交" % (r["repo"], r.get("n", 0)))
            else:
                lines.append("  ⚠ %s：%s" % (r["repo"], r.get("why")))
        return "\n".join(lines)


# ====================================================================== demo
def demo(cfg: ProjectConfig | None = None) -> int:
    import tempfile
    print("### 整洁闸 g_clean 演示（跑完不许搞脏）")
    d = Path(tempfile.mkdtemp(prefix="gclean_"))
    subprocess.run(["git", "init", "-q"], cwd=str(d), capture_output=True)
    (d / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(d), capture_output=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init"],
                   cwd=str(d), capture_output=True)
    fails = 0
    g = CleanGate(cfg)
    g.reg = {"repos": [str(d)]}
    rep = g.check()
    ok1 = rep["ok"] is True
    print("  %s 干净仓 ⇒ %s" % ("✓" if ok1 else "✗✗", rep["summary"]))
    if not ok1:
        fails += 1
    (d / "dirty.txt").write_text("y", encoding="utf-8")      # 造脏
    rep = g.check()
    ok2 = rep["ok"] is False
    print("  %s 造一条未提交 ⇒ %s" % ("✓✓拦下" if ok2 else "✗✗ **没拦住**", rep["summary"]))
    if not ok2:
        fails += 1
    g.reg = {"repos": [str(d)], "allow": ["dirty.txt"]}
    rep = g.check()
    ok3 = rep["ok"] is True
    print("  %s 允许清单里的路径 ⇒ 不算脏（%s）" % ("✓" if ok3 else "✗✗", rep["summary"]))
    if not ok3:
        fails += 1
    g.reg = {"repos": [str(Path(tempfile.mkdtemp()))]}
    rep = g.check()
    ok4 = (rep["ok"] is True) and rep.get("unverified") == 1
    print("  %s 非 git 仓 ⇒ 未验（不等于干净）" % ("✓" if ok4 else "✗✗"))
    if not ok4:
        fails += 1
    print("\n### 自检结果：%s" % ("全部通过（脏必红 / 白名单放行 / 非 git 记未验）"
                                if fails == 0 else "%d 项失败" % fails))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        print("[g_clean] stdout 重设为 utf-8 失败（继续跑）：%r" % (e,), file=sys.stderr)
    from guard import load as load_project

    ap = argparse.ArgumentParser(prog="g_clean")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--selftest", "--demo", action="store_true", dest="selftest")
    a = ap.parse_args()
    g = CleanGate(load_project(a.project, a.project_root))
    if a.selftest:
        sys.exit(demo(load_project(a.project, a.project_root)))
    rep = g.check()
    print(g.render(rep))
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    sys.exit(0 if rep["ok"] else 1)
