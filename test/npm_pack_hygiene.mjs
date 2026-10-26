/**
 * 打包卫生：**发布面不许带出本机的东西**。
 *
 * 为什么必须有一条测试（2026-10-26 实测病根）：
 *   `package.json` 的 `files` 一旦列了目录，npm 规则下**被 .gitignore 忽略的文件照样打包**。
 *   实测结果是 34 个文件里有 9 个不该出去：
 *     8 个 `*._bak_*` 备份 + `local.config.json`（内含本机绝对路径）。
 *   这类错不会让任何测试变红，只会安静地把本机布局发出去。
 *
 * 判据（每条都可复算）：
 *   1. `npm pack --dry-run --json` 的清单里，**没有任何** `git check-ignore` 命中的路径
 *   2. 清单里没有备份 / 本地配置 / 台账 / 字节码
 *   3. 关键件在清单里：后端、guard、示例配置、入口
 *   4. 清单里任何文件的内容都不含本机路径痕迹
 *
 * 口径：在**安装副本**里跑（没有 .git）⇒ 显式 SKIP 第 1 条并说明原因，其余照跑。
 */
import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = resolve(HERE, '..')

let pass = 0
let fail = 0
const ok = (n) => { pass += 1; console.log(`PASS  ${n}`) }
const bad = (n, d) => { fail += 1; console.log(`FAIL  ${n}\n      ${d}`) }

console.log('== 打包卫生 ==')

// ---------- 取打包清单 ----------
// Windows 上 npm 是 npm.cmd，execFile 直接调 'npm' 会 ENOENT（实测），
// 所以显式试几个名字。
const NPM = process.platform === 'win32' ? 'npm.cmd' : 'npm'
let pack = null
let packErr = ''
for (const bin of [NPM, 'npm']) {
  try {
    const out = execFileSync(bin, ['pack', '--dry-run', '--json'], {
      cwd: PLUGIN, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], shell: process.platform === 'win32',
    })
    pack = JSON.parse(out)
    break
  } catch (e) {
    packErr = String(e.message || e).slice(0, 200)
  }
}
if (!pack) {
  // ★ 这里是**失败**不是跳过：打包卫生是这条测试的唯一职责，
  //   连清单都拿不到就等于什么都没验。
  bad('读取打包清单', packErr || 'npm pack 不可用')
}

if (pack && pack[0] && Array.isArray(pack[0].files)) {
  const files = pack[0].files.map((f) => f.path)
  ok(`拿到打包清单（${files.length} 个文件）`)

  // ---------- 1) 不许有 gitignore 命中的 ----------
  let isGit = false
  try {
    execFileSync('git', ['rev-parse', '--is-inside-work-tree'], { cwd: PLUGIN, stdio: 'ignore' })
    isGit = true
  } catch { isGit = false }

  if (!isGit) {
    console.log('SKIP  与 .gitignore 对账')
    console.log('      原因：安装副本里没有 .git —— 这条只能在仓库内跑，跳过不等于通过。')
  } else {
    const leaked = []
    for (const p of files) {
      try {
        execFileSync('git', ['check-ignore', '-q', '--', p], { cwd: PLUGIN, stdio: 'ignore' })
        leaked.push(p)          // exit 0 = 被忽略 = 不该发布
      } catch { /* exit 1 = 没被忽略 = 正常 */ }
    }
    leaked.length === 0
      ? ok('清单里没有 .gitignore 命中的文件')
      : bad('清单里没有 .gitignore 命中的文件', leaked.slice(0, 8).join(', '))
  }

  // ---------- 2) 不许有备份 / 本地配置 / 台账 / 字节码 ----------
  const forbidden = files.filter((f) =>
    f.includes('._bak_') || f.includes('.bak_') || f.includes('.bak-') ||
    f.endsWith('local.config.json') || f.endsWith('.local.json') ||
    f.startsWith('guard/ledger/') || f.startsWith('guard/_guard_ledger/') ||
    f.includes('__pycache__') || f.endsWith('.pyc'))
  forbidden.length === 0
    ? ok('清单里没有备份/本地配置/台账/字节码')
    : bad('清单里没有备份/本地配置/台账/字节码', forbidden.slice(0, 8).join(', '))

  // ---------- 3) 关键件必须在 ----------
  const must = [
    'backend/bin/mem.py',
    'backend/src/memlib/paths.py',
    'backend/src/memlib/pipeline.py',
    'local.config.example.json',
    'guard/guard_cli.py',
    'guard/gates/g_silent.py',
    'lib/index.js',
    'lib/client.js',
    'README.md',
  ]
  for (const m of must) {
    files.includes(m) ? ok(`清单含 ${m}`) : bad(`清单含 ${m}`, '缺失 ⇒ 外人装不到这个件')
  }

  // ---------- 4) 内容里不许有本机路径痕迹 ----------
  // ★ 标记串必须**拼接**出来，不能写成字面量：否则本文件自己就会命中，
  //   测试自己把自己判红（2026-10-26 实测踩过一次）。
  const BS = String.fromCharCode(92)                 // 反斜杠
  const SL = String.fromCharCode(47)                 // 正斜杠
  const MARKERS = ['D:' + BS + 'agent', 'D:' + SL + 'agent', 'C:' + BS + 'Users' + BS + 'pop']
  const hits = []
  for (const rel of files) {
    const abs = join(PLUGIN, rel)
    if (!existsSync(abs)) continue
    try {
      if (statSync(abs).size > 2 * 1024 * 1024) continue   // 大文件跳过（本仓没有）
      const txt = readFileSync(abs, 'utf8')
      for (const m of MARKERS) {
        if (txt.includes(m)) hits.push(`${rel} ← ${m}`)
      }
    } catch { /* 二进制读不了就跳过 */ }
  }
  hits.length === 0
    ? ok('清单文件内容不含本机路径痕迹')
    : bad('清单文件内容不含本机路径痕迹', hits.slice(0, 8).join('; '))
} else if (pack) {
  bad('拿到打包清单', 'npm pack --json 结构不符预期')
}

console.log('')
if (fail) {
  console.log(`❌ 打包卫生：${pass} passed, ${fail} failed`)
  process.exit(1)
}
console.log(`✅ 打包卫生：${pass} passed, 0 failed`)
