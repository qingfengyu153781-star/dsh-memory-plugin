/**
 * dsh-memory-plugin — DSH 跨会话记忆库插件（v0.1.5）
 *
 * 注册面（对齐 DSH runtime 0.2.x）：
 *   1. `ctx.tools.register(...)`       -> `memory_recall` 工具
 *   2. `ctx.systemPrompt.section(...)` -> 系统提示里的使用纪律
 *   3. `ctx.on('agent/pre-step', ...)` -> 每轮自动召回注入（本版重点改造）
 *   4. `ctx.on('agent/created', ...)`  -> 会话生命周期
 *   5. `ctx.commands.register(...)`    -> `/memory-recall`
 *   6. `ctx.webServer.register(...)`   -> `GET /dsh-memory/health`
 *
 * ===== v0.1.5 修的四件事（依据 2026-10-01 全天实测：1,851 次注入 / 97% query 是样板 /
 *        89.5% 被硬截断 / 85% 是 weak 却整块注入 / 8 条 query 重复注入 387 次）=====
 *   A. **命中才给正文**：`strong` 才注入正文；`weak` 只给一行提示（默认），不再整块灌。
 *   B. **子智能体会话降量**：`origin=subagent` 的会话只给 compact（默认 2 条 × 300 字），
 *      实测 8 个 teammate 会话被注入 87–271 次、显式使用 0 次。
 *   C. **不再硬截断**：改成"结构化摘要 + 出处指针"（每条给 session#turn + 取全文命令），
 *      预算内丢掉整条而不是从中间切断 JSON —— 旧版截断率 89.5%。
 *   D. **同 query / 同命中去重**：默认 30 分钟内同 query 或命中集未变 ⇒ 不注入。
 *   另：每会话每小时注入上限；系统提示加"派活前先核专条并写出处"（P82/P84 的教训）。
 *
 * 设计纪律（保持不变）：
 *   - 零 `@deepseek-ai/*` 导入（避免 profile 里解析出第二份模块实例）。
 *   - 每个非必要注册都 try/catch：apply() 抛异常会让整个 DSH 启动失败。
 *   - 召回失败**开放降级**：任何错误 = 本轮不注入，绝不弄坏一轮对话。
 *
 * 环境开关：
 *   DSH_MEMORY_PYTHON       python 解释器                默认 "python"
 *   DSH_MEMORY_SCRIPT       mem.py 路径                  默认 <记忆库>/bin/mem.py
 *   DSH_MEMORY_AUTO         "0" 关自动注入               默认 开
 *   DSH_MEMORY_K            注入召回条数                 默认 6
 *   DSH_MEMORY_TIMEOUT      mem.py 单次超时 ms           默认 20000
 *   DSH_MEMORY_INGEST       "0" 关后台增量入库           默认 开
 *   DSH_MEMORY_INGEST_INTERVAL  两次入库最小间隔 ms       默认 60000
 *   DSH_MEMORY_DEBUG        "1" 打诊断到 stderr           默认 关
 *   ---- v0.1.5 新增 ----
 *   DSH_MEMORY_WEAK_MODE        weak 时：hint|off|full    默认 hint
 *   DSH_MEMORY_SUBAGENT_MODE    子会话：compact|hint|off|full  默认 compact
 *   DSH_MEMORY_DEDUP_MIN        同 query 去重窗口 分钟     默认 30（0=关）
 *   DSH_MEMORY_INJECT_FORMAT    digest|raw                默认 digest
 *   DSH_MEMORY_HIT_CHARS        每条摘录上限（digest）     默认 600
 *   DSH_MEMORY_SUBAGENT_HITS    子会话最多给几条           默认 2
 *   DSH_MEMORY_MAX_INJECT_PER_HOUR  每会话每小时注入上限   默认 40（超限后只放 strong）
 *   DSH_MEMORY_SESSIONS_TTL_MS  origin 表缓存时长 ms       默认 600000
 */

/* __ORCH_WIRED__ 注意力编排层（2026-10-03 用户要求：默认关的右栏开关） */
import { orchestrateTurn as __orchTurn, registerRoutes as __orchRoutes, failureNotice as __orchFailureNotice, isOwnInjectionText as __isOwnInjection, logSkip as __orchLogSkip, isOrchEnabled as __orchEnabled, filterEchoCandidates as __filterEcho, echoThreshold as __echoThreshold, listContinuitySessions as __listSessions } from '../orchestrator/host.js'

/* ══ 本机路径解析（2026-10-16 根治：代码里不写本机路径）════════════════════
   顺序：环境变量 → `local.config.json`（随插件目录走、**已 gitignore**）
        → 自推导（插件在记忆库仓内时上溯即记忆库）→ 中性兜底。
   为什么：写死 `<工作区>\<记忆库>` 这类本机路径的插件**换台机器就废**；而且发布出去的写法
   还会**泄露本机布局**。★ 只写占位符不实现解析 = 写出一个不存在的路径
   （那是**假根治**，本项目 2026-10-16 实测过两次）。 */
const _HERE = dirname(fileURLToPath(import.meta.url))          // <plugin>/lib
const _PLUGIN = _pathResolve(_HERE, '..')                      // <plugin>
const _LOCAL_CFG = (() => {
  for (const p of [_pathJoin(_PLUGIN, 'local.config.json'),
                   _pathResolve(_PLUGIN, '..', '..', 'local.config.json')]) {
    try { if (_fsExists(p)) return JSON.parse(_fsRead(p, 'utf8')) } catch { /* 读不到继续 */ }
  }
  return {}
})()
const _DETECTED_BASE = (() => {
  for (const c of [_pathResolve(_PLUGIN, '..', '..'), _pathJoin(_homedir(), 'dsh-rag-memory')]) {
    try { if (_fsExists(_pathJoin(c, 'bin', 'mem.py'))) return c } catch { /* 继续 */ }
  }
  return _pathJoin(_homedir(), 'dsh-rag-memory')
})()
const MEM_BASE = process.env.DSH_MEMORY_BASE ?? _LOCAL_CFG.memoryBase ?? _DETECTED_BASE

/**
 * 解析某个会话的**真实项目**（会话工作目录）。
 *
 * 为什么需要：连续性候选的 project 标签必须是来源会话自己的项目。
 * 旧代码把 `continuity.project`（授权筛选条件）直接当标签贴上去 ——
 * 实测污染（2026-10-21）：18 条候选全标成 "PCM_RUNTIME_TEST"，
 * 而它们的 sourceSession 来自 4 个真实会话（示例项目甲/示例项目乙/本会话），
 * 于是「项目闸」看着很严，实际标签是配置值而非来源事实。
 *
 * 纪律：查不到就返回 ''（fail-closed），绝不猜测、绝不用当前会话顶替。
 * 缓存 60 秒，避免每轮都 spawn 一次 python。
 */
let __sessionProjectCache = { at: 0, map: null }
async function resolveSessionProject(baseDir, sessionId) {
  if (!sessionId) return ''
  const now = Date.now()
  if (!__sessionProjectCache.map || now - __sessionProjectCache.at > 60000) {
    try {
      const r = await __listSessions(baseDir)
      const map = new Map()
      if (r && r.ok) for (const s of r.sessions || []) map.set(s.sessionId, s.project || '')
      __sessionProjectCache = { at: now, map }
    } catch { return '' }
  }
  return __sessionProjectCache.map.get(sessionId) || ''
}

/**
 * 写侧判定：这一轮的内容能不能成为候选，候选该打什么项目标签。
 *
 * 抽成纯函数的原因（2026-10-21）：这段逻辑原本内联在 pre-step 里，
 * 而它恰好是「标签取错」的病根所在 —— 内联就只能靠运行态撞运气验证。
 * 现在 pre-step 调它、测试也调它，**同一份实现**，不会各写一套。
 *
 * 关键：**"来源项目未知"只在 project 模式下才意味着不能捕获**。
 *   - all：范围就是全部，标签未知只影响标注，不该阻止捕获；
 *   - sessions：范围由 selectedSessions 决定，与项目无关；
 *   - project：范围就是"项目等于 X"，来源项目未知 ⇒ 无法证明匹配 ⇒ 不捕获（fail-closed）。
 * 若把空项目判定提到模式分派之前，sessions/all 会在「老会话不在 --limit 200 列表里
 * 或 mem.py 超时」时静默停止捕获 —— 那是静默失效，不是防泄漏。
 */
export function candidateCaptureDecision({ continuity = {}, sourceProject = '', sessionKey = '', selfKey = '' } = {}) {
  const mode = continuity.mode
  if (continuity.enabled !== true || mode === 'off' || !MODES.has(mode)) return { allowed: false, project: '', reason: 'disabled' }
  if (mode === 'all') return { allowed: true, project: sourceProject, reason: 'mode-all' }
  // ★ 2026-10-22：范围已按对话分开存（scope），`selfKey` = 本对话 id。
  //   本对话自己的内容**永远允许**捕获 —— 否则本对话确认的东西下一轮就进不来，
  //   而"接上别人却丢了自己"正是这一版要避免的另一种错。
  //   身份比较一律过 normSessionId：实测两种前缀并存（`session-<uuid>` / 裸 `<uuid>`），
  //   按原串比会把"自己"判成"别人"，也会把接上的对话判成没接上。
  const self = normSessionId(selfKey)
  const picked = (Array.isArray(continuity.selectedSessions) ? continuity.selectedSessions : []).map(normSessionId).filter(Boolean)
  const sourceAllowed = (sid) => { const n = normSessionId(sid); return Boolean(n) && (n === self || picked.includes(n)) }
  if (mode === 'project') {
    if (!continuity.project) return { allowed: false, project: '', reason: 'scope-project-empty' }
    if (!sourceProject) return { allowed: false, project: '', reason: 'source-project-unknown' }
    if (continuity.project !== sourceProject) return { allowed: false, project: '', reason: 'project-mismatch' }
    // 工作区内可再限定对话：selectedSessions 非空 ⇒ 只在这些对话里捕获（与 inScope 同口径）。
    if (picked.length > 0 && !sourceAllowed(sessionKey)) return { allowed: false, project: '', reason: 'project-session-not-selected' }
    return { allowed: true, project: sourceProject, reason: 'project-match' }
  }
  return sourceAllowed(sessionKey)
    ? { allowed: true, project: sourceProject, reason: normSessionId(sessionKey) === self ? 'self-session' : 'session-selected' }
    : { allowed: false, project: '', reason: 'session-not-selected' }
}

const SCRIPT = process.env.DSH_MEMORY_SCRIPT ?? _LOCAL_CFG.memoryScript
  ?? _pathJoin(MEM_BASE, 'bin', 'mem.py')
// ★ <WORKSPACE> 语义 = 记忆库的父目录（本机 D:/agent）；也可在 local.config.json 里显式给
const _WORKSPACE = _LOCAL_CFG.workspace ?? _pathResolve(MEM_BASE, '..')
/* ★ HippoMem 接线层（2026-10-07 · 设计: <WORKSPACE>/记忆系统设计_20261005/ · 引擎: <WORKSPACE>/hippomem/hippo.py）
   策略（零新钩子兜底版）：反思触发挂在 agent/pre-step（轮 N 开始时检测「上一轮 N-1」的事件边界信号——
   用户纠错词 / [exit code: N≠0]），命中则后台 fire-and-forget spawn 引擎 reflect。
   fail-open 铁律：反思绝不阻塞对话；失败由引擎 failures 台账负责可见（非静默）。 */
const HIPPO_PY = process.env.DSH_HIPPO_SCRIPT ?? _LOCAL_CFG.hippoScript
  ?? _pathJoin(_WORKSPACE, 'hippomem', 'hippo.py')
const HIPPO_PYTHON = process.env.DSH_MEMORY_PYTHON ?? 'python'
// ★ 2026-10-17：Windows 控制台默认 GBK，Python 子进程的中文输出会变乱码
//   （实测 memory_hippo_status 台账里的「候选/记忆/原始层」全成 �）。
//   统一强制子进程 UTF-8：PYTHONIOENCODING + PYTHONUTF8 双保险。
const PY_ENV = { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' }
const HIPPO_AUTO = process.env.DSH_HIPPO_AUTO !== '0'
const HIPPO_COOLDOWN_MS = Number.parseInt(process.env.DSH_HIPPO_COOLDOWN_MIN ?? '20', 10) * 60000
const HIPPO_PROVIDER = process.env.DSH_HIPPO_PROVIDER ?? 'ccc'  // 实测 2026-10-07: official=402 / bbb 推理队列堵 / ccc 活
const CORRECTION_WORDS = ['你又错了', '不是这样', '我说的是', '重犯', '上次说过', '又来了', '搞错了', '返工', '荒唐', '不对', '错了', '你弄错了']
/** @type {Map<string,{lastAt:number}>} */
const hippoState = new Map()

export function hippoPrevUserText(messages) {
  // 上一轮（倒数第 2 条）真实用户文本：跳过 plugin:* 注入回声与记忆注入块（防误触发）
  let seen = 0
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (!m || m.role !== 'user') continue
    const src = String(m.source ?? '')
    if (src.startsWith('plugin:')) continue
    const t = typeof m.content === 'string' ? m.content : ''
    if (!t.trim()) continue
    if (t.includes('<memory_recall') || t.includes('<attention_orchestration')) continue
    seen++
    if (seen === 2) return t             // 1 = 本轮，2 = 上一轮
  }
  return null
}

export function hippoDetectTrigger(prevText) {
  if (!prevText) return null
  for (const w of CORRECTION_WORDS) if (prevText.includes(w)) return 'user-correction'
  const m = prevText.match(/\[exit code:\s*(\d+)\]/)
  if (m && m[1] !== '0') return 'task-failed'
  return null
}

function hippoMaybeReflect(payload, decision) {
  if (!HIPPO_AUTO) return
  try {
    const sid = payload?.agent?.id
    const turn = Number(payload?.turn)
    if (!sid || !Number.isFinite(turn) || turn < 2) return      // 第 1 轮没有上一轮可反思
    const trig = hippoDetectTrigger(hippoPrevUserText(decision?.messages ?? []))
    if (!trig) return
    let st = hippoState.get(sid)
    if (!st) { st = { lastAt: 0 }; hippoState.set(sid, st) }
    if (Date.now() - st.lastAt < HIPPO_COOLDOWN_MS) return      // 同会话冷却：连环纠错只跑一次
    st.lastAt = Date.now()
    const child = spawn(HIPPO_PYTHON, [HIPPO_PY, 'reflect', '--session', String(sid),
      '--turn', String(turn - 1), '--trigger', trig, '--provider', HIPPO_PROVIDER],
      { detached: true, stdio: 'ignore', windowsHide: true, env: PY_ENV })
    child.unref()                                               // fire-and-forget：对话链零等待
    try { process.stderr.write(`[hippo] 反思触发 sid=${String(sid).slice(0, 12)} turn=${turn - 1} trig=${trig}（后台）\n`) } catch { /* noop */ }
  } catch (e) {
    try { process.stderr.write(`[hippo] 触发检测失败（fail-open 不静默）：${e?.message ?? e}\n`) } catch { /* noop */ }
  }
}

import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
// ★ 2026-10-04 修正⑤-b：fs/path 走顶层 import —— 裸 require 在 ESM 上下文是 undefined，
//   try/catch 会把它静默吞成 ''；顶层 import 在原生 ESM 与转译 CJS 两种加载器下都合法。
import { existsSync as _fsExists, readFileSync as _fsRead } from 'node:fs'
import { resolve as _pathResolve, join as _pathJoin, dirname } from 'node:path'
import { homedir as _homedir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { loadContinuity, saveContinuity, updateContinuityPacket, confirmContinuityItem, captureContinuityCandidate, renderContinuityPacket, packetFromMessages, continuityPath, MODES, resetContinuity, rejectContinuityCandidate, rejectContinuityItem, classifyContinuityStatement, isSafeContinuityCandidate, scopeFor, saveScope, normSessionId, toBool } from '../continuity.js'

export const name = 'dsh-memory-plugin'
export const VERSION = '0.4.0'   // 0.4.0: 端口跟随（客户端只走当前页面同源）+ 新增 /dsh-memory/instance 实例身份探针（pid/startedAt）
// 2026-10-15(8): 第八轮 —— 删掉"形状闸"(用户否决：粗暴切割)，改按**信息增量**做回声过滤：重复提问照样能召回
// 运行实例身份：随插件宿主进程固定；重启后 pid 与 startedAt 都会变化。
const INSTANCE_STARTED_AT = Date.now() - Math.round(process.uptime() * 1000)
// 2026-10-15(7): 第七轮 —— MEM_BASE 可覆盖：测试台账写临时目录，不再污染生产台账（自检曾因此误报 broken）
// 2026-10-15(6): 第六轮 —— 纯状态陈述轮不回顾历史(确定性闸) + 判官输入补环境/时效 + 空手不再回落老路灌正文
// 2026-10-15(5): 第五轮 —— 跳过也要留痕（outcome:skipped，主会话才记），消掉"为什么没注入"的盲区
// 2026-10-15(4): 第四轮 —— 修我第二轮引入的回归（规范化后时间戳丢失/方括号截坏）
// 2026-10-15(3): 第三轮根治 —— llm 服务用 ctx.llm 直接取 + inject(['llm']) 等它来（不再用会间歇取不到的 ctx.get('llm')）

/** Cordis waits for these services before calling `apply`. */
export const inject = ['tools']

// ══════════════════════════════════════════════════════════════════════
// llm 服务取法（2026-10-15 第三轮根治）
// ──────────────────────────────────────────────────────────────────────
// 病根（实测，不是推测）：编排器调模型前用 `ctx.get('llm')` 取服务，**会间歇性取不到** ——
//   2026-10-15T10:34:44.426Z / .428Z 台账连续两条 `call-failed`：
//     error='llm service unavailable'  ms=0  model=deepseek-flash  candidates=1
//   `ms=0` + 该错误串 ⇒ 是 callModel 开头的 `if (!svc || typeof svc.stream !== 'function')`
//   抛的：**服务根本没取到**（连一次模型调用都没发出去）。
//
// 正确写法（读运行时源码得到，不是猜）：
//   `dsh-agent-loop/lib/index.js:1189` → `this.loopCtx.llm.prepareCall(proposedConfig, signal)`
//   —— DSH 的 Cordis 服务是**挂在 ctx 上的属性**（`ctx.llm` / `ctx.tools` / `ctx.webServer` …），
//   而本插件其它地方也正是这么用的（`ctx.tools.register`）。
//
// 修法（双保险，缺一不可）：
//   ① **等它来**：`ctx.inject(['llm'], …)` 拿到服务后**缓存**（与 `ctx.inject(['systemPrompt'])` 同一套写法）；
//   ② **直接取属性优先**：`ctx.llm` → 退回 `ctx.get('llm')`（兼容老 runtime）。
//   两者都拿不到才判失败，并且**把"看到了什么"写进错误里**（下次不必再猜）。
let __llmSvc = null

function llmOf(c) {
  try {
    if (c && c.llm && typeof c.llm.stream === 'function') return c.llm
    if (c && typeof c.get === 'function') {
      const s = c.get('llm')
      if (s && typeof s.stream === 'function') return s
    }
  } catch { /* noop */ }
  return null
}

function llmDiag(c) {
  const bits = []
  try { bits.push('ctx.llm=' + (c && c.llm ? typeof c.llm : 'undefined')) } catch { bits.push('ctx.llm=?') }
  try { bits.push('ctx.get=' + (c && typeof c.get)) } catch { bits.push('ctx.get=?') }
  try { bits.push('cached=' + (__llmSvc ? 'yes' : 'no')) } catch { /* noop */ }
  return bits.join(' / ')
}

// 导出供回归用例直测（2026-10-15(3)）
export { llmOf, llmDiag }

const PLUGIN_ID = 'dsh-memory-plugin'

const PYTHON = process.env.DSH_MEMORY_PYTHON ?? 'python'
// SCRIPT 已在上面的『本机路径解析』块里定义（环境变量 → local.config.json → 自推导）
// ★ 2026-10-15(6)：记忆库根**可被环境变量覆盖**（测试用）。
//   起因（实测）：`test/selfcheck.mjs` / `inject.mjs` 走的是真 `apply(ctx)`，
//   而编排器写台账用的是这里**写死的**路径 ⇒ 测试把 `selfcheck-session` 的失败/跳过
//   灌进了**生产台账**，把 `orchestrate-status` 的"最近 20 轮"染成零成功 ⇒ 自检误报 `STATUS=broken`。
//   （实测：最近 20 条 100% 是测试会话：selfcheck-session 14 / selfcheck-sub 4 / session-test 2）
//   修法：测试设 `DSH_MEMORY_BASE=<临时目录>` ⇒ 台账落临时目录，生产台账只留真实轮次。
// MEM_BASE 同上：已在上面的块里定义，这里不再写死
const AUTO = process.env.DSH_MEMORY_AUTO !== '0'
const K = Number.parseInt(process.env.DSH_MEMORY_K ?? '6', 10)
const TIMEOUT_MS = Number.parseInt(process.env.DSH_MEMORY_TIMEOUT ?? '20000', 10)
const DEBUG = process.env.DSH_MEMORY_DEBUG === '1'
/** keep the library fresh: incremental `mem.py ingest` in the background. */
const INGEST = process.env.DSH_MEMORY_INGEST !== '0'
const INGEST_INTERVAL_MS = Number.parseInt(process.env.DSH_MEMORY_INGEST_INTERVAL ?? '60000', 10)
const INGEST_TIMEOUT_MS = Number.parseInt(process.env.DSH_MEMORY_INGEST_TIMEOUT ?? '120000', 10)
const MIN_QUERY_CHARS = Number.parseInt(process.env.DSH_MEMORY_MIN_QUERY ?? '6', 10)
const MAX_QUERY_CHARS = Number.parseInt(process.env.DSH_MEMORY_MAX_QUERY ?? '400', 10)
/** 注入块硬上限：防止把自己上次的注入再召回一遍导致上下文滚雪球（实测见过 40K 字符）。 */
const INJECT_MAX_CHARS = Number.parseInt(process.env.DSH_MEMORY_INJECT_MAX ?? '6000', 10)
/** v0.1.5：weak / 子会话 / 去重 / 限流 策略 */
const WEAK_MODE = (process.env.DSH_MEMORY_WEAK_MODE ?? 'hint').toLowerCase()
const SUBAGENT_MODE = (process.env.DSH_MEMORY_SUBAGENT_MODE ?? 'compact').toLowerCase()
const DEDUP_MIN = Number.parseInt(process.env.DSH_MEMORY_DEDUP_MIN ?? '30', 10)
const INJECT_FORMAT = (process.env.DSH_MEMORY_INJECT_FORMAT ?? 'digest').toLowerCase()
const HIT_CHARS = Number.parseInt(process.env.DSH_MEMORY_HIT_CHARS ?? '600', 10)
const SUBAGENT_HITS = Number.parseInt(process.env.DSH_MEMORY_SUBAGENT_HITS ?? '2', 10)
const SUBAGENT_BUDGET = Number.parseInt(process.env.DSH_MEMORY_SUBAGENT_BUDGET ?? '1800', 10)
const MAX_INJECT_PER_HOUR = Number.parseInt(process.env.DSH_MEMORY_MAX_INJECT_PER_HOUR ?? '40', 10)
const SESSIONS_TTL_MS = Number.parseInt(process.env.DSH_MEMORY_SESSIONS_TTL_MS ?? '600000', 10)

let lastIngestAt = 0

/** stderr diagnostics; never throws, never writes to the model. */
function log(...parts) {
  try {
    process.stderr.write(`[${PLUGIN_ID}] ${parts.join(' ')}\n`)
  } catch {
    /* a closed stderr must never break the plugin */
  }
}

/**
 * Run `python <SCRIPT> <args...>`。
 * @param {string[]} args
 * @param {{timeoutMs?: number, signal?: AbortSignal}} [options]
 * @returns {Promise<{ok: boolean, exitCode: number, stdout: string, stderr: string}>}
 */
export function runMem(args, options = {}) {
  const timeout = Number.isFinite(options.timeoutMs) && options.timeoutMs > 0 ? options.timeoutMs : TIMEOUT_MS
  const signal = options.signal
  return new Promise((resolve) => {
    let child
    try {
      child = spawn(PYTHON, [SCRIPT, ...args], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'], env: PY_ENV })
    } catch (error) {
      resolve({ ok: false, exitCode: -1, stdout: '', stderr: `spawn failed: ${error?.message ?? error}` })
      return
    }

    let stdout = ''
    let stderr = ''
    let settled = false
    const finish = (result) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (signal !== undefined) signal.removeEventListener('abort', onAbort)
      resolve(result)
    }

    const timer = setTimeout(() => {
      try { child.kill() } catch { /* already gone */ }
      finish({ ok: false, exitCode: -1, stdout, stderr: `${stderr}\n[${PLUGIN_ID}] timed out after ${timeout} ms` })
    }, timeout)

    const onAbort = () => {
      try { child.kill() } catch { /* already gone */ }
      finish({ ok: false, exitCode: -1, stdout, stderr: `${stderr}\n[${PLUGIN_ID}] aborted by caller` })
    }
    if (signal !== undefined) {
      if (signal.aborted) { onAbort(); return }
      signal.addEventListener('abort', onAbort, { once: true })
    }

    child.stdout.on('data', (chunk) => { stdout += chunk.toString('utf8') })
    child.stderr.on('data', (chunk) => { stderr += chunk.toString('utf8') })
    child.on('error', (error) => finish({ ok: false, exitCode: -1, stdout, stderr: `spawn error: ${error?.message ?? error}` }))
    child.on('close', (code) => finish({ ok: code === 0, exitCode: typeof code === 'number' ? code : -1, stdout, stderr }))
  })
}

/**
 * Run `python <SCRIPT> recall <query> --json --k <k> [--session <session>]`.
 * @param {object} options
 * @param {string} options.query - recall query text.
 * @param {number} [options.k] - number of hits.
 * @param {string} [options.session] - restrict to one session id.
 * @param {AbortSignal} [options.signal] - caller cancellation; kills the child.
 * @returns {Promise<{ok: boolean, exitCode: number, stdout: string, stderr: string}>}
 */
export function runMemRecall({ query, k, session, signal }) {
  const depth = Number.isFinite(k) && k > 0 ? Math.trunc(k) : 10
  const args = ['recall', String(query), '--json', '--k', String(depth)]
  if (typeof session === 'string' && session.length > 0) args.push('--session', session)
  return runMem(args, { signal })
}

/** v0.1.5：召回调用走一层可替换的间接引用，测试才能确定性断言注入行为（不再依赖真库返回什么）。 */
let recallFnImpl = null
/**
 * 测试专用：替换/还原召回实现。
 * @param {null|((options: object) => Promise<{ok: boolean, exitCode: number, stdout: string, stderr: string}>)} fn
 */
export function __setRecallFn(fn) { recallFnImpl = typeof fn === 'function' ? fn : null }

function recallImpl(options) {
  return recallFnImpl === null ? runMemRecall(options) : recallFnImpl(options)
}

/**
 * Run `python <SCRIPT> ingest` (incremental) in the background so that new
 * conversations land in the library without anyone remembering to run it.
 * Throttled; failures are logged and ignored. Never awaited by the turn.
 * @returns {void}
 */
export function refreshIndex() {
  if (!INGEST) return
  // the self-check exercises apply() with a mock ctx; it must not spawn a real
  // ingest (that leaked a lock file once — see the 2026-09-30 retrospective).
  if (process.env.DSH_MEMORY_NO_INGEST === '1') return
  const now = Date.now()
  if (now - lastIngestAt < (Number.isFinite(INGEST_INTERVAL_MS) ? INGEST_INTERVAL_MS : 60000)) return
  lastIngestAt = now
  try {
    const child = spawn(PYTHON, [SCRIPT, 'ingest'], { windowsHide: true, stdio: 'ignore', env: PY_ENV })
    const timer = setTimeout(() => { try { child.kill() } catch { /* gone */ } },
      Number.isFinite(INGEST_TIMEOUT_MS) && INGEST_TIMEOUT_MS > 0 ? INGEST_TIMEOUT_MS : 120000)
    child.on('error', (error) => { clearTimeout(timer); log(`ingest spawn error: ${error?.message ?? error}`) })
    child.on('close', (code) => {
      clearTimeout(timer)
      if (DEBUG) log(`ingest finished code=${code}`)
    })
    if (DEBUG) log('ingest started (background, incremental)')
  } catch (error) {
    log(`ingest skipped: ${error?.message ?? error}`)
  }
}

/**
 * Build one identified user-role message. Field-complete on purpose: the inbox validates identity.
 * Shape matches `createUserMessage({content, source})` from `@deepseek-ai/dsh-llm`
 * (`dsh-llm/lib/types/message.js:53`), constructed without importing that package.
 * @param {string} text - model-facing text.
 * @returns {object} a complete `UserMessage`.
 */
function pluginMessage(text) {
  return {
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: `plugin:${PLUGIN_ID}` },
  }
}

/** Pull the newest user-role text out of a claimed message batch. */
export function lastUserText(messages) {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i]
    if (message?.role !== 'user') continue
    // never recall on our own injected block (or any other plugin-injected text).
    // 兼容两种来源形状：`plugin`（本插件构造的）与 `plugin:<name>`（运行时归一化后的）
    const kind = message.source?.kind
    if (typeof kind === 'string' && (kind === 'plugin' || kind.startsWith('plugin:'))) continue
    const text = (message.content ?? [])
      .filter((block) => block?.type === 'text' && typeof block.text === 'string')
      .map((block) => block.text)
      .join('\n')
      .trim()
    if (text.length > 0) return text
  }
  return undefined
}

/**
 * Harness / plugin chrome that must never become a recall query. Session
 * 34c686b4 turn 5 proved why: the runtime-context block was used verbatim, so
 * the index returned copies of that same boilerplate instead of anything useful.
 * 2026-10-01 实测：v0.1.4 之前 97% 的 query 是这些样板（Top: "Its closing message:"
 * 216 次 / "You are a delegated subagent…" 114 次），这份名单据此补齐。
 */
const BOILERPLATE_LINE = [
  'Current runtime context',
  'Current DSH file policy',
  'Instructions from: AGENTS.md',   // ★修正⑦-b：2026-10-04 实测它成了 recall query（浪费一次模型调用）
  'Approval prompts are disabled',
  'Approval policy:',
  'Approval policy：',
  '用户原话保留在本轮人类消息中',
  'Background subagent',
  'sent a message:',
  'finished and will do no further',
  'Time sampled while preparing turn',
  'Browser time zone for this request',
  'Elapsed since the preceding',
  'This snapshot supersedes earlier',
  '<system-reminder>',
  '[插件辅助上下文',
  '【协作基调',
  '【本轮要求',
  '【质量解释',
  '【实现选项',
  '【未决项',
  '<memory_recall',
  '</memory_recall',
  // ★ 2026-10-15：另两个注入标签也必须进样板名单 ——
  //   旧名单只挡 `<memory_recall` / `<goal_round>`，编排块与告警块会**整段变成检索 query**
  //   （那等于拿自己的输出当"用户在问什么"去搜，越搜越偏）。
  '<attention_orchestration',
  '</attention_orchestration',
  '<memory-orchestrator-alert',
  '</memory-orchestrator-alert',
  'Recalled excerpts from earlier sessions',
  'Use these as evidence',
  // v0.1.5 补：这两条是 2026-10-01 注入噪声榜的冠亚军，v0.1.4 名单没盖住
  'Its closing message:',
  'You are a delegated subagent',
  'You are an AI agent powered by',
  'The following workspace instructions may be relevant',
  '<goal_round>',
  '<compacted-summary>',
  'This is an automatically generated checkpoint',
  'Do not use todo_write to track this planning phase',
]
/** 样板行（前缀匹配之外，还需按形态匹配的行，例如子代理通知）。 */
const BOILERPLATE_PATTERN = [
  /^Agent\s+[0-9a-fA-F-]{8,}\s+sent a message:/,
  /^Background subagent\s+[0-9a-fA-F-]{8,}/,
  /^\d{4}-\d{2}-\d{2}T[\d:+.]+\[[^\]]+\]\s*$/,      // 纯时间戳行
  /^dsh:[0-9a-z-]{8,}#turn=\d+/,                    // 引用行不是提问
]
/** Pure acknowledgment turns are not worth a recall either. */
const SMALL_TALK = new Set(['继续', '继续吧', '接着', '下一步', '好', '好的', '嗯', 'ok', 'okay',
  'go on', 'continue', '谢谢', '收到'])

/**
 * Turn a raw turn message into something worth searching for.
 * @param {string|undefined} raw - newest user text of the turn.
 * @returns {string|undefined} cleaned query, or undefined when recall should be skipped.
 */
export function cleanQuery(raw) {
  if (typeof raw !== 'string') return undefined
  const kept = []
  for (const rawLine of raw.split(/\r?\n/)) {
    const line = rawLine.trim()
    if (line === '') continue
    if (BOILERPLATE_LINE.some((m) => line.startsWith(m))) continue
    if (BOILERPLATE_PATTERN.some((rx) => rx.test(line))) continue
    kept.push(line)
  }
  let text = kept.join('\n').trim()
  if (text.length > MAX_QUERY_CHARS) text = text.slice(0, MAX_QUERY_CHARS)
  // ★★ 2026-10-04 修正①-b：小话轮必须排在长度检查之前判定
  //   MIN_QUERY_CHARS 默认 6，而「继续」只有 2 字 ⇒ 在到达 SMALL_TALK 检查之前
  //   就被 return undefined 掉了 ⇒ 原 SMALL_TALK 那行**从来不可能命中（死代码）**。
  //   修法：先判小话轮（返回文本 + 由调用方标 smallTalk），再判长度。
  if (SMALL_TALK.has(text.toLowerCase())) return text
  if (text.length < MIN_QUERY_CHARS) return undefined
  return text
}

/** ℹ 这一轮是不是「纯应答/继续」（没有新要求信息）？ */
export function isSmallTalkTurn(text) {
  return SMALL_TALK.has(String(text ?? '').trim().toLowerCase())
}

// ---------------------------------------------------------------------------
// v0.1.5 注入决策（纯函数，便于 test/inject.mjs 做正例/负例）
// ---------------------------------------------------------------------------

/** 归一 query：用于"同 query 去重"。 */
export function normalizeQuery(q) {
  return String(q ?? '').toLowerCase().replace(/\s+/g, ' ').trim().slice(0, 200)
}

// ---------------------------------------------------------------------------
// ★★ 2026-10-04 修正②：**当前要求通道**（用户点破「…」）
//
// 病根：query 只取 lastUserText(messages) —— **最新一条用户消息**。
//   于是它检索的是「历史上跟这句话语义相近的片段」，
//   而**用户这次的要求（goal）根本不在检索输入里**。
//   ⇒ 它做的是「帮你回忆以前怎么做的」，而你需要的是「记住我这次要什么」。
// ---------------------------------------------------------------------------

/** ℹ 从 payload 里抽 goal 文本（多种可能字段，取到即用）。 */
export function extractGoalText(payload) {
  const g = payload?.goal ?? payload?.agent?.goal ?? payload?.context?.goal
  if (!g) return ''
  if (typeof g === 'string') return g.trim()
  const obj = g.goal ?? g
  return String(obj?.objective ?? obj?.text ?? '').trim()
}

/** ℹ 读要求锚 BRIEF.md（g_brief 落的锚）。读不到就返回 ''（不阻断）。 */
export function loadBriefText(baseDir, sessionId) {
  try {
    if (!baseDir) return ''
    // ★★ 2026-10-04 修正⑥（用户实测报「…」）：**按会话隔离**
    //   旧版读全局唯一 BRIEF.md ⇒ 任何会话落的锚都会被注入别的会话。
    //   新版：每会话一个 BRIEF_<sessionId>.md；sessionId 取 payload.agent.id。
    //   找不到本会话的锚 ⇒ 返回 ''（不注入）—— **取错锚比无锚更坏**
    const sid = String(sessionId ?? '').trim()
    if (!sid) return ''
    const safe = sid.replace(/[^A-Za-z0-9._-]/g, '_')
    const agentRoot = _pathResolve(baseDir, '..')
    const ledger = _pathJoin(agentRoot, '_guard_ledger')
    const candidates = [
      process.env.DSH_BRIEF_PATH,                        // 调试用覆盖（全局设置会重新引入串台，仅测试时用）
      _pathJoin(ledger, `BRIEF_${safe}.md`),             // ★ 会话隔离版
      sid.startsWith('session-')
        ? _pathJoin(ledger, `BRIEF_${safe.slice(8)}.md`)
        : _pathJoin(ledger, `BRIEF_session-${safe}.md`), // 前缀形态互补
    ].filter(Boolean)
    for (const p of candidates) {
      if (_fsExists(p)) return _fsRead(p, 'utf8').slice(0, 4000)
    }
    return ''
  } catch { return '' }
}

/** ★ 组装「本轮应该检索什么」—— goal 优先于「最新一句话」。 */
export function buildQueryWithGoal(userText, goalText) {
  const parts = []
  if (goalText) parts.push(`〔当前要求〕${goalText}`)
  if (userText) parts.push(`〔本轮〕${userText}`)
  return parts.join('\n').trim()
}

/** 渲染「要求常驻块」—— 与命中无关，每轮都注入（有内容时）。 */
export function renderGoalBlock(goalText, briefText) {
  const lines = []
  if (goalText) lines.push(`**当前要求（逐字）**：${goalText}`)
  if (briefText) {
    const rows = briefText.split('\n').filter((l) => l.trim().startsWith('|'))
    const body = rows.slice(0, 48).join('\n')
    if (body) lines.push('', '**要求锚（BRIEF，逐字，收尾须逐条对账）**：', body)
  }
  if (lines.length === 0) return ''
  return [
    '<attention_orchestration source="dsh-memory-plugin" kind="goal-anchor">',
    '【本轮要求锚 · 常驻】以下是用户**逐字**提出的要求，不是历史片段。',
    '**收尾时须逐条对账；有未兑现项不许宣布完成**（闸：g_brief / R-42）。',
    '',
    ...lines,
    '</attention_orchestration>',
  ].join('\n')
}

/** 命中的稳定标识：用于"命中集未变 ⇒ 不重复注入"。 */

export function hitKeys(parsed) {
  const hits = Array.isArray(parsed?.hits) ? parsed.hits : []
  return hits.map((h) => `${h?.session_id ?? ''}|${h?.turn ?? ''}|${h?.chunk_id ?? ''}`)
}

/**
 * 决定这一轮注入什么。
 * @param {{verdict?: string, isSubagent?: boolean, weakMode?: string, subagentMode?: string}} input
 * @returns {{mode: 'skip'|'hint'|'digest'|'compact', reason: string}}
 */
export function decideInjection(input) {
  const verdict = input?.verdict
  const isSub = input?.isSubagent === true
  const weakMode = (input?.weakMode ?? WEAK_MODE).toLowerCase()
  const subMode = (input?.subagentMode ?? SUBAGENT_MODE).toLowerCase()
  if (verdict === 'none' || verdict === undefined || verdict === null) {
    return { mode: 'skip', reason: 'verdict-none' }
  }
  if (isSub) {
    if (subMode === 'off') return { mode: 'skip', reason: 'subagent-off' }
    if (subMode === 'hint') return { mode: 'hint', reason: 'subagent-hint' }
    if (verdict !== 'strong') return { mode: 'skip', reason: 'subagent-weak-skip' }
    return { mode: subMode === 'full' ? 'digest' : 'compact', reason: 'subagent-strong' }
  }
  if (verdict === 'strong') return { mode: 'digest', reason: 'strong-main' }
  if (weakMode === 'off') return { mode: 'skip', reason: 'weak-off' }
  if (weakMode === 'full') return { mode: 'digest', reason: 'weak-full' }
  return { mode: 'hint', reason: 'weak-hint' }
}

function shortId(sid) {
  return String(sid ?? '?').replace(/^session-/, '').slice(0, 13)
}

function fmtTime(ms) {
  const n = Number(ms)
  if (!Number.isFinite(n) || n <= 0) return '--'
  const d = new Date(n)
  const p = (x) => String(x).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function collapse(text) {
  return String(text ?? '').replace(/\r/g, '').replace(/\s*\n+\s*/g, ' ⏎ ').replace(/[ \t]{2,}/g, ' ').trim()
}

/**
 * weak 时的一行提示（不注入正文）。实测 85% 的注入是 weak 却整块灌 —— 这一行把它压到 ~120 字。
 * @param {string} query
 * @param {object} parsed
 * @returns {string}
 */
export function buildHintBlock(query, parsed) {
  const top = parsed?.topScore
  return [
    '<memory_recall mode="hint" source="dsh-memory-plugin">',
    `query: ${query}`,
    `库里可能有相关记录（verdict=weak, top=${top ?? '?'}），本轮**不注入正文**。`
      + '需要时用 `memory_recall` 工具检索，或 `python ' + SCRIPT + ' ask "<问题>"`。',
    '</memory_recall>',
  ].join('\n')
}

/**
 * 结构化摘要 + 出处指针：**永不从中间截断**（旧版 89.5% 的注入是被硬切的半截 JSON）。
 * 预算不够就整条不列，并在末尾写明还有几条没列。
 * @param {{query: string, parsed: object, mode: 'digest'|'compact', budget?: number,
 *          hitChars?: number, maxHits?: number}} input
 * @returns {{block: string, listed: number, dropped: number, chars: number}}
 */
export function buildDigestBlock(input) {
  const query = input.query
  const parsed = input.parsed ?? {}
  const mode = input.mode === 'compact' ? 'compact' : 'digest'
  const budget = Number.isFinite(input.budget) && input.budget > 0
    ? input.budget : (mode === 'compact' ? SUBAGENT_BUDGET : INJECT_MAX_CHARS)
  const hitChars = Number.isFinite(input.hitChars) && input.hitChars > 0
    ? input.hitChars : (mode === 'compact' ? 300 : HIT_CHARS)
  const maxHits = Number.isFinite(input.maxHits) && input.maxHits > 0
    ? input.maxHits : (mode === 'compact' ? SUBAGENT_HITS : (Number.isFinite(K) ? K : 6))
  const hits = Array.isArray(parsed?.hits) ? parsed.hits : []
  // ★ 2026-10-15 融合：脑（认知图）的「经审结论」与上面的「原文摘录」**同一个块**。
  //   为什么要并进来：两套记忆各自往对话里注入，就会长出"自己注入→自己召回→自己当证据"
  //   的自噬闭环。融合后分工是——本库管**原文可溯源**，脑管**结论还成不成立**。
  const brain = parsed?.brain ?? null
  const brainItems = Array.isArray(brain?.items) ? brain.items : []
  const brainNote = brain
    ? (brain.status === 'ok'
      ? `结论段: 脑在线，${brainItems.length} 条可用`
      : `结论段: 脑不可用（status=${brain.status ?? '?'}${brain.reason ? '：' + String(brain.reason).slice(0, 120) : ''}）`)
    : '结论段: 未启用（--no-brain 或旧版 mem.py）'
  const head = [
    `<memory_recall mode="${mode}" source="dsh-memory-plugin"${brain ? ` brain="${brain.status ?? '?'}"` : ''} verdict="strong">`,
    `query: ${query}`,
    `confidence: strong（top=${parsed?.topScore ?? '?'}）｜命中 ${hits.length} 条，下列 ${Math.min(hits.length, maxHits)} 条上限内可用`,
    brainNote,
    'excerpts（**原文摘录**，每条都带出处；要用就引 session/turn，别当常识）:',
  ].join('\n')
  const tail = [
    'Use these as evidence; cite the session/turn when you rely on one.',
    '</memory_recall>',
  ].join('\n')
  const lines = []
  let used = head.length + tail.length
  let listed = 0
  for (let i = 0; i < hits.length && listed < maxHits; i += 1) {
    const h = hits[i] ?? {}
    const s = h._session ?? {}
    const excerpt = collapse(h.text).slice(0, hitChars)
    const part = [
      `[${i + 1}] dsh:${shortId(h.session_id)}#turn=${h.turn ?? '?'} @${fmtTime(h.time_start)}`
        + ` score=${h.score ?? '?'} origin=${s.origin ?? '?'} kind=${h.kind ?? '?'}`,
      `    ${excerpt}`,
      `    ↳ 取全文: ${PYTHON} ${SCRIPT} show ${h.session_id ?? '?'} --turn ${h.turn ?? 0}`,
    ].join('\n')
    if (used + part.length + 1 > budget) break
    lines.push(part)
    used += part.length + 1
    listed += 1
  }
  const dropped = hits.length - listed
  const notListed = dropped > 0
    ? [`…另 ${dropped} 条未列（预算 ${budget} 字内优先给高分条；要别的用 memory_recall 工具取）`]
    : []
  // ---- 结论段（脑）：与摘要同预算，装不下就整条不列 ----
  const concl = []
  if (brain) {
    if (brain.status !== 'ok') {
      concl.push(`conclusions: 未取到（${brainNote}）`)
    } else if (brainItems.length === 0) {
      concl.push('conclusions: 本库暂无相关结论（脑在线，但按当前 query 无命中）')
    } else {
      concl.push('conclusions（**经审的结论**，带验证态；引 id。与上面"原文摘录"是两类证据，'
        + '别把结论当原文引）:')
      for (let i = 0; i < brainItems.length; i += 1) {
        const c = brainItems[i] ?? {}
        const na = Array.isArray(c.not_applicable) && c.not_applicable.length
          ? `｜**不适用**: ${c.not_applicable.join('；')}` : ''
        const part = [
          `[C${i + 1}] ${collapse(c.title ?? '').slice(0, 120)}`
            + `  〔验证态=${c.verification_state ?? '?'}`
            + `${c.qualification ? ` / 资格=${c.qualification}` : ''}〕 id=${c.id ?? '?'}`,
          `    ${collapse(c.snippet ?? '').slice(0, 300)}`,
          `    ↳ 出处: brain:${c.id ?? '?'}${na}`,
        ].join('\n')
        if (used + part.length + 1 > budget) break
        concl.push(part)
        used += part.length + 1
      }
    }
  }
  const block = [head, ...lines, ...notListed, ...concl, tail].join('\n')
  return { block, listed, dropped, chars: block.length, brainListed: brainItems.length }
}

// ---------------------------------------------------------------------------
// origin 表（判断当前会话是不是子智能体）：一次 `mem.py sessions --json`，带 TTL 缓存
// ---------------------------------------------------------------------------

let sessionOrigins = new Map()
let sessionOriginsAt = 0

/** @returns {Promise<Map<string,string>>} sessionId -> origin */
export async function refreshSessionOrigins(signal) {
  const result = await runMem(['sessions', '--json', '--limit', '5000'], { timeoutMs: 30000, signal })
  if (!result.ok) return sessionOrigins
  try {
    const rows = JSON.parse(result.stdout)
    const next = new Map()
    for (const r of rows ?? []) {
      const id = r?.session_id ?? r?.session
      if (typeof id === 'string' && id.length > 0) next.set(id, r?.origin ?? 'user')
    }
    if (next.size > 0) { sessionOrigins = next; sessionOriginsAt = Date.now() }
  } catch { /* 解析失败保留旧表 */ }
  return sessionOrigins
}

/**
 * 当前会话是不是子智能体会话（origin=subagent）。未知 ⇒ 当主会话（失败开放）。
 * @param {string|undefined} sessionId
 * @param {{signal?: AbortSignal, ttlMs?: number}} [options]
 * @returns {Promise<boolean>}
 */
export async function isSubagentSession(sessionId, options = {}) {
  if (typeof sessionId !== 'string' || sessionId.length === 0) return false
  const ttl = Number.isFinite(options.ttlMs) && options.ttlMs > 0 ? options.ttlMs : SESSIONS_TTL_MS
  if (Date.now() - sessionOriginsAt > ttl) {
    try { await refreshSessionOrigins(options.signal) } catch { /* 失败开放 */ }
  }
  return sessionOrigins.get(sessionId) === 'subagent'
}

/** 测试用：直接塞一张 origin 表。 */
export function __setSessionOrigins(map) {
  sessionOrigins = new Map(Object.entries(map ?? {}))
  sessionOriginsAt = Date.now()
}

// ---------------------------------------------------------------------------
// 本会话"活动过的项目目录"（R4 项目闸的判据来源，2026-10-16）
// ---------------------------------------------------------------------------
// 用途：编排器判"这条候选是不是本项目的"。
//   ★ **不能只看用户自己说过的话**（初版就是这么写的，被实测推翻）：
//     实证 session-examplesession（「示例项目甲」项目）的 user_text / assistant_text 里
//     **一个项目路径都没有** —— 路径全在 tool_call(51) / tool_result(105) / reasoning 里
//     ⇒ 只看用户话 = 永远取不到线索 = 闸形同不存在（"判据量错了对象"）。
// 取数：`mem.py session-domains <sid> --json`（扫**本会话全部原子**，排除插件注入块本身）。
// 成本：每个会话 TTL 内只 spawn 一次；**失败开放**（返回上次结果或空表，绝不阻断对话）。
//   ⚠ 与"用户的红队测试"无关：字段缺失/命令不存在（旧版 mem.py）⇒ 返回 []，闸退回"只看用户话"。
let sessionDomainsCache = new Map()
const SESSION_DOMAINS_TTL_MS =
  Number.parseInt(process.env.DSH_MEMORY_SESSION_DOMAINS_TTL_MS ?? '600000', 10)

/**
 * 取一次就同时拿到"项目目录"与"本会话用户口径"（**共用一次 spawn、共用一个缓存**）。
 * @returns {Promise<{domains: string[], keypoints: Array<{text,cite,turn,time}>}>}
 */
async function fetchSessionContext(sessionId, options = {}) {
  const empty = { domains: [], keypoints: [] }
  if (typeof sessionId !== 'string' || sessionId.length === 0) return empty
  if (process.env.DSH_MEMORY_NO_SESSION_DOMAINS === '1') return empty
  const ttl = Number.isFinite(options.ttlMs) && options.ttlMs > 0 ? options.ttlMs : SESSION_DOMAINS_TTL_MS
  const cached = sessionDomainsCache.get(sessionId)
  if (cached && Date.now() - cached.at <= ttl) return cached
  let result
  try {
    result = await runMem(['session-domains', sessionId, '--json', '--keypoints', '6'],
      { timeoutMs: 15000, signal: options.signal })
  } catch { return cached || empty }
  if (!result.ok) return cached || empty
  try {
    const j = JSON.parse(result.stdout)
    const domains = Array.isArray(j?.domains)
      ? j.domains.map((d) => d?.name ?? d?.key).filter((x) => typeof x === 'string' && x.length > 0)
      : []
    // ★ 本会话用户口径：**出处由库给出（真实）**，插件不编造 —— R0 的反编造校验因此仍然有效。
    const keypoints = (Array.isArray(j?.keypoints) ? j.keypoints : [])
      .map((k) => ({
        text: String(k?.text ?? '').slice(0, 300),
        cite: `dsh:${sessionId.slice(0, 12)}#turn=${k?.turn ?? ''}`,
        turn: k?.turn ?? null,
        time: k?.time ?? null,
      }))
      .filter((k) => k.text && k.cite)
    const next = { at: Date.now(), domains, keypoints }
    sessionDomainsCache.set(sessionId, next)
    if (sessionDomainsCache.size > 200) {          // 有界：别把内存当台账
      const first = sessionDomainsCache.keys().next().value
      if (first !== undefined) sessionDomainsCache.delete(first)
    }
    return next
  } catch { return cached || empty }
}

/**
 * @param {string|undefined} sessionId
 * @param {{signal?: AbortSignal, ttlMs?: number}} [options]
 * @returns {Promise<string[]>} 项目目录名（如 ["示例项目甲_2026-10", "示例项目己"]）
 */
export async function getSessionDomains(sessionId, options = {}) {
  return (await fetchSessionContext(sessionId, options)).domains
}

/**
 * 本会话用户自己定过的**口径**（带真实出处）。
 * ★ 2026-10-17 新增，治的是"信息丢失"里**最要命的那一半**：
 *   实证（「示例项目甲」2026-10-16）：用户 21:10 定了「把目标改成正确认知 3D 空间」，
 *   23:33 说「叙述一下总体目标」⇒ 检索（全库字面相似）召回的 6 条**全是别会话日志、本会话口径一条没有**
 *   ⇒ 判官空手 ⇒ 记忆 0 注入 ⇒ 模型把本体目标写成"落笔式临摹" ⇒ 用户「3D 部分为何缺失」。
 *   ⇒ 本会话自己定过的口径**必须进候选池**（出处真实，判官可挑进 persist）。
 * @param {string|undefined} sessionId
 * @param {{signal?: AbortSignal, ttlMs?: number}} [options]
 * @returns {Promise<Array<{text,cite,turn,time}>>}
 */
export async function getSessionKeypoints(sessionId, options = {}) {
  return (await fetchSessionContext(sessionId, options)).keypoints
}

/** 测试用：直接塞一张"会话 → 项目目录"表。 */
export function __setSessionDomains(map) {
  sessionDomainsCache = new Map()
  for (const [k, v] of Object.entries(map ?? {})) {
    // v 可以是数组（只塞目录）或 {domains, keypoints}（测试口径候选时用）
    const domains = Array.isArray(v) ? v : (Array.isArray(v?.domains) ? v.domains : [])
    const keypoints = Array.isArray(v?.keypoints) ? v.keypoints : []
    sessionDomainsCache.set(k, { at: Date.now(), domains, keypoints })
  }
}

const POLICY_TEXT = [
  '## 历史记忆（dsh-memory-plugin）',
  '',
  '本会话运行在一个可检索的历史记忆库之上。当用户提到"上次""之前""我们说过""那个文件"这类',
  '指向过去会话的内容，或你需要确认既有决策 / 参数 / 路径时，先调用 `memory_recall` 工具检索，',
  '再回答；不要凭空回忆。检索结果是带 session_id / turn / 出处的原文片段，引用时说明来源。',
  '',
  '**★两条硬纪律（2026-10-01 实战教训，P82/P84）**：',
  '1. **派活 / 下发硬参数（尺寸、口径、阈值、路径）之前**，先用记忆库核该对象的**专条**',
  '   （`07`／`09` 等权威源），并把出处写进消息；凭印象给参数 = 让下游照错做（当天廊柱',
  '   Ø0.30／高 2.5 就是"派活前提拍脑袋"，靠记忆库才查回专条 Ø0.22／2.80）。',
  '2. **检索到的旧记录必须先确认是否已被取代**才能当依据——旧记录可能是"已作废的早期构建"。',
].join('\n')

/**
 * Cordis plugin entry point.
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 */
export function apply(ctx) {
  // ---- 1. the model tool -------------------------------------------------
  // Wrapped on purpose: an exception escaping apply() makes the whole DSH launch
  // fail (the launcher exits 1). A missing tool is a degraded plugin; a broken
  // boot is a broken product. So even the core registration fails soft.
  safeRegister(ctx, () => ctx.tools.register({
    name: 'memory_recall',
    description:
      'Search the durable cross-session memory index for this machine (DSH session logs, notes and '
      + 'transcripts). Returns the matching raw excerpts with their session id, turn and provenance. '
      + 'Use it whenever the user refers to earlier work or you must confirm a past decision.',
    parameters: {
      type: 'object',
      properties: {
        query: { type: 'string', description: 'What to search for, in natural language.' },
        k: { type: 'number', description: 'How many excerpts to return (default 10).' },
        session: { type: 'string', description: 'Restrict the search to one session id.' },
      },
      required: ['query'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          exitCode: { type: 'number' },
          stdout: { type: 'string' },
          stderr: { type: 'string' },
        },
        required: ['ok', 'exitCode', 'stdout', 'stderr'],
        additionalProperties: false,
      },
      render(args, value) {
        if (!value.ok) {
          return [{ type: 'text', text: `memory_recall failed (exit ${value.exitCode}).\n${value.stderr}`.trim() }]
        }
        return [{ type: 'text', text: value.stdout.trim() }]
      },
    },
    timeoutMs: Number.isFinite(TIMEOUT_MS) && TIMEOUT_MS > 0 ? TIMEOUT_MS + 5000 : 25000,
    async execute(args, exec) {
      const result = await runMemRecall({
        query: args.query,
        k: args.k,
        // 只在调用方**显式**给了 session 时才过滤；默认要跨会话检索（记忆库的意义就在这）
        session: args.session,
        signal: exec.signal,
      })
      if (DEBUG) log(`recall query=${JSON.stringify(args.query)} ok=${result.ok} exit=${result.exitCode}`)
      return result
    },
  }), 'memory_recall tool')

  // ---- 1b. 注意力编排的开关工具（v0.2.0）---------------------------------
  // 用户要求：加一个能开关"注意力编排"的面板。
  // 右栏 sidebar.right.pane.tab 那条路走不通（第三方包进 combo 批次后未被执行），
  // 改用**已被证明可用**的通路：工具调用的 toolview 卡片（dsh-visualize 就是这么做的）。
  // 模型调用 memory_orchestrator，结果在对话里渲染成一张带开关的卡片。
  safeRegister(ctx, () => ctx.tools.register({
    name: 'memory_orchestrator',
    description:
      'Attention-orchestration switch. Call this when the user asks to turn the attention '
      + 'orchestrator on/off, choose its model, or see its status. With no arguments it returns '
      + 'the current state. This is the UI surface for the orchestrator — the panel renders as '
      + 'a card with clickable controls. ⚠️ Enabling it SPENDS EXTRA TOKENS: while it is on, '
      + 'every turn makes one additional model call (plus one more round-trip latency). It is '
      + 'off by default for that reason; tell the user this cost before turning it on.',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          enum: ['status', 'on', 'off', 'set-model', 'decisions'],
          description: 'status (default) | on | off | set-model | decisions',
        },
        modelMode: { type: 'string', enum: ['follow', 'custom'], description: 'for set-model' },
        provider: { type: 'string', description: 'for set-model + custom' },
        model: { type: 'string', description: 'for set-model + custom' },
      },
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          enabled: { type: 'boolean' },
          modelMode: { type: 'string' },
          customProvider: { type: 'string' },
          customModel: { type: 'string' },
          total: { type: 'number' },
          okCount: { type: 'number' },
          failed: { type: 'number' },
          lastSummary: { type: 'string' },
          models: { type: 'string' },
          decisions: { type: 'string' },
          message: { type: 'string' },
        },
        required: ['ok', 'enabled', 'modelMode', 'message'],
        additionalProperties: true,
      },
      // 这个 render 决定"卡片上显示什么"（真正的可点控件由客户端 toolview 提供）
      render(_args, value) {
        if (!value.ok) return [{ type: 'text', text: `memory_orchestrator failed: ${value.message}` }]
        const on = value.enabled ? '已开启' : '已关闭'
        const lines = [
          `注意力编排：${on}`,
          `编排模型：${value.modelMode === 'custom' ? `自选 ${value.customModel || '(未选)'}` : '跟随当前会话'}`,
          `⚠️ 开启后**每轮额外调一次模型**（挑该注入什么）⇒ **额外消耗 token**（并多一次模型往返）`,
          `累计编排：${value.total ?? 0} 次（成功 ${value.okCount ?? 0}／失败 ${value.failed ?? 0}）`,
        ]
        if (value.lastSummary) lines.push(`最近：${value.lastSummary}`)
        if (value.message) lines.push(value.message)
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },
    async execute(args) {
      const { loadConfig, saveConfig, stats, recentDecisions } = await import('../orchestrator/core.js')
      const base = MEM_BASE
      const action = args.action || 'status'
      if (action === 'on') saveConfig(base, { enabled: true })
      else if (action === 'off') saveConfig(base, { enabled: false })
      else if (action === 'set-model') {
        const patch = {}
        if (args.modelMode) patch.modelMode = args.modelMode
        if (args.provider !== undefined) patch.customProvider = args.provider
        if (args.model !== undefined) patch.customModel = args.model
        saveConfig(base, patch)
      }
      const st = stats(base)
      const out = {
        ok: true,
        enabled: !!st.enabled,
        modelMode: st.modelMode || 'follow',
        customProvider: st.customProvider || '',
        customModel: st.customModel || '',
        total: st.total || 0,
        okCount: st.ok || 0,
        failed: st.failed || 0,
        lastSummary: st.last
          ? `${st.last.ms}ms 常驻${st.last.persist ?? '-'}/引用${st.last.quote ?? '-'} 模型=${st.last.model || '-'}`
          : '',
        models: '',
        decisions: '',
        message: '',
      }
      if (action === 'decisions') {
        const rec = recentDecisions(base, 10)
        out.decisions = rec.map((r) => `${(r.ts || '').slice(11, 19)} ${r.outcome} ${r.ms}ms`).join('\n')
      }
      return out
    },
  }), 'memory_orchestrator tool')

  // ---- 1c. continuity_memory（项目方向锁 + 跨对话连续性） -----------------
  safeRegister(ctx, () => ctx.tools.register({
    name: 'continuity_memory',
    description: 'Manage the user-controlled project continuity packet. The continuity scope is PER CONVERSATION: enabled/mode/project/selectedSessions all apply only to the conversation that issues this call, and never leak into other conversations. AUTO-CONFIRM IS ON BY DEFAULT: goals, constraints and decisions that carry a real citation take effect immediately without per-item manual approval, so long-running work does not need a human to click every item; set autoConfirm=false to require manual confirmation. Two things are never auto-promoted: questions/hedged phrasing, and a goal that is already locked by a human (a human-locked goal is never auto-replaced; an auto-locked goal may only be replaced by a more substantial one, with the old goal kept in goalHistory). Every confirmed item requires a source citation. Use status to inspect, set to change THIS conversation\'s scope, and confirm to lock a goal, constraint, decision, or open issue.',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', enum: ['status', 'set', 'confirm', 'reject', 'reset'], description: 'status | set | confirm | reject | reset' },
        enabled: { type: 'boolean' },
        mode: { type: 'string', enum: ['off', 'project', 'sessions', 'all'] },
        project: { type: 'string' },
        selectedSessions: { type: 'array', items: { type: 'string' }, description: '其他要接上的对话（本对话自动包含，不必列出自己）' },
        autoConfirm: { type: 'boolean', description: '自动确认（默认 true）：目标/约束/决定带出处直接生效，无需逐条手动点。问句与已有目标不会被自动覆盖。' },
        item: { type: 'object', additionalProperties: true },
        cite: { type: 'string' },
        supersedes: { type: 'string' },
        id: { type: 'string', description: 'reject/rejectItem 时的条目 id（候选或已确认条目都可以）' },
        keepScope: { type: 'boolean', description: 'reset 时是否保留本对话的授权范围（默认 true）' },
        confirmedBy: { type: 'string', enum: ['human', 'auto'], description: 'confirm 时标注是谁确认的：human=人点的（目标锁不会被自动改写）；auto=机器自动确认' },
      },
      additionalProperties: false,
    },
    output: { schema: { type: 'object', additionalProperties: true }, render(_args, value) {
      if (!value?.ok) return [{ type: 'text', text: `continuity_memory failed: ${value?.error || 'unknown error'}` }]
      // ★ 读的是"本对话自己的"范围。confirm/reject/reset 也必须带 scope 回来，
      //   否则一次成功的确认会显示成"本对话未开启" —— 假报告。
      const sc = value.scope || {}
      const links = Array.isArray(sc.selectedSessions) ? sc.selectedSessions.length : 0
      const where = sc.enabled && sc.mode !== 'off'
        ? `本对话已开启；范围=${sc.mode}${sc.mode === 'sessions' ? `（接上 ${links} 个对话）` : sc.mode === 'project' ? `（工作区 ${sc.project || '未选'}）` : ''}；自动确认=${sc.autoConfirm === false ? '关（需逐条确认）' : '开（无需手动点）'}`
        : '本对话未开启（不影响其他对话）'
      // 目标锁由谁锁的必须报出来：人工锁不会被自动改写，自动锁可被更实质的目标取代。
      const goal = value.packet?.goalLock
      const goalNote = goal?.text
        ? `目标锁=${goal.confirmedBy === 'human' ? '人工确认（不会被自动改写）' : '自动确认（可被更实质的目标取代）'}`
        : '目标锁=无'
      const hist = (value.packet?.goalHistory || []).length
      return [{ type: 'text', text: `项目连续性：${where}；${goalNote}${hist ? `；历史目标 ${hist} 条（可追溯）` : ''}；版本=${value.packet?.version || '-'}。` }]
    } },
    async execute(args, exec) {
      const action = args.action || 'status'
      // ★ 2026-10-22：授权范围按对话存，所以工具也必须知道"当前是哪个对话"。
      //   exec.agent.id 就是 SessionId（与 pre-step 里的 payload.agent.id 同一来源）。
      //   拿不到就**拒绝写** —— 写成全局正是要消灭的那个 bug。
      const sessionId = String(exec?.agent?.id ?? '')
      // 统一出口：任何分支返回的状态都必须带上本对话的 scope（render 依赖它）。
      const withScope = (state) => ({ ...state, sessionId, scope: scopeFor(state, sessionId) })
      if (action === 'set') {
        const patch = {}
        for (const k of ['enabled', 'mode', 'project', 'selectedSessions', 'autoConfirm']) if (args[k] !== undefined) patch[k] = args[k]
        if (patch.mode && !MODES.has(patch.mode)) return { ok: false, error: 'invalid mode' }
        if (!sessionId) return { ok: false, error: 'cannot resolve current session; continuity scope is per-conversation' }
        const saved = saveScope(MEM_BASE, sessionId, patch)
        return { ok: true, ...saved, sessionId, scope: saved.scope }
      }
      if (action === 'confirm') {
        if (!args.item || !args.cite) return { ok: false, error: 'confirm requires item and cite' }
        try {
          return { ok: true, ...withScope(confirmContinuityItem(MEM_BASE, args.item, {
            cite: args.cite, project: args.project || '', supersedes: args.supersedes || '',
            // 工具显式确认 = 人的决定 ⇒ confirmedBy='human'（目标锁不会被自动改写）。
            confirmedBy: args.confirmedBy === 'auto' ? 'auto' : 'human',
          })) }
        } catch (e) { return { ok: false, error: String(e?.message ?? e) } }
      }
      if (action === 'reject') {
        if (!args.id) return { ok: false, error: 'reject requires id' }
        // ★ 2026-10-22：先当候选删；不是候选就再当**已确认条目**删。
        //   自动确认会直接写进 goalLock/decisions，那些条目不在 candidates 里 ——
        //   只认候选的旧逻辑会让"撤销自动确认的东西"变成 ok:false（等于撤不掉）。
        const asCandidate = rejectContinuityCandidate(MEM_BASE, String(args.id))
        if (asCandidate.ok) return { ok: true, removed: asCandidate.removed, ...withScope(loadContinuity(MEM_BASE)) }
        const asItem = rejectContinuityItem(MEM_BASE, String(args.id))
        if (!asItem.ok) return { ok: false, error: asItem.error }
        return { ok: true, removed: asItem.removed, ...withScope(loadContinuity(MEM_BASE)) }
      }
      if (action === 'reset') {
        // keepScope=false ⇒ 连授权范围一起清（回到 off/未选会话），用于彻底回到干净态。
        const keepScope = args.keepScope !== false
        return { ok: true, reset: true, keepScope, ...withScope(resetContinuity(MEM_BASE, { keepScope })) }
      }
      if (action !== 'status') return { ok: false, error: `unknown action: ${action}` }
      // status 同时报"本对话自己的范围"，否则用户看不出"这条对话到底接没接上"。
      return { ok: true, ...withScope(loadContinuity(MEM_BASE)) }
    },
  }), 'continuity_memory tool')

  // ---- 1d. memory_hippo_status（HippoMem 引擎台账，暴露给模型 · 2026-10-07） ------
  // 静默失效教训的直接落地：判"记忆系统在不在工作"以台账为准，不看文档声明 enabled。
  safeRegister(ctx, () => ctx.tools.register({
    name: 'memory_hippo_status',
    // ★ 2026-10-07 实测修正：删掉 title 字段 —— ToolDefinition 白名单外的字段会让宿主拾取层
    //   拒收整条注册（safeRegister 吞掉后静默丢弃，与 pi-ai inputModalities 静默丢字段同型病）。
    //   字段集严格对齐 memory_orchestrator 的合法形态：name/description/parameters/output/render。
    description:
      'Query the HippoMem engine ledger (metacognition reflect → gatekeeper → 3-tier store → '
      + 'sleep compaction): candidate count, memory tier counts, compaction batches, injection '
      + 'usage rate, and consecutive-failure red lights. Judge "is it working" from this ledger, '
      + 'never from config claims.',
    parameters: {
      type: 'object',
      properties: {},
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          text: { type: 'string' },
        },
        required: ['ok', 'text'],
        additionalProperties: true,
      },
      // ★ 2026-10-17 适配 DSH 0.2.1：ToolOutputDefinition 的契约是
      //   { schema, render, presentationMeta? } —— render **必须在 output 内**。
      //   旧代码把 render 放在工具顶层（output 外），宿主校验报
      //   "tool must declare output { schema, render, presentationMeta? }" 并整条拒收；
      //   safeRegister 吞掉异常 ⇒ 静默不注册（与"静默失效"同型）。
      render(_args, value) {
        return [{ type: 'text', text: value.ok ? `【HippoMem 引擎台账】\n${value.text}` : `hippo status 失败: ${value.text}` }]
      },
    },
    async execute() {
      return await new Promise((resolve) => {
        let child
        try {
          child = spawn(HIPPO_PYTHON, [HIPPO_PY, 'status'], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'], env: PY_ENV })
        } catch (e) {
          resolve({ ok: false, text: `spawn failed: ${e?.message ?? e}` })
          return
        }
        let out = ''
        let err = ''
        const t = setTimeout(() => {
          try { child.kill() } catch { /* noop */ }
          resolve({ ok: false, text: 'timeout 15s' })
        }, 15000)
        child.stdout.on('data', (d) => { out += String(d) })
        child.stderr.on('data', (d) => { err += String(d) })
        child.on('exit', (code) => { clearTimeout(t); resolve({ ok: code === 0, text: (out || err).slice(0, 2000) }) })
        child.on('error', (e) => { clearTimeout(t); resolve({ ok: false, text: String(e?.message ?? e) }) })
      })
    },
  }), 'memory_hippo_status tool')

  // ---- 2. system-prompt paragraph ---------------------------------------
  // `ctx.inject(deps, cb)` is the optional-dependency idiom: the callback runs only when the
  // service exists, so a composition without dsh-system-prompt still loads this plugin.
  safeRegister(ctx, () => {
    ctx.inject(['systemPrompt'], (spCtx) => {
      try {
        spCtx.systemPrompt.section({ name: 'memory:recall-policy', order: 6000, text: POLICY_TEXT })
        if (DEBUG) log('systemPrompt section registered')
      } catch (error) {
        log(`systemPrompt section not registered: ${error?.message ?? error}`)
      }
    })
  }, 'systemPrompt section')

  // ---- 2b. llm 服务：**等它来并缓存**（2026-10-15 第三轮根治）-------------
  // 病根：编排器用 `ctx.get('llm')` 碰运气 ⇒ 实测间歇取不到
  //   （2026-10-15T10:34:44Z 两条 `call-failed / llm service unavailable / ms=0`）。
  // 做法：与上面 `ctx.inject(['systemPrompt'])` 同一套"可选用依赖"写法 ——
  //   服务出现时缓存下来；取不到**不阻断插件加载**（编排器本来默认关）。
  safeRegister(ctx, () => {
    // 先试一次直接取（服务已就绪的常见情形）
    __llmSvc = llmOf(ctx) || __llmSvc
    ctx.inject(['llm'], (lctx) => {
      const got = llmOf(lctx) || llmOf(ctx)
      if (got) {
        __llmSvc = got
        if (DEBUG) log('llm service captured')
      } else {
        log(`llm service not visible even after inject(['llm'])：${llmDiag(lctx)}`)
      }
    })
  }, 'llm service capture')

  // ---- 3. per-turn recall injection -------------------------------------
  safeRegister(ctx, () => {
    // Turn numbers are monotone per agent; `step` is NOT reset per turn (the loop does
    // `const step = phase.step + 1` over the whole driver wake — see the runtime's
    // dsh-agent-loop/lib/index.js:953), so keying injection off `step === 1` would fire
    // only once per wake. Key off the turn instead: inject exactly once per turn.
    /** @type {Map<string, {turn:number, hour:number, hourCount:number, lastQueryAt:number, lastQuery:string, lastKeys:Set<string>}>} */
    const state = new Map()
    const stateOf = (key) => {
      let s = state.get(key)
      if (s === undefined) {
        s = { turn: -1, hour: -1, hourCount: 0, lastQueryAt: 0, lastQuery: '', lastKeys: new Set() }
        state.set(key, s)
        if (state.size > 500) {           // 有界：别把内存当记忆库
          const first = state.keys().next().value
          if (first !== undefined) state.delete(first)
        }
      }
      return s
    }

    /**
     * 2026-10-04 新增：解析"这一轮该走哪个 provider/model"。
     *
     * 病根：`agent/pre-step` 的真实 payload **只有** `{messages, turn, step, signal}`
     *      （实证：dsh-agent-loop 里 `dispatch.waterfall("agent/pre-step", {messages: claimed, ...position, signal}, ...)`）——
     *      **根本没有 provider/model**。而 orchestrateTurn 里的 resolveModel 读的是
     *      `payload.agent.model` / `payload.model`，全都取不到 ⇒ 返回空串
     *      ⇒ `decisions.jsonl` 里 92/92 全是 `"model":""`、`empty stream`。
     *
     * 解法：多级兜底，从最可靠到最宽松
     *   ① payload 里若真有（未来版本可能加）
     *   ② agent 服务上的 options.provider / options.model（实测 agent-loop 用的是这个）
     *   ③ llm 服务 providerInfo / listModels 反查（单 provider 场景）
     *   ④ 配置文件（cordis.patch.yml 里 agent-default-model）
     */
    async function resolveRoute(ctx, payload) {
      const pick = (p, m) => (p && m ? { provider: String(p), model: String(m) } : null)
      try {
        // ① payload（当代没有，留给未来）
        const a = payload && payload.agent
        const direct = pick(
          a?.provider || payload?.provider || a?.config?.provider,
          a?.model || payload?.model || a?.config?.model
        )
        if (direct) return direct

        // ② agent 服务：真实路由所在（agent-loop: this.options.provider / this.options.model）
        try {
          const ag = typeof ctx.get === 'function' ? ctx.get('agent') : undefined
          if (ag) {
            let opts = ag.options
            if (!opts && typeof ag.get === 'function' && a?.id) {
              try { opts = ag.get(a.id)?.options } catch { /* noop */ }
            }
            const fromAgent = pick(opts?.provider, opts?.model)
            if (fromAgent) return fromAgent
          }
        } catch { /* noop */ }

        // ③ 配置文件兜底（★修正⑦：提前到 listModels 之前 —— 2026-10-04 实测：
        //    旧顺序下 listModels 硬编码 ['bbb','aaa','deepseek-official']（没有 ccc）
        //    且“随便取第一个模型”，②级 miss 时编排器会落到 bbb/deepseek-flash，
        //    违背「…」。agent-default-model 是用户显式配的默认，
        //    优先级必须高于“哪个 provider 能列就取谁”。）
        try {
          const fs = await import('node:fs')
          const os = await import('node:os')
          const path = await import('node:path')
          const cfgPath = path.join(os.homedir(), '.dsh', 'profiles', 'desktop', 'cordis.patch.yml')
          if (fs.existsSync(cfgPath)) {
            const txt = fs.readFileSync(cfgPath, 'utf8')
            const mp = txt.match(/agent-default-model[\s\S]{0,120}?provider:\s*([\w.-]+)[\s\S]{0,80}?model:\s*([\w.-]+)/)
            if (mp) return { provider: mp[1], model: mp[2] }
          }
        } catch { /* noop */ }

        // ④ llm 服务反查（最后手段：哪个 provider 能列模型就取谁——顺序也补上 ccc）
        try {
          const llm = llmOf(ctx)     // ★ 2026-10-15(3)：ctx.llm 优先（见文件头 llm 服务取法）
          if (llm && typeof llm.listModels === 'function') {
            const provs = ['ccc', 'bbb', 'aaa', 'deepseek-official']
            for (const p of provs) {
              try {
                const ms = await llm.listModels(p)
                if (Array.isArray(ms) && ms.length) {
                  const id = ms[0]?.id ?? ms[0]?.model ?? ms[0]
                  const r = pick(p, id)
                  if (r) return r
                }
              } catch { /* 该 provider 不可用，试下一个 */ }
            }
          }
        } catch { /* noop */ }
      } catch { /* noop */ }
      return { provider: '', model: '' }
    }

    ctx.on('agent/pre-step', async (payload, next) => {
      const decision = await next()
      if (!AUTO || decision?.kind !== 'enter') return decision
      if (decision.messages.length === 0) return decision
      const key = payload.agent?.id ?? '<unknown>'
      const s = stateOf(key)
      if (s.turn === payload.turn) return decision
      // ★ HippoMem（2026-10-07）：上一轮事件边界 → 后台反思（fail-open，绝不阻塞注入链）
      hippoMaybeReflect(payload, decision)
      // ★★ 2026-10-04 修正②：**要求锚常驻块**（不受命中去重影响）
      //   病根：原来整条链路只有在「检索到东西」时才注入，
      //        而用户的要求既不在 query 里、也不一定检索得到 ⇒ 每轮都在丢要求。
      //   修法：goal + BRIEF 组成常驻块，**先算好**，任何时候都附到 decision 上。
      const goalText = extractGoalText(payload)
      // 项目连续性包：只在用户主动开启后注入；确认项带出处，候选项不会冒充事实。
      try {
        const continuity = loadContinuity(MEM_BASE)
        const packetText = renderContinuityPacket(continuity, { sessionId: key })
        if (packetText) decision.messages = [...decision.messages, pluginMessage(packetText)]
      } catch (e) { if (DEBUG) log(`continuity packet skipped: ${e?.message ?? e}`) }
      // ★ 修正⑥：锚按会话隔离 —— payload.agent.id 即本会话 id，只取本会话的锚
      const briefText = loadBriefText(MEM_BASE, payload.agent?.id ?? '')
      const goalBlock = renderGoalBlock(goalText, briefText)
      if (goalBlock) {
        // ★★ 2026-10-04 修正⑤（决定性）：必须用 pluginMessage() 注入
        //   实测教训：裸 {role:'user',content:[…]} 没有 id+source 标记 ⇒ 被框架**静默丢弃**
        //   —— goal-anchor 从未出现在任何一轮上下文，而 orchestrator 自己的块
        //   （同样走 pluginMessage）每轮都出现。两者唯一差异就是 source 标记。
        //   另：lastUserText 会跳过 plugin:* 来源 ⇒ 本注入不会污染 recall query。
        try {
          decision.messages = [...decision.messages, pluginMessage(goalBlock)]
          if (DEBUG) log(`goal-anchor injected ${goalBlock.length} chars`)
        } catch (e) {
          // 失败必须可见（D6 同款纪律）：不许静默吞
          try { console.error('[goal-anchor] 注入失败（不静默吞）：', e?.message ?? e) } catch { /* noop */ }
        }
      }

      // only recall on something worth searching for — never on harness chrome
      const userText = cleanQuery(lastUserText(decision.messages))
      // 连续性写侧只形成候选，不自动升格为事实；确认必须由用户/工具提供出处。
      // ★ 2026-10-21 修：候选的 project 必须是**来源会话的真实项目**，
      //   不能拿 continuity.project（那是授权筛选条件）当标签 ——
      //   实测污染：18 条候选全被贴成 "PCM_RUNTIME_TEST"，而它们的 sourceSession 来自 4 个真实会话。
      try {
        const continuity = loadContinuity(MEM_BASE)
        // ★ 2026-10-22：写侧也必须按**本对话自己的**范围判定。
        //   旧版读全局 enabled/mode ⇒ A 对话开着，B 对话也在悄悄收集，
        //   这就是"切到别的对话也被接上"的另一半（不只是注入，连采集都是全局的）。
        const scope = scopeFor(continuity, key)
        if (scope.enabled && scope.mode !== 'off') {
          const sourceProject = await resolveSessionProject(MEM_BASE, key)
          // 判定与标签都走 candidateCaptureDecision（同一份实现，测试也调它）。
          // selfKey = 本对话：自己的内容永远允许（接上别人不能丢掉自己）。
          const pick = candidateCaptureDecision({ continuity: scope, sourceProject, sessionKey: key, selfKey: key })
          if (pick.allowed) {
            const cite = `dsh:${key}#turn=${Number(payload.turn) || 0}`
            // ★ 2026-10-22：自动确认由**本对话的 scope**决定（默认开）。
            //   用户原话：「<用户原话已隐去：自动确认不该靠人点>」
            //   守卫（问句不升格 / 目标锁不自动覆盖 / 只收决定类）都在 continuity.js 里，
            //   这里只负责把开关传下去。
            const auto = { autoConfirm: scope.autoConfirm === true }
            if (goalText) captureContinuityCandidate(MEM_BASE, { kind: 'goal', text: goalText, cite, sourceSession: key, project: pick.project }, auto)
            // ★ 2026-10-21：不再"任何 ≥6 字的用户话都存成 episode"。
            //   实测污染：用户粘贴的一段别处群聊记录、一轮里的运行环境样板，全被存成"项目事实候选"，
            //   随后被注入到状态包里（用户当场贴出铁证）。候选只是提示，宁缺毋滥。
            const stmt = userText ? classifyContinuityStatement(userText) : null
            if (stmt) {
              captureContinuityCandidate(MEM_BASE, { kind: stmt.kind, text: stmt.text, cite, sourceSession: key, project: pick.project }, auto)
            } else if (DEBUG && userText) log('continuity candidate skipped: not-a-statement')
          } else if (DEBUG) log(`continuity candidate skipped: ${pick.reason}`)
        }
      } catch (e) { if (DEBUG) log(`continuity write candidate skipped: ${e?.message ?? e}`) }
      // ★ 修正①：「继续」不再 return（旧行为会让整轮跳过）
      const smallTalk = userText === undefined || isSmallTalkTurn(userText)
      // ★ 修正②：query 必须带上 goal —— 否则「继续」这类轮检索的是空气
      //
      // ★★ 2026-10-15 修根因：**去掉 `|| userText` 那个兜底**。
      //   旧版 `buildQueryWithGoal(...) || userText` ⇒ 小话轮且**没有 goal** 时，
      //   query 会退化成「继续」两个字 ⇒ 拿它去检索，召回一片无关历史 ——
      //   这正是"不正确的注意力提示"的一种来源（用噪音当"用户在问什么"）。
      //   现在：无 goal 的小话轮**直接跳过召回**。要求锚（goalBlock）是独立注入的
      //   （见上方 goal-anchor 分支），跳过这里不会丢用户要求。
      const query = buildQueryWithGoal(smallTalk ? '' : userText, goalText)
      if (query === undefined || query === '') {
        if (DEBUG) log('skip: small-talk without goal — 不拿噪音当检索词')
        // ★ 2026-10-15(5)：**跳过也要留痕**（否则"有意跳过"与"插件没跑"分不出来）
        __orchLogSkip({ baseDir: MEM_BASE, sessionKey: key, why: 'small-talk-no-goal', enabled: __orchEnabled(),
          extra: { query: String(userText || '').slice(0, 40), smallTalk } })
        return decision
      }
      // ★★ 2026-10-15(8)：**这里原本有一道"形状闸"（isAckOnly：短+无问号+无请求词 ⇒ 不回顾），已删除** ★★
      //   用户 2026-10-15 原话否决：「…」
      //   它错在两处：① 按**句子形状**切，与病根无关 —— 重复问同一件事会被误杀
      //   （再问一次「廊柱截面多大」同样是 6 字、无问号、无请求词）；
      //   ② 真病根是「**同一句话在别处出现过就被当成知识**」，形状闸根本没碰它。
      //   ⇒ 改由下面的**回声过滤**（按信息增量判，与句子长短/是否重复无关）根治。
      s.turn = payload.turn
      const hour = Math.floor(Date.now() / 3600000)
      if (s.hour !== hour) { s.hour = hour; s.hourCount = 0 }
      // D. 同 query 去重（默认 30 分钟窗口）
      const nq = normalizeQuery(query)
      if (DEDUP_MIN > 0 && s.lastQuery === nq && Date.now() - s.lastQueryAt < DEDUP_MIN * 60000) {
        if (DEBUG) log(`dedup: same query within ${DEDUP_MIN} min — injection skipped`)
        __orchLogSkip({ baseDir: MEM_BASE, sessionKey: key, why: 'dedup-same-query', enabled: __orchEnabled(),
          extra: { query: nq.slice(0, 40) } })
        return decision
      }
      try {
        refreshIndex()
        // 关键：**不要**限定 session！限定等于只在当前会话里搜，跨会话记忆就废了
        // （2026-09-30 质量验证发现：注入回来的命中全部来自当前会话自己）
        const result = await recallImpl({ query, k: K, signal: payload.signal })
        if (!result.ok || result.stdout.trim() === '') {
          __orchLogSkip({ baseDir: MEM_BASE, sessionKey: key, why: 'recall-empty', enabled: __orchEnabled(), extra: {} })
          return decision
        }

        let parsed = null
        try { parsed = JSON.parse(result.stdout) } catch { parsed = null }
        if (parsed !== null && Array.isArray(parsed.hits)) {
          // 连续性模式开启时，仅把带真实出处的召回结果存为“待确认候选”；不会直接升级为事实。
          try {
            const cc = loadContinuity(MEM_BASE)
            // ★ 2026-10-22：召回捕获同样是**本对话自己的**开关。
            //   旧版读全局 cc.enabled ⇒ 一条开着连续性的对话，会替所有对话把召回内容收成候选，
            //   之后这些候选又按各自的 scope 注入 —— 采集侧也必须是 per-conversation。
            const scope = scopeFor(cc, key)
            if (scope.enabled && scope.mode !== 'off') {
              for (const h of parsed.hits.slice(0, 3)) {
                const cite = String(h?.cite || (h?.session_id && h?.turn !== undefined ? `dsh:${h.session_id}#turn=${h.turn}` : ''))
                const sourceSession = String(h?.session_id || '')
                const sourceProject = String(h?._session?.cwd || h?._session?.dir || h?.project || '')
                // 复用写侧判定，避免"召回捕获"与"当前轮捕获"两套口径各写一份。
                // selfKey=本对话：本对话自己的命中永远允许（接上别人不能丢了自己）。
                const pick = candidateCaptureDecision({ continuity: scope, sourceProject, sessionKey: sourceSession, selfKey: key })
                // ★ 2026-10-21：召回命中也必须过卫生 —— 用户贴出的铁证就是"别的会话群聊记录"
                //   经 recall 命中被存成候选，再被注入状态包。这里漏判等于前面所有卫生都白做。
                const inScope = pick.allowed && isSafeContinuityCandidate(h?.text)
                // ★ 2026-10-22：召回片段一律 **不自动确认**（episode 是上下文，不是用户的决定）。
                //   即使 scope.autoConfirm=true，这里也显式传 false —— 这是守卫②。
                if (inScope && cite && h?.text) captureContinuityCandidate(MEM_BASE, { kind: 'episode', text: h.text, cite, sourceSession, project: sourceProject }, { autoConfirm: false })
              }
            }
          } catch (e) { if (DEBUG) log(`continuity candidate capture skipped: ${e?.message ?? e}`) }
          // 去掉自己上次注入的回声（v0.1.4 起；实测注入块曾 13.8K→40K 滚雪球）
          // ★★ 2026-10-15 修根因：旧过滤串写成 `'<memory_recall source='`，
          //    而真块形态是 `<memory_recall mode="digest" source="dsh-memory-plugin" …>` ——
          //    `mode=` 在 `source=` 之前 ⇒ **永远不命中** ⇒ 这行防护**从未生效**
          //    （记忆库里 2686 条 `<memory_recall` 注入块自证；实测 8 种写法全漏）。
          //    改为按**标签**判定（core.js `isOwnInjectionText`，允许前带 `[用户] ` 角色前缀）。
          const fresh = parsed.hits.filter((h) => !__isOwnInjection(h?.text))
          if (fresh.length === 0) {
            if (DEBUG) log('recall hits were all our own past injections — injection skipped')
            return decision
          }
          parsed.hits = fresh
        }

        // ★★ 2026-10-15(8)：**回声过滤** —— 只按"信息增量"判，与句子长短/是否重复**无关** ★★
        //   判据：候选只是把 query 又抄了一遍、且没有任何具体内容（数字/路径/标识符/决定口径）
        //   ⇒ 它是**回声**，信息增量 0，不能当"事实"注入（thr 见 cfg.echoRatioMax，默认 0.6）。
        //   反例（必须放过）：重复问「廊柱截面多大」，而候选是「廊柱专条 Ø0.22 × 高 2.80，已定口径」
        //   —— 含 query 但**带具体内容** ⇒ 不是回声 ⇒ 照常进判官 ⇒ **重复提问照样能召回**。
        if (Array.isArray(parsed.hits) && parsed.hits.length) {
          const __echo = __filterEcho(parsed.hits, query, { ratioMax: __echoThreshold(MEM_BASE) })
          if (__echo.dropped.length) {
            if (DEBUG) log(`echo filter: 剔除 ${__echo.dropped.length} 条回声候选（信息增量为 0）`)
            __orchLogSkip({ baseDir: MEM_BASE, sessionKey: key, why: 'echo-filtered', enabled: __orchEnabled(),
              extra: { query: String(userText || '').slice(0, 40), echoDropped: __echo.dropped.slice(0, 3) } })
          }
          parsed.hits = __echo.kept
          if (parsed.hits.length === 0) {
            if (DEBUG) log('all candidates were echoes — injection skipped')
            return decision
          }
        }

        // B. 子会话判定（origin=subagent）
        const isSub = await isSubagentSession(key, { signal: payload.signal })
        // A/C. 模式决策
        const choice = decideInjection({ verdict: parsed?.verdict, isSubagent: isSub })

        // 限流：超每小时上限后只放 strong
        const overCap = MAX_INJECT_PER_HOUR > 0 && s.hourCount >= MAX_INJECT_PER_HOUR
        const mode = overCap && choice.mode !== 'digest' && choice.mode !== 'compact' ? 'skip' : choice.mode
        const reason = overCap ? `${choice.reason}+hourcap` : choice.reason

        if (mode === 'skip') {
          if (DEBUG) log(`skip turn=${payload.turn} reason=${reason}`)
          __orchLogSkip({ baseDir: MEM_BASE, sessionKey: key, why: `mode-skip:${reason}`, enabled: __orchEnabled(),
            extra: { candidates: (parsed?.hits || []).length } })
          s.lastQuery = nq
          s.lastQueryAt = Date.now()
          return decision
        }

        // D. 命中集未变 ⇒ 不重复注入
        const keys = hitKeys(parsed)
        if (DEDUP_MIN > 0 && keys.length > 0 && keys.every((k) => s.lastKeys.has(k))) {
          if (DEBUG) log('dedup: identical hit set — injection skipped')
          __orchLogSkip({ baseDir: MEM_BASE, sessionKey: key, why: 'dedup-identical-hits', enabled: __orchEnabled(),
            extra: { candidates: (parsed?.hits || []).length } })
          s.lastQuery = nq
          s.lastQueryAt = Date.now()
          return decision
        }

        let block
        if (mode === 'hint') {
          block = buildHintBlock(query, parsed)
        } else if (mode === 'compact') {
          block = buildDigestBlock({ query, parsed, mode: 'compact' }).block
        } else if (INJECT_FORMAT === 'raw') {
          const body = String(result.stdout).trim()
          block = [
            '<memory_recall mode="raw" source="dsh-memory-plugin">',
            `query: ${query}`,
            body.slice(0, INJECT_MAX_CHARS),
            '</memory_recall>',
          ].join('\n')
        } else {
          block = buildDigestBlock({ query, parsed, mode: 'digest' }).block
        }

        // ---- 注意力编排层（默认关；开启时由它替换注入块）----
        try {
          // ★ 本会话上下文（项目目录 + 用户口径）：**一次 spawn、一个缓存**，任何异常都是空表
          const __sd = await fetchSessionContext(key, { signal: payload?.signal })
            .catch(() => ({ domains: [], keypoints: [] }))
          const __o = await __orchTurn({            ctx,
            payload,
            decision,
            query,
            parsed,
            isSubagent: isSub,
            // ★ 2026-10-15：会话 key（告警按会话隔离）+ 真路由解析器（让块头/台账写真实模型名）
            sessionKey: key,
            resolveRoute: () => resolveRoute(ctx, payload),
            baseDir: MEM_BASE,
            // ★ 2026-10-16：本会话**真实活动过**的项目目录（R4 项目闸判据）——
            //   实测只看用户话取不到线索（路径都在 tool_call/tool_result 里）。
            // ★ 2026-10-17：同一次取数还带回**本会话用户口径**（keypoints）——
            //   治"本会话定过的口径进不了候选池 ⇒ 判官空手 ⇒ 信息丢失"（见 getSessionKeypoints 注释）。
            //   任何异常 ⇒ 空表，闸/候选退回旧行为（fail-open，绝不阻断对话）。
            sessionDomains: __sd.domains,
            sessionKeypoints: __sd.keypoints,
            callModel: async ({ messages, provider, model, timeoutMs }) => {
              // ★ 2026-10-15(3)：`ctx.llm` 优先 + 缓存（inject(['llm']) 拿到的那个）。
              //   旧写法 `ctx.get('llm')` 会间歇取不到 ⇒ 台账里 `ms=0 / llm service unavailable`。
              //   抛错时**带上诊断**（看到了什么），下次排查不必再猜。
              const svc = __llmSvc || llmOf(ctx)
              if (!svc || typeof svc.stream !== 'function') {
                throw new Error('llm service unavailable（' + llmDiag(ctx) + '）')
              }

              // ── 2026-10-04 修复：必须走 prepareCall 才能真正路由到 provider ──
              // 病根：原实现直接 svc.stream({messages})，**没有 provider/model 路由**，
              //       流里一个 chunk 都不来 ⇒ `empty stream`。
              //       实测 2026-10-03 decisions.jsonl：92/92 全失败（ms=0~2，连模型都没调起来）。
              // 真相（读 dsh-agent-loop 的真实用法）：
              //       preparedCall = await llm.prepareCall({provider, model}, signal)
              //       config = preparedCall.config        ← 收**一个 config 对象**
              let prov = provider
              let mod = model
              if (!prov || !mod) {
                const rm = await resolveRoute(ctx, payload)
                prov = prov || rm.provider
                mod = mod || rm.model
              }
              if (!prov || !mod) throw new Error('no provider/model route resolved')

              let acc = ''
              const ac = new AbortController()
              const t = setTimeout(() => { try { ac.abort() } catch { /* noop */ } }, timeoutMs || 20000)
              try {
                let streamFn = null
                let cfg = { provider: prov, model: mod }
                if (typeof svc.prepareCall === 'function') {
                  const prepared = await svc.prepareCall({ provider: prov, model: mod })
                  if (prepared) {
                    if (prepared.config) cfg = prepared.config
                    if (typeof prepared.stream === 'function') streamFn = prepared.stream
                  }
                }
                if (!streamFn) {
                  if (typeof svc.stream !== 'function') throw new Error('llm.stream unavailable')
                  streamFn = (o) => svc.stream(o)
                }
                // ★修正⑧（2026-10-04，用户：保持，该修复的修复）：超时信号必须真正接进请求 ——
                //   病根：AbortController 建了、abort() 也会触发，但 ac.signal 从未挂到 req 上
                //   （旧代码只挂 payload.signal）⇒ 20s 超时是摆设，实测挂到 140s 才被对端断掉。
                //   修法：req.signal = ac.signal（超时接管）；payload 的取消转发到 ac（两个信号都要活）。
                const req = { ...cfg, messages, signal: ac.signal }
                try {
                  if (payload && payload.signal && typeof payload.signal.addEventListener === 'function') {
                    payload.signal.addEventListener('abort', () => { try { ac.abort() } catch { /* noop */ } })
                  }
                } catch { /* noop */ }
                for await (const chunk of streamFn(req)) {
                  if (!chunk) continue
                  if (chunk.type === 'text-delta' && typeof chunk.text === 'string') acc += chunk.text
                  else if (chunk.type === 'text' && typeof chunk.text === 'string') acc += chunk.text
                  else if (chunk.type === 'block-start' && chunk.block && typeof chunk.block.text === 'string') acc += chunk.block.text
                  else if (chunk.type === 'finish' && chunk.reason && chunk.reason.kind === 'error') {
                    const f = chunk.reason.failure || {}
                    throw new Error(f.message || f.code || 'stream failed')
                  }
                }
              } finally { clearTimeout(t) }
              if (!acc) throw new Error('empty stream')
              return acc
            },
            fallbackModel: { provider: '', model: '' },
          })
          if (__o && __o.status === 'ok' && __o.block) {
            s.hourCount += 1
            s.lastQuery = nq
            s.lastQueryAt = Date.now()
            s.lastKeys = new Set(keys)
            if (DEBUG) log(`orch inject turn=${payload.turn} model=${__o.model} ms=${__o.ms} why=${__o.why}`)
            return { kind: 'enter', messages: [...decision.messages, pluginMessage(__o.block)] }
          }
          // ── 2026-10-04：失败必须**可见**，不能静默 ──
          // ★★ 2026-10-15 修根因：**只有真失败才提示**。
          //   旧版这里对"任何非块返回值"都去问 failureNotice()，而 `disabled`/`gated`
          //   与 `failed` 在旧契约里都表现为 null ⇒ **功能关着也可能弹"连续失败"告警**
          //   （repro2.mjs 已复现：3 连败后关掉开关，冷却期一过照样弹）。
          //   现在 host.js 返回四态，这里只认 failed，且告警文本由 host.js 当场生成。
          if (__o && __o.status === 'failed' && __o.notice) {
            if (DEBUG) log(`orch failed turn=${payload.turn} err=${__o.error}`)
            return { kind: 'enter', messages: [...decision.messages, pluginMessage(__o.notice)] }
          }
          // disabled / gated ⇒ **静默回落**，不提示
          //
          // ★ 2026-10-15（第二轮）：`empty`（编排器判"候选里没有相关的"）同样**静默回落** ——
          //   往下走会注入老路的 digest/hint 块（自带 score/出处 与"别当常识"提醒）。
          //   旧行为是 core.js 的 `R2 兜底` 把检索最高分的那条**冒充成「必须常驻的事实」**，
          //   实证：2026-10-15T10:32Z 用户问"重启重试测验"，注入的却是"stream idle timeout"
          //   的助手思考片段 —— 那才是"不正确的注意力提示"。
          if (__o && __o.status === 'empty') {
            // ★★ 2026-10-15(6) 修根因：**空手不再回落老路 digest**。
            //   病根：旧版"编排器判无关"之后仍往下走、把**同一批候选**以 digest 形式灌进上下文
            //   （换个标签而已，噪音照旧）—— 与 R2 冒充同一个毛病：代码不认判官的结论。
            //   现在：只留一行说明（不注入正文），并照常记账，保证"为什么没内容"看得见。
            if (DEBUG) log(`orch empty turn=${payload.turn} ⇒ 不注入正文`)
            s.hourCount += 1
            s.lastQuery = nq
            s.lastQueryAt = Date.now()
            s.lastKeys = new Set(keys)
            const __eb = [
              '<memory_recall mode="orchestrator-empty" source="dsh-memory-plugin">',
              `编排器判定：本轮命中 ${keys.length} 条候选，但**与本轮提问无关** ⇒ 不注入正文。`,
              '（这是**判定**，不是故障；需要时用 `memory_recall` 工具自行检索。）',
              '</memory_recall>',
            ].join('\n')
            return { kind: 'enter', messages: [...decision.messages, pluginMessage(__eb)] }
          }
          if (__o && __o.status === 'gated') {
            __orchLogSkip({ baseDir: MEM_BASE, sessionKey: key, why: `gated:${__o.why || ''}`, enabled: __orchEnabled(),
              extra: { isSubagent: isSub } })
          }
        } catch (__e) {
          log('orchestrator failed open: ' + (__e?.message ?? __e))
          try {
            const __n = __orchFailureNotice(key)
            if (__n) return { kind: 'enter', messages: [...decision.messages, pluginMessage(__n)] }
          } catch { /* noop */ }
        }
        // ---- 编排未开启/失败 ⇒ 以下走 v0.1.5 原逻辑 ----

        s.hourCount += 1
        s.lastQuery = nq
        s.lastQueryAt = Date.now()
        s.lastKeys = new Set(keys)
        if (DEBUG) {
          log(`inject turn=${payload.turn} mode=${mode} reason=${reason} sub=${isSub} `
            + `query=${JSON.stringify(query.slice(0, 40))} block=${block.length} chars`)
        }
        return { kind: 'enter', messages: [...decision.messages, pluginMessage(block)] }
      } catch (error) {
        log(`pre-step recall failed open: ${error?.message ?? error}`)
        return decision
      }
    })
    if (DEBUG) {
      log(`pre-step recall hook registered (auto=${AUTO}, k=${K}, weak=${WEAK_MODE}, subagent=${SUBAGENT_MODE}, `
        + `dedup=${DEDUP_MIN}min, format=${INJECT_FORMAT})`)
    }
  }, 'agent/pre-step hook')

  // ---- 4. session lifecycle hook (0.2.x event name) ----------------------
  safeRegister(ctx, () => {
    ctx.on('agent/created', ({ agent, source }) => {
      if (DEBUG) log(`agent/created id=${agent?.id ?? '?'} source=${source ?? '?'}`)
      refreshIndex()
    })
  }, 'agent/created hook')

  // ---- 5. slash command (optional service) ------------------------------
  safeRegister(ctx, () => {
    ctx.inject(['commands'], (cmdCtx) => {
      try {
        cmdCtx.commands.register({
          name: 'memory-recall',
          description: 'Search the cross-session memory index.',
          input: { hint: 'what to search for' },
          async handler(invocation) {
            const query = invocation.rawInput.trim()
            if (query === '') return { kind: 'error', text: 'usage: /memory-recall <query>' }
            // 同样不限定会话：这是一次"翻历史"的检索，锁在当前会话等于白问
            const result = await runMemRecall({ query, k: K, signal: invocation.signal })
            if (!result.ok) return { kind: 'error', text: `memory_recall failed: ${result.stderr.trim()}` }
            return { kind: 'success', text: result.stdout.trim() }
          },
        })
        if (DEBUG) log('commands.register registered')
      } catch (error) {
        log(`slash command not registered: ${error?.message ?? error}`)
      }
    })
  }, 'slash command')

  // ---- 6. HTTP route (optional service, Web compositions only) -----------
  safeRegister(ctx, () => {
    ctx.inject(['webServer'], (webCtx) => {
      // ★ 诊断：问 DSH 自己的 clientModules 服务，它眼里有没有本插件的客户端半侧
      try {
        webCtx.webServer.register({
          kind: 'exact',
          path: '/dsh-memory/client-diag',
          handler: async (_req, res) => {
            const out = { askedAt: new Date().toISOString() }
            try {
              const cm = typeof ctx.get === 'function' ? ctx.get('clientModules') : undefined
              out.serviceFound = cm !== undefined && cm !== null
              if (cm) {
                out.serviceKeys = Object.keys(Object.getPrototypeOf(cm) || {}).concat(Object.keys(cm)).slice(0, 30)
                try {
                  const g = typeof cm.graph === 'function' ? cm.graph() : undefined
                  if (g) {
                    out.graphRev = g.rev
                    out.entryCount = Array.isArray(g.entries) ? g.entries.length : undefined
                    out.entryIds = (g.entries || []).map((e) => e && e.id).filter(Boolean)
                    out.hasMine = out.entryIds.includes('dsh-memory-plugin')
                    out.batchCount = Array.isArray(g.batches) ? g.batches.length : undefined
                  }
                } catch (e2) { out.graphError = String(e2?.message ?? e2) }
                try {
                  out.myClientPath = typeof cm.clientPath === 'function' ? cm.clientPath('dsh-memory-plugin') : undefined
                } catch (e3) { out.clientPathError = String(e3?.message ?? e3) }
                // ★ 关键：不调 serveBundle（它要真 req/res），改为**逐个检查 combo 里
                //    每个包的 bundle 文件是否都真实存在** —— 任何一个缺失，整条 combo 就失败。
                try {
                  const g2 = cm.graph()
                  const batches = (g2 && g2.batches) || []
                  out.batchUrls = batches.map((b) => b && b.url)
                  const mine = batches.find((b) => String((b && b.url) || '').includes('dsh-memory-plugin'))
                  if (mine) {
                    out.myBatchUrl = mine.url
                    const q = String(mine.url).split('??')[1] || ''
                    const ids = q.split('&')[0].split(',').map((s) => s.replace(/\/client\.js$/, '')).filter(Boolean)
                    out.myBatchIds = ids
                    const check = []
                    for (const id of ids) {
                      let p
                      try { p = typeof cm.clientPath === 'function' ? cm.clientPath(id) : undefined } catch { p = undefined }
                      let exists = false
                      let size = 0
                      try { if (p) { const st = statSync(p); exists = true; size = st.size } } catch { exists = false }
                      check.push({ id, exists, size })
                    }
                    out.myBatchCheck = check
                    out.myBatchMissing = check.filter((c) => !c.exists).map((c) => c.id)
                    out.myBatchAllPresent = out.myBatchMissing.length === 0
                    // 浏览器真正会执行的那份内容 = 各 bundle 拼接；这里验我自己的那份可读
                    const myP = typeof cm.clientPath === 'function' ? cm.clientPath('dsh-memory-plugin') : undefined
                    if (myP) {
                      try {
                        const txt = readFileSync(myP, 'utf8')
                        out.myBundleBytes = txt.length
                        out.myBundleHasCode = txt.includes('memory-orchestrator')
                        out.myBundleInject = (txt.match(/const inject = \[[^\]]*\]/) || [null])[0]
                      } catch (e5) { out.myBundleReadError = String(e5?.message ?? e5) }
                    }
                  } else {
                    out.myBatchFound = false
                  }
                } catch (e4) { out.batchCheckError = String(e4?.message ?? e4) }
              }
            } catch (e) { out.error = String(e?.message ?? e) }
            res.writeHead(200, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
            res.end(JSON.stringify(out, null, 1))
          },
        })
      } catch (error) {
        log(`client-diag route not registered: ${error?.message ?? error}`)
      }

      try {
        webCtx.webServer.register({
          kind: 'exact',
          path: '/dsh-memory/health',
          handler: async (_req, res) => {
            try {
              const result = await runMemRecall({ query: 'health', k: 1 })
              res.writeHead(200, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
              res.end(JSON.stringify({
                plugin: PLUGIN_ID, version: VERSION, script: SCRIPT, ok: result.ok, exitCode: result.exitCode,
                pid: process.pid, startedAt: INSTANCE_STARTED_AT,
                policy: { weak: WEAK_MODE, subagent: SUBAGENT_MODE, dedupMin: DEDUP_MIN, format: INJECT_FORMAT },
              }))
            } catch (error) {
              log(`/dsh-memory/health failed: ${error?.message ?? error}`)
            }
          },
        })
        if (DEBUG) log('webServer route registered at /dsh-memory/health')

        // 轻量实例身份探针（不 spawn mem.py，毫秒级）。
        // 为什么单独一条：/dsh-memory/health 会跑一次 mem.py recall，实测 8.2s；
        // 用它做端点发现会拖慢验收，也会让短超时误判「不可达」。
        webCtx.webServer.register({
          kind: 'exact',
          path: '/dsh-memory/instance',
          handler: (_req, res) => {
            res.writeHead(200, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
            res.end(JSON.stringify({
              plugin: PLUGIN_ID, version: VERSION, pid: process.pid, startedAt: INSTANCE_STARTED_AT,
              memoryBase: MEM_BASE,
            }))
          },
        })
        try {
          const __n = __orchRoutes(webCtx, MEM_BASE)
          if (DEBUG) log(`orchestrator routes registered: ${__n}`)
        } catch (__e) { log('orch routes not registered: ' + (__e?.message ?? __e)) }
      } catch (error) {
        log(`web route not registered: ${error?.message ?? error}`)
      }
    })
  }, 'web route')

  log(`loaded v${VERSION} (auto=${AUTO}, k=${K}, weak=${WEAK_MODE}, subagent=${SUBAGENT_MODE}, `
    + `dedup=${DEDUP_MIN}min, format=${INJECT_FORMAT}, script=${SCRIPT})`)
}

/**
 * Run one optional registration, converting any failure into a stderr line.
 * An exception escaping `apply()` fails the whole application start, so optional
 * surface never gets to do that.
 * @param {unknown} _ctx - plugin context (kept for symmetry and future use).
 * @param {() => void} register - the registration to attempt.
 * @param {string} label - what to name in the diagnostic.
 */
function safeRegister(_ctx, register, label) {
  try {
    register()
  } catch (error) {
    log(`optional surface "${label}" not registered: ${error?.message ?? error}`)
  }
}
