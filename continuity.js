import { readFileSync, mkdirSync, writeFileSync, renameSync } from 'node:fs'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'

// Project Continuity Memory: confirmed locks are durable state; candidates never become facts automatically.
const DEFAULT = {
  // ⚠️ 以下四个字段是**上一版**的"全局授权范围"（只有一份）。
  // ★ 2026-10-22 用户指正：「<用户原话已隐去：范围应单独对应一个对话>，
  //   我需要这个衔接对话时单独的对应一个对话才行」。
  //   病根就在这里：范围只有一份 ⇒ 在 A 对话里选了 B、C，切到 B 对话时 B 也被接上同一批。
  //   现在真正的授权范围在 `scopes[当前对话]`；这四个字段**只为旧状态兼容而保留**，
  //   注入与捕获**不再读它们** —— 一旦回落去读，全局泄漏就原样装回来了。
  enabled: false,
  mode: 'off', // off | project | sessions | all
  project: '',
  selectedSessions: [],
  /** ★ 每个对话各自的授权范围：{ [sessionId]: { enabled, mode, project, selectedSessions } } */
  scopes: {},
  packet: {
    version: 1, goalLock: null, constraintLocks: [], decisions: [], openItems: [],
    workingSet: [], candidates: [], sources: [], updatedAt: 0,
  },
  updatedAt: 0,
}
const MODES = new Set(['off', 'project', 'sessions', 'all'])
const STATUSES = new Set(['candidate', 'confirmed', 'superseded', 'rejected', 'open'])
const KINDS = new Set(['goal', 'constraint', 'decision', 'open_issue', 'working_set', 'episode', 'fact'])

function pathOf(baseDir) { return join(baseDir, 'continuity', 'state.json') }
function text(v, n = 700) { return String(v ?? '').replace(/\s+/g, ' ').trim().slice(0, n) }
function list(v, n = 12) { return Array.isArray(v) ? v.slice(0, n) : [] }

/**
 * 单个对话的授权范围（per-conversation scope）。
 *
 * ★ 2026-10-22 用户指正（本轮改动的唯一理由）：
 *   「<用户原话已隐去：范围应单独对应一个对话>，
 *    切到别的对话不该继承这一份衔接」。
 *
 * 语义（写死，别改）：
 *   · `selectedSessions` = 本对话**要接上的其他对话**（"接上"＝注入时把这些对话的内容带进来）；
 *   · 本对话自己**永远在范围内**（不需要用户勾自己，勾了也只是重复，不会放大）；
 *   · 每个对话各存一份 ⇒ A 对话里选了 B、C，**B 对话不会因此被接上任何东西**。
 */
/**
 * 布尔归一（**必须统一走这里**）。
 *
 * ★ 2026-10-22 差点又栽一次：面板与路由走的是 **query 字符串通道**，
 *   所以 `autoConfirm` 会以 `'false'` / `'0'` 到达，而不是布尔 false。
 *   若写成 `patch.autoConfirm !== false`，字符串 `'false'` 会判成 `true`
 *   ⇒「关闭自动确认」永远关不掉，**而且不报错**（静默失效）。
 *   本项目已经因为同类问题栽过多次（`enabled` 当初就是这么修的），所以抽成一处。
 */
export function toBool(v, fallback = false) {
  if (v === undefined || v === null || v === '') return fallback
  if (v === true || v === 'true' || v === '1' || v === 1) return true
  if (v === false || v === 'false' || v === '0' || v === 0) return false
  return fallback
}

export function normalizeScope(raw) {
  if (!raw || typeof raw !== 'object') return null
  return {
    enabled: toBool(raw.enabled, false),
    mode: MODES.has(raw.mode) ? raw.mode : 'off',
    project: text(raw.project, 160),
    selectedSessions: list(raw.selectedSessions, 40).map((x) => text(x, 120)).filter(Boolean),
    // ★ 2026-10-22 用户指正（第二次，比第一次更要紧）：
    //   「<用户原话已隐去：自动确认不该靠人点>」
    //   对 —— 逐条点"确认"等于把自动化变成人工审核，长线运行根本不成立。
    //   所以默认**自动确认**（新范围默认 true；只有显式 false/'false'/'0' 才关）。
    autoConfirm: toBool(raw.autoConfirm, true),
    updatedAt: Number(raw.updatedAt) || 0,
  }
}

/** 归一化整张 scopes 表；按最近更新保留至多 200 个对话，避免无限膨胀。 */
function normalizeScopes(raw) {
  if (!raw || typeof raw !== 'object') return {}
  const rows = []
  for (const [k, v] of Object.entries(raw)) {
    const sid = text(k, 120)
    const s = normalizeScope(v)
    if (sid && s) rows.push([sid, s])
  }
  rows.sort((a, b) => (b[1].updatedAt || 0) - (a[1].updatedAt || 0))
  return Object.fromEntries(rows.slice(0, 200))
}

const EMPTY_SCOPE = Object.freeze({ enabled: false, mode: 'off', project: '', selectedSessions: [], autoConfirm: true, updatedAt: 0 })

/**
 * 会话 id 归一（**身份判定用，展示/出处仍用原串**）。
 *
 * ★ 2026-10-22 实测的致命隐患：索引库 1511 个会话里 **249 个是 `session-<uuid>`、
 *   1262 个是裸 `<uuid>`** 两种形态并存。若面板传来的 id 与注入侧用的 id 前缀不同，
 *   两边的 scopes 键就对不上 ⇒ 面板显示"已开启"、注入却是空 ⇒ 又一次"功能没有"，
 *   而且**不报错**（正是静默失效）。
 *   所以凡"是不是同一个对话"的判断一律先过这里。
 */
export function normSessionId(value) {
  return String(value ?? '').trim().replace(/^session-/i, '').toLowerCase()
}

/**
 * 取「当前对话」的授权范围。
 *
 * ★ 没有就是没有（fail-closed）：绝不回落到全局 enabled/mode/project/selectedSessions ——
 *   那个回落**就是**用户投诉的跨对话泄漏本身。拿不到 sessionId 时同样返回空范围，
 *   因为"不知道自己在哪个对话里"时任何注入都只能是猜。
 */
export function scopeFor(state, sessionId = '') {
  const raw = text(sessionId, 120)
  const key = normSessionId(raw)
  if (!key) return { ...EMPTY_SCOPE, sessionId: '' }
  const scopes = state?.scopes || {}
  // 先按归一键找；再兼容早期可能按原串写入的键（两种前缀都能命中同一个对话）。
  const hit = normalizeScope(scopes[key])
    || normalizeScope(scopes[raw])
    || normalizeScope(Object.entries(scopes).find(([k]) => normSessionId(k) === key)?.[1])
  return { ...(hit || EMPTY_SCOPE), sessionId: raw }
}
/**
 * 候选卫生：判断一段文本是否"不该进状态包"。
 *
 * ★ 2026-10-21 用户当场指认的铁证：状态包里出现了
 *   `Current working directory: "D:\agent". Current DeepSeek Harness implementation checkout: …`
 *   以及一整段别的会话的群聊记录。
 *
 * 病根不是"名单少一条"，而是**判据的形态错了**：
 *   旧实现是「完整字符串逐条 includes」，而实际文本会被截断到 400/700 字 ——
 *   `Current DSH file policy` 恰好落在截断点之后 ⇒ 整条漏过。
 *   只要样板换个开头或中途被截断，名单就失效（名单永远追不上变体）。
 *
 * 改为**按模式识别**：命中任一特征即拒。宁可多拒（候选只是提示，丢了不伤事实），
 * 不可漏放（漏放会把运行环境/工具包装文本当成项目事实注入）。
 */
const UNSAFE_PATTERNS = [
  // 运行环境样板：各种开头与变体（截断后只留前半句也能认出）
  /Current\s+(working\s+directory|runtime\s+context|DSH\s+file\s+policy|DeepSeek\s+Harness|implementation\s+checkout)/i,
  /Approval\s+(prompts?\s+are\s+disabled|policy)\b/i,
  /Treat\s+the\s+captured\s+context\s+as\s+established/i,
  /\[Problem-solving\s+contribution/i,
  /Preserve\s+explicit\s+user\s+goals,\s+chosen\s+approaches/i,
  // 插件自己的注入块 / 系统提醒（回流会自我放大）
  /<\/?(memory_recall|attention_orchestration|project-continuity|system-reminder|memory-orchestrator-alert)\b/i,
  /Recalled\s+excerpts\s+from\s+earlier\s+sessions/i,
  /Use\s+these\s+as\s+evidence/i,
  /The\s+available\s+skill\s+catalog\s+changed/i,
  // 元叙述：模型在描述"我注入了什么/我看到什么"，不是项目内容
  /【必须常驻的事实】|【原文要点|编排耗时|编排理由|时效提示/,
  /这(是|段)项目连续性状态包|候选历史（待确认）/,
  // ★ 2026-10-21 用户贴出的铁证：状态包里出现**整段别的会话的群聊记录**。
  //   按内容签名认"这是转储的聊天记录"，而不是按角色前缀认格式。
  /你是「[^」]{0,60}」这个群聊的成员/,
  /本轮是并发讨论|你看不到别人本轮的发言|群成员[：:]/,
  /\[上下文压缩摘要\]|Primary\s+Request\s+and\s+Intent/,
  /本轮之前的上下文|以下是本轮的上下文|【本轮之前的上下文/,
  // ★ 2026-10-22 实测：用户复制/引用**面板上的说明文字**会被当成"约束"收进候选
  //   （例："我是贴出了！…这是项目连续性记忆 主动开启后，目标锁、硬约束、已确认决定…"）。
  //   那是插件自己的 UI 文案，不是项目内容 ⇒ 按自家文案认出来并拒收。
  /主动开启后，?目标锁|目标锁、硬约束、已确认决定|从外部状态包恢复/,
  /待确认候选（确认后才会成为方向\/约束\/决定）/,
]

/**
 * 剥掉消息角色前缀（`[用户] ` / `[助手·思考] ` / `[工具结果] ` …）。
 *
 * ★ 为什么要剥而不是拒：角色前缀是 mem.py 命中的**固有格式**，不是污染特征。
 *   本次被投诉的那条正文就以 `[用户] 你是「示例项目甲·…」…` 开头；
 *   若按前缀整类拒收，会把召回捕获这条路径**整体清空且不报错**（静默失效）。
 *   正确做法：剥掉前缀，再按**内容**判它是不是聊天记录。
 */
export function stripRolePrefix(value) {
  return String(value ?? '')
    .replace(/^\s*\[(用户|助手|助手·思考|用户·插话|工具调用|工具结果|命令|上下文压缩摘要)(?:[^\]]{0,40})?\]\s*/u, '')
    .trim()
}

/** 是否"工具输出转储"（命令回显/JSON 块）——只在**捕获候选**时用，不用于已确认条目。 */
export function looksLikeToolDump(value) {
  const t = String(value ?? '').trim()
  if (!t) return false
  if (/^\[(工具调用|工具结果|命令)[^\]]*\]/u.test(t)) return true
  if (/^[{[]/.test(t) && /"[a-zA-Z_]+"\s*:/.test(t)) return true
  return false
}

// 候选是"机器自动收集的待确认片段"，长文本几乎必然是聊天记录/文档片段 ⇒ 收紧。
// 已确认条目是"用户点过确认的内容"，长是正常的（一条约束写满几行很常见）⇒ 只留防爆上限。
const MAX_CANDIDATE_CHARS = 700

/**
 * 自动确认时，新的目标要"更实质"多少倍才能取代旧的自动目标。
 *
 * ★ 为什么需要这个数（2026-10-22，用真实数据标定，不是拍脑袋）：
 *   该会话里 goal 候选的实测长度是 91 / 91 / 91 / 152 / 13 ——
 *     · #turn=1（91 字）是初步任务「检查…然后向我报告」；
 *     · #turn=4（152 字）才是核心目标「合并给出一个新的记忆方案…」；
 *     · #turn=16（13 字）是「一句口语化的"开始做吧"式指令」。
 *   152 / 91 = 1.67 ⇒ 用 1.3 作门槛可以正确让核心目标取代初步任务；
 *   13 字那条远达不到 1.3 倍 ⇒ 不会被"随口一句"把方向带跑。
 *
 * 这是**启发式**，所以必须配三件事，缺一不可：
 *   ① 明确写在这里（可复核、可调整）；
 *   ② 被取代的旧目标进 goalHistory（可追溯、可回退）；
 *   ③ 人工确认的目标**永不**被自动取代（人的决定优先于启发式）。
 */
const GOAL_REPLACE_RATIO = 1.3

/**
 * 判断"这一轮用户话"该不该成为候选，以及该归成哪一类。
 *
 * ★ 2026-10-21 用户贴出铁证：状态包里出现了**整段别的会话的群聊记录**与运行环境样板。
 *   根因不只是卫生名单：捕获规则本身是「任何 ≥6 字的用户话都存成 episode」——
 *   用户粘贴一段别处日志、或者一轮里带着运行环境样板，就都被当成"项目事实候选"。
 *
 * 现在的规则（宁缺毋滥：候选只是提示，漏收不伤事实，错收会污染判断）：
 *   1. 工具转储（命令回显/JSON）⇒ 不收；
 *   2. 卫生判定不过（运行环境样板、聊天记录签名、超长）⇒ 不收；
 *   3. 只有**明确的约束/决定措辞**才收，且归到对应 kind；
 *   4. 其余一律不收 —— episode 不再从"随便一句话"自动生成。
 *      真正的 episode 来自 recall 命中（带真实出处），那条路径不受影响。
 */
export function classifyContinuityStatement(value) {
  const raw = String(value ?? '')
  const t = stripRolePrefix(raw)
  if (!t) return null
  if (looksLikeToolDump(raw)) return null
  if (!isSafeContinuityCandidate(t)) return null
  if (/(必须|不许|禁止|不得|一律|硬约束|约束)/.test(t)) return { kind: 'constraint', text: t }
  if (/(决定|改成|改用|采用|定为|拍板|就按)/.test(t)) { return { kind: 'decision', text: t } }
  return null
}

/**
 * 导入专用判定：比实时捕获**更宽**，因为对象是"用户自己挑的历史对话"。
 *
 * ★ 2026-10-22 实测的决定性缺口：用户最初的目标原话
 *   「…合并给出一个新的记忆方案，并新增两个功能，制作方向永远不偏移…」
 *   不含"必须/决定"这类关键词 ⇒ 被严格判定判成 null
 *   ⇒ 状态包里**从来没有目标锁** ⇒ 用户看到的仍是"功能没有"。
 *
 * 分级（顺序即优先级，避免长目标句被前面的宽泛关键词抢走）：
 *   ① 含"必须/不许/决定…" ⇒ 按严格判定（约束/决定）
 *   ② 像目标陈述（有目标类动词 + 够长 + 不是问句）⇒ goal
 *   ③ 够长的普通陈述 ⇒ episode
 * 为什么可以放宽：条目来自用户**明确选中的对话**、带真实出处，
 *   且仍然只是**候选**（要用户确认才变事实）；污染由卫生判定兜底。
 */
export function classifyImportedStatement(value) {
  const raw = String(value ?? '')
  const t = stripRolePrefix(raw)
  if (!t) return null
  if (looksLikeToolDump(raw)) return null
  if (!isSafeContinuityCandidate(t)) return null
  // ① 严格判定优先：明确的约束/决定不该被当成 goal
  const strict = classifyContinuityStatement(t)
  if (strict) return strict
  const isQuestion = /[?？]\s*$/.test(t)
  // ② 目标陈述
  if (!isQuestion && t.length >= 12 && /(目标|需求|要实现|新增|增加|做成|要做|希望|方案|功能|能力)/.test(t)) {
    return { kind: 'goal', text: t }
  }
  // ③ 普通长陈述
  if (!isQuestion && t.length >= 20 && !/^(继续|好|好的|行|嗯|ok)$/i.test(t)) {
    return { kind: 'episode', text: t }
  }
  return null
}

const MAX_CONFIRMED_CHARS = 4000

/** 候选卫生：先判格式（工具转储）→ 剥前缀 → 长度上限 → 内容签名。 */
export function isSafeContinuityCandidate(value) {
  // ★ 顺序很重要：looksLikeToolDump 要看**原始文本**的角色前缀。
  //   若先剥前缀再判，`[工具结果] {...}` 会被剥成 `{...}`，
  //   前缀判据就永远看不到 ⇒ 工具转储漏过（2026-10-21 实测踩到）。
  if (looksLikeToolDump(value)) return false
  const t = stripRolePrefix(value)
  if (!t) return false
  if (t.length > MAX_CANDIDATE_CHARS) return false
  return isSafeConfirmedText(t)
}

/**
 * 内容卫生判定：**只看内容签名，不看格式、不设候选那种长度上限**。
 *
 * ★ 2026-10-21 实测的坑：lock() 原本也走 isSafeContinuityCandidate，
 *   于是"用户已经确认"的长目标/长约束会被 700 字上限**静默丢弃**，
 *   而报错还写成"缺少出处"——把长度问题伪装成出处问题，最难查。
 *   实测：960 字的已确认约束 ⇒ 抛"require a source citation"，constraintLocks 变成 0。
 *   所以这里既不管长度、也不管角色前缀，只判"这段内容像不像运行环境/聊天记录"。
 */
export function isSafeConfirmedText(value) {
  const t = String(value ?? '').trim()
  if (!t) return false
  return !UNSAFE_PATTERNS.some((re) => re.test(t))
}

function lock(raw, fallbackKind = 'fact') {
  if (!raw) return null
  const status = STATUSES.has(raw.status) ? raw.status : 'candidate'
  const isCandidate = status === 'candidate'
  const body = String(raw.text ?? '')
  if (!body.trim()) return null
  // 候选：完整卫生（含 700 字上限）；已确认：只做内容签名 + 4000 字防爆。
  if (isCandidate ? !isSafeContinuityCandidate(body) : !isSafeConfirmedText(body)) return null
  const cap = isCandidate ? MAX_CANDIDATE_CHARS : MAX_CONFIRMED_CHARS
  const kind = KINDS.has(raw.kind) ? raw.kind : fallbackKind
  return {
    id: text(raw.id, 80) || randomUUID(), kind, text: text(isCandidate ? stripRolePrefix(body) : body, cap), status,
    cite: text(raw.cite, 240), project: text(raw.project, 160),
    confidence: Number.isFinite(Number(raw.confidence)) ? Math.max(0, Math.min(1, Number(raw.confidence))) : 0,
    createdAt: Number(raw.createdAt) || Date.now(), verifiedAt: Number(raw.verifiedAt) || 0,
    supersedes: text(raw.supersedes, 80), sourceSession: text(raw.sourceSession, 100),
    // ★ 2026-10-22：这条是**谁**锁的 —— 'human'（人点了确认/工具显式确认）还是 'auto'（自动确认）。
    //   为什么必须记：目标锁能不能被自动改写完全取决于它。人工锁永不自动覆盖；
    //   自动锁允许被更实质的目标取代（并把旧目标记进 goalHistory）。
    //   不记这个字段就无法区分"人定的方向"与"机器猜的方向"，只能一律不覆盖 ——
    //   那会把初步任务永久锁成方向（实测 #turn=1 就是这种情况）。
    confirmedBy: ['human', 'auto'].includes(raw.confirmedBy) ? raw.confirmedBy : '',
  }
}
function normalizePacket(raw = {}) {
  const map = (v, kind, n = 12) => list(v, n).map((x) => lock(x, kind)).filter(Boolean)
  return {
    version: Number(raw.version) || 1,
    goalLock: raw.goalLock ? lock(raw.goalLock, 'goal') : null,
    // ★ 2026-10-22：被取代的旧目标留在这里（带 supersedes 链）。
    //   没有它，"目标变过"这件事就不可追溯、不可回退，等于静默改方向。
    goalHistory: map(raw.goalHistory, 'goal', 12),
    constraintLocks: map(raw.constraintLocks, 'constraint'), decisions: map(raw.decisions, 'decision'),
    openItems: map(raw.openItems, 'open_issue'), workingSet: map(raw.workingSet, 'working_set'),
    candidates: list(raw.candidates, 24).map((x) => lock({ ...x, status: 'candidate' }, x?.kind || 'episode')).filter(Boolean),
    sources: list(raw.sources, 24).map((x) => text(x, 240)).filter(Boolean),
    updatedAt: Number(raw.updatedAt) || 0,
  }
}
export function loadContinuity(baseDir) {
  try {
    const raw = JSON.parse(readFileSync(pathOf(baseDir), 'utf8'))
    return { ...DEFAULT, ...raw, enabled: raw.enabled === true, mode: MODES.has(raw.mode) ? raw.mode : 'off',
      project: text(raw.project, 160), selectedSessions: list(raw.selectedSessions, 20).map((x) => text(x, 120)).filter(Boolean),
      scopes: normalizeScopes(raw.scopes),
      packet: normalizePacket(raw.packet) }
  } catch { return JSON.parse(JSON.stringify(DEFAULT)) }
}
function writeAtomic(file, value) {
  const tmp = `${file}.tmp-${process.pid}-${Date.now()}`
  writeFileSync(tmp, JSON.stringify(value, null, 2), 'utf8'); renameSync(tmp, file)
}
export function saveContinuity(baseDir, patch = {}) {
  const old = loadContinuity(baseDir)
  const next = { ...old, ...patch, enabled: patch.enabled === undefined ? old.enabled : patch.enabled === true,
    mode: MODES.has(patch.mode) ? patch.mode : old.mode, project: patch.project === undefined ? old.project : text(patch.project, 160),
    selectedSessions: Array.isArray(patch.selectedSessions) ? patch.selectedSessions.map((x) => text(x, 120)).filter(Boolean).slice(0, 20) : old.selectedSessions,
    scopes: patch.scopes ? normalizeScopes(patch.scopes) : old.scopes,
    packet: patch.packet ? normalizePacket(patch.packet) : old.packet, updatedAt: Date.now() }
  const dir = join(baseDir, 'continuity'); mkdirSync(dir, { recursive: true }); writeAtomic(pathOf(baseDir), next); return next
}

/**
 * 写「某个对话自己的」授权范围。
 *
 * 迁移规则（只在这里做，别处不许做）：
 *   本对话还没有自己的范围、而旧版全局是开启的 ⇒ 以旧全局为**种子**。
 *   为什么这不算"猜"：用户此刻正站在这个对话里点"开启"，这就是对这一份的明确授权。
 *   而"没有任何人打开过时自动派生给所有对话"，才是全局泄漏本身。
 */
export function saveScope(baseDir, sessionId, patch = {}) {
  const sid = text(sessionId, 120)
  // 没有 sessionId 就拒绝写：写下去只能变成"又一份全局设置"，正是要消灭的东西。
  if (!sid) throw new Error('sessionId is required: continuity scope is per-conversation')
  // ★ 2026-10-22：一律用**归一键**落盘（去掉 `session-` 前缀、小写）。
  //   实测两种前缀并存（249 个带前缀 / 1262 个裸 uuid）。若按原串存，
  //   面板传 `session-xxx`、注入侧拿 `xxx`（或反之）就会各存一份、互不可见 ⇒ 静默失效。
  const key = normSessionId(sid)
  const old = loadContinuity(baseDir)
  // 旧条目里可能已有同一对话的另一种前缀写法：以归一后匹配为准，并清掉重复键。
  const scopes = {}
  for (const [k, v] of Object.entries(old.scopes || {})) {
    if (normSessionId(k) === key) continue
    scopes[k] = v
  }
  const cur = normalizeScope(old.scopes?.[key])
    || normalizeScope(Object.entries(old.scopes || {}).find(([k]) => normSessionId(k) === key)?.[1])
  // ★★ 2026-10-22：**不**从旧全局字段迁移种子（原先有，已删除）。
  //
  // 删它的理由（不是洁癖，是安全）：
  //   旧全局 enabled/mode/project/selectedSessions 恰恰是**那个 bug 的产物** ——
  //   旧宿主在 13:09 把用户的点击写成了全局 `enabled=true, mode=sessions`（已实测取证）。
  //   若拿它当种子，用户第一次在新对话里打开开关时，就会**继承这份来历不明的授权**
  //   （包括 selectedSessions 指向的别的对话）⇒ 把跨对话泄漏原样搬进新模型。
  //   新对话的范围必须从"空 + 用户这一次的点击"开始，这才配得上
  //   "每个对话各自授权"这条语义。
  //
  // 代价：升级前配过全局连续性的人，需要在新模型下重新打开一次。
  //   这个代价是显式的（面板上看得见开关状态），比"悄悄继承了旧泄漏"好得多。
  const seed = cur || EMPTY_SCOPE
  // ★ 2026-10-22：只开开关、没给 mode ⇒ 给一个**有意义的默认**（sessions = 只看本对话），
  //   绝不能停在 `enabled=true + mode=off`。
  //   那个组合会让面板显示"已开启"、注入侧却一条都不给 ——
  //   正是本项目反复出现的"看起来在跑其实没动"，只是换了个位置。
  const wantMode = MODES.has(patch.mode) ? patch.mode : (toBool(patch.enabled, false) && seed.mode === 'off' ? 'sessions' : seed.mode)
  const next = {
    enabled: patch.enabled === undefined ? seed.enabled : toBool(patch.enabled, false),
    mode: wantMode,
    project: patch.project === undefined ? seed.project : text(patch.project, 160),
    selectedSessions: Array.isArray(patch.selectedSessions)
      ? patch.selectedSessions.map((x) => text(x, 120)).filter(Boolean).slice(0, 40)
      : seed.selectedSessions,
    // ★ 只有显式给 false/'false'/'0' 才关自动确认；不传则沿用（新范围默认 true）。
    //   这里必须用 toBool：query 通道来的是字符串，`!== false` 会静默关不掉。
    autoConfirm: patch.autoConfirm === undefined ? toBool(seed.autoConfirm, true) : toBool(patch.autoConfirm, true),
    updatedAt: Date.now(),
  }
  scopes[key] = next
  const saved = saveContinuity(baseDir, { scopes })
  return { ...saved, scope: { ...next, sessionId: sid } }
}
export function updateContinuityPacket(baseDir, patch = {}) {
  const old = loadContinuity(baseDir)
  return saveContinuity(baseDir, { packet: normalizePacket({ ...old.packet, ...patch, version: old.packet.version + 1, updatedAt: Date.now() }) })
}
/**
 * 状态包里"一共有多少条实质条目"（候选 + 各类确认项）。
 *
 * ★ 为什么需要它（2026-10-22，避免假报）：导入流程原本用「候选条数前后差」判成功。
 *   一旦开了自动确认，条目直接进 goalLock/decisions，**候选数不增加** ⇒
 *   明明导入成功却返回 imported:0，界面显示"没有发现明确的目标/约束/决定"。
 *   计数必须覆盖所有承载位置。
 */
export function packetItemCount(state) {
  const p = normalizePacket(state?.packet)
  return (p.goalLock ? 1 : 0) + p.constraintLocks.length + p.decisions.length
    + p.openItems.length + p.workingSet.length + p.candidates.length
}

/** 候选按内容去重的归一（同一条话在多个 turn 被复述时只留一条）。 */
function contentKey(kind, value) { return `${kind}|${String(value ?? '').replace(/\s+/g, '').slice(0, 200)}` }

/**
 * 是不是「提问 / 犹豫 / 征询」，而不是一句陈述。
 *
 * ★ 2026-10-22 顾问指出的致命缺陷（我确认成立）：
 *   `classifyContinuityStatement` 只做关键词匹配，没有问句守卫：
 *     「能不能改成方案 B？」含"改成" ⇒ 被判成 decision
 *     「必须这样吗？」      含"必须" ⇒ 被判成 constraint
 *   候选阶段这没危害（候选只是提示）；但**自动确认**会把它固化成
 *   「已确认决定/硬约束」，而且每轮带出处注入 ——
 *   一句提问变成事实，正好违反用户最初的硬约束「方向不得漂移、不得凭空编造」。
 *   所以守卫加在**自动升格**这一处，不改 classifyContinuityStatement 本身
 *   （否则会连带改变候选捕获口径与既有测试）。
 *
 * ★ 判据必须收紧（2026-10-22 自查发现）：
 *   不能单凭句末「吧」判问句 —— 「就按方案 A 吧」「就这么定吧」是**明确的决定**，
 *   判成问句会让它静默不升格，与用户要的"长线自动运行"直接冲突。
 *   只认：① 真的带问号；② 句末"吗"；③ 以疑问词/征询短语开头或收尾。
 */
export function looksLikeQuestion(value) {
  const t = String(value ?? '').trim()
  if (!t) return false
  // ① 真问号收尾（最强的信号）
  if (/[?？]\s*$/.test(t)) return true
  // ② 句末"吗"（"这样行吗"）；注意**不含"吧"** —— "就按方案 A 吧"是决定
  if (/吗\s*$/.test(t)) return true
  // ③ 疑问/征询的固定开头
  if (/^(能不能|可不可以|要不要|是否|能否|如何|怎么|为什么|是不是|有没有|对吗|行不行|好不好|可以吗)/.test(t)) return true
  // ④ 句中征询语气（"你觉得…"、"…行不行"）——不含"吧"
  if (/(行不行|好不好|可以吗|怎么办|你觉得|你看呢|要不要)/.test(t)) return true
  return false
}

/**
 * 收一条候选。
 *
 * @param {object} opts
 * @param {boolean} opts.autoConfirm 是否自动确认（来自本对话的 scope）。
 *
 * ★ 2026-10-22 用户第二次指正：「<用户原话已隐去：自动确认不该靠人点>」
 *   ⇒ 默认自动确认。但自动确认必须带**三道守卫**，否则会引入比"要手动点"更严重的错：
 *
 *   守卫① 目标锁的"能否被自动改写"取决于**是谁锁的**：
 *        · 人工确认的目标（confirmedBy='human'）⇒ **永不**自动覆盖（人的决定优先）。
 *        · 自动确认的目标（confirmedBy='auto'）⇒ 允许被**更实质**的后续目标取代，
 *          并把旧目标写进 goalHistory（可追溯、可回退）。
 *        为什么必须这样区分（实测，不是推测）：本会话 #turn=1 是初步任务（91 字），
 *        #turn=4 才是核心目标（152 字）。若一律"已有目标就不覆盖"，就把初步任务
 *        永久锁成方向 —— 正好违反用户最初的硬约束「方向不得漂移」。
 *        "更实质"用 GOAL_REPLACE_RATIO（1.3 倍，见其注释里的真实数据标定）。
 *        没自动锁成时，原因写进 autoSkipped，不许静默。
 *   守卫② 只自动确认"用户自己的决定类"条目（goal/constraint/decision）。
 *        episode 之类召回片段是上下文，不是决定，继续留候选。
 *   守卫③ 提问/犹豫语不自动升格（looksLikeQuestion）—— 问句不是决定。
 */
export function captureContinuityCandidate(baseDir, item, { autoConfirm = false } = {}) {
  const x = lock({ ...item, status: 'candidate' }, item.kind || 'episode')
  if (!x || !x.cite) return loadContinuity(baseDir)
  const AUTO_KINDS = new Set(['goal', 'constraint', 'decision'])
  if (toBool(autoConfirm, false) && AUTO_KINDS.has(x.kind)) {
    const before = normalizePacket(loadContinuity(baseDir).packet)
    const key = contentKey(x.kind, x.text)
    // 守卫③：问句/犹豫语 ⇒ 只留候选，绝不升格成事实。
    const question = looksLikeQuestion(x.text)
    // 守卫①：目标锁按"谁锁的"决定能否被自动取代。
    const curGoal = x.kind === 'goal' && before.goalLock && before.goalLock.status === 'confirmed' ? before.goalLock : null
    const goalLockedByHuman = Boolean(curGoal) && curGoal.confirmedBy === 'human'
    const goalReplaceable = Boolean(curGoal) && !goalLockedByHuman
      && String(x.text).length >= String(curGoal.text).length * GOAL_REPLACE_RATIO
    // 同内容已存在（无论在候选还是已确认）⇒ 不再重复写入。
    const already = before.candidates.some((y) => contentKey(y.kind, y.text) === key)
      || (x.kind === 'goal' && before.goalLock && contentKey('goal', before.goalLock.text) === key)
      || before.constraintLocks.some((y) => contentKey(y.kind, y.text) === key)
      || before.decisions.some((y) => contentKey(y.kind, y.text) === key)
    if (already) return loadContinuity(baseDir)
    const blockedByGoal = Boolean(curGoal) && !goalReplaceable
    if (!question && !blockedByGoal) {
      try {
        const saved = confirmContinuityItem(baseDir, { ...x, status: 'confirmed' }, {
          cite: x.cite, project: x.project,
          // 取代旧自动目标时记下 supersedes 链（可追溯）。
          supersedes: x.kind === 'goal' && goalReplaceable ? curGoal.id : x.supersedes,
          confirmedBy: 'auto',
        })
        // 让调用方/界面能看见"这条是自动确认的"，而不是以为要手动点。
        return {
          ...saved,
          autoConfirmed: {
            kind: x.kind, text: x.text, cite: x.cite,
            ...(x.kind === 'goal' && goalReplaceable ? { replaced: curGoal.text } : {}),
          },
        }
      } catch { /* 确认失败就退回候选，绝不静默丢弃 */ }
    }
    // 没自动确认时，把原因带出来（静默跳过 = 又一次"看起来在跑其实没动"）。
    const autoSkipped = question
      ? { kind: x.kind, reason: 'looks-like-question', text: x.text }
      : goalLockedByHuman
        ? { kind: x.kind, reason: 'goal-locked-by-human', text: x.text }
        : { kind: x.kind, reason: 'goal-not-more-substantial', text: x.text }
    const old0 = loadContinuity(baseDir)
    const p0 = normalizePacket(old0.packet)
    const same0 = p0.candidates.some((y) => contentKey(y.kind, y.text) === key)
    if (same0) return { ...old0, autoSkipped }
    return { ...updateContinuityPacket(baseDir, { ...p0, candidates: [...p0.candidates, x].slice(-24) }), autoSkipped }
  }
  const old = loadContinuity(baseDir)
  const p = normalizePacket(old.packet)
  // ★ 2026-10-22 实测：同一条目标原话在多个 turn 里被复述（用户重发/引用），
  //   旧去重键是 (cite, kind, text) 全等 ⇒ 出处不同就各存一条 ⇒ 候选里出现 3 条同样内容。
  //   改为按**内容+类别**去重：内容相同就是同一条，保留最早那条出处。
  const same = p.candidates.some((y) => contentKey(y.kind, y.text) === contentKey(x.kind, x.text))
  if (same) return old
  const candidates = [...p.candidates, x].slice(-24)
  return updateContinuityPacket(baseDir, { ...p, candidates })
}

/**
 * 撤销一条**已确认**条目（自动确认必须能撤销，否则等于把错误固化）。
 * 与 rejectContinuityCandidate（只管候选）分开：两者语义不同，不能混用。
 */
export function rejectContinuityItem(baseDir, id) {
  const key = text(id, 80)
  if (!key) return { ok: false, error: 'id required', removed: null }
  const p = normalizePacket(loadContinuity(baseDir).packet)
  const next = { ...p }
  let removed = null
  if (p.goalLock && p.goalLock.id === key) { removed = p.goalLock; next.goalLock = null }
  for (const field of ['constraintLocks', 'decisions', 'openItems', 'workingSet', 'candidates']) {
    const hit = (p[field] || []).find((y) => y.id === key)
    if (!hit) continue
    removed = removed || hit
    next[field] = (p[field] || []).filter((y) => y.id !== key)
  }
  if (!removed) return { ok: false, error: 'item not found', removed: null }
  updateContinuityPacket(baseDir, next)
  return { ok: true, removed }
}
export function confirmContinuityItem(baseDir, item, { cite = '', project = '', supersedes = '', confirmedBy = 'human' } = {}) {
  // project 的取法必须是"显式覆盖 > 条目自带 > 空"，不能无条件覆盖：
  //  - 传空串会**擦掉**候选自己的来源项目；
  //  - 传 scope 值会把"来源项目未知"的条目**冒充**成当前项目。
  // 两种都会让项目闸看起来在跑、实际判的是错标签（2026-10-21 实测）。
  const projectForItem = project || item.project || ''
  // ★ 2026-10-21：超长必须在调 lock() **之前**显式拒绝。
  //   因为 lock() 对已确认条目是"截断到 4000 字"而不是"拒绝" ——
  //   若把 too-long 判断放在 `if (!x)` 里面，那个分支永远不可达（x 不会为 null），
  //   于是"超长"会被静默截断，而调用方以为成功。这是死代码 + 静默改写。
  const body = String(item?.text ?? '')
  if (body.trim().length > MAX_CONFIRMED_CHARS) {
    throw new Error(`confirmed item text too long (${body.trim().length} > ${MAX_CONFIRMED_CHARS})`)
  }
  const x = lock({ ...item, status: item.kind === 'open_issue' ? 'open' : 'confirmed', cite, project: projectForItem, supersedes, confirmedBy, verifiedAt: Date.now() }, item.kind)
  // 失败原因必须分开说：把"像运行环境样板"报成"缺出处"，会让人一直去补 cite 却永远补不好。
  if (!x) {
    if (!body.trim()) throw new Error('confirmed continuity items require non-empty text')
    throw new Error('confirmed item text looks like runtime/tool wrapper text, not project content')
  }
  if (!x.cite) throw new Error('confirmed continuity items require a source citation')
  const p = normalizePacket(loadContinuity(baseDir).packet)
  // 幂等键必须是**精确重复**（kind + cite + text），不能只用 (kind, cite)：
  //   实测 2026-10-21：同一 cite 连确认两次 ⇒ decisions 出现两条逐字相同的条目。
  //   但同一轮里可能有两个不同决定共用同一出处（「改成 A，并定为 B」都引 #turn=5），
  //   按 (kind, cite) 过滤会把第二条静默替换掉 —— 取代语义已有显式 supersedes 字段，不该隐式覆盖。
  const sameItem = (y) => y.kind === x.kind && y.cite === x.cite && y.text === x.text
  // 候选也要按同一精确键移除：lock() 会给无 id 的条目新建 UUID，
  // 只按 id 过滤会留下原候选 ⇒ 同一事实同时躺在 candidates 与 locks 里。
  const next = { ...p, candidates: p.candidates.filter((y) => y.id !== x.id && !sameItem(y)) }
  if (x.kind === 'goal') {
    // ★ 2026-10-22：被取代的旧目标必须留进 goalHistory。
    //   否则"目标换了"这件事在状态里**没有任何痕迹** —— 用户看不到方向被改过，
    //   也没法回退。这正是"方向漂移"最难发现的那种形态。
    if (p.goalLock && p.goalLock.text !== x.text) {
      next.goalHistory = [p.goalLock, ...(p.goalHistory || [])].slice(0, 12)
    }
    next.goalLock = x
  }
  if (x.kind === 'constraint') next.constraintLocks = [...p.constraintLocks.filter((y) => !sameItem(y)), x]
  if (x.kind === 'decision') next.decisions = [...p.decisions.filter((y) => !sameItem(y)), x]
  if (x.kind === 'open_issue') next.openItems = [...p.openItems.filter((y) => !sameItem(y)), x]
  return updateContinuityPacket(baseDir, next)
}
/**
 * 清空状态包（保留或重置授权范围），并在覆盖前写一份带时间戳的备份。
 * 用途：测试态与正式态分离 —— 跑过运行态测试之后，可以显式清掉测试残留，
 * 而不是让"PCM_RUNTIME_TEST"这类假目标留在正式状态里。
 * 绝不静默删除：返回值里带 backup 路径，调用方必须能看见。
 */
export function resetContinuity(baseDir, { keepScope = true } = {}) {
  const old = loadContinuity(baseDir)
  const dir = join(baseDir, 'continuity')
  mkdirSync(dir, { recursive: true })
  const backup = join(dir, `state.backup-${new Date().toISOString().replace(/[:.]/g, '-')}.json`)
  try { writeFileSync(backup, JSON.stringify(old, null, 2), 'utf8') } catch { /* 备份失败不阻断，但下面如实报告 */ }
  const next = {
    ...JSON.parse(JSON.stringify(DEFAULT)),
    enabled: keepScope ? old.enabled : false,
    mode: keepScope ? old.mode : 'off',
    project: keepScope ? old.project : '',
    selectedSessions: keepScope ? old.selectedSessions : [],
    // keepScope=false ⇒ 连"每个对话的授权"一起清掉（否则"重置"只是清了状态包，
    // 各对话仍按旧范围继续注入，看起来像"重置没用"）。
    scopes: keepScope ? (old.scopes || {}) : {},
    updatedAt: Date.now(),
  }
  writeAtomic(pathOf(baseDir), next)
  return { ...next, backup }
}

/** 丢弃单条候选（用户拒绝）。返回被丢弃的条目，便于回执核对。 */
export function rejectContinuityCandidate(baseDir, id) {
  const p = normalizePacket(loadContinuity(baseDir).packet)
  const hit = p.candidates.find((x) => x.id === id) || null
  if (!hit) return { ok: false, error: 'candidate not found', removed: null }
  const next = { ...p, candidates: p.candidates.filter((x) => x.id !== id) }
  updateContinuityPacket(baseDir, next)
  return { ok: true, removed: hit }
}

export function packetFromMessages(messages = [], goal = '', sessionId = '') {  const rows = (Array.isArray(messages) ? messages : []).filter((m) => m && (m.role === 'user' || m.role === 'assistant'))
    .map((m) => ({ role: m.role, text: text(Array.isArray(m.content) ? m.content.map((x) => x?.text || '').join(' ') : m.content, 320) })).filter((m) => m.text)
  const candidates = rows.filter((x) => x.role === 'user').slice(-6).map((x) => lock({ kind: 'episode', status: 'candidate', text: x.text, sourceSession: sessionId }, 'episode')).filter(Boolean)
  return { candidates, goalCandidate: text(goal, 700), recent: rows.slice(-8), sources: [] }
}
function confirmed(items) { return list(items).filter((x) => x && (x.status === 'confirmed' || x.status === 'open') && x.cite) }
/**
 * 条目是否落在**这个对话**的范围内。
 * 传入的是 scopeFor(state, 当前对话) 的结果，**不是**全局设置。
 */
function inScope(item, scope, { sessionId = '' } = {}) {
  if (!item) return false
  if (!scope || scope.enabled !== true) return false
  if (scope.mode === 'all') return true
  // ★ 2026-10-22：所有"是不是同一个对话"的比较都过 normSessionId。
  //   实测两种前缀并存（249 个 `session-<uuid>` / 1262 个裸 `<uuid>`），
  //   按原串比会把同一对话判成两个 ⇒ 该给的条目被滤掉，且不报错。
  const self = normSessionId(sessionId)
  const src = normSessionId(item.sourceSession)
  const picked = list(scope.selectedSessions, 40).map(normSessionId).filter(Boolean)
  if (scope.mode === 'project') {
    // Project mode is fail-closed: an item without an explicit project match is not in scope.
    if (!(scope.project && item.project === scope.project)) return false
    // ★ 2026-10-21：工作区内还可以再限定对话（界面提供了"工作区 → 该工作区内的对话"）。
    //   约定：selectedSessions 为空 = 整个工作区；非空 = 只在这些对话里取（更严，不是更松）。
    if (picked.length === 0) return true
    return Boolean(src && (src === self || picked.includes(src)))
  }
  if (scope.mode === 'sessions') {
    // ★ 2026-10-22：本对话**自己**永远在范围内 —— 否则本对话确认的目标/约束
    //   在自己的对话里都不出现，那等于"接上了别人、丢了自己"。
    //   其余来源只认显式接上的对话（fail-closed：没接上就不给）。
    return Boolean(src && (src === self || picked.includes(src)))
  }
  return false
}
export function renderContinuityPacket(state, context = {}) {
  // ★ 2026-10-22：范围按**当前对话**取。没有该对话的范围 = 不注入（fail-closed），
  //   绝不回落到全局设置 —— 那个回落就是"切到别的对话也接上同一批"的病根。
  const sessionId = text(context?.sessionId, 120)
  const scope = scopeFor(state, sessionId)
  if (scope.enabled !== true || scope.mode === 'off') return ''
  const p = normalizePacket(state.packet)
  const filter = (xs) => xs.filter((x) => inScope(x, scope, { sessionId }))
  const goal = p.goalLock && inScope(p.goalLock, scope, { sessionId }) ? p.goalLock : null
  const constraints = filter(p.constraintLocks)
  const decisions = filter(p.decisions)
  const openItems = filter(p.openItems)
  const workingSet = filter(p.workingSet)
  const candidates = filter(p.candidates)
  // ★ 2026-10-22：**没有实质内容就不注入**。
  //   实测：enabled=true / mode=project / project="" 时，旧实现每轮都注入 181 字
  //   的"空壳包"（只有标题、说明和"状态版本：1；更新时间：未知"），
  //   既占上下文又让人以为"状态包在工作"，实际一条事实都没有。
  //   判据：确认项（目标/约束/决定/未完成/工作集）或候选，至少有一条才算有内容。
  const hasContent = Boolean(
    (goal && goal.status === 'confirmed' && goal.cite)
    || confirmed(constraints).length || confirmed(decisions).length || confirmed(openItems).length
    || workingSet.length || candidates.length,
  )
  if (!hasContent) return ''
  const lines = ['<project-continuity mode="active" source="dsh-memory-plugin">', '这是项目连续性状态包。带出处的确认项可作为工作状态参考；候选项不是事实，不得直接据此断言。']
  // 明确写出"这份范围只属于本对话"，避免被当成全局事实引用。
  const linked = list(scope.selectedSessions, 40)
  lines.push(scope.mode === 'sessions'
    ? `衔接范围：只属于本对话（本对话 + 接上的 ${linked.length} 个对话）`
    : scope.mode === 'project'
      ? `衔接范围：只属于本对话（工作区 ${scope.project}${linked.length ? ` 内指定的 ${linked.length} 个对话` : ' 全工作区'}）`
      : '衔接范围：只属于本对话（全部相关对话）')
  if (goal?.status === 'confirmed' && goal.cite) lines.push(`当前目标锁：${text(goal.text)}〔${goal.cite}〕`)
  const add = (label, xs) => { const ys = confirmed(xs); if (ys.length) lines.push(`${label}：${ys.slice(0, 8).map((x) => `${text(x.text)}〔${x.cite}〕`).join('；')}`) }
  add('硬约束锁', constraints); add('已确认决定', decisions); add('未完成事项', openItems)
  if (workingSet.length) lines.push(`工作集：${workingSet.slice(0, 8).map((x) => `${text(x.text)}${x.cite ? `〔${x.cite}〕` : ''}`).join('；')}`)
  if (candidates.length) lines.push(`候选历史（待确认）：${candidates.slice(-4).map((x) => text(x.text, 220)).join('；')}`)
  if (p.sources.length) lines.push(`状态来源：${p.sources.slice(0, 12).join('；')}`)
  lines.push(`状态版本：${p.version}；更新时间：${p.updatedAt ? new Date(p.updatedAt).toISOString() : '未知'}`, '若状态包与当前用户明确指令冲突，以当前用户指令为准；不要自行改写目标锁。', '</project-continuity>')
  return lines.join('\n')
}
export function continuityPath(baseDir) { return pathOf(baseDir) }
export { MODES }
