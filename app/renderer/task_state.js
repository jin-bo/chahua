"use strict";

// 任务房间状态镜像（docs/P5-任务房间.md §4.2 事件分工）。前端任务状态以最近一次
// ``task_info`` 为准；``task_open`` / ``task_update`` / ``task_decision_added`` /
// ``task_artifact_added`` 是 hint，由 task_panel / composer chip 自己挑场合 flash。
//
// 状态对外冻结，避免消费方误改造成"前端误改 state 不回写 server"的边界面；订阅者
// 每次 ``task_info`` 都收到全量新 snapshot（不 diff，量小）。

const listeners = new Set();

const EMPTY = Object.freeze({ tasks: Object.freeze([]), activeTaskId: null });

let state = EMPTY;
let lastSerialized = "";
// 懒建索引 —— 每次 setSnapshot 清掉；getTaskById 第一次调时 rebuild。message-chip
// refresh 一次走 N 条消息时 O(N*M) 退化到 O(N+M)。
let tasksById = null;

function normalize(envData) {
  if (envData == null) return EMPTY;
  const tasks = Array.isArray(envData.tasks) ? envData.tasks : [];
  const activeRaw = envData.active_task_id;
  const activeTaskId = typeof activeRaw === "string" && activeRaw ? activeRaw : null;
  return Object.freeze({ tasks: Object.freeze(tasks.slice()), activeTaskId });
}

export function setSnapshot(envData) {
  const next = normalize(envData);
  // server 在多种 mutator / 重连快照路径下都会重发 task_info；当负载语义没变时跳过
  // 监听者重渲（markdown 解析 + DOM 重建是面板里最重的活）。
  const serialized = JSON.stringify(next);
  if (serialized === lastSerialized) return;
  lastSerialized = serialized;
  state = next;
  tasksById = null;
  // F12 console 直接 ``__chahua_task_state`` 取当前快照，dev 调试用。
  if (typeof window !== "undefined") window.__chahua_task_state = state;
  for (const fn of listeners) {
    try { fn(state); } catch (err) { console.error("task_state listener", err); }
  }
}

export function getState() {
  return state;
}

export function getTaskById(id) {
  if (!id) return null;
  if (!tasksById) {
    tasksById = new Map();
    for (const t of state.tasks) {
      if (t && typeof t.id === "string") tasksById.set(t.id, t);
    }
  }
  return tasksById.get(id) ?? null;
}

export function getActiveTask() {
  return getTaskById(state.activeTaskId);
}

// 订阅：注册即触发一次让消费方拿到初值；返回 unsubscribe。
export function subscribe(fn) {
  listeners.add(fn);
  try { fn(state); } catch (err) { console.error("task_state listener", err); }
  return () => listeners.delete(fn);
}
