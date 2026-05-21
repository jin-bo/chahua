"use strict";

// 茶客 propose 卡片渲染 + 去重（docs/P5-任务房间.md §6.3）。
// hint 事件不持久化：刷新页面 + reset() （切房 / clear_room 回环时调）会清空去重指纹。

import { EventType, Inbound, TaskProposalKind } from "./events.js";

// 提议去重指纹集合 —— 反复刷同一 propose 时只渲染一张。模块级；renderer.js 在
// renderSidebar 入口调 reset() 清，避免跨房间残留。指纹构成按 kind 分（见 dedupKey）。
const _seen = new Set();

// 逐轮瞬态的 handoff kind —— 指纹额外含宿主 message_id（见 dedupKey 注释）。
const _HANDOFF_KINDS = new Set([
  TaskProposalKind.HANDOFF_DELEGATE,
  TaskProposalKind.HANDOFF_REVIEW,
  TaskProposalKind.HANDOFF_PANEL,
]);

// handoff propose 卡的「采纳」发的是抢占式 handoff_* inbound：若在任意 turn 还在跑
// 时点采纳，server 把 in-flight user-turn 当可抢占 → cancel/drain 掉它，反而截断了
// 当前发言（codex P2）。所以点「采纳」时若有 turn 在跑，这一下不发、只把按钮 gate
// 住（置灰反馈），收到 AI 链收尾（onTurnIdle）再统一放开。gate 在**点击时**判 ——
// 覆盖"卡片当轮"与"留到后续任意 turn 才点的旧卡"，不靠渲染时一次性判定。
const _gatedAccepts = new Set();

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

function dedupKey(data, messageId) {
  // task 决策 / 开任务提议是幂等的 —— 跨消息也去重（同一提议反复出现只渲一张，
  // 否则用户可能误采纳重复决策 / 开重复任务）。handoff 是逐轮的瞬态动作：同一茶客
  // 在后续某轮再次 propose 同样的 handoff 挂在另一条消息气泡下，必须能再渲一张卡 ——
  // 所以 handoff kind 的指纹额外含宿主 message_id，只有同一条消息内工具被调两次
  // （同一 envelope 重复）才去重，跨轮的同样提议不再被吞（codex P2）。
  const base = `${data.proposer || ""}|${data.kind || ""}|${stableStringify(
    data.payload ?? null,
  )}`;
  return _HANDOFF_KINDS.has(data.kind) ? `${base}|${messageId || ""}` : base;
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
  } else if (kind === TaskProposalKind.HANDOFF_DELEGATE) {
    const head = document.createElement("div");
    head.className = "proposal-card-kind";
    head.textContent = `建议交给『${payload.target ?? ""}』发下一句`;
    box.appendChild(head);
    if (typeof payload.reason === "string" && payload.reason) {
      const why = document.createElement("div");
      why.className = "proposal-card-summary";
      why.textContent = truncate(payload.reason, 200);
      box.appendChild(why);
    }
  } else if (kind === TaskProposalKind.HANDOFF_REVIEW) {
    const head = document.createElement("div");
    head.className = "proposal-card-kind";
    head.textContent = `建议请『${payload.target ?? ""}』审阅一条发言`;
    const body = document.createElement("div");
    body.className = "proposal-card-summary";
    body.textContent = `审『${payload.reviewee ?? ""}』：${truncate(payload.snippet ?? "", 80)}`;
    box.appendChild(head);
    box.appendChild(body);
  } else if (kind === TaskProposalKind.HANDOFF_PANEL) {
    const head = document.createElement("div");
    head.className = "proposal-card-kind";
    head.textContent = "建议发起圆桌";
    const targets = Array.isArray(payload.targets) ? payload.targets : [];
    const members = document.createElement("div");
    members.className = "proposal-card-summary";
    members.textContent = `成员：${targets.join("、")}`;
    box.appendChild(head);
    box.appendChild(members);
    if (typeof payload.summarizer === "string" && payload.summarizer) {
      const sum = document.createElement("div");
      sum.className = "proposal-card-refs";
      sum.textContent = `汇总：${payload.summarizer}`;
      box.appendChild(sum);
    }
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
  //   kind=decision         → ADD_DECISION（task_id 必填）
  //   kind=open             → OPEN_TASK（不依赖当前 active）
  //   kind=handoff_delegate → HANDOFF_DELEGATE
  //   kind=handoff_review   → HANDOFF_REVIEW
  //   kind=handoff_panel    → HANDOFF_PANEL
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
  // P7.4：handoff propose 采纳即拼回 P7.1~7.3 既有的 handoff_* inbound。kind 去掉
  // 「handoff_」前缀即 inbound type 后缀；只挑该 inbound 白名单内的键 ——
  // propose-only 字段（reviewee / snippet）绝不漏进帧（server 白名单严格会丢帧）。
  if (data.kind === TaskProposalKind.HANDOFF_DELEGATE) {
    const target = typeof payload.target === "string" ? payload.target : "";
    if (!target) return null;
    // proposer 进 reason 前缀 —— HandoffItem 不落盘，reason 是唯一能带 provenance
    // 的字段（debug record / 队列预览 hover 据此看出"C 提议、用户采纳"）。
    const why = typeof payload.reason === "string" ? payload.reason : "";
    const reason = why ? `${data.proposer} 提议：${why}` : `${data.proposer} 提议`;
    return { type: Inbound.HANDOFF_DELEGATE, target, reason };
  }
  if (data.kind === TaskProposalKind.HANDOFF_REVIEW) {
    const target = typeof payload.target === "string" ? payload.target : "";
    const mid = typeof payload.message_id === "string" ? payload.message_id : "";
    if (!target || !mid) return null;
    return { type: Inbound.HANDOFF_REVIEW, target, message_id: mid };
  }
  if (data.kind === TaskProposalKind.HANDOFF_PANEL) {
    // 坏 targets 一律 return null —— 不 filter「修正」成另一个合法 panel。
    const targets = payload.targets;
    if (!Array.isArray(targets) || targets.length < 2) return null;
    if (!targets.every((x) => typeof x === "string" && x)) return null;
    const out = { type: Inbound.HANDOFF_PANEL, targets };
    if (typeof payload.summarizer === "string" && payload.summarizer) {
      out.summarizer = payload.summarizer;
    }
    return out;
  }
  return null;
}

// 创建一只 proposal card 渲染器实例。
//
//   messagesEl   消息流容器（按 messageId 找宿主气泡）。
//   sendInbound  ws 帧发送闭包（payload obj → JSON ws.send）；renderer.js 持。
//
//   isTurnActive 闭包 () => bool，判当前是否有 turn 在跑（renderer.js 传
//                `() => currentTurnId !== null`）；handoff 采纳按钮的 gate 用。
//
// 返回 { onEnvelope(env), onTurnIdle(), reset() }：
//   onEnvelope —— renderer.js 在 EventType.TASK_PROPOSAL 路径上调一次。
//   onTurnIdle —— renderer.js 在 AI 链收尾（currentTurnId 归 null）时调，放开被
//                 gate 的采纳按钮；不在每个 turn_end 调（next==='ai' 时链未结束）。
//   reset      —— 切房 / clear_room 回环时调，清跨房间累积的去重指纹 + gate 集。
export function createProposalCard({ messagesEl, sendInbound, isTurnActive }) {
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

    const key = dedupKey(data, env.message_id);
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
        // handoff 采纳发抢占式 handoff_* inbound —— turn 在跑时点会 cancel/drain
        // 掉 in-flight turn、截断当前发言（codex P2）。所以有 turn 在跑就先不发、
        // 把按钮 gate 住（置灰），AI 链收尾时 onTurnIdle 放开让用户重点。在点击时判
        // 覆盖旧卡留到后续 turn 的情况。decision / open 的 inbound 不抢占，不 gate。
        if (_HANDOFF_KINDS.has(data.kind) && isTurnActive()) {
          acceptBtn.disabled = true;
          acceptBtn.title = "茶客发言中，本轮结束后可采纳";
          _gatedAccepts.add(acceptBtn);
          return;
        }
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

  // AI 链收尾、无 in-flight turn —— 放开 gate 住的 handoff 采纳按钮。已被忽略 / 已
  // 采纳的按钮即便还在集合里，disabled=false 作用在游离 / 已锁节点上无害。
  function onTurnIdle() {
    for (const btn of _gatedAccepts) {
      btn.disabled = false;
      btn.title = "";
    }
    _gatedAccepts.clear();
  }

  function reset() {
    _seen.clear();
    _gatedAccepts.clear();
  }

  return { onEnvelope, onTurnIdle, reset };
}
