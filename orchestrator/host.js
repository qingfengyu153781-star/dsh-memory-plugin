/**
 * dsh-memory-plugin · 宿主半侧编排接线（orchestrator/host.js）
 *
 * 职责：
 *   1. 注册 HTTP 路由，供右栏面板读写开关/模型/状态：/dsh-memory/orch/*
 *   2. 提供 orchestrateTurn()，供 index.js 的 agent/pre-step 钩子调用
 *   3. 解析"跟随当前会话模型 / 自选模型"
 *
 * 纪律：
 *   · **默认关**：enabled=false 时 orchestrateTurn 直接返回 null，index.js 走原逻辑。
 *   · **失败开放**：任何异常 ⇒ 返回 null，由调用方回落。
 *   · 每个注册都 try/catch —— 宿主插件抛异常会让整个 DSH 启动失败。
 */

import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import {
  loadConfig, saveConfig, stats, recentDecisions,
  shouldOrchestrate, orchestrate, hasForceSignal, isOwnInjectionText, INJECT_TAGS, logDecision,
  looksLikeRequest, isAckOnly, ageText,
  tokenize, echoRatio, hasSubstantive, isEchoOnly, filterEchoCandidates,
} from './core.js'

export {
  isOwnInjectionText, INJECT_TAGS, looksLikeRequest, isAckOnly, ageText,
  tokenize, echoRatio, hasSubstantive, isEchoOnly, filterEchoCandidates,
}

// ══════════════════════════════════════════════════════════════════════
// 跳过也要留痕（2026-10-15 第五轮）
// ──────────────────────────────────────────────────────────────────────
// 起因（本会话实测踩到）：用户说「已经重启」（4 字 < `MIN_QUERY_CHARS` 6）+ 无 goal
// ⇒ 被当"小话轮"**有意跳过召回**；但**跳过不写台账** ⇒ 台账里一片空白，
// **"有意跳过"与"插件根本没跑"分不出来** —— 我当场只能靠读门槛常量去反推。
// 这正是本工作区反复强调的病：**跳过/失败不改变任何可见状态 = 静默**。
//
// 做法：把"这一轮为什么没注入"写进同一本台账（`outcome:'skipped'`）。
//   · 只记**主会话**（子智能体轮次量大，记它会淹掉真信号）；
//   · 编排层未开启时不记（那是用户设置，`orchestrate-status` 一眼能看到）。
// ══════════════════════════════════════════════════════════════════════
export function logSkip({ baseDir = BASE, sessionKey = '', why = '', enabled = true, extra = {} } = {}) {  try {
    if (!enabled) return false
    if (extra && extra.isSubagent === true) return false
    logDecision(baseDir, {
      outcome: 'skipped',
      session: String(sessionKey || '<unknown>'),
      why: String(why || '').slice(0, 80),
      model: '',
      candidates: Number((extra && extra.candidates) ?? 0),
      ...(extra || {}),
    })
    return true
  } catch { return false }
}

/** 编排层此刻是否开启（5 秒缓存；给"跳过留痕"判断用，避免每次读盘）。 */
let __cfgCache = { at: 0, enabled: false }
export function isOrchEnabled(baseDir = BASE) {
  try {
    const now = Date.now()
    if (now - __cfgCache.at < 5000) return __cfgCache.enabled
    const en = !!loadConfig(baseDir).enabled
    __cfgCache = { at: now, enabled: en }
    return en
  } catch { return false }
}

/** 回声过滤阈值（`config.echoRatioMax`，默认 0.6）。调到 1 等于关掉过滤。 */
export function echoThreshold(baseDir = BASE) {
  try { return Number(loadConfig(baseDir).echoRatioMax ?? 0.6) } catch { return 0.6 }
}

const BASE = 'D:\\agent\\dsh-rag-memory'

/** 读 DSH 已配置的模型清单（给面板下拉用）。 */
export function listModels() {
  const out = []
  const cands = [
    'C:\\Users\\pop\\.dsh\\settings.yaml',
    'C:\\Users\\pop\\.dsh\\settings.yaml.imported',
  ]
  for (const p of cands) {
    try {
      if (!existsSync(p)) continue
      const s = readFileSync(p, 'utf8')
      // 极简解析：按缩进扫 llm-* 块里的 `- id: xxx`
      let cur = null
      for (const line of s.split('\n')) {
        const mTop = line.match(/^(llm-[A-Za-z0-9_-]+):/)
        if (mTop) { cur = mTop[1]; continue }
        if (!cur) continue
        if (/^\S/.test(line)) { cur = null; continue }
        const mId = line.match(/^\s*-\s*id:\s*(\S+)/)
        if (mId) out.push({ provider: cur, model: mId[1], label: mId[1] })
      }
      if (out.length) break
    } catch { /* 试下一个 */ }
  }
  // 去重
  const seen = new Set()
  return out.filter((m) => {
    const k = `${m.provider}|${m.model}`
    if (seen.has(k)) return false
    seen.add(k)
    return true
  })
}

/**
 * 解析这一轮编排该用哪个 provider/model。
 * follow 模式：从 payload 里拿当前会话的模型；拿不到就回落到自选/默认。
 */
export function resolveModel({ cfg, payload, fallback }) {
  if (cfg.modelMode === 'custom' && cfg.customProvider && cfg.customModel) {
    return { provider: cfg.customProvider, model: cfg.customModel }
  }
  // 跟随当前会话模型
  const cands = [
    payload?.agent?.model,
    payload?.model,
    payload?.agent?.config?.model,
    payload?.request?.model,
  ].filter(Boolean)
  if (cands.length) {
    const m = String(cands[0])
    // provider 未知时交给 llm 服务按名解析（核心 API 允许未列出的 ID）
    return { provider: payload?.agent?.provider || payload?.provider || '', model: m }
  }
  return fallback || { provider: '', model: '' }
}

/**
 * 一轮的函数：拿候选 → 请编排器 → 返回注入块。
 *
 * ★ 2026-10-15：返回值从"块 or null"改成**四态**。
 *   病根：旧版"功能关掉"和"调用失败"都返回 null，调用方拿到 null 只能猜，
 *        于是走 `failureNotice()` 这条路 ⇒ **功能明明关着也会弹"连续失败"告警**
 *        （已用 repro2.mjs 复现：3 连败后关掉开关，冷却期一过照样弹）。
 *   现在：`{status:'disabled'}` / `{status:'gated'}` / `{status:'failed', error, notice}` /
 *        `{status:'ok', block, plan, ms, model, why}` —— **调用方不必再猜**。
 *
 * @returns {Promise<{status:'disabled'|'gated'|'failed'|'ok', block?:string, notice?:string|null,
 *                    error?:string, plan?:object, ms?:number, model?:string, why?:string}>}
 */
export async function orchestrateTurn({
  ctx, payload, decision, query, parsed, isSubagent, callModel, fallbackModel,
  resolveRoute, sessionKey = '', baseDir = BASE,
}) {
  // ★ 告警/失败计数**按会话隔离**（旧版是进程级共享 ⇒ 会在无关会话里替别的会话报故障）
  const key = String(sessionKey || payload?.agent?.id || '<unknown>')
  try {
    const cfg = loadConfig(baseDir)
    if (!cfg.enabled) return { status: 'disabled' }

    // ★ 2026-10-04：**必须把时间戳带进去** ——
    //   起因（用户点名）：编排器注入了一条「重启后实测 read_image 失败，未声明图像输入」，
    //   但那条**当天已经修好了** ⇒ 它把**已过期的历史事实当成现况**喂进上下文。
    //   根因：hits 里本来就有 `time_start`（毫秒时间戳），但这里 **map 时被丢掉了**，
    //        渲染时无从判断"这是多久以前的事" ⇒ 无法提示过期。
    //   ⇒ 补上 timeStart/timeEnd，交给 renderBlock 标"距今多久 + 需 reopen 复核"。
    const candidates = (parsed?.hits || []).map((hit) => ({
      text: hit.text || hit.snippet || '',
      cite: hit.cite || citeOf(hit),
      session_id: hit.session_id,
      score: hit.score,
      timeStart: hit.time_start ?? hit.timeStart ?? null,
      timeEnd: hit.time_end ?? hit.timeEnd ?? null,
      turn: hit.turn ?? null,
      // ★ 第八轮：把"与 query 的字面重合度"一起给判官 —— 它据此识别**回声候选**
      //   （只把 query 又抄一遍、没有任何具体内容的候选）。
      echo: echoRatio(hit.text || hit.snippet || '', query),
    })).filter((c) => c.text)

    const gate = shouldOrchestrate({
      cfg, turn: Number(payload?.turn ?? 0),
      userText: query, candidates, isSubagent,
    })
    if (!gate.run) return { status: 'gated', why: gate.why }

    let rm = resolveModel({ cfg, payload, fallback: fallbackModel })
    // ★ P2-b（2026-10-15）：**在调用之前**把真路由定下来。
    //   病根：真路由是 callModel 内部兜底出来的（resolveRoute），结果没回填给 rm
    //        ⇒ 注入块头部写 `model="?"`、台账写 `"model":""`
    //        （实测：372 个真注入块里 319 个 = 85.8% 是 `?`）。
    //   修法：调用方把 resolveRoute 传进来，这里先解析、解析结果同时用于**调用、渲染、留痕**。
    if ((!rm.provider || !rm.model) && typeof resolveRoute === 'function') {
      try {
        const r2 = await resolveRoute()
        if (r2 && r2.provider && r2.model) rm = { provider: String(r2.provider), model: String(r2.model) }
      } catch { /* 保持原值，交给 callModel 自己兜 */ }
    }

    // 最近几轮（从 decision.messages 里取，天然是"最近"）
    const recentTurns = (decision?.messages || [])
      .filter((m) => m && (m.role === 'user' || m.role === 'assistant'))
      .slice(-(cfg.recentTurns * 2))
      .map((m) => ({ role: m.role, text: extractText(m) }))
      .filter((m) => m.text)

    const r = await orchestrate({
      cfg, userText: query, recentTurns, candidates,
      taskState: '',
      callModel, resolvedProvider: rm.provider, resolvedModel: rm.model,
      baseDir,
      sessionKey: key,     // ★ 2026-10-15(3)：台账须能答"是哪个会话出的问题"
    })
    // 失败 ⇒ 回落（但要**可见**）：告警文本在这里就生成好，随四态一起交回调用方
    if (!r.ok) return { status: 'failed', error: String(r.error ?? ''), notice: noteFailure(key, r.error) }
    noteSuccess(key)
    // ★ 2026-10-15（第二轮）：**编排器空手 ⇒ 不出块**（status='empty'）。
    //   病根：旧版在 core.js 里有 `R2 兜底` —— 代码把**检索最高分的那一条冒充成
    //   「必须常驻的事实」**注入（实证 2026-10-15T10:32Z 那条：用户问"重启重试测验"，
    //   注入的却是"stream idle timeout"的助手思考片段，且带着最强断言语气）。
    //   现在：交回 'empty'，由调用方**回落成普通召回**（老路块自带 score/出处 与
    //   "别当常识、要用就引 session/turn" 的提醒，不冒充"必须常驻"）。
    //   empty **不是失败** ⇒ 不发告警、不计失败。
    if (r.outcome === 'empty' || !r.block) {
      return {
        status: 'empty', plan: r.plan, ms: r.ms, model: rm.model || r.model,
        why: 'orchestrator-empty', gate: gate.why,
      }
    }
    return {
      status: 'ok', block: r.block, plan: r.plan, ms: r.ms,
      model: rm.model || r.model, why: gate.why,
    }
  } catch (e) {
    const msg = e?.message ?? String(e)
    try { console.log('[orch-host] failed open:', msg) } catch { /* noop */ }
    return { status: 'failed', error: msg, notice: noteFailure(key, msg) }
  }
}

// ══════════════════════════════════════════════════════════════════════
// 失败可见（2026-10-04 新增；2026-10-15 改为按会话隔离 + 话术纠正）
// ──────────────────────────────────────────────────────────────────────
// 病根：编排器是 fail-open 的（失败不阻断对话），所以 2026-10-03 **一整天 92/92 全失败
//      却没有任何地方报红** —— 用户完全不知道"注意力功能等于没开"。
//       这正是用户反复强调的：「写了断言但不 raise ＝ 没有断言」。
//
// ★ 2026-10-15 两处纠正（都是实测出来的"不正确的提示"）：
//   ① **按会话隔离**：旧版 `_failState` 是模块级共享 ⇒ 甲会话的失败会在乙会话里弹告警
//      （实测告警出现在 11 个不同会话的原始记录里）。
//   ② **话术纠正**：旧版写"它当前**没有在给上下文注入记忆**" —— **不成立**：
//      编排失败后代码继续走 v0.1.5 老路**照旧注入 memory_recall 块**（lib/index.js 失败分支之后）。
//      正确说法是"**没有做编排级挑选，已回落成普通召回**"。
//   ③ 归因纠正：把上游 5xx / Connection error 报成"编排器坏了"会误导，
//      告警里明确点出"若是 5xx/连接错误，多半是上游线路故障"。
//   ④ 只有**真失败**才发告警：`disabled` / `gated` 两个状态不再触发（见 orchestrateTurn 四态）。
// ══════════════════════════════════════════════════════════════════════
const _failState = new Map()        // sessionKey -> {streak, alertedAt, total, lastErr}
const _FAIL_MAX_SESSIONS = 500      // 有界：别把内存当台账
const FAIL_ALERT_AFTER = 3          // 连续失败几次开始提示
const FAIL_ALERT_COOLDOWN_MS = 30 * 60 * 1000   // 提示后 30 分钟不再重复

function _fail(key = '') {
  const k = String(key || '<unknown>')
  let s = _failState.get(k)
  if (!s) {
    s = { key: k, streak: 0, alertedAt: 0, total: 0, lastErr: '' }
    if (_failState.size >= _FAIL_MAX_SESSIONS) {
      const first = _failState.keys().next().value
      if (first !== undefined) _failState.delete(first)
    }
    _failState.set(k, s)
  }
  return s
}

function _noticeFor(s) {
  if (!s || s.streak < FAIL_ALERT_AFTER) return null
  const now = Date.now()
  if (now - s.alertedAt < FAIL_ALERT_COOLDOWN_MS) return null
  s.alertedAt = now
  return [
    '<memory-orchestrator-alert>',
    `⚠️ **注意力编排器连续失败 ${s.streak} 次**（本会话累计 ${s.total} 次）——`,
    '它本轮**没有做编排级挑选**，已回落成普通召回（记忆仍会注入，只是没经编排器挑）。',
    `最后一次错误：${s.lastErr || '(未知)'}`,
    '若错误是 5xx / `Connection error` / `empty stream`，多半是**上游线路故障**，不是编排器坏了。',
    '排查：`python D:\\agent\\dsh-rag-memory\\bin\\mem.py orchestrate-status`',
    '</memory-orchestrator-alert>',
  ].join('\n')
}

/** 记一次失败；返回**该发的告警文本**（不到阈值/冷却中 ⇒ null）。 */
function noteFailure(key, err) {
  try {
    const s = _fail(key)
    s.streak += 1
    s.total += 1
    s.lastErr = String(err ?? '')
    return _noticeFor(s)
  } catch { return null }
}

function noteSuccess(key) {
  try { const s = _fail(key); s.streak = 0; s.lastErr = '' } catch { /* noop */ }
}

/**
 * 该不该在对话里提示"注意力功能坏了"？（**按会话**判）
 * 连续失败 ≥ FAIL_ALERT_AFTER 且距上次提示超过冷却期 ⇒ 返回提示文本，否则 null。
 */
export function failureNotice(key = '') {
  try { return _noticeFor(_failState.get(String(key || '<unknown>'))) } catch { return null }
}

export function failureStats(key = '') {
  try {
    if (key) return { ..._fail(key) }
    const out = { sessions: {} }
    for (const [k, s] of _failState.entries()) out.sessions[k] = { ...s }
    return out
  } catch { return {} }
}

export function citeOf(hit) {
  try {
    const sid = hit.session_id || hit.session?.session_id || ''
    const turn = hit.turn ?? hit.session?.turn ?? ''
    if (sid && turn !== '') return `dsh:${String(sid).slice(0, 12)}#turn=${turn}`
    if (sid) return `dsh:${String(sid).slice(0, 12)}`
    return ''
  } catch { return '' }
}

function extractText(m) {
  try {
    if (typeof m.content === 'string') return m.content
    if (Array.isArray(m.content)) return m.content.map((c) => c?.text || '').join(' ').trim()
    return ''
  } catch { return '' }
}

/**
 * 注册 HTTP 路由（面板读取/写入配置）。
 * 路由挂在 /dsh-memory/orch/* 下，与既有 /dsh-memory/health 不冲突。
 *
 * 注意：DSH webServer 的 handler 是 **Node 原生签名 (req, res)**，
 * 且需要 `kind: 'exact'`。这里统一按该约定书写，并把业务逻辑与收发分离，
 * 便于单测（单测里直接调 handler 会因为没有真 res 而走 _json 分支）。
 */
export function registerRoutes(webCtx, baseDir = BASE) {
  const send = (res, obj, status = 200) => {
    if (res && typeof res.writeHead === 'function') {
      res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
      res.end(JSON.stringify(obj))
    }
    return { status, body: JSON.stringify(obj) }
  }

  const readBody = async (req) => {
    // 预解析好的 body 优先（单测/内部调用直接给）
    if (req && req._orchBody !== undefined) return req._orchBody
    if (!req || typeof req.on !== 'function') return {}
    const chunks = []
    await new Promise((resolve) => {
      let done = false
      const fin = () => { if (!done) { done = true; resolve() } }
      req.on('data', (c) => chunks.push(c))
      req.on('end', fin)
      req.on('error', fin)
      setTimeout(fin, 3000)
    })
    try { return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}') } catch { return {} }
  }

  const routeList = [
    { path: '/dsh-memory/orch/stats', method: 'GET', fn: () => json(stats(baseDir)) },
    {
      // ★ 2026-10-03：GET 版配置读写。POST 的 body 在 DSH 的 exact 路由里读不到
      //   （实测写不进去 ⇒ 点开关"勾上又跳回去"）。query 参数不受此影响。
      //   带参数 ⇒ 写入；不带 ⇒ 纯读。
      path: '/dsh-memory/orch/config', method: 'GET',
      fn: (req) => {
        const changed = {}
        try {
          const u = new URL(req?.url || '/', 'http://x')
          const allowed = ['enabled', 'modelMode', 'customProvider', 'customModel', 'everyNthTurn']
          const patch = {}
          for (const [k, v] of u.searchParams.entries()) {
            if (!allowed.includes(k)) continue
            if (k === 'enabled') patch[k] = (v === 'true' || v === '1')
            else if (k === 'everyNthTurn') patch[k] = Number(v)
            else patch[k] = v
          }
          if (Object.keys(patch).length > 0) { saveConfig(baseDir, patch); Object.assign(changed, patch) }
        } catch { /* 读不到就纯读 */ }
        return json({ ok: true, saved: changed, ...stats(baseDir), ...loadConfig(baseDir) })
      },
    },
    { path: '/dsh-memory/orch/models', method: 'GET', fn: () => json({ models: listModels() }) },
    {
      path: '/dsh-memory/orch/decisions', method: 'GET',
      fn: (req) => {
        let n = 20
        try {
          const u = new URL(req?.url || '/', 'http://x')
          n = Math.min(100, Math.max(1, Number(u.searchParams.get('n') ?? 20)))
        } catch { /* 默认 20 */ }
        return json({ decisions: recentDecisions(baseDir, n) })
      },
    },
    {
      path: '/dsh-memory/orch/config', method: 'POST',
      fn: async (req) => {
        // 双通道：body 优先，读不到就退回 query
        let patch = await readBody(req)
        if (!patch || Object.keys(patch).length === 0) {
          try {
            const u = new URL(req?.url || '/', 'http://x')
            patch = {}
            for (const [k, v] of u.searchParams.entries()) patch[k] = v
            if (patch.enabled !== undefined) patch.enabled = (patch.enabled === 'true' || patch.enabled === '1')
            if (patch.everyNthTurn !== undefined) patch.everyNthTurn = Number(patch.everyNthTurn)
          } catch { patch = {} }
        }
        const allowed = ['enabled', 'modelMode', 'customProvider', 'customModel', 'everyNthTurn']
        const clean = {}
        for (const k of allowed) if (k in patch) clean[k] = patch[k]
        const next = saveConfig(baseDir, clean)
        // ★ 先落盘、再统计：stats() 必须在 saveConfig 之后调用，
        //   否则返回的是**变更前**的 enabled，前端会以为"没点上"。
        const st = stats(baseDir)
        return json({ ok: true, ...st, ...next, saved: clean })
      },
    },
    {
      // 诊断：DSH 的客户端模块系统眼里，本插件是什么样？
      // 用来定位"客户端面板不出现"这类问题，不必再猜。
      path: '/dsh-memory/orch/diag', method: 'GET',
      fn: () => {
        const out = { plugin: 'dsh-memory-plugin', checks: {} }
        try {
          const self = 'D:\\agent\\dsh-rag-memory\\plugin-src\\dsh-memory-plugin'
          const pkgPath = join(self, 'package.json')
          const raw = readFileSync(pkgPath, 'utf8')
          const pkg = JSON.parse(raw)
          out.checks.pkgFound = true
          out.checks.pkgPath = pkgPath
          out.checks.version = pkg.version
          out.checks.dshClient = pkg.dsh && pkg.dsh.client ? pkg.dsh.client : null
          out.checks.clientExport = pkg.exports ? pkg.exports['./client'] : null
          const rel = typeof pkg.exports?.['./client'] === 'string' ? pkg.exports['./client'] : null
          if (rel) {
            const abs = join(self, rel)
            out.checks.bundlePath = abs
            out.checks.bundleExists = existsSync(abs)
            if (existsSync(abs)) {
              const s = readFileSync(abs, 'utf8')
              out.checks.bundleBytes = s.length
              out.checks.bundleHead = s.slice(0, 60)
              out.checks.bundleLooksValid = s.startsWith('window.__ModuleLoader__.load(')
            }
          }
        } catch (e) {
          out.checks.error = String(e?.message ?? e)
        }
        return json(out)
      },
    },
  ]

  let okN = 0
  for (const r of routeList) {
    try {
      webCtx.webServer.register({
        kind: 'exact',
        path: r.path,
        handler: async (req, res) => {
          try {
            const out = await r.fn(req)
            return send(res, out)
          } catch (e) {
            return send(res, { ok: false, error: String(e?.message ?? e) }, 500)
          }
        },
      })
      okN += 1
    } catch { /* 单条失败不影响其它 */ }
  }
  return okN
}

function json(obj) { return obj }
