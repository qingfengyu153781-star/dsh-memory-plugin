/**
 * 融合回归：**脑（认知图）并进本插件**这条链，端到端必须成立。
 *
 * 治的病：两套记忆各自往对话里注入 ⇒ 长出"自己注入→自己召回→自己当证据"的自噬闭环。
 * 融合后应当是：**一次 `mem.py recall --json`** 同时带回
 *   ① 本库原文摘录（`hits`）② 脑的经审结论（`brain.items`），
 * 并由 `buildDigestBlock` 渲染进**同一个** `<memory_recall>` 块。
 *
 * 断言（每条都有可核对物）：
 *   A1 子进程 recall --json 返回体里有 `brain` 段（旧版没有 ⇒ 融合没接上）
 *   A2 `brain.status` 必须是 ok / unavailable **之一**（不许缺字段、不许异常崩）
 *   A3 若 ok：items 里的条目必须带 `verification_state`（验证态）——不许拿 `state`（四态资格）冒充
 *   A4 渲染块必须是**一个** `<memory_recall>`：块内含 excerpts 段；若脑有命中，同一块内还要有 conclusions 段
 *   A5 脑不可用时：块里仍要有 conclusions 段并**写明原因**（显式降级，不许静默消失）
 *
 * 用法：node test/brain_fusion.mjs          退出码 0 = 全过
 */
import { spawnSync } from 'node:child_process'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { buildDigestBlock } from '../lib/index.js'

const PY = process.env.DSH_MEMORY_PYTHON ?? 'python'
const here = dirname(fileURLToPath(import.meta.url))          // <plugin>/test
const REPO = process.env.DSH_MEMORY_BASE ?? join(here, '..', '..', '..')   // 上溯三级 = 记忆库
const SCRIPT = process.env.DSH_MEMORY_SCRIPT ?? join(REPO, 'bin', 'mem.py')
const QUERY = '跨版本合并 去重键 调用标识'

let pass = 0
let fail = 0
function ok(cond, msg, extra) {
  if (cond) { pass += 1; console.log('PASS  ' + msg) } else {
    fail += 1
    console.log('FAIL  ' + msg + (extra ? '  ← ' + String(extra).slice(0, 300) : ''))
  }
}

const r = spawnSync(PY, [SCRIPT, 'recall', QUERY, '--k', '2', '--json'], {
  cwd: REPO, encoding: 'utf8', timeout: 120000,
  env: { ...process.env, PYTHONPATH: REPO + '\\src', PYTHONIOENCODING: 'utf-8' },
})
ok(r.status === 0, 'A0 子进程 recall --json 退出码 0', r.stderr)
let parsed = null
try { parsed = JSON.parse(r.stdout) } catch (e) { parsed = null }
ok(parsed !== null, 'A0b 输出是可解析的 JSON', r.stdout?.slice(0, 200))

const brain = parsed?.brain ?? null
ok(brain !== null, 'A1 返回体里有 brain 段（融合已接上）', Object.keys(parsed ?? {}))
ok(brain && ['ok', 'unavailable'].includes(brain.status), 'A2 brain.status ∈ {ok, unavailable}', brain?.status)

const items = Array.isArray(brain?.items) ? brain.items : []
if (brain?.status === 'ok' && items.length > 0) {
  ok(items.every((it) => typeof it.verification_state === 'string' && it.verification_state.length > 0),
    'A3 脑条目都带 verification_state（验证态）', JSON.stringify(items[0]))
  ok(!items.some((it) => it.verification_state === 'DEFER' || it.verification_state === 'ACCEPT'),
    'A3b 没把四态资格（ACCEPT/DEFER）误当验证态', JSON.stringify(items.map((i) => i.verification_state)))
} else {
  console.log('SKIP  A3 脑在线但无命中（' + (brain?.reason ?? 'no_items') + '）——验证态检查跳过')
}

const out = buildDigestBlock({ query: QUERY, parsed, mode: 'digest', budget: 6000 })
const block = out.block
const nOpen = (block.match(/<memory_recall/g) ?? []).length
ok(nOpen === 1, 'A4 渲染出**一个** <memory_recall> 块（不是两个注入源）', nOpen)
ok(block.includes('excerpts'), 'A4b 块内有 excerpts 段')
ok(block.includes('conclusions'), 'A4c 块内有 conclusions 段')
if (brain?.status === 'ok' && items.length > 0) {
  ok(block.includes(String(items[0].id)), 'A4d 结论条目带 id（可引）', items[0].id)
} else {
  ok(/conclusions: 未取到|conclusions: 本库暂无相关结论/.test(block),
    'A5 脑不可用/无命中时，块内**写明原因**（显式降级，不许静默消失）', block.split('\n').slice(0, 8).join(' | '))
}

console.log('\n=== 融合回归：' + pass + ' passed, ' + fail + ' failed ===')
process.exit(fail === 0 ? 0 : 1)
