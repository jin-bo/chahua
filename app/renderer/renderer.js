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
  Inbound,
  TASK_UNTITLED,
  isTaskClosed,
} from "./events.js";
import {
  closeActionPopover,
  openActionPopover,
} from "./ui_popover.js";
import { createSidebar } from "./sidebar.js";
import { createMention } from "./mention.js";
import { createUpload } from "./upload.js";
import { wireModals } from "./modals.js";
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
import { createMessageFilter } from "./message_filter.js";
import { createCommands } from "./commands.js";
import { createConnection } from "./connection.js";
import { createRoomList } from "./room_list.js";
import { createTurnState } from "./turn_state.js";
import { createEnvelopeRouter } from "./envelope_router.js";

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
// 添加茶客 / 新建房间 / 新建任务 / 导入 persona 四个 modal 的 DOM 节点在 ./modals.js
// 内自取（仅那里用）。
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
const markDecisionModal = document.getElementById("mark-decision-modal");
const markDecisionSummaryEl = document.getElementById("mark-decision-summary");
const markDecisionSupportEl = document.getElementById("mark-decision-support");
const markDecisionSubmitBtn = document.getElementById("mark-decision-submit");

const wsUrl = window.chahua?.wsUrl;
if (!wsUrl) {
  setStatus("error", "没有拿到 wsUrl —— preload / main 装配出了问题");
  throw new Error("missing wsUrl");
}

// WebSocket 连接 + 重连退避封在 ./connection.js —— renderer 经 send / isConnected
// 读写连接、经 onConnecting / onMessage / onClose 回调接生命周期。此处提前装配
// （feature module 工厂在下文要拿 send，必须早于它们就绪）；onMessage 引用的
// envelopeRouter 在下文才装配，经 arrow 推迟解析。
const connection = createConnection({
  wsUrl,
  setStatus,
  onConnecting: () => setInputEnabled(false),
  // envelopeRouter 在下文 feature 工厂之后才装配 —— arrow 推迟到调用时解析（ws
  // message 在 connection.start() 之后才到，那时 envelopeRouter 已就绪）。
  onMessage: (env) => envelopeRouter.handleEnvelope(env),
  onClose: handleDisconnect,
});
const send = connection.send;

// 侧栏房间列表 + P9「进行中」后台房徽标 —— 见 ./room_list.js。后台房集合封在模块内，
// renderer 经 render（room_info 重建）/ onBackgroundMilestone（后台里程碑）两出口交互。
const roomList = createRoomList({
  roomsEl,
  isConnected: connection.isConnected,
  send,
  setStatus,
});

// 当前 turn 状态 + 发送/停止按钮封在 ./turn_state.js —— renderer 经 isActive /
// turnId / set / clear / syncToRoom 交互。
const turnState = createTurnState({ submitBtn });

// chat-stream（气泡 / 流式 / 历史回放）见 ./chat_stream.js；在途消息 Map 封装在
// 该模块内，外部只通过 ``closeInFlightOnDisconnect`` 一键清。
// renderSidebar 上次渲染的房间 id —— 用来区分"真正切房" vs "同房 room_info 刷新"
// （改头像 / 改设置 / 增删茶客都会重发 room_info）。handoff 队列预览只在真正切房时清。
let handoffRoomId = null;
// 茶客名单（room_info 来时装）—— @ 补全候选 + 一众模块（mention / assign / 任务卡
// owner …）读取。头像 / 打分 / 用户行渲染在 ./sidebar.js，经 getGuests 取这份名单。
let guests = []; // [{name, permission, isolation, avatar_data_uri}, ...]
// 可用 persona 候选（room_info 来时装）—— "添加茶客" / "新建房间"的 picker 用。
let personasAvailable = []; // [{persona, name, avatar_data_uri}, ...]
// 房间生效的 max_consecutive_ai_turns（room_info.orchestrator 来时装）—— 圆桌
// cap 预校验用；缺省 20 与 OrchestratorConfig 默认一致。
let maxAiTurns = 20;

// 侧栏茶客列表 + 头像 + 打分 + 「我」那一行 —— 见 ./sidebar.js。打分明细 / 用户显示名
// 封在模块内；茶客名单仍归 renderer，sidebar 经 getGuests 取数。makeAvatar /
// makeUserAvatar 下文喂 chat_stream。
const sidebar = createSidebar({
  guestsEl,
  userNameEl,
  userAvatarWrapEl,
  isConnected: connection.isConnected,
  send,
  setStatus,
  getGuests: () => guests,
  showPermissionPopover: (anchor, g) => showPermissionPopover(anchor, g),
});

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
  taskPanelNewBtn.disabled = !connection.isConnected();
  taskPanelNewBtn.title = "新建任务";
}

function stickToBottom(mutate) {
  const stick = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 80;
  mutate();
  if (stick) messagesEl.scrollTop = messagesEl.scrollHeight;
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
  if (!connection.isConnected()) return;
  closeActionPopover();
  assignPopover?.show(assignHandoffBtn);
}

// P7.2「请审…」—— 点消息气泡的请审按钮触发。复用 action popover 当茶客选择菜单：
// 每位茶客一项（含被审消息作者自己——自审合法、不屏蔽），选中即发 handoff_review。
// anchor 是请审按钮本身；message_id 由 chat_stream 经 attachReviewButton 透传上来。
function requestReview(messageId, anchorBtn) {
  if (!connection.isConnected()) return;
  if (guests.length === 0) {
    setStatus("error", "房间里没有茶客 —— 无法请审");
    return;
  }
  showActionPopover(anchorBtn, "请审这条 —— 交给", guests.map((g) => ({
    label: g.name,
    onClick: () => {
      // 菜单是异步的——用户从弹出菜单里选茶客时连接可能已断，落点再查一次。
      if (!connection.isConnected()) return;
      send({ type: Inbound.HANDOFF_REVIEW, target: g.name, message_id: messageId });
      setStatus("", `已请「${g.name}」审阅这条发言`);
    },
  })));
}

// 消息上的任务 chip + filter 视图（P5.2.10）—— 见 ./message_filter.js。filter 状态
// 全封在模块内，renderer 只通过 afterAppendMessage / exitFilter / onTaskStateChange
// 三个出口交互。
const messageFilter = createMessageFilter({ messagesEl, taskState });

// chat-stream 工厂 —— 在所有依赖（messagesEl / sidebar 头像 / stickToBottom /
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
  makeAvatar: sidebar.makeAvatar,
  makeUserAvatar: sidebar.makeUserAvatar,
  stickToBottom,
  afterAppendMessage: messageFilter.afterAppendMessage,
  onRequestReview: requestReview,
});

// ── sidebar 装配（room_info）────────────────────────────────────────

function renderSidebar(roomInfo) {
  // 进新房（首次连接 / 换房）的全量重置：清 in-flight 流 + 消息容器 + 待发文件 pills
  // （pill 的 rel 是按房间 share/ 计的相对路径，换房后挂别房不通）。打分残留由下文
  // sidebar.renderGuests 重建时清。
  clearInFlight();
  // 切房 / 重连 / 清空 → filter 视图无意义，强 exit 让新房间从全量视角起步。
  messageFilter.exitFilter();
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
    // P9：切房后旧房在后台续跑 —— 它的 turn_end 不再回到本视图，currentTurnId
    // 不会被旧房收尾事件清掉。必须按**新前台房间**的 busy 快照重置「发送/停止」
    // 按钮（见 turn_state.syncToRoom）。同房刷新不进此分支，turn 状态不动（本房
    // turn 仍在跑时不该误清）。
    const nextRoom = (roomInfo.rooms_available ?? []).find(
      (r) => r.room_id === nextRoomId,
    );
    turnState.syncToRoom(!!nextRoom?.busy);
  }
  // sidebar 全量重渲会替掉头像 DOM —— 旧 anchor 一旦被 detach，popover 的"贴右侧"
  // 位置就指向虚空了，干脆关掉。
  closePermissionPopover();
  closeAssignPopover();
  setStatus("ok", `已连接 ${wsUrl}`);
  roomNameEl.textContent = roomInfo.room_name || "—";
  roomTopicEl.textContent = roomInfo.topic || "";
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
  sidebar.setUser({
    displayName: roomInfo.user_display_name,
    avatarDataUri: roomInfo.user_avatar_data_uri,
  });
  guests = Array.isArray(roomInfo.guests) ? roomInfo.guests : [];
  personasAvailable = Array.isArray(roomInfo.personas_available) ? roomInfo.personas_available : [];
  maxAiTurns = roomInfo.orchestrator?.max_consecutive_ai_turns ?? 20;
  sidebar.renderGuests();
  // 房间列表 + P9「进行中」后台房徽标（见 ./room_list.js）—— render 内部从权威快照
  // rooms_available[].busy 重建后台房集合再渲染。
  roomList.render(roomInfo.rooms_available, roomInfo.current_room_id);
  // room_info 到达 → composer 解锁；之前 onopen 不再 enable，避免 userDisplayName
  // 跳变窗口（用户在 "我" 状态发了一条，第二条又变成实际显示名）。
  setInputEnabled(true);
  textInput.focus();
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

// ── ws 断线清理 ────────────────────────────────────────────────────
// connection.js 在 ws close 时回调本函数 —— 重连决策（退避 / NO_RECONNECT_CODES）
// 留在模块内，这里只做 renderer 侧状态归位。
function handleDisconnect(code) {
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
  upload.dropPending(`ws closed (${code})`);
  // 断线时 turn_end 不会再来；本地清"停止"状态，让重连成功后按钮回到"发送"。
  turnState.clear();
}

// ── modal ────────────────────────────────────────────────────────────
// openModal / closeModal 是 modal 显隐的单点（modals.js 与 decision_support 共用）。
function openModal(modal) {
  modal.hidden = false;
}

function closeModal(modal) {
  modal.hidden = true;
}

// 添加茶客 / 新建房间 / 新建任务 / 导入 persona 四个 modal 的装配在 ./modals.js ——
// 模块自取 DOM 节点，全 modal 的「点 backdrop / × / ESC 关闭」兜底也一并挂。
wireModals({
  isConnected: connection.isConnected,
  send,
  setStatus,
  getGuests: () => guests,
  getPersonas: () => personasAvailable,
  pickFolder: window.chahua?.pickFolder,
  openModal,
  closeModal,
});

// composer 底栏「指派」按钮 —— 弹统一的指派 / 圆桌 popover。
assignHandoffBtn.addEventListener("click", openAssignPopover);

// ── 我（USER.md / 头像）─────────────────────────────────────────────

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
  isConnected: connection.isConnected,
  send,
  setStatus,
});

const guestSettings = createGuestSettings({
  isConnected: connection.isConnected,
  send,
  setStatus,
});

const roomSettings = createRoomSettings({
  isConnected: connection.isConnected,
  send,
  setStatus,
  openGuestSettings: (g) => guestSettings.open(g),
  openRawRoomToml: () => settings.openEditRoomToml(),
});

permissionPopover = createPermissionPopover({
  send,
  setStatus,
  isConnected: connection.isConnected,
  openGuestSettings: (g) => guestSettings.open(g),
});

assignPopover = createAssignPopover({
  send,
  setStatus,
  isConnected: connection.isConnected,
  getGuests: () => guests.map((g) => g.name),
  getMaxAiTurns: () => maxAiTurns,
});

// composer 上方的 handoff 队列预览小条 —— 订阅 handoff_state，队列变即重渲。
// renderSidebar 的 handoffState.reset() 会触发一次空队列 render 把小条收起。
const handoffQueueBar = createHandoffQueueBar({
  barEl: handoffQueueBarEl,
  send,
  isConnected: connection.isConnected,
});
handoffState.subscribe((queue) => handoffQueueBar.render(queue));

// P8.3 托管会话：任务卡里的「托管」按钮（入口 + 状态 + 停止三合一）。状态来自
// managed_session_* envelope；与 handoff 队列同瞬态——切房 / 断线即 reset。
managedSession = createManagedSession({
  send,
  isConnected: connection.isConnected,
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
  isConnected: connection.isConnected,
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
  isConnected: connection.isConnected,
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
  isConnected: connection.isConnected,
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
    if (!connection.isConnected()) return;
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
    if (!connection.isConnected()) return;
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
  isTurnActive: turnState.isActive,
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

// envelope 分派器 —— ws 下行每帧按 type 分发到各 feature 模块（见 ./envelope_router.js）。
// 在所有被分发到的 feature 模块就绪之后装配；connection.onMessage 经 arrow 推迟引用。
const envelopeRouter = createEnvelopeRouter({
  roomList,
  debugPanel,
  renderSidebar,
  renderHistory,
  applyScores: sidebar.applyScores,
  turnState,
  proposalCard,
  startStreamingMessage,
  appendDelta,
  endStreamingMessage,
  appendBubble,
  setStatus,
  upload,
  taskPanel,
  managedSession,
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
// 关任务都靠这条 subscriber 顺着重刷（见 ./message_filter.js）。
taskState.subscribe(messageFilter.onTaskStateChange);

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

// 决策入口：右键消息气泡 → action popover → modal。message_id 仅在 server 落盘后
// 才挂到 li 上（用户自己 echo 的那条没有，直到下次 room_history 才补），所以未带
// id 的 li 一律跳过 —— 决策必须能引回 transcript 里的真实 message。
// 标为决策 modal —— 见 ./decision_support.js（factory 实例化在 createUpload 之后）。
messagesEl.addEventListener("contextmenu", (ev) => {
  if (!connection.isConnected()) return;
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

// 清空聊天：本地不抢先清 DOM，让回环一致 —— 服务端清完会重发 room_info +
// room_history(空)，renderSidebar 一帧 messagesEl.replaceChildren；失败 / 服务端拒收
// 时 UI 不会出现"明明清了又冒出来"的诡异状态。
function clearCurrentRoom() {
  if (!connection.isConnected()) return;
  const roomName = roomNameEl.textContent;
  if (!window.confirm(`确定清空「${roomName}」的全部聊天记录？\n茶客在场，但本房间的 transcript / 摘要 / 游标会被重置。`)) return;
  send({ type: Inbound.CLEAR_ROOM });
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
  if (!connection.isConnected()) return;
  showActionPopover(anchor, "我（设置）", [
    { label: "编辑配置", desc: "改 USER.md（显示名 / 个人偏好）", onClick: settings.openEditUserMd },
    { label: "换头像", desc: "PNG / JPG / WebP / GIF，自动裁方 + 压到 256px PNG", onClick: settings.pickAvatarFile },
  ]);
}

function showRoomActionsPopover(anchor) {
  if (!connection.isConnected()) return;
  const roomName = roomNameEl.textContent || "本房间";
  showActionPopover(anchor, `房间「${roomName}」`, [
    { label: "详细设置", desc: "编排参数 / 打分 / 摘要模型 / 茶客一览", onClick: () => roomSettings.open() },
    { label: "更改房间配置", desc: "直接编辑 room.toml（name / topic / rules / [[guest]]）", onClick: settings.openEditRoomToml },
    { label: "导出 markdown", desc: "把本房间历史保存为 .md 文件", onClick: exportCurrentRoom },
    { label: "清空聊天", desc: "重置 transcript / 摘要 / 游标，茶客在场", danger: true, onClick: clearCurrentRoom },
  ]);
}

function exportCurrentRoom() {
  if (!connection.isConnected()) return;
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

// 斜杠命令处理器 —— composer submit 时 tryHandle(text) 优先拦截。clearInput 复刻
// 原内联的"清空 textarea + 重算高度"两步。
const commands = createCommands({
  isConnected: connection.isConnected,
  send,
  setStatus,
  appendBubble,
  taskState,
  upload,
  clearRoom: clearCurrentRoom,
  clearInput: () => {
    textInput.value = "";
    autoResizeTextarea();
  },
});

composer.addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (!dropdownEl.hidden) {
    // dropdown 开着时 Enter 已在 keydown 里被消费；这里防御性 noop。
    return;
  }
  if (!connection.isConnected()) return;
  // 「停止」分支：turnState.isActive() 表示 AI 链在跑，submit 按钮处于「停止」语义。
  // 服务端收到后 task.cancel() 当前 turn task，turn_end(status=cancelled) 回来时
  // turn_state 会清掉，按钮自动复原成「发送」。textInput 内容不动。
  if (turnState.isActive()) {
    send({ type: Inbound.CANCEL, turn_id: turnState.turnId() });
    setStatus("", "已请求停止…");
    return;
  }
  if (upload.isUploading()) {
    // 还有文件在上传中 —— snapshotRels 此刻只含已收到 echo 的；强发会丢已选的 attach。
    setStatus("", "文件还在上传，等所有 pill 出现后再发。");
    return;
  }
  const text = textInput.value.trim();
  // 斜杠命令（/help /tools /skills /task /clear task /clear /new）—— 见 ./commands.js。
  // 命中即在模块内执行并返回 true，普通发送路径不再走。
  if (commands.tryHandle(text)) return;
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
    speaker: sidebar.getUserDisplayName(),
    text: echo,
    kind: "user",
    taskId: taskState.getActiveTask()?.id ?? null,
  });
  send({
    type: Inbound.USER_MESSAGE,
    text,
    ...(files.length > 0 ? { files } : {}),
  });
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
connection.start();
