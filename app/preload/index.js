"use strict";

// preload（P3.1）：在 sandbox 渲染进程里跑，唯一职责是把 main 传过来的 wsUrl
// 通过 contextBridge 安全暴露给 renderer。
//
// 走 additionalArguments（main/index.js webPreferences）而不是 IPC handshake：
// renderer 一加载就能拿到 wsUrl，省一次往返 + 不引入 ipcRenderer 表面。

const { contextBridge } = require("electron");

function parseFlag(name) {
  const prefix = `--${name}=`;
  for (const a of process.argv) {
    if (a.startsWith(prefix)) return a.slice(prefix.length);
  }
  return null;
}

contextBridge.exposeInMainWorld("chahua", {
  wsUrl: parseFlag("chahua-ws-url"),
});
