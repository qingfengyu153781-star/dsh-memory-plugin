/**
 * 真实 HTTP 沙箱回归（2026-10-22）
 *
 * 与其它套件的区别：前面几个都是**直接调函数**（route.handler 用假 req/res）。
 * 这里起一个**真的 HTTP 服务器**、真的监听端口、真的走 fetch —— 覆盖
 * 「跨源预检 → 带 Origin 的实际请求 → JSON 回执」这条浏览器真正走的路。
 *
 * 沙箱纪律：
 *   · DSH_MEMORY_BASE 指向临时目录，绝不碰正式的记忆库 continuity/state.json
 *   · 端口用 0（系统分配），不占用任何已知端口
 *   · 跑完关服务器，不残留进程
 *
 * 判据（就是用户投诉的那件事）：
 *   A 在 A 对话里设"接上 B、C" ⇒ A 读到 B、C
 *   B 切到 B 对话 ⇒ 读到的是空（不继承 A 的）
 *   C 浏览器跨源（Origin: dsh-app://app）能通过预检并拿到数据
 *   D 不带 sessionId 写 ⇒ 拒绝，且不落成全局
 *
 * 跑法：node test/continuity_http_sandbox.mjs
 */
import { createServer } from 'node:http'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { registerRoutes } from '../orchestrator/host.js'
import { loadContinuity, saveScope } from '../continuity.js'

const BASE = mkdtempSync(join(tmpdir(), 'dshmem-http-'))
const routes = []
registerRoutes({ webServer: { register: (r) => { routes.push(r); return () => {} } } }, BASE)

let pass = 0
const check = (name, cond, detail = '') => {
  if (cond) { pass += 1; console.log('  ✓ ' + name) } else { console.log('  ✗ ' + name + (detail ? '\n      ' + detail : '')); process.exitCode = 1 }
}

// 把插件的全部路由挂到一个真服务器上
const server = createServer(async (req, res) => {
  const u = new URL(req.url || '/', 'http://x')
  const route = routes.find((r) => r.path === u.pathname)
  if (!route) { res.writeHead(404, { 'content-type': 'application/json' }); res.end('{"ok":false,"error":"not found"}'); return }
  try { await route.handler(req, res) } catch (e) {
    try { res.writeHead(500, { 'content-type': 'application/json' }); res.end(JSON.stringify({ ok: false, error: String(e?.message ?? e) })) } catch { /* 已结束 */ }
  }
})
await new Promise((r) => server.listen(0, '127.0.0.1', r))
const port = server.address().port
const base = `http://127.0.0.1:${port}`
console.log(`\n【沙箱服务器】${base}（临时状态目录 ${BASE}）`)

const getJson = async (path, { origin, method = 'GET' } = {}) => {
  const headers = { accept: 'application/json' }
  if (origin) headers.origin = origin
  const r = await fetch(`${base}${path}`, { method, headers })
  let j = null
  try { j = await r.json() } catch { /* 非 JSON */ }
  return { status: r.status, headers: Object.fromEntries(r.headers), json: j }
}

try {
  console.log('\n【C 浏览器跨源：预检 → 实际请求】')
  {
    const pre = await getJson('/dsh-memory/orch/continuity?sessionId=A', { method: 'OPTIONS', origin: 'dsh-app://app' })
    check('预检 204', pre.status === 204, String(pre.status))
    check('预检放行 dsh-app://app', pre.headers['access-control-allow-origin'] === 'dsh-app://app', String(pre.headers['access-control-allow-origin']))
    const real = await getJson('/dsh-memory/orch/continuity?sessionId=A', { origin: 'dsh-app://app' })
    check('实际请求 200', real.status === 200, String(real.status))
    check('实际请求带 allow-origin', real.headers['access-control-allow-origin'] === 'dsh-app://app', String(real.headers['access-control-allow-origin']))
    const evil = await getJson('/dsh-memory/orch/continuity?sessionId=A', { origin: 'https://evil.example' })
    check('外部源不放行', evil.headers['access-control-allow-origin'] === undefined, String(evil.headers['access-control-allow-origin']))
  }

  console.log('\n【A 在 A 对话里接上 B、C】')
  {
    const q = `sessionId=A&enabled=true&mode=sessions&selectedSessions=${encodeURIComponent(JSON.stringify(['B', 'C']))}`
    const w = await getJson(`/dsh-memory/orch/continuity?${q}`, { origin: 'dsh-app://app' })
    check('写入 200 且 ok', w.status === 200 && w.json?.ok === true, JSON.stringify(w.json)?.slice(0, 160))
    check('回执 scope 是 A 的', (w.json?.scope?.selectedSessions || []).join() === 'B,C', JSON.stringify(w.json?.scope))
    const a = await getJson('/dsh-memory/orch/continuity?sessionId=A', { origin: 'dsh-app://app' })
    check('A 读到已开启', a.json?.scope?.enabled === true && a.json?.scope?.mode === 'sessions', JSON.stringify(a.json?.scope))
  }

  console.log('\n【B ★ 切到 B 对话 ⇒ 不继承 A（用户投诉的那一条）】')
  {
    const b = await getJson('/dsh-memory/orch/continuity?sessionId=B', { origin: 'dsh-app://app' })
    check('B 读到未开启', b.json?.scope?.enabled === false, JSON.stringify(b.json?.scope))
    check('B 读到接上的对话为空', (b.json?.scope?.selectedSessions || []).length === 0, JSON.stringify(b.json?.scope?.selectedSessions))
    check('B 不会看到 A 的设置', b.json?.scope?.mode !== 'sessions', String(b.json?.scope?.mode))
    check('落盘只有 1 份范围', Object.keys(loadContinuity(BASE).scopes || {}).length === 1, JSON.stringify(Object.keys(loadContinuity(BASE).scopes || {})))
  }

  console.log('\n【D 不带 sessionId 写 ⇒ 拒绝，且不落成全局】')
  {
    const before = JSON.stringify(loadContinuity(BASE))
    const r = await getJson('/dsh-memory/orch/continuity?enabled=true&mode=all', { origin: 'dsh-app://app' })
    check('返回 ok:false', r.json?.ok === false, JSON.stringify(r.json)?.slice(0, 160))
    check('错误点明 sessionId 必需', /sessionId is required/i.test(String(r.json?.error || '')), String(r.json?.error))
    check('状态完全没变', JSON.stringify(loadContinuity(BASE)) === before)
  }

  console.log('\n【E 两种 id 前缀 ⇒ 同一对话（HTTP 层）】')
  {
    const r = await getJson('/dsh-memory/orch/continuity?sessionId=session-A', { origin: 'dsh-app://app' })
    check('session-A 读到与 A 同一份', r.json?.scope?.mode === 'sessions' && (r.json?.scope?.selectedSessions || []).join() === 'B,C', JSON.stringify(r.json?.scope))
  }

  console.log('\n【F 真实 import 路由：只把自己列进去 ⇒ 没有可导入对象】')
  {
    // ★ 这里必须用一个**没接任何对话**的会话。
    //   若复用 A（它接上了 B、C），路由在"剔除自己后为空"时会回落到 A 已接上的
    //   ['B','C'] 继续导入，返回 ok:true —— 那条断言会变成假失败（依据：路由源码）。
    saveScope(BASE, 'SELF', { enabled: true, mode: 'sessions', selectedSessions: [] })
    const r = await getJson('/dsh-memory/orch/continuity/import?sessionId=SELF&sessionIds=' + encodeURIComponent(JSON.stringify(['SELF'])), { origin: 'dsh-app://app' })
    check('没接对话 + 只列自己 ⇒ ok:false', r.json?.ok === false, JSON.stringify(r.json)?.slice(0, 200))
    check('提示要先勾选要接上的对话', /勾选|可导入/.test(String(r.json?.error || '')), String(r.json?.error))
  }

  console.log('\n【G 真实 import 路由：接上了别人 ⇒ 会真的去扫（不是假成功）】')
  {
    // 用一个不存在的会话 id：应当扫不到、但返回结构完整（ok:true, imported=0, skippedNoProject>0）
    const r = await getJson('/dsh-memory/orch/continuity/import?sessionId=A&sessionIds=' + encodeURIComponent(JSON.stringify(['no-such-session-xyz'])), { origin: 'dsh-app://app' })
    check('返回结构完整', r.json && typeof r.json === 'object' && 'imported' in r.json, JSON.stringify(r.json)?.slice(0, 200))
    check('不存在的会话 ⇒ 导入 0 条（不假装成功）', Number(r.json?.imported) === 0, JSON.stringify(r.json)?.slice(0, 200))
  }
} finally {
  // 回调式关闭，避免"先 close 再等 close 事件"的时序竞态（会卡住整个测试进程）
  await new Promise((r) => server.close(r))
}
console.log(`\n【收尾】服务器已关闭，临时目录 ${BASE} 不影响正式状态`)
console.log(`${process.exitCode ? '❌ 有失败' : '✅ 全部通过'}：${pass} passed`)
