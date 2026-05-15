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
    const img = makeAvatarImg(p.avatar_data_uri, "persona-avatar", p.name);
    if (img) li.appendChild(img);
    const name = document.createElement("span");
    name.className = "persona-name";
    name.textContent = p.name;
    li.appendChild(name);
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
  function reset() {
    const folderRadio = modal.querySelector("input[name='import-source'][value='folder']");
    if (folderRadio) folderRadio.checked = true;
    folderPathEl.value = "";
    githubUrlEl.value = "";
  }

  importBtn.addEventListener("click", () => {
    if (!isConnected()) return;
    reset();
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
}
