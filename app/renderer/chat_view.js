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

// 静态文本渲染 + 挂复制按钮。流式路径不走这里 —— 它要自挂闭包版复制按钮才能动态
// 读 accumulated 缓冲。
export function renderGuestText({ textEl, bubble }, text) {
  textEl.innerHTML = renderMarkdown(text);
  attachCopyButton(bubble, () => text);
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
