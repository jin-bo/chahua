"use strict";

// P11 C10：composer 上方「后台运行」列表条。
//
// 每条 bg run 渲一行：``茶客名 · 来源 · 指令预览 · × 取消按钮``。事件驱动：
//   - room_info.data.background_runs：整批权威覆盖（applyAll，切回房 / 重连 / clear 都重发，
//     幽灵指示靠它清）；
//   - AGENT_RUN_STARTED：增量加入；
//   - AGENT_RUN_FINISHED / _CANCELLED / _ERROR：增量移除。
//
// 取消按钮发 AGENT_RUN_CANCEL inbound（带 run_id）。
//
// 与 sidebar.guest-bg-run 同源：本条与 sidebar 各自维护 run_id 视图，envelope_router
// 同步喂两个 sink；room_info 整批覆盖也是。

import { Inbound } from "./events.js";

export function createBgRunBar({ barEl, send, isConnected, sidebar }) {
  // run_id → {guest_name, instruction_preview, issued_by, source_guest?, task_id?}
  const runs = new Map();

  function render() {
    barEl.replaceChildren();
    // HTML 上 ``hidden`` 属性优先级高过 inline ``style.display`` —— 必须经
    // ``barEl.hidden = true/false`` 切属性，否则 ``style.display = ""`` 不会让
    // 元素显出来（与 handoff_queue_bar.js 一致）。
    if (runs.size === 0) {
      barEl.hidden = true;
      return;
    }
    barEl.hidden = false;
    for (const [runId, info] of runs) {
      const row = document.createElement("div");
      row.className = "bg-run-row";
      row.dataset.runId = runId;
      const head = document.createElement("span");
      head.className = "bg-run-head";
      head.textContent = `${info.guest_name || "?"}`;
      row.appendChild(head);
      const src = document.createElement("span");
      src.className = "bg-run-source";
      src.textContent = info.issued_by === "agent"
        ? `(由 ${info.source_guest || "?"} 派发)`
        : "(用户)";
      row.appendChild(src);
      const preview = document.createElement("span");
      preview.className = "bg-run-preview";
      preview.textContent = info.instruction_preview || "";
      row.appendChild(preview);
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "bg-run-cancel";
      cancel.textContent = "×";
      cancel.title = "取消后台运行";
      cancel.addEventListener("click", () => {
        if (!isConnected()) return;
        send({ type: Inbound.AGENT_RUN_CANCEL, run_id: runId });
      });
      row.appendChild(cancel);
      barEl.appendChild(row);
    }
  }

  function applyAll(list) {
    runs.clear();
    for (const r of list || []) {
      if (!r || typeof r.run_id !== "string") continue;
      runs.set(r.run_id, {
        guest_name: r.guest_name,
        instruction_preview: r.instruction_preview,
        issued_by: r.issued_by,
        source_guest: r.source_guest,
        task_id: r.task_id,
      });
    }
    render();
    // 同步刷 sidebar 指示（room_info 整批权威源）。
    if (sidebar && typeof sidebar.applyBackgroundRuns === "function") {
      sidebar.applyBackgroundRuns(list || []);
    }
  }

  function onStarted(d) {
    if (!d || typeof d.run_id !== "string") return;
    runs.set(d.run_id, {
      guest_name: d.guest_name,
      instruction_preview: d.instruction_preview,
      issued_by: d.issued_by,
      source_guest: d.source_guest,
      task_id: d.task_id,
    });
    render();
    if (sidebar && typeof sidebar.markGuestBgRun === "function") {
      sidebar.markGuestBgRun(d.guest_name, d.run_id);
    }
  }

  function onFinished(d) {
    if (!d || typeof d.run_id !== "string") return;
    // envelope 可能漏带 ``guest_name``（防御性：协议变更 / 错误路径）—— 优先用
    // 本地 ``runs`` map 里缓存的茶客名，避免 sidebar 指示残留。
    const cached = runs.get(d.run_id);
    const guestName = (d.guest_name && typeof d.guest_name === "string")
      ? d.guest_name
      : (cached ? cached.guest_name : null);
    runs.delete(d.run_id);
    render();
    if (sidebar && typeof sidebar.unmarkGuestBgRun === "function" && guestName) {
      sidebar.unmarkGuestBgRun(guestName);
    }
  }

  // 初始隐藏。
  render();

  return { applyAll, onStarted, onFinished };
}
