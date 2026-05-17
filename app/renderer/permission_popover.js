"use strict";

// 权限 popover（点 sidebar 头像）—— 从 renderer.js 拆出（P5.2 重构，长文件瘦身）。
//
// 三档权限 + persona MCP 信任开关 + 房间级 MCP 摘要 + skills 摘要 + 详细设置 modal 入口。
// 自管开关态：再次点同一头像 → toggle close。一个 renderer 一份实例，DOM 上 .popover.permission-popover 至多一份。

import {
  Inbound,
  DEFAULT_PERMISSION,
  PERMISSION_OPTIONS,
} from "./events.js";
import {
  attachPopoverDismissHandlers,
  positionPopoverByAnchor,
} from "./ui_popover.js";

export function createPermissionPopover({
  send,
  setStatus,
  isConnected,
  openGuestSettings,
}) {
  let openForGuest = null;
  let detachDismiss = null;

  function close() {
    const pop = document.querySelector(".permission-popover");
    if (pop) pop.remove();
    if (detachDismiss) {
      detachDismiss();
      detachDismiss = null;
    }
    openForGuest = null;
  }

  // 在 anchor 元素附近浮出权限选择 popover。再次点同一头像 → 切回关闭（toggle）。
  function show(anchor, g) {
    if (openForGuest === g.name) {
      close();
      return;
    }
    close();
    const current = g.permission || DEFAULT_PERMISSION;
    const pop = document.createElement("div");
    pop.className = "popover permission-popover";
    const title = document.createElement("div");
    title.className = "popover-title";
    title.textContent = `设置「${g.name}」的权限`;
    pop.appendChild(title);
    for (const opt of PERMISSION_OPTIONS) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "popover-option permission-option";
      btn.dataset.permission = opt.value;
      if (opt.value === current) btn.classList.add("current");
      // 左侧色点：与 V 标同色，read-only 用中性灰；视觉一眼分档。
      const swatch = document.createElement("span");
      swatch.className = "permission-swatch";
      swatch.dataset.permission = opt.value;
      btn.appendChild(swatch);
      const meta = document.createElement("span");
      meta.className = "popover-option-meta";
      const label = document.createElement("span");
      label.className = "popover-option-label";
      label.textContent = opt.label;
      if (opt.value === current) {
        const tag = document.createElement("span");
        tag.className = "popover-option-current";
        tag.textContent = "（当前）";
        label.appendChild(tag);
      }
      meta.appendChild(label);
      const desc = document.createElement("span");
      desc.className = "popover-option-desc";
      desc.textContent = opt.desc;
      meta.appendChild(desc);
      btn.appendChild(meta);
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        if (opt.value === current) {
          close();
          return;
        }
        send({
          type: Inbound.UPDATE_GUEST_PERMISSION,
          name: g.name,
          permission: opt.value,
        });
        setStatus("", `设置「${g.name}」权限为 ${opt.label}…`);
        close();
      });
      pop.appendChild(btn);
    }
    appendMcpSection(pop, g);
    appendSkillsSection(pop, g);
    appendDetailsEntry(pop, g);
    document.body.appendChild(pop);
    positionPopoverByAnchor(pop, anchor);
    openForGuest = g.name;
    detachDismiss = attachPopoverDismissHandlers(pop, close);
  }

  // MCP 因为带来"任意可执行"风险，必须用户显式勾才装载；skills 是 prompt，无门控只展示。
  function appendListSection(pop, { title, listClass, items, renderItem, head }) {
    if (items.length === 0 && !head) return;
    const sec = document.createElement("div");
    sec.className = "popover-section";
    const titleEl = document.createElement("div");
    titleEl.className = "popover-section-title";
    titleEl.textContent = title;
    sec.appendChild(titleEl);
    if (head) sec.appendChild(head);
    if (items.length > 0) {
      const list = document.createElement("ul");
      list.className = listClass;
      for (const it of items) {
        const li = document.createElement("li");
        renderItem(li, it);
        list.appendChild(li);
      }
      sec.appendChild(list);
    }
    pop.appendChild(sec);
  }

  function appendMcpSection(pop, g) {
    // P4.4：envelope 把 MCP 拆成两块。
    //   - persona_mcp_servers：persona sidecar mcp.json，整 persona 一刀切走 trust。
    //   - room_mcp_servers：[[guest.extra_mcp_servers]]，自动信任，逐条可编辑（P4.5 UI）。
    // popover 这里仅做"显示 + persona 信任开关"。逐条增删走详细设置 modal（P4.5）。
    const personaServers = Array.isArray(g.persona_mcp_servers) ? g.persona_mcp_servers : [];
    const roomServers = Array.isArray(g.room_mcp_servers) ? g.room_mcp_servers : [];
    if (personaServers.length === 0 && roomServers.length === 0) return;

    const renderRow = (li, s) => {
      const name = document.createElement("span");
      name.className = "popover-mcp-name";
      name.textContent = s.name;
      const cmd = document.createElement("span");
      cmd.className = "popover-mcp-cmd";
      const args = Array.isArray(s.args) ? s.args : [];
      cmd.textContent = `${s.command || "?"} ${args.join(" ")}`.trim();
      cmd.title = cmd.textContent;
      li.append(name, cmd);
    };

    if (personaServers.length > 0) {
      const head = document.createElement("label");
      head.className = "popover-checkbox";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !!g.persona_mcp_trusted;
      cb.addEventListener("change", () => {
        if (!isConnected()) {
          cb.checked = !cb.checked;
          return;
        }
        const trusted = cb.checked;
        send({
          type: Inbound.SET_PERSONA_MCP_TRUST,
          persona_rel: g.persona_rel,
          trusted,
        });
        setStatus(
          "",
          trusted
            ? `信任「${g.name}」的 MCP 服务器…`
            : `撤销对「${g.name}」MCP 的信任…`,
        );
        close();
      });
      head.append(cb, document.createTextNode("信任此 persona 的 MCP（持续生效，跨房间）"));
      appendListSection(pop, {
        title: "persona 自带 MCP",
        listClass: "popover-mcp-list",
        items: personaServers,
        head,
        renderItem: renderRow,
      });
    }

    if (roomServers.length > 0) {
      appendListSection(pop, {
        title: "房间级 MCP（自动信任）",
        listClass: "popover-mcp-list",
        items: roomServers,
        renderItem: renderRow,
      });
    }
  }

  function appendSkillsSection(pop, g) {
    const skills = Array.isArray(g.skills_available) ? g.skills_available : [];
    appendListSection(pop, {
      title: "Skills（持续加载）",
      listClass: "popover-skills-list",
      items: skills,
      renderItem: (li, s) => { li.textContent = s; },
    });
  }

  // 详细设置 modal 入口（P4.5）—— popover 末尾一条；快捷"切 permission / 勾 MCP"
  // 仍保留在 popover 上头（90% 场景一键搞定不必开 modal）。
  function appendDetailsEntry(pop, g) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "popover-option popover-details-entry";
    btn.textContent = "详细设置…";
    btn.title = "模型 / 隔离 / 房间级 MCP 一站式编辑";
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      close();
      openGuestSettings(g);
    });
    pop.appendChild(btn);
  }

  return { show, close };
}
