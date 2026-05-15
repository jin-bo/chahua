"use strict";

// 茶话室 popover 原语。跨 popover 互斥（action 与 permission "开一个关另一个"）是
// 调用方的活，不写进本模块 —— 模块对 renderer state / ws / Inbound 零依赖。

export function positionPopoverByAnchor(pop, anchor) {
  const rect = anchor.getBoundingClientRect();
  pop.style.position = "fixed";
  pop.style.left = `${Math.round(rect.right + 8)}px`;
  pop.style.top = `${Math.round(rect.top)}px`;
  // 右边贴边时退到 anchor 下方。
  const popRect = pop.getBoundingClientRect();
  if (popRect.right > window.innerWidth - 8) {
    const fallbackLeft = Math.max(8, rect.left);
    pop.style.left = `${Math.round(fallbackLeft)}px`;
    pop.style.top = `${Math.round(rect.bottom + 6)}px`;
  }
}

// setTimeout 0 延后挂载 —— 否则触发 popover 那一下 click 冒泡到 document 会立刻关。
export function attachPopoverDismissHandlers(pop, onClose) {
  const outside = (ev) => {
    if (pop.contains(ev.target)) return;
    onClose();
  };
  const esc = (ev) => {
    if (ev.key === "Escape") {
      ev.stopPropagation();
      onClose();
    }
  };
  setTimeout(() => {
    document.addEventListener("mousedown", outside, true);
    document.addEventListener("keydown", esc);
  }, 0);
  return () => {
    document.removeEventListener("mousedown", outside, true);
    document.removeEventListener("keydown", esc);
  };
}

let _actionPopoverAnchor = null;
let _actionPopoverEl = null;
let _detachActionPopoverDismiss = null;

export function closeActionPopover() {
  if (_actionPopoverEl) {
    _actionPopoverEl.remove();
    _actionPopoverEl = null;
  }
  if (_detachActionPopoverDismiss) {
    _detachActionPopoverDismiss();
    _detachActionPopoverDismiss = null;
  }
  _actionPopoverAnchor = null;
}

// items: [{ label, desc?, onClick, danger? }]
// 同 anchor 再调一次 → toggle 关。
export function openActionPopover(anchor, title, items) {
  if (_actionPopoverAnchor === anchor) {
    closeActionPopover();
    return;
  }
  closeActionPopover();
  const pop = document.createElement("div");
  pop.className = "popover action-popover";
  const head = document.createElement("div");
  head.className = "popover-title";
  head.textContent = title;
  pop.appendChild(head);
  for (const it of items) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "popover-option action-option";
    if (it.danger) btn.classList.add("danger");
    const meta = document.createElement("span");
    meta.className = "popover-option-meta";
    const label = document.createElement("span");
    label.className = "popover-option-label";
    label.textContent = it.label;
    meta.appendChild(label);
    if (it.desc) {
      const desc = document.createElement("span");
      desc.className = "popover-option-desc";
      desc.textContent = it.desc;
      meta.appendChild(desc);
    }
    btn.appendChild(meta);
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      closeActionPopover();
      try { it.onClick(); } catch (e) { console.error("action popover click handler 抛错", e); }
    });
    pop.appendChild(btn);
  }
  document.body.appendChild(pop);
  positionPopoverByAnchor(pop, anchor);
  _actionPopoverEl = pop;
  _actionPopoverAnchor = anchor;
  _detachActionPopoverDismiss = attachPopoverDismissHandlers(
    pop, closeActionPopover,
  );
}
