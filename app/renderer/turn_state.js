"use strict";

// 当前 turn 状态 + 发送/停止按钮。从 renderer.js 抽出（renderer 重构）。
//
// P3.3 cancel 状态机：turn_start 时 set、turn_end(next=user) 或 status=cancelled 时
// clear；turn_end(next=ai) 不 clear（下个 turn_start 会刷新）。currentTurnId 非空即
// "AI 链在跑" —— submit button 切到「停止」语义，submit handler 路由到 cancel 帧。
// currentTurnId 全封进模块，外部只经 isActive / turnId / set / clear / syncToRoom 交互。

export function createTurnState({ submitBtn }) {
  // P9：切到一个后台正在跑 turn 的房间时，前端没有那个 turn 的真实 id（turn_start 在
  // 切房前就发过了）。用这个哨兵占位让「停止」按钮正确显示 —— render 只看 currentTurnId
  // 是否非空、cancel 帧服务端只认 type 不校验 turn_id。真实 turn_start 到达会覆盖它。
  const FOREGROUND_BUSY_TURN_PLACEHOLDER = "turn_foreground_busy";
  // 同一个按钮承担「发送 / 停止」双重职责；aria-label 同步切，屏读器读"发送"/"停止"
  // 而不是裸符号。颜色由 .stop 类 + style.css 控制。
  const SEND_ICON = "↑";
  const STOP_ICON = "■";

  let currentTurnId = null;

  function render() {
    const busy = currentTurnId !== null;
    submitBtn.textContent = busy ? STOP_ICON : SEND_ICON;
    submitBtn.setAttribute("aria-label", busy ? "停止" : "发送");
    submitBtn.title = busy ? "停止当前轮次" : "发送 (Enter)";
    submitBtn.classList.toggle("stop", busy);
  }

  return {
    isActive: () => currentTurnId !== null,
    turnId: () => currentTurnId,
    // turn_start：记下真实 turn id。
    set(turnId) {
      currentTurnId = turnId;
      render();
    },
    // turn_end(next=user) / cancelled / 断线：清空 → 按钮复原「发送」。
    clear() {
      currentTurnId = null;
      render();
    },
    // renderSidebar 切房：按新前台房的 busy 快照决定占位 or 清。从忙房切到闲房 →
    // 复原「发送」；切进一个后台仍在跑的房 → 维持「停止」（哨兵占位，真实
    // turn_start/turn_end 后续会校准）。
    syncToRoom(busy) {
      currentTurnId = busy ? FOREGROUND_BUSY_TURN_PLACEHOLDER : null;
      render();
    },
  };
}
