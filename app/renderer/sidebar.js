"use strict";

// 侧栏茶客列表 + 头像 + 打分小数字 + 「我」那一行。从 renderer.js 抽出（renderer 重构）。
//
// 拥有：茶客打分明细（scoresByName）+ 茶客行 → 打分 span 引用（scoreSpansByName）+
// 用户显示名 / 头像。茶客名单（guests）仍归 renderer（被一众模块读），本模块经
// getGuests 取数。renderer 经 makeAvatar / makeUserAvatar（喂 chat_stream）/ setUser /
// renderGuests / applyScores / getUserDisplayName 交互。

import {
  makeAvatarImg,
  scoreText,
  makeBadge,
  makePermissionBadge,
} from "./chat_view.js";
import { Inbound, ScoreKind, DEFAULT_PERMISSION } from "./events.js";

export function createSidebar({
  guestsEl,
  userNameEl,
  userAvatarWrapEl,
  isConnected,
  send,
  setStatus,
  getGuests,
  showPermissionPopover,
}) {
  // 茶客行 → 打分 span 的引用。renderGuests 重建时填，applyScores 直接改文字，
  // 不重建 DOM（避免头像 / 徽章闪烁）。
  const scoreSpansByName = new Map();
  // 当前 turn 的打分明细。turn_start replace / turn_end 清（经 applyScores）。
  let scoresByName = new Map();
  let userDisplayName = "我";
  let userAvatarDataUri = null;

  // 茶客头像 —— 按名字在 guests 数组里 find；茶客 ≤ 个位数，线性查比维护并行 Map 简单
  // （且任何 guests 变更都自动跟上）。
  function makeAvatar(name, className) {
    return makeAvatarImg(
      getGuests().find((g) => g.name === name)?.avatar_data_uri,
      className,
      name,
    );
  }

  // 用户头像 —— 走 userAvatarDataUri（room_info 时装）。
  function makeUserAvatar(className) {
    return makeAvatarImg(userAvatarDataUri, className, userDisplayName);
  }

  // 头像 + 右上角 V 标的组合节点。默认 permission 直接返 img；显著 permission 用
  // .avatar-wrap 包起来让 badge 浮右上角。
  function makeAvatarWithPermission(g, avatarClassName, badgeClassName) {
    const img = makeAvatar(g.name, avatarClassName);
    const showBadge = g.permission && g.permission !== DEFAULT_PERMISSION;
    if (!showBadge) return img;
    const wrap = document.createElement("span");
    wrap.className = "avatar-wrap";
    wrap.appendChild(img);
    const badge = makePermissionBadge(g.permission, badgeClassName);
    badge.classList.add("on-avatar");
    wrap.appendChild(badge);
    return wrap;
  }

  // 茶客行：头像 / 名字（权限 anchor）+ 打分小数字 + × 请离。指派 / 圆桌入口已挪到
  // composer 底栏「指派」按钮（见 assign_popover.js），不再挂在茶客行上。
  function renderGuestRowNormal(li, g, lastGuestLock) {
    // 头像 / 名字都是「设置权限」的 anchor —— popover 锚在头像更直观，但点名字也能开。
    const node = makeAvatarWithPermission(g, "avatar", "permission-badge");
    node.classList.add("permission-anchor");
    node.title = `点击设置「${g.name}」的权限（当前 ${g.permission || DEFAULT_PERMISSION}）`;
    node.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (!isConnected()) return;
      showPermissionPopover(node, g);
    });
    li.appendChild(node);
    const nameBadge = makeBadge("guest-name", null, g.name);
    nameBadge.classList.add("permission-anchor");
    nameBadge.title = `点击设置「${g.name}」的权限（当前 ${g.permission || DEFAULT_PERMISSION}）`;
    nameBadge.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (!isConnected()) return;
      showPermissionPopover(node, g);
    });
    li.appendChild(nameBadge);
    if (g.isolation && g.isolation !== "room") {
      li.appendChild(makeBadge("isolation-badge", "isolation", g.isolation));
    }
    // 打分小数字（turn_start 时填，turn_end 时清）。margin-left:auto 推到右侧；
    // 空 textContent 时占位不可见，文字一来就显示，不引发布局抖动（min-width）。
    const score = document.createElement("span");
    score.className = "guest-score";
    li.appendChild(score);
    scoreSpansByName.set(g.name, score);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "row-icon-btn row-remove";
    remove.textContent = "×";
    if (lastGuestLock) {
      remove.disabled = true;
      remove.title = "至少要保留一位茶客";
    } else {
      remove.title = `请离 ${g.name}`;
      remove.addEventListener("click", () => {
        if (!isConnected()) return;
        if (!window.confirm(`请离茶客「${g.name}」？\n房间历史不动，guests/${g.name}/ 工作目录也不会被删，但不会再参与对话。`)) return;
        send({ type: Inbound.REMOVE_GUEST, name: g.name });
        setStatus("", `请离 ${g.name}…`);
      });
    }
    li.appendChild(remove);
  }

  // 打分写到 sidebar 各茶客行的 ``.guest-score`` span 上（不是主聊天区）。
  // scoresByName 没该茶客 → 清空 span（turn_end 走这条路径，统一清）。
  function applyScoresToSidebar() {
    for (const [name, span] of scoreSpansByName) {
      const r = scoresByName.get(name);
      if (!r) {
        span.textContent = "";
        delete span.dataset.kind;
        continue;
      }
      span.textContent = scoreText(r);
      // kind=scored 走默认浅灰（数字打分），其余 kind 给 [data-kind=...] 语义色。
      if (r.kind && r.kind !== ScoreKind.SCORED) {
        span.dataset.kind = r.kind;
      } else {
        delete span.dataset.kind;
      }
    }
  }

  return {
    // chat_stream 渲染气泡头像用。
    makeAvatar,
    makeUserAvatar,
    // submit echo 的 speaker 名。
    getUserDisplayName: () => userDisplayName,
    // room_info：装用户显示名 / 头像 + 重渲「我」那一行。
    setUser({ displayName, avatarDataUri }) {
      userDisplayName = displayName || "我";
      userAvatarDataUri = avatarDataUri || null;
      userNameEl.textContent = userDisplayName;
      userAvatarWrapEl.replaceChildren(makeUserAvatar("user-avatar"));
    },
    // room_info：全量重渲茶客列表（getGuests 取最新名单）。打分残留同时清。
    renderGuests() {
      guestsEl.replaceChildren();
      scoreSpansByName.clear();
      scoresByName = new Map();
      // 最后一位茶客不能删（与 server 端 admin.remove_guest 硬约束一致）—— 前端禁用
      // 按钮，用户少踩一次"提交后才发现不行"的坑。
      const guests = getGuests();
      const lastGuestLock = guests.length <= 1;
      for (const g of guests) {
        const li = document.createElement("li");
        renderGuestRowNormal(li, g, lastGuestLock);
        guestsEl.appendChild(li);
      }
    },
    // turn_start / turn_end 经 envelope_router 调 —— 用打分列表（turn_end 传空）
    // 替换 scoresByName 并刷 sidebar。
    applyScores(scoresList) {
      scoresByName = new Map((scoresList ?? []).map((r) => [r.guest_name, r]));
      applyScoresToSidebar();
    },
  };
}
