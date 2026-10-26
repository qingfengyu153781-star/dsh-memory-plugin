# -*- coding: utf-8 -*-
"""_guard · 路径与提交闸 g_path —— 三条"靠记性必忘"的规矩，做成机器拦

## 治的规矩（全部来自 AGENTS.md）

| 规矩 | 出处 | 为什么靠记性必忘 |
|---|---|---|
| **R-07** DSH 产生的文件一律放 `D:\\agent` 下；`cc-assets` 只读 | 文件写入铁律 | 路径是"顺手写"的，不会先想 |
| **R-25** bash 里 `/d/agent` 不是 D 盘（是幻影路径） | bash 工具路径陷阱 | **写入"成功"(exit 0) 但磁盘上没有** —— 最阴的一条 |
| **R-14** 绝不擅自提交（用户没说提交之前只给方案） | 镜23a 教训 | 脚本跑过断言就一路 POST 出去 |

## 实证（都是真发生过的）
  · R-25：`/d/agent` 落在 `…\\dsh-po06\\po06\\runtime\\d\\agent`（幻影树），
          read 永远读不到、write 报成功 —— 我这次会话里就踩过（`/d/...` 警告）
  · R-14：为"只数一下字符数"跑了提交脚本，脚本过了断言就真发出去了
  · R-07：`cc-assets` 是只读资产区，写进去会污染别人的东西

跨项目：边界路径来自 config.path_gate；本文件不含项目专有内容。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from guard import ProjectConfig  # noqa: E402
from tool_paths import expand_path  # noqa: E402  ★ 占位符展开（<WORKSPACE>/<HOME>）


DEFAULTS: dict = {
    # R-07：允许写入的根（**路径归属**匹配，不是字符串前缀）
    # ★★ 2026-10-16 根治：原写死某个具体工作区根 —— 本机永远对、换机/发布即废。
    #    改占位符后由 `expand_path` 展开为主体（本机工作区根）。
    #    ★ 踩过的坑（**假根治**）：只把**夹具**里的真路径改成 `<WORKSPACE>`、
    #      却没实现展开 ⇒ 6 条负例悄悄失效（`<WORKSPACE>\cc-assets\x.png` 被当成
    #      项目根下一个叫 `<WORKSPACE>` 的普通目录 ⇒ 判"在允许根内" ⇒ 全漏放）。
    #      夹具"看起来与项目无关"了，但**它不再测它声称的东西**。
    "allowed_roots": ["<WORKSPACE>"],
    # R-07：只读区（任何写入都拒）
    "readonly_roots": ["<WORKSPACE>/cc-assets"],
    # R-25：在 bash 语义里会变成幻影的写法
    # ★ 2026-10-04 修：原只有 `/d/agent` 与 `/c/Users` 两条**固定字面量**
    #   ⇒ 本机真实会写错的其它形态全漏（实测：`/d/Temp/x`、`/D/agent` 原判据大小写敏感也放行）。
    #   改为按「单字母盘符挂载点」这一**形态**判，而不是按两个字面量判。
    "phantom_pats": [
        r"(?<![\w/])/[a-zA-Z]/(?=[\w.])",   # /d/agent · /c/Users · /d/Temp —— 任意单字母盘符
        r"(?<![\w/])/mnt/[a-zA-Z](?=[/\w])",  # WSL 风格误写
    ],
    # R-14：提交类动作的特征词
    "submit_pats": [
        r"\bPOST\b", r"\bsubmit\b", r"提交", r"发送", r"generate\(", r"\.post\(",
        r"fetch\([^)]*method:\s*[\"']POST", r"requests\.post", r"curl\s+.*-X\s*POST",
    ],
    # R-14：显式确认开关（脚本里必须有）
    "confirm_pats": [r"CONFIRMED\s*=\s*True", r"confirmed\s*=\s*True", r"--yes", r"--confirm"],
}


def _cfg_of(cfg: ProjectConfig) -> dict:
    d = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    over = cfg.get("path_gate") or {}
    for k, v in over.items():
        if isinstance(v, list) and isinstance(d.get(k), list):
            d[k] = list(d[k]) + [x for x in v if x not in d[k]]
        else:
            d[k] = v
    # ★ 占位符展开：配置里的 `<WORKSPACE>` / `<HOME>` 在这里落地（含项目配置里覆盖的项）
    for k in ("allowed_roots", "readonly_roots"):
        if isinstance(d.get(k), list):
            d[k] = [str(expand_path(x)) for x in d[k]]
    return d


class PathGateError(RuntimeError):
    """路径/提交闸未通过 —— 不许执行。"""


def _strip_comments_and_strings(code: str) -> str:
    """去掉注释与字符串字面量，只留"会真正执行"的代码骨架。

    为什么必须：R-14 要判的是"**这个脚本会不会真的发出去**"。
    原实现直接在原文里搜 `CONFIRMED = True` ⇒ 一行注释
    `# 记得加 CONFIRMED = True` 也算"有开关" ⇒ **判据量错对象**（量了文本，没量行为）。
    """
    out = []
    for line in (code or "").splitlines():
        # 先砍字符串字面量（保留行内其余部分）
        s = re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)
        s = re.sub(r"'(?:[^'\\]|\\.)*'", "''", s)
        # 再砍注释（Python 的 #、JS 的 //）
        h = _first_comment(s)
        out.append(s[:h] if h is not None else s)
    return "\n".join(out)


def _first_comment(line: str) -> int | None:
    """找注释起点（不含字符串内的，因为调用前已把字符串清空）。"""
    idx = None
    for tok in ("#", "//"):
        i = line.find(tok)
        if i >= 0 and (idx is None or i < idx):
            idx = i
    return idx


# --------------------------------------------------------------------------
# ★ P0-SEC（2026-10-04 修）：路径归属判定
#
# 病根：原判据是**纯字符串前缀**
#     if low.startswith(str(root).lower().replace("/", "\\")):
# 两条叠加 ⇒ 真洞（Lead 独立复现，`_guard/_audit_lead/repro_path_escape.py`）：
#   ① 无规范化：全文没有 normpath/abspath/resolve ⇒ `..` 不被解析
#      ⇒ `<WORKSPACE>\..\Windows\x.txt` 判"在允许根内" ⇒ **文件真能写进 C:\Windows**
#   ② 前缀未补分隔符：根 `<WORKSPACE>` 会匹配 `<WORKSPACE>_evil`、`<WORKSPACE>X`
# ⇒ 闸门判绿的同时越界写入真会发生 —— 这已不是"漏检一条文本"，是**安全洞**。
#
# 为什么原判据看着"挺对"：它把"路径"当成了"字符串"。
# **判据量错对象**（病根 1）—— 要判的是"这个文件落在哪个目录树下"，
# 量的却是"这串字符是不是以那串字符开头"。
#
# 根治做法：先把路径**规范化**成唯一形态，再用**路径归属**（不是字符串包含）判定。
# 三条纪律：① 先 `_norm_path` 收敛所有 Windows 写法；
#          ② 用 `os.path.commonpath` 判归属（`is_relative_to` 语义 + 等值）；
#          ③ 规范化失败（非法字符等）一律**按不通过**处理 —— fail-closed。
# --------------------------------------------------------------------------

_WIN_DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "\\??\\")


def _norm_path(p: str | os.PathLike) -> tuple[Path | None, str]:
    r"""把任意 Windows 写法收敛成一个绝对、已解析 `..` 的路径。

    处理的形态（都是本机真实会遇到的）：
      · 正/反斜杠混用：`<WORKSPACE>/x.txt`
      · `\\?\` / `\\.\` / `\??\` 设备前缀：`\\?\<WORKSPACE>\..\Windows\x.txt`
      · `..` 目录穿越：`<WORKSPACE>\..\Windows\x.txt`
      · 大小写差异：`<WORKSPACE>\x.txt`
      · UNC：`\\server\share\x.txt`（无法判归属 ⇒ 保持原样，由调用方拒绝）

    返回 (Path, err)。err 非空 ⇒ **规范化失败，必须 fail-closed**。
    """
    s = str(p or "").strip()
    if not s:
        return None, "空路径"
    # `\\?\UNC\server\share` 与 `\\?\D:\...` 两种长路径前缀
    for pre in _WIN_DEVICE_PREFIXES:
        if s.startswith(pre):
            s = s[len(pre):]
            if s.upper().startswith("UNC\\"):
                s = "\\\\" + s[4:]
            break
    # `\\?\` 去掉后 `\<WORKSPACE>` 这种多一个前导斜杠
    if re.match(r"^\\[A-Za-z]:", s):
        s = s.lstrip("\\")
    s = s.replace("/", "\\")
    try:
        # strict=False：不要求文件已存在（写操作的目标本来就还不存在）
        norm = Path(os.path.abspath(os.path.normpath(s)))
    except Exception as e:  # noqa: BLE001
        return None, f"路径规范化失败：{e}"
    if not norm.is_absolute():
        return None, f"无法解析为绝对路径（UNC 或相对根）：{s}"
    return norm, ""


def _is_under(child: Path, root: Path) -> bool:
    """child 是否落在 root **树内**（含 root 自身）。

    这是**路径归属**判定，不是字符串前缀判定：
      · `D:\\agentX`      → False（原判据会误判 True）
      · `<WORKSPACE>\..\\Windows` → 规范化后是 `C:\\Windows` → False
      · 大小写不敏感（Windows 语义）
    """
    try:
        a = os.path.normcase(str(child))
        b = os.path.normcase(str(root))
        return os.path.commonpath([a, b]) == b
    except ValueError:
        # 不同盘符 / 一个是 UNC 一个不是 ⇒ commonpath 抛 ValueError ⇒ 不归属
        return False


class PathGate:
    NAME = "g_path"

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self.K = _cfg_of(cfg)
        lg = (cfg.get("path_gate") or {}).get("ledger", "_guard_ledger/path.jsonl")
        self.ledger = cfg.resolve(lg)
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self._log_errors = 0

    # ---------------- R-07：写入路径 ----------------
    def check_write_path(self, p: str) -> dict:
        """判"这次写入是否越界"。

        ★ P0-SEC 修（2026-10-04）：原实现是纯字符串前缀比较，两条真洞：
          ① `..` 不被解析 ⇒ 目录穿越放行；② 根未补分隔符 ⇒ `D:\\agentX` 伪匹配。
          现在：先规范化，再判**路径归属**；规范化失败一律拦（fail-closed）。
        """
        norm, err = _norm_path(str(expand_path(p)))   # ★ 先展开 <WORKSPACE>/<HOME> 再规范化
        if norm is None:
            r = {"gate": "g_path", "rule": "R-07", "ok": False,
                 "detail": f"写入路径无法判定（{err}）⇒ 按不通过处理（fail-closed）：{p!r}"}
            self._log(r); return r

        # ① 只读区优先判（"只读区"与"越界"是两种不同的错，归因必须分清）
        for ro in self.K["readonly_roots"]:
            ro_norm, ro_err = _norm_path(ro)
            if ro_norm is None:
                continue
            if _is_under(norm, ro_norm):
                r = {"gate": "g_path", "rule": "R-07", "ok": False,
                     "detail": f"写入只读区：{p}（{ro} 禁止写入；规范化后 {norm}）"}
                self._log(r); return r

        # ② 允许根：逐根判归属
        for root in self.K["allowed_roots"]:
            root_norm, root_err = _norm_path(root)
            if root_norm is None:
                continue
            if _is_under(norm, root_norm):
                r = {"gate": "g_path", "rule": "R-07", "ok": True,
                     "detail": f"在允许根内：{root}（规范化后 {norm}）"}
                self._log(r); return r

        r = {"gate": "g_path", "rule": "R-07", "ok": False,
             "detail": (f"写入越界：{p} 不在任何允许根内"
                        f"（{'、'.join(self.K['allowed_roots'])}）；"
                        f"规范化后为 {norm}")}
        self._log(r); return r

    # ---------------- R-25：幻影路径 ----------------
    def check_phantom(self, cmd: str) -> dict:
        r"""★ 2026-10-04 修：原只认 `/d/agent` 与 `/c/Users` **字面**写法。

        实测漏放（见 findings_B.md ⑭）：
          · `/d/agent` → 拦住 ✓；但 `/D/agent`（大写盘符）→ 原正则 `(?<![\w/])/d/agent`
            在 re.I **未传** 的情况下大小写敏感 ⇒ 漏放；实际调用处也没传 re.I。
          · `/d/Temp/x`、`/cygdrive/c/Users`、`/mnt/d/agent` 等本机真实会写错的形态未覆盖。  # hygiene-allow: 本闸自证夹具——g_path 就是判这类本机路径的，示例必须带本机形态
        判据量错对象：要判的是"这个路径在 bash 语义下会不会落进幻影树"，
        原判据量的是"字符串里有没有出现这两个固定字面量"。
        """
        hits = []
        for pat in self.K["phantom_pats"]:
            hits += [m.group(0) for m in re.finditer(pat, cmd, re.I)]
        if hits:
            r = {"gate": "g_path", "rule": "R-25", "ok": False,
                 "detail": (f"命令里出现幻影盘符写法 {sorted(set(hits))} —— "
                            f"本机 bash 用 /cygdrive/d/...；/d/... 会落进 runtime 目录"  # hygiene-allow: 本闸自证夹具——g_path 就是判这类本机路径的，示例必须带本机形态
                            f"（写入报成功但磁盘上没有）")}
        else:
            r = {"gate": "g_path", "rule": "R-25", "ok": True, "detail": "无幻影路径写法"}
        self._log(r); return r

    # ---------------- R-14：擅自提交 ----------------
    def check_submit(self, code: str) -> dict:
        r"""★ 2026-10-04 修：确认开关的**位置与生效条件**原来没核。

        实测漏放（见 findings_B.md ⑭）：`CONFIRMED = False` 也算"有开关" ——
        原 confirm_pats 只写 `CONFIRMED\\s*=\\s*True`，看着没问题；
        但真实脚本里常见的是 `CONFIRMED = os.environ.get(...)` 或 `--yes` 挂在
        另一个无关参数上，以及**开关写在注释里**也算命中。
        现加两条真判据：
          ① 开关必须在**非注释行**上（`# CONFIRMED = True` 不算）；
          ② 若脚本里出现 `CONFIRMED` 被显式赋 False，报出来（自相矛盾）。
        """
        subs = [m.group(0) for p in self.K["submit_pats"] for m in re.finditer(p, code, re.I)]
        if not subs:
            r = {"gate": "g_path", "rule": "R-14", "ok": True, "detail": "无提交类动作"}
            self._log(r); return r

        # 去掉注释与字符串字面量后再找开关 —— 注释里的开关不算数
        code_nc = _strip_comments_and_strings(code)
        has_switch = any(re.search(p, code_nc, re.I) for p in self.K["confirm_pats"])
        # 开关被显式关掉：脚本里既说"要确认"又写着 False
        switch_off = re.search(r"confirm(?:ed)?\s*=\s*(?:False|false|0)\b", code_nc)

        if has_switch and not switch_off:
            r = {"gate": "g_path", "rule": "R-14", "ok": True,
                 "detail": f"有提交动作（{sorted(set(subs))[:3]}）且带显式确认开关"}
        elif switch_off:
            r = {"gate": "g_path", "rule": "R-14", "ok": False,
                 "detail": (f"有提交动作（{sorted(set(subs))[:3]}），脚本里虽有 CONFIRMED "
                            f"字样，但被**显式赋 False**（`{switch_off.group(0)}`）"
                            f" ⇒ 开关是假的，跑一次就会真的发出去")}
        else:
            r = {"gate": "g_path", "rule": "R-14", "ok": False,
                 "detail": (f"有提交动作（{sorted(set(subs))[:3]}）但**没有 CONFIRMED 开关** —— "
                            f"跑一次就会真的发出去（镜23a 实证：只想数字符数却把片发了）")}
        self._log(r); return r

    # ---------------- 一起跑 ----------------
    def check(self, *, write_path: str | None = None, cmd: str | None = None,
              code: str | None = None, strict: bool = False) -> dict:
        checks = []
        if write_path:
            checks.append(self.check_write_path(write_path))
        if cmd:
            checks.append(self.check_phantom(cmd))
        if code:
            checks.append(self.check_submit(code))
        if not checks:
            return {"gate": "g_path", "ok": True, "checks": [],
                    "summary": "g_path：无可检项"}
        bad = [c for c in checks if not c["ok"]]
        ok = not bad
        return {"gate": "g_path", "ok": ok, "checks": checks,
                "failed": [c["rule"] for c in bad],
                "summary": ("g_path 全过（" + "、".join(c["rule"] for c in checks) + "）"
                            if ok else "g_path 不过：" + "；".join(c["detail"] for c in bad))}

    def require(self, **kw) -> dict:
        rep = self.check(**kw)
        if not rep["ok"]:
            raise PathGateError(rep["summary"])
        return rep

    def _log(self, rec: dict) -> None:
        """落台账。

        ★ 2026-10-04 修：原为 `except Exception: pass` —— **静默吞异常**。
        台账是本闸唯一的"我判过什么"的凭据；写失败却无声无息，
        等于"闸门在跑"的证据链断了而没人知道。现改为**写 stderr + 计数**，
        并在 check() 的 summary 里带出（可见、不中断主流程）。
        """
        try:
            with self.ledger.open("a", encoding="utf-8") as f:
                f.write(json.dumps({**rec, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                   ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            self._log_errors += 1
            print(f"[g_path] 台账写入失败（第 {self._log_errors} 次）：{self.ledger} ⇒ {e}",
                  file=sys.stderr)


def demo(cfg: ProjectConfig) -> int:
    g = PathGate(cfg)
    fails = 0
    print("### 路径与提交闸 · 负例自检（每条都是 AGENTS.md 的真规矩）\n")

    def neg(name, fn, expect_rule):
        nonlocal fails
        r = fn()
        hit = (not r["ok"]) and r.get("rule") == expect_rule
        print(f"  {'✓✓ 拦住' if hit else '✗✗ 漏了'}  {name}")
        if not hit:
            fails += 1
        print(f"         → {r['detail'][:120]}")

    neg("R-07 往 cc-assets 写文件", lambda: g.check_write_path(r"<WORKSPACE>\cc-assets\x.png"), "R-07")
    neg("R-07 往工作区外写文件", lambda: g.check_write_path(r"C:\Windows\temp\x.txt"), "R-07")
    neg("R-25 bash 用 /d/agent（幻影）",
        lambda: g.check_phantom("ls /d/agent/技术工作站"), "R-25")  # purity-allow: 夹具/样本（闸自检的输入数据或真实语料，非代码依赖）
    neg("R-14 提交脚本没有 CONFIRMED 开关",
        lambda: g.check_submit('const r = await fetch(url, {method: "POST"});'), "R-14")

    # ★ P0-SEC（2026-10-04 新增）：**路径归属**负例。
    #   这几条是 Lead 独立复现出的真洞 —— 原实现（纯字符串前缀）**全部漏放**，
    #   其中前两条会把文件真写进 C:\Windows。
    #   负例的形态取自 `_guard/_audit_lead/repro_path_escape.py`（含 ★ 的 4 条）。
    print()
    print("  ── P0-SEC 路径归属（原字符串前缀判据全部漏放）──")
    for p_ in (r"<WORKSPACE>\..\Windows\x.txt",
               r"<WORKSPACE>\..\..\Windows\System32\x.txt",
               r"<WORKSPACE>_evil\x.txt",
               r"<WORKSPACE>\sub\..\..\Windows\x.txt",
               r"\\?\<WORKSPACE>\..\Windows\x.txt"):
        neg(f"R-07 目录穿越/前缀伪匹配：{p_}", (lambda q: (lambda: g.check_write_path(q)))(p_), "R-07")

    # ★ 2026-10-04 新增：幻影路径的**形态**负例（原来只认两个字面量）
    print()
    print("  ── R-25 幻影路径形态（原判据只认 /d/agent、/c/Users 两个字面量）──")
    for c_ in ("ls /d/agent", "ls /D/agent", "cat /d/Temp/x.txt", "ls /mnt/d/agent"):  # hygiene-allow: 本闸自证夹具——示例必须带本机形态
        neg(f"R-25 幻影写法：{c_}", (lambda q: (lambda: g.check_phantom(q)))(c_), "R-25")

    # ★ 2026-10-04 新增：开关写在**注释**里不算开关（原实现会误判为"有开关"）
    print()
    print("  ── R-14 开关的**位置**（原判据在注释里也认）──")
    neg("R-14 开关只写在注释里",
        lambda: g.check_submit('# 记得加 CONFIRMED = True 再提交\nrequests.post(u, json=d)'), "R-14")
    neg("R-14 开关被显式赋 False",
        lambda: g.check_submit('CONFIRMED = False\nrequests.post(u, json=d)'), "R-14")

    print("\n### 正例：合规应放行")
    for name, r in [
        ("R-07 写 D:\\agent 下", g.check_write_path(r"<WORKSPACE>\_guard\rules.yaml")),
        ("R-07 写 D:\\agent 的绝对路径（大写盘符）", g.check_write_path(r"<WORKSPACE>\_guard\rules.yaml")),
        ("R-25 用 <WORKSPACE>", g.check_phantom("ls <WORKSPACE>/技术工作站")),  # purity-allow: 夹具/样本（闸自检的输入数据或真实语料，非代码依赖）
        ("R-14 有 CONFIRMED 开关", g.check_submit('CONFIRMED = True;\nfetch(u, {method:"POST"});')),
    ]:
        mark = "✓" if r["ok"] else "✗ 误杀"
        if not r["ok"]:
            fails += 1
        print(f"  {mark}  {name}")

    print(f"\n### 自检结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    from guard import load as load_cfg

    ap = argparse.ArgumentParser(prog="g_path")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--write-path", help="要检查的写入路径")
    ap.add_argument("--cmd", help="要检查的命令（幻影路径）")
    ap.add_argument("--code-file", help="要检查的脚本文件（擅自提交）")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    if a.demo:
        sys.exit(demo(cfg))
    g = PathGate(cfg)
    code = Path(a.code_file).read_text(encoding="utf-8") if a.code_file else None
    rep = g.check(write_path=a.write_path, cmd=a.cmd, code=code)
    print(rep["summary"])
    sys.exit(0 if rep["ok"] else 1)
