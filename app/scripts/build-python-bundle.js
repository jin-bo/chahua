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
//   3. 依赖**只认 ``uv.lock``**（P19）：``uv export --locked`` 导出带 hash 的运行依赖 →
//      ``pip install --require-hashes -r``；chahua 自身 ``uv build --wheel`` 后
//      ``--no-deps`` 装，不给 pip 第二次自由解析的机会。开发环境与安装包因此同版本。
//      agentao 走 PyPI 锁定版，**不再依赖同级 ``../agentao`` 源码**。
//   4. **不走** 生成的 ``bin/chahua-server`` 入口脚本 —— pip 写的 shebang 是构建时
//      绝对路径，bundle 搬到 .app 内立即失效。运行时 sidecar.js 直接 ``python -m
//      chahua.server`` 绕过 shebang
//
// 平台分支（P3.3.3 Windows 接缝）：``platformInfo()`` 一处分派，目录命名 / 可执行
// 后缀 / scripts 目录差异都集中在那。当前只跑 macOS；非 macOS 路径只是占位定义。
//
// 缓存（P19）：``bundle-manifest.json`` 记构建指纹（锁文件 / chahua 构建输入 / python
// 请求版本 / OS·架构 / 本脚本 / agentao 来源）。指纹一致才跳过；manifest **只在全部检查
// 通过后**才写，半截失败的产物永远命不中缓存。
//
// 用法：
//   node app/scripts/build-python-bundle.js          # 指纹一致则跳过
//   FORCE=1 node app/scripts/build-python-bundle.js  # 强制清重建
//   CHAHUA_AGENTAO_SOURCE=../agentao node ...        # 本地联调：锁定依赖装完后用该源码
//       覆盖 agentao。manifest 记 commit + dirty；**不得用于正式发布**（进指纹，
//       不带该变量的正式构建必然重建，不会误用联调产物）。

const fs = require("node:fs");
const fsp = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const crypto = require("node:crypto");
const { spawnSync } = require("node:child_process");

const APP_DIR = path.resolve(__dirname, "..");
const REPO_ROOT = path.resolve(APP_DIR, "..");
const CHAHUA_SOURCE = REPO_ROOT;
const BUNDLE_ROOT = path.join(APP_DIR, "python-bundle");
const STABLE_PYTHON_DIR = path.join(BUNDLE_ROOT, "python");
const MANIFEST_PATH = path.join(BUNDLE_ROOT, "bundle-manifest.json");
const LOCK_PATH = path.join(REPO_ROOT, "uv.lock");
// 决定 chahua wheel 内容的输入：包源码（含 personas 资源）+ 构建元数据。
const CHAHUA_BUILD_INPUTS = ["chahua", "pyproject.toml", "README.md", "LICENSE"];

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

function capture(cmd, args, opts = {}) {
  const res = spawnSync(cmd, args, { encoding: "utf8", ...opts });
  if (res.status !== 0) {
    throw new Error(`${[cmd, ...args].join(" ")} → 退出码 ${res.status ?? res.signal}\n${res.stderr || ""}`);
  }
  return res.stdout.trim();
}

// 本地联调入口：显式 opt-in，返回 null 表示走锁定的 PyPI 发布版（正式构建）。
function localAgentaoSource() {
  const raw = process.env.CHAHUA_AGENTAO_SOURCE;
  if (!raw) return null;
  const dir = path.resolve(REPO_ROOT, raw);
  if (!fs.existsSync(path.join(dir, "pyproject.toml"))) {
    throw new Error(`CHAHUA_AGENTAO_SOURCE 不是 agentao 源码目录：${dir}`);
  }
  let commit = null;
  let dirty = null;
  try {
    commit = capture("git", ["-C", dir, "rev-parse", "HEAD"]);
    dirty = capture("git", ["-C", dir, "status", "--porcelain"]) !== "";
  } catch {
    // 非 git 目录：commit / dirty 留 null，manifest 里照实记
  }
  return { path: dir, commit, dirty };
}

function hashTree(hash, abs, rel) {
  const st = fs.statSync(abs);
  if (st.isDirectory()) {
    for (const name of fs.readdirSync(abs).sort()) {
      if (name === "__pycache__" || name.endsWith(".pyc") || name === ".DS_Store") continue;
      hashTree(hash, path.join(abs, name), `${rel}/${name}`);
    }
    return;
  }
  hash.update(`\0${rel}\0`);
  hash.update(fs.readFileSync(abs));
}

function computeFingerprint(info, localSrc) {
  const hash = crypto.createHash("sha256");
  hash.update(JSON.stringify({
    platform: info.label,
    python: PYTHON_REQUEST,
    // dirty 的联调源码内容不可指纹化 → 每次都重建
    agentao: localSrc ? { ...localSrc, nonce: localSrc.dirty !== false ? Date.now() : 0 } : "lock",
  }));
  hashTree(hash, LOCK_PATH, "uv.lock");
  hashTree(hash, __filename, "build-python-bundle.js");
  for (const rel of CHAHUA_BUILD_INPUTS) {
    const abs = path.join(REPO_ROOT, rel);
    if (fs.existsSync(abs)) hashTree(hash, abs, rel);
  }
  return hash.digest("hex");
}

function readManifest() {
  try {
    return JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf8"));
  } catch {
    return null;
  }
}

// uv.lock 里 agentao 的锁定版本 —— bundle 实装版本必须与它逐字相等。
function lockedAgentaoVersion() {
  const m = fs.readFileSync(LOCK_PATH, "utf8").match(/\[\[package\]\]\r?\nname = "agentao"\r?\nversion = "([^"]+)"/);
  if (!m) throw new Error("uv.lock 里没找到 agentao");
  return m[1];
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

  const localSrc = localAgentaoSource();
  if (localSrc) {
    console.warn(
      `[build-python] ⚠ 本地联调：agentao 取自 ${localSrc.path} ` +
      `(commit=${localSrc.commit ?? "?"} dirty=${localSrc.dirty ?? "?"}) —— 不得用于正式发布`,
    );
  }
  const fingerprint = computeFingerprint(info, localSrc);

  const finalPyExe = path.join(STABLE_PYTHON_DIR, info.pythonExeRel);
  const prev = readManifest();
  if (!force && fs.existsSync(finalPyExe) && prev && prev.fingerprint === fingerprint) {
    console.log(`[build-python] 指纹一致，跳过（agentao ${prev.agentaoVersion}）；FORCE=1 强制重建`);
    return;
  }
  if (!force && fs.existsSync(finalPyExe)) {
    console.log("[build-python] 构建输入已变（或旧 bundle 无 manifest）→ 重建");
  }

  // 先验锁再清旧 bundle：锁过期 / 解析不到 agentao 时不白删一份能用的产物
  const locked = lockedAgentaoVersion();
  run("uv", ["lock", "--check"], { cwd: REPO_ROOT });

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

  // 4. 按 uv.lock 装运行依赖 + chahua wheel。``--locked`` 在 lock 与 pyproject 不一致时
  // 直接失败；``--require-hashes`` 钉死制品；chahua ``--no-deps`` 不触发二次解析。
  const pyExe = path.join(STABLE_PYTHON_DIR, info.pythonExeRel);
  const pip = ["-m", "pip", "install", "--no-warn-script-location", "--disable-pip-version-check"];
  const tmp = await fsp.mkdtemp(path.join(os.tmpdir(), "chahua-bundle-"));
  try {
    const reqs = path.join(tmp, "requirements.txt");
    run("uv", ["export", "--quiet", "--locked", "--no-dev", "--no-emit-project", "--format",
      "requirements-txt", "-o", reqs], { cwd: REPO_ROOT });
    run(pyExe, [...pip, "--require-hashes", "-r", reqs]);

    const wheelDir = path.join(tmp, "wheel");
    run("uv", ["build", "--wheel", "-o", wheelDir], { cwd: CHAHUA_SOURCE });
    const wheels = (await fsp.readdir(wheelDir)).filter((n) => n.endsWith(".whl"));
    if (wheels.length !== 1) throw new Error(`期望恰好 1 个 chahua wheel，看到：${wheels.join(", ")}`);
    run(pyExe, [...pip, "--no-deps", path.join(wheelDir, wheels[0])]);

    if (localSrc) {
      run(pyExe, [...pip, "--no-deps", "--force-reinstall", localSrc.path]);
    }
  } finally {
    await rmrf(tmp);
  }

  // 5. 检查：依赖一致 / agentao 实装版本 = 锁定版本 / sidecar 可启动
  run(pyExe, ["-m", "pip", "check", "--disable-pip-version-check"]);
  const agentaoVersion = capture(pyExe, ["-c",
    "import importlib.metadata as m; print(m.version('agentao'))"]);
  if (!localSrc && agentaoVersion !== locked) {
    throw new Error(`bundle 内 agentao=${agentaoVersion}，uv.lock 锁定=${locked}`);
  }
  console.log(`[build-python] agentao ${agentaoVersion}（uv.lock: ${locked}）`);
  run(pyExe, ["-m", "chahua.server", "--help"]);

  // 6. 体积报告（方便估 .dmg 大小）
  const sizeRes = spawnSync("du", ["-sh", BUNDLE_ROOT], { encoding: "utf8" });
  if (sizeRes.stdout) console.log(`[build-python] bundle 体积: ${sizeRes.stdout.trim()}`);

  // 7. 全部检查通过才写 manifest —— 失败产物没有它，下次必重建
  await fsp.writeFile(MANIFEST_PATH, `${JSON.stringify({
    fingerprint,
    platform: info.label,
    pythonRequest: PYTHON_REQUEST,
    agentaoVersion,
    agentaoSource: localSrc ?? "lock",
    chahuaVersion: capture(pyExe, ["-c", "import importlib.metadata as m; print(m.version('chahua'))"]),
    builtAt: new Date().toISOString(),
  }, null, 2)}\n`);

  console.log(`[build-python] ✓ ${STABLE_PYTHON_DIR}`);
}

main().catch((e) => {
  console.error(`[build-python] 失败：${e.message}`);
  process.exit(1);
});
