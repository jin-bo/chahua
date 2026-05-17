"use strict";

// 任务面板（docs/P5-任务房间.md §5.1）。
//
// 渲染契约：
// - 订阅 ``task_state`` —— 每次 ``task_info`` envelope 到达就全量重渲（任务量个位
//   数，diff 不值）。``TASK_OPEN`` / ``TASK_UPDATE`` / ``TASK_DECISION_ADDED`` /
//   ``TASK_ARTIFACT_ADDED`` 这些 hint 走 ``flashHint`` 入口高亮增量项，不重渲；
//   server 端会随后追发 ``task_info`` 让面板状态本身向前推一步。
// - 折叠状态记 ``localStorage('chahua.taskPanel.collapsed')`` —— 这是 user prefs
//   而非房间属性，跨房 / 跨重启都保留。
//
// 风格契合 sidebar：色调延续米黄；markdown goal 走 ``renderMarkdown`` 与气泡同一
// 渲染管线（XSS 由 dompurify 兜底）。

import { renderMarkdown } from "./chat_view.js";
import { EventType, taskStatusLabel } from "./events.js";
import * as taskState from "./task_state.js";

const LS_COLLAPSED_KEY = "chahua.taskPanel.collapsed";
// 高亮 hint 持续时间 —— 600ms 是 P3 sidebar 选中态淡出口径，复用同一感觉。
const FLASH_MS = 600;
// 第一帧后才挂回 transition 类，避免冷启动从 280px → 32px 的折叠态首屏抖动。
const ANIMATED_CLASS = "task-panel-animated";

function readCollapsed() {
  try {
    return window.localStorage.getItem(LS_COLLAPSED_KEY) === "1";
  } catch {
    // 隐私模式 / sandbox 偶尔抛 SecurityError —— 当未折叠处理。
    return false;
  }
}

function writeCollapsed(collapsed) {
  try {
    window.localStorage.setItem(LS_COLLAPSED_KEY, collapsed ? "1" : "0");
  } catch {
    // 写失败就让它失败 —— 下次还是默认展开。
  }
}

function formatSize(bytes) {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatTs(ms) {
  if (typeof ms !== "number" || !Number.isFinite(ms)) return "";
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function flashEl(el) {
  if (!el) return;
  el.classList.remove("task-flash");
  // 强制 reflow 让 CSS transition 重启（同 class 反复加不会重触发）。
  void el.offsetWidth;
  el.classList.add("task-flash");
  setTimeout(() => el.classList.remove("task-flash"), FLASH_MS);
}

function cssEscape(s) {
  // task_id / decision_id 都是 ``<prefix>_<hex>`` 形式（events.py new_id），不含 CSS
  // 特殊字符；fallback 仅转义 " 与 \ 已足够。
  if (typeof window !== "undefined" && window.CSS && typeof window.CSS.escape === "function") {
    return window.CSS.escape(String(s ?? ""));
  }
  return String(s ?? "").replace(/["\\]/g, "\\$&");
}

export function createTaskPanel({
  panelEl,
  toggleBtnEl,
  bodyEl,
  // (taskId, patch) → 发 update_task inbound 的回调。title / goal blur 后调；调用方
  // 自己判断是否真的有变化（这里只在前后值不同时调）。无 callback 时不挂 inline 编辑。
  onPatchTask = null,
}) {
  let collapsed = readCollapsed();
  applyCollapsed();
  // 装好折叠态后再下一帧打开 transition —— 冷启动若 localStorage = 1，不要让用户看到
  // 280px → 32px 的滑动；之后用户主动点折叠按钮再有动画。
  requestAnimationFrame(() => panelEl.classList.add(ANIMATED_CLASS));

  toggleBtnEl.addEventListener("click", () => {
    collapsed = !collapsed;
    writeCollapsed(collapsed);
    applyCollapsed();
  });

  function applyCollapsed() {
    panelEl.dataset.collapsed = collapsed ? "1" : "0";
    toggleBtnEl.textContent = collapsed ? "◀" : "▶";
    toggleBtnEl.title = collapsed ? "展开任务面板" : "折叠任务面板";
  }

  function render(state) {
    bodyEl.replaceChildren();
    const active = taskState.getActiveTask();
    if (!active) {
      bodyEl.appendChild(renderEmpty(state));
      return;
    }
    bodyEl.appendChild(renderTaskCard(active));
    bodyEl.appendChild(renderArtifacts(active));
    bodyEl.appendChild(renderDecisions(active));
    consumePendingFlashes();
  }

  function renderEmpty(state) {
    const hint = document.createElement("div");
    hint.className = "task-empty";
    // "有任务但 active 丢"在 P5.1 不会发生（一房间最多 1 任务 + 加载时双向修复
    // state.json↔task.json）；保留 fallback 应对 P5.2 起的过渡窗口。
    if (state.tasks.length === 0) {
      hint.textContent = "还没有任务。新建一个把这场聊天升级为任务房间。";
    } else {
      hint.textContent = "任务暂未激活（state.json 缺失？）—— 重启 sidecar 应自动修复。";
    }
    return hint;
  }

  function renderTaskCard(task) {
    const card = document.createElement("section");
    card.className = "task-card";
    card.dataset.taskId = task.id;

    const titleRow = document.createElement("div");
    titleRow.className = "task-card-title-row";
    const titleIcon = document.createElement("span");
    titleIcon.className = "task-card-icon";
    titleIcon.textContent = "📌";
    titleRow.appendChild(titleIcon);
    const titleEl = document.createElement("div");
    titleEl.className = "task-card-title";
    titleEl.textContent = task.title || "";
    if (onPatchTask) {
      attachInlineEditor(titleEl, {
        field: "title",
        task,
        singleLine: true,
        placeholder: "（无标题，点这里加）",
      });
    } else if (!task.title) {
      titleEl.textContent = "（无标题）";
    }
    titleRow.appendChild(titleEl);
    card.appendChild(titleRow);

    // goal 在 inline 编辑模式下退掉 markdown 渲染（用 plaintext contenteditable，
    // 避免 contenteditable 上的 markdown DOM 被浏览器输入工具改写）。无 callback 时
    // 保留原 markdown 渲染。
    if (onPatchTask) {
      const goal = document.createElement("div");
      goal.className = "task-card-goal task-card-goal-editable";
      goal.textContent = task.goal || "";
      attachInlineEditor(goal, {
        field: "goal",
        task,
        singleLine: false,
        placeholder: "（点这里写目标 / 验收口径）",
      });
      card.appendChild(goal);
    } else if (task.goal) {
      const goal = document.createElement("div");
      goal.className = "task-card-goal markdown";
      goal.innerHTML = renderMarkdown(task.goal);
      card.appendChild(goal);
    }

    const meta = document.createElement("dl");
    meta.className = "task-card-meta";
    appendMetaRow(meta, "负责人", task.owner || "全员");
    appendMetaRow(meta, "状态", taskStatusLabel(task.status));
    if (task.created_at_ms) {
      appendMetaRow(meta, "建于", formatTs(task.created_at_ms));
    }
    card.appendChild(meta);

    return card;
  }

  // contenteditable inline 编辑器 —— focus 时记原值，blur 时若文本变了就调
  // onPatchTask。ESC 撤回；title 上的 Enter 提交（防回车换行），goal 允许换行。
  // server task_info 回环重渲面板会清掉当前 DOM 节点；但编辑链路是同步 user → blur
  // → send → 等 server 回 → 重渲，blur 时焦点已离开，重渲不抢焦点。
  function attachInlineEditor(el, { field, task, singleLine, placeholder }) {
    // plaintext-only：Chromium 支持，剥粘贴的 rich-text 格式。换行控制走 keydown
    // 拦截 —— title 上 Enter 直接 commit（blur）；goal 让 Enter 走默认（插入 \n）。
    el.contentEditable = "plaintext-only";
    el.dataset.placeholder = placeholder;
    el.spellcheck = false;
    let original = el.textContent;
    el.addEventListener("focus", () => {
      original = el.textContent;
    });
    el.addEventListener("blur", () => {
      const next = singleLine ? el.textContent.trim() : el.textContent;
      if (next === original) return;
      // title 空串走拒绝路径 —— server 端 update_task 也不接受空 title，前端先早返
      // 避免一次无意义的来回 + NOTICE error 弹窗。
      if (singleLine && next === "") {
        el.textContent = original;
        return;
      }
      onPatchTask(task.id, { [field]: next });
    });
    el.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") {
        el.textContent = original;
        ev.preventDefault();
        el.blur();
      } else if (singleLine && ev.key === "Enter") {
        ev.preventDefault();
        el.blur();
      }
    });
  }

  function appendMetaRow(dl, label, value) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    dl.appendChild(dt);
    dl.appendChild(dd);
  }

  // 抽出来给 artifacts / decisions 共用：section 容器 + 标题计数 + 空态 / ul 列表
  // 都同型。renderItem 是 (item) → <li> —— 调用方只关心 per-item 渲染。
  function renderListSection({ kind, title, items, emptyText, renderItem }) {
    const section = document.createElement("section");
    section.className = "task-section";
    section.dataset.kind = kind;

    const head = document.createElement("div");
    head.className = "task-section-head";
    head.textContent = `${title} (${items.length})`;
    section.appendChild(head);

    if (items.length === 0) {
      const empty = document.createElement("div");
      empty.className = "task-section-empty";
      empty.textContent = emptyText;
      section.appendChild(empty);
      return section;
    }
    const ul = document.createElement("ul");
    ul.className = `task-${kind}`;
    for (const item of items) {
      ul.appendChild(renderItem(item));
    }
    section.appendChild(ul);
    return section;
  }

  function renderArtifacts(task) {
    return renderListSection({
      kind: "artifacts",
      title: "产物",
      items: Array.isArray(task.artifacts) ? task.artifacts : [],
      emptyText: "暂无",
      renderItem: (a) => {
        const li = document.createElement("li");
        li.className = "task-artifact";
        li.dataset.rel = a.rel || "";
        const name = document.createElement("span");
        name.className = "task-artifact-name";
        name.textContent = a.name || a.rel || "(unnamed)";
        li.appendChild(name);
        const detail = document.createElement("span");
        detail.className = "task-artifact-detail";
        const parts = [];
        const sizeText = formatSize(a.size);
        if (sizeText) parts.push(sizeText);
        if (a.created_by && a.created_by !== "user") parts.push(`by ${a.created_by}`);
        detail.textContent = parts.join("，");
        li.appendChild(detail);
        return li;
      },
    });
  }

  function renderDecisions(task) {
    return renderListSection({
      kind: "decisions",
      title: "决策",
      items: Array.isArray(task.decisions) ? task.decisions : [],
      emptyText: "暂无",
      renderItem: (d) => {
        const li = document.createElement("li");
        li.className = "task-decision";
        li.dataset.decisionId = d.decision_id || "";
        const text = document.createElement("div");
        text.className = "task-decision-text";
        text.textContent = d.summary || "(空决策)";
        li.appendChild(text);
        const meta = document.createElement("div");
        meta.className = "task-decision-meta";
        const tsText = formatTs(d.ts_ms);
        const supCount = Array.isArray(d.supporting_message_ids) ? d.supporting_message_ids.length : 0;
        const parts = [];
        if (tsText) parts.push(tsText);
        if (supCount > 0) parts.push(`引用 ${supCount} 条消息`);
        meta.textContent = parts.join("　·　");
        li.appendChild(meta);
        return li;
      },
    });
  }

  // hint envelope 总是先于 TASK_INFO 到达（server 端 _inbound_add_decision /
  // _inbound_attach_artifact emit hint 后才发 task_info 快照）—— 此刻新增的 li 还没
  // 出现在 DOM 里，直接 querySelector 必然 null。把待 flash 的 key 入队，等 render
  // 重建完 DOM 后再走 consumePendingFlashes 命中新 li。
  // TASK_OPEN / TASK_UPDATE 不闪：整张 task-card 会被 replaceChildren 顶掉，没法做
  // 有意义的高亮（要做得移到卡片级"刚开任务"动画，留 P5.2 再评估）。
  const pendingFlashes = [];

  function flashHint(envType, data) {
    if (!data) return;
    if (envType === EventType.TASK_ARTIFACT_ADDED && data.rel) {
      pendingFlashes.push({ selector: `.task-artifact[data-rel="${cssEscape(data.rel)}"]` });
    } else if (envType === EventType.TASK_DECISION_ADDED && data.decision_id) {
      pendingFlashes.push({ selector: `.task-decision[data-decision-id="${cssEscape(data.decision_id)}"]` });
    }
  }

  function consumePendingFlashes() {
    while (pendingFlashes.length > 0) {
      const { selector } = pendingFlashes.shift();
      flashEl(bodyEl.querySelector(selector));
    }
  }

  const unsubscribe = taskState.subscribe(render);

  return {
    flashHint,
    dispose() { unsubscribe(); },
  };
}
