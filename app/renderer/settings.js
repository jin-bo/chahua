"use strict";

// 「我」与「房间」的设置编辑：USER.md 文本 / room.toml 文本 / 头像上传裁切。
//
// 模块 owns 4 个 snapshot state —— 由 renderSidebar 在 room_info envelope 到达时
// 调 setSnapshot 喂入；编辑 modal 打开时从 snapshot prefill。submit 后**不**本地
// 改 snapshot —— server 会 echo 新 room_info 重发 snapshot，渲染一致性走环路。
// 头像同款：本地编码 PNG dataURI 发 UPDATE_USER_AVATAR，等 server 在下条 room_info
// 里回 user_avatar_data_uri 再生效。

import { Inbound } from "./events.js";

// 浏览器对 PNG / JPEG / WebP / GIF 都能 <img> 原生解码 → 画进 canvas → 一律
// toDataURL("image/png")，服务端只认 PNG。GIF 走 <img> 时 canvas 只能拿到首帧 ——
// 静态头像场景下符合预期（动图当头像意义不大、且 PNG 不存动）。
const AVATAR_TARGET_PX = 256;
const AVATAR_MAX_BYTES = 20 * 1024 * 1024;
const AVATAR_ACCEPTED_MIME = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
]);
// 状态条提示文案 —— 让用户感知"我传的是 X，落地是 PNG"的转换发生。
const AVATAR_FORMAT_LABEL = {
  "image/jpeg": "JPG",
  "image/webp": "WebP",
  "image/gif": "GIF",
};

// 中央裁方 + 等比缩到 AVATAR_TARGET_PX × AVATAR_TARGET_PX。
// 裁方原因：sidebar / 气泡里的头像 wrapper 是圆形（border-radius:50%），方形源截出
// 的圆刚好居中；矩形源会被 object-fit:cover 切边。原图比目标小则不放大保留 native。
function cropAndEncodeAvatar(img) {
  const side = Math.min(img.width, img.height);
  const sx = Math.floor((img.width - side) / 2);
  const sy = Math.floor((img.height - side) / 2);
  const target = Math.min(AVATAR_TARGET_PX, side);
  const canvas = document.createElement("canvas");
  canvas.width = target;
  canvas.height = target;
  const ctx = canvas.getContext("2d");
  // imageSmoothingQuality 默认 "low"，"high" 在缩图时视觉差异明显（128px 头像里
  // 头发 / 五官清晰度肉眼可辨）。
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(img, sx, sy, side, side, 0, 0, target, target);
  return canvas.toDataURL("image/png");
}

export function createSettings({
  userMd,        // {modal, textarea, hintEl, submitBtn}
  roomToml,      // {modal, textarea, hintEl, submitBtn}
  avatarFileInput,
  isConnected,
  send,
  setStatus,
}) {
  let userMdContent = "";
  let userMdSource = null;
  let roomTomlContent = "";
  let roomTomlSource = null;

  // 一份 editor = "改一个文本字段 + 弹同款 modal" 三件套。两条配置（USER.md /
  // room.toml）共用同一形态，wire 类型 / 状态文案 / 兜底提示由参数定。
  function makeEditor(els, { inboundType, statusText, getContent, getSource, emptyHintText }) {
    els.submitBtn.addEventListener("click", () => {
      if (!isConnected()) return;
      send({ type: inboundType, content: els.textarea.value });
      setStatus("", statusText);
      els.modal.hidden = true;
    });
    return () => {
      if (!isConnected()) return;
      els.textarea.value = getContent();
      const src = getSource();
      els.hintEl.textContent = src ? `当前文件：${src}` : emptyHintText;
      els.modal.hidden = false;
      els.textarea.focus();
    };
  }

  const openEditUserMd = makeEditor(userMd, {
    inboundType: Inbound.UPDATE_USER_MD,
    statusText: "保存用户配置…",
    getContent: () => userMdContent,
    getSource: () => userMdSource,
    emptyHintText: "尚无 USER.md，保存后会落到 user_data_root/USER.md。",
  });
  const openEditRoomToml = makeEditor(roomToml, {
    inboundType: Inbound.UPDATE_ROOM_TOML,
    statusText: "保存房间配置…",
    getContent: () => roomTomlContent,
    getSource: () => roomTomlSource,
    emptyHintText: "",
  });

  avatarFileInput.addEventListener("change", () => {
    const file = avatarFileInput.files?.[0];
    if (!file) return;
    if (file.type && !AVATAR_ACCEPTED_MIME.has(file.type)) {
      window.alert(`不支持的图片格式：${file.type}\n请选 PNG / JPG / WebP / GIF。`);
      return;
    }
    if (file.size > AVATAR_MAX_BYTES) {
      window.alert(`图片超过 ${(AVATAR_MAX_BYTES / 1024 / 1024).toFixed(0)}MB，挑张小一点的吧。`);
      return;
    }
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      const dataUri = cropAndEncodeAvatar(img);
      send({ type: Inbound.UPDATE_USER_AVATAR, data_uri: dataUri });
      const label = AVATAR_FORMAT_LABEL[file.type];
      setStatus("", label ? `上传头像（${label} → PNG）…` : "上传头像…");
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      window.alert("图片解码失败，文件可能损坏或格式不被浏览器支持。");
    };
    img.src = url;
  });

  return {
    setSnapshot(snap) {
      userMdContent = snap.userMdContent || "";
      userMdSource = snap.userMdSource || null;
      roomTomlContent = snap.roomTomlContent || "";
      roomTomlSource = snap.roomTomlSource || null;
    },
    openEditUserMd,
    openEditRoomToml,
    pickAvatarFile() {
      if (!isConnected()) return;
      // reset 让相同文件再选也能触发 change（浏览器对同名同源文件默认不再 fire）。
      avatarFileInput.value = "";
      avatarFileInput.click();
    },
  };
}
