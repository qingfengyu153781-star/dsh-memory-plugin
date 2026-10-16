# -*- coding: utf-8 -*-
"""_guard · 对外面卫生闸 g_public —— 凭据 / 本机绝对路径 / PII 不得进对外面

## 治的哪条规矩（R-55，2026-10-15 立）

**R-55**：任何**对外面**（要推 GitHub 的、要交付给外部方的、要引入固化进本仓的外部件）
在定稿前必须过卫生扫描；命中凭据 / 本机绝对路径字面量 / PII **即红**，
要放过只能在同行写 `hygiene-allow: <理由>`。

## 为什么必须做成闸（此前它是**裸规矩**）

AGENTS.md 里写着「**公共仓库发布安全**：推 GitHub 前扫凭据、核对 .gitignore、git status」，
但 `rules.yaml` 里**没有这条**——即**没有任何东西在跑它**，全靠人记得。
本工作区的铁律是「每条规矩都必须有个东西在跑它」，故补此闸。

## 来源

lingshu（FuRongJun-1999）的**边界章程**（`docs/architecture.md` §6「四不」）与
**引入双清单**（内容政策 + 隐私：无凭据 / 无本机路径 / 无隐私 / 无第三方受限内容）。
它每引入一件都做"机械扫描"，但**那一步是人肉的**（写在 `docs/intake/*/AUDIT.md`）；
本器把那一步机器化，且**独立复核过它自己的仓**：385 文件 0 命中（其 CLEAN 声明成立）。

## 判据

| 情形 | 判定 |
|---|---|
| 登记面里有命中 | **红（退出 1）** |
| **未登记任何对外面** | **红（退出 1）**——"没扫"不等于"没问题"（抄 lingshu `verify_manifest` 修 #408 的语义：扫描面被清空不得静默变绿） |
| 登记面非空且零命中 | 绿 |

★登记面来自 `tools/public_faces.txt`（每行一个路径）；用 `--paths` 可临时指定。

## 用法

    python _guard/gates/g_public.py                     # 扫登记的对外面
    python _guard/gates/g_public.py --paths <p1,p2>     # 扫指定路径
    python _guard/gates/g_public.py --demo              # 4 个负例自检
    python _guard/guard_cli.py public [--paths ...]

## 边界（如实登记）

- **只扫文本**：二进制（.docx/.xlsx/.png…）跳过——对 Office 交付物**必须先导出为文本/解包再扫**，
  本闸不做文本抽取（那是另一个件的工作）。
- 规则是**正则启发式**：会有假阳（把示例 key 当真）与假阴（不在表里的凭据形态）。
  它是**下界**，不是"扫过就干净"的保证；真凭据一旦进过仓库历史，**改文件不够，必须吊销**。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # _guard/gates
GUARD = HERE.parent                             # _guard
sys.path.insert(0, str(GUARD))
from guard import ProjectConfig  # noqa: E402

TOOLS = GUARD / "tools"
SCAN_PATH = TOOLS / "public_hygiene_scan.py"
FACES_PATH = TOOLS / "public_faces.txt"


def _load_scan():
    if not SCAN_PATH.exists():
        raise FileNotFoundError("扫描核缺失：%s（fail-closed）" % SCAN_PATH)
    spec = importlib.util.spec_from_file_location("public_hygiene_scan", SCAN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def registered_faces() -> list[dict]:
    """读登记面：每行 `<路径> | <说明> | ignore=RULE[,RULE]`（后两段可选）。

    ★ **面级 `ignore=` 是根治做法**：让"面的属性"决定规则集合
      （如"申请材料"允许 `PII-MAIL`——联系方式本就是材料的一部分），
      于是该面**新增同类内容也不必每次补行级豁免**。
    """
    if not FACES_PATH.exists():
        return []
    out: list[dict] = []
    for line in FACES_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = [x.strip() for x in s.split("|")]
        ignore: tuple = ()
        for extra in parts[2:]:
            if extra.startswith("ignore="):
                ignore = tuple(x.strip() for x in extra[len("ignore="):].split(",")
                               if x.strip())
        out.append({"path": parts[0], "ignore": ignore})
    return out


class PublicGate:
    """对外面卫生闸。"""

    def __init__(self, cfg: ProjectConfig | None = None):
        self.cfg = cfg

    def check(self, paths: list[str] | None = None) -> dict:
        faces = ([{"path": p, "ignore": ()} for p in paths] if paths
                 else registered_faces())
        if not faces:
            return {
                "ok": False, "gate": "g_public",
                "note": "未登记任何对外面 ⇒ **不当绿**（没扫 ≠ 没问题）。"
                        "登记方式：在 tools/public_faces.txt 每行写一个路径（可加 `| 说明`，"
                        "可加 `| ignore=规则名` 做面级豁免），"
                        "或用 --paths 临时指定；本次要扫的路径：guard_cli.py public --paths ...",
                "faces": [], "hit_total": 0, "hits": [], "files_scanned": 0,
            }
        scan = _load_scan()
        missing = [f["path"] for f in faces if not Path(f["path"]).exists()]
        if missing:
            return {"ok": False, "gate": "g_public",
                    "note": "登记面里有路径不存在（审计面不完整，不当绿）：%s" % ", ".join(missing),
                    "faces": [f["path"] for f in faces], "hit_total": 0, "hits": [],
                    "files_scanned": 0}
        hits: list = []
        scanned = 0
        by_rule: dict = {}
        for f in faces:                       # 逐面扫：各面可用自己的 ignore 规则集
            rep = scan.scan([f["path"]], ignore_rules=f["ignore"])
            hits += rep["hits"]
            scanned += rep["files_scanned"]
            for k, v in rep["by_rule"].items():
                by_rule[k] = by_rule.get(k, 0) + v
        return {"ok": not hits, "gate": "g_public", "faces": [f["path"] for f in faces],
                "note": "", "hit_total": len(hits), "hits": hits,
                "files_scanned": scanned, "by_rule": dict(sorted(by_rule.items()))}

    def render(self, rep: dict) -> str:
        if rep.get("note"):
            return "对外面卫生：%s" % rep["note"]
        if rep["ok"]:
            return ("对外面卫生：绿（%d 个文件零命中）｜面：%s"
                    % (rep["files_scanned"], ", ".join(rep["faces"])))
        lines = ["对外面卫生：红（命中 %d 处，扫描 %d 个文件）"
                 % (rep["hit_total"], rep["files_scanned"])]
        for h in rep["hits"][:40]:
            lines.append("  %s:%d  [%s %s]  %s"
                         % (h["file"], h["line"], h["rule"], h["desc"], h["excerpt"]))
        lines.append("  处置：真凭据 ⇒ **先吊销**再改文件/环境变量；确属公开示例 ⇒ 同行加 "
                     "`hygiene-allow: <理由>`")
        return "\n".join(lines)


# ------------------------------------------------------------------ 负例自检
def demo(cfg: ProjectConfig | None = None) -> int:
    scan = _load_scan()
    base = Path(tempfile.mkdtemp(prefix="public_demo_"))
    try:
        ok = True
        f = base / "a.txt"

        # ① 真凭据形态 ⇒ 必须命中
        #    ★夹具不能用 `AKIA…EXAMPLE`：AWS 官方示例值含 "EXAMPLE"，而已按
        #      PLACEHOLDER_PATS 排除（那是**正当**的示例值）⇒ 会被判不命中（实测踩过）。
        #      要测"真形态能被抓"，夹具就得用**不含占位符特征**的值。
        f.write_text('AWS = "AKIA3K7QZ9WX2MPL4NVT"\n', encoding="utf-8")
        r1 = scan.scan([str(base)])
        print("  ① 假 AWS key            -> 命中 %d（应 ≥1）" % r1["hit_total"])
        ok &= r1["hit_total"] >= 1

        # ② 同行 hygiene-allow ⇒ 必须放过（豁免是行级、显式、可 grep）
        f.write_text('AWS = "AKIA3K7QZ9WX2MPL4NVT"  # hygiene-allow: 确属公开示例值\n',
                     encoding="utf-8")
        r2 = scan.scan([str(base)])
        print("  ② 同行 hygiene-allow    -> 命中 %d（应 0）" % r2["hit_total"])
        ok &= r2["hit_total"] == 0

        # ③ 示例域名邮箱 ⇒ 必须放过（白名单域名）
        f.write_text('mail = "nobody@example.com"\n', encoding="utf-8")
        r3 = scan.scan([str(base)])
        print("  ③ 示例域名邮箱          -> 命中 %d（应 0）" % r3["hit_total"])
        ok &= r3["hit_total"] == 0

        # ④ 输出必须打码（不把凭据原文写进报告）
        f.write_text('api_key = "sk-abcdefghijklmnopqrstuvwxyz123456"\n', encoding="utf-8")
        rep4 = scan.scan([str(base)])
        rendered = scan.render(rep4)
        leaked = "sk-abcdefghijklmnopqrstuvwxyz123456" in rendered
        print("  ④ 报告是否原文泄露凭据  -> %s（应 False）" % leaked)
        ok &= (not leaked) and rep4["hit_total"] >= 1

        # ⑤ 未登记对外面 ⇒ check() 必须**不绿**。
        #    ★必须用**隔离的** FACES_PATH：真实登记面可能非空（本闸此刻已登记真实对外面），
        #      否则这条负例测的就不是"未登记"了——实测踩过：登记后此例假失败。
        global FACES_PATH
        _old_faces = FACES_PATH
        try:
            FACES_PATH = base / "no_such_faces.txt"      # 不存在 ⇒ 登记面为空
            rep5 = PublicGate(cfg).check()
        finally:
            FACES_PATH = _old_faces
        print("  ⑤ 未登记对外面          -> ok=%s（应 False：没扫≠没问题）" % rep5["ok"])
        ok &= (rep5["ok"] is False)

        print("  demo 结论：%s" % ("全过（闸真的会动）" if ok else "有负例未按预期——闸坏了"))
        return 0 if ok else 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _log(rec: dict) -> None:
    try:
        ledger = GUARD / "ledger"
        ledger.mkdir(exist_ok=True)
        with (ledger / "g_public.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:                       # 记录失败不静默
        print("g_public: 台账写入失败：%r" % (exc,), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="g_public.py", description="对外面卫生闸（R-55）")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--paths", help="逗号分隔的路径（省略=用登记面）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    cfg = None
    if not a.demo:
        try:
            from guard import load as load_cfg
            cfg = load_cfg(a.project, a.project_root)
        except Exception as exc:
            print("g_public: 项目配置加载失败，退到默认：%r" % (exc,), file=sys.stderr)

    if a.demo:
        rc = demo(cfg)
        _log({"demo": True, "ok": rc == 0})
        return rc

    paths = [p.strip() for p in a.paths.split(",")] if a.paths else None
    rep = PublicGate(cfg).check(paths)
    _log({"ok": rep["ok"], "faces": rep.get("faces"), "hits": rep.get("hit_total")})
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, sort_keys=True))
    elif not a.quiet:
        print(PublicGate(cfg).render(rep))
        print("结论：%s" % ("绿" if rep["ok"] else "红（见上）"))
    else:
        print(PublicGate(cfg).render(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
