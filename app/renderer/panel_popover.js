"use strict";

// 圆桌（panel）配置 popover —— 茶客侧栏「圆桌」多选模式下点「发起圆桌(N)」触发。
// docs/P7.3 §6.1。
//
// 列已选 panelist + 一个 summarizer 下拉，确认 → 发 handoff_panel inbound。
// summarizer 下拉选项 = 未被勾为 panelist 的在场茶客 + 一个「不汇总」项，与 inbound
// 第 4 道校验（summarizer ∉ targets）对齐——下拉天然选不到 panelist，不靠服务端兜。
//
// 自管开关态：再次点同一 anchor → toggle close。一个 renderer 一份实例。

import { Inbound } from "./events.js";
import {
  attachPopoverDismissHandlers,
  positionPopoverByAnchor,
} from "./ui_popover.js";

export function createPanelPopover({ send, setStatus, isConnected }) {
  let isOpen = false;
  let detachDismiss = null;

  function close() {
    const pop = document.querySelector(".panel-popover");
    if (pop) pop.remove();
    if (detachDismiss) {
      detachDismiss();
      detachDismiss = null;
    }
    isOpen = false;
  }

  function submit(targets, summarizer, onSubmitted) {
    if (!isConnected()) return;
    const payload = { type: Inbound.HANDOFF_PANEL, targets };
    // summarizer 可缺省：「不汇总」时不带字段（与 server 端 summarizer 可缺省口径一致）。
    if (summarizer) payload.summarizer = summarizer;
    send(payload);
    setStatus("", `已发起圆桌：${targets.join("、")}`);
    close();
    onSubmitted();
  }

  // anchor：发起按钮；panelists：已选 panelist 名（≥2，按茶客行顺序）；
  // otherGuests：未被勾选的在场茶客名（summarizer 候选）；onSubmitted：发起后回调
  // （renderer 用它退出圆桌多选模式）。
  function show(anchor, panelists, otherGuests, onSubmitted) {
    if (isOpen) {
      close();
      return;
    }
    close();
    const pop = document.createElement("div");
    pop.className = "popover panel-popover";

    const title = document.createElement("div");
    title.className = "popover-title";
    title.textContent = "发起圆桌";
    pop.appendChild(title);

    const members = document.createElement("div");
    members.className = "panel-popover-members";
    members.textContent = `圆桌成员：${panelists.join("、")}`;
    pop.appendChild(members);

    const summLabel = document.createElement("label");
    summLabel.className = "panel-popover-summ-label";
    summLabel.textContent = "汇总者（可选）";
    const summSelect = document.createElement("select");
    summSelect.className = "panel-popover-summ";
    const noneOpt = document.createElement("option");
    noneOpt.value = "";
    noneOpt.textContent = "不汇总";
    summSelect.appendChild(noneOpt);
    for (const name of otherGuests) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      summSelect.appendChild(opt);
    }
    summLabel.appendChild(summSelect);
    pop.appendChild(summLabel);

    const confirm = document.createElement("button");
    confirm.type = "button";
    confirm.className = "panel-popover-confirm";
    confirm.textContent = "发起";
    confirm.addEventListener("click", (ev) => {
      ev.stopPropagation();
      submit(panelists, summSelect.value, onSubmitted);
    });
    pop.appendChild(confirm);

    document.body.appendChild(pop);
    positionPopoverByAnchor(pop, anchor);
    isOpen = true;
    detachDismiss = attachPopoverDismissHandlers(pop, close);
  }

  return { show, close };
}
