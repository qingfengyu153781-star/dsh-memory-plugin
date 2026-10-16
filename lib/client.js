window.__ModuleLoader__.load({
	id: "dsh-memory-plugin",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		const __m0 = require("react");
		const { createElement: h, useCallback, useEffect, useState } = __m0;

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


		const name = 'dsh-memory-plugin-client'

		/**
		 * ★ 必须声明的服务。
		 * 2026-10-03 教训：cordis 会**等 inject 里的服务就绪**才调用 apply()。
		 * 漏声明 ⇒ apply() 在服务就绪前跑 ⇒ 拿不到 ⇒ 静默失败。
		 */
		const inject = ['slots']

		/**
		 * 宿主 API 基址（多通道自动探测）。
		 *
		 * ★ 2026-10-03 修：原来用相对路径 '/dsh-memory/orch'，在 Electron 桌面版里**到不了宿主** ——
		 *   页面 origin 是 `dsh-app://app`，相对路径会变成 `dsh-app://app/dsh-memory/orch/...`。
		 *   这就是"点开关没反应"的原因（visualize 没这问题，因为它纯前端渲染、不调后端）。
		 *
		 * 探测顺序（第一个通的记住，后续不再试）：
		 *   ① 相对路径           —— web 版（页面 origin 就是 DSH 服务）
		 *   ② location.origin    —— 显式同源
		 *   ③ loopback 常见端口  —— 桌面版 dsh-app:// 场景
		 * 目标路由注册在宿主 webServer 上，**不走 /api 的 cookie 鉴权**（实测无 cookie 也 200）。
		 */
		const API_PATH = '/dsh-memory/orch'
		const LOOPBACK_PORTS = [19387, 19388, 19389]

		function candidateBases() {
		  const out = ['']
		  try {
		    const o = (typeof location !== 'undefined' && location.origin) || ''
		    if (o && o.startsWith('http') && !out.includes(o)) out.push(o)
		  } catch { /* noop */ }
		  for (const p of LOOPBACK_PORTS) {
		    const b = `http://127.0.0.1:${p}`
		    if (!out.includes(b)) out.push(b)
		  }
		  return out
		}

		/** 记住哪条通道可用，避免每次都试一圈。 */
		let goodBase = null

		async function apiFetch(path, init) {
		  const bases = goodBase !== null ? [goodBase] : candidateBases()
		  let lastErr = null
		  for (const b of bases) {
		    const url = `${b}${API_PATH}${path}`
		    try {
		      const ctl = typeof AbortController === 'function' ? new AbortController() : null
		      const timer = ctl ? setTimeout(() => { try { ctl.abort() } catch { /* noop */ } }, 4000) : null
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
		        try { console.log('[orch-ui] API base =', b || '(relative)', '→', url) } catch { /* noop */ }
		      }
		      return j
		    } catch (e) { lastErr = e }
		  }
		  throw lastErr || new Error('no reachable API base')
		}

		const api = (path, init) => apiFetch(path, init)

		const TOOL_NAME = 'memory_orchestrator'

		// ---------------------------------------------------------------------------
		// 主组件：常驻小胶囊 + 点开变大面板
		// ---------------------------------------------------------------------------

		/**
		 * 常驻席位（输入框 dock）空间很小，所以默认显示一个**可点的小胶囊**：
		 *   [◈ 编排 关]   ← 一眼看到状态
		 * 点一下展开成完整面板（开关 / 模型 / 状态 / 留痕）。
		 */
		function OrchestratorCard() {
		  const [open, setOpen] = useState(false)
		  const [st, setSt] = useState(null)
		  const [err, setErr] = useState('')
		  const [busy, setBusy] = useState(false)
		  const [models, setModels] = useState([])
		  const [showLog, setShowLog] = useState(false)
		  const [log, setLog] = useState([])

		  const refresh = useCallback(async () => {
		    try {
		      const s = await api('/stats')
		      setSt(s); setErr('')
		    } catch (e) { setErr(String(e?.message ?? e)) }
		  }, [])

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
		    h('div', { style: S.hint },
		      enabled
		        ? '每轮另调一次模型挑选"该给主模型看什么"，调完即弃，它的上下文始终干净。'
		        : '关闭时不介入，行为与原来完全一致。'),

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
		      h('option', { value: '|' }, '— 选择模型 —'),
		      ...models.map((m) => h('option', { key: `${m.provider}|${m.model}`, value: `${m.provider}|${m.model}` },
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
		    background: 'rgba(128,128,128,.06)', maxHeight: '60vh', overflow: 'auto',
		    width: '100%', maxWidth: '460px',   // 不要把整行撑满
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
		  select: {
		    width: '100%', marginTop: '5px', padding: '3px 5px',
		    background: 'rgba(128,128,128,.12)', color: 'inherit',
		    border: '1px solid rgba(128,128,128,.3)', borderRadius: '5px', fontSize: '12px',
		  },
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
		function apply(ctx) {
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


		exports.name = name;
		exports.inject = inject;
		exports.OrchestratorCard = OrchestratorCard;
		exports.apply = apply;
		return module.exports;
	}
});
