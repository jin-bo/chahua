"use strict";

// 茶话室 sidecar 生命周期管理（P3.1 + P3.3.2.e）。
//
// main 进程拉起 chahua-server 子进程：
//   1. 找一个空闲端口（避免与本机其他 chahua 实例 / 用户的 7860 冲突）
//   2. `uv run chahua-server --host 127.0.0.1 --port <port>`，cwd 设到 appRoot
//   3. 读 stderr，等到 "监听 ws://" 行出现再 resolve（拿到 wsUrl）
//   4. stderr / stdout 同时落到 logsDir/sidecar.log（append + session header），
//      console 仍透传 [sidecar] 前缀让 dev 实时看见
//   5. 退出时 SIGINT 优雅停，2s 不死强杀
//
// 不在范围（P4）：日志旋转、失败重启、日志窗口、多实例（已有 single-instance lock）。

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const fsp = require("node:fs/promises");
const net = require("node:net");
const path = require("node:path");

// 与 chahua/server.py 的 print 行匹配："茶话室 server 监听 ws://127.0.0.1:7860"。
// 服务端将来微调措辞时，这里要跟着改 —— 唯一硬耦合点。
const SIDECAR_READY_RE = /监听\s+ws:\/\//;

// 起得太慢直接报错：超时 99% 是 uv 在 resolve / 下依赖，让用户先 `uv sync`。
const READY_TIMEOUT_MS = 15_000;

// SIGINT 后给 server 多久优雅关停（asyncio Event.wait → finally 关 ws）。
const STOP_GRACE_MS = 2_000;

// Electron 缺省进哪个茶室。与 chahua/session.py:DEFAULT_ROOM_REL 解耦 —— CLI 仍走
// p1-test（开发自测），桌面壳走 p3-黄河路（默认展示 3 茶客 + workspace-write）。
// P3.3+ 加房间选择 UI 后改成参数。
const DEFAULT_ROOM_REL = "rooms/p3-黄河路";

// dev 与 packaged 的 sidecar 启动命令分派。
//
// dev：依赖系统 uv，命令是 ``uv run chahua-server <args>`` —— 装新依赖 / 改源码
// 立即生效（agentao 也是 editable）。
//
// packaged：``app/scripts/build-python-bundle.js`` 把 python-build-standalone +
// chahua + agentao 烤进 ``<resources>/python-bundle/python/``。运行时**不走** pip
// 生成的 ``bin/chahua-server`` 入口脚本 —— shebang 是构建时绝对路径，bundle 搬到
// .app 里立即失效；改走 ``python -m chahua.server`` 绕过 shebang。
//
// 平台分支（P3.3.3 Windows 接缝）：python-build-standalone macOS/Linux 把可执行
// 放在 ``<bundle>/bin/python3``，Windows 直接 ``<bundle>/python.exe``。其他参数
// （--host / --port / --room）一致。
function resolveSidecarCommand({ paths }) {
  if (!paths.isPackaged) {
    return { cmd: "uv", preArgs: ["run", "chahua-server"] };
  }
  const bundleDir = path.join(paths.appRoot, "python-bundle", "python");
  const pyExe = process.platform === "win32"
    ? path.join(bundleDir, "python.exe")
    : path.join(bundleDir, "bin", "python3");
  return { cmd: pyExe, preArgs: ["-m", "chahua.server"] };
}

// 打开 sidecar.log 准备 append 写入。失败返回 null，调用方继续启动（日志写不了
// 不该挡 sidecar 起 —— logsDir 权限问题罕见但不致命）。
//
// 多次启动累加进同一文件，靠 session header 行分段；rotation 留 P4。
async function openSidecarLog(logsDir) {
  try {
    await fsp.mkdir(logsDir, { recursive: true });
    const file = path.join(logsDir, "sidecar.log");
    const stream = fs.createWriteStream(file, { flags: "a" });
    stream.write(
      `\n----- sidecar session ${new Date().toISOString()} pid=${process.pid} -----\n`,
    );
    return { stream, file };
  } catch (e) {
    console.warn(`[chahua] 打不开 sidecar.log（${logsDir}）：`, e);
    return null;
  }
}

async function findFreePort() {
  return await new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

async function startSidecar({ paths, logsDir }) {
  const { appRoot, userDataRoot } = paths;
  const port = await findFreePort();
  const logEntry = logsDir ? await openSidecarLog(logsDir) : null;
  if (logEntry) {
    console.log(`[chahua] sidecar log → ${logEntry.file}`);
  }
  // Node 文档不保证 'error' 与 'exit' 互斥（ENOENT 路径只 error，正常退只 exit；
  // 但极端竞态下都触发也不能让 stream 二次 end）。一个 boolean 防一下。
  let logClosed = false;
  const closeLog = (footer) => {
    if (logClosed || !logEntry) return;
    logClosed = true;
    logEntry.stream.end(footer);
  };
  // CHAHUA_APP_ROOT / CHAHUA_USER_DATA 透传给 python 端的 Paths.from_env()：
  // dev 两者同源仓库根（行为不变）；packaged 拆开 —— python 那边按双根搜 persona、
  // 按 userDataRoot 解 rooms / USER.md。cwd 仍用 appRoot 让 ``uv run`` 找得到
  // pyproject（dev）/ 让 python 找得到相对 import（packaged 多余但无害）。
  const { cmd, preArgs } = resolveSidecarCommand({ paths });
  const child = spawn(
    cmd,
    [
      ...preArgs,
      "--host", "127.0.0.1",
      "--port", String(port),
      "--room", DEFAULT_ROOM_REL,
    ],
    {
      cwd: appRoot,
      // PYTHONUNBUFFERED 让 print 立即冲到 stderr，不至于卡在 buffer 等不到 ready 行。
      env: {
        ...process.env,
        PYTHONUNBUFFERED: "1",
        CHAHUA_APP_ROOT: appRoot,
        CHAHUA_USER_DATA: userDataRoot,
      },
      // stdin 改 ``pipe`` —— P3.3.2.d stop() 关 stdin 写端给 python 优雅关停信号。
      // python 端 _watch_stdin_eof 监 EOF；stdin 是 tty 时不装 watcher 避抢字符。
      stdio: ["pipe", "pipe", "pipe"],
    },
  );

  let settled = false;
  let resolveReady;
  let rejectReady;
  const ready = new Promise((res, rej) => {
    resolveReady = res;
    rejectReady = rej;
  });
  const settle = (fn, val) => {
    if (settled) return;
    settled = true;
    fn(val);
  };

  let scanBuf = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => {
    logEntry?.stream.write(chunk);
    process.stderr.write(`[sidecar] ${chunk}`);
    // settle 后 stderr 仍要透传到主进程 console + 文件，但不再做 ready 扫描 ——
    // 省掉 slice + RegExp.test 的微小常驻成本。
    if (settled) return;
    // ready 行可能跨 chunk —— 把最近窗口拼起来再扫一次。
    scanBuf = (scanBuf + chunk).slice(-512);
    if (SIDECAR_READY_RE.test(scanBuf)) {
      settle(resolveReady);
      scanBuf = "";
    }
  });
  // 之前 stdio[1] = "pipe" 但 stdout 无 listener，Python 那边大量 print 会把
  // pipe buffer（~64KB）写满后阻塞 —— 一直没爆是因为 server.py 自身没怎么用
  // print。P3.3.2.e 落盘顺手吃掉 stdout，buffer 永远走得动。
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    logEntry?.stream.write(chunk);
    process.stdout.write(`[sidecar] ${chunk}`);
  });
  child.on("exit", (code, signal) => {
    closeLog(
      `----- sidecar exit code=${code} signal=${signal} ${new Date().toISOString()} -----\n`,
    );
    settle(rejectReady, new Error(
      `sidecar exited before ready (code=${code} signal=${signal})`,
    ));
  });
  child.on("error", (err) => {
    closeLog(`----- sidecar spawn error: ${err.message} -----\n`);
    settle(rejectReady, err);
  });

  const timeout = setTimeout(() => {
    settle(rejectReady, new Error(
      `sidecar didn't print 监听 within ${READY_TIMEOUT_MS}ms; try \`uv sync\` first`,
    ));
  }, READY_TIMEOUT_MS);

  try {
    await ready;
  } finally {
    clearTimeout(timeout);
  }

  return {
    wsUrl: `ws://127.0.0.1:${port}`,
    pid: child.pid,
    async stop() {
      if (child.exitCode !== null) return true;
      // 跨平台优雅关：关 stdin 写端 → python _watch_stdin_eof 读到 EOF →
      // set stop event → server.serve_forever 返回 → 整套 graceful 关。
      //
      // 之前 SIGINT 路径在 Windows 上其实是 TerminateProcess（不 graceful），
      // 还要 python 端两段式 signal trick 才能接到 —— stdin EOF 一条路径覆盖所有
      // 平台，且不依赖信号语义。``.end()`` 关写端，python 那侧 read() 返空 bytes。
      try {
        child.stdin?.end();
      } catch {
        // child 已退 / stdin 已关 → 无所谓，下面 exit 监听仍生效。
      }
      return await new Promise((res) => {
        const t = setTimeout(() => {
          // grace 没赶上 = python 卡了 / stdin watcher 没装上 → 硬杀。
          child.kill("SIGKILL");
          res(false);
        }, STOP_GRACE_MS);
        child.once("exit", () => {
          clearTimeout(t);
          res(true);
        });
      });
    },
  };
}

module.exports = { startSidecar };
