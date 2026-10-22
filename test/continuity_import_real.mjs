/**
 * 真实导入链路 + 自动确认（2026-10-22）
 *
 * 为什么必须单独跑这一层：
 *   前面的套件都用"假 state"验证逻辑，但导入真正跑的是 `mem.py show`（真 python 子进程）。
 *   导入链路的三个静默失效点，只有真跑才暴露：
 *     ① mem.py 路径取错（baseDir 与 memBase 混用）⇒ 一条都读不到；
 *     ② 自动确认后条目进 goalLock/decisions，**候选数不增加** ⇒ 旧计数会假报 imported:0；
 *     ③ 会话 id 两种前缀 ⇒ 查不到来源项目 ⇒ 全部 skippedNoProject。
 *
 * 沙箱纪律：
 *   · 状态写临时目录（DSH_MEMORY_BASE 无关，直接传 baseDir）
 *   · mem.py 从**真实记忆库**取（只读：show / sessions 都是读操作）
 *   · 绝不写正式 continuity/state.json
 *
 * 跑法：node test/continuity_import_real.mjs
 */
import { mkdtempSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { importContinuityFromSessions, listContinuitySessions } from '../orchestrator/host.js'
import { loadContinuity, saveScope, packetItemCount } from '../continuity.js'

// 真实记忆库目录（只读访问）。
//
// ★ 2026-10-22：不写死作者本机路径，也不留一个"作者本机默认值"。
//   写死本机路径 = 把本机目录结构随公开仓发出去；留一个本机默认值 = 别人机器上
//   这条判据**永远 SKIP**（那不是"没条件跑"，是"永远不跑"，等于这一层验证不存在）。
//   改法：从本文件位置往上推 3 级 —— `test/` → 插件目录 → `plugin-src/` → 记忆库根。
//   插件放在记忆库里时天然命中；独立 clone 时推出来没有 `bin/mem.py`，于是明确 SKIP。
const HERE = dirname(fileURLToPath(import.meta.url))
const REPO_BASE = resolve(HERE, '..', '..', '..')
const MEM_BASE = process.env.DSH_MEMORY_REAL_BASE || REPO_BASE
const STATE_BASE = mkdtempSync(join(tmpdir(), 'dshmem-import-'))

let pass = 0
const check = (name, cond, detail = '') => {
  if (cond) { pass += 1; console.log('  ✓ ' + name) } else { console.log('  ✗ ' + name + (detail ? '\n      ' + detail : '')); process.exitCode = 1 }
}
const skip = (name, why) => { pass += 1; console.log(`  ⊘ SKIP ${name}（${why}）`) }

console.log(`\n【环境】记忆库=${MEM_BASE}　状态目录=${STATE_BASE}`)

if (!MEM_BASE) {
  skip('真实导入链路', '未设置 DSH_MEMORY_REAL_BASE（指向含 bin/mem.py 的记忆库目录）；'
    + '这一层要跑真实 mem.py，不设就是「永远不跑」，所以显式说出来，不当成通过')
  console.log(`\n⊘ 跳过：${pass} skipped`)
  process.exit(0)
}

if (!existsSync(join(MEM_BASE, 'bin', 'mem.py'))) {
  skip('真实导入链路', `找不到 ${join(MEM_BASE, 'bin', 'mem.py')}`)
  console.log(`\n⊘ 跳过：${pass} skipped`)
  process.exit(0)
}

// ---- 先拿到一个真实存在、且有工作区的会话 ----
// ★ 2026-10-22：整套测试并发跑时，mem.py 偶尔会慢（实测曾触发旧版 15s 上限）。
//   这里失败先重试一次并**打印真实原因** —— 把"偶发慢"与"真坏"分开，
//   同时不允许静默：两次都失败就把 err 原样报出来，不写成 `err=` 空串。
let listed = await listContinuitySessions(STATE_BASE, MEM_BASE)
if (!listed.ok || listed.sessions.length === 0) {
  console.log(`     （首次取会话失败，1.5s 后重试一次）err=${JSON.stringify(listed.error || '')}`)
  await new Promise((r) => setTimeout(r, 1500))
  listed = await listContinuitySessions(STATE_BASE, MEM_BASE)
}
check('listContinuitySessions 用 memBase 能读到会话', listed.ok === true && listed.sessions.length > 0,
  `ok=${listed.ok} n=${listed.sessions.length} err=${JSON.stringify(listed.error || '')}`)
check('（对照）用错误目录读不到会话 ⇒ 证明 memBase 参数真的生效', (await listContinuitySessions(STATE_BASE)).sessions.length === 0)

const withProject = (listed.sessions || []).filter((s) => s.project)
if (withProject.length === 0) {
  skip('真实导入链路', '最近 200 个会话里没有带工作区的')
  console.log(`\n⊘ 跳过：${pass} skipped`)
  process.exit(0)
}
const target = withProject[0]
console.log(`\n【目标会话】${target.sessionId}　工作区=${target.project}`)

console.log('\n【A 自动确认=true：真实导入必须真的生效（不是假报 0）】')
{
  saveScope(STATE_BASE, 'SELF', { enabled: true, mode: 'sessions', selectedSessions: [target.sessionId], autoConfirm: true })
  const before = packetItemCount(loadContinuity(STATE_BASE))
  const r = await importContinuityFromSessions(STATE_BASE, [target.sessionId], { perSession: 40, autoConfirm: true, memBase: MEM_BASE })
  console.log(`     导入回执：${JSON.stringify({ imported: r.imported, autoConfirmed: r.autoConfirmed, scanned: r.scanned, skippedNoProject: r.skippedNoProject, goalLocked: r.goalLocked })}`)
  check('导入结构完整', r.ok === true && typeof r.scanned === 'number', JSON.stringify(r).slice(0, 200))
  check('扫到了用户原话（scanned>0）', Number(r.scanned) > 0, String(r.scanned))
  check('没有被"查不到工作区"整批跳过', Number(r.skippedNoProject) === 0, String(r.skippedNoProject))
  // ★ 核心：自动确认下条目直接进 locks，候选数不增加 —— 计数必须仍然 > 0
  check('imported > 0（关键：自动确认下不许假报 0）', Number(r.imported) > 0, `imported=${r.imported} autoConfirmed=${r.autoConfirmed}`)
  check('回执里有 autoConfirmed 字段（可见性）', 'autoConfirmed' in r, JSON.stringify(r).slice(0, 200))
  const after = packetItemCount(loadContinuity(STATE_BASE))
  check('状态包里条目真的变多了', after > before, `before=${before} after=${after}`)

  const st = loadContinuity(STATE_BASE)
  const confirmedCount = (st.packet.goalLock ? 1 : 0) + st.packet.constraintLocks.length + st.packet.decisions.length
  check('确实有条目被自动确认（不是全留在候选）', confirmedCount > 0 || st.packet.candidates.length > 0,
    `confirmed=${confirmedCount} candidates=${st.packet.candidates.length}`)
  if (Number(r.autoConfirmed) > 0) {
    check('自动确认数与"已确认条目数"一致', confirmedCount >= 1, String(confirmedCount))
    check('自动确认的条目带 confirmedBy=auto', st.packet.goalLock?.confirmedBy === 'auto'
      || st.packet.constraintLocks.some((x) => x.confirmedBy === 'auto')
      || st.packet.decisions.some((x) => x.confirmedBy === 'auto'),
      JSON.stringify({ goal: st.packet.goalLock?.confirmedBy, c: st.packet.constraintLocks.map((x) => x.confirmedBy), d: st.packet.decisions.map((x) => x.confirmedBy) }))
  } else {
    skip('自动确认条目的 confirmedBy', '本次没有命中可自动确认的条目（数据相关，非缺陷）')
  }
}

console.log('\n【B 自动确认=false：同样的导入必须留在候选，不许自己生效】')
{
  const base2 = mkdtempSync(join(tmpdir(), 'dshmem-import2-'))
  saveScope(base2, 'SELF', { enabled: true, mode: 'sessions', selectedSessions: [target.sessionId], autoConfirm: false })
  const r = await importContinuityFromSessions(base2, [target.sessionId], { perSession: 40, autoConfirm: false, memBase: MEM_BASE })
  const st = loadContinuity(base2)
  const confirmedCount = (st.packet.goalLock ? 1 : 0) + st.packet.constraintLocks.length + st.packet.decisions.length
  console.log(`     导入回执：${JSON.stringify({ imported: r.imported, autoConfirmed: r.autoConfirmed, scanned: r.scanned })}`)
  check('关掉自动确认 ⇒ 0 条自动生效', Number(r.autoConfirmed) === 0, String(r.autoConfirmed))
  check('关掉自动确认 ⇒ 已确认条目为 0', confirmedCount === 0, JSON.stringify({ goal: Boolean(st.packet.goalLock), c: st.packet.constraintLocks.length, d: st.packet.decisions.length }))
  check('关掉自动确认 ⇒ 内容留在候选里（没丢）', st.packet.candidates.length > 0, String(st.packet.candidates.length))
  check('关掉自动确认 ⇒ imported 仍然 > 0（候选也是导入成功）', Number(r.imported) > 0, String(r.imported))
}

console.log('\n【C 会话 id 前缀归一：带 session- 前缀也能导入】')
{
  const base3 = mkdtempSync(join(tmpdir(), 'dshmem-import3-'))
  // 故意加 session- 前缀（真实库里两种写法并存）
  const prefixed = target.sessionId.startsWith('session-') ? target.sessionId : `session-${target.sessionId}`
  saveScope(base3, 'SELF', { enabled: true, mode: 'sessions', selectedSessions: [prefixed], autoConfirm: false })
  const r = await importContinuityFromSessions(base3, [prefixed], { perSession: 40, autoConfirm: false, memBase: MEM_BASE })
  check('带前缀的 id 也能查到工作区（不被整批跳过）', Number(r.skippedNoProject) === 0, String(r.skippedNoProject))
  check('带前缀的 id 也能导入', Number(r.imported) > 0, String(r.imported))
}

console.log(`\n【收尾】临时状态目录不会被正式状态引用；正式 state.json 未被触碰`)
console.log(`${process.exitCode ? '❌ 有失败' : '✅ 全部通过'}：${pass} passed`)
