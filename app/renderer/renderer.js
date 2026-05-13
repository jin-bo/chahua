"use strict";

// 茶话室 renderer（P3.2.2）：在 P3.2.1 流式打字机基础上加 sidebar + @ 补全。
//
// envelope 消费完整路径：
//   room_info     → ws 连上首帧：装 sidebar（房间名 / topic / 茶客 + permission）
//                  + 喂 @ 补全候选 + 触发 user_display_name 显示
//   turn_start    → 顶端横条列本轮打分明细（同步装 scoresByName）
//   message_start → 起新一行 <li>，speaker 后面挂当前茶客的 score 徽章
//   message_delta → 增量 append chunk 到 textEl
//   message_end   → 按 status 封口（ok 收尾 / cancelled / error）
//   turn_end      → 清 scoresByName
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
const dropdownEl = document.getElementById("mention-dropdown");

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
// 茶客名单（room_info 来时装）—— @ 补全候选 + 用户显示名。
let guests = []; // [{name, permission, isolation}, ...]
let userDisplayName = "我";

function setStatus(kind, text) {
  statusEl.className = `status ${kind}`;
  statusEl.textContent = text;
}

function setComposerEnabled(enabled) {
  textInput.disabled = !enabled;
  submitBtn.disabled = !enabled;
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

// 通用 badge 工厂：permission / isolation / mention-permission 三处同构。
// 调用方决定 className（决定 CSS 样式）和 dataKey（决定语义色）。
function makeBadge(className, dataKey, value) {
  const b = document.createElement("span");
  b.className = className;
  if (dataKey) b.dataset[dataKey] = value;
  b.textContent = value;
  return b;
}

// ── 消息流渲染 ───────────────────────────────────────────────────────

function appendBubble({ speaker, text, kind }) {
  stickToBottom(() => {
    const li = document.createElement("li");
    if (kind) li.className = kind;
    const s = document.createElement("span");
    s.className = "speaker";
    s.textContent = `${speaker}：`;
    const t = document.createElement("span");
    t.className = "text";
    t.textContent = text;
    li.appendChild(s);
    li.appendChild(t);
    messagesEl.appendChild(li);
  });
}

function appendTurnBanner(scores) {
  if (!scores || scores.length === 0) return;
  stickToBottom(() => {
    const li = document.createElement("li");
    li.className = "turn-banner";
    li.textContent = scores
      .map((r) => `${r.guest_name ?? "?"}·${scoreText(r)}`)
      .join("  ");
    messagesEl.appendChild(li);
  });
}

function makeScoreBadge(speaker) {
  const score = scoresByName.get(speaker);
  if (!score) return null;
  const b = document.createElement("span");
  b.className = "score-badge";
  b.textContent = scoreText(score);
  // kind=scored 走默认色（数字打分），其余 kind 给 [data-kind=...] 语义色。
  if (score.kind && score.kind !== ScoreKind.SCORED) b.dataset.kind = score.kind;
  return b;
}

function startStreamingMessage(env) {
  stickToBottom(() => {
    const speaker = env.guest_name || "?";
    const li = document.createElement("li");
    li.className = "msg";
    const s = document.createElement("span");
    s.className = "speaker";
    s.textContent = `${speaker}：`;
    const badge = makeScoreBadge(speaker);
    const t = document.createElement("span");
    t.className = "text streaming";
    li.appendChild(s);
    if (badge) li.appendChild(badge);
    li.appendChild(t);
    messagesEl.appendChild(li);
    inFlight.set(env.message_id, { textEl: t, li });
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
  setStatus("ok", `已连接 ${wsUrl}`);
  roomNameEl.textContent = roomInfo.room_name || "—";
  roomTopicEl.textContent = roomInfo.topic || "";
  userDisplayName = roomInfo.user_display_name || "我";
  guests = Array.isArray(roomInfo.guests) ? roomInfo.guests : [];
  guestsEl.replaceChildren();
  for (const g of guests) {
    const li = document.createElement("li");
    const name = makeBadge("guest-name", null, g.name);
    li.appendChild(name);
    // read-only 是默认，徽章只标显著权限节省视觉噪音；isolation=room 同理。
    if (g.permission && g.permission !== "read-only") {
      li.appendChild(makeBadge("permission-badge", "permission", g.permission));
    }
    if (g.isolation && g.isolation !== "room") {
      li.appendChild(makeBadge("isolation-badge", "isolation", g.isolation));
    }
    guestsEl.appendChild(li);
  }
  // room_info 到达 → composer 解锁；之前 onopen 不再 enable，避免 userDisplayName
  // 跳变窗口（用户在 "我" 状态发了一条，第二条又变成实际显示名）。
  setComposerEnabled(true);
  textInput.focus();
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

function matchGuests(query) {
  const q = query.toLowerCase();
  return guests.filter((g) => g.name.toLowerCase().startsWith(q));
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
    li.appendChild(makeBadge("mention-name", null, g.name));
    if (g.permission && g.permission !== "read-only") {
      li.appendChild(makeBadge("mention-permission", null, g.permission));
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

function handleEnvelope(env) {
  switch (env.type) {
    case EventType.ROOM_INFO:
      renderSidebar(env.data ?? {});
      return;
    case EventType.TURN_START: {
      const scores = env.data?.scores ?? [];
      console.debug("[turn_start]", scores);
      scoresByName = new Map(scores.map((r) => [r.guest_name, r]));
      appendTurnBanner(scores);
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
      return;
    // guest_thinking / tool_* 暂时静默。
  }
}

// ── ws 连接 ─────────────────────────────────────────────────────

function connect() {
  setStatus("", `连接中… ${wsUrl}`);
  setComposerEnabled(false);
  ws = new WebSocket(wsUrl);
  ws.addEventListener("open", () => {
    connected = true;
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
    setComposerEnabled(false);
    setStatus("error", `连接断开 (${ev.code} ${ev.reason || ""})`);
    closeInFlightOnDisconnect();
  });
  ws.addEventListener("error", (ev) => {
    console.error("ws error", ev);
  });
}

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
