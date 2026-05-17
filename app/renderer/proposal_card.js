"use strict";

// P5.3.6 茶客 propose 卡片渲染 + 去重（docs/P5-任务房间.md §6.3）。
//
// 茶客通过 task_propose_decision / task_propose_open 工具发起的"提议"经服务端转发成
// TASK_PROPOSAL envelope（chahua/task_tools.py），前端在对应茶客气泡下渲染一张
// "采纳 / 忽略"卡片让用户决定是否真正入库。
//
// **写权限永远在用户**（CLAUDE.md "artifact / decision / task 的写权限只在用户"）——
// 卡片渲染不写库；采纳 = 拼回既有 ADD_DECISION / OPEN_TASK inbound，服务端走原 handler。
// 忽略 = 本地丢卡片，session-local（刷新即清，不持久化）。
//
// 去重折叠：按 (proposer, kind, payload-hash) —— 偏执 LLM 反复刷同一 propose 时只
// 渲染一张卡片，避免聊天流被 propose 灌爆。模块级 seen 集合在 ws 重连 / 切房间时不
// 清，刷新页面（reload renderer）才清；这是有意为之 —— 同一 session 内重发同一 propose
// 始终视为同一次。

import { EventType, Inbound, TaskProposalKind } from "./events.js";

// 同一 session 内已渲染过的 propose 指纹集合。模块级 → 页面刷新才清。
const _seen = new Set();

function stableStringify(obj) {
  // JSON.stringify 的字段顺序对 dedup 关键 —— 同一 payload 对象不同 key 顺序应该
  // 产同一指纹。简单做法：按 sort 后的 key 序列化。
  if (obj === null || typeof obj !== "object") return JSON.stringify(obj);
  if (Array.isArray(obj)) {
    return "[" + obj.map(stableStringify).join(",") + "]";
  }
  const keys = Object.keys(obj).sort();
  const parts = keys.map((k) => JSON.stringify(k) + ":" + stableStringify(obj[k]));
  return "{" + parts.join(",") + "}";
}

function dedupKey(data) {
  const proposer = String(data.proposer || "");
  const kind = String(data.kind || "");
  const payloadStr = stableStringify(data.payload ?? null);
  return `${proposer}|${kind}|${payloadStr}`;
}

function truncate(s, n) {
  if (typeof s !== "string") return "";
  return s.length <= n ? s : s.slice(0, n) + "…";
}

function renderPayloadPreview(kind, payload) {
  const box = document.createElement("div");
  box.className = "proposal-card-preview";
  if (kind === TaskProposalKind.DECISION) {
    const summary = truncate(payload.summary ?? "", 200);
    const sup = Array.isArray(payload.supporting_message_ids)
      ? payload.supporting_message_ids
      : [];
    const head = document.createElement("div");
    head.className = "proposal-card-kind";
    head.textContent = "提议决策";
    const body = document.createElement("div");
    body.className = "proposal-card-summary";
    body.textContent = summary;
    box.appendChild(head);
    box.appendChild(body);
    if (sup.length) {
      const refs = document.createElement("div");
      refs.className = "proposal-card-refs";
      refs.textContent = `引用 ${sup.length} 条消息`;
      box.appendChild(refs);
    }
  } else if (kind === TaskProposalKind.OPEN) {
    const title = truncate(payload.title ?? "", 60);
    const goal = truncate(payload.goal ?? "", 200);
    const head = document.createElement("div");
    head.className = "proposal-card-kind";
    head.textContent = "提议开新任务";
    const titleEl = document.createElement("div");
    titleEl.className = "proposal-card-title";
    titleEl.textContent = title;
    const goalEl = document.createElement("div");
    goalEl.className = "proposal-card-goal";
    goalEl.textContent = goal;
    box.appendChild(head);
    box.appendChild(titleEl);
    box.appendChild(goalEl);
  } else {
    // 未知 kind —— 兜底展示原始 JSON 让用户至少能看到，开发期捕错
    const head = document.createElement("div");
    head.className = "proposal-card-kind";
    head.textContent = `未知提议类型：${kind}`;
    box.appendChild(head);
  }
  return box;
}

function findHostBubble(messagesEl, messageId) {
  // 优先挂在 propose 工具被调那帧的茶客气泡下；server 端 transport bind 期内
  // emit_chahua 会带上 envelope.message_id，与茶客本轮消息同源。
  if (!messageId) return null;
  return messagesEl.querySelector(`li[data-message-id="${CSS.escape(messageId)}"]`);
}

// ── 工厂入口 ────────────────────────────────────────────────────────────────
//
// 创建一只 proposal card 渲染器实例。
//
//   messagesEl   消息流容器（用于按 messageId 找宿主气泡）。
//   sendInbound  传入 inbound 帧的发送闭包（renderer.js 持 ws，注入进来即可）；
//                P5.3.6 阶段卡片采纳按钮已具备但 inbound 帧组装在 P5.3.7 完成。
//
// 返回 ``{ onEnvelope(env) }``：renderer.js 在 EventType.TASK_PROPOSAL 路径上调一次。
export function createProposalCard({ messagesEl, sendInbound }) {
  function onEnvelope(env) {
    if (env.type !== EventType.TASK_PROPOSAL) return;
    const data = env.data || {};
    if (!data.kind || !data.payload || !data.proposer) return;

    const key = dedupKey(data);
    if (_seen.has(key)) return;
    _seen.add(key);

    const host = findHostBubble(messagesEl, env.message_id);
    if (!host) {
      // 茶客气泡不在视野（已被 clear_room 抹掉 / scroll 远？）：丢弃。
      // 提议是 hint，不持久化；找不到挂点不补 toast，避免在用户已切场景时干扰。
      return;
    }

    const card = document.createElement("div");
    card.className = "proposal-card";
    card.dataset.proposer = data.proposer;
    card.dataset.kind = data.kind;

    const meta = document.createElement("div");
    meta.className = "proposal-card-meta";
    meta.textContent = `${data.proposer} 提议（等用户确认）`;
    card.appendChild(meta);

    card.appendChild(renderPayloadPreview(data.kind, data.payload));

    const actions = document.createElement("div");
    actions.className = "proposal-card-actions";

    const acceptBtn = document.createElement("button");
    acceptBtn.type = "button";
    acceptBtn.className = "proposal-card-accept";
    acceptBtn.textContent = "采纳";
    acceptBtn.addEventListener("click", () => {
      onAccept(card, data, env);
    });

    const ignoreBtn = document.createElement("button");
    ignoreBtn.type = "button";
    ignoreBtn.className = "proposal-card-ignore";
    ignoreBtn.textContent = "忽略";
    ignoreBtn.addEventListener("click", () => {
      card.remove();
    });

    actions.appendChild(acceptBtn);
    actions.appendChild(ignoreBtn);
    card.appendChild(actions);

    host.appendChild(card);
  }

  function onAccept(card, data, env) {
    // P5.3.7 把这块拼成 ADD_DECISION / OPEN_TASK inbound；P5.3.6 仅卡片渲染 + dedup。
    // 占位实现：禁用按钮 + 文案"已采纳"，让 UI 不悬空（dev 测肉眼可见）。
    const btns = card.querySelectorAll("button");
    btns.forEach((b) => (b.disabled = true));
    const status = document.createElement("span");
    status.className = "proposal-card-status";
    status.textContent = "（采纳逻辑 P5.3.7 接入）";
    card.querySelector(".proposal-card-actions").appendChild(status);
    // P5.3.7 实际会 sendInbound(...) 这里
    void sendInbound;
    void env;
  }

  return { onEnvelope };
}

// 测试钩子 —— 仅给 unit test 用，清空模块级 dedup 集合。
export function _resetSeenForTest() {
  _seen.clear();
}
