"use strict";

// LLM section 表单工具 —— 模型 / base_url / api_key_env / temperature 四件套
// + "继承默认 vs 自定义" radio + 提交时 diff。guest_settings.js（茶客 LLM）与
// room_settings.js（[scoring] / [summary]）共用同一形态。
//
// 约定的 DOM id：``${idPrefix}-mode`` (radio name) / ``${idPrefix}-fields`` (custom 字段
// 块容器) / ``${idPrefix}-model`` / ``${idPrefix}-base-url`` / ``${idPrefix}-api-key-env``
// / ``${idPrefix}-temperature`` / ``${idPrefix}-default-label`` / ``${idPrefix}-key-status``。
// 两个 modal 的 HTML 都按这个 prefix 写就能复用。

// 与 chahua.llm_spec._TEMPERATURE_MIN/MAX 同口径 —— 客户端拦掉越界提前给用户红字提示，
// 避免来回 echo 等服务端 RoomConfigError。
const TEMPERATURE_MIN = 0;
const TEMPERATURE_MAX = 2;

export function $(id) { return document.getElementById(id); }

export function setRadio(name, value) {
  for (const inp of document.querySelectorAll(`input[name="${name}"]`)) {
    inp.checked = inp.value === value;
  }
}

export function getRadio(name) {
  const checked = document.querySelector(`input[name="${name}"]:checked`);
  return checked ? checked.value : null;
}

// `customSourceTag` 是 envelope ``llm.source`` 的字面：guest 段 = ``"guest"``、
// 房间 scoring/summary 段 = ``"room"``。fill 时与该 tag 相等才认为"用户在 toml 里写过"，
// 决定 form 是 default 还是 custom；diff 时同理。
export function createLlmSection({ idPrefix, customSourceTag, label }) {
  const radioName = `${idPrefix}-mode`;
  function refresh() {
    $(`${idPrefix}-fields`).hidden = getRadio(radioName) !== "custom";
  }
  return {
    bindModeChange() {
      for (const inp of document.querySelectorAll(`input[name="${radioName}"]`)) {
        inp.addEventListener("change", refresh);
      }
    },
    fill(spec, defaultLabel) {
      $(`${idPrefix}-default-label`).textContent = defaultLabel;
      const isCustom = spec?.source === customSourceTag;
      setRadio(radioName, isCustom ? "custom" : "default");
      // default 时清空 input —— 切到 custom 时让用户从空白起编辑，避免"已显示 X /
      // 我没改也是 X？"的歧义。
      $(`${idPrefix}-model`).value = isCustom ? (spec?.model || "") : "";
      $(`${idPrefix}-base-url`).value = isCustom ? (spec?.base_url || "") : "";
      $(`${idPrefix}-api-key-env`).value = isCustom ? (spec?.api_key_env || "") : "";
      // temperature: spec.temperature 是 number 或 null；null 表示本段没显式写温度
      // → custom 模式留空让用户自填（提交时不带这个键，spec.temperature 仍 None）。
      const tempInput = $(`${idPrefix}-temperature`);
      if (tempInput) {
        tempInput.value =
          isCustom && spec?.temperature != null ? String(spec.temperature) : "";
      }
      refresh();
      const ready = !!spec?.api_key_ready;
      $(`${idPrefix}-key-status`).textContent =
        `${spec?.api_key_env || "<PROVIDER>_API_KEY"}：${ready ? "✓ 已配置" : "✗ 未配置"}`;
    },
    read() {
      if (getRadio(radioName) !== "custom") return { spec: null };
      const model = $(`${idPrefix}-model`).value.trim();
      if (!model) return { error: `${label} model 必填（形如 openai/gpt-5.4）` };
      const spec = { model };
      const baseUrl = $(`${idPrefix}-base-url`).value.trim();
      if (baseUrl) spec.base_url = baseUrl;
      const apiKeyEnv = $(`${idPrefix}-api-key-env`).value.trim();
      if (apiKeyEnv) spec.api_key_env = apiKeyEnv;
      const tempInput = $(`${idPrefix}-temperature`);
      if (tempInput) {
        const tempRaw = tempInput.value.trim();
        if (tempRaw) {
          const temperature = Number(tempRaw);
          if (!Number.isFinite(temperature)) {
            return { error: `${label} temperature 必须是数值（如 0.7）` };
          }
          if (temperature < TEMPERATURE_MIN || temperature > TEMPERATURE_MAX) {
            return {
              error: `${label} temperature=${tempRaw} 越界，要求 [${TEMPERATURE_MIN}, ${TEMPERATURE_MAX}]`,
            };
          }
          spec.temperature = temperature;
        }
      }
      return { spec };
    },
    // "default" 永远 ≠ "custom"，即便 model 字面相同 —— 用户显式 pin 住不随房间默认漂移。
    changed(newSpec, oldSpec) {
      const wasCustom = oldSpec?.source === customSourceTag;
      const old = wasCustom
        ? {
            model: oldSpec.model,
            ...(oldSpec.base_url ? { base_url: oldSpec.base_url } : {}),
            ...(oldSpec.api_key_env ? { api_key_env: oldSpec.api_key_env } : {}),
            ...(oldSpec.temperature != null ? { temperature: oldSpec.temperature } : {}),
          }
        : null;
      return JSON.stringify(newSpec) !== JSON.stringify(old);
    },
  };
}
