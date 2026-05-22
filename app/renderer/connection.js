"use strict";

// WebSocket 连接 + 重连退避。从 renderer.js 抽出（renderer 重构）。
//
// 持有 socket 与 connected 标志；renderer 经 send / isConnected 读写连接，经
// onConnecting / onMessage / onClose 三个回调接生命周期。重连决策（退避梯度 /
// NO_RECONNECT_CODES）整个封在模块内。

// 退避梯度（ms）。超过梯度长度后稳定在最后一档，永远尝试 —— 桌面 App 无云端账号，
// 用户没退就接着连；不退指数到分钟级避免长时间断线后用户等不及。
const RECONNECT_BACKOFF_MS = [1000, 2000, 5000, 10000];

// 这些 close code 表示对端"主动且无意挽回"，不自动重连：
//   1000 normal closure（双方约定关）
//   1001 going away（页面切走 / 后端 quit）
//   1008 policy violation（server.py 拒第二客户端 —— 重连只会再次被拒）
const NO_RECONNECT_CODES = new Set([1000, 1001, 1008]);

export function createConnection({
  wsUrl,
  setStatus,
  onConnecting,
  onMessage,
  onClose,
}) {
  let ws = null;
  let connected = false;
  let reconnectAttempt = 0;

  function reconnectDelayMs() {
    const i = Math.min(reconnectAttempt, RECONNECT_BACKOFF_MS.length - 1);
    return RECONNECT_BACKOFF_MS[i];
  }

  function scheduleReconnect() {
    const delay = reconnectDelayMs();
    reconnectAttempt += 1;
    setStatus("error", `连接断开 —— 第 ${reconnectAttempt} 次重试，${delay / 1000}s 后…`);
    // P3.3 加"立即重连 / 停止重连"按钮时再持有 timer 引用便于 clearTimeout。
    setTimeout(connect, delay);
  }

  function connect() {
    // 重试中文案 vs 首次连接区分开 —— 首次空白，重试带次数。
    const tag = reconnectAttempt > 0 ? `（第 ${reconnectAttempt} 次重试）` : "";
    setStatus("", `连接中… ${wsUrl}${tag}`);
    onConnecting();
    ws = new WebSocket(wsUrl);
    ws.addEventListener("open", () => {
      connected = true;
      reconnectAttempt = 0;
      setStatus("ok", `已连接 ${wsUrl}（等 room_info）`);
      // composer 解锁延迟到 renderSidebar —— 让 user echo 名字、@ 候选都准备就绪
      // 后再让用户能输入；避免 "我" → 真名跳变 + @ 候选空白窗口。
    });
    ws.addEventListener("message", (ev) => {
      try {
        onMessage(JSON.parse(ev.data));
      } catch (e) {
        console.error("envelope parse failed:", e, ev.data);
      }
    });
    ws.addEventListener("close", (ev) => {
      connected = false;
      // renderer 侧断线清理（输入禁用 / in-flight 收尾 / handoff / MTS / 上传 / 按钮）。
      onClose(ev.code, ev.reason);
      if (NO_RECONNECT_CODES.has(ev.code)) {
        setStatus("error", `连接断开 (${ev.code} ${ev.reason || ""})`);
        return;
      }
      scheduleReconnect();
    });
    ws.addEventListener("error", (ev) => {
      console.error("ws error", ev);
    });
  }

  return {
    start: connect,
    // 闭包读最新 ws —— 重连时 ws 被重赋值，每次调用读到当前绑定。
    send: (payload) => ws.send(JSON.stringify(payload)),
    isConnected: () => connected,
  };
}
