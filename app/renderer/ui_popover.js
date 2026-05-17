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

// composer 底栏 chip / textarea 这类近窗底 anchor 用 —— 默认锚到 anchor 上方，
// 顶到屏幕顶（短窗 + 长 popover 罕见）才退回 anchor 下方。与 positionPopoverByAnchor
// 同构（fixed + rect-read + 兜底分支），独立函数避免一个 placement 参数让两套 fallback
// 逻辑混在一起。
export function positionPopoverAboveAnchor(pop, anchor) {
  const rect = anchor.getBoundingClientRect();
  pop.style.position = "fixed";
  pop.style.left = `${Math.round(rect.left)}px`;
  pop.style.bottom = `${Math.round(window.innerHeight - rect.top + 6)}px`;
  pop.style.top = "";
  const popRect = pop.getBoundingClientRect();
  if (popRect.top < 8) {
    pop.style.bottom = "";
    pop.style.top = `${Math.round(rect.bottom + 6)}px`;
  }
}

// 共享 .popover-option 按钮构造 —— action popover / task chip dropdown 同款骨架
// （button > meta > label + optional desc）。click 自动 stopPropagation + onClose
// + onClick try/catch。permission popover 因为多一道 swatch / "（当前）" 行内 tag
// 不复用本助手，自管 DOM。
export function makePopoverOption({
  label, desc, onClick,
  extraClass = "",
  danger = false,
  current = false,
  onClose = () => {},
}) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "popover-option";
  if (extraClass) btn.classList.add(...extraClass.split(/\s+/));
  if (danger) btn.classList.add("danger");
  if (current) btn.classList.add("current");
  const meta = document.createElement("span");
  meta.className = "popover-option-meta";
  const labelEl = document.createElement("span");
  labelEl.className = "popover-option-label";
  labelEl.textContent = label;
  meta.appendChild(labelEl);
  if (desc) {
    const descEl = document.createElement("span");
    descEl.className = "popover-option-desc";
    descEl.textContent = desc;
    meta.appendChild(descEl);
  }
  btn.appendChild(meta);
  btn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    onClose();
    try { onClick(); } catch (e) { console.error("popover option click", e); }
  });
  return btn;
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
    pop.appendChild(makePopoverOption({
      label: it.label,
      desc: it.desc,
      onClick: it.onClick,
      danger: it.danger,
      extraClass: "action-option",
      onClose: closeActionPopover,
    }));
  }
  document.body.appendChild(pop);
  positionPopoverByAnchor(pop, anchor);
  _actionPopoverEl = pop;
  _actionPopoverAnchor = anchor;
  _detachActionPopoverDismiss = attachPopoverDismissHandlers(
    pop, closeActionPopover,
  );
}
