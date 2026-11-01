// P20.1 node 层：live_status_core.mjs 文案口径。
// 跑法：`node --test app/renderer/live_status_core.test.mjs`；由 tests/test_frontend_llm_calls.py 接进 uv run pytest。

import test from "node:test";
import assert from "node:assert/strict";

import { describeLiveStatus } from "./live_status_core.mjs";

test("retrying：带序号与上限，原因进 title", () => {
  const h = describeLiveStatus({ state: "retrying", retry: 1, max_retries: 4, delay_s: 1.6, reason: "status=503" });
  assert.equal(h.state, "retrying");
  assert.equal(h.text, "正在重连…（第 1/4 次）");
  assert.match(h.title, /status=503/);
});

test("retrying：缺上限 / 缺序号 / 坏值时降级", () => {
  assert.equal(describeLiveStatus({ state: "retrying", retry: 2 }).text, "正在重连…（第 2 次）");
  assert.equal(describeLiveStatus({ state: "retrying" }).text, "正在重连…");
  assert.equal(describeLiveStatus({ state: "retrying", retry: "x", max_retries: 3 }).text, "正在重连…");
  assert.doesNotMatch(describeLiveStatus({ state: "retrying" }).title, /（）/);
});

test("compacting", () => {
  assert.equal(describeLiveStatus({ state: "compacting" }).text, "正在整理上下文…");
});

test("idle / 未知 / 坏数据 → null（清提示）", () => {
  assert.equal(describeLiveStatus({ state: "idle" }), null);
  assert.equal(describeLiveStatus({ state: "whatever" }), null);
  assert.equal(describeLiveStatus(null), null);
  assert.equal(describeLiveStatus("retrying"), null);
});
