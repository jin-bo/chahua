#!/usr/bin/env node
"use strict";

// 茶话室 python sidecar 打包脚本（P3.3.2.c）。
//
// 在 ``electron-builder`` 之前跑一次，把可重定位的 python + chahua + agentao
// 烤进 ``app/python-bundle/python/``。``electron-builder`` 通过 ``extraResources``
// 把整个目录搬到 ``.app/Contents/Resources/python-bundle/``。
//
// 配方（实测可走）：
//   1. ``uv python install <ver> --install-dir <bundle>`` 拉 python-build-standalone
//      （uv 内部走 indygreg/astral-sh release，自动按 platform-arch 选 tarball）
//   2. uv 装出来的 python 默认带 ``lib/python<ver>/EXTERNALLY-MANAGED`` 标记，
//      pip / uv pip 拒绝直装 → 删了就行（python-build-standalone 原版 tarball 没这个）
//   3. ``<python>/bin/python -m pip install <agentao-src> <chahua-src>`` 非 editable
//      装，把 chahua / agentao 源码 + personas .md/.png 全 copy 进 site-packages
//   4. **不走** 生成的 ``bin/chahua-server`` 入口脚本 —— pip 写的 shebang 是构建时
//      绝对路径，bundle 搬到 .app 内立即失效。运行时 sidecar.js 直接 ``python -m
//      chahua.server`` 绕过 shebang
//
// 平台分支（P3.3.3 Windows 接缝）：``platformInfo()`` 一处分派，目录命名 / 可执行
// 后缀 / scripts 目录差异都集中在那。当前只跑 macOS；非 macOS 路径只是占位定义。
//
// 用法：
//   node app/scripts/build-python-bundle.js          # idempotent，已存在跳过
//   FORCE=1 node app/scripts/build-python-bundle.js  # 强制清重建

const fs = require("node:fs");
const fsp = require("node:fs/promises");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const APP_DIR = path.resolve(__dirname, "..");
const REPO_ROOT = path.resolve(APP_DIR, "..");
const PARENT_DIR = path.resolve(REPO_ROOT, "..");
const AGENTAO_SOURCE = path.join(PARENT_DIR, "agentao");
const CHAHUA_SOURCE = REPO_ROOT;
const BUNDLE_ROOT = path.join(APP_DIR, "python-bundle");
const STABLE_PYTHON_DIR = path.join(BUNDLE_ROOT, "python");

// uv 拉 python-build-standalone：major.minor 即可，uv 自动选最新 patch。pin 死 patch
// 反而坏 —— uv 老版本可能没那个 patch；让 uv 自由选当前可用最新。
const PYTHON_REQUEST = "3.12";

// 哪些条目跨平台一致 —— platformInfo 只暴露平台差异。
function platformInfo() {
  switch (process.platform) {
    case "darwin":
      return {
        label: `macos-${process.arch}`,
        binDir: "bin",                  // <python>/bin/python3.12
        pythonExeRel: "bin/python3.12", // 相对 stable python dir
        scriptsDir: "bin",              // pip 写 entry scripts 到这里（macOS 同 binDir）
      };
    case "win32":
      // Windows 接缝（P3.3.3 启用）。命名约定与 python-build-standalone 一致：
      // python.exe 在根、pip / chahua-server.exe 在 Scripts/。
      return {
        label: `windows-${process.arch}`,
        binDir: ".",                    // <python>/python.exe
        pythonExeRel: "python.exe",
        scriptsDir: "Scripts",          // <python>/Scripts/chahua-server.exe（不走，但接缝在此）
      };
    case "linux":
      return {
        label: `linux-${process.arch}`,
        binDir: "bin",
        pythonExeRel: "bin/python3.12",
        scriptsDir: "bin",
      };
    default:
      throw new Error(`不支持的平台：${process.platform}`);
  }
}

function run(cmd, args, opts = {}) {
  const display = [cmd, ...args].join(" ");
  console.log(`\n[build-python] $ ${display}`);
  const res = spawnSync(cmd, args, { stdio: "inherit", ...opts });
  if (res.status !== 0) {
    throw new Error(`${display} → 退出码 ${res.status ?? res.signal}`);
  }
}

async function rmrf(p) {
  await fsp.rm(p, { recursive: true, force: true });
}

// uv 装到 --install-dir 后，子目录像这样：
//   <bundle>/
//   ├── cpython-3.12-macos-aarch64-none           ← symlink alias（短）
//   └── cpython-3.12.13-macos-aarch64-none        ← 真目录（含 patch 版本）
// 我们要找真目录、改名 ``python``，再删 alias。
async function findRealPythonDir() {
  const entries = await fsp.readdir(BUNDLE_ROOT, { withFileTypes: true });
  const reals = entries.filter(
    (e) => e.isDirectory() && !e.isSymbolicLink() && e.name.startsWith("cpython-"),
  );
  if (reals.length !== 1) {
    throw new Error(
      `python-bundle 里没找到唯一的 cpython-* 真目录，看到：${entries.map((e) => e.name).join(", ")}`,
    );
  }
  return path.join(BUNDLE_ROOT, reals[0].name);
}

async function removeMarker(stablePyDir) {
  // EXTERNALLY-MANAGED 在 lib/python3.X/EXTERNALLY-MANAGED；用 glob 找省得知 X 是几。
  const libDir = path.join(stablePyDir, "lib");
  if (!fs.existsSync(libDir)) return;
  for (const name of await fsp.readdir(libDir)) {
    if (!name.startsWith("python3.")) continue;
    const marker = path.join(libDir, name, "EXTERNALLY-MANAGED");
    if (fs.existsSync(marker)) {
      await fsp.rm(marker);
      console.log(`[build-python] removed ${marker}`);
    }
  }
}

async function main() {
  const info = platformInfo();
  const force = process.env.FORCE === "1";
  console.log(`[build-python] target=${info.label} python=${PYTHON_REQUEST}`);

  if (!fs.existsSync(AGENTAO_SOURCE)) {
    throw new Error(`agentao 源码不存在：${AGENTAO_SOURCE}（应当与 chahua 同级）`);
  }

  const finalPyExe = path.join(STABLE_PYTHON_DIR, info.pythonExeRel);
  if (fs.existsSync(finalPyExe) && !force) {
    console.log(`[build-python] 已存在 ${finalPyExe}；FORCE=1 强制重建`);
    return;
  }

  await rmrf(BUNDLE_ROOT);
  await fsp.mkdir(BUNDLE_ROOT, { recursive: true });

  // 1. uv 拉 python
  run("uv", ["python", "install", PYTHON_REQUEST, "--install-dir", BUNDLE_ROOT]);

  // 2. 找真目录、改名 python；删 alias 占位
  const realDir = await findRealPythonDir();
  console.log(`[build-python] 真目录: ${realDir}`);
  await fsp.rename(realDir, STABLE_PYTHON_DIR);
  // alias 与 uv 内部状态目录一并清掉（``.lock`` / ``.temp`` / ``.gitignore`` /
  // ``cpython-3.12-...`` symlink）—— bundle 只留 ``python/``
  for (const name of await fsp.readdir(BUNDLE_ROOT)) {
    if (name === "python") continue;
    await rmrf(path.join(BUNDLE_ROOT, name));
  }

  // 3. 删 EXTERNALLY-MANAGED marker
  await removeMarker(STABLE_PYTHON_DIR);

  // 4. 装 agentao + chahua（顺序重要 —— agentao 先装，否则 pip resolve chahua deps
  // 时会去 PyPI 拉一份过时的 agentao 覆盖；先装本地 agentao，后续 chahua 依赖
  // 满足，不会触发 PyPI fetch）
  const pyExe = path.join(STABLE_PYTHON_DIR, info.pythonExeRel);
  run(pyExe, ["-m", "pip", "install", "--no-warn-script-location", AGENTAO_SOURCE]);
  run(pyExe, ["-m", "pip", "install", "--no-warn-script-location", CHAHUA_SOURCE]);

  // 5. sanity：``python -m chahua.server --help`` 应当 exit 0
  run(pyExe, ["-m", "chahua.server", "--help"]);

  // 6. 体积报告（方便估 .dmg 大小）
  const sizeRes = spawnSync("du", ["-sh", BUNDLE_ROOT], { encoding: "utf8" });
  if (sizeRes.stdout) console.log(`[build-python] bundle 体积: ${sizeRes.stdout.trim()}`);

  console.log(`[build-python] ✓ ${STABLE_PYTHON_DIR}`);
}

main().catch((e) => {
  console.error(`[build-python] 失败：${e.message}`);
  process.exit(1);
});
