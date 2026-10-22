/**
 * 按对话独立衔接（2026-10-22 用户核心指正）
 *
 * 用户原话见「…」，要点：
 *   范围必须是**单个对话**的 —— 切到别的对话不该继承这一份衔接。
 *
 * 所以判据只有一条，但必须用**真实的 pre-step 钩子**跑（不是只测纯函数）：
 *   在 A 对话里接上 B、C ⇒ A 能看到 B、C 的内容；
 *   **切到 B 对话，B 不能被接上任何东西**（这正是被投诉的行为）；
 *   B 自己开启后，只看到自己 + 自己接上的。
 *
 * 跑法：node test/continuity_per_session.mjs   （exit 0 = 全过）
 */
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'

process.env.DSH_MEMORY_NO_INGEST = '1'
process.env.DSH_MEMORY_BASE = mkdtempSync(join(tmpdir(), 'dshmem-persession-'))

const here = dirname(fileURLToPath(import.meta.url))
const BASE = process.env.DSH_MEMORY_BASE
const cont = await import(pathToFileURL(join(here, '..', 'continuity.js')).href)
const { saveScope, confirmContinuityItem, loadContinuity, scopeFor } = cont
const mod = await import(pathToFileURL(join(here, '..', 'lib', 'index.js')).href)

let failures = 0
let checks = 0
const t = (label, cond, detail = '') => {
  checks += 1
  if (cond) process.stdout.write(`PASS  ${label}\n`)
  else { failures += 1; process.stdout.write(`FAIL  ${label}\n      ${detail}\n`) }
}

// ---- 用真实的 apply() 拿真实的 pre-step 钩子（与生产同一条代码路径）----
const registered = { listeners: [] }
const ctx = {
  tools: { register: () => () => {} },
  commands: { register: () => () => {} },
  systemPrompt: { section: () => () => {} },
  webServer: { register: () => () => {} },
  on: (event, listener) => { registered.listeners.push({ event, listener }); return () => {} },
  inject: (deps, cb) => { cb(ctx); return () => {} },
  effect: (fn) => { try { fn() } catch { /* noop */ } return () => {} },
}
mod.apply(ctx)
const hook = registered.listeners.find((l) => l.event === 'agent/pre-step')?.listener
t('拿到真实 pre-step 钩子', typeof hook === 'function', String(typeof hook))
if (typeof hook !== 'function') {
  process.stdout.write(`\n❌ 无法继续：钩子没注册\n`)
  process.exit(1)
}

/** 跑一轮 pre-step，返回本轮最终 messages 的纯文本。 */
const runTurn = async (sessionId, turn, text = '继续') => {
  const decision = { kind: 'enter', messages: [{ role: 'user', content: [{ type: 'text', text }] }] }
  const out = await hook({ agent: { id: sessionId }, turn, signal: undefined }, async () => decision)
  return (out?.messages || [])
    .map((m) => (Array.isArray(m.content) ? m.content.map((c) => c?.text || '').join('') : String(m.content || '')))
    .join('\n')
}

// ---- 造数据：B 对话里确认了一条目标；A 对话接上 B、C ----
// ★ sourceSession 必须显式给：lock() 写的是 text(raw.sourceSession)，不给就是空串；
//   而 inScope 的判据是 `Boolean(src && …)` ⇒ 空串直接 false。
//   少写这个字段会得到"假失败"（看起来像按对话独立没实现），而不是产品缺陷。
confirmContinuityItem(BASE, { kind: 'goal', text: 'B 对话里的目标：把报告写完', project: 'P', sourceSession: 'B' }, { cite: 'dsh:B#turn=2', project: 'P' })
confirmContinuityItem(BASE, { kind: 'constraint', text: 'C 对话里的硬约束：必须保留出处', project: 'P', sourceSession: 'C' }, { cite: 'dsh:C#turn=3', project: 'P' })
saveScope(BASE, 'A', { enabled: true, mode: 'sessions', selectedSessions: ['B', 'C'] })

// 边界固定下来：已确认条目若没有 sourceSession，任何对话都看不到（fail-closed）。
// 不写成断言，下次又会靠"偶然踩坑"才想起这条。
confirmContinuityItem(BASE, { kind: 'decision', text: '没有来源会话的决定', project: 'P' }, { cite: 'dsh:unknown#turn=1', project: 'P' })
const orphanText = await runTurn('A', 9)
t('已确认条目缺 sourceSession ⇒ 任何对话都看不到（fail-closed）', !orphanText.includes('没有来源会话的决定'), orphanText.slice(0, 200))

// ---- A 对话：应当看到 B、C ----
const aText = await runTurn('A', 1)
t('A 能看到接上的 B 的目标', aText.includes('B 对话里的目标'), aText.slice(0, 200))
t('A 能看到接上的 C 的约束', aText.includes('C 对话里的硬约束'), aText.slice(0, 200))
t('A 的注入里写明"范围只属于本对话"', aText.includes('只属于本对话'), aText.slice(0, 200))

// ---- ★ 被投诉的那一条：切到 B，B 不该被接上 ----
// 注意：钩子返回的是 decision.messages，里面**本来就有**本轮用户消息（这里是"继续"）。
// 所以判据不能是"文本为空"，必须是"没有状态包块 / 没有别人的内容"。
const hasPacket = (s) => String(s).includes('<project-continuity')
const bText = await runTurn('B', 1)
t('★ B 对话不继承 A 的衔接设置（切过去没有状态包）', !hasPacket(bText), bText.slice(0, 200))
t('★ B 对话里看不到 A 接上的内容', !bText.includes('B 对话里的目标') && !bText.includes('C 对话里的硬约束'), bText.slice(0, 200))

const cText = await runTurn('C', 1)
t('★ C 对话同样不继承', !hasPacket(cText), cText.slice(0, 200))

// ---- B 自己开启后：只看到自己 + 自己接上的 ----
saveScope(BASE, 'B', { enabled: true, mode: 'sessions', selectedSessions: [] })
const b2 = await runTurn('B', 2)
t('B 自己开启后能看到自己的目标', b2.includes('B 对话里的目标'), b2.slice(0, 200))
t('B 自己开启后仍看不到 C 的约束（没接上）', !b2.includes('C 对话里的硬约束'), b2.slice(0, 200))

// ---- A 的设置没被 B 的开启覆盖 ----
const st = loadContinuity(BASE)
t('A 与 B 的范围各自独立并存',
  scopeFor(st, 'A').selectedSessions.join() === 'B,C' && scopeFor(st, 'B').selectedSessions.length === 0,
  JSON.stringify({ A: scopeFor(st, 'A'), B: scopeFor(st, 'B') }))
t('A 仍然开启（没被 B 的写入带掉）', scopeFor(st, 'A').enabled === true)

// ---- 前缀归一：用带 session- 的 id 读同一份设置 ----
const aPrefixed = await runTurn('session-A', 3)
t('带 session- 前缀访问同一对话 ⇒ 仍是同一份设置', aPrefixed.includes('B 对话里的目标'), aPrefixed.slice(0, 200))

// ---- 负例：完全没设置过的对话，什么都不注入 ----
const zText = await runTurn('Z', 1)
t('没设置过的对话 ⇒ 零注入（fail-closed）', !hasPacket(zText), zText.slice(0, 200))

process.stdout.write(`\n${failures ? '❌ 有失败' : '✅ 全部通过'}：${checks - failures}/${checks} 通过\n`)
process.exitCode = failures === 0 ? 0 : 1
