"use strict";

// 茶客 propose 卡片渲染 + 去重（docs/P5-任务房间.md §6.3）。
// hint 事件不持久化：刷新页面 + reset() （切房 / clear_room 回环时调）会清空去重指纹。

import { EventType, Inbound, TaskProposalKind } from "./events.js";

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

function buildAcceptInbound(data, taskId) {
  // 把 propose envelope 转译成既有 inbound（CLAUDE.md "写权限永远在用户"）：
  //   kind=decision → ADD_DECISION（task_id 必填）
  //   kind=open     → OPEN_TASK（不依赖当前 active）
  // 返回 null 表示 payload 不合法 / 缺前置条件，调用方禁用按钮并提示。
  const payload = data.payload || {};
  if (data.kind === TaskProposalKind.DECISION) {
    if (!taskId) return null;  // decision 必须挂某个 task，没就拒绝采纳
    const summary = typeof payload.summary === "string" ? payload.summary : "";
    if (!summary) return null;
    const supRaw = Array.isArray(payload.supporting_message_ids)
      ? payload.supporting_message_ids
      : [];
    const supporting = supRaw.filter((x) => typeof x === "string");
    return {
      type: Inbound.ADD_DECISION,
      task_id: taskId,
      summary,
      supporting_message_ids: supporting,
    };
  }
  if (data.kind === TaskProposalKind.OPEN) {
    const title = typeof payload.title === "string" ? payload.title : "";
    const goal = typeof payload.goal === "string" ? payload.goal : "";
    if (!title || !goal) return null;
    return { type: Inbound.OPEN_TASK, title, goal };
  }
  return null;
}

// 创建一只 proposal card 渲染器实例。
//
//   messagesEl   消息流容器（按 messageId 找宿主气泡）。
//   sendInbound  ws 帧发送闭包（payload obj → JSON ws.send）；renderer.js 持。
//
// 返回 { onEnvelope(env), reset() }：
//   onEnvelope —— renderer.js 在 EventType.TASK_PROPOSAL 路径上调一次。
//   reset      —— 切房 / clear_room 回环时调，清跨房间累积的去重指纹。
export function createProposalCard({ messagesEl, sendInbound }) {
  function onEnvelope(env) {
    if (env.type !== EventType.TASK_PROPOSAL) return;
    const data = env.data || {};
    if (!data.kind || !data.payload || !data.proposer) return;

    const host = findHostBubble(messagesEl, env.message_id);
    if (!host) {
      // hint 找不到挂点（已切场景 / 已 clear_room）：丢弃且不留指纹 —— 否则下次同
      // propose 真有挂点时也会被错误 dedup 掉。
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

    // 解析采纳 inbound：拼好后留闭包用；null 表示 payload 不合法（缺 task / 缺字段），
    // 采纳按钮渲成 disabled + 文案提示。
    const taskId = typeof data.task_id === "string" ? data.task_id : null;
    const inbound = buildAcceptInbound(data, taskId);

    const acceptBtn = document.createElement("button");
    acceptBtn.type = "button";
    acceptBtn.className = "proposal-card-accept";
    acceptBtn.textContent = "采纳";
    if (inbound === null) {
      acceptBtn.disabled = true;
      acceptBtn.title =
        data.kind === TaskProposalKind.DECISION
          ? "需要附在某个任务上才能采纳"
          : "字段不齐，无法采纳";
    } else {
      acceptBtn.addEventListener("click", () => {
        sendInbound(inbound);
        // 采纳后双按钮锁住 + 卡片留在原位作为历史痕迹；server 回环 TASK_INFO + 对应 hint
        // 会让任务面板更新。
        actions.querySelectorAll("button").forEach((b) => (b.disabled = true));
        const status = document.createElement("span");
        status.className = "proposal-card-status";
        status.textContent = "已采纳";
        actions.appendChild(status);
      });
    }

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
