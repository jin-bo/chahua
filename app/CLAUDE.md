# app/CLAUDE.md

Electron 壳（`main/` / `preload/` / `renderer/`）承重不变量。仓库顶层 `CLAUDE.md` 是全局约定，本文件只在动 `app/` 下文件时加载。

## 聊天界面渲染（P10）

- **mermaid 只在 message_end 全文到位时渲一次**，流式 delta 期间禁调（半截抛 parse error 闪烁）；失败保留原 `<pre>` + `.mermaid-error`。
- **mermaid SVG 走手工 sanitize，不能换 DOMPurify**（DOMPurify 强制清空 `<foreignObject>`，mermaid v11 label 会被剥光）；安全靠 mermaid 自带 sanitize + CSP + 手工剥 `on*`/`javascript:` 三层兜底。
- **挂件按 rel 去重**（防 live+history 双触发）；图片预览懒拉不 eager 内嵌（占位 `<img>` 后发 `download_file purpose=preview` 回包灌字节），SVG 走 pill 不内嵌；**切房 / clear 必 `clearPendingArtifactPreviews()`**（否则等待中的 `<img>` 已被摘走无处灌）。
- **P10.1 数学/化学走 marked「转义前封箱」+ KaTeX live-DOM 延后渲**：行内扩展把整段 `$…$`/`$$…$$` 作单一 token 收走成 carrier span（否则 CommonMark 反斜杠转义先吃掉 `\,`/`\\`）；只认 `$`/`$$`，货币歧义 pandoc 风格收窄。
- **KaTeX 在 DOMPurify 之后、只 message_end 调一次、逐公式独立 render**；`trust:false` + `throwOnError:false` + **不传 `macros`**（`\gdef` 不跨公式泄漏）。
- **highlight.js v11 喂转义 textContent、单块单次、跳 mermaid/已高亮/未注册语言**；懒加载走 `@highlightjs/cdn-assets` ESM（**不能用 `highlight.js` 包的 CJS shim**，沙箱渲染进程无 CJS 互操作）。
- **`enhanceContent` = mermaid + highlight + math 单钩子**（各自幂等），挂 `renderGuestText` / `endStreamingMessage` / `task_panel` goal 三注入点；流式 `appendDelta` 不调。P10.1 纯前端零后端改、不 bump `schema_version`、CSP 不改；打包须保 `katex/dist/fonts/`。
