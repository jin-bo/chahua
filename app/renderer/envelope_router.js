"use strict";

// envelope 分派 —— ws 下行每帧一条 envelope，按 type 分发到各 feature 模块。
// 从 renderer.js 抽出（renderer 重构）。
//
// envelope 消费完整路径：
//   room_info     → ws 连上首帧：装 sidebar（房间名 / topic / 茶客 + permission）
//   room_history  → 历史回放 + 调试索引
//   turn_start    → sidebar 各茶客名右侧显示本轮打分小数字
//   message_*     → 流式气泡 起 / 增量 / 封口
//   turn_end      → 清打分 + 收「停止」按钮
//   task_* / handoff_* / managed_session_* → 各 feature 模块
//   guest_thinking / tool_*  → 静默
//
// foregroundRoomId（当前前台房 envelope room_id）全封进模块：P9 后台房间里程碑据它
// 分流 —— room_id 与前台不同且属白名单类型 → 只刷房间列表徽标，不污染前台。

import {
  EventType,
  Status,
  NoticeLevel,
  BACKGROUND_MILESTONE_TYPES,
  formatTaskEventNotice,
  formatManagedSessionNotice,
} from "./events.js";
import * as taskState from "./task_state.js";
import * as handoffState from "./handoff_state.js";

// GUEST_CAPS_INFO envelope → system 气泡文案。view 决定列 tools 还是 skills 段
// （permission / guest 两段共用）。description 取首行 + 截断，避免长描述刷屏。
function formatGuestCaps(data, view) {
  const guest = data.guest || "?";
  const brief = (s) => (s || "").split("\n")[0].slice(0, 100);
  if (view === "skills") {
    const skills = Array.isArray(data.skills) ? data.skills : [];
    const lines = [`**${guest}** 可用 skills（${skills.length}）`];
    if (!skills.length) lines.push("_（无）_");
    for (const s of skills) {
      lines.push(`- \`${s.name}\`${s.description ? ` —— ${brief(s.description)}` : ""}`);
    }
    return lines.join("\n");
  }
  const tools = Array.isArray(data.tools) ? data.tools : [];
  const lines = [`**${guest}** 注册的 tools（${tools.length}，权限模式：${data.permission || "?"}）`];
  for (const t of tools) {
    const ro = t.is_read_only ? "read-only" : "write";
    lines.push(`- \`${t.name}\` (${ro})${t.description ? ` —— ${brief(t.description)}` : ""}`);
  }
  return lines.join("\n");
}

// Blob → <a download> 触发浏览器原生下载。CSP 已允许 'self'，blob: URL 在 Chromium
// 里也通过 default-src 'self'。
function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export function createEnvelopeRouter({
  roomList,
  debugPanel,
  renderSidebar,
  renderHistory,
  applyScores,
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
}) {
  // P9：当前前台房间在 envelope 里的 room_id（= room.name，见 chahua/events.py）。
  // 从 room_info envelope 顶层 room_id 捕获。
  let foregroundRoomId = null;

  function handleEnvelope(env) {
    // P9：后台房间里程碑分流 —— envelope.room_id 与前台不同、且属后台白名单类型
    // （turn_* / message_end / task_info / task_artifact_added / managed_session_* /
    // room_background_finished）→ 当作后台房间里程碑，只更新房间列表「进行中」徽标，
    // 不让它污染前台聊天 / 任务面板 / 调试抽屉。控制面事件（room_info / room_history
    // / notice 等）不在白名单内 —— 切房时新房 room_info 的 room_id 虽 ≠ 旧前台，仍
    // 照常落到下面 switch 并由 ROOM_INFO 分支刷新 foregroundRoomId。
    if (
      foregroundRoomId !== null &&
      env.room_id &&
      env.room_id !== foregroundRoomId &&
      BACKGROUND_MILESTONE_TYPES.has(env.type)
    ) {
      roomList.onBackgroundMilestone(env);
      return;
    }
    // 调试抽屉旁路消费 —— 模块内部按 env.type dispatch；ROOM_INFO 的 reset 在
    // renderSidebar 里调（与 inFlight / proposalCard 同步清）。
    debugPanel.onEnvelope(env);
    switch (env.type) {
      case EventType.ROOM_INFO:
        // 前台房间在 envelope 里的 room_id（= room.name）—— 后台分流的判定基准。
        foregroundRoomId = env.room_id ?? null;
        renderSidebar(env.data ?? {});
        return;
      case EventType.ROOM_HISTORY:
        renderHistory(env.data?.messages ?? []);
        // P6.3.A：``turns_index`` 倒序投影注入调试抽屉的历史索引（``debug.enabled=False``
        // 时整字段缺省 → applyTurnsIndex 不入 array，索引区维持空）。
        debugPanel.applyTurnsIndex(env.data?.turns_index ?? null);
        return;
      case EventType.TURN_START: {
        const scores = env.data?.scores ?? [];
        console.debug("[turn_start]", scores);
        applyScores(scores);
        turnState.set(env.turn_id);
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
      case EventType.TURN_END: {
        applyScores([]);
        // AI 链终态判定：status != ok（cancelled）或 next==='user'（用户该说话了）→ 收
        // 「停止」按钮。next==='ai' 时维持，下一个 turn_start 立刻刷新 turn 状态。
        const next = env.data?.next;
        if (env.status !== Status.OK || next === "user") {
          turnState.clear();
          // AI 链收尾、无 in-flight turn —— 放开被 gate 的 handoff 采纳按钮（此刻采纳
          // 不会抢占截断任何 turn）。next==='ai' 时链未结束，不放。
          proposalCard.onTurnIdle();
        }
        return;
      }
      case EventType.NOTICE: {
        // 服务端 mutator 的一次性反馈 —— 目前 persona 导入用。错误强提示 alert，
        // 让用户立刻看到原因；info 走 status bar 不打断流。
        const level = env.data?.level || NoticeLevel.INFO;
        const text = env.data?.text || "";
        if (!text) return;
        if (level === NoticeLevel.ERROR) {
          window.alert(text);
          setStatus("error", text);
        } else {
          setStatus("ok", text);
        }
        return;
      }
      case EventType.FILE_UPLOADED:
        upload.onServerEcho(env.data ?? {});
        return;
      // 任务房间（docs/P5-任务房间.md §4.2）。TASK_INFO 是权威快照 → 全量覆盖 taskState
      // 让面板重渲；其它 4 个是 hint → 在对应 DOM 上闪一下，权威 state 等紧跟而至的
      // TASK_INFO 推进。
      case EventType.TASK_INFO:
        taskState.setSnapshot(env.data ?? {});
        return;
      // P5.8：task hint 既闪面板增量项、又在聊天区追一条居中系统气泡（task event
      // 不再合成 user 消息进 transcript）。气泡是瞬时 UI 通知，刷新 / 切房即清。
      case EventType.TASK_OPEN:
      case EventType.TASK_DECISION_ADDED:
      case EventType.TASK_ARTIFACT_ADDED:
      case EventType.TASK_CLOSE: {
        taskPanel.flashHint(env.type, env.data ?? {});
        const text = formatTaskEventNotice(env.type, env.data ?? {});
        if (text) appendBubble({ kind: "system", text });
        return;
      }
      case EventType.TASK_ARTIFACTS_CLEARED: {
        // 不调 flashHint —— 清空动作没有可高亮的"新增项"，状态推进全靠紧随的
        // TASK_INFO；本事件唯一价值就是系统气泡。
        const text = formatTaskEventNotice(env.type, env.data ?? {});
        if (text) appendBubble({ kind: "system", text });
        return;
      }
      // TASK_UPDATE 不显示气泡 —— 避免用户编辑标题时聊天流被刷。
      case EventType.TASK_UPDATE:
        taskPanel.flashHint(env.type, env.data ?? {});
        return;
      case EventType.TASK_PROPOSAL:
        proposalCard.onEnvelope(env);
        return;
      // P7.1 显式 handoff（docs/P7 §3.3）。三个事件维护本地队列预览；权威状态就是
      // 队列本身——服务端不落盘，刷新 / 切房在 renderSidebar 里 reset。
      case EventType.HANDOFF_ENQUEUED:
        handoffState.applyEnqueued(env.data?.queue ?? []);
        return;
      case EventType.HANDOFF_CONSUMED:
        handoffState.applyConsumed(env.data?.item ?? null);
        return;
      case EventType.HANDOFF_CLEARED:
        handoffState.applyCleared();
        return;
      // P8.3 托管会话（docs/P8.3 §3.4）。三个 hint 事件维护状态条；ended 追系统气泡。
      case EventType.MANAGED_SESSION_STARTED:
        managedSession.onStarted(env.data ?? {});
        return;
      case EventType.MANAGED_SESSION_ADVANCED:
        managedSession.onAdvanced(env.data ?? {});
        return;
      case EventType.MANAGED_SESSION_ENDED: {
        const reason = env.data?.reason;
        managedSession.onEnded(env.data ?? {});
        appendBubble({ kind: "system", text: formatManagedSessionNotice(reason) });
        return;
      }
      case EventType.GUEST_CAPS_INFO: {
        // view 由请求方经 envelope 回声带回 —— 多条 /tools / /skills 并发时各响应
        // 自带"查的是哪段"，不靠全局态对号入座。
        const data = env.data ?? {};
        appendBubble({ kind: "system", text: formatGuestCaps(data, data.view) });
        return;
      }
      case EventType.ROOM_EXPORT: {
        const markdown = env.data?.markdown;
        const filename = env.data?.filename || "chahua-export.md";
        if (typeof markdown !== "string") return;
        triggerDownload(
          new Blob([markdown], { type: "text/markdown;charset=utf-8" }),
          filename,
        );
        setStatus("ok", `已导出 ${filename}`);
        return;
      }
      case EventType.FILE_DOWNLOAD: {
        // 失败路径：NOTICE 已弹过 alert，envelope 仅含 {rel, error}，跳过 Blob 流程。
        const d = env.data || {};
        if (d.error) return;
        const b64 = d.content_b64;
        const name = d.name || "download";
        if (typeof b64 !== "string") return;
        // base64 → Uint8Array → Blob。atob 一次性解码 ~200MB 在 Chromium 还稳；超大
        // base64 字符串在 ws 入站早就被 _DOWNLOAD_MAX_BYTES 拦了。
        let bin;
        try {
          bin = atob(b64);
        } catch (e) {
          setStatus("error", `下载「${name}」失败：base64 解码出错`);
          return;
        }
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
        // 走 application/octet-stream 让浏览器把它当下载而非 inline preview。具体 mime
        // 类型由扩展名决定，留给操作系统打开时自己识别。
        triggerDownload(
          new Blob([bytes], { type: "application/octet-stream" }),
          name,
        );
        setStatus("ok", `已下载「${name}」`);
        return;
      }
      // guest_thinking / tool_* 暂时静默。
    }
  }

  return { handleEnvelope };
}
