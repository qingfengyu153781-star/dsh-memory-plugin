/**
 * Offline self-check for dsh-memory-plugin.
 *
 * Proves three things without installing anything and without a running DSH:
 *   1. The entry module imports cleanly (syntax + top-level evaluation).
 *   2. `apply(ctx)` accepts a Cordis-shaped context and registers every surface.
 *   3. The registered `memory_recall` tool actually runs `bin/mem.py recall --json`
 *      and returns the tool contract declared in `output.schema`.
 *
 * Usage:  node test/selfcheck.mjs
 * Exit 0 = every check passed.
 */

import { mkdtempSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'

// apply() kicks off a background incremental ingest; a self-check must not do that
// (it leaked a running child + lock file the first time — 2026-09-30 retrospective).
process.env.DSH_MEMORY_NO_INGEST = '1'
// ★ 2026-10-15(6)：**台账必须写到临时目录**。本自检走的是真 `apply(ctx)`，
//   插件默认把编排台账写进生产目录 ⇒ 实测把 `selfcheck-session` 的失败/跳过灌进生产台账，
//   使 `orchestrate-status` 的"最近 20 轮"全被测试会话占满 ⇒ 误报 `STATUS=broken`。
process.env.DSH_MEMORY_BASE = mkdtempSync(join(tmpdir(), 'dshmem-selfcheck-'))

const here = dirname(fileURLToPath(import.meta.url))
// ★ 2026-10-15：入口必须是**真身** `lib/index.js`（package.json 的 exports/main 都指它）。
//   旧版指向根 `index.js` —— 那是 lib 的一份过期副本，且它的
//   `import '../orchestrator/host.js'` 会解析到 `plugin-src/orchestrator/host.js`（不存在）
//   ⇒ 本测试套件自 2026-10-04 起一跑就 `Cannot find module`，等于没有测试。
const entry = join(here, '..', 'lib', 'index.js')

let failures = 0
const ok = (label) => process.stdout.write(`PASS  ${label}\n`)
const bad = (label, detail) => {
  failures += 1
  process.stdout.write(`FAIL  ${label}\n      ${detail}\n`)
}

/** Minimal Cordis-shaped context: records what the plugin registers. */
function makeCtx() {
  const registered = { tools: [], commands: [], sections: [], routes: [], listeners: [], injected: [] }
  const ctx = {
    tools: { register: (definition) => { registered.tools.push(definition); return () => {} } },
    commands: { register: (definition) => { registered.commands.push(definition); return () => {} } },
    systemPrompt: { section: (section) => { registered.sections.push(section); return () => {} } },
    webServer: { register: (route) => { registered.routes.push(route); return () => {} } },
    on: (event, listener) => { registered.listeners.push({ event, listener }); return () => {} },
    inject: (deps, callback) => {
      registered.injected.push(deps)
      // Simulate every declared dependency being present in this composition.
      const child = { ...ctx }
      callback(child)
      return () => {}
    },
  }
  return { ctx, registered }
}

// ---- 1. import -----------------------------------------------------------
let mod
try {
  mod = await import(pathToFileURL(entry).href)
  ok(`import ${entry}`)
} catch (error) {
  bad(`import ${entry}`, error?.stack ?? String(error))
  process.exit(1)
}

if (typeof mod.apply === 'function') ok('export apply is a function')
else bad('export apply is a function', `got ${typeof mod.apply}`)

if (mod.name === 'dsh-memory-plugin') ok(`export name = ${mod.name}`)
else bad('export name', `got ${JSON.stringify(mod.name)}`)

if (Array.isArray(mod.inject) && mod.inject.includes('tools')) ok(`export inject = ${JSON.stringify(mod.inject)}`)
else bad('export inject includes "tools"', `got ${JSON.stringify(mod.inject)}`)

// ---- 1b. cleanQuery: 召回查询卫生（2026-09-30 实测踩坑：拿运行环境样板文字当查询）----
if (typeof mod.cleanQuery === 'function') {
  ok('export cleanQuery is a function')
  const runtimeContext = [
    'Current runtime context. This snapshot supersedes earlier runtime-context snapshots.',
    '',
    'Current DSH file policy: danger-full-access.',
    'Approval prompts are disabled in this session: actions that require approval are rejected.',
    'Time sampled while preparing turn 5, step 1: 2026-09-30T16:56:21+08:00[Asia/Shanghai]',
  ].join('\n')
  const cases = [
    ['纯运行环境样板 → 跳过', runtimeContext, undefined],
    // ★ 2026-10-15：这条断言**改的是测试，不是代码** —— 原断言写的是"⇒ 跳过(undefined)"，
    //   而 2026-10-04 修正①-b 有意把契约改成「**返回小话文本**，由调用方标 smallTalk」
    //   （原因写在 cleanQuery 里：`MIN_QUERY_CHARS` 默认 6，「继续」只有 2 字，
    //    若先判长度就永远到不了小话判定 ⇒ SMALL_TALK 曾是死代码）。
    //   所以这里断言"只留小话标记、样板一个字不剩"，并另测小话轮判定与 query 组装。
    ['样板 + 小尾巴“继续” → 只留小话标记（样板一字不剩）', `${runtimeContext}\n继续`, '继续'],
    ['真实提问 → 保留', `${runtimeContext}\n上次镜17a 换脸最后是怎么解决的？`, '上次镜17a 换脸最后是怎么解决的？'],
    // 2026-10-01：会话 <会话> 用的是另一套措辞，老名单没盖住 →
    // 那次注入的 query 变成了「Approval policy: ask. …」样板文字
    ['另一套运行环境措辞（Approval policy: ask）→ 只留真问题',
      'Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\n'
      + 'Current DSH file policy: workspace-write.\n\n'
      + 'Approval policy: ask. Operations that require approval may ask through the configured answerers; '
      + 'without an available answerer, the request fails closed.\n'
      + '用户原话保留在本轮人类消息中，以下仅为辅助说明。\n'
      + '- 先去查看工作区里那个「技术工作站」文件夹，而不是凭印象作答。',
      '- 先去查看工作区里那个「技术工作站」文件夹，而不是凭印象作答。'],
    ['纯「Approval policy: ask」样板 → 跳过',
      'Approval policy: ask. Operations that require approval may ask through the configured answerers; '
      + 'without an available answerer, the request fails closed.\n'
      + 'Time sampled while preparing turn 1, step 1: 2026-09-30T22:57:25+08:00[Asia/Shanghai]',
      undefined],
    ['子代理通知 → 跳过',
      'Agent 4ad1281e-af25-4dd6-9749-52c17c955bc0 sent a message: SA5 完成\n'
      + 'Background subagent 4ad1281e-af25-4dd6-9749-52c17c955bc0 finished and will do no further work.',
      undefined],
  ]
  for (const [label, input, expected] of cases) {
    const got = mod.cleanQuery(input)
    if (got === expected) ok(`cleanQuery: ${label}`)
    else bad(`cleanQuery: ${label}`, `expected ${JSON.stringify(expected)} got ${JSON.stringify(got)}`)
  }

  // 小话轮的真实意图（**不是**"返回 undefined"，而是"别拿样板或'继续'去检索"）：
  //   ① 小话轮必须被判出来；② 无 goal 时组装出的 query 必须是空串 ⇒ 调用方据此**跳过召回**。
  if (typeof mod.isSmallTalkTurn === 'function' && typeof mod.buildQueryWithGoal === 'function') {
    const t = mod.cleanQuery(`${runtimeContext}\n继续`)
    if (mod.isSmallTalkTurn(t) === true) ok('小话轮："继续" 被判定为 small talk')
    else bad('小话轮："继续" 被判定为 small talk', `isSmallTalkTurn(${JSON.stringify(t)}) !== true`)
    const q = mod.buildQueryWithGoal('', '')
    if (q === '') ok('小话轮：无 goal 时 query 为空 ⇒ 调用方跳过召回（不拿"继续"去检索）')
    else bad('小话轮：无 goal 时 query 为空', `buildQueryWithGoal('','') = ${JSON.stringify(q)}`)
  } else {
    bad('export isSmallTalkTurn/buildQueryWithGoal is a function', 'missing export')
  }

  // 结构性保证：插件自己注入的消息不参与下一轮召回（不靠字符串匹配）
  if (typeof mod.lastUserText === 'function') {
    const batch = [
      { role: 'user', content: [{ type: 'text', text: '真实提问内容' }], source: { kind: 'user' } },
      {
        role: 'user',
        content: [{ type: 'text', text: '<memory_recall source="dsh-memory-plugin">\nquery: x\n</memory_recall>' }],
        source: { kind: 'plugin', plugin: 'dsh-memory-plugin' },
      },
    ]
    const got = mod.lastUserText(batch)
    if (got === '真实提问内容') ok('lastUserText: 跳过插件自己注入的消息')
    else bad('lastUserText: 跳过插件自己注入的消息', `got ${JSON.stringify(got)}`)
  } else {
    bad('export lastUserText is a function', 'missing export')
  }
} else {
  bad('export cleanQuery is a function', 'missing export')
}

// ---- 2. apply ------------------------------------------------------------
const { ctx, registered } = makeCtx()
try {
  mod.apply(ctx)
  ok('apply(ctx) completed without throwing')
} catch (error) {
  bad('apply(ctx) completed without throwing', error?.stack ?? String(error))
  process.exit(1)
}

const tool = registered.tools.find((t) => t.name === 'memory_recall')
if (tool !== undefined) ok('ctx.tools.register received a tool named memory_recall')
else bad('ctx.tools.register received a tool named memory_recall', `registered: ${registered.tools.map((t) => t.name).join(', ') || '(none)'}`)

if (tool !== undefined) {
  const declared = Object.keys(tool.parameters?.properties ?? {})
  const expected = ['query', 'k', 'session']
  if (expected.every((key) => declared.includes(key))) ok(`tool parameters = ${JSON.stringify(declared)}`)
  else bad('tool parameters', `expected ${JSON.stringify(expected)}, got ${JSON.stringify(declared)}`)

  if (Array.isArray(tool.parameters?.required) && tool.parameters.required.includes('query')) ok('tool requires "query"')
  else bad('tool requires "query"', JSON.stringify(tool.parameters?.required))

  if (typeof tool.output?.render === 'function' && tool.output?.schema !== undefined) ok('tool declares output.schema + output.render')
  else bad('tool declares output.schema + output.render', JSON.stringify(tool.output))

  if (typeof tool.execute === 'function') ok('tool declares execute()')
  else bad('tool declares execute()', typeof tool.execute)
}

if (registered.sections.length === 1) ok(`systemPrompt.section registered: ${registered.sections[0].name} (order ${registered.sections[0].order})`)
else bad('systemPrompt.section registered', `count=${registered.sections.length}`)

if (registered.commands.some((c) => c.name === 'memory-recall')) ok('commands.register registered /memory-recall')
else bad('commands.register registered /memory-recall', JSON.stringify(registered.commands.map((c) => c.name)))

if (registered.routes.some((r) => r.path === '/dsh-memory/health')) ok('webServer.register registered GET /dsh-memory/health')
else bad('webServer.register registered GET /dsh-memory/health', JSON.stringify(registered.routes.map((r) => r.path)))

const events = registered.listeners.map((l) => l.event)
if (events.includes('agent/pre-step')) ok(`ctx.on registered: ${events.join(', ')}`)
else bad('ctx.on registered agent/pre-step', JSON.stringify(events))

// ---- 3. live tool execution ---------------------------------------------
if (tool !== undefined) {
  const controller = new AbortController()
  let value
  try {
    value = await tool.execute(
      { query: '插件', k: 2 },
      { callId: 'selfcheck', name: 'memory_recall', arguments: { query: '插件', k: 2 }, signal: controller.signal },
    )
  } catch (error) {
    bad('tool.execute() resolved', error?.stack ?? String(error))
  }
  if (value !== undefined) {
    if (typeof value.ok === 'boolean' && typeof value.exitCode === 'number'
      && typeof value.stdout === 'string' && typeof value.stderr === 'string') {
      ok(`tool.execute() returned the declared value shape (ok=${value.ok}, exitCode=${value.exitCode}, stdout=${value.stdout.length} bytes)`)
    } else {
      bad('tool.execute() return value matches output.schema', JSON.stringify(value).slice(0, 200))
    }
    if (value.ok) {
      try {
        const parsed = JSON.parse(value.stdout)
        ok(`tool.execute() stdout is JSON (query=${JSON.stringify(parsed.query)}, hits=${parsed.hits?.length ?? 0})`)
      } catch {
        bad('tool.execute() stdout is JSON', value.stdout.slice(0, 200))
      }
      const rendered = tool.output.render({ query: '插件', k: 2 }, value)
      if (Array.isArray(rendered) && rendered[0]?.type === 'text' && rendered[0].text.length > 0) ok('output.render produced model-facing text')
      else bad('output.render produced model-facing text', JSON.stringify(rendered).slice(0, 200))
    } else {
      process.stdout.write(`NOTE  mem.py exited ${value.exitCode}; stderr: ${value.stderr.trim().slice(0, 300)}\n`)
      const rendered = tool.output.render({ query: 'x', k: 1 }, value)
      if (Array.isArray(rendered) && typeof rendered[0]?.text === 'string') ok('output.render handles the failure branch')
      else bad('output.render handles the failure branch', JSON.stringify(rendered).slice(0, 200))
    }
  }
}

// ---- 4. pre-step injection behaviour ------------------------------------
// v0.1.5：这一节改成**确定性**断言（用 __setRecallFn 打桩），不再依赖"真库这次返回了什么"。
// 旧版靠真库返回，注入与否看运气，等于没测。
const preStep = registered.listeners.find((l) => l.event === 'agent/pre-step')
if (preStep !== undefined) {
  mod.__setSessionOrigins({ 'selfcheck-session': 'user' })     // 主会话（TTL 新鲜 ⇒ 不会去 spawn）
  const strongHits = (tag) => {
    // 不同问题命中不同记录（贴近真实：换问题就会换命中集），否则会被"命中集未变"去重挡住
    let h = 0
    for (const ch of String(tag)) h = (h * 31 + ch.codePointAt(0)) % 100000
    return {
      verdict: 'strong', topScore: 9.3, sufficient: true, note: '',
      hits: [{
        session_id: '<会话>-1b5d-4e48-b127-0a47854e8a46', turn: 173 + (h % 7), chunk_id: h,
        time_start: 1790862139396, kind: 'turn', score: 9.3, text: `打桩命中 ${tag}：「廊柱专条 Ø0.22 × 高 2.80」`,
        _session: { origin: 'user', title: 'stub' },
      }],
    }
  }
  mod.__setRecallFn(async ({ query }) => ({ ok: true, exitCode: 0, stdout: JSON.stringify(strongHits(query)), stderr: '' }))

  const mk = (text) => ({ id: 'm-' + text.length, role: 'user', content: [{ type: 'text', text }], source: { kind: 'user' } })
  const call = (turn, step, text) => preStep.listener(
    { agent: { id: 'selfcheck-session' }, messages: [mk(text)], turn, step, signal: new AbortController().signal },
    async () => ({ kind: 'enter', messages: [mk(text)] }),
  )
  const Q1 = '镜17a 换脸最后是怎么解决的'
  const Q2 = '廊柱的尺寸口径最后定的多少'

  const first = await call(1, 1, Q1)
  const injected = first?.kind === 'enter' && first.messages.length === 2 ? first.messages[1] : undefined
  if (injected !== undefined && injected.source?.kind === `plugin:dsh-memory-plugin`) {
    ok(`agent/pre-step injected one plugin message on turn 1 (${injected.content[0].text.length} bytes)`)
  } else {
    bad('agent/pre-step injected one plugin message on turn 1', `messages=${first?.messages?.length}`)
  }

  // 同一轮第二次 step：不得再注入
  const sameTurn = await call(1, 2, Q1)
  if (sameTurn?.kind === 'enter' && sameTurn.messages.length === 1) ok('agent/pre-step does not re-inject within the same turn')
  else bad('agent/pre-step does not re-inject within the same turn', `messages=${sameTurn?.messages?.length}`)

  // 下一轮 + **新问题**：必须再注入（证明"一轮一次"的键是 turn 而不是 step===1）
  const nextTurn = await call(2, 3, Q2)
  if (nextTurn?.kind === 'enter' && nextTurn.messages.length === 2) ok('agent/pre-step injects again on the next turn with a new question')
  else bad('agent/pre-step injects again on the next turn with a new question', `messages=${nextTurn?.messages?.length}`)

  // v0.1.5 新增：**同 query 再问一次 ⇒ 去重跳过**（今天 8 条 query 重复注入 387 次）
  const dupTurn = await call(3, 4, Q2)
  if (dupTurn?.kind === 'enter' && dupTurn.messages.length === 1) ok('agent/pre-step dedupes the same query within the window')
  else bad('agent/pre-step dedupes the same query within the window', `messages=${dupTurn?.messages?.length}`)

  // v0.1.5 新增：**strong 时注入的是摘要块（带出处+取全文指针），不是半截 JSON**
  const body = nextTurn?.messages?.[1]?.content?.[0]?.text ?? ''
  if (body.includes('mode="digest"') && body.includes('取全文') && !body.includes('注入内容超长已截断')) {
    ok('agent/pre-step strong ⇒ digest block with pointers (no mid-JSON truncation)')
  } else {
    bad('agent/pre-step strong ⇒ digest block with pointers', body.slice(0, 120))
  }

  // ★ 2026-10-15(8)：**回声过滤**（用户否决上一轮"形状闸"后的根治版）
  //   用户原话：「…」
  //   ⇒ 判据改成"候选有没有 query 之外的信息增量"，与句子长短/是否重复**无关**。
  const stubRecall = (text) => mod.__setRecallFn(async () => ({
    ok: true, exitCode: 0, stderr: '',
    stdout: JSON.stringify({
      verdict: 'strong', topScore: 9.0, sufficient: true, note: '',
      hits: [{
        session_id: 'session-echo-1b5d-4e48-b127-0a47854e8a46', turn: 12, chunk_id: 4242,
        time_start: Date.now(), kind: 'turn', score: 9.0, text, _session: { origin: 'user' },
      }],
    }),
  }))

  // (a) 候选只是把 query 又抄一遍、毫无具体内容 ⇒ 回声 ⇒ 不注入
  stubRecall('用户本轮表示「我已经重启了」（与上一轮表述相同）')
  const echoTurn = await call(9, 1, '我已经重启了，')
  if (echoTurn?.kind === 'enter' && echoTurn.messages.length === 1) {
    ok('agent/pre-step：回声候选（只抄 query、无具体内容）⇒ 不注入')
  } else {
    bad('agent/pre-step：回声候选 ⇒ 不注入', `messages=${echoTurn?.messages?.length}`)
  }

  // (b) ★ 用户**重复问同一件事** + 候选带答案 ⇒ **必须照常注入**（用户明确要求的那条）
  stubRecall('廊柱截面多大 —— 已定口径：Ø0.22 × 高 2.80（07 §S02）')
  const repTurn = await call(11, 1, '廊柱截面多大')
  if (repTurn?.kind === 'enter' && repTurn.messages.length === 2) {
    ok('agent/pre-step：★ 重复提问 + 候选带答案 ⇒ 照常注入（不因"重复/句子短"被切）')
  } else {
    bad('agent/pre-step：重复提问 + 候选带答案 ⇒ 照常注入', `messages=${repTurn?.messages?.length}`)
  }

  // v0.1.5 新增：子会话 + weak ⇒ 不注入（今天 8 个 teammate 会话被注入 87–271 次、用 0 次）
  mod.__setSessionOrigins({ 'selfcheck-sub': 'subagent' })
  mod.__setRecallFn(async ({ query }) => ({
    ok: true, exitCode: 0, stderr: '',
    stdout: JSON.stringify({ verdict: 'weak', topScore: 5.1, sufficient: false, note: '', hits: [{ session_id: 'session-x', turn: 9, chunk_id: 1, text: '弱命中', _session: {} }] }),
  }))
  const subCall = await preStep.listener(
    { agent: { id: 'selfcheck-sub' }, messages: [mk('这个镜头的口径到底该怎么算才对')], turn: 1, step: 1, signal: new AbortController().signal },
    async () => ({ kind: 'enter', messages: [mk('这个镜头的口径到底该怎么算才对')] }),
  )
  if (subCall?.kind === 'enter' && subCall.messages.length === 1) ok('agent/pre-step subagent + weak ⇒ no injection')
  else bad('agent/pre-step subagent + weak ⇒ no injection', `messages=${subCall?.messages?.length}`)
  mod.__setRecallFn(null)
}

process.stdout.write(failures === 0 ? '\nSELF-CHECK: ALL PASS\n' : `\nSELF-CHECK: ${failures} FAILURE(S)\n`)
process.exit(failures === 0 ? 0 : 1)
