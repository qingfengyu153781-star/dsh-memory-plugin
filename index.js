/**
 * ⚠ 历史遗留入口 —— **不要在这里写逻辑**。
 *
 * 本插件真正的入口是 `lib/index.js`（见 `package.json` 的 `exports["."]` 与 `main`）。
 * 这个根文件曾经是 `lib/index.js` 的**一份过期副本**（停在 v0.2.0），带来两个真问题：
 *   ① 两份副本内容不一致（2026-10-15 实测 md5 不同：根 `6fbb3044…` vs `lib 9799c084…`）
 *      ⇒ "改了源码副本却不生效"或"改了装机版被下一次同步覆盖"；
 *   ② 它的 `import '../orchestrator/host.js'` **解析到不存在的路径**
 *      （`plugin-src/orchestrator/host.js`，真文件在 `plugin-src/dsh-memory-plugin/orchestrator/`）
 *      ⇒ `test/selfcheck.mjs` 一跑就 `Cannot find module`，测试套件等于没有。
 *
 * ★ 2026-10-15 修法：本文件改为**纯转发** ⇒ 全插件只有一处真身 `lib/index.js`。
 */
export * from './lib/index.js'
