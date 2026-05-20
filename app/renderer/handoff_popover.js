"use strict";

// 「交给…」delegate popover（点茶客卡片的 ⇨ 按钮）—— P7.1.9 / docs/P7 §5.1.1。
//
// 弹一个带可选「内部备注」textarea 的小卡：确认 → 发 handoff_delegate inbound。
// 备注是 reason 字段，仅进 debug record + 队列预览 hover，**不进茶客 prompt**——
// placeholder 文案据此措辞，不写"指令"等会让用户误以为茶客能看到的词。
//
// 自管开关态：再次点同一 anchor → toggle close。一个 renderer 一份实例。

import { Inbound } from "./events.js";
import {
  attachPopoverDismissHandlers,
  positionPopoverByAnchor,
} from "./ui_popover.js";

export function createHandoffPopover({ send, setStatus, isConnected }) {
  let openForGuest = null;
  let detachDismiss = null;

  function close() {
    const pop = document.querySelector(".handoff-popover");
    if (pop) pop.remove();
    if (detachDismiss) {
      detachDismiss();
      detachDismiss = null;
    }
    openForGuest = null;
  }

  function submit(guestName, reason) {
    if (!isConnected()) return;
    const payload = { type: Inbound.HANDOFF_DELEGATE, target: guestName };
    // reason 是内部备注：留空就不带字段（与 server 端 reason 可缺省口径一致）。
    if (reason) payload.reason = reason;
    send(payload);
    setStatus("", `已把下一句指派给「${guestName}」`);
    close();
  }

  function show(anchor, guestName) {
    if (openForGuest === guestName) {
      close();
      return;
    }
    close();
    const pop = document.createElement("div");
    pop.className = "popover handoff-popover";

    const title = document.createElement("div");
    title.className = "popover-title";
    title.textContent = `交给「${guestName}」发下一句`;
    pop.appendChild(title);

    const note = document.createElement("textarea");
    note.className = "handoff-note";
    note.rows = 3;
    note.placeholder = "内部备注（用于自己回看 / 调试，不发给茶客）";

    const confirm = document.createElement("button");
    confirm.type = "button";
    confirm.className = "handoff-confirm";
    confirm.textContent = "加入队列";
    confirm.addEventListener("click", (ev) => {
      ev.stopPropagation();
      submit(guestName, note.value.trim());
    });
    // Cmd/Ctrl+Enter 提交；裸 Enter 留给 textarea 换行（备注可多行）。
    note.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) {
        ev.preventDefault();
        submit(guestName, note.value.trim());
      }
    });

    pop.appendChild(note);
    pop.appendChild(confirm);
    document.body.appendChild(pop);
    positionPopoverByAnchor(pop, anchor);
    note.focus();
    openForGuest = guestName;
    detachDismiss = attachPopoverDismissHandlers(pop, close);
  }

  return { show, close };
}
