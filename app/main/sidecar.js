"use strict";

// 茶话室 sidecar 生命周期管理（P3.1）。
//
// main 进程拉起 chahua-server 子进程：
//   1. 找一个空闲端口（避免与本机其他 chahua 实例 / 用户的 7860 冲突）
//   2. `uv run chahua-server --host 127.0.0.1 --port <port>`，cwd 设到仓库根
//   3. 读 stderr，等到 "监听 ws://" 行出现再 resolve（拿到 wsUrl）
//   4. 退出时 SIGINT 优雅停，2s 不死强杀
//
// 不在范围（P3.2+）：失败重启、日志窗口、多实例（已有 single-instance lock）。

const { spawn } = require("node:child_process");
const net = require("node:net");

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

async function startSidecar({ repoRoot }) {
  const port = await findFreePort();
  const child = spawn(
    "uv",
    [
      "run", "chahua-server",
      "--host", "127.0.0.1",
      "--port", String(port),
      "--room", DEFAULT_ROOM_REL,
    ],
    {
      cwd: repoRoot,
      // PYTHONUNBUFFERED 让 print 立即冲到 stderr，不至于卡在 buffer 等不到 ready 行。
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
      stdio: ["ignore", "pipe", "pipe"],
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
    process.stderr.write(`[sidecar] ${chunk}`);
    // settle 后 stderr 仍要透传到主进程 console，但不再做 ready 扫描 ——
    // 省掉 slice + RegExp.test 的微小常驻成本。
    if (settled) return;
    // ready 行可能跨 chunk —— 把最近窗口拼起来再扫一次。
    scanBuf = (scanBuf + chunk).slice(-512);
    if (SIDECAR_READY_RE.test(scanBuf)) {
      settle(resolveReady);
      scanBuf = "";
    }
  });
  child.on("exit", (code, signal) => {
    settle(rejectReady, new Error(
      `sidecar exited before ready (code=${code} signal=${signal})`,
    ));
  });
  child.on("error", (err) => {
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
      child.kill("SIGINT");
      return await new Promise((res) => {
        const t = setTimeout(() => {
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
