"use strict";

// 茶话室首启动 seed（P3.3.2.b）。
//
// 把 ``app/templates/`` 下的默认房 / .env.example / USER.md 拷到 ``userDataRoot``。
// 幂等：每条目只在 dest 不存在时拷 —— 用户改过的 USER.md / .env 永不被覆盖；
// 用户删过的默认房不会被"复活"硬塞回来（删 = 用户意愿，下次启动尊重）。
//
// dev 模式下 ``userDataRoot === appRoot === 仓库根``，所有目标已存在 → 全跳过；
// 与 P3.3.2.a 之前行为一致，不会动 repo 里的真文件。

const fs = require("node:fs");
const fsp = require("node:fs/promises");
const path = require("node:path");

// userDataRoot 第一次出现的"被 seed 过"标记。存在 = 跳过整轮 seed（即使用户自己删过
// 某些条目也不再补 —— 见上）。文件名带前导点避免在 Finder / Explorer 露出。
const SEED_MARKER = ".chahua-seeded";

// 模板里要拷的条目（相对 templatesDir）。每条目独立判存在 + 拷贝；目录走递归拷。
// 顺序无关紧要，列表只是 source of truth。
const ENTRIES = [
  { rel: "USER.md", kind: "file" },
  { rel: ".env.example", kind: "file" },
  { rel: "rooms/p3-黄河路", kind: "dir" },
];

async function copyFileIfMissing(src, dest) {
  try {
    await fsp.access(dest, fs.constants.F_OK);
    return false; // 已存在，跳过
  } catch {
    /* fall through to copy */
  }
  await fsp.mkdir(path.dirname(dest), { recursive: true });
  await fsp.copyFile(src, dest);
  return true;
}

async function copyDirIfMissing(src, dest) {
  try {
    await fsp.access(dest, fs.constants.F_OK);
    return false; // 整目录已存在 → 跳过（不做"逐文件 merge"，避免和用户编辑撞）
  } catch {
    /* fall through */
  }
  await fsp.cp(src, dest, { recursive: true });
  return true;
}

// 把 templates → userDataRoot 拷一遍。返回真正拷过的条目数（0 = 全已存在或跳过）。
//
// userDataRoot 与 appRoot 同源（dev）时直接 return —— 没必要往仓库里写 marker，
// 而且 entries 已经全在 repo 根下；marker 是 packaged 路径专属概念。
async function seedUserData({ templatesDir, userDataRoot, appRoot }) {
  if (userDataRoot === appRoot) return 0;

  const markerPath = path.join(userDataRoot, SEED_MARKER);
  // marker 在 → 之前 seed 过，跳过整轮。哪怕用户后来删了某条目，也尊重那个意愿。
  try {
    await fsp.access(markerPath, fs.constants.F_OK);
    return 0;
  } catch {
    /* 继续 seed */
  }

  let copied = 0;
  for (const { rel, kind } of ENTRIES) {
    const src = path.join(templatesDir, rel);
    const dest = path.join(userDataRoot, rel);
    const fn = kind === "dir" ? copyDirIfMissing : copyFileIfMissing;
    if (await fn(src, dest)) copied += 1;
  }

  // marker 最后写 —— 之前的拷贝失败异常会被抛出来，marker 不留，下次重试。
  await fsp.mkdir(userDataRoot, { recursive: true });
  await fsp.writeFile(
    markerPath,
    `seeded at ${new Date().toISOString()}\n`,
    "utf8",
  );
  return copied;
}

module.exports = { seedUserData };
