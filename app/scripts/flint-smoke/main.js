// P10.2 Electron 真渲染冒烟（`npm run smoke:flint`）—— **不打包**（build.files 里 `!scripts/**/*`）。
//
// 为何要它：node 层的 flint_core.test.mjs 只能证纯逻辑；**「两个 npm 包能不能在 sandbox:true 的纯
// Chromium ESM 上下文里 import」只有真渲染进程能证** —— P10.1 正是在这里栽过一次（highlight.js 的
// es/* 是重导出 CJS 的 shim，node 测试因有 CJS interop 假性通过、Electron 里 import 即抛）。
// 本 harness 用与 app/main/index.js **同一份 webPreferences**、冒烟页用与 index.html **逐字同一份
// CSP**，跑完把结果打到 stdout 并按失败数退出，故可无人值守跑。
//
// 覆盖 P10.2 §7 手测 18 项里可自动化的那部分：1 渲染 / 8 注入 / 9 不误伤 / 11 拒 data.url /
// 12 三档徽标 / 13 空图守门 / 4 实例不泄漏（resetFlintCharts 那半）。
// **仍需人眼的**：2 窄气泡可读性、3 气泡放宽后的观感、5 视口门、6 LRU 自愈、7 流式、10 tooltip、
// 14 懒加载（Network 面板）、15 离线、16/17 CLI 与导出零回归、18 分面图。
const { app, BrowserWindow } = require("electron");
const path = require("path");

const RENDERER = path.join(__dirname, "smoke.html");

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    width: 1000,
    height: 800,
    show: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  let done = false;
  win.webContents.on("console-message", (_e, _level, message) => {
    if (message.startsWith("FLINT_SMOKE_RESULT ")) {
      done = true;
      const results = JSON.parse(message.slice("FLINT_SMOKE_RESULT ".length));
      let bad = 0;
      for (const r of results) {
        if (!r.pass) bad++;
        console.log(`${r.pass ? "PASS" : "FAIL"} ${r.name}${r.extra ? "  [" + r.extra + "]" : ""}`);
      }
      console.log(bad ? `\n${bad} FAILED` : `\nALL ${results.length} PASSED`);
      app.exit(bad ? 1 : 0);
    } else {
      console.log("[renderer]", message);
    }
  });
  await win.loadFile(RENDERER);
  setTimeout(() => {
    if (!done) {
      console.log("TIMEOUT: 渲染进程没有回报结果");
      app.exit(2);
    }
  }, 30000);
});
