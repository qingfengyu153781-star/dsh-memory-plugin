/**
 * 路由 CORS 回归（2026-10-21）
 *
 * 背景（实测，非推测）：桌面版渲染进程 origin 是 `dsh-app://app`，
 * 而插件路由挂在 `http://127.0.0.1:<port>`。原响应**没有任何 Access-Control-Allow-Origin**
 * ⇒ 浏览器拦掉跨源 fetch ⇒ 面板"开启项目连续性"点了没反应（reports/cors_check.txt）。
 *
 * 判据：
 *   A 预检 OPTIONS 必须 204 且带 allow-origin/methods/headers，并且**不执行业务**
 *   B 实际请求必须带 allow-origin（等于请求的 Origin）
 *   C 外部源不得放行
 *   D 无 Origin（同源/命令行）不加任何 CORS 头
 *
 * 跑法：node test/cors_routes.mjs
 */
import { registerRoutes } from '../orchestrator/host.js'

let pass = 0
const check = (name, cond, detail = '') => {
  if (cond) { pass += 1; console.log('  ✓ ' + name) } else { console.log('  ✗ ' + name + (detail ? '\n      ' + detail : '')); process.exitCode = 1 }
}

const routes = []
const webCtx = { webServer: { register: (r) => { routes.push(r); return () => {} } } }
registerRoutes(webCtx, process.cwd())

const call = async (path, { method = 'GET', origin, url } = {}) => {
  const route = routes.find((r) => r.path === path)
  const headers = {}
  let body = ''
  const res = { writeHead: (s, h) => { Object.assign(headers, h); headers.__status = s }, end: (b) => { body = String(b || '') } }
  const req = { method, url: url || path, headers: origin ? { origin } : {} }
  await route.handler(req, res)
  return { headers, body, status: headers.__status }
}

check('注册了 /dsh-memory/orch/stats', routes.some((r) => r.path === '/dsh-memory/orch/stats'), `routes=${routes.length}`)

const pre = await call('/dsh-memory/orch/stats', { method: 'OPTIONS', origin: 'dsh-app://app' })
check('A 预检 ⇒ 204', pre.status === 204, String(pre.status))
check('A 预检带 allow-origin', pre.headers['access-control-allow-origin'] === 'dsh-app://app', String(pre.headers['access-control-allow-origin']))
check('A 预检带 allow-methods', /GET/.test(pre.headers['access-control-allow-methods'] || ''), String(pre.headers['access-control-allow-methods']))
check('A 预检带 allow-headers', /content-type/i.test(pre.headers['access-control-allow-headers'] || ''), String(pre.headers['access-control-allow-headers']))
check('A 预检不执行业务（无 stats 正文）', !pre.body.includes('"total"'), pre.body.slice(0, 60))

const get = await call('/dsh-memory/orch/stats', { origin: 'dsh-app://app' })
check('B 实际请求带 allow-origin', get.headers['access-control-allow-origin'] === 'dsh-app://app', String(get.headers['access-control-allow-origin']))
check('B 实际请求仍返回业务数据', get.body.includes('{'), get.body.slice(0, 60))

const loop = await call('/dsh-memory/orch/stats', { origin: 'http://127.0.0.1:12345' })
check('B2 回环 http 源也放行（Web 版换端口）', loop.headers['access-control-allow-origin'] === 'http://127.0.0.1:12345', String(loop.headers['access-control-allow-origin']))

const evil = await call('/dsh-memory/orch/stats', { origin: 'https://evil.example' })
check('C 外部源不放行', evil.headers['access-control-allow-origin'] === undefined, String(evil.headers['access-control-allow-origin']))

const none = await call('/dsh-memory/orch/stats', {})
check('D 无 Origin ⇒ 不加 CORS 头', none.headers['access-control-allow-origin'] === undefined)

// 写路由同样要能跨源（开关就是 GET 带 query 写的）
const w = await call('/dsh-memory/orch/continuity', { origin: 'dsh-app://app', url: '/dsh-memory/orch/continuity' })
check('E continuity 路由也带 allow-origin', w.headers['access-control-allow-origin'] === 'dsh-app://app', String(w.headers['access-control-allow-origin']))

console.log(`\n${process.exitCode ? '❌ 有失败' : '✅ 全部通过'}：${pass} passed`)
