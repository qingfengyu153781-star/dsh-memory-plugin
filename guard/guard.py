# -*- coding: utf-8 -*-
"""_guard · 配置装载器（v2：自动识别项目 + 零写死路径）

v2 新增（治"换项目根太麻烦"）：
  · 项目根**不用传**：从当前工作目录往上一层层找指纹文件，自动认出是哪个项目
  · 指纹 = 该项目独有的一组文件（写在 yaml 的 fingerprint 里）
  · 也支持直接给目录；找不到就列出所有项目的指纹让人挑

设计要点（跨项目靠这一层做到）：
  · 闸门逻辑全在本工具层，**不含任何项目专有路径/文件名**
  · 项目差异全部从 projects/<名字>.yaml 读入
  · 因此「换一个目录打开就能用」= 零参数
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

GUARD_ROOT = Path(__file__).resolve().parent
PROJECTS_DIR = GUARD_ROOT / "projects"


# --------------------------------------------------------------------------
# 极简 YAML 读取（只支持本工具层用到的子集；不引外部依赖，任何项目裸跑）
# --------------------------------------------------------------------------
def _split_top(s: str, sep: str = ",") -> list[str]:
    """按 sep 切分，但忽略引号内的分隔符（用于解析 {k: "a, b"} 这种值）。"""
    out, buf, q = [], [], None
    for ch in s:
        if q:
            buf.append(ch)
            if ch == q:
                q = None
            continue
        if ch in "\"'":
            q = ch
            buf.append(ch)
        elif ch == sep:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


def _strip_comment(s: str) -> str:
    """去掉行尾注释，但保留引号内的 #。"""
    q = None
    for i, ch in enumerate(s):
        if q:
            if ch == q:
                q = None
            continue
        if ch in "\"'":
            q = ch
        elif ch == "#":
            # 前置需为空白才算注释（避免把 C#/tag#1 当注释）
            if i == 0 or s[i - 1] in " \t":
                return s[:i]
    return s


def _scalar(tok: str):
    tok = _strip_comment(tok).strip()
    if tok == "" or tok == "~" or tok.lower() in ("null", "none"):
        return None
    if tok.lower() in ("true", "yes", "on"):
        return True
    if tok.lower() in ("false", "no", "off"):
        return False
    # 行内列表 [a, b]
    if tok.startswith("[") and tok.endswith("]"):
        inner = tok[1:-1].strip()
        return [_scalar(x) for x in _split_top(inner)] if inner else []
    # 行内映射 {k: v, k2: v2}  —— 断言公差 {abs_max: 0.02}、用例对象 {name: xx} 都用得到
    if tok.startswith("{") and tok.endswith("}"):
        inner = tok[1:-1].strip()
        d = {}
        if inner:
            for part in _split_top(inner):
                k, _, v = part.partition(":")
                if k.strip():
                    d[k.strip()] = _scalar(v)
        return d
    # 去引号
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        return tok[1:-1]
    # 数字
    try:
        if re.fullmatch(r"-?\d+", tok):
            return int(tok)
        if re.fullmatch(r"-?\d*\.\d+([eE]-?\d+)?", tok) or re.fullmatch(r"-?\d+[eE]-?\d+", tok):
            return float(tok)
    except ValueError:
        pass
    return tok


def _parse_yaml(text: str) -> dict:
    """缩进驱动的极简解析：支持 map / list / 嵌套 / 行内 [] {}。注释用 #。"""
    root: dict = {}
    stack: list[tuple[int, object]] = [(-1, root)]
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        body = raw.strip()
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if body.startswith("- "):
            item = body[2:].strip()
            if not isinstance(parent, list):
                continue
            # 行内映射：- {k: v, k2: v2}
            if item.startswith("{") and item.endswith("}"):
                parent.append(_scalar(item))
                continue
            if ":" in item and not item.startswith(("[", "'", '"')):
                k, _, v = item.partition(":")
                d = {k.strip(): _scalar(v)}
                parent.append(d)
                stack.append((indent, d))
            else:
                parent.append(_scalar(item))
            continue

        if ":" in body:
            k, _, v = body.partition(":")
            k = k.strip()
            v = v.strip()
            if v == "":
                nxt = ""
                for j in range(i, len(lines)):
                    if lines[j].strip() and not lines[j].lstrip().startswith("#"):
                        nxt = lines[j].strip()
                        break
                child = [] if nxt.startswith("- ") else {}
                if isinstance(parent, dict):
                    parent[k] = child
                stack.append((indent, child))
            else:
                if isinstance(parent, dict):
                    parent[k] = _scalar(v)
    return root


# --------------------------------------------------------------------------
# 配置对象
# --------------------------------------------------------------------------
@dataclass
class ProjectConfig:
    name: str
    project_root: Path
    raw: dict = field(default_factory=dict)
    detected_by: str = ""

    def get(self, key, default=None):
        return self.raw.get(key, default)

    @property
    def spec_sources(self) -> list[str]:
        return list(self.raw.get("spec_sources") or [])

    @property
    def assertions(self) -> dict:
        return dict(self.raw.get("assertions") or {})

    @property
    def write_paths(self) -> list[str]:
        return list(self.raw.get("write_paths") or [])

    @property
    def protected(self) -> list[str]:
        return list(self.raw.get("protected") or [])

    @property
    def vault(self) -> dict:
        return dict(self.raw.get("vault") or {})

    @property
    def done_checks(self) -> list:
        return list(self.raw.get("done_checks") or [])

    @property
    def fingerprint(self) -> list[str]:
        return list(self.raw.get("fingerprint") or [])

    @property
    def language(self) -> dict:
        return dict(self.raw.get("language") or {})

    @property
    def plugins(self) -> list:
        return list(self.raw.get("plugins") or [])

    def resolve(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (self.project_root / p)

    def missing_spec_sources(self) -> list[str]:
        return [s for s in self.spec_sources if not self.resolve(s).exists()]

    def match_score(self, root: Path) -> int:
        """这个目录有多像本项目的根：命中的指纹文件数。"""
        return sum(1 for f in self.fingerprint if (root / f).exists())


# --------------------------------------------------------------------------
# 装载 / 自动识别
# --------------------------------------------------------------------------
def _deep_merge(base: dict, over: dict) -> dict:
    """over 覆盖 base；字典递归合并，其它类型直接覆盖（列表也覆盖）。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_raw(name: str, inherit_default: bool = True) -> dict:
    """读项目配置。默认把 _default 的通用部分合并进来（项目里没写的通用规矩照样生效）。"""
    yml = PROJECTS_DIR / f"{name}.yaml"
    if not yml.exists():
        raise FileNotFoundError(
            f"未找到项目配置：{yml}\n"
            f"可用项目：{available_projects()}\n"
            f"新增项目＝在 {PROJECTS_DIR} 下写一份 <名字>.yaml"
        )
    raw = _parse_yaml(yml.read_text(encoding="utf-8"))
    if inherit_default and name != "_default":
        d = PROJECTS_DIR / "_default.yaml"
        if d.exists():
            base = _parse_yaml(d.read_text(encoding="utf-8"))
            # 项目自己的写法优先；但通用层若项目缺项则补齐
            merged = _deep_merge(base, raw)
            merged["project"] = raw.get("project", name)
            merged["universal"] = False
            raw = merged
    return raw


def available_projects(include_default: bool = False) -> list[str]:
    """已配置的项目名。默认不含 _ 开头的通用/内部配置。"""
    out = []
    for p in sorted(PROJECTS_DIR.glob("*.yaml")):
        if p.stem.startswith("_") and not (include_default and p.stem == "_default"):
            continue
        out.append(p.stem)
    return out


def detect(start: str | os.PathLike | None = None, max_up: int = 6) -> tuple[str, Path, str] | None:
    """自动认出当前在哪个项目里。从 start 往上一层层试，返回 (项目名, 根, 命中说明)。

    命中规则：某层目录命中的指纹文件数 ≥ 2（避免同名文件误判），取命中最多者。
    """
    cur = Path(start or os.getcwd()).resolve()
    best: tuple[str, Path, str] | None = None
    for _ in range(max_up + 1):
        for name in available_projects():
            raw = _read_raw(name)
            fp = list(raw.get("fingerprint") or [])
            if not fp:
                continue
            hits = [f for f in fp if (cur / f).exists()]
            if len(hits) >= 2 and (best is None or len(hits) > len(best[2].split("、"))):
                best = (name, cur, "、".join(hits))
        if cur.parent == cur:
            break
        cur = cur.parent
    return best


def load(name: str | None = None, project_root: str | os.PathLike | None = None,
         cwd: str | os.PathLike | None = None, fallback: bool = True) -> ProjectConfig:
    """装载配置。四种用法：
       load()                    → 从 cwd 自动识别（零参数，推荐）
       load("巫山女")             → 指定项目名，根自动识别
       load("巫山女", r"D:\\...")  → 两者都指定
       load(fallback=True)       → 认不出项目时，退到通用默认配置（闸门逻辑照跑）
    """
    if name and project_root:
        return ProjectConfig(name=name, project_root=Path(project_root).expanduser(),
                             raw=_read_raw(name), detected_by="显式指定")
    if name and not project_root:
        raw = _read_raw(name)
        cur = Path(cwd or os.getcwd()).resolve()
        for _ in range(7):
            hits = [f for f in (raw.get("fingerprint") or []) if (cur / f).exists()]
            if len(hits) >= 2:
                return ProjectConfig(name=name, project_root=cur, raw=raw,
                                     detected_by="指纹：" + "、".join(hits))
            if cur.parent == cur:
                break
            cur = cur.parent
        raise FileNotFoundError(
            f"认不出『{name}』的项目根（当前目录 {cwd or os.getcwd()} 起往上 7 层都没命中指纹）。\n"
            f"  该项目的指纹文件：{raw.get('fingerprint')}\n"
            f"  请 cd 到项目里，或用 --project-root 显式指定。"
        )
    # 全自动
    hit = detect(cwd)
    if hit:
        nm, root, why = hit
        return ProjectConfig(name=nm, project_root=root, raw=_read_raw(nm),
                             detected_by="指纹：" + why)
    if fallback:
        # 通用默认：认不出项目也能用（闸门照跑，只是没有项目专有口径）
        d = _read_raw("_default")
        return ProjectConfig(name="_default", project_root=Path(cwd or os.getcwd()).resolve(),
                             raw=d, detected_by="通用默认（未识别到具体项目）")
    raise FileNotFoundError(
        "自动识别失败：当前目录（及上层 6 层）都认不出属于哪个项目。\n"
        f"  已配置项目：{available_projects()}\n"
        "  请 cd 到项目目录，或用 --project 指定。"
    )


def selfcheck() -> int:
    print("=" * 74)
    print("_guard 装载自检")
    print(f"  工具层位置：{GUARD_ROOT}")
    print(f"  可用项目  ：{available_projects() or '（无）'}")
    print("=" * 74)
    try:
        cfg = load()
        print(f"\n★ 当前目录自动识别 → 项目『{cfg.name}』")
        print(f"   项目根：{cfg.project_root}")
        print(f"   依据  ：{cfg.detected_by}")
    except FileNotFoundError as e:
        print("\n★ 当前目录自动识别：失败")
        print("   " + str(e).splitlines()[0])
    for name in available_projects():
        raw = _read_raw(name)
        print(f"\n■ {name}")
        print(f"  指纹（≥2 命中即认定）：{raw.get('fingerprint')}")
        print(f"  标准来源  ：{len(raw.get('spec_sources') or [])} 份")
        print(f"  断言条数  ：{len(raw.get('assertions') or {})}")
        print(f"  语言口径  ：{raw.get('language') or '（未设）'}")
        print(f"  插件清单  ：{len(raw.get('plugins') or [])} 项")
    return 0


if __name__ == "__main__":
    sys.exit(selfcheck())
