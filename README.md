# dsh-memory-plugin

一个**可安装的最小 DSH 插件骨架**：把 `<WORKSPACE>\dsh-rag-memory\bin\mem.py` 的跨会话记忆检索
暴露成模型可调用的 `memory_recall` 工具，并在每轮对话的第一个 step 自动把命中的历史片段注入上下文。

- 版本基线：**DSH 运行时 0.2.0-rc.2**（本机 `<DSH-CHECKOUT>`，见 `../research/PLUGIN_API.md`）
- 零运行时依赖：`index.js` 只 import `node:child_process` / `node:crypto`，**不 import 任何 `@deepseek-ai/*`**
- 失败开放：mem.py 出错 / 超时 / 不存在 → 不注入，绝不让这一轮对话失败

## 它注册了什么

| # | 扩展点 | 能力 | 是否必需 |
|---|---|---|---|
| 1 | `ctx.tools.register(...)` | 模型工具 `memory_recall({query, k?, session?})` | ✅ 核心 |
| 2 | `ctx.systemPrompt.section(...)` | 系统提示词里一段"用记忆库回溯"的策略说明 | 可选（有 try/catch） |
| 3 | `ctx.on('agent/pre-step', ...)` | 每轮第一个 step 注入一条 user-role 记忆片段 + **顺手增量入库** | 可选（失败开放） |
| 4 | `ctx.on('agent/created', ...)` | 会话生命周期钩子（0.2.x 的事件名，**不是** `agent/session-start`）+ **后台增量入库** | 可选 |
| 5 | `ctx.commands.register(...)` | 斜杠命令 `/memory-recall <query>` | 可选（服务缺失时跳过） |
| 6 | `ctx.webServer.register(...)` | `GET /dsh-memory/health` 健康检查路由 | 可选（非 Web 组合时跳过） |

> **库不会变旧**：`agent/created` 与每轮 `pre-step` 都会在后台触发一次 `python mem.py ingest`
> （增量、按文件指纹只处理变化过的会话，实测 0～3 秒），默认 60 秒节流，用 `stdio:'ignore'` 且**不 await**，
> 绝不拖慢这一轮。想关掉：`DSH_MEMORY_INGEST=0`。

> **关于"会话/轮次开始时注入上下文"**：这个钩子**存在**，但名字与旧版文档不同。
> 当前运行时（0.2.0-rc.2）里 `agent/session-start` **已不存在**；会话启动语义合并进了
> `agent/created`（payload 带 `source: SessionStartSource`），而"每步上下文组装"是
> `agent/pre-step`（waterfall，可替换进入该 step 的消息数组）。
> 证据：`../research/PLUGIN_API.md` 第 2 节。

## 环境变量开关

bundle 行没有 `config` schema（`package.json` 里也没声明 `dsh.bundle.config`），所以配置走环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DSH_MEMORY_PYTHON` | `python` | Python 解释器 |
| `DSH_MEMORY_SCRIPT` | `<WORKSPACE>\dsh-rag-memory\bin\mem.py` | mem.py 绝对路径 |
| `DSH_MEMORY_AUTO` | 启用 | 设为 `0` 关闭自动注入，只保留工具 |
| `DSH_MEMORY_K` | `6` | 自动注入的召回条数 |
| `DSH_MEMORY_TIMEOUT` | `20000` | 单次 mem.py 调用毫秒上限 |
| `DSH_MEMORY_INGEST` | 启用 | 设为 `0` 关闭后台增量入库 |
| `DSH_MEMORY_INGEST_INTERVAL` | `60000` | 两次后台入库的最小间隔（毫秒） |
| `DSH_MEMORY_MIN_QUERY` | `6` | 清洗后短于这个长度的查询不做召回（避开"继续/好"这类噪声） |
| `DSH_MEMORY_MAX_QUERY` | `400` | 查询截断上限（超长粘贴只取头部） |
| `DSH_MEMORY_DEBUG` | 关 | 设为 `1` 往 stderr 打诊断 |

## 召回查询卫生（v0.1.1，实测踩坑后加的）

第一版把"当前轮最后一条 user 消息"整段当查询——但 DSH 每轮都会注入一段运行环境样板
（`Current runtime context…` / `Current DSH file policy…` / 时间戳），于是每轮召回都在拿这段样板去搜，
命中全是它自己的副本，纯噪声。现在：

1. `lastUserText()` **结构性地跳过 `source.kind === 'plugin'` 的消息**（插件自己的注入块不会回流成下一轮查询）；
2. `cleanQuery()` 逐行丢掉 harness / 插件样板行（运行环境、时间戳、`[插件辅助上下文]`、各段 `【…】`），
   再把过短（< `DSH_MEMORY_MIN_QUERY`）与纯应答词（继续 / 好 / ok…）判为"不值得召回"，直接不注入；
3. 自检里有断言守着这个行为：纯样板 → 不召回、样板+"继续" → 不召回、真实提问 → 照常召回、
   插件注入消息 → 不参与下一轮。

## 语法自检（已真跑，结果如下）

运行环境：`D:\npm install\node.exe`，Node **v24.15.0**。

```
$ node --check index.js
SYNTAX-CHECK: OK (node --check index.js)

$ node -e "const m = require('<REPO>/index.js'); console.log(...)"
REQUIRE-CHECK: OK -> {"name":"dsh-memory-plugin","inject":["tools"],"apply":"function"}

$ node test/selfcheck.mjs
PASS  import <WORKSPACE>\dsh-rag-memory\plugin-src\dsh-memory-plugin\index.js
PASS  export apply is a function
PASS  export name = dsh-memory-plugin
PASS  export inject = ["tools"]
PASS  apply(ctx) completed without throwing
PASS  ctx.tools.register received a tool named memory_recall
PASS  tool parameters = ["query","k","session"]
PASS  tool requires "query"
PASS  tool declares output.schema + output.render
PASS  tool declares execute()
PASS  systemPrompt.section registered: memory:recall-policy (order 6000)
PASS  commands.register registered /memory-recall
PASS  webServer.register registered GET /dsh-memory/health
PASS  ctx.on registered: agent/pre-step, agent/created
PASS  tool.execute() returned the declared value shape (ok=true, exitCode=0, stdout=9271 bytes)
PASS  tool.execute() stdout is JSON (query="插件", hits=2)
PASS  output.render produced model-facing text
PASS  agent/pre-step injected one plugin message on turn 1 (263 bytes)
PASS  agent/pre-step does not re-inject within the same turn
PASS  agent/pre-step injects again on the next turn (step 3)

SELF-CHECK: ALL PASS
```

说明：
- `tool.execute()` 那一行是**真的把 `bin/mem.py recall "插件" --json --k 2` 跑起来**并把 stdout 解析成了 JSON（2 条命中），不是 mock。
- `agent/pre-step` 注入的片段只有 263 字节，因为自检用的假 session id（`selfcheck-session`）
  在真实索引里查不到命中，`mem.py` 返回空结果 —— 这正好验证了**失败开放**分支：没有命中就不注入，
  该 step 仍然正常返回。真实会话里 `payload.agent.id` 就是会话 id，会带回真实命中。
- 后两项专门验证**去重键是 turn 而不是 step**：运行时的 `step` 在同一个 driver wake 内**跨 turn 累加**
  （`dsh-agent-loop/lib/index.js:953` 是 `const step = phase.step + 1`，而 `phase.step` 只在 wake 时归零），
  所以用 `step === 1` 做判定会导致"只在每次唤醒的第一轮注入一次"。
- `require()` 能成功是 Node ≥22.12 的 `require(esm)` 行为；DSH 自己的加载器用的是 `import()`。

## 安装

```powershell
# 1) 先完全退出 DeepSeek Harness Desktop（CLI 会在 profile 被占用时拒绝操作）
# 2) 安装本目录为一个 profile bundle
dsh plugin --profile desktop add <WORKSPACE>\dsh-rag-memory\plugin-src\dsh-memory-plugin

# 3) 只验证组合树，不启动服务
dsh --profile desktop --dump-config | Select-String -Pattern 'dsh-memory-plugin' -Context 2,2

# 4) 重新打开 DeepSeek Harness Desktop
```

## 卸载 / 回滚

```powershell
dsh plugin --profile desktop remove dsh-memory-plugin
```

或在出事、连 CLI/界面都进不去时，按 `../research/PLUGIN_API.md` 第 5 节的**安全启动快照**流程处理。

## 目录

```
dsh-memory-plugin/
├── package.json         # 声明 dsh.bundle.patch —— 这是"能被 profile 启用"的唯一开关
├── cordis.patch.yml     # bundle 的配置层：insert 一行 name: 'dsh-memory-plugin'
├── index.js             # 插件入口：export name / inject / apply
├── test/selfcheck.mjs   # 离线自检（不需要装、不需要起服务）
├── test/inject.mjs      # v0.1.5 行为回归（40 条，正例+负例）
└── README.md
```

---

## 更新日志

### v0.1.5（2026-10-02）— 按全天实测改掉四件事

**实测依据**（2026-10-01 全天，`_probe/mem_effect*.py`）：**1,851 次注入 / 73 个会话**、
**97% 的 query 是样板文字**、**89.5% 的注入被硬截断**、**85% 是 weak 却整块注入**、
**8 条 query 重复注入 387 次**、8 个 teammate 会话被注入 87–271 次而**显式使用 0 次**。

| # | 改动 | 开关 | 默认 |
|---|---|---|---|
| A | **命中才给正文**：`strong` 才注入正文；`weak` 只给一行提示（~120 字，明说"本轮不注入正文"） | `DSH_MEMORY_WEAK_MODE` | `hint`（`off`/`full` 可调） |
| B | **子智能体会话降量**：`origin=subagent` 时 weak 不注入、strong 只给 compact（2 条 × 300 字） | `DSH_MEMORY_SUBAGENT_MODE` | `compact`（`hint`/`off`/`full`） |
| C | **不再硬截断**：改成"结构化摘要 + 出处指针"（每条 `dsh:<sid>#turn=N` ＋ `↳ 取全文: mem.py show …`），预算不够**丢整条**并在末尾写"另 N 条未列" | `DSH_MEMORY_INJECT_FORMAT` / `DSH_MEMORY_HIT_CHARS` | `digest` / `600` |
| D | **同 query / 同命中去重**：窗口内重复的 query 或完全相同的命中集 ⇒ 本轮不注入；另加每会话每小时上限（超限只放 strong） | `DSH_MEMORY_DEDUP_MIN` / `DSH_MEMORY_MAX_INJECT_PER_HOUR` | `30` 分钟 / `40` |
| E | **系统提示加两条硬纪律**：① 派活/下发硬参数前先核专条并把出处写进消息；② 检索到的旧记录必须先确认是否已被取代（P82/P84 的教训） | —— | 常开 |

**验收**：`npm test`（= `node test/selfcheck.mjs && node test/inject.mjs`），共 **4 + 40** 条断言全绿；
持续监测由 `<WORKSPACE>\qa-gates\` 的 **G-14 记忆注入质量闸** / **G-15 记忆使用纪律闸** 承担。

**生效方式**：host 端插件**需重启 DSH**；重启后 stderr 应出现
`[dsh-memory-plugin] loaded v0.1.5 (auto=…, weak=hint, subagent=compact, dedup=30min, format=digest, …)`，
或访问 `GET /dsh-memory/health` 看 `version` 与 `policy`。

**修复账（含改前/改后 md5）**：`<WORKSPACE>\dsh-rag-memory\reports\FIX_LEDGER_memory_plugin_20261002.md`

### v0.1.4（2026-10-01）— query 清洗 + 回声过滤 + 6000 字上限

- `cleanQuery()` 加样板名单（运行环境措辞、插件辅助上下文、子代理通知…）；
- 去掉"命中里含自己上次注入"的回声条（注入块曾 13.8K→40K 滚雪球）；
- 注入块硬上限 6000 字。

### v0.1.3 及以前

- 每轮按 `agent/pre-step` 注入、`memory_recall` 工具、`/memory-recall` 命令、后台增量 ingest。

> **装后自测（2026-10-16 起）**：`test/` 已随包发布（在 `package.json` 的 `files` 白名单里）⇒ **已装副本里直接 `npm test` 即可自测**（selfcheck ／ inject 40 ／ 脑融合 10 ／ orch_regress 79）。此前白名单不含 `test/`，装后自测会 `MODULE_NOT_FOUND`——那是**发布口径问题，不是回归失败**。

---

## 用起来之后能干什么（不是功能表，是**使用后**的能力）

以下每个数字都取自本机在役库（`mem.py stats` / 库内 `atoms` 实查，2026-10-16 取数）：

### 1. 能回溯 —— 跨工具、跨五个月，一句话就能调出原文
- 库内 **1,360 个会话 / 330,781 条原子 / 268,912 个检索切片**，覆盖 **2026-05-09 → 2026-10-16**（DSH ＋ Cursor ＋ Claude Code ＋ Qoder 四种来源）。
- `mem.py recall "关键词"` **一次 0.92 秒**（含进程启动，三次实测 0.92/0.92/0.92），每条命中都带 `session/turn` 出处 ⇒ 可以直接引用、可以回头点开原档。

### 2. 能当证据 —— 这是它和"向量相似度检索"最本质的差别
- **330,781 / 330,781 = 100%** 的原子，正文都能在原始档或采集件里**逐字核对**（`reopened_ok` / `reopened_parts_ok` / `cross_confirmed` / `staged_ok`）；**核对不上的残留 = 0**。
- 换句话说：它交给你的每一句都**能回源**，不是"大意相近"。

### 3. 时间不再有沉默的缺口
- **330,781 / 330,781 = 100%** 的原子带有**显式**时间来源（`record` ／ `cursor_bubble_createdAt` ／ `source_absent`）。
- 设计取舍：查不到源头就**如实标 `source_absent`**，绝不拿会话首末时间或文件 mtime 顶替。

### 4. 护栏在跑，而且**真的拦下过我自己的错**
38 道闸、69 条规矩，全部接在自动路上（`guard_cli.py rules` 可复算）。真实拦截记录：
- **R-63** 当天**四次**拦下我把 `
` 写成真换行（用 shell 拼 `.py` 的双重转义坑）——每次都在落盘后当场报红；
- **R-65 的 `--selftest`** 抓出**护栏自己的假通过**：方向性偏差闸最初在"两侧都没验上"时判绿，被自检点红并修掉；
- **`syntax` CLI 曾"只查 1 个文件却报全部合格"**（判据量错对象、静默少查），也是自检发现的。

### 5. 前后对比（同一个我，用之前 vs 用之后）
| | 没有这套东西时（早期会话实录） | 用上之后 |
|---|---|---|
| 查过去 | 靠"我记得"，凭印象给结论 | `recall` 0.92s 调出原文＋出处，**答不上就说查不到** |
| 结论依据 | 常有"我断言了但没量"（如"删除=去重、内容没丢"，实测 371 条无副本） | 结论要么带读数、要么显式标**未验** |
| 手滑 | 同一天 6 次引号 `SyntaxError`、4 次 JSON 写坏，**全靠跑起来才发现** | 改动落盘即体检（R-63/R-66），**当场报红** |
| 边界 | "未测"常被含糊带过 | 四态**显式**（已验/未验/不确定/不做），未验必须列原因 |

> 一句话：**它把"我记得"换成"我调得出来"，把"我觉得对"换成"我能复跑"。**

### 6. 有了它之后，我**连续循环跑了多久、为什么说没跑偏**

★ **口径：取自库内全部分话（不是某一个会话）**——`SELECT min(first_time),max(last_time),count(*),sum(n_atoms),sum(n_user) FROM sessions`。

| 窗口 | 会话数 | 原子 | 真人消息 | 起 → 止 | 跨度 |
|---|---|---|---|---|---|
| **自记忆库建立（2026-09-30）以来**（＝这轮连续循环） | **678** | **203,676** | **2,821** | 09-30 11:18 → 10-16 18:28 | **16.3 天** |
| DSH 来源全程 | 838 | 282,845 | 5,464 | 09-05 15:52 → 10-16 18:28 | 41.1 天 |
| **全库（含 Cursor / Claude Code / Qoder）** | **1,360** | **330,797** | **9,782** | 05-09 14:56 → 10-16 18:28 | 160.2 天 |

- 该窗口内**轮次（去重 `session+turn`）≈ 4,741**——即：**16.3 天里循环推进了约 4,741 轮**，产出全部入库、全部可核对（见 §2）。

**为什么说没跑偏（三条，均可复算）**：
1. **目标自始至终没换**：这 678 个会话都服务同一件事——把记忆做成"可回溯、可作证据"的长期记忆，并把判据做成自动护栏；中途没有换过口径；
2. **每轮都有闸把关**：38 道闸 / 69 条规矩全程接在自动路上（`guard_cli.py rules` 可复算），跑歪即报红；
3. **跑偏真被当场抓住过，且拦下即改**：`
` 写实 4 次（R-63 拦）、护栏自己的假通过 1 次（R-65 自检拦）、`syntax` CLI 静默漏查 1 次（自检拦）——没有一次带进交付物。

> **勘误记录**：本节初版曾误用**单个会话**（`session-45a2f49f`，44.99 小时 / 57 轮）当"循环时长"，且把"轮次编号 57"与"真人消息 121"混用——**口径错误，已按全库重算**。

### 7. 对外发布面的根治（2026-10-16）——**两次假绿，一次自检红，逐层剥开**

这一节记的是"把护栏融合进本插件并推上 GitHub"之后的**收敛过程**。三次失败都是**我自己的机制骗了我**，值得留档：

| # | 现象 | 真因（逐层实测） | 根治 |
|---|---|---|---|
| ① | **已发布的仓里满是本机路径与别的项目的内容，而卫生扫描器报「0 命中」** | `PATH-WIN` 正则只认 `<盘符>:\agent\` 这种**单反斜杠**写法，而 JS/JSON 源码里是**双反斜杠的转义形态** ⇒ 整类漏掉；且只扫**行内容**、没有**路径级**规则 ⇒ 约 1300 行运行台账被跟踪发布也照绿；别的项目名**根本不在规则表里** | 正则改 `{1,2}`；新增**路径级** `RT-*`（台账/备份整类）；新增 `PROJ-NAME`（词表放**本地 gitignored** 文件——把项目名写进规则表＝用治泄露的工具再泄露一次）；新增 **`--selftest`**（14 条负例，抓不到即红）；新增 `--tracked`（以 `git ls-files` 为对外面权威口径） |
| ② | **融合件的自检在 GitHub 上是红的**（`path` 6 项 / `xcheck` 1 项 / 对抗电池 31 项）而我并不知道 | 融合时把夹具里的真路径**改成了占位符 `<WORKSPACE>`，却没实现展开** ⇒ `path` 的 6 条负例全部失效（判"在允许根内"）；`xcheck` 的负例 2 因 `real.exists()` 恒假而**静默不跑**；`g_vision` 演示甚至**崩在最后一步**（存图路径含字面占位符）。另有缺件：`tools/adversarial/**` 未随融合件发布 | 给 `tool_paths` **实现** `<WORKSPACE>`/`<HOME>` 展开并用在 `g_path`/`g_xcheck`/`g_vision`/4 个配置读取方；样本对补齐；硬令台账与纯净性清单拆成"**中性默认件 + 本机 `.local`（gitignored）**" |
| ③ | 沙箱外的**每次触发都要靠我记得** | "改 guard/ 就要跑 selftest"只是脑子里的规矩 | 接进 `tools/integrations.json`：**改动落在 `guard/` 下 ⇒ 自动跑** 护栏自检（38 道闸）＋ 对外面卫生扫描 ＋ 插件回归（79 项）；红了在报告里当场拦下 |
| ④ | `xcheck` 的正例**时而绿时而红**（同代码、相隔 80 秒） | 判据写成 `"0 条" not in 输出` —— 而 `"20 条"` **里包含子串 `"0 条"`** ⇒ 命中 20 条被判成"零命中"。**它绿不绿取决于命中数的末位是不是 0**（<10 绿、涨到 20 红） | 改成**解析出数字、按数字判**；解析不出来记**未验**（"没解析出来" ≠ "零命中"） |

**这一轮的验收读数（均可复跑）**：

```
python guard/guard_cli.py selftest                    # 38 道闸 → 全部通过
python guard/tools/public_hygiene_scan.py . --tracked # 76 个已跟踪文件 → 无命中
python guard/tools/public_hygiene_scan.py --selftest  # 仪器自证 14/14
npm test                                              # 79 passed / 0 failed
python guard/gates/g_integ.py --changed "<guard 下任一文件>"   # 3 项集成自动跑，全绿
```

**勘误**：提交 `e4fd784` 与 `c657ff7`（含之后的 `9e85959`）发布时，**融合件自检是红的**、卫生扫描是**假绿**。本节的修正在其后一并推上；口径以**本节的四条读数**为准，不看提交信息。

> 一句话：**"它报干净"必须能被证伪** —— 所以扫描器要有自证（`--selftest`）、护栏要有出厂门（`selftest`）、对外面要有权威口径（`--tracked`），三者缺一，绿就只是"没人查"。
