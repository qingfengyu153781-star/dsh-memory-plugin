/**
 * v0.1.5 行为回归（正例 + 负例）—— 每个用例都对应 2026-10-01 实测到的一个真实故障。
 *
 * 用法：node test/inject.mjs   （exit 0 = 全过）
 *
 * 覆盖：
 *   A 命中才给正文：weak → 一行提示（不再整块注入）
 *   B 子会话降量：origin=subagent 时 weak 不注入、strong 只给 compact
 *   C 不再硬截断：预算内丢整条 + 给"取全文"指针，绝不出现半截 JSON 标记
 *   D 去重：同 query / 同命中集
 *   E query 清洗：今天噪声榜 Top（"Its closing message:" / "You are a delegated subagent…" /
 *     "用户原话保留在本轮人类消息中…" / "Approval policy:…"）必须被丢掉
 *   F apply() 注册 pre-step 钩子，且样板轮**不 spawn**、原样返回 decision
 */

import { readFileSync, mkdtempSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'

process.env.DSH_MEMORY_NO_INGEST = '1'          // 自检绝不触发真入库（2026-09-30 教训）
// ★ 2026-10-15(6)：台账写临时目录，别拿测试会话污染生产台账（见 selfcheck.mjs 同款注释）
process.env.DSH_MEMORY_BASE = mkdtempSync(join(tmpdir(), 'dshmem-inject-'))

const here = dirname(fileURLToPath(import.meta.url))
// ★ 2026-10-15：入口统一到真身 `lib/index.js`（旧版走根 index.js 那份过期副本）
const mod = await import(pathToFileURL(join(here, '..', 'lib', 'index.js')).href)
const {
  VERSION, cleanQuery, decideInjection, buildDigestBlock, buildHintBlock,
  normalizeQuery, hitKeys, apply, __setSessionOrigins,
} = mod

let failures = 0
let checks = 0
const ok = (label) => { checks += 1; process.stdout.write(`PASS  ${label}\n`) }
const bad = (label, detail) => {
  checks += 1; failures += 1
  process.stdout.write(`FAIL  ${label}\n      ${detail}\n`)
}
const t = (label, cond, detail = '') => (cond ? ok(label) : bad(label, detail))

// ---------------------------------------------------------------- E query 清洗
// ★ 2026-10-15：这份名单混了两种东西，判据必须分开（旧版一律断言 `undefined`）：
//   · **样板/框架文本** ⇒ 洗干净后应当为空（undefined）——"别拿样板当检索词"；
//   · **纯应答词**（"好"/"继续"） ⇒ 2026-10-04 起有意返回**小话标记本身**，
//     由调用方 `isSmallTalkTurn()` 判定后走"无 goal 即跳过召回"的分支
//     （若这里返回 undefined，调用方就分不清"小话轮"与"什么都没说"）。
const NOISE_TODAY = [
  ['Its closing message:', '今天噪声榜第 1（216 次）', undefined],
  ['You are a delegated subagent: your permission scope was fixed when you were created.', '第 2（114 次）', undefined],
  ['用户原话保留在本轮人类消息中，以下仅为辅助说明。', '主会话噪声源', undefined],
  ['Approval policy: ask. Operations that require approval may ask through the UI.', '会话 50b8ffea 的样板', undefined],
  ['Current runtime context. This snapshot supersedes earlier runtime-context snapshots.', '运行环境样板', undefined],
  ['<goal_round>', 'goal 样板', undefined],
  ['Agent 3f2a91cc-1234-5678 sent a message:', '子代理通知', undefined],
  ['2026-10-01T23:56:44+08:00[Asia/Shanghai]', '纯时间戳行', undefined],
  ['好', '纯应答词（保留为小话标记，调用方据此跳过召回）', '好'],
]
for (const [line, why, expected] of NOISE_TODAY) {
  const q = cleanQuery(line)
  t(`E 清洗（${why}）: ${line.slice(0, 32)}`, q === expected,
    `expected ${JSON.stringify(expected)} 得到 ${JSON.stringify(q)}`)
}
const REAL_Q = '有没有办法完全避免里面的问题再次复发？'
t('E 保留真实提问', cleanQuery(REAL_Q) === REAL_Q, `得到 ${JSON.stringify(cleanQuery(REAL_Q))}`)
t('E 保留真实提问（带样板前后缀的多行）',
  cleanQuery(`Current runtime context. This snapshot supersedes.\n${REAL_Q}`) === REAL_Q,
  '多行里应只留真问题')

// ---------------------------------------------------------------- A/B 决策
const strong = { verdict: 'strong', topScore: 9.1, hits: [] }
const weak = { verdict: 'weak', topScore: 5.2, hits: [] }
const none = { verdict: 'none', topScore: 2.0, hits: [] }
const d = (parsed, isSub) => decideInjection({ verdict: parsed.verdict, isSubagent: isSub })

t('A strong+主会话 ⇒ digest', d(strong, false).mode === 'digest', JSON.stringify(d(strong, false)))
t('A weak+主会话 ⇒ hint（不再整块注入）', d(weak, false).mode === 'hint', JSON.stringify(d(weak, false)))
t('A weak+主会话 不是 digest', d(weak, false).mode !== 'digest', '回归：旧版这里会给整块')
t('A verdict=none ⇒ skip', d(none, false).mode === 'skip', JSON.stringify(d(none, false)))
t('A weakMode=off ⇒ skip', decideInjection({ verdict: 'weak', isSubagent: false, weakMode: 'off' }).mode === 'skip', '')
t('A weakMode=full ⇒ digest（后向兼容）', decideInjection({ verdict: 'weak', isSubagent: false, weakMode: 'full' }).mode === 'digest', '')
t('B 子会话+weak ⇒ skip（今天 8 个 teammate 被注入 87–271 次、用了 0 次）',
  d(weak, true).mode === 'skip', JSON.stringify(d(weak, true)))
t('B 子会话+strong ⇒ compact', d(strong, true).mode === 'compact', JSON.stringify(d(strong, true)))
t('B 子会话+strong 不是 full digest', d(strong, true).mode !== 'digest', '')
t('B subagentMode=off ⇒ 任何情况都 skip', decideInjection({ verdict: 'strong', isSubagent: true, subagentMode: 'off' }).mode === 'skip', '')

// ---------------------------------------------------------------- C 摘要而不是截断
const bigHit = (i) => ({
  session_id: `session-${String(i).padStart(8, '0')}-aaaa-bbbb-cccc-ddddeeeeffff`,
  turn: 100 + i, chunk_id: 9000 + i, time_start: 1790862139396, kind: 'turn', score: 8.5 - i * 0.1,
  _session: { origin: 'user', title: 'T' },
  text: '内容'.repeat(1000),                        // 2000 字/条
})
const many = { verdict: 'strong', topScore: 9.0, hits: Array.from({ length: 10 }, (_, i) => bigHit(i)) }
const dig = buildDigestBlock({ query: REAL_Q, parsed: many, mode: 'digest', budget: 6000, hitChars: 600 })
t('C 预算内没被灌爆（≤ budget+200）', dig.chars <= 6200, `chars=${dig.chars}`)
t('C 列出了若干条 + 说明了丢弃数', dig.listed > 0 && dig.dropped === 10 - dig.listed,
  `listed=${dig.listed} dropped=${dig.dropped}`)
t('C 绝不出现"半截 JSON"截断标记', !dig.block.includes('注入内容超长已截断'), '')
t('C 每条都带出处与"取全文"指针', dig.block.includes('dsh:') && dig.block.includes('取全文'), '')
t('C 每条摘录不超过 hitChars', !dig.block.includes('内容'.repeat(301)), '')
const small = buildDigestBlock({ query: REAL_Q, parsed: { verdict: 'strong', topScore: 9, hits: [bigHit(0)] }, mode: 'compact' })
t('C compact 更小（子会话预算）', small.chars <= 1800 + 200, `chars=${small.chars}`)
t('C compact 只列 2 条以内', small.listed <= 2, `listed=${small.listed}`)

// ---------------------------------------------------------------- A hint 体积
const hint = buildHintBlock(REAL_Q, { verdict: 'weak', topScore: 5.19 })
t('A hint 一行级体积（< 400 字，旧版整块可达 6000）', hint.length < 400, `chars=${hint.length}`)
t('A hint 明说"不注入正文"', hint.includes('不注入正文'), '')
t('A hint 保留 query 便于溯源', hint.includes(REAL_Q), '')

// ---------------------------------------------------------------- D 去重键
const p1 = { hits: [bigHit(0), bigHit(1)] }
const p2 = { hits: [bigHit(0), bigHit(1)] }
const p3 = { hits: [bigHit(0), bigHit(2)] }
t('D 同命中集 ⇒ 同键（可去重）', JSON.stringify(hitKeys(p1)) === JSON.stringify(hitKeys(p2)), '')
t('D 命中集变了 ⇒ 键不同（不会误吞）', JSON.stringify(hitKeys(p1)) !== JSON.stringify(hitKeys(p3)), '')
t('D query 归一：大小写/空白不影响去重判定',
  normalizeQuery('  弄一个  总的问题文件 ') === normalizeQuery('弄一个 总的问题文件'), '')

// ---------------------------------------------------------------- F 钩子注册 + 样板轮不 spawn
function makeCtx() {
  const registered = { tools: [], commands: [], sections: [], routes: [], listeners: [], injected: [] }
  const ctx = {
    tools: { register: (x) => { registered.tools.push(x); return () => {} } },
    commands: { register: (x) => { registered.commands.push(x); return () => {} } },
    systemPrompt: { section: (x) => { registered.sections.push(x); return () => {} } },
    webServer: { register: (x) => { registered.routes.push(x); return () => {} } },
    on: (event, listener) => { registered.listeners.push({ event, listener }); return () => {} },
    inject: (deps, cb) => { registered.injected.push(deps); cb({ ...ctx }); return () => {} },
  }
  return { ctx, registered }
}
const { ctx, registered } = makeCtx()
apply(ctx)
// ★ 2026-10-15 两处**过期断言**修正（测试写死了旧版本的行为，不是代码坏了）：
//   ① 工具数：v0.3.0 起注册三个（memory_recall / memory_orchestrator / memory_hippo_status），
//      旧断言写 `length === 1` ⇒ 新工具一上就永远红。
//   ② 版本号：旧断言写死 `VERSION === '0.1.5'`。**判据量错了对象** ——
//      要测的是"版本标识与发布物一致"，不是"等于某个历史数字"。
//      改成直接和 package.json 对账 ⇒ 以后 bump 一处忘了另一处会当场报红。
const pkgVersion = JSON.parse(readFileSync(join(here, '..', 'package.json'), 'utf8')).version
t('F 注册了 memory_recall 工具',
  registered.tools.some((x) => x?.name === 'memory_recall'),
  `registered=${registered.tools.map((x) => x?.name).join(', ')}`)
t('F 注册了 agent/pre-step 钩子', registered.listeners.some((l) => l.event === 'agent/pre-step'), '')
t('F 系统提示写了"派活前先核专条"纪律',
  registered.sections.some((s) => (s.text ?? '').includes('派活')), '系统提示缺 v0.1.5 的两条硬纪律')

const hook = registered.listeners.find((l) => l.event === 'agent/pre-step').listener
let spawned = 0
const origSpawn = mod.runMemRecall
t('F 只注册一个 pre-step（不会重复注入）', registered.listeners.filter((l) => l.event === 'agent/pre-step').length === 1, '')

const decision = {
  kind: 'enter',
  messages: [{ role: 'user', content: [{ type: 'text', text: 'Current runtime context. This snapshot supersedes.' }] }],
}
const same = await hook({ agent: { id: 'session-test' }, turn: 1, signal: undefined }, async () => decision)
t('F 样板轮：原样返回 decision（不注入、不 spawn mem.py）', same === decision && spawned === 0, JSON.stringify(same)?.slice(0, 80))
t('F 版本标识与 package.json 一致', VERSION === pkgVersion, `VERSION=${VERSION} package.json=${pkgVersion}`)

// ------------------------------------------- G 上下文压缩后仍能续接（2026-10-21）
// 目标：模拟"上下文被压缩"——早前的对话全没了，只剩本轮一条短消息。
// 判据：状态包里的确认项（目标锁 / 硬约束）必须**照旧注入**，因为它们来自磁盘，不来自消息历史。
// 这条测的是"续接能力本身"，不是"注入到模型后的最终 messages"（后者外部看不到）。
{
  const { saveScope, confirmContinuityItem } = await import(pathToFileURL(join(here, '..', 'continuity.js')).href)
  // ★ 2026-10-22：范围按对话存 —— 这里给的就是下面 hook 里用的那个会话 id（session-test）。
  saveScope(process.env.DSH_MEMORY_BASE, 'session-test', { enabled: true, mode: 'all' })
  confirmContinuityItem(process.env.DSH_MEMORY_BASE, { kind: 'goal', text: '压缩后必须保住的目标', project: 'p' }, { cite: 'dsh:s9#turn=1', project: 'p' })
  confirmContinuityItem(process.env.DSH_MEMORY_BASE, { kind: 'constraint', text: '压缩后必须保住的硬约束', project: 'p' }, { cite: 'dsh:s9#turn=2', project: 'p' })

  // 压缩后的样子：没有历史、只有一条极短的新消息（且不是插件来源）
  const compacted = {
    kind: 'enter',
    messages: [{ role: 'user', content: [{ type: 'text', text: '继续' }] }],
  }
  const out = await hook({ agent: { id: 'session-test' }, turn: 2, signal: undefined }, async () => compacted)
  const joined = (out?.messages || []).map((m) => (Array.isArray(m.content) ? m.content.map((c) => c?.text || '').join('') : String(m.content || ''))).join('\n')
  t('G 压缩后：目标锁仍被注入', joined.includes('压缩后必须保住的目标'), joined.slice(0, 160))
  t('G 压缩后：硬约束仍被注入', joined.includes('压缩后必须保住的硬约束'), joined.slice(0, 160))
  t('G 压缩后：注入项带出处（可回溯）', joined.includes('dsh:s9#turn=1'), '')
  // 负例：关掉连续性 ⇒ 同样场景不得注入（证明上面不是"任何文本都会被塞进去"）
  saveScope(process.env.DSH_MEMORY_BASE, 'session-test', { enabled: false, mode: 'off' })
  const out2 = await hook({ agent: { id: 'session-test' }, turn: 3, signal: undefined }, async () => ({ kind: 'enter', messages: [{ role: 'user', content: [{ type: 'text', text: '继续' }] }] }))
  const joined2 = (out2?.messages || []).map((m) => (Array.isArray(m.content) ? m.content.map((c) => c?.text || '').join('') : String(m.content || ''))).join('\n')
  t('G 负例：关闭后同样场景不再注入', !joined2.includes('压缩后必须保住的目标'), joined2.slice(0, 160))
}

process.stdout.write(`\nv0.1.5 回归：${checks - failures}/${checks} 通过\n`)
process.exit(failures === 0 ? 0 : 1)
