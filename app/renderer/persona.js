"use strict";

// persona picker（add-guest / new-room modal 共用的人设网格）+ 导入 persona modal
// （本地目录 / GitHub URL 两路）。
//
// picker 是纯渲染：传 personas 列表 + 选项，调 onPick；不持状态。
// 导入 modal 自挂三个按钮（importBtn 开 / folderPickBtn 选目录 / submitBtn 发帧）+
// reset 在每次开时清旧输入。

import { Inbound } from "./events.js";
import { makeAvatarImg } from "./chat_view.js";

// 单选 onPick(persona)；多选给 li 加 .selected toggle 由调用方在 submit 时收集
// `li.persona-item.selected` 拿到选中项。excludeNames：已在场茶客名（add-guest 时禁
// 重复项；new-room 不传，所有候选都可选）。
export function renderPersonaPicker(rootEl, { personas, multi, excludeNames, onPick }) {
  rootEl.replaceChildren();
  if (personas.length === 0) {
    const empty = document.createElement("li");
    empty.className = "persona-empty";
    empty.textContent = "没有可用的人设。可在 chahua/personas/ 下放置 <name>.md（可选 <name>.png 头像）。";
    rootEl.appendChild(empty);
    return;
  }
  for (const p of personas) {
    const li = document.createElement("li");
    li.className = "persona-item";
    li.dataset.persona = p.persona;
    li.dataset.name = p.name;
    li.appendChild(makeAvatarImg(p.avatar_data_uri, "persona-avatar", p.name));
    // 名字 + （可选）summary 包一层垂直容器，让 .persona-hint 仍在右侧、布局零回归。
    const text = document.createElement("div");
    text.className = "persona-text";
    const name = document.createElement("span");
    name.className = "persona-name";
    name.textContent = p.name;
    text.appendChild(name);
    if (p.summary) {
      const summary = document.createElement("span");
      summary.className = "persona-summary";
      summary.textContent = p.summary;
      text.appendChild(summary);
    }
    li.appendChild(text);
    if (excludeNames && excludeNames.has(p.name)) {
      li.classList.add("disabled");
      const tag = document.createElement("span");
      tag.className = "persona-hint";
      tag.textContent = "已在场";
      li.appendChild(tag);
    } else {
      li.addEventListener("click", () => {
        if (multi) {
          li.classList.toggle("selected");
          return;
        }
        onPick(p);
      });
    }
    rootEl.appendChild(li);
  }
}

// ── 已安装 persona 行渲染（P12.6）──────────────────────────────────────────

// 上游状态枚举 → 徽章文案。镜像 chahua/persona_import.py STATUS_*。
const INSTALLED_STATUS_LABELS = Object.freeze({
  unknown: "未检查",
  up_to_date: "最新",
  update_available: "可更新",
  source_unavailable: "不可更新",
  error: "检查失败",
});

// 「更新」按钮可点：来源可更新（github/folder）且（检到更新 或 本地已改 —— 后者可
// 「拉回上游覆盖本地」）。unknown / source_unavailable / error 一律禁用。
function installedUpdatable(p) {
  if (p.source_type !== "github" && p.source_type !== "folder") return false;
  return p.status === "update_available" || p.local_modified === true;
}

function buildInstalledMeta(p) {
  const parts = [];
  if (p.source_type === "github") parts.push("来源：GitHub");
  else if (p.source_type === "folder") parts.push("来源：本地文件夹");
  else parts.push("来源：未知");
  // 版本号**纯展示**、不参与判定：有更新且两侧都在 → 并列；否则只显已装版本。
  if (p.status === "update_available" && p.installed_version && p.latest_version) {
    parts.push(`本地 v${p.installed_version} · 上游 v${p.latest_version}`);
  } else if (p.installed_version) {
    parts.push(`v${p.installed_version}`);
  }
  return parts.join(" · ");
}

function buildInstalledBadges(p) {
  const wrap = document.createElement("span");
  wrap.className = "installed-badges";
  const badge = document.createElement("span");
  badge.className = `installed-badge installed-badge-${p.status}`;
  badge.textContent = INSTALLED_STATUS_LABELS[p.status] || p.status;
  wrap.appendChild(badge);
  if (p.local_modified) {
    const mod = document.createElement("span");
    mod.className = "installed-badge installed-badge-modified";
    mod.textContent = "本地已改";
    wrap.appendChild(mod);
  }
  return wrap;
}

// onUpdate(p) / onDelete(p) 由 createPersonaImport 注入。
export function renderInstalledPersonas(rootEl, { personas, onUpdate, onDelete }) {
  rootEl.replaceChildren();
  if (!personas || personas.length === 0) {
    const empty = document.createElement("li");
    empty.className = "installed-empty";
    empty.textContent = "还没有已安装的 persona（从「导入」页装一个）。";
    rootEl.appendChild(empty);
    return;
  }
  for (const p of personas) {
    const li = document.createElement("li");
    li.className = "installed-item";
    li.dataset.name = p.name;
    li.appendChild(makeAvatarImg(p.avatar_data_uri, "persona-avatar", p.display_name || p.name));

    const text = document.createElement("div");
    text.className = "installed-text";
    const nameRow = document.createElement("div");
    nameRow.className = "installed-name-row";
    const name = document.createElement("span");
    name.className = "persona-name";
    name.textContent = p.display_name || p.name;
    nameRow.appendChild(name);
    nameRow.appendChild(buildInstalledBadges(p));
    text.appendChild(nameRow);

    const meta = document.createElement("span");
    meta.className = "installed-meta";
    meta.textContent = buildInstalledMeta(p);
    text.appendChild(meta);

    if (p.detail) {
      const detail = document.createElement("span");
      detail.className = "installed-detail";
      detail.textContent = p.detail;
      text.appendChild(detail);
    }
    li.appendChild(text);

    const actions = document.createElement("div");
    actions.className = "installed-actions";
    const updateBtn = document.createElement("button");
    updateBtn.type = "button";
    updateBtn.textContent = "更新";
    updateBtn.disabled = !installedUpdatable(p);
    updateBtn.addEventListener("click", () => onUpdate(p));
    actions.appendChild(updateBtn);
    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "installed-delete";
    deleteBtn.textContent = "删除";
    deleteBtn.addEventListener("click", () => onDelete(p));
    actions.appendChild(deleteBtn);
    li.appendChild(actions);

    rootEl.appendChild(li);
  }
}

export function createPersonaImport({
  modal,
  folderPathEl,
  folderPickBtn,
  githubUrlEl,
  submitBtn,
  importBtn,
  isConnected,
  send,
  setStatus,
  pickFolder,
}) {
  // 「已安装」页 DOM 都在本 modal 内 —— 就地查，免得 modals.js 调用点堆一长串形参。
  const importPanel = modal.querySelector("#persona-import-panel");
  const installedPanel = modal.querySelector("#persona-installed-panel");
  const importFooter = modal.querySelector("#persona-import-footer");
  const installedListEl = modal.querySelector("#installed-personas-list");
  const checkAllBtn = modal.querySelector("#installed-check-all");
  const updateAllBtn = modal.querySelector("#installed-update-all");
  const tabs = Array.from(modal.querySelectorAll(".persona-modal-tab"));

  // 最近一帧 personas_installed 的快照（「全部更新」据此挑候选）。
  let installedPersonas = [];
  // 「全部更新」串行队列：[{name, force}, ...]，每收到一帧回环 echo 发下一条。
  let updateQueue = [];

  function reset() {
    const folderRadio = modal.querySelector("input[name='import-source'][value='folder']");
    if (folderRadio) folderRadio.checked = true;
    folderPathEl.value = "";
    githubUrlEl.value = "";
  }

  function showTab(which) {
    for (const t of tabs) t.classList.toggle("active", t.dataset.personaTab === which);
    importPanel.hidden = which !== "import";
    installedPanel.hidden = which !== "installed";
    importFooter.hidden = which !== "import";
    if (which === "installed" && isConnected()) {
      send({ type: Inbound.LIST_INSTALLED_PERSONAS });
    }
  }

  function onUpdate(p) {
    if (!isConnected()) return;
    let force = false;
    if (p.local_modified) {
      if (!window.confirm(`「${p.display_name || p.name}」有本地改动，更新会丢弃这些改动。继续？`)) return;
      force = true;
    }
    send({ type: Inbound.UPDATE_PERSONA, name: p.name, force });
    setStatus("", `更新 persona「${p.name}」…`);
  }

  function onDelete(p) {
    if (!isConnected()) return;
    if (!window.confirm(
      `删除 persona「${p.display_name || p.name}」？\n正被某房间引用时，请先在该房间移除这位茶客。`
    )) return;
    send({ type: Inbound.DELETE_PERSONA, name: p.name });
    setStatus("", `删除 persona「${p.name}」…`);
  }

  function pumpUpdateQueue() {
    if (updateQueue.length === 0) return;
    const next = updateQueue.shift();
    send({ type: Inbound.UPDATE_PERSONA, name: next.name, force: next.force });
  }

  function onUpdateAll() {
    if (!isConnected()) return;
    // 与每行「更新」按钮同口径（installedUpdatable）：含 update_available，也含「仅本地已改、
    // 上游 up_to_date」的行（后者可拉回上游覆盖本地）。否则 bulk 与逐行不一致。
    const candidates = installedPersonas.filter(installedUpdatable);
    if (candidates.length === 0) {
      window.alert("没有可更新的 persona（先点「检查更新」）。");
      return;
    }
    const modified = candidates.filter((p) => p.local_modified);
    let includeModified = false;
    if (modified.length > 0) {
      includeModified = window.confirm(
        `${modified.length} 个 persona 有本地改动，一并更新会丢弃这些改动。\n确定全部更新？取消则只更新未改动的。`
      );
    }
    const toUpdate = includeModified
      ? candidates
      : candidates.filter((p) => !p.local_modified);
    if (toUpdate.length === 0) return;
    updateQueue = toUpdate.map((p) => ({ name: p.name, force: p.local_modified === true }));
    setStatus("", `全部更新 ${updateQueue.length} 个 persona…`);
    pumpUpdateQueue();
  }

  // 收到 PERSONAS_INSTALLED 全量快照 —— 整批覆盖渲染，并推进「全部更新」串行队列。
  function renderInstalled(personas) {
    installedPersonas = Array.isArray(personas) ? personas : [];
    renderInstalledPersonas(installedListEl, {
      personas: installedPersonas, onUpdate, onDelete,
    });
    if (updateQueue.length > 0) pumpUpdateQueue();
  }

  for (const t of tabs) {
    t.addEventListener("click", () => showTab(t.dataset.personaTab));
  }
  if (checkAllBtn) {
    checkAllBtn.addEventListener("click", () => {
      if (!isConnected()) return;
      send({ type: Inbound.CHECK_PERSONA_UPDATES });
      setStatus("", "检查 persona 更新…");
    });
  }
  if (updateAllBtn) updateAllBtn.addEventListener("click", onUpdateAll);

  importBtn.addEventListener("click", () => {
    if (!isConnected()) return;
    reset();
    updateQueue = [];
    showTab("import");
    modal.hidden = false;
  });

  folderPickBtn.addEventListener("click", async () => {
    // pickFolder 由 contextBridge 暴露的 main → dialog.showOpenDialog handle；
    // 没拿到（preload 旧版 / dev hot-reload corner case）→ 降级允许手粘绝对路径。
    if (typeof pickFolder !== "function") {
      window.alert("当前 Electron 环境不支持文件夹选择，请直接粘贴绝对路径。");
      folderPathEl.readOnly = false;
      return;
    }
    const picked = await pickFolder();
    if (picked) folderPathEl.value = picked;
  });

  submitBtn.addEventListener("click", () => {
    if (!isConnected()) return;
    const source = modal.querySelector("input[name='import-source']:checked")?.value;
    if (source === "github") {
      const url = githubUrlEl.value.trim();
      if (!url) {
        githubUrlEl.focus();
        return;
      }
      send({ type: Inbound.IMPORT_PERSONA_GITHUB, url });
      setStatus("", `导入 GitHub persona…`);
    } else {
      const path = folderPathEl.value.trim();
      if (!path) {
        window.alert("先选一个目录，或粘贴绝对路径。");
        return;
      }
      send({ type: Inbound.IMPORT_PERSONA_FOLDER, path });
      setStatus("", `导入本地 persona…`);
    }
    modal.hidden = true;
  });

  // 暴露给 envelope_router：收到 PERSONAS_INSTALLED 帧时重渲「已安装」列表。
  return { renderInstalled };
}
