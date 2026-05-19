"use strict";

// 调试抽屉 —— 实时视图（docs/P6 §8）。
//
// 与 task_panel.js 互斥占用右侧 ``<aside>`` 槽位。**只看实时本会话**：消费 envelope
// 流（``TURN_START`` / ``MESSAGE_*`` / ``TOOL_*``），不读历史。
//
// 切房 / ``room_info`` 重发 → ``reset()`` 清状态；renderer.js 在 ``ROOM_INFO`` 分支
// 调一次。隐藏期 envelope 进 ``turns`` Map 但不渲 DOM，``show()`` 时一次性补刷
// （visibility gate，避免后台 50× rerender 浪费）。
//
// 渲染契约：
// - 最新 turn 在顶（``insertBefore(firstChild)``）。
// - prompt 走 ``<details><pre>`` 默认折叠（不变量"prompt 在前端 <details> 默认折叠 +
//   隔离于聊天 DOM"，docs §不变量）—— 巨型 prompt 不进主 ``<ul id="messages">``。
// - ``capture_prompts=false`` 时 piggyback 字段缺，UI 显示"prompt 捕获已关"提示
//   而非空 ``<pre>``（区分"关了" vs "空字符串"两种语义）。
// - artifact 路径前端再派生一次（与后端 ``debug_recorder.record_artifact_path`` 同算法）；
//   envelope 里没有 artifact_paths 字段，前端不另开 inbound 拉。
// - ``MAX_TURNS_IN_MEMORY=50`` 兜底长会话内存。Map 保插入顺序，超额时丢最早进的。

import { EventType, ScoreKind, Status } from "./events.js";

// keep in sync with chahua/task_tools.py::TASK_WRITE_ARTIFACT_TOOL_NAME
const TASK_WRITE_ARTIFACT = "task_write_artifact";

const MAX_TURNS_IN_MEMORY = 50;

// 跟 .gitignore / docs §不变量同口径：仅 task_write_artifact 派生 artifact 路径。
// 后续若加新写盘工具按 tool 名扩；不挂 ArtifactDetector。
//
// name 校验镜像 ``tasks_store._validate_artifact_name``（``/`` / ``\`` / ``..`` /
// 前缀 ``.``）—— 非法名会被写盘层拒、不会落盘，前端记一条"幽灵 artifact"会误导
// 用户。后端 ``transport_bridge._maybe_record_artifact_path`` 同口径过滤。
function deriveArtifactPath(toolName, args, taskId) {
  if (toolName !== TASK_WRITE_ARTIFACT) return null;
  if (!taskId) return null;
  if (!args || typeof args !== "object") return null;
  const name = args.name;
  if (typeof name !== "string" || !name) return null;
  if (name.includes("/") || name.includes("\\") || name.includes("..")) return null;
  if (name.startsWith(".")) return null;
  return `tasks/${taskId}/artifacts/${name}`;
}

function formatTs(ms) {
  if (typeof ms !== "number" || !Number.isFinite(ms)) return "";
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function formatScore(score) {
  if (typeof score !== "number" || !Number.isFinite(score)) return "—";
  return score.toFixed(2);
}

function makeBadge(text, className = "") {
  const el = document.createElement("span");
  el.className = className;
  el.textContent = text;
  return el;
}

function makePre(text) {
  const pre = document.createElement("pre");
  pre.className = "debug-pre";
  pre.textContent = text;
  return pre;
}

function makeDisabledPromptHint() {
  const el = document.createElement("div");
  el.className = "debug-pre-disabled";
  el.textContent = "prompt 捕获已关（room.toml `[debug] capture_prompts = true` 重启后可见）";
  return el;
}

// "label + body" 是 candidate / message 渲染里出现 5 次的模板：一个小标签 + 一个
// pre/disabled 体块。集中在这里，渲染端调一行。
function appendLabeledPre(parent, label, text) {
  const lab = document.createElement("div");
  lab.className = "debug-pre-label";
  lab.textContent = label;
  parent.appendChild(lab);
  parent.appendChild(makePre(text));
}
function appendLabeledDisabled(parent, label) {
  const lab = document.createElement("div");
  lab.className = "debug-pre-label";
  lab.textContent = label;
  parent.appendChild(lab);
  parent.appendChild(makeDisabledPromptHint());
}

// ── 内部数据形态 ─────────────────────────────────────────────────────
//
// turns: Map<turn_id, {
//   ts_ms,
//   scores: [{guest_name, score, kind, raw}],
//   scoring_prompts: {guest_name: text} | null,
//   messages: Map<message_id, {
//     guest,
//     speak_prompt: string | null,   // null = capture_prompts=false
//     tool_calls: Map<call_id, {tool, args, source, mcp_server, status, duration_ms, error}>,
//     artifact_paths: Set<string>,
//     status: "running" | "ok" | "cancelled" | "error",
//     seq: number | null,
//   }>,
//   task_id: string | null,           // 从 message_start.data.task_id 推断
// }>

export function createDebugPanel({ panelEl, bodyEl, clearBtnEl }) {
  const turns = new Map();             // turn_id → turnRecord
  const turnNodes = new Map();         // turn_id → DOM <section>
  // message_id 跨多 envelope 但 turn_id 只在 MESSAGE_START 出现 —— 后续 MESSAGE_DELTA /
  // TOOL_START / MESSAGE_END 都靠这张反查表回到所属 turn。
  const messageTurn = new Map();       // message_id → turn_id
  // 缓存最近一次 rerender 的内容；isVisible=false 时跳渲，show() 时把缓的 turn 一次性
  // 重灌。dirty 集合记"自上次 visible 后改过哪些 turn"，下次 show 只刷它们。
  const dirty = new Set();

  if (clearBtnEl) {
    clearBtnEl.addEventListener("click", () => {
      reset();
    });
  }

  renderEmpty();

  function renderEmpty() {
    bodyEl.replaceChildren();
    if (turns.size === 0) {
      const empty = document.createElement("div");
      empty.className = "debug-empty";
      empty.textContent = "等待第一轮发生…";
      bodyEl.appendChild(empty);
    }
  }

  function reset() {
    turns.clear();
    turnNodes.clear();
    messageTurn.clear();
    dirty.clear();
    renderEmpty();
  }

  function show() {
    panelEl.hidden = false;
    // 隐藏时累积的 dirty turn 这次一次性刷出来（visibility gate）。
    for (const turnId of dirty) rerenderTurn(turnId);
    dirty.clear();
  }
  function hide() {
    panelEl.hidden = true;
  }
  function isVisible() {
    return !panelEl.hidden;
  }

  function onEnvelope(env) {
    switch (env.type) {
      case EventType.TURN_START:
        return handleTurnStart(env);
      case EventType.MESSAGE_START:
        return handleMessageStart(env);
      case EventType.MESSAGE_END:
        return handleMessageEnd(env);
      case EventType.TOOL_START:
        return handleToolStart(env);
      case EventType.TOOL_COMPLETE:
        return handleToolComplete(env);
      // TURN_END / MESSAGE_DELTA 当前不影响调试视图；MESSAGE_DELTA 字符级流不存价值
      // 太低（speak_prompt 已在 message_start.data；真有需求 P6.3 走 thinking_chunks 加 hook）。
    }
  }

  function handleTurnStart(env) {
    const turnId = env.turn_id;
    if (!turnId) return;
    const data = env.data || {};
    const scores = Array.isArray(data.scores) ? data.scores : [];
    const record = {
      ts_ms: Date.now(),
      scores,
      // piggyback 字段：``capture_prompts=false`` 时整字段缺，记 null 区分"关了" vs
      // "空 prompt"；前端 ``<details>`` 渲染据此显示禁用提示而非空 ``<pre>``。
      scoring_prompts: data.scoring_prompts && typeof data.scoring_prompts === "object"
        ? data.scoring_prompts
        : null,
      messages: new Map(),
      task_id: null,
    };
    turns.set(turnId, record);
    evictOldest();
    if (isVisible()) {
      const empty = bodyEl.querySelector(".debug-empty");
      if (empty) empty.remove();
      const node = renderTurn(turnId, record);
      turnNodes.set(turnId, node);
      bodyEl.insertBefore(node, bodyEl.firstChild);
    } else {
      dirty.add(turnId);
    }
  }

  function evictOldest() {
    // 长会话内存兜底；P6.3 历史 turn 进 room_history 之前先把"实时窗口"封顶。
    // Map 保插入顺序，最早进的 turn 在 keys() 头部。
    while (turns.size > MAX_TURNS_IN_MEMORY) {
      const oldest = turns.keys().next().value;
      if (!oldest) break;
      turns.delete(oldest);
      const node = turnNodes.get(oldest);
      if (node) node.remove();
      turnNodes.delete(oldest);
      for (const [mid, tid] of messageTurn) {
        if (tid === oldest) messageTurn.delete(mid);
      }
      dirty.delete(oldest);
    }
  }

  function handleMessageStart(env) {
    const turnId = env.turn_id;
    const messageId = env.message_id;
    if (!turnId || !messageId) return;
    const turn = turns.get(turnId);
    if (!turn) return;
    const data = env.data || {};
    // task_id 只在 message_start.data 里带（envelope 顶层不动）—— 取首条非 null 即够。
    if (turn.task_id == null && typeof data.task_id === "string") {
      turn.task_id = data.task_id;
    }
    const msg = {
      guest: env.guest_name || "?",
      // capture_prompts=false 时整字段缺 key；区分"关了" vs "空 prompt" 用 null。
      speak_prompt: typeof data.speak_prompt === "string" ? data.speak_prompt : null,
      tool_calls: new Map(),
      artifact_paths: new Set(),
      status: "running",
      seq: null,
    };
    turn.messages.set(messageId, msg);
    messageTurn.set(messageId, turnId);
    // 局部重渲该 turn —— 单次开销 O(候选数 + 已记 message 数)，量级小（≤ 5 + ≤ 3）。
    rerenderTurn(turnId);
  }

  function handleMessageEnd(env) {
    const turnId = messageTurn.get(env.message_id);
    if (!turnId) return;
    const turn = turns.get(turnId);
    const msg = turn && turn.messages.get(env.message_id);
    if (!msg) return;
    msg.status = env.status || Status.OK;
    msg.seq = typeof env.seq === "number" ? env.seq : null;
    rerenderTurn(turnId);
  }

  function handleToolStart(env) {
    const turnId = messageTurn.get(env.message_id);
    if (!turnId) return;
    const turn = turns.get(turnId);
    const msg = turn && turn.messages.get(env.message_id);
    if (!msg) return;
    const data = env.data || {};
    const tool = data.tool || "";
    const callId = data.call_id || `_anon_${msg.tool_calls.size}`;
    const { source, mcp_server } = classifyToolSource(tool);
    msg.tool_calls.set(callId, {
      tool,
      args: data.args,
      source,
      mcp_server,
      status: "started",
      duration_ms: null,
      error: null,
    });
    // 前端再做一次派生（与后端 transport_bridge._maybe_record_artifact_path 同算法）。
    const path = deriveArtifactPath(tool, data.args, turn.task_id);
    if (path) msg.artifact_paths.add(path);
    rerenderTurn(turnId);
  }

  function handleToolComplete(env) {
    const turnId = messageTurn.get(env.message_id);
    if (!turnId) return;
    const turn = turns.get(turnId);
    const msg = turn && turn.messages.get(env.message_id);
    if (!msg) return;
    const data = env.data || {};
    const callId = data.call_id;
    // call_id 缺时无法合并 —— 跳过（不引入"按 tool 名匹配最近一条 started"的脆弱启发式）。
    if (!callId) return;
    const entry = msg.tool_calls.get(callId);
    if (!entry) return;
    entry.status = data.status || "ok";
    entry.duration_ms = typeof data.duration_ms === "number" ? data.duration_ms : null;
    entry.error = data.error || null;
    rerenderTurn(turnId);
  }

  // MCP 源识别 —— 与后端 debug_recorder._classify_tool_source 同算法（前端复刻，
  // 避免再开个 envelope 字段透传）。
  function classifyToolSource(tool) {
    if (typeof tool !== "string" || !tool) return { source: "unknown", mcp_server: null };
    if (tool.startsWith("mcp__")) {
      const rest = tool.slice("mcp__".length);
      const idx = rest.indexOf("__");
      const server = idx > 0 ? rest.slice(0, idx) : rest;
      return { source: "mcp", mcp_server: server || null };
    }
    return { source: "builtin", mcp_server: null };
  }

  // ── 渲染 ─────────────────────────────────────────────────────────────

  function rerenderTurn(turnId) {
    if (!isVisible()) {
      dirty.add(turnId);
      return;
    }
    const turn = turns.get(turnId);
    if (!turn) return;
    const oldNode = turnNodes.get(turnId);
    const node = renderTurn(turnId, turn);
    if (oldNode) {
      oldNode.replaceWith(node);
    } else {
      // turn 第一次 show 时（隐藏期内 handleTurnStart 没建过 DOM）插到顶部。
      const empty = bodyEl.querySelector(".debug-empty");
      if (empty) empty.remove();
      bodyEl.insertBefore(node, bodyEl.firstChild);
    }
    turnNodes.set(turnId, node);
  }

  function renderTurn(turnId, turn) {
    const section = document.createElement("section");
    section.className = "debug-turn";
    section.dataset.turnId = turnId;

    const header = document.createElement("div");
    header.className = "debug-turn-header";
    const ts = document.createElement("span");
    ts.className = "debug-turn-ts";
    ts.textContent = formatTs(turn.ts_ms);
    header.appendChild(ts);

    const winnersWrap = document.createElement("div");
    winnersWrap.className = "debug-turn-winners";
    // 真实 winners = 本 turn 实际发言者（``MESSAGE_START`` 的 guest 集合）。
    // envelope 不携带 orchestrator 的 ``winners`` 字段，``scores`` 包含全部候选（含
    // 0.1 落选者），所以不能从 scores 反推；从 messages 推导既准确（哪怕只有 @mention
    // 路由的确定性选手）又一致（pending 期 messages 为空，badge 自然不出现）。
    const winnerNames = new Set();
    for (const msg of turn.messages.values()) {
      if (msg && msg.guest) winnerNames.add(msg.guest);
    }
    for (const w of winnerNames) {
      winnersWrap.appendChild(makeBadge(w, "debug-winner-badge"));
    }
    header.appendChild(winnersWrap);
    section.appendChild(header);

    section.appendChild(renderCandidates(turn));

    if (turn.messages.size > 0) {
      section.appendChild(renderMessages(turn));
    }

    return section;
  }

  function renderCandidates(turn) {
    const det = document.createElement("details");
    det.className = "debug-section";
    det.open = false;
    const sum = document.createElement("summary");
    sum.textContent = `候选（${turn.scores.length}）`;
    det.appendChild(sum);

    const ul = document.createElement("ul");
    ul.className = "debug-candidates";
    for (const s of turn.scores) {
      ul.appendChild(renderCandidate(s, turn.scoring_prompts));
    }
    det.appendChild(ul);
    return det;
  }

  function renderCandidate(score, scoringPrompts) {
    const li = document.createElement("li");
    li.className = "debug-candidate";
    const row = document.createElement("div");
    row.className = "debug-candidate-row";
    const name = document.createElement("span");
    name.className = "debug-candidate-name";
    name.textContent = score.guest_name || "?";
    row.appendChild(name);
    const kind = document.createElement("span");
    kind.className = "debug-candidate-kind";
    kind.dataset.kind = score.kind || ScoreKind.SCORED;
    kind.textContent = score.kind || ScoreKind.SCORED;
    row.appendChild(kind);
    const sc = document.createElement("span");
    sc.className = "debug-candidate-score";
    sc.textContent = formatScore(score.score);
    row.appendChild(sc);
    li.appendChild(row);

    const det = document.createElement("details");
    const sum = document.createElement("summary");
    sum.textContent = "raw / prompt";
    det.appendChild(sum);

    if (score.raw) {
      appendLabeledPre(det, "Scoring response (raw)", score.raw);
    }
    if (scoringPrompts === null) {
      appendLabeledDisabled(det, "Scoring prompt");
    } else if (scoringPrompts && scoringPrompts[score.guest_name]) {
      appendLabeledPre(det, "Scoring prompt", scoringPrompts[score.guest_name]);
    }
    li.appendChild(det);
    return li;
  }

  function renderMessages(turn) {
    const det = document.createElement("details");
    det.className = "debug-section";
    det.open = true;
    const sum = document.createElement("summary");
    sum.textContent = `消息（${turn.messages.size}）`;
    det.appendChild(sum);

    const ul = document.createElement("ul");
    ul.className = "debug-messages";
    for (const [mid, msg] of turn.messages) {
      ul.appendChild(renderMessage(mid, msg));
    }
    det.appendChild(ul);
    return det;
  }

  function renderMessage(messageId, msg) {
    const li = document.createElement("li");
    li.className = "debug-message";
    li.dataset.messageId = messageId;

    const head = document.createElement("div");
    head.className = "debug-message-head";
    const guest = document.createElement("span");
    guest.className = "debug-message-guest";
    guest.textContent = msg.guest;
    head.appendChild(guest);
    const status = document.createElement("span");
    status.className = "debug-status";
    status.dataset.status = msg.status;
    status.textContent = msg.status;
    head.appendChild(status);
    li.appendChild(head);

    const det = document.createElement("details");
    const sum = document.createElement("summary");
    sum.textContent = "prompt / 工具 / 产物";
    det.appendChild(sum);

    if (msg.speak_prompt === null) {
      appendLabeledDisabled(det, "Speak prompt");
    } else {
      appendLabeledPre(det, "Speak prompt", msg.speak_prompt);
    }

    if (msg.tool_calls.size > 0) {
      const lab = document.createElement("div");
      lab.className = "debug-pre-label";
      lab.textContent = `工具调用（${msg.tool_calls.size}）`;
      det.appendChild(lab);
      const ul = document.createElement("ul");
      ul.className = "debug-tools";
      for (const [, t] of msg.tool_calls) {
        ul.appendChild(renderTool(t));
      }
      det.appendChild(ul);
    }

    if (msg.artifact_paths.size > 0) {
      const lab = document.createElement("div");
      lab.className = "debug-pre-label";
      lab.textContent = `产物（${msg.artifact_paths.size}）`;
      det.appendChild(lab);
      const ul = document.createElement("ul");
      ul.className = "debug-artifacts";
      for (const p of msg.artifact_paths) {
        const ali = document.createElement("li");
        ali.className = "debug-artifact";
        ali.textContent = p;
        ul.appendChild(ali);
      }
      det.appendChild(ul);
    }

    li.appendChild(det);
    return li;
  }

  function renderTool(t) {
    const li = document.createElement("li");
    li.className = "debug-tool";

    const head = document.createElement("div");
    head.className = "debug-tool-head";

    const name = document.createElement("span");
    name.className = "debug-tool-name";
    name.textContent = t.tool || "(unnamed)";
    head.appendChild(name);

    const src = document.createElement("span");
    src.className = "debug-tool-source";
    src.dataset.source = t.source;
    src.textContent = t.source === "mcp" && t.mcp_server
      ? `mcp:${t.mcp_server}`
      : t.source;
    head.appendChild(src);

    if (typeof t.duration_ms === "number") {
      const dur = document.createElement("span");
      dur.className = "debug-tool-duration";
      dur.textContent = `${t.duration_ms}ms`;
      head.appendChild(dur);
    }

    const st = document.createElement("span");
    st.className = "debug-tool-status";
    st.dataset.status = t.status;
    st.textContent = t.status;
    head.appendChild(st);

    li.appendChild(head);

    if (t.args !== undefined && t.args !== null) {
      // args 可能很大；rerenderTurn 每条 envelope 都会调一次本函数，缓在 entry 上
      // 让同一条 tool_call 后续渲染（complete 帧 / 邻位变更）省 stringify 开销。
      if (t._argsJson === undefined) t._argsJson = safeStringify(t.args);
      const pre = makePre(t._argsJson);
      pre.style.maxHeight = "120px";
      li.appendChild(pre);
    }
    if (t.error) {
      const err = document.createElement("div");
      err.className = "debug-tool-error";
      err.textContent = String(t.error);
      li.appendChild(err);
    }
    return li;
  }

  function safeStringify(v) {
    try {
      return JSON.stringify(v, null, 2);
    } catch {
      return String(v);
    }
  }

  return {
    onEnvelope,
    reset,
    show,
    hide,
    isVisible,
  };
}
