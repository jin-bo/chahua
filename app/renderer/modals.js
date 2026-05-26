"use strict";

// 添加茶客 / 新建房间 / 新建任务 / 导入 persona 四个 modal 的装配。从 renderer.js
// 抽出（renderer 重构）。
//
// 模块自取所需 DOM 节点（这些节点只此处用），renderer 经少量回调注入连接 / 发送 /
// 名单。openModal / closeModal 由 renderer 传入（decision_support 等也共用同一对）。
// 全部 modal 的「点 backdrop / × / ESC 关闭」通用兜底也在这里一并挂。

import { Inbound, buildOwnerOptionData } from "./events.js";
import { renderPersonaPicker, createPersonaImport } from "./persona.js";

export function wireModals({
  isConnected,
  send,
  setStatus,
  getGuests,
  getPersonas,
  pickFolder,
  openModal,
  closeModal,
}) {
  const $ = (id) => document.getElementById(id);

  // ── 添加茶客 ──────────────────────────────────────────────────────
  const addGuestModal = $("add-guest-modal");
  const addGuestListEl = $("add-guest-list");
  $("add-guest").addEventListener("click", () => {
    if (!isConnected()) return;
    const inRoom = new Set(getGuests().map((g) => g.name));
    renderPersonaPicker(addGuestListEl, {
      personas: getPersonas(),
      multi: false,
      excludeNames: inRoom,
      onPick: (p) => {
        // 不送 permission 字段 —— admin 层根据 persona manifest [defaults.guest].permission
        // 与 DEFAULT_MODE 做三级 coalesce（P12 C3 承重不变量第 6 条：默认值合一仅在 admin
        // 层，frontend/inbound 全链路用 None 表示「用户未显式选」）。
        send({
          type: Inbound.ADD_GUEST,
          persona: p.persona,
          name: p.name,
        });
        setStatus("", `添加茶客 ${p.name}…`);
        closeModal(addGuestModal);
      },
    });
    openModal(addGuestModal);
  });

  // ── 新建房间 ──────────────────────────────────────────────────────
  const addRoomModal = $("add-room-modal");
  const newRoomNameEl = $("new-room-name");
  const newRoomTopicEl = $("new-room-topic");
  const newRoomRulesEl = $("new-room-rules");
  const newRoomGuestsEl = $("new-room-guests");
  $("add-room").addEventListener("click", () => {
    if (!isConnected()) return;
    newRoomNameEl.value = "";
    newRoomTopicEl.value = "";
    newRoomRulesEl.value = "";
    renderPersonaPicker(newRoomGuestsEl, {
      personas: getPersonas(),
      multi: true,
      excludeNames: null,
    });
    openModal(addRoomModal);
    newRoomNameEl.focus();
  });
  $("new-room-submit").addEventListener("click", () => {
    if (!isConnected()) return;
    const name = newRoomNameEl.value.trim();
    if (!name) {
      newRoomNameEl.focus();
      return;
    }
    const selected = Array.from(newRoomGuestsEl.querySelectorAll("li.persona-item.selected"));
    if (selected.length === 0) {
      window.alert("至少选 1 位茶客");
      return;
    }
    // 同 ADD_GUEST：不送 permission，admin 层走 manifest defaults → DEFAULT_MODE 三级 coalesce。
    const guestsPayload = selected.map((li) => ({
      persona: li.dataset.persona,
      name: li.dataset.name,
    }));
    // room_id 用 name 直接当目录名 —— server 端 normalize_room_id 会再洗一遍非法字符；
    // 暴露独立 id 字段会增加 UI 复杂度（用户难以理解 id vs name 的区别）。
    send({
      type: Inbound.CREATE_ROOM,
      room_id: name,
      name,
      topic: newRoomTopicEl.value.trim(),
      rules: newRoomRulesEl.value.trim(),
      guests: guestsPayload,
    });
    setStatus("", `新建房间 ${name}…`);
    closeModal(addRoomModal);
  });

  // ── 导入 persona ──────────────────────────────────────────────────
  createPersonaImport({
    modal: $("import-persona-modal"),
    folderPathEl: $("import-folder-path"),
    folderPickBtn: $("import-folder-pick"),
    githubUrlEl: $("import-github-url"),
    submitBtn: $("import-persona-submit"),
    importBtn: $("import-persona"),
    isConnected,
    send,
    setStatus,
    pickFolder,
  });

  // ── 新建任务 ──────────────────────────────────────────────────────
  const newTaskModal = $("new-task-modal");
  const newTaskTitleEl = $("new-task-title");
  const newTaskGoalEl = $("new-task-goal");
  const newTaskOwnerEl = $("new-task-owner");
  const taskPanelNewBtn = $("task-panel-new");
  taskPanelNewBtn.addEventListener("click", () => {
    if (!isConnected() || taskPanelNewBtn.disabled) return;
    newTaskTitleEl.value = "";
    newTaskGoalEl.value = "";
    // owner 选项与任务卡 owner 下拉同源 —— "" 表示全员，submit 时再 normalize 回 null。
    newTaskOwnerEl.replaceChildren();
    for (const data of buildOwnerOptionData(getGuests().map((g) => g.name))) {
      const opt = document.createElement("option");
      opt.value = data.value;
      opt.textContent = data.label;
      newTaskOwnerEl.appendChild(opt);
    }
    newTaskOwnerEl.value = "";
    openModal(newTaskModal);
    newTaskTitleEl.focus();
  });
  $("new-task-submit").addEventListener("click", () => {
    if (!isConnected()) return;
    const title = newTaskTitleEl.value.trim();
    if (!title) {
      newTaskTitleEl.focus();
      return;
    }
    const ownerRaw = newTaskOwnerEl.value;
    send({
      type: Inbound.OPEN_TASK,
      title,
      goal: newTaskGoalEl.value,
      owner: ownerRaw === "" ? null : ownerRaw,
    });
    setStatus("", `新建任务「${title}」…`);
    closeModal(newTaskModal);
  });

  // ── 通用关闭：点 backdrop（modal-backdrop 自身、不是内部 .modal）/ × 按钮 / ESC ──
  const allModals = [
    addGuestModal, addRoomModal, $("edit-user-modal"), $("import-persona-modal"),
    $("edit-room-toml-modal"), $("edit-guest-modal"), $("edit-room-modal"),
    newTaskModal, $("mark-decision-modal"),
  ];
  for (const modal of allModals) {
    modal.addEventListener("click", (ev) => {
      if (ev.target === modal) closeModal(modal);
      if (ev.target.matches("[data-close]")) closeModal(modal);
    });
  }
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    for (const m of allModals) if (!m.hidden) closeModal(m);
  });
}
