/**
 * 后端副本与真源必须一致。
 *
 * 为什么需要这条测试（2026-10-26 架构决定）：
 *   `backend/` 是发布面唯一副本（外人装包拿到的就是它），但真源在记忆库仓的
 *   `bin/mem.py` + `src/memlib/`。两份并存 ⇒ 必然有漂移。
 *   漂移的后果不是"测试红"，而是**外人拿到的后端比我们修好的版本旧** ——
 *   这种错不会自己喊出来，所以必须有一条测试盯着。
 *
 * 口径：
 *   · 在**记忆库仓内**跑 ⇒ 严格对账，不一致就红
 *   · 在**安装副本/单独 checkout** 里跑（找不到真源）⇒ 显式 SKIP 并说明原因，
 *     不假装通过（"跳过"和"通过"是两回事）
 */
import { createHash } from 'node:crypto'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = resolve(HERE, '..')
const BACKEND = join(PLUGIN, 'backend')
const REPO = resolve(PLUGIN, '..', '..')          // 记忆库仓根

let pass = 0
let fail = 0
const md5 = (p) => createHash('md5').update(readFileSync(p)).digest('hex')

function ok(name) { pass += 1; console.log(`PASS  ${name}`) }
function bad(name, detail) { fail += 1; console.log(`FAIL  ${name}\n      ${detail}`) }

console.log('== 后端副本一致性 ==')

// 1) 副本自身必须齐全（这一条在任何环境都该成立）
const must = [
  'backend/bin/mem.py',
  'backend/src/memlib/__init__.py',
  'backend/src/memlib/paths.py',
  'backend/src/memlib/pipeline.py',
  'backend/src/memlib/store.py',
  'backend/src/memlib/dsh.py',
  'backend/src/memlib/brain.py',
]
for (const rel of must) {
  existsSync(join(PLUGIN, rel)) ? ok(`副本存在 ${rel}`) : bad(`副本存在 ${rel}`, '文件缺失')
}

// 2) paths.py 必须存在且真的定义了两个根（防止有人把它删了却没人发现）
const pathsSrc = existsSync(join(BACKEND, 'src', 'memlib', 'paths.py'))
  ? readFileSync(join(BACKEND, 'src', 'memlib', 'paths.py'), 'utf8')
  : ''
pathsSrc.includes('CODE_ROOT') && pathsSrc.includes('DATA_ROOT')
  ? ok('paths.py 定义了 CODE_ROOT 与 DATA_ROOT（代码/数据已解耦）')
  : bad('paths.py 定义了 CODE_ROOT 与 DATA_ROOT（代码/数据已解耦）', '缺定义')

// 3) 后端不再把数据根绑在代码根上
const pipelineSrc = existsSync(join(BACKEND, 'src', 'memlib', 'pipeline.py'))
  ? readFileSync(join(BACKEND, 'src', 'memlib', 'pipeline.py'), 'utf8')
  : ''
const boundToCode = /^ROOT\s*=\s*os\.path\.dirname/m.test(pipelineSrc)
boundToCode
  ? bad('pipeline.ROOT 不再由 __file__ 推导', '仍写着 ROOT = os.path.dirname(...)')
  : ok('pipeline.ROOT 不再由 __file__ 推导（改用 paths.DATA_ROOT）')

// 4) 真源在不在
const srcMem = join(REPO, 'bin', 'mem.py')
const srcLib = join(REPO, 'src', 'memlib')
if (!existsSync(srcMem) || !existsSync(srcLib)) {
  console.log('SKIP  与真源 md5 对账')
  console.log(`      原因：找不到记忆库仓真源（${srcMem}）`)
  console.log('      这不是通过 —— 安装副本里本来就看不到真源，只有仓库内才能对账。')
} else {
  const pairs = [['bin/mem.py', 'backend/bin/mem.py']]
  for (const f of readdirSync(srcLib)) {
    if (f.endsWith('.py')) pairs.push([`src/memlib/${f}`, `backend/src/memlib/${f}`])
  }
  let drift = 0
  for (const [a, b] of pairs) {
    const pa = join(REPO, a)
    const pb = join(PLUGIN, b)
    if (!existsSync(pb)) { drift += 1; bad(`对账 ${b}`, '副本缺失'); continue }
    const ha = md5(pa)
    const hb = md5(pb)
    if (ha !== hb) {
      drift += 1
      bad(`对账 ${b}`, `源 ${ha.slice(0, 8)} ≠ 副本 ${hb.slice(0, 8)}；跑 python tools/sync_backend.py --sync`)
    }
  }
  drift === 0
    ? ok(`与真源一致（${pairs.length} 个文件）`)
    : console.log(`      漂移 ${drift} 个 —— 外人装到的后端与我们修好的版本不同`)
}

console.log('')
if (fail) {
  console.log(`❌ 后端一致性：${pass} passed, ${fail} failed`)
  process.exit(1)
}
console.log(`✅ 后端一致性：${pass} passed, 0 failed`)
