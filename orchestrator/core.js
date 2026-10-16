/**
 * dsh-memory-plugin · 注意力编排器（orchestrator）核心
 *
 * 目的（用户 2026-10-03 明令）：
 *   插件每一轮**另外调一次 API**，由那个调用**替当前会话的模型做"注意力结构决策"**——
 *   决定这一轮该把什么塞进主模型；**调完即弃**，所以那个调用的上下文永远干净、充足。
 *
 * 架构（无状态函数）：
 *   (用户消息, 最近 N 轮, 候选记忆, 任务状态) ──→ 编排器 ──→ 注入方案 JSON ──→ 丢弃
 *
 * 设计纪律：
 *   1. **默认关**：不开时本模块完全不介入，行为与 v0.1.5 一模一样。
 *   2. **失败开放**：任何错误 ⇒ 回落原逻辑，绝不弄坏一轮对话。
 *   3. **兜底硬规则**：用户提"之前/上次/那个"时，相关候选**强制常驻**，编排器否决无效。
 *   4. **留痕**：每轮决策写 orchestrator/decisions.jsonl，可审计可复算。
 *   5. **零外部依赖**，可单测。
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync, appendFileSync } from 'node:fs'
import { join } from 'node:path'

// ---------------------------------------------------------------------------
// 配置
// ---------------------------------------------------------------------------

export const DEFAULT_CONFIG = {
  enabled: false,              // ★默认关（用户定）
  modelMode: 'follow',         // 'follow' 跟随当前会话模型 | 'custom' 自选
  customProvider: '',
  customModel: '',
  recentTurns: 6,
  candidates: 20,
  persistMax: 4,
  quoteMax: 4,
  everyNthTurn: 1,
  forceOnKeywords: ['之前', '上次', '我们说过', '当时', '那个文件', '后来改成', '早先', '前面说'],
  timeoutMs: 20000,
  // ★ 第八轮：回声过滤阈值 —— 候选内容里"只是 query 字面"的比例上限。
  //   超过它、且候选没有任何具体内容（数字/路径/标识符/决定口径）⇒ 判为回声，剔除。
  //   0.6 是**可调的判据常量**（不是观测量）：调到 1 等于关掉过滤。
  echoRatioMax: 0.6,
  debug: false,
}

export function loadConfig(baseDir) {
  const p = join(baseDir, 'orchestrator', 'config.json')
  try {
    if (existsSync(p)) return { ...DEFAULT_CONFIG, ...JSON.parse(readFileSync(p, 'utf8')) }
  } catch { /* 坏配置 ⇒ 用默认 */ }
  return { ...DEFAULT_CONFIG }
}

export function saveConfig(baseDir, patch) {
  try {
    const dir = join(baseDir, 'orchestrator')
    mkdirSync(dir, { recursive: true })
    const next = { ...loadConfig(baseDir), ...patch }
    writeFileSync(join(dir, 'config.json'), JSON.stringify(next, null, 2), 'utf8')
    return next
  } catch {
    return loadConfig(baseDir)
  }
}

// ---------------------------------------------------------------------------
// 触发判定
// ---------------------------------------------------------------------------

export function hasForceSignal(userText, cfg = DEFAULT_CONFIG) {
  const t = String(userText || '')
  if (!t) return false
  return (cfg.forceOnKeywords || []).some((k) => k && t.includes(k))
}

export function shouldOrchestrate({ cfg, turn, userText, candidates, isSubagent }) {
  if (!cfg.enabled) return { run: false, why: 'disabled' }
  if (isSubagent) return { run: false, why: 'subagent' }
  if (!candidates || candidates.length === 0) return { run: false, why: 'no-candidates' }
  const force = hasForceSignal(userText, cfg)
  const n = Math.max(1, Number(cfg.everyNthTurn) || 1)
  if (n > 1 && turn % n !== 0 && !force) return { run: false, why: `everyNth(${n})` }
  return { run: true, why: force ? 'force-signal' : 'scheduled' }
}

// ---------------------------------------------------------------------------
// 「这一轮到底有没有可回顾的东西」
// ---------------------------------------------------------------------------
/**
 * 用户这句话是不是**带着请求/问题**（而不是单纯报告一个已完成的状态）？
 *
 * ★ 2026-10-15（第六轮 · 来自活注入块的实测）★★
 * 实证：用户说「我已经重启了，」⇒ 检索按**逐字相同**命中 10-04 那个会话里
 *   「我已经重启了」的**元记录**（"助手观察到注入块已生效""可见上下文中没有 goal-anchor 块"…），
 *   编排器再把它们挑成【必须常驻的事实】注入 —— 7 条全是**别处对同一句短话的元记录**。
 *   根因不是"检索坏了"，是**这类轮次本来就没有可回顾的东西**：
 *   用户在报告一个**已完成的状态**，下一步该做的是**去读现状**，不是回看历史。
 *
 * 判据（**确定性、可测、可解释**）：短（≤12 字）且 **既无问号、也无请求动词** ⇒ 判为陈述/应答轮。
 *   · "我已经重启了，"（6 字、无请求词）⇒ 命中 ⇒ 不注入；
 *   · "已经重启继续检查"（有"检查"）⇒ 不命中 ⇒ 照常；
 *   · "完整克隆下来了吗"（有"吗"）⇒ 不命中 ⇒ 照常；
 *   · "继续"（小话轮）由既有 SMALL_TALK 处理，不归这里。
 * ★ 只有"无 goal"时才跳过（有 goal 时 review 目标更有价值，见调用方）。
 */
const REQUEST_WORDS = [
  '？', '?', '吗', '呢', '怎么', '如何', '为什么', '什么', '哪些', '哪个', '多少', '是否',
  '帮我', '给我', '请', '告诉', '解释', '说明', '总结', '查看', '看看', '检查', '查', '搜',
  '做', '改', '修', '加', '删', '写', '跑', '测', '试', '继续', '开始', '下一步', '接着',
  '给我看', '对比', '评估', '分析', '规划', '实现', '部署', '优化', '找', '看',
]

export function looksLikeRequest(text) {
  const t = String(text || '').trim()
  if (!t) return false
  return REQUEST_WORDS.some((w) => t.includes(w))
}

/** 判为"纯状态陈述/应答轮"（**只作为给判官的提示**，不再当闸用 —— 见文件末注释）。 */
export function isAckOnly(text, { maxChars = 12 } = {}) {
  const t = String(text || '').trim()
  if (!t) return false
  if (t.length > maxChars) return false
  return !looksLikeRequest(t)
}

// ---------------------------------------------------------------------------
// ★★ 回声过滤：与"句子长短/是否重复"无关的判据（第八轮 · 用户否决了"形状闸"）★★
// ---------------------------------------------------------------------------
// 用户的否决（2026-10-15 原话）：「**我不可能永远说话不重复，这不算根治！这是粗暴的切割！**」
// 上一轮我用 `isAckOnly`（短 + 无问号 + 无请求词 ⇒ 不回顾）当闸，确实错在两处：
//   ① 它是**按句子形状**切的，与病根无关 —— 用户重复问同一件事会被误杀
//      （例：再问一次「廊柱截面多大」也是 6 字、无问号、无请求词）；
//   ② 病根是「**同一句话在别处出现过，就被当成知识**」，形状闸根本没碰它。
//
// 正确的判据只有一条：**候选有没有 query 之外的信息增量**。
//   一个候选若只是把那句话（或那句话的近义重复）又抄了一遍，信息增量≈0 ⇒ 它是**回声**，不是知识。
//   与"这句话是不是应答、是不是短、是不是第一次说"**全部无关**。
//   · 「我已经重启了，」→ 那边候选是「用户本轮表示「我已经重启了」（与上一轮表述相同）」
//     ⇒ 逐字含 query、且无任何具体内容（数字/路径/标识符/决定）⇒ 回声 ⇒ 剔；
//   · 「廊柱截面多大」（重复问）→ 那边候选是「廊柱专条 Ø0.22 × 高 2.80，已定口径」
//     ⇒ 含 query 但**有具体内容** ⇒ 不是回声 ⇒ 照常进判官 ⇒ **重复提问照样能召回**。
// ---------------------------------------------------------------------------

/** 极简分词：ASCII 词（含 _ . - 与数字）+ CJK 二元组。无外部依赖、结果稳定可测。 */
export function tokenize(s) {
  const t = String(s || '').toLowerCase()
  const out = (t.match(/[a-z0-9_][a-z0-9_.\-]{1,}/g) || []).slice()
  for (const run of t.replace(/[^\u4e00-\u9fa5]+/g, ' ').split(/\s+/)) {
    if (!run) continue
    if (run.length < 2) { out.push(run); continue }
    for (let i = 0; i + 2 <= run.length; i += 1) out.push(run.slice(i, i + 2))
  }
  return out
}

/** query 的"核心串"：去标点/去空白，用于"逐字包含"判断。 */
export function queryCore(query) {
  return String(query || '')
    .replace(/^[〔\[]?本轮[〕\]]?\s*/, '')
    .replace(/[\s，。！？、；：,.!?;:"'“”‘’（）()\[\]【】]/g, '')
}

/**
 * 候选是否含**具体内容**（数字/路径/文件名/标识符/决定口径）。
 * 这是"有没有信息增量"的正向判据 —— 只认具体，不认"像不像事实"。
 */
const SUBSTANTIVE_PAT = [
  /\d/,                                     // 任何数字：尺寸/版本/轮次/阈值
  /[a-z]:[\\/]/i,                            // Windows 路径
  /\.(py|js|mjs|md|json|yaml|yml|txt|blend|png|jpg|jpeg|mp4|xlsx|docx|pdf)\b/i,
  /\b[a-z_][a-z0-9_]*[_.][a-z0-9_]+\b/i,      // 带下划线/点的标识符（db_path、a.b）
  /\b[a-z]+[A-Z][a-zA-Z]{2,}\b/,              // camelCase（buildQueryWithGoal）
  /(定|决定|裁定|口径|规格|必须|不许|禁止|改用|采用|不用|上限|下限|阈值|约定|标准|基线|要求|禁止)/,
  /[\u4e00-\u9fa5]{2,}[×x*][\u4e00-\u9fa5]{2,}/,   // 「Ø0.22 × 高 2.80」这类量纲对
]
export function hasSubstantive(text) {
  return SUBSTANTIVE_PAT.some((re) => re.test(String(text || '')))
}

/** 候选内容里有多大比例**只是 query 的字面**（0=完全无关，1=全是 query）。 */
export function echoRatio(text, query) {
  const A = tokenize(text)
  if (!A.length) return 0
  const Q = new Set(tokenize(query))
  let hit = 0
  for (const a of A) if (Q.has(a)) hit += 1
  return hit / A.length
}

/**
 * 是不是"回声候选"：**只是把 query 又抄了一遍，没有任何具体内容**。
 * @param {string} text  候选正文
 * @param {string} query 本轮 query
 * @param {number} ratioMax 重合度上限（默认 0.6，可配；见 DEFAULT_CONFIG.echoRatioMax）
 */
export function isEchoOnly(text, query, ratioMax = 0.6) {
  const t = String(text || '')
  if (!t.trim()) return false
  const core = queryCore(query)
  const verbatim = core.length >= 4 && t.includes(core)
  const ratio = echoRatio(t, query)
  if (!verbatim && ratio < ratioMax) return false      // 既不是逐字抄、重合也不高 ⇒ 不是回声
  return !hasSubstantive(t)                            // 有具体内容 ⇒ 放过（重复提问也能召回）
}

/** 把候选池里的回声剔掉。返回 {kept, dropped}（dropped 用于留痕，可见）。 */
export function filterEchoCandidates(candidates, query, { ratioMax = 0.6 } = {}) {
  const kept = []
  const dropped = []
  for (const c of (Array.isArray(candidates) ? candidates : [])) {
    const text = String(c?.text || '')
    if (isEchoOnly(text, query, ratioMax)) {
      dropped.push({
        cite: String(c?.cite || c?.session_id || '').slice(0, 48),
        ratio: Number(echoRatio(text, query).toFixed(2)),
        text: text.replace(/\s+/g, ' ').slice(0, 60),
      })
    } else {
      kept.push(c)
    }
  }
  return { kept, dropped }
}


// ---------------------------------------------------------------------------
// 请求组装
// ---------------------------------------------------------------------------

export const SYSTEM_PROMPT = `你是「注意力编排器」。你不回答用户，你只做一件事：
在给定材料里**挑出**这一轮主模型必须看到的内容，并给出去处。

铁律：
1. 你只能从【候选记忆】里挑，**绝不自己回忆或编造**任何事实。候选里没有的，不许写。
2. 每条挑出来的都必须带 cite（原样照抄候选里的出处字段）。
3. 宁可少挑，不可挑错。挑不准的放 drop。**一条都挑不准时，persist/quote 就都留空**——留空是对的答案，不是失败。
4. 用户这轮若提到"之前/上次/那个"等回看信号，与之相关的候选**必须**进 persist 或 quote。
5. 只输出 JSON，不要解释文字、不要 markdown 代码围栏。
6. ★ **"同一句话在别处出现过"不等于"与这一轮相关"**：
   若候选只是**别处对同一句短话的元记录**（例如用户上一轮说过的同一句“我已经重启了”，
   而候选是当时助手对"注入块生没生效"的观察/推断），**全部 drop**。
   这种候选与本轮要解决的问题无关，注入只会误导。
7. ★ 用户这句若是**报告一个已完成的状态**（“我已重启了”“好了”“做完了”），
   本轮通常**没有任何东西需要常驻**：persist 留空即可（要核现状就现查，不靠历史）。
8. ★ 判据是"**主模型这轮要解决什么**"：只挑**直接服务于那件事**的候选；
   只是"背景相关""同一主题"的，放 drop。
9. ★ **回声判据（最重要）**：每条候选都标了〔距今 …〕与〔与 query 重合 X%〕。
   **只把 query 又抄了一遍、没有 query 之外具体内容**（数字／路径／文件名／标识符／决定口径）的候选，
   是"回声"，**必须放 drop** —— 它的信息增量是 0，注入只会误导。
10. ★ **用户重复说同一件事是正常的**。判据只看"候选有没有 query 之外的信息量"，
   **绝不**看"用户这句话短不短、像不像应答、是不是第一次说"。
   同一个问题再问一遍时，只要候选里带着答案（数字/结论/口径），**照常挑选**。

输出契约：
{
  "persist": [{"fact": "<=60字的事实", "cite": "出处原样"}],
  "quote":   [{"text": "<=200字的原文要点", "cite": "出处原样"}],
  "drop":    ["被丢弃候选的 cite 或简短标识"],
  "order":   ["persist","quote","recent","task"],
  "reason":  "一句话，说明你为什么这么挑"
}`

/** 距今多久（给人看的短标签），写进编排器的**输入**里，让它能判"这条是不是过期了"。 */
export function ageText(ms, now = Date.now()) {
  const t = Number(ms)
  if (!Number.isFinite(t) || t <= 0) return '时间未知'
  const d = Math.max(0, now - t)
  const min = Math.floor(d / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  if (min < 60 * 24) return `${Math.floor(min / 60)} 小时前`
  return `${Math.floor(min / 1440)} 天前`
}

export function buildRequest({ cfg, userText, recentTurns, candidates, taskState, forceSignal, sessionKey }) {
  const L = []
  // ★ 2026-10-15（第六轮）：把**判官需要的环境事实**给它 ——
  //   旧版只给"用户说了什么 + 候选"，于是它把"同一句话在别处的元记录"当成了本轮相关。
  //   现在补：当前会话、当前时间、本轮形态、以及**每条候选距今多久**。
  L.push('## 本轮环境')
  L.push(`- 当前会话：${String(sessionKey || '(未知)').slice(0, 48)}`)
  L.push(`- 当前时间：${new Date().toISOString().slice(0, 16).replace('T', ' ')}Z`)
  if (isAckOnly(userText)) {
    L.push('- 本轮形态：**用户在报告一个已完成的状态**（不是提问/要求）'
      + ' ⇒ 通常**没有任何内容需要常驻**；要核现状请现查，别靠历史。')
  }
  L.push('')
  L.push('## 用户这一轮说')
  L.push(String(userText || '').slice(0, 800))
  L.push('')
  if (forceSignal) {
    L.push('⚠ 本轮命中**回看信号**：与"用户提到的往事"相关的候选必须进 persist 或 quote。')
    L.push('')
  }
  if (recentTurns && recentTurns.length) {
    L.push(`## 最近 ${recentTurns.length} 轮（仅供理解上下文，不要从这里挑事实）`)
    for (const m of recentTurns) {
      L.push(`- [${m.role}] ${String(m.text || '').replace(/\s+/g, ' ').slice(0, 160)}`)
    }
    L.push('')
  }
  if (taskState) {
    L.push('## 当前任务状态')
    L.push(String(taskState).slice(0, 600))
    L.push('')
  }
  const now = Date.now()
  L.push(`## 候选记忆（共 ${(candidates || []).length} 条，只能从这里挑）`)
  ;(candidates || []).forEach((c, i) => {
    const cite = c.cite || c.session_id || `#${i}`
    const age = ageText(c.timeStart ?? c.time_start, now)
    // ★ 第八轮：把"与 query 的重合度"也标出来 —— 判官据此识别"回声候选"
    const er = c.echo != null ? `〔与 query 重合 ${Math.round(Number(c.echo) * 100)}%〕` : ''
    L.push(`${i + 1}. [${cite}] 〔距今 ${age}〕${er} ${String(c.text || '').replace(/\s+/g, ' ').slice(0, 300)}`)
  })
  L.push('')
  L.push('## 你的输出上限')
  L.push(`- persist 最多 ${cfg.persistMax} 条（**0 条是合法答案**）`)
  L.push(`- quote 最多 ${cfg.quoteMax} 条（**0 条是合法答案**）`)
  return L.join('\n')
}

// ---------------------------------------------------------------------------
// 响应解析（严厉：解析不出来就回落，不猜）
// ---------------------------------------------------------------------------

export function extractJson(raw) {
  const s = String(raw || '')
  if (!s.trim()) return null
  try { return JSON.parse(s) } catch { /* next */ }
  const fence = s.match(/```(?:json)?\s*([\s\S]*?)```/i)
  if (fence) { try { return JSON.parse(fence[1]) } catch { /* next */ } }
  const a = s.indexOf('{'); const b = s.lastIndexOf('}')
  if (a >= 0 && b > a) { try { return JSON.parse(s.slice(a, b + 1)) } catch { /* next */ } }
  return null
}

export function parsePlan(raw, { cfg = DEFAULT_CONFIG } = {}) {
  const j = extractJson(raw)
  if (!j || typeof j !== 'object') return { ok: false, error: 'not-json' }
  const arr = (x) => (Array.isArray(x) ? x : [])
  const norm = (it) => {
    if (!it || typeof it !== 'object') return null
    const cite = String(it.cite ?? '').trim()
    if (!cite) return null                        // 铁律2：没出处 ⇒ 丢
    // ★ 2026-10-15：fact 也**折行 + 截断** —— 旧版原样收下，模型偶尔整段贴进来，
    //   渲染进"必须常驻的事实"就会变成一大坨（注入块体量失控的旧病）。
    const fact = String(it.fact ?? it.text ?? '').replace(/\s+/g, ' ').trim().slice(0, 400)
    if (!fact) return null
    return { fact, cite }
  }
  const persist = arr(j.persist).map(norm).filter(Boolean).slice(0, cfg.persistMax)
  const quote = arr(j.quote).map(norm).filter(Boolean).slice(0, cfg.quoteMax)
  const drop = arr(j.drop).map((x) => String(x).slice(0, 120)).slice(0, 40)
  const order = arr(j.order).map(String).filter((x) => ['persist', 'quote', 'recent', 'task'].includes(x))
  return {
    ok: true,
    plan: {
      persist, quote, drop,
      order: order.length ? order : ['persist', 'quote', 'recent', 'task'],
      reason: String(j.reason ?? '').slice(0, 300),
    },
  }
}

// ---------------------------------------------------------------------------
// 兜底硬规则
// ---------------------------------------------------------------------------

export function enforceHardRules(plan, { cfg, candidates, forceSignal, userText }) {
  /**
   * 时间表的**规范键**：把任意出处写法归一成 `<sid>#<turn>`。
   * 用 `function` 声明 ⇒ 提升，可以在下面的建表循环里先调用（避免 const 的 TDZ）。
   * 与 parseCite 同源规则，但不依赖其初始化顺序。
   */
  function tkey(s) {
    const t = String(s ?? '').trim()
    if (!t) return ''
    const m = t.match(/#(?:turn|T)\s*=\s*(\d+)/i)
    const turn = m ? m[1] : ''
    let sid = t.replace(/#(?:turn|T)\s*=\s*\d+/i, '')
    sid = sid.replace(/^[\[('"`]+/, '').replace(/^dsh:/i, '').replace(/^session-/i, '')
    const ms = sid.match(/^[0-9a-f][0-9a-f-]{3,40}/i)
    sid = ms ? ms[0].replace(/-+$/, '').toLowerCase() : ''
    return (sid || turn) ? (sid + '#' + turn) : ''
  }

  // ★ R3 防御：plan 可能是任何东西（编排器返回垃圾、上游抛异常）。
  //   本函数每轮都跑，绝不能因 plan 缺字段而抛 —— 抛了就是"弄坏一轮对话"。
  const P = (plan && typeof plan === 'object') ? plan : {}
  const A = (x) => (Array.isArray(x) ? x : [])
  const out = {
    persist: [...A(P.persist)],
    quote: [...A(P.quote)],
    drop: [...A(P.drop)],
    order: A(P.order).length ? [...P.order] : ['persist', 'quote', 'recent', 'task'],
    reason: typeof P.reason === 'string' ? P.reason : '',
    flags: [],
    // ★ 2026-10-15（第二轮）：被 R0 丢掉的出处**留痕**（有界）。
    //   起因：旧版只记"丢了几条"，于是"模型到底写了什么垃圾"**无从查证** ——
    //   而它直接决定 R2 兜底会不会被触发（见下方 R2 注释）。
    rejectedCites: [],
    empty: false,
  }

  // ★ 2026-10-04：给每条补上**时间戳**（渲染时要标"距今多久"）。
  //   parsePlan 产出的条目只有 {fact, cite} —— 模型不会自己带时间，
  //   时间只能由**候选**提供 ⇒ 这里按 cite 反查候选的 timeStart。
  //
  // ★★ 2026-10-15（第四轮）修回归：**盖章必须在"出处规范化之后"再补一次**。
  //   我第二轮加了"出处先规范化"，但盖章仍只用**模型原文**（带注解的那串）去查表 ⇒
  //   查不到 ⇒ `timeStart` 为空 ⇒ 渲染时**一个〔距今 X〕都没有、时效提示也不出现**。
  //   实证（2026-10-15T10:39:40Z 真实注入块）：`R0:规范化5条出处` + 全文**零个〔距今〕**，
  //   而它注入的偏偏是「源码≠运行副本 / 需再重启」这类**会过期**的状态断言。
  //
  //   附带把查表方式也换稳：键从"出处原串"改成**规范化键**（`<sid>#<turn>`），
  //   这样方括号/去前缀/带注解/短 sid 全都能命中（函数用 `function` 声明 ⇒ 提升，无 TDZ）。
  const tmap = new Map()
  try {
    for (const c of (candidates || [])) {
      const cite = c && (c.cite || c.session_id)
      if (!cite) continue
      const t = c.timeStart ?? c.time_start ?? null
      const k = tkey(cite)
      if (k && t) tmap.set(k, t)
    }
  } catch { /* 建表失败 ⇒ 后面盖不上，不阻断注入 */ }

  /** 按（原样 / 规范化后的）出处查候选时间戳。 */
  const timeOf = (...cites) => {
    for (const c of cites) {
      if (!c) continue
      const k = tkey(c)
      const t = k ? tmap.get(k) : null
      if (t) return t
    }
    return null
  }

  try {
    if (tmap.size) {
      const stamp = (x) => {
        if (!x || typeof x !== 'object') return x
        if (x.timeStart != null) return x
        const t = timeOf(x.cite)
        return t ? { ...x, timeStart: t } : x
      }
      out.persist = out.persist.map(stamp)
      out.quote = out.quote.map(stamp)
    }
  } catch { /* 打时间戳失败不影响注入 */ }

  // ★ R0：只许**挑**，不许**编** —— 出处必须是真实候选之一，否则丢弃。
  //   parsePlan 只能查"cite 非空"；"cite 是否真实存在"必须在这里查（只有这里有 candidates）。
  //
  // ★★ 2026-10-04 修复（"编排器每次都被判空手"的真因）★★
  //   原实现是**纯字符串全等**：`known.has(String(x.cite))`。
  //   实测（node 直跑 core.js，8 种模型常见写法）：
  //     ✓保留 原样              "dsh:session-bbbb#turn=7"
  //     ✗丢弃 去 dsh: 前缀       "session-bbbb#turn=7"
  //     ✗丢弃 去 #turn          "dsh:session-bbbb"
  //     ✗丢弃 方括号包裹          "[dsh:session-bbbb#turn=7]"
  //     ✗丢弃 序号               "1"
  //     ✗丢弃 完整 session_id     "dsh:session-bbbb6d38-...#turn=7"
  //     ✗丢弃 尾部空格            "dsh:session-bbbb#turn=7 "
  //     ✗丢弃 turn 写 T          "dsh:session-bbbb#T=7"
  //   ⇒ **8 种里只有 1 种通过**。模型稍一改写就被判"编造"全丢
  //     ⇒ out.persist 归零 ⇒ 触发 R2 兜底 ⇒ **记录里 4/4 全是"空手兜底"**，
  //     模型那 4.4–6.7 秒的真实判断**一条都没进上下文**。
  //
  //   修法：**保留防编造的用意，改成"按 (会话, 轮次) 双键比对"** ——
  //     把 cite 拆成 `{sid, turn}` 两个键分别比：
  //       · sid 取 `session-` 后面那段十六进制/短横线（含 UUID 全量或短前缀）
  //       · turn 取 `#turn=N` 的数字
  //     判定：**turn 必须相等，且 sid 一方是另一方的前缀** ⇒ 视为同一个出处。
  //     这样"去 dsh: 前缀""去 #turn""方括号""完整 UUID""后缀空格""#T="全部能对上，
  //     而 `dsh:session-ffff#turn=999` 这类**真编造**照样被拒（sid 对不上）。
  const parseCite = (s) => {
    let t = String(s ?? '').trim()
      .replace(/^[\[\('"`]+|[\]\)'"`]+$/g, '')
      .replace(/\s+/g, '')
    const m = t.match(/#(?:turn|T)\s*=\s*(\d+)/i)
    const turn = m ? m[1] : ''
    let sid = t.replace(/#(?:turn|T)\s*=\s*\d+/i, '').trim()
    sid = sid.replace(/^dsh:/i, '').replace(/^session-/i, '').replace(/-+$/, '')
    // ★ 2026-10-15：sid **只取开头那段合法 token**。
    //   病根：旧版把 `88eb] [用户] <memory_recall mode="digest" …` 整段当作 sid，
    //        而下面的"前缀相等"规则用 `k.sid.startsWith(o.sid)` ⇒ 只要开头贴着真 sid
    //        就判同源 ⇒ **一段垃圾也能当出处**（实测 decisions.jsonl 900 条出处里 145 条是脏的）。
    //   修法：先按合法字符集截出 sid；再在 sameSource 里要求 cite **整串形态合法**。
    const ms = sid.match(/^[0-9a-f][0-9a-f-]{3,40}/i)
    sid = ms ? ms[0].replace(/-+$/, '').toLowerCase() : ''
    return { sid, turn }
  }

  // 出处必须是**出处本身**，不是一段话。
  // 允许模型常见写法（去 dsh: / 去 session- / 方括号 / 完整 UUID / #T= / 尾空格 / 带 seq= / 带 @时间），
  // 但**整串之外不许再有别的字**。
  const CITE_SHAPE = /^\[?\s*(?:dsh:)?(?:session-)?[0-9a-f][0-9a-f-]{3,40}(?:#(?:turn|T)\s*=\s*\d+)?(?:\s+seq=\d+\.\.\d+)?(?:\s*@[\d\-: ]{1,24})?\s*\]?$/i
  const isCiteShaped = (s) => CITE_SHAPE.test(String(s ?? '').trim())
  // ★ 2026-10-15（第二轮）：合法**开头**（后面允许跟注解）——见下方 resolveCite 的用法说明。
  const CITE_HEAD = /^\s*\[?\s*(?:dsh:)?(?:session-)?[0-9a-f][0-9a-f-]{3,40}(?:#(?:turn|T)\s*=\s*\d+)?/i

  const known = new Set()
  const knownKeys = []
  for (const c of (candidates || [])) {
    const cite = c && (c.cite || c.session_id)
    if (!cite) continue
    const raw = String(cite)
    known.add(raw)
    const k = parseCite(raw)
    if (k.sid || k.turn) knownKeys.push(k)
  }

  // 某个 cite 是否指向**真实候选**（按 (会话, 轮次) 双键比对）
  const matchesKnown = (cite) => {
    const k = parseCite(cite)
    if (!k.sid && !k.turn) return false                   // 空 cite ⇒ 拒
    for (const o of knownKeys) {
      // ② turn 必须相等（两边都有才比）
      if (k.turn && o.turn && k.turn !== o.turn) continue
      // ③ sid：一方是另一方的前缀（短 sid 是 UUID 的截断版）
      if (k.sid && o.sid) {
        if (o.sid.startsWith(k.sid) || k.sid.startsWith(o.sid)) return true
        continue
      }
      // ④ 只有一边有 sid：另一边若 turn 也缺失则不算（避免"1"这种序号命中）
      if ((k.sid && !o.sid) || (!k.sid && o.sid)) continue
    }
    return false
  }

  /**
   * 把一个 cite 归到"合法且真实存在"的出处；归不了 ⇒ null。
   *
   * ★★ 2026-10-15（第二轮根治）★★
   * 病根（实测 2026-10-15 18:32 真实一条）：上一轮改成"整串形态不合法就丢"，
   *   而模型很爱写「出处 + 注解」（`dsh:x#turn=3 —— 用户当时说…`）⇒ **6 条合法挑选全被丢**
   *   ⇒ 触发 `R2 兜底` ⇒ 代码把**检索最高分的一条冒充成「必须常驻的事实」**注入主模型
   *   （那条实际是"stream idle timeout"的助手思考，与用户问的"重启重试测验"无关）。
   * 修法：**先规范化、再判真伪**
   *   ① 原样命中已知候选 ⇒ 用它；
   *   ② 整串形态合法 ⇒ 再按 (sid,turn) 比对；
   *   ③ 形态不合法但**开头**是合法出处 ⇒ 截出开头那段（去掉注解）再比对 —— 命中就把出处
   *      **替换成干净的那段**（可回溯），既不丢模型的真实挑选，也不会把整段注解当出处注入；
   *   ④ 连开头都不像出处 ⇒ 判编造，丢弃。
   */
  const resolveCite = (rawCite) => {
    const s = String(rawCite ?? '').trim()
    if (!s) return null
    if (known.has(s)) return s                             // ①
    if (isCiteShaped(s)) return matchesKnown(s) ? s : null  // ②
    const h = s.match(CITE_HEAD)                           // ③
    if (h) {
      let cand = h[0].trim()
      // ★ 2026-10-15（第四轮）修回归：**方括号要配对**。
      //   实证（10:39:40Z 真实注入块）：出处渲染成 `[dsh:session-cccc#turn=11`（缺 `]`）——
      //   CITE_HEAD 吃掉了开头的 `[`，却没人去补结尾的 `]` ⇒ 出处串不完整。
      const rest = s.slice(h[0].length)
      if (cand.startsWith('[') && !cand.endsWith(']') && /^\s*\]/.test(rest)) cand += ']'
      if (known.has(cand) || matchesKnown(cand)) return cand
    }
    return null                                            // ④
  }

  if (knownKeys.length > 0) {
    const fixPick = (arr) => {
      const kept = []
      let dropped = 0
      let normalized = 0
      for (const x of (Array.isArray(arr) ? arr : [])) {
        if (!x || typeof x !== 'object') continue
        const c = resolveCite(x.cite)
        if (!c) {
          dropped += 1
          if (out.rejectedCites.length < 5) out.rejectedCites.push(String(x.cite ?? '').slice(0, 90))
          continue
        }
        if (c !== String(x.cite ?? '').trim()) {
          normalized += 1
          // ★ 2026-10-15（第四轮）修回归：**规范化之后必须补盖时间戳**。
          //   盖章发生在 R0 之前、用的是模型原文（带注解 ⇒ 查不到表）⇒ 规范化后的条目
          //   一个时间都没有 ⇒ 渲染时既不标〔距今 X〕、也不出"先 reopen 复核再引用"的时效提示。
          //   实证：10:39:40Z 那条注入块全文零个〔距今〕，而内容正是会过期的"源码≠运行副本"。
          const t = x.timeStart ?? timeOf(x.cite, c)
          kept.push(t ? { ...x, cite: c, timeStart: t } : { ...x, cite: c })
        } else {
          const t = x.timeStart ?? timeOf(c)
          kept.push(t ? { ...x, timeStart: t } : x)
        }
      }
      return { kept, dropped, normalized }
    }
    const p = fixPick(out.persist)
    const q = fixPick(out.quote)
    out.persist = p.kept
    out.quote = q.kept
    const dropped = p.dropped + q.dropped
    const normalized = p.normalized + q.normalized
    if (dropped > 0) out.flags.push(`R0:丢弃${dropped}条无出处/编造项`)
    if (normalized > 0) out.flags.push(`R0:规范化${normalized}条出处(原串带注解)`)
  }

  if (forceSignal) {
    // 回看信号时：把候选**按相关度**排序后强制取前若干条。
    // 注意：信号词在"用户这句话"里，不一定在记忆文本里；所以不能只按关键词匹配记忆。
    const kws = (cfg.forceOnKeywords || []).filter((k) => k && String(userText || '').includes(k))
    const scored = (candidates || []).map((c, i) => {
      const t = String(c.text || '')
      let score = 0
      for (const k of kws) if (t.includes(k)) score += 10
      // 与用户这句话有字面重叠的加分（粗糙但有效）
      for (const tok of String(userText || '').replace(/[，。！？、\s]/g, ' ').split(' ')) {
        if (tok.length >= 2 && t.includes(tok)) score += 3
      }
      return { c, i, score }
    })
    scored.sort((a, b) => (b.score - a.score) || (a.i - b.i))
    const forced = scored.slice(0, Math.max(1, cfg.quoteMax || 4))
    let added = 0
    for (const c of forced) {
      const cite = c.cite || c.session_id
      if (!cite) continue
      const dup = out.persist.some((p) => p.cite === cite) || out.quote.some((q) => q.cite === cite)
      if (dup) continue
      const fact = String(c.text || '').replace(/\s+/g, ' ').slice(0, 200)
      if (!fact) continue
      // ★ R1 的语义是"**强制常驻**" —— 必须进 persist（渲染成"必须常驻的事实"），
      //   进 quote 只是"要点"，达不到"编排器否决不了"的效果。
      //   第一条进 persist（保证渲染出"必须常驻"段），其余进 quote 补足。
      if (added === 0) out.persist.push({ fact, cite, forced: true })
      else out.quote.push({ fact, cite, forced: true })
      added++
    }
    if (added > 0 && !out.flags.includes('R1:强制常驻(回看信号)')) out.flags.push('R1:强制常驻(回看信号)')
  }

  if (out.persist.length === 0 && out.quote.length === 0) {
    // ★★ 2026-10-15（第二轮根治）：**不再把"检索最高分的一条"冒充成「必须常驻的事实」** ★★
    //
    // 旧行为（实测 80/307 条历史记录走过这条路）：
    //   编排器判"候选里没有相关的" ⇒ 代码却硬塞候选第一条，并渲染在
    //   【必须常驻的事实】标题下 —— 那是一条**凭代码编出来的断言**，
    //   模型会照着它当"用户要求我记住的事实"用。
    //
    // 实证（2026-10-15T10:32Z，重启后第一条真实记录）：
    //   `flags=['R0:丢弃6条无出处/编造项','R2:编排器空手⇒兜底第一条']`，
    //   用户问「已经重启进行重试测验」，注入的"必须常驻的事实"却是
    //   「[助手·思考] stream idle timeout … wire 日志计数 0」——
    //   与提问无关，却带着最强的断言语气。
    //
    // 修法：标注 `empty`，由 host.js 返回 `status:'empty'` ⇒ 调用方**回落成普通召回**：
    //   老路注入块自带 score / 出处 / "别当常识、要用就引 session/turn" 的提醒，
    //   既不冒充"必须常驻"，也不会让主模型什么都看不到。
    out.empty = true
    out.flags.push('R2:编排器空手⇒不冒充事实，回落普通召回')
  }
  return out
}

// ---------------------------------------------------------------------------
// 注入块渲染
// ---------------------------------------------------------------------------

export function renderBlock(plan, { query, model, ms }) {
  // 防御：本函数在每一轮对话里跑，绝不能因 plan 缺字段而抛
  const p = plan || {}
  const persist = Array.isArray(p.persist) ? p.persist : []
  const quote = Array.isArray(p.quote) ? p.quote : []
  const drop = Array.isArray(p.drop) ? p.drop : []
  const order = Array.isArray(p.order) && p.order.length ? p.order : ['persist', 'quote', 'recent', 'task']
  const flags = Array.isArray(p.flags) ? p.flags : []

  const L = []
  L.push(`<attention_orchestration source="dsh-memory-plugin" model="${String(model || '?')}">`)
  L.push(`query: ${String(query || '').slice(0, 120)}`)
  L.push(`编排耗时: ${ms}ms｜常驻 ${persist.length}｜引用 ${quote.length}｜丢弃 ${drop.length}`)
  if (flags.length) L.push(`硬规则: ${flags.join(' / ')}`)
  if (p.reason) L.push(`编排理由: ${p.reason}`)
  L.push('')
  for (const o of order) {
    if (o === 'persist' && persist.length) {
      L.push('【必须常驻的事实】')
      for (const x of persist) L.push(`· ${x.fact}${ageTag(x)}\n  出处：${x.cite}`)
      L.push('')
    }
    if (o === 'quote' && quote.length) {
      L.push('【原文要点（要引用时用这个）】')
      for (const x of quote) L.push(`· ${x.fact}${ageTag(x)}\n  出处：${x.cite}`)
      L.push('')
    }
  }
  // ★ 2026-10-04：过期提醒（用户点名）
  //   起因：编排器注入「重启后实测 read_image 失败，未声明图像输入」，
  //        而那条**当天已修好** ⇒ 把过期事实当现况喂进上下文。
  //   做法：只要**有任何一条**注入了时间，就统一加一句"先 reopen 再引用"。
  const hasAge = [...persist, ...quote].some((x) => x && (x.timeStart != null || x.ageMs != null))
  if (hasAge) {
    L.push('⚠ **时效提示**：上面标了「距今 …」的条目是**历史记录**，不是现况。')
    L.push('   凡涉及"能不能用/通不通/有没有修好"这类**状态判断**，')
    L.push('   **必须先 reopen 复核（读盘/跑命令）再引用**，不许直接当现状回答。')
    L.push('   （实证：曾有"read_image 未声明图像输入"的旧结论被当现况注入，而它当天已修好。）')
    L.push('')
  }
  L.push('（以上由注意力编排器挑选；未列出者本轮不注入。引用请带出处。）')
  L.push('</attention_orchestration>')
  return L.join('\n')
}

/**
 * 把 planItem.timeStart（毫秒）算成「距今 X」的短标签。
 * 没有时间戳 ⇒ 返回空串（不硬编）。
 */
function ageTag(x) {
  try {
    const t = x && (x.timeStart ?? x.ageMs)
    if (!t) return ''
    const ms = Date.now() - Number(t)
    if (!Number.isFinite(ms) || ms < 0) return ''
    const min = Math.floor(ms / 60000)
    let human
    if (min < 1) human = '刚刚'
    else if (min < 60) human = `${min} 分钟前`
    else if (min < 60 * 24) human = `${Math.floor(min / 60)} 小时前`
    else human = `${Math.floor(min / 1440)} 天前`
    return `　〔距今 ${human}〕`
  } catch { return '' }
}

// ---------------------------------------------------------------------------
// 回声识别：哪些文本是"本插件自己注入过的块"
// ---------------------------------------------------------------------------
/**
 * 病根（2026-10-15 实测）：`lib/index.js` 的过滤串写成 `'<memory_recall source='`，
 * 而真块形态是 `<memory_recall mode="digest" source="dsh-memory-plugin" …>` ——
 * `mode=` 在 `source=` 之前 ⇒ **永不命中** ⇒ "防注入块滚雪球"的过滤从未生效
 * （记忆库里有 2686 条 `<memory_recall` 注入块自证）。
 * 修法：按**标签**判定（前缀匹配，允许前带 `[用户] ` 这类角色前缀）。
 */
export const INJECT_TAGS = [
  '<memory_recall', '<attention_orchestration', '<goal_round', '<memory-orchestrator-alert',
]

export function isOwnInjectionText(raw) {
  const t = String(raw ?? '').replace(/^\s*\[[^\]\n]{1,16}\]\s*/, '').trimStart()
  if (!t) return false
  return INJECT_TAGS.some((s) => t.startsWith(s))
}

// ---------------------------------------------------------------------------
// 留痕与统计
// ---------------------------------------------------------------------------

export function logDecision(baseDir, rec) {
  try {
    const dir = join(baseDir, 'orchestrator')
    mkdirSync(dir, { recursive: true })
    appendFileSync(join(dir, 'decisions.jsonl'),
      JSON.stringify({ ts: new Date().toISOString(), ...rec }) + '\n', 'utf8')
  } catch { /* 留痕失败不影响对话 */ }
}

export function recentDecisions(baseDir, n = 20) {
  try {
    const p = join(baseDir, 'orchestrator', 'decisions.jsonl')
    if (!existsSync(p)) return []
    const lines = readFileSync(p, 'utf8').trim().split('\n').filter(Boolean)
    return lines.slice(-n).map((l) => { try { return JSON.parse(l) } catch { return null } }).filter(Boolean)
  } catch { return [] }
}

export function stats(baseDir) {
  const cfg = loadConfig(baseDir)
  const rec = recentDecisions(baseDir, 50)
  return {
    enabled: !!cfg.enabled,
    modelMode: cfg.modelMode,
    customProvider: cfg.customProvider,
    customModel: cfg.customModel,
    everyNthTurn: cfg.everyNthTurn,
    total: rec.length,
    ok: rec.filter((r) => r.outcome === 'ok').length,
    failed: rec.filter((r) => r.outcome !== 'ok').length,
    last: rec[rec.length - 1] || null,
  }
}

// ---------------------------------------------------------------------------
// 主流程（被 index.js 调用；llm 由调用方注入，便于测试）
// ---------------------------------------------------------------------------

/**
 * 跑一次编排。
 * @param {object} o
 * @param {object} o.cfg           配置
 * @param {string} o.userText      用户这一轮的话
 * @param {Array}  o.recentTurns   最近 N 轮 [{role,text}]
 * @param {Array}  o.candidates    候选记忆 [{text,cite,...}]
 * @param {string} o.taskState     任务状态文字（可空）
 * @param {function} o.callModel   async ({messages, provider, model}) => string
 * @param {string} o.resolvedProvider
 * @param {string} o.resolvedModel
 * @param {string} o.baseDir       留痕目录
 * @returns {Promise<{ok,plan?,block?,ms,why,outcome,error?}>}
 */
export async function orchestrate(o) {
  const t0 = Date.now()
  // ★ 2026-10-15(3)：台账记下**哪个会话** —— 起因：10:34:44 连续两条一模一样的
  //   `call-failed`（ms=0），分不出是哪个会话/哪个 agent 出的问题，只能靠猜。
  const sess = String(o.sessionKey ?? '')
  const forceSignal = hasForceSignal(o.userText, o.cfg)
  const req = buildRequest({ ...o, forceSignal })
  const messages = [
    { role: 'system', content: [{ type: 'text', text: SYSTEM_PROMPT }] },
    { role: 'user', content: [{ type: 'text', text: req }] },
  ]
  let raw = ''
  try {
    raw = await o.callModel({
      messages,
      provider: o.resolvedProvider,
      model: o.resolvedModel,
      timeoutMs: o.cfg.timeoutMs,
    })
  } catch (e) {
    const ms = Date.now() - t0
    logDecision(o.baseDir, {
    session: sess, outcome: 'call-failed', error: String(e?.message ?? e).slice(0, 200), ms,
      model: o.resolvedModel, candidates: (o.candidates || []).length, forceSignal,
    })
    return { ok: false, outcome: 'call-failed', error: String(e?.message ?? e), ms, why: 'call-failed' }
  }

  const parsed = parsePlan(raw, { cfg: o.cfg })
  if (!parsed.ok) {
    const ms = Date.now() - t0
    logDecision(o.baseDir, {
    session: sess, outcome: 'parse-failed', error: parsed.error, ms,
      model: o.resolvedModel, rawHead: String(raw).slice(0, 200),
      candidates: (o.candidates || []).length, forceSignal,
    })
    return { ok: false, outcome: 'parse-failed', error: parsed.error, ms, why: 'parse-failed' }
  }

  const plan = enforceHardRules(parsed.plan, {
    cfg: o.cfg, candidates: o.candidates, forceSignal, userText: o.userText,
  })
  const ms = Date.now() - t0
  // ★ 2026-10-15（第二轮）：编排器**空手**（判"没有相关的"）⇒ 不出块、由调用方回落普通召回。
  //   `outcome` 记成 `empty`（不是 ok），这样台账里一眼能看出"这轮没注入编排块、也没冒充事实"。
  if (plan.empty) {
    logDecision(o.baseDir, {
      session: sess, outcome: 'empty', ms, model: o.resolvedModel, forceSignal,
      candidates: (o.candidates || []).length,
      persist: 0, quote: 0, drop: plan.drop.length,
      flags: plan.flags, reason: plan.reason,
      rejectedCites: plan.rejectedCites,
      dropHead: plan.drop.slice(0, 5).map((s) => String(s).slice(0, 80)),
    })
    return { ok: true, outcome: 'empty', plan, block: '', ms, why: 'empty' }
  }
  const block = renderBlock(plan, { query: o.userText, model: o.resolvedModel, ms })
  logDecision(o.baseDir, {
    session: sess, outcome: 'ok', ms, model: o.resolvedModel, forceSignal,
    candidates: (o.candidates || []).length,
    persist: plan.persist.length, quote: plan.quote.length, drop: plan.drop.length,
    flags: plan.flags, reason: plan.reason,
    cites: [...plan.persist, ...plan.quote].map((x) => x.cite).slice(0, 12),
    // ★ 留痕：被 R0 丢掉的出处（有界）—— 下次再触发 R2 时，能直接看到模型写了什么
    rejectedCites: plan.rejectedCites,
  })
  return { ok: true, outcome: 'ok', plan, block, ms, why: 'ok' }
}
