// P20.1 茶客实时状态提示 —— guest_status envelope → 气泡提示文案（纯逻辑，node:test 覆盖）。
//
// 设计见 docs/P20.1-茶客实时状态提示.md §3、§5：
// - retrying  → 「正在重连…（第 n/m 次）」，悬停显示原因（如 status=429）；
// - compacting → 「正在整理上下文…」；
// - idle / 未知 state / 坏数据 → null（清提示）。
// 不做倒计时：delay_s 只是计划等待时长，倒数到 0 仍在等反而误导。
//
// 为何是 .mjs：见 flint_core.mjs 头注（app/package.json 不能加 "type":"module"）。

export const LIVE_STATE_RETRYING = "retrying";
export const LIVE_STATE_COMPACTING = "compacting";

function isPosInt(v) {
  return Number.isInteger(v) && v > 0;
}

/** guest_status.data → ``{state, text, title}``；需要清提示时返回 null。 */
export function describeLiveStatus(data) {
  if (!data || typeof data !== "object") return null;
  if (data.state === LIVE_STATE_RETRYING) {
    let count = "";
    if (isPosInt(data.retry)) {
      count = isPosInt(data.max_retries)
        ? `（第 ${data.retry}/${data.max_retries} 次）`
        : `（第 ${data.retry} 次）`;
    }
    const reason = typeof data.reason === "string" && data.reason ? data.reason : "";
    return {
      state: LIVE_STATE_RETRYING,
      text: `正在重连…${count}`,
      title: reason ? `模型服务暂时不可用（${reason}），自动重试中` : "模型服务暂时不可用，自动重试中",
    };
  }
  if (data.state === LIVE_STATE_COMPACTING) {
    return {
      state: LIVE_STATE_COMPACTING,
      text: "正在整理上下文…",
      title: "对话历史较长，正在压缩成摘要，可能需要几十秒",
    };
  }
  return null;
}
