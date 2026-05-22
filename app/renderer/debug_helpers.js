"use strict";

// 调试抽屉的纯工具集 —— 常量 + 格式化 + 小 DOM 构件 + 工具源识别。从 debug_panel.js
// 抽出（debug_panel 重构）。全部无状态、本模块无 import，被 debug_render.js /
// debug_panel.js 共用。

// keep in sync with chahua/task_tools.py::TASK_WRITE_ARTIFACT_TOOL_NAME
const TASK_WRITE_ARTIFACT = "task_write_artifact";

// scoring_path 徽标文案 —— scoring / mention / broadcast 沿用既有英文标识；
// handoff_* 这类对用户不直观的 wire 值给中文短标。data-path 仍存原值供 CSS 配色。
const SCORING_PATH_LABELS = Object.freeze({
  handoff_delegate: "指派",
  handoff_review: "请审",
  handoff_panel: "圆桌",
});

// handoff turn 顶部提示条整句。与 SCORING_PATH_LABELS（折叠徽标短标）分开维护：
// delegate / review 的短标恰好是动词、panel 的「圆桌」是名词，提示条要完整动词
// 短语，不能统一从短标拼。
export const HANDOFF_NOTE_TEXT = Object.freeze({
  handoff_delegate: "由用户指派",
  handoff_review: "由用户请审",
  handoff_panel: "由用户发起圆桌",
});

export function scoringPathLabel(path) {
  return SCORING_PATH_LABELS[path] || path;
}

// handoff turn 的判定信号 —— scoring_path 以 ``handoff_`` 起头（live turn_start 与
// 历史 jsonl 都带这个字段，比 trigger.kind 在两条路径上都可用）。
export function isHandoffPath(path) {
  return typeof path === "string" && path.startsWith("handoff_");
}

// 跟 .gitignore / docs §不变量同口径：仅 task_write_artifact 派生 artifact 路径。
// 后续若加新写盘工具按 tool 名扩；不挂 ArtifactDetector。
//
// name 校验镜像 ``tasks_store._validate_artifact_name``（``/`` / ``\`` / ``..`` /
// 前缀 ``.``）—— 非法名会被写盘层拒、不会落盘，前端记一条"幽灵 artifact"会误导
// 用户。后端 ``transport_bridge._maybe_record_artifact_path`` 同口径过滤。
export function deriveArtifactPath(toolName, args, taskId) {
  if (toolName !== TASK_WRITE_ARTIFACT) return null;
  if (!taskId) return null;
  if (!args || typeof args !== "object") return null;
  const name = args.name;
  if (typeof name !== "string" || !name) return null;
  if (name.includes("/") || name.includes("\\") || name.includes("..")) return null;
  if (name.startsWith(".")) return null;
  return `tasks/${taskId}/artifacts/${name}`;
}

export function formatTs(ms) {
  if (typeof ms !== "number" || !Number.isFinite(ms)) return "";
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function formatScore(score) {
  if (typeof score !== "number" || !Number.isFinite(score)) return "—";
  return score.toFixed(2);
}

export function makeBadge(text, className = "") {
  const el = document.createElement("span");
  el.className = className;
  el.textContent = text;
  return el;
}

export function makePre(text) {
  const pre = document.createElement("pre");
  pre.className = "debug-pre";
  pre.textContent = text;
  return pre;
}

// 空 winners + n_messages=0 = "全员低分的空 turn"，给个明显的视觉提示，避免与
// "winner 数据丢失" 混淆。
export function formatHistoryWinners(entry) {
  const winners = Array.isArray(entry.winners) ? entry.winners : [];
  if (winners.length > 0) return winners.join(", ");
  if (entry.n_messages === 0) return "（无人接话）";
  return "—";
}

export function makeDisabledPromptHint() {
  const el = document.createElement("div");
  el.className = "debug-pre-disabled";
  el.textContent = "prompt 捕获已关（room.toml `[debug] capture_prompts = true` 重启后可见）";
  return el;
}

// "label + body" 是 candidate / message 渲染里出现 5 次的模板：一个小标签 + 一个
// pre/disabled 体块。集中在这里，渲染端调一行。
export function appendLabeledPre(parent, label, text) {
  const lab = document.createElement("div");
  lab.className = "debug-pre-label";
  lab.textContent = label;
  parent.appendChild(lab);
  parent.appendChild(makePre(text));
}
export function appendLabeledDisabled(parent, label) {
  const lab = document.createElement("div");
  lab.className = "debug-pre-label";
  lab.textContent = label;
  parent.appendChild(lab);
  parent.appendChild(makeDisabledPromptHint());
}

// MCP 源识别 —— 与后端 debug_recorder._classify_tool_source 同算法（前端复刻，
// 避免再开个 envelope 字段透传）。
export function classifyToolSource(tool) {
  if (typeof tool !== "string" || !tool) return { source: "unknown", mcp_server: null };
  if (tool.startsWith("mcp__")) {
    const rest = tool.slice("mcp__".length);
    const idx = rest.indexOf("__");
    const server = idx > 0 ? rest.slice(0, idx) : rest;
    return { source: "mcp", mcp_server: server || null };
  }
  return { source: "builtin", mcp_server: null };
}
