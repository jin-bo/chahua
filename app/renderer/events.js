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
  NOTICE: "notice",
  // 服务端确认收到一个文件上传：data.rel 是落盘相对路径（share/xxx），前端挂 pending pill。
  FILE_UPLOADED: "file_uploaded",
  // 服务端把整个房间 transcript 拼成 markdown 回吐：data.filename / data.markdown。
  // 前端走 Blob + a.download 触发浏览器下载，不写服务器盘。
  ROOM_EXPORT: "room_export",
});

export const Status = Object.freeze({
  OK: "ok",
  ERROR: "error",
  CANCELLED: "cancelled",
});

// NOTICE envelope 的 data.level —— 镜像 chahua/events.py NOTICE_LEVEL_*。
export const NoticeLevel = Object.freeze({
  INFO: "info",
  ERROR: "error",
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
  UPDATE_GUEST_PERMISSION: "update_guest_permission",
  SET_PERSONA_MCP_TRUST: "set_persona_mcp_trust",
  CREATE_ROOM: "create_room",
  DELETE_ROOM: "delete_room",
  UPDATE_USER_MD: "update_user_md",
  UPDATE_USER_AVATAR: "update_user_avatar",
  // 覆盖当前房间 room.toml 全文。服务端校验失败会回滚 + emit notice(error)。
  UPDATE_ROOM_TOML: "update_room_toml",
  // 结构化 mutator（P4 详细设置 modal 用）。payload 见对应 chahua/server.py
  // _inbound_* —— spec=null 是合法 payload，语义"清整段，回房间默认"。
  UPDATE_ROOM_ORCHESTRATOR: "update_room_orchestrator",
  UPDATE_ROOM_LLM: "update_room_llm",
  UPDATE_GUEST_LLM: "update_guest_llm",
  UPDATE_GUEST_ISOLATION: "update_guest_isolation",
  UPDATE_GUEST_EXTRA_MCP: "update_guest_extra_mcp",
  // persona 导入：本地文件夹（main 端 dialog 选目录后传 path）/ GitHub URL。
  // 成功 / 失败都走 NOTICE envelope 回报；前端再用 alert / status 显示。
  IMPORT_PERSONA_FOLDER: "import_persona_folder",
  IMPORT_PERSONA_GITHUB: "import_persona_github",
  // 上传文件到房间共享目录。payload: {filename, content_b64}。
  // 服务端校验后写 share/<safe-name>、回 file_uploaded envelope。
  UPLOAD_FILE: "upload_file",
  // 导出当前房间为 markdown。无 payload，服务端读 transcript 全量后回 ROOM_EXPORT。
  EXPORT_ROOM: "export_room",
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
