"use strict";

// 标为决策 modal 的支持消息列表 —— 从 renderer.js 拆出（P5.2 重构，长文件瘦身）。
//
// 入口：``open(anchorLi, taskId)`` —— 显示 modal，预选"右键的那条"，往上最多 4 条带
// message_id 的兄弟消息纳入候选；用户勾完 + 写一句 summary → ``add_decision`` inbound。
// task_id 挂在 modal 节点 ``dataset.taskId`` 上：ESC / backdrop 关闭也跟着 modal 一起消失。

import { Inbound } from "./events.js";

const DECISION_SUPPORT_MAX = 5;

export function createDecisionSupport({
  modal,
  summaryEl,
  supportEl,
  submitBtn,
  send,
  setStatus,
  openModal,
  closeModal,
  isConnected,
}) {
  submitBtn.addEventListener("click", () => {
    if (!isConnected()) return;
    const taskId = modal.dataset.taskId;
    if (!taskId) return;
    const summary = summaryEl.value.trim();
    if (!summary) {
      summaryEl.focus();
      return;
    }
    const supporting_message_ids = Array.from(
      supportEl.querySelectorAll("input[type='checkbox']:checked"),
    ).map((el) => el.value);
    send({
      type: Inbound.ADD_DECISION,
      task_id: taskId,
      summary,
      supporting_message_ids,
    });
    setStatus("", "已记录决策…");
    closeModal(modal);
  });

  function open(anchorLi, taskId) {
    modal.dataset.taskId = taskId;
    summaryEl.value = "";
    renderSupportList(collectCandidates(anchorLi));
    openModal(modal);
    summaryEl.focus();
  }

  function renderSupportList(candidates) {
    supportEl.replaceChildren();
    for (const { li, defaultChecked } of candidates) {
      const item = document.createElement("li");
      item.className = "decision-support-item";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = li.dataset.messageId;
      cb.checked = defaultChecked;
      item.appendChild(cb);
      const speaker = document.createElement("span");
      speaker.className = "decision-support-speaker";
      speaker.textContent = li.dataset.speaker || "?";
      item.appendChild(speaker);
      const snippet = document.createElement("span");
      snippet.className = "decision-support-snippet";
      snippet.textContent = snippetOf(li) || "(空)";
      item.appendChild(snippet);
      // 整行点击 = 切 checkbox（除非用户已直接点到 checkbox 本身，避免双触发）。
      item.addEventListener("click", (ev) => {
        if (ev.target === cb) return;
        cb.checked = !cb.checked;
      });
      supportEl.appendChild(item);
    }
  }

  return { open };
}

function snippetOf(li) {
  // 取气泡里的可见文本 —— textContent 会把 markdown 渲染后的 DOM 拍平成纯文本。
  const bubble = li.querySelector(".bubble");
  if (!bubble) return "";
  const text = bubble.querySelector(".text");
  return (text ? text.textContent : bubble.textContent).trim().replace(/\s+/g, " ");
}

function collectCandidates(anchorLi) {
  const items = [{ li: anchorLi, defaultChecked: true }];
  let cur = anchorLi.previousElementSibling;
  while (cur && items.length < DECISION_SUPPORT_MAX) {
    if (cur.dataset.messageId) {
      items.push({ li: cur, defaultChecked: false });
    }
    cur = cur.previousElementSibling;
  }
  return items;
}
