# -*- coding: utf-8 -*-
"""_guard · 版本闸 g_vault —— 回滚点必须是写操作的副产物，不是"记得另存"

治的条目：P1-27 P2-24 P2-26 P3-01 P3-02 P3-03 P3-16 P3-17 P67-01 P67-02 P67-20 P67-25 P67-26（13 条）

病根：本轮全程只覆盖同一个 .blend，.blend1 只有一层轮转（版本深度=1），
      用户点名的 v20 在 30 版之前 ⇒ 根本没有回滚点；
      最后靠从 46MB 会话日志里抠 100 行代码才复原几何。

根除做法：把"快照"挂进写操作本身——
  · snapshot()  在**每次写之前**自动落一份（滚动保留 N 份）
  · accept()    用户点头 ⇒ 生成带时间戳与理由的"认可版"
  · rollback()  回档是**一条命令**，回完自动比对验证
  · verify()    文本比 md5（字节级确定）/ 场景比像素差（"看起来一样"≠"真的一样"）

跨项目：写哪些路径由 config.write_paths 决定；本文件不含任何项目专有路径。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402

DEFAULT_KEEP = 50

# --------------------------------------------------------------------------
# ★ 2026-10-04 新增（task-7 · P1-性能）：默认排除目录
#
# 病根（Lead 报、本侧独立复现 `_B_evidence/scale_before.txt`）：
#   `targets()` 全递归 `write_paths`（真实项目是 `["."]` = `<WORKSPACE>` 整个根）
#   ⇒ **实测 82.1 秒 / 命中 62,109 个文件**，其中 **81.4% 落在噪音目录**
#     （`.venv` / `node_modules` / `_logs` / `site-packages` …；`.py` 就有 40,720 个，
#      绝大多数是第三方库）。
#   ⇒ "每次写操作前自动留回滚点"这一步**光找文件就要 83 秒、还要拷 6 万个文件**
#     ⇒ **它现实中根本挂不到写操作上**。
#     这才是 `g_vault` 从装上至今几乎没有真实记录的根本原因：
#     **不是没人用，是它慢到没法用**（7263 条历史记录全是探针触发的）。
#
# 为什么算"硬缺陷"而不是"性能优化"：
#   一个跑不动的闸 = 一个不存在的闸。它与"静默失效"是同一族 ——
#   表面上有这道闸、命令也能敲，**实际上它永远不在自动路上**。
#
# 三层修法（缺一不可）：
#   ① 排除清单（本节）—— 且**可配置**：放 `projects/*.yaml` 的 `vault.exclude`，
#      不写死在代码里（换项目不用改闸门代码）；
#   ② 改"按需"不"全扫" —— `snapshot(files=[...])` 只备份调用方声明的文件；
#   ③ 性能护栏 —— 命中数或耗时超阈值 ⇒ **显式报错并要求确认**，
#      不许静默跑 83 秒（否则又是一个"静默失效"变体）。
# --------------------------------------------------------------------------
DEFAULT_EXCLUDE = [
    # 虚拟环境 / 依赖（这些名字足够专用，误伤面小）
    ".venv", "venv", "node_modules", "site-packages", "dist-packages",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    # 版本控制 / 工具
    ".git", ".hg", ".svn", ".idea", ".vscode",
    # 本工具层自身（版本库、台账）—— ★ 这两条是**底线**，永不放开
    "_guard_vault", "_guard_ledger",
    # ★ 注意：`logs` / `tmp` / `temp` / `env` / `cache` 这类**通用名不进默认清单** ——
    #   实测它们会把 `%TEMP%` 下的整个项目都排掉（见 `_excluded` 的注释）。
    #   某项目确要排除，请在 `projects/<名>.yaml` 的 `vault.exclude` 里显式写。
]

# 性能护栏默认值（可被 projects yaml 的 vault.max_targets / vault.max_seconds 覆盖）
DEFAULT_MAX_TARGETS = 2000
DEFAULT_MAX_SECONDS = 5.0


class VaultTooBig(RuntimeError):
    """命中量超过护栏 —— 必须显式确认，不许静默跑几十秒。"""


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def now_stamp_us() -> str:
    """★ 2026-10-04 修：**微秒级**时间戳。

    病根：原 `now_stamp()` 只到秒，而 `snapshot()` 用 `versions/<stamp>/` 作目录、
    `f"{i:03d}_{src.name}"` 作文件名 ⇒ **同一秒内的第二次快照直接覆盖第一次**
    （不报错）。实测（见 findings_B.md ㉓/B-19）：两次快照后
    `versions/<stamp>/001_d.txt` 里只剩第二次的内容，
    但 index.jsonl 里**两条记录都在**、md5 各不相同
    ⇒ `list` 报两个版本，回档到第一个却拿到第二个的内容 —— **回滚点是有毒的**。
    这正是"回滚点必须是写操作的副产物"这条规矩的反面：
    它建了一个**看着存在、实际是别的版本**的回滚点。

    修法：时间戳加微秒（`%f`）⇒ 同秒内每次快照必得不同目录。
    """
    return time.strftime("%Y%m%d_%H%M%S_") + f"{time.time_ns() % 1_000_000_000 // 1000:06d}"


def md5_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def human(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{u}"
        n /= 1024.0
    return f"{n:.1f}TB"


# --------------------------------------------------------------------------
# 版本库
# --------------------------------------------------------------------------
class Vault:
    """一个项目的版本库。目录布局：<vault_dir>/versions/  + index.jsonl"""

    def __init__(self, cfg: ProjectConfig):
        v = cfg.vault or {}
        root_rel = v.get("dir") or "_guard_vault"
        self.cfg = cfg
        self.root = cfg.resolve(root_rel)
        self.versions = self.root / "versions"
        self.index = self.root / "index.jsonl"
        self.keep = int(v.get("keep", DEFAULT_KEEP))
        self.tracked_exts = [e.lower() for e in (v.get("extensions") or [".blend"])]
        # ★ task-7：排除清单**可配置**（projects/*.yaml 的 vault.exclude），
        #   默认见 DEFAULT_EXCLUDE。项目配置是**追加**，不是覆盖 ——
        #   保证"_guard_vault 自身永不入列"这条底线任何项目都改不掉。
        extra = [str(x) for x in (v.get("exclude") or [])]
        self.exclude = list(dict.fromkeys(DEFAULT_EXCLUDE + extra))
        self.versions.mkdir(parents=True, exist_ok=True)

    # ---- 采集：按 write_paths / extensions 找"要保护的资产" ----
    def targets(self, only: list | None = None, enforce: bool = True) -> list[Path]:
        r"""找出"要保护的资产"。

        ★ 2026-10-04 修（P1 · Lead 独立复现，本侧证据
        `_B_evidence/before_p1_explosion.txt`）：

        原实现递归扫 `write_paths`（真实项目是 `["."]` = 工程根），
        而**版本库自己就在工程根下**（`_guard_vault/`）⇒
        `versions/<stamp>/*` 里的备份文件**又被当成下一次的备份源**：

            第 1 次快照前 targets() = 1 个
            第 2 次 = 2 → 第 3 次 = 4 → 8 → 16 → 32     ★ **指数增殖**

        实测后果：源文件只有 1 个、快照 6 次（本应 6 条记录），
        实际 `index.jsonl` **64 条**、磁盘上 **63 个 .txt**。
        ⇒ 真实项目上**每次写操作都会把整个版本库再备份一遍**，磁盘占用指数上涨。
        （这也是上一轮"keep=3 目录数确实是 3，但 index 里 58 条 manual"那个怪现象的根因：
          `keep` 只压住了**目录数**，压不住**每次快照采集到的源数量**。）

        为什么原来没被发现 —— 这是"**判据量错对象**"的变体：
        `targets()` 声明要找的是"**项目的资产**"，实际量的是
        "**根目录下所有后缀匹配的文件**"，而版本库恰好也在根目录下。
        **"我在备份"这件事没有把自己排除出去** ⇒ 备份产生新的待备份物 ⇒ 自反馈。

        修法（根治）：
          ① 显式排除**版本库自身的整棵子树**（`self.root`）—— 备份不是资产；
          ② 排除索引文件本身；
          ③ 加**不变量自证**（见 `_demo`）：快照 N 次后 `len(targets())`
             不得随 N 增长。这是唯一能防"备份自我增殖"的判据 ——
             只读代码看不出来，**只有跑才现形**。

        ★ 2026-10-04 再修（task-7 · P1-性能）：
          ④ **排除噪音目录**（可配置，见 `DEFAULT_EXCLUDE` / `vault.exclude`）——
             实测排除前 62,109 个命中（82.1 秒），81.4% 在 `.venv`/`node_modules`/`_logs` 里；
          ⑤ **性能护栏** —— 命中数或耗时超阈值时抛 `VaultTooBig`，
             要求显式确认，**不许静默跑 83 秒**。
             （跑不动的闸 = 不存在的闸；这是"静默失效"的变体。）

        参数：
          only     —— **按需模式**：只对这些路径做快照（task-7 的主用法）。
                      传了它就**不做全量扫描** ⇒ 挂到写操作上是毫秒级。
          enforce  —— True 时超护栏抛 `VaultTooBig`；False 时只在 stderr 警告。
        """
        # ---- 按需模式：调用方已经知道要改哪些文件，不必扫全根 ----
        if only:
            vr0 = self.root.resolve()
            got = []
            for x in only:
                p = Path(x)
                # 相对路径按**项目根**解析（挂到写操作上时最常用的写法）
                if not p.is_absolute() and not p.exists():
                    p = self.cfg.resolve(str(x))
                got.append(self._accept_path(p, vr0))
            return [q for q in got if q is not None]

        t0 = time.perf_counter()
        out: list[Path] = []
        vault_root = self.root.resolve()
        max_n = int(self.cfg.vault.get("max_targets", DEFAULT_MAX_TARGETS) or 0)
        max_s = float(self.cfg.vault.get("max_seconds", DEFAULT_MAX_SECONDS) or 0)
        ex_abs = self._exclude_abs(vault_root)
        # ★★ 2026-10-04 Lead 修（判据可达性）：
        #   原实现在命中数超护栏时 **`break` 中途截断**，然后调 `_guard_scale`
        #   报错（除非 enforce=False）。问题：
        #     `enforce=False`（"知情放行"）时它**跳过报错但仍返回被截断的清单**
        #     ⇒ 调用方拿到一份**看起来完整、实际少了一半**的备份集
        #     ⇒ 备份**静默漏掉**护栏外的文件。这是"假绿"的又一变体：
        #       **返回了一个不完整的清单，而没有任何迹象表明它不完整。**
        #   ⇒ 修法：`truncated` 显式随结果回传（`self._last_truncated`），
        #     并且 **enforce=False 时也保留"曾截断"的痕迹**，不许无声给出半个集合。
        truncated = False
        for rel in self.cfg.write_paths:
            base = self.cfg.resolve(rel)
            if base.is_file():
                q = self._accept_path(base, vault_root)
                if q is not None:
                    out.append(q)
            elif base.is_dir():
                # ★ task-7：**遍历时就剪枝**，不是遍历完再过滤。
                #   原写法 `for p in base.rglob("*")` 会**实际走完** .venv/node_modules
                #   的每一个条目（6.2 万次 is_file 系统调用）—— 这在 Windows 上就是
                #   那 80+ 秒的来源。排除清单只在**过滤结果**，没省下**遍历成本**。
                #   ⇒ 这叫"判据量错对象"：要省的是**扫描**，量的却是**结果集**。
                for p in self._walk(base, ex_abs, vault_root):
                    q = self._accept_path(p, vault_root)
                    if q is not None:
                        out.append(q)
                        # ⑤ 命中数护栏：超了立刻停，不再白扫
                        if max_n and len(out) > max_n:
                            truncated = True
                            break
                if truncated:
                    break
        uniq = sorted(set(out))
        dt = time.perf_counter() - t0
        self._last_truncated = truncated
        self._guard_scale(uniq, dt, max_n, max_s, enforce=enforce)
        return uniq

    def _exclude_abs(self, vault_root: Path) -> set[str]:
        """把排除清单（名字）与版本库根、台账根，统一成**绝对路径小写集合**。

        用于遍历时按目录整棵剪枝。
        """
        s = {x.lower() for x in self.exclude}
        try:
            s.add(str(vault_root).lower())
        except Exception:  # noqa: BLE001
            pass
        try:
            s.add(str(self.root.parent.resolve()).lower() + "\\__never__")
        except Exception:  # noqa: BLE001
            pass
        return s

    def _walk(self, base: Path, ex_abs: set[str], vault_root: Path):
        """带剪枝的遍历：遇到排除目录**不再往下走**（省掉整棵子树的系统调用）。

        语义与 `rglob("*")` 一致（只产出文件），但跳过排除目录的整棵子树。
        """
        stack = [base]
        while stack:
            d = stack.pop()
            try:
                entries = list(d.iterdir())
            except (OSError, PermissionError):
                continue
            for e in entries:
                name = e.name.lower()
                if name in ex_abs:                 # 按**目录名**排除
                    continue
                try:
                    if e.is_dir():
                        # 版本库自身整棵跳过
                        if str(e.resolve()).lower() == str(vault_root).lower():
                            continue
                        stack.append(e)
                    elif e.is_file():
                        yield e
                except (OSError, PermissionError):
                    continue

    def _excluded(self, p: Path) -> bool:
        r"""p 是否落在排除目录里。

        ★ 判据是"**项目根之下**的相对路径里有没有这些目录名"，不是"全路径有没有"。

        ★ 2026-10-04 修（自查发现的误杀）：第一版用 `p.parts`（**全路径分量**）判，
        于是 `<HOME>\AppData\Local\Temp\<项目>\x.md` 里的 `Temp`
        命中排除清单里的 `temp` ⇒ **整个临时目录下的项目全被排除**，
        `snapshot(files=[...])` 直接备份 0 个（实测 `len(made)=0`）。
        这是本闸自己的老病复发 —— **判据量错对象**：
        要判的是"**资产在项目内的位置**"，量的却是"**这台机器上的绝对路径**"。
        ⇒ 改成先 `relative_to(project_root)` 再判分量。
        另外：`tmp`/`temp`/`logs`/`env` 这类**太通用的名字**从默认清单里去掉，
        它们误伤面大于收益（要排的项目请在 `vault.exclude` 里显式写）。
        """
        roots = []
        try:
            roots.append(self.cfg.project_root.resolve())
        except OSError:
            pass
        # 只对**项目根之下**的部分判分量；不在根下的（如按需传入的绝对路径）
        # 退化为"最后若干级里有排除名"也只认真正的目录型名字。
        for r in roots:
            try:
                rel = p.resolve().relative_to(r)
                parts = {x.lower() for x in rel.parts[:-1]}   # 不含文件名本身
                return any(x.lower() in parts for x in self.exclude)
            except (ValueError, OSError):
                continue
        parts = {x.lower() for x in p.parts[:-1]}
        return any(x.lower() in parts for x in self.exclude)

    @staticmethod
    def _inside_vault(p: Path, vault_root: Path) -> bool:
        """p 是否落在版本库子树内（用**路径归属**判，不是字符串前缀）。"""
        try:
            rp = p.resolve()
            return rp == vault_root or vault_root in rp.parents
        except Exception:  # noqa: BLE001
            # 解析不了 ⇒ 保守认为"在库内"（宁可少备份，也不许自我增殖）
            return True

    def _accept_path(self, p: Path, vault_root: Path) -> Path | None:
        """单文件准入判定：不合格返回 None。"""
        try:
            if not p.is_file():
                return None
        except OSError:
            return None
        # ① 排除版本库整棵子树（备份不是资产）
        if self._inside_vault(p, vault_root):
            return None
        # ② 排除索引文件本身
        try:
            if p.resolve() == self.index.resolve():
                return None
        except OSError:
            return None
        # ③ 排除噪音目录
        if self._excluded(p):
            return None
        if self.tracked_exts and p.suffix.lower() not in self.tracked_exts:
            return None
        return p

    def _guard_scale(self, targets: list[Path], dt: float,
                     max_n: int, max_s: float, enforce: bool) -> None:
        """性能护栏（task-7 第 3 条）。

        ★ 为什么必须有：原 `targets()` 静默跑 83 秒、拷 6 万个文件 ——
        调用方**完全不知道自己在触发一次全盘扫描**。这是"静默失效"的变体：
        没有报错 != 行为合理。护栏把"我扫了多少、花了多久"**变成必须看见的东西**。
        """
        problems = []
        if max_n and len(targets) > max_n:
            problems.append(f"命中 {len(targets)} 个文件 > 上限 {max_n}")
        if max_s and dt > max_s:
            problems.append(f"耗时 {dt:.1f}s > 上限 {max_s}s")
        if not problems:
            return
        msg = ("版本闸 g_vault：全量扫描规模超护栏 —— " + "；".join(problems)
               + "。\n  ⇒ 这通常说明 write_paths 圈得太大"
                 "（如 ['.'] 含 .venv/node_modules/_logs），"
                 "\n     或该用**按需模式**：vault.snapshot(files=[本次要改的文件])。"
                 "\n  ⇒ 如确需全量：在项目 yaml 里调 vault.max_targets / vault.max_seconds，"
                 "\n     或显式传 enforce=False 表示知情。")
        if enforce:
            raise VaultTooBig(msg)
        # ★ 2026-10-04 Lead：`enforce=False`（知情放行）时也要**留痕**，
        #   并且如果清单**被截断**（truncated），必须显式说"这份清单不完整" ——
        #   否则调用方会把半个集合当成全集去备份 ⇒ 静默漏备（"假绿"变体）。
        extra = ""
        if getattr(self, "_last_truncated", False):
            extra = ("\n  ⇒★★ 且本次清单**已被护栏截断**（返回的是**不完整**的集合）"
                     "⇒ 不要把它当全集用于备份；请改用按需模式或调高上限。")
        print("[g_vault] " + msg + extra, file=sys.stderr)
        self._last_truncated = getattr(self, "_last_truncated", False)

    # ---- 记录 ----
    def _append(self, rec: dict) -> None:
        with open(self.index, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def records(self) -> list[dict]:
        if not self.index.exists():
            return []
        out = []
        for line in self.index.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # ---- snapshot：写操作前自动落一份 ----
    def snapshot(self, reason: str = "auto", label: str | None = None,
                 files: list | None = None) -> list[dict]:
        r"""落一份快照。

        ★ task-7（P1-性能）：新增 `files=` —— **按需模式，这才是写操作的正常用法**。

        病根：原实现**每次快照都全量扫 write_paths**。真实项目 `write_paths=["."]`
        ⇒ 实测 82.1 秒 / 62,109 个文件（`_B_evidence/scale_before.txt`）
        ⇒ "每次写操作前自动留回滚点"这一步**根本挂不上去**。
        而写操作的特点是：**调用方本来就知道自己刚改了哪个文件**
        ⇒ 没有理由为此重扫整个工程。

        用法（挂到写操作上）：
            vault.snapshot(reason="改了分镜表", files=["分镜/S01.md"])
            ⇒ 只备份这一个文件，毫秒级。

        不传 `files` 时退回全量扫描（保留给显式命令 / 首次建档），
        但会走**性能护栏**：超过 `vault.max_targets` / `max_seconds` 就抛
        `VaultTooBig`，**不许静默跑 83 秒**。
        """
        targets = self.targets(only=files) if files else self.targets()
        if not targets:
            return []
        stamp = now_stamp_us()          # ★ 微秒级，杜绝同秒覆盖
        made: list[dict] = []
        for i, src in enumerate(targets, 1):
            rel = src.relative_to(self.cfg.project_root)
            dest_dir = self.versions / stamp
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{i:03d}_{src.name}"
            # ★ 双保险：万一 stamp 仍撞车（手工指定等），绝不静默覆盖既有备份
            n = 1
            while dest.exists():
                dest = dest_dir / f"{i:03d}_{n}_{src.name}"
                n += 1
            shutil.copy2(src, dest)
            rec = {
                # ★ 2026-10-04 修：kind 只分 auto/manual 会让 _prune 判据量错对象。
                #   accept() 传 reason='accept'，结果 kind='manual' ⇒ _prune（只挑 'auto'）
                #   永远清不掉它 ⇒ **认可版无限增长**（见 findings_B.md ㉔/B-20）。
                #   现在显式记 accept 类，_prune 明确"认可版永不清理"。
                "kind": ("accept" if reason == "accept"
                         else ("auto" if reason == "auto" else "manual")),
                "stamp": stamp,
                "reason": reason,
                "label": label,
                "src": str(rel),
                "backup": str(dest.relative_to(self.root)),
                "bytes": src.stat().st_size,
                "md5": md5_of(src),
                "edges": int(src.stat().st_mtime),
                "taken_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._append(rec)
            made.append(rec)
        self._prune()
        return made

    # ---- accept：用户点头 ⇒ 认可版（带理由，可追溯）----
    def accept(self, reason: str) -> dict:
        recs = self.snapshot(reason="accept", label=reason)
        # 认可版另存一份"不会被滚动清理"的稳定副本
        keep_dir = self.root / "accepted"
        keep_dir.mkdir(parents=True, exist_ok=True)
        stamp = now_stamp_us()
        saved = []
        mismatched = []
        for r in recs:
            src = self.root / r["backup"]
            safe = "".join(c if c not in '\\/:*?"<>|' else "_" for c in reason)[:40]
            dest = keep_dir / f"{stamp}_{safe}_{Path(r['src']).name}"
            shutil.copy2(src, dest)
            # ★ 2026-10-04 修：原来**只记 md5 不校验**（见 findings_B.md ㉔）。
            #   "认可版"是最高价值的一份拷贝，必须当场比一次 md5，
            #   不一致要报出来 —— 否则等于把一个可能损坏的副本当成"用户认可的那版"。
            got = md5_of(dest)
            entry = {"src": r["src"], "accepted_copy": str(dest.relative_to(self.root)),
                     "md5": r["md5"]}
            if got != r["md5"]:
                entry["ok"] = False
                entry["got_md5"] = got[:12]
                mismatched.append(r["src"])
            else:
                entry["ok"] = True
            saved.append(entry)
        out = {"stamp": stamp, "reason": reason, "files": len(recs), "saved": saved,
               "all_match": not mismatched, "mismatched": mismatched}
        self._append({**out, "kind": "accepted_marker",
                      "taken_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        return out

    # ---- 滚动清理（认可版永不清理）----
    def _prune(self) -> int:
        """★ 2026-10-04 重写：原实现有**两个**判据量错对象（都是死代码级）。

        病根 A（更严重）：`stamps = {r['stamp'] for r in records() if r.get('kind') == 'auto'}`
          —— `snapshot()` 里 `kind = 'auto' if reason == 'auto' else 'manual'`，
          而**真实调用一律带理由**（`reason="auto"` 只在默认参数时出现）。
          实测（findings_B.md ⑲/B-15）：keep=3、连做 6 次带理由的快照，
          `kind` 计数 = `{"manual": 63}` ⇒ stamps 为空 ⇒ **while 从不执行**
          ⇒ `versions/` 残留 6 个目录，**"keep 滚动保留"完全失效、无限增长**。
          与 g_behavior 那条 `mx = max(mx, 2)` 同类：**分支条件永远不成立**。

        病根 B：判据量的是"index 里有哪些 stamp"，不是"磁盘上有哪些目录"。
          手工/外部删掉一个 `versions/<stamp>/` 后，该 stamp 仍在 index 里，
          `stamps.pop(0)` 把它弹掉（算作"清理了 1 个"），
          而真正该清的目录被留到最后 —— 实测目录数**不减少**（findings_B.md ⑳/B-16）。

        修法（根治，不是补丁）：
          ① 受管集合 = **磁盘上真实存在的版本目录**（唯一真相源），不再看 index；
          ② 认可版（`accepted/` 与其 `kind=='accept'` 的目录指针）明确排除；
          ③ 顺带把"index 有记录但目录已丢"的**孤儿记录**报出来（这是数据损坏，要可见）。
        """
        if not self.versions.exists():
            return 0
        # ① 唯一真相源：磁盘目录
        dirs_on_disk = sorted(p.name for p in self.versions.iterdir() if p.is_dir())
        # ② 认可版：不再参与滚动清理（accept 的备份目录名 = 其 stamp）
        accepted_stamps = {r.get("stamp") for r in self.records()
                           if r.get("kind") == "accept"}
        managed = [d for d in dirs_on_disk if d not in accepted_stamps]

        removed = 0
        while len(managed) > self.keep:
            old = managed.pop(0)
            d = self.versions / old
            if d.exists():
                shutil.rmtree(d)
                removed += 1

        # ③ 孤儿记录（index 说有、磁盘没有）—— 数据损坏，必须可见
        self._orphan_check(dirs_on_disk)
        return removed

    def _orphan_check(self, dirs_on_disk: list[str]) -> list[str]:
        """index 里记着、磁盘上却没有的备份 ⇒ 回滚点已失效，必须报出来。

        ★ 为什么必须报：`rollback()` 遇到这种情况只对**那一条**文件报
        `"快照文件已不在（可能被滚动清理）"`，但如果没人调用 rollback，
        这个"回滚点其实已经没了"的事实**永远不会浮现**
        —— 等到真出事要回档时才发现，那就晚了。
        这是"静默失效"，与本文件治的病同源。
        """
        on_disk = set(dirs_on_disk)
        orphans = []
        for r in self.records():
            if r.get("kind") not in ("auto", "manual", "accept"):
                continue
            b = r.get("backup")
            if not b:
                continue
            if not (self.root / b).exists():
                orphans.append(f"{r.get('stamp')}:{r.get('src')}")
        if orphans:
            self._append({"kind": "orphan_backups", "n": len(orphans),
                          "samples": sorted(set(orphans))[:10],
                          "taken_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        return orphans

    # ---- list ----
    def list_versions(self) -> list[dict]:
        seen: dict[str, dict] = {}
        for r in self.records():
            if r.get("kind") in ("auto", "manual"):
                seen.setdefault(r["stamp"], {"stamp": r["stamp"], "files": 0, "md5s": [], "reason": r.get("reason")})
                seen[r["stamp"]]["files"] += 1
                seen[r["stamp"]]["md5s"].append(r["md5"][:12])
        return [seen[k] for k in sorted(seen)]

    # ---- rollback：一条命令，回完自动验证 ----
    def rollback(self, which: str, dry_run: bool = False) -> dict:
        recs = self.records()
        auto = [r for r in recs if r.get("kind") in ("auto", "manual")]
        if not auto:
            return {"ok": False, "error": "版本库为空，无可回档的版本"}

        stamp_list = []
        for r in auto:
            if r["stamp"] not in stamp_list:
                stamp_list.append(r["stamp"])
        stamp_list.sort()

        # 支持：序号(从1起) / 时间戳 / md5 前缀
        chosen_stamp = None
        if which.isdigit():
            idx = int(which)
            if 1 <= idx <= len(stamp_list):
                chosen_stamp = stamp_list[idx - 1]
        if chosen_stamp is None:
            for s in stamp_list:
                if s == which:
                    chosen_stamp = s
                    break
        if chosen_stamp is None:
            for r in auto:
                if r["md5"].startswith(which) or r["md5"][:12] == which:
                    chosen_stamp = r["stamp"]
                    break
        if chosen_stamp is None:
            return {"ok": False, "error": f"找不到版本『{which}』；可用序号 1..{len(stamp_list)} 或时间戳/md5前缀",
                    "available": stamp_list}

        todo = [r for r in auto if r["stamp"] == chosen_stamp]
        result = {"ok": True, "stamp": chosen_stamp, "files": [], "dry_run": bool(dry_run)}
        for r in todo:
            src = self.root / r["backup"]
            dst = self.cfg.resolve(r["src"])
            if not src.exists():
                result["files"].append({"src": r["src"], "ok": False,
                                        "error": "快照文件已不在（可能被滚动清理）"})
                continue
            if dry_run:
                # ★ 2026-10-04 修：dry-run 原样把 ok 写成**无条件 True**，
                #   而 all_match 又据此计算 ⇒ 返回 all_match=True 却不含任何实测比对
                #   （见 findings_B.md ㉒/B-18）。"回完自动比对验证"这句声明在
                #   dry-run 下是假的。现在 dry-run 明确标 verified=False，
                #   并额外**只读**校验备份文件自身的 md5（不写磁盘、不回档）。
                backup_md5 = md5_of(src)
                result["files"].append({
                    "src": r["src"], "ok": backup_md5 == r["md5"],
                    "dry_run": True, "verified": False,
                    "expect_md5": r["md5"][:12], "backup_md5": backup_md5[:12],
                    "note": "未回档、未比对目标文件；此处校验的是**备份自身**是否完好",
                })
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            got = md5_of(dst)
            result["files"].append({
                "src": r["src"], "ok": got == r["md5"],
                "expect_md5": r["md5"][:12], "got_md5": got[:12],
            })
        if dry_run:
            # dry-run 下**不许**声称"逐字节一致" —— 根本没写任何目标文件
            result["all_match"] = None      # None = 未验证（不是"通过"）
            result["verified"] = False
        else:
            result["all_match"] = all(f.get("ok") for f in result["files"])
            result["verified"] = True
        self._append({"kind": "rollback", "stamp": chosen_stamp,
                      "taken_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "dry_run": bool(dry_run),
                      "files": result["files"], "all_match": result["all_match"]})
        return result

    # ---- verify：回档是否真的退回去了 ----
    def verify(self, src_rel: str, expect_md5: str) -> dict:
        p = self.cfg.resolve(src_rel)
        if not p.exists():
            return {"ok": False, "error": f"文件不存在：{p}"}
        got = md5_of(p)
        return {"ok": got == expect_md5, "file": src_rel, "expect": expect_md5[:12], "got": got[:12],
                "bytes": p.stat().st_size}


# --------------------------------------------------------------------------
# 像素差验证（场景/渲染类资产用："看起来一样"≠"真的一样"）
# --------------------------------------------------------------------------
def pixel_diff(a: Path, b: Path) -> dict:
    """用 ffmpeg 做像素差。返回 平均差 / 最大差 / 明显不同像素占比。"""
    exe = shutil.which("ffmpeg")
    if not exe:
        return {"ok": False, "error": "未找到 ffmpeg，无法做像素差验证（文本类请用 md5）"}
    # 只取第一帧做比对（够用于"是否同一版画面"）
    cmd = [exe, "-v", "error", "-i", str(a), "-i", str(b),
           "-filter_complex", "[0:v][1:v]blend=all_mode=difference,signalstats,metadata=print:file=-",
           "-frames:v", "1", "-f", "null", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"ffmpeg 执行失败：{e}"}
    yavg = None
    for line in out.splitlines():
        if "YAVG" in line or "average" in line.lower():
            try:
                yavg = float(line.split("=")[-1])
            except ValueError:
                pass
    if yavg is None:
        return {"ok": False, "error": "未能从 ffmpeg 输出解析出差值", "raw_tail": out[-400:]}
    # YAVG 是 0-255 尺度下的平均绝对差
    same = yavg <= 0.5
    return {"ok": True, "mean_diff_255": round(yavg, 4),
            "verdict": "同版" if same else "不同版（差异显著）",
            "threshold": 0.5}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _demo() -> int:
    """统一自检入口：与其余九道闸同样的 --demo 契约（rules.py 靠它验"闸还活着"）。

    跑的是一条**真**的端到端演练：临时文件 → 快照 → 改坏 → 回档 → 逐字节比对。
    全程在临时目录里，不碰项目资产。

    ★ 2026-10-04 修：原自检**只跑 reason='demo' 一次、keep=5** ⇒
      三个真缺陷全部漏过（原自检判绿，真实形态却照样失效）：
        ① `keep` 从不生效（kind 判据写错）—— 原自检只做 1 次快照，`len(stamps) > keep`
           永远不成立 ⇒ **该分支根本没被走到**；
        ② 同秒两次快照互相覆盖 —— 原自检两次操作中间没有第二次快照 ⇒ 走不到；
        ③ dry-run 假装验过 —— 原自检**从不调 dry_run**。
      ⇒ 这正是"demo 负例照着正则写"的自证循环：**自检只覆盖自己想到的形态**。
      现在补三段**基于真实失败形态**的负例（形态取自 2026-10-03 的真实事故与
      `_guard/_audit/_B_evidence/before_minrepro.txt` 的实测复现）。
    """
    import tempfile
    from pathlib import Path as _P
    fails = 0
    tmp = _P(tempfile.mkdtemp(prefix="g_vault_demo_"))

    class _Cfg:
        project_root = tmp
        vault = {"dir": "_v", "extensions": [".txt"], "keep": 5}
        write_paths = ["."]
        protected: list = []
        def get(self, k):
            return {"vault": self.vault, "write_paths": self.write_paths}.get(k)
        def resolve(self, p):
            return tmp / p

    v = Vault(_Cfg())  # type: ignore[arg-type]
    target = tmp / "asset.txt"
    target.write_text("原始内容 v1", encoding="utf-8")
    before = md5_of(target)

    snaps = v.snapshot(reason="demo")
    print(f"  ① 快照：{len(snaps)} 份  md5={before[:12]}")
    if not snaps:
        print("  ✗✗ 没落快照"); fails += 1

    target.write_text("被改坏了 v2", encoding="utf-8")
    broke = md5_of(target)
    print(f"  ② 改坏：md5={broke[:12]}")
    if broke == before:
        print("  ✗✗ 没改坏，演练无效"); fails += 1

    rows = v.list_versions()
    if not rows:
        print("  ✗✗ 版本列表为空"); fails += 1
    else:
        v.rollback(rows[-1]["stamp"])
        after = md5_of(target)
        ok = (after == before)
        print(f"  ③ 回档：md5={after[:12]}  "
              f"{'✓✓ 逐字节一致' if ok else '✗✗ 不一致'}")
        if not ok:
            fails += 1

    # ---------------- ★ 负例 1：keep 滚动保留必须真生效 ----------------
    print("\n  ── ★ 负例 1：keep 滚动保留（原实现：kind 判据写错 ⇒ 恒不触发）──")
    tmp2 = _P(tempfile.mkdtemp(prefix="g_vault_keep_"))
    class _Cfg2(_Cfg):
        project_root = tmp2
        vault = {"dir": "_v", "extensions": [".txt"], "keep": 3}
        def resolve(self, p):
            return tmp2 / p
    v2 = Vault(_Cfg2())  # type: ignore[arg-type]
    f2 = tmp2 / "k.txt"
    for i in range(1, 7):
        f2.write_text(f"版本 {i}", encoding="utf-8")
        # ★ 用**带理由**的快照（真实写操作都带理由）—— 原实现正是死在这条上
        v2.snapshot(reason=f"第{i}次改动")
    n_dirs = len([p for p in v2.versions.iterdir() if p.is_dir()])
    ok1 = n_dirs <= 3
    print(f"     keep=3，6 次**带理由**快照 ⇒ versions/ 目录数 = {n_dirs}  "
          f"{'✓✓ 滚动保留生效' if ok1 else '✗✗ 未清理（无限增长）'}")
    if not ok1:
        fails += 1

    # ---------------- ★ 负例 2：同秒两次快照不得互相覆盖 ----------------
    print("\n  ── ★ 负例 2：同秒两次快照不得覆盖（原实现：stamp 只到秒）──")
    tmp3 = _P(tempfile.mkdtemp(prefix="g_vault_same_"))
    class _Cfg3(_Cfg):
        project_root = tmp3
        def resolve(self, p):
            return tmp3 / p
    v3 = Vault(_Cfg3())  # type: ignore[arg-type]
    f3 = tmp3 / "s.txt"
    f3.write_text("内容甲", encoding="utf-8")
    v3.snapshot(reason="r1")
    f3.write_text("内容乙", encoding="utf-8")
    v3.snapshot(reason="r2")
    recs = [r for r in v3.records() if r.get("kind") in ("auto", "manual", "accept")]
    md5s = {r["md5"] for r in recs}
    on_disk = {md5_of(p) for p in v3.versions.rglob("*.txt")}
    ok2 = md5s.issubset(on_disk)
    print(f"     记录 {len(recs)} 条，md5 集合 {len(md5s)} 个；"
          f"磁盘实存 md5 集合 {len(on_disk)} 个")
    print(f"     {'✓✓ 每个记录的内容都在磁盘上（可回档）' if ok2 else '✗✗ 有记录的内容已被覆盖 ⇒ 该回滚点是假的'}")
    if not ok2:
        fails += 1

    # ---------------- ★ 负例 3：dry-run 不得声称"已验证" ----------------
    print("\n  ── ★ 负例 3：dry-run 不许假装验过（原 all_match 恒 True）──")
    dr = v3.rollback("1", dry_run=True)
    ok3 = dr.get("verified") is False and dr.get("all_match") is None
    print(f"     dry_run={dr.get('dry_run')}  verified={dr.get('verified')}  "
          f"all_match={dr.get('all_match')}")
    print(f"     {'✓✓ 如实报"未验证"' if ok3 else '✗✗ dry-run 谎称已比对'}")
    if not ok3:
        fails += 1

    # ---------------- ★ 负例 4：回滚点丢掉必须可见（不得静默）----------------
    print("\n  ── ★ 负例 4：备份被外部删除 ⇒ 必须报出来，不许静默 ──")
    tmp4 = _P(tempfile.mkdtemp(prefix="g_vault_orphan_"))
    class _Cfg4(_Cfg):
        project_root = tmp4
        def resolve(self, p):
            return tmp4 / p
    v4 = Vault(_Cfg4())  # type: ignore[arg-type]
    f4 = tmp4 / "o.txt"
    f4.write_text("原始", encoding="utf-8")
    v4.snapshot(reason="a")
    for d in list(v4.versions.iterdir()):
        shutil.rmtree(d)
    f4.write_text("又改一次", encoding="utf-8")
    v4.snapshot(reason="b")
    orphans = [r for r in v4.records() if r.get("kind") == "orphan_backups"]
    ok4 = bool(orphans)
    print(f"     手工删掉第一个备份后再快照 ⇒ orphan_backups 记录 {len(orphans)} 条  "
          f"{'✓✓ 已报出' if ok4 else '✗✗ 静默失效'}"
          + (f"（{orphans[-1].get('samples')}）" if orphans else ""))
    if not ok4:
        fails += 1

    # ---------------- ★ 负例 5：备份不得把自己当源（P1 指数增殖的不变量）----------------
    #   这是**唯一**能防"备份自我增殖"的判据：只读代码看不出来，只有跑才现形。
    #   形态出处：Lead 独立复现（1→2→4→8），本侧落盘 `_B_evidence/before_p1_explosion.txt`。
    print("\n  ── ★ 负例 5：targets() 不得把版本库自己的备份当源（P1）──")
    tmp5 = _P(tempfile.mkdtemp(prefix="g_vault_selfsrc_"))
    class _Cfg5(_Cfg):
        project_root = tmp5
        vault = {"dir": "_vault", "extensions": [".txt"], "keep": 5}
        def resolve(self, p):
            return tmp5 / p
    v5 = Vault(_Cfg5())  # type: ignore[arg-type]
    f5 = tmp5 / "s.txt"
    seq = []
    for i in range(1, 7):
        f5.write_text(f"内容{i}", encoding="utf-8")
        seq.append(len(v5.targets()))        # ★ 快照**前**采样
        v5.snapshot(reason=f"t{i}")
    invariant = len(set(seq)) == 1 and seq[0] == 1
    print(f"     6 次快照前 targets() 数量序列 = {seq}")
    print(f"     {'✓✓ 恒为 1（无自我增殖）' if invariant else '✗✗ 随次数增长 = 指数增殖（P1）'}")
    if not invariant:
        fails += 1
    # 记录数也不得翻倍：源 1 个 × 6 次 ≈ 6 条，留一点余量
    n_autoish = len([r for r in v5.records()
                     if r.get("kind") in ("auto", "manual", "accept")])
    rec_ok = n_autoish <= 8
    print(f"     index 记录数 = {n_autoish}（源 1 个 × 6 次 ≈ 6）  "
          f"{'✓' if rec_ok else '✗✗ 记录数翻倍 = 增殖'}")
    if not rec_ok:
        fails += 1
    # 磁盘文件数也不得翻倍
    n_txt = len([p for p in tmp5.rglob("*.txt")])
    disk_ok = n_txt <= 12
    print(f"     vault 下 .txt 文件数 = {n_txt}（P1 修前此处为 63）  "
          f"{'✓' if disk_ok else '✗✗ 磁盘文件翻倍'}")
    if not disk_ok:
        fails += 1

    # ---------------- ★ 负例 6：按需模式 + 性能护栏（task-7）----------------
    print("\n  ── ★ 负例 6：按需备份 + 性能护栏（task-7 · 全量扫描 83s 不可用）──")
    tmp6 = _P(tempfile.mkdtemp(prefix="g_vault_ondemand_"))
    class _Cfg6(_Cfg):
        project_root = tmp6
        vault = {"dir": "_vault", "extensions": [".md"], "keep": 5}
        def resolve(self, p):
            return tmp6 / p
    v6 = Vault(_Cfg6())  # type: ignore[arg-type]
    (tmp6 / "a.md").write_text("甲", encoding="utf-8")
    (tmp6 / "b.md").write_text("乙", encoding="utf-8")
    m6 = v6.snapshot(reason="按需", files=[str(tmp6 / "a.md")])
    ok61 = len(m6) == 1 and m6[0]["src"].endswith("a.md")
    print(f"     snapshot(files=[a.md]) ⇒ 备份 {len(m6)} 个 "
          f"{'✓✓ 只备份声明的文件（毫秒级）' if ok61 else '✗✗ 不符'}")
    if not ok61:
        fails += 1
    m6r = v6.snapshot(reason="按需-相对", files=["b.md"])
    ok62 = len(m6r) == 1
    print(f"     snapshot(files=['b.md'])（相对路径）⇒ {len(m6r)} 个 "
          f"{'✓' if ok62 else '✗✗ 相对路径未解析'}")
    if not ok62:
        fails += 1
    # 性能护栏：把上限压到 1，全量扫描必须**抛错**而不是静默跑完
    class _Cfg7(_Cfg):
        project_root = tmp6
        vault = {"dir": "_vault", "extensions": [".md"], "keep": 5,
                 "max_targets": 1, "max_seconds": 0}
        def resolve(self, p):
            return tmp6 / p
    v7 = Vault(_Cfg7())  # type: ignore[arg-type]
    (tmp6 / "c.md").write_text("丙", encoding="utf-8")
    (tmp6 / "d.md").write_text("丁", encoding="utf-8")
    try:
        v7.targets()
        print("     ✗✗ 超上限却静默跑完（护栏失效）")
        fails += 1
    except VaultTooBig as e:
        print(f"     ✓✓ 超上限抛 VaultTooBig：{str(e).splitlines()[0][:70]}")
    # 知情后放宽（enforce=False）应能跑完，且只在 stderr 警告
    got7 = v7.targets(enforce=False)
    ok7 = len(got7) > 1
    print(f"     enforce=False ⇒ 跑完并返回 {len(got7)} 个（知情放行）"
          f"{'✓' if ok7 else '✗✗'}")
    if not ok7:
        fails += 1

    print(f"\n  ### 自检结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}")
    return 0 if fails == 0 else 1


def _cli(argv: list[str]) -> int:
    import argparse

    # ★ 统一契约：--demo 自检（其余八道闸都有；rules.py 靠它验"闸还活着"）
    if argv and argv[0] == "--demo":
        # Windows 控制台默认 GBK，打印 ✓/✗ 会 UnicodeEncodeError
        # （表里 P 系列真坑：崩溃在打印阶段，此前的计算全作废）
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        return _demo()

    ap = argparse.ArgumentParser(prog="g_vault", description="_guard 版本闸：回档点是写操作的副产物")
    ap.add_argument("--project", required=True, help="项目名（对应 projects/<名字>.yaml）")
    ap.add_argument("--project-root", required=True, help="项目根目录（不写死，由这里传）")
    ap.add_argument("--reason", default="auto", help="快照理由")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("snapshot", help="落一份快照（写操作前调用）")
    pa = sub.add_parser("accept", help="标记认可版（用户点头时调用）")
    pa.add_argument("--note", default="用户认可", help="认可理由（进文件名，可追溯）")
    sub.add_parser("list", help="列出版本")
    pr = sub.add_parser("rollback", help="回档到指定版本并自动验证")
    pr.add_argument("which", help="序号(1起) / 时间戳 / md5前缀")
    pr.add_argument("--dry-run", action="store_true")
    pv = sub.add_parser("verify", help="校验某个文件当前 md5 是否等于期望值")
    pv.add_argument("file")
    pv.add_argument("md5")
    sub.add_parser("targets", help="列出本项目的受保护资产")

    a = ap.parse_args(argv)
    from guard import load as load_cfg

    cfg = load_cfg(a.project, a.project_root)
    v = Vault(cfg)

    if a.cmd == "snapshot":
        made = v.snapshot(reason=a.reason)
        print(f"SNAPSHOT {len(made)} 份 → {v.versions}")
        for m in made:
            print(f"   {m['md5'][:12]}  {human(m['bytes']):>8}  {m['src']}")
        return 0 if made else 1

    if a.cmd == "accept":
        out = v.accept(a.note)
        print(f"ACCEPTED {out['files']} 份  时间戳 {out['stamp']}  理由『{out['reason']}』")
        for s in out["saved"]:
            print(f"   {s['md5'][:12]}  {s['accepted_copy']}")
        return 0

    if a.cmd == "list":
        rows = v.list_versions()
        if not rows:
            print("（版本库为空）")
            return 0
        print(f"共 {len(rows)} 个版本（滚动保留最近 {v.keep} 个；认可版另存 accepted/ 不清理）")
        for i, r in enumerate(rows, 1):
            print(f"  [{i:>3}] {r['stamp']}  {r['files']:>2} 件  {r['reason'] or ''}")
        return 0

    if a.cmd == "rollback":
        res = v.rollback(a.which, dry_run=a.dry_run)
        if not res.get("ok"):
            print(f"ROLLBACK 失败：{res.get('error')}")
            if res.get("available"):
                print("  可用版本：" + ", ".join(res["available"]))
            return 2
        mode = "DRY-RUN（未写盘）" if a.dry_run else "实回"
        print(f"ROLLBACK({mode}) → {res['stamp']}")
        for f in res["files"]:
            flag = "✓" if f.get("ok") else "✗"
            print(f"  {flag} {f['src']}  期望 {f.get('expect_md5')}  "
                  f"实得 {f.get('got_md5') or f.get('backup_md5') or '-'}")
        if a.dry_run:
            # ★ dry-run **不许**打印"ALL_MATCH=True"假装验过了（见 findings_B.md ㉒）
            print("ALL_MATCH=未验证（dry-run 未写盘；上表校验的是备份自身是否完好）")
            return 0 if all(f.get("ok") for f in res["files"]) else 3
        print(f"ALL_MATCH={res['all_match']}")
        return 0 if res.get("all_match") else 3

    if a.cmd == "verify":
        r = v.verify(a.file, a.md5)
        print(("VERIFY OK  " if r["ok"] else "VERIFY FAIL") + f" {r.get('file')} 期望 {r.get('expect')} 实得 {r.get('got')}")
        return 0 if r["ok"] else 3

    if a.cmd == "targets":
        ts = v.targets()
        print(f"受保护资产 {len(ts)} 件（按 write_paths + extensions 采集）")
        for t in ts[:40]:
            print(f"   {human(t.stat().st_size):>9}  {t.relative_to(cfg.project_root)}")
        if len(ts) > 40:
            print(f"   … 另 {len(ts)-40} 件")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
