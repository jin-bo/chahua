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

import { EventType, Status, Inbound, ScoreKind } from "./events.js";

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

const wsUrl = window.chahua?.wsUrl;
if (!wsUrl) {
  setStatus("error", "没有拿到 wsUrl —— preload / main 装配出了问题");
  throw new Error("missing wsUrl");
}

let ws = null;
let connected = false;

// 在途消息：message_id → { textEl, li }。
const inFlight = new Map();
// 当前 turn 的打分明细。turn_start replace / turn_end 清。
let scoresByName = new Map();
// 茶客行 → 打分 span 的引用。sidebar 装好后填，turn_start/turn_end 直接改文字，
// 不重建 DOM（避免头像 / 徽章闪烁）。
const scoreSpansByName = new Map();
// 茶客名单（room_info 来时装）—— @ 补全候选 + 头像查找 + 用户显示名 / 头像。
let guests = []; // [{name, permission, isolation, avatar_data_uri}, ...]
let userDisplayName = "我";
let userAvatarDataUri = null;

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
  const showBadge = g.permission && g.permission !== "read-only";
  if (!showBadge) return img;
  const wrap = document.createElement("span");
  wrap.className = "avatar-wrap";
  wrap.appendChild(img);
  const badge = makePermissionBadge(g.permission, badgeClassName);
  badge.classList.add("on-avatar");
  wrap.appendChild(badge);
  return wrap;
}

// ── 消息流渲染 ───────────────────────────────────────────────────────
//
// 茶客发言（li.msg / li.error）：头像 + 气泡（header: 名字 + 打分徽章 / body: 文字），左对齐。
// 用户发言（li.user）：单独气泡，右对齐，无头像无名字（自己知道是自己）。
// turn-banner（li.turn-banner）：不变，meta 行，非气泡。

// 茶客行（speaker bubble + avatar）。streaming=true 时 text span 带 streaming class 闪光标。
// 返回 li 与 textEl —— 调用方在 textEl 上 append 文本（流式 / 一次性）。
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
  const textEl = document.createElement("span");
  textEl.className = streaming ? "text streaming" : "text";
  bubble.appendChild(textEl);
  li.appendChild(bubble);
  return { li, textEl };
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
  li.appendChild(bubble);
  const avatar = makeUserAvatar("msg-avatar");
  if (avatar) li.appendChild(avatar);
  return li;
}

function appendBubble({ speaker, text, kind }) {
  stickToBottom(() => {
    let li;
    if (kind === "user") {
      li = makeUserRow(text);
    } else {
      const row = makeGuestRow(speaker);
      row.textEl.textContent = text;
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
    const { li, textEl } = makeGuestRow(speaker, { streaming: true });
    messagesEl.appendChild(li);
    inFlight.set(env.message_id, { textEl, li });
  });
}

function appendDelta(env) {
  const m = inFlight.get(env.message_id);
  if (!m) return;
  stickToBottom(() => {
    m.textEl.append(env.data?.chunk ?? "");
  });
}

function statusTail(env) {
  if (env.status === Status.CANCELLED) return "  [中断]";
  if (env.status === Status.ERROR) {
    const err = env.data?.error || "未知错误";
    return `  [出错：${err}]`;
  }
  return "";
}

function endStreamingMessage(env) {
  const m = inFlight.get(env.message_id);
  if (!m) {
    const speaker = env.guest_name || "?";
    if (env.status === Status.OK) {
      appendBubble({ speaker, text: env.data?.text ?? "" });
    } else {
      appendBubble({
        speaker,
        text: (env.data?.partial_text ?? "") + statusTail(env),
        kind: "error",
      });
    }
    return;
  }
  inFlight.delete(env.message_id);
  stickToBottom(() => {
    m.textEl.classList.remove("streaming");
    if (env.status === Status.OK) return;
    m.li.classList.add("error");
    m.textEl.append(statusTail(env));
  });
}

function closeInFlightOnDisconnect() {
  if (inFlight.size === 0) return;
  for (const m of inFlight.values()) {
    m.textEl.classList.remove("streaming");
    m.li.classList.add("error");
    m.textEl.append("  [连接断开]");
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
  setStatus("ok", `已连接 ${wsUrl}`);
  roomNameEl.textContent = roomInfo.room_name || "—";
  roomTopicEl.textContent = roomInfo.topic || "";
  userDisplayName = roomInfo.user_display_name || "我";
  userAvatarDataUri = roomInfo.user_avatar_data_uri || null;
  guests = Array.isArray(roomInfo.guests) ? roomInfo.guests : [];
  guestsEl.replaceChildren();
  for (const g of guests) {
    const li = document.createElement("li");
    // V 标默认浮在头像右上角；缺头像（罕见）时回退到名字后 inline，避免丢失权限提示。
    const showBadge = g.permission && g.permission !== "read-only";
    const node = makeAvatarWithPermission(g, "avatar", "permission-badge");
    if (node) li.appendChild(node);
    li.appendChild(makeBadge("guest-name", null, g.name));
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
    guestsEl.appendChild(li);
  }
  renderRoomsList(roomInfo.rooms_available, roomInfo.current_room_id);
  // room_info 到达 → composer 解锁；之前 onopen 不再 enable，避免 userDisplayName
  // 跳变窗口（用户在 "我" 状态发了一条，第二条又变成实际显示名）。
  setInputEnabled(true);
  textInput.focus();
}

// 切换房间列表 —— 列其它房间（含当前），click 非 current 项发 switch_room frame。
// 当前房间高亮、不可点；其它房间显示 name + topic 一句话预览。
function renderRoomsList(roomsAvailable, currentRoomId) {
  roomsEl.replaceChildren();
  const rooms = Array.isArray(roomsAvailable) ? roomsAvailable : [];
  for (const r of rooms) {
    const li = document.createElement("li");
    li.dataset.roomId = r.room_id;
    if (r.room_id === currentRoomId) li.classList.add("current");
    const name = document.createElement("div");
    name.className = "room-name";
    name.textContent = r.name || r.room_id;
    li.appendChild(name);
    if (r.topic) {
      const topic = document.createElement("div");
      topic.className = "room-topic";
      topic.textContent = r.topic;
      li.appendChild(topic);
    }
    if (r.room_id !== currentRoomId) {
      li.addEventListener("click", () => {
        if (!connected) return;
        ws.send(JSON.stringify({ type: Inbound.SWITCH_ROOM, room_id: r.room_id }));
        setStatus("", `切换到 ${r.name || r.room_id}…`);
      });
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
    if (!g.broadcast && g.permission && g.permission !== "read-only") {
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
        row.textEl.textContent = m.text;
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
    case EventType.TURN_END:
      scoresByName = new Map();
      applyScoresToSidebar();
      return;
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
  const text = textInput.value.trim();
  if (!text || !connected) return;
  appendBubble({ speaker: userDisplayName, text, kind: "user" });
  ws.send(JSON.stringify({ type: Inbound.USER_MESSAGE, text }));
  textInput.value = "";
});

connect();
