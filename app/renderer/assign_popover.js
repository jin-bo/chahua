"use strict";

// 「指派下一句」popover —— composer 底栏「指派」按钮触发。
//
// 取代 P7.1 的茶客行 ⇨ delegate popover + P7.3 的圆桌侧栏多选模式：delegate 与 panel
// 后端本是同一件事（panel targets 长度=1 即 delegate），这里合一个入口，**勾选人数**
// 决定语义：
//   - 勾 1 人 → handoff_delegate（可带「内部备注」reason，不进茶客 prompt）
//   - 勾 2~4 人 → handoff_panel（可选 summarizer 汇总者）
// summarizer 下拉只在 ≥2 人时显示、内部备注只在 1 人时显示（panel inbound 无 reason
// 字段）；确认按钮文案随勾选人数变。cap 预校验镜像服务端 §3.3 数学，少一次往返；
// 服务端五道校验仍是权威。
//
// 自管开关态：再次点同一 anchor → toggle close。一个 renderer 一份实例。

import { Inbound } from "./events.js";
import {
  attachPopoverDismissHandlers,
  positionPopoverAboveAnchor,
} from "./ui_popover.js";

// 镜像 chahua/handoff.py::MAX_PANEL_TARGETS。
export const MAX_PANEL_TARGETS = 4;

// 圆桌 panelist 数上限 —— 镜像服务端 cap 数学
// min(MAX_PANEL_TARGETS, max_consecutive_ai_turns - 有无 summarizer)。
function panelTargetCap(maxAiTurns, hasSummarizer) {
  return Math.min(MAX_PANEL_TARGETS, maxAiTurns - (hasSummarizer ? 1 : 0));
}

export function createAssignPopover({
  send,
  setStatus,
  isConnected,
  getGuests,
  getMaxAiTurns,
}) {
  let isOpen = false;
  let detachDismiss = null;

  function close() {
    const pop = document.querySelector(".assign-popover");
    if (pop) pop.remove();
    if (detachDismiss) {
      detachDismiss();
      detachDismiss = null;
    }
    isOpen = false;
  }

  function show(anchor) {
    if (isOpen) {
      close();
      return;
    }
    close();
    if (!isConnected()) return;
    // guestNames 顺序稳定 —— 选中态 / summarizer 候选 / 提交 targets 都按它取。
    const guestNames = getGuests();
    if (guestNames.length === 0) {
      setStatus("error", "房间里没有茶客 —— 无法指派");
      return;
    }

    const pop = document.createElement("div");
    pop.className = "popover assign-popover";

    const title = document.createElement("div");
    title.className = "popover-title";
    title.textContent = "指派下一句";
    pop.appendChild(title);

    const selected = new Set();

    const list = document.createElement("div");
    list.className = "assign-guest-list";
    for (const name of guestNames) {
      const row = document.createElement("label");
      row.className = "assign-guest-row";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "assign-guest-cb";
      cb.addEventListener("change", () => {
        if (cb.checked) selected.add(name);
        else selected.delete(name);
        refresh();
      });
      const nameSpan = document.createElement("span");
      nameSpan.className = "assign-guest-name";
      nameSpan.textContent = name;
      row.appendChild(cb);
      row.appendChild(nameSpan);
      list.appendChild(row);
    }
    pop.appendChild(list);

    // 汇总者下拉 —— 仅 ≥2 人（panel）时显示。候选 = 未勾的在场茶客（下拉天然选不到
    // panelist，对齐 inbound 第 4 道校验 summarizer ∉ targets）。
    const summLabel = document.createElement("label");
    summLabel.className = "assign-summ-label";
    summLabel.textContent = "汇总者（可选）";
    const summSelect = document.createElement("select");
    summSelect.className = "assign-summ";
    summLabel.appendChild(summSelect);
    pop.appendChild(summLabel);

    // 内部备注 —— 仅 1 人（delegate）时显示；panel inbound 白名单无 reason 字段。
    const note = document.createElement("textarea");
    note.className = "assign-note";
    note.rows = 2;
    note.placeholder = "内部备注（自己回看 / 调试，不发给茶客）";
    pop.appendChild(note);

    const confirm = document.createElement("button");
    confirm.type = "button";
    confirm.className = "assign-confirm";
    confirm.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (!confirm.disabled) submit();
    });
    pop.appendChild(confirm);

    // Cmd/Ctrl+Enter 从备注框直接提交；裸 Enter 留给 textarea 换行。
    note.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) {
        ev.preventDefault();
        if (!confirm.disabled) submit();
      }
    });

    // 重建汇总者候选 —— 勾选变化后未勾茶客集合变；尽量保留当前选中值。
    function rebuildSummOptions() {
      const prev = summSelect.value;
      summSelect.replaceChildren();
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "不汇总";
      summSelect.appendChild(none);
      for (const name of guestNames) {
        if (selected.has(name)) continue;
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        summSelect.appendChild(opt);
      }
      // 之前选的汇总者若被勾成 panelist 了 → 回落「不汇总」。
      const stillValid = [...summSelect.options].some((o) => o.value === prev);
      summSelect.value = stillValid ? prev : "";
    }

    function refresh() {
      const n = selected.size;
      const isPanel = n >= 2;
      summLabel.hidden = !isPanel;
      note.hidden = n !== 1;
      if (isPanel) rebuildSummOptions();

      let reason = "";
      if (n === 0) reason = "至少勾选一位茶客";
      else if (isPanel) {
        const cap = panelTargetCap(getMaxAiTurns(), Boolean(summSelect.value));
        if (n > cap) {
          reason =
            `圆桌人数 ${n} 超出上限 ${cap}（受 max_consecutive_ai_turns ` +
            `与上限 ${MAX_PANEL_TARGETS} 约束）—— 请减少茶客或去掉汇总者`;
        }
      }
      confirm.disabled = Boolean(reason);
      confirm.title = reason;
      if (n === 1) confirm.textContent = `交给「${[...selected][0]}」发下一句`;
      else if (isPanel) confirm.textContent = `发起圆桌（${n}）`;
      else confirm.textContent = "指派";
    }
    summSelect.addEventListener("change", refresh);

    function submit() {
      if (!isConnected()) return;
      const names = guestNames.filter((nm) => selected.has(nm));
      if (names.length === 1) {
        const payload = { type: Inbound.HANDOFF_DELEGATE, target: names[0] };
        const reason = note.value.trim();
        if (reason) payload.reason = reason;
        send(payload);
        setStatus("", `已把下一句指派给「${names[0]}」`);
      } else {
        const payload = { type: Inbound.HANDOFF_PANEL, targets: names };
        if (summSelect.value) payload.summarizer = summSelect.value;
        send(payload);
        setStatus("", `已发起圆桌：${names.join("、")}`);
      }
      close();
    }

    document.body.appendChild(pop);
    positionPopoverAboveAnchor(pop, anchor);
    isOpen = true;
    detachDismiss = attachPopoverDismissHandlers(pop, close);
    refresh();
  }

  return { show, close };
}
