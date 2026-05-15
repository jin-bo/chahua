"use strict";

// 茶话室 @ 补全 dropdown。inputEl 上的 input 事件喂 refresh()；keydown 用
// handleKeydown 让模块独占导航键（Arrow / Enter / Tab / Esc）当 dropdown 开启时；
// 关闭 dropdown 时键盘事件原样返还 caller，让 composer 的 Enter → submit 路径生效。
//
// dropdown 开启时 handleKeydown 一律返 true —— 即使按了未识别的键。这是为了挡住
// caller 的"Enter 提交"：dropdown 开 + 没选中项 + 按 Enter 应该走 textarea 默认
// 换行，而非提交（与原版行为一致）。

import { DEFAULT_PERMISSION } from "./events.js";
import { makeBadge, makePermissionBadge } from "./chat_view.js";

// @broadcast 候选项 —— 镜像 chahua/orchestrator.py:_BROADCAST_TOKENS 的"全员"语义。
// 用 .find() 取**第一个**匹配返回单条，避免空 query 时"所有人 / ALL"同时占两行
// （语义重复）。顺序决定优先级：中文优先，拉丁起头 fallthrough 到 ALL。server 端
// _BROADCAST_TOKENS 还包含 everyone / 大家 / 各位三个不展示但能手输的兼容词。
const BROADCAST_CANDIDATES = Object.freeze([
  { name: "所有人", broadcast: true },
  { name: "ALL", broadcast: true },
]);

export function createMention({ inputEl, dropdownEl, getGuests }) {
  let active = -1;

  // 找输入框当前光标前最近的 "@<query>"。返回 {start, end, query} 或 null。
  // 字符集 \S* 与 server 端 _AT_PATTERN 反黑名单语义对齐 —— 含 - / . / 撇号的茶客
  // 名也能被前端 typeahead 捕到；候选过滤靠 startsWith 兜底（不在册的名字自然
  // 候选为空，dropdown 自动 hide）。
  function detect() {
    const end = inputEl.selectionStart ?? inputEl.value.length;
    const before = inputEl.value.slice(0, end);
    const m = /(?:^|\s)@(\S*)$/.exec(before);
    if (!m) return null;
    // @ 在 m[0] 中的偏移：m[0] 起头是 @ 时偏 0，是空白时偏 1。
    const start = m.index + (m[0].startsWith("@") ? 0 : 1);
    return { start, end, query: m[1] };
  }

  function match(query) {
    const q = query.toLowerCase();
    const broadcast = BROADCAST_CANDIDATES.find((b) =>
      b.name.toLowerCase().startsWith(q),
    );
    const guestMatches = getGuests().filter((g) =>
      g.name.toLowerCase().startsWith(q),
    );
    // broadcast 候选放前面 —— "全员注意"比"点名某人"更强意图。
    return broadcast ? [broadcast, ...guestMatches] : guestMatches;
  }

  function show(m) {
    const candidates = match(m.query);
    if (candidates.length === 0) {
      hide();
      return;
    }
    dropdownEl.replaceChildren();
    candidates.forEach((g, i) => {
      const li = document.createElement("li");
      if (i === 0) li.className = "active";
      if (g.broadcast) li.classList.add("broadcast");
      li.appendChild(makeBadge("mention-name", null, g.name));
      // broadcast 项不显示 permission 徽章；只茶客有 permission 概念。
      if (!g.broadcast && g.permission && g.permission !== DEFAULT_PERMISSION) {
        li.appendChild(makePermissionBadge(g.permission, "mention-permission"));
      }
      if (g.broadcast) {
        const hint = document.createElement("span");
        hint.className = "mention-hint";
        hint.textContent = "广播";
        li.appendChild(hint);
      }
      li.dataset.name = g.name;
      li.addEventListener("mousedown", (ev) => {
        // mousedown 而非 click —— input blur 前完成补全，避免 dropdown 先消失。
        ev.preventDefault();
        accept(m, g.name);
      });
      dropdownEl.appendChild(li);
    });
    active = 0;
    dropdownEl.hidden = false;
  }

  function hide() {
    dropdownEl.hidden = true;
    active = -1;
  }

  function move(delta) {
    const items = dropdownEl.querySelectorAll("li");
    if (items.length === 0) return;
    items[active]?.classList.remove("active");
    active = (active + delta + items.length) % items.length;
    items[active].classList.add("active");
    items[active].scrollIntoView({ block: "nearest" });
  }

  function accept(m, name) {
    const v = inputEl.value;
    const before = v.slice(0, m.start);
    const after = v.slice(m.end);
    inputEl.value = `${before}@${name} ${after}`;
    const cursor = before.length + 1 + name.length + 1;
    inputEl.setSelectionRange(cursor, cursor);
    hide();
    inputEl.focus();
  }

  return {
    refresh() {
      const m = detect();
      if (!m) {
        hide();
        return;
      }
      show(m);
    },
    hide,
    handleKeydown(ev) {
      if (dropdownEl.hidden) return false;
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        move(1);
      } else if (ev.key === "ArrowUp") {
        ev.preventDefault();
        move(-1);
      } else if (ev.key === "Enter" || ev.key === "Tab") {
        const items = dropdownEl.querySelectorAll("li");
        if (active >= 0 && items[active]) {
          ev.preventDefault();
          const m = detect();
          if (m) accept(m, items[active].dataset.name);
        }
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        hide();
      }
      return true;
    },
  };
}
