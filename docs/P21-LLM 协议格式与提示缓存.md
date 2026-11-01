# P21 LLM 协议格式与提示缓存

> 状态：已实现（分支 `feat/p21-api-format-prompt-cache`），验收记录见 §7
> 起因：2026-10-31 用 `~/.env` 真凭证验 P20 缓存读数时发现（数字见 §1）。

## 1. 问题

chahua 的 `build_client` 只给 agentao `LLMClient` 传 `api_key / base_url / model / temperature`。agentao 0.5.18 已支持的两个端点级开关传不进去：

- **`api_format`**（`openai-completions` 默认 / `anthropic-messages` / `openai-responses`）。`~/.env` 里写的 `OPENAI_API_FORMAT=openai-responses`、`CLAUDE_API_FORMAT=anthropic-messages` 全被忽略，一律走 Chat Completions：
  - `gpt-6-astra` 带工具只能走 `/v1/responses`，chahua 里**每次发言 400**；
  - Anthropic 退到其 OpenAI 兼容层。
- **`prompt_cache`**（`"anthropic"` 或关）。agentao 对 Anthropic 的缓存断点是 opt-in；不开则每轮全价。

实测（宝总两轮，P20 `llm_calls`）：

| 配置 | 第 2 轮输入 | 其中缓存读 |
|---|---|---|
| OpenAI 现状 | 400 | — |
| OpenAI + `openai-responses` | 7,731 | 7,656 |
| Anthropic 现状 / 仅 `anthropic-messages` | 13,353 | 0 |
| Anthropic + `anthropic-messages` + `prompt_cache=anthropic` | 13,353 | 13,257 |

附带问题：上面那次 400，`speak` 得到的是 agentao 的 `[LLM API error: …]` 通知文本，**按正常发言**（`status=ok`）进了 transcript。agentao 的 `agent.last_turn.incomplete_reason == "llm_error"` 才是判据，chahua 没看。

## 2. 目标 / 非目标

目标：

1. `LLMSpec` 加 `api_format` / `prompt_cache` 两个可选字段，env / toml / 管理端写回 / 设置界面 / 桌面注入全链路贯通，`build_client` 传给 `LLMClient`。
2. 上游调用失败（`llm_error`）走 `message_end status=error`，不进 transcript。

非目标：

- **缓存 TTL**（`5m` / `1h`）不做，走上游默认 5m。
- 不按 base_url / model 名猜协议或自动开缓存（agentao 明确拒绝推断，chahua 同口径）。
- `llm_error` 之外的 `incomplete_reason`（`no_output` / `max_iterations` / `length_truncated` / …）行为不变——它们带的是模型自己的部分输出或 agentao 的占位文本，改不改另议。
- agentao 把 `[LLM API error: …]` 追加进 `agent.messages` 的行为不动（上游设计，下一轮模型能看到上一轮失败）。

## 3. 配置语义

| 字段 | toml（`[room.llm]` / `[scoring]` / `[summary]` / `[[guest]]`） | env（仅 env 推断的房间默认） |
|---|---|---|
| `api_format` | `api_format = "anthropic-messages"` | `<PREFIX>_API_FORMAT` |
| `prompt_cache` | `prompt_cache = "anthropic"` | `<PREFIX>_PROMPT_CACHE` |

- **两者都是端点属性，按 section 走，不做跨 section / env 回退**。toml 段没写 → `None` → agentao 默认（Chat Completions / 不打缓存断点）。与 `temperature`（toml 缺则读 `LLM_TEMPERATURE`）**有意不同**：缓存标记在 Chat Completions 上也会发出（agentao `_openai_completions.py`），某网关不认就 400；把 A 端点的开关漂到 B 端点上是错配。
- **env 用 `<PREFIX>_PROMPT_CACHE` 而不是 agentao CLI 的全局 `LLM_PROMPT_CACHE`**：同理，全局开关在桌面注入切换 provider 后会残留到新端点。`<PREFIX>_API_FORMAT` 与 agentao 同名。
- 取值校验：`api_format ∈ {openai-completions, anthropic-messages, openai-responses}`，`prompt_cache ∈ {anthropic, off}`（大小写 / 空白归一；`off` 与空 = `None`）。toml 非法 → `RoomConfigError`；env 非法 → `SystemExit`（与 `LLM_TEMPERATURE` 同口径，早炸）。chahua 持有自己的白名单，契约用例断言与 agentao 内部常量一致（升级漂移即红）。
- toml all-or-nothing 不变：两字段同 `base_url` 一样不能脱离 `model` 单独出现。

## 4. 各处改动

- `llm_spec.py`：`LLMSpec` 加字段；`LLM_TOML_FIELDS` 加两键；`try_from_env` / `from_toml` 解析 + 校验；`build_client` 透传。spec 仍 frozen + hashable（`session.py` 按 spec 复用 client，字段不同自然分开）。
- `config.py`：`_ALLOWED_GUEST_KEYS` 的 LLM 部分改为派生自 `LLM_TOML_FIELDS`（防再漂）。
- `admin_toml.py`：`_llm_spec_to_dict` 写回两字段（`None` 不写）。
- `server_room_snapshot._llm_summary`：`room_info` 下发 `api_format` / `prompt_cache`（原值，`None` = 默认）。不 bump `schema_version`。
- 前端 `llm_section_form.js` + `index.html` 四个表单：加「协议」下拉（默认 / Anthropic Messages / OpenAI Responses）与「Anthropic 提示缓存」勾选；`fill` / `read` / `changed` 三处同步——**漏 `changed` 会让「没改其它字段」判成未改，漏 `read` 会让一次无关保存把 toml 里的两字段静默抹掉**。
- `set_llm_credentials`（P15）：白名单加可选 `api_format` / `prompt_cache`；与 `base_url` 同语义——给了就写 `<PREFIX>_API_FORMAT` / `<PREFIX>_PROMPT_CACHE`，缺则 pop（注入的凭证自洽，不继承 `.env` 里同前缀的残留）；env 快照 / 回滚覆盖这两个键；非法值 NOTICE error 丢帧。
- `guest.py`：`arun` 返回后读 `agent.last_turn`，`incomplete_reason == "llm_error"` → `_emit_failure(STATUS_ERROR, text)`、不 `room.append`、返回 `None`（与异常分支同）。读 `last_turn` 失败按旧行为（当正常回复）。

## 5. 不变量

- **`api_format` / `prompt_cache` 是端点属性：按 section 生效，toml 段缺即默认，不回退 env、不跨 section 继承**；env 只对 env 推断的房间默认生效且按 `<PREFIX>` 分。
- **LLM 字段集单点 `LLM_TOML_FIELDS`**：toml 白名单、guest 顶层白名单、写回、表单都从它或与它同步；新增字段必须走完「解析 → 写回 → room_info → 表单 fill/read/changed」闭环。
- **上游调用失败不是发言**：`last_turn.incomplete_reason == "llm_error"` 的回合走 error `message_end`，不进 transcript / 不触发后续打分。

## 6. 测试

- `LLMSpec`：env 两键解析 / 归一 / 非法 SystemExit / 前缀隔离；toml 两键解析 / 非法 / 脱离 model 报错；`build_client` 透传到 `LLMClient`（读 `client.api_format` / `client.prompt_cache`）。
- 白名单与 agentao 内部常量一致（契约）。
- `admin_toml` 往返；`room_info` 带字段；guest 白名单接受。
- `set_llm_credentials`：带 / 不带两字段时 env 写入与 pop、非法值拒绝、失败回滚。
- `speak`：`last_turn` 为 `llm_error` → `message_end status=error`、transcript 不变、返回 None；真 agentao + 假端点恒 400 用例。
- 前端：表单 read/changed 纯逻辑若可抽出则 node 覆盖，否则桌面壳手测。
- 真凭证：`~/.env` 两家各两轮，复现 §1 表格的「修后」两行；桌面壳设置界面改协议 / 缓存后 toml 正确、抽屉显示缓存读。

## 7. 实现与验收记录（2026-10-31）

- 测试：新增 `tests/test_p21_api_format_prompt_cache.py`（env / toml / 透传 / 白名单对齐 agentao / 写回往返 / room_info / guest 白名单 / `llm_error` 判失败与其它 outcome 不变）、`test_set_llm_credentials.py` 4 条、真 agentao 恒 400 契约 1 条。既有 `test_llm_spec` 的错误文案子串保留。全量 1628 → 1665 绿。
- 真凭证（`~/.env`，仓库代码，无补丁；`CLAUDE_PROMPT_CACHE=anthropic` 仅设在测试进程）：

| 配置 | 第 1 轮输入（写缓存） | 第 2 轮输入（缓存读） |
|---|---|---|
| OpenAI `gpt-6-astra` + `openai-responses` | 7,659（7,656） | 7,731（7,656） |
| Anthropic `claude-sonnet-5` + `anthropic-messages` + 缓存 | 13,259（13,257） | 13,347（13,257） |
| 同上不开缓存 | — | 13,356（0） |

- 桌面壳（隔离 `CHAHUA_USER_DATA`）：房间设置 → 房间默认模型改 custom + Anthropic Messages + 勾缓存 → 保存，`room.toml` 写出 `api_format` / `prompt_cache`，重开表单正确回填；只取消缓存勾选再保存 → 判为改动、`prompt_cache` 从 toml 删除、协议保留；零控制台错误。
- 未验：`set_llm_credentials` 经真实桌面登录流程带两字段（host 侧目前不发，单测覆盖 server 行为）；Windows。
