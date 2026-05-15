"use strict";

// preload（P3.1）：在 sandbox 渲染进程里跑，把 main 端的安全能力通过 contextBridge
// 暴露给 renderer。
//
// 暴露面：
// - wsUrl：sidecar 的 ws://...，通过 additionalArguments（main/index.js webPreferences）
//   塞进 process.argv，省一次 IPC 往返。
// - pickFolder()：唤起系统文件夹选择器，给 persona import 用。走 ipcRenderer.invoke
//   而不是 dialog 直连，因为 dialog API 只能 main 端调；sandbox=true 下 preload 仍可用
//   ipcRenderer.invoke。

const { contextBridge, ipcRenderer } = require("electron");

function parseFlag(name) {
  const prefix = `--${name}=`;
  for (const a of process.argv) {
    if (a.startsWith(prefix)) return a.slice(prefix.length);
  }
  return null;
}

contextBridge.exposeInMainWorld("chahua", {
  wsUrl: parseFlag("chahua-ws-url"),
  // 返回选中的绝对路径字符串；用户取消 → null。
  pickFolder: () => ipcRenderer.invoke("chahua:pick-folder"),
});
