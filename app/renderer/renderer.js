"use strict";

// 茶话室 renderer（P3.2.2）：在 P3.2.1 流式打字机基础上加 sidebar + @ 补全。
//
// envelope 消费完整路径：
//   room_info     → ws 连上首帧：装 sidebar（房间名 / topic / 茶客 + permission）
//                  + 喂 @ 补全候选 + 触发 user_display_name 显示
//   turn_start    → sidebar 各茶客名右侧显示本轮打分小数字（applyScoresToSidebar）
//   message_start → 起新一行 <li>（头像 + 气泡，无 score 徽章 —— 在 sidebar）
//   message_delta → 增量 append chunk 到 textEl
//   message_end   → 按 status 封口（ok 收尾 / cancelled / error）
//   turn_end      → 清 sidebar 上的打分
//   guest_thinking / tool_*  → 静默
//
// 用户消息（renderer 自己 echo 的那条）不走 envelope path —— 直接 appendBubble。

import { EventType, Status, Inbound, ScoreKind, NoticeLevel, DEFAULT_PERMISSION } from "./events.js";
import { marked } from "../node_modules/marked/lib/marked.esm.js";
import DOMPurify from "../node_modules/dompurify/dist/purify.es.mjs";

// gfm 开 GitHub 风格扩展（表格 / 删除线 / 任务列表）；breaks 让单换行 = <br>，
// 符合聊天里"按 Enter 换行"的直觉（LLM 输出也常用单换行分句）。
marked.setOptions({ gfm: true, breaks: true });

// LLM 输出走 marked → DOMPurify 一遍：前者结构化为 HTML，后者剥掉
// <script> / on* / javascript: 等危险载荷。USE_PROFILES.html 是 DOMPurify 推荐的
// 富文本白名单（允许 a/ul/ol/li/code/pre/blockquote/h*/table 但禁脚本）。
function renderMarkdown(text) {
  return DOMPurify.sanitize(marked.parse(text || ""), { USE_PROFILES: { html: true } });
}

// 流式重渲 ``innerHTML`` 会把节点全换一遍，用户正在做的拖选 / Cmd+C copy 会瞬间被擦。
// 这里检测有活动选区（非 collapsed）且 anchor 或 focus 在 node 子树内，调用方据此跳过
// 本次渲染、等 selection 解除再补渲。``isCollapsed`` 排除"光标位置"这种没意义的伪选区。
function isSelectionInside(node) {
  const sel = document.getSelection();
  if (!sel || sel.isCollapsed) return false;
  return node.contains(sel.anchorNode) || node.contains(sel.focusNode);
}

const statusEl = document.getElementById("status");
const messagesEl = document.getElementById("messages");
const composer = document.getElementById("composer");
const textInput = document.getElementById("text");
const submitBtn = composer.querySelector("button");
const roomNameEl = document.getElementById("room-name");
const roomTopicEl = document.getElementById("room-topic");
const guestsEl = document.getElementById("guests");
const roomsEl = document.getElementById("rooms");
const dropdownEl = document.getElementById("mention-dropdown");
const clearRoomBtn = document.getElementById("clear-room");
const addGuestBtn = document.getElementById("add-guest");
const importPersonaBtn = document.getElementById("import-persona");
const addRoomBtn = document.getElementById("add-room");
const addGuestModal = document.getElementById("add-guest-modal");
const addRoomModal = document.getElementById("add-room-modal");
const importPersonaModal = document.getElementById("import-persona-modal");
const addGuestListEl = document.getElementById("add-guest-list");
const newRoomNameEl = document.getElementById("new-room-name");
const newRoomTopicEl = document.getElementById("new-room-topic");
const newRoomRulesEl = document.getElementById("new-room-rules");
const newRoomGuestsEl = document.getElementById("new-room-guests");
const newRoomSubmitEl = document.getElementById("new-room-submit");
const importFolderPathEl = document.getElementById("import-folder-path");
const importFolderPickBtn = document.getElementById("import-folder-pick");
const importGithubUrlEl = document.getElementById("import-github-url");
const importPersonaSubmitBtn = document.getElementById("import-persona-submit");
const editUserMdBtn = document.getElementById("edit-user-md");
const uploadAvatarBtn = document.getElementById("upload-avatar");
const avatarFileInput = document.getElementById("avatar-file-input");
const editUserModal = document.getElementById("edit-user-modal");
const userMdTextarea = document.getElementById("user-md-textarea");
const userMdSourceHintEl = document.getElementById("user-md-source-hint");
const userMdSubmitBtn = document.getElementById("user-md-submit");
const userAvatarWrapEl = document.getElementById("user-avatar-wrap");
const userNameEl = document.getElementById("user-name");

const wsUrl = window.chahua?.wsUrl;
if (!wsUrl) {
  setStatus("error", "没有拿到 wsUrl —— preload / main 装配出了问题");
  throw new Error("missing wsUrl");
}

let ws = null;
let connected = false;

// 在途消息：message_id → { textEl, li }。
const inFlight = new Map();
// 当前 turn 的 id —— P3.3 cancel 状态机：turn_start 时设、turn_end(next=user) 或
// status=cancelled 时清；turn_end(next=ai) 不清（下个 turn_start 会刷新）。非 null 即
// "AI 链在跑"，submit button 切到「停止」语义，submit handler 路由到 cancel 帧。
let currentTurnId = null;
// 当前 turn 的打分明细。turn_start replace / turn_end 清。
let scoresByName = new Map();
// 茶客行 → 打分 span 的引用。sidebar 装好后填，turn_start/turn_end 直接改文字，
// 不重建 DOM（避免头像 / 徽章闪烁）。
const scoreSpansByName = new Map();
// 茶客名单（room_info 来时装）—— @ 补全候选 + 头像查找 + 用户显示名 / 头像。
let guests = []; // [{name, permission, isolation, avatar_data_uri}, ...]
let userDisplayName = "我";
let userAvatarDataUri = null;
// 可用 persona 候选（room_info 来时装）—— "添加茶客" / "新建房间"的 picker 用。
let personasAvailable = []; // [{persona, name, avatar_data_uri}, ...]
// 用户 USER.md 当前内容 + source 路径（room_info 时装）—— "编辑配置"modal prefill 用。
let userMdContent = "";
let userMdSource = null;

// 头像 <img> 通用工厂：dataUri 缺 → 返 null（调用方按"无头像"降级）。
function makeAvatarImg(dataUri, className, alt) {
  if (!dataUri) return null;
  const img = document.createElement("img");
  img.className = className;
  img.src = dataUri;
  img.alt = alt || "";
  // data URI 不会 404，但解码失败（坏图）时静默 hide。
  img.addEventListener("error", () => img.remove(), { once: true });
  return img;
}

// 茶客头像 —— 按名字在 guests 数组里 find；茶客 ≤ 个位数，线性查比维护并行 Map 简单
// （且任何 guests 变更都自动跟上）。
function makeAvatar(name, className) {
  return makeAvatarImg(
    guests.find((g) => g.name === name)?.avatar_data_uri,
    className,
    name,
  );
}

// 用户头像 —— 走 userAvatarDataUri（room_info 时装）。
function makeUserAvatar(className) {
  return makeAvatarImg(userAvatarDataUri, className, userDisplayName);
}

function setStatus(kind, text) {
  statusEl.className = `status ${kind}`;
  statusEl.textContent = text;
}

function setInputEnabled(enabled) {
  textInput.disabled = !enabled;
  submitBtn.disabled = !enabled;
  clearRoomBtn.disabled = !enabled;
  addGuestBtn.disabled = !enabled;
  importPersonaBtn.disabled = !enabled;
  addRoomBtn.disabled = !enabled;
  editUserMdBtn.disabled = !enabled;
  uploadAvatarBtn.disabled = !enabled;
}

// 按 currentTurnId 切换 submitBtn 的文字 + class —— 同一个按钮承担「发送 / 停止」
// 双重职责（参照 Claude / ChatGPT 输入框右侧的 send/stop 形变）；颜色由 .stop 类 +
// style.css 控制。
function updateSendButton() {
  if (currentTurnId === null) {
    submitBtn.textContent = "发送";
    submitBtn.classList.remove("stop");
  } else {
    submitBtn.textContent = "停止";
    submitBtn.classList.add("stop");
  }
}

function stickToBottom(mutate) {
  const stick = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 80;
  mutate();
  if (stick) messagesEl.scrollTop = messagesEl.scrollHeight;
}

function scoreText(r) {
  switch (r.kind) {
    case ScoreKind.MENTION: return "@";
    case ScoreKind.COOLDOWN: return "冷却";
    case ScoreKind.ERROR: return "失败";
    case ScoreKind.SCORED:
    default:
      return typeof r.score === "number" ? r.score.toFixed(2) : "?";
  }
}

// 通用 badge 工厂：guest-name / isolation / mention-name 走这个（文字徽章）。
// permission 不走这里 —— 走 makePermissionBadge（V 标）。
function makeBadge(className, dataKey, value) {
  const b = document.createElement("span");
  b.className = className;
  if (dataKey) b.dataset[dataKey] = value;
  b.textContent = value;
  return b;
}

// Permission V 标。workspace-write 蓝 / full-access 红 / read-only 调用方先过滤不渲染。
// 颜色经 data-permission 由 CSS 决定；title 给鼠标 hover 文本兜底 + 屏幕阅读器。
// 调用方决定 inline（默认）还是 overlay（加 .on-avatar，浮在头像右上角）。
function makePermissionBadge(permission, className) {
  const b = document.createElement("span");
  b.className = className;
  b.dataset.permission = permission;
  b.textContent = "✓";
  b.title = permission;
  return b;
}

// 头像 + 右上角 V 标的组合节点。头像缺图返 null（调用方自行决定 fallback）；
// 头像有 + 显著 permission → 用 ``.avatar-wrap`` 包起来，badge 浮右上角；
// 头像有 + 默认 permission → 直接返 img。
function makeAvatarWithPermission(g, avatarClassName, badgeClassName) {
  const img = makeAvatar(g.name, avatarClassName);
  if (!img) return null;
  const showBadge = g.permission && g.permission !== DEFAULT_PERMISSION;
  if (!showBadge) return img;
  const wrap = document.createElement("span");
  wrap.className = "avatar-wrap";
  wrap.appendChild(img);
  const badge = makePermissionBadge(g.permission, badgeClassName);
  badge.classList.add("on-avatar");
  wrap.appendChild(badge);
  return wrap;
}

// ── 权限 popover（点 sidebar 头像）───────────────────────────────────
//
// 三档权限文案 + 描述 —— 镜像 chahua/permissions.py 的 VALID_MODES。颜色与
// .permission-badge 的 [data-permission] 同枚举，read-only 不挂底色（默认浅灰）。
// 改这里的 value 字符串要跟 chahua/permissions.py::PermissionMode 同步。
const PERMISSION_OPTIONS = Object.freeze([
  { value: "read-only", label: "只读", desc: "仅看/搜索，不写文件、不跑 shell" },
  { value: "workspace-write", label: "工作区可写", desc: "可写茶客工作目录、跑常规命令" },
  { value: "full-access", label: "完全访问", desc: "放行所有工具（含潜在破坏性，谨慎）" },
]);

let permissionPopoverGuest = null;

function closePermissionPopover() {
  const pop = document.querySelector(".permission-popover");
  if (pop) pop.remove();
  document.removeEventListener("mousedown", _popoverOutsideHandler, true);
  document.removeEventListener("keydown", _popoverEscHandler);
  permissionPopoverGuest = null;
}

function _popoverOutsideHandler(ev) {
  const pop = document.querySelector(".permission-popover");
  if (!pop) return;
  // 点 popover 自身或当前 anchor 头像不关 —— 让用户在选项之间来回看；其它一律关。
  if (pop.contains(ev.target)) return;
  closePermissionPopover();
}

function _popoverEscHandler(ev) {
  if (ev.key === "Escape") {
    ev.stopPropagation();
    closePermissionPopover();
  }
}

// 在 anchor 元素附近浮出权限选择 popover。再次点同一头像 → 切回关闭（toggle）。
function showPermissionPopover(anchor, g) {
  if (permissionPopoverGuest === g.name) {
    closePermissionPopover();
    return;
  }
  closePermissionPopover();
  const current = g.permission || DEFAULT_PERMISSION;
  const pop = document.createElement("div");
  pop.className = "permission-popover";
  const title = document.createElement("div");
  title.className = "permission-popover-title";
  title.textContent = `设置「${g.name}」的权限`;
  pop.appendChild(title);
  for (const opt of PERMISSION_OPTIONS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "permission-option";
    btn.dataset.permission = opt.value;
    if (opt.value === current) btn.classList.add("current");
    // 左侧色点：与 V 标同色，read-only 用中性灰；视觉一眼分档。
    const swatch = document.createElement("span");
    swatch.className = "permission-swatch";
    swatch.dataset.permission = opt.value;
    btn.appendChild(swatch);
    const meta = document.createElement("span");
    meta.className = "permission-option-meta";
    const label = document.createElement("span");
    label.className = "permission-option-label";
    label.textContent = opt.label;
    if (opt.value === current) {
      const tag = document.createElement("span");
      tag.className = "permission-option-current";
      tag.textContent = "（当前）";
      label.appendChild(tag);
    }
    meta.appendChild(label);
    const desc = document.createElement("span");
    desc.className = "permission-option-desc";
    desc.textContent = opt.desc;
    meta.appendChild(desc);
    btn.appendChild(meta);
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (opt.value === current) {
        closePermissionPopover();
        return;
      }
      ws.send(JSON.stringify({
        type: Inbound.UPDATE_GUEST_PERMISSION,
        name: g.name,
        permission: opt.value,
      }));
      setStatus("", `设置「${g.name}」权限为 ${opt.label}…`);
      closePermissionPopover();
    });
    pop.appendChild(btn);
  }
  document.body.appendChild(pop);
  // 定位：贴 anchor 右侧；右侧不够（窄窗 / sidebar 贴边）退到 anchor 下方。
  const rect = anchor.getBoundingClientRect();
  pop.style.position = "fixed";
  pop.style.left = `${Math.round(rect.right + 8)}px`;
  pop.style.top = `${Math.round(rect.top)}px`;
  const popRect = pop.getBoundingClientRect();
  if (popRect.right > window.innerWidth - 8) {
    const fallbackLeft = Math.max(8, rect.left);
    pop.style.left = `${Math.round(fallbackLeft)}px`;
    pop.style.top = `${Math.round(rect.bottom + 6)}px`;
  }
  permissionPopoverGuest = g.name;
  // 下一 tick 才挂 outside handler —— 否则当前 click（冒泡到 document）会立刻关掉。
  setTimeout(() => {
    document.addEventListener("mousedown", _popoverOutsideHandler, true);
    document.addEventListener("keydown", _popoverEscHandler);
  }, 0);
}

// ── 消息流渲染 ───────────────────────────────────────────────────────
//
// 茶客发言（li.msg / li.error）：头像 + 气泡（header: 名字 + 打分徽章 / body: 文字），左对齐。
// 用户发言（li.user）：单独气泡，右对齐，无头像无名字（自己知道是自己）。
// turn-banner（li.turn-banner）：不变，meta 行，非气泡。

// 茶客行（speaker bubble + avatar）。streaming=true 时气泡末尾挂闪烁 ``.streaming-cursor``。
// 返回 li / textEl / bubble —— textEl 上由调用方 ``innerHTML = renderMarkdown(...)``
// 整段重渲；cursor 与状态尾走 sibling 节点，避开 innerHTML 替换的擦除。
//
// 打分不挂在气泡里 —— 沿 sidebar 茶客名右侧显示，见 ``applyScoresToSidebar``。
function makeGuestRow(speaker, { streaming = false } = {}) {
  const li = document.createElement("li");
  li.className = "msg";
  const avatar = makeAvatar(speaker, "msg-avatar");
  if (avatar) li.appendChild(avatar);
  const bubble = document.createElement("div");
  bubble.className = "bubble bubble-guest";
  const header = document.createElement("div");
  header.className = "bubble-header";
  const s = document.createElement("span");
  s.className = "speaker";
  s.textContent = speaker;
  header.appendChild(s);
  bubble.appendChild(header);
  const textEl = document.createElement("div");
  textEl.className = "text markdown";
  bubble.appendChild(textEl);
  if (streaming) {
    const cursor = document.createElement("span");
    cursor.className = "streaming-cursor";
    bubble.appendChild(cursor);
  }
  li.appendChild(bubble);
  return { li, textEl, bubble };
}

// 用户行：右对齐气泡 + 头像（无名字 —— 自己看自己 redundant）。镜像茶客布局：
// 茶客是 [头像][气泡]，用户是 [气泡][头像]。
function makeUserRow(text) {
  const li = document.createElement("li");
  li.className = "user";
  const bubble = document.createElement("div");
  bubble.className = "bubble bubble-user";
  const t = document.createElement("span");
  t.className = "text";
  t.textContent = text;
  bubble.appendChild(t);
  attachCopyButton(bubble, () => text);
  li.appendChild(bubble);
  const avatar = makeUserAvatar("msg-avatar");
  if (avatar) li.appendChild(avatar);
  return li;
}

// 气泡右上角 hover 出现的「复制」按钮。复制的是 ``getText()`` 返回的 markdown 源
// 而非 ``textEl.textContent`` —— 后者会把代码块 / 列表的结构压扁成连续文字，粘到别处
// 几乎不可读。流式气泡传 ``() => entry.accumulated`` 闭包动态读取；定稿气泡 / 用户
// 气泡传静态 ``() => text``。
function attachCopyButton(bubble, getText) {
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

// 静态茶客文本（历史回放 / 一次性 appendBubble / message_end fallback）的"渲染 +
// 挂复制按钮"三件套。流式茶客的复制按钮在 startStreamingMessage 单独挂（闭包要持
// inFlight entry 引用，动态读 accumulated）。
function renderGuestText({ textEl, bubble }, text) {
  textEl.innerHTML = renderMarkdown(text);
  attachCopyButton(bubble, () => text);
}

// 茶客气泡的 status tail（[中断] / [出错…] / [连接断开]）走 bubble 的 sibling
// .status-tail span —— textEl 已经被 innerHTML(markdown) 占据，纯文本尾巴塞同一节点
// 会被下一次 markdown 重渲覆盖；且 tail 视觉上属于"元信息"，不该走 markdown。
function setStatusTail(bubble, text) {
  let tail = bubble.querySelector(":scope > .status-tail");
  if (!tail) {
    tail = document.createElement("span");
    tail.className = "status-tail";
    bubble.appendChild(tail);
  }
  tail.textContent = text;
}

function removeStreamingCursor(bubble) {
  bubble.querySelector(":scope > .streaming-cursor")?.remove();
}

function appendBubble({ speaker, text, kind }) {
  stickToBottom(() => {
    let li;
    if (kind === "user") {
      li = makeUserRow(text);
    } else {
      const row = makeGuestRow(speaker);
      renderGuestText(row, text);
      if (kind === "error") row.li.classList.add("error");
      li = row.li;
    }
    messagesEl.appendChild(li);
  });
}

// 打分写到 sidebar 各茶客行的 ``.guest-score`` span 上（不是主聊天区）。
// scoresByName 没该茶客 → 清空 span（turn_end 走这条路径，统一清）。
function applyScoresToSidebar() {
  for (const [name, span] of scoreSpansByName) {
    const r = scoresByName.get(name);
    if (!r) {
      span.textContent = "";
      delete span.dataset.kind;
      continue;
    }
    span.textContent = scoreText(r);
    // kind=scored 走默认浅灰（数字打分），其余 kind 给 [data-kind=...] 语义色。
    if (r.kind && r.kind !== ScoreKind.SCORED) {
      span.dataset.kind = r.kind;
    } else {
      delete span.dataset.kind;
    }
  }
}

function startStreamingMessage(env) {
  stickToBottom(() => {
    const speaker = env.guest_name || "?";
    const { li, textEl, bubble } = makeGuestRow(speaker, { streaming: true });
    messagesEl.appendChild(li);
    // accumulated 累积完整 markdown 源 —— 每个 delta 整段重渲 innerHTML，
    // 因为 markdown 局部 patch（增量解析 + DOM diff）实现成本远大于聊天量级的全渲耗时。
    const entry = { textEl, li, bubble, accumulated: "" };
    inFlight.set(env.message_id, entry);
    // 闭包持 entry 引用，click 时读最新 accumulated（流式过程中也能复制到当前为止的全部）。
    attachCopyButton(bubble, () => entry.accumulated);
  });
}

function appendDelta(env) {
  const m = inFlight.get(env.message_id);
  if (!m) return;
  const chunk = env.data?.chunk ?? "";
  if (!chunk) return;
  m.accumulated += chunk;
  // 选区在 textEl 内 → 跳渲，让用户的拖选 / Cmd+C 不被擦；下一个 chunk 来时如果选区
  // 已解除，会一次性追上累积差额（accumulated 全量重渲）。
  if (isSelectionInside(m.textEl)) return;
  stickToBottom(() => {
    m.textEl.innerHTML = renderMarkdown(m.accumulated);
  });
}

function statusTail(env) {
  if (env.status === Status.CANCELLED) return "[中断]";
  if (env.status === Status.ERROR) {
    const err = env.data?.error || "未知错误";
    return `[出错：${err}]`;
  }
  return "";
}

function endStreamingMessage(env) {
  const m = inFlight.get(env.message_id);
  if (!m) {
    // server 没发过 message_start 直接 message_end（罕见但合法 —— 比如缓存命中
    // 整段一次性回）。OK 路径走 appendBubble；error 路径要挂 status-tail，appendBubble
    // 没那个口子，单独装一行。
    const speaker = env.guest_name || "?";
    if (env.status === Status.OK) {
      appendBubble({ speaker, text: env.data?.text ?? "" });
      return;
    }
    const partial = env.data?.partial_text ?? "";
    const row = makeGuestRow(speaker);
    renderGuestText(row, partial);
    row.li.classList.add("error");
    setStatusTail(row.bubble, statusTail(env));
    stickToBottom(() => messagesEl.appendChild(row.li));
    return;
  }
  inFlight.delete(env.message_id);
  stickToBottom(() => {
    removeStreamingCursor(m.bubble);
    if (env.status !== Status.OK) {
      m.li.classList.add("error");
      setStatusTail(m.bubble, statusTail(env));
    }
    // 选区还在 textEl 内 —— 用户正在 copy。光标 / 状态尾 / error class 先就位，但
    // 最终 markdown 内容延后渲染到 selection 解除（一次性 selectionchange 监听）；
    // 否则 innerHTML 替换会瞬间擦掉选区，复制操作功亏一篑。
    // ``isConnected`` 是兜底：换房 / 清空 / 断线把 bubble 从 DOM 摘了，下一次任何
    // selectionchange 触发时本监听器自卸，避免持 m 引用 + 写到孤立节点。
    if (isSelectionInside(m.textEl)) {
      const finalize = () => {
        if (!m.textEl.isConnected) {
          document.removeEventListener("selectionchange", finalize);
          return;
        }
        if (isSelectionInside(m.textEl)) return;
        document.removeEventListener("selectionchange", finalize);
        m.textEl.innerHTML = renderMarkdown(m.accumulated);
      };
      document.addEventListener("selectionchange", finalize);
      return;
    }
    m.textEl.innerHTML = renderMarkdown(m.accumulated);
  });
}

function closeInFlightOnDisconnect() {
  if (inFlight.size === 0) return;
  for (const m of inFlight.values()) {
    removeStreamingCursor(m.bubble);
    m.li.classList.add("error");
    setStatusTail(m.bubble, "[连接断开]");
  }
  inFlight.clear();
}

// ── sidebar 装配（room_info）────────────────────────────────────────

function renderSidebar(roomInfo) {
  // 进新房（首次连接 / 换房）的全量重置：清 in-flight 流 + 当前 turn 打分残留 + 消息
  // 容器，让接下来的 room_history.replaceChildren 不闪过旧房 DOM。
  inFlight.clear();
  scoresByName = new Map();
  scoreSpansByName.clear();
  messagesEl.replaceChildren();
  // sidebar 全量重渲会替掉头像 DOM —— 旧 anchor 一旦被 detach，popover 的"贴右侧"
  // 位置就指向虚空了，干脆关掉。
  closePermissionPopover();
  setStatus("ok", `已连接 ${wsUrl}`);
  roomNameEl.textContent = roomInfo.room_name || "—";
  roomTopicEl.textContent = roomInfo.topic || "";
  userDisplayName = roomInfo.user_display_name || "我";
  userAvatarDataUri = roomInfo.user_avatar_data_uri || null;
  userMdContent = roomInfo.user_md_content || "";
  userMdSource = roomInfo.user_md_source || null;
  renderUserRow();
  guests = Array.isArray(roomInfo.guests) ? roomInfo.guests : [];
  personasAvailable = Array.isArray(roomInfo.personas_available) ? roomInfo.personas_available : [];
  guestsEl.replaceChildren();
  // 最后一位茶客不能删（与 server 端 admin.remove_guest 硬约束一致）—— 前端禁用按钮，
  // 用户少踩一次"提交后才发现不行"的坑。
  const lastGuestLock = guests.length <= 1;
  for (const g of guests) {
    const li = document.createElement("li");
    // V 标默认浮在头像右上角；缺头像（罕见）时回退到名字后 inline，避免丢失权限提示。
    const showBadge = g.permission && g.permission !== DEFAULT_PERMISSION;
    const node = makeAvatarWithPermission(g, "avatar", "permission-badge");
    // 头像 / 名字都可点 —— 点了开"设置权限"popover。头像存在则 click anchor 用头像，
    // 否则 fall through 到名字徽章（保证用户始终能找到入口）。
    if (node) {
      node.classList.add("permission-anchor");
      node.title = `点击设置「${g.name}」的权限（当前 ${g.permission || DEFAULT_PERMISSION}）`;
      node.addEventListener("click", (ev) => {
        ev.stopPropagation();
        if (!connected) return;
        showPermissionPopover(node, g);
      });
      li.appendChild(node);
    }
    const nameBadge = makeBadge("guest-name", null, g.name);
    nameBadge.classList.add("permission-anchor");
    nameBadge.title = `点击设置「${g.name}」的权限（当前 ${g.permission || DEFAULT_PERMISSION}）`;
    nameBadge.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (!connected) return;
      showPermissionPopover(node || nameBadge, g);
    });
    li.appendChild(nameBadge);
    if (!node && showBadge) {
      li.appendChild(makePermissionBadge(g.permission, "permission-badge"));
    }
    if (g.isolation && g.isolation !== "room") {
      li.appendChild(makeBadge("isolation-badge", "isolation", g.isolation));
    }
    // 打分小数字（turn_start 时填，turn_end 时清）。margin-left:auto 推到右侧；
    // 空 textContent 时占位不可见，文字一来就显示，不引发布局抖动（min-width）。
    const score = document.createElement("span");
    score.className = "guest-score";
    li.appendChild(score);
    scoreSpansByName.set(g.name, score);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "row-remove";
    remove.textContent = "×";
    if (lastGuestLock) {
      remove.disabled = true;
      remove.title = "至少要保留一位茶客";
    } else {
      remove.title = `请离 ${g.name}`;
      remove.addEventListener("click", () => {
        if (!connected) return;
        if (!window.confirm(`请离茶客「${g.name}」？\n房间历史不动，guests/${g.name}/ 工作目录也不会被删，但不会再参与对话。`)) return;
        ws.send(JSON.stringify({ type: Inbound.REMOVE_GUEST, name: g.name }));
        setStatus("", `请离 ${g.name}…`);
      });
    }
    li.appendChild(remove);
    guestsEl.appendChild(li);
  }
  renderRoomsList(roomInfo.rooms_available, roomInfo.current_room_id);
  // room_info 到达 → composer 解锁；之前 onopen 不再 enable，避免 userDisplayName
  // 跳变窗口（用户在 "我" 状态发了一条，第二条又变成实际显示名）。
  setInputEnabled(true);
  textInput.focus();
}

// 切换房间列表 —— 列其它房间（含当前），click 非 current 项发 switch_room frame。
// 当前房间高亮、不可点；其它房间显示 name + topic 一句话预览，hover 出现"删除"按钮。
function renderRoomsList(roomsAvailable, currentRoomId) {
  roomsEl.replaceChildren();
  const rooms = Array.isArray(roomsAvailable) ? roomsAvailable : [];
  for (const r of rooms) {
    const li = document.createElement("li");
    li.dataset.roomId = r.room_id;
    if (r.room_id === currentRoomId) li.classList.add("current");
    // text 块外裹 .room-meta，方便 row-remove 用 margin-left:auto 推到右侧。
    const meta = document.createElement("div");
    meta.className = "room-meta";
    const name = document.createElement("div");
    name.className = "room-name";
    name.textContent = r.name || r.room_id;
    meta.appendChild(name);
    if (r.topic) {
      const topic = document.createElement("div");
      topic.className = "room-topic";
      topic.textContent = r.topic;
      meta.appendChild(topic);
    }
    li.appendChild(meta);
    if (r.room_id !== currentRoomId) {
      li.addEventListener("click", () => {
        if (!connected) return;
        ws.send(JSON.stringify({ type: Inbound.SWITCH_ROOM, room_id: r.room_id }));
        setStatus("", `切换到 ${r.name || r.room_id}…`);
      });
      // 删除房间按钮 —— 只对非当前房显示。当前房不能删（先切走再删，与 server 端约束一致）。
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "row-remove";
      remove.textContent = "×";
      remove.title = `删除房间 ${r.name || r.room_id}`;
      remove.addEventListener("click", (ev) => {
        ev.stopPropagation();
        if (!connected) return;
        if (!window.confirm(`删除房间「${r.name || r.room_id}」？\n房间目录及其全部历史 / 摘要 / 茶客工作区会被永久删除，无法撤销。`)) return;
        ws.send(JSON.stringify({ type: Inbound.DELETE_ROOM, room_id: r.room_id }));
        setStatus("", `删除 ${r.name || r.room_id}…`);
      });
      li.appendChild(remove);
    }
    roomsEl.appendChild(li);
  }
}

// ── @ 补全 ────────────────────────────────────────────────────────

let mentionActive = -1; // dropdown 高亮项索引；-1 表示未显示

// 找输入框当前光标位置前最近的 "@<query>"；返回 {start, end, query} 或 null。
//
// 字符集放到 `\S*`（任何非空白）—— 与 server 端 _AT_PATTERN 反黑名单语义对齐，
// 含 `-` `.` 撇号的茶客名也能被前端 typeahead 捕到；候选过滤靠 guests 名册的
// startsWith 兜底（不在册的名字自然 dropdown 为空，hideMentionDropdown）。
function detectMention() {
  const end = textInput.selectionStart ?? textInput.value.length;
  const before = textInput.value.slice(0, end);
  const m = /(?:^|\s)@(\S*)$/.exec(before);
  if (!m) return null;
  // @ 在 m[0] 中的偏移：m[0] 起头是 @ 时偏 0，是空白时偏 1。
  const start = m.index + (m[0].startsWith("@") ? 0 : 1);
  return { start, end, query: m[1] };
}

// @broadcast 候选项 —— 镜像 chahua/orchestrator.py:_BROADCAST_TOKENS 的"全员"语义。
// 用 `.find()` 选**第一个匹配**返回单条，避免空 query 时 "所有人 / ALL" 同时
// 占两行（语义重复）。顺序决定优先级：中文优先（茶话室中文为主），拉丁起头时
// fallthrough 到 ALL。server 端 _BROADCAST_TOKENS 还包含 everyone / 大家 / 各位
// 三个不展示但能手输的兼容词。
const BROADCAST_CANDIDATES = Object.freeze([
  { name: "所有人", broadcast: true },
  { name: "ALL", broadcast: true },
]);

function matchGuests(query) {
  const q = query.toLowerCase();
  const broadcast = BROADCAST_CANDIDATES.find((b) =>
    b.name.toLowerCase().startsWith(q)
  );
  const guestMatches = guests.filter((g) => g.name.toLowerCase().startsWith(q));
  // broadcast 候选放前面 —— 广播是更强烈的意图（"我要全员注意"vs"我点名某个人"）。
  return broadcast ? [broadcast, ...guestMatches] : guestMatches;
}

function showMentionDropdown(candidates, match) {
  if (candidates.length === 0) {
    hideMentionDropdown();
    return;
  }
  dropdownEl.replaceChildren();
  candidates.forEach((g, i) => {
    const li = document.createElement("li");
    if (i === 0) li.className = "active";
    if (g.broadcast) li.classList.add("broadcast");
    li.appendChild(makeBadge("mention-name", null, g.name));
    // broadcast 项不显示 permission 徽章；只茶客有 permission 概念。
    if (!g.broadcast && g.permission && g.permission !== DEFAULT_PERMISSION) {
      li.appendChild(makePermissionBadge(g.permission, "mention-permission"));
    }
    if (g.broadcast) {
      const hint = document.createElement("span");
      hint.className = "mention-hint";
      hint.textContent = "广播";
      li.appendChild(hint);
    }
    li.dataset.name = g.name;
    li.addEventListener("mousedown", (ev) => {
      // mousedown 而非 click —— input blur 之前完成补全，避免 dropdown 先消失。
      ev.preventDefault();
      acceptMention(match, g.name);
    });
    dropdownEl.appendChild(li);
  });
  mentionActive = 0;
  dropdownEl.hidden = false;
}

function hideMentionDropdown() {
  dropdownEl.hidden = true;
  mentionActive = -1;
}

function moveActive(delta) {
  const items = dropdownEl.querySelectorAll("li");
  if (items.length === 0) return;
  items[mentionActive]?.classList.remove("active");
  mentionActive = (mentionActive + delta + items.length) % items.length;
  items[mentionActive].classList.add("active");
  items[mentionActive].scrollIntoView({ block: "nearest" });
}

function acceptMention(match, name) {
  // match 由调用方上下文传入（input 路径 / keydown 路径都拿同一份 match），
  // 不在这里重算 —— 避免与触发时的 detectMention 视图不一致。
  const v = textInput.value;
  const before = v.slice(0, match.start);
  const after = v.slice(match.end);
  textInput.value = `${before}@${name} ${after}`;
  const cursor = before.length + 1 + name.length + 1;
  textInput.setSelectionRange(cursor, cursor);
  hideMentionDropdown();
  textInput.focus();
}

textInput.addEventListener("input", () => {
  const m = detectMention();
  if (!m) { hideMentionDropdown(); return; }
  showMentionDropdown(matchGuests(m.query), m);
});

textInput.addEventListener("keydown", (ev) => {
  // 中文 IME 候选窗 Enter（拼音 → 汉字）会先发 keydown isComposing=true。
  // 不拦：让 IME 自己消费 Enter，待 compositionend 后再走正常 input 流程。
  if (ev.isComposing || ev.keyCode === 229) return;
  if (dropdownEl.hidden) return;
  if (ev.key === "ArrowDown") { ev.preventDefault(); moveActive(1); }
  else if (ev.key === "ArrowUp") { ev.preventDefault(); moveActive(-1); }
  else if (ev.key === "Enter" || ev.key === "Tab") {
    const items = dropdownEl.querySelectorAll("li");
    if (mentionActive >= 0 && items[mentionActive]) {
      ev.preventDefault();
      const m = detectMention();
      if (m) acceptMention(m, items[mentionActive].dataset.name);
    }
  } else if (ev.key === "Escape") {
    ev.preventDefault();
    hideMentionDropdown();
  }
});

textInput.addEventListener("blur", () => {
  // mousedown 在 blur 之前已完成 acceptMention；这里只是兜底关 dropdown。
  setTimeout(hideMentionDropdown, 100);
});

// ── envelope 分派 ────────────────────────────────────────────────

// USER 的稳定 ID（与 chahua/user_md.py::USER_SPEAKER_ID 同源；transcript.jsonl 里
// 用户发言的 speaker_id 字段值）。前端按 speaker_id 分派气泡布局。
const USER_SPEAKER_ID = "user";

function renderHistory(messages) {
  // 进 Room（首次连接 / 重连 / 换房）时全量回放。messagesEl 的清空由 renderSidebar
  // 一帧前完成（room_info 先于 room_history 到达），单点 owner 改一处即可。
  if (!Array.isArray(messages) || messages.length === 0) return;
  stickToBottom(() => {
    for (const m of messages) {
      if (m.speaker_id === USER_SPEAKER_ID) {
        messagesEl.appendChild(makeUserRow(m.text));
      } else {
        const row = makeGuestRow(m.speaker_id);
        renderGuestText(row, m.text);
        messagesEl.appendChild(row.li);
      }
    }
    // 强制滚到底 —— stickToBottom 在 mutate 前 messagesEl 是空，stick=true，自动定到底。
  });
}

function handleEnvelope(env) {
  switch (env.type) {
    case EventType.ROOM_INFO:
      renderSidebar(env.data ?? {});
      return;
    case EventType.ROOM_HISTORY:
      renderHistory(env.data?.messages ?? []);
      return;
    case EventType.TURN_START: {
      const scores = env.data?.scores ?? [];
      console.debug("[turn_start]", scores);
      scoresByName = new Map(scores.map((r) => [r.guest_name, r]));
      applyScoresToSidebar();
      currentTurnId = env.turn_id;
      updateSendButton();
      return;
    }
    case EventType.MESSAGE_START:
      startStreamingMessage(env);
      return;
    case EventType.MESSAGE_DELTA:
      appendDelta(env);
      return;
    case EventType.MESSAGE_END:
      endStreamingMessage(env);
      return;
    case EventType.TURN_END: {
      scoresByName = new Map();
      applyScoresToSidebar();
      // AI 链终态判定：status != ok（cancelled）或 next==='user'（用户该说话了）→ 收
      // 「停止」按钮。next==='ai' 时维持，下一个 turn_start 立刻刷新 currentTurnId。
      const next = env.data?.next;
      if (env.status !== Status.OK || next === "user") {
        currentTurnId = null;
        updateSendButton();
      }
      return;
    }
    case EventType.NOTICE: {
      // 服务端 mutator 的一次性反馈 —— 目前 persona 导入用。错误强提示 alert，
      // 让用户立刻看到原因；info 走 status bar 不打断流。
      const level = env.data?.level || NoticeLevel.INFO;
      const text = env.data?.text || "";
      if (!text) return;
      if (level === NoticeLevel.ERROR) {
        window.alert(text);
        setStatus("error", text);
      } else {
        setStatus("ok", text);
      }
      return;
    }
    // guest_thinking / tool_* 暂时静默。
  }
}

// ── ws 连接 + 重连退避 ─────────────────────────────────────────────

// 退避梯度（ms）。超过梯度长度后稳定在最后一档，永远尝试 —— 桌面 App 无云端账号，
// 用户没退就接着连；不退指数到分钟级避免长时间断线后用户等不及。
const RECONNECT_BACKOFF_MS = [1000, 2000, 5000, 10000];

// 这些 close code 表示对端"主动且无意挽回"，不自动重连：
//   1000 normal closure（双方约定关）
//   1001 going away（页面切走 / 后端 quit）
//   1008 policy violation（server.py 拒第二客户端 —— 重连只会再次被拒）
const NO_RECONNECT_CODES = new Set([1000, 1001, 1008]);

let reconnectAttempt = 0;

function reconnectDelayMs() {
  const i = Math.min(reconnectAttempt, RECONNECT_BACKOFF_MS.length - 1);
  return RECONNECT_BACKOFF_MS[i];
}

function scheduleReconnect() {
  const delay = reconnectDelayMs();
  reconnectAttempt += 1;
  setStatus("error", `连接断开 —— 第 ${reconnectAttempt} 次重试，${delay / 1000}s 后…`);
  // P3.3 加"立即重连 / 停止重连"按钮时再持有 timer 引用便于 clearTimeout。
  setTimeout(connect, delay);
}

function connect() {
  // 重试中文案 vs 首次连接区分开 —— 首次空白，重试带次数。
  const tag = reconnectAttempt > 0 ? `（第 ${reconnectAttempt} 次重试）` : "";
  setStatus("", `连接中… ${wsUrl}${tag}`);
  setInputEnabled(false);
  ws = new WebSocket(wsUrl);
  ws.addEventListener("open", () => {
    connected = true;
    reconnectAttempt = 0;
    setStatus("ok", `已连接 ${wsUrl}（等 room_info）`);
    // composer 解锁延迟到 renderSidebar —— 让 user echo 名字、@ 候选都准备就绪
    // 后再让用户能输入；避免 "我" → 真名跳变 + @ 候选空白窗口。
  });
  ws.addEventListener("message", (ev) => {
    try {
      handleEnvelope(JSON.parse(ev.data));
    } catch (e) {
      console.error("envelope parse failed:", e, ev.data);
    }
  });
  ws.addEventListener("close", (ev) => {
    connected = false;
    setInputEnabled(false);
    closeInFlightOnDisconnect();
    // 断线时 turn_end 不会再来；本地清"停止"状态，让重连成功后按钮回到"发送"。
    currentTurnId = null;
    updateSendButton();
    if (NO_RECONNECT_CODES.has(ev.code)) {
      setStatus("error", `连接断开 (${ev.code} ${ev.reason || ""})`);
      return;
    }
    scheduleReconnect();
  });
  ws.addEventListener("error", (ev) => {
    console.error("ws error", ev);
  });
}

// ── 添加茶客 / 新建房间 modal ─────────────────────────────────────────

function openModal(modal) {
  modal.hidden = false;
}

function closeModal(modal) {
  modal.hidden = true;
}

// 渲染 persona picker：单选 onPick(persona)；多选给 li 加 .selected toggle 状态。
// excludeNames：当前房间已在场茶客名（添加时禁掉重复项；新建房不传，所有候选都可选）。
function renderPersonaPicker(rootEl, { multi, excludeNames, onPick }) {
  rootEl.replaceChildren();
  if (personasAvailable.length === 0) {
    const empty = document.createElement("li");
    empty.className = "persona-empty";
    empty.textContent = "没有可用的人设。可在 chahua/personas/ 下放置 <name>.md（可选 <name>.png 头像）。";
    rootEl.appendChild(empty);
    return;
  }
  for (const p of personasAvailable) {
    const li = document.createElement("li");
    li.className = "persona-item";
    li.dataset.persona = p.persona;
    li.dataset.name = p.name;
    const img = makeAvatarImg(p.avatar_data_uri, "persona-avatar", p.name);
    if (img) li.appendChild(img);
    const name = document.createElement("span");
    name.className = "persona-name";
    name.textContent = p.name;
    li.appendChild(name);
    if (excludeNames && excludeNames.has(p.name)) {
      li.classList.add("disabled");
      const tag = document.createElement("span");
      tag.className = "persona-hint";
      tag.textContent = "已在场";
      li.appendChild(tag);
    } else {
      li.addEventListener("click", () => {
        if (multi) {
          li.classList.toggle("selected");
          return;
        }
        onPick(p);
      });
    }
    rootEl.appendChild(li);
  }
}

addGuestBtn.addEventListener("click", () => {
  if (!connected) return;
  const inRoom = new Set(guests.map((g) => g.name));
  renderPersonaPicker(addGuestListEl, {
    multi: false,
    excludeNames: inRoom,
    onPick: (p) => {
      ws.send(JSON.stringify({
        type: Inbound.ADD_GUEST,
        persona: p.persona,
        name: p.name,
        permission: DEFAULT_PERMISSION,
      }));
      setStatus("", `添加茶客 ${p.name}…`);
      closeModal(addGuestModal);
    },
  });
  openModal(addGuestModal);
});

addRoomBtn.addEventListener("click", () => {
  if (!connected) return;
  newRoomNameEl.value = "";
  newRoomTopicEl.value = "";
  newRoomRulesEl.value = "";
  renderPersonaPicker(newRoomGuestsEl, { multi: true, excludeNames: null });
  openModal(addRoomModal);
  newRoomNameEl.focus();
});

// ── 导入 persona modal ─────────────────────────────────────────────────────

// 重置 modal 状态 —— 每次打开重置选项 / 输入，避免上次输入残留误导用户。
function resetImportPersonaModal() {
  const folderRadio = importPersonaModal.querySelector("input[name='import-source'][value='folder']");
  if (folderRadio) folderRadio.checked = true;
  importFolderPathEl.value = "";
  importGithubUrlEl.value = "";
}

importPersonaBtn.addEventListener("click", () => {
  if (!connected) return;
  resetImportPersonaModal();
  openModal(importPersonaModal);
});

importFolderPickBtn.addEventListener("click", async () => {
  // contextBridge 暴露的 main → dialog.showOpenDialog handle —— 返绝对路径或 null。
  // 没拿到 chahua.pickFolder 说明 preload 旧版（dev hot-reload corner case），给个降级提示。
  const pickFolder = window.chahua?.pickFolder;
  if (typeof pickFolder !== "function") {
    window.alert("当前 Electron 环境不支持文件夹选择，请直接粘贴绝对路径。");
    importFolderPathEl.readOnly = false;
    return;
  }
  const picked = await pickFolder();
  if (picked) importFolderPathEl.value = picked;
});

importPersonaSubmitBtn.addEventListener("click", () => {
  if (!connected) return;
  const source = importPersonaModal.querySelector("input[name='import-source']:checked")?.value;
  if (source === "github") {
    const url = importGithubUrlEl.value.trim();
    if (!url) {
      importGithubUrlEl.focus();
      return;
    }
    ws.send(JSON.stringify({ type: Inbound.IMPORT_PERSONA_GITHUB, url }));
    setStatus("", `导入 GitHub persona…`);
  } else {
    const path = importFolderPathEl.value.trim();
    if (!path) {
      window.alert("先选一个目录，或粘贴绝对路径。");
      return;
    }
    ws.send(JSON.stringify({ type: Inbound.IMPORT_PERSONA_FOLDER, path }));
    setStatus("", `导入本地 persona…`);
  }
  closeModal(importPersonaModal);
});

newRoomSubmitEl.addEventListener("click", () => {
  if (!connected) return;
  const name = newRoomNameEl.value.trim();
  if (!name) {
    newRoomNameEl.focus();
    return;
  }
  const selected = Array.from(newRoomGuestsEl.querySelectorAll("li.persona-item.selected"));
  if (selected.length === 0) {
    window.alert("至少选 1 位茶客");
    return;
  }
  const guestsPayload = selected.map((li) => ({
    persona: li.dataset.persona,
    name: li.dataset.name,
    permission: DEFAULT_PERMISSION,
  }));
  // room_id 用 name 直接当目录名 —— server 端 normalize_room_id 会再洗一遍非法字符；
  // 暴露独立 id 字段会增加 UI 复杂度（用户难以理解 id vs name 的区别）。
  ws.send(JSON.stringify({
    type: Inbound.CREATE_ROOM,
    room_id: name,
    name,
    topic: newRoomTopicEl.value.trim(),
    rules: newRoomRulesEl.value.trim(),
    guests: guestsPayload,
  }));
  setStatus("", `新建房间 ${name}…`);
  closeModal(addRoomModal);
});

// ── 我（USER.md / 头像）─────────────────────────────────────────────

function renderUserRow() {
  userNameEl.textContent = userDisplayName;
  userAvatarWrapEl.replaceChildren();
  const img = makeAvatarImg(userAvatarDataUri, "user-avatar", userDisplayName);
  if (img) userAvatarWrapEl.appendChild(img);
}

editUserMdBtn.addEventListener("click", () => {
  if (!connected) return;
  userMdTextarea.value = userMdContent;
  userMdSourceHintEl.textContent = userMdSource
    ? `当前文件：${userMdSource}`
    : "尚无 USER.md，保存后会落到 user_data_root/USER.md。";
  openModal(editUserModal);
  userMdTextarea.focus();
});

userMdSubmitBtn.addEventListener("click", () => {
  if (!connected) return;
  ws.send(JSON.stringify({
    type: Inbound.UPDATE_USER_MD,
    content: userMdTextarea.value,
  }));
  setStatus("", "保存用户配置…");
  closeModal(editUserModal);
});

uploadAvatarBtn.addEventListener("click", () => {
  if (!connected) return;
  // reset 让相同文件再选也能触发 change（浏览器对同名同源文件默认不再 fire）。
  avatarFileInput.value = "";
  avatarFileInput.click();
});

// 头像上传：浏览器对 PNG / JPEG / WebP / GIF 都能 <img> 原生解码 → 画进 canvas →
// 一律 toDataURL("image/png") 出去，服务端只认 PNG。也即"用户传 JPG/WebP/GIF，
// 落盘 PNG"是这里完成的转换。GIF 走 <img> 时 canvas 只能拿到首帧 —— 静态头像
// 场景下这是符合预期的（动图当头像意义不大、且 PNG 不存动）。中央裁方 + 缩到
// AVATAR_TARGET_PX 让头像形状一致 + 压缩体积（256×256 PNG 大约 30~80KB）。
const AVATAR_TARGET_PX = 256;
// 浏览器 File MIME 走 file.type；accept 已经在 input 上限定，这里再校验一次防御
// 用户用 drag-drop 等绕路或 type 为空的 corner case。
const AVATAR_ACCEPTED_MIME = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
]);
// 用于状态条提示文案 —— 让用户感知到"我传的是 X，落地是 PNG"的转换发生。
const AVATAR_FORMAT_LABEL = {
  "image/jpeg": "JPG",
  "image/webp": "WebP",
  "image/gif": "GIF",
};

avatarFileInput.addEventListener("change", () => {
  const file = avatarFileInput.files?.[0];
  if (!file) return;
  if (file.type && !AVATAR_ACCEPTED_MIME.has(file.type)) {
    window.alert(`不支持的图片格式：${file.type}\n请选 PNG / JPG / WebP / GIF。`);
    return;
  }
  if (file.size > 20 * 1024 * 1024) {
    window.alert("图片超过 20MB，挑张小一点的吧。");
    return;
  }
  const url = URL.createObjectURL(file);
  const img = new Image();
  img.onload = () => {
    URL.revokeObjectURL(url);
    const dataUri = cropAndEncodeAvatar(img);
    ws.send(JSON.stringify({
      type: Inbound.UPDATE_USER_AVATAR,
      data_uri: dataUri,
    }));
    const label = AVATAR_FORMAT_LABEL[file.type];
    setStatus("", label ? `上传头像（${label} → PNG）…` : "上传头像…");
  };
  img.onerror = () => {
    URL.revokeObjectURL(url);
    window.alert("图片解码失败，文件可能损坏或格式不被浏览器支持。");
  };
  img.src = url;
});

// 中央裁方 + 等比缩到 AVATAR_TARGET_PX × AVATAR_TARGET_PX。
// 裁方原因：sidebar / 气泡里的头像 wrapper 都是圆形（border-radius:50%），方形源
// 截出来的圆刚好居中；矩形源会被 object-fit:cover 切边，不如 server 端就裁齐
// 让落盘文件本身没浪费像素。原图比目标小则不放大，保留 native 分辨率。
function cropAndEncodeAvatar(img) {
  const side = Math.min(img.width, img.height);
  const sx = Math.floor((img.width - side) / 2);
  const sy = Math.floor((img.height - side) / 2);
  const target = Math.min(AVATAR_TARGET_PX, side);
  const canvas = document.createElement("canvas");
  canvas.width = target;
  canvas.height = target;
  const ctx = canvas.getContext("2d");
  // 缩放质量 —— 浏览器默认 imageSmoothingQuality 是 "low"，"high" 在缩图时
  // 视觉差异明显（128px 头像里头发 / 五官清晰度肉眼可辨）。
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(img, sx, sy, side, side, 0, 0, target, target);
  return canvas.toDataURL("image/png");
}

// modal 关闭：点 backdrop（modal-backdrop 自身、不是内部 .modal）/ × 按钮 / ESC。
const ALL_MODALS = [addGuestModal, addRoomModal, editUserModal, importPersonaModal];
for (const modal of ALL_MODALS) {
  modal.addEventListener("click", (ev) => {
    if (ev.target === modal) closeModal(modal);
    if (ev.target.matches("[data-close]")) closeModal(modal);
  });
}
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  for (const m of ALL_MODALS) if (!m.hidden) closeModal(m);
});

// 清空聊天：本地不抢先清 DOM，让回环一致 —— 服务端清完会重发 room_info +
// room_history(空)，renderSidebar 一帧 messagesEl.replaceChildren；失败 / 服务端拒收
// 时 UI 不会出现"明明清了又冒出来"的诡异状态。
clearRoomBtn.addEventListener("click", () => {
  if (!connected) return;
  const roomName = roomNameEl.textContent;
  if (!window.confirm(`确定清空「${roomName}」的全部聊天记录？\n茶客在场，但本房间的 transcript / 摘要 / 游标会被重置。`)) return;
  ws.send(JSON.stringify({ type: Inbound.CLEAR_ROOM }));
  setStatus("", `清空「${roomName}」…`);
});

composer.addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (!dropdownEl.hidden) {
    // dropdown 开着时 Enter 已在 keydown 里被消费；这里防御性 noop。
    return;
  }
  if (!connected) return;
  // 「停止」分支：currentTurnId 非空表示 AI 链在跑，submit 按钮处于「停止」语义。
  // 服务端收到后 task.cancel() 当前 turn task，turn_end(status=cancelled) 回来时
  // 状态机会清掉 currentTurnId，按钮自动复原成「发送」。textInput 内容不动。
  if (currentTurnId !== null) {
    ws.send(JSON.stringify({ type: Inbound.CANCEL, turn_id: currentTurnId }));
    setStatus("", "已请求停止…");
    return;
  }
  const text = textInput.value.trim();
  if (!text) return;
  appendBubble({ speaker: userDisplayName, text, kind: "user" });
  ws.send(JSON.stringify({ type: Inbound.USER_MESSAGE, text }));
  textInput.value = "";
});

// markdown 渲染出的 <a href> 默认在 renderer 进程里导航 —— 那会把整页换成外部 URL，
// 整个聊天 UI 就没了。这里一律 preventDefault 拦住；后续 P3.x 接 main 的
// shell.openExternal 桥再把外链甩给系统浏览器。
messagesEl.addEventListener("click", (ev) => {
  const a = ev.target.closest("a[href]");
  if (a) ev.preventDefault();
});

connect();
