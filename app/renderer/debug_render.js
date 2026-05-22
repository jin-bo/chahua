"use strict";

// 调试抽屉的 turn 渲染树 —— 把内部 turnRecord 渲成一个 ``<section>`` DOM 子树。
// 从 debug_panel.js 抽出（debug_panel 重构）。
//
// 纯函数：只吃 ``(turnId, turn)`` record、只调本模块 + debug_helpers 的纯构件，
// 不碰 debug_panel 的闭包状态（turns / turnNodes / dirty …）。实时 turn 与历史
// 详情共用同一棵 —— renderTurn 是唯一对外出口，rerenderTurn / swapRowToDetail /
// handleTurnStart 三处调用。
//
// turnRecord 形态见 debug_panel.js §内部数据形态。

import { ScoreKind } from "./events.js";
import {
  HANDOFF_NOTE_TEXT,
  scoringPathLabel,
  isHandoffPath,
  formatTs,
  formatScore,
  makeBadge,
  makePre,
  appendLabeledPre,
  appendLabeledDisabled,
} from "./debug_helpers.js";

export function renderTurn(turnId, turn) {
  const section = document.createElement("section");
  section.className = "debug-turn";
  section.dataset.turnId = turnId;

  const header = document.createElement("div");
  header.className = "debug-turn-header";
  header.title = "点击折叠 / 展开";
  // 点击 header 切 ``.debug-turn-collapsed`` —— 整 section body 全收起，只剩 header。
  // 实时 + 历史详情通吃；不抢内部 ``<details>``（候选 / 消息）的独立点击。
  header.addEventListener("click", () => {
    section.classList.toggle("debug-turn-collapsed");
  });
  const chevron = document.createElement("span");
  chevron.className = "debug-turn-chevron";
  chevron.textContent = "▾"; // ▾ 折叠态 CSS 旋转 -90deg 成 ▸
  header.appendChild(chevron);
  const ts = document.createElement("span");
  ts.className = "debug-turn-ts";
  ts.textContent = formatTs(turn.ts_ms);
  header.appendChild(ts);

  // 折叠态徽标：scoring_path（与 .debug-history-path 同款配色）。展开态由 CSS 隐藏。
  if (turn.scoring_path) {
    const path = document.createElement("span");
    path.className = "debug-turn-path";
    path.dataset.path = turn.scoring_path;
    path.textContent = scoringPathLabel(turn.scoring_path);
    header.appendChild(path);
  }

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
  // 折叠态用同一段 winner names 渲为纯文本（与 .debug-history-winners 同款）；
  // 展开态由 CSS 隐藏 —— 让折叠/展开走同一份 DOM，避免重渲两套结构。
  const winnersText = document.createElement("span");
  winnersText.className = "debug-turn-winners-text";
  winnersText.textContent = winnerNames.size > 0
    ? [...winnerNames].join(", ")
    : "（无人接话）";
  winnersWrap.appendChild(winnersText);
  header.appendChild(winnersWrap);

  // 折叠态消息数（与 .debug-history-n 同款）。展开态由 CSS 隐藏。
  if (turn.messages.size > 0) {
    const n = document.createElement("span");
    n.className = "debug-turn-n";
    n.textContent = `${turn.messages.size} 条`;
    header.appendChild(n);
  }

  section.appendChild(header);

  // handoff turn 顶部提示条，与打分驱动的 turn 一眼区分。进 section body（非
  // header）—— 折叠态随 body 一起收起。未知 handoff_* path 退回短标拼。
  if (isHandoffPath(turn.scoring_path)) {
    const note = document.createElement("div");
    note.className = "debug-turn-handoff-note";
    note.textContent = HANDOFF_NOTE_TEXT[turn.scoring_path]
      || "由用户" + scoringPathLabel(turn.scoring_path);
    section.appendChild(note);
  }

  // P9：兜底建桩的 turn（切回房间时已在后台进行）—— 顶部提示「明细不全」，
  // 避免用户把空候选当成打分坏了。
  if (turn.partial) {
    const note = document.createElement("div");
    note.className = "debug-turn-partial-note";
    note.textContent =
      "切回房间时这一轮已在后台进行 —— 切回前的打分候选 / 早前消息明细可能不全；"
      + "完整取证见 debug/turns.jsonl（本轮结束后下次切房即补全）";
    section.appendChild(note);
  }

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
