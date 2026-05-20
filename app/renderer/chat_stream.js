// 主聊天区消息流渲染（茶客 / 用户气泡 + 流式 message_start/delta/end）。
//
// P6.x 重构：从 renderer.js 抽出。本模块只关消息流 DOM —— 任务 chip、消息 filter、
// 打分写 sidebar、连接生命周期，都在 renderer.js 持有。
//
// 工厂模式与 createDebugPanel / createTaskPanel 同口径。依赖通过参数注入：DOM 容器
// （messagesEl）、头像构造（makeAvatar / makeUserAvatar，闭包 guests / userAvatar 状态）、
// 滚动锚（stickToBottom）、消息挂上后的 hook（afterAppendMessage：单出口刷 chip /
// 应用 filter，将来加搜索高亮 / 已读标记等也只动 renderer.js 那一处）。

import { Status } from "./events.js";
import {
  attachCopyButton,
  attachReviewButton,
  isSelectionInside,
  removeStreamingCursor,
  renderGuestText,
  renderMarkdown,
  setStatusTail,
} from "./chat_view.js";

// 与 chahua/user_md.py::USER_SPEAKER_ID 同源；transcript.jsonl 里用户发言的
// speaker_id 字段值。
const USER_SPEAKER_ID = "user";

export function createChatStream({
  messagesEl,
  makeAvatar,
  makeUserAvatar,
  stickToBottom,
  afterAppendMessage,
  onRequestReview,
}) {
  // 流式 in-flight 状态：message_id → { textEl, li, bubble, accumulated }。
  // 由本模块封装；外部只通过 :func:`closeInFlightOnDisconnect` 一键清空。
  const inFlight = new Map();

  // 茶客行（speaker bubble + avatar）。streaming=true 时气泡末尾挂闪烁 ``.streaming-cursor``。
  // 返回 li / textEl / bubble —— textEl 上由调用方 ``innerHTML = renderMarkdown(...)``
  // 整段重渲；cursor 与状态尾走 sibling 节点，避开 innerHTML 替换的擦除。
  //
  // 打分不挂在气泡里 —— 沿 sidebar 茶客名右侧显示（renderer.applyScoresToSidebar）。
  function makeGuestRow(speaker, { streaming = false, messageId = null, taskId = null, review = true } = {}) {
    const li = document.createElement("li");
    li.className = "msg";
    if (messageId) li.dataset.messageId = messageId;
    if (taskId) li.dataset.taskId = taskId;
    li.dataset.speaker = speaker;
    li.appendChild(makeAvatar(speaker, "msg-avatar"));
    const bubble = document.createElement("div");
    bubble.className = "bubble bubble-guest";
    const header = document.createElement("div");
    header.className = "bubble-header";
    const s = document.createElement("span");
    s.className = "speaker";
    s.textContent = speaker;
    header.appendChild(s);
    bubble.appendChild(header);
    const textEl = document.createElement("div");
    textEl.className = "text markdown";
    bubble.appendChild(textEl);
    if (streaming) {
      const cursor = document.createElement("span");
      cursor.className = "streaming-cursor";
      bubble.appendChild(cursor);
    }
    li.appendChild(bubble);
    // streaming 期间 message_id 已知，但 server 尚未把消息 append 进 transcript ——
    // 此刻挂「请审…」会让用户在长流式里点出 `_inbound_handoff_review` 的 ID 不存在
    // 错误。流式气泡的请审按钮延后到 endStreamingMessage（仅 OK 时消息落盘）。
    // `review=false` 让 cancelled/error 气泡也不挂 —— 那些 message_id 同样不进 transcript。
    if (!streaming && review) attachReviewButton(bubble, messageId, onRequestReview);
    return { li, textEl, bubble };
  }

  // 用户行：右对齐气泡 + 头像（无名字 —— 自己看自己 redundant）。镜像茶客布局：
  // 茶客是 [头像][气泡]，用户是 [气泡][头像]。
  function makeUserRow(text, { messageId = null, taskId = null } = {}) {
    const li = document.createElement("li");
    li.className = "user";
    if (messageId) li.dataset.messageId = messageId;
    if (taskId) li.dataset.taskId = taskId;
    li.dataset.speaker = USER_SPEAKER_ID;
    const bubble = document.createElement("div");
    bubble.className = "bubble bubble-user";
    const t = document.createElement("span");
    t.className = "text";
    t.textContent = text;
    bubble.appendChild(t);
    attachCopyButton(bubble, () => text);
    attachReviewButton(bubble, messageId, onRequestReview);
    li.appendChild(bubble);
    li.appendChild(makeUserAvatar("msg-avatar"));
    return li;
  }

  // 居中浮签式系统气泡 —— 不带头像 / 不带 speaker / 不带 message_id；本地 UI 提示
  // 专用（``/help`` 输出、未来其它 client-only 信息）。**不进 transcript** 也不与
  // server envelope 流耦合，刷新 / 切房 / clear 都会消失。
  function makeSystemRow(text) {
    const li = document.createElement("li");
    li.className = "sys";
    const bubble = document.createElement("div");
    bubble.className = "bubble bubble-system";
    const t = document.createElement("div");
    t.className = "text markdown";
    t.innerHTML = renderMarkdown(text);
    bubble.appendChild(t);
    li.appendChild(bubble);
    return li;
  }

  function appendBubble({ speaker, text, kind, messageId = null, taskId = null }) {
    stickToBottom(() => {
      let li;
      if (kind === "user") {
        li = makeUserRow(text, { messageId, taskId });
      } else if (kind === "system") {
        li = makeSystemRow(text);
      } else {
        const row = makeGuestRow(speaker, { messageId, taskId });
        renderGuestText(row, text);
        if (kind === "error") row.li.classList.add("error");
        li = row.li;
      }
      messagesEl.appendChild(li);
      afterAppendMessage(li);
    });
  }

  function startStreamingMessage(env) {
    stickToBottom(() => {
      const speaker = env.guest_name || "?";
      const taskId = env.data?.task_id ?? null;
      const { li, textEl, bubble } = makeGuestRow(speaker, {
        streaming: true,
        messageId: env.message_id,
        taskId,
      });
      messagesEl.appendChild(li);
      afterAppendMessage(li);
      // accumulated 累积完整 markdown 源 —— 每个 delta 整段重渲 innerHTML，
      // 因为 markdown 局部 patch（增量解析 + DOM diff）实现成本远大于聊天量级的全渲耗时。
      const entry = { textEl, li, bubble, accumulated: "" };
      inFlight.set(env.message_id, entry);
      // 闭包持 entry 引用，click 时读最新 accumulated（流式过程中也能复制到当前为止的全部）。
      attachCopyButton(bubble, () => entry.accumulated);
    });
  }

  function appendDelta(env) {
    const m = inFlight.get(env.message_id);
    if (!m) return;
    const chunk = env.data?.chunk ?? "";
    if (!chunk) return;
    m.accumulated += chunk;
    // 选区在 textEl 内 → 跳渲，让用户的拖选 / Cmd+C 不被擦；下一个 chunk 来时如果选区
    // 已解除，会一次性追上累积差额（accumulated 全量重渲）。
    if (isSelectionInside(m.textEl)) return;
    stickToBottom(() => {
      m.textEl.innerHTML = renderMarkdown(m.accumulated);
    });
  }

  function statusTail(env) {
    if (env.status === Status.CANCELLED) return "[中断]";
    if (env.status === Status.ERROR) {
      const err = env.data?.error || "未知错误";
      return `[出错：${err}]`;
    }
    return "";
  }

  function endStreamingMessage(env) {
    const m = inFlight.get(env.message_id);
    if (!m) {
      // server 没发过 message_start 直接 message_end（罕见但合法 —— 比如缓存命中
      // 整段一次性回）。OK 路径走 appendBubble；error 路径要挂 status-tail，appendBubble
      // 没那个口子，单独装一行。
      const speaker = env.guest_name || "?";
      const taskId = env.data?.task_id ?? null;
      if (env.status === Status.OK) {
        appendBubble({ speaker, text: env.data?.text ?? "", messageId: env.message_id, taskId });
        return;
      }
      const partial = env.data?.partial_text ?? "";
      // cancelled/error 的 message_id 不进 transcript —— 不挂请审按钮。
      const row = makeGuestRow(speaker, { messageId: env.message_id, taskId, review: false });
      renderGuestText(row, partial);
      row.li.classList.add("error");
      setStatusTail(row.bubble, statusTail(env));
      stickToBottom(() => {
        messagesEl.appendChild(row.li);
        afterAppendMessage(row.li);
      });
      return;
    }
    inFlight.delete(env.message_id);
    stickToBottom(() => {
      removeStreamingCursor(m.bubble);
      // 仅 OK 时消息已落 transcript，请审按钮此刻才有合法锚点（见 makeGuestRow）；
      // cancelled/error 的 message_id 不进 transcript，挂了点击必被 server 拒。
      if (env.status === Status.OK) {
        attachReviewButton(m.bubble, env.message_id, onRequestReview);
      }
      if (env.status !== Status.OK) {
        m.li.classList.add("error");
        setStatusTail(m.bubble, statusTail(env));
      }
      // 选区还在 textEl 内 —— 用户正在 copy。光标 / 状态尾 / error class 先就位，但
      // 最终 markdown 内容延后渲染到 selection 解除（一次性 selectionchange 监听）；
      // 否则 innerHTML 替换会瞬间擦掉选区，复制操作功亏一篑。
      // ``isConnected`` 是兜底：换房 / 清空 / 断线把 bubble 从 DOM 摘了，下一次任何
      // selectionchange 触发时本监听器自卸，避免持 m 引用 + 写到孤立节点。
      if (isSelectionInside(m.textEl)) {
        const finalize = () => {
          if (!m.textEl.isConnected) {
            document.removeEventListener("selectionchange", finalize);
            return;
          }
          if (isSelectionInside(m.textEl)) return;
          document.removeEventListener("selectionchange", finalize);
          m.textEl.innerHTML = renderMarkdown(m.accumulated);
        };
        document.addEventListener("selectionchange", finalize);
        return;
      }
      m.textEl.innerHTML = renderMarkdown(m.accumulated);
    });
  }

  // 进新房（首次连接 / 换房 / clear_room）时调 —— 仅清 Map，不挂"连接断开"尾。
  // 紧跟着 renderSidebar 会 ``messagesEl.replaceChildren()`` 把 DOM 一并清；这里只是
  // 把 in-flight 字典里的引用断掉，避免后续 stale message_id 命中老 entry。
  function clearInFlight() {
    inFlight.clear();
  }

  function closeInFlightOnDisconnect() {
    if (inFlight.size === 0) return;
    for (const m of inFlight.values()) {
      removeStreamingCursor(m.bubble);
      m.li.classList.add("error");
      setStatusTail(m.bubble, "[连接断开]");
    }
    inFlight.clear();
  }

  // 进 Room（首次连接 / 重连 / 换房）时全量回放。messagesEl 的清空由 renderSidebar
  // 一帧前完成（room_info 先于 room_history 到达），单点 owner 改一处即可。room_history
  // 先于 task_info 到达 —— 这里 li 上的 data-task-id 先就位；后续 task_info 触发的
  // refreshMessageTaskChips 会找到这些 li 并把 chip 挂上。
  function renderHistory(messages) {
    if (!Array.isArray(messages) || messages.length === 0) return;
    stickToBottom(() => {
      for (const m of messages) {
        const taskId = m.task_id ?? null;
        if (m.speaker_id === USER_SPEAKER_ID) {
          messagesEl.appendChild(makeUserRow(m.text, { messageId: m.message_id, taskId }));
        } else {
          const row = makeGuestRow(m.speaker_id, { messageId: m.message_id, taskId });
          renderGuestText(row, m.text);
          messagesEl.appendChild(row.li);
        }
      }
      // 强制滚到底 —— stickToBottom 在 mutate 前 messagesEl 是空，stick=true，自动定到底。
    });
  }

  return {
    appendBubble,
    startStreamingMessage,
    appendDelta,
    endStreamingMessage,
    clearInFlight,
    closeInFlightOnDisconnect,
    renderHistory,
  };
}
