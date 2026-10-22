/**
 * 「先选工作区 → 再选该工作区里的对话」两级选择器的回归（2026-10-22 用户报障）
 *
 * 用户原话见「…」：工作区选择能力消失，希望能先选工作区、再选其中的对话。
 *
 * 真实根因（读代码确认，不是猜）：两级选择器**代码一直在**（projectOptions /
 * projectOfSession），但整段只挂在 `mode === 'project'` 这一条分支下；而"开启本对话的
 * 连续性"默认写的是 `mode: 'sessions'` ⇒ 那条分支永不成立 ⇒ 选择器被条件藏掉。
 * **代码没丢，是入口条件把它藏了。**
 *
 * 所以本测试要锁两件事：
 *   ① 取数口径正确（sessionsInWorkspace：筛工作区、排除本对话、空值=不筛）；
 *   ② 入口条件正确（sessions 模式里**也必须**渲染工作区下拉）——
 *      这一条是上一版没有的，正是它缺失导致"功能消失"。
 *
 * 跑法：node test/workspace_picker.mjs
 */
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const SRC = new URL('../client.js', import.meta.url)
const BUILT = new URL('../lib/client.js', import.meta.url)

let pass = 0
const check = (name, cond, detail = '') => {
  if (cond) { pass += 1; console.log('  ✓ ' + name) } else { console.log('  ✗ ' + name + (detail ? '\n      ' + detail : '')); process.exitCode = 1 }
}

const src = readFileSync(SRC, 'utf8')
const start = src.indexOf('export function projectOptions')
const end = src.indexOf('const api = (path, init)')
const snippet = src.slice(start, end).replace(/^export /gm, '')

const ctx = vm.createContext({ JSON, Array, String })
vm.runInContext(
  snippet + '\nglobalThis.projectOptions = projectOptions\nglobalThis.projectOfSession = projectOfSession\nglobalThis.sessionsInWorkspace = sessionsInWorkspace',
  ctx,
)

const WS = 'D:\\示例工作区\\示例项目甲'
const sessions = [
  { sessionId: 'session-aaa', title: '示例项目甲·规范', project: WS },
  { sessionId: 'bbb', title: '示例项目甲·审计', project: WS },
  { sessionId: 'session-ccc', title: '示例项目乙', project: 'D:\\示例工作区\\示例项目乙' },
  { sessionId: 'session-self', title: '本对话', project: WS },
  { sessionId: 'ddd', title: '无工作区', project: '' },
]

// ---- ① 取数口径 ----
{
  const inWs = ctx.sessionsInWorkspace(sessions, WS, 'session-self')
  check('按工作区筛出该工作区的对话（2 个）', inWs.length === 2, JSON.stringify(inWs.map((s) => s.sessionId)))
  check('★ 排除本对话（列表里不该出现自己）', !inWs.some((s) => s.sessionId === 'session-self'), JSON.stringify(inWs.map((s) => s.sessionId)))
  check('★ 不混入别的工作区', !inWs.some((s) => s.project === 'D:\\示例工作区\\示例项目乙'), JSON.stringify(inWs.map((s) => s.project)))
  check('前缀不同也算同一个自己（session- 归一）', ctx.sessionsInWorkspace(sessions, WS, 'self').every((s) => s.sessionId !== 'session-self'))

  const all = ctx.sessionsInWorkspace(sessions, '', 'session-self')
  check('空工作区 = 不筛（全部工作区，4 个）', all.length === 4, String(all.length))

  check('未知工作区 ⇒ 空（不猜、不回落全部）', ctx.sessionsInWorkspace(sessions, 'D:\\示例工作区\\不存在', 'x').length === 0)
  check('空列表不炸', ctx.sessionsInWorkspace([], WS, 'x').length === 0)
  check('非数组不炸', ctx.sessionsInWorkspace(null, WS, 'x').length === 0)
}

// ---- ② 默认落本对话所在工作区 ----
{
  check('默认工作区 = 本对话所在工作区', ctx.projectOfSession(sessions, 'session-self') === WS)
  check('拿不到本对话 ⇒ 空（切到"全部工作区"，不硬猜）', ctx.projectOfSession(sessions, '不存在') === '')
}

// ---- ③ 入口条件：这才是"功能消失"的真正判据 ----
// 断言方式：把 sessions 分支整段取出来，要求它同时出现
//   ① 工作区下拉（label 里写"先选工作区"）
//   ② 第二级列表（用 sessionsInWorkspace 取数）
// 上一版这段里一个都没有 —— 所以这条测试在旧代码上必须失败。
{
  const scanBranch = (text) => {
    const i = text.indexOf("scMode === 'sessions'")
    if (i < 0) return null
    // 取到下一个同级分支（project 模式）为止
    const j = text.indexOf("scMode === 'project'", i)
    return j > i ? text.slice(i, j) : text.slice(i, i + 4000)
  }
  for (const [name, text] of [['源码', src], ['构建产物', readFileSync(BUILT, 'utf8')]]) {
    const branch = scanBranch(text)
    check(`${name}：sessions 模式存在`, branch !== null)
    check(`${name}：★ sessions 模式里有工作区一级（先选工作区）`,
      !!branch && branch.includes('先选工作区'), '找不到工作区下拉 ⇒ 用户看到的就是"功能没了"')
    check(`${name}：★ 第二级按工作区取数（sessionsInWorkspace）`,
      !!branch && branch.includes('sessionsInWorkspace'), '第二级没按工作区筛 ⇒ 仍是一张跨工作区大平铺')
    check(`${name}：工作区下拉有"全部工作区"兜底项`,
      !!branch && branch.includes('全部工作区'), '缺兜底项 ⇒ 某些工作区一条都没有时无法退回去')
    check(`${name}：工作区下拉的 option 都带样式（不白底）`,
      !!branch && (branch.match(/h\('option', \{/g) || []).length === (branch.match(/h\('option', \{[^]*?style: S\.option/g) || []).length,
      '有 option 漏了 style: S.option')
  }
}

// ---- ④ 不许把"选工作区"写成持久化范围（会破坏已验证的按对话隔离）----
{
  const i = src.indexOf("scMode === 'sessions'")
  const j = src.indexOf("scMode === 'project'", i)
  const branch = src.slice(i, j)
  check('★ 选工作区不写 scope（setWsFilter 只改本地 state）',
    branch.includes('setWsFilter') && !/patchContinuity\(\s*\{\s*project/.test(branch),
    '一旦把工作区写进 scope，就会重新引入跨对话继承')
  check('工作区筛选状态独立于 scope（wsFilter）', src.includes('useState(null)') && src.includes('wsFilter'))
}

console.log(`\n${process.exitCode ? '❌ 有失败' : '✅ 全部通过'}：${pass} passed`)
