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
  // 服务端按前端 download_file inbound 请求把文件读出后回吐：
  // 成功 {rel, name, size, content_b64} / 失败 {rel, error}。前端走 Blob + a.download。
  FILE_DOWNLOAD: "file_download",
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
  // P5.8：/clear task 清空产物后的 hint。data {task_id, title, count, names}。
  // 其它 task hint 自带 diff，清空动作只有 TASK_INFO 全量快照 —— 单独加这个轻量
  // hint 让前端不必从快照猜 diff。镜像 chahua/events.py::TASK_ARTIFACTS_CLEARED。
  TASK_ARTIFACTS_CLEARED: "task_artifacts_cleared",
  // P5.3.3：茶客通过 task_propose_* 工具发出的"提议"（开任务 / 记决策）。data 形状：
  // {task_id?, proposer, kind: "decision"|"open", payload}。**纯 hint** —— 写权限永远
  // 在用户，前端渲染成"采纳/忽略"卡片，采纳后薄包装转译回既有 ADD_DECISION / OPEN_TASK
  // inbound。镜像 chahua/events.py::TASK_PROPOSAL。
  TASK_PROPOSAL: "task_proposal",
  // P6.3.A：调试抽屉点击某条历史索引行后服务端按 turn_id 回吐。data 形状：
  // ``found=true`` 路径 ``{found, turn, prompts: {<rel>: text}}``（``prompts`` 字段
  // 始终存在，最少 ``{}``；单 key 严格三重满足才出现）；``found=false`` 路径
  // ``{found: false}`` —— 被 rotation 清掉 / 协议过期。镜像 chahua/events.py::TURN_DETAIL。
  TURN_DETAIL: "turn_detail",
  // P7.1 显式 handoff / delegation（docs/P7 §3.3）。队列是调度层瞬态、不进 TASK_INFO；
  // 前端靠这三个事件维护本地 handoffQueueState「队列预览」，刷新 / 切房即清。
  //   - HANDOFF_ENQUEUED ``{queue: [item, ...]}`` 权威快照 → 整体替换；
  //   - HANDOFF_CONSUMED ``{item}`` + 顶层 turn_id → 该项已被消费、从队首移除；
  //   - HANDOFF_CLEARED  ``{items_dropped: [item, ...]}`` → 队列清空。
  HANDOFF_ENQUEUED: "handoff_enqueued",
  HANDOFF_CONSUMED: "handoff_consumed",
  HANDOFF_CLEARED: "handoff_cleared",
  // /tools / /skills slash 查询的回包。data 形状 {guest, permission, tools, skills,
  // view} —— 茶客 agent 注册的 tools + 可用 skills + 权限模式；view 是请求方回声，
  // 前端按它裁剪显示哪段。一次性查询、不进 transcript。镜像 chahua/events.py。
  GUEST_CAPS_INFO: "guest_caps_info",
  // P8.3 托管任务会话（MTS，docs/P8.3-原生自动推进.md §3.4）。三者都是 hint 型事件
  // —— 前端渲状态条 / 系统气泡，不进 transcript、不触发 AI。镜像 chahua/events.py。
  //   - MANAGED_SESSION_STARTED  {task_id, manager_guest, budget}
  //   - MANAGED_SESSION_ADVANCED {manager_guest, remaining_budget}（UI 倒计时）
  //   - MANAGED_SESSION_ENDED    {reason}（见 formatManagedSessionNotice）
  MANAGED_SESSION_STARTED: "managed_session_started",
  MANAGED_SESSION_ADVANCED: "managed_session_advanced",
  MANAGED_SESSION_ENDED: "managed_session_ended",
  // P9 切房后房间后台续跑（docs/P9 §4 / §5）。hint 型里程碑：切走后仍 busy 的房间
  // 转后台续跑，其 turn / handoff drain 跑完、runtime 自毁时 emit 一次。前端据此
  // 清掉房间列表「进行中」徽标。镜像 chahua/events.py::ROOM_BACKGROUND_FINISHED。
  ROOM_BACKGROUND_FINISHED: "room_background_finished",
  // P10.2 房间级（非任务）产物。data {rel, name, size, originated_message_id?} ——
  // 与 TASK_ARTIFACT_ADDED 平行的 hint，缺 task_id（语义上不归属任何任务）。前端按
  // originated_message_id 优先挂气泡末尾，命不中走系统气泡兜底。
  ROOM_ARTIFACT_ADDED: "room_artifact_added",
  // P11 后台 Agent run（docs/P11-后台 Agent.md §「envelope」）。4 个 hint 型 ——
  // 不进 transcript、不触发 AI。data 形状均含 ``{run_id, guest_name, task_id?,
  // issued_by, source_guest?, instruction_preview?}``；ERROR 加 ``{error}``。
  // 前端职责分叉：
  //   - AGENT_RUN_STARTED → 新增运行列表条 + sidebar .guest-bg-run 指示亮；
  //   - AGENT_RUN_FINISHED → 移除列表条 + sidebar 指示熄（**只**这个事件做这事；
  //     正常茶客气泡靠普通 MESSAGE_END(ok) 追加）；
  //   - AGENT_RUN_CANCELLED / AGENT_RUN_ERROR → 同上移除 + 中央系统气泡，
  //     **不**生成结果气泡。
  // 镜像 chahua/events.py::ChahuaEventType.AGENT_RUN_*。
  AGENT_RUN_STARTED: "agent_run_started",
  AGENT_RUN_FINISHED: "agent_run_finished",
  AGENT_RUN_CANCELLED: "agent_run_cancelled",
  AGENT_RUN_ERROR: "agent_run_error",
  // P12.6 已安装 persona 列表 + 检查 / 更新 / 删除的权威全量快照。data 形状
  // {personas: [{name, display_name, persona, avatar_data_uri, summary, source_type,
  // source_url, status, local_modified, installed_version, latest_version, detail}, ...]}。
  // list / check / update / delete 后均回这一帧，前端整批覆盖渲染。镜像
  // chahua/events.py::ChahuaEventType.PERSONAS_INSTALLED。
  PERSONAS_INSTALLED: "personas_installed",
});

// P9 §4：后台房间事件白名单 —— 后台 router 只放行这些里程碑。前端用同一集合判定
// 「这条 envelope 是否可能来自后台房」：在集合内且 envelope.room_id 非前台 → 当作
// 后台里程碑分流（只更新房间列表徽标，不碰前台聊天 / 任务面板）。镜像
// chahua/room_runtime.py::_BACKGROUND_WHITELIST。
export const BACKGROUND_MILESTONE_TYPES = Object.freeze(new Set([
  EventType.TURN_START,
  EventType.TURN_END,
  EventType.MESSAGE_END,
  EventType.TASK_INFO,
  EventType.TASK_ARTIFACT_ADDED,
  EventType.ROOM_ARTIFACT_ADDED,
  EventType.MANAGED_SESSION_STARTED,
  EventType.MANAGED_SESSION_ADVANCED,
  EventType.MANAGED_SESSION_ENDED,
  EventType.ROOM_BACKGROUND_FINISHED,
  // P11：bg run 4 个里程碑加入后台白名单。
  EventType.AGENT_RUN_STARTED,
  EventType.AGENT_RUN_FINISHED,
  EventType.AGENT_RUN_CANCELLED,
  EventType.AGENT_RUN_ERROR,
]));

// TASK_PROPOSAL envelope 的 data.kind 取值。镜像 chahua/events.py::TASK_PROPOSAL_KIND_*。
// 前端"采纳"按钮按 kind 分发对应 inbound 帧。
export const TaskProposalKind = Object.freeze({
  DECISION: "decision",
  OPEN: "open",
  // P8.2：改任务状态提议。采纳后按 status 分流 update_task / close_task。
  STATUS: "status",
  // P7.4：handoff propose —— 平铺 kind，字面值与 Inbound.HANDOFF_* 同形。
  HANDOFF_DELEGATE: "handoff_delegate",
  HANDOFF_REVIEW: "handoff_review",
  HANDOFF_PANEL: "handoff_panel",
});

// artifact 的 created_by 取值。镜像 chahua/task.py::ARTIFACT_CREATED_BY_GUEST /
// MARKED_BY_USER —— 用户 attach_artifact 上传 vs 茶客自动归集两条来源。
export const ArtifactCreatedBy = Object.freeze({
  GUEST: "guest",
  USER: "user",
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
  // 下载房间内文件。payload: {rel: "share/<name>" | "tasks/<id>/artifacts/<name>"}。
  // 服务端校验前缀 + 路径不穿越后回 file_download envelope。
  DOWNLOAD_FILE: "download_file",
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
  // 2026-05-19：清空任务产物（/clear task 入口）。仅删 tasks/<id>/artifacts/ 下文件，
  // 任务本身（status / decisions / 摘要）保留；服务端 confirm 由前端做（destructive）。
  CLEAR_TASK_ARTIFACTS: "clear_task_artifacts",
  // P6.3.A：按 turn_id 拉历史 turn 详情 + 关联 prompt 文件。payload: {turn_id}。
  // 服务端 regex 校验 ``^turn_[0-9a-f]+$``；未知 / rotation 清掉 → TURN_DETAIL{found=false}。
  FETCH_TURN_DETAIL: "fetch_turn_detail",
  // P7.1 / P7.2 / P7.3 显式 handoff（docs/P7 §3.2 / docs/P7.2 §3.2 / docs/P7.3 §3.2）。
  //   - HANDOFF_DELEGATE {target, reason?}：reason 是内部备注、不进茶客 prompt。
  //   - HANDOFF_REVIEW {target, message_id}：请某茶客审阅指定历史消息；无 reason
  //     字段——语义锚就是 message_id。
  //   - HANDOFF_PANEL {targets, summarizer?}：圆桌——多 panelist 平行发言 + 可选汇总者；
  //     无 reason 字段——targets 已把意图说清。
  //   - HANDOFF_CLEAR 无 payload —— 全部取消（cancel 当前 in-flight + 清队列）。
  // 入站严格，未知字段服务端 NOTICE error 丢帧。
  HANDOFF_DELEGATE: "handoff_delegate",
  HANDOFF_REVIEW: "handoff_review",
  HANDOFF_PANEL: "handoff_panel",
  HANDOFF_CLEAR: "handoff_clear",
  // P8.3 托管任务会话（docs/P8.3 §3.2）。
  //   - MANAGED_SESSION_START {task_id, manager_guest, budget}：开启 MTS。
  //   - MANAGED_SESSION_STOP 无 payload：停止托管（不取消当前 in-flight turn）。
  MANAGED_SESSION_START: "managed_session_start",
  MANAGED_SESSION_STOP: "managed_session_stop",
  // /tools / /skills slash 查询。payload: {guest, view}。服务端回 GUEST_CAPS_INFO
  // 并原样带回 view（请求方据此裁剪显示，多查询并发不串台）。
  LIST_GUEST_CAPS: "list_guest_caps",
  // P11 后台 Agent run inbound。
  //   - AGENT_RUN_START {target, instruction, task_id?}：用户手动 / /bg 命令入口。
  //     instruction 必填非空、不允许默认句兜底。
  //   - AGENT_RUN_CANCEL {run_id}：只 cancel 指定 run，不动其它 bg run / 前台 turn。
  AGENT_RUN_START: "agent_run_start",
  AGENT_RUN_CANCEL: "agent_run_cancel",
  // P12.6 已安装 persona 管理。服务端处理后回 PERSONAS_INSTALLED 全量快照；
  // update / delete 另回 NOTICE + room_info。镜像 chahua/server_inbound_io.py::INBOUND_*。
  //   - LIST_INSTALLED_PERSONAS 无 payload。
  //   - CHECK_PERSONA_UPDATES {name?}：name 缺省 = 检查全部可更新来源。
  //   - UPDATE_PERSONA {name, force?}：force 缺省 false，本地已改时须 true（前端 confirm 后置）。
  //   - DELETE_PERSONA {name}：destructive，前端已 confirm。
  LIST_INSTALLED_PERSONAS: "list_installed_personas",
  CHECK_PERSONA_UPDATES: "check_persona_updates",
  UPDATE_PERSONA: "update_persona",
  DELETE_PERSONA: "delete_persona",
});

// 镜像 chahua/handoff.py::HandoffKind —— handoff 队列项的 ``kind`` wire 值。
// 队列预览据此分流渲染；新加 kind 时两边同步。
export const HandoffKind = Object.freeze({
  DELEGATE: "delegate",
  REVIEW: "review",
  PANEL: "panel",
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
  Object.freeze({ value: "ready", label: "已就绪" }),
  Object.freeze({ value: "doing", label: "进行中" }),
  Object.freeze({ value: "blocked", label: "被阻塞" }),
  Object.freeze({ value: "review", label: "评审中" }),
  Object.freeze({ value: "done", label: "已完成" }),
  Object.freeze({ value: "abandoned", label: "已放弃" }),
]);

const _TASK_STATUS_LABELS = new Map(TASK_STATUS_OPTIONS.map((o) => [o.value, o.label]));

export function taskStatusLabel(status) {
  return _TASK_STATUS_LABELS.get(status) ?? (status || TASK_STATUS_OPTIONS[0].label);
}

// task.title 为空 / 未填时前端渲染的占位 —— 任务面板 / chip / chip dropdown / 其它
// 任务列表都从这里取，避免散落手抄。"(无标题)" 与 newTaskTitleEl 的 placeholder 口径
// 区分：placeholder 鼓励用户填，本常量是 fallback。
export const TASK_UNTITLED = "(无标题)";

// 消息 chip / composer chip / chip dropdown 共用 "📋 <title>" 文案 —— 单源避免散落手抄。
// 改图标 / 改格式只动一处。task_panel.js 的"其它任务"列表用 "📌" 表区别（卡片 vs chip
// 视觉分流），不走这条。
export function formatTaskLabel(task) {
  if (!task) return "";
  return `📋 ${task.title || TASK_UNTITLED}`;
}

// P5.8：把 task hint envelope 渲成聊天区居中系统气泡的文案。task event 不再合成
// user 消息进 transcript（docs/P5.8-任务事件系统气泡.md）—— 系统气泡是当前客户端的
// 瞬时 UI 通知，刷新 / 切房即清。文案唯一来源，renderer.js task hint 分支调用。
export function formatTaskEventNotice(type, data) {
  switch (type) {
    case EventType.TASK_OPEN: {
      const goal = firstLineClamped(data.goal);
      return `📋 用户开启任务【${data.title || TASK_UNTITLED}】${
        goal ? ` · 目标：${goal}` : ""
      }`;
    }
    case EventType.TASK_CLOSE:
      // data.title 由后端补字段（docs §5.5），不依赖此刻 taskState 是否已被 TASK_INFO 推进。
      return data.status === "done"
        ? `✅ 任务【${data.title || TASK_UNTITLED}】已完成`
        : `✗ 任务【${data.title || TASK_UNTITLED}】已放弃`;
    case EventType.TASK_DECISION_ADDED:
      return `📌 决策：${data.summary || ""}`;
    case EventType.TASK_ARTIFACT_ADDED:
      return data.created_by === ArtifactCreatedBy.GUEST
        ? `📎 茶客产出：${data.name || ""}`
        : `📎 用户挂载产物：${data.name || ""}`;
    case EventType.ROOM_ARTIFACT_ADDED:
      return `📎 房间产物：${data.name || ""}`;
    case EventType.TASK_ARTIFACTS_CLEARED:
      return `🗑️ 用户清空了任务【${data.title || TASK_UNTITLED}】的产物（${
        data.count || 0
      } 个文件）`;
    default:
      return "";
  }
}

// P8.3 / P8.4：managed_session_ended 的 reason → 居中系统气泡文案。文案唯一来源
// （同 formatTaskEventNotice 口径）。P8.4 起 5 reason —— ``manager_finished`` 已
// 退役（管理者没派活 → MTS dormant 等用户下一句话，不发 ENDED）。``task_closed``
// 文案扩展成「任务关闭或切换」覆盖三种来源：① 关 MTS 任务；② 用户采纳
// ``propose_status("done")`` 走 close_task；③ 用户开新任务 / 切走 active 让 MTS
// 任务不再 active（P8.4 §2 / §6）。默认 fallback「托管会话结束」兜未知 reason
// （含从老后端误送的 ``manager_finished``）。
const _MANAGED_SESSION_END_REASONS = Object.freeze({
  budget_exhausted: "托管预算已用尽",
  task_closed: "任务关闭或切换，托管会话结束",
  cap_reached: "已达连续发言上限，托管会话结束",
  user_stopped: "你停止了托管会话",
  user_cancel: "托管会话已结束",
});

export function formatManagedSessionNotice(reason) {
  const label = _MANAGED_SESSION_END_REASONS[reason] || "托管会话结束";
  return `🛑 ${label}`;
}

// goal 取首行并截到 80 字 —— 系统气泡只给一句概要，完整 goal 已在任务面板。
function firstLineClamped(s) {
  if (typeof s !== "string" || !s) return "";
  const line = s.split("\n", 1)[0].trim();
  return line.length <= 80 ? line : line.slice(0, 80) + "…";
}

// owner 选 null 时的展示文案；wire 层 owner = null，UI 层 "全员"。下拉 / meta 都从这里取。
export const TASK_OWNER_ALL = "全员";

// owner 下拉的选项数据 —— 新任务 modal 与活跃任务卡 owner select 共用同一份 shape，
// 避免两边各自手抄 "全员" + guest names。返回纯数据让调用方按需挂 DOM / framework。
export function buildOwnerOptionData(guestNames) {
  return [
    { value: "", label: TASK_OWNER_ALL },
    ...guestNames.map((name) => ({ value: name, label: name })),
  ];
}

// 镜像 chahua/tasks_store.py::CLOSED_STATUSES —— 终结态白名单。
// 渲染层 / inbound 校验都共享这份单一真理源，避免 JS 与 Python 各自手抄 "done" / "abandoned"
// 字面值，将来加 / 改终结态时漏改一边。
export const TASK_CLOSED_STATUSES = Object.freeze(new Set(["done", "abandoned"]));

export function isTaskClosed(status) {
  return TASK_CLOSED_STATUSES.has(status);
}
