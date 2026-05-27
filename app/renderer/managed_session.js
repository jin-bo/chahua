"use strict";

// 托管任务会话（MTS，P8.3 / P8.4，docs/P8.3-原生自动推进.md §8 + P8.4 §6）的前端模块。
//
// 自包含：持本地 MTS 状态（瞬态，刷新 / 切房即清，与 handoff_state 同口径）+ 渲染
// active 任务卡里的「托管」按钮（经 mountManageButton 挂进 task_panel）+ 启动 popover。
// 同一个按钮既是入口又是状态：空闲时「🤖 托管运行」、跑动时「🤖 托管中 · 剩余 N 轮」
// 且点击即停止。三个 managed_session_* envelope 由 renderer.js 转译进
// onStarted / onAdvanced / onEnded；managed_session_ended 的系统气泡由 renderer.js 追。
//
// **P8.4 §6 dormant 派生态**：按钮文案由三源合成 —— MTS 状态（本模块自维）+ handoff
// 队列镜像（``getHandoffQueueLength``）+ 前台 in-flight 状态（``isTurnActive``）。
// dormant = MTS 活 + 队列空 + 无 in-flight → 显式渲「托管中（待机）· {manager} 管理
// · 剩余 N 轮 · 等待用户消息」。不发新 envelope —— 三源都是既有信号（``handoff_*`` /
// ``managed_session_*`` / ``turn_*``），重发等价 envelope 是冗余。

import { Inbound } from "./events.js";
import {
  attachPopoverDismissHandlers,
  positionPopoverAboveAnchor,
} from "./ui_popover.js";

// 镜像 chahua/handoff.py 的两个常量。
export const MAX_MANAGED_BUDGET = 20;
export const MANAGED_SESSION_DEFAULT_BUDGET = 6;

export function createManagedSession({
  send,
  isConnected,
  setStatus,
  getGuests,
  getActiveTask,
  // P8.4 §6：dormant 推导用的两个 getter。缺省时（旧调用方）退化为 false / 0，
  // 行为与 P8.4 之前一致 —— 按钮仍渲「托管中 · 剩余 N 轮」单态文案。
  getHandoffQueueLength = () => 0,
  isTurnActive = () => false,
}) {
  // { manager, budget } | null。budget 是「剩余复查轮数」，advanced 事件倒计时刷新。
  let mts = null;
  let popoverOpen = false;
  let detachDismiss = null;
  // task_panel 每次重渲 active 任务卡时经 mountManageButton 交来的「托管」按钮槽位。
  // 卡被 replaceChildren 顶掉后 manageSlot 变 detached —— renderManageSlot 用 isConnected
  // 守卫跳过陈旧槽位。
  let manageSlot = null;

  function closePopover() {
    const pop = document.querySelector(".managed-popover");
    if (pop) pop.remove();
    if (detachDismiss) {
      detachDismiss();
      detachDismiss = null;
    }
    popoverOpen = false;
  }

  // ── 任务卡里的「托管」按钮（入口 + 状态 + 停止三合一） ────────────────

  // task_panel 重渲 active 任务卡时调：把「托管」按钮槽位交进来。popover 启动用的
  // task 走 getActiveTask()（卡只渲 active 任务，二者恒一致）。
  function mountManageButton(slotEl) {
    manageSlot = slotEl;
    renderManageSlot();
  }

  // 渲染「托管」按钮。空闲 → 「🤖 托管运行」点击弹启动 popover；跑动 → 「🤖 托管中
  // · 剩余 N 轮」点击即停止托管。**P8.4 dormant 子态**：MTS 活 + 队列空 + 无 in-flight
  // → 「🤖 托管中（待机）· {manager} · 剩余 N 轮 · 等待用户消息」，仍可点停止；点击
  // 行为与 active 一致（发 managed_session_stop）。槽位已 detached（任务卡被顶掉）则跳过。
  function renderManageSlot() {
    if (manageSlot === null || !manageSlot.isConnected) return;
    manageSlot.replaceChildren();
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "task-card-manage-btn";
    btn.disabled = !isConnected();
    if (mts !== null) {
      btn.classList.add("is-running");
      // P8.4 §6 dormant 三源推导：本模块持 mts → 队列镜像 → in-flight。
      const queueLen = Math.max(0, getHandoffQueueLength() | 0);
      const turnActive = !!isTurnActive();
      const isDormant = queueLen === 0 && !turnActive;
      const rounds = Math.max(0, mts.budget);
      const manager = mts.manager || "?";
      if (isDormant) {
        btn.classList.add("is-dormant");
        btn.textContent = `🤖 托管中（待机）· ${manager} 管理 · 剩余 ${rounds} 轮 · 等待用户消息 · 点击停止`;
        btn.title =
          "托管会话进入待机：管理者这一轮没派下一步，正等你下一句话续推。点击停止托管 —— 当前发言自然跑完即止；要立即掐断走「全部取消」";
      } else {
        btn.textContent = `🤖 托管中 · 剩余 ${rounds} 轮 · 点击停止`;
        btn.title =
          "停止托管会话 —— 当前发言自然跑完即止；要立即掐断走「全部取消」";
      }
      btn.addEventListener("click", () => {
        if (!isConnected()) return;
        send({ type: Inbound.MANAGED_SESSION_STOP });
      });
    } else {
      btn.textContent = "🤖 托管运行";
      btn.title = "开启托管任务会话：授权一位管理者茶客在预算内自主推进";
      btn.addEventListener("click", () => openPopover(btn));
    }
    manageSlot.appendChild(btn);
  }

  // 连接态 / MTS 态变化时调 —— 重渲按钮。
  function refresh() {
    renderManageSlot();
  }

  // ── 启动 popover ─────────────────────────────────────────────────────

  function openPopover(anchor) {
    if (popoverOpen) {
      closePopover();
      return;
    }
    closePopover();
    if (!isConnected() || mts !== null) return;
    const active = getActiveTask();
    if (!active) {
      setStatus("error", "当前没有任务 —— 先开一个任务才能托管推进");
      return;
    }
    const guests = getGuests();
    if (guests.length === 0) {
      setStatus("error", "房间里没有茶客 —— 无法指派管理者");
      return;
    }
    // 管理者候选 = 任务负责人范围 ∩ 在场茶客。owner 取值：
    // - null / 空串 = 全员 → 候选 = 全部在场茶客；
    // - "user"（与 chahua/user_md.py::USER_SPEAKER_ID 同源 + Task.owner docstring
    //   明确允许）= 用户自己负责 → 不强制限定 AI manager，退化到全员（Codex round 4 P2）；
    // - 茶客名 → 候选 = [该茶客]（若在场）或 空（错误提示引导用户改负责人）。
    const rawOwner = typeof active.owner === "string" && active.owner ? active.owner : null;
    const ownerIsUser = rawOwner === "user";
    const effectiveOwner = ownerIsUser ? null : rawOwner;
    const guestSet = new Set(guests);
    const candidates = effectiveOwner === null
      ? guests
      : (guestSet.has(effectiveOwner) ? [effectiveOwner] : []);
    if (candidates.length === 0) {
      setStatus(
        "error",
        `任务负责人「${effectiveOwner}」不在场 —— 先把 ta 加回房间，或把负责人改成「全员」`,
      );
      return;
    }

    const pop = document.createElement("div");
    pop.className = "popover managed-popover";

    const title = document.createElement("div");
    title.className = "popover-title";
    title.textContent = "托管运行";
    pop.appendChild(title);

    const hint = document.createElement("div");
    hint.className = "managed-popover-hint";
    hint.textContent =
      "授权一位管理者茶客在预算内自主推进：复查 → 指派下一步 → 自动续转。随时可停止。";
    pop.appendChild(hint);

    const mgrLabel = document.createElement("label");
    mgrLabel.className = "managed-popover-field";
    mgrLabel.textContent = "管理者";
    const mgrSelect = document.createElement("select");
    mgrSelect.className = "managed-popover-manager";
    for (const name of candidates) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      mgrSelect.appendChild(opt);
    }
    if (candidates.length === 1) mgrSelect.disabled = true;
    mgrLabel.appendChild(mgrSelect);
    pop.appendChild(mgrLabel);

    if (ownerIsUser) {
      const note = document.createElement("div");
      note.className = "managed-popover-note";
      note.textContent = "当前任务负责人是「用户」；选一位茶客代为推进";
      pop.appendChild(note);
    } else if (effectiveOwner !== null) {
      const note = document.createElement("div");
      note.className = "managed-popover-note";
      note.textContent = `当前任务负责人是「${effectiveOwner}」；想换人请先改任务负责人`;
      pop.appendChild(note);
    }

    const budgetLabel = document.createElement("label");
    budgetLabel.className = "managed-popover-field";
    budgetLabel.textContent = `预算（复查轮数 1–${MAX_MANAGED_BUDGET}）`;
    const budgetInput = document.createElement("input");
    budgetInput.type = "number";
    budgetInput.className = "managed-popover-budget";
    budgetInput.min = "1";
    budgetInput.max = String(MAX_MANAGED_BUDGET);
    budgetInput.step = "1";
    budgetInput.value = String(MANAGED_SESSION_DEFAULT_BUDGET);
    budgetLabel.appendChild(budgetInput);
    pop.appendChild(budgetLabel);

    const confirm = document.createElement("button");
    confirm.type = "button";
    confirm.className = "managed-popover-confirm";
    confirm.textContent = "开始托管";
    confirm.addEventListener("click", (ev) => {
      ev.stopPropagation();
      submit();
    });
    pop.appendChild(confirm);

    function submit() {
      if (!isConnected() || mts !== null) return;
      const task = getActiveTask();
      if (!task) {
        setStatus("error", "当前没有任务");
        closePopover();
        return;
      }
      const budget = Math.trunc(Number(budgetInput.value));
      if (!Number.isFinite(budget) || budget < 1 || budget > MAX_MANAGED_BUDGET) {
        setStatus("error", `预算必须是 1–${MAX_MANAGED_BUDGET} 的整数`);
        return;
      }
      send({
        type: Inbound.MANAGED_SESSION_START,
        task_id: task.id,
        manager_guest: mgrSelect.value,
        budget,
      });
      setStatus("", `已开启托管 —— ${mgrSelect.value} 管理，预算 ${budget} 轮`);
      closePopover();
    }

    document.body.appendChild(pop);
    positionPopoverAboveAnchor(pop, anchor);
    popoverOpen = true;
    detachDismiss = attachPopoverDismissHandlers(pop, closePopover);
  }

  // ── envelope 入口（renderer.js 转译） ───────────────────────────────

  function onStarted(data) {
    mts = {
      manager: data?.manager_guest || "?",
      budget: Number(data?.budget) || 0,
    };
    closePopover();
    refresh();
  }

  function onAdvanced(data) {
    if (mts === null) return;
    if (data?.manager_guest) mts.manager = data.manager_guest;
    mts.budget = Number(data?.remaining_budget) || 0;
    refresh();
  }

  function onEnded(_data) {
    mts = null;
    closePopover();
    refresh();
  }

  // 切房 / 断线 / clear_room —— 与 handoff_state.reset 同口径强清。
  function reset() {
    mts = null;
    closePopover();
    refresh();
  }

  return { onStarted, onAdvanced, onEnded, reset, refresh, mountManageButton };
}
