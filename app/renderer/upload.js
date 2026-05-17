"use strict";

// 茶话室文件上传 —— composer 左侧附件按钮 → 文件被传到房间 share/ 目录、pill 显示
// 在 composer 上方，与下一条 user_message 一起进 transcript。
//
// 协议合环：上行 INBOUND_UPLOAD_FILE → 服务端落盘 → 下行 FILE_UPLOADED envelope，
// 调用方把 envelope 的 data 喂给 onServerEcho；模块去重 + 加 pill + setStatus。

import { Inbound } from "./events.js";

// 与 server.py 的 _UPLOAD_MAX_BYTES 同步（200MB）。前端早拒省一次 base64 + ws 来回。
const UPLOAD_MAX_BYTES = 200 * 1024 * 1024;

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

  // 串行上传循环里"等服务端吃下当前文件再读下一个"用 —— send() 立刻返回（payload 进
  // ws.bufferedAmount），不等就开下一份 readAsDataURL 会把 N 份 ~267MB string 同时
  // 堆在内存里。FILE_UPLOADED echo 是服务端处理完的信号（成功 / 失败都发，与 server
  // 端 _upload_file 的"恒发"契约对齐）。FIFO 队列：服务端 inbound 是单 ws 串行消费 →
  // echo 顺序与 send 顺序一致；shift 头部 resolve / reject 即可一一对应。
  //
  // 每项是 ``{resolve, reject}`` —— ws 断了走 ``dropPending()`` reject 所有 waiter，让
  // ``await echo`` 抛错触发 change handler 的 finally 清 ``isUploading``；否则按钮会
  // 一直 disabled 直到刷新。
  const pendingEchoes = [];
  // 阻止 change 事件重入 —— 用户点附件 → 选文件 → 上传未完时再点 / 再选会触发第二
  // 个 change handler 并发跑 uploadOne，破坏"最多 1 个 in-flight"的内存保证 + 让 FIFO
  // 队列里 resolver 顺序与实际 send 顺序错位。
  let isUploading = false;
  // 切房 / 清空房间时 bump —— ``readFileAsBase64`` 是个长 IO 窗口（200MB 文件 ~秒级），
  // 期间 clear() 无 pending echo 可拒（resolver 还没 push）；读完后 generation 已变就
  // 直接放弃这文件，避免落到新房去。
  let generation = 0;

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
    const startGen = generation;
    let content_b64;
    try {
      content_b64 = await readFileAsBase64(file);
    } catch (e) {
      setStatus("error", `读「${file.name}」失败：${e.message || e}`);
      return;
    }
    if (generation !== startGen) {
      // 读期间用户切了房 / 清了房 —— 这个文件已经"属于"旧房，不再发送。
      throw new Error("room switched during upload");
    }
    const echo = new Promise((resolve, reject) => {
      pendingEchoes.push({ resolve, reject });
    });
    try {
      send({
        type: Inbound.UPLOAD_FILE,
        filename: file.name,
        content_b64,
      });
    } catch (e) {
      // 同步 send 失败（ws 已关）—— 回滚刚 push 的 resolver，保持 FIFO 与 server 处理
      // 顺序对齐；后续 echo（理论上不会再来）shift 出的 head 仍是真正在飞的请求。
      const head = pendingEchoes.pop();
      if (head) head.reject(e);
      throw e;
    }
    // 立即丢弃本地引用，让 GC 回收 ~267MB 字符串（ws 已自己拷一份进 bufferedAmount）。
    content_b64 = null;
    await echo;
  }

  attachFileBtn.addEventListener("click", () => {
    if (!isConnected() || isUploading) return;
    // reset 让相同文件再选也能触发 change（浏览器对同源文件默认不再 fire）。
    fileInputEl.value = "";
    fileInputEl.click();
  });

  fileInputEl.addEventListener("change", async () => {
    const files = Array.from(fileInputEl.files || []);
    if (files.length === 0 || isUploading) return;
    isUploading = true;
    attachFileBtn.disabled = true;
    try {
      // 串行：单文件上限 200MB，base64 + JSON quoting 后峰值 ~267MB。N 个并发会让渲染
      // 进程同时持有 N 份 base64 string + ws 发送缓冲全堆住，轻则卡，重则 OOM。
      for (const file of files) {
        try {
          await uploadOne(file);
        } catch (e) {
          // 单个文件 send / echo 失败 → 把剩下的也跳过；ws 多半已断，继续推也只是再
          // 抛一次。状态 bar 已在 send 早路径或 dropPending 路径写过 reason。
          setStatus("error", `上传中断：${e.message || e}`);
          break;
        }
      }
    } finally {
      isUploading = false;
      // 断线场景：dropPending 触发的 finally 不该把按钮启用 —— ws 已关 setInputEnabled
      // 也置过 disabled，重连后由那里负责复位。在线时才解除"上传中"的 disable。
      if (isConnected()) {
        attachFileBtn.disabled = false;
      }
    }
  });

  return {
    // FILE_UPLOADED envelope 来时调；server 已落盘并 echo 回 rel/original/...
    onServerEcho(data) {
      const rel = data?.rel;
      if (typeof rel === "string" && rel) {
        const original = data?.original || rel;
        if (pendingFiles.some((f) => f.rel === rel)) {
          // 重复上传同名 → server 覆盖落盘；pill 去重避免 pending 区两条同名。
          setStatus("ok", `已覆盖「${original}」`);
        } else {
          pendingFiles.push({ rel, original: data?.original || "" });
          renderPills();
          setStatus("ok", `已上传「${original}」`);
        }
      }
      // FIFO 唤醒上传循环 —— echo 顺序与 send 顺序匹配（server inbound 单 ws 串行消费）。
      const head = pendingEchoes.shift();
      if (head) head.resolve();
    },
    dropPending(reason = "connection closed") {
      // ws.onclose 时调一次 —— 把 await echo 翻成 reject，让正在跑的上传 finally 清状态。
      while (pendingEchoes.length > 0) {
        pendingEchoes.shift().reject(new Error(reason));
      }
    },
    snapshotRels() {
      return pendingFiles.map((f) => f.rel);
    },
    hasPending() {
      return pendingFiles.length > 0;
    },
    isUploading() {
      return isUploading;
    },
    clear() {
      pendingFiles.length = 0;
      renderPills();
      // 切房 / 清空房间时：① bump generation 让 readFileAsBase64 后的 send 早路径被
      // 跳过；② 拒掉所有已经 push 的 echo waiter，让在飞循环抛错走 finally。
      generation += 1;
      while (pendingEchoes.length > 0) {
        pendingEchoes.shift().reject(new Error("room switched"));
      }
    },
  };
}
