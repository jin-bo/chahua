"use strict";

// 侧栏房间列表 + P9「进行中」后台房徽标。从 renderer.js 抽出（renderer 重构）。
//
// 「进行中」后台房集合（backgroundActiveRooms）全封进模块 —— renderer 只经 render
// （room_info 来时按权威快照重建 + 渲染）/ onBackgroundMilestone（后台里程碑事件增量
// 维护）两个出口交互。后台房集合按 envelope room_id（= room.name）键。

import { EventType, Inbound } from "./events.js";

export function createRoomList({ roomsEl, isConnected, send, setStatus }) {
  // P9：当前判定「进行中」的后台房间 envelope room_id 集合。后台里程碑事件 add、
  // room_background_finished remove；refreshRoomBadges 据此渲染列表「进行中」徽标。
  const backgroundActiveRooms = new Set();

  // 切换房间列表 —— 列其它房间（含当前），click 非 current 项发 switch_room frame。
  // 当前房间高亮、不可点；其它房间显示 name + topic 一句话预览，hover 出现"删除"按钮。
  function renderRoomsList(roomsAvailable, currentRoomId) {
    roomsEl.replaceChildren();
    const rooms = Array.isArray(roomsAvailable) ? roomsAvailable : [];
    for (const r of rooms) {
      const li = document.createElement("li");
      li.dataset.roomId = r.room_id;
      // P9：后台房徽标按 envelope room_id（= room.name）匹配 —— 存 r.name 供
      // refreshRoomBadges 查（envelope 顶层 room_id 用 room.name，不是目录名）。
      li.dataset.roomName = r.name || r.room_id;
      if (r.room_id === currentRoomId) li.classList.add("current");
      // text 块外裹 .room-meta，方便 row-remove 用 margin-left:auto 推到右侧。
      const meta = document.createElement("div");
      meta.className = "room-meta";
      const name = document.createElement("div");
      name.className = "room-name";
      name.textContent = r.name || r.room_id;
      meta.appendChild(name);
      if (r.topic) {
        const topic = document.createElement("div");
        topic.className = "room-topic";
        topic.textContent = r.topic;
        meta.appendChild(topic);
      }
      li.appendChild(meta);
      if (r.room_id !== currentRoomId) {
        li.addEventListener("click", () => {
          if (!isConnected()) return;
          send({ type: Inbound.SWITCH_ROOM, room_id: r.room_id });
          setStatus("", `切换到 ${r.name || r.room_id}…`);
        });
        // 删除房间按钮 —— 只对非当前房显示。当前房不能删（先切走再删，与 server 端约束一致）。
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "row-icon-btn row-remove";
        remove.textContent = "×";
        remove.title = `删除房间 ${r.name || r.room_id}`;
        remove.addEventListener("click", (ev) => {
          ev.stopPropagation();
          if (!isConnected()) return;
          if (!window.confirm(`删除房间「${r.name || r.room_id}」？\n房间目录及其全部历史 / 摘要 / 茶客工作区会被永久删除，无法撤销。`)) return;
          send({ type: Inbound.DELETE_ROOM, room_id: r.room_id });
          setStatus("", `删除 ${r.name || r.room_id}…`);
        });
        li.appendChild(remove);
      }
      roomsEl.appendChild(li);
    }
    refreshRoomBadges();
  }

  // P9：按 backgroundActiveRooms 集合刷新房间列表「进行中」徽标 —— 不重建整个列表
  // （避免后台事件高频到达时房间列表闪烁）。徽标只挂非当前房；当前房自身活动由聊天区
  // 体现。徽标元素增量挂 / 摘，不动其余 DOM。
  function refreshRoomBadges() {
    // [...children] 快照 —— 当前循环只改 li 内的徽标 span、不增删 li，但 children 是
    // live HTMLCollection，快照后即便将来改成增删 li 也不会漏元素。
    for (const li of [...roomsEl.children]) {
      const active =
        !li.classList.contains("current") &&
        backgroundActiveRooms.has(li.dataset.roomName);
      let badge = li.querySelector(".room-busy-badge");
      if (active && !badge) {
        badge = document.createElement("span");
        badge.className = "room-busy-badge";
        badge.textContent = "进行中";
        badge.title = "该房间正在后台续跑";
        li.querySelector(".room-name")?.appendChild(badge);
      } else if (!active && badge) {
        badge.remove();
      }
    }
  }

  return {
    // room_info 来时：从权威快照 rooms_available[].busy 重建「进行中」后台房集合
    // （切房 / 重连后这帧是真理源 —— 剔除已结束的、补上切走后转后台的；当前房不计），
    // 再渲染房间列表。随后到的后台里程碑事件经 onBackgroundMilestone 增量维护。
    render(roomsAvailable, currentRoomId) {
      backgroundActiveRooms.clear();
      for (const r of roomsAvailable ?? []) {
        if (r.busy && r.room_id !== currentRoomId) {
          backgroundActiveRooms.add(r.name);
        }
      }
      renderRoomsList(roomsAvailable, currentRoomId);
    },
    // handleEnvelope 后台房间里程碑分流 —— room_background_finished = 后台 runtime
    // 跑完自毁；其余里程碑（turn_* / message_end / task_* / managed_session_*）→
    // 标记该房后台仍在跑。维护集合 + 刷新徽标。
    onBackgroundMilestone(env) {
      const roomId = env.room_id;
      if (!roomId) return;
      if (env.type === EventType.ROOM_BACKGROUND_FINISHED) {
        backgroundActiveRooms.delete(roomId);
        console.debug("[room_background_finished]", roomId);
      } else {
        backgroundActiveRooms.add(roomId);
      }
      refreshRoomBadges();
    },
  };
}
