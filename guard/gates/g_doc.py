# -*- coding: utf-8 -*-
"""_guard · 文档健康闸 g_doc —— 记忆文件自身不能违反它自己定的规矩

## 治的是什么（2026-10-03 用户点名「改」）

**背景**：`D:\\agent\\AGENTS.md` 涨到 **67,792 B**，超过**会话注入上限 65,536 B** ⇒
**末尾被截断**，而**被截掉的恰好是用户亲定的最高优先级铁律**：

```
## 🔴🔴🔴 最高优先级铁律（2026-10-02 用户明令）
# 做任何东西都要考虑合理性
**这条规则优先于本文件其余一切条款…**
```

**两个病灶，都是"文档结构违反了它自己的规矩"**：

| 病灶 | 违反了什么 | 判据 |
|---|---|---|
| **D1 超注入上限** | 文件太长 ⇒ **末尾静默失效**（不报错、不提示） | `字节数 ≤ limit` |
| **D2 最高优先级排在最后** | 自称"最高优先级"却排最末 ⇒ 必然第一个被截 | 含「最高优先级/铁律」的章节**必须在前 N%** |
| **D3 索引里塞详情** | 索引膨胀 ⇒ 挤掉正文（复盘索引曾占 59%） | 索引段占比 ≤ 阈值 |
| **D4 声明与事实不符** | 文件里写"XX 条规矩/XX 道闸门"，实际数不对 | 数字要能对上 |

**为什么必须做成闸**：这四条**没有一条会报错** ——
文件照样能存、能读、能提交，**只是悄悄失效**。这与表里"假绿"是同一个形态。

## 判据来源
  · 注入上限 65,536 B：2026-10-03 DSH 提示文件预算（实测报错原文
    `Workspace instruction budget 65536 bytes: truncated AGENTS.md from 67792 to 65244 bytes`）
  · 最高优先级须靠前：合理性原则（"最高"若排最后则名不副实）

跨项目：被检查文件/阈值/关键词来自 config.doc；本文件不含项目专有内容。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard import ProjectConfig  # noqa: E402

DEFAULTS = {
    # 会话注入上限（字节）。超了 ⇒ 末尾静默截断。
    "inject_limit": 65536,
    # 留多少余量算"安全"（余量太小 ⇒ 下次加一点就超）
    "safe_margin": 4096,
    # 被检查的记忆文件
    "files": ["AGENTS.md"],
    # 「最高优先级」类关键词 —— 出现这些的章节必须在文件前部
    "top_priority_pats": [r"最高优先级", r"最高优先", r"优先级最高"],
    # 前置比例：这些章节的**起点**必须落在文件的这个比例之前
    "front_ratio": 0.35,
    # 索引类章节名 —— 这些段只应是"索引"，占比不得过大
    "index_pats": [r"索引", r"目录", r"导航"],
    "index_max_ratio": 0.30,
    # 声明与事实：文件里若出现 "N 道闸门/N 条规矩"，须与实测一致
    # ★ 2026-10-03 修：原正则只认阿拉伯数字（`(\d+)`），而 AGENTS.md 写的是
    #   "**十六**道闸门" —— 中文数字 ⇒ **匹配不到 ⇒ 静默跳过 ⇒ 漏检**。
    #   与 pi-ai "字段名写错被静默丢弃" 同一类病：**判据没覆盖实际写法，就假装通过**。
    #   修法：两条正则都补上中文数字分支；并在下面加"M5 声明了却没解析出"告警。
    "claim_gates_pat": r"([0-9]+|[一二三四五六七八九十百]+)\s*道闸门",
    "claim_rules_pat": r"([0-9]+|[一二三四五六七八九十百]+)\s*条规矩",
    # 出现这些词，就说明"文件确实声称了数量"——若一个都没解析出来，要报而不是跳过
    "claim_marker_pats": [r"道闸门", r"条规矩"],
}


def _cfg_of(cfg: ProjectConfig) -> dict:
    d = {}
    for k, v in DEFAULTS.items():
        d[k] = list(v) if isinstance(v, list) else v
    over = cfg.get("doc") or {}
    for k, v in over.items():
        if isinstance(v, list) and isinstance(d.get(k), list):
            d[k] = list(d[k]) + [x for x in v if x not in d[k]]
        else:
            d[k] = v
    return d


class DocGate:
    NAME = "g_doc"

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self.K = _cfg_of(cfg)
        lg = (cfg.get("doc") or {}).get("ledger", "_guard_ledger/doc.jsonl")
        self.ledger = cfg.resolve(lg)
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self._log_errors = 0

    # ---------------- 单文件检查 ----------------
    def check_file(self, p: str | Path, *, live_gates: int | None = None,
                   live_rules: int | None = None) -> dict:
        path = Path(p)
        if not path.exists():
            # 相对路径按工作区根找
            alt = self.cfg.resolve(str(p))
            if alt.exists():
                path = alt
            else:
                rep = {"gate": self.NAME, "ok": False, "file": str(p),
                       "summary": f"文档健康：文件不存在 {p}"}
                self._log(rep)
                return rep
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        n = len(raw)
        rows: list[dict] = []

        # ---- D1：注入上限（超了 ⇒ 末尾静默失效）----
        limit = int(self.K["inject_limit"])
        margin = int(self.K["safe_margin"])
        over = n - limit
        if over > 0:
            rows.append({"id": "D1", "name": "不超注入上限", "ok": False,
                         "detail": (f"**{n} B 超过上限 {limit} B（超 {over} B）** ⇒ "
                                    f"**末尾会被静默截断**：不报错、不提示，"
                                    f"只是最后那段规矩**从此注不进会话**")})
        elif n > limit - margin:
            rows.append({"id": "D1", "name": "不超注入上限", "ok": True,
                         "detail": (f"{n} B ≤ {limit} B ✓ 但**余量仅 {limit-n} B**"
                                    f"（<安全线 {margin}）—— 再添一点就超，建议瘦身")})
        else:
            rows.append({"id": "D1", "name": "不超注入上限", "ok": True,
                         "detail": f"{n} B ≤ {limit} B（余量 {limit-n} B）✓"})

        # ---- D2：最高优先级必须靠前 ----
        #
        # ★ 2026-10-04 修（B-4 / B-5 / B-6）：
        #   原实现有三处范式级缺陷：
        #   ① **死代码**：`if near[2] > ratio and False: pass` —— 条件里写死 `and False`
        #      ⇒ 永远进不去，分支体还是 `pass`。与 g_behavior 那条
        #      `mx = max(mx, 2)`（阈值 3）**同一个病：写了一辈子走不到的分支**。
        #   ② **只看 `##`/`###` 标题**：正则 `^(#{2,3})\s+(.+)$` 要求"最高优先级"
        #      出现在二级/三级标题里。实测（findings_B.md ⑦）：
        #      写成 `# 🔴 最高优先级铁律`（一级标题）、或写成一个段落
        #      `**最高优先级铁律**：…`、或干脆**没有标题**只在正文里提 ——
        #      三种真实形态**全部被判"未声明，跳过"⇒ 放行**。
        #      而 2026-10-03 真实被截掉的那条，恰恰可能是正文段落形态。
        #   ③ **只要有另一处靠前就放行**：`bad = [命中且超过比例]`，
        #      而文件里"最高优先级"往往出现多次（第一处是正文引用，最后才是章节）
        #      ⇒ 取 `max` 只报最靠后那处的**阈值判断**，但**只要有一处在前就判绿**的
        #      逻辑写在了别处（`if not hits` 才跳过）⇒ 真凶（最后那处）被平均掉。
        #
        #   修法（根治）：
        #     · 命中范围扩到**三种形态**：任意级标题（`#`~`######`）、
        #       加粗行（`**…**`）、以及**独立的正文行**（含关键词且是短行）；
        #     · 判据改为"**命中里最靠后的那处**"必须在前 ratio —— 因为被截断
        #       伤的是**最后**那处（它是"读者的最后依靠"），不是最先那处；
        #     · 死代码删除。
        #
        #   判据量错对象（病根 1）在这里的具体形态：规矩说的是
        #   "**自称最高优先级的那段内容**必须靠前"，
        #   而原判据量的是"**标题文本里**有没有这些词"。
        hits = []
        for m in re.finditer(r"^(#{1,6})\s+(.+)$", text, re.M):
            title = m.group(2)
            if any(re.search(p, title) for p in self.K["top_priority_pats"]):
                hits.append((m.start(), title.strip(), "标题"))
        if not hits:
            # ② 加粗行 / 正文短行形态
            for m in re.finditer(r"^(\*\*[^\n]{0,40}?\*\*[^\n]{0,60})$", text, re.M):
                line = m.group(1)
                if any(re.search(p, line) for p in self.K["top_priority_pats"]):
                    hits.append((m.start(), line.strip(), "加粗行"))
        if not hits:
            # ③ 独立正文行（含关键词、且该行不长 ⇒ 是"声明"不是"顺带一提"）
            for ln in re.finditer(r"^([^\n]{0,80})$", text, re.M):
                line = ln.group(1)
                if not line.strip():
                    continue
                if any(re.search(p, line) for p in self.K["top_priority_pats"]):
                    hits.append((ln.start(), line.strip(), "正文行"))
        ratio = float(self.K["front_ratio"])
        if not hits:
            rows.append({"id": "D2", "name": "最高优先级靠前", "ok": True,
                         "detail": "本文件未以任何形态声明「最高优先级」，跳过"})
        else:
            positions = []
            for pos, title, kind in hits:
                positions.append((pos, title, len(text[:pos].encode("utf-8")) / max(1, n), kind))
            # ★ 判据：**最靠后**那处必须在前 ratio
            #   （被静默截断时，最后那处先消失 ⇒ 它才是风险点）
            worst = max(positions, key=lambda x: x[2])
            bad = worst[2] > ratio
            kinds = sorted({k for _, _, _, k in positions})
            if bad:
                rows.append({"id": "D2", "name": "最高优先级靠前", "ok": False,
                             "detail": (f"**「{worst[1][:28]}」（{worst[3]}）排在第 "
                                        f"{worst[2]:.0%} 处**（应在前 {ratio:.0%}）—— "
                                        f"自称最高优先级却排后面 ⇒ **必然第一个被截断**"
                                        f"（2026-10-03 实测：67,792 B 时它真被截掉了）；"
                                        f"本文件共 {len(positions)} 处声明（形态："
                                        f"{'、'.join(kinds)}）")})
            else:
                rows.append({"id": "D2", "name": "最高优先级靠前", "ok": True,
                             "detail": (f"{len(positions)} 处最高优先级声明（形态："
                                        f"{'、'.join(kinds)}），最靠后的一处在前 "
                                        f"{worst[2]:.0%}（≤ {ratio:.0%}）✓")})

        # ---- D3：索引段不得膨胀 ----
        idx_spans = []
        secs = [(m.start(), m.group(1)) for m in re.finditer(r"^##\s+(.+)$", text, re.M)]
        secs.append((len(text), "__END__"))
        for i in range(len(secs) - 1):
            st, title = secs[i]
            en = secs[i + 1][0]
            if any(re.search(p, title) for p in self.K["index_pats"]):
                idx_spans.append((title.strip(), len(text[st:en].encode("utf-8"))))
        imax = float(self.K["index_max_ratio"])
        if not idx_spans:
            rows.append({"id": "D3", "name": "索引段不膨胀", "ok": True,
                         "detail": "无索引类章节，跳过"})
        else:
            title, sz = max(idx_spans, key=lambda x: x[1])
            r = sz / max(1, n)
            rows.append({"id": "D3", "name": "索引段不膨胀", "ok": r <= imax,
                         "detail": (f"「{title[:24]}」占 {sz} B = 全文 {r:.0%}"
                                    + ("✓" if r <= imax else
                                       f" ✗ **超过 {imax:.0%}** —— 索引里塞了详情 ⇒ "
                                       f"**挤掉正文**（2026-10-03 实测：复盘索引占 59%，"
                                       f"把最高优先级铁律顶出注入范围）"))})

        # ---- D4：声明与事实一致 ----
        claims = []
        raw_seen = {"闸门": 0, "规矩": 0}
        if live_gates is not None:
            for m in re.finditer(self.K["claim_gates_pat"], text):
                v = _parse_count(m.group(1))
                if v is not None:
                    claims.append(("闸门", v, live_gates))
        if live_rules is not None:
            for m in re.finditer(self.K["claim_rules_pat"], text):
                v = _parse_count(m.group(1))
                if v is not None:
                    claims.append(("规矩", v, live_rules))
        # ★ M5：文件里明明写了"道闸门/条规矩"，却一个数字都没解析出来
        #   ⇒ 这是"判据没覆盖写法"的静默漏检，必须报出来，不许当"无声明"跳过。
        #
        # ★ 2026-10-04 修（B-8）：原判据只在 `n_hit>0 and n_parsed==0` 时报，
        #   即"**一处都没解析出来**"才算漏检。实测（findings_B.md ⑩）：
        #   文件里同时写 `17 道闸门`（可解析）与 `十七道闸门`（旧写法）
        #   ⇒ n_parsed=1>0 ⇒ **静默漏检**，D4 判绿。
        #   这跟 pi-ai "字段名写错被静默丢弃"是同一类病：
        #   **判据覆盖不全时，不是报"没覆盖到"，而是假装通过。**
        #
        #   ★ 但**不能**把 `每道闸门` / `这道闸门` 也算成数量声明 ——
        #     实测第一版修法就误报了 AGENTS.md 的"本文件的每道闸门"与
        #     "所以**每道闸门**要有个东西在跑它"（那是**泛指**，不是数量）。
        #     ⇒ 只有在"道闸门"**前面确实是数量位**（数字/中文数字，
        #        且中间没有其它名词）时，才算一处待核对的声明。
        #        否则它不是声明，无从核对 —— 既不该拦，也不该静默当"已核对"。
        unresolved_ctx = []
        n_claims_seen = 0
        # 「数量位」= 紧挨着"道闸门/条规矩"的那个数量词。
        # 允许：阿拉伯数字、中文数字、以及**模糊量词**（若干/多少/几/数）
        #   —— 模糊量词也是一种"声明了却不给数"，必须报（否则就是假装核对过）。
        _NUM_OR_VAGUE = (r"([0-9]+|[一二三四五六七八九十百]+|若干|多少|几|数)\s*$")
        for kind, pat in (("闸门", r"道闸门"), ("规矩", r"条规矩")):
            for m in re.finditer(pat, text):
                ctx = text[max(0, m.start() - 14):m.start()]
                last = re.search(_NUM_OR_VAGUE, ctx)
                if last is None:
                    continue                      # 前面不是数量位 ⇒ 泛指，不核
                n_claims_seen += 1
                if _parse_count(last.group(1)) is None:
                    unresolved_ctx.append(f"{kind}：『…{ctx.strip()[-14:]}道闸门』"
                                          f"这一处的数量**没解析出来**"
                                          f"（写法『{last.group(1)}』超出可核对范围）")
        # ★ M6（2026-10-04 新增）：数量声明**自相矛盾** —— 同一文件里写了两套数
        #   实测（findings_B.md ⑩）：`17 道闸门…旧文档还写『十七道闸门』`
        #   两处解析结果**相同**，旧实现会因为 `(kind, claimed, actual)` 去重而
        #   只留一条 ⇒ 看不出"同一文件写了两套"。
        #   ⇒ 改为**按出现次数**统计：同一 kind 出现 >1 处不同写法就要报出来。
        contrad = []
        by_kind: dict[str, list] = {}
        for kind, claimed, _actual in claims:
            by_kind.setdefault(kind, []).append(claimed)
        for kind, vals in by_kind.items():
            if len(vals) > 1:
                contrad.append(f"{kind}：同一文件里出现 {len(vals)} 处数量声明 "
                               f"{vals} —— **口径不唯一**（须裁决以哪处为准）")
        unresolved = unresolved_ctx

        if not claims:
            if unresolved:
                rows.append({"id": "D4", "name": "声明与事实一致", "ok": False,
                             "detail": "**声明无法核对** —— " + "；".join(unresolved[:4])})
            else:
                rows.append({"id": "D4", "name": "声明与事实一致", "ok": True,
                             "detail": "无法核对（未提供实测值或文件未声明数量），跳过"})
        else:
            bads = []
            seen = set()
            for kind, claimed, actual in claims:
                key = (kind, claimed, actual)
                if key in seen:
                    continue
                seen.add(key)
                if claimed != actual:
                    bads.append(f"{kind}：文件写 {claimed}，实际 {actual}")
            if bads or unresolved or contrad:
                rows.append({"id": "D4", "name": "声明与事实一致", "ok": False,
                             "detail": "**声明与事实不符** —— "
                                       + "；".join((bads + contrad + unresolved)[:4])})
            else:
                rows.append({"id": "D4", "name": "声明与事实一致", "ok": True,
                             "detail": f"{len(seen)} 处数量声明与实测一致 ✓"})

        miss = [r for r in rows if not r["ok"]]
        rep = {
            "gate": self.NAME, "ok": not miss, "file": str(path), "bytes": n, "rows": rows,
            "summary": (f"文档健康：{path.name} {n} B，{len(rows)-len(miss)}/{len(rows)} 项通过"
                        + ("" if not miss else "；不过：" +
                           "；".join(r["detail"] for r in miss))),
        }
        self._log({**rep, "rows": None})
        return rep

    def check_all(self, **kw) -> dict:
        reps = [self.check_file(f, **kw) for f in self.K["files"]]
        ok = all(r["ok"] for r in reps)
        return {"gate": self.NAME, "ok": ok, "files": reps,
                "summary": ("文档健康：" + ("全部通过" if ok else
                            "；".join(r["summary"] for r in reps if not r["ok"])))}

    def _log(self, rec: dict) -> None:
        """落台账。

        ★ 2026-10-04 修：原为 `except Exception: pass` —— **静默吞异常**。
        D4 的对账结论（"文件写 17，实际 17"）是本闸的产出凭据，
        写失败却无声无息 ⇒ 等于"闸在跑"的证据链断了没人知道。
        现改为写 stderr + 计数（可见、不中断主流程）。
        """
        try:
            with self.ledger.open("a", encoding="utf-8") as f:
                f.write(json.dumps({**rec, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                   ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            self._log_errors += 1
            print(f"[g_doc] 台账写入失败（第 {self._log_errors} 次）：{self.ledger} ⇒ {e}",
                  file=sys.stderr)


# --------------------------------------------------------------------------
_CN_DIGIT = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _parse_count(s: str) -> int | None:
    """把'16'/'十六'/'二十'/'三十五'这类写法解析成整数。

    ★ 为什么必须有：AGENTS.md 正文习惯写中文数字（"十七道闸门"），
    而 D4 原正则只认阿拉伯数字 ⇒ 静默漏检（2026-10-03 实测踩到）。
    """
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if not all(ch in _CN_DIGIT or ch == "百" for ch in s):
        return None
    # 处理 "十" / "十七" / "二十" / "三十五" / "一百"
    total, cur = 0, 0
    for ch in s:
        if ch == "百":
            cur = (cur or 1) * 100
            total += cur
            cur = 0
        elif ch == "十":
            cur = (cur or 1) * 10
            total += cur
            cur = 0
        else:
            cur = _CN_DIGIT[ch]
    return total + cur


def _parse_count_in(ctx: str) -> int | None:
    """从一段**上下文尾巴**里取出紧邻的数量（★ 2026-10-04 新增，治 B-8）。

    用于逐处核对："道闸门"前 14 个字符内那个数量，到底解不解析得出来。
    取**最后一个**匹配 —— 它离"道闸门"最近。
    """
    found = re.findall(r"([0-9]+|[一二三四五六七八九十百]+)", ctx or "")
    for tok in reversed(found):
        v = _parse_count(tok)
        if v is not None:
            return v
    return None


def _live_counts(here: Path) -> tuple[int | None, int | None]:
    """实测闸门数与规矩数（用于 D4 对账）。

    ★ 2026-10-04 修（B-7 · 判据量错对象）：
      原实现 `gates = len([p for p in gd.glob("g_*.py")])` —— 数的是
      **`gates/` 目录下的文件个数**。但 AGENTS.md 里说的"十七道闸门"
      指的是 `_guard/projects/_default.yaml` 里 `gates:` 列表登记的条目数
      （也就是 CLI 真正会跑的闸门集合）。
      两者当前恰好都是 17 ⇒ **看着对得上，其实是巧合**：
        · 少登记一个（yaml 里有、目录里没有）⇒ 数字仍相等，漏报；
        · 留一个孤儿 .py（比如 `g_foo.py` 没登记）⇒ 数字虚高，误报。
      ⇒ 这与"全项目 40/40 接闸"的口径必须一致：**权威口径是 CLI 会跑的集合**。
      修法：**优先**读 `_default.yaml` 的 `gates:` 列表长度（真口径），
      文件个数作为**交叉复核**一并返回，两者不等时由调用方报出来。
    """
    gates = None
    files = None
    try:
        gd = here / "gates"
        files = len([p for p in gd.glob("g_*.py") if p.is_file()])
    except Exception as e:  # noqa: BLE001
        print(f"[g_doc] 数闸门文件失败：{e}", file=sys.stderr)
    # 真口径：_default.yaml 里登记的 gates 列表
    try:
        dy = here / "projects" / "_default.yaml"
        if dy.exists():
            m = re.search(r"^gates:\s*\[(.*?)\]", dy.read_text(encoding="utf-8"),
                          re.M | re.S)
            if m:
                gates = len([x for x in m.group(1).split(",") if x.strip()])
    except Exception as e:  # noqa: BLE001
        print(f"[g_doc] 解析 _default.yaml 的 gates 失败：{e}", file=sys.stderr)
    if gates is None:                 # 退化：没有 yaml 就退回文件个数，并说明
        gates = files
    rules = None
    try:
        ry = here / "rules.yaml"
        if ry.exists():
            # 用 _guard 自己的极简解析器（不引第三方依赖）
            sys.path.insert(0, str(here))
            try:
                from rules import load_rules  # type: ignore
                rules = len(load_rules().get("rules", []))
            except Exception:
                import yaml  # type: ignore
                d = yaml.safe_load(ry.read_text(encoding="utf-8"))
                if isinstance(d, dict) and isinstance(d.get("rules"), list):
                    rules = len(d["rules"])
                elif isinstance(d, list):
                    rules = len(d)
    except Exception as e:  # noqa: BLE001
        print(f"[g_doc] 数规矩失败：{e}", file=sys.stderr)
    if gates is not None and files is not None and gates != files:
        print(f"[g_doc] ⚠ 闸门数口径不一致：yaml 登记 {gates} 个 / 目录文件 {files} 个 "
              f"⇒ D4 用 yaml（真口径），请核对是否有漏登记或孤儿 .py", file=sys.stderr)
    return gates, rules


def demo(cfg: ProjectConfig) -> int:
    here = Path(__file__).resolve().parent.parent
    gates, rules = _live_counts(here)
    g = DocGate(cfg)
    fails = 0
    print("### 文档健康闸 · 实例自检\n")
    print(f"（实测：闸门 {gates} 道 / 规矩 {rules} 条 —— 用于 D4 对账）\n")

    print("### 负例 1：文件超注入上限（末尾静默失效）")
    fake = """# 假记忆文件

## 第一节
""" + "填充内容。" * 9000 + """

## 🔴 最高优先级铁律
这条本该最重要，却排在最后。
"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="docgate_"))
    fp = tmp / "AGENTS.md"
    fp.write_text(fake, encoding="utf-8")
    r = DocGate(cfg)
    r.K["files"] = [str(fp)]
    rep = r.check_file(fp, live_gates=gates, live_rules=rules)
    d1 = [x for x in rep["rows"] if x["id"] == "D1"][0]
    print(f"  文件 {rep['bytes']} B")
    print(f"  D1 {'✓拦下' if not d1['ok'] else '✗放行'}：{d1['detail'][:96]}")
    if d1["ok"]:
        fails += 1
    d2 = [x for x in rep["rows"] if x["id"] == "D2"][0]
    print(f"  D2 {'✓拦下' if not d2['ok'] else '✗放行'}：{d2['detail'][:96]}")
    if d2["ok"]:
        fails += 1
    print()

    print("### 负例 2：索引段塞详情（挤掉正文）")
    fake2 = """# 假记忆文件

## 复盘索引
""" + "- 某条复盘：非常长的详情……" * 3000
    fp2 = tmp / "AGENTS2.md"
    fp2.write_text(fake2, encoding="utf-8")
    rep2 = r.check_file(fp2, live_gates=gates, live_rules=rules)
    d3 = [x for x in rep2["rows"] if x["id"] == "D3"][0]
    print(f"  D3 {'✓拦下' if not d3['ok'] else '✗放行'}：{d3['detail'][:96]}")
    if d3["ok"]:
        fails += 1
    print()

    print("### 负例 3：声明数量与实际不符")
    fake3 = "# 假记忆文件\n\n当前：99 道闸门 / 88 条规矩。\n"
    fp3 = tmp / "AGENTS3.md"
    fp3.write_text(fake3, encoding="utf-8")
    rep3 = r.check_file(fp3, live_gates=gates, live_rules=rules)
    d4 = [x for x in rep3["rows"] if x["id"] == "D4"][0]
    print(f"  D4 {'✓拦下' if not d4['ok'] else '✗放行'}：{d4['detail'][:96]}")
    if d4["ok"]:
        fails += 1
    print()

    # ★ 2026-10-04 新增：三种**真实形态**的"最高优先级"（原判据只看 ## 标题 ⇒ 全瞎）
    #   形态出处：2026-10-03 被截掉的那条在 AGENTS.md 里是 `## 🔴🔴🔴 最高优先级铁律`，
    #   但**用户自己写的时候是散文段落**（"这条是 2026-10-02 你亲定的最高优先级"），
    #   而且文件里同类词出现多次（正文引用 + 章节标题）—— 原实现只认 `##`/`###`。
    print("### 负例 4（★新增）：『最高优先级』的三种真实形态排在最后都必须拦")
    forms = {
        "一级标题形态": "# 假记忆\n\n## 开头\n" + "填充。" * 3000 +
                        "\n# 🔴 最高优先级铁律\n这条本该最重要，却排在最后。\n",
        "加粗段落形态": "# 假记忆\n\n## 开头\n" + "填充。" * 3000 +
                        "\n**最高优先级铁律**：做任何东西都要考虑合理性。\n",
        "纯正文神态": "# 假记忆\n\n## 开头\n" + "填充。" * 3000 +
                      "\n顺带一提，这条是最高优先级的，别忘了。\n",
    }
    for label, txt in forms.items():
        fp_ = tmp / (label + ".md")
        fp_.write_text(txt, encoding="utf-8")
        rr_ = r.check_file(fp_, live_gates=gates, live_rules=rules)
        d2_ = [x for x in rr_["rows"] if x["id"] == "D2"][0]
        hit_ = not d2_["ok"]
        print(f"  {'✓✓拦下' if hit_ else '✗✗漏放'}  {label}：{d2_['detail'][:84]}")
        if not hit_:
            fails += 1
    print()

    print("### 负例 5（★新增）：同一文件出现两套数量 ⇒ 口径不一致必须拦")
    fp_mix = tmp / "mixed.md"
    fp_mix.write_text(f"# 假记忆\n\n本盘有 {gates} 道闸门、{rules} 条规矩，"
                      f"旧文档还写『十七道闸门』。\n", encoding="utf-8")
    rep_mix = r.check_file(fp_mix, live_gates=gates, live_rules=rules)
    d4m = [x for x in rep_mix["rows"] if x["id"] == "D4"][0]
    print(f"  D4 {'✓拦下' if not d4m['ok'] else '✗✗漏放'}：{d4m['detail'][:120]}")
    if d4m["ok"]:
        fails += 1
    print()

    print("### 负例 6（★新增）：数量写法超出正则（解析不出）不许当『无声明』跳过")
    fp_bad = tmp / "badnum.md"
    fp_bad.write_text("# 假记忆\n\n本盘共有若干道闸门、一批条规矩。\n", encoding="utf-8")
    rep_bad = r.check_file(fp_bad, live_gates=gates, live_rules=rules)
    d4b = [x for x in rep_bad["rows"] if x["id"] == "D4"][0]
    print(f"  D4 {'✓拦下' if not d4b['ok'] else '✗✗漏放'}：{d4b['detail'][:110]}")
    if d4b["ok"]:
        fails += 1
    print()

    print("### 正例：真实 AGENTS.md 应通过（或如实报出问题）")
    real = Path(r"<WORKSPACE>\AGENTS.md")
    if real.exists():
        rr = r.check_file(real, live_gates=gates, live_rules=rules)
        print(f"  {rr['summary'][:150]}")
        for row in rr["rows"]:
            print(f"     {'✓' if row['ok'] else '✗'} {row['id']} {row['detail'][:88]}")
    print()

    print(f"### 自检结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    from guard import load as load_cfg

    ap = argparse.ArgumentParser(prog="g_doc")
    ap.add_argument("--project")
    ap.add_argument("--project-root")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--file", help="要检查的文档")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cfg = load_cfg(a.project, a.project_root)
    here = Path(__file__).resolve().parent.parent
    lg, lr = _live_counts(here)
    g = DocGate(cfg)
    if a.demo:
        sys.exit(demo(cfg))
    if a.file:
        rep = g.check_file(a.file, live_gates=lg, live_rules=lr)
    else:
        rep = g.check_all(live_gates=lg, live_rules=lr)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(rep["summary"])
        rr = rep.get("rows") or []
        if not rr:
            for fr in rep.get("files", []):
                for row in fr["rows"]:
                    print(("  ✓ " if row["ok"] else "  ✗ ") + f"{row['id']} {row['detail']}")
        else:
            for row in rr:
                print(("  ✓ " if row["ok"] else "  ✗ ") + f"{row['id']} {row['detail']}")
    sys.exit(0 if rep["ok"] else 1)
