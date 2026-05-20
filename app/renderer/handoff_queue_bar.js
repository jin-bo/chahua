"use strict";

// composer 上方的 handoff 队列预览小条（P7.1.10 / docs/P7 §5.3）。
//
// 订阅 handoff_state：队列非空 → 浮现「➡️ 下一句：A → B」+ 右侧「全部取消」✕；
// 空队列 → 整条 hidden 不占位。队列项的 reason 是内部备注 → hover 出 title 提示，
// 不直接铺在条上（与"备注不发给茶客"语义对齐，条上只放调度信息）。

import { Inbound } from "./events.js";

// 队列项 kind → 预览后缀。镜像 chahua/handoff.py::HandoffKind 取值；未知 kind
// （老前端遇新枚举）落空串、只显茶客名，不报错。
const KIND_SUFFIX = Object.freeze({
  delegate: "（指派）",
  review: "（请审）",
});

// 队列项 → 预览文案。delegate / review 是单 target；panel 是自描述单项
// （docs/P7.3 §6.2），列 targets + 可选 summarizer，不做相邻分组。
function itemPreviewText(item) {
  if (!item) return "?";
  if (item.kind === "panel") {
    const targets = Array.isArray(item.targets) ? item.targets : [];
    let text = `🔄 圆桌：${targets.join("、") || "?"}`;
    if (item.summarizer) text += ` → 汇总:${item.summarizer}`;
    return text;
  }
  return (item.target || "?") + (KIND_SUFFIX[item.kind] || "");
}

export function createHandoffQueueBar({ barEl, send, isConnected }) {
  function render(queue) {
    barEl.replaceChildren();
    if (!Array.isArray(queue) || queue.length === 0) {
      barEl.hidden = true;
      return;
    }
    barEl.hidden = false;

    const label = document.createElement("span");
    label.className = "handoff-queue-label";
    label.textContent = "➡️ 下一句：";
    barEl.appendChild(label);

    const chain = document.createElement("span");
    chain.className = "handoff-queue-chain";
    queue.forEach((item, i) => {
      if (i > 0) {
        const sep = document.createElement("span");
        sep.className = "handoff-queue-sep";
        sep.textContent = "→";
        chain.appendChild(sep);
      }
      const target = document.createElement("span");
      target.className = "handoff-queue-target";
      target.textContent = itemPreviewText(item);
      // reason 是内部备注：仅 hover title 显示，不进茶客 prompt、不铺在条上。
      // review / panel 项无 reason 字段，自然不挂 title。
      if (item && item.reason) target.title = `理由：${item.reason}`;
      chain.appendChild(target);
    });
    barEl.appendChild(chain);

    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "handoff-queue-clear";
    clear.textContent = "✕";
    clear.title = "全部取消（取消当前发言 + 清空后续队列）";
    clear.addEventListener("click", () => {
      if (!isConnected()) return;
      send({ type: Inbound.HANDOFF_CLEAR });
    });
    barEl.appendChild(clear);
  }

  return { render };
}
