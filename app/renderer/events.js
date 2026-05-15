"use strict";

// Envelope wire 协议常量 —— 镜像 chahua/events.py 的 ChahuaEventType + STATUS_*
// + INBOUND_USER_MESSAGE。两端是 wire 协议合同（envelope 跨语言契约），不是抽象层；
// 改 wire 时这里和 chahua/events.py / chahua/server.py 同步动。
//
// 不要把 renderer 内部 CSS class 的 kind（"user" / "error"）混进来 —— 那是
// presentation 层概念，跟 envelope status 形似而语义无关。

export const EventType = Object.freeze({
  ROOM_INFO: "room_info",
  ROOM_HISTORY: "room_history",
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
  SWITCH_ROOM: "switch_room",
  CLEAR_ROOM: "clear_room",
  CANCEL: "cancel",
  // 茶客 / 房间 增删 —— 镜像 chahua/server.py:INBOUND_*。服务端处理后会重发
  // room_info(+ history)；前端不做乐观更新，等回环。
  ADD_GUEST: "add_guest",
  REMOVE_GUEST: "remove_guest",
  CREATE_ROOM: "create_room",
  DELETE_ROOM: "delete_room",
  UPDATE_USER_MD: "update_user_md",
  UPDATE_USER_AVATAR: "update_user_avatar",
});

// 默认权限模式，镜像 chahua/permissions.py::DEFAULT_MODE。前端"添加茶客" /
// "新建房间"提交时给的兜底值，以及"是否显示 V 标"的判定基准（≠ DEFAULT 才显示）。
export const DEFAULT_PERMISSION = "read-only";

// 镜像 chahua/scoring.py::ScoreKind —— turn_start.data.scores[i].kind 取值。
// 与 EventType / Status 同性质：wire 协议常量，不是 presentation。
// label / 颜色 / 是否显示数字这些渲染决定留给 renderer，不污染本文件。
export const ScoreKind = Object.freeze({
  SCORED: "scored",
  MENTION: "mention",
  COOLDOWN: "cooldown",
  ERROR: "error",
});
