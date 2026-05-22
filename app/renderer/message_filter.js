"use strict";

// 消息上的任务 chip + filter 视图（P5.2.10）。从 renderer.js 抽出（renderer 重构）。
//
// 非 active 任务的消息气泡挂一个可点的任务 chip；点 chip 进 filter 视图（只显示该
// 任务的消息，其余 .filtered-out 隐起来）+ 顶部出现一条"返回全部"横幅。filter 状态
// （_filterTaskId / banner DOM）是本模块私有 —— 外部只通过 afterAppendMessage /
// exitFilter / onTaskStateChange 三个出口交互。

import { formatTaskLabel, TASK_UNTITLED } from "./events.js";

export function createMessageFilter({ messagesEl, taskState }) {
  let _filterTaskId = null;
  let _filterBannerEl = null;

  function getLiTaskId(li) {
    return li.dataset.taskId || null;
  }

  function refreshMessageTaskChip(li) {
    const taskId = getLiTaskId(li);
    let chip = li.querySelector(".message-task-chip");
    const removeChip = () => { if (chip) { chip.remove(); chip = null; } };
    if (!taskId) {
      removeChip();
      return;
    }
    const activeId = taskState.getState().activeTaskId;
    // active 任务的消息不挂 chip —— 默认 chat 主流就是 active，挂了反而吵。
    if (taskId === activeId) {
      removeChip();
      return;
    }
    // task 还没加载（room_history 先于 task_info）/ 被删了 —— 不挂；下次 subscriber 补。
    const task = taskState.getTaskById(taskId);
    if (!task) {
      removeChip();
      return;
    }
    const text = formatTaskLabel(task);
    if (chip) {
      if (chip.textContent !== text) chip.textContent = text;
      return;
    }
    chip = document.createElement("button");
    chip.type = "button";
    chip.className = "message-task-chip";
    chip.textContent = text;
    chip.title = `进入任务「${task.title || TASK_UNTITLED}」的 filter 视图`;
    chip.addEventListener("click", (ev) => {
      ev.stopPropagation();
      enterMessageFilter(taskId);
    });
    const bubble = li.querySelector(".bubble");
    if (bubble) bubble.appendChild(chip);
  }

  function refreshAllMessageTaskChips() {
    for (const li of messagesEl.querySelectorAll("li[data-task-id]")) {
      refreshMessageTaskChip(li);
    }
  }

  function applyMessageFilterTo(li) {
    if (_filterTaskId === null) {
      li.classList.remove("filtered-out");
      return;
    }
    li.classList.toggle("filtered-out", getLiTaskId(li) !== _filterTaskId);
  }

  function applyMessageFilterAll() {
    for (const li of messagesEl.querySelectorAll("li")) applyMessageFilterTo(li);
  }

  // 三处 append 调用站（appendBubble / startStreamingMessage / endStreamingMessage 的
  // 无 start 分支）的尾部都做一样的事 —— 刷 chip + 应用 filter。单出口，将来加 third
  // concern（搜索高亮 / 已读标记等）也只动这里。
  function afterAppendMessage(li) {
    refreshMessageTaskChip(li);
    applyMessageFilterTo(li);
  }

  function enterMessageFilter(taskId) {
    _filterTaskId = taskId;
    ensureFilterBanner();
    updateFilterBannerText();
    _filterBannerEl.hidden = false;
    applyMessageFilterAll();
  }

  function exitMessageFilter() {
    if (_filterTaskId === null) return;
    _filterTaskId = null;
    if (_filterBannerEl) _filterBannerEl.hidden = true;
    applyMessageFilterAll();
  }

  function ensureFilterBanner() {
    if (_filterBannerEl) return;
    _filterBannerEl = document.createElement("div");
    _filterBannerEl.className = "message-filter-banner";
    _filterBannerEl.hidden = true;
    const text = document.createElement("span");
    text.className = "message-filter-banner-text";
    _filterBannerEl.appendChild(text);
    const exit = document.createElement("button");
    exit.type = "button";
    exit.className = "message-filter-banner-exit";
    exit.textContent = "返回全部";
    exit.addEventListener("click", exitMessageFilter);
    _filterBannerEl.appendChild(exit);
    // 插到 messagesEl 紧前 —— flex 容器 #main 里同级 sibling，挡不到聊天区域滚动。
    messagesEl.parentNode.insertBefore(_filterBannerEl, messagesEl);
  }

  function updateFilterBannerText() {
    if (!_filterBannerEl || _filterTaskId === null) return;
    const task = taskState.getTaskById(_filterTaskId);
    const title = task ? task.title || TASK_UNTITLED : _filterTaskId;
    _filterBannerEl.querySelector(".message-filter-banner-text").textContent =
      `仅显示任务「${title}」的消息 —— 其余隐起来`;
  }

  return {
    // chat_stream 三处 append 的尾部钩子。
    afterAppendMessage,
    // 切房 / 重连 / 清空 → filter 视图无意义，强 exit。
    exitFilter: exitMessageFilter,
    // taskState 变（改名 / 切 active / 关任务）→ 刷 chip 文案 + filter banner 文案。
    onTaskStateChange() {
      refreshAllMessageTaskChips();
      if (_filterTaskId !== null) updateFilterBannerText();
    },
  };
}
