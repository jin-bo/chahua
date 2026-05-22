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

import {
  EventType,
  Status,
  Inbound,
  ScoreKind,
  NoticeLevel,
  DEFAULT_PERMISSION,
  TASK_UNTITLED,
  BACKGROUND_MILESTONE_TYPES,
  buildOwnerOptionData,
  formatTaskLabel,
  formatTaskEventNotice,
  formatManagedSessionNotice,
  isTaskClosed,
} from "./events.js";
import {
  closeActionPopover,
  openActionPopover,
} from "./ui_popover.js";
import {
  makeAvatarImg,
  scoreText,
  makeBadge,
  makePermissionBadge,
} from "./chat_view.js";
import { createMention } from "./mention.js";
import { createUpload } from "./upload.js";
import { renderPersonaPicker, createPersonaImport } from "./persona.js";
import { createSettings } from "./settings.js";
import { createGuestSettings } from "./guest_settings.js";
import { createRoomSettings } from "./room_settings.js";
import { createPermissionPopover } from "./permission_popover.js";
import { createAssignPopover } from "./assign_popover.js";
import { createHandoffQueueBar } from "./handoff_queue_bar.js";
import { createManagedSession } from "./managed_session.js";
import { createDecisionSupport } from "./decision_support.js";
import * as taskState from "./task_state.js";
import * as handoffState from "./handoff_state.js";
import { createTaskPanel } from "./task_panel.js";
import { createDebugPanel } from "./debug_panel.js";
import { createSplitter } from "./splitter.js";
import { createProposalCard } from "./proposal_card.js";
import { createComposerTaskChip } from "./composer_task_chip.js";
import { createChatStream } from "./chat_stream.js";

const statusEl = document.getElementById("status");
const messagesEl = document.getElementById("messages");
const composer = document.getElementById("composer");
const textInput = document.getElementById("text");
// 必须按 type 选 —— composer 里第一个 <button> 是附件按钮（type="button"），
// `querySelector("button")` 会错抓到它，导致 updateSendButton 把"停止/发送"文字
// 写到附件按钮上，真正的发送按钮反而成了静态"发送"显示。
const submitBtn = composer.querySelector("button[type='submit']");
const roomNameEl = document.getElementById("room-name");
const roomTopicEl = document.getElementById("room-topic");
const guestsEl = document.getElementById("guests");
const roomsEl = document.getElementById("rooms");
const dropdownEl = document.getElementById("mention-dropdown");
const addGuestBtn = document.getElementById("add-guest");
const importPersonaBtn = document.getElementById("import-persona");
const assignHandoffBtn = document.getElementById("assign-handoff");
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
const avatarFileInput = document.getElementById("avatar-file-input");
const editUserModal = document.getElementById("edit-user-modal");
const userMdTextarea = document.getElementById("user-md-textarea");
const userMdSourceHintEl = document.getElementById("user-md-source-hint");
const userMdSubmitBtn = document.getElementById("user-md-submit");
const userAvatarWrapEl = document.getElementById("user-avatar-wrap");
const userNameEl = document.getElementById("user-name");
const editRoomTomlModal = document.getElementById("edit-room-toml-modal");
const roomTomlTextarea = document.getElementById("room-toml-textarea");
const roomTomlSourceHintEl = document.getElementById("room-toml-source-hint");
const roomTomlSubmitBtn = document.getElementById("room-toml-submit");
const attachFileBtn = document.getElementById("attach-file");
const fileInputEl = document.getElementById("file-input");
const pendingFilesEl = document.getElementById("pending-files");
const taskPanelEl = document.getElementById("task-panel");
const taskPanelToggleBtn = document.getElementById("task-panel-toggle");
const taskPanelBodyEl = document.getElementById("task-panel-body");
const taskPanelNewBtn = document.getElementById("task-panel-new");
const taskPanelShowDebugBtn = document.getElementById("task-panel-show-debug");
const debugPanelEl = document.getElementById("debug-panel");
const debugPanelBodyEl = document.getElementById("debug-panel-body");
const debugPanelBackBtn = document.getElementById("debug-panel-back");
const debugPanelClearBtn = document.getElementById("debug-panel-clear");
const composerTaskChipEl = document.getElementById("composer-task-chip");
const handoffQueueBarEl = document.getElementById("handoff-queue-bar");
const newTaskModal = document.getElementById("new-task-modal");
const newTaskTitleEl = document.getElementById("new-task-title");
const newTaskGoalEl = document.getElementById("new-task-goal");
const newTaskOwnerEl = document.getElementById("new-task-owner");
const newTaskSubmitBtn = document.getElementById("new-task-submit");
const markDecisionModal = document.getElementById("mark-decision-modal");
const markDecisionSummaryEl = document.getElementById("mark-decision-summary");
const markDecisionSupportEl = document.getElementById("mark-decision-support");
const markDecisionSubmitBtn = document.getElementById("mark-decision-submit");

const wsUrl = window.chahua?.wsUrl;
if (!wsUrl) {
  setStatus("error", "没有拿到 wsUrl —— preload / main 装配出了问题");
  throw new Error("missing wsUrl");
}

let ws = null;
let connected = false;

// 闭包读最新 ws —— 重连时 ws 变量会被重赋值，闭包每次调用读到当前绑定。所有
// feature module（upload / persona / settings）共享同一份 ref，留单点未来加
// readyState 守卫。
const send = (payload) => ws.send(JSON.stringify(payload));

// chat-stream（气泡 / 流式 / 历史回放）见 ./chat_stream.js；在途消息 Map 封装在
// 该模块内，外部只通过 ``closeInFlightOnDisconnect`` 一键清。
// 当前 turn 的 id —— P3.3 cancel 状态机：turn_start 时设、turn_end(next=user) 或
// status=cancelled 时清；turn_end(next=ai) 不清（下个 turn_start 会刷新）。非 null 即
// "AI 链在跑"，submit button 切到「停止」语义，submit handler 路由到 cancel 帧。
let currentTurnId = null;
// 当前 turn 的打分明细。turn_start replace / turn_end 清。
let scoresByName = new Map();
// renderSidebar 上次渲染的房间 id —— 用来区分"真正切房" vs "同房 room_info 刷新"
// （改头像 / 改设置 / 增删茶客都会重发 room_info）。handoff 队列预览只在真正切房时清。
let handoffRoomId = null;
// P9：当前前台房间在 envelope 里的 room_id（= room.name，见 chahua/events.py）。
// 从 room_info envelope 顶层 room_id 捕获。后台房间的里程碑事件 room_id 与此不同 ——
// 据此把它们分流给 handleBackgroundMilestone，不让它们污染前台聊天 / 任务面板。
let foregroundRoomId = null;
// P9：当前判定「进行中」的后台房间 envelope room_id 集合。后台里程碑事件 add、
// room_background_finished remove；renderRoomsList 据此渲染房间列表「进行中」徽标。
const backgroundActiveRooms = new Set();
// 茶客行 → 打分 span 的引用。sidebar 装好后填，turn_start/turn_end 直接改文字，
// 不重建 DOM（避免头像 / 徽章闪烁）。
const scoreSpansByName = new Map();
// 茶客名单（room_info 来时装）—— @ 补全候选 + 头像查找 + 用户显示名 / 头像。
let guests = []; // [{name, permission, isolation, avatar_data_uri}, ...]
let userDisplayName = "我";
let userAvatarDataUri = null;
// 可用 persona 候选（room_info 来时装）—— "添加茶客" / "新建房间"的 picker 用。
let personasAvailable = []; // [{persona, name, avatar_data_uri}, ...]
// 房间生效的 max_consecutive_ai_turns（room_info.orchestrator 来时装）—— 圆桌
// cap 预校验用；缺省 20 与 OrchestratorConfig 默认一致。
let maxAiTurns = 20;

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
  addGuestBtn.disabled = !enabled;
  importPersonaBtn.disabled = !enabled;
  assignHandoffBtn.disabled = !enabled;
  addRoomBtn.disabled = !enabled;
  attachFileBtn.disabled = !enabled;
  updateNewTaskBtn();
  // P8.3：「托管」按钮可点性 = connected + 有 active 任务 + 无 MTS（managedSession 自判）。
  managedSession?.refresh();
  // 双击 anchor 不走 disabled——dblclick handler 自己看 connected 决定弹不弹 popover。
  // 视觉上 .dblclick-anchor 的 hover 在 :not(.disconnected) 下亮，连接断时变灰。
  document.body.classList.toggle("disconnected", !enabled);
}

// "+ 新任务" 只受 connected 制约（多任务允许任意时刻新建）。
function updateNewTaskBtn() {
  taskPanelNewBtn.disabled = !connected;
  taskPanelNewBtn.title = "新建任务";
}

// 同一个按钮承担「发送 / 停止」双重职责；aria-label 同步切，屏读器读"发送"/"停止"而
// 不是裸符号。颜色由 .stop 类 + style.css 控制。
const SEND_ICON = "↑";
const STOP_ICON = "■";

// ``/help`` 输出文案 —— 用 markdown 渲，居中系统气泡承载。**Web 与 CLI 两面 help 各
// 自维护本面支持的命令**：CLI 端的 ``_HELP_LINES`` 含 ``/info`` / ``/quit``（REPL 才
// 有意义），Web 端含 ``/task``（CLI 走 ``@<名字>`` 路径不开任务）。加 / 改命令时
// 改本面的那份即可，不必试图 cross-runtime 同步。
const HELP_TEXT = [
  "**chahua 系统命令**",
  "",
  "- `/help` 或 `/?` —— 显示本帮助",
  "- `/task <标题>` —— 新建任务（已有 active 时服务端会拒）",
  "- `/clear` 或 `/new` —— 清空整间房间聊天（重置 transcript / 摘要 / 茶客会话窗口）",
  "- `/clear task` —— 清空当前任务的全部产物（仅删 `artifacts/`，任务本身保留）",
  "- `/tools <茶客名>` —— 查看某位茶客 agent 注册的工具",
  "- `/skills <茶客名>` —— 查看某位茶客可用的 skills",
  "",
  "_本帮助是本地提示，不进 transcript；刷新 / 切房后消失。_",
].join("\n");

// GUEST_CAPS_INFO envelope → system 气泡文案。view 决定列 tools 还是 skills 段
// （permission / guest 两段共用）。description 取首行 + 截断，避免长描述刷屏。
function formatGuestCaps(data, view) {
  const guest = data.guest || "?";
  const brief = (s) => (s || "").split("\n")[0].slice(0, 100);
  if (view === "skills") {
    const skills = Array.isArray(data.skills) ? data.skills : [];
    const lines = [`**${guest}** 可用 skills（${skills.length}）`];
    if (!skills.length) lines.push("_（无）_");
    for (const s of skills) {
      lines.push(`- \`${s.name}\`${s.description ? ` —— ${brief(s.description)}` : ""}`);
    }
    return lines.join("\n");
  }
  const tools = Array.isArray(data.tools) ? data.tools : [];
  const lines = [`**${guest}** 注册的 tools（${tools.length}，权限模式：${data.permission || "?"}）`];
  for (const t of tools) {
    const ro = t.is_read_only ? "read-only" : "write";
    lines.push(`- \`${t.name}\` (${ro})${t.description ? ` —— ${brief(t.description)}` : ""}`);
  }
  return lines.join("\n");
}
function updateSendButton() {
  const busy = currentTurnId !== null;
  submitBtn.textContent = busy ? STOP_ICON : SEND_ICON;
  submitBtn.setAttribute("aria-label", busy ? "停止" : "发送");
  submitBtn.title = busy ? "停止当前轮次" : "发送 (Enter)";
  submitBtn.classList.toggle("stop", busy);
}

function stickToBottom(mutate) {
  const stick = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 80;
  mutate();
  if (stick) messagesEl.scrollTop = messagesEl.scrollHeight;
}

// 头像 + 右上角 V 标的组合节点。默认 permission 直接返 img；显著 permission 用
// .avatar-wrap 包起来让 badge 浮右上角。
function makeAvatarWithPermission(g, avatarClassName, badgeClassName) {
  const img = makeAvatar(g.name, avatarClassName);
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

// 权限 popover（点 sidebar 头像）—— factory 实例化在 guestSettings 之后（依赖它的 open）。
// 见 ./permission_popover.js。
let permissionPopover = null;
function closePermissionPopover() {
  permissionPopover?.close();
}
function showPermissionPopover(anchor, g) {
  permissionPopover?.show(anchor, g);
}

// ── 指派下一句（delegate + 圆桌 panel 统一入口）─────────────────────
// composer 底栏「指派」按钮触发；勾 1 人 = delegate、勾 2~4 人 = panel。delegate 与
// panel 后端本是同一件事，前端入口也合一，勾选人数定语义。见 ./assign_popover.js。
let assignPopover = null;
// P8.3 托管会话模块——工厂实例化在文件后段（依赖 send / guests），先 let 占位让
// setInputEnabled / taskState.subscribe 等早于实例化的引用点安全（避免 const TDZ）。
let managedSession = null;
function closeAssignPopover() {
  assignPopover?.close();
}
function openAssignPopover() {
  if (!connected) return;
  closeActionPopover();
  assignPopover?.show(assignHandoffBtn);
}

// P7.2「请审…」—— 点消息气泡的请审按钮触发。复用 action popover 当茶客选择菜单：
// 每位茶客一项（含被审消息作者自己——自审合法、不屏蔽），选中即发 handoff_review。
// anchor 是请审按钮本身；message_id 由 chat_stream 经 attachReviewButton 透传上来。
function requestReview(messageId, anchorBtn) {
  if (!connected) return;
  if (guests.length === 0) {
    setStatus("error", "房间里没有茶客 —— 无法请审");
    return;
  }
  showActionPopover(anchorBtn, "请审这条 —— 交给", guests.map((g) => ({
    label: g.name,
    onClick: () => {
      // 菜单是异步的——用户从弹出菜单里选茶客时连接可能已断，落点再查一次。
      if (!connected) return;
      send({ type: Inbound.HANDOFF_REVIEW, target: g.name, message_id: messageId });
      setStatus("", `已请「${g.name}」审阅这条发言`);
    },
  })));
}

// ── 消息上的任务 chip + filter 视图（P5.2.10）─────────────────────────────
//
// chip 状态随 taskState 变 —— task_panel 没把消息 chip 文案当模块状态存（DOM 即真
// 源），refresh 是幂等的"看一眼 taskState、按需改 chip"。新挂的 li 直接调一次
// afterAppendMessage 而不是等 subscriber 全局 walk（walk N 条消息比单挂贵）。

let _filterTaskId = null;
let _filterBannerEl = null;

function getLiTaskId(li) {
  return li.dataset.taskId || null;
}

function refreshMessageTaskChip(li) {
  const taskId = getLiTaskId(li);
  let chip = li.querySelector(".message-task-chip");
  const removeChip = () => { if (chip) { chip.remove(); chip = null; } };
  if (!taskId) {
    removeChip();
    return;
  }
  const activeId = taskState.getState().activeTaskId;
  // active 任务的消息不挂 chip —— 默认 chat 主流就是 active，挂了反而吵。
  if (taskId === activeId) {
    removeChip();
    return;
  }
  // task 还没加载（room_history 先于 task_info）/ 被删了 —— 不挂；下次 subscriber 补。
  const task = taskState.getTaskById(taskId);
  if (!task) {
    removeChip();
    return;
  }
  const text = formatTaskLabel(task);
  if (chip) {
    if (chip.textContent !== text) chip.textContent = text;
    return;
  }
  chip = document.createElement("button");
  chip.type = "button";
  chip.className = "message-task-chip";
  chip.textContent = text;
  chip.title = `进入任务「${task.title || TASK_UNTITLED}」的 filter 视图`;
  chip.addEventListener("click", (ev) => {
    ev.stopPropagation();
    enterMessageFilter(taskId);
  });
  const bubble = li.querySelector(".bubble");
  if (bubble) bubble.appendChild(chip);
}

function refreshAllMessageTaskChips() {
  for (const li of messagesEl.querySelectorAll("li[data-task-id]")) {
    refreshMessageTaskChip(li);
  }
}

function applyMessageFilterTo(li) {
  if (_filterTaskId === null) {
    li.classList.remove("filtered-out");
    return;
  }
  li.classList.toggle("filtered-out", getLiTaskId(li) !== _filterTaskId);
}

function applyMessageFilterAll() {
  for (const li of messagesEl.querySelectorAll("li")) applyMessageFilterTo(li);
}

// 三处 append 调用站（appendBubble / startStreamingMessage / endStreamingMessage 的
// 无 start 分支）的尾部都做一样的事 —— 刷 chip + 应用 filter。单出口，将来加 third
// concern（搜索高亮 / 已读标记等）也只动这里。
function afterAppendMessage(li) {
  refreshMessageTaskChip(li);
  applyMessageFilterTo(li);
}

function enterMessageFilter(taskId) {
  _filterTaskId = taskId;
  ensureFilterBanner();
  updateFilterBannerText();
  _filterBannerEl.hidden = false;
  applyMessageFilterAll();
}

function exitMessageFilter() {
  if (_filterTaskId === null) return;
  _filterTaskId = null;
  if (_filterBannerEl) _filterBannerEl.hidden = true;
  applyMessageFilterAll();
}

function ensureFilterBanner() {
  if (_filterBannerEl) return;
  _filterBannerEl = document.createElement("div");
  _filterBannerEl.className = "message-filter-banner";
  _filterBannerEl.hidden = true;
  const text = document.createElement("span");
  text.className = "message-filter-banner-text";
  _filterBannerEl.appendChild(text);
  const exit = document.createElement("button");
  exit.type = "button";
  exit.className = "message-filter-banner-exit";
  exit.textContent = "返回全部";
  exit.addEventListener("click", exitMessageFilter);
  _filterBannerEl.appendChild(exit);
  // 插到 messagesEl 紧前 —— flex 容器 #main 里同级 sibling，挡不到聊天区域滚动。
  messagesEl.parentNode.insertBefore(_filterBannerEl, messagesEl);
}

function updateFilterBannerText() {
  if (!_filterBannerEl || _filterTaskId === null) return;
  const task = taskState.getTaskById(_filterTaskId);
  const title = task ? task.title || TASK_UNTITLED : _filterTaskId;
  _filterBannerEl.querySelector(".message-filter-banner-text").textContent =
    `仅显示任务「${title}」的消息 —— 其余隐起来`;
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

// chat-stream 工厂 —— 在所有依赖（messagesEl / makeAvatar / stickToBottom /
// afterAppendMessage）都已定义之后实例化。返回的 6 个方法是 envelope 分派与
// 连接生命周期的唯一对接面，老的 makeGuestRow / makeUserRow / inFlight Map 都封在
// 模块内不外泄。
const {
  appendBubble,
  startStreamingMessage,
  appendDelta,
  endStreamingMessage,
  clearInFlight,
  closeInFlightOnDisconnect,
  renderHistory,
} = createChatStream({
  messagesEl,
  makeAvatar,
  makeUserAvatar,
  stickToBottom,
  afterAppendMessage,
  onRequestReview: requestReview,
});

// ── 茶客列表渲染 ──────────────────────────────────────────────────
// 茶客行：头像 / 名字（权限 anchor）+ 打分小数字 + × 请离。指派 / 圆桌入口已挪到
// composer 底栏「指派」按钮（见 assign_popover.js），不再挂在茶客行上。
function renderGuestRowNormal(li, g, lastGuestLock) {
  // 头像 / 名字都是「设置权限」的 anchor —— popover 锚在头像更直观，但点名字也能开。
  const node = makeAvatarWithPermission(g, "avatar", "permission-badge");
  node.classList.add("permission-anchor");
  node.title = `点击设置「${g.name}」的权限（当前 ${g.permission || DEFAULT_PERMISSION}）`;
  node.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (!connected) return;
    showPermissionPopover(node, g);
  });
  li.appendChild(node);
  const nameBadge = makeBadge("guest-name", null, g.name);
  nameBadge.classList.add("permission-anchor");
  nameBadge.title = `点击设置「${g.name}」的权限（当前 ${g.permission || DEFAULT_PERMISSION}）`;
  nameBadge.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (!connected) return;
    showPermissionPopover(node, g);
  });
  li.appendChild(nameBadge);
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
  remove.className = "row-icon-btn row-remove";
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
}

function renderGuests() {
  guestsEl.replaceChildren();
  scoreSpansByName.clear();
  // 最后一位茶客不能删（与 server 端 admin.remove_guest 硬约束一致）—— 前端禁用按钮，
  // 用户少踩一次"提交后才发现不行"的坑。
  const lastGuestLock = guests.length <= 1;
  for (const g of guests) {
    const li = document.createElement("li");
    renderGuestRowNormal(li, g, lastGuestLock);
    guestsEl.appendChild(li);
  }
}

// ── sidebar 装配（room_info）────────────────────────────────────────

function renderSidebar(roomInfo) {
  // 进新房（首次连接 / 换房）的全量重置：清 in-flight 流 + 当前 turn 打分残留 + 消息
  // 容器 + 待发文件 pills（pill 的 rel 是按房间 share/ 计的相对路径，换房后挂别房不通）。
  clearInFlight();
  scoresByName = new Map();
  scoreSpansByName.clear();
  // 切房 / 重连 / 清空 → filter 视图无意义，强 exit 让新房间从全量视角起步。
  exitMessageFilter();
  messagesEl.replaceChildren();
  upload.clear();
  // propose 卡片去重指纹也按房间边界清 —— 否则跨房间同 proposer/kind/payload 会被错误折叠。
  proposalCard.reset();
  // 调试抽屉只看实时本会话；切房 / 重连 / 清空 → reset 清 turn 记录。
  debugPanel.reset();
  // handoff 队列是调度层瞬态（不落盘）。只在**真正切房**时清 —— renderSidebar 每条
  // room_info 都跑（含改头像 / 改设置 / 增删茶客等同房刷新），无差别 reset 会在服务端
  // 队列仍在时把预览抹掉，且服务端不会在 room_info 里重发 handoff_enqueued 补救
  // （队列不进房间快照）。同房刷新 → 队列原样保留。clear_room 走 reset_room 清服务端
  // 队列，由 clearCurrentRoom 单独 reset；switch_room 房间 id 变 → 这里自然清。
  const nextRoomId = roomInfo.current_room_id ?? null;
  if (nextRoomId !== handoffRoomId) {
    handoffState.reset();
    // P8.3：MTS 与 handoff 队列同瞬态——切房即清状态条。
    managedSession?.reset();
    handoffRoomId = nextRoomId;
  }
  // sidebar 全量重渲会替掉头像 DOM —— 旧 anchor 一旦被 detach，popover 的"贴右侧"
  // 位置就指向虚空了，干脆关掉。
  closePermissionPopover();
  closeAssignPopover();
  setStatus("ok", `已连接 ${wsUrl}`);
  roomNameEl.textContent = roomInfo.room_name || "—";
  roomTopicEl.textContent = roomInfo.topic || "";
  userDisplayName = roomInfo.user_display_name || "我";
  userAvatarDataUri = roomInfo.user_avatar_data_uri || null;
  settings.setSnapshot({
    userMdContent: roomInfo.user_md_content,
    userMdSource: roomInfo.user_md_source,
    roomTomlContent: roomInfo.room_toml_content,
    roomTomlSource: roomInfo.room_toml_source,
  });
  guestSettings.setSnapshot({
    roomDefaultLlmModel: roomInfo.room_default_llm?.model || null,
  });
  roomSettings.setSnapshot(roomInfo);
  renderUserRow();
  guests = Array.isArray(roomInfo.guests) ? roomInfo.guests : [];
  personasAvailable = Array.isArray(roomInfo.personas_available) ? roomInfo.personas_available : [];
  maxAiTurns = roomInfo.orchestrator?.max_consecutive_ai_turns ?? 20;
  renderGuests();
  // P9：从权威快照 rooms_available[].busy 重建「进行中」后台房集合 —— 切房 / 重连后
  // 这帧是真理源（剔除已结束的、补上切走后转后台的）。后台房集合按 envelope room_id
  // （= room.name）键，与 discover_rooms 的 r.name 同口径；当前房不计（自身活动由
  // 聊天区体现，不需徽标）。随后到的后台里程碑事件再增量维护这个集合。
  backgroundActiveRooms.clear();
  for (const r of roomInfo.rooms_available ?? []) {
    if (r.busy && r.room_id !== roomInfo.current_room_id) {
      backgroundActiveRooms.add(r.name);
    }
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
    // P9：后台房徽标按 envelope room_id（= room.name）匹配 —— 存 r.name 供
    // refreshRoomBadges 查（envelope 顶层 room_id 用 room.name，不是目录名）。
    li.dataset.roomName = r.name || r.room_id;
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
      remove.className = "row-icon-btn row-remove";
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
  refreshRoomBadges();
}

// P9：按 backgroundActiveRooms 集合刷新房间列表「进行中」徽标 —— 不重建整个列表
// （避免后台事件高频到达时房间列表闪烁）。徽标只挂非当前房；当前房自身活动由聊天区
// 体现。徽标元素增量挂 / 摘，不动其余 DOM。
function refreshRoomBadges() {
  for (const li of roomsEl.children) {
    const active =
      !li.classList.contains("current") &&
      backgroundActiveRooms.has(li.dataset.roomName);
    let badge = li.querySelector(".room-busy-badge");
    if (active && !badge) {
      badge = document.createElement("span");
      badge.className = "room-busy-badge";
      badge.textContent = "进行中";
      badge.title = "该房间正在后台续跑";
      li.querySelector(".room-name")?.appendChild(badge);
    } else if (!active && badge) {
      badge.remove();
    }
  }
}

// ── @ 补全 + composer 键盘 ────────────────────────────────────────────

const mention = createMention({
  inputEl: textInput,
  dropdownEl,
  getGuests: () => guests,
});

// textarea 自适应高度：内容增长时撑开，达到上限切滚动。style.height = "auto" 先把
// scrollHeight 算回单行视图，再读取实际内容高度赋值 —— 不重置 height 的话旧的高
// px 值会让 scrollHeight 永远 ≥ 旧值，textarea 只长不缩。max-height 与 #text 的
// CSS 上限对齐（200px），超出后 overflow:auto 接管。
const TEXTAREA_MAX_HEIGHT_PX = 200;
function autoResizeTextarea() {
  textInput.style.height = "auto";
  const desired = Math.min(textInput.scrollHeight, TEXTAREA_MAX_HEIGHT_PX);
  textInput.style.height = `${desired}px`;
  textInput.style.overflowY =
    textInput.scrollHeight > TEXTAREA_MAX_HEIGHT_PX ? "auto" : "hidden";
}

textInput.addEventListener("input", () => {
  autoResizeTextarea();
  mention.refresh();
});

textInput.addEventListener("keydown", (ev) => {
  // 中文 IME 候选窗 Enter（拼音 → 汉字）先发 keydown isComposing=true；让 IME 自己
  // 消费 Enter，待 compositionend 后再走 input 流程。
  if (ev.isComposing || ev.keyCode === 229) return;
  // dropdown 开启时 mention 模块独占键盘 —— Arrow / Enter-accept / Tab / Esc 全在
  // 模块里处理；返 true 即"已消化"，跳过下面的 composer Enter→submit 分支。
  if (mention.handleKeydown(ev)) return;
  // Enter 发送、Shift+Enter 换行（与 Slack / ChatGPT / Claude 一致）。requestSubmit
  // 走 form submit 事件管道复用现有 handler。
  if (
    ev.key === "Enter" &&
    !ev.shiftKey &&
    !ev.ctrlKey &&
    !ev.metaKey &&
    !ev.altKey
  ) {
    ev.preventDefault();
    composer.requestSubmit();
  }
});

textInput.addEventListener("blur", () => {
  // mousedown 已在 blur 之前完成补全；这里 100ms 后兜底关 dropdown。
  setTimeout(mention.hide, 100);
});

// ── envelope 分派 ────────────────────────────────────────────────

// P9：后台房间里程碑事件 —— 只维护「进行中」房间集合 + 刷新房间列表徽标。
// room_background_finished = 后台 runtime 跑完自毁 → 该房不再「进行中」；其余里程碑
// （turn_* / message_end / task_* / managed_session_*）→ 标记该房后台仍在跑。
function handleBackgroundMilestone(env) {
  const roomId = env.room_id;
  if (!roomId) return;
  if (env.type === EventType.ROOM_BACKGROUND_FINISHED) {
    backgroundActiveRooms.delete(roomId);
    console.debug("[room_background_finished]", roomId);
  } else {
    backgroundActiveRooms.add(roomId);
  }
  refreshRoomBadges();
}

function handleEnvelope(env) {
  // P9：后台房间里程碑分流 —— envelope.room_id 与前台不同、且属后台白名单类型
  // （turn_* / message_end / task_info / task_artifact_added / managed_session_* /
  // room_background_finished）→ 当作后台房间里程碑，只更新房间列表「进行中」徽标，
  // 不让它污染前台聊天 / 任务面板 / 调试抽屉。控制面事件（room_info / room_history
  // / notice 等）不在白名单内 —— 切房时新房 room_info 的 room_id 虽 ≠ 旧前台，仍
  // 照常落到下面 switch 并由 ROOM_INFO 分支刷新 foregroundRoomId。
  if (
    foregroundRoomId !== null &&
    env.room_id &&
    env.room_id !== foregroundRoomId &&
    BACKGROUND_MILESTONE_TYPES.has(env.type)
  ) {
    handleBackgroundMilestone(env);
    return;
  }
  // 调试抽屉旁路消费 —— 模块内部按 env.type dispatch；ROOM_INFO 的 reset 在
  // renderSidebar 里调（与 inFlight / proposalCard 同步清）。
  debugPanel.onEnvelope(env);
  switch (env.type) {
    case EventType.ROOM_INFO:
      // 前台房间在 envelope 里的 room_id（= room.name）—— 后台分流的判定基准。
      foregroundRoomId = env.room_id ?? null;
      renderSidebar(env.data ?? {});
      return;
    case EventType.ROOM_HISTORY:
      renderHistory(env.data?.messages ?? []);
      // P6.3.A：``turns_index`` 倒序投影注入调试抽屉的历史索引（``debug.enabled=False``
      // 时整字段缺省 → applyTurnsIndex 不入 array，索引区维持空）。
      debugPanel.applyTurnsIndex(env.data?.turns_index ?? null);
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
        // AI 链收尾、无 in-flight turn —— 放开被 gate 的 handoff 采纳按钮（此刻采纳
        // 不会抢占截断任何 turn）。next==='ai' 时链未结束，不放。
        proposalCard.onTurnIdle();
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
    case EventType.FILE_UPLOADED:
      upload.onServerEcho(env.data ?? {});
      return;
    // 任务房间（docs/P5-任务房间.md §4.2）。TASK_INFO 是权威快照 → 全量覆盖 taskState
    // 让面板重渲；其它 4 个是 hint → 在对应 DOM 上闪一下，权威 state 等紧跟而至的
    // TASK_INFO 推进。
    case EventType.TASK_INFO:
      taskState.setSnapshot(env.data ?? {});
      return;
    // P5.8：task hint 既闪面板增量项、又在聊天区追一条居中系统气泡（task event
    // 不再合成 user 消息进 transcript）。气泡是瞬时 UI 通知，刷新 / 切房即清。
    case EventType.TASK_OPEN:
    case EventType.TASK_DECISION_ADDED:
    case EventType.TASK_ARTIFACT_ADDED:
    case EventType.TASK_CLOSE: {
      taskPanel.flashHint(env.type, env.data ?? {});
      const text = formatTaskEventNotice(env.type, env.data ?? {});
      if (text) appendBubble({ kind: "system", text });
      return;
    }
    case EventType.TASK_ARTIFACTS_CLEARED: {
      // 不调 flashHint —— 清空动作没有可高亮的"新增项"，状态推进全靠紧随的
      // TASK_INFO；本事件唯一价值就是系统气泡。
      const text = formatTaskEventNotice(env.type, env.data ?? {});
      if (text) appendBubble({ kind: "system", text });
      return;
    }
    // TASK_UPDATE 不显示气泡 —— 避免用户编辑标题时聊天流被刷。
    case EventType.TASK_UPDATE:
      taskPanel.flashHint(env.type, env.data ?? {});
      return;
    case EventType.TASK_PROPOSAL:
      proposalCard.onEnvelope(env);
      return;
    // P7.1 显式 handoff（docs/P7 §3.3）。三个事件维护本地队列预览；权威状态就是
    // 队列本身——服务端不落盘，刷新 / 切房在 renderSidebar 里 reset。
    case EventType.HANDOFF_ENQUEUED:
      handoffState.applyEnqueued(env.data?.queue ?? []);
      return;
    case EventType.HANDOFF_CONSUMED:
      handoffState.applyConsumed(env.data?.item ?? null);
      return;
    case EventType.HANDOFF_CLEARED:
      handoffState.applyCleared();
      return;
    // P8.3 托管会话（docs/P8.3 §3.4）。三个 hint 事件维护状态条；ended 追系统气泡。
    case EventType.MANAGED_SESSION_STARTED:
      managedSession.onStarted(env.data ?? {});
      return;
    case EventType.MANAGED_SESSION_ADVANCED:
      managedSession.onAdvanced(env.data ?? {});
      return;
    case EventType.MANAGED_SESSION_ENDED: {
      const reason = env.data?.reason;
      managedSession.onEnded(env.data ?? {});
      appendBubble({ kind: "system", text: formatManagedSessionNotice(reason) });
      return;
    }
    case EventType.GUEST_CAPS_INFO: {
      // view 由请求方经 envelope 回声带回 —— 多条 /tools / /skills 并发时各响应
      // 自带"查的是哪段"，不靠全局态对号入座。
      const data = env.data ?? {};
      appendBubble({ kind: "system", text: formatGuestCaps(data, data.view) });
      return;
    }
    case EventType.ROOM_EXPORT: {
      const markdown = env.data?.markdown;
      const filename = env.data?.filename || "chahua-export.md";
      if (typeof markdown !== "string") return;
      // Blob 走 markdown 文本 mime；<a download> 触发浏览器原生下载流程。CSP
      // 已允许 'self'，blob: URL 在 Chromium 里也通过 default-src 'self'。
      const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setStatus("ok", `已导出 ${filename}`);
      return;
    }
    case EventType.FILE_DOWNLOAD: {
      // 失败路径：NOTICE 已弹过 alert，envelope 仅含 {rel, error}，跳过 Blob 流程。
      const d = env.data || {};
      if (d.error) return;
      const b64 = d.content_b64;
      const name = d.name || "download";
      if (typeof b64 !== "string") return;
      // base64 → Uint8Array → Blob。atob 一次性解码 ~200MB 在 Chromium 还稳；超大 base64
      // 字符串在 ws 入站早就被 _DOWNLOAD_MAX_BYTES 拦了。
      let bin;
      try {
        bin = atob(b64);
      } catch (e) {
        setStatus("error", `下载「${name}」失败：base64 解码出错`);
        return;
      }
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
      // 走 application/octet-stream 让浏览器把它当下载而非 inline preview。具体 mime 类型
      // 由扩展名决定，留给操作系统打开时自己识别。
      const blob = new Blob([bytes], { type: "application/octet-stream" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setStatus("ok", `已下载「${name}」`);
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
    // handoff 队列预览是 per-connection 瞬态：断线期间漏收的 handoff_consumed /
    // handoff_cleared 不会补发，服务端也可能已重启换了空队列 —— 留着旧预览会误导，
    // 且点 ✕ 会发 handoff_clear 误取消正在跑的 turn。断线即清，与"刷新即清"同口径；
    // 重连后 renderSidebar 房间 id 不变不会再清，靠这里兜底。
    handoffState.reset();
    // P8.3：MTS 状态条同断线即清（与 handoff 预览同口径）。
    managedSession?.reset();
    // 拒绝所有等 echo 的上传，让 change handler 的 finally 清 isUploading + 启用附件按钮。
    upload.dropPending(`ws closed (${ev.code})`);
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

addGuestBtn.addEventListener("click", () => {
  if (!connected) return;
  const inRoom = new Set(guests.map((g) => g.name));
  renderPersonaPicker(addGuestListEl, {
    personas: personasAvailable,
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

// composer 底栏「指派」按钮 —— 弹统一的指派 / 圆桌 popover。
assignHandoffBtn.addEventListener("click", openAssignPopover);

addRoomBtn.addEventListener("click", () => {
  if (!connected) return;
  newRoomNameEl.value = "";
  newRoomTopicEl.value = "";
  newRoomRulesEl.value = "";
  renderPersonaPicker(newRoomGuestsEl, {
    personas: personasAvailable,
    multi: true,
    excludeNames: null,
  });
  openModal(addRoomModal);
  newRoomNameEl.focus();
});

createPersonaImport({
  modal: importPersonaModal,
  folderPathEl: importFolderPathEl,
  folderPickBtn: importFolderPickBtn,
  githubUrlEl: importGithubUrlEl,
  submitBtn: importPersonaSubmitBtn,
  importBtn: importPersonaBtn,
  isConnected: () => connected,
  send,
  setStatus,
  pickFolder: window.chahua?.pickFolder,
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
  userAvatarWrapEl.replaceChildren(makeUserAvatar("user-avatar"));
}

const settings = createSettings({
  userMd: {
    modal: editUserModal,
    textarea: userMdTextarea,
    hintEl: userMdSourceHintEl,
    submitBtn: userMdSubmitBtn,
  },
  roomToml: {
    modal: editRoomTomlModal,
    textarea: roomTomlTextarea,
    hintEl: roomTomlSourceHintEl,
    submitBtn: roomTomlSubmitBtn,
  },
  avatarFileInput,
  isConnected: () => connected,
  send,
  setStatus,
});

const guestSettings = createGuestSettings({
  isConnected: () => connected,
  send,
  setStatus,
});

const roomSettings = createRoomSettings({
  isConnected: () => connected,
  send,
  setStatus,
  openGuestSettings: (g) => guestSettings.open(g),
  openRawRoomToml: () => settings.openEditRoomToml(),
});

permissionPopover = createPermissionPopover({
  send,
  setStatus,
  isConnected: () => connected,
  openGuestSettings: (g) => guestSettings.open(g),
});

assignPopover = createAssignPopover({
  send,
  setStatus,
  isConnected: () => connected,
  getGuests: () => guests.map((g) => g.name),
  getMaxAiTurns: () => maxAiTurns,
});

// composer 上方的 handoff 队列预览小条 —— 订阅 handoff_state，队列变即重渲。
// renderSidebar 的 handoffState.reset() 会触发一次空队列 render 把小条收起。
const handoffQueueBar = createHandoffQueueBar({
  barEl: handoffQueueBarEl,
  send,
  isConnected: () => connected,
});
handoffState.subscribe((queue) => handoffQueueBar.render(queue));

// P8.3 托管会话：任务卡里的「托管」按钮（入口 + 状态 + 停止三合一）。状态来自
// managed_session_* envelope；与 handoff 队列同瞬态——切房 / 断线即 reset。
managedSession = createManagedSession({
  send,
  isConnected: () => connected,
  setStatus,
  getGuests: () => guests.map((g) => g.name),
  getActiveTask: () => taskState.getActiveTask(),
});

// ── 上传文件到房间共享目录 ──────────────────────────────────────────
// pending pills 仅在前端内存里；切房 / submit 时清空。

const upload = createUpload({
  pendingFilesEl,
  fileInputEl,
  attachFileBtn,
  isConnected: () => connected,
  send,
  setStatus,
  onAttachToTask: (rel) => attachArtifact(rel),
});

const decisionSupport = createDecisionSupport({
  modal: markDecisionModal,
  summaryEl: markDecisionSummaryEl,
  supportEl: markDecisionSupportEl,
  submitBtn: markDecisionSubmitBtn,
  send,
  setStatus,
  openModal,
  closeModal,
  isConnected: () => connected,
});

function attachArtifact(rel) {
  const active = taskState.getActiveTask();
  if (!active) {
    // body.has-active-task gate 已隐了按钮，正常进不到这；保留兜底防 dev 加新触发点。
    setStatus("error", "当前没有任务 —— 先新建任务才能拷贝产物");
    return;
  }
  send({
    type: Inbound.ATTACH_ARTIFACT,
    task_id: active.id,
    share_rel: rel,
  });
  setStatus("", `拷贝「${rel}」到任务「${active.title || TASK_UNTITLED}」…`);
}

// composer 底栏的任务 chip + 切换任务 dropdown（P5.2.10 / P5.2.8）。在 taskPanel 之前
// 构造 —— onSetActive 复用本模块的 sendSetActiveTask 让"任务卡里切 active"和"chip
// dropdown 里切 active"走完全同一行 wire 帧。
const composerTaskChip = createComposerTaskChip({
  chipEl: composerTaskChipEl,
  isConnected: () => connected,
  send,
  setStatus,
  taskState,
  closeOtherPopovers: () => {
    closeActionPopover();
    closePermissionPopover();
    closeAssignPopover();
  },
});

// 任务面板（订阅 task_state；TASK_INFO 到达 → 全量重渲）。模块级单例 —— 重连不重建，
// 重连后服务端 _emit_room_snapshot 会重发 task_info 让面板状态前进。
const taskPanel = createTaskPanel({
  panelEl: taskPanelEl,
  toggleBtnEl: taskPanelToggleBtn,
  bodyEl: taskPanelBodyEl,
  // 任务卡所有写都走这里 —— title / goal / owner / status 都打成 patch；按 status 是否
  // 终结态在本层 smart-route 到 close_task vs update_task wire frame。task_panel.js 对
  // wire 协议零感知。
  onPatchTask: (taskId, patch) => {
    if (!connected) return;
    if (patch.status && isTaskClosed(patch.status)) {
      send({ type: Inbound.CLOSE_TASK, task_id: taskId, status: patch.status });
      setStatus("", patch.status === "done" ? "标记完成…" : "放弃任务…");
      return;
    }
    send({ type: Inbound.UPDATE_TASK, task_id: taskId, patch });
  },
  // 点"其它任务"折叠区的卡片 / composer chip dropdown 都走同一入口。server 端
  // _inbound_set_active_task 走前 cancel inflight turn，前端不必再判 connected 之外
  // 的状态。
  onSetActive: composerTaskChip.sendSetActiveTask,
  // 每次重渲都 fresh 拉 —— 茶客加 / 删后 owner 下拉同步刷新（room_info 回环会触发
  // taskState 重渲，guests 已先一步在 renderSidebar 里更新）。
  getGuestNames: () => guests.map((g) => g.name),
  // 产物 li 点击：发 download_file inbound，等 FILE_DOWNLOAD envelope 回吐后 Blob 触发
  // 浏览器原生下载。断网时早返避免 send 抛。
  onDownloadArtifact: (rel) => {
    if (!connected) return;
    send({ type: Inbound.DOWNLOAD_FILE, rel });
    setStatus("", `下载「${rel}」…`);
  },
  // P8.3：active 任务卡里的「托管运行」按钮槽位交给 managed_session 模块渲染 ——
  // 由它按 MTS 状态决定渲「托管运行」还是「托管中」。
  onMountManage: (slotEl) => managedSession.mountManageButton(slotEl),
});

// 茶客 propose 卡片渲染 + 去重 + 采纳/忽略闭环。renderSidebar 进新房时调 reset() 清
// 跨房间 dedup；采纳按钮按 kind 拼 ADD_DECISION / OPEN_TASK inbound 走既有 server handler。
const proposalCard = createProposalCard({
  messagesEl,
  sendInbound: send,
  isTurnActive: () => currentTurnId !== null,
});

// 调试抽屉。默认隐藏 —— 用户点 task panel header 的 🔬 切到；调试内 ← 切回。
// 模块级单例：重连时不重建（状态本就只活在本会话，renderSidebar 调 reset 清。）
const debugPanel = createDebugPanel({
  panelEl: debugPanelEl,
  bodyEl: debugPanelBodyEl,
  clearBtnEl: debugPanelClearBtn,
  // P6.3.A 点击历史索引行 → FETCH_TURN_DETAIL inbound；服务端回 TURN_DETAIL 经
  // debugPanel.onEnvelope 处理。
  sendInbound: send,
});

// task ↔ debug 互斥占槽：``setVisible(bool)`` 让各自模块持有自家 hidden 不变量，
// renderer 只负责"一开一关"配对。
taskPanelShowDebugBtn.addEventListener("click", () => {
  taskPanel.setVisible(false);
  debugPanel.show();
});
debugPanelBackBtn.addEventListener("click", () => {
  debugPanel.hide();
  taskPanel.setVisible(true);
});

// 三栏拖拽。左 splitter 改 sidebar 宽度；右 splitter 改 task / debug 抽屉共享宽度。
// localStorage 持久化跨重启 + 跨房间（与 task_panel 折叠 pref 同口径）。
createSplitter(
  document.getElementById("splitter-left"),
  "--sidebar-width",
  { min: 140, max: 480, edge: "left", fallback: 200 },
);
createSplitter(
  document.getElementById("splitter-right"),
  "--right-panel-width",
  { min: 200, max: 640, edge: "right", fallback: 300 },
);

// taskPanel 已通过 subscribe 重渲 panel body；这里多挂一条监听 composer chip + 按钮
// 这俩 panel 之外的派生 UI。按钮的 hasAny 判断走 task 存在性而非 active —— state.json
// 丢失时仍 disable，防止悄悄允许开第二个（docs §7.1 实现项第 4 条）。
//
// 任务房间消息 chip + filter banner 文案也跟着 taskState 走 —— 任务改名 / 切 active /
// 关任务都靠这条 subscriber 顺着重刷。filter banner 自身的可见性由 enter / exit 控制；
// 这里只刷文案。
taskState.subscribe(() => {
  refreshAllMessageTaskChips();
  if (_filterTaskId !== null) updateFilterBannerText();
});

taskState.subscribe((state) => {
  const active = taskState.getActiveTask();
  const hasAny = state.tasks.length > 0;
  // 0 任务 → 整个 chip 隐起来（与 P5.1 同口径，避免点开"房间级"下拉发现啥可切的都没）
  composerTaskChipEl.hidden = !hasAny;
  if (hasAny) {
    composerTaskChip.render(active);
  }
  // body class 给 CSS 用 —— 控制 share/ pill 上 "拷贝到当前任务" 按钮是否可见。
  document.body.classList.toggle("has-active-task", !!active);
  updateNewTaskBtn();
  // P8.3：active 任务变化 → 刷新「托管」按钮可点性。
  managedSession?.refresh();
});

taskPanelNewBtn.addEventListener("click", () => {
  if (!connected || taskPanelNewBtn.disabled) return;
  newTaskTitleEl.value = "";
  newTaskGoalEl.value = "";
  // owner 选项与任务卡 owner 下拉同源 —— "" 表示全员，submit 时再 normalize 回 null。
  newTaskOwnerEl.replaceChildren();
  for (const data of buildOwnerOptionData(guests.map((g) => g.name))) {
    const opt = document.createElement("option");
    opt.value = data.value;
    opt.textContent = data.label;
    newTaskOwnerEl.appendChild(opt);
  }
  newTaskOwnerEl.value = "";
  openModal(newTaskModal);
  newTaskTitleEl.focus();
});

newTaskSubmitBtn.addEventListener("click", () => {
  if (!connected) return;
  const title = newTaskTitleEl.value.trim();
  if (!title) {
    newTaskTitleEl.focus();
    return;
  }
  const ownerRaw = newTaskOwnerEl.value;
  const payload = {
    type: Inbound.OPEN_TASK,
    title,
    goal: newTaskGoalEl.value,
    owner: ownerRaw === "" ? null : ownerRaw,
  };
  send(payload);
  setStatus("", `新建任务「${title}」…`);
  closeModal(newTaskModal);
});

// 决策入口：右键消息气泡 → action popover → modal。message_id 仅在 server 落盘后
// 才挂到 li 上（用户自己 echo 的那条没有，直到下次 room_history 才补），所以未带
// id 的 li 一律跳过 —— 决策必须能引回 transcript 里的真实 message。
// 标为决策 modal —— 见 ./decision_support.js（factory 实例化在 createUpload 之后）。
messagesEl.addEventListener("contextmenu", (ev) => {
  if (!connected) return;
  const li = ev.target.closest("li[data-message-id]");
  if (!li) return;
  ev.preventDefault();
  const active = taskState.getActiveTask();
  if (!active) {
    setStatus("error", "当前没有任务 —— 先新建任务才能记决策");
    return;
  }
  openActionPopover(li, "消息操作", [
    {
      label: "📌 标为决策",
      desc: `把这条加进任务「${active.title || TASK_UNTITLED}」`,
      onClick: () => decisionSupport.open(li, active.id),
    },
  ]);
});

// modal 关闭：点 backdrop（modal-backdrop 自身、不是内部 .modal）/ × 按钮 / ESC。
const editGuestModal = document.getElementById("edit-guest-modal");
const editRoomModal = document.getElementById("edit-room-modal");
const ALL_MODALS = [
  addGuestModal, addRoomModal, editUserModal, importPersonaModal,
  editRoomTomlModal, editGuestModal, editRoomModal, newTaskModal,
  markDecisionModal,
];
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
function clearCurrentRoom() {
  if (!connected) return;
  const roomName = roomNameEl.textContent;
  if (!window.confirm(`确定清空「${roomName}」的全部聊天记录？\n茶客在场，但本房间的 transcript / 摘要 / 游标会被重置。`)) return;
  ws.send(JSON.stringify({ type: Inbound.CLEAR_ROOM }));
  // 服务端 reset_room 会清 handoff 队列，但回环的 room_info 房间 id 不变 ——
  // renderSidebar 的切房判定不会触发，这里显式 reset 让预览跟上。
  handoffState.reset();
  // P8.3：reset_room 也清 MTS（orchestrator.reset_room），状态条同步收起。
  managedSession?.reset();
  setStatus("", `清空「${roomName}」…`);
}

// 跨 popover 互斥：开 action 前先关 permission / 指派 popover。
function showActionPopover(anchor, title, items) {
  closePermissionPopover();
  closeAssignPopover();
  openActionPopover(anchor, title, items);
}

function showUserActionsPopover(anchor) {
  if (!connected) return;
  showActionPopover(anchor, "我（设置）", [
    { label: "编辑配置", desc: "改 USER.md（显示名 / 个人偏好）", onClick: settings.openEditUserMd },
    { label: "换头像", desc: "PNG / JPG / WebP / GIF，自动裁方 + 压到 256px PNG", onClick: settings.pickAvatarFile },
  ]);
}

function showRoomActionsPopover(anchor) {
  if (!connected) return;
  const roomName = roomNameEl.textContent || "本房间";
  showActionPopover(anchor, `房间「${roomName}」`, [
    { label: "详细设置", desc: "编排参数 / 打分 / 摘要模型 / 茶客一览", onClick: () => roomSettings.open() },
    { label: "更改房间配置", desc: "直接编辑 room.toml（name / topic / rules / [[guest]]）", onClick: settings.openEditRoomToml },
    { label: "导出 markdown", desc: "把本房间历史保存为 .md 文件", onClick: exportCurrentRoom },
    { label: "清空聊天", desc: "重置 transcript / 摘要 / 游标，茶客在场", danger: true, onClick: clearCurrentRoom },
  ]);
}

function exportCurrentRoom() {
  if (!connected) return;
  send({ type: Inbound.EXPORT_ROOM });
  setStatus("", "导出中…");
}

// preventDefault 压住浏览器默认的 dblclick 选中文本行为，让 popover 弹出时 anchor
// 不会被高亮选中。outside-close 的兜底来自 attachPopoverDismissHandlers 的 setTimeout
// 延后挂载——dblclick 本身的 mousedown 已经在监听器装上之前结束，不会触发自关。
userAvatarWrapEl.addEventListener("dblclick", (ev) => {
  ev.preventDefault();
  showUserActionsPopover(userAvatarWrapEl);
});
userNameEl.addEventListener("dblclick", (ev) => {
  ev.preventDefault();
  showUserActionsPopover(userAvatarWrapEl);
});
roomNameEl.addEventListener("dblclick", (ev) => {
  ev.preventDefault();
  showRoomActionsPopover(roomNameEl);
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
  if (upload.isUploading()) {
    // 还有文件在上传中 —— snapshotRels 此刻只含已收到 echo 的；强发会丢已选的 attach。
    setStatus("", "文件还在上传，等所有 pill 出现后再发。");
    return;
  }
  const text = textInput.value.trim();
  // /help（或 /?）—— 在聊天区里以系统气泡显示可用斜杠命令清单。本地 UI 行为，不
  // 走 server 帧、不进 transcript；刷新 / 切房 / clear 都会消失。
  if (text === "/help" || text === "/?") {
    appendBubble({ kind: "system", text: HELP_TEXT });
    textInput.value = "";
    autoResizeTextarea();
    return;
  }
  // /tools <茶客名> / /skills <茶客名> —— 查某位茶客 agent 的 tools / 可用 skills。
  // 两者走同一只读 inbound list_guest_caps；view 随帧发出、回包原样带回，前端按它
  // 裁剪显示哪段（不靠全局态，多查询并发不串台）。本地查询，不进 transcript。
  {
    const isTools = text === "/tools" || text.startsWith("/tools ");
    const isSkills = text === "/skills" || text.startsWith("/skills ");
    if (isTools || isSkills) {
      const cmd = isTools ? "/tools" : "/skills";
      const guest = text.slice(cmd.length).trim();
      if (!guest) {
        setStatus("error", `${cmd} 后面要跟茶客名`);
        return;
      }
      if (!connected) {
        setStatus("error", "未连接服务端");
        return;
      }
      send({
        type: Inbound.LIST_GUEST_CAPS,
        guest,
        view: isTools ? "tools" : "skills",
      });
      textInput.value = "";
      autoResizeTextarea();
      return;
    }
  }
  // 斜杠命令 —— /task <title> 走 open_task inbound 而非 user_message。已有任务等
  // server 端拒绝（NOTICE error），前端不重复判定，让两条路径决断口径完全同源。
  if (text.startsWith("/task ") || text === "/task") {
    const title = text.slice("/task".length).trim();
    if (!title) {
      setStatus("error", "/task 后面要跟任务标题");
      return;
    }
    if (upload.hasPending()) {
      setStatus("error", "新建任务前先 × 掉待发文件 —— 任务不接附件");
      return;
    }
    send({ type: Inbound.OPEN_TASK, title, goal: "", owner: null });
    setStatus("", `新建任务「${title}」…`);
    textInput.value = "";
    autoResizeTextarea();
    return;
  }
  // /clear task：清空当前 active 任务的全部产物（仅删 artifacts/，任务本身保留）。
  // 必须排在 /clear / /new 之前 —— 否则 startsWith 边界让 "/clear task" 被前缀吞掉。
  if (text === "/clear task" || text === "/clear-task") {
    const active = taskState.getActiveTask();
    if (!active) {
      setStatus("error", "/clear task 需要先选中一个任务");
      return;
    }
    const count = Array.isArray(active.artifacts) ? active.artifacts.length : 0;
    if (count === 0) {
      setStatus("", `任务「${active.title}」没有产物`);
      textInput.value = "";
      autoResizeTextarea();
      return;
    }
    if (!window.confirm(
      `确定清空任务「${active.title}」的全部产物（${count} 个文件）？\n任务本身（决策 / 状态 / 摘要）保留，仅删 artifacts/ 下的文件。`,
    )) return;
    send({ type: Inbound.CLEAR_TASK_ARTIFACTS, task_id: active.id });
    setStatus("", `清空任务「${active.title}」的产物…`);
    textInput.value = "";
    autoResizeTextarea();
    return;
  }
  // /clear 与 /new：键盘快捷的"清空聊天"，与房间菜单同入口共用 confirm —— 滑指误打
  // /task 边界（如想 /task 打成 /clear）有 confirm 兜底；clearCurrentRoom 内部不连接
  // 时 noop，所以此处不再判 connected。
  if (text === "/clear" || text === "/new") {
    textInput.value = "";
    autoResizeTextarea();
    clearCurrentRoom();
    return;
  }
  // 文件不空时即使 text 为空也允许发送 —— 用户拖了文件就是有意图。
  if (!text && !upload.hasPending()) return;
  const files = upload.snapshotRels();
  // echo 显示：文本 + 文件引用（与 server 端 _attach_files_to_text 同口径），
  // 让用户在自己气泡里就能看到"我刚发了什么"。
  const echoLines = [text];
  for (const f of files) echoLines.push(`<./${f}>`);
  const echo = echoLines.filter(Boolean).join("\n");
  // task_id snapshot at submit —— server 端在 inbound 接帧时按当前 active 打 tag，与
  // 这里 active 通常一致（单客户端串行 inbound + 用户改 active 也走 inbound 排队）。
  appendBubble({
    speaker: userDisplayName,
    text: echo,
    kind: "user",
    taskId: taskState.getActiveTask()?.id ?? null,
  });
  ws.send(JSON.stringify({
    type: Inbound.USER_MESSAGE,
    text,
    ...(files.length > 0 ? { files } : {}),
  }));
  textInput.value = "";
  upload.clear();
  autoResizeTextarea();
});

// markdown 渲染出的 <a href> 默认在 renderer 进程里导航 —— 那会把整页换成外部 URL，
// 整个聊天 UI 就没了。这里一律 preventDefault 拦住；后续 P3.x 接 main 的
// shell.openExternal 桥再把外链甩给系统浏览器。
messagesEl.addEventListener("click", (ev) => {
  const a = ev.target.closest("a[href]");
  if (a) ev.preventDefault();
});

// 浏览器对 rows="1" 的默认高度算法有 1~2 px 差异 —— 一次 resize 把 textarea
// 锁到 CSS min-height (36px)，与发送 / 附件按钮单行视图三者对齐。
autoResizeTextarea();
connect();
