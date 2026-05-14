"use strict";

// 茶话室 main 进程路径解析（P3.3.2.b）。
//
// 双根模型镜像 chahua/_paths.py:Paths：
//   - appRoot：只读 asset 根（python 包 + ship 自带 personas / templates）。
//     dev = 仓库根；packaged = .app/Contents/Resources 下（P3.3.2.c 实装）。
//   - userDataRoot：用户数据根（rooms / USER.md / .env / 用户自定义 personas）。
//     dev = 仓库根（与 appRoot 同源 → seed 不触发，保留 dev 直改 repo 文件的工效）；
//     packaged = ``app.getPath('userData')``（macOS ~/Library/Application Support/chahua/）。
//
// 这里只决定路径"是什么"，不负责拷文件 —— 拷文件在 seed.js。

const path = require("node:path");
const { app } = require("electron");

// dev 仓库根 = app/ 的父。打包路径下 __dirname 在 .asar 里，REPO_ROOT 不应被使用；
// 调用方走 ``app.isPackaged`` 分支。
const REPO_ROOT = path.resolve(__dirname, "..", "..");

// app/templates/ —— 首启动 seed 的源。dev 和 packaged 都从 .app/Resources/app/templates/
// 拷出（electron-builder 默认把整个 app/ 打进去）。
const TEMPLATES_DIR = path.join(__dirname, "..", "templates");

function resolvePaths() {
  if (app.isPackaged) {
    // packaged：``process.resourcesPath`` = ``.app/Contents/Resources``。
    // electron-builder extraResources 把 python-bundle/ 搬到这里，与
    // chahua/personas/ 等 ship 自带 asset 同根。
    return {
      appRoot: process.resourcesPath,
      userDataRoot: app.getPath("userData"),
      templatesDir: TEMPLATES_DIR,
      isPackaged: true,
    };
  }
  // dev：两根同源仓库根，与 P3.3.2.a 之前的行为 100% 一致。
  return {
    appRoot: REPO_ROOT,
    userDataRoot: REPO_ROOT,
    templatesDir: TEMPLATES_DIR,
    isPackaged: false,
  };
}

module.exports = { resolvePaths };
