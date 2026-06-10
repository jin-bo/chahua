"use strict";

// 茶话室 chat / sidebar 视图原语。纯 DOM + 字符串渲染，不读 renderer.js 模块状态
// （guests / userAvatarDataUri / messagesEl 等）—— 调用方按需把数据传进来。

import { ScoreKind } from "./events.js";
import { marked } from "../node_modules/marked/lib/marked.esm.js";
import DOMPurify from "../node_modules/dompurify/dist/purify.es.mjs";

// gfm 开 GitHub 风格扩展（表格 / 删除线 / 任务列表）；breaks 让单换行 = <br>，
// 符合聊天里"按 Enter 换行"的直觉（LLM 输出也常用单换行分句）。
marked.setOptions({ gfm: true, breaks: true });

// LLM 输出走 marked → DOMPurify 一遍：前者结构化为 HTML，后者剥掉 <script> / on* /
// javascript: 等危险载荷。USE_PROFILES.html 是 DOMPurify 推荐的富文本白名单（允许
// a/ul/ol/li/code/pre/blockquote/h*/table 但禁脚本）。
export function renderMarkdown(text) {
  return DOMPurify.sanitize(marked.parse(text || ""), { USE_PROFILES: { html: true } });
}

// Mermaid 懒加载：mermaid 解包后约 3MB JS，不用就别 parse。第一次撞到
// ```mermaid 代码块才动态 import，之后缓存 Promise 复用。失败一次后允许下条消息重试
// （清掉 mermaidPromise），避免首次网络/磁盘瞬时抖动让整 session 都没图。
let mermaidPromise = null;
function loadMermaid() {
  if (mermaidPromise) return mermaidPromise;
  mermaidPromise = import("../node_modules/mermaid/dist/mermaid.esm.mjs")
    .then((mod) => {
      const m = mod.default || mod;
      // startOnLoad=false：我们手动调 render，不让 mermaid 全页扫 .mermaid 类（避免它
      // 跟我们的 DOM 抢节点）。securityLevel=strict：禁 click 回调跑用户 JS、禁
      // 远程 mermaid config 加载。theme=neutral 配茶话室的米色 / 浅灰背景。
      //
      // 不设 flowchart.htmlLabels:false —— mermaid v11 的 dagre-wrapper renderer 对节点
      // label 强制走 <foreignObject>+HTML，此开关对边标签生效但对节点 label 不生效。
      // 由 DOMPurify 那头放宽白名单解决（见 renderMermaidIn）。
      m.initialize({
        startOnLoad: false,
        theme: "neutral",
        securityLevel: "strict",
      });
      return m;
    })
    .catch((err) => {
      mermaidPromise = null;  // 允许下次重试
      throw err;
    });
  return mermaidPromise;
}

let mermaidIdSeq = 0;

function normalizeMermaidSource(src) {
  return (src || "")
    .replace(/\r\n?/g, "\n")
    .replace(/<br\s*\/>/gi, "<br>")
    .replace(/\[([^\]\n]*<br\b[^>\n]*>[^\]\n]*)\]/gi, (_match, label) => {
      const trimmed = label.trim();
      if (
        (trimmed.startsWith('"') && trimmed.endsWith('"')) ||
        (trimmed.startsWith("'") && trimmed.endsWith("'"))
      ) {
        return `[${label}]`;
      }
      return `["${label.replace(/"/g, "#quot;")}"]`;
    });
}

// SVG 手工 sanitize：剥 <script> / 所有 on* 事件属性 / href|src 里的 javascript: URL。
// 比 DOMPurify svg profile 粗放（不做 element 白名单），但**保住 `<foreignObject>` 内
// HTML 子树** —— DOMPurify 对 foreignObject 是「safe-by-default 强制清空内容」，任何
// 配置（USE_PROFILES + ADD_TAGS + xhtml parser + DOM Node 输入）都救不回，实测三种
// 都把节点 label 文字剥光（边 label 因为走 `<text>` 幸存，所以症状是「边上有字、框
// 里没字」）。
//
// 安全语义靠多层兜底，不靠这一道独撑：
// ① mermaid 自身 `securityLevel:'strict'` 内部已过一道 sanitize（节点文字本身就被
//    mermaid 自带 DOMPurify 过滤过 script/on*）；
// ② Electron CSP `script-src 'self'` 禁所有 inline script 与远程脚本，即使有 `<script>`
//    漏到 DOM 也跑不起来；
// ③ 这一道补 attribute 级的 on* / javascript: URL（CSP 不管这两个）。
// 不剥 <style> / <iframe> 等：mermaid 不输出 <iframe>，<style> 内嵌 CSS 在 CSP
// 'unsafe-inline' 下合法、为 mermaid 主题所需。
function sanitizeSvgInPlace(root) {
  // 1) 删所有 <script>（无论命名空间）。
  for (const s of root.querySelectorAll("script")) s.remove();
  // 2) 走全树清 on* 与 javascript: URL。on* 属性大小写都剥；href / xlink:href / src
  //    抽 attr.value trim+lowercase 比 `javascript:` 前缀。
  for (const el of root.querySelectorAll("*")) {
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name;
      if (/^on/i.test(name)) {
        el.removeAttribute(name);
        continue;
      }
      const lname = attr.localName || name;
      if (lname === "href" || lname === "src") {
        const v = (attr.value || "").trim().toLowerCase();
        // removeAttributeNS 第二参是 localName 不是 qualified name；用 lname 才能匹到
        // `xlink:href` 这种带命名空间前缀的属性。无命名空间属性时 namespaceURI=null、
        // 仍然有效。
        if (v.startsWith("javascript:")) el.removeAttributeNS(attr.namespaceURI, lname);
      }
    }
  }
}

// 扫 container 内所有 ```mermaid 代码块、替换为渲染好的 SVG。**异步、fire-and-forget**：
// mermaid 是异步渲染的，调用方不要 await（不能阻塞流式 / history 重放的 DOM 路径）。
//
// **不要在流式 delta 期间调** —— 半截的 `graph TD\n  A -->` 会让 mermaid 抛 parse error
// 闪烁刷屏；只在 message_end 全文到位 / 静态 renderGuestText / history 重放调一次。
//
// `pre.dataset.mermaidRendered` 双语义：① 排重，多次进入同一 container 不重启渲染；
// ② render 成功后 pre 已 replaceWith host，下次 querySelectorAll 自然找不到。失败的 pre
// 保留原样 + .mermaid-error class，让用户看到源码 + 错误（dataset.mermaidError）。
//
// 解析路径：DOMParser('text/html') 拿 detached document。两个好处：
// ① HTML5 spec 对 `<svg>` 内嵌的 `<foreignObject>` 有特别处理（"foreign content"），会
//   切回 HTML 解析上下文，foreignObject 子树里的元素自动归 HTML 命名空间，且 `<br>`
//   `<hr>` `<img>` 等 HTML void element 合法（mermaid 在 foreignObject 里就用 `<br>`
//   做换行）。`DOMParser('image/svg+xml')` 严格 XML 模式遇 `<br>` 直接报 mismatch。
// ② detached document 不加载资源（`<img src>` 不发请求）、不跑脚本，sanitize 跑完才
//   importNode 进主文档；中间步骤无副作用窗口。
export function renderMermaidIn(container) {
  if (!container) return;
  const codes = container.querySelectorAll("pre > code.language-mermaid");
  if (codes.length === 0) return;
  loadMermaid().then((mermaid) => {
    for (const code of codes) {
      const pre = code.parentElement;
      if (!pre || pre.dataset.mermaidRendered === "1") continue;
      pre.dataset.mermaidRendered = "1";
      const src = normalizeMermaidSource(code.textContent || "");
      const id = `mmd-${++mermaidIdSeq}`;
      mermaid.render(id, src).then(({ svg }) => {
        if (!pre.isConnected) return;  // 切房 / clear 把节点摘走了
        const doc = new DOMParser().parseFromString(svg, "text/html");
        // text/html parse 把 svg 放在 doc.body 下，根 svg 元素经 querySelector 取。
        const svgEl = doc.body.querySelector("svg");
        if (!svgEl) throw new Error("SVG parse failed");
        sanitizeSvgInPlace(svgEl);
        const host = document.createElement("div");
        host.className = "mermaid-rendered";
        host.appendChild(document.importNode(svgEl, true));
        pre.replaceWith(host);
      }).catch((err) => {
        if (!pre.isConnected) return;
        pre.classList.add("mermaid-error");
        pre.dataset.mermaidError = String(err?.message || err);
      });
    }
  }).catch((err) => {
    console.warn("[chahua] mermaid 加载失败：", err);
  });
}

// 流式重渲 innerHTML 会把节点全换一遍，用户正在做的拖选 / Cmd+C copy 会瞬间被擦。
// 检测有活动选区（非 collapsed）且 anchor 或 focus 在 node 子树内 —— 调用方据此跳过
// 本次渲染、等 selection 解除再补渲。isCollapsed 排除"光标位置"这种伪选区。
export function isSelectionInside(node) {
  const sel = document.getSelection();
  if (!sel || sel.isCollapsed) return false;
  return node.contains(sel.anchorNode) || node.contains(sel.focusNode);
}

// dataUri 缺 / 加载失败 → 用 alt 的首字符 + hash 色圆形 SVG 当兜底。返回值始终是
// <img>，调用方不必区分两条路径，CSS 规则也共用（width/height/border-radius 来自
// className）。
export function makeAvatarImg(dataUri, className, alt) {
  if (!dataUri) return makeInitialAvatar(alt, className);
  const img = document.createElement("img");
  img.className = className;
  img.src = dataUri;
  img.alt = alt || "";
  // 服务端给的 data URI 不会 404，但 base64 损坏时 onerror 会触发 —— 用首字母兜底
  // 替换，避免节点空白挂着。
  img.addEventListener("error", () => {
    img.replaceWith(makeInitialAvatar(alt, className));
  }, { once: true });
  return img;
}

// hash 字符串到 0..360 的 hue —— 同名总落同色。配 hsl(_, 55%, 45%) 给一组对比鲜明、
// 白字读得清的"头像底色"。
function avatarHue(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return ((h % 360) + 360) % 360;
}

function escapeXml(s) {
  return s.replace(/[&<>'"]/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  }[c]));
}

// 首字符 + hash 色圆形底，包成 inline-SVG data URI 当 <img src=>。viewBox 100x100
// 让 text 跟着 className 的 width/height 自动缩放，不必为每个头像尺寸单写 CSS。
function makeInitialAvatar(name, className) {
  const safe = name || "?";
  const letter = escapeXml(safe.charAt(0));
  const hue = avatarHue(safe);
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">` +
    `<circle cx="50" cy="50" r="50" fill="hsl(${hue} 55% 45%)"/>` +
    `<text x="50" y="50" text-anchor="middle" dominant-baseline="central" ` +
    `fill="white" font-size="52" font-family="system-ui, sans-serif" ` +
    `font-weight="600">${letter}</text></svg>`;
  const img = document.createElement("img");
  img.className = className;
  img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  img.alt = name || "";
  return img;
}

export function scoreText(r) {
  switch (r.kind) {
    case ScoreKind.MENTION: return "@";
    case ScoreKind.COOLDOWN: return "冷却";
    case ScoreKind.ERROR: return "失败";
    case ScoreKind.SCORED:
    default:
      return typeof r.score === "number" ? r.score.toFixed(2) : "?";
  }
}

// 文字 badge。permission 走 makePermissionBadge（V 标）。
export function makeBadge(className, dataKey, value) {
  const b = document.createElement("span");
  b.className = className;
  if (dataKey) b.dataset[dataKey] = value;
  b.textContent = value;
  return b;
}

// Permission V 标。workspace-write 蓝 / full-access 红 / read-only 调用方先过滤不渲染。
// 颜色经 data-permission 由 CSS 决定；title 给鼠标 hover 文本兜底 + 屏幕阅读器。
// 调用方决定 inline（默认）还是 overlay（加 .on-avatar，浮在头像右上角）。
export function makePermissionBadge(permission, className) {
  const b = document.createElement("span");
  b.className = className;
  b.dataset.permission = permission;
  b.textContent = "✓";
  b.title = permission;
  return b;
}

// 复制 markdown 源而非 textEl.textContent —— 后者会把代码块 / 列表结构压扁。
// getText 是 thunk 让流式路径能再读到最新 accumulated 缓冲。
export function attachCopyButton(bubble, getText) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "bubble-copy";
  btn.title = "复制";
  btn.textContent = "复制";
  btn.addEventListener("click", async (ev) => {
    // 防止冒泡触发 messagesEl 的 link 拦截 / sticky-bottom 等。
    ev.stopPropagation();
    try {
      await navigator.clipboard.writeText(getText());
      btn.textContent = "已复制";
      btn.classList.add("copied");
    } catch {
      btn.textContent = "失败";
    }
    setTimeout(() => {
      btn.textContent = "复制";
      btn.classList.remove("copied");
    }, 1500);
  });
  bubble.appendChild(btn);
}

// 「请审…」按钮 —— hover 气泡时浮现在右下角（与 .bubble-copy 右上角错开）。仅当消息
// 已落 transcript（messageId 非空）才挂：用户本地 echo 气泡没有 message_id、请审无锚点
// （docs/P7.2 §5.3）。点击交给注入的 onRequestReview —— renderer 弹茶客选择菜单后发
// handoff_review inbound。
export function attachReviewButton(bubble, messageId, onRequestReview) {
  if (!messageId) return;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "bubble-review";
  btn.title = "加入审阅队列";
  btn.textContent = "请审…";
  btn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    onRequestReview(messageId, btn);
  });
  bubble.appendChild(btn);
}

// 静态文本渲染 + 挂复制按钮。流式路径不走这里 —— 它要自挂闭包版复制按钮才能动态
// 读 accumulated 缓冲。Mermaid 渲染附在末尾：history 重放 / appendBubble 全文路径都
// 经此进入，唯一出口；流式终点（endStreamingMessage）另外手动调一次 renderMermaidIn。
export function renderGuestText({ textEl, bubble }, text) {
  textEl.innerHTML = renderMarkdown(text);
  renderMermaidIn(textEl);
  attachCopyButton(bubble, () => text);
}

// 「气泡后挂图片 / 下载链」 —— task_artifact_added 拿到 originated_message_id 后
// 调本函数，把渲染好的图片 / 下载 pill 挂到气泡尾。history 回放走 chat_stream，每条
// 消息的 originated_artifacts 也调这里。
//
// 图片走懒加载：file_download inbound 带 ``purpose: "preview"`` 触发服务端读盘，
// 收到 ``file_download`` envelope 时 envelope_router 调 :func:`resolveArtifactPreview`
// 把 bytes 灌进 <img>。SVG 走保守路径（pill 下载）避免内嵌脚本风险。
//
// 同 (task_id, name) 多次挂只保留最后一份 —— ArtifactDetector.detect diff 不会
// 重复 emit，但 history 回放 + live emit 可能在切回房瞬间双触发；按 dataset.rel 去重。
const IMAGE_PREVIEW_EXTENSIONS = new Set([
  "png", "jpg", "jpeg", "gif", "webp",
]);
const ARTIFACT_REL_DATASET_KEY = "rel";

// 等待 preview 字节回吐的 <img> 元素表：rel → [imgEl, ...]。同一个 rel 可能挂在多个
// 气泡里（用户上传后茶客再写同名），envelope 回包时全填一遍。
const pendingPreview = new Map();

// pill 字节大小辅助：1.5KB / 12.4MB 这种短副文本。极小 (<1024B) 显示原始字节。
function formatArtifactSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function artifactExtension(name) {
  const idx = String(name || "").lastIndexOf(".");
  if (idx < 0) return "";
  return name.slice(idx + 1).toLowerCase();
}

function shouldRenderInline(name) {
  return IMAGE_PREVIEW_EXTENSIONS.has(artifactExtension(name));
}

// 气泡末尾的容器（多个挂件共用一块横排盒）。已挂同 rel 时不重复创建。返回容器与是否首次。
function ensureAttachmentsContainer(bubble) {
  let host = bubble.querySelector(":scope > .artifact-attachments");
  if (host) return host;
  host = document.createElement("div");
  host.className = "artifact-attachments";
  bubble.appendChild(host);
  return host;
}

// 挂一份 artifact 到气泡末尾。``artifact`` 形如 ``{name, rel, size, task_id}``；
// ``onRequestPreview(rel)`` 由调用方注入（envelope_router 拿 ws 句柄发 download_file
// purpose=preview），``onRequestDownload(rel)`` 同理（purpose=download）。
//
// 已挂同 rel → 静默跳（防 live + history 双触发）。挂件本身不做 mark_seen 等业务
// 状态变更，仅纯渲染。
// ``opts.lazy``（P10.3 修）：``true`` = 历史回放路径，preview 请求挂到
// IntersectionObserver 上，仅当 figure 进入视口才触发——防止打开一个 50 张图的历史
// 房间瞬间发 50 个 download_file purpose=preview 雷暴。``false`` = live emit 路径,
// 用户视线就在底部，立即拉取。
// ``opts.pillOnly``：图片也走下载 pill、不内嵌预览 —— 用户消息附件用
// （attachUserAttachments：用户刚上传的图自己见过，不值得再发 preview 帧拉字节）。
export function attachArtifactToBubble(bubble, artifact, callbacks, opts = null) {
  if (!bubble || !artifact || !artifact.rel || !artifact.name) return;
  const host = ensureAttachmentsContainer(bubble);
  const dupSel = `[data-${ARTIFACT_REL_DATASET_KEY}="${cssEscape(artifact.rel)}"]`;
  if (host.querySelector(dupSel)) return;
  const lazy = !!(opts && opts.lazy);
  const pillOnly = !!(opts && opts.pillOnly);
  if (!pillOnly && shouldRenderInline(artifact.name)) {
    host.appendChild(buildImageNode(artifact, callbacks, lazy));
  } else {
    host.appendChild(buildDownloadPill(artifact, callbacks));
  }
}

function cssEscape(s) {
  // 简易转义：rel 段允许的字符里只 `"` 与 `\` 需要在选择器属性里转。生产场景仍走
  // CSS.escape（chromium 早有），这里调用即可。
  return typeof CSS !== "undefined" && CSS.escape
    ? CSS.escape(s)
    : String(s).replace(/(["\\])/g, "\\$1");
}

// IntersectionObserver 单例 —— 给历史回放路径的图片懒触发用。一次性建好，所有
// 历史图片共享；离开视口不取消（preview 一旦发出就等回包），仅在第一次进入视口时
// 触发一次 onRequestPreview。``IntersectionObserver`` 在 Electron 上恒可用,
// fallback 不需要。
//
// P10.3 review §3 修：``_observedFigures`` 跟踪所有正被观察的 figure；切房/clear 时
// ``clearPendingArtifactPreviews`` 一并 unobserve，防止 detached node 因 observer
// 持引用永远不被 GC（DOM 节点 + closure + image 占位字节都跟着泄露）。
let _previewIO = null;
const _observedFigures = new Set();
function _ensurePreviewIO() {
  if (_previewIO) return _previewIO;
  _previewIO = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const fig = entry.target;
        _previewIO.unobserve(fig);
        _observedFigures.delete(fig);
        const trigger = fig.__previewTrigger;
        if (typeof trigger === "function") trigger();
      }
    },
    // rootMargin 让在视口下方 200px 内的图片提前请求 —— 用户慢慢滚到时图片已经准备好。
    { root: null, rootMargin: "200px 0px", threshold: 0 },
  );
  return _previewIO;
}

function buildImageNode(artifact, callbacks, lazy) {
  const fig = document.createElement("figure");
  fig.className = "artifact-image";
  fig.dataset[ARTIFACT_REL_DATASET_KEY] = artifact.rel;
  fig.title = artifact.name;
  const img = document.createElement("img");
  img.alt = artifact.name;
  img.loading = "lazy";
  // 占位渲染：宽高未知，靠 CSS aspect-ratio 兜底，bytes 回来后自适应裁剪。
  fig.appendChild(img);
  const caption = document.createElement("figcaption");
  caption.textContent = artifact.name;
  fig.appendChild(caption);
  // 点击 → 走下载（用户可能想保存原图）。bytes 已经在内存里时不再发 ws 帧 ——
  // 但走重发兜底简单且无害（服务端读盘廉价）。
  fig.addEventListener("click", () => {
    if (callbacks?.onRequestDownload) callbacks.onRequestDownload(artifact.rel);
  });

  // P10.3 修：waiter 仅在 onRequestPreview 真发出帧后才入队 —— 断线时早返、不留
  // 永挂的 placeholder。preview 触发器抽出为闭包：lazy=false 立即跑、lazy=true
  // 入 IntersectionObserver 等首次进入视口。
  const triggerPreview = () => {
    if (!callbacks?.onRequestPreview) return;
    const sent = callbacks.onRequestPreview(artifact.rel);
    if (!sent) return;
    pendingPreview.set(
      artifact.rel,
      [...(pendingPreview.get(artifact.rel) || []), img],
    );
  };
  if (lazy) {
    fig.__previewTrigger = triggerPreview;
    _ensurePreviewIO().observe(fig);
    _observedFigures.add(fig);
  } else {
    triggerPreview();
  }
  return fig;
}

function buildDownloadPill(artifact, callbacks) {
  const pill = document.createElement("button");
  pill.type = "button";
  pill.className = "artifact-pill";
  pill.dataset[ARTIFACT_REL_DATASET_KEY] = artifact.rel;
  pill.title = `${artifact.name}（点击下载）`;
  const icon = document.createElement("span");
  icon.className = "artifact-pill-icon";
  // 默认路径图片走 buildImageNode 不进这里（恒 📄）；pillOnly 路径图片也走 pill，
  // 按扩展名换图标。
  icon.textContent = shouldRenderInline(artifact.name) ? "🖼️" : "📄";
  const name = document.createElement("span");
  name.className = "artifact-pill-name";
  name.textContent = artifact.name;
  pill.append(icon, name);
  // 用户附件路径 size 未知（标记里只有 uri / mimetype）—— 空文本时不挂 span。
  const sizeText = formatArtifactSize(artifact.size);
  if (sizeText) {
    const size = document.createElement("span");
    size.className = "artifact-pill-size";
    size.textContent = sizeText;
    pill.appendChild(size);
  }
  pill.addEventListener("click", () => {
    if (callbacks?.onRequestDownload) callbacks.onRequestDownload(artifact.rel);
  });
  return pill;
}

// ── 用户消息附件标记 ────────────────────────────────────────────────────────
// server 端 _attach_files_to_text 与 renderer 本地 echo 都把上传文件渲成独立一行
// `<attachment uri="share/x.png" mimetype="image/png"/>` 附在用户文本末尾（与 agentao
// 视觉降级标签同格式，transcript / prompt 口径不动）。展示层把这些行抽出来变
// 「文件图标 + 文件名」pill 挂气泡尾，不再以文本形式可见。
//
// quoteattr 在值含 `"` 时会改用单引号包裹，两种引号都接；属性值只反转义
// `&amp; &lt; &gt; &quot;`（rel 经 sanitize，正常不含特殊字符，这里是兜底）。
const ATTACHMENT_LINE_RE =
  /^\s*<attachment\s+uri=("[^"]*"|'[^']*')(?:\s+mimetype=("[^"]*"|'[^']*'))?\s*\/>\s*$/;

// 迁移前的旧格式（`<./share/x.png>` 整行）—— 既有 transcript 里仍存在，display 层
// 向后兼容渲成同样的 pill（不重写 transcript）。无转义（旧 server 端原样拼接）。
const LEGACY_ATTACHMENT_LINE_RE = /^\s*<\.\/([^<>]+)>\s*$/;

function unescapeXmlAttr(s) {
  return s
    .replaceAll("&quot;", '"')
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">")
    .replaceAll("&amp;", "&");
}

// 把用户消息文本拆成「可见正文 + 附件引用列表」。仅整行匹配的标记被抽出（用户手敲
// 在句中的 `<attachment` 字样原样保留）；解析出空 rel / 空文件名的畸形标记也保留为
// 文本，宁可丑也不静默吞内容。
export function parseUserAttachments(text) {
  const src = String(text ?? "");
  if (!src.includes("<attachment") && !src.includes("<./")) {
    return { body: src, refs: [] };
  }
  const bodyLines = [];
  const refs = [];
  for (const line of src.split("\n")) {
    let rel = null;
    const m = ATTACHMENT_LINE_RE.exec(line);
    if (m) {
      rel = unescapeXmlAttr(m[1].slice(1, -1));
    } else {
      const legacy = LEGACY_ATTACHMENT_LINE_RE.exec(line);
      if (legacy) rel = legacy[1];
    }
    const name = rel ? rel.split("/").pop() : "";
    if (rel && name) {
      refs.push({ rel, name });
    } else {
      bodyLines.push(line);
    }
  }
  return { body: bodyLines.join("\n"), refs };
}

// 把 parseUserAttachments 解析出的引用挂到用户气泡尾，走 pill 形态（pillOnly）。
// 容器 / 按 rel 去重 / 点击下载全部复用 attachArtifactToBubble 单一入口。
export function attachUserAttachments(bubble, refs, callbacks) {
  if (!bubble || !Array.isArray(refs)) return;
  for (const ref of refs) {
    attachArtifactToBubble(bubble, ref, callbacks, { pillOnly: true });
  }
}

// envelope_router 收到 ``file_download{purpose:"preview"}`` 时调，按 rel 把 bytes 填给
// 所有等待的 <img>。无等待者（rel 不在 pending 里）→ 静默丢，正常路径（用户也可能
// 手动点了下载触发）。
//
// 返回 ``true`` 表示有等待者被消费（envelope_router 据此跳过 a.download 兜底）；
// ``false`` 表示无 preview 等待，envelope_router 把 envelope 按 download 处理。
export function resolveArtifactPreview({ rel, content_b64, name, error }) {
  const waiters = pendingPreview.get(rel);
  if (!waiters || waiters.length === 0) return false;
  pendingPreview.delete(rel);
  if (error) {
    for (const img of waiters) {
      const fig = img.parentElement;
      if (!fig || !fig.isConnected) continue;
      fig.classList.add("artifact-image-error");
      img.alt = `${name || rel}（预览失败：${error}）`;
    }
    return true;
  }
  // mime 由扩展名推断 —— preview 通道只走 png/jpg/jpeg/gif/webp 白名单（SVG 走 pill
  // 路径不会进这里）。未知扩展退化成 octet-stream，浏览器一般也能直接渲常见 raster。
  const ext = artifactExtension(name || rel);
  const mime = ext === "png" ? "image/png"
    : ext === "gif" ? "image/gif"
    : ext === "webp" ? "image/webp"
    : ext === "jpg" || ext === "jpeg" ? "image/jpeg"
    : "application/octet-stream";
  const dataUrl = `data:${mime};base64,${content_b64}`;
  for (const img of waiters) {
    if (!img.isConnected) continue;
    img.src = dataUrl;
  }
  return true;
}

// 切房 / clear 后调，丢掉所有等待中的 preview 请求 —— 那些 <img> 节点已被
// messagesEl.replaceChildren 摘走，回包来了也没处灌；不清的话 pendingPreview Map 会
// 持续涨。
// P10.3 review §3 修：同步 unobserve 所有 lazy 触发的 figure —— IntersectionObserver
// 持的 target 引用会让 detached DOM 节点 + closure + image 占位字节永远不被 GC。
export function clearPendingArtifactPreviews() {
  pendingPreview.clear();
  if (_previewIO !== null) {
    for (const fig of _observedFigures) {
      _previewIO.unobserve(fig);
    }
  }
  _observedFigures.clear();
}

// 茶客气泡的 status tail（[中断] / [出错…] / [连接断开]）走 bubble 的 sibling
// .status-tail span —— textEl 已经被 innerHTML(markdown) 占据，纯文本尾巴塞同一节点
// 会被下一次 markdown 重渲覆盖；且 tail 视觉上属于"元信息"，不该走 markdown。
export function setStatusTail(bubble, text) {
  let tail = bubble.querySelector(":scope > .status-tail");
  if (!tail) {
    tail = document.createElement("span");
    tail.className = "status-tail";
    bubble.appendChild(tail);
  }
  tail.textContent = text;
}

export function removeStreamingCursor(bubble) {
  bubble.querySelector(":scope > .streaming-cursor")?.remove();
}
