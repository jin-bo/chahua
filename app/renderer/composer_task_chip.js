"use strict";

// composer 底栏的任务 chip（P5.2.10）+ 切换任务 dropdown（P5.2.8）。
//
// chip 文案随 taskState 变（active task title / "🗨 房间级"）；点 chip 弹 dropdown 列
// 所有非终结态任务 + 当前 active（即使 closed），让用户切 active 或回到房间级。
// dropdown 走 .popover 共享 CSS（白底 + 阴影），anchor 在 chip 上方（chip 在底栏，下
// 方一定溢出）。dismiss 沿用 ui_popover.attachPopoverDismissHandlers。
//
// 跨 popover 互斥：开 dropdown 前调 closeOtherPopovers() 让 action / permission popover
// 先关；反向（开 action 时关 chip）靠 outside-mousedown 兜底，无需显式回调。

import {
  attachPopoverDismissHandlers,
  makePopoverOption,
  positionPopoverAboveAnchor,
} from "./ui_popover.js";
import {
  Inbound,
  TASK_UNTITLED,
  formatTaskLabel,
  isTaskClosed,
  taskStatusLabel,
} from "./events.js";

export function createComposerTaskChip({
  chipEl,
  isConnected,
  send,
  setStatus,
  taskState,
  closeOtherPopovers,
}) {
  let dropdownEl = null;
  let detachDismiss = null;

  function close() {
    if (dropdownEl) {
      dropdownEl.remove();
      dropdownEl = null;
    }
    if (detachDismiss) {
      detachDismiss();
      detachDismiss = null;
    }
  }

  function sendSetActiveTask(taskId) {
    if (!isConnected()) return;
    send({ type: Inbound.SET_ACTIVE_TASK, task_id: taskId });
    setStatus("", taskId == null ? "切回房间级…" : "切换任务…");
  }

  function open() {
    if (!isConnected()) return;
    // 同 anchor 再点 → toggle 关闭
    if (dropdownEl) {
      close();
      return;
    }
    closeOtherPopovers();

    const state = taskState.getState();
    const active = taskState.getActiveTask();
    // 列：所有非终结态任务 + 当前 active（即使是 closed，也保留让用户看到自己挂在哪）。
    const items = state.tasks.filter(
      (t) => !isTaskClosed(t.status) || (active && t.id === active.id),
    );
    const pop = document.createElement("div");
    pop.className = "popover task-chip-dropdown";
    pop.appendChild(makePopoverOption({
      label: "🗨 房间级",
      desc: active ? "不归任何任务（聊天回到房间级闲聊）" : "当前选中",
      current: !active,
      extraClass: "task-chip-option",
      onClose: close,
      onClick: () => active && sendSetActiveTask(null),
    }));
    for (const t of items) {
      const isCurrent = !!(active && t.id === active.id);
      pop.appendChild(makePopoverOption({
        label: formatTaskLabel(t),
        desc: isCurrent ? "当前选中" : `状态：${taskStatusLabel(t.status)}`,
        current: isCurrent,
        extraClass: "task-chip-option",
        onClose: close,
        onClick: () => !isCurrent && sendSetActiveTask(t.id),
      }));
    }
    document.body.appendChild(pop);
    positionPopoverAboveAnchor(pop, chipEl);
    dropdownEl = pop;
    detachDismiss = attachPopoverDismissHandlers(pop, close);
  }

  // taskState.subscribe 触发频次高（每次 TASK_INFO；decision/artifact 变更也算）。
  // chip 只依赖 active.id / title / status，其余字段变化照渲是无谓 DOM churn —— 用
  // unit-separator 拼指纹判等价跳过。
  let lastRenderKey = null;

  function render(active) {
    const key = active
      ? `${active.id}\x1f${active.title || ""}\x1f${active.status || ""}`
      : "";
    if (key === lastRenderKey) return;
    lastRenderKey = key;
    const titleText = active ? active.title || TASK_UNTITLED : null;
    chipEl.replaceChildren();
    const label = document.createElement("span");
    label.className = "composer-task-chip-label";
    label.textContent = active ? formatTaskLabel(active) : "🗨 房间级";
    const chev = document.createElement("span");
    chev.className = "composer-task-chip-chevron";
    chev.textContent = "▾";
    chipEl.appendChild(label);
    chipEl.appendChild(chev);
    chipEl.title = active
      ? `当前任务：${titleText} —— 点击切换`
      : "当前未挂任务（房间级闲聊）—— 点击切到任务";
    chipEl.classList.toggle("composer-task-chip-empty", !active);
  }

  chipEl.addEventListener("click", (ev) => {
    ev.stopPropagation();
    open();
  });

  return { render, open, close, sendSetActiveTask };
}
