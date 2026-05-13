"use strict";

// Envelope wire 协议常量 —— 镜像 chahua/events.py 的 ChahuaEventType + STATUS_*
// + INBOUND_USER_MESSAGE。两端是 wire 协议合同（envelope 跨语言契约），不是抽象层；
// 改 wire 时这里和 chahua/events.py / chahua/server.py 同步动。
//
// 不要把 renderer 内部 CSS class 的 kind（"user" / "error"）混进来 —— 那是
// presentation 层概念，跟 envelope status 形似而语义无关。

export const EventType = Object.freeze({
  ROOM_INFO: "room_info",
  TURN_START: "turn_start",
  MESSAGE_START: "message_start",
  MESSAGE_DELTA: "message_delta",
  MESSAGE_END: "message_end",
  TURN_END: "turn_end",
  GUEST_THINKING: "guest_thinking",
  TOOL_START: "tool_start",
  TOOL_COMPLETE: "tool_complete",
});

export const Status = Object.freeze({
  OK: "ok",
  ERROR: "error",
  CANCELLED: "cancelled",
});

export const Inbound = Object.freeze({
  USER_MESSAGE: "user_message",
  // CANCEL: "cancel" —— P3.3 加停止按钮时启用，与 chahua/server.py 的
  // _handle_inbound 同步加路由。
});

// 镜像 chahua/scoring.py::ScoreKind —— turn_start.data.scores[i].kind 取值。
// 与 EventType / Status 同性质：wire 协议常量，不是 presentation。
// label / 颜色 / 是否显示数字这些渲染决定留给 renderer，不污染本文件。
export const ScoreKind = Object.freeze({
  SCORED: "scored",
  MENTION: "mention",
  COOLDOWN: "cooldown",
  ERROR: "error",
});
