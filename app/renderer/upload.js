"use strict";

// 茶话室文件上传 —— composer 左侧附件按钮 → 文件被传到房间 share/ 目录、pill 显示
// 在 composer 上方，与下一条 user_message 一起进 transcript。
//
// 协议合环：上行 INBOUND_UPLOAD_FILE → 服务端落盘 → 下行 FILE_UPLOADED envelope，
// 调用方把 envelope 的 data 喂给 onServerEcho；模块去重 + 加 pill + setStatus。

import { Inbound } from "./events.js";

// 与 server.py 的 _UPLOAD_MAX_BYTES 同步（200MB）。前端早拒省一次 base64 + ws 来回。
const UPLOAD_MAX_BYTES = 200 * 1024 * 1024;

// P13 C3：视觉输入上限（与后端 agentao.media_limits.MAX_IMAGE_BYTES = 20MB 对齐）。
// 超过此值的图片仍能上传（≤200MB），但茶客模型看不到像素、退回 `<attachment .../>` 文本
// 引用 —— pill 上提示一下，避免用户以为大图也被「看见」。
const VISION_MAX_IMAGE_BYTES = 20 * 1024 * 1024;

const SHARE_PREFIX = "share/";

// pending-file card 左侧色块图标。非图片文件共用一个 emoji；图片走缩略图（见 renderPills）。
const PENDING_FILE_ICON = "📄";

// P13 C2：与后端 image_input._EXT_TO_MIME / chat_view 预览白名单一致 —— 只对这些扩展名
// 渲缩略图。SVG 不内嵌（可藏 <script>），与视觉输入白名单同口径。
const IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp"]);

function isImageName(name) {
  if (typeof name !== "string") return false;
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || dot === name.length - 1) return false;
  return IMAGE_EXTS.has(name.slice(dot + 1).toLowerCase());
}

// "." 在首位（dotfile，如 .gitignore）当作无扩展名走 fallback，避免把整个文件名当 type
// 显示出来。
function fileTypeLabel(name) {
  if (typeof name !== "string") return "FILE";
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || dot === name.length - 1) return "FILE";
  return name.slice(dot + 1).toUpperCase();
}

// 非图 / 缩略图解码失败时的 emoji 方块图标 —— 正常分支与 img.onerror 兜底共用。
function makePendingFileIcon() {
  const div = document.createElement("div");
  div.className = "pending-file-icon";
  div.textContent = PENDING_FILE_ICON;
  return div;
}

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
  // 可选：点 pill 上的 📎 时调用。如不传，pill 不渲染该按钮。当前任务存在与否由
  // 调用方在 body 上挂 .has-active-task class 控制按钮可见性（见 style.css）。
  onAttachToTask = null,
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
      const displayName = f.original || landedName;

      let icon;
      if (f.thumb) {
        // P13 C2：图片 pill 渲缩略图（本地 blob URL，不走网络）。object-fit:cover
        // 由 .pending-file-thumb 样式定，撑满与 emoji 图标同尺寸的方块。
        icon = document.createElement("img");
        icon.className = "pending-file-icon pending-file-thumb";
        icon.src = f.thumb;
        icon.alt = "";
        // 解码失败（文件损坏 / 非真图）→ 换回 emoji 方块，别留裂图。顺手 revoke +
        // 清 f.thumb：renderPills 每次 echo / 移除都整体重建，不清的话同一个坏 blob
        // 每次重建都再解码失败一遍（大文件耗时 + img→emoji 闪烁）。比对 thumbUrl
        // 防错杀 —— 同名覆盖路径可能已 revoke 旧 blob 并换上新 thumb。
        const thumbUrl = f.thumb;
        icon.addEventListener("error", () => {
          if (f.thumb === thumbUrl) {
            URL.revokeObjectURL(thumbUrl);
            f.thumb = null;
          }
          icon.replaceWith(makePendingFileIcon());
        }, { once: true });
      } else {
        icon = makePendingFileIcon();
      }

      const info = document.createElement("div");
      info.className = "pending-file-info";
      const name = document.createElement("span");
      name.className = "pending-file-name";
      name.textContent = displayName;
      // sanitize 后落地文件名变了才挂 title 提示用户"我点的 vs 落地的"差异；同名时
      // title 加 ellipsis 后才有用，这里就不挂了。
      if (f.original && f.original !== landedName) {
        name.title = `已上传为 ${landedName}（原名 ${f.original} 含非法字符被替换）`;
      }
      const type = document.createElement("span");
      type.className = "pending-file-type";
      type.textContent = fileTypeLabel(displayName);
      info.append(name, type);
      // P13 C3：超视觉上限的图片 —— pill 上挂一行小提示，茶客看到的是文本引用而非像素。
      if (f.visionOversize) {
        const warn = document.createElement("span");
        warn.className = "pending-file-vision-warn";
        warn.textContent = "超 20MB · 茶客看文本引用";
        warn.title = "图片超过视觉模型上限（20MB），茶客将收到 <attachment> 文本引用而非像素";
        info.append(warn);
      }

      li.append(icon, info);
      if (onAttachToTask) {
        const attach = document.createElement("button");
        attach.type = "button";
        attach.className = "pending-file-action pending-file-attach";
        attach.textContent = "📎";
        attach.title = "拷贝到当前任务（share/ 原文件保留）";
        attach.addEventListener("click", () => onAttachToTask(f.rel));
        li.appendChild(attach);
      }
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "pending-file-action pending-file-remove";
      remove.textContent = "×";
      remove.title = "不发送这个文件（文件已在房间 share 目录里，下次还能引用）";
      remove.addEventListener("click", () => {
        const idx = pendingFiles.indexOf(f);
        if (idx >= 0) {
          if (f.thumb) URL.revokeObjectURL(f.thumb);
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
    // P13 C2：图片在丢弃 File 前生成一份 objectURL 缩略图，随 FIFO echo 透传给 pill
    // （上传串行 → echo 顺序与 send 顺序一致，head.thumb 即当前文件的缩略图）。非图为
    // null。pill 移除 / clear / 重复覆盖时 revoke，避免 blob URL 泄漏。
    const isImage = isImageName(file.name);
    const thumb = isImage ? URL.createObjectURL(file) : null;
    // 图片超视觉上限 → 标记 pill，提示「茶客将看到文本引用而非像素」。
    const visionOversize = isImage && file.size > VISION_MAX_IMAGE_BYTES;
    const echo = new Promise((resolve, reject) => {
      pendingEchoes.push({ resolve, reject, thumb, visionOversize });
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
      if (thumb) URL.revokeObjectURL(thumb);
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

  // 串行上传一批 File —— 文件选择 / paste / drag-drop 三入口共用。``isUploading``
  // 重入门保证「最多 1 个 in-flight」+ FIFO echo 顺序不乱（见 pendingEchoes 注释）。
  async function uploadFiles(files) {
    files = Array.from(files || []);
    if (files.length === 0) return;
    // paste / drag-drop 绕过附件按钮的 disabled 视觉提示 —— 这两条入口忙 / 断线时必须
    // 给状态反馈，否则用户粘了图却无声丢弃（附件按钮路径靠 disabled 自然挡住）。
    if (!isConnected()) {
      setStatus("error", "未连接，无法上传；连上后再试。");
      return;
    }
    if (isUploading) {
      setStatus("", "还有文件在上传，等当前批次完成后再粘贴 / 拖拽。");
      return;
    }
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
  }

  fileInputEl.addEventListener("change", () => {
    void uploadFiles(fileInputEl.files);
  });

  return {
    // FILE_UPLOADED envelope 来时调；server 已落盘并 echo 回 rel/original/...
    onServerEcho(data) {
      // FIFO 唤醒上传循环 —— echo 顺序与 send 顺序匹配（server inbound 单 ws 串行消费）。
      // 先 shift 取出本 echo 对应的 head（含缩略图），再挂到 pill 上。
      const head = pendingEchoes.shift();
      const thumb = head?.thumb ?? null;
      const visionOversize = head?.visionOversize ?? false;
      const rel = data?.rel;
      if (typeof rel === "string" && rel) {
        const original = data?.original || rel;
        const existing = pendingFiles.find((f) => f.rel === rel);
        if (existing) {
          // 重复上传同名 → server 覆盖落盘；pill 去重，缩略图更新（revoke 旧 blob）。
          if (existing.thumb) URL.revokeObjectURL(existing.thumb);
          existing.thumb = thumb;
          existing.visionOversize = visionOversize;
          renderPills();
          setStatus("ok", `已覆盖「${original}」`);
        } else {
          pendingFiles.push({
            rel, original: data?.original || "", thumb, visionOversize,
          });
          renderPills();
          setStatus("ok", `已上传「${original}」`);
        }
      } else if (thumb) {
        // rel 缺失的异常 echo —— 缩略图无处挂载，revoke 防 blob 泄漏。
        URL.revokeObjectURL(thumb);
      }
      if (head) head.resolve();
    },
    uploadFiles,
    dropPending(reason = "connection closed") {
      // ws.onclose 时调一次 —— 把 await echo 翻成 reject，让正在跑的上传 finally 清状态。
      while (pendingEchoes.length > 0) {
        const head = pendingEchoes.shift();
        if (head.thumb) URL.revokeObjectURL(head.thumb);
        head.reject(new Error(reason));
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
      // 先 revoke 所有 pill 缩略图 blob URL 再清，避免泄漏。
      for (const f of pendingFiles) {
        if (f.thumb) URL.revokeObjectURL(f.thumb);
      }
      pendingFiles.length = 0;
      renderPills();
      // 切房 / 清空房间时：① bump generation 让 readFileAsBase64 后的 send 早路径被
      // 跳过；② 拒掉所有已经 push 的 echo waiter，让在飞循环抛错走 finally。
      generation += 1;
      while (pendingEchoes.length > 0) {
        const head = pendingEchoes.shift();
        if (head.thumb) URL.revokeObjectURL(head.thumb);
        head.reject(new Error("room switched"));
      }
    },
  };
}
