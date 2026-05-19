"use strict";

// 三栏拖拽 splitter。一个 splitter = 一个被它驱动的 CSS 变量（``--sidebar-width`` /
// ``--right-panel-width``）；拖动时直接写 ``document.documentElement.style``，让所
// 有引用该变量的 panel 同步生效（task / debug 抽屉互斥但共享右侧宽度变量 → 拖一次
// 两边都对齐）。
//
// 持久化：mouseup 时写 localStorage；启动时读回灌进 :root。隐私模式 / sandbox 读
// 写 localStorage 偶尔抛 SecurityError，全部 try/catch 包到底（同 task_panel 折叠
// pref 同口径），失败回 fallback 默认。

const LS_PREFIX = "chahua.splitter.";

// 单点 clamp —— 给键盘 / 鼠标 / localStorage 三条入口共用，避免某条路径忘 clamp。
function clamp(px, min, max) {
  return Math.max(min, Math.min(max, px));
}

function readPref(varName) {
  try {
    const raw = window.localStorage.getItem(LS_PREFIX + varName);
    if (raw === null) return null;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? n : null;
  } catch {
    return null;
  }
}

function writePref(varName, px) {
  try {
    window.localStorage.setItem(LS_PREFIX + varName, String(Math.round(px)));
  } catch {
    // intentional drop
  }
}

function setVar(varName, px) {
  document.documentElement.style.setProperty(varName, `${Math.round(px)}px`);
}

/**
 * 装一个 splitter。
 *
 * @param {HTMLElement} el       splitter DOM 节点（``.splitter``）。
 * @param {string} varName       要驱动的 CSS 变量名，含 ``--`` 前缀。
 * @param {object} opts
 * @param {number} opts.min      最小宽度（px）。
 * @param {number} opts.max      最大宽度（px）。
 * @param {"left"|"right"} opts.edge
 *   被驱动元素位于 splitter 哪一侧：``left`` = splitter 左侧元素的宽度受控（如
 *   sidebar）；``right`` = splitter 右侧元素受控（如 right panel）。决定鼠标 dx
 *   方向。
 * @param {number} opts.fallback  当 localStorage 没值时的初始宽度（与 CSS 默认对齐）。
 */
export function createSplitter(el, varName, { min, max, edge, fallback }) {
  // 启动时回灌持久化值（CSS 默认值 fallback 作兜底）。
  const initial = readPref(varName);
  if (initial !== null) setVar(varName, clamp(initial, min, max));

  let startX = 0;
  let startPx = 0;
  let dragging = false;

  function currentPx() {
    // 拿到当前 var 解析后的实际 px。读 element style 拿不到 CSS 默认；getComputedStyle
    // 返回根元素属性。
    const val = getComputedStyle(document.documentElement).getPropertyValue(varName);
    const n = parseFloat(val);
    return Number.isFinite(n) ? n : fallback;
  }

  function onMouseMove(ev) {
    if (!dragging) return;
    const dx = ev.clientX - startX;
    const next = edge === "left" ? startPx + dx : startPx - dx;
    setVar(varName, clamp(next, min, max));
  }

  function onMouseUp() {
    if (!dragging) return;
    dragging = false;
    el.classList.remove("dragging");
    document.body.classList.remove("splitter-dragging");
    document.removeEventListener("mousemove", onMouseMove);
    document.removeEventListener("mouseup", onMouseUp);
    writePref(varName, currentPx());
  }

  el.addEventListener("mousedown", (ev) => {
    // 左键独占；其它按钮不进拖动（防止右键菜单弹出后状态卡死）。
    if (ev.button !== 0) return;
    ev.preventDefault();
    dragging = true;
    startX = ev.clientX;
    startPx = currentPx();
    el.classList.add("dragging");
    document.body.classList.add("splitter-dragging");
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
  });

  // 键盘可达性：箭头键步进 16px，Shift+箭头 64px。与拖动同一 clamp。
  el.addEventListener("keydown", (ev) => {
    let dir = 0;
    if (ev.key === "ArrowLeft") dir = edge === "left" ? -1 : 1;
    else if (ev.key === "ArrowRight") dir = edge === "left" ? 1 : -1;
    else return;
    ev.preventDefault();
    const step = (ev.shiftKey ? 64 : 16) * dir;
    const next = clamp(currentPx() + step, min, max);
    setVar(varName, next);
    writePref(varName, next);
  });

  return {
    /** 重置回默认宽度并清持久化值。 */
    reset() {
      document.documentElement.style.removeProperty(varName);
      try {
        window.localStorage.removeItem(LS_PREFIX + varName);
      } catch {
        // intentional drop
      }
    },
  };
}
