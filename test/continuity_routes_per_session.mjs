/**
 * 路由级「按对话独立」回归（2026-10-22）
 *
 * 为什么要有这一层：continuity_isolation.mjs 测的是 continuity.js 的纯函数，
 * 但**面板实际走的是 HTTP 路由**。路由里一旦漏掉 sessionId（或把它丢掉、或落成全局），
 * 纯函数全绿、界面上照样是"全局一份" —— 这正是用户投诉的那个行为。
 *
 * 判据：
 *   A 带 sessionId 写 ⇒ 只写该对话的范围
 *   B 不带 sessionId 写 ⇒ 明确报错（绝不落成全局）
 *   C 读 A 的对话 ⇒ 拿到 A 的范围；读 B 的对话 ⇒ 拿不到 A 的范围
 *   D 两种 id 前缀（session-xxx / xxx）视为同一对话
 *   E reset keepScope=false ⇒ 所有对话范围一起清
 *   F import 路由：本对话自己不进导入列表
 *
 * 跑法：node test/continuity_routes_per_session.mjs
 */
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { registerRoutes } from '../orchestrator/host.js'
import { loadContinuity, saveScope } from '../continuity.js'

const BASE = mkdtempSync(join(tmpdir(), 'dshmem-routes-'))

let pass = 0
const check = (name, cond, detail = '') => {
  if (cond) { pass += 1; console.log('  ✓ ' + name) } else { console.log('  ✗ ' + name + (detail ? '\n      ' + detail : '')); process.exitCode = 1 }
}

const routes = []
const webCtx = { webServer: { register: (r) => { routes.push(r); return () => {} } } }
registerRoutes(webCtx, BASE)

const call = async (path, { method = 'GET', url } = {}) => {
  const route = routes.find((r) => r.path === path)
  if (!route) throw new Error(`route not registered: ${path}`)
  const headers = {}
  let body = ''
  const res = { writeHead: (s, h) => { Object.assign(headers, h); headers.__status = s }, end: (b) => { body = String(b || '') } }
  const req = { method, url: url || path, headers: {} }
  await route.handler(req, res)
  let json = null
  try { json = JSON.parse(body) } catch { /* 非 JSON */ }
  return { headers, body, json, status: headers.__status }
}

const C = '/dsh-memory/orch/continuity'
const q = (obj) => Object.entries(obj).map(([k, v]) => `${k}=${encodeURIComponent(Array.isArray(v) ? JSON.stringify(v) : String(v))}`).join('&')

console.log('\n【A 带 sessionId 写 ⇒ 只写该对话】')
{
  const r = await call(C, { url: `${C}?${q({ sessionId: 'AAA', enabled: 'true', mode: 'sessions', selectedSessions: ['BBB', 'CCC'] })}` })
  check('写入成功', r.json?.ok === true, r.body.slice(0, 160))
  check('回执带本对话的 scope', r.json?.scope?.enabled === true && r.json?.scope?.mode === 'sessions', JSON.stringify(r.json?.scope))
  check('回执 scope 里接上的对话正确', (r.json?.scope?.selectedSessions || []).join() === 'BBB,CCC', JSON.stringify(r.json?.scope?.selectedSessions))
  const st = loadContinuity(BASE)
  check('落盘：只有 AAA 一份范围', Object.keys(st.scopes || {}).length === 1, JSON.stringify(Object.keys(st.scopes || {})))
  check('落盘：AAA 的内容正确', st.scopes?.aaa?.selectedSessions?.join() === 'BBB,CCC', JSON.stringify(st.scopes?.aaa))
}

console.log('\n【B 不带 sessionId 写 ⇒ 明确报错，绝不落成全局】')
{
  const before = JSON.stringify(loadContinuity(BASE).scopes)
  const r = await call(C, { url: `${C}?${q({ enabled: 'true', mode: 'all' })}` })
  check('返回 ok:false', r.json?.ok === false, r.body.slice(0, 160))
  check('错误信息点明 sessionId 必需', /sessionId is required/i.test(String(r.json?.error || '')), String(r.json?.error))
  check('状态没被改动（不会偷偷写成全局）', JSON.stringify(loadContinuity(BASE).scopes) === before)
  // 旧全局字段也不得被写成"开启"——那是泄漏的入口
  check('旧全局 enabled 未被置真', loadContinuity(BASE).enabled !== true || loadContinuity(BASE).mode === 'off',
    `enabled=${loadContinuity(BASE).enabled} mode=${loadContinuity(BASE).mode}`)
}

console.log('\n【C 读：各对话互不可见】')
{
  const a = await call(C, { url: `${C}?${q({ sessionId: 'AAA' })}` })
  check('读 AAA ⇒ 拿到 AAA 的范围', a.json?.scope?.mode === 'sessions', JSON.stringify(a.json?.scope))
  const b = await call(C, { url: `${C}?${q({ sessionId: 'BBB' })}` })
  check('读 BBB ⇒ 未开启（不继承 AAA）', b.json?.scope?.enabled === false, JSON.stringify(b.json?.scope))
  check('读 BBB ⇒ 接上的对话为空', (b.json?.scope?.selectedSessions || []).length === 0, JSON.stringify(b.json?.scope?.selectedSessions))
  const none = await call(C, { url: C })
  check('不带 sessionId 读 ⇒ scope 为空范围（fail-closed）', none.json?.scope?.enabled === false, JSON.stringify(none.json?.scope))
}

console.log('\n【D 两种 id 前缀视为同一对话】')
{
  const r = await call(C, { url: `${C}?${q({ sessionId: 'session-AAA' })}` })
  check('读 session-AAA ⇒ 与 AAA 同一份', r.json?.scope?.mode === 'sessions' && (r.json?.scope?.selectedSessions || []).join() === 'BBB,CCC', JSON.stringify(r.json?.scope))
  const w = await call(C, { url: `${C}?${q({ sessionId: 'session-AAA', mode: 'all' })}` })
  check('写 session-AAA ⇒ 改的是同一份', w.json?.scope?.mode === 'all', JSON.stringify(w.json?.scope))
  check('不会新增第二个键', Object.keys(loadContinuity(BASE).scopes || {}).length === 1, JSON.stringify(Object.keys(loadContinuity(BASE).scopes || {})))
}

console.log('\n【E reset keepScope=false ⇒ 所有对话范围一起清】')
{
  saveScope(BASE, 'BBB', { enabled: true, mode: 'all' })
  check('先确认有两份范围', Object.keys(loadContinuity(BASE).scopes || {}).length === 2, JSON.stringify(Object.keys(loadContinuity(BASE).scopes || {})))
  const r = await call(C, { url: `${C}?${q({ sessionId: 'BBB', reset: 'true', keepScope: 'false' })}` })
  check('reset 返回 ok', r.json?.ok === true, r.body.slice(0, 160))
  check('所有对话范围被清空', Object.keys(loadContinuity(BASE).scopes || {}).length === 0, JSON.stringify(Object.keys(loadContinuity(BASE).scopes || {})))
  check('reset 留下备份（可回溯）', typeof r.json?.backup === 'string' && r.json.backup.length > 0, String(r.json?.backup))
}

console.log('\n【F import 路由：本对话自己不进导入列表】')
{
  // 不真跑 mem.py：只验证"自己会被剔除"这一层。给一个不存在的对话 ⇒ 应该报"没有可导入的对话"
  saveScope(BASE, 'SELF', { enabled: true, mode: 'sessions', selectedSessions: ['SELF'] })
  const r = await call('/dsh-memory/orch/continuity/import', {
    url: `/dsh-memory/orch/continuity/import?${q({ sessionId: 'SELF', sessionIds: ['SELF'] })}`,
  })
  check('只把自己列进去 ⇒ 拒绝（ok:false）', r.json?.ok === false, r.body.slice(0, 200))
  check('提示要先勾选要接上的对话', /勾选|可导入/.test(String(r.json?.error || '')), String(r.json?.error))
}

console.log(`\n${process.exitCode ? '❌ 有失败' : '✅ 全部通过'}：${pass} passed`)
