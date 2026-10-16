/**
 * 构建客户端 bundle：把 client.js（ESM 源码）转成 DSH 要的浏览器 bundle 格式。
 *
 * DSH 客户端 bundle 契约（读自 @nagi-ovo/dsh-visualize/lib/client.js）：
 *   window.__ModuleLoader__.load({
 *     id: "<包名>",
 *     factory: (require) => {
 *       var module = { exports: {} }; var exports = module.exports;
 *       ...（模块体，用 require("react") 等拿基座模块）...
 *       exports.apply = apply; exports.inject = inject; exports.name = name;
 *       return module.exports;
 *     }
 *   });
 *
 * 本构建器**不引入 bundler**：把源码里的 import 语句改写成 require 绑定，
 * 其余原样保留（我们只用 react 一个外部依赖，转换可控且可验证）。
 */
import { readFileSync, writeFileSync } from 'node:fs'

const SRC = new URL('./client.js', import.meta.url)
const OUT = new URL('./lib/client.js', import.meta.url)
const PKG = 'dsh-memory-plugin'

let src = readFileSync(SRC, 'utf8')

// ---- import 改写 ----
const imports = []
src = src.replace(/^import\s+([\s\S]*?)\s+from\s+['"]([^'"]+)['"];?\s*$/gm, (m, spec, from) => {
  imports.push({ spec: spec.trim(), from })
  return ''
})

const binds = imports.map(({ spec, from }) => {
  // 形式：{ a, b as c }  或  默认名  或  默认名, { x }
  const parts = []
  const named = spec.match(/\{([^}]*)\}/)
  if (named) {
    for (const item of named[1].split(',').map((s) => s.trim()).filter(Boolean)) {
      const [orig, alias] = item.split(/\s+as\s+/).map((s) => s.trim())
      parts.push(alias ? `${alias}: ${orig}` : orig)
    }
  }
  const def = spec.replace(/\{[^}]*\}/, '').replace(/,\s*$/, '').trim()
  if (def) parts.push(`default: ${def}`)
  return `\t\tconst ${spec.replace(/\{[^}]*\}/, '_named').replace(/[^A-Za-z0-9_$]/g, '_') || '_m'} = require(${JSON.stringify(from)});`
}).join('\n')

// 更稳妥：逐个 import 生成独立 const，避免解构重名
const binds2 = imports.map(({ spec, from }, i) => {
  const named = spec.match(/\{([^}]*)\}/)
  const lines = []
  const varName = `__m${i}`
  lines.push(`\t\tconst ${varName} = require(${JSON.stringify(from)});`)
  if (named) {
    const items = named[1].split(',').map((s) => s.trim()).filter(Boolean).map((item) => {
      const [orig, alias] = item.split(/\s+as\s+/).map((s) => s.trim())
      return alias ? `${orig}: ${alias}` : orig
    })
    if (items.length) lines.push(`\t\tconst { ${items.join(', ')} } = ${varName};`)
  }
  const def = spec.replace(/\{[^}]*\}/, '').replace(/,\s*$/, '').trim()
  if (def && def !== '') lines.push(`\t\tconst ${def} = ${varName}.default ?? ${varName};`)
  return lines.join('\n')
}).join('\n')

// 去掉源码里的 export 关键字，并记录需要导出的名字
const exported = new Set()
src = src.replace(/^export\s+const\s+([A-Za-z0-9_$]+)/gm, (m, name) => { exported.add(name); return `const ${name}` })
src = src.replace(/^export\s+function\s+([A-Za-z0-9_$]+)/gm, (m, name) => { exported.add(name); return `function ${name}` })
src = src.replace(/^export\s+\{([^}]*)\};?\s*$/gm, (m, names) => {
  for (const n of names.split(',').map((s) => s.trim()).filter(Boolean)) exported.add(n)
  return ''
})
// 模块体末尾统一收口
const exportLines = [...exported].map((n) => `\t\texports.${n} = ${n};`).join('\n')

// 缩进模块体
const body = src.split('\n').map((l) => (l.trim() ? '\t\t' + l : l)).join('\n')

const out = `window.__ModuleLoader__.load({
\tid: ${JSON.stringify(PKG)},
\tfactory: (require) => {
\t\tvar module = { exports: {} };
\t\tvar exports = module.exports;
\t\tObject.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
${binds2}

${body}

${exportLines}
\t\treturn module.exports;
\t}
});
`

writeFileSync(OUT, out, 'utf8')
console.log('已构建 lib/client.js：' + out.length + ' 字节，imports=' + imports.length)
console.log('  ' + imports.map((i) => i.from).join(', '))
