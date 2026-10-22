/**
 * dsh-memory-plugin · 客户端半侧（web）· v0.2.0
 *
 * 用户要求（2026-10-03）：加一个能开关"注意力编排"的面板。
 *
 * ===== 为什么改用 toolview 卡片 =====
 * 先前想做"右栏 sidebar-right 浮窗"，但那条路在这台机器上走不通：
 *   第三方包的 bundle 被编进了 combo 批次（boot 图里有、文件也在），
 *   但浏览器没有执行它 —— 查了 Code Cache、boot 图、bundle 内容，全部正常，
 *   唯独看不到浏览器侧的报错（DSH 未开调试端口，/plugins 路由要鉴权）。
 *
 * 改用**已被证明可用**的通路：工具调用的 toolview 卡片。
 * `@nagi-ovo/dsh-visualize` 就是靠它渲染卡片的，而它在这台机器上正常工作。
 *
 * 契约（读自 @deepseek-ai/dsh-client-ui-tool 的 README）：
 *   ctx.slots.inject('tool.call.toolview', () =>
 *     ctx.slots.register({ name: 'tool.call.toolview', key: '<wire tool name>' }, View))
 *
 * 所以：模型调用 `memory_orchestrator` 工具 → 结果在对话里渲染成带开关的卡片。
 *
 * 兼容性纪律：
 *   · 每个注册都 try/catch —— 客户端插件抛异常会让整个 GUI 白屏。
 *   · 拿不到宿主路由时显示"宿主未就绪"，而不是死掉。
 */

import { createElement as h, useCallback, useEffect, useState } from 'react'

export const name = 'dsh-memory-plugin-client'

/**
 * ★ 必须声明的服务。
 * 2026-10-03 教训：cordis 会**等 inject 里的服务就绪**才调用 apply()。
 * 漏声明 ⇒ apply() 在服务就绪前跑 ⇒ 拿不到 ⇒ 静默失败。
 */
export const inject = ['slots']

/**
 * 宿主 API 基址（多通道自动探测）。
 *
 * ★ 2026-10-03 修：原来用相对路径 '/dsh-memory/orch'，在 Electron 桌面版里**到不了宿主** ——
 *   页面 origin 是 `dsh-app://app`，相对路径会变成 `dsh-app://app/dsh-memory/orch/...`。
 *   这就是"点开关没反应"的原因（visualize 没这问题，因为它纯前端渲染、不调后端）。
 *
 * 端口纪律：客户端不保存、猜测或枚举旧的 loopback 端口。
 * Web GUI 页面本身就是由当前 DSH 实例提供，因此所有请求只走当前页面的
 * same-origin（相对路径）。重启后页面切到新 origin，请求自然跟随新端口。
 * dsh-app:// 等非 HTTP 页面不再偷偷回落到历史端口；这类页面明确报宿主未就绪，
 * 避免误连到其它本地服务或上一实例。
 * 目标路由注册在宿主 webServer 上，**不走 /api 的 cookie 鉴权**（实测无 cookie 也 200）。
 */
const API_PATH = '/dsh-memory/orch'

/**
 * 解析本插件宿主 HTTP 基址。
 *
 * ★ 2026-10-21 实测（桌面日志为证）：桌面版渲染进程的 origin 是 `dsh-app://app`
 *   （日志里成排出现 `dsh-app://app/assets/…`、`dsh-app://app/plugins/…`）。
 *   旧实现只认 http(s) origin，于是桌面版**永远拿不到基址** ⇒ 面板开关点了没反应。
 *
 * 正确通道不是猜端口，而是框架自己用的那个：
 *   `globalThis.__DSH_TRANSPORT__?.streamBaseUrl ?? document.baseURI`
 *   （见 @deepseek-ai 的 stream-client：远端流就是这么解析宿主 origin 的；
 *     包 README 亦写明"静态桌面页面可通过 __DSH_TRANSPORT__.streamBaseUrl 提供其宿主 HTTP origin"）。
 *
 * 候选顺序（逐个试，第一个通的记住）：
 *   ① http(s) 页面 origin —— Web 版，同源，天然跟随重启后的新端口；
 *   ② __DSH_TRANSPORT__.streamBaseUrl —— 桌面版，由宿主注入，不猜端口；
 *   ③ document.baseURI（仅当它是 http）—— 最后兜底。
 * 三者都没有 ⇒ 明确报错，绝不回落到历史端口。
 */
function originOf(value) {
  try {
    const u = new URL(String(value || ''))
    if (!/^https?:$/i.test(u.protocol)) return ''
    if (!['127.0.0.1', 'localhost', '::1', '[::1]'].includes(u.hostname)) return ''
    return `${u.protocol}//${u.hostname === 'localhost' ? '127.0.0.1' : u.hostname}${u.port ? `:${u.port}` : ''}`
  } catch { return '' }
}

export function candidateBases(globals) {
  const g = globals || (typeof globalThis !== 'undefined' ? globalThis : {})
  const out = []
  const push = (v) => { const o = originOf(v); if (o && !out.includes(o)) out.push(o) }
  try { push(g.location && g.location.origin) } catch { /* noop */ }
  try { push(g.__DSH_TRANSPORT__ && g.__DSH_TRANSPORT__.streamBaseUrl) } catch { /* noop */ }
  try { push(g.document && g.document.baseURI) } catch { /* noop */ }
  return out
}

function currentBase() {
  const bases = candidateBases()
  return bases.length ? bases[0] : ''
}

// 只缓存当前候选集；候选集变化（DSH 重启换端口 / 换载体）立即丢弃缓存。
let goodBase = null
let goodOrigin = null

async function apiFetch(path, init) {
  const key = candidateBases().join('|')
  if (goodOrigin !== key) { goodBase = null; goodOrigin = key }
  const bases = goodBase !== null ? [goodBase] : candidateBases()
  let lastErr = null
  for (const b of bases) {
    const url = `${b}${API_PATH}${path}`
    let timer = null
    try {
      const ctl = typeof AbortController === 'function' ? new AbortController() : null
      timer = ctl ? setTimeout(() => { try { ctl.abort() } catch { /* noop */ } }, 4000) : null
      const r = await fetch(url, {
        headers: { 'content-type': 'application/json' },
        signal: ctl ? ctl.signal : undefined,
        ...init,
      })
      if (timer) clearTimeout(timer)
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
      const j = await r.json()
      if (goodBase !== b) {
        goodBase = b
        try { console.log('[orch-ui] API base =', b, '→', url) } catch { /* noop */ }
      }
      return j
    } catch (e) {
      if (timer) clearTimeout(timer)
      // 当前实例失败时不要继续请求旧端口；清缓存后由下一次刷新重试当前 origin。
      goodBase = null
      lastErr = e
    }
  }
  const tried = candidateBases()
  throw lastErr || new Error(
    tried.length
      ? `宿主未就绪：候选地址都不可用（${tried.join('、')}）`
      : '宿主未就绪：页面既没有 http origin，也没有 __DSH_TRANSPORT__.streamBaseUrl',
  )
}

/**
 * 从会话列表提取"工作区（项目）"选项（纯函数，便于测试）。
 *
 * 为什么要有它（2026-10-21 用户要求）：原来 project 模式是一个自由文本框
 * "项目标识或工作区目录"——用户得手打 `<WORKSPACE>\示例项目甲` 这种路径，打错一个字
 * 就变成"精确匹配不上 ⇒ 一条都不注入"，而且看不出错在哪。
 * 改成选项：直接挑一个真实存在的工作区，或挑一个对话自动取它的工作区。
 */
export function projectOptions(sessions = []) {
  const map = new Map()
  for (const s of Array.isArray(sessions) ? sessions : []) {
    const project = String(s?.project || '').trim()
    if (!project) continue
    const cur = map.get(project) || { project, count: 0 }
    cur.count += 1
    map.set(project, cur)
  }
  const base = (p) => {
    const parts = String(p).split(/[\\/]+/).filter(Boolean)
    return parts.length ? parts[parts.length - 1] : p
  }
  return [...map.values()]
    .sort((a, b) => a.project.localeCompare(b.project))
    .map((x) => ({ ...x, label: `${base(x.project)} — ${x.project}（${x.count} 个对话）` }))
}

/** 某个会话属于哪个工作区；查不到返回 ''（fail-closed，不猜）。 */
export function projectOfSession(sessions = [], sessionId = '') {
  const hit = (Array.isArray(sessions) ? sessions : []).find((s) => s && sameSession(s.sessionId, sessionId))
  return hit ? String(hit.project || '') : ''
}

/**
 * 两级选择器第二级的取数：给定工作区，返回该工作区里**除本对话之外**的会话。
 * 空工作区 = 不筛（"全部工作区"），这是**筛选口径**，不是持久化口径。
 *
 * 为什么单独抽成纯函数（2026-10-22）：用户反馈"工作区选择能力消失"，
 * 根因是这级 UI 只挂在 project 模式分支下、sessions 模式根本没有它。
 * 抽出来后，两级逻辑可以直接被测试执行，不必依赖 React 渲染 ——
 * 否则这类"看起来有、其实条件不成立"的回归只能靠人眼发现。
 */
export function sessionsInWorkspace(sessions = [], workspace = '', selfSessionId = '') {
  const ws = String(workspace ?? '').trim()
  const list = (Array.isArray(sessions) ? sessions : [])
    .filter((s) => s && s.sessionId && !sameSession(s.sessionId, selfSessionId))
  return ws ? list.filter((s) => String(s.project || '') === ws) : list
}

/**
 * 两个 id 是不是同一个对话。
 *
 * ★ 2026-10-22 实测：索引库里 **249 个会话是 `session-<uuid>`、1262 个是裸 `<uuid>`**，
 *   两种前缀并存。按原串比会把"本对话"判成"另一个对话" ⇒ 列表里出现自己、
 *   并且（更严重）面板写的键与注入侧读的键对不上 ⇒ 显示已开启却注入为空。
 *   与宿主侧 continuity.js 的 normSessionId 保持同一口径。
 */
export function sameSession(a, b) {
  const n = (v) => String(v ?? '').trim().replace(/^session-/i, '').toLowerCase()
  const x = n(a)
  return x !== '' && x === n(b)
}

const api = (path, init) => apiFetch(path, init)

/**
 * 构造"确认候选"的请求路径（纯函数，便于用 vm 直接测源码）。
 *
 * 为什么不用 POST body（2026-10-21 实测）：DSH 的 exact 路由读不到 body，
 * 服务端只回 `item required`，UI 上点"确认"必然失败。改走 query 参数。
 *
 * 为什么不带 continuity.project：候选的项目标签必须来自它自己的来源会话；
 * 拿 scope 值覆盖会把"来源未知"冒充成当前项目。
 */
export function continuityItemPath(item = {}) {
  const qs = [
    `item=${encodeURIComponent(JSON.stringify(item))}`,
    `cite=${encodeURIComponent(item.cite || '')}`,
    item.project ? `project=${encodeURIComponent(item.project)}` : '',
  ].filter(Boolean).join('&')
  return `/continuity/item?${qs}`
}

const TOOL_NAME = 'memory_orchestrator'

// ---------------------------------------------------------------------------
// 主组件：常驻小胶囊 + 点开变大面板
// ---------------------------------------------------------------------------

/**
 * 常驻席位（输入框 dock）空间很小，所以默认显示一个**可点的小胶囊**：
 *   [◈ 编排 关]   ← 一眼看到状态
 * 点一下展开成完整面板（开关 / 模型 / 状态 / 留痕）。
 */
export function OrchestratorCard(props) {
  // ★ 2026-10-22：本对话 id。`conversation.input.dock` 的 standardProps 里就有 `sessionId`
  //   （由 cordis_inspect_query 读到的实时契约，不是猜的）。
  //   连续性范围**按对话存**，所以没有它就不能写 —— 宁可不给按钮，也不写成全局。
  const selfSessionId = String((props && props.sessionId) || '')
  const [open, setOpen] = useState(false)
  const [st, setSt] = useState(null)
  const [continuity, setContinuity] = useState(null)
  const [continuityBusy, setContinuityBusy] = useState(false)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [models, setModels] = useState([])
  const [continuitySessions, setContinuitySessions] = useState([])
  // ★ 会话列表取失败时要能说出来。默认 null = 还没取过；'' = 取到了；
  //   非空 = 失败原因。为什么必须单独记（2026-10-22）：`/continuity/sessions`
  //   内部要 spawn mem.py（超时 15s），实测有整次返回 `{ok:false, sessions:[]}` 的情况。
  //   原代码 `setContinuitySessions(sr.sessions || [])` 把失败当"没有会话"处理 ⇒
  //   工作区下拉**空着** ⇒ 用户看到的就是"工作区选择功能没了"。静默失效。
  const [sessionsErr, setSessionsErr] = useState(null)
  const [showLog, setShowLog] = useState(false)
  const [log, setLog] = useState([])
  // ★★ 2026-10-22 用户反馈：工作区选择能力消失，希望能先选工作区、再选其中的对话。
  //   两级选择器（先选工作区 → 再选该工作区里的对话）**代码一直在**（projectOptions/projectOfSession），
  //   但它原来只挂在 `mode === 'project'` 这条分支下，而"开启本对话的连续性"默认写的是 `mode: 'sessions'`
  //   ⇒ 那条分支永不成立 ⇒ 选择器整段被藏掉。**代码没丢，是入口条件把它藏了。**
  //   修法：sessions 模式也给它一级「工作区」筛选。
  //   注意这是**纯客户端筛选**：不改持久化格式（scopes 仍按 sessionId 存）、不改注入语义，
  //   所以上一轮验证过的"A 看得见 B/C，B 和 C 不继承"不受影响。
  //   null = 还没默认过；'' = 用户主动选了"全部工作区"（不能被默认值再改回去）。
  const [wsFilter, setWsFilter] = useState(null)

  const refresh = useCallback(async () => {
    try {
      const s = await api('/stats')
      setSt(s)
      // ★ 2026-10-22：读的必须是**本对话自己的**范围。
      //   不带 sessionId 就只能读到全局旧字段，面板会显示"已开启"而实际一条都不注入 —— 假报告。
      const c = await api(selfSessionId ? `/continuity?sessionId=${encodeURIComponent(selfSessionId)}` : '/continuity')
      setContinuity(c)
      // ★ 会话列表：失败必须**说出来**，不能当成"没有会话"。
      //   实测过整次返回 `{ok:false, sessions:[]}`（宿主里 spawn mem.py 有 15s 上限），
      //   老代码把它渲染成空工作区下拉 ⇒ 用户看到"工作区选择没了"。静默失效。
      //   失败先重试一次，仍失败就记原因、由面板显式提示。
      let sr = await api('/continuity/sessions')
      if (!sr || sr.ok === false || !Array.isArray(sr.sessions)) {
        await new Promise((r) => setTimeout(r, 1200))
        sr = await api('/continuity/sessions')
      }
      // ★ 失败与"确实没有会话"必须分开（2026-10-22 自查）：
      //   混成一个字段会让**合法的空列表**被渲染成红字报错，那是另一种假警报。
      //   sessionsErr: null = 还没取过；'' = 取成功；非空 = 失败原因。
      if (sr && sr.ok !== false && Array.isArray(sr.sessions)) {
        setContinuitySessions(sr.sessions)
        setSessionsErr('')
      } else {
        setContinuitySessions([])
        setSessionsErr(String((sr && sr.error) || '会话列表请求失败（宿主未返回 sessions 数组）'))
      }
      setErr('')
    } catch (e) { setErr(String(e?.message ?? e)) }
  }, [selfSessionId])

  // 会话列表到手后，工作区一级默认落在**本对话所在的工作区**，
  // 而不是让用户每次从空选择开始（这是"先选工作区"这个入口默认就该有的样子）。
  // 只默认一次：一旦用户自己改过（含主动选"全部工作区" = ''），就不再覆盖。
  useEffect(() => {
    if (wsFilter !== null) return
    if (!continuitySessions.length) return
    setWsFilter(projectOfSession(continuitySessions, selfSessionId) || '')
  }, [continuitySessions, selfSessionId, wsFilter])

  const patchContinuity = useCallback(async (p) => {
    // 没有本对话 id 就**不写**：写下去只会变成全局设置，正是要消灭的那个 bug。
    if (!selfSessionId) { setErr('拿不到本对话 id，无法保存（范围按对话存，不能写成全局）'); return }
    setContinuityBusy(true)
    try {
      const qs = Object.entries(p).map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(Array.isArray(v) ? JSON.stringify(v) : String(v))}`).join('&')
      const c = await api(`/continuity?sessionId=${encodeURIComponent(selfSessionId)}&${qs}`, { method: 'POST' })
      if (c && c.ok === false) throw new Error(c.error || '保存失败')
      setContinuity(c); setErr('')
    } catch (e) { setErr(String(e?.message ?? e)) }
    finally { setContinuityBusy(false) }
  }, [selfSessionId])

  const confirmCandidate = useCallback(async (item) => {
    setContinuityBusy(true)
    try {
      // ★ 2026-10-21 修：改走 query 参数。DSH 的 exact 路由读不到 POST body
      //   （实测返回 `item required`），所以 body 写法在 UI 上必然失败。
      //   路径由 continuityItemPath() 生成（纯函数，测试直接执行同一份源码）。
      const r = await api(continuityItemPath(item))
      if (!r.ok) throw new Error(r.error || '确认失败')
      const c = await api(selfSessionId ? `/continuity?sessionId=${encodeURIComponent(selfSessionId)}` : '/continuity')
      setContinuity(c); setErr('')
    } catch (e) { setErr(String(e?.message ?? e)) }
    finally { setContinuityBusy(false) }
  }, [selfSessionId])

  /**
   * 撤销一条**已生效**的条目（自动确认必须能撤销）。
   * 走 query 的 rejectItem 通道 —— 与其它写路由同口径（exact 路由读不到 body）。
   */
  const revokeItem = useCallback(async (item) => {
    if (!item?.id) { setErr('这条没有 id，无法撤销'); return }
    setContinuityBusy(true)
    try {
      const qs = `rejectItem=${encodeURIComponent(item.id)}${selfSessionId ? `&sessionId=${encodeURIComponent(selfSessionId)}` : ''}`
      const r = await api(`/continuity?${qs}`)
      if (!r || r.ok === false) throw new Error(r?.error || '撤销失败')
      const c = await api(selfSessionId ? `/continuity?sessionId=${encodeURIComponent(selfSessionId)}` : '/continuity')
      setContinuity(c)
      setErr(`已撤销：${String(item.text || '').slice(0, 40)}`)
    } catch (e) { setErr(String(e?.message ?? e)) }
    finally { setContinuityBusy(false) }
  }, [selfSessionId])

  useEffect(() => {
    refresh()
    api('/models').then((m) => setModels(m.models || [])).catch(() => {})
    const t = setInterval(refresh, 5000)
    return () => clearInterval(t)
  }, [refresh])

  const patch = useCallback(async (p) => {
    setBusy(true)
    try {
      // 乐观更新：点下去立刻反映（不等网络）
      setSt((old) => ({ ...(old || {}), ...p }))
      // ★ 2026-10-03：改用 **GET + query 参数** 写配置。
      //   原因：POST 的 body 在 DSH 的 exact 路由里读不到（实测写不进去 ⇒
      //   点开关"勾上又跳回去"）。query 通道已本地实测可行，且不依赖 body。
      const qs = Object.entries(p)
        .filter(([, v]) => v !== undefined && v !== null)
        .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
        .join('&')
      await api(`/config?${qs}`)
      // 写完重新读一次：以服务器实际落盘的状态为准
      const fresh = await api('/stats')
      setSt((old) => ({ ...(old || {}), ...fresh, modelMode: fresh.modelMode || (p.modelMode ?? old?.modelMode) }))
      setErr('')
    } catch (e) {
      setErr(String(e?.message ?? e))
      try { const fresh = await api('/stats'); setSt(fresh) } catch { /* noop */ }
    } finally { setBusy(false) }
  }, [])

  const loadLog = useCallback(async () => {
    try {
      const r = await api('/decisions?n=15')
      setLog(r.decisions || []); setShowLog(true)
    } catch (e) { setErr(String(e?.message ?? e)) }
  }, [])

  const enabled = !!(st && st.enabled)
  const mode = (st && st.modelMode) || 'follow'
  const last = st && st.last

  // ---- 收起态：一行小胶囊（常驻可见；只占内容宽，不撑满整行）----
  if (!open) {
    return h('div', { style: S.pillRow },
      h('button', {
        style: enabled ? S.pillOn : S.pillOff,
        onClick: () => setOpen(true),
        title: (err ? ('宿主未就绪：' + err) : '点击打开注意力编排设置')
          + (st && st.total ? `（已编排 ${st.total} 次）` : ''),
      },
        h('span', { style: S.dotOnOff(enabled) }, '●'),
        h('span', { style: S.pillLabel }, '编排'),
      ),
    )
  }

  // ---- 展开态：完整面板 ----
  return h('div', { style: S.wrap },
    h('div', { style: S.head },
      h('span', { style: S.title }, '注意力编排'),
      h('span', { style: enabled ? S.badgeOn : S.badgeOff }, enabled ? '已开启' : '已关闭'),
      h('button', { style: S.close, onClick: () => setOpen(false), title: '收起' }, '×'),
    ),
    err ? h('div', { style: S.warn }, '宿主未就绪：' + err) : null,

    // ★★ 2026-10-22：状态块放在**面板最顶部**。
    //   理由（沙箱用 DOM 坐标量出来的，不是猜的）：
    //     ① 最初排在 60 个会话列表之后 ⇒ 撤销按钮 top=989，视口高 871 ⇒ 视口外；
    //     ② 提到会话列表之前仍不够 ⇒ 面板自身 maxHeight:60vh + overflow:auto，
    //        元素虽在视口坐标内，却超出**滚动容器的 clip 框**（offsetInClip 343 > clientHeight 322）
    //        ⇒ 照样被裁掉。第一版判据只比视口、没比 clip，量错了对象。
    //   结论：只有放到顶部，才能保证"打开面板第一眼就看见机器自动确认了什么"。
    //   这也是自动确认唯一的纠错入口 —— 看不见就等于没有撤销能力。
    (() => {
      const p = continuity?.packet || {}
      const sc2 = continuity?.scope || {}
      const rows = []
      const push = (label, it) => { if (it && it.text) rows.push({ label, it }) }
      push('目标锁', p.goalLock)
      for (const it of (p.constraintLocks || [])) push('硬约束', it)
      for (const it of (p.decisions || [])) push('决定', it)
      for (const it of (p.openItems || [])) push('未完成', it)
      const cands = Array.isArray(p.candidates) ? p.candidates : []
      if (rows.length === 0 && cands.length === 0) return null
      return h('div', null,
        rows.length > 0 && h('div', { style: S.candidateBox },
          h('div', { style: S.label }, `已生效的条目（${rows.length}）—— 自动确认的也可撤销`),
          ...rows.slice(-10).map(({ label, it }) => h('div', { key: it.id, style: S.candidateRow },
            h('span', { style: S.candidateText },
              `[${label}${it.confirmedBy === 'auto' ? '·自动' : it.confirmedBy === 'human' ? '·人工' : ''}] ${it.text}${it.cite ? `〔${it.cite}〕` : ''}`),
            h('button', { style: S.miniBtn, disabled: continuityBusy || !it.id,
              onClick: () => revokeItem(it) }, '撤销'),
          )),
        ),
        cands.length > 0 && h('div', { style: S.candidateBox },
          h('div', { style: S.label }, sc2.autoConfirm === false
            ? '待确认候选（确认后才会成为方向/约束/决定）'
            : '待确认候选（自动确认已开；这些是问句或不比现有目标更实质的，需要你判断）'),
          ...cands.slice(-6).map((item) => h('div', { key: item.id, style: S.candidateRow },
            h('span', { style: S.candidateText }, `${item.text}${item.cite ? `〔${item.cite}〕` : ''}`),
            h('button', { style: S.miniBtn, disabled: continuityBusy || !item.cite, onClick: () => confirmCandidate(item) }, '确认'),
          )),
        ),
      )
    })(),
    h('div', { style: S.hint },
      enabled
        ? '每轮另调一次模型挑选"该给主模型看什么"，调完即弃，它的上下文始终干净。'
        : '关闭时不介入，行为与原来完全一致。'),

    h('div', { style: S.sep }),
    h('div', { style: S.label }, '项目连续性记忆（按对话独立）'),
    h('div', { style: S.hint }, '每个对话各存一份衔接设置：在这里开启只影响本对话，切到别的对话不会继承。开启后，目标锁、硬约束、已确认决定和未完成事项从外部状态包恢复；上下文变满时仍可继续工作。'),
    // ★ 2026-10-22：明确显示"这份设置属于哪个对话"，否则用户无法判断自己改的是哪一份。
    h('div', { style: S.kv }, selfSessionId ? `本对话：${selfSessionId.slice(0, 18)}…` : '⚠ 拿不到本对话 id：无法保存（不会写成全局）'),
    (() => {
      // ★ 关键：读的是**本对话自己的** scope，不是全局旧字段。
      //   读全局会让面板显示"已开启"而注入侧一条都不给 —— 正是用户投诉的那种"看起来有、实际没有"。
      //
      // ★★ 2026-10-22 用户反馈：那个开关点不动（勾上立刻弹回）：
      //   根因是宿主侧（host）还是旧版本 —— 它不返回 scope 字段，于是这里 sc 恒为 {}，
      //   勾上立刻被下一次 refresh 覆盖回"未开启"，看起来就是"点不开"。
      //
      // ★ 判据必须分**三态**（2026-10-22 自查发现，差点写成假警报）：
      //   ① continuity === null ⇒ 还在加载（refresh 要等 mem.py，可达数秒）；
      //      这里若直接报"宿主版本过旧"，**每次打开面板都会先误报一次** —— 判据量错了对象。
      //   ② 已拿到响应但没有 scope 字段 ⇒ 才是真的宿主版本旧，明确提示重启；
      //   ③ 有 scope ⇒ 正常渲染。
      if (continuity === null) {
        return h('div', { style: S.hint }, continuityBusy ? '正在读取本对话的设置…' : '正在读取本对话的设置（首次可能需要几秒）…')
      }
      const hostReady = Object.prototype.hasOwnProperty.call(continuity, 'scope')
      if (!hostReady) {
        return h('div', null,
          h('div', { style: S.warn }, '⚠ 宿主侧还是旧版本：本次请求没有返回「本对话的设置」，所以开关点了会立刻弹回去。'),
          h('div', { style: S.hint }, '这是插件宿主模块未重新加载造成的（面板文件已更新、宿主进程还是旧的）。完全退出 DSH 再启动即可；不用改任何设置。'),
          h('div', { style: S.hint }, `诊断：响应里缺少 scope 字段（sessionId=${selfSessionId ? selfSessionId.slice(0, 18) + '…' : '未取到'}）`),
        )
      }
      const sc = continuity.scope || {}
      const scEnabled = !!sc.enabled
      const scMode = sc.mode || 'off'
      const picked = Array.isArray(sc.selectedSessions) ? sc.selectedSessions : []
      // ★ 身份比较一律走 sameSession（两种前缀并存），否则"本对话"会被当成"另一个对话"，
      //   列表里出现自己，勾选状态也会因为前缀不同而显示成没勾上。
      const isPicked = (id) => picked.some((x) => sameSession(x, id))
      // "要接上的其他对话"里不该出现本对话自己（本对话自动在范围内）
      const others = continuitySessions.filter((s) => !sameSession(s.sessionId, selfSessionId))
      const toggle = (id) => {
        const next = picked.filter((x) => !sameSession(x, id))
        if (!isPicked(id)) next.push(id)
        patchContinuity({ selectedSessions: [...next] })
      }
      const importer = (ids, label) => h('button', { style: { ...S.btn, marginTop: '6px' }, disabled: continuityBusy || !selfSessionId || ids.length === 0,
        onClick: async () => {
          setContinuityBusy(true)
          try {
            const qs = `sessionId=${encodeURIComponent(selfSessionId)}&sessionIds=${encodeURIComponent(JSON.stringify(ids.slice(0, 5)))}`
            const r = await api(`/continuity/import?${qs}`)
            if (!r.ok) throw new Error(r.error || '导入失败')
            const c = await api(`/continuity?sessionId=${encodeURIComponent(selfSessionId)}`)
            setContinuity(c); setErr('')
            // ★ 2026-10-22：导入结果必须说清"自动生效了几条"。
            //   自动确认下条目直接进目标/约束/决定，**候选数不增加**；
            //   只报候选会让人以为"什么都没导入"（假报 0）。
            if (r.imported) {
              const auto = Number(r.autoConfirmed || 0)
              setErr(`已导入 ${r.imported} 条${auto ? `（其中 ${auto} 条已自动生效，无需手动点确认）` : '（待确认）'}${r.goalLocked ? '；目标锁已建立' : ''}`)
            } else {
              setErr(`已扫描 ${r.scanned ?? 0} 条用户原话，没有发现明确的目标/约束/决定${r.note ? '；' + r.note : ''}`)
            }
          } catch (e) { setErr(String(e?.message ?? e)) }
          finally { setContinuityBusy(false) }
        } }, `从这些对话导入（${label}）`)
      return h('div', null,
        h('label', { style: S.row },
          h('input', { type: 'checkbox', checked: scEnabled, disabled: continuityBusy || !selfSessionId,
            onChange: (e) => patchContinuity({ enabled: e.target.checked, mode: e.target.checked ? (scMode === 'off' ? 'sessions' : scMode) : 'off' }), style: S.check }),
          h('span', { style: S.switchLabel }, scEnabled ? '关闭本对话的连续性' : '开启本对话的连续性'),
        ),
        h('div', { style: S.label }, '本对话衔接范围'),
        h('select', { style: S.select, disabled: continuityBusy || !scEnabled || !selfSessionId,
          value: scMode, onChange: (e) => patchContinuity({ mode: e.target.value }) },
          h('option', { value: 'off', style: S.option }, '关闭'),
          h('option', { value: 'sessions', style: S.option }, '接上我选的对话'),
          h('option', { value: 'project', style: S.option }, '整个工作区'),
          h('option', { value: 'all', style: S.option }, '全部相关对话'),
        ),

        // ---- 自动确认（2026-10-22 用户指正：不能靠人逐条点，否则无法长线运行）----
        h('label', { style: S.row },
          h('input', { type: 'checkbox', checked: sc.autoConfirm !== false, disabled: continuityBusy || !selfSessionId,
            onChange: (e) => patchContinuity({ autoConfirm: e.target.checked }), style: S.check }),
          h('span', { style: S.switchLabel }, '自动确认（不需要我逐条点）'),
        ),
        h('div', { style: S.hint },
          sc.autoConfirm === false
            ? '当前：关 —— 每条都要你手动点「确认」才会生效。'
            : '当前：开 —— 带出处的目标/约束/决定直接生效。问句与"已有目标"不会被自动改写（人锁定的目标永不被自动替换）。'),

        // ★★ 2026-10-22 沙箱实测发现的真实可用性缺陷（DOM 坐标量出来的，不是猜的）：
        //   「已生效的条目 / 撤销」原本排在 60 个会话列表之后 ⇒ 撤销按钮 top=989，视口只有 871，
        //   先在视口外；提到会话列表之前后仍不够 —— 面板自身 maxHeight:60vh + overflow:auto，
        //   元素虽落在视口坐标内，却超出**滚动容器的 clip 框**（offsetInClip 343 > clientHeight 322），
        //   照样被裁掉（这一层是我第一次修完才量出来的，上一版判据只比了视口，量错了对象）。
        //   ⇒ 最终位置：面板**最顶部**（标题正下方），保证一打开就在可视区内。
        //   （渲染代码已上移到 head 之后，这里不再重复渲染 —— 重复会出现两份、6 个撤销按钮。）

        // ---- 未开启时也要说明"开启后有什么" ----
        //   ★ 2026-10-22 用户反馈"工作区选择能力消失"的第二种情形：本对话没开启连续性时，
        //     选择器按设计整段不渲染 ⇒ 界面上只剩一个勾选框，用户无从知道这里本来能
        //     "先选工作区、再选对话"。**能力存在但看不见，与能力不存在，在用户眼里一样。**
        //     所以关闭态给一行说明（不是假控件：这里不渲染任何下拉，避免"点了没反应"）。
        !scEnabled && h('div', { style: S.hint },
          '开启后这里会出现：① 先选工作区 → ② 在该工作区里勾要接上的对话，再点「从这些对话导入」。'),

        // ---- 接上我选的对话：两级选择（① 先选工作区 → ② 再勾该工作区里的对话）----
        //   ★ 2026-10-22 用户反馈"工作区选择能力消失"：这一级原来根本不存在 ——
        //     sessions 模式给的是一张**跨所有工作区的大平铺列表**（最多 60 条），
        //     工作区只作为每条末尾的灰字 `· <WORKSPACE>\示例项目甲`，既不能先筛、也看不出边界。
        //     现在补上一级工作区下拉：默认落本对话所在工作区，可切到"全部工作区"。
        //     这一级**只在客户端筛**，不写进 scope —— 选了工作区 ≠ 让整个工作区都生效，
        //     否则会重新引入跨对话继承，破坏上一轮验过的隔离。
        (scEnabled && scMode === 'sessions') && (() => {
          const wss = projectOptions(continuitySessions)
          const curWs = wsFilter === null ? '' : String(wsFilter)
          const knownWs = wss.some((o) => o.project === curWs)
          const inWs = sessionsInWorkspace(continuitySessions, curWs, selfSessionId)
          return h('div', null,
            // ★ 列表取失败时必须显式说出来。否则下拉空着，用户只会看到"工作区选择没了"
            //   —— 那正是这次报障的样子（失败与"没有会话"在界面上长得一模一样）。
            sessionsErr ? h('div', { style: S.warn },
              `⚠ 取会话列表失败：${sessionsErr}`) : null,
            sessionsErr ? h('button', { style: { ...S.btn, marginTop: '4px' }, disabled: continuityBusy,
              onClick: () => { refresh() } }, '重新取列表') : null,
            h('div', { style: S.label }, '① 先选工作区'),
            h('select', { style: S.select, disabled: continuityBusy, value: curWs,
              onChange: (e) => setWsFilter(e.target.value) },
              h('option', { value: '', style: S.option }, `全部工作区（${others.length} 个对话）`),
              (curWs && !knownWs) ? h('option', { value: curWs, style: S.option }, `${curWs}（当前值，不在最近列表中）`) : null,
              ...wss.map((o) => h('option', { key: o.project, value: o.project, style: S.option }, o.label)),
            ),
            h('div', { style: S.label }, `② 再勾要接上的对话（本工作区 ${inWs.length} 个 · 已选 ${picked.length} 个）`),
            h('div', { style: S.hint }, '本对话自动包含，不用勾。'),
            inWs.length === 0
              ? h('div', { style: S.hint }, '这个工作区在最近 200 个对话里没有其他会话；可把上面切到「全部工作区」。')
              : h('div', { style: S.sessionPicker },
                  ...inWs.slice(0, 60).map((s) => h('label', { key: s.sessionId, style: S.row },
                    h('input', { type: 'checkbox', checked: isPicked(s.sessionId), disabled: continuityBusy,
                      onChange: () => toggle(s.sessionId), style: S.check }),
                    h('span', { style: S.radioLabel }, s.title || s.sessionId.slice(0, 12)),
                  )),
                ),
            importer(picked, `${picked.length} 个`),
            h('div', { style: S.hint }, '只从这些对话里取内容；没勾的对话不会进来。导入的是"待确认候选"，不是事实。'),
          )
        })(),

        // ---- 整个工作区：先选工作区，可在其内再限定对话 ----
        (scEnabled && scMode === 'project') && (() => {
          const opts = projectOptions(continuitySessions)
          const cur = sc.project || ''
          const known = opts.some((o) => o.project === cur)
          // project 模式下空工作区是 fail-closed（一条都不注入），所以未选时列表必须为空 ——
          // 不能直接套 sessionsInWorkspace('') 的"不筛"语义，否则会把全部对话都列出来。
          const inProject = cur ? sessionsInWorkspace(continuitySessions, cur, selfSessionId) : []
          return h('div', null,
            h('select', { style: S.select, disabled: continuityBusy, value: cur,
              onChange: (e) => patchContinuity({ project: e.target.value, selectedSessions: [] }) },
              h('option', { value: '', style: S.option }, '— 选择工作区（项目）—'),
              // 旧值不在最近列表里也要能看见，否则 select 会静默显示第一项，看起来像"自己变了"
              (cur && !known) ? h('option', { value: cur, style: S.option }, `${cur}（当前值，不在最近列表中）`) : null,
              ...opts.map((o) => h('option', { key: o.project, value: o.project, style: S.option }, o.label)),
            ),
            // 空项目在 project 模式下是 fail-closed：一条都不会注入。必须说出来，否则"开了却没效果"。
            !cur ? h('div', { style: S.warn }, '还没选工作区：这个模式下未选工作区不会注入任何条目。') : null,
            (cur && !known && opts.length > 0) ? h('div', { style: S.warn }, '当前值不在最近 200 个对话的工作区里，可能已改名或来自更早的会话。') : null,
            cur ? h('div', null,
              h('div', { style: S.label }, `工作区内的其他对话（${inProject.length}）`),
              inProject.length === 0
                ? h('div', { style: S.hint }, '这个工作区在最近 200 个对话里没有其他会话。')
                : h('div', { style: S.sessionPicker },
                    h('label', { style: S.row },
                      h('input', { type: 'checkbox', checked: picked.length === 0, disabled: continuityBusy,
                        onChange: () => patchContinuity({ selectedSessions: [] }), style: S.check }),
                      h('span', { style: S.radioLabel }, '整个工作区（不限定对话）'),
                    ),
                    ...inProject.slice(0, 60).map((s) => h('label', { key: s.sessionId, style: S.row },
                      h('input', { type: 'checkbox', checked: isPicked(s.sessionId), disabled: continuityBusy,
                        onChange: () => toggle(s.sessionId), style: S.check }),
                      h('span', { style: S.radioLabel }, s.title || s.sessionId.slice(0, 12)),
                    )),
                  ),
              picked.length > 0 ? h('div', { style: S.hint }, `已限定 ${picked.length} 个对话：只从这些对话里取，工作区内其它对话不参与。`) : null,
              importer(picked.length > 0 ? picked : inProject.map((s) => s.sessionId),
                `${picked.length > 0 ? picked.length : inProject.length} 个`),
              h('div', { style: S.hint }, '导入的是"待确认候选"，不是事实：在上方「待确认候选」里逐条点确认后才会进入状态包。'),
            ) : null,
          )
        })(),

        (scEnabled && scMode === 'all') && h('div', { style: S.hint },
          '全部相关对话：本对话会从所有对话里取相关内容。这个范围最宽，跨项目内容也会进来。'),

      )
    })(),

    h('div', { style: S.sep }),
    h('label', { style: S.row },
      h('input', {
        type: 'checkbox', checked: enabled, disabled: busy,
        onChange: (e) => patch({ enabled: e.target.checked }), style: S.check,
      }),
      h('span', { style: S.switchLabel }, enabled ? '关闭注意力编排' : '开启注意力编排'),
    ),

    h('div', { style: S.sep }),
    h('div', { style: S.label }, '编排模型'),
    h('label', { style: S.row },
      h('input', {
        type: 'radio', name: 'mm', checked: mode === 'follow', disabled: busy,
        onChange: () => patch({ modelMode: 'follow' }),
      }),
      h('span', { style: S.radioLabel }, '跟随当前会话模型'),
    ),
    h('label', { style: S.row },
      h('input', {
        type: 'radio', name: 'mm', checked: mode === 'custom', disabled: busy,
        onChange: () => patch({ modelMode: 'custom' }),
      }),
      h('span', { style: S.radioLabel }, '自选模型'),
    ),
    mode === 'custom' && h('select', {
      style: S.select, disabled: busy,
      value: `${(st && st.customProvider) || ''}|${(st && st.customModel) || ''}`,
      onChange: (e) => {
        const [prov, mod] = String(e.target.value).split('|')
        patch({ customProvider: prov, customModel: mod })
      },
    },
      h('option', { value: '|', style: S.option }, '— 选择模型 —'),
      ...models.map((m) => h('option', { key: `${m.provider}|${m.model}`, value: `${m.provider}|${m.model}`, style: S.option },
        `${m.label || m.model}  (${m.provider})`)),
    ),

    h('div', { style: S.sep }),
    h('div', { style: S.label }, '运行状态'),
    st
      ? h('div', null,
          h('div', { style: S.kv }, `累计编排 ${st.total || 0} 次 · 成功 ${st.ok || 0} · 失败 ${st.failed || 0}`),
          last
            ? h('div', { style: S.kv }, `最近 ${last.ms}ms · 常驻 ${last.persist ?? '-'} · 引用 ${last.quote ?? '-'}`)
            : h('div', { style: S.hint }, '还没编排过。'),
        )
      : h('div', { style: S.hint }, '加载中…'),

    h('div', { style: S.sep }),
    h('button', { style: S.btn, onClick: loadLog, disabled: busy }, '最近决策'),
    h('button', { style: { ...S.btn, marginLeft: 8 }, onClick: refresh, disabled: busy }, '刷新'),

    showLog && h('div', { style: S.logBox },
      log.length === 0
        ? h('div', { style: S.hint }, '（无记录）')
        : log.slice().reverse().map((r, i) => h('div', { key: i, style: S.logRow },
            `${(r.ts || '').slice(11, 19)} ${r.outcome} ${r.ms}ms`,
          )),
    ),
    busy && h('div', { style: S.hint }, '保存中…'),
  )
}

const S = {
  // 收起态胶囊：外层只占内容宽（关键 —— dock 是满宽的，不包一层会被拉长）
  pillRow: { display: 'inline-flex', width: 'auto', maxWidth: '100%', alignItems: 'center' },
  pillOn: {
    display: 'inline-flex', alignItems: 'center', gap: '5px', width: 'auto',
    padding: '1px 8px', fontSize: '12px', cursor: 'pointer', borderRadius: '10px',
    border: '1px solid rgba(46,160,67,.45)', background: 'rgba(46,160,67,.14)',
    color: 'inherit', fontFamily: 'inherit', lineHeight: 1.35, whiteSpace: 'nowrap',
  },
  pillOff: {
    display: 'inline-flex', alignItems: 'center', gap: '5px', width: 'auto',
    padding: '1px 8px', fontSize: '12px', cursor: 'pointer', borderRadius: '10px',
    border: '1px solid rgba(128,128,128,.35)', background: 'rgba(128,128,128,.10)',
    color: 'inherit', opacity: 0.72, fontFamily: 'inherit', lineHeight: 1.35, whiteSpace: 'nowrap',
  },
  dotOnOff: (on) => ({ fontSize: '9px', lineHeight: 1, color: on ? '#3fb950' : 'rgba(128,128,128,.85)' }),
  pillLabel: { fontWeight: 500 },

  // 展开态
  wrap: {
    padding: '10px 12px', fontSize: '13px', lineHeight: 1.55,
    fontFamily: 'var(--dsh-font-family, system-ui, sans-serif)',
    color: 'inherit', boxSizing: 'border-box',
    border: '1px solid rgba(128,128,128,.28)', borderRadius: '8px',
    background: 'rgba(128,128,128,.06)', maxHeight: '80vh', overflow: 'auto',
    width: '100%', maxWidth: '460px',   // 不要把整行撑满
    // ★ 2026-10-22 沙箱实测（量出来的，不是猜的）：
    //   dock 是 flex 列，我们的 div 默认 flex-shrink:1 ⇒ 被压到 **328px**，
    //   而它自己的 maxHeight 是 60vh（522px）、父链可用高度 769px 都不是瓶颈。
    //   结果：「自动确认」落在折叠线以下（offsetInClip 343 > 可视 322），
    //   用户得先滚动才看得到 —— 这正是"看不出自动确认开没开"的来源。
    //   ⇒ flexShrink:0 让它保住高度（仍受 maxHeight:60vh 封顶，不会顶爆输入区）。
    flexShrink: 0,
  },
  head: { display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' },
  title: { fontSize: '13px', fontWeight: 600 },
  close: {
    marginLeft: 'auto', border: 'none', background: 'transparent', color: 'inherit',
    fontSize: '16px', lineHeight: 1, cursor: 'pointer', opacity: 0.6, padding: '0 2px',
  },
  badgeOn: {
    fontSize: '10px', padding: '1px 6px', borderRadius: '8px',
    background: 'rgba(46,160,67,.18)', color: '#3fb950', border: '1px solid rgba(46,160,67,.35)',
  },
  badgeOff: {
    fontSize: '10px', padding: '1px 6px', borderRadius: '8px',
    background: 'rgba(128,128,128,.15)', opacity: 0.7, border: '1px solid rgba(128,128,128,.3)',
  },
  row: { display: 'flex', alignItems: 'center', gap: '7px', margin: '4px 0', cursor: 'pointer' },
  check: { width: '14px', height: '14px', cursor: 'pointer' },
  switchLabel: { fontWeight: 500 },
  radioLabel: { fontSize: '12px' },
  hint: { fontSize: '11.5px', opacity: 0.62, margin: '3px 0 0', lineHeight: 1.45 },
  label: { fontSize: '11.5px', opacity: 0.72, margin: '3px 0 1px', fontWeight: 500 },
  sep: { height: '1px', background: 'rgba(128,128,128,.22)', margin: '9px 0' },
  // ★ 2026-10-21 用户实测："选项点击后背景全是白的，我看不清"。
  //   病根：半透明背景 + color: inherit 只作用于**控件本身**；
  //   下拉展开的弹层由系统绘制，默认白底 ⇒ 深色主题下就是白底浅字。
  //   修法：① 控件与每个 option 都显式给实色（主题变量 + 兜底），不靠 inherit；
  //        ② color-scheme 写 'light dark'（主题中性，不强制深色，避免浅色主题反向看不清）。
  select: {
    width: '100%', marginTop: '5px', padding: '4px 6px', boxSizing: 'border-box',
    background: 'var(--dsw-alias-bg-layer-2, #2b2b2b)',
    color: 'var(--dsw-alias-label-primary, #e8e8e8)',
    border: '1px solid var(--dsw-alias-border-l2, rgba(128,128,128,.45))',
    borderRadius: '5px', fontSize: '12px', colorScheme: 'light dark',
  },
  // 弹层里每一行也必须上色，否则仍是白底浅字（这条必须被每个 <option> 实际引用）
  option: {
    background: 'var(--dsw-alias-bg-overlay, #2b2b2b)',
    color: 'var(--dsw-alias-label-primary, #e8e8e8)',
  },
  textInput: {
    width: '100%', marginTop: '5px', padding: '4px 6px', boxSizing: 'border-box',
    background: 'var(--dsw-alias-bg-layer-2, #2b2b2b)',
    color: 'var(--dsw-alias-label-primary, #e8e8e8)',
    border: '1px solid var(--dsw-alias-border-l2, rgba(128,128,128,.45))',
    borderRadius: '5px', fontSize: '12px', colorScheme: 'light dark',
  },
  // ★★ 2026-10-22 布局量测（不是目测）：面板 top=120 / bottom=729 / clientH=608，
  //   而「待确认候选」这一块自己就占 **333px**（158→491），把下面的连续性区块一路推到
  //   「① 先选工作区」top=766、「从这些对话导入」top=932 —— 全在面板底 729 之外，
  //   用户必须滚动才看得见。**这就是"选择器像消失了"的直接原因**：
  //   它不是没渲染，而是被上面一堆候选顶出了可视区。
  //   修法：候选块自己内部滚动（上限 108px），不再让它的行数去顶别人的位置。
  //   为什么不删候选行数：删了就等于牺牲"看得见自动确认了什么"这个纠错入口。
  //   ★ 108 这个数不是拍的：132 时导入按钮 bottom=772、面板底=729，仍差 43px；
  //     与 sessionPicker 96（各收 24px）合起来才够。改任一数值都要重新量（tools/measure_panel_layout.mjs）。
  candidateBox: { marginTop: '7px', padding: '6px', border: '1px solid rgba(229,160,13,.35)', borderRadius: '6px', maxHeight: '108px', overflowY: 'auto' },
  // ★ 2026-10-22 沙箱实测：会话列表 180px 把「从这些对话导入」挤到折叠线以下
  //   （offsetInClip 623 > 可视 522）。列表本身只需要能扫几行，收到 120px 后
  //   导入按钮进入可视区 —— 比"把按钮挪到列表前面"更好：挪上去会在未勾选时
  //   显示成禁用的「（0 个）」，那是另一种"点了没反应"。
  sessionPicker: { maxHeight: '96px', overflow: 'auto', marginTop: '5px', padding: '4px', border: '1px solid rgba(128,128,128,.25)', borderRadius: '5px' },
  candidateRow: { display: 'flex', alignItems: 'center', gap: '6px', margin: '4px 0' },
  candidateText: { flex: 1, fontSize: '11px', lineHeight: 1.35 },
  miniBtn: { padding: '2px 6px', fontSize: '10px', cursor: 'pointer', borderRadius: '4px', border: '1px solid rgba(128,128,128,.35)', background: 'rgba(128,128,128,.12)', color: 'inherit' },
  kv: { fontSize: '11.5px', margin: '2px 0', opacity: 0.9 },
  btn: {
    padding: '4px 9px', fontSize: '11.5px', cursor: 'pointer', borderRadius: '6px',
    border: '1px solid rgba(128,128,128,.35)',
    background: 'rgba(128,128,128,.12)', color: 'inherit',
  },
  warn: { fontSize: '11.5px', color: '#e5a00d', margin: '5px 0' },
  logBox: {
    marginTop: '8px', maxHeight: '150px', overflow: 'auto',
    border: '1px solid rgba(128,128,128,.25)', borderRadius: '6px', padding: '5px 7px',
  },
  logRow: { fontSize: '10.5px', fontFamily: 'ui-monospace, Consolas, monospace', padding: '1px 0', opacity: 0.85 },
}

// ---------------------------------------------------------------------------
// 注册（常驻席位 —— 用户要求"必须有个常住的、我能随时点的"）
// ---------------------------------------------------------------------------

/**
 * 三处都注册，任何一处生效都能看到：
 *   ① conversation.input.dock    ← ★输入框旁的 dock，**常驻、随时可点**（主入口）
 *   ② tool.call.toolview         ← 工具结果卡片
 *   ③ conversation.chat.turnTail ← 每轮末尾兜底
 *
 * 契约照抄 @nagi-ovo/dsh-visualize（本机已验证可用）：
 *   ctx.slots.inject('conversation.input.dock', () => ctx.slots.register(
 *     { name: 'conversation.input.dock', id: 'visualize-stream', order: 30 }, StreamingPreview))
 */
export function apply(ctx) {
  const log = (...a) => { try { console.log('[orch-ui]', ...a) } catch { /* noop */ } }
  const done = []

  const tryReg = (slotName, def, Comp, label) => {
    try {
      if (!ctx.slots || typeof ctx.slots.inject !== 'function' || typeof ctx.slots.register !== 'function') {
        log('ctx.slots 不可用，跳过 ' + slotName)
        return false
      }
      ctx.effect(() => ctx.slots.inject(slotName, () => ctx.slots.register(
        { name: slotName, ...def },
        Comp,
      )), 'dsh-memory: ' + label)
      done.push(slotName)
      log('registered ' + slotName)
      return true
    } catch (e) {
      log('register failed ' + slotName + ': ' + (e?.message ?? e))
      return false
    }
  }

  tryReg('conversation.input.dock', { id: 'memory-orchestrator-dock', order: 40 }, OrchestratorCard, 'dock')
  // 只在 dock 没注册上时才用兜底席位（避免同一控件出现两遍）
  if (done.length === 0) {
    tryReg('conversation.chat.turnTail', { id: 'memory-orchestrator-tail', order: 120 }, OrchestratorCard, 'turnTail')
  }

  log(`done (${done.length} 处: ${done.join(', ')})`)
}
