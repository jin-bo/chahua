"use strict";

// 显式 handoff 队列的前端镜像（docs/P7 §3.3）。队列是调度层瞬态——服务端不落盘、
// 不进 TASK_INFO 权威快照；前端靠三个 handoff_* 事件维护本地预览，刷新 / 切房即清。
//
// 事件分工：
//   - HANDOFF_ENQUEUED ``{queue}`` 是权威快照 → 整体替换，不按单项增量拼；
//   - HANDOFF_CONSUMED ``{item}`` → 该项已被 drain loop 消费，从队首移除；
//   - HANDOFF_CLEARED  → 队列清空。
//
// 状态对外冻结，与 task_state.js 同口径——消费方不应误改造成"前端改 state 不回写"。

const listeners = new Set();

let queue = Object.freeze([]);

function publish() {
  // F12 console 直接 ``__chahua_handoff_state`` 取当前队列，dev 调试用。
  if (typeof window !== "undefined") window.__chahua_handoff_state = queue;
  for (const fn of listeners) {
    try { fn(queue); } catch (err) { console.error("handoff_state listener", err); }
  }
}

// HANDOFF_ENQUEUED：服务端发整份快照，前端整体替换。
export function applyEnqueued(nextQueue) {
  queue = Object.freeze(Array.isArray(nextQueue) ? nextQueue.slice() : []);
  publish();
}

// HANDOFF_CONSUMED：drain loop popleft 了队首该项。按 created_at_ms 定位移除，
// 兜底移除队首——FIFO 消费下队首即被消费项。
export function applyConsumed(item) {
  if (queue.length === 0) return;
  const stamp = item && item.created_at_ms;
  let idx = 0;
  if (typeof stamp === "number") {
    const found = queue.findIndex((q) => q && q.created_at_ms === stamp);
    if (found >= 0) idx = found;
  }
  const next = queue.slice();
  next.splice(idx, 1);
  queue = Object.freeze(next);
  publish();
}

// HANDOFF_CLEARED：全部取消。
export function applyCleared() {
  if (queue.length === 0) return;
  queue = Object.freeze([]);
  publish();
}

// 切房 / 重连 / clear_room —— 与 in-flight 流、proposalCard 去重指纹同口径强清。
export function reset() {
  queue = Object.freeze([]);
  publish();
}

export function getQueue() {
  return queue;
}

// 订阅：注册即触发一次让消费方拿到初值；返回 unsubscribe。
export function subscribe(fn) {
  listeners.add(fn);
  try { fn(queue); } catch (err) { console.error("handoff_state listener", err); }
  return () => listeners.delete(fn);
}
