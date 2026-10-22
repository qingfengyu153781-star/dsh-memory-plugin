/**
 * 客户端"确认候选"路径的回归（2026-10-21）
 *
 * 为什么单独测这条：UI 页面要鉴权，外部拿不到 bundle 内容；
 * 但"确认按钮点了没反应"正是之前的真实故障（POST body 在 DSH exact 路由里读不到）。
 * 这里直接执行 client.js 的**源码片段**（与浏览器加载的构建产物同源），
 * 断言路径形态，而不是靠人点一次。
 *
 * 跑法：node test/client_confirm_path.mjs
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const SRC = new URL('../client.js', import.meta.url)
const BUILT = new URL('../lib/client.js', import.meta.url)

let pass = 0
const check = (name, cond, detail = '') => {
  if (cond) { pass += 1; console.log('  ✓ ' + name) } else { console.log('  ✗ ' + name + (detail ? '\n      ' + detail : '')); process.exitCode = 1 }
}

const src = readFileSync(SRC, 'utf8')
const start = src.indexOf('export function continuityItemPath')
const end = src.indexOf('const TOOL_NAME')
const snippet = src.slice(start, end).replace('export function', 'function')

const ctx = vm.createContext({ JSON, encodeURIComponent })
vm.runInContext(snippet + '\nglobalThis.build = continuityItemPath', ctx)

const item = { id: 'c1', kind: 'goal', text: '目标：带中文与空格', cite: 'dsh:s1#turn=3', project: 'D:\\示例工作区\\示例项目甲' }
const path = ctx.build(item)

check('走 /continuity/item?query（不是 POST body）', path.startsWith('/continuity/item?'), path)
check('带 item 参数且可还原', (() => {
  const q = new URLSearchParams(path.slice(path.indexOf('?') + 1))
  return JSON.stringify(JSON.parse(q.get('item'))) === JSON.stringify(item)
})(), path)
check('cite 原样带出', new URLSearchParams(path.split('?')[1]).get('cite') === 'dsh:s1#turn=3')
check('project 原样带出（中文路径可还原）', new URLSearchParams(path.split('?')[1]).get('project') === 'D:\\示例工作区\\示例项目甲')
check('特殊字符被编码（# 不会截断）', !path.includes('turn=3&project') || path.includes('%23'), path)

// 候选没有项目 ⇒ 不得凭空塞一个 project
const noProj = ctx.build({ kind: 'episode', text: 'x', cite: 'dsh:s2#turn=1' })
check('无项目 ⇒ 不带 project 参数', !new URLSearchParams(noProj.split('?')[1]).has('project'), noProj)

// 构建产物必须包含这条路径（否则浏览器加载的还是旧逻辑）
const built = readFileSync(BUILT, 'utf8')
check('构建产物含 continuityItemPath', built.includes('continuityItemPath'))
check('构建产物不再有 POST body 写法', !/method:\s*'POST',\s*body:\s*JSON\.stringify\(\{\s*item/.test(built))

console.log(`\n${process.exitCode ? '❌ 有失败' : '✅ 全部通过'}：${pass} passed`)
