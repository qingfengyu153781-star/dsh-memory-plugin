/**
 * 项目选择选项的回归（2026-10-21 用户要求）
 *
 * 背景：project 模式原来是自由文本框，用户得手打 `<WORKSPACE>\示例项目甲`；
 * 打错一个字 ⇒ 精确匹配不上 ⇒ 一条都不注入，而且界面上看不出错在哪。
 * 现在改成"选工作区"或"选一个对话自动取其工作区"。
 *
 * 这里直接执行 client.js 的源码片段（与浏览器加载的构建产物同源）。
 *
 * 跑法：node test/project_options.mjs
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
vm.runInContext(snippet + '\nglobalThis.projectOptions = projectOptions\nglobalThis.projectOfSession = projectOfSession', ctx)

const sessions = [
  { sessionId: 's1', title: '示例项目甲·规范', project: 'D:\\示例工作区\\示例项目甲' },
  { sessionId: 's2', title: '示例项目甲·审计', project: 'D:\\示例工作区\\示例项目甲' },
  { sessionId: 's3', title: '示例项目乙', project: 'D:\\示例工作区\\示例项目乙' },
  { sessionId: 's4', title: '无工作区', project: '' },
  { sessionId: 's5', title: '根目录', project: 'D:\\示例工作区' },
]

const opts = ctx.projectOptions(sessions)
check('去重：示例项目甲只出现一次', opts.filter((o) => o.project === 'D:\\示例工作区\\示例项目甲').length === 1, JSON.stringify(opts.map((o) => o.project)))
check('空工作区不进选项', !opts.some((o) => o.project === ''), JSON.stringify(opts.map((o) => o.project)))
check('统计对话数（示例项目甲 2 个）', opts.find((o) => o.project === 'D:\\示例工作区\\示例项目甲')?.count === 2, JSON.stringify(opts))
check('label 带目录名与路径（人一眼能认）',
  /^示例项目甲 — D:\\示例工作区\\示例项目甲（2 个对话）$/.test(opts.find((o) => o.project === 'D:\\示例工作区\\示例项目甲')?.label || ''),
  String(opts.find((o) => o.project === 'D:\\示例工作区\\示例项目甲')?.label))
check('按路径排序（稳定，不会每次刷新跳位）',
  JSON.stringify(opts.map((o) => o.project)) === JSON.stringify([...opts.map((o) => o.project)].sort((a, b) => a.localeCompare(b))),
  JSON.stringify(opts.map((o) => o.project)))
check('全部工作区都在（3 个）', opts.length === 3, JSON.stringify(opts.map((o) => o.project)))

check('按对话取工作区', ctx.projectOfSession(sessions, 's3') === 'D:\\示例工作区\\示例项目乙')
check('未知对话 ⇒ 空（不猜）', ctx.projectOfSession(sessions, 'nope') === '')
check('空列表不炸', Array.isArray(ctx.projectOptions([])) && ctx.projectOptions([]).length === 0)
check('非数组不炸', ctx.projectOptions(null).length === 0)

const built = readFileSync(BUILT, 'utf8')
check('构建产物含 projectOptions', built.includes('projectOptions'))
// 断言"输入框的 placeholder 属性"已消失，而不是搜任意文本 ——
// 注释里保留说明（"原来叫项目标识或工作区目录"）是应该的，按全文搜会误报。
check('构建产物不再有手打路径的输入框（placeholder 属性）',
  !/placeholder:\s*['"]项目标识或工作区目录['"]/.test(built),
  '仍存在 placeholder 属性')

// ---- 用户实测反馈（2026-10-21）："选项点击后背景全是白的，我看不清" ----
// 病根：半透明背景 + color:inherit 只管控件本身；下拉弹层由系统绘制，默认白底。
// 判据：① 每个 <option> 都显式带样式（漏一个就是白底浅字）；
//      ② 样式里用主题变量 + 兜底色，不靠 inherit；
//      ③ color-scheme 必须是中性 'light dark'，不能强制 'dark light'
//         （强制深色会让浅色主题反向看不清）。
{
  const scan = (text) => {
    const bad = []
    let total = 0
    let i = 0
    while (true) {
      i = text.indexOf("h('option', {", i)
      if (i < 0) break
      total += 1
      let j = i + "h('option', {".length
      let depth = 1
      while (j < text.length && depth) {
        if (text[j] === '{') depth += 1
        else if (text[j] === '}') depth -= 1
        j += 1
      }
      if (!text.slice(i, j).includes('style: S.option')) bad.push(text.slice(i, i + 70))
      i = j
    }
    return { total, bad }
  }
  const srcScan = scan(src)
  const builtScan = scan(built)
  check('源码：每个 option 都带样式', srcScan.total > 0 && srcScan.bad.length === 0, `${srcScan.total} 个，缺 ${srcScan.bad.length}：${srcScan.bad.join(' | ')}`)
  check('构建产物：每个 option 都带样式', builtScan.total > 0 && builtScan.bad.length === 0, `${builtScan.total} 个，缺 ${builtScan.bad.length}`)
  check('option 样式用主题变量（不靠 inherit）', /option:\s*\{[^}]*var\(--dsw-alias-bg-overlay/.test(built), '')
  check('option 样式带兜底色', /option:\s*\{[^}]*#[0-9a-fA-F]{6}/.test(built), '')
  check('select 不再 color:inherit', !/select:\s*\{[^}]*color:\s*'inherit'/.test(built), '')
  check("color-scheme 是中性 'light dark'", /colorScheme:\s*'light dark'/.test(built) && !/colorScheme:\s*'dark light'/.test(built), '')
}

console.log(`\n${process.exitCode ? '❌ 有失败' : '✅ 全部通过'}：${pass} passed`)
