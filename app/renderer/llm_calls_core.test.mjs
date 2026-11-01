// P20 node 层：llm_calls_core.mjs 的汇总与显示口径（docs/P20 §6.3、§8 前端）。
// 跑法：`node --test app/renderer/llm_calls_core.test.mjs`；由 tests/test_frontend_llm_calls.py 接进 uv run pytest。

import test from "node:test";
import assert from "node:assert/strict";

import {
  normalizeLlmRecords,
  summarizeLlmCalls,
  formatPart,
  formatMs,
  describeCall,
  describeRetries,
  describeCompaction,
  describeSummary,
} from "./llm_calls_core.mjs";

const call = (over = {}) => ({
  status: "ok",
  duration_ms: 1000,
  first_token_ms: 300,
  finish_reason: "stop",
  finish_reason_reported: true,
  prompt_tokens: 1000,
  completion_tokens: 50,
  cache_read_tokens: 800,
  cache_creation_tokens: 0,
  reasoning_tokens: 0,
  ...over,
});
const msg = (calls, extra = {}) => ({ llm: normalizeLlmRecords({ llm_calls: calls, ...extra }) });

test("normalize：缺 llm_calls 键 → null（无记录）；坏项过滤", () => {
  assert.equal(normalizeLlmRecords({}), null);
  assert.equal(normalizeLlmRecords(null), null);
  assert.equal(normalizeLlmRecords({ llm_calls: "x" }), null);
  const n = normalizeLlmRecords({ llm_calls: [call(), null, 3], llm_records_truncated: true });
  assert.equal(n.calls.length, 1);
  assert.deepEqual(n.compactions, []);
  assert.equal(n.truncated, true);
});

test("缓存 / 推理是「其中」：合计不重复相加", () => {
  const s = summarizeLlmCalls([msg([call({ reasoning_tokens: 20 }), call()])]);
  assert.equal(s.input.sum, 2000);
  assert.equal(s.output.sum, 100);
  assert.equal(s.cacheRead, 1600);
  assert.equal(s.reasoning, 20);
  assert.equal(formatPart(s.input), "2,000");
});

test("输入、输出分别判断完整性", () => {
  const s = summarizeLlmCalls([msg([call(), call({ completion_tokens: null })])]);
  assert.equal(formatPart(s.input), "2,000");
  assert.equal(formatPart(s.output), "50（部分未提供）");
});

test("缺键与 null 同为未知；全部未知显示「未提供」而非 0", () => {
  const s = summarizeLlmCalls([msg([{ status: "cancelled", retries: [{ retry: 1 }] }, call({ prompt_tokens: null, completion_tokens: null })])]);
  assert.equal(s.calls, 2);
  assert.equal(formatPart(s.input), "未提供");
  assert.equal(formatPart(s.output), "未提供");
  assert.doesNotMatch(describeSummary(s).text, /输入 0/);
});

test("截断提示", () => {
  const s = summarizeLlmCalls([msg([call()], { llm_records_truncated: true })]);
  assert.deepEqual(describeSummary(s).notes, ["记录已截断，仅统计已记录部分"]);
});

test("新旧混合：null = 部分消息无记录；undefined（进行中）静默跳过", () => {
  const s = summarizeLlmCalls([msg([call()]), { llm: null }, { llm: undefined }, {}]);
  assert.equal(s.recordedMessages, 1);
  assert.equal(s.missingMessages, 1);
  assert.deepEqual(describeSummary(s).notes, ["部分消息无记录"]);
});

test("单次调用：0 的缓存 / 推理不显示括注；null 显示未提供", () => {
  const line = describeCall(call({ cache_read_tokens: 0 }), 1);
  assert.doesNotMatch(line, /缓存读/);
  assert.doesNotMatch(line, /推理/);
  assert.match(line, /输入 1,000 · 输出 50/);
  assert.match(describeCall(call({ prompt_tokens: null }), 2), /输入 未提供/);
  assert.match(describeCall(call({ cache_read_tokens: 800 }), 1), /输入 1,000（缓存读 800）/);
});

test("finish_reason 未报告 / error 行", () => {
  assert.match(describeCall(call({ finish_reason_reported: false }), 1), /stop（未报告）/);
  const err = describeCall({ status: "error", error_class: "RateLimitError", duration_ms: 900 }, 1);
  assert.match(err, /#1 · error · 900ms/);
  assert.match(err, /RateLimitError/);
});

test("重试与压缩描述", () => {
  assert.equal(describeRetries([]), "");
  assert.equal(
    describeRetries([{ retry: 1, delay_s: 1.5, reason: "status=429" }]),
    "↻ 重试 1 次（status=429，等待 1.5s）",
  );
  assert.equal(
    describeCompaction({ kind: "full", status: "success", pre_msgs: 120, post_msgs: 14, duration_ms: 21000 }),
    "full · success · 120→14 条 · 21.0s",
  );
});

test("formatMs", () => {
  assert.equal(formatMs(840), "840ms");
  assert.equal(formatMs(3120), "3.12s");
  assert.equal(formatMs(21000), "21.0s");
  assert.equal(formatMs(null), "—");
});

test("汇总行：重试与压缩计数", () => {
  const s = summarizeLlmCalls([
    msg([call({ retries: [{ retry: 1 }, { retry: 2 }] })], {
      compactions: [{ kind: "full", status: "success" }],
    }),
  ]);
  const { text, notes } = describeSummary(s);
  assert.match(text, /^1 次 · 用时 1.00s · 输入 1,000（缓存读 800） · 输出 50 · 重试 2 · 压缩 1$/);
  assert.deepEqual(notes, []);
});

test("describeSummary: 部分调用用时未知 → 用时标「部分未提供」", () => {
  const s = summarizeLlmCalls([
    { llm: { calls: [{ status: "ok", duration_ms: 1000, prompt_tokens: 1, completion_tokens: 1 }, { status: "cancelled" }], compactions: [], truncated: false } },
  ]);
  const { text } = describeSummary(s);
  assert.match(text, /用时 1\.00s（部分未提供）/);
});

test("其中项只随父项计：父项未知的调用不贡献缓存 / 推理", () => {
  const s = summarizeLlmCalls([msg([
    call({ prompt_tokens: null, cache_read_tokens: 800, completion_tokens: null, reasoning_tokens: 30 }),
    call({ prompt_tokens: 100, cache_read_tokens: 0, completion_tokens: 10, reasoning_tokens: 0 }),
  ])]);
  assert.equal(s.cacheRead, 0);
  assert.equal(s.reasoning, 0);
  assert.equal(describeSummary(s).text.includes("缓存读"), false);
});
