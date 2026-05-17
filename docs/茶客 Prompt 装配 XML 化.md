# 茶话室 LLM Prompt 装配重构：XML 标签化 + 任务块字段补全

## Context

### 这次改造的对象是哪一条 LLM 消息？

茶话室的茶客 LLM 调用是**双消息结构**：

- **System message** = `persona_md`（茶客的人格设定 / mythos）—— 在 `TeaGuest.register()` 时通过 `Agentao(project_instructions=persona_md)` 注入（`chahua/guest.py:148`），由 agentao 内部塞进底层 LLM client 的 system slot。
- **User message #1** = `context_message`（房间设定 + USER.md + 房间摘要 + 任务块 + 最近原文 + 发言指令的拼装结果）—— 由 `Orchestrator._build_context_for()` 拼装（`chahua/orchestrator.py:628`），通过 `agent.arun(context_message, ...)` 作为单条 user message 喂给底层 LLM（`chahua/guest.py:198-201`）。

**本文档只改 user message #1 的拼装格式，system message（persona）完全不动。**

这一事实直接决定了风格选型——XML 标签在 user message 里组织复杂上下文是 Anthropic 官方 prompt engineering 推荐的标准做法（"用 XML tags 让 Claude 区分 prompt 的不同部分"），且不会和 system 层 persona 的写作风格冲突。`<speak_instruction>` 块放在 user message 末尾 = "调用方在每条消息里临时指明要哪个茶客发言"——比改 system 更动态（同一 Agentao 实例对应同一茶客，但 orchestrator 也确实只让该茶客发自己名字的言；保持当前架构不需要改）。

### 现状暴露的三个痛点

茶话室喂给茶客 LLM 的 prompt 当前由 `orchestrator._render_onboarding()` / `_render_incremental()` 把 5+ 个段（房间设定 / USER.md / 房间摘要 / 任务 / 最近原文 / 发言指令）用单个 `\n` 拼接成纯文本。实际跑出来的 user message 暴露三个用户反馈痛点：

1. **结构不清晰**：所有段落只靠"以中文冒号开头的关键词"（"当前话题："/"房间规则："/"当前任务："/"最近原文："）做边界提示，没有强分隔符；LLM 在长 prompt 里容易段落串味。
2. **用户定义边界不清晰**：USER.md 原文里的 H2（`## 身份` / `## 语气偏好` / `## 忌讳`）与 prompt 外层主结构同一视觉层级——LLM 看着像 prompt 主纲。
3. **任务目标不清晰**：full 模式任务块有 title/goal/status/owner/decisions/artifacts/summary_tail，但**漏掉了 `./task/` 工作目录的存在提示**（compact 模式有"产物可从 ./task/ 读取"这句，full 模式没说）。茶客可能不知道能从 `./task/<name>` 读到 artifact 文件。

经与用户确认，采用 **XML 标签包裹外层 + Markdown 渲染内层** + **中等扩展任务块字段**。XML 标签让 USER.md 内部 H2 被自然降级为"段内 H2"，外层结构靠 `<room>` / `<user_persona>` / `<current_task>` 等强分隔；Claude 训练偏好、Qwen/GPT/DeepSeek 等通用模型也都识别良好。

## 设计目标

- 用 XML 标签把 5 段拼装结果切成强边界块；不改字段提取逻辑（USER.md / room / task 数据来源全不动）。
- USER.md 完整原文被 `<user_persona display_name="...">` 包住，内部 H2 不再与外层结构冲突。
- 任务块 full 模式补 `./task/` 工作目录提示，使茶客始终知道这个路径的存在。
- **不改打分 prompt**（`scoring.py` 走独立通道，与"喂茶客发言用的 context_message"无关），不动 `LLMSpec` / `room.toml` / wire protocol。
- 保留所有现有不变量（envelope 配对、speak_instruction 文本、task_id 透传等）。

## 改动范围

### 1. 改文件：`chahua/orchestrator.py`

**仅四个纯渲染函数**——无字段提取层改动、无 wiring 改动。

#### `_render_onboarding()`（行 678-719）→ 整体重写

新输出结构（按 `parts` 追加顺序）：

```
<room name="{self.room.name}">
话题：{self.room.topic}             ← 仅当 topic 非空
规则：{self.room.rules}             ← 仅当 rules 非空
在场：{display1}（人类用户）, {display2}, {display3}, ...
</room>

<user_persona display_name="{self.user_config.display_name}">
以下是该参与者关于自己的说明：

{strip_top_h1(self.user_config.full_md).strip()}
</user_persona>
                                    ← 该块仅当 user_config.has_persona 时存在

<room_summary>
{recent_summary_bullets_joined_by_\n\n}
</room_summary>
                                    ← 该块仅当 self.summarizer.summaries 非空时存在
                                    ← 取末 onboarding_recent_summaries 段；
                                    ← 删掉原"近期梗概：" 这行 label（XML 标签已表达）

<current_task status="{status_display}">
{task_block 的内部部分（status 行去掉，因 XML 属性已带）}
</current_task>
                                    ← 该块仅当 task_block 非空时存在
                                    ← 详见下面 _render_task_block 的改动

<recent_messages count="{len(tail)}">
{format_messages(tail, display_for)}
</recent_messages>
                                    ← 仅当 tail 非空时存在
                                    ← tail = increment[-onboarding_recent_messages:]

<speak_instruction>
请以「{guest_name}」的身份发言。只说要说的内容，不要复述别人、不要加引号或前缀。
</speak_instruction>
```

块与块之间用 **单个空行**（`\n\n`）连接，块内部用 `\n`。"在场"行**将"（含人类参与者）。"** 拆分到具体名字后用 "（人类用户）" 后缀（更明确，告诉茶客哪个是真人），不再用句尾笼统标记。

#### `_render_incremental()`（行 721-740）→ 同步 XML 化

```
<room_update name="{room.name}">
{format_messages(increment)}
</room_update>

<current_task status="{status}">
{compact task body}
</current_task>
                                    ← 仅当 task_block 非空时存在

<speak_instruction>
请以「{guest_name}」的身份发言。...
</speak_instruction>
```

删除原"（房间·{name}·继续）"口语 header；`<room_update>` 标签 + name 属性已表达。

#### `_render_task_block()`（行 818-864）→ 补 `./task/` 提示，去掉"状态："行

**status 移到 XML 标签的 `status="..."` 属性里，块内部不再重复**：

full 模式（`compact=False`）输出：
```
标题：{title}
负责人：{owner}                      ← 仅当 task.owner 非空，单独一行（与 status 解耦）
目标：
{task.goal}                          ← 仅当 task.goal 非空

近期决策（最近 N 条）：
- {summary 1}
- {summary 2}
...                                  ← 同原实现，cap 不变

当前产物（./task/ 工作目录下可读，按需读取，不嵌入内容）：
- {name} ({size}, {mtime})
...                                  ← 同原实现 cap=10
                                     ← 改 label 文案：把 "（不嵌内容，按需走 ./task/ 读取）：" 
                                     ← 改成 "（./task/ 工作目录下可读，按需读取，不嵌入内容）："
                                     ← 强化 ./task/ 的路径感

任务近期进展：
{tail_joined_by_\n\n}                ← 同原实现 cap=3
```

**新增**：当**没有 artifacts** 但 task 已开时，渲染一行 `工作目录 ./task/ 当前为空。` —— 让茶客始终知道这个路径的存在（status 不会因此被屏蔽，因为 status 走 XML 属性了）。

compact 模式（`compact=True`）保留原状（已有 `./task/` 提示）。

**接口签名**：函数仍接收完整 `Task` / `decisions` / `artifacts` / `summary_tail`，但**返回值不再含 status 行**——status 由调用方拼到 XML 属性里。调用方需要 status_display：
- `_render_onboarding()` 自己取 `TASK_STATUS_DISPLAY.get(task.status, task.status)` 拼到 `<current_task status="...">` 属性。
- 这意味着 `_render_task_block()` 需要把"status 数据"额外暴露给调用方——最简单做法是**让函数返回 `(body: str, status_display: str)` 二元组**，调用方拼接时用元组的两个分量。

#### `_speak_instruction()`（行 742-746）→ 微调文案 + 不变

文本保持现状（"请以「{name}」的身份发言。只说你要说的内容，不要复述别人的话，不要加引号或前缀。"）。**XML 包裹由调用方做**——`_speak_instruction()` 只返回内层文案，外层 `<speak_instruction>` 标签在 `_render_*` 函数里拼。

### 2. 不动文件

- `chahua/user_md.py` —— USER.md 加载 / 解析逻辑全不动。
- `chahua/scoring.py` —— 打分 prompt 是独立通道，不在这次改造范围。
- `chahua/guest.py` —— `speak()` 接收 `context_message` 整段字符串作为 user message #1 喂给 `agent.arun()`，对内容格式透明；**system 层的 `persona_md` 完全不动**（茶客人格不变）。
- `chahua/room.py` —— `format_messages()` 不动；transcript 格式保持"老金 说：xxx"。
- `chahua/tasks_store.py` / `chahua/server_inbound_task.py` —— task 数据层不动。
- `app/renderer/*` —— 前端不感知 prompt 内部结构。

### 3. CLAUDE.md 加一条不变量

在「关键不变量」段加一行：

> - **喂茶客的 context_message 用 XML 标签包外层 + Markdown 渲内层**。`<room>` / `<user_persona>` / `<current_task>` / `<recent_messages>` / `<speak_instruction>` 是固定 5 块，外层结构与 USER.md / task.goal 内部的 markdown 互不干扰。改 `_render_onboarding` / `_render_incremental` / `_render_task_block` 时保住 XML 边界。

### 4. 测试改动

现有测试需更新（assert 字符串内容会变）：

- `tests/test_render_task_block.py` —— assertion 改成期望"标题：xxx" 而不是"当前任务：xxx"；status 行不再出现在返回的 body 里（变成元组的第二元素），需要拆 assertion。
- `tests/test_build_context_task_block.py` —— full prompt assert 改成期望 `<current_task status="..."` / `</current_task>` / `<recent_messages` 出现。

新增测试建议（薄一层即可）：
- `tests/test_render_onboarding_xml.py` —— 1 个 happy path（room+user+task+messages 都齐）+ 1 个 task=None + 1 个 user_persona 缺失，确认每一块的 XML 边界正确闭合。

## 改动后的样本输出（用户用例）

```
<room name="审稿委员会">
话题：围绕用户提交的论文、方案、报告或草稿进行审稿会：先确认审查对象与评价标准...
规则：保持中文；先读材料再判断，没读到就明确说明...
在场：老金（人类用户）, 魏理论, 贾数据, 水文献, 梅逻辑, 关主编
</room>

<user_persona display_name="老金">
以下是该参与者关于自己的说明：

## 身份
做一个多 agent 群聊 App（茶话室）的工程师。爱看《繁花》，爱在代码里讲腔调。

## 语气偏好
直接、不客套。可以叫我"老金"，不要叫"用户"或"亲"。
上海话和普通话混着用没问题，但别强行翻译。

## 忌讳
- 别用"哈喽"、"嗨"开头
- 别讲冷段子
- 别动不动"作为一个 AI"
</user_persona>

<current_task status="进行中">
标题：审稿
目标：
老金上传待审稿件，
各位评审专家依次评审，
关主编 总结，并形成 PDF 的审稿报告

工作目录 ./task/ 当前为空。
</current_task>

<recent_messages count="4">
老金 说：@关主编 删除 share 目录下所有文件
关主编 说：老金，share 目录里 5 个文件，包括刚刚审过的 SBOM 指南...
老金 说：全删
老金 说：<./share/投稿.docx>
</recent_messages>

<speak_instruction>
请以「魏理论」的身份发言。只说你要说的内容，不要复述别人的话，不要加引号或前缀。
</speak_instruction>
```

## 关键文件清单

- 改：`chahua/orchestrator.py:678-746`（4 个 render 函数）
- 改：`chahua/orchestrator.py:818-864`（`_render_task_block` 签名 + 内容）
- 改：`tests/test_render_task_block.py`
- 改：`tests/test_build_context_task_block.py`
- 改：`CLAUDE.md`（加一行不变量）
- 新增（可选）：`tests/test_render_onboarding_xml.py`

## 架构事实备注（影响 token 估算）

- **每个 `TeaGuest` 持独立 `Agentao` 实例**（`guest.py:145`）—— `agent.messages` 累积历史是**每茶客私有**，不跨茶客共享。茶客 A 只能通过 chahua 在自己的 `user_message` 里转发的 transcript 增量感知茶客 B 的发言。
- 因此**单个茶客的 `agent.messages` 累积规模 ∝ 该茶客自己的发言次数**，不是房间总消息数。N 茶客房间，单茶客累积约为总量的 1/(N+1)。
- **`./share/` 软链指向房间公共目录**（多茶客共看），**`./task/` 软链跟 active task**（多茶客共看 active 任务的 artifacts）—— 这两条不影响本文档，但说明 cwd 隔离是"软隔离"。
- **agentao 内部 token 节流**：`context_manager` 默认 200K 上限，55% (110K) 触发 `microcompact_messages()` 截断大 tool result，65% (130K) 触发 `compress_messages()` 调一次 LLM 把老消息 summarize。chahua 短聊场景一般跑不到。

## 复用的已有逻辑（不重复造轮子）

- `strip_top_h1()` —— `user_md.py:84-95`，USER.md 去 H1
- `format_messages()` —— `room.py:237-249`，messages → "speaker 说：text"
- `format_artifact_size()` / `format_artifact_mtime()` —— `tasks_store.py`，artifact 元信息渲染
- `TASK_STATUS_DISPLAY` —— `tasks_store.py`，status 国际化映射
- `_FULL_DECISIONS_CAP` / `_FULL_ARTIFACTS_CAP` / `_FULL_SUMMARY_TAIL_CAP` —— `orchestrator.py:808-815`，预算 cap 常量
- `self._display_map()` —— `orchestrator.py:760-767`，speaker_id → display_name

## 验证方式

1. `uv run pytest tests/test_render_task_block.py tests/test_build_context_task_block.py -v` —— 改完后两个文件全绿。
2. 新增 `tests/test_render_onboarding_xml.py`（如做）—— 全绿。
3. `uv run pytest` —— 全测试套件不回归。
4. **CLI 实跑**：`uv run chahua --room rooms/p3-黄河路`，发一条消息触发茶客发言；用 `--debug` 或 transcript log 看茶客实际拿到的 context_message，确认 XML 标签出现、USER.md 内部 H2 没串外层、`./task/` 提示出现。
5. **Electron 实跑**：`cd app && npm run dev`，开"审稿委员会"房间，开任务、上传 docx、@茶客发言；后台观察 sidecar 日志里的 prompt 装配结果与 mock 一致。
6. **不同 LLM provider 抽测**：分别用 Claude (Anthropic) / GPT-4o (OpenAI) / Qwen (OpenRouter) 各跑 1-2 轮，确认所有模型都正确解析 XML 块边界，没有模型把 `<current_task status="...">` 当文本输出。

## 不做的事（明确范围）

- 不动 incremental 路径的取数逻辑（仍按 last_seen / onboarding_threshold 判断）。
- 不补 next_action / deadline / progress 等任务字段（用户选了"中等扩展"，不选"激进重设计"）。
- 不动打分 prompt 模板。
- 不改 envelope schema_version（context_message 是 server → guest 内部数据，不上 wire）。
- 不加 emoji 装饰 status（保持文字 "进行中" / "已完成" 等，避免视觉过度）。
- **不做 token 节流优化**（已评估）。XML 化的增量成本：100 轮闲聊约 +3K tokens ≈ 现状 21K 的 14%，且距离 agentao 110K micro-compact 阈值还有 5 倍距离；同时 XML 块边界稳定有利于 prompt cache 命中，部分抵消增量。后续如要节流见下文「下一阶段方向」。

## 下一阶段方向：refresh-first-message 方案（B+）

**已认可的方向，本文档不实施，单独 ticket 跟进**。这里记下设计骨架，后续立 ticket 时直接复用，不用重新讨论。

### 思路

利用 agentao 的 `agent.messages` 累积特性，**把房间/USER/任务状态注入到 `messages[0]`**（每次该茶客被调度发言前 refresh）；后续每次 `speak()` 喂的 user message 仅含"增量 transcript + speak_instruction"。状态块用 `<system-reminder>` 包裹（Claude 训练时的语义标签，对其他 provider 降级为普通 XML，无副作用）。

### 实施后 LLM 看到的形态

```
system: persona_md

user (messages[0], 每次 speak 前 mutate 刷新):
  <system-reminder>Current Date/Time: ...</system-reminder>
  <system-reminder data-chahua-state="v1">
    <room>...</room>
    <user_persona>...</user_persona>
    <current_task>...</current_task>
  </system-reminder>
  <recent_messages>{首次 onboarding 末 N 条}</recent_messages>
  <speak_instruction>...</speak_instruction>

assistant: 第 1 次回复
user (messages[2], speak 时 append): <recent_messages>{增量}</recent_messages> + <speak_instruction>
assistant: 第 2 次回复
user (messages[4], speak 时 append): <recent_messages>{增量}</recent_messages> + <speak_instruction>
...
```

### 关键改造点

1. **拆 `_render_onboarding`** 成两个纯渲染函数：
   - `_render_state_block(room, user_config, task, ...)` → 仅渲染 `<system-reminder data-chahua-state="v1">...房间+USER+任务...</system-reminder>` 内容（无 recent_messages、无指令）
   - `_render_speak_payload(increment, guest_name, ...)` → 渲染 `<recent_messages>...</recent_messages>` + `<speak_instruction>...</speak_instruction>`
2. **改造 `TeaGuest.speak()`** 在 `arun` 前：
   - 如果 `agent.messages` 为空 → 首次：把 state_block + 首次 recent_messages + speak_instruction 一起作为 arun 参数喂
   - 否则 → mutate path：找 `messages[0]['content']` 里的 `<system-reminder data-chahua-state="v1">` sentinel，替换包裹内容；找不到 sentinel（被 agentao compress 总结过了）→ 走重建路径，insert 一条新的 state user message 到 `messages[0]` 位置，并在新 user message 头部加"前情提要"提示
3. **加 sentinel 定位 helper**：`_find_state_block_span(content: str) -> Optional[tuple[int, int]]` 用 regex 找 sentinel 的起止位置；返回 None 时触发重建。
4. **arun 调用切换为最小 payload**：后续 speak 喂给 `arun()` 的 user_message 仅含 `<recent_messages>` + `<speak_instruction>`，不再含 state。
5. **测试覆盖**：
   - 首次 speak → messages[0] 含 state sentinel
   - 第 2 次 speak 后 mutate messages[0] → 验证 state 块更新、recent_messages 不被覆盖（恢复路径）
   - 模拟 agentao compress 后 messages[0] 无 sentinel → 走重建路径
   - 任务切换后下次 speak → state 块里的 `<current_task>` 立即变成新任务

### 三个风险及处理

| 风险 | 处理 |
|---|---|
| **耦合 agentao 内部**（直接 mutate `agent.messages[0]`）| 现状 chahua 已经依赖 `tool_runner` / `permission_engine` 等 agentao 内部接口，多一个 mutate 同性质。**CLAUDE.md "关键不变量" 段加一条"脆性接缝"说明**，标注 agentao 升级时这里要回归。 |
| **agentao 130K compress 后 messages[0] 被 LLM-summarize**（XML 结构丢失）| sentinel 找不到 → 触发重建路径：insert 新的 state user message 到 `messages[0]`，content 头部带"会话已 compact，以下是最新状态"。 |
| **mutate messages[0] 让 prompt cache miss**（每次 prefix 变 = cache 失效）| **先在 A 落地后实测一轮 cache 命中率**，量化对比"省下的 input tokens" vs "失去的 cache 折扣"；如 cache 收益 > token 节省，B+ 可降级为"仅状态变化时刷新"（diff-aware 路径）。 |

### 前置条件

**A 落地后在真实长会话场景跑一周抓数据**：
- 每个茶客 `agent.messages` 累积 token 量（确认离 110K compress 阈值多远）
- prompt cache 命中率（Anthropic / DeepSeek provider 有 usage 字段返回 cache_read_tokens）
- 用户实际感知到的"茶客对房间/任务状态过时"的频率（A 路径下长间隔重发 onboarding 是否够用）

数据出来后再决定 B+ 是否值得做、做哪一档（全 refresh / 仅状态变化时 refresh / 不做）。

### 工程量估算

约为本文档主体（XML 化）的 1.5-2 倍。改动文件：`chahua/orchestrator.py`（拆渲染函数）+ `chahua/guest.py`（speak() 加 mutate path）+ 新增测试 + CLAUDE.md（加"脆性接缝"不变量）。
