"use strict";

// 茶客详细设置 modal（P4.5，对应 docs/P4-专业茶客配置闭环.md §5.1）。
//
// **diff 提交**：提交时只发用户改了的 section 的 inbound（最多 4 条 update_guest_*），
// 不发 update_room_toml 整张表。单字段非法只回滚那一项 —— UI 状态更精确（设计 §5.3）。

import { Inbound, DEFAULT_PERMISSION, PERMISSION_OPTIONS } from "./events.js";

function $(id) { return document.getElementById(id); }

function setRadio(name, value) {
  const inputs = document.querySelectorAll(`input[name="${name}"]`);
  for (const inp of inputs) inp.checked = inp.value === value;
}

function getRadio(name) {
  const checked = document.querySelector(`input[name="${name}"]:checked`);
  return checked ? checked.value : null;
}

function refreshLlmFieldsVisibility() {
  $("edit-guest-llm-fields").hidden = getRadio("edit-guest-llm-mode") !== "custom";
}

function renderPermissionRow() {
  const row = $("edit-guest-permission-row");
  row.replaceChildren();
  for (const opt of PERMISSION_OPTIONS) {
    const label = document.createElement("label");
    label.className = "modal-radio";
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "edit-guest-permission";
    input.value = opt.value;
    label.appendChild(input);
    const span = document.createElement("span");
    span.textContent = opt.label;
    label.appendChild(span);
    row.appendChild(label);
  }
}

// 房间级 MCP 列表 —— 每行 [name][command][args 空格分隔][×]，args 用字符串编辑而非
// 多 input 数组：UI 简单胜过表单 array，与 popover 展示形态一致。
function appendRoomMcpRow(ul, s = { name: "", command: "", args: [] }) {
  const li = document.createElement("li");
  li.className = "modal-mcp-row";
  const name = document.createElement("input");
  name.type = "text";
  name.placeholder = "name";
  name.value = s.name || "";
  name.className = "modal-mcp-name";
  const cmd = document.createElement("input");
  cmd.type = "text";
  cmd.placeholder = "command";
  cmd.value = s.command || "";
  cmd.className = "modal-mcp-cmd";
  const args = document.createElement("input");
  args.type = "text";
  args.placeholder = "args（空格分隔）";
  args.value = Array.isArray(s.args) ? s.args.join(" ") : "";
  args.className = "modal-mcp-args";
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "row-remove";
  remove.textContent = "×";
  remove.title = "移除这条";
  remove.addEventListener("click", () => li.remove());
  li.append(name, cmd, args, remove);
  ul.appendChild(li);
  return li;
}

function renderRoomMcpList(servers) {
  const ul = $("edit-guest-room-mcp-list");
  ul.replaceChildren();
  for (const s of servers) appendRoomMcpRow(ul, s);
}

function readRoomMcpList() {
  const out = [];
  for (const li of $("edit-guest-room-mcp-list").querySelectorAll(".modal-mcp-row")) {
    const name = li.querySelector(".modal-mcp-name").value.trim();
    const command = li.querySelector(".modal-mcp-cmd").value.trim();
    const argsRaw = li.querySelector(".modal-mcp-args").value.trim();
    // 留空行：用户开了一条但没填 —— 跳过（与"加了一行然后移除"等价），不报错。
    if (!name && !command && !argsRaw) continue;
    const entry = { name, command };
    if (argsRaw) entry.args = argsRaw.split(/\s+/).filter(Boolean);
    out.push(entry);
  }
  return out;
}

function renderPersonaMcpList(servers) {
  const ul = $("edit-guest-persona-mcp-list");
  ul.replaceChildren();
  if (!servers.length) {
    const li = document.createElement("li");
    li.className = "modal-mcp-empty";
    li.textContent = "（无）";
    ul.appendChild(li);
    return;
  }
  for (const s of servers) {
    const li = document.createElement("li");
    li.className = "modal-mcp-readonly";
    const name = document.createElement("span");
    name.className = "popover-mcp-name";
    name.textContent = s.name;
    const cmd = document.createElement("span");
    cmd.className = "popover-mcp-cmd";
    const args = Array.isArray(s.args) ? s.args : [];
    cmd.textContent = `${s.command || "?"} ${args.join(" ")}`.trim();
    li.append(name, cmd);
    ul.appendChild(li);
  }
}

function renderSkills(skills) {
  const ul = $("edit-guest-skills-list");
  $("edit-guest-skills-fieldset").hidden = skills.length === 0;
  ul.replaceChildren();
  for (const s of skills) {
    const li = document.createElement("li");
    li.className = "modal-mcp-readonly";
    li.textContent = s;
    ul.appendChild(li);
  }
}

function fillForm(g, roomDefaultModel) {
  $("edit-guest-title").textContent = `设置「${g.name}」`;
  $("edit-guest-persona-hint").textContent = `人格：${g.persona_rel || ""}`;
  $("edit-guest-workspace-hint").textContent = `工作目录：${g.workspace_path || ""}`;

  const llm = g.llm || {};
  const source = llm.source || "room_default";
  $("edit-guest-llm-default-label").textContent =
    roomDefaultModel ? `继承房间默认（${roomDefaultModel}）` : "继承房间默认";
  setRadio("edit-guest-llm-mode", source === "guest" ? "custom" : "default");
  // 房间默认时 model 字段留空 —— 切到 custom 时让用户从空白起编辑，避免"已经显示 X /
  // 我没改也是 X？"的歧义。
  $("edit-guest-llm-model").value = source === "guest" ? (llm.model || "") : "";
  $("edit-guest-llm-base-url").value = source === "guest" ? (llm.base_url || "") : "";
  $("edit-guest-llm-api-key-env").value = source === "guest" ? (llm.api_key_env || "") : "";
  refreshLlmFieldsVisibility();
  const ready = !!llm.api_key_ready;
  $("edit-guest-llm-key-status").textContent =
    `${llm.api_key_env || "<PROVIDER>_API_KEY"}：${ready ? "✓ 已配置" : "✗ 未配置（缺环境变量）"}`;

  setRadio("edit-guest-permission", g.permission || DEFAULT_PERMISSION);
  setRadio("edit-guest-isolation", g.isolation || "room");

  renderPersonaMcpList(g.persona_mcp_servers || []);
  renderRoomMcpList(g.room_mcp_servers || []);
  renderSkills(g.skills_available || []);

  // persona MCP 信任态由 sidebar 头像 popover 切（modal 里只展示）—— hint 把那条路径告知。
  const trusted = !!g.persona_mcp_trusted;
  const personaCount = (g.persona_mcp_servers || []).length;
  $("edit-guest-persona-mcp-hint").textContent =
    personaCount === 0
      ? "persona 自带（无）"
      : `persona 自带（${trusted ? "✓ 已信任" : "✗ 未信任（在 sidebar 头像 popover 上勾）"}）`;
}

// 提交时按 section diff —— 用户只改了模型不该重置 permission；非法 model 一处错误
// 不该把整张表回滚。
function diffPayloads(g) {
  const out = [];
  const llmMode = getRadio("edit-guest-llm-mode");
  const wasCustom = g.llm?.source === "guest";
  let newSpec = null;
  if (llmMode === "custom") {
    const model = $("edit-guest-llm-model").value.trim();
    if (!model) return { error: "model 必填（形如 openai/gpt-5.4）" };
    newSpec = { model };
    const baseUrl = $("edit-guest-llm-base-url").value.trim();
    if (baseUrl) newSpec.base_url = baseUrl;
    const apiKeyEnv = $("edit-guest-llm-api-key-env").value.trim();
    if (apiKeyEnv) newSpec.api_key_env = apiKeyEnv;
  }
  // wasCustom 当 sentinel：source=guest 才有 user-explicit api_key_env 概念，default
  // 时 envelope 里那个字段是 server 端按 <PROVIDER>_API_KEY 推断回填的，不能算"用户写过"。
  const oldSpec = wasCustom
    ? {
        model: g.llm.model,
        ...(g.llm.base_url ? { base_url: g.llm.base_url } : {}),
        ...(g.llm.api_key_env ? { api_key_env: g.llm.api_key_env } : {}),
      }
    : null;
  if (JSON.stringify(oldSpec) !== JSON.stringify(newSpec)) {
    out.push({ type: Inbound.UPDATE_GUEST_LLM, name: g.name, spec: newSpec });
  }

  const newPerm = getRadio("edit-guest-permission");
  if (newPerm && newPerm !== g.permission) {
    out.push({ type: Inbound.UPDATE_GUEST_PERMISSION, name: g.name, permission: newPerm });
  }

  const newIso = getRadio("edit-guest-isolation");
  if (newIso && newIso !== g.isolation) {
    out.push({ type: Inbound.UPDATE_GUEST_ISOLATION, name: g.name, isolation: newIso });
  }

  const newMcp = readRoomMcpList();
  const oldMcp = g.room_mcp_servers || [];
  if (JSON.stringify(newMcp) !== JSON.stringify(oldMcp)) {
    out.push({ type: Inbound.UPDATE_GUEST_EXTRA_MCP, name: g.name, servers: newMcp });
  }
  return { error: null, payloads: out };
}

export function createGuestSettings({ isConnected, send, setStatus }) {
  const modal = $("edit-guest-modal");
  let openedGuest = null;
  let roomDefaultModel = null;

  renderPermissionRow();
  for (const inp of document.querySelectorAll('input[name="edit-guest-llm-mode"]')) {
    inp.addEventListener("change", refreshLlmFieldsVisibility);
  }
  $("edit-guest-room-mcp-add").addEventListener("click", () => {
    appendRoomMcpRow($("edit-guest-room-mcp-list"));
  });

  $("edit-guest-submit").addEventListener("click", () => {
    if (!isConnected() || !openedGuest) return;
    const diff = diffPayloads(openedGuest);
    if (diff.error) {
      window.alert(diff.error);
      return;
    }
    // 设计 §2.5：isolation 切换会改 cwd，旧 .agentao/memory.db 不自动迁移。
    const isoChange = diff.payloads.find((p) => p.type === Inbound.UPDATE_GUEST_ISOLATION);
    if (isoChange) {
      const ok = window.confirm(
        `把「${openedGuest.name}」切到 ${isoChange.isolation} 隔离？\n\n` +
          "切换会改变工作目录路径。已有的 .agentao/memory.db / sessions/ 不会自动迁移，" +
          "保留旧记忆请先手动 cp 老路径到新路径；否则茶客从空白起步。",
      );
      if (!ok) return;
    }
    for (const p of diff.payloads) send(p);
    setStatus("", diff.payloads.length === 0 ? "没有改动" : `保存「${openedGuest.name}」设置…`);
    modal.hidden = true;
    openedGuest = null;
  });

  return {
    setSnapshot({ roomDefaultLlmModel }) {
      roomDefaultModel = roomDefaultLlmModel || null;
    },
    open(g) {
      if (!isConnected()) return;
      openedGuest = g;
      fillForm(g, roomDefaultModel);
      modal.hidden = false;
    },
  };
}
