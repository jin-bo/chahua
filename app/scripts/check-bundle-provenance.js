#!/usr/bin/env node
"use strict";

// 出包门（P19）。在 ``electron-builder`` 之前跑，挡住三类"包里装的不是该装的东西"：
//
//   ① bundle 的 agentao 来自本地联调源码（``CHAHUA_AGENTAO_SOURCE``）而非 uv.lock
//      —— 未测过的代码不得进 dmg / exe。
//   ② manifest 里的 agentao 版本与 ``uv.lock`` 锁定版不符（手改 / 拷贝 manifest）。
//   ③ bundle 里的 chahua 版本与 ``app/package.json`` 不是同一版 —— 历史上
//      "改了 python 代码但 bundle 没重建"是最容易漏的一种，v0.1.11 之前靠每版
//      ``FORCE=1`` 兜；P19 的构建指纹已自动失效，这里再钉一道机械校验。
//
// 只挡 build:mac / build:mac:* / build:win 四个**发布**脚本；``build:dir`` 不挡，
// 本地联调要能一路走到可运行的 .app。真要出一个联调包，直接调 electron-builder。
//
// 用法：node app/scripts/check-bundle-provenance.js

const fs = require("node:fs");
const path = require("node:path");

const APP_DIR = path.resolve(__dirname, "..");
const REPO_ROOT = path.resolve(APP_DIR, "..");
const MANIFEST_PATH = path.join(APP_DIR, "python-bundle", "bundle-manifest.json");
const LOCK_PATH = path.join(REPO_ROOT, "uv.lock");

// npm ``0.1.12-dev`` 与 python ``0.1.12.dev0`` 是同一版的两种写法；拆成
// {core, dev} 再比，既认出"版本号不一致"，也认出"只去了一半后缀"。
function normalize(v) {
  const m = String(v).match(/^(\d+\.\d+\.\d+)(?:[-.]dev\d*)?$/);
  if (!m) return null;
  return { core: m[1], dev: /[-.]dev\d*$/.test(String(v)) };
}

function fail(msg) {
  console.error(`[check-bundle] ✗ ${msg}`);
  process.exit(1);
}

function main() {
  if (!fs.existsSync(MANIFEST_PATH)) {
    fail(`缺 ${MANIFEST_PATH}\n  先跑 npm run build:python（构建失败不会写 manifest）`);
  }
  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf8"));
  } catch (e) {
    fail(`manifest 解析失败：${e.message}`);
  }

  // ① 来源必须是锁定发布版
  if (manifest.agentaoSource !== "lock") {
    const src = manifest.agentaoSource || {};
    fail(
      `bundle 的 agentao 来自本地联调源码，不得发布：\n` +
      `  path=${src.path ?? "?"} commit=${src.commit ?? "?"} dirty=${src.dirty ?? "?"}\n` +
      `  清掉 CHAHUA_AGENTAO_SOURCE 后重跑 FORCE=1 npm run build:python`,
    );
  }

  // ② agentao 版本 == uv.lock 锁定版
  const lockMatch = fs.readFileSync(LOCK_PATH, "utf8")
    .match(/\[\[package\]\]\r?\nname = "agentao"\r?\nversion = "([^"]+)"/);
  if (!lockMatch) fail("uv.lock 里没找到 agentao");
  if (manifest.agentaoVersion !== lockMatch[1]) {
    fail(`bundle agentao=${manifest.agentaoVersion}，uv.lock 锁定=${lockMatch[1]}`);
  }

  // ③ chahua 版本 == app/package.json（防打进上一版 python）
  const appVersion = JSON.parse(
    fs.readFileSync(path.join(APP_DIR, "package.json"), "utf8"),
  ).version;
  const a = normalize(appVersion);
  const b = normalize(manifest.chahuaVersion);
  if (!a) fail(`app/package.json version 形如 X.Y.Z / X.Y.Z-dev，收到 ${appVersion}`);
  if (!b) fail(`bundle chahua 版本形如 X.Y.Z / X.Y.Z.dev0，收到 ${manifest.chahuaVersion}`);
  if (a.core !== b.core || a.dev !== b.dev) {
    fail(
      `版本不一致：app/package.json=${appVersion}，bundle 内 chahua=${manifest.chahuaVersion}\n` +
      `  两处都要改（发版须五处同步），再 npm run build:python`,
    );
  }

  console.log(
    `[check-bundle] ✓ agentao ${manifest.agentaoVersion}（lock）· ` +
    `chahua ${manifest.chahuaVersion} · ${manifest.platform} · built ${manifest.builtAt}`,
  );
}

main();
