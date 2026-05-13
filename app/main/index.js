"use strict";

// 茶话室 Electron main 进程（P3.1）。
//
// 流程：
//   1. 单实例锁 —— 避免两个 main 进程同时拉 sidecar 抢 transcript.jsonl。
//   2. app 就绪 → 起 sidecar，拿到 wsUrl
//   3. 建 BrowserWindow，把 wsUrl 通过 additionalArguments 喂给 preload
//   4. before-quit 优先关 sidecar 再 exit
//
// 安全姿态：contextIsolation: true、nodeIntegration: false、sandbox: true。
// renderer 只能通过 contextBridge 拿到 wsUrl，其余一概不暴露。

const path = require("node:path");
const { app, BrowserWindow } = require("electron");
const { startSidecar } = require("./sidecar");

// 仓库根 = app/ 的父目录。sidecar 需要 cwd 在这里才能 `uv run chahua-server`。
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const PRELOAD_PATH = path.join(__dirname, "..", "preload", "index.js");
const RENDERER_HTML = path.join(__dirname, "..", "renderer", "index.html");

if (!app.requestSingleInstanceLock()) {
  // 第二实例：直接退，不喊出 dock 也不 focus 现有窗 —— P3.1 单房间会话型，等
  // P3.2+ 真正需要"focus existing window"再处理 second-instance 事件。
  app.quit();
  process.exit(0);
}

let sidecar = null;

async function createWindow(wsUrl) {
  // Electron 内部维持 BrowserWindow 引用，模块级 mainWindow 不必要（避免 P3.3+
  // 真要 webContents.send 时再加回来）。
  const win = new BrowserWindow({
    width: 880,
    height: 640,
    title: "茶话室",
    webPreferences: {
      preload: PRELOAD_PATH,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      additionalArguments: [`--chahua-ws-url=${wsUrl}`],
    },
  });
  await win.loadFile(RENDERER_HTML);
}

app.whenReady().then(async () => {
  try {
    sidecar = await startSidecar({ repoRoot: REPO_ROOT });
  } catch (e) {
    console.error("[chahua] sidecar 启动失败:", e);
    app.quit();
    return;
  }
  await createWindow(sidecar.wsUrl);
});

app.on("window-all-closed", () => {
  // 茶话室是单房间会话 App —— 关窗 = 退出，不留 dock 残影。
  // macOS 的 reopen-on-dock-click 体验留给 P3.3+ 多房间路由再考虑。
  app.quit();
});

let quitting = false;
app.on("before-quit", (e) => {
  if (quitting || !sidecar) return;
  quitting = true;
  e.preventDefault();
  const s = sidecar;
  sidecar = null;
  s.stop()
    .catch((err) => console.error("[chahua] sidecar 关停异常:", err))
    .finally(() => app.exit(0));
});
