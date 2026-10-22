/**
 * 连续性隔离回归（2026-10-21）
 *
 * 目标：把"跨会话/跨项目泄漏"和"测试残留混进正式状态"变成可重跑判据。
 * 跑法：node test/continuity_isolation.mjs
 */
import assert from 'node:assert/strict'
import { mkdtempSync, readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { pathToFileURL } from 'node:url'
import { dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const mod = await import(pathToFileURL(join(here, '..', 'continuity.js')).href)
const {
  loadContinuity, saveContinuity, saveScope, scopeFor, captureContinuityCandidate, confirmContinuityItem,
  renderContinuityPacket, isSafeContinuityCandidate, resetContinuity, rejectContinuityCandidate,
} = mod

let pass = 0
const check = (name, cond, detail = '') => {
  if (cond) { pass += 1; console.log('  ✓ ' + name) } else { console.log('  ✗ ' + name + (detail ? '\n      ' + detail : '')); process.exitCode = 1 }
}
const fresh = () => mkdtempSync(join(tmpdir(), 'continuity-iso-'))

console.log('\n【1 项目模式：必须精确匹配，空/未知项目一律排除】')
{
  const base = fresh()
  // ★ 2026-10-22 起范围按对话存：saveScope(对话, ...)，注入也按该对话取。
  saveScope(base, 's1', { enabled: true, mode: 'project', project: 'D:\\示例工作区\\示例项目甲' })
  captureContinuityCandidate(base, { kind: 'episode', text: '本项目的决定', cite: 'dsh:s1#turn=1', sourceSession: 's1', project: 'D:\\示例工作区\\示例项目甲' })
  captureContinuityCandidate(base, { kind: 'episode', text: '别的项目的决定', cite: 'dsh:s2#turn=1', sourceSession: 's2', project: 'D:\\示例工作区\\示例项目乙' })
  captureContinuityCandidate(base, { kind: 'episode', text: '没有项目标签', cite: 'dsh:s3#turn=1', sourceSession: 's3', project: '' })
  const text = renderContinuityPacket(loadContinuity(base), { sessionId: 's1' })
  check('本项目候选出现', text.includes('本项目的决定'))
  check('跨项目候选不出现', !text.includes('别的项目的决定'))
  check('无项目标签候选不出现（fail-closed）', !text.includes('没有项目标签'))
}

console.log('\n【2 会话模式：只认本对话 + 显式接上的对话】')
{
  const base = fresh()
  saveScope(base, 's9', { enabled: true, mode: 'sessions', selectedSessions: ['s1'] })
  captureContinuityCandidate(base, { kind: 'episode', text: '接上的会话内容', cite: 'dsh:s1#turn=1', sourceSession: 's1', project: 'p' })
  captureContinuityCandidate(base, { kind: 'episode', text: '没接上的会话内容', cite: 'dsh:s2#turn=1', sourceSession: 's2', project: 'p' })
  const text = renderContinuityPacket(loadContinuity(base), { sessionId: 's9' })
  check('接上的对话出现', text.includes('接上的会话内容'))
  check('没接上的对话不出现（即便同项目）', !text.includes('没接上的会话内容'))
  // ★ 2026-10-22 新语义：本对话**自己**永远在范围内（接上别人不能丢了自己）
  const selfText = renderContinuityPacket(loadContinuity(base), { sessionId: 's2' })
  // s2 没有自己的范围 ⇒ fail-closed 不注入（不是继承 s9 的设置）
  check('别的对话没有自己的范围 ⇒ 不注入（不继承）', selfText === '', selfText.slice(0, 80))
}

console.log('\n【3 候选卫生：插件/运行环境文本不得成为候选】')
{
  const base = fresh()
  saveScope(base, 's1', { enabled: true, mode: 'all' })
  const dirty = [
    '<memory_recall mode="digest" source="dsh-memory-plugin">',
    '<project-continuity mode="active">',
    'Current runtime context. This snapshot supersedes',
    '[工具结果] 一些工具输出',
  ]
  for (const t of dirty) captureContinuityCandidate(base, { kind: 'episode', text: t, cite: 'dsh:s1#turn=1', sourceSession: 's1' })
  check('污染文本全部被拦', loadContinuity(base).packet.candidates.length === 0, JSON.stringify(loadContinuity(base).packet.candidates))
  check('正常文本可通过', isSafeContinuityCandidate('把报告写到 D:\\示例工作区\\reports') === true)
}

console.log('\n【4 确认必须有出处】')
{
  const base = fresh()
  let threw = false
  try { confirmContinuityItem(base, { kind: 'goal', text: '目标' }, { cite: '' }) } catch { threw = true }
  check('无出处确认 ⇒ 抛错', threw)
  const ok = confirmContinuityItem(base, { kind: 'goal', text: '目标' }, { cite: 'dsh:s1#turn=2' })
  check('有出处确认 ⇒ 进入目标锁', ok.packet.goalLock?.text === '目标' && ok.packet.goalLock?.cite === 'dsh:s1#turn=2')
}

console.log('\n【5 reject / reset：可清除，且 reset 必须先备份】')
{
  const base = fresh()
  saveScope(base, 's1', { enabled: true, mode: 'all' })
  const st = captureContinuityCandidate(base, { kind: 'episode', text: '要丢弃的候选', cite: 'dsh:s1#turn=1', sourceSession: 's1' })
  const id = st.packet.candidates[0].id
  const bad = rejectContinuityCandidate(base, 'not-exist')
  check('reject 未知 id ⇒ ok:false（不假装成功）', bad.ok === false)
  const r = rejectContinuityCandidate(base, id)
  check('reject 生效', r.ok === true && loadContinuity(base).packet.candidates.length === 0)

  confirmContinuityItem(base, { kind: 'goal', text: '测试目标' }, { cite: 'runtime-test#goal-1', project: 'PCM_RUNTIME_TEST' })
  const before = readdirSync(join(base, 'continuity')).length
  const reset = resetContinuity(base, { keepScope: false })
  const after = loadContinuity(base)
  check('reset 清空目标锁', after.packet.goalLock === null)
  check('reset 清空候选', after.packet.candidates.length === 0)
  check('reset keepScope=false ⇒ 回到 off', after.enabled === false && after.mode === 'off' && after.project === '')
  check('reset keepScope=false ⇒ 连每个对话的范围一起清', Object.keys(after.scopes || {}).length === 0, JSON.stringify(after.scopes))
  check('reset 留下备份文件', typeof reset.backup === 'string' && readdirSync(join(base, 'continuity')).length === before + 1)
  const backup = JSON.parse(readFileSync(reset.backup, 'utf8'))
  check('备份里仍是旧数据（可回溯）', backup.packet.goalLock?.text === '测试目标')
}

console.log('\n【6 关闭即不注入】')
{
  const base = fresh()
  saveScope(base, 's1', { enabled: false, mode: 'off' })
  confirmContinuityItem(base, { kind: 'goal', text: '不该出现的目标' }, { cite: 'dsh:s1#turn=1' })
  check('enabled=false ⇒ 空包', renderContinuityPacket(loadContinuity(base), { sessionId: 's1' }) === '')
  check('完全没设置过的对话 ⇒ 空包（fail-closed）', renderContinuityPacket(loadContinuity(base), { sessionId: 'never-set' }) === '')
  check('没有 sessionId ⇒ 空包（不猜自己在哪个对话）', renderContinuityPacket(loadContinuity(base), {}) === '')
}

console.log('\n【7 确认时项目标签的取法：显式覆盖 > 条目自带 > 空（不许擦掉/不许冒充）】')
{
  const base = fresh()
  // 不传 project ⇒ 必须保留条目自带的来源项目
  const keep = confirmContinuityItem(base, { kind: 'decision', text: '自带项目的决定', project: 'D:\\示例工作区\\示例项目甲' }, { cite: 'dsh:s1#turn=1' })
  check('不传 project ⇒ 保留条目自带项目', keep.packet.decisions.at(-1)?.project === 'D:\\示例工作区\\示例项目甲')
  // 条目没有项目、也不传 ⇒ 必须是空（fail-closed），不能被 scope 顶替
  const empty = confirmContinuityItem(base, { kind: 'decision', text: '没有项目的决定' }, { cite: 'dsh:s2#turn=1' })
  check('都没有 ⇒ 保持为空（不冒充当前项目）', empty.packet.decisions.at(-1)?.project === '')
  // 显式传 ⇒ 以显式为准
  const override = confirmContinuityItem(base, { kind: 'decision', text: '被覆盖的决定', project: 'old' }, { cite: 'dsh:s3#turn=1', project: 'D:\\agent' })
  check('显式 project ⇒ 覆盖条目自带值', override.packet.decisions.at(-1)?.project === 'D:\\agent')
}

console.log('\n【8 重复确认必须幂等，但同出处的不同条目不得互相吞掉】')
{
  const base = fresh()
  // 同一 cite + 同文本连确认两次 ⇒ 只能有一条（实测 2026-10-21 出现过两条逐字相同）
  confirmContinuityItem(base, { kind: 'decision', text: '同一个决定', project: 'p' }, { cite: 'dsh:s1#turn=5' })
  const twice = confirmContinuityItem(base, { kind: 'decision', text: '同一个决定', project: 'p' }, { cite: 'dsh:s1#turn=5' })
  check('同 cite 同文本 ⇒ 幂等（只有 1 条）', twice.packet.decisions.length === 1, JSON.stringify(twice.packet.decisions.map((d) => d.text)))
  // 同一 cite、不同文本 ⇒ 两条都要在（「改成 A，并定为 B」共用同一轮出处）
  const both = confirmContinuityItem(base, { kind: 'decision', text: '同一轮里的另一个决定', project: 'p' }, { cite: 'dsh:s1#turn=5' })
  check('同 cite 不同文本 ⇒ 两条都保留', both.packet.decisions.length === 2, JSON.stringify(both.packet.decisions.map((d) => d.text)))
  // 候选确认后必须从 candidates 消失（即便它没有 id）
  const b2 = fresh()
  saveContinuity(b2, { enabled: true, mode: 'all' })
  const st = captureContinuityCandidate(b2, { kind: 'decision', text: '带 id 的候选', cite: 'dsh:s2#turn=7', sourceSession: 's2' })
  const cand = st.packet.candidates[0]
  const after = confirmContinuityItem(b2, { ...cand }, { cite: cand.cite })
  check('候选确认后从 candidates 移除', after.packet.candidates.length === 0, JSON.stringify(after.packet.candidates.map((c) => c.text)))
  check('确认后进入 decisions', after.packet.decisions.length === 1)
}

console.log('\n【9 写侧判定：标签取来源、范围按模式分派（生产路径调的就是这个函数）】')
{
  const { candidateCaptureDecision } = await import(pathToFileURL(join(here, '..', 'lib', 'index.js')).href)
  const d = candidateCaptureDecision
  // 关键：来源项目未知时，all / sessions 仍应捕获（否则会静默停止收集）
  check('all + 来源未知 ⇒ 仍捕获（标签留空）', d({ continuity: { enabled: true, mode: 'all' }, sourceProject: '', sessionKey: 's1' }).allowed === true)
  check('sessions + 已选中 + 来源未知 ⇒ 仍捕获', d({ continuity: { enabled: true, mode: 'sessions', selectedSessions: ['s1'] }, sourceProject: '', sessionKey: 's1' }).allowed === true)
  check('sessions + 未选中 ⇒ 不捕获', d({ continuity: { enabled: true, mode: 'sessions', selectedSessions: ['s1'] }, sourceProject: 'p', sessionKey: 's2' }).allowed === false)
  check('project + 匹配 ⇒ 捕获且标签=来源', (() => { const r = d({ continuity: { enabled: true, mode: 'project', project: 'p' }, sourceProject: 'p', sessionKey: 's1' }); return r.allowed === true && r.project === 'p' })())
  check('project + 不匹配 ⇒ 不捕获', d({ continuity: { enabled: true, mode: 'project', project: 'p' }, sourceProject: 'other', sessionKey: 's1' }).allowed === false)
  check('project + 来源未知 ⇒ 不捕获（fail-closed）', d({ continuity: { enabled: true, mode: 'project', project: 'p' }, sourceProject: '', sessionKey: 's1' }).allowed === false)
  check('project + scope 为空 ⇒ 不捕获', d({ continuity: { enabled: true, mode: 'project', project: '' }, sourceProject: 'p', sessionKey: 's1' }).allowed === false)
  check('关闭 ⇒ 不捕获', d({ continuity: { enabled: false, mode: 'all' }, sourceProject: 'p', sessionKey: 's1' }).allowed === false)
  // 标签绝不能等于 scope 值：不匹配时必须不捕获，而不是贴 scope
  check('标签不会被 scope 顶替', d({ continuity: { enabled: true, mode: 'project', project: 'scope-value' }, sourceProject: 'real', sessionKey: 's1' }).project === '')
}

console.log('\n【10 项目模式内再限定对话（用户 2026-10-21：先选工作区，再选工作区内的对话）】')
{
  const base = fresh()
  // 同一工作区内的两个会话 + 另一个工作区的一个会话
  saveScope(base, 'a1', { enabled: true, mode: 'project', project: 'P', selectedSessions: [] })
  captureContinuityCandidate(base, { kind: 'episode', text: 'P 内会话甲的条目', cite: 'dsh:a1#turn=1', sourceSession: 'a1', project: 'P' })
  captureContinuityCandidate(base, { kind: 'episode', text: 'P 内会话乙的条目', cite: 'dsh:a2#turn=1', sourceSession: 'a2', project: 'P' })
  captureContinuityCandidate(base, { kind: 'episode', text: '别的工作区的条目', cite: 'dsh:b1#turn=1', sourceSession: 'b1', project: 'Q' })

  const all = renderContinuityPacket(loadContinuity(base), { sessionId: 'a1' })
  check('未限定对话 ⇒ 整个工作区都注入', all.includes('会话甲的条目') && all.includes('会话乙的条目'), all.slice(0, 120))
  check('未限定对话 ⇒ 别的工作区仍排除', !all.includes('别的工作区的条目'))

  saveScope(base, 'a1', { selectedSessions: ['a1'] })
  const narrowed = renderContinuityPacket(loadContinuity(base), { sessionId: 'a1' })
  check('限定 a1 ⇒ a1 的条目在', narrowed.includes('会话甲的条目'))
  check('限定 a1 ⇒ 同工作区但未选的 a2 被排除（承诺必须兑现）', !narrowed.includes('会话乙的条目'), narrowed.slice(0, 160))

  // 写侧同口径
  const { candidateCaptureDecision } = await import(pathToFileURL(join(here, '..', 'lib', 'index.js')).href)
  check('写侧：project + 已限定且命中 ⇒ 捕获',
    candidateCaptureDecision({ continuity: { enabled: true, mode: 'project', project: 'P', selectedSessions: ['a1'] }, sourceProject: 'P', sessionKey: 'a1', selfKey: 'a1' }).allowed === true)
  check('写侧：project + 已限定但未命中 ⇒ 不捕获',
    candidateCaptureDecision({ continuity: { enabled: true, mode: 'project', project: 'P', selectedSessions: ['a1'] }, sourceProject: 'P', sessionKey: 'a2', selfKey: 'a1' }).allowed === false)
  check('写侧：project + 未限定 ⇒ 同工作区任意会话都捕获',
    candidateCaptureDecision({ continuity: { enabled: true, mode: 'project', project: 'P', selectedSessions: [] }, sourceProject: 'P', sessionKey: 'a2', selfKey: 'a1' }).allowed === true)
}

console.log('\n【11 卫生：运行环境样板 / 别的会话聊天记录 / 工具转储 一律不收（用户 2026-10-21 铁证）】')
{
  const must = [
    ['运行环境样板（截断后只剩前半句）', 'Current working directory: "D:\示例工作区". Current DeepSeek Harness implementation checkout: "D:\示例运行时". [Problem-solving co', false],
    ['别的会话群聊记录', '[用户] 你是「示例项目甲·示例子项」这个群聊的成员：Chief。群成员：Chief、侦察-脑', false],
    ['工具结果转储', '[工具结果] {"ok":true,"hits":[]}', false],
    ['工具调用转储', '[工具调用 bash] {"command":"ls"}', false],
    ['插件注入块', '<memory_recall mode="digest" source="dsh-memory-plugin">', false],
    ['状态包自身', '这是项目连续性状态包。带出处的确认项可作为工作状态参考', false],
    ['超长（>700）', 'x'.repeat(701), false],
    ['正常约束', '必须先把报告写到 D:\示例工作区\reports 再汇报', true],
  ]
  for (const [name, text, expect] of must) {
    check(`卫生·${name} ⇒ ${expect ? '收' : '不收'}`, isSafeContinuityCandidate(text) === expect, String(isSafeContinuityCandidate(text)))
  }
  check('卫生·带 [用户] 前缀的正常内容仍可收（前缀不是污染特征）',
    isSafeContinuityCandidate('[用户] 必须保留出处') === true)
}

console.log('\n【12 捕获规则：只有明确的约束/决定才成为候选，闲聊与粘贴不收】')
{
  const { classifyContinuityStatement } = mod
  check('约束句 ⇒ constraint', classifyContinuityStatement('必须保留出处，不许省略')?.kind === 'constraint')
  check('决定句 ⇒ decision', classifyContinuityStatement('就按方案 A 定下来，改用新接口')?.kind === 'decision')
  check('普通闲聊 ⇒ 不收（旧实现会存成 episode）', classifyContinuityStatement('我们刚才在干嘛') === null)
  check('粘贴的群聊 ⇒ 不收', classifyContinuityStatement('你是「示例项目甲·示例子项」这个群聊的成员：Chief') === null)
  check('工具转储 ⇒ 不收', classifyContinuityStatement('[工具结果] {"ok":true}') === null)
  check('空文本 ⇒ 不收', classifyContinuityStatement('') === null)
}

console.log('\n【13 已确认条目不受候选长度上限影响，但超长要显式报错（不许静默截断）】')
{
  const base = fresh()
  const long = '很长的已确认约束'.repeat(120)
  const ok = confirmContinuityItem(base, { kind: 'constraint', text: long, project: 'p' }, { cite: 'dsh:long#turn=1' })
  check('960 字已确认约束 ⇒ 保留（不被 700 上限丢掉）', ok.packet.constraintLocks.length === 1, String(ok.packet.constraintLocks.length))
  let msg = ''
  try { confirmContinuityItem(base, { kind: 'constraint', text: 'x'.repeat(4001), project: 'p' }, { cite: 'dsh:toolong#turn=1' }) } catch (e) { msg = String(e.message) }
  check('4001 字 ⇒ 显式报 too long（不是静默截断、不是报缺出处）', /too long/.test(msg), msg)
}

console.log('\n【14 空壳不注入（2026-10-22 实测：开着但没选工作区时，旧实现每轮注入 181 字空壳）】')
{
  const base = fresh()
  saveScope(base, 's1', { enabled: true, mode: 'project', project: '' })
  check('开启但无内容 ⇒ 不注入（长度 0）', renderContinuityPacket(loadContinuity(base), { sessionId: 's1' }) === '', String(renderContinuityPacket(loadContinuity(base), { sessionId: 's1' }).length))
  confirmContinuityItem(base, { kind: 'goal', text: '有内容的目标', project: 'p' }, { cite: 'dsh:g#turn=1' })
  saveScope(base, 's1', { project: 'p' })
  check('有确认项 ⇒ 正常注入', renderContinuityPacket(loadContinuity(base), { sessionId: 's1' }).length > 0)
  const b2 = fresh()
  saveScope(b2, 's1', { enabled: true, mode: 'all' })
  captureContinuityCandidate(b2, { kind: 'episode', text: '一条候选', cite: 'dsh:c#turn=1', sourceSession: 's1' })
  check('只有候选 ⇒ 也注入（用户要能看到待确认项）', renderContinuityPacket(loadContinuity(b2), { sessionId: 's1' }).length > 0)
}

console.log('\n【15 导入判定：用户核心目标不能再被丢掉（2026-10-22 决定性缺口）】')
{
  const { classifyImportedStatement } = mod
  const goal = '对比这两种记忆方式、结合论文、其他可用方式、人类的成长学习和记忆方式、GitHub调研合并给出一个新的记忆方案，并新增两个功能，制作方向永远不偏移不会出现幻觉制作，跨对话无缝衔接，以及一个即使上下文填满也不会忘记能够正常发挥功能的能力'
  check('核心目标原话 ⇒ goal（旧实现判 null，导致状态包永远没有目标锁）',
    classifyImportedStatement(goal)?.kind === 'goal', JSON.stringify(classifyImportedStatement(goal)))
  check('约束句仍按严格判定 ⇒ constraint', classifyImportedStatement('必须保留出处，不许省略')?.kind === 'constraint')
  check('决定句仍按严格判定 ⇒ decision', classifyImportedStatement('就按方案 A 定下来')?.kind === 'decision')
  check('问句不判成目标', classifyImportedStatement('需要我做什么？') === null)
  check('闲聊不收', classifyImportedStatement('我们之前在干嘛') === null)
  check('运行环境样板仍拒（放宽不等于放污染）',
    classifyImportedStatement('Current working directory: "D:\示例工作区". Current DSH file policy') === null)
}

console.log('\n【16 候选按内容去重（同一目标在多个 turn 被复述只留一条）】')
{
  const base = fresh()
  saveScope(base, 's1', { enabled: true, mode: 'all' })
  const text = '合并给出一个新的记忆方案，并新增两个功能'
  captureContinuityCandidate(base, { kind: 'goal', text, cite: 'dsh:s1#turn=4', sourceSession: 's1', project: 'p' })
  captureContinuityCandidate(base, { kind: 'goal', text, cite: 'dsh:s1#turn=9', sourceSession: 's1', project: 'p' })
  captureContinuityCandidate(base, { kind: 'goal', text: text + '  ', cite: 'dsh:s1#turn=12', sourceSession: 's1', project: 'p' })
  const cs = loadContinuity(base).packet.candidates
  check('同内容不同出处 ⇒ 只留 1 条', cs.length === 1, String(cs.length))
  check('保留的是最早那条出处', cs[0]?.cite === 'dsh:s1#turn=4', String(cs[0]?.cite))
  // 不同内容仍要都留
  captureContinuityCandidate(base, { kind: 'goal', text: '另一个不同的目标', cite: 'dsh:s1#turn=20', sourceSession: 's1', project: 'p' })
  check('不同内容 ⇒ 各留一条', loadContinuity(base).packet.candidates.length === 2)
}

console.log('\n【17 按对话独立：A 接上了 B、C，B/C 不会因此被接上（2026-10-22 用户核心指正）】')
{
  const base = fresh()
  // A 对话：接上 B、C
  saveScope(base, 'A', { enabled: true, mode: 'sessions', selectedSessions: ['B', 'C'] })
  captureContinuityCandidate(base, { kind: 'episode', text: 'B 对话里的事', cite: 'dsh:B#turn=1', sourceSession: 'B', project: 'P' })
  captureContinuityCandidate(base, { kind: 'episode', text: 'C 对话里的事', cite: 'dsh:C#turn=1', sourceSession: 'C', project: 'P' })
  captureContinuityCandidate(base, { kind: 'episode', text: 'D 对话里的事', cite: 'dsh:D#turn=1', sourceSession: 'D', project: 'P' })
  const a = renderContinuityPacket(loadContinuity(base), { sessionId: 'A' })
  check('A 能看到 B、C', a.includes('B 对话里的事') && a.includes('C 对话里的事'))
  check('A 看不到没接上的 D', !a.includes('D 对话里的事'))

  // ★ 这是用户投诉的那一条：切到 B 对话，B 不该被接上 A 选的那批
  const b = renderContinuityPacket(loadContinuity(base), { sessionId: 'B' })
  check('B 对话不会继承 A 的衔接设置（关键）', b === '', b.slice(0, 120))
  const c = renderContinuityPacket(loadContinuity(base), { sessionId: 'C' })
  check('C 对话同样不继承', c === '', c.slice(0, 120))

  // B 自己开启后，只看自己 + 自己选的
  saveScope(base, 'B', { enabled: true, mode: 'sessions', selectedSessions: [] })
  const b2 = renderContinuityPacket(loadContinuity(base), { sessionId: 'B' })
  check('B 自己开启后能看到自己的内容', b2.includes('B 对话里的事'), b2.slice(0, 120))
  check('B 自己开启后仍看不到 A 的内容（A 没被 B 接上）', !b2.includes('C 对话里的事'))
  // 每份范围互不覆盖
  const st = loadContinuity(base)
  check('两份范围并存', scopeFor(st, 'A').selectedSessions.join() === 'B,C' && scopeFor(st, 'B').selectedSessions.length === 0,
    JSON.stringify({ A: scopeFor(st, 'A').selectedSessions, B: scopeFor(st, 'B').selectedSessions }))
  // 没有 sessionId 不许写（否则又变成一份全局设置）
  let threw = ''
  try { saveScope(base, '', { enabled: true, mode: 'all' }) } catch (e) { threw = String(e.message) }
  check('saveScope 无 sessionId ⇒ 拒绝写', /sessionId is required/.test(threw), threw)
}

console.log('\n【18 旧全局字段不再驱动注入（防止泄漏被"兼容"回来）】')
{
  const base = fresh()
  // 直接把旧字段写成"开启 + 全部对话"，但没有任何 per-session scope
  saveContinuity(base, { enabled: true, mode: 'all', project: 'P', selectedSessions: ['x'] })
  captureContinuityCandidate(base, { kind: 'episode', text: '旧字段时代的候选', cite: 'dsh:x#turn=1', sourceSession: 'x', project: 'P' })
  check('只有旧全局字段 ⇒ 任何对话都不注入', renderContinuityPacket(loadContinuity(base), { sessionId: 'x' }) === '')
  check('只有旧全局字段 ⇒ 无 sessionId 也不注入', renderContinuityPacket(loadContinuity(base), {}) === '')

  // ★ 2026-10-22：旧全局**不得**成为新对话的迁移种子。
  //   旧全局正是那个 bug 的产物（旧宿主 13:09 把点击写成了全局 enabled+sessions，已取证）。
  //   拿它当种子 ⇒ 用户第一次在新对话开开关就继承了来历不明的授权（含别的对话）。
  saveScope(base, 'brand-new', { enabled: true })
  const freshScope = loadContinuity(base).scopes?.['brand-new']
  check('新对话的范围不从旧全局继承 selectedSessions',
    (freshScope?.selectedSessions || []).length === 0, JSON.stringify(freshScope?.selectedSessions))
  check('新对话的范围不从旧全局继承 project',
    (freshScope?.project || '') === '', JSON.stringify(freshScope?.project))
  // 只给 enabled、没给 mode ⇒ 不得因旧全局 mode=all 就变成 all
  check('新对话的 mode 不继承旧全局（不会突然变成 all）',
    (freshScope?.mode || 'off') !== 'all', String(freshScope?.mode))
}

console.log('\n【19 会话 id 两种前缀必须视为同一对话（实测 249 带 session- / 1262 裸 uuid）】')
{
  const { normSessionId } = mod
  check('归一：去掉 session- 前缀', normSessionId('session-abc-123') === 'abc-123')
  check('归一：裸 id 不变', normSessionId('abc-123') === 'abc-123')
  check('归一：大小写统一', normSessionId('Session-ABC') === normSessionId('abc'))

  // 用带前缀的 id 写范围，用裸 id 读 —— 必须是同一份
  const base = fresh()
  saveScope(base, 'session-AAA', { enabled: true, mode: 'sessions', selectedSessions: [] })
  captureContinuityCandidate(base, { kind: 'episode', text: '同一对话的内容', cite: 'dsh:AAA#turn=1', sourceSession: 'AAA', project: 'P' })
  const viaBare = renderContinuityPacket(loadContinuity(base), { sessionId: 'AAA' })
  check('带前缀写、裸 id 读 ⇒ 能读到（关键：否则面板与注入对不上）', viaBare.includes('同一对话的内容'), viaBare.slice(0, 120))
  const viaPrefixed = renderContinuityPacket(loadContinuity(base), { sessionId: 'session-AAA' })
  check('带前缀读 ⇒ 也是同一份', viaPrefixed.includes('同一对话的内容'))

  // 反复写不得产生两个键
  saveScope(base, 'AAA', { enabled: true, mode: 'sessions', selectedSessions: [] })
  const keys = Object.keys(loadContinuity(base).scopes || {})
  check('两种写法只留一个键（不会各存一份）', keys.length === 1, JSON.stringify(keys))

  // 条目来源 id 带前缀、本对话 id 不带 ⇒ 仍算"自己的内容"
  const b2 = fresh()
  saveScope(b2, 'self', { enabled: true, mode: 'sessions', selectedSessions: [] })
  captureContinuityCandidate(b2, { kind: 'episode', text: '自己带前缀的条目', cite: 'dsh:session-self#turn=1', sourceSession: 'session-self', project: 'P' })
  check('来源带前缀、本对话裸 id ⇒ 仍算自己（不会被滤掉）',
    renderContinuityPacket(loadContinuity(b2), { sessionId: 'self' }).includes('自己带前缀的条目'))

  // 接上的对话同理
  const b3 = fresh()
  saveScope(b3, 'self', { enabled: true, mode: 'sessions', selectedSessions: ['session-other'] })
  captureContinuityCandidate(b3, { kind: 'episode', text: '接上的对话内容', cite: 'dsh:other#turn=1', sourceSession: 'other', project: 'P' })
  check('接上的对话两种前缀也能命中',
    renderContinuityPacket(loadContinuity(b3), { sessionId: 'self' }).includes('接上的对话内容'))
}

console.log(`\n${process.exitCode ? '❌ 有失败' : '✅ 全部通过'}：${pass} passed`)
