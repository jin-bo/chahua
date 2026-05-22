"use strict";

// 斜杠命令解析。从 renderer.js 的 composer submit handler 抽出（renderer 重构）。
//
// tryHandle(text) 命中某条命令即执行并返回 true（调用方据此 return、不再走普通发送
// 路径）；非命令返回 false。所有命令都是本地 UI 行为或单条 inbound，不进 transcript，
// 刷新 / 切房后消失。调用前 composer submit handler 已确认 connected。

import { Inbound } from "./events.js";

// ``/help`` 输出文案 —— 用 markdown 渲，居中系统气泡承载。**Web 与 CLI 两面 help 各
// 自维护本面支持的命令**：CLI 端的 ``_HELP_LINES`` 含 ``/info`` / ``/quit``（REPL 才
// 有意义），Web 端含 ``/task``（CLI 走 ``@<名字>`` 路径不开任务）。加 / 改命令时
// 改本面的那份即可，不必试图 cross-runtime 同步。
const HELP_TEXT = [
  "**chahua 系统命令**",
  "",
  "- `/help` 或 `/?` —— 显示本帮助",
  "- `/task <标题>` —— 新建任务（已有 active 时服务端会拒）",
  "- `/clear` 或 `/new` —— 清空整间房间聊天（重置 transcript / 摘要 / 茶客会话窗口）",
  "- `/clear task` —— 清空当前任务的全部产物（仅删 `artifacts/`，任务本身保留）",
  "- `/tools <茶客名>` —— 查看某位茶客 agent 注册的工具",
  "- `/skills <茶客名>` —— 查看某位茶客可用的 skills",
  "",
  "_本帮助是本地提示，不进 transcript；刷新 / 切房后消失。_",
].join("\n");

export function createCommands({
  isConnected,
  send,
  setStatus,
  appendBubble,
  taskState,
  upload,
  clearRoom,
  clearInput,
}) {
  function tryHandle(text) {
    // /help（或 /?）—— 在聊天区里以系统气泡显示可用斜杠命令清单。本地 UI 行为，不
    // 走 server 帧、不进 transcript；刷新 / 切房 / clear 都会消失。
    if (text === "/help" || text === "/?") {
      appendBubble({ kind: "system", text: HELP_TEXT });
      clearInput();
      return true;
    }
    // /tools <茶客名> / /skills <茶客名> —— 查某位茶客 agent 的 tools / 可用 skills。
    // 两者走同一只读 inbound list_guest_caps；view 随帧发出、回包原样带回，前端按它
    // 裁剪显示哪段（不靠全局态，多查询并发不串台）。本地查询，不进 transcript。
    const isTools = text === "/tools" || text.startsWith("/tools ");
    const isSkills = text === "/skills" || text.startsWith("/skills ");
    if (isTools || isSkills) {
      const cmd = isTools ? "/tools" : "/skills";
      const guest = text.slice(cmd.length).trim();
      if (!guest) {
        setStatus("error", `${cmd} 后面要跟茶客名`);
        return true;
      }
      if (!isConnected()) {
        setStatus("error", "未连接服务端");
        return true;
      }
      send({
        type: Inbound.LIST_GUEST_CAPS,
        guest,
        view: isTools ? "tools" : "skills",
      });
      clearInput();
      return true;
    }
    // 斜杠命令 —— /task <title> 走 open_task inbound 而非 user_message。已有任务等
    // server 端拒绝（NOTICE error），前端不重复判定，让两条路径决断口径完全同源。
    if (text.startsWith("/task ") || text === "/task") {
      const title = text.slice("/task".length).trim();
      if (!title) {
        setStatus("error", "/task 后面要跟任务标题");
        return true;
      }
      if (upload.hasPending()) {
        setStatus("error", "新建任务前先 × 掉待发文件 —— 任务不接附件");
        return true;
      }
      send({ type: Inbound.OPEN_TASK, title, goal: "", owner: null });
      setStatus("", `新建任务「${title}」…`);
      clearInput();
      return true;
    }
    // /clear task：清空当前 active 任务的全部产物（仅删 artifacts/，任务本身保留）。
    // 必须排在 /clear / /new 之前 —— 否则 startsWith 边界让 "/clear task" 被前缀吞掉。
    if (text === "/clear task" || text === "/clear-task") {
      const active = taskState.getActiveTask();
      if (!active) {
        setStatus("error", "/clear task 需要先选中一个任务");
        return true;
      }
      const count = Array.isArray(active.artifacts) ? active.artifacts.length : 0;
      if (count === 0) {
        setStatus("", `任务「${active.title}」没有产物`);
        clearInput();
        return true;
      }
      if (!window.confirm(
        `确定清空任务「${active.title}」的全部产物（${count} 个文件）？\n任务本身（决策 / 状态 / 摘要）保留，仅删 artifacts/ 下的文件。`,
      )) return true;
      send({ type: Inbound.CLEAR_TASK_ARTIFACTS, task_id: active.id });
      setStatus("", `清空任务「${active.title}」的产物…`);
      clearInput();
      return true;
    }
    // /clear 与 /new：键盘快捷的"清空聊天"，与房间菜单同入口共用 confirm —— 滑指误打
    // /task 边界（如想 /task 打成 /clear）有 confirm 兜底；clearRoom 内部不连接
    // 时 noop，所以此处不再判 connected。
    if (text === "/clear" || text === "/new") {
      clearInput();
      clearRoom();
      return true;
    }
    return false;
  }

  return { tryHandle };
}
