# -*- coding: utf-8 -*-
"""_guard · 统一入口 —— 五道闸门从这里过

用法（任何项目都一样，项目根由参数传）：
  python guard_cli.py --project <名> --project-root <根> <子命令>

子命令：
  status                     装载自检：读了哪些标准、装了哪些闸门
  selftest                   跑全部五闸的负例自检（构造以前会犯的错，看拦不拦）
  vault-snapshot             写操作前落快照
  vault-list                 列版本
  vault-accept  --note 理由   标记认可版
  vault-rollback <序号|戳|md5> 回档并自动验证
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from guard import load, available_projects  # noqa: E402

GATE_FILES = {
    "spec": HERE / "gates" / "g_spec.py",
    "vault": HERE / "gates" / "g_vault.py",
    "obj": HERE / "gates" / "g_obj.py",
    "probe": HERE / "gates" / "g_probe.py",
    "done": HERE / "gates" / "g_done.py",
    "lang": HERE / "gates" / "g_lang.py",
    "cap": HERE / "gates" / "g_cap.py",
    # 第八闸：根治病根闸 —— 把《问题总表》归纳出的 7 个病根做成机器拦得住的检查
    "root": HERE / "gates" / "g_root.py",
    # 第九闸：意图闸 —— 动手之前，先把"我以为你要我做什么"变成可确认的清单
    "intent": HERE / "gates" / "g_intent.py",
    # 第十闸：路径与提交闸 —— 三条"靠记性必忘"的 AGENTS.md 规矩
    "path": HERE / "gates" / "g_path.py",
    # 第十一闸：结构声明闸 —— 治本：查"有没有那一栏"，不查"用了什么词"
    "struct": HERE / "gates" / "g_struct.py",
    # 第十二闸：交叉复核闸 —— 去盘上/记忆库/台账真核一遍（治"蓄意编造"）
    "xcheck": HERE / "gates" / "g_xcheck.py",
    # 第十三闸：视觉核验闸 —— 图像结论必须有"看过"的证据；看不了就明说
    "vision": HERE / "gates" / "g_vision.py",
    # 第十四闸：决策过程闸 —— 核"决策站不站得住"（比选/判据/可复算/不撞拒绝清单）
    "decision": HERE / "gates" / "g_decision.py",
    # 第十五闸：文档健康闸 —— 记忆文件不能违反它自己的规矩
    "doc": HERE / "gates" / "g_doc.py",
    # 第十六闸：行为闸 —— 把 4 条"只在文档里写着"的裸规矩变成可跑的检查
    "behavior": HERE / "gates" / "g_behavior.py",
    # 第十七闸：内存闸 —— 跑重活之前先实测可用内存，不够就降档/拒绝开跑
    #   （2026-10-03 立：两个子智能体在 ASR 中途 OOM 死掉，根因是这条没闸在跑）
    "mem": HERE / "gates" / "g_mem.py",
    # ★ 第十八闸：要求锚闸 —— 治"手艺越来越精，方向越来越偏"（2026-10-04 用户点破）
    #   此前 41 条规矩**全是"做法类"，要求类 = 0 条** ⇒
    #   闸能保证"做得对不对"，保证不了"是不是用户要的那件事"。
    #   实证：示例项目A构件全合规、整座山方向错 ⇒ 42 轮返工后被删。
    "brief": HERE / "gates" / "g_brief.py",
    # ★ 第十九闸：开工前提问闸 —— 不理解为或有疑问 ⇒ 必须先问，绝对不开工
    #   （2026-10-04 用户明令；与 g_intent 互补：g_intent 管指代歧义，本闸管"该问没问"）
    "ask": HERE / "gates" / "g_ask.py",
    # ★ 第二十闸：静默宽 except 闸 —— 治本工作区头号病「静默失效」的可数化
    #   （2026-10-14 立；审计核移植自 lingshu（MIT）+ 我们两份基线；
    #    上线首次跑就抓到本闸自身 3 处新静默 except ⇒ 已改成失败可见）
    "silent": HERE / "gates" / "g_silent.py",
    # ★ 第二十一闸：对外面卫生闸 —— 凭据/本机绝对路径/PII 不得进对外面（R-55）
    #   （2026-10-15 立；此前 AGENTS.md 的"推 GitHub 前扫凭据"是**裸规矩**，无闸在跑）
    "public": HERE / "gates" / "g_public.py",
    # ★ 第二十二闸：通用层纯净性闸 —— `_guard` 是跨项目通用层，不许沾具体项目（R-56）
    #   （2026-10-15 立；起因：2026-10-14 我自己在 g_silent.py 里写死了项目路径）
    "purity": HERE / "gates" / "g_purity.py",
    # ★ 2026-10-15（R-57 挂载完整性闸首测暴露）：以下 5 道闸此前**只有文件、没登记** ⇒
    #   `guard_cli` 跑不到、`selftest` 也不跑 ⇒ 坏了没人知道（双盲）。
    #   ⚠ 其中 `del` 的 `--demo` **当前为红**（漏拦"先删后补矛盾"句式，见 2026-10-15 文档）
    #     —— **照实登记，让缺陷可见**（修好前 `selftest` 会报它红，这是**有意**的，不是回归）。
    "batch": HERE / "gates" / "g_batch.py",
    "constraints": HERE / "gates" / "g_constraints.py",
    "criteria": HERE / "gates" / "g_criteria.py",
    "del": HERE / "gates" / "g_del.py",
    "loop": HERE / "gates" / "g_loop.py",
    # ★ 第二十三闸：闸挂载完整性闸 —— 每一道闸都必须「跑得到」（R-57）
    "mount": HERE / "gates" / "g_mount.py",
    # ★ 第二十四闸（2026-10-15）：记忆库管道卫生闸 ——
    #   插件/框架注入不得被当成"用户原话"入库（R-58；自噬闭环的源头）
    "mem_hygiene": HERE / "gates" / "g_mem_hygiene.py",
    # ★ 第二十五闸（2026-10-15）：对抗电池闸 —— 把「假报错/假根治/假通过」变成每次收尾都在跑的判据（R-59）
    "adv": HERE / "gates" / "g_adv.py",
    # ★ 第二十六闸（2026-10-15）：来源闸 —— 学自灵枢 provenance 六问 + #217 来源洗白（R-60）
    "prov": HERE / "gates" / "g_prov.py",
    # ★ 第二十七闸（2026-10-15）：自写自读闸 —— 学自灵枢 #263「维护周期不许往记忆图写自己的日志」（R-61）
    "selfwrite": HERE / "gates" / "g_selfwrite.py",
    # ★ 第二十八闸（2026-10-15）：语法/格式闸 —— 改动过的 .py/.json 当场过编译与解析（R-63）
    #   起因：同一天里我 6 次把中文串里的 ASCII 引号写坏（SyntaxError）、4 次把 JSON 写坏，
    #   每次都靠"跑起来报错"才发现；其中两次让闸自己 fail-closed 瘫掉。
    "syntax": HERE / "gates" / "g_syntax.py",
    # ★ 第二十九闸（2026-10-16）：集成回归闸 —— 改动命中声明面 ⇒ **自动跑**该项目集成回归（R-64）
    #   起因：脑融合的端到端回归写完后，package.json 的 test 脚本里**没有它**（靠人记得跑=等于没有）
    "integ": HERE / "gates" / "g_integ.py",
    # ★ 第三十闸（2026-10-16）：方向性偏差闸 —— 检索有没有系统性丢掉「矛盾的另一侧」（R-65，学自 hive-memory-bench）
    "directional": HERE / "gates" / "g_directional.py",
    # ★ 第三十一闸（2026-10-16）：极性/否定闸 —— 拿"关键词覆盖"冒充"语义存活"必须判红（R-67）
    "polarity": HERE / "gates" / "g_polarity.py",
    # ★ 第三十二/三十三闸（2026-10-16）：独立见证者（R-68，学自 dsh-bounty 思路）+ 整洁闸（R-69）
    "witness": HERE / "gates" / "g_witness.py",
    "clean": HERE / "gates" / "g_clean.py",
}


def _cfg(a):
    """统一装载：--project / --project-root 都可省（自动识别）。"""
    return load(a.project, a.project_root)


def cmd_status(a) -> int:
    cfg = _cfg(a)
    print("=" * 74)
    print(f"_guard 装载自检 · 项目『{cfg.name}』")
    print(f"  项目根    ：{cfg.project_root}")
    print(f"  识别依据  ：{cfg.detected_by}")
    print(f"  工具层    ：{HERE}（与项目无关，零参数可用）")
    print("=" * 74)
    # 标准文件
    miss = cfg.missing_spec_sources()
    print(f"\n【口径闸】读了 {len(cfg.spec_sources)} 份标准，缺 {len(miss)} 份：")
    for s in cfg.spec_sources:
        print(f"   {'✓' if cfg.resolve(s).exists() else '✗ 缺'}  {s}")
    print(f"   断言 {len(cfg.assertions)} 条：" + ", ".join(list(cfg.assertions)[:6]) +
          (" …" if len(cfg.assertions) > 6 else ""))
    # 版本闸
    v = cfg.vault or {}
    print(f"\n【版本闸】金库 → {cfg.resolve(v.get('dir', '_guard_vault'))}")
    print(f"   保护 {len(cfg.write_paths)} 条路径，扩展名 {v.get('extensions')}，滚动保留 {v.get('keep', 50)} 份")
    # 对象闸
    print(f"\n【对象闸】保护名单 {len(cfg.protected)} 条：")
    for p in cfg.protected:
        print(f"   · {p}")
    # 探针 / 完成
    print(f"\n【判据闸】已装（量测必须带对照自证）")
    print(f"\n【完成闸】交付检查项 {len(cfg.done_checks)} 条：")
    for d in cfg.done_checks:
        print(f"   · {d}")
    # ★ 自证：config 里声明的闸门，必须与磁盘上真实存在的闸门文件一致。
    #   （踩过：加了 g_root 但 config 的 gates: 没同步 ⇒ status 少报一个闸，
    #    而 selftest 却会跑它 —— 两处口径不一致，正是"台账与实物不符"那条病根。）
    declared = [g for g in (cfg.get("gates") or [])]
    installed = [k for k in GATE_FILES]
    missing_cfg = [g for g in installed if g not in declared]      # 有文件、没声明
    missing_file = [g for g in declared if g not in GATE_FILES]    # 声明了、没文件
    print(f"\n已装闸门（{len(installed)} 道）：{', '.join(installed)}")
    if missing_cfg:
        print(f"   ⚠ config 未声明（但文件在，selftest 会跑）：{', '.join(missing_cfg)}")
    if missing_file:
        print(f"   ✗ config 声明了但文件不存在：{', '.join(missing_file)}")
    if not missing_cfg and not missing_file:
        print("   ✓ config 声明与磁盘文件一致（口径自证通过）")
    return 0


def cmd_selftest(a) -> int:
    """跑全部闸门的负例自检：把'以前会犯的错'喂进去，看闸门当场红。"""
    rc_total = 0
    rc_unverified = 0
    ran = 0
    unverified: list = []
    for name in ("spec", "obj", "probe", "done", "lang", "cap", "root", "intent", "path", "struct", "xcheck", "vision", "decision", "doc", "behavior", "mem", "brief", "ask", "silent", "public", "purity", "vault", "batch", "constraints", "criteria", "del", "loop", "mount", "mem_hygiene", "adv", "prov", "selfwrite", "syntax", "integ", "directional", "polarity", "witness", "clean"):
        print("\n" + "█" * 74)
        print(f"█ 闸门自检：{name}")
        print("█" * 74)
        args = [sys.executable, str(GATE_FILES[name]), "--demo"]
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = r.stdout or r.stderr
        print(out)
        ran += 1
        # ★ 三态口径（2026-10-16 / 2026-10-26 修）：本机集成闸在**环境未接**时输出「未验」。
        #   **未验 ≠ 通过**（所以退出码仍非 0，不许被当成绿灯），
        #   但**未验 ≠ 失败**（所以不许并进"有失败项"）。
        #   旧写法直接 `rc_total |= r.returncode` ⇒ 一个全新安装里
        #   xcheck/constraints/mem_hygiene 三道因"本机没台账"记未验，
        #   汇总就报"有失败项"，让人以为闸门坏了。
        if "自检结果：未验" in out:
            reason = next((ln.strip() for ln in out.splitlines() if "自检结果：未验" in ln), "")
            unverified.append("%s：%s" % (name, reason))
            rc_unverified |= r.returncode
        else:
            rc_total |= r.returncode
    print("\n" + "=" * 74)
    if rc_total != 0:
        print("闸门自检汇总：有失败项（见上）")
        if unverified:
            print("  另有 %d 道未验（环境未接，非通过）：" % len(unverified))
            for u in unverified:
                print("    %s" % u)
    elif unverified:
        print("闸门自检汇总：无红（%d 道全过 ｜ %d 道未验 ｜ 0 红）"
              % (ran - len(unverified), len(unverified)))
        for u in unverified:
            print("  未验（环境未接，非通过）：%s" % u)
        print("  ⚠ 未验不算通过：退出码保持非 0，免得被当绿灯。")
    else:
        print("闸门自检汇总：全部通过")
    return rc_total or rc_unverified


def cmd_vault(a, extra: list[str]) -> int:
    args = [sys.executable, str(GATE_FILES["vault"]), *extra]
    if a.project:
        args += ["--project", a.project]
    if a.project_root:
        args += ["--project-root", a.project_root]
    r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
    return r.returncode


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(prog="guard_cli", description="_guard 统一入口（七道闸门；项目根可省，自动识别）")
    ap.add_argument("--project", help=f"项目名（可省；已配置：{available_projects()}）")
    ap.add_argument("--project-root", help="项目根目录（可省；默认从当前目录自动识别）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="装载自检（含自动识别结果）")
    sub.add_parser("selftest", help="全部闸门负例自检")
    sub.add_parser("capability", help="列出本机能力清单（当场扫描）")
    sub.add_parser("measure-plan", help="Blender 取数：列出可自动量的断言（7/7 覆盖率）")
    pm = sub.add_parser("measure-code", help="Blender 取数：生成可在 Blender 里执行的探测代码")
    pm.add_argument("--out", help="写到文件（不写则打印）")
    pc = sub.add_parser("measure-check", help="Blender 取数：吃回传的 MEASURED JSON 跑口径闸")
    pc.add_argument("json", help="JSON 字符串或文件路径")
    sub.add_parser("hook-selftest", help="自动挂钩实机自检")
    # 第八闸：根治病根闸
    gr = sub.add_parser("root", help="根治病根闸：检查一份报告有没有踩那 7 个病根")
    gr.add_argument("text_file", help="要检查的文件路径")
    gr.add_argument("--kind", default="report", choices=["report", "plan", "article", "auto"],
                    help="报告体/方案体/文章体（文章体只跑 3 道，避免误杀）")
    gr.add_argument("--strict", action="store_true", help="严格模式：任一不过即拦")
    gr.add_argument("--json", action="store_true", help="输出完整 JSON")
    sub.add_parser("root-demo", help="根治病根闸负例自检")
    sub.add_parser("report-selftest", help="报告闸实机自检（坏报告必须拦下且不落盘）")
    cr = sub.add_parser("check-report", help="检查一份报告（不过则退出码 1）—— 写报告前跑它")
    cr.add_argument("report_file")
    cr.add_argument("--kind", default="report", choices=["report", "plan", "article", "auto"])
    # 第九闸：意图闸
    gi = sub.add_parser("intent", help="意图闸：动手前解析用户的话，有歧义就生成选择题")
    gi.add_argument("--said", required=True, help="用户原话")
    gi.add_argument("--candidates", help="同类候选项，分号分隔")
    gi.add_argument("--uncertain", help="我不确定的地方，分号分隔")
    gi.add_argument("--reading", default="", help="我打算怎么理解（一句话）")
    sub.add_parser("intent-demo", help="意图闸负例自检（含真实那次误解）")
    sub.add_parser("intent-selftest", help="意图闸实机自检（hook 侧）")
    # 第十一闸：结构声明闸
    gs = sub.add_parser("struct", help="结构声明闸：查报告有没有那七栏（换词绕不过）")
    gs.add_argument("--text-file", help="要检查的报告")
    gs.add_argument("--template", action="store_true", help="输出照抄就能过闸的报告模板")
    gs.add_argument("--json", action="store_true")
    # 第十二闸：交叉复核闸
    gx = sub.add_parser("xcheck", help="交叉复核闸：把报告里的声明拿去盘上/记忆库核一遍")
    gx.add_argument("--text-file", help="要复核的报告")
    gx.add_argument("--items", help="只跑指定项 X1,X2,X3,X4,X5")
    gx.add_argument("--json", action="store_true")
    # 第十三闸：视觉核验闸
    gv = sub.add_parser("vision", help="视觉核验闸：能力探测/看图/检查报告里的图像结论")
    gv.add_argument("--capability", action="store_true", help="只探能不能看图")
    gv.add_argument("--image", help="要看的图")
    gv.add_argument("--ask", help="要问的问题")
    gv.add_argument("--text-file", help="检查报告里的图像结论")
    # 第十四闸：决策过程闸
    gd = sub.add_parser("decision", help="决策过程闸：核方案比选/判据/可复算，不核结论对错")
    gd.add_argument("--text-file", help="要检查的方案文本")
    gd.add_argument("--json", action="store_true")
    # 视觉标注工具：给人眼看之前，先在图上框出来
    va = sub.add_parser("annotate", help="在图上画框+编号+放大（要人眼判细线级细节时先跑这个）")
    va.add_argument("--image", required=True)
    va.add_argument("--boxes", required=True, help="x,y,w,h,编号,说明;x,y,w,h,编号,说明")
    va.add_argument("--out-dir", default="_guard/_anno")
    va.add_argument("--sheet", action="store_true", help="同时输出给人看的一页纸")
    # 第十五闸：文档健康闸
    gdc = sub.add_parser("doc", help="文档健康闸：记忆文件不能违反它自己的规矩")
    gdc.add_argument("--file", help="要检查的文档（省略=检查 config 里列的全部）")
    gdc.add_argument("--json", action="store_true")
    # 第十六闸：行为闸
    gb = sub.add_parser("behavior", help="行为闸：连续调参/搬回已拒绝/像素冒充看图/反驳用户")
    gb.add_argument("--text-file", help="要检查的文本")
    gb.add_argument("--json", action="store_true")
    # 第十闸：路径与提交闸
    gp = sub.add_parser("path", help="路径与提交闸：查写入越界/幻影路径/擅自提交")
    gp.add_argument("--write-path", help="要检查的写入路径")
    gp.add_argument("--cmd-text", dest="cmd_text", help="要检查的命令（幻影路径）")
    gp.add_argument("--code-file", help="要检查的脚本文件（擅自提交）")
    # 第二十闸：静默宽 except 闸（R-54）
    gsl = sub.add_parser("silent", help="静默宽 except 闸：超基线/解析失败即红（只防新增）")
    gsl.add_argument("--json", action="store_true")
    gsl.add_argument("--demo", action="store_true", help="负例自检")
    # 第二十一闸：对外面卫生闸（R-55）
    gpb = sub.add_parser("public", help="对外面卫生闸：凭据/本机路径/PII 不得进对外面（命中即红）")
    gpb.add_argument("--paths", help="逗号分隔的路径（省略=用 tools/public_faces.txt 登记面）")
    gpb.add_argument("--json", action="store_true")
    gpb.add_argument("--demo", action="store_true", help="负例自检")
    # 第二十二闸：通用层纯净性闸（R-56）
    gpu = sub.add_parser("purity", help="通用层纯净性闸：_guard 代码里不许写死具体项目路径/名")
    gpu.add_argument("--json", action="store_true")
    gpu.add_argument("--demo", action="store_true", help="负例自检")
    # 第二十三闸：闸挂载完整性闸（R-57）
    gmt = sub.add_parser("mount", help="闸挂载完整性闸：每道闸是否都登记进 CLI 与 selftest")
    gmt.add_argument("--json", action="store_true")
    gmt.add_argument("--demo", action="store_true", help="负例自检")
    gav = sub.add_parser("adv", help="对抗电池闸：假报错/假根治/假通过（R-59）")
    gav.add_argument("--full", action="store_true", help="全量（含所有 --demo 类闸）")
    gav.add_argument("--changed", default="", help="只跑改到的闸（收尾自动路）")
    gav.add_argument("--json", action="store_true")
    gav.add_argument("--demo", action="store_true", help="负例自检（覆盖 + 坏样本自证 + 真样本全跑）")
    gmh = sub.add_parser("mem-hygiene", help="记忆库管道卫生闸：插件注入不得被当用户原话入库（R-58）")
    gmh.add_argument("--json", action="store_true")
    gmh.add_argument("--demo", action="store_true", help="负例自检")
    gmh.add_argument("--db", action="store_true", help="追加现库判据（慢，全表 LIKE 扫）")
    gpv = sub.add_parser("prov", help="来源闸：灵枢来源六问 + 来源洗白（R-60）")
    gpv.add_argument("--json", action="store_true")
    gpv.add_argument("--demo", action="store_true", help="负例自检（造坏库自证）")
    gpv.add_argument("--db", help="指定库路径（默认取登记表 tools/prov_registry.json）")
    gsw = sub.add_parser("selfwrite", help="自写自读闸：自检/维护不许写进被检索的面（R-61）")
    gsw.add_argument("--json", action="store_true")
    gsw.add_argument("--demo", action="store_true", help="负例自检（造坏库/坏源码）")
    gsw.add_argument("--db", help="指定库路径")
    gsw.add_argument("--scan", action="append", help="扫描根（可多次）")
    gsw.add_argument("--db-only", action="store_true", help="只核库，不扫源码")
    gsy = sub.add_parser("syntax", help="语法/格式闸：改动过的 .py/.json 当场编译/解析（R-63）")
    for _n, _h in (("witness", "独立见证者闸：核验器不得与被检对象同源（R-68）"),
                   ("clean", "整洁闸：跑完不许把仓搞脏（R-69）")):
        _p = sub.add_parser(_n, help=_h)
        _p.add_argument("--json", dest="json_out")
        _p.add_argument("--selftest", action="store_true")
    gpo = sub.add_parser("polarity", help="极性/否定闸：覆盖≠蕴含（R-67）")
    gpo.add_argument("--claim")
    gpo.add_argument("--quotes")
    gpo.add_argument("--json", dest="json_out")
    gpo.add_argument("--selftest", action="store_true")
    gdi = sub.add_parser("directional", help="方向性偏差闸：检索是否丢掉「矛盾的另一侧」（R-65）")
    gdi.add_argument("--pairs", help="矛盾对 JSON（a=题面侧，b=对面那一侧）")
    gdi.add_argument("--retrieval", help="检索片段：目录或单文件")
    gdi.add_argument("--tol", type=float, default=0.2)
    gdi.add_argument("--json", dest="json_out")
    gdi.add_argument("--selftest", action="store_true", help="负例自检")
    git = sub.add_parser("integ", help="集成回归闸：改动命中声明面 ⇒ 自动跑集成回归（R-64）")
    git.add_argument("--changed", default="", help="分号/逗号分隔的改动文件清单")
    git.add_argument("--all", action="store_true", help="全部跑（不看改动面）")
    git.add_argument("--json", action="store_true")
    git.add_argument("--demo", action="store_true", help="负例自检")
    gsy.add_argument("--files", nargs="*", default=[])
    gsy.add_argument("--changed", default="")
    gsy.add_argument("--json", action="store_true")
    gsy.add_argument("--demo", action="store_true", help="负例自检")
    pr = sub.add_parser("rules", help="AGENTS.md 规矩 → 闸 的接线核对（看哪些规矩是裸的）")
    pr.add_argument("--no-run", action="store_true", help="只查文件在不在，不跑自检")
    sub.add_parser("vault-snapshot")
    sub.add_parser("vault-list")
    pa = sub.add_parser("vault-accept"); pa.add_argument("--note", default="用户认可")
    pr = sub.add_parser("vault-rollback"); pr.add_argument("which"); pr.add_argument("--dry-run", action="store_true")

    a, extra = ap.parse_known_args()
    if a.cmd == "status":
        return cmd_status(a)
    if a.cmd == "selftest":
        return cmd_selftest(a)
    if a.cmd == "capability":
        r = subprocess.run([sys.executable, str(GATE_FILES["cap"]), "--list"],
                           text=True, encoding="utf-8", errors="replace")
        print(r.stdout)
        return r.returncode
    if a.cmd == "struct":
        args = [sys.executable, str(GATE_FILES["struct"])]
        if a.template:
            args.append("--template")
        if a.text_file:
            args += ["--text-file", a.text_file]
        if a.json:
            args.append("--json")
        if len(args) == 2:
            args.append("--demo")
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "decision":
        args = [sys.executable, str(GATE_FILES["decision"])]
        if a.text_file:
            args += ["--text-file", a.text_file]
        if a.json:
            args.append("--json")
        if len(args) == 2:
            args.append("--demo")
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "annotate":
        from gates.vannot import annotate, question_sheet, Region  # noqa: E402
        regs = []
        for part in a.boxes.split(";"):
            f = part.split(",")
            if len(f) >= 4:
                regs.append(Region(int(f[0]), int(f[1]), int(f[2]), int(f[3]),
                                   f[4] if len(f) > 4 else "1",
                                   ",".join(f[5:]) if len(f) > 5 else ""))
        res = annotate(a.image, regs, out_dir=a.out_dir)
        print(res.detail)
        for c in res.crops:
            print("  放大：" + c)
        if a.sheet:
            sheet = question_sheet(a.image, regs)
            sp = Path(a.out_dir) / (Path(a.image).stem + "_请人眼判.md")
            sp.write_text(sheet, encoding="utf-8")
            print("  判读单：" + str(sp))
        return 0 if res.ok else 1
    if a.cmd == "behavior":
        args = [sys.executable, str(GATE_FILES["behavior"])]
        if a.text_file:
            args += ["--text-file", a.text_file]
        if a.json:
            args.append("--json")
        if len(args) == 2:
            args.append("--demo")
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "doc":
        args = [sys.executable, str(GATE_FILES["doc"])]
        if a.file:
            args += ["--file", a.file]
        if a.json:
            args.append("--json")
        if len(args) == 2:
            args.append("--demo")
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "vision":
        args = [sys.executable, str(GATE_FILES["vision"])]
        if a.capability:
            args.append("--capability")
        if a.image:
            args += ["--image", a.image]
        if a.ask:
            args += ["--ask", a.ask]
        if a.text_file:
            args += ["--text-file", a.text_file]
        if len(args) == 2:
            args.append("--demo")
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "xcheck":
        args = [sys.executable, str(GATE_FILES["xcheck"])]
        if a.text_file:
            args += ["--text-file", a.text_file]
        if a.items:
            args += ["--items", a.items]
        if a.json:
            args.append("--json")
        if len(args) == 2:
            args.append("--demo")
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "rules":
        args = [sys.executable, str(HERE / "rules.py")]
        if getattr(a, "no_run", False):
            args.append("--no-run")
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "silent":
        args = [sys.executable, str(GATE_FILES["silent"])]
        if getattr(a, "json", False):
            args.append("--json")
        if getattr(a, "demo", False):
            args.append("--demo")
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace",
                           capture_output=True)
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "public":
        args = [sys.executable, str(GATE_FILES["public"])]
        if getattr(a, "paths", None):
            args += ["--paths", a.paths]
        if getattr(a, "json", False):
            args.append("--json")
        if getattr(a, "demo", False):
            args.append("--demo")
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace",
                           capture_output=True)
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "purity":
        args = [sys.executable, str(GATE_FILES["purity"])]
        if getattr(a, "json", False):
            args.append("--json")
        if getattr(a, "demo", False):
            args.append("--demo")
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace",
                           capture_output=True)
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "adv":
        args = [sys.executable, str(GATE_FILES["adv"])]
        if getattr(a, "json", False):
            args.append("--json")
        if getattr(a, "demo", False):
            args.append("--demo")
        elif getattr(a, "full", False):
            args.append("--full")
        elif getattr(a, "changed", ""):
            args += ["--changed", a.changed]
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        return r.returncode
    if a.cmd == "mem_hygiene" or a.cmd == "mem-hygiene":
        args = [sys.executable, str(GATE_FILES["mem_hygiene"])]
        if getattr(a, "json", False):
            args.append("--json")
        if getattr(a, "demo", False):
            args.append("--demo")
        if getattr(a, "db", False):
            args.append("--db")
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        return r.returncode
    if a.cmd == "prov":
        args = [sys.executable, str(GATE_FILES["prov"])]
        if getattr(a, "json", False):
            args.append("--json")
        if getattr(a, "demo", False):
            args.append("--demo")
        if getattr(a, "db", None):
            args += ["--db", a.db]
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        return r.returncode
    if a.cmd == "selfwrite":
        args = [sys.executable, str(GATE_FILES["selfwrite"])]
        if getattr(a, "json", False):
            args.append("--json")
        if getattr(a, "demo", False):
            args.append("--demo")
        if getattr(a, "db", None):
            args += ["--db", a.db]
        for s in (getattr(a, "scan", None) or []):
            args += ["--scan", s]
        if getattr(a, "db_only", False):
            args.append("--db-only")
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        return r.returncode
    if a.cmd == "syntax":
        args = [sys.executable, str(GATE_FILES["syntax"])]
        # ★ 一次 `--files a b c`：早先写成循环拼 `--files X`，argparse 只留最后一个
        #   ⇒ **只查了 1 个文件却报"均合格"**（判据量错对象、静默少查）。
        fl = [f for f in (getattr(a, "files", None) or [])]
        if fl:
            args += ["--files"] + fl
        if getattr(a, "changed", ""):
            args += ["--changed", a.changed]
        if getattr(a, "json", False):
            args.append("--json")
        if getattr(a, "demo", False):
            args.append("--demo")
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        return r.returncode
    if a.cmd in ("witness", "clean"):
        args = [sys.executable, str(GATE_FILES[a.cmd])]
        if getattr(a, "selftest", False):
            args.append("--selftest")
        if getattr(a, "json_out", None):
            args += ["--json", a.json_out]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        return r.returncode
    if a.cmd == "polarity":
        args = [sys.executable, str(GATE_FILES["polarity"])]
        if getattr(a, "selftest", False):
            args.append("--selftest")
        for k in ("claim", "quotes"):
            if getattr(a, k, None):
                args += ["--" + k, getattr(a, k)]
        if getattr(a, "json_out", None):
            args += ["--json", a.json_out]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        return r.returncode
    if a.cmd == "directional":
        args = [sys.executable, str(GATE_FILES["directional"])]
        if getattr(a, "selftest", False):
            args.append("--selftest")
        if getattr(a, "pairs", None):
            args += ["--pairs", a.pairs]
        if getattr(a, "retrieval", None):
            args += ["--retrieval", a.retrieval]
        if getattr(a, "tol", None) is not None:
            args += ["--tol", str(a.tol)]
        if getattr(a, "json_out", None):
            args += ["--json", a.json_out]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        return r.returncode
    if a.cmd == "integ":
        args = [sys.executable, str(GATE_FILES["integ"])]
        if getattr(a, "changed", ""):
            args += ["--changed", a.changed]
        if getattr(a, "all", False):
            args.append("--all")
        if getattr(a, "json", False):
            args.append("--json")
        if getattr(a, "demo", False):
            args.append("--demo")
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        return r.returncode
    if a.cmd == "mount":
        args = [sys.executable, str(GATE_FILES["mount"])]
        if getattr(a, "json", False):
            args.append("--json")
        if getattr(a, "demo", False):
            args.append("--demo")
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace",
                           capture_output=True)
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "path":
        args = [sys.executable, str(GATE_FILES["path"])]
        if a.write_path:
            args += ["--write-path", a.write_path]
        if a.cmd_text:
            args += ["--cmd", a.cmd_text]
        if a.code_file:
            args += ["--code-file", a.code_file]
        if len(args) == 2:          # 没给任何检查项 ⇒ 跑自检
            args.append("--demo")
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "intent-selftest":
        args = [sys.executable, str(HERE / "hook.py"), "--intent-selftest"]
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "intent-demo":
        r = subprocess.run([sys.executable, str(GATE_FILES["intent"]), "--demo"],
                           text=True, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "intent":
        args = [sys.executable, str(GATE_FILES["intent"]), "--said", a.said,
                "--reading", a.reading]
        if a.candidates:
            args += ["--candidates", a.candidates]
        if a.uncertain:
            args += ["--uncertain", a.uncertain]
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "report-selftest":
        args = [sys.executable, str(HERE / "hook.py"), "--report-selftest"]
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "check-report":
        args = [sys.executable, str(HERE / "hook.py"), "--check-report", a.report_file,
                "--kind", a.kind]
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "root-demo":
        r = subprocess.run([sys.executable, str(GATE_FILES["root"]), "--demo"],
                           text=True, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "root":
        args = [sys.executable, str(GATE_FILES["root"]),
                "--text-file", a.text_file, "--kind", a.kind]
        if a.strict:
            args.append("--strict")
        if a.json:
            args.append("--json")
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, text=True, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
        return r.returncode
    if a.cmd == "hook-selftest":
        r = subprocess.run([sys.executable, str(HERE / "hook.py"), "--selftest"],
                           text=True, encoding="utf-8", errors="replace")
        print(r.stdout)
        return r.returncode
    if a.cmd in ("measure-plan", "measure-code", "measure-check"):
        argmap = {"measure-plan": ["--plan"], "measure-code": ["--code"], "measure-check": ["--check"]}
        args = [sys.executable, str(HERE / "blender_probe.py"), *argmap[a.cmd]]
        if a.cmd == "measure-code" and a.out:
            args += ["--out", a.out]
        if a.cmd == "measure-check":
            args += [a.json]
        if a.project:
            args += ["--project", a.project]
        if a.project_root:
            args += ["--project-root", a.project_root]
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
        sys.stdout.write(r.stdout or "")
        sys.stderr.write(r.stderr or "")
        return r.returncode
    if a.cmd == "vault-snapshot":
        return cmd_vault(a, ["snapshot", *extra])
    if a.cmd == "vault-list":
        return cmd_vault(a, ["list"])
    if a.cmd == "vault-accept":
        return cmd_vault(a, ["accept", "--note", a.note])
    if a.cmd == "vault-rollback":
        return cmd_vault(a, ["rollback", a.which] + (["--dry-run"] if a.dry_run else []))
    return 1


if __name__ == "__main__":
    sys.exit(main())
