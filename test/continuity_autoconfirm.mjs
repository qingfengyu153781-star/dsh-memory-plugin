/**
 * 自动确认（长线运行）回归 —— 2026-10-22
 *
 * 用户原话：「<用户原话已隐去：自动确认不该靠人点>」
 *
 * 所以这里测的不是"能不能自动"，而是**自动之后不会把方向搞坏**：
 *   A 默认自动确认：目标/约束/决定带出处直接生效，不需要逐条点
 *   B 问句不自动升格（"能不能改成方案 B？" 不能变成已确认决定）
 *   C "就按方案 A 吧" 是决定，**不能**被误判成问句（这条最容易写错）
 *   D 人锁定的目标永不被自动替换
 *   E 自动锁定的目标可被更实质的目标取代，且旧目标进 goalHistory（可追溯）
 *   F 13 字的随口一句不能把 91 字的方向带跑（用真实数据标定）
 *   G toBool：query 通道的字符串 'false'/'0' 必须真的关掉
 *   H packetItemCount 覆盖所有承载位置（否则导入会假报 0）
 *   I 已确认条目可撤销（自动确认必须有撤销出口）
 *   J 召回片段（episode）即使 autoConfirm=true 也不自动确认
 *
 * 跑法：node test/continuity_autoconfirm.mjs
 */
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'
import { dirname } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const mod = await import(pathToFileURL(join(here, '..', 'continuity.js')).href)
const {
  loadContinuity, saveScope, scopeFor, captureContinuityCandidate, confirmContinuityItem,
  rejectContinuityItem, packetItemCount, toBool, looksLikeQuestion, renderContinuityPacket,
} = mod

let pass = 0
const check = (name, cond, detail = '') => {
  if (cond) { pass += 1; console.log('  ✓ ' + name) } else { console.log('  ✗ ' + name + (detail ? '\n      ' + detail : '')); process.exitCode = 1 }
}
const fresh = () => mkdtempSync(join(tmpdir(), 'continuity-auto-'))
const pkt = (base) => loadContinuity(base).packet

console.log('\n【A 默认自动确认：目标/约束/决定直接生效，不需要逐条点】')
{
  const base = fresh()
  saveScope(base, 'S', { enabled: true, mode: 'sessions', selectedSessions: [] })
  check('新范围默认 autoConfirm=true', scopeFor(loadContinuity(base), 'S').autoConfirm === true)

  const r1 = captureContinuityCandidate(base, { kind: 'goal', text: '把这份报告写成可交付版本并保留出处', cite: 'dsh:S#turn=1', sourceSession: 'S', project: 'P' }, { autoConfirm: true })
  check('goal 自动确认', Boolean(r1.autoConfirmed), JSON.stringify(r1.autoConfirmed))
  check('goal 进入 goalLock（不是候选）', pkt(base).goalLock?.text?.includes('可交付版本'), JSON.stringify(pkt(base).goalLock?.text))
  check('goalLock 标记为自动确认', pkt(base).goalLock?.confirmedBy === 'auto', String(pkt(base).goalLock?.confirmedBy))
  check('确认后不再留在候选里', (pkt(base).candidates || []).length === 0, String((pkt(base).candidates || []).length))

  const r2 = captureContinuityCandidate(base, { kind: 'constraint', text: '必须保留出处，不许省略引用', cite: 'dsh:S#turn=2', sourceSession: 'S', project: 'P' }, { autoConfirm: true })
  check('constraint 自动确认', Boolean(r2.autoConfirmed))
  check('constraint 进入 constraintLocks', pkt(base).constraintLocks.length === 1, String(pkt(base).constraintLocks.length))

  const r3 = captureContinuityCandidate(base, { kind: 'decision', text: '就按方案 A 定下来', cite: 'dsh:S#turn=3', sourceSession: 'S', project: 'P' }, { autoConfirm: true })
  check('decision 自动确认', Boolean(r3.autoConfirmed))
  check('decision 进入 decisions', pkt(base).decisions.length === 1, String(pkt(base).decisions.length))

  // 注入侧必须真的看得到（自动确认不能只是写进去不注入）
  const rendered = renderContinuityPacket(loadContinuity(base), { sessionId: 'S' })
  check('自动确认的内容真的被注入', rendered.includes('可交付版本') && rendered.includes('必须保留出处'), rendered.slice(0, 160))
}

console.log('\n【B 问句不自动升格（问句不是决定）】')
{
  const base = fresh()
  saveScope(base, 'S', { enabled: true, mode: 'sessions', selectedSessions: [] })
  for (const [text, why] of [
    ['能不能改成方案 B？', '带问号 + 疑问开头'],
    ['必须这样吗', '句末"吗"'],
    ['是否应该改用新接口', '以"是否"开头'],
    ['你觉得改成 A 行不行', '句中征询'],
  ]) {
    const r = captureContinuityCandidate(base, { kind: 'decision', text, cite: `dsh:S#t${why.length}`, sourceSession: 'S', project: 'P' }, { autoConfirm: true })
    check(`问句不自动确认：${why}`, !r.autoConfirmed, JSON.stringify(r.autoConfirmed))
    check(`问句原因可见：${why}`, r.autoSkipped?.reason === 'looks-like-question', JSON.stringify(r.autoSkipped))
  }
  check('问句全部留在候选，没进 decisions', pkt(base).decisions.length === 0, String(pkt(base).decisions.length))
  check('问句留在候选里（没被丢掉）', (pkt(base).candidates || []).length === 4, String((pkt(base).candidates || []).length))
}

console.log('\n【C "就按…吧" 是决定，不能被误判成问句（最容易写错的一条）】')
{
  // 直接测判定函数
  check('"就按方案 A 吧" 不是问句', looksLikeQuestion('就按方案 A 吧') === false)
  check('"就这么定吧" 不是问句', looksLikeQuestion('就这么定吧') === false)
  check('"就这样做吧，别再问了" 不是问句', looksLikeQuestion('就这样做吧，别再问了') === false)
  check('"这样行吗" 是问句', looksLikeQuestion('这样行吗') === true)
  check('"能不能…？" 是问句', looksLikeQuestion('能不能换成 B？') === true)
  check('"是否" 开头是问句', looksLikeQuestion('是否要加缓存') === true)
  // 行为层再验一次
  const base = fresh()
  saveScope(base, 'S', { enabled: true, mode: 'sessions', selectedSessions: [] })
  const r = captureContinuityCandidate(base, { kind: 'decision', text: '就按方案 A 吧', cite: 'dsh:S#turn=9', sourceSession: 'S', project: 'P' }, { autoConfirm: true })
  check('"就按方案 A 吧" 真的自动确认了', Boolean(r.autoConfirmed), JSON.stringify(r.autoSkipped))
}

console.log('\n【D 人锁定的目标永不被自动替换】')
{
  const base = fresh()
  saveScope(base, 'S', { enabled: true, mode: 'sessions', selectedSessions: [] })
  // 人工确认（面板点确认 / 工具显式确认）
  confirmContinuityItem(base, { kind: 'goal', text: '人定的方向：先把接口稳定下来', project: 'P', sourceSession: 'S' },
    { cite: 'dsh:S#turn=1', project: 'P', confirmedBy: 'human' })
  check('人工锁已建立', pkt(base).goalLock?.confirmedBy === 'human', String(pkt(base).goalLock?.confirmedBy))

  const r = captureContinuityCandidate(base, {
    kind: 'goal',
    text: '一个非常非常长的替代目标，长到足以超过任何长度门槛，用来验证人工锁不会被自动覆盖，这是关键的安全性质，必须成立',
    cite: 'dsh:S#turn=2', sourceSession: 'S', project: 'P',
  }, { autoConfirm: true })
  check('人工锁不被自动替换', !r.autoConfirmed, JSON.stringify(r.autoConfirmed))
  check('原因可见：goal-locked-by-human', r.autoSkipped?.reason === 'goal-locked-by-human', JSON.stringify(r.autoSkipped))
  check('目标锁仍是人定的那条', pkt(base).goalLock?.text?.includes('先把接口稳定下来'), String(pkt(base).goalLock?.text))
  check('新目标留在候选（等人判断，不丢）', (pkt(base).candidates || []).some((c) => c.text.includes('非常非常长')))
}

console.log('\n【E 自动锁定的目标可被更实质的目标取代，旧目标进 goalHistory】')
{
  const base = fresh()
  saveScope(base, 'S', { enabled: true, mode: 'sessions', selectedSessions: [] })
  const first = '检查本dsh的记忆存储方式、记忆插件运行模式、参考某个仓库的记忆方式然后向我报告不需要做成文件' // 91 字量级（实测原句）
  const core = '对比这两种记忆方式、结合论文、其他可用方式、人类的成长学习和记忆方式、GitHub调研合并给出一个新的记忆方案，并新增两个功能，制作方向永远不偏移不会出现幻觉制作，跨对话无缝衔接' // 152 字量级
  captureContinuityCandidate(base, { kind: 'goal', text: first, cite: 'dsh:S#turn=1', sourceSession: 'S', project: 'P' }, { autoConfirm: true })
  check('初步任务先锁上（自动）', pkt(base).goalLock?.text === first, String(pkt(base).goalLock?.text))

  const r = captureContinuityCandidate(base, { kind: 'goal', text: core, cite: 'dsh:S#turn=4', sourceSession: 'S', project: 'P' }, { autoConfirm: true })
  check('核心目标取代了初步任务', Boolean(r.autoConfirmed), JSON.stringify(r.autoSkipped))
  check('目标锁换成核心目标', pkt(base).goalLock?.text === core, String(pkt(base).goalLock?.text)?.slice(0, 40))
  check('回执里说明了被取代的是哪条', String(r.autoConfirmed?.replaced || '').includes('检查本dsh'), String(r.autoConfirmed?.replaced)?.slice(0, 40))
  check('旧目标进 goalHistory（可追溯）', (pkt(base).goalHistory || []).some((g) => g.text === first), JSON.stringify((pkt(base).goalHistory || []).map((g) => g.text?.slice(0, 20))))
  check('新目标记了 supersedes 链', Boolean(pkt(base).goalLock?.supersedes), String(pkt(base).goalLock?.supersedes))
}

console.log('\n【F 随口一句不能把方向带跑（用真实数据标定：13 字 vs 91 字）】')
{
  const base = fresh()
  saveScope(base, 'S', { enabled: true, mode: 'sessions', selectedSessions: [] })
  const first = '检查本dsh的记忆存储方式、记忆插件运行模式、参考某个仓库的记忆方式然后向我报告不需要做成文件'
  captureContinuityCandidate(base, { kind: 'goal', text: first, cite: 'dsh:S#turn=1', sourceSession: 'S', project: 'P' }, { autoConfirm: true })
  const r = captureContinuityCandidate(base, { kind: 'goal', text: '就这么办吧设定目标开始制作', cite: 'dsh:S#turn=16', sourceSession: 'S', project: 'P' }, { autoConfirm: true })
  check('13 字随口一句不取代 91 字方向', !r.autoConfirmed, JSON.stringify(r.autoConfirmed))
  check('原因可见：goal-not-more-substantial', r.autoSkipped?.reason === 'goal-not-more-substantial', JSON.stringify(r.autoSkipped))
  check('目标锁没变', pkt(base).goalLock?.text === first, String(pkt(base).goalLock?.text)?.slice(0, 30))
}

console.log('\n【G toBool：query 通道的字符串必须真的能关掉】')
{
  check("'false' ⇒ false（不是 true）", toBool('false', true) === false, String(toBool('false', true)))
  check("'0' ⇒ false", toBool('0', true) === false, String(toBool('0', true)))
  check("'true' ⇒ true", toBool('true', false) === true)
  check("'1' ⇒ true", toBool('1', false) === true)
  check('真布尔 false ⇒ false', toBool(false, true) === false)
  check('undefined ⇒ 用默认值', toBool(undefined, true) === true && toBool(undefined, false) === false)
  check("'' ⇒ 用默认值", toBool('', true) === true)
  // 行为层：把 autoConfirm 用字符串 'false' 传进去，必须真的关掉
  const base = fresh()
  saveScope(base, 'S', { enabled: true, mode: 'sessions', selectedSessions: [], autoConfirm: 'false' })
  check("saveScope 传字符串 'false' ⇒ 存成 false", scopeFor(loadContinuity(base), 'S').autoConfirm === false, String(scopeFor(loadContinuity(base), 'S').autoConfirm))
  const r = captureContinuityCandidate(base, { kind: 'decision', text: '就按方案 B 定下来', cite: 'dsh:S#turn=5', sourceSession: 'S', project: 'P' }, { autoConfirm: scopeFor(loadContinuity(base), 'S').autoConfirm })
  check('关掉后确实不自动确认', !r.autoConfirmed, JSON.stringify(r.autoConfirmed))
  check('关掉后留在候选', (pkt(base).candidates || []).length === 1, String((pkt(base).candidates || []).length))
}

console.log('\n【H packetItemCount 覆盖所有承载位置（否则导入假报 0）】')
{
  const base = fresh()
  check('空状态 ⇒ 0', packetItemCount(loadContinuity(base)) === 0, String(packetItemCount(loadContinuity(base))))
  saveScope(base, 'S', { enabled: true, mode: 'sessions', selectedSessions: [] })
  // 自动确认会把条目写进 goalLock/constraintLocks/decisions —— 候选数不增加
  captureContinuityCandidate(base, { kind: 'goal', text: '目标一', cite: 'dsh:S#t1', sourceSession: 'S', project: 'P' }, { autoConfirm: true })
  captureContinuityCandidate(base, { kind: 'constraint', text: '必须约束一', cite: 'dsh:S#t2', sourceSession: 'S', project: 'P' }, { autoConfirm: true })
  captureContinuityCandidate(base, { kind: 'decision', text: '就按方案 C 定下来', cite: 'dsh:S#t3', sourceSession: 'S', project: 'P' }, { autoConfirm: true })
  check('候选数仍是 0（都自动确认了）', (pkt(base).candidates || []).length === 0, String((pkt(base).candidates || []).length))
  check('但 packetItemCount = 3（否则导入会假报 0）', packetItemCount(loadContinuity(base)) === 3, String(packetItemCount(loadContinuity(base))))
}

console.log('\n【I 已确认条目可撤销（自动确认必须有撤销出口）】')
{
  const base = fresh()
  saveScope(base, 'S', { enabled: true, mode: 'sessions', selectedSessions: [] })
  captureContinuityCandidate(base, { kind: 'goal', text: '待撤销的目标', cite: 'dsh:S#turn=1', sourceSession: 'S', project: 'P' }, { autoConfirm: true })
  const id = pkt(base).goalLock?.id
  check('拿到目标 id', Boolean(id), String(id))
  const r = rejectContinuityItem(base, id)
  check('撤销成功', r.ok === true, JSON.stringify(r).slice(0, 120))
  check('目标锁已清空', pkt(base).goalLock === null, JSON.stringify(pkt(base).goalLock))
  check('撤销未知 id ⇒ ok:false（不假装成功）', rejectContinuityItem(base, 'no-such-id').ok === false)

  // 决定与约束也要能撤
  captureContinuityCandidate(base, { kind: 'decision', text: '就按方案 D 定下来', cite: 'dsh:S#turn=7', sourceSession: 'S', project: 'P' }, { autoConfirm: true })
  const did = pkt(base).decisions[0]?.id
  check('撤销决定', rejectContinuityItem(base, did).ok === true && pkt(base).decisions.length === 0, String(pkt(base).decisions.length))
}

console.log('\n【J 召回片段（episode）即使 autoConfirm=true 也不自动确认】')
{
  const base = fresh()
  saveScope(base, 'S', { enabled: true, mode: 'sessions', selectedSessions: [] })
  const r = captureContinuityCandidate(base, { kind: 'episode', text: '一段召回命中的历史片段内容', cite: 'dsh:other#turn=3', sourceSession: 'other', project: 'P' }, { autoConfirm: true })
  check('episode 不自动确认', !r.autoConfirmed, JSON.stringify(r.autoConfirmed))
  check('episode 留在候选', (pkt(base).candidates || []).length === 1, String((pkt(base).candidates || []).length))
}

console.log(`\n${process.exitCode ? '❌ 有失败' : '✅ 全部通过'}：${pass} passed`)
