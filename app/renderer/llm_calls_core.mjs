// P20 茶客调用消耗取证 —— 调试抽屉的纯逻辑（零浏览器 API，node:test 覆盖）。
//
// 口径见 docs/P20-茶客调用消耗取证.md §6.3，承重的几条：
// - 「一次调用」= 一次主循环模型调用（agentao 一次 chat_stream，含内部重试），不是 HTTP 请求。
// - 缓存 / 推理 token 是输入 / 输出的「其中」，合计**绝不**再加。
// - null 与缺键都算未知；上游 0 可能是「未声明」，不当确认零展示（括注直接不显示）。
// - 输入、输出**分别**判断完整性；全部未知显示「未提供」，绝不显示 0。
// - 截断、旧记录混合都要显式提示，部分数据绝不显示成完整合计。
//
// 为何是 .mjs：见 flint_core.mjs 头注（app/package.json 不能加 "type":"module"）。

/** 把 message_end.data 或 turns.jsonl 的 message 条目规范化成抽屉内部形态。
 *  缺 ``llm_calls`` 键（P20 前的旧记录 / 记录失败）→ 返回 null =「无记录」。 */
export function normalizeLlmRecords(source) {
  if (!source || typeof source !== "object" || !Array.isArray(source.llm_calls)) {
    return null;
  }
  const isObj = (x) => x && typeof x === "object" && !Array.isArray(x);
  return {
    calls: source.llm_calls.filter(isObj),
    compactions: Array.isArray(source.compactions) ? source.compactions.filter(isObj) : [],
    truncated: source.llm_records_truncated === true,
  };
}

function isCount(v) {
  return typeof v === "number" && Number.isFinite(v) && v >= 0;
}

function newPart() {
  return { sum: 0, known: 0, unknown: 0 };
}

function addTo(part, v) {
  if (isCount(v)) {
    part.sum += v;
    part.known += 1;
  } else {
    part.unknown += 1;
  }
}

/** 汇总若干条消息的调用记录。``messages`` 每项 = 内部 message record，读其 ``llm`` 字段：
 *  - ``undefined``：实时进行中（message_end 未到）→ 静默跳过，不算缺；
 *  - ``null``：无记录（旧记录 / 记录失败）→ 计入 ``missingMessages``；
 *  - 对象：``normalizeLlmRecords`` 的结果。 */
export function summarizeLlmCalls(messages) {
  const out = {
    calls: 0,
    durationMs: 0,
    durationKnown: 0,
    input: newPart(),
    output: newPart(),
    cacheRead: 0,
    reasoning: 0,
    retries: 0,
    compactions: 0,
    truncated: false,
    recordedMessages: 0,
    missingMessages: 0,
  };
  for (const m of messages) {
    const llm = m ? m.llm : undefined;
    if (llm === undefined) continue;
    if (llm === null) {
      out.missingMessages += 1;
      continue;
    }
    out.recordedMessages += 1;
    if (llm.truncated) out.truncated = true;
    out.compactions += llm.compactions.length;
    for (const c of llm.calls) {
      out.calls += 1;
      if (isCount(c.duration_ms)) {
        out.durationMs += c.duration_ms;
        out.durationKnown += 1;
      }
      addTo(out.input, c.prompt_tokens);
      addTo(out.output, c.completion_tokens);
      // 「其中」项只随父项一起计：父项未知时把子项加进来，会出现「输入 100（缓存读 800）」
      // 这种子项大于父项的读数。
      if (isCount(c.prompt_tokens) && isCount(c.cache_read_tokens)) out.cacheRead += c.cache_read_tokens;
      if (isCount(c.completion_tokens) && isCount(c.reasoning_tokens)) out.reasoning += c.reasoning_tokens;
      if (Array.isArray(c.retries)) out.retries += c.retries.length;
    }
  }
  return out;
}

export function formatCount(n) {
  return Math.round(n).toLocaleString("en-US");
}

export function formatMs(ms) {
  if (!isCount(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)}s`;
}

/** 合计的一项（输入或输出）：完整 / 部分 / 全未知 三态。 */
export function formatPart(part) {
  if (part.known === 0) return "未提供";
  const base = formatCount(part.sum);
  return part.unknown > 0 ? `${base}（部分未提供）` : base;
}

/** 单次调用的一项：null / 缺键 → 未提供。 */
function formatOne(v) {
  return isCount(v) ? formatCount(v) : "未提供";
}

/** 「其中」括注：只在 > 0 时出现（上游 0 可能是未声明）。 */
function ofWhich(label, v) {
  return isCount(v) && v > 0 ? `（${label} ${formatCount(v)}）` : "";
}

/** 单次调用的一行文本片段。``index`` 从 1 起。 */
export function describeCall(call, index) {
  const parts = [`#${index}`, String(call.status || "?")];
  if (isCount(call.duration_ms)) parts.push(formatMs(call.duration_ms));
  if (isCount(call.first_token_ms)) parts.push(`首字 ${formatMs(call.first_token_ms)}`);
  parts.push(`输入 ${formatOne(call.prompt_tokens)}${ofWhich("缓存读", call.cache_read_tokens)}`);
  parts.push(`输出 ${formatOne(call.completion_tokens)}${ofWhich("推理", call.reasoning_tokens)}`);
  if (typeof call.finish_reason === "string" && call.finish_reason) {
    parts.push(call.finish_reason + (call.finish_reason_reported === false ? "（未报告）" : ""));
  }
  if (call.status === "error" && call.error_class) parts.push(String(call.error_class));
  return parts.join(" · ");
}

export function describeRetries(retries) {
  if (!Array.isArray(retries) || retries.length === 0) return "";
  const detail = retries
    .map((r) => {
      const reason = r && typeof r.reason === "string" ? r.reason : "?";
      const wait = r && isCount(r.delay_s) ? `，等待 ${+r.delay_s.toFixed(2)}s` : "";
      return `${reason}${wait}`;
    })
    .join("；");
  return `↻ 重试 ${retries.length} 次（${detail}）`;
}

export function describeCompaction(c) {
  const parts = [c.kind || "?", c.status || "?"];
  if (isCount(c.pre_msgs) && isCount(c.post_msgs)) parts.push(`${c.pre_msgs}→${c.post_msgs} 条`);
  if (isCount(c.duration_ms)) parts.push(formatMs(c.duration_ms));
  return parts.join(" · ");
}

/** 汇总行（消息级与 turn 级共用）+ 需要叠加的完整性提示。 */
export function describeSummary(s) {
  // 用时全未知（如取消占位）显示「—」，不冒充 0ms。
  // 部分调用用时未知（如混入取消占位）时与输入 / 输出同口径标「部分未提供」。
  let dur = s.durationKnown > 0 ? formatMs(s.durationMs) : "—";
  if (s.durationKnown > 0 && s.durationKnown < s.calls) dur += "（部分未提供）";
  let text = `${s.calls} 次 · 用时 ${dur} · 输入 ${formatPart(s.input)}`;
  if (s.cacheRead > 0) text += `（缓存读 ${formatCount(s.cacheRead)}）`;
  text += ` · 输出 ${formatPart(s.output)}`;
  if (s.reasoning > 0) text += `（推理 ${formatCount(s.reasoning)}）`;
  if (s.retries > 0) text += ` · 重试 ${s.retries}`;
  if (s.compactions > 0) text += ` · 压缩 ${s.compactions}`;
  const notes = [];
  if (s.truncated) notes.push("记录已截断，仅统计已记录部分");
  if (s.missingMessages > 0) notes.push("部分消息无记录");
  return { text, notes };
}

export const CALL_UNIT_HINT =
  "一次模型调用 = 茶客一次「调模型 → 跑工具」循环里的一次模型请求；"
  + "用时含重试与退避等待，用量只来自最终那次响应。不含打分、摘要与上下文压缩本身的消耗。";
