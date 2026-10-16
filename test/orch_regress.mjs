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
  const before = nRows()
  let err = ''
  try {
    execFileSync(process.execPath, [join(ROOT, 'test', 'selfcheck.mjs')],
      { env: { ...process.env, DSH_MEMORY_NO_INGEST: '1' }, stdio: 'pipe' })
  } catch (e) { err = String(e?.message || e).slice(0, 80) }
  const after = nRows()
  check('跑一次 selfcheck ⇒ 生产台账零增长', before >= 0 && after === before,
    `before=${before} after=${after}${err ? ' childErr=' + err : ''}`)
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
  const A3 = '技术工作站那边的剧本已经拆到第 7 幕，道具表缺 3 项'
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

console.log('\n【H 单一真身（防两副本漂移）】')
for (const f of ['lib/index.js', 'orchestrator/core.js', 'orchestrator/host.js', 'index.js', 'package.json']) {
  const inst = join(INSTALLED, f)
  if (!existsSync(inst)) { ok(`跳过（装机目录不存在）${f}`); continue }
  const a = md5(inst), b = md5(join(ROOT, f))
  check(`md5 一致 ${f}`, a === b, `装机=${a} 源码=${b}`)
}

console.log(`\n${fail === 0 ? '✅ 全部通过' : '❌ 有失败'}：${pass} passed, ${fail} failed`)
process.exit(fail === 0 ? 0 : 1)
