"use strict";

// 茶客 propose 卡片渲染 + 去重（docs/P5-任务房间.md §6.3）。
// hint 事件不持久化：刷新页面 + reset() （切房 / clear_room 回环时调）会清空去重指纹。

import { EventType, TaskProposalKind } from "./events.js";

// (proposer, kind, payload-hash) 指纹集合 —— 反复刷同一 propose 时只渲染一张。
// 模块级；renderer.js 在 renderSidebar 入口调 reset() 清，避免跨房间残留。
const _seen = new Set();

// 同 task_panel.js::cssEscape —— 防御 older Electron / 测试环境没 CSS.escape；message_id
// 是 `msg_<hex>` 不含特殊字符，fallback 仅转义 " 与 \ 已足够。
function cssEscape(s) {
  if (typeof window !== "undefined" && window.CSS && typeof window.CSS.escape === "function") {
    return window.CSS.escape(String(s ?? ""));
  }
  return String(s ?? "").replace(/["\\]/g, "\\$&");
}

function stableStringify(obj) {
  // 同 payload 不同 key 顺序应产同一指纹 —— 用 sorted-key 递归序列化。
  if (obj === null || typeof obj !== "object") return JSON.stringify(obj);
  if (Array.isArray(obj)) {
    return "[" + obj.map(stableStringify).join(",") + "]";
  }
  const keys = Object.keys(obj).sort();
  const parts = keys.map((k) => JSON.stringify(k) + ":" + stableStringify(obj[k]));
  return "{" + parts.join(",") + "}";
}

function dedupKey(data) {
  return `${data.proposer || ""}|${data.kind || ""}|${stableStringify(data.payload ?? null)}`;
}

function truncate(s, n) {
  if (typeof s !== "string") return "";
  return s.length <= n ? s : s.slice(0, n) + "…";
}

function renderPayloadPreview(kind, payload) {
  const box = document.createElement("div");
  box.className = "proposal-card-preview";
  if (kind === TaskProposalKind.DECISION) {
    const head = document.createElement("div");
    head.className = "proposal-card-kind";
    head.textContent = "提议决策";
    const body = document.createElement("div");
    body.className = "proposal-card-summary";
    body.textContent = truncate(payload.summary ?? "", 200);
    box.appendChild(head);
    box.appendChild(body);
    const sup = Array.isArray(payload.supporting_message_ids)
      ? payload.supporting_message_ids
      : [];
    if (sup.length) {
      const refs = document.createElement("div");
      refs.className = "proposal-card-refs";
      refs.textContent = `引用 ${sup.length} 条消息`;
      box.appendChild(refs);
    }
  } else if (kind === TaskProposalKind.OPEN) {
    const head = document.createElement("div");
    head.className = "proposal-card-kind";
    head.textContent = "提议开新任务";
    const titleEl = document.createElement("div");
    titleEl.className = "proposal-card-title";
    titleEl.textContent = truncate(payload.title ?? "", 60);
    const goalEl = document.createElement("div");
    goalEl.className = "proposal-card-goal";
    goalEl.textContent = truncate(payload.goal ?? "", 200);
    box.appendChild(head);
    box.appendChild(titleEl);
    box.appendChild(goalEl);
  } else {
    // 未知 kind —— dev 期捕错，不静默兜底成空卡。
    const head = document.createElement("div");
    head.className = "proposal-card-kind";
    head.textContent = `未知提议类型：${kind}`;
    box.appendChild(head);
  }
  return box;
}

function findHostBubble(messagesEl, messageId) {
  if (!messageId) return null;
  return messagesEl.querySelector(`li[data-message-id="${cssEscape(messageId)}"]`);
}

// 创建一只 proposal card 渲染器实例。
//
//   messagesEl   消息流容器（按 messageId 找宿主气泡）。
//
// 返回 { onEnvelope(env), reset() }：
//   onEnvelope —— renderer.js 在 EventType.TASK_PROPOSAL 路径上调一次。
//   reset      —— 切房 / clear_room 回环时调，清掉跨房间累积的去重指纹。
//
// 采纳按钮的 inbound 帧组装目前是占位（按钮禁用 + 状态文案）。
export function createProposalCard({ messagesEl }) {
  function onEnvelope(env) {
    if (env.type !== EventType.TASK_PROPOSAL) return;
    const data = env.data || {};
    if (!data.kind || !data.payload || !data.proposer) return;

    const host = findHostBubble(messagesEl, env.message_id);
    if (!host) {
      // hint 找不到挂点（已切场景 / 已 clear_room）：丢弃且不留指纹 —— 否则下次同 propose
      // 真有挂点时也会被错误 dedup 掉。
      return;
    }

    const key = dedupKey(data);
    if (_seen.has(key)) return;
    _seen.add(key);

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
      // 占位实现 —— 把 inbound 帧组装放在后续 wiring 提交完成。
      actions.querySelectorAll("button").forEach((b) => (b.disabled = true));
      const status = document.createElement("span");
      status.className = "proposal-card-status";
      status.textContent = "（采纳后端 wiring 待接入）";
      actions.appendChild(status);
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

  function reset() {
    _seen.clear();
  }

  return { onEnvelope, reset };
}
