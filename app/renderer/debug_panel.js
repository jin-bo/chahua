"use strict";

// 调试抽屉 —— 实时视图（docs/P6 §8） + 历史 turn 索引（docs/P6.3.A）。
//
// 与 task_panel.js 互斥占用右侧 ``<aside>`` 槽位。
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
//
// P6.3.A 历史 turn：``room_history.data.turns_index`` 倒序投影（≤ 1000 条）注入
// ``historicalIndex`` 数组 —— 渲为轻量索引行（一行高），常驻不参与 evict。点击行
// 触发 ``FETCH_TURN_DETAIL`` inbound，回 ``TURN_DETAIL`` envelope 后把同一个 ``<li>``
// 内容换成详情卡片（详情进 ``turns`` Map，与实时 turn 共用 ``MAX_TURNS_IN_MEMORY``
// 上限）。详情被 evict → 还原成轻量索引行（``<li>`` 节点不删，保留点击复拉入口，
// docs §6.1 §10 评审第 1+3 条承重墙）。
//
// turn 渲染树（``renderTurn`` 及其子函数）见 ./debug_render.js；纯工具（格式化 /
// 小 DOM 构件 / 工具源识别）见 ./debug_helpers.js。本文件留实时 + 历史的有状态
// 装配（``turns`` / ``turnNodes`` / 历史索引共享同一套 evict）。

import { EventType, Inbound, ScoreKind, Status } from "./events.js";
import { renderTurn } from "./debug_render.js";
import {
  formatTs,
  scoringPathLabel,
  formatHistoryWinners,
  deriveArtifactPath,
  classifyToolSource,
} from "./debug_helpers.js";

const MAX_TURNS_IN_MEMORY = 50;

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

export function createDebugPanel({ panelEl, bodyEl, clearBtnEl, sendInbound }) {
  const turns = new Map();             // turn_id → turnRecord（实时 + 已展开的历史详情共用）
  const turnNodes = new Map();         // turn_id → DOM <section>（详情卡片 / 实时 turn 节点）
  // message_id 跨多 envelope 但 turn_id 只在 MESSAGE_START 出现 —— 后续 MESSAGE_DELTA /
  // TOOL_START / MESSAGE_END 都靠这张反查表回到所属 turn。
  const messageTurn = new Map();       // message_id → turn_id
  // 缓存最近一次 rerender 的内容；isVisible=false 时跳渲，show() 时把缓的 turn 一次性
  // 重灌。dirty 集合记"自上次 visible 后改过哪些 turn"，下次 show 只刷它们。
  const dirty = new Set();
  // room_history 来的轻量索引（倒序，≤ 1000 条）+ 索引行 DOM 反查表。
  // 索引行常驻不参与 evict（轻量 ~200B/条，1000 条仍仅 ~200KB DOM）；详情走
  // FETCH_TURN_DETAIL 后展开，详情被 evict 时还原成轻量索引行（li 节点不删）。
  const historicalIndex = [];          // [{turn_id, ts_ms, winners, ...}]
  const indexRows = new Map();         // turn_id → <li>
  // 去重并发 fetch（双击 / 慢网下重复点）；TURN_DETAIL 回来后清。
  const inflightFetches = new Set();

  if (clearBtnEl) {
    clearBtnEl.addEventListener("click", () => {
      reset();
    });
  }

  renderEmpty();

  function renderEmpty() {
    bodyEl.replaceChildren();
    if (turns.size === 0 && historicalIndex.length === 0) {
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
    historicalIndex.length = 0;
    indexRows.clear();
    inflightFetches.clear();
    renderEmpty();
  }

  function show() {
    panelEl.hidden = false;
    // 隐藏时累积的 dirty turn 这次一次性刷出来（visibility gate）。
    for (const turnId of dirty) rerenderTurn(turnId);
    dirty.clear();
    // 历史索引：隐藏期 applyTurnsIndex 只把数据塞进 historicalIndex（renderHistoricalIndex
    // 早 return），show 时一次性把 DOM 渲出来。
    if (historicalIndex.length > 0 && bodyEl.querySelectorAll("li.debug-history-row, li.debug-history-detail").length === 0) {
      renderHistoricalIndex();
    }
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
      case EventType.TURN_DETAIL:
        return handleTurnDetail(env);
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
      // 折叠态徽标用；与 historicalIndex[i].scoring_path 同源。
      scoring_path: typeof data.scoring_path === "string" ? data.scoring_path : null,
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
    // 长会话内存兜底；实时 turn + 历史详情共用 MAX_TURNS_IN_MEMORY。Map 保插入顺序，
    // 最早进的 turn 在 keys() 头部 —— LRU 口径。被 evict 的若是 fromHistory 详情，
    // **不删 DOM 节点** —— 把 <li> 内容还原回轻量索引行（保留点击复拉入口）；实时
    // turn 仍走整节点 remove。
    while (turns.size > MAX_TURNS_IN_MEMORY) {
      const oldest = turns.keys().next().value;
      if (!oldest) break;
      const record = turns.get(oldest);
      turns.delete(oldest);
      for (const [mid, tid] of messageTurn) {
        if (tid === oldest) messageTurn.delete(mid);
      }
      dirty.delete(oldest);
      if (record && record.fromHistory && record.indexEntry) {
        // turnNodes 该项要删 —— 否则后续 turnNodes.has(oldest) 误判这条详情仍在内存。
        swapRowBackToIndex(oldest, record.indexEntry);
        turnNodes.delete(oldest);
      } else {
        const node = turnNodes.get(oldest);
        if (node) node.remove();
        turnNodes.delete(oldest);
      }
    }
  }

  // P9：切回一个 turn 在后台续跑的房间 —— 该 turn 的 turn_start（乃至当下在打字
  // 那条消息的 message_start）已在后台被丢弃（后台只推里程碑）。下面两个 ensure*
  // 据切回后到达的事件兜底建桩，让当前 turn 仍在调试视图里成形：turn 标 partial
  // （切回前的打分候选 / prompt 拿不到），完整取证仍在 debug/turns.jsonl，下次切房
  // turns_index 会带出完整记录。
  function ensureTurn(turnId) {
    let turn = turns.get(turnId);
    if (turn) return turn;
    turn = {
      ts_ms: Date.now(),
      scores: [],
      scoring_prompts: null,
      scoring_path: null,
      messages: new Map(),
      task_id: null,
      partial: true,
    };
    turns.set(turnId, turn);
    evictOldest();
    rerenderTurn(turnId);
    return turn;
  }

  // 解析 envelope 所属的 {turn, msg} 记录；turn / message 桩缺失即兜底建。
  // message_id / turn_id 任一缺 → null（调用方跳过）。
  function ensureMessage(env) {
    const messageId = env.message_id;
    if (!messageId) return null;
    const turnId = messageTurn.get(messageId) || env.turn_id;
    if (!turnId) return null;
    const turn = ensureTurn(turnId);
    let msg = turn.messages.get(messageId);
    if (!msg) {
      msg = {
        guest: env.guest_name || "?",
        speak_prompt: null,
        tool_calls: new Map(),
        artifact_paths: new Set(),
        status: "running",
        seq: null,
      };
      turn.messages.set(messageId, msg);
      messageTurn.set(messageId, turnId);
    }
    return { turnId, turn, msg };
  }

  function handleMessageStart(env) {
    const turnId = env.turn_id;
    const messageId = env.message_id;
    if (!turnId || !messageId) return;
    const turn = ensureTurn(turnId);
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
    const found = ensureMessage(env);
    if (!found) return;
    const { turnId, msg } = found;
    msg.status = env.status || Status.OK;
    msg.seq = typeof env.seq === "number" ? env.seq : null;
    rerenderTurn(turnId);
  }

  function handleToolStart(env) {
    const found = ensureMessage(env);
    if (!found) return;
    const { turnId, turn, msg } = found;
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
    const found = ensureMessage(env);
    if (!found) return;
    const { turnId, msg } = found;
    const data = env.data || {};
    const callId = data.call_id;
    // call_id 缺时无法合并 —— 跳过（不引入"按 tool 名匹配最近一条 started"的脆弱启发式）。
    if (!callId) return;
    let entry = msg.tool_calls.get(callId);
    if (!entry) {
      // P9：tool_start 在后台路由被白名单滤掉（切回房间前那位茶客正调工具）——
      // 用 tool_complete 自带字段补一个 stub 条目，避免整条工具调用从调试面板蒸发。
      // args 在 tool_complete envelope 里没有 —— stub 不显示参数，可接受。
      const tool = data.tool || "";
      const { source, mcp_server } = classifyToolSource(tool);
      entry = {
        tool,
        args: undefined,
        source,
        mcp_server,
        status: "started",
        duration_ms: null,
        error: null,
      };
      msg.tool_calls.set(callId, entry);
    }
    entry.status = data.status || "ok";
    entry.duration_ms = typeof data.duration_ms === "number" ? data.duration_ms : null;
    entry.error = data.error || null;
    rerenderTurn(turnId);
  }

  // ── 历史 turn 索引 ──────────────────────────────────────────────────

  function applyTurnsIndex(index) {
    // room_history 重发触发：先清旧索引行 DOM（详情卡片走 turns Map / turnNodes 各自
    // 通过 reset() 已清，applyTurnsIndex 紧跟 reset 在 ROOM_HISTORY 分支调）。
    historicalIndex.length = 0;
    indexRows.clear();
    if (!Array.isArray(index)) return;
    historicalIndex.push(...index);
    renderHistoricalIndex();
  }

  function renderHistoricalIndex() {
    // 索引行紧随实时 turn 节点之后渲染。当前实现：每次重渲一遍 —— historicalIndex
    // 只在 room_history 帧来时变（应用启动 / 切房 / clear_room）；DOM 节点 ~1000 个
    // 一行 li，Electron 单 <aside> 容得下。
    if (!isVisible()) return;
    // 清空"等待第一轮发生…"占位（如果还在）。
    const empty = bodyEl.querySelector(".debug-empty");
    if (empty) empty.remove();
    // 删旧索引行（实时 turn / 详情卡片不动）。
    for (const li of bodyEl.querySelectorAll("li.debug-history-row")) {
      li.remove();
    }
    if (historicalIndex.length === 0) return;
    // 容器若不存在则建。ul 用 li 直接当索引行；后续详情 swap 时复用同一 <li>。
    let ul = bodyEl.querySelector("ul.debug-history");
    if (!ul) {
      ul = document.createElement("ul");
      ul.className = "debug-history";
      bodyEl.appendChild(ul);
    } else {
      ul.replaceChildren();
    }
    for (const entry of historicalIndex) {
      const li = makeIndexRow(entry);
      indexRows.set(entry.turn_id, li);
      ul.appendChild(li);
    }
  }

  function makeIndexRow(entry) {
    const li = document.createElement("li");
    li.className = "debug-history-row";
    li.dataset.turnId = entry.turn_id;
    fillIndexRowContent(li, entry);
    li.addEventListener("click", () => {
      // 详情已展开（实时 / 之前 fetch 过仍在 turns 内）— 不重复 fetch；点击就跳出。
      if (turns.has(entry.turn_id)) return;
      fetchTurnDetail(entry.turn_id);
    });
    return li;
  }

  function fillIndexRowContent(li, entry) {
    li.replaceChildren();
    li.classList.add("debug-history-row");
    li.classList.remove("debug-history-detail");
    const ts = document.createElement("span");
    ts.className = "debug-history-ts";
    ts.textContent = formatTs(entry.ts_ms);
    li.appendChild(ts);
    // scoring_path 徽标 —— mention / broadcast / scoring / handoff_delegate 视觉区分。
    if (entry.scoring_path) {
      const path = document.createElement("span");
      path.className = "debug-history-path";
      path.dataset.path = entry.scoring_path;
      path.textContent = scoringPathLabel(entry.scoring_path);
      li.appendChild(path);
    }
    const w = document.createElement("span");
    w.className = "debug-history-winners";
    w.textContent = formatHistoryWinners(entry);
    li.appendChild(w);
    if (typeof entry.n_messages === "number" && entry.n_messages > 0) {
      const n = document.createElement("span");
      n.className = "debug-history-n";
      n.textContent = `${entry.n_messages} 条`;
      li.appendChild(n);
    }
  }

  function fetchTurnDetail(turnId) {
    if (!turnId) return;
    if (turns.has(turnId)) return;            // 已加载（实时 / 历史详情）
    if (inflightFetches.has(turnId)) return;  // 去重并发
    if (typeof sendInbound !== "function") return;
    inflightFetches.add(turnId);
    // 视觉反馈：行变 loading 状态；TURN_DETAIL 回来后 swap 走详情卡片。
    const li = indexRows.get(turnId);
    if (li) li.classList.add("debug-history-loading");
    sendInbound({ type: Inbound.FETCH_TURN_DETAIL, turn_id: turnId });
  }

  function handleTurnDetail(env) {
    const turnId = env.turn_id;
    if (!turnId) return;
    inflightFetches.delete(turnId);
    const li = indexRows.get(turnId);
    if (li) li.classList.remove("debug-history-loading");
    const data = env.data || {};
    if (!data.found) {
      // rotation 已清 / 服务端找不到 —— 整行 DOM 移除（前端索引也跟着剪）。
      if (li) li.remove();
      indexRows.delete(turnId);
      const idx = historicalIndex.findIndex((e) => e && e.turn_id === turnId);
      if (idx >= 0) historicalIndex.splice(idx, 1);
      return;
    }
    const indexEntry = historicalIndex.find((e) => e && e.turn_id === turnId)
                       || { turn_id: turnId, ts_ms: 0, winners: [] };
    const record = buildHistoricalRecord(data.turn || {}, data.prompts || {});
    record.fromHistory = true;
    record.indexEntry = indexEntry;
    turns.set(turnId, record);
    swapRowToDetail(turnId, record);
    // 先 swap 再 evict —— evictOldest 是 LRU，把 oldestId 出列；先 swap 让本次详情
    // 进 turns Map 不被自己挤出。
    evictOldest();
  }

  function swapRowToDetail(turnId, record) {
    const li = indexRows.get(turnId);
    if (!li) return;
    li.replaceChildren();
    li.classList.add("debug-history-detail");
    li.classList.remove("debug-history-row");
    // 详情卡片直接挂在 li 内，与实时 turn 用同款 renderTurn。
    const section = renderTurn(turnId, record);
    li.appendChild(section);
    turnNodes.set(turnId, section);
  }

  function swapRowBackToIndex(turnId, indexEntry) {
    const li = indexRows.get(turnId);
    if (!li) return;
    fillIndexRowContent(li, indexEntry);
  }

  // 历史 ``turn`` 行 → 内部 turnRecord（与实时 record 同形态，附 ``fromHistory`` /
  // ``indexEntry`` 让 evictOldest 走"还原索引行"分支）。
  //
  // 服务端 ``turns.jsonl`` 行的字段 schema 见 chahua/debug_recorder.py§数据模型。
  function buildHistoricalRecord(turn, prompts) {
    const record = {
      ts_ms: typeof turn.ts_ms === "number" ? turn.ts_ms : 0,
      scores: [],
      scoring_prompts: null,
      // jsonl 行里 scoring_path 是顶级字段（debug_recorder.py§record_scoring）。
      scoring_path: typeof turn.scoring_path === "string" ? turn.scoring_path : null,
      messages: new Map(),
      task_id: typeof turn.task_id === "string" ? turn.task_id : null,
    };
    const scoring = turn.scoring || {};
    const results = Array.isArray(scoring.results) ? scoring.results : [];
    // 投影 results → scores（与实时 turn_start.data.scores 同形态）。
    record.scores = results.map((r) => ({
      guest_name: r.guest || "?",
      score: typeof r.score === "number" ? r.score : 0,
      kind: r.kind || ScoreKind.SCORED,
      raw: r.raw || "",
    }));
    // 拼 scoring_prompts: { guest_name: prompt 文本 }。prompts 字段始终存在；单 key
    // 三重满足才出现。任一 key 缺 → 整 scoring_prompts 仍可能非空（部分茶客的
    // prompt 在），缺的那位走"prompt 不可用"提示分支。
    const scoringPrompts = {};
    let hasAny = false;
    for (const r of results) {
      const rel = r.prompt_file;
      if (rel && typeof prompts[rel] === "string") {
        scoringPrompts[r.guest || "?"] = prompts[rel];
        hasAny = true;
      }
    }
    record.scoring_prompts = hasAny ? scoringPrompts : null;
    // messages：与实时 record 同形态。tool_calls 走 Map 保插入顺序。
    const msgs = Array.isArray(turn.messages) ? turn.messages : [];
    for (const m of msgs) {
      const mid = typeof m.message_id === "string" ? m.message_id : null;
      if (!mid) continue;
      const speakRel = m.speak_prompt_file;
      const speakPrompt = speakRel && typeof prompts[speakRel] === "string"
        ? prompts[speakRel]
        : null;
      const tool_calls = new Map();
      const toolList = Array.isArray(m.tool_calls) ? m.tool_calls : [];
      for (const t of toolList) {
        const cid = t.call_id || `_anon_${tool_calls.size}`;
        tool_calls.set(cid, {
          tool: t.tool || "",
          args: t.args,
          source: t.source || "unknown",
          mcp_server: t.mcp_server || null,
          status: t.status || "ok",
          duration_ms: typeof t.duration_ms === "number" ? t.duration_ms : null,
          error: t.error || null,
        });
      }
      record.messages.set(mid, {
        guest: m.guest || "?",
        speak_prompt: speakPrompt,
        tool_calls,
        artifact_paths: new Set(
          Array.isArray(m.artifact_paths) ? m.artifact_paths : []
        ),
        status: m.status || Status.OK,
        seq: typeof m.seq === "number" ? m.seq : null,
      });
    }
    return record;
  }

  // ── 重渲 ─────────────────────────────────────────────────────────────

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

  return {
    onEnvelope,
    reset,
    show,
    hide,
    isVisible,
    applyTurnsIndex,
  };
}
