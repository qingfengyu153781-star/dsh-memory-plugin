# dsh-memory-plugin

一个**可安装的最小 DSH 插件骨架**：把 `D:\agent\dsh-rag-memory\bin\mem.py` 的跨会话记忆检索
暴露成模型可调用的 `memory_recall` 工具，并在每轮对话的第一个 step 自动把命中的历史片段注入上下文。

- 版本基线：**DSH 运行时 0.2.0-rc.2**（本机 `D:\deepseekzmd\resources\app.asar\dsh`，见 `../research/PLUGIN_API.md`）
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
| `DSH_MEMORY_SCRIPT` | `D:\agent\dsh-rag-memory\bin\mem.py` | mem.py 绝对路径 |
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

$ node -e "const m = require('D:/agent/dsh-rag-memory/plugin-src/dsh-memory-plugin/index.js'); console.log(...)"
REQUIRE-CHECK: OK -> {"name":"dsh-memory-plugin","inject":["tools"],"apply":"function"}

$ node test/selfcheck.mjs
PASS  import D:\agent\dsh-rag-memory\plugin-src\dsh-memory-plugin\index.js
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
dsh plugin --profile desktop add D:\agent\dsh-rag-memory\plugin-src\dsh-memory-plugin

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
持续监测由 `D:\agent\qa-gates\` 的 **G-14 记忆注入质量闸** / **G-15 记忆使用纪律闸** 承担。

**生效方式**：host 端插件**需重启 DSH**；重启后 stderr 应出现
`[dsh-memory-plugin] loaded v0.1.5 (auto=…, weak=hint, subagent=compact, dedup=30min, format=digest, …)`，
或访问 `GET /dsh-memory/health` 看 `version` 与 `policy`。

**修复账（含改前/改后 md5）**：`D:\agent\dsh-rag-memory\reports\FIX_LEDGER_memory_plugin_20261002.md`

### v0.1.4（2026-10-01）— query 清洗 + 回声过滤 + 6000 字上限

- `cleanQuery()` 加样板名单（运行环境措辞、插件辅助上下文、子代理通知…）；
- 去掉"命中里含自己上次注入"的回声条（注入块曾 13.8K→40K 滚雪球）；
- 注入块硬上限 6000 字。

### v0.1.3 及以前

- 每轮按 `agent/pre-step` 注入、`memory_recall` 工具、`/memory-recall` 命令、后台增量 ingest。

> **装后自测（2026-10-16 起）**：`test/` 已随包发布（在 `package.json` 的 `files` 白名单里）⇒ **已装副本里直接 `npm test` 即可自测**（selfcheck ／ inject 40 ／ 脑融合 10 ／ orch_regress 79）。此前白名单不含 `test/`，装后自测会 `MODULE_NOT_FOUND`——那是**发布口径问题，不是回归失败**。
