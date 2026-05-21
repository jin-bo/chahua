"use strict";

// 房间详细设置 modal（P4.6，对应 docs/P4-专业茶客配置闭环.md §5.2）。
//
// **scope**：表单只暴露 P4 新接的"声明性"字段 —— 房间默认 LLM（P4.9）/ 编排参数 /
// [scoring] / [summary]。name / topic / rules / user_md 改动走 raw textarea
// （"打开 raw room.toml"按钮），避免在 JS 里搓 TOML 合成。
//
// **diff 提交**：每段一条 inbound。编排走 update_room_orchestrator（整体覆盖语义，
// 与 admin.update_room_orchestrator 一致 —— 空 dict = 清掉所有 override）；
// room.llm / scoring / summary 走 update_room_llm（section 字段分流，spec=null =
// 清整段）。

import { Inbound } from "./events.js";
import { $, createLlmSection } from "./llm_section_form.js";

// 与 chahua/config.py::ORCH_FIELD_BOUNDS 同口径 —— 客户端拦掉越界 / 类型错可以在
// admin 层报错前给用户红字提示，避免来回 echo 等错。
const ORCH_BOUNDS = Object.freeze({
  want_threshold: { kind: "float", min: 0, max: 1, default: 0.45 },
  max_consecutive_ai_turns: { kind: "int", min: 1, default: 20 },
  speaker_cooldown_turns: { kind: "int", min: 0, default: 1 },
  onboarding_threshold: { kind: "int", min: 1, default: 20 },
});

const ORCH_INPUT_IDS = Object.freeze({
  want_threshold: "edit-room-orch-want-threshold",
  max_consecutive_ai_turns: "edit-room-orch-max-turns",
  speaker_cooldown_turns: "edit-room-orch-cooldown",
  onboarding_threshold: "edit-room-orch-onboarding",
});

// 房间默认 LLM —— customSourceTag = "room" 表示 toml 里有 [room.llm]；server 端
// envelope source = "default" 时表示走 env 推断。
const roomDefaultSection = createLlmSection({
  idPrefix: "edit-room-default",
  customSourceTag: "room",
  label: "[room.llm]",
});

const scoringSection = createLlmSection({
  idPrefix: "edit-room-scoring",
  customSourceTag: "room",
  label: "[scoring]",
});

const summarySection = createLlmSection({
  idPrefix: "edit-room-summary",
  customSourceTag: "room",
  label: "[summary]",
});

function fillOrchestrator(orchEffective, overridesKeys) {
  // overrides_keys 告诉前端"用户实际填过的键"；未填的输入框留空，placeholder 显默认值。
  const overrides = new Set(overridesKeys || []);
  for (const key of Object.keys(ORCH_BOUNDS)) {
    const input = $(ORCH_INPUT_IDS[key]);
    input.value = overrides.has(key) && orchEffective?.[key] != null
      ? String(orchEffective[key])
      : "";
  }
}

function readOrchestrator() {
  const out = {};
  for (const [key, bounds] of Object.entries(ORCH_BOUNDS)) {
    const raw = $(ORCH_INPUT_IDS[key]).value.trim();
    if (!raw) continue;  // 空 = 走默认 / 清掉 override
    const value = bounds.kind === "int" ? parseInt(raw, 10) : parseFloat(raw);
    if (Number.isNaN(value)) return { error: `${key} 必须是${bounds.kind === "int" ? "整数" : "数值"}` };
    if (value < bounds.min) return { error: `${key} 不能小于 ${bounds.min}` };
    if (bounds.max != null && value > bounds.max) {
      return { error: `${key} 不能大于 ${bounds.max}` };
    }
    out[key] = value;
  }
  return { overrides: out };
}

function renderGuestsList(guests, onEdit) {
  const ul = $("edit-room-guests-list");
  ul.replaceChildren();
  for (const g of guests || []) {
    const li = document.createElement("li");
    li.className = "modal-room-guest-row";
    const name = document.createElement("span");
    name.className = "modal-room-guest-name";
    name.textContent = g.name;
    const meta = document.createElement("span");
    meta.className = "modal-room-guest-meta";
    meta.textContent = `${g.permission || "read-only"} · ${g.isolation || "room"}`;
    const settingsBtn = document.createElement("button");
    settingsBtn.type = "button";
    settingsBtn.className = "modal-secondary";
    settingsBtn.textContent = "设置";
    settingsBtn.addEventListener("click", () => onEdit(g));
    li.append(name, meta, settingsBtn);
    ul.appendChild(li);
  }
}

// effective = 默认+override；旧 overrides 取 keys 子集 → 差集即用户改动。
function orchestratorChanged(newOverrides, oldEffective, oldKeys) {
  const oldOverrides = {};
  for (const k of oldKeys || []) {
    if (oldEffective?.[k] != null) oldOverrides[k] = oldEffective[k];
  }
  return JSON.stringify(newOverrides) !== JSON.stringify(oldOverrides);
}

export function createRoomSettings({ isConnected, send, setStatus, openGuestSettings, openRawRoomToml }) {
  const modal = $("edit-room-modal");
  let snapshot = null;

  roomDefaultSection.bindModeChange();
  scoringSection.bindModeChange();
  summarySection.bindModeChange();

  $("edit-room-open-raw").addEventListener("click", () => {
    modal.hidden = true;
    openRawRoomToml();
  });

  $("edit-room-submit").addEventListener("click", () => {
    if (!isConnected() || !snapshot) return;
    const orch = readOrchestrator();
    if (orch.error) {
      window.alert(orch.error);
      return;
    }
    const roomDefault = roomDefaultSection.read();
    if (roomDefault.error) {
      window.alert(roomDefault.error);
      return;
    }
    const scoring = scoringSection.read();
    if (scoring.error) {
      window.alert(scoring.error);
      return;
    }
    const summary = summarySection.read();
    if (summary.error) {
      window.alert(summary.error);
      return;
    }

    const payloads = [];
    // 房间默认 LLM 先发 —— 切到 custom 时 scoring/summary 的 "继承默认" 应该指向
    // 新的房间默认值；先发让 server 重装 session 后再 emit room_info / 跑后续 diff
    // 时已经能看到新默认。但 server 一次只能跑一个 session 重装，这里只控制顺序，
    // 真正的"串行投递"靠 ws 单连接 inbound 顺序保证。
    if (roomDefaultSection.changed(roomDefault.spec, snapshot.room_default_llm)) {
      payloads.push({ type: Inbound.UPDATE_ROOM_LLM, section: "room", spec: roomDefault.spec });
    }
    if (orchestratorChanged(orch.overrides, snapshot.orchestrator, snapshot.orchestrator_overrides_keys)) {
      payloads.push({ type: Inbound.UPDATE_ROOM_ORCHESTRATOR, overrides: orch.overrides });
    }
    if (scoringSection.changed(scoring.spec, snapshot.scoring_llm)) {
      payloads.push({ type: Inbound.UPDATE_ROOM_LLM, section: "scoring", spec: scoring.spec });
    }
    if (summarySection.changed(summary.spec, snapshot.summary_llm)) {
      payloads.push({ type: Inbound.UPDATE_ROOM_LLM, section: "summary", spec: summary.spec });
    }
    for (const p of payloads) send(p);
    setStatus("", payloads.length === 0 ? "没有改动" : `保存房间设置（${payloads.length} 项）…`);
    modal.hidden = true;
  });

  return {
    setSnapshot(snap) {
      snapshot = snap;
    },
    open() {
      if (!isConnected() || !snapshot) return;
      $("edit-room-title").textContent = `房间「${snapshot.room_name || ""}」`;
      $("edit-room-meta-hint").textContent =
        `话题：${snapshot.topic || "（无）"}  ·  规则与 name 改动请走"打开 raw room.toml"`;

      fillOrchestrator(snapshot.orchestrator, snapshot.orchestrator_overrides_keys);

      // 房间默认 LLM 自己的 "default" 模式 = 走 env 推断；label 顺手把 env 当前
      // 解析到的 model 露出来，便于用户判断 env 是否就绪。
      const envLabel = snapshot.room_default_llm?.model
        ? `按环境变量推断（当前：${snapshot.room_default_llm.model}）`
        : "按环境变量推断（LLM_PROVIDER / <PREFIX>_MODEL）";
      roomDefaultSection.fill(snapshot.room_default_llm, envLabel);

      const roomDefaultLabel = snapshot.room_default_llm?.model
        ? `跟房间默认走（${snapshot.room_default_llm.model}）`
        : "跟房间默认走";
      scoringSection.fill(snapshot.scoring_llm, roomDefaultLabel);
      const scoringEffectiveLabel = snapshot.scoring_llm?.model
        ? `复用打分模型（${snapshot.scoring_llm.model}）`
        : "复用打分模型";
      summarySection.fill(snapshot.summary_llm, scoringEffectiveLabel);

      renderGuestsList(snapshot.guests, (g) => {
        modal.hidden = true;
        openGuestSettings(g);
      });
      modal.hidden = false;
    },
  };
}
