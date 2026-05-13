"use strict";

// 茶话室 renderer（P3.1 极简）：
//   - 拿 preload 暴露的 wsUrl → 连 ws
//   - 只渲染 message_end 一条一行（delta / start / turn_* 留给 P3.2 的打字机）
//   - 回车 → JSON.stringify({type:"user_message",text})

import { EventType, Status, Inbound } from "./events.js";

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

function setStatus(kind, text) {
  statusEl.className = `status ${kind}`;
  statusEl.textContent = text;
}

function appendMessage({ speaker, text, kind }) {
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
  // 只在用户没主动往上翻时自动滚到底 —— 阅读历史不被打断。
  const nearBottom =
    messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 80;
  if (nearBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
}

function setComposerEnabled(enabled) {
  textInput.disabled = !enabled;
  submitBtn.disabled = !enabled;
}

function handleEnvelope(env) {
  if (env.type !== EventType.MESSAGE_END) return;
  const speaker = env.guest_name || "?";
  const partial = env.data?.partial_text ?? "";
  if (env.status === Status.OK) {
    appendMessage({ speaker, text: env.data?.text ?? "" });
  } else if (env.status === Status.CANCELLED) {
    appendMessage({ speaker, text: `${partial}  [中断]`, kind: "error" });
  } else if (env.status === Status.ERROR) {
    const err = env.data?.error || "未知错误";
    appendMessage({ speaker, text: `${partial}  [出错：${err}]`, kind: "error" });
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
  });
  ws.addEventListener("error", (ev) => {
    console.error("ws error", ev);
  });
}

composer.addEventListener("submit", (ev) => {
  ev.preventDefault();
  const text = textInput.value.trim();
  if (!text || !connected) return;
  appendMessage({ speaker: "我", text, kind: "user" });
  ws.send(JSON.stringify({ type: Inbound.USER_MESSAGE, text }));
  textInput.value = "";
});

connect();
