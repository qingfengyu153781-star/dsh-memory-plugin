/**
 * 编排器回归用例（2026-10-15 立）—— 把「注意力提示不正确」的每条根因变成可重跑判据。
 *
 * 跑法：node test/orch_regress.mjs        （在 plugin-src/dsh-memory-plugin 下）
 *
 * 覆盖：
 *   A 四态契约      disabled / gated / failed / ok（"关掉"与"坏了"必须可区分）
 *   B 关闭不发告警   3 连败后关掉开关、并把时钟拨过 30 分钟冷却期 ⇒ 仍不得发告警
 *   C 告警按会话隔离 甲会话的失败不得在乙会话里弹
 *   D 告警话术     不得再写"没有在给上下文注入记忆"（那与事实相反）
 *   E 回声过滤     注入块（含带角色前缀的）必须被认出，正常文本不得误杀
 *   F 出处校验     脏出处（出处后挂一整段话）必须拒；合法出处必须留
 *   G 块头模型名   写真实模型名，不许是 "?"
 *   H 单一真身     装机副本与源码副本 md5 必须一致（防"改一份不生效"）
 */
import { readFileSync, mkdirSync, writeFileSync, existsSync } from 'node:fs'
// ★ 2026-10-16 修：execFileSync 来自 node:child_process（旧版误从 node:fs 取 ⇒
//   L 段"测试不得污染生产台账"一直抛 ReferenceError 被 catch 吞掉 ⇒ **假过**）。
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { tmpdir, homedir } from 'node:os'

const here = dirname(fileURLToPath(import.meta.url))
const ROOT = join(here, '..')
const INSTALLED = process.env.ORCH_PLUGIN_DIR
  || join(homedir(), '.dsh', 'profiles', 'desktop', 'node_modules', 'dsh-memory-plugin')

let pass = 0, fail = 0
const ok = (name) => { pass += 1; console.log(`  ✓ ${name}`) }
const bad = (name, detail) => { fail += 1; console.log(`  ✗ ${name}\n      ${detail}`) }
const check = (name, cond, detail = '') => (cond ? ok(name) : bad(name, detail))
const md5 = (p) => createHash('md5').update(readFileSync(p)).digest('hex')

// ── 载入被测模块（源码副本 = 装机副本，H 会验）
const core = await import(pathToFileURL(join(ROOT, 'orchestrator', 'core.js')).href)
const host = await import(pathToFileURL(join(ROOT, 'orchestrator', 'host.js')).href)
const { enforceHardRules, isOwnInjectionText, renderBlock } = core
const { orchestrateTurn, failureNotice, failureStats } = host

// ── 临时 baseDir（配置只写这里，绝不碰真实 baseDir）
const TMP = join(tmpdir(), 'orch_regress_' + Date.now())
mkdirSync(join(TMP, 'orchestrator'), { recursive: true })
const setCfg = (o) => writeFileSync(join(TMP, 'orchestrator', 'config.json'),
  JSON.stringify({ modelMode: 'follow', everyNthTurn: 1, recentTurns: 2, timeoutMs: 5000, ...o }), 'utf8')

const parsed = { hits: [{ text: '候选事实', cite: 'dsh:session-abc#turn=1', session_id: 'session-abc', score: 1, time_start: 1791134411526 }] }
const payload = { turn: 1, agent: { id: 'session-A' }, messages: [] }
const planJson = JSON.stringify({ persist: [{ fact: '事实', cite: 'dsh:session-abc#turn=1' }], quote: [], drop: [], order: [], reason: 'r' })
const call = (over = {}) => orchestrateTurn({
  ctx: {}, payload, decision: { messages: [] }, query: '问', parsed, isSubagent: false,
  callModel: async () => planJson, sessionKey: 'session-A', baseDir: TMP, ...over,
})

console.log('\n【A 四态契约】')
setCfg({ enabled: false })
let r = await call()
check('关闭 ⇒ status=disabled（不是 null、不是 failed）', r && r.status === 'disabled', JSON.stringify(r))

setCfg({ enabled: true })
r = await call({ isSubagent: true })
check('子智能体 ⇒ status=gated', r && r.status === 'gated', JSON.stringify(r))

r = await call({ callModel: async () => { throw new Error('Connection error.') } })
check('调用抛错 ⇒ status=failed 且带 error', r && r.status === 'failed' && /Connection/.test(r.error || ''), JSON.stringify(r))

r = await call()
check('正常 ⇒ status=ok 且有 block', r && r.status === 'ok' && typeof r.block === 'string' && r.block.length > 0, JSON.stringify(r && r.status))

console.log('\n【B 关闭不发告警（旧 bug：关了也弹"连续失败"）】')
setCfg({ enabled: true })
let last = null
for (let i = 0; i < 3; i += 1) last = await call({ callModel: async () => { throw new Error('Connection error.') } })
check('3 连败 ⇒ 第 3 次带回告警', last && last.status === 'failed' && !!last.notice, JSON.stringify(last && { s: last.status, n: !!last.notice }))
const realNow = Date.now
Date.now = () => realNow() + 31 * 60 * 1000            // 拨过 30 分钟冷却期
setCfg({ enabled: false })
r = await call()
Date.now = realNow
check('拨过冷却期 + 开关已关 ⇒ status=disabled 且**无告警**', r && r.status === 'disabled' && !r.notice, JSON.stringify(r))

console.log('\n【C 告警按会话隔离】')
check('甲会话 streak=3', (failureStats('session-A').streak || 0) === 3, JSON.stringify(failureStats('session-A')))
check('乙会话不受影响（streak=0）', (failureStats('session-B').streak || 0) === 0, JSON.stringify(failureStats('session-B')))
check('乙会话取告警 ⇒ null', failureNotice('session-B') === null, String(failureNotice('session-B')))

console.log('\n【D 告警话术不得与事实相反】')
setCfg({ enabled: true })
const noticeLike = JSON.stringify(last && last.notice || '')
check('不得写"没有在给上下文注入记忆"', !noticeLike.includes('没有在给上下文注入记忆'), noticeLike.slice(0, 120))
check('要说明"回落成普通召回"', noticeLike.includes('回落'), noticeLike.slice(0, 160))

console.log('\n【E 回声过滤】')
check('认出 <memory_recall …>', isOwnInjectionText('<memory_recall mode="digest" source="dsh-memory-plugin">') === true)
check('认出 <attention_orchestration …>', isOwnInjectionText('<attention_orchestration source="dsh-memory-plugin">') === true)
check('认出告警块', isOwnInjectionText('<memory-orchestrator-alert>\n⚠️\n</memory-orchestrator-alert>') === true)
check('认出带角色前缀的块', isOwnInjectionText('[用户] <memory_recall mode="digest" source="x">') === true)
check('不误杀正常文本', isOwnInjectionText('用户问的是 lingshu 仓库有哪些功能') === false)

console.log('\n【F 出处校验】')
const cfg = { persistMax: 4, quoteMax: 4, forceOnKeywords: ['之前'] }
const candidates = [
  { text: 't1', cite: 'dsh:session-aaaa#turn=10', timeStart: 1 },
  { text: 't2', cite: 'dsh:session-bbbb#turn=7', timeStart: 1 },
]
const runPicks = (picks) => enforceHardRules(
  { persist: picks, quote: [], drop: [], order: [], reason: '' },
  { cfg, candidates, forceSignal: false, userText: 'x' },
)
const keptCite = (cite) => {
  const r = runPicks([{ fact: 'f', cite }])
  return r.persist.length ? String(r.persist[0].cite) : null
}
const DIRTY = '[dsh:session-aaaa#turn=10] [用户] <memory_recall mode="digest" source="dsh-memory-plugin"> ' + 'x'.repeat(200)
// ★ 2026-10-15（第二轮）语义变更（**改代码，也改断言**）：
//   旧断言是"出处后挂一整段话 ⇒ 拒"。实测证明那样会把**合法的挑选整条丢掉**
//   （18:32 那条 R0 丢 6/6 ⇒ 触发 R2 兜底 ⇒ 冒充"必须常驻的事实"）。
//   现契约：**先规范化再判真伪** —— 开头是合法出处就截出干净的那段**保留**（出处干净、可回溯），
//   开头就不像出处才拒。所以这里断言"保留 + 出处被规范成干净串"，而不是"拒"。
const norm1 = keptCite(DIRTY)
check('带注解的出处 ⇒ 规范化后保留（不丢合法挑选）', norm1 !== null, String(norm1))
check('规范化后的出处是干净串（注解不进上下文）',
  norm1 !== null && /^\[?(?:dsh:)?(?:session-)?[0-9a-f][0-9a-f-]{3,40}(#(?:turn|T)=\d+)?\]?$/.test(norm1) && !norm1.includes('memory_recall'),
  String(norm1))
check('拒真编造', keptCite('dsh:session-dead#turn=10') === null)
check('拒轮次不符', keptCite('dsh:session-aaaa#turn=99') === null)
check('拒"连开头都不像出处"（如序号 1）', keptCite('1') === null)
check('拒纯文本（无出处）', keptCite('用户当时说的是别的') === null)
check('留合法出处', keptCite('dsh:session-aaaa#turn=10') === 'dsh:session-aaaa#turn=10')
check('留方括号合法出处', keptCite('[dsh:session-bbbb#turn=7]') !== null)
check('留 #T= 写法', keptCite('dsh:session-bbbb#T=7') !== null)

console.log('\n【F2 空手不得冒充「必须常驻的事实」（第二轮根治）】')
// 实证：2026-10-15T10:32Z 那条 —— R0 丢 6/6 ⇒ R2 把检索最高分的一条塞进 persist，
// 以【必须常驻的事实】标题注入（内容是"stream idle timeout"的助手思考，与提问无关）。
const junkAll = enforceHardRules(
  { persist: [{ fact: 'a', cite: '1' }, { fact: 'b', cite: 'nope' }], quote: [], drop: [], order: [], reason: '' },
  { cfg, candidates, forceSignal: false, userText: 'x' },
)
check('全垃圾出处 ⇒ persist 归零（不再硬塞候选第一条）', junkAll.persist.length === 0, JSON.stringify(junkAll.persist))
check('全垃圾出处 ⇒ 标 empty', junkAll.empty === true, String(junkAll.empty))
check('全垃圾出处 ⇒ flags 说明"不冒充事实"',
  junkAll.flags.some((f) => f.includes('不冒充事实')), JSON.stringify(junkAll.flags))
check('被丢的出处留痕（可查模型写了什么）',
  Array.isArray(junkAll.rejectedCites) && junkAll.rejectedCites.length === 2, JSON.stringify(junkAll.rejectedCites))
// 端到端：host 必须把它当 empty 交回（不是 ok+block）
setCfg({ enabled: true, everyNthTurn: 1 })
r = await call({ callModel: async () => JSON.stringify({ persist: [{ fact: 'f', cite: '1' }], quote: [], drop: [], order: [], reason: '' }) })
check('host：空手 ⇒ status=empty（不出块）', r && r.status === 'empty', JSON.stringify(r && { s: r.status, b: !!r.block }))
check('host：空手不算失败（不发告警）', r && !r.notice, JSON.stringify(r && r.notice))
check('host：空手不产生含「必须常驻」的块', !r || !r.block, String(r && r.block).slice(0, 60))

console.log('\n【F3 规范化后必须补回时间戳 + 方括号配对（第四轮修回归）】')
// 实证：2026-10-15T10:39:40Z 真实注入块 —— `R0:规范化5条出处`，但全文**零个〔距今〕**，
// 而注入内容正是「源码≠运行副本 / 需再重启」这类**会过期**的状态断言。
// 根因：盖章发生在规范化之前、用模型原文（带注解）查表 ⇒ 查不到 ⇒ 时间戳为空。
const annotated = runPicks([{ fact: 'f', cite: '[dsh:session-aaaa#turn=10] —— 用户当时说…' }])
const p0 = annotated.persist[0]
check('规范化后出处方括号配对（不再缺 ]）', !!p0 && p0.cite === '[dsh:session-aaaa#turn=10]', String(p0 && p0.cite))
check('规范化后仍带时间戳', !!p0 && p0.timeStart != null, String(p0 && p0.timeStart))
const blk2 = renderBlock(annotated, { query: 'q', model: 'm', ms: 1 })
check('渲染出〔距今 X〕', blk2.includes('〔距今'), blk2.split('\n').slice(0, 4).join(' ｜ '))
check('渲染出时效提示（先 reopen 复核再引用）', blk2.includes('先 reopen 复核'), '')

console.log('\n【G 块头模型名】')
const blk = renderBlock({ persist: [{ fact: 'f', cite: 'dsh:x#turn=1' }], quote: [], drop: [], order: [], reason: '' },
  { query: 'q', model: 'glm-5.3', ms: 12 })
check('写真实模型名', /model="glm-5\.3"/.test(blk), blk.split('\n')[0])

console.log('\n【I llm 服务取法（第三轮根治：不再 ctx.get 碰运气）】')
// 实证：2026-10-15T10:34:44.426Z / .428Z 台账连续两条
//   `call-failed / llm service unavailable / ms=0` ⇒ `ctx.get('llm')` 取不到服务。
// 正确写法见 dsh-agent-loop/lib/index.js:1189 `this.loopCtx.llm.prepareCall(...)` ⇒ **ctx.llm**。
const pluginEntry = await import(pathToFileURL(join(ROOT, 'lib', 'index.js')).href)
const { llmOf, llmDiag } = pluginEntry
check('llmOf 直接属性优先（ctx.llm）', !!llmOf({ llm: { stream() {} } }), 'ctx.llm')
check('llmOf 兼容老 runtime（ctx.get）', !!llmOf({ get: (k) => (k === 'llm' ? { stream() {} } : null) }), 'ctx.get')
check('llmOf 都没有 ⇒ null（判失败，不硬撑）', llmOf({}) === null, String(llmOf({})))
check('llmOf 拒绝"有名字但没 stream"的伪服务',
  llmOf({ llm: {} }) === null, String(llmOf({ llm: {} })))
check('失败诊断串带上"看到了什么"', /ctx\.llm=/.test(llmDiag({})), llmDiag({}))

console.log('\n【J 跳过也要留痕（第五轮：可见性）】')
// 起因：用户说「…」（4 字 < 门槛 6）+ 无 goal ⇒ 有意跳过召回，但台账一片空白
// ⇒ "有意跳过"与"插件根本没跑"分不出来。修法：跳过写 `outcome:'skipped'` 进同一本台账。
{
  const { logSkip } = host
  const ledger = join(TMP, 'orchestrator', 'decisions.jsonl')
  const readLedger = () => {
    try {
      return readFileSync(ledger, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l))
    } catch { return [] }
  }
  const n0 = readLedger().length
  const wrote = logSkip({
    baseDir: TMP, sessionKey: 'session-J', why: 'small-talk-no-goal',
    enabled: true, extra: { query: '已经重启' },
  })
  const rows = readLedger()
  const last = rows[rows.length - 1] || {}
  check('开启时：跳过会写台账', wrote === true && rows.length === n0 + 1, `n0=${n0} n1=${rows.length}`)
  check('跳过记录 outcome=skipped', last.outcome === 'skipped', String(last.outcome))
  check('跳过记录带 why 与 session',
    last.why === 'small-talk-no-goal' && last.session === 'session-J', JSON.stringify(last))
  const n1 = readLedger().length
  logSkip({ baseDir: TMP, sessionKey: 'session-J', why: 'x', enabled: false })
  check('未开启时：不写（那是用户设置，不是跳过）', readLedger().length === n1, String(readLedger().length))
  logSkip({ baseDir: TMP, sessionKey: 'session-J', why: 'x', enabled: true, extra: { isSubagent: true } })
  check('子智能体轮次：不写（量大，会淹掉真信号）', readLedger().length === n1, String(readLedger().length))
}

console.log('\n【K 判官要拿到判据、陈述轮不回顾（第六轮）】')
// 实证（活注入块）：用户说「…」⇒ 检索按逐字相同命中 10-04 会话里同一句话的
// **元记录**（"助手观察到注入块已生效""没有出现 goal-anchor 块"…），被挑成【必须常驻的事实】。
const { isAckOnly, looksLikeRequest, buildRequest, ageText: ageT } = core
check('isAckOnly：报告已完成状态 ⇒ 真', isAckOnly('我已经重启了，') === true, '我已经重启了，')
check('isAckOnly：短状态句 ⇒ 真', isAckOnly('已经重启了') === true, '已经重启了')
check('isAckOnly：带问号 ⇒ 假', isAckOnly('克隆下来了吗？') === false, '克隆下来了吗？')
check('isAckOnly：带请求动词 ⇒ 假', isAckOnly('继续检查') === false, '继续检查')
check('isAckOnly：长句不误杀', isAckOnly('把那个仓库拉下来单独放一个文件夹，然后再看一遍工程纪律') === false, '长句')
check('looksLikeRequest：问句 ⇒ 真', looksLikeRequest('他有什么功能？') === true, '')
check('looksLikeRequest：纯状态 ⇒ 假', looksLikeRequest('我已经重启了，') === false, '')

// 判官输入必须包含"能判相关性/时效"的事实（否则它只能按字面相似度猜）
const reqText = buildRequest({
  cfg: { persistMax: 4, quoteMax: 4 }, userText: '我已经重启了，', recentTurns: [],
  forceSignal: false, sessionKey: '<会话>-xxxx',
  candidates: [{ text: '打桩候选', cite: 'dsh:session-cccc#turn=12', timeStart: Date.now() - 11 * 86400000 }],
})
check('判官输入含"本轮环境"', reqText.includes('## 本轮环境'), '')
check('判官输入含当前会话（可辨"这是别的会话的记录"）', reqText.includes('<会话>'), '')
check('判官输入含候选距今时长', reqText.includes('〔距今 11 天前〕'), reqText.split('\n').slice(-7).join(' | '))
check('判官输入对陈述轮给出明确提示', reqText.includes('报告一个已完成的状态'), '')
check('判官输入写明"0 条是合法答案"', reqText.includes('0 条是合法答案'), '')
check('系统提示写明"同一句话在别处出现 ≠ 相关"', /同一句话在别处出现过/.test(core.SYSTEM_PROMPT), '')
check('ageText：11 天', ageT(Date.now() - 11 * 86400000) === '11 天前', ageT(Date.now() - 11 * 86400000))

console.log('\n【L 测试不得污染生产台账（第六轮）】')
// 实证：selfcheck/inject 走真 apply(ctx)，把 `selfcheck-session` 的失败/跳过写进**生产台账**
// ⇒ `orchestrate-status` 的"最近 20 轮"100% 被测试会话占满 ⇒ 误报 STATUS=broken。
// 修法：插件支持 `DSH_MEMORY_BASE`，测试写临时目录。这条判据 = 跑一次 selfcheck，看生产台账有没有长。
{
  const LEDGER = process.env.ORCH_LEDGER || join(ROOT, '..', '..', 'orchestrator', 'decisions.jsonl')
  const nRows = () => {
    try { return readFileSync(LEDGER, 'utf8').trim().split('\n').filter(Boolean).length } catch { return -1 }
  }
  // 只数「测试会话」行。生产台账同时被正在运行的 DSH 真实轮次写入，
  // 用总行数会误报（2026-10-21 实测 before=2288 after=2289，增长来自真实会话）。
  const testRows = () => {
    try {
      return readFileSync(LEDGER, 'utf8').trim().split('\n').filter(Boolean)
        .map((l) => { try { return JSON.parse(l) } catch { return null } })
        .filter((r) => r && /^(selfcheck|session-test)/.test(String(r.session || ''))).length
    } catch { return -1 }
  }
  const before = testRows()
  let err = ''
  try {
    const isolatedBase = join(tmpdir(), 'orch-selfcheck-base-' + Date.now())
    mkdirSync(isolatedBase, { recursive: true })
    execFileSync(process.execPath, [join(ROOT, 'test', 'selfcheck.mjs')],
      { env: { ...process.env, DSH_MEMORY_NO_INGEST: '1', DSH_MEMORY_BASE: isolatedBase }, stdio: 'pipe' })
  } catch (e) { err = String(e?.message || e).slice(0, 80) }
  const after = testRows()
  check('跑一次 selfcheck ⇒ 生产台账无测试会话新增', before >= 0 && after === before,
    `testRows before=${before} after=${after}${err ? ' childErr=' + err : ''}`)
}

console.log('\n【M 回声过滤：只按信息增量判，与"是否重复/句子长短"无关（第八轮根治）】')
// 用户 2026-10-15 否决了上一轮的"形状闸"（短+无问号 ⇒ 不回顾）：
//   「<用户原话已隐去：否决粗暴切割式判据>」
// 新判据 = 候选有没有 query 之外的信息增量；形状/重复次数一概不看。
{
  const { isEchoOnly, echoRatio, hasSubstantive, filterEchoCandidates } = core

  // ① 实证里那一条：逐字含 query + 全是元记录 ⇒ 回声
  const E1 = '用户本轮表示「我已经重启了」（与上一轮表述相同）'
  check('回声·逐字抄 query 且无具体内容 ⇒ 判为回声', isEchoOnly(E1, '我已经重启了，') === true,
    `ratio=${echoRatio(E1, '我已经重启了，').toFixed(2)}`)
  check('回声·该条确实没有具体内容', hasSubstantive(E1) === false, '')

  // ② ★ 用户最在意的那条：**重复问同一件事，必须照样能召回**
  const Q = '廊柱截面多大'
  const A1 = '廊柱截面多大？'                                        // 只是把问题又抄一遍
  const A2 = '廊柱截面多大 —— 已定口径：Ø0.22 × 高 2.80（07 §S02）'   // 带答案
  check('回声·只抄问题（无答案）⇒ 剔', isEchoOnly(A1, Q) === true, '')
  check('★ 回声·**重复提问但候选带答案 ⇒ 必须放过**', isEchoOnly(A2, Q) === false,
    `ratio=${echoRatio(A2, Q).toFixed(2)} substantive=${hasSubstantive(A2)}`)

  // ③ 与 query 无关的候选 ⇒ 一律放过（不许误杀）
  const A3 = '示例项目己那边的剧本已经拆到第 7 幕，道具表缺 3 项'
  check('回声·无关候选不误杀', isEchoOnly(A3, Q) === false, '')
  // ④ 短 query：**逐字包含**那条路不生效（<4 字不判，避免误伤）；
  //    但"重合度"那条仍管用 —— 短 query 且候选就是它自己 ⇒ 仍是回声；带具体内容则放过。
  check('回声·短 query + 纯回声候选 ⇒ 仍判回声', isEchoOnly('继续', '继续') === true, '')
  check('回声·短 query + 带内容候选 ⇒ 放过', isEchoOnly('重启后要看日志：第 3 行有 500 报错', '重启') === false, '')
  // ⑤ 池级过滤：留下带答案的、剔掉纯回声
  const pool = filterEchoCandidates(
    [{ text: A1, cite: 'dsh:s1#turn=1' }, { text: A2, cite: 'dsh:s1#turn=2' }, { text: A3, cite: 'dsh:s2#turn=3' }],
    Q,
  )
  check('池级·纯回声被剔（1 条）', pool.dropped.length === 1 && pool.dropped[0].cite === 'dsh:s1#turn=1',
    JSON.stringify(pool.dropped))
  check('池级·带答案的重复提问留下', pool.kept.some((c) => c.cite === 'dsh:s1#turn=2'), '')
  check('池级·无关候选留下', pool.kept.some((c) => c.cite === 'dsh:s2#turn=3'), '')
  check('池级·剔除项带 ratio 与原文（可审计）',
    pool.dropped[0] && typeof pool.dropped[0].ratio === 'number' && !!pool.dropped[0].text, JSON.stringify(pool.dropped[0]))

  // ⑥ 判官输入里必须标出"与 query 重合度"（否则它无法识别回声）
  const jr2 = buildRequest({
    cfg: { persistMax: 4, quoteMax: 4 }, userText: Q, recentTurns: [], forceSignal: false,
    sessionKey: 'session-x', candidates: [{ text: A2, cite: 'dsh:s1#turn=2', timeStart: Date.now(), echo: 0.12 }],
  })
  check('判官输入·含"与 query 重合 X%"', jr2.includes('与 query 重合'), '')
}

console.log('\n【N 项目闸：另一个项目的记录不得冒充本项目的「必须常驻的事实」（2026-10-16 根治）】')
// 实证（用户点名「<用户原话已隐去：另一个项目被错误注入>」）：
//   2026-10-16 22:45:20 真实注入块（session-examplesession＝「示例项目甲」项目，问"项目已有技术基础及当前研发进度"）
//   把《示例项目乙》（上游委托项目（内部红线））的《示例报告_20260915》与
//   `…\示例项目乙_2026\07_测试与性能\_audit_H_示例.md` 放在【必须常驻的事实】标题下。
//   主模型当场识破（"那是上游委托项目，我不进材料"）—— 但**注入已经发生**；换个没识破的模型就写进材料了。
{
  const { projectDomains, projectVerdict } = core
  const OWN = '我们在做 D:\\示例工作区\\示例项目甲_2026-10\\09_项目意义与应用前景_v0.1.md'
  const BANXUE = '《示例报告_20260915》（示例项目乙）：120项功能清单已逐项过账 '
    + 'D:\\示例工作区\\示例项目乙_2026\\07_测试与性能\\_audit_H_示例.md'
  check('项目闸·指纹抽出"示例项目乙"', projectDomains(BANXUE).some((d) => d.key === '示例项目乙'),
    JSON.stringify(projectDomains(BANXUE)))
  check('项目闸·年份后缀归一（示例项目乙_2026 ⇒ 示例项目乙）',
    projectDomains('D:\\示例工作区\\示例项目乙_2026\\a')[0].key === '示例项目乙', '')
  // 正斜杠写法也认（两种分隔符都当分隔符）；引号必须留在排除集里
  check('项目闸·正斜杠写法只取一级目录',
    projectDomains('D:/示例工作区/示例项目乙_2026/_coldstart_data/x.json')[0].raw === '示例项目乙_2026',
    JSON.stringify(projectDomains('D:/示例工作区/示例项目乙_2026/_coldstart_data/x.json')))
  check('项目闸·与本会话项目不相交 ⇒ cross', projectVerdict(BANXUE, OWN).cross === true, '')
  check('项目闸·用户自己点名该项目 ⇒ 不算 cross（跨项目取经）',
    projectVerdict(BANXUE, '把示例项目乙的东西学习过来').cross === false, '')
  check('项目闸·本会话无线索 ⇒ 不下判断（不误杀跨会话召回）', projectVerdict(BANXUE, '').cross === false, '')
  check('项目闸·同一项目 ⇒ 不误杀',
    projectVerdict('D:\\示例工作区\\示例项目甲_2026-10\\x.md', OWN).cross === false, '')
  check('项目闸·无路径候选 ⇒ 不降级（交给判官按相关性判）',
    projectVerdict('审计H批12项状态：已实现2项（47、53）', OWN).cross === false, '')

  const cfgN = { persistMax: 4, quoteMax: 4, forceOnKeywords: ['之前'], projectGate: true }
  const cands = [
    { text: BANXUE, cite: 'dsh:session-examplecite#turn=49', timeStart: Date.now() - 30 * 86400000 },
    { text: '本项目：09_项目意义与应用前景_v0.1.md 已存在 D:\\示例工作区\\示例项目甲_2026-10\\09_项目意义与应用前景_v0.1.md',
      cite: 'dsh:session-examplesess#turn=16', timeStart: Date.now() - 600000 },
  ]
  const pick = (p, q, cfg2 = cfgN, userText = '项目已有技术基础及当前研发进度') => enforceHardRules(
    { persist: p, quote: q, drop: [], order: [], reason: '' },
    { cfg: cfg2, candidates: cands, forceSignal: false, userText, recentTurns: [{ role: 'user', text: OWN }] },
  )
  const r1 = pick([{ fact: '《示例报告_20260915》（示例项目乙）', cite: 'dsh:session-examplecite#turn=49' }], [])
  check('项目闸·跨项目条目被降级、不留在 persist',
    r1.persist.length === 0 && r1.other.length === 1, JSON.stringify({ p: r1.persist.length, o: r1.other.length }))
  check('项目闸·降级留痕（flag 可审计）', r1.flags.some((f) => f.includes('R4')), JSON.stringify(r1.flags))
  check('项目闸·记下"是哪个项目"（真实目录名，可去盘上核）',
    (r1.otherProjects || []).some((x) => String(x).includes('示例项目乙')), JSON.stringify(r1.otherProjects))
  const blk1 = renderBlock(r1, { query: '项目已有技术基础及当前研发进度', model: 'm', ms: 1 })
  check('项目闸·渲染单独成"别的项目"栏 + 强警示',
    blk1.includes('来自**别的项目**') && blk1.includes('另一项目：示例项目乙'), blk1.split('\n').slice(0, 5).join(' ｜ '))
  check('项目闸·跨项目条目不得出现在【必须常驻的事实】栏下',
    !blk1.includes('【必须常驻的事实】'), '')
  const blkQ = renderBlock(pick([], [{ fact: '《开发进度报告》要点', cite: 'dsh:session-examplecite#turn=49' }]),
    { query: 'q', model: 'm', ms: 1 })
  check('项目闸·跨项目的"引用"也一并移入警示栏（不在【原文要点】）',
    !blkQ.includes('【原文要点'), blkQ.split('\n').slice(0, 5).join(' ｜ '))
  // ★★ 2026-10-16（第二轮）：**信息不许丢** —— 降级只换标签，条目正文必须仍在块里。
  //   实证（重启后 22:5x）：我第一版铁律 11 写"另一个项目 ⇒ 默认一律 drop"，
  //   判官照办把 6 条全丢 ⇒ 空手 ⇒ 调用方只注入一行"本轮不注入正文" ⇒ 用户看到"信息没了"。
  check('项目闸·降级后条目正文仍在块里（换标签≠丢内容）',
    blkQ.includes('开发进度报告'), blkQ.split('\n').slice(0, 6).join(' ｜ '))
  check('项目闸·系统提示禁止整条丢弃（防信息丢失）',
    /严禁因为/.test(core.SYSTEM_PROMPT) && /整条丢掉/.test(core.SYSTEM_PROMPT), '')
  check('项目闸·系统提示不再写"默认一律 drop"',
    !/默认\*\*一律 drop\*\*/.test(core.SYSTEM_PROMPT), '')

  const r2 = pick([{ fact: '本项目：09_项目意义与应用前景_v0.1.md 已存在', cite: 'dsh:session-examplesess#turn=16' }], [])
  check('项目闸·本项目条目照常进 persist（不误杀）',
    r2.persist.length === 1 && r2.other.length === 0, JSON.stringify({ p: r2.persist.length, o: r2.other.length }))
  const r3 = pick([], [{ fact: '示例项目乙的实测清单', cite: 'dsh:session-examplecite#turn=49' }],
    cfgN, '把示例项目乙的东西学习过来')
  check('项目闸·用户点名该项目 ⇒ 放行（不降级）',
    r3.other.length === 0 && r3.quote.length === 1, JSON.stringify({ q: r3.quote.length, o: r3.other.length }))
  const r4 = pick([{ fact: 'x', cite: 'dsh:session-examplecite#turn=49' }], [], { ...cfgN, projectGate: false })
  check('项目闸·projectGate=false ⇒ 退回旧行为（可回退）',
    r4.persist.length === 1 && r4.other.length === 0, JSON.stringify({ p: r4.persist.length, o: r4.other.length }))

  const reqN = buildRequest({
    cfg: cfgN, userText: '项目已有技术基础及当前研发进度',
    recentTurns: [{ role: 'user', text: OWN }], forceSignal: false,
    sessionKey: 'session-examplesession', candidates: cands,
  })
  check('项目闸·判官输入含"本会话的项目线索"',
    reqN.includes('## 本会话的项目线索') && reqN.includes('示例项目甲'), '')
  check('项目闸·判官输入给跨项目候选打〔⚠ 另一项目〕标', reqN.includes('〔⚠ 另一项目'), '')
  check('项目闸·判官输入写明"不要整条 drop"（防空手）',
    reqN.includes('全部 drop') && reqN.includes('信息丢失'), '')
  check('项目闸·系统提示写明"不得进 persist"', /不得进 persist/.test(core.SYSTEM_PROMPT), '')

  // ── ★ 真实条件（这才是当天那条失败的原样）：用户话里**没有任何路径** ──
  //   实测：session-examplesession 的 user_text/assistant_text 里一个项目路径都没有
  //   （路径全在 tool_call/tool_result 里）⇒ 只看用户话 = 闸取不到线索 = 形同不存在。
  //   补上来自记忆库的「本会话活动目录」后，闸必须照样触发。
  {
    const REAL_SD = ['示例项目甲_2026-10', '示例项目丁', '示例项目己', '示例项目戊']
    const NO_PATH_USER = '项目已有技术基础及当前研发进度'
    const v = projectVerdict(BANXUE, NO_PATH_USER, REAL_SD)
    check('项目闸·真实条件（用户话无路径 + 活动目录来自库）⇒ 仍判 cross', v.cross === true, JSON.stringify(v))
    const r = enforceHardRules(
      { persist: [{ fact: '《示例报告_20260915》（示例项目乙）', cite: 'dsh:session-examplecite#turn=49' }], quote: [], drop: [], order: [], reason: '' },
      { cfg: cfgN, candidates: cands, forceSignal: false, userText: NO_PATH_USER,
        recentTurns: [{ role: 'user', text: NO_PATH_USER }], sessionDomains: REAL_SD },
    )
    check('项目闸·真实条件 ⇒ 《示例项目乙》被降级（当天那条就修住了）',
      r.persist.length === 0 && r.other.length === 1, JSON.stringify({ p: r.persist.length, o: r.other.length }))
    const rKeep = enforceHardRules(
      { persist: [{ fact: '本项目：09_项目意义与应用前景_v0.1.md 已存在', cite: 'dsh:session-examplesess#turn=16' }], quote: [], drop: [], order: [], reason: '' },
      { cfg: cfgN, candidates: cands, forceSignal: false, userText: NO_PATH_USER,
        recentTurns: [{ role: 'user', text: NO_PATH_USER }], sessionDomains: REAL_SD },
    )
    check('项目闸·真实条件 ⇒ 本项目条目照样进 persist（不误杀）',
      rKeep.persist.length === 1 && rKeep.other.length === 0, JSON.stringify({ p: rKeep.persist.length, o: rKeep.other.length }))
  }

  // ── 真库取数核对（`mem.py session-domains`）：可选，设 DSH_MEMORY_TEST_SESSION 才跑。
  //    公开仓不含任何真实会话 id / 真实项目名，因此默认显式跳过（不是"假通过"）。
  {
    const sid = process.env.DSH_MEMORY_TEST_SESSION || ''
    if (!sid) {
      ok('跳过真库取数核对（未设 DSH_MEMORY_TEST_SESSION）')
    } else {
      let sd = null
      try {
        const py = process.env.ORCH_PYTHON || 'python'
        const raw = execFileSync(py, [join(ROOT, '..', '..', 'bin', 'mem.py'),
          'session-domains', sid, '--json'],
        { encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' }, timeout: 60000 })
        sd = JSON.parse(String(raw).trim().split('\n').pop())
      } catch (e) { sd = { error: String(e?.message || e).slice(0, 80) } }
      const names = Array.isArray(sd?.domains) ? sd.domains.map((d) => d.name) : []
      check('真库·session-domains 能返回目录且名字非空',
        names.length > 0 && names.every((n) => typeof n === 'string' && n.length > 0),
        JSON.stringify(sd).slice(0, 120))
    }
  }
}

console.log('\n【O 本会话口径候选（2026-10-17：治"该带的没带进来"）】')
// 实证：「示例项目甲」2026-10-16 用户 21:10 定下「把目标改成正确认知 3D 空间」，
//   23:33 说「叙述一下总体目标」⇒ 检索（全库字面相似）召回的 6 条**全是别会话日志、本会话口径 0 条**
//   ⇒ 判官空手 ⇒ 记忆 0 注入 ⇒ 模型把本体目标(3D)写成"落笔式临摹"⇒ 用户指出「3D 部分缺失」。
{
  const { buildRequest } = core
  const oCfg = { persistMax: 4, quoteMax: 4, forceOnKeywords: ['之前'], projectGate: true }
  const KP = [
    { text: '现在我的目标转变了从让模型学会剪辑，变成让模型正确认知3d空间、真正理解人类看到的画面，',
      cite: 'dsh:session-examplesess#turn=2', turn: 2, time: Date.now() - 3600 * 1000 },
    { text: '叙述总体目标', cite: 'dsh:session-examplesess#turn=26', turn: 26, time: Date.now() },
  ]
  // ① 判官输入：口径候选带标 + 写明"优先挑进 persist、不许空手"
  const rq = buildRequest({
    cfg: oCfg, userText: '叙述总体目标', recentTurns: [], forceSignal: false,
    sessionKey: 'session-examplesession',
    candidates: [{ ...KP[0], own: true }, { text: '别会话的日志', cite: 'dsh:other#turn=1', timeStart: Date.now() }],
  })
  check('本会话口径·判官输入带〔本会话口径·用户原话〕标', rq.includes('〔本会话口径·用户原话'), '')
  check('本会话口径·判官输入写明"优先挑进 persist / 不许空手"',
    rq.includes('优先把它们挑进 persist') && rq.includes('空手'), '')

  // ② host 级：口径候选进池 ⇒ 判官挑了它 ⇒ **真能被 R0 接受并注入**（这是关键：出处真实）
  let seen = ''
  const rkp = await call({
    query: '叙述总体目标',
    sessionKeypoints: KP,
    sessionKey: 'session-examplesession',
    callModel: async ({ messages }) => {
      seen = (messages?.[1]?.content?.[0]?.text) || ''
      return JSON.stringify({
        persist: [{ fact: '本项目目标：让模型正确认知 3d 空间', cite: 'dsh:session-examplesess#turn=2' }],
        quote: [], drop: [], order: [], reason: '口径优先',
      })
    },
  })
  check('本会话口径·进了判官输入（正文可核）', seen.includes('正确认知3d空间'),
    seen.slice(0, 0) + (seen.includes('〔本会话口径·用户原话') ? '带标' : '无标'))
  check('本会话口径·query 自己的回声被剔（不当口径注入）', !seen.includes('1. [dsh:session-examplesess#turn=26]'), '')
  check('本会话口径·判官挑中后**能进注入块**（出处真实、过 R0）',
    rkp && rkp.status === 'ok' && /必须常驻的事实/.test(rkp.block || '') && /正确认知 3d 空间/.test(rkp.block || ''),
    JSON.stringify(rkp && { s: rkp.status, b: (rkp.block || '').slice(0, 80) }))

  // ③ 真库取数：本会话口径能取到、且出处是真的（可选，设 DSH_MEMORY_TEST_SESSION 才跑）
  const sid2 = process.env.DSH_MEMORY_TEST_SESSION || ''
  if (!sid2) {
    ok('跳过真库口径取数核对（未设 DSH_MEMORY_TEST_SESSION）')
  } else {
    let kp = null
    try {
      const py = process.env.ORCH_PYTHON || 'python'
      const raw = execFileSync(py, [join(ROOT, '..', '..', 'bin', 'mem.py'),
        'session-domains', sid2, '--json', '--keypoints', '6'],
      { encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' }, timeout: 60000 })
      kp = JSON.parse(String(raw).trim().split('\n').pop())
    } catch (e) { kp = { error: String(e?.message || e).slice(0, 80) } }
    const kps = Array.isArray(kp?.keypoints) ? kp.keypoints : []
    check('真库·能取到本会话口径', kps.length > 0, JSON.stringify(kp).slice(0, 140))
    check('真库·口径带真实出处（dsh:<会话>#turn=N）',
      kps.every((x) => Number.isFinite(Number(x.turn))), JSON.stringify(kps.map((x) => x.turn)))
  }
}

console.log('\n【H 单一真身（防两副本漂移）】')
// ★ 2026-10-22：这条判据的前提是「被测源码 == 装机目录」。在 `git archive` 解包出来的
//   发布面里跑测试时，ROOT 是归档副本、INSTALLED 仍指向真实装机目录 ⇒ 必然不一致，
//   那不是"两个副本漂移"，是**判据用错了对象**（会把干净的发布面报成失败）。
//   所以先确认"我是不是那个被装机的目录"：只有两者是同一份时，这条才有意义。
const isInstalledCopy = existsSync(INSTALLED) && md5(join(INSTALLED, 'package.json')) === md5(join(ROOT, 'package.json'))
if (!isInstalledCopy) {
  ok('跳过整段（当前目录不是装机副本 —— 例如在 git archive 发布面上跑）')
} else {
  for (const f of ['lib/index.js', 'orchestrator/core.js', 'orchestrator/host.js', 'index.js', 'package.json']) {
    const inst = join(INSTALLED, f)
    if (!existsSync(inst)) { ok(`跳过（装机目录不存在）${f}`); continue }
    const a = md5(inst), b = md5(join(ROOT, f))
    check(`md5 一致 ${f}`, a === b, `装机=${a} 源码=${b}`)
  }
}

console.log(`\n${fail === 0 ? '✅ 全部通过' : '❌ 有失败'}：${pass} passed, ${fail} failed`)
process.exit(fail === 0 ? 0 : 1)
