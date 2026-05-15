"use strict";

// 茶话室 Electron main 进程（P3.1）。
//
// 流程：
//   1. 单实例锁 —— 避免两个 main 进程同时拉 sidecar 抢 transcript.jsonl。
//   2. app 就绪 → 解析 paths（dev = 仓库根；packaged = .app/Resources + userData）
//      → 首启动 seed templates → userData → 起 sidecar，拿到 wsUrl
//   3. 建 BrowserWindow，把 wsUrl 通过 additionalArguments 喂给 preload
//   4. before-quit 优先关 sidecar 再 exit
//
// 安全姿态：contextIsolation: true、nodeIntegration: false、sandbox: true。
// renderer 只能通过 contextBridge 拿到 wsUrl，其余一概不暴露。

const path = require("node:path");
const { app, BrowserWindow, dialog, ipcMain } = require("electron");
const { startSidecar } = require("./sidecar");
const { resolvePaths } = require("./paths");
const { seedUserData } = require("./seed");

// app.getPath('logs') 读的是 app.getName()。package.json 里 name 是 "chahua-app"
// （Electron 要 dash 而非空格），但日志目录想要的是 ~/Library/Logs/chahua/。
// setName 必须在 getPath('logs') 之前；放最顶安全。
app.setName("chahua");

const PRELOAD_PATH = path.join(__dirname, "..", "preload", "index.js");
const RENDERER_HTML = path.join(__dirname, "..", "renderer", "index.html");

if (!app.requestSingleInstanceLock()) {
  // 第二实例：直接退，不喊出 dock 也不 focus 现有窗 —— P3.1 单房间会话型，等
  // P3.2+ 真正需要"focus existing window"再处理 second-instance 事件。
  app.quit();
  process.exit(0);
}

let sidecar = null;
let shutdownPromise = null;

// 单一关停入口：normal quit（before-quit）/ SIGINT / SIGTERM 都汇到这里。
// shutdownPromise 单例化保证 stop() 只跑一遍 —— Ctrl+C 后 Electron 自己也会
// emit before-quit，二次 entry 直接复用同一个 Promise 不重复 stop。
async function shutdownAndExit(exitCode = 0) {
  if (shutdownPromise) return shutdownPromise;
  shutdownPromise = (async () => {
    const runningSidecar = sidecar;
    sidecar = null;
    if (runningSidecar) {
      try {
        await runningSidecar.stop();
      } catch (err) {
        console.error("[chahua] sidecar 关停异常:", err);
      }
    }
    app.exit(exitCode);
  })();
  return shutdownPromise;
}

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

// IPC：renderer 通过 preload chahua.pickFolder() 唤起系统文件夹选择器，给 persona
// import 用。返回字符串绝对路径或 null（用户取消 / 关闭对话框）。dialog API 只能在
// main 端调，所以这里 handle，preload 端 invoke 转发。
ipcMain.handle("chahua:pick-folder", async () => {
  const win = BrowserWindow.getFocusedWindow();
  const result = win
    ? await dialog.showOpenDialog(win, {
        title: "选 persona 目录（含 <Name>.md）",
        properties: ["openDirectory"],
      })
    : await dialog.showOpenDialog({
        title: "选 persona 目录（含 <Name>.md）",
        properties: ["openDirectory"],
      });
  if (result.canceled || !result.filePaths.length) return null;
  return result.filePaths[0];
});

app.whenReady().then(async () => {
  const paths = resolvePaths();
  try {
    const copied = await seedUserData(paths);
    if (copied > 0) {
      console.log(`[chahua] seeded ${copied} entries → ${paths.userDataRoot}`);
    }
  } catch (e) {
    // seed 失败不致命 —— python 端在 userDataRoot 缺 rooms 时会给清晰报错，
    // 这里只记 stderr 让用户能溯源；硬塞继续启动反而让现场更难判。
    console.error("[chahua] seed userData 失败:", e);
  }
  try {
    sidecar = await startSidecar({ paths, logsDir: app.getPath("logs") });
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

app.on("before-quit", (e) => {
  if (shutdownPromise || !sidecar) return;
  e.preventDefault();
  shutdownAndExit(0);
});

// PowerShell Ctrl+C 在 Windows 上把 SIGINT 直接发到 electron.exe，可能不走
// before-quit；POSIX 上终端 Ctrl+C 在 dev `npm run dev` 链路里也直接砸到 Node。
// 显式接 SIGINT/SIGTERM → shutdownAndExit，避免 sidecar 留孤儿。
for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => {
    shutdownAndExit(0);
  });
}
