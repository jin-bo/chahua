"use strict";

// LLM section 表单工具 —— 模型 / base_url / api_key_env 三件套 + "继承默认 vs 自定义"
// radio + 提交时 diff。guest_settings.js（茶客 LLM）与 room_settings.js（[scoring] /
// [summary]）共用同一形态。
//
// 约定的 DOM id：``${idPrefix}-mode`` (radio name) / ``${idPrefix}-fields`` (custom 字段
// 块容器) / ``${idPrefix}-model`` / ``${idPrefix}-base-url`` / ``${idPrefix}-api-key-env``
// / ``${idPrefix}-default-label`` / ``${idPrefix}-key-status``。两个 modal 的 HTML 都按
// 这个 prefix 写就能复用。

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
          }
        : null;
      return JSON.stringify(newSpec) !== JSON.stringify(old);
    },
  };
}
