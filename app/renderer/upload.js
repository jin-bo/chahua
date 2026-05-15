"use strict";

// 茶话室文件上传 —— composer 左侧附件按钮 → 文件被传到房间 share/ 目录、pill 显示
// 在 composer 上方，与下一条 user_message 一起进 transcript。
//
// 协议合环：上行 INBOUND_UPLOAD_FILE → 服务端落盘 → 下行 FILE_UPLOADED envelope，
// 调用方把 envelope 的 data 喂给 onServerEcho；模块去重 + 加 pill + setStatus。

import { Inbound } from "./events.js";

// 与 server.py 的 _UPLOAD_MAX_BYTES 同步（2MB）。前端早拒省一次 base64 + ws 来回。
const UPLOAD_MAX_BYTES = 2 * 1024 * 1024;

const SHARE_PREFIX = "share/";

// File → 纯 base64 字符串（去掉 data URI 头）。FileReader.readAsDataURL 比手写
// ArrayBuffer → btoa 链路省一次大数组中转。
function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error("FileReader 没返回字符串"));
        return;
      }
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = () => reject(reader.error || new Error("FileReader 失败"));
    reader.readAsDataURL(file);
  });
}

export function createUpload({
  pendingFilesEl,
  fileInputEl,
  attachFileBtn,
  isConnected,
  send,
  setStatus,
}) {
  // rel = "share/<safe-name>"（server 派发，filesystem 洗过名）；
  // original = 用户原文件名，pill 显示用 + sanitize 改过时挂 title 对齐"我点的"vs"落地的"。
  const pendingFiles = [];

  function renderPills() {
    pendingFilesEl.replaceChildren();
    if (pendingFiles.length === 0) {
      pendingFilesEl.hidden = true;
      return;
    }
    pendingFilesEl.hidden = false;
    for (const f of pendingFiles) {
      const li = document.createElement("li");
      li.className = "pending-file";
      const landedName = f.rel.slice(SHARE_PREFIX.length);
      const name = document.createElement("span");
      name.className = "pending-file-name";
      name.textContent = f.original || landedName;
      if (f.original && f.original !== landedName) {
        name.title = `已上传为 ${landedName}（原名 ${f.original} 含非法字符被替换）`;
      }
      li.appendChild(name);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "pending-file-remove";
      remove.textContent = "×";
      remove.title = "不发送这个文件（文件已在房间 share 目录里，下次还能引用）";
      remove.addEventListener("click", () => {
        const idx = pendingFiles.indexOf(f);
        if (idx >= 0) {
          pendingFiles.splice(idx, 1);
          renderPills();
        }
      });
      li.appendChild(remove);
      pendingFilesEl.appendChild(li);
    }
  }

  async function uploadOne(file) {
    if (file.size > UPLOAD_MAX_BYTES) {
      window.alert(
        `「${file.name}」超过 ${(UPLOAD_MAX_BYTES / 1024 / 1024).toFixed(1)}MB 上限，挑张小一点的。`,
      );
      return;
    }
    setStatus("", `上传「${file.name}」…`);
    let content_b64;
    try {
      content_b64 = await readFileAsBase64(file);
    } catch (e) {
      setStatus("error", `读「${file.name}」失败：${e.message || e}`);
      return;
    }
    send({
      type: Inbound.UPLOAD_FILE,
      filename: file.name,
      content_b64,
    });
  }

  attachFileBtn.addEventListener("click", () => {
    if (!isConnected()) return;
    // reset 让相同文件再选也能触发 change（浏览器对同源文件默认不再 fire）。
    fileInputEl.value = "";
    fileInputEl.click();
  });

  fileInputEl.addEventListener("change", () => {
    const files = Array.from(fileInputEl.files || []);
    if (files.length === 0) return;
    // 并发读 —— FileReader 在 worker 线程，串行没必要等前一个 onload 才开下一个；
    // ws.send 非阻塞，server 端 inbound 循环按到达顺序串行处理。
    Promise.all(files.map(uploadOne));
  });

  return {
    // FILE_UPLOADED envelope 来时调；server 已落盘并 echo 回 rel/original/...
    onServerEcho(data) {
      const rel = data?.rel;
      if (typeof rel !== "string" || !rel) return;
      const original = data?.original || rel;
      // 重复上传同名文件 → server 覆盖落盘；pill 去重避免 pending 区两条同名条目。
      if (pendingFiles.some((f) => f.rel === rel)) {
        setStatus("ok", `已覆盖「${original}」`);
        return;
      }
      pendingFiles.push({ rel, original: data?.original || "" });
      renderPills();
      setStatus("ok", `已上传「${original}」`);
    },
    snapshotRels() {
      return pendingFiles.map((f) => f.rel);
    },
    hasPending() {
      return pendingFiles.length > 0;
    },
    clear() {
      pendingFiles.length = 0;
      renderPills();
    },
  };
}
