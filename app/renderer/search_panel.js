"use strict";

// P18 当前房历史搜索抽屉 —— 与 task / debug 面板互斥占右侧槽位。
//
// 输入回车发 search_room {query}（只读、当前房、子串）；SEARCH_RESULTS 回来经
// onResults 渲染命中列表，点行调 jumpToMessage(message_id) 滚动 + 高亮主聊天区；
// 命不中（消息被切房 / clear 移出 DOM）闪一条提示。切房 / clear 时 renderer 调 reset。
//
// 与 debug_panel.js 的历史索引列表同构（列表行 → 点击 → 定位），但更简单：命中消息
// 已在主 DOM，点击直接滚动高亮，不必二次请求后端。

import { Inbound, USER_SPEAKER_ID } from "./events.js";
import { formatTs } from "./debug_helpers.js";

export function createSearchPanel({
  panelEl,
  inputEl,
  bodyEl,
  closeBtnEl,
  send,
  jumpToMessage,
  isConnected,
  onClose,
}) {
  function runSearch() {
    if (!isConnected()) return;
    const query = inputEl.value.trim();
    if (!query) {
      renderEmpty("输入关键词后回车搜索。");
      return;
    }
    renderEmpty("搜索中…");
    send({ type: Inbound.SEARCH_ROOM, query });
  }

  inputEl.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      runSearch();
    }
  });
  if (closeBtnEl && onClose) closeBtnEl.addEventListener("click", onClose);

  function renderEmpty(text) {
    bodyEl.replaceChildren();
    const p = document.createElement("div");
    p.className = "search-empty";
    p.textContent = text;
    bodyEl.appendChild(p);
  }

  function flashNote(text) {
    const note = document.createElement("div");
    note.className = "search-note";
    note.textContent = text;
    bodyEl.insertBefore(note, bodyEl.firstChild);
    setTimeout(() => note.remove(), 2000);
  }

  function onResults(data) {
    const results = Array.isArray(data.results) ? data.results : [];
    const query = data.query || "";
    if (results.length === 0) {
      renderEmpty(`没有找到「${query}」。`);
      return;
    }
    bodyEl.replaceChildren();
    if (data.truncated) {
      const note = document.createElement("div");
      note.className = "search-truncated";
      note.textContent = `命中较多，仅显示前 ${results.length} 条。`;
      bodyEl.appendChild(note);
    }
    const ul = document.createElement("ul");
    ul.className = "search-results";
    for (const hit of results) ul.appendChild(makeResultRow(hit));
    bodyEl.appendChild(ul);
  }

  function makeResultRow(hit) {
    const li = document.createElement("li");
    li.className = "search-result-row";
    li.dataset.messageId = hit.message_id || "";
    const meta = document.createElement("div");
    meta.className = "search-result-meta";
    const speaker = document.createElement("span");
    speaker.className = "search-result-speaker";
    speaker.textContent =
      hit.speaker_id === USER_SPEAKER_ID ? "我" : hit.speaker_id || "";
    const ts = document.createElement("span");
    ts.className = "search-result-ts";
    ts.textContent = typeof hit.ts_ms === "number" ? formatTs(hit.ts_ms) : "";
    meta.appendChild(speaker);
    meta.appendChild(ts);
    const snippet = document.createElement("div");
    snippet.className = "search-result-snippet";
    snippet.textContent = hit.snippet || "";
    li.appendChild(meta);
    li.appendChild(snippet);
    li.addEventListener("click", () => {
      // 命不中原因可能是：被任务筛选隐藏（display:none）、刚发送的本地 echo 气泡还没
      // message_id、已切房 / 清空。文案保持中性、不误导成「已切房」。
      if (!jumpToMessage(hit.message_id)) {
        flashNote("未能跳转到该消息（可能被任务筛选隐藏，或它是刚发送、已清空的消息）。");
      }
    });
    return li;
  }

  function show() {
    panelEl.hidden = false;
    inputEl.focus();
  }
  function hide() {
    panelEl.hidden = true;
  }
  function isVisible() {
    return !panelEl.hidden;
  }
  // 切房 / clear：清输入 + 结果（旧房命中的 message_id 已不在新房 DOM）。
  function reset() {
    inputEl.value = "";
    renderEmpty("输入关键词后回车搜索。");
  }

  reset();

  return { onResults, show, hide, isVisible, reset };
}
