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
  // P5.1 任务房间（docs/P5-任务房间.md §4.2）。TASK_INFO 是权威快照：每次任务状态
  // 变更（open / update / decision / artifact）后服务端重发整份 {tasks, active_task_id}，
  // 前端任务状态以最近一次 TASK_INFO 为准。另外四个是 hint —— 给前端做 toast / 动画 /
  // 高亮增量项，不进任务状态本身。
  TASK_INFO: "task_info",
  TASK_OPEN: "task_open",
  TASK_UPDATE: "task_update",
  TASK_DECISION_ADDED: "task_decision_added",
  TASK_ARTIFACT_ADDED: "task_artifact_added",
  // P5.2.4：任务推入终结态（done / abandoned）后的 hint。与其它四个 hint 同口径 —— 紧
  // 跟一帧 TASK_INFO 才是权威状态；这里仅给前端做 toast / 卡片翻灰动画用。
  TASK_CLOSE: "task_close",
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
  // P5.1 / P5.2 任务房间 inbound（docs/P5-任务房间.md §4.3）。服务端成功后会重发整份
  // task_info（权威快照）+ emit 对应 hint envelope；前端不做乐观更新，等回环。
  OPEN_TASK: "open_task",
  UPDATE_TASK: "update_task",
  ATTACH_ARTIFACT: "attach_artifact",
  ADD_DECISION: "add_decision",
  // P5.2.5：多任务管理 —— 切 active / 关闭任务。set_active_task 的 task_id 可为 null
  // （清回房间级闲聊）；close_task 的 status 仅接受 "done" / "abandoned"，其余非法值
  // 走服务端 NOTICE error 退回。
  SET_ACTIVE_TASK: "set_active_task",
  CLOSE_TASK: "close_task",
});

// 默认权限模式，镜像 chahua/permissions.py::DEFAULT_MODE。前端"添加茶客" /
// "新建房间"提交时给的兜底值，以及"是否显示 V 标"的判定基准（≠ DEFAULT 才显示）。
export const DEFAULT_PERMISSION = "read-only";

// 三档权限文案 —— 镜像 chahua/permissions.py 的 VALID_MODES。popover（renderer.js）
// 与 详细设置 modal（guest_settings.js）共享这份描述。``desc`` 给 popover 多行解释用，
// modal 只读 ``label``。
export const PERMISSION_OPTIONS = Object.freeze([
  Object.freeze({ value: "read-only", label: "只读", desc: "仅看/搜索，不写文件、不跑 shell" }),
  Object.freeze({ value: "workspace-write", label: "工作区可写", desc: "可写茶客工作目录、跑常规命令" }),
  Object.freeze({ value: "full-access", label: "完全访问", desc: "放行所有工具（含潜在破坏性，谨慎）" }),
]);

// 镜像 chahua/scoring.py::ScoreKind —— turn_start.data.scores[i].kind 取值。
// 与 EventType / Status 同性质：wire 协议常量，不是 presentation。
// label / 颜色 / 是否显示数字这些渲染决定留给 renderer，不污染本文件。
export const ScoreKind = Object.freeze({
  SCORED: "scored",
  MENTION: "mention",
  COOLDOWN: "cooldown",
  ERROR: "error",
});

// 镜像 chahua/task.py::TaskStatus。task.status 的 wire 字符串 + 中文 label 同源 ——
// 任务面板 / 状态下拉 / 历史列表都从这里取，避免散落到各 renderer 里手抄 5 个 case。
// 顺序与 task.py 的 Literal 顺序一致；新加状态时两边同步。
export const TASK_STATUS_OPTIONS = Object.freeze([
  Object.freeze({ value: "open", label: "未开始" }),
  Object.freeze({ value: "in_progress", label: "进行中" }),
  Object.freeze({ value: "blocked", label: "被阻塞" }),
  Object.freeze({ value: "done", label: "已完成" }),
  Object.freeze({ value: "abandoned", label: "已放弃" }),
]);

const _TASK_STATUS_LABELS = new Map(TASK_STATUS_OPTIONS.map((o) => [o.value, o.label]));

export function taskStatusLabel(status) {
  return _TASK_STATUS_LABELS.get(status) ?? (status || TASK_STATUS_OPTIONS[0].label);
}

// 镜像 chahua/tasks_store.py::CLOSED_STATUSES —— 终结态白名单。
// 渲染层 / inbound 校验都共享这份单一真理源，避免 JS 与 Python 各自手抄 "done" / "abandoned"
// 字面值，将来加 / 改终结态时漏改一边。
export const TASK_CLOSED_STATUSES = Object.freeze(new Set(["done", "abandoned"]));

export function isTaskClosed(status) {
  return TASK_CLOSED_STATUSES.has(status);
}
