"use strict";

// 茶话室 renderer（P3.2.1）：消息流升级到流式打字机 + 打分徽章。
//
// envelope 消费完整路径：
//   turn_start    → 顶端横条列本轮打分明细（同步把 scoresByName 装一遍，供 message_start 取本人 score）
//   message_start → 起新一行 <li>，speaker 后面挂当前茶客的 score 徽章
//   message_delta → 增量 append chunk 到 textEl
//   message_end   → 按 status 封口（ok 收尾 / cancelled 追"[中断]" / error 追"[出错…]"）
//   turn_end      → 清 scoresByName，避免跨 turn 拿到上轮的分
//   guest_thinking / tool_*  → 静默（P3.2.2+ 再考虑要不要露）
//
// 用户消息（renderer 自己 echo 的那条）不走 envelope path —— 直接 appendBubble。

import { EventType, Status, Inbound, ScoreKind } from "./events.js";

const statusEl = document.getElementById("status");
const messagesEl = document.getElementById("messages");
const composer = document.getElementById("composer");
const textInput = document.getElementById("text");
const submitBtn = composer.querySelector("button");

const wsUrl = window.chahua?.wsUrl;
if (!wsUrl) {
  setStatus("error", "没有拿到 wsUrl —— preload / main 装配出了问题");
  throw new Error("missing wsUrl");
}

let ws = null;
let connected = false;

// 在途消息：message_id → { textEl, li } —— delta / end 按 ID 找回。
const inFlight = new Map();
// 当前 turn 的打分明细（guest_name → score record），message_start 时 O(1) 取本人 score。
// turn_start 时整张表 replace，turn_end 时清空 —— 避免跨 turn 错挂上一轮的徽章。
let scoresByName = new Map();

function setStatus(kind, text) {
  statusEl.className = `status ${kind}`;
  statusEl.textContent = text;
}

function setComposerEnabled(enabled) {
  textInput.disabled = !enabled;
  submitBtn.disabled = !enabled;
}

// "用户没主动往上翻" → DOM 改完滚到底；否则保持位置，不打断阅读历史。
// stick 必须在 DOM 修改 *之前* 算，scrollHeight 改完会变化。
function stickToBottom(mutate) {
  const stick = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 80;
  mutate();
  if (stick) messagesEl.scrollTop = messagesEl.scrollHeight;
}

// 打分 record → 显示文本。kind=mention/cooldown/error 给固定 label，
// kind=scored 或未知时回落到数字 score（未知 kind 没有 score 就显示 "?"）。
function scoreText(r) {
  switch (r.kind) {
    case ScoreKind.MENTION:
      return "@";
    case ScoreKind.COOLDOWN:
      return "冷却";
    case ScoreKind.ERROR:
      return "失败";
    case ScoreKind.SCORED:
    default:
      return typeof r.score === "number" ? r.score.toFixed(2) : "?";
  }
}

// 一条用户消息或一次性公告 —— 不走 streaming。
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

// turn_start 横条：把本轮所有候选的打分摆一行，方便用户看"为啥这位说话"。
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
  // kind=mention/cooldown/error 走 CSS [data-kind=...] 语义色；scored 无 dataset 走默认色。
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
  if (!m) return; // delta 早于 start / start 被丢弃 —— 不渲染孤儿 chunk
  stickToBottom(() => {
    // node.append(string) 只 mark 新 text node dirty，不替换整个 textContent 触发完整 reflow。
    m.textEl.append(env.data?.chunk ?? "");
  });
}

function endStreamingMessage(env) {
  const m = inFlight.get(env.message_id);
  if (!m) {
    // 没在 inFlight：通常是 ws 重连场景错过了 message_start，或非常规边角。
    // 无条件降级 bubble —— 不要静默丢消息（OK 路径以前会丢，已修）。
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
    if (env.status === Status.OK) {
      // delta 流已经 stream 完整个 text。data.text 与拼接结果应一致；不再 append。
      return;
    }
    m.li.classList.add("error");
    m.textEl.append(statusTail(env));
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

// ws 断线时把所有 in-flight 流式消息封口 —— 否则光标永远闪、inFlight 永远不清。
function closeInFlightOnDisconnect() {
  if (inFlight.size === 0) return;
  for (const m of inFlight.values()) {
    m.textEl.classList.remove("streaming");
    m.li.classList.add("error");
    m.textEl.append("  [连接断开]");
  }
  inFlight.clear();
}

function handleEnvelope(env) {
  switch (env.type) {
    case EventType.TURN_START: {
      const scores = env.data?.scores ?? [];
      // dev 期间方便核对 wire —— 横条没出来时打开 DevTools (Cmd+Opt+I) 验 envelope 是否到。
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
      // 本轮打分明细随 turn 结束失效，避免下个 turn pick=None 时 message_start
      // 拿到上轮残留的徽章数据。
      scoresByName = new Map();
      return;
    // guest_thinking / tool_* 暂时静默。
  }
}

function connect() {
  setStatus("", `连接中… ${wsUrl}`);
  setComposerEnabled(false);
  ws = new WebSocket(wsUrl);
  ws.addEventListener("open", () => {
    connected = true;
    setStatus("ok", `已连接 ${wsUrl}`);
    setComposerEnabled(true);
    textInput.focus();
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
  const text = textInput.value.trim();
  if (!text || !connected) return;
  appendBubble({ speaker: "我", text, kind: "user" });
  ws.send(JSON.stringify({ type: Inbound.USER_MESSAGE, text }));
  textInput.value = "";
});

connect();
