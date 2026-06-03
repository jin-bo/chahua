# P14：系统生成 prompt 英文化 + 精练 + 发言前回顾自身历史

> 把**所有「系统生成、喂给 LLM」的 prompt 文案**由中文改英文，**并在同一遍逐块精练**（去 rationale / 去重复 / 散文转祈使），同时收两份红利：省 token + 提升指令遵循（冗余/解释性文字稀释指令）。其中 `_speak_instruction_block` 在英文化的同时做一个**行为增强**：要求茶客发言前**回顾自己 `agent.messages` 里的 tool-call 结果**（文件内容 / 查询结果 / 命令输出），优先据此作答；**仅当历史里相关内容被截断 / 缺失时**才回 source 重读（如重读 `./share/<name>`）。**用户内容（persona / USER.md / 房间 topic·rules / 聊天本身）一律不动，输出语言仍随对话（默认中文）；精练只删冗余、绝不丢承重指令、不动功能性字面量。**

## Summary

现状：chahua 大量「系统生成、喂给 LLM」的 prompt 是中文 —— context 块（`<speak_instruction>` / `<order_hint>` / `<room>` / `<current_task>` 等）、功能块（`<managed_session>` / `<review_target>` / `<panel_context>` / `<agent_run_task>`）、task 渲染、茶客侧工具的 description 与 return 文案。中文 prompt 比等价英文多耗 ~30-40% token，且与 agentao 英文 base prompt 混注会稀释指令。打分链路（`scoring.py`）已先行英文化，验证了可行性与「输出仍可保持中文」。

P14 把上述系统 prompt 全量英文化 + 精练（分批，见「验收范围」），**纯文案 + 工具 return 文案改动，零结构变更** —— 不动 `Message` / transcript / envelope / `schema_version` / 工具入参 schema 形状 / 事件类型。同时在 `_speak_instruction_block` 追加 recall 段落，解决「茶客被问到早期 tool 结果时倾向『不记得 / 没收到』而不主动翻历史」的问题。

**精练与英文化合并进行**：既然逐块重写文案，一次到位最省事。这些 prompt 的三个通病 —— ① 解释性文字混进指令（`**为什么**：…` / `典型场景：…` / `（例：…）` 是写给人的 rationale，LLM 不需要、还稀释指令）；② 同一约束重复多次（如 `task_rendering` 的「不要用 write_file」在 full-mode 两分支 + compact 共 3 处）；③ 散文体长定语从句。精练动作 = 去 rationale（移进代码注释）、去重复、散文转祈使短句 + 枚举。**收益双重**：token 下降（最肥的 `./task/` 指引块可降 ~85%）+ 指令遵循提升（剩下全是可执行指令）。

recall 问题根因：早期轮次的 tool 结果（文件全文、查询输出、命令结果）多数仍在 `agent.messages` 里，但 onboarding/incremental 每轮注入的新上下文把注意力重新定向到「近期对话 + summary」，而 summary 已把 tool 结果压成几个字 —— LLM 不会主动翻阅自己的历史。一句显式指令把注意力引回历史 tool 结果。

## 承重不变量

- **范围 = 仅「系统生成、喂 LLM」的 prompt 文案**。包含：context 块 / 功能块 / task 渲染 / 工具 description / 工具 return 文案 / 便宜模型（summarizer / persona_summary）system prompt。**绝不动**：用户内容（persona md / USER.md / 房间 `topic`·`rules` / 聊天消息）、agentao base prompt（在 agentao 包内，不归 chahua）、`HandoffItem.reason`（按既有不变量不进茶客 prompt）。**docstring / 注释 / 日志不是英文化目标**（它们不喂 LLM，无需为省 token 翻译）—— **但与 P14 行为变更直接相关的说明性 docstring 必须同步更新**（如 `format_messages` 分隔符改动后其 docstring、`task.py` 常量「与 JS 同源」docstring 的分叉说明，见下文与同步清单）；这是「修正会误导后人的陈述」，与「英文化文案」是两回事，不冲突。**Why**：英文化是为省 token + 对齐指令，只对「我们生成、且模型会读」的文案有意义；用户内容英文化 = 篡改用户数据；docstring 不进 prompt 故不翻译，但其内容若被 P14 改动证伪则必须随手修正。
- **纯文案改动，绝不引入结构 / schema / 形参 / 事件变更**。只改字符串字面量（+ 相邻注释同步）。**不动** `Message` / `transcript.jsonl` / envelope / `schema_version` / 工具 JSON-Schema 的字段名与形状 / 事件类型。工具 description 改的是 `description` 文本，**不改** `properties` 键名。**Why**：这是 prompt 工程调优，不是协议 / 能力变更；任何结构改动都会越出 P14 边界、放大评审面。
- **指令语言 ≠ 输出语言：凡塑造茶客回复的块都带语言锚点，产出仍中文**。每轮块（speak_instruction / task 指引 / review / panel / managed_session）英文化时统一加 `Always reply in the same language as the conversation (default: Chinese).`；`summarizer._SUMMARY_SYSTEM` / `persona_summary._SYSTEM_PROMPT` 英文化指令但**必须保留「输出中文」要求**（summary 喂回 `<room_summary>` 且给用户看，persona 摘要进 `<room>` 花名册）。**Why**：打分能放心英文化是因为输出是 JSON；中文指令此前隐性锚定中文输出，全英文化若不补锚点，茶客会开始用英文回复中文房间。
- **recall 行为（`_speak_instruction_block` 专属）：回顾历史为主、重读 source 兜底**。新增段落先要求「回顾自身 `agent.messages` 里的 tool 结果（file / query / command output 三类）并据此作答」，**仅在**历史里相关内容被截断 / 缺失时才提示「回 source 重读（如 `./share/<name>`）」。**Why**：历史里不止文件 —— 还有查询结果、命令输出等无法靠重读文件复原的 tool 结果，回顾历史不可省；重读只对「可重新获取的 source」有效，是退路不是首选。
- **`_speak_instruction_block` 改一处两态同时生效**。该块被 onboarding（`context_renderer.py:384`）与 incremental（`:416`）两条路径共调，改 `body` 即对两态生效。**Why**：只在 onboarding 注入会让多数短轮（走 incremental）失去 recall 提示，正是 recall 失效的高发场景。
- **`format_messages` 分隔符改成语言无关的 `{display}: {text}`；persona 文件不作为规则修改**。`room.py:277` 的 `{display} 说：{text}` 是单点定义、喂所有 transcript 视图。`format_messages` docstring 与 `docs/INVARIANTS.md` 历史上假设「`personas/*.md` 明示了 X 说： 格式」—— 但**当前扫描内置 `chahua/personas/*.md`（宝总 / 范总 / 玲子 / 汪小姐 / 爷叔）无一描述该格式**，所以那只是防御性假设、非已知必改项。策略：① 分隔符改成**中性、语言无关的 `{display}: {text}`**（冒号无语种，省「说：」token 且 persona 不必描述特定语种格式）；② **更新** `room.py::format_messages` docstring + `docs/INVARIANTS.md` 里关于「X 说：」的不变量措辞；③ **扫描**内置 persona，**仅当**某卡确实描述了旧格式才改那一句（当前无）；④ 用户 / 社区 persona **不迁移**（不强制、也无法强制同步）。**Why**：persona 是用户/作者内容，不在英文化范围；分隔符走语言中性正是为了**不依赖** persona 同步。
- **`task.py` 的 `TASK_UNTITLED` / `TASK_STATUS_DISPLAY` 后端用途全是 LLM-facing，可直接英文化；但要改「与 JS 同源」docstring 记录分叉**。前端**自带平行中文映射**（`app/renderer/events.js:286+` 的 value/label），且 `task_info` envelope 只下发**裸 status 枚举**（`task.py:125` `"status": self.status`），前端据枚举渲自己的中文 —— 后端这两个常量只被 `task_rendering`（prompt）+ `task_tools` ack（批 4）消费，**全 LLM-facing**。故英文化它们**不影响用户 UI**。**但** `task.py:48` docstring 现写「与 `events.js::TASK_UNTITLED` 同源（JS 镜像 Python）」—— 英文化后两边**有意分叉**（后端 LLM 英文 / 前端用户中文），docstring 必须改成「后端 LLM-facing 英文、前端 `events.js` 保留用户中文，二者不再镜像」，否则误导后人把前端也改英文。
- **工具 return / ack 是 LLM-facing，归 P14 英文化；用户看到的是前端 propose 卡片（`proposal_card.js`），属前端中文文案、不在 P14 范围**。工具 `return` 的字符串（「已提议…等用户采纳」「Error: …」）只作 tool result 回灌进发起调用茶客的 LLM 上下文，**不进前端**：`TOOL_COMPLETE` envelope 的 data 只带 `tool / call_id / status / duration_ms / error`（`transport_bridge.py:340-348`），**不携带 return 字符串**；P6 取证 `record_tool_complete` 同样只记 status/duration/error。**Why**：批 4 纯 LLM-facing，英文化省 token / 对齐指令、用户看不到；用户真正读的「采纳 / 忽略」卡片由 `TASK_PROPOSAL` envelope → 前端 JS 渲染（中文），那是前端文案、不归后端 prompt。误把卡片文案英文化或漏掉批 4，都是踩错这条边界。
- **精练只删冗余，承重指令一条不丢**。精练动作限于：删 rationale（`为什么`/`典型场景`/举例 → 移进代码注释，注释不进 prompt）、去重复、散文转祈使。**每块精练后须逐条核对原块的承重语义点仍在**（如 `./task/` 块的「读 read_file / 写 task_write_artifact / 否定 write_file / 命名习惯 / 概要+引用」五点；MTS 块的「自动入队 / 忽略等待 ack / spawn 并发 / 没下一步就收尾不结束 / done 收尾」五条）。**Why**：精练比纯英文化多一层语义判断风险，删错一句可能丢承重指令；省 token 不能以丢指令为代价。
- **不动功能性字面量；但区分「属性名 / raw 枚举」（不动）与「属性里的 display label」（LLM-facing，可英文化）**。精练 / 英文化都**绝不改**：工具名（`task_write_artifact` / `read_file` / `spawn_agent_runs`）、路径（`./share/` / `./task/`）、`Error:` 前缀、XML **标签名**、XML **属性名**（如 `status=` 这个键）、envelope 里的 **raw status 枚举**（`task.py:125` `"status": self.status` 下发的 `open` / `doing` / `done` …）、JSON 字段名。**但** `<current_task status="未开始">` 里 `status=` 的**值**是 `TASK_STATUS_DISPLAY` 渲染的 **display label**（不是 raw 枚举），属 LLM-facing 文案 —— 随 `TASK_STATUS_DISPLAY` 英文化变成 `status="not started"`，**这是预期改动、不算破坏字面量**。**Why**：机器约定（标签/属性名/raw 枚举/字段名）被调用方 / 解析器 / 不变量依赖其字面，改了破坏解析；而 `<current_task>` 的 status 值只给 LLM 读（前端用裸枚举 + 自带中文映射，见 task.py 常量不变量），英文化它不影响任何机器路径。
- **MTS `<managed_session>` 块的 bullet 顺序不能变**。CLAUDE.md / `docs/INVARIANTS.md` 按「第 ② 条作废 propose_* 等待语义」编号引用此块；精练可压缩措辞，但**保持 5 条指令的相对顺序**，否则须同步改两处不变量的条目编号。**Why**：编号引用是承重契约，重排会让不变量指错条目。
- **`{guest_name}` 身份指令 + chat vs 交付物纠偏语义不丢**。`_speak_instruction_block` 英文化后仍以 `Speak as "{guest_name}"` 开头，并完整保留「默认角色化短消息、显式要产物 / 被指派才切交付形态」的条件化豁免。**Why**：身份是每轮唯一锚定发言人的地方；纠偏是该块原有承重职责（抵消 agentao base prompt 的「知识工作执行者」默认产物倾向）。recall + 语言锚点是叠加，不是替换。

## 验收范围（分批，均属 P14）

P14 = 下列全部批次英文化 + speak_instruction 的 recall 增强。分批是为了**每批独立 PR + 独立回归**，但都在 P14 验收内。每批凡塑造茶客中文回复的块都带语言锚点。

### 批 0 —— 打分链路（已在工作区，纳入 P14 栈）

| 位置 | 内容 |
|---|---|
| `scoring.py:67` `_SCORING_PROMPT_TEMPLATE` | 打分主 prompt（scoring guide / JSON 约束） |
| `scoring.py:106/116` `_USER_BLOCK` / `_SUBJECT_HINT` | 用户介绍块 / 被提及计数块 |
| `scoring.py:129` `_ORDER_HINT_BLOCK` | 打分版发言顺序 hint |

> **状态校准**：批 0 的英文化**当前只存在于工作区未提交改动**（`git status` 显示 `M chahua/scoring.py`），HEAD 上仍是中文。P14 把它正式收编为「批 0」—— **与 P14 其余批次同栈合入**（不再作为「无关的脏改动」游离）。评审时须把 `scoring.py` 的 diff 一并纳入 P14 PR 栈说明。

### 批 1 —— 每轮喂茶客（token 收益最大）

| 位置 | 块 | 频率 |
|---|---|---|
| `context_renderer.py:419` `_speak_instruction_block` | `<speak_instruction>` | 每轮（+ recall 增强 + 语言锚点） |
| `context_renderer.py:36` `_SPEAK_ORDER_HINT_BLOCK` | `<order_hint>` | 每轮（有 task 时） |
| `context_renderer.py:303` `_render_roster` | `<room>`「在场：」/「（人类用户）」 | onboarding |
| `context_renderer.py:338-340` | `<room>`「话题：/规则：」 | onboarding |
| `context_renderer.py:362` | `<user_persona>`「以下是 {name} 关于自己的说明：」 | onboarding |
| `context_renderer.py:406` | `<room_update>`「（无新消息）」fallback | incremental |
| `room.py:277` `format_messages` | **`<message>` 内的「{display} 说：{text}」分隔标记** —— 最普遍的系统标记，进 `<recent_messages>` / `<room_update>` / scoring transcript / summarizer / review block 全部 transcript 视图 | 每条消息 |
| `task_rendering.py:58/80/109` | `render_task_header/scoring_header/task_block`：「标题：/目标：/负责人：/近期决策/当前产物/任务近期进展」+ **大段 `./task/` 落盘指引（单块最长中文 prompt，~200+ 字）** | 每轮（有 active task 时） |
| `task.py:47/51` `TASK_UNTITLED` / `TASK_STATUS_DISPLAY` | task block 的空标题 fallback「(无标题)」+ 状态中文 label（进 `<current_task status="未开始">` 属性、task block body、`task_tools` ack） | 每轮（有 active task 时） |

### 批 2 —— 功能块（条件触发）

| 位置 | 块 | 触发 |
|---|---|---|
| `context_renderer.py:47` `render_agent_run_block` | `<agent_run_task>` | bg run |
| `context_renderer.py:79` `render_managed_session_block` | `<managed_session>`（长） | MTS 管理者回合 |
| `_orchestrator_consts.py:20` `_REVIEW_INSTRUCTION` + `_orchestrator_handoff_drain.py:370` `_render_review_block` | `<review_target>` | review |
| `_orchestrator_consts.py:29` `_PANEL_SUMMARY_BLOCK` | `<panel_summary_request>` | panel 汇总 |
| `_orchestrator_handoff_drain.py:395` `_render_panel_block` | `<panel_context>` | panel |

### 批 3 —— 工具 description + 便宜模型 system prompt

- **递归覆盖工具 JSON Schema 内所有 `description` 文本（进 tool schema = 常驻 token）**：`task_tools.py`（5 个）+ `handoff_tools.py`（3 个）+ `agent_run_tools.py`（`spawn_agent_run` / `spawn_agent_runs`）。**不止顶层与一级 `properties.*.description`，还含嵌套层**：如 `spawn_agent_runs` 的 `parameters.properties.runs.description` + `runs.items.properties.{target,instruction,task_id}.description`（`agent_run_tools.py:236/240/244/250`）。例：`task_tools.py:193`「决策摘要（≤ 200 字一句话）」/ `handoff_tools.py:96`「被指派发言的茶客名」/ `agent_run_tools.py:156`「被指派的茶客名」。**所有层级的 description 文本都英文化；但绝不改 `properties` 键名 / 类型 / `required` / 形状 / `additionalProperties`** —— 键名与 shape 是线协议契约，改了会破坏 inbound 解析。实现时**递归遍历整个 schema dict 找 `description` 键**，不靠枚举行号。
- `summarizer.py:59` `_SUMMARY_SYSTEM`：英文化指令，**保留「输出中文 3~5 条要点」要求**。
- `persona_summary.py:47` `_SYSTEM_PROMPT`：英文化指令，**保留「中文 ≤30 字、结尾不加标点」输出要求**。

### 批 4 —— 工具 return / ack 文案（回灌进 LLM 上下文）

工具 return 的字符串会作为 tool result 回到模型上下文，与 description 是两类，易漏。**判据 = 工具任意 `return` 路径里的中文，含 happy-path 头/尾、成功 ack、以及中英混排串（如 `Successfully wrote … （文件现 N bytes）。`）—— 不止空清单 / 错误路径。** 注意一类间接路径：工具调用 `_start_agent_run` 等 server 回调拿到中文 `err`，再由工具侧拼成 `Error: ...` 回灌 LLM —— **被调用方的中文 err 串也归批 4**。下表是**易漏返回路径清单（非全量，以判据为准）**：

| 位置 | 文案类型 |
|---|---|
| `handoff_tools.py:73` | 成功 ack「已提议，等用户在 UI 采纳后才会生效。」 |
| `handoff_tools.py:152` | Error「「{reviewee}」最近没有发言可审…」 |
| `task_tools.py:82` | 空清单「（当前任务暂无产物）」 |
| `task_tools.py:84` | **非空清单 happy-path 头行**「当前任务产物清单（不嵌内容，按需 ls / cat 走 `./task/` 读取）：」 |
| `task_tools.py:91` | **截断行**「…（另有 {N} 个未列）」 |
| `task_tools.py:130/133` | 「当前无活跃任务…」/「任务 {id} 不存在…」 |
| `task_tools.py:169/305` | 成功 ack「已提议「…」，等用户在 UI 采纳后入库。」+ `ack_label`「把任务标记为 {label}」 |
| `task_tools.py:296/300/388/391/393/403` | 一系列 `Error: …`（status 非法 / 无活跃任务 / 任务已终结 / 写入失败） |
| `task_tools.py:405-408` | **写盘成功 happy-path（中英混排）**「Successfully {action} ./task/{name}（文件现 {size} bytes）。」 |
| `agent_run_tools.py:125` | 「内部错误（…）：…」 |
| `agent_run_tools.py:180-187` | `spawn_agent_run` 校验 Error（入口未装 / target / instruction / task_id） |
| `agent_run_tools.py:260/262/265-266/274/279/281/286/289-290` | **`spawn_agent_runs` 全量校验 Error**（入口未装 / runs 非数组 / 超 cap / runs[i] 非 object / target / instruction / task_id / 同批次重复） |
| `agent_run_tools.py:308-313` | **批量 partial-success 状态返回**「Error: runs[{i}] …；本批前 {N} 条已创建（run_id: …）。」 |
| `agent_run_tools.py:314` | **空 run_id 护栏**「Error: runs[{i}] (...): server 返回空 run_id（不变量破坏）」 |
| `_server_agent_run.py:155/163/166/172/178/197` | **server 回调 `_start_agent_run` 的 `err`** —— **双路消费，改返无参数原因码**（见下「双路 err」）：target 不在场 / task_id 不存在 / task_id 已关闭 / target 正忙 / **房间 bg run 数已达上限** / target 是当前 MTS 管理者 |

> 实现批 4 时**通读每个工具的所有 `return` 分支**，不靠本表行号（行号会漂）；表是「易漏点」提示，判据才是权威 —— 任何回灌进 tool result 的中文都要英文化（保留调用方依赖的 `Error:` 前缀等机器约定）。

> **双路 err（`_start_agent_run`）——以原因码 seam 落地（实现修订）**：它的 `err` 有两个消费方 —— ① 工具侧（`agent_run_tools` spawn 路径）拼成 `Error: …` 回灌 LLM（要英文）；② 用户手动 `agent_run_start` inbound（`server_inbound_agent_run.py`）拼成前端 NOTICE 给**用户**看（保持中文、P14 范围外）。一份成形字符串无法同时满足两语言，且「源头中文 + 工具侧正则回译」是脆弱耦合。**最终方案：`_start_agent_run` 改返无参数原因码**（`agent_run.AgentRunError` Literal + 6 个 `AGENT_RUN_ERR_*` 常量：`target_absent` / `task_not_found` / `task_closed` / `target_busy` / `room_cap` / `mts_manager`），两个消费方各自本地化 —— inbound `_agent_run_err_zh` 渲中文 NOTICE、工具 `_agent_run_err_en` 渲英文 `Error:`。所有插值参数（target / task_id / room cap）在两个 render 点都各自可得，故**码无参数**，两语言由构造保证同步、无回译。这是内部 helper 的返回约定调整（非 wire/schema/envelope 变更），仍守「不动协议」不变量；`test_start_agent_run_helper` 改断言原因码（源头契约），inbound NOTICE 中文与改前**逐字一致**。其余批 4 工具 return 是工具内联字面量、单一消费方（仅回灌 LLM），不涉及此分叉。

### 封口扫描：有意排除（非遗漏，附理由）

全库按「最终会进 LLM 的字符串」判据扫过一轮，批 0~4 已覆盖全部 LLM-facing 系统 prompt。以下是**确认在 LLM 路径之外、有意排除**的项 —— 记录下来避免后人误当遗漏补刀：

| 排除项 | 位置 | 理由 |
|---|---|---|
| 房间导出 markdown | `exporter.py::format_room_markdown` | 导出给**用户**的文件，非 LLM 输入 |
| CLI REPL 文案 | `cli.py` | 只渲染终端 + 透传用户输入，无系统 prompt 喂 LLM |
| `HandoffItem.reason` | `_orchestrator_managed_session.py:264/397` 等 | 按既有不变量**不进茶客 prompt**（provenance / UI 用） |
| `assert` 消息 | `_orchestrator_handoff_drain.py:384` 等 | 抛给开发者 / 日志，非 LLM |
| `ScoreKind` 枚举行内注释 | `scoring.py:47-50` | 注释非 prompt；枚举值本身是英文 wire 值 |
| `wrap_current_task` | `task_rendering.py:47` | 纯 XML 包装、无中文字面量（status 来自 `task.py` 常量，见批 1） |
| `TeaGuest.speak` → `arun` | `guest.py:396` | 只传 `context_message`（已覆盖）+ 图，无额外中文包装 |
| agentao base prompt | agentao 包内 | 不归 chahua；茶客「知识工作执行者」默认倾向由 `<speak_instruction>` 纠偏 |
| 用户内容 | persona md / USER.md / 房间 topic·rules / 聊天消息 | 用户数据，英文化 = 篡改（见承重不变量第一条） |
| 前端 JS 文案 | `app/renderer/*.js`（如 propose 卡片 / `events.js` status label） | 用户可见、属前端，不归后端 prompt（见工具 return 不变量） |

## 现状（P14 前）—— 以 `_speak_instruction_block` 为例

`context_renderer.py:419-433`：

```python
def _speak_instruction_block(self, guest_name: str) -> str:
    # 末句纠偏 chat vs 交付物：agentao base prompt 把茶客定位成「知识工作执行者」...
    body = (
        f"（请以「{guest_name}」的身份发言。只说你要说的内容，"
        f"不要复述别人的话，不要加引号或前缀。\n"
        f"这是群聊对话：默认用角色化的口语短消息回应，不要套用"
        f"「结论/证据/局限」「分步骤交付物」「待办清单」等正式产出格式；"
        f"只有当用户明确要求产出文档 / 分析 / 代码 / 任务产物，"
        f"或你被显式指派去做事时，才切换到完整的工作交付形态。）"
    )
    return f"<speak_instruction>\n{body}\n</speak_instruction>"
```

缺口：① 全中文；② 无任何「回顾自身历史」指令，茶客被问到早期 tool 结果时倾向「不记得 / 没收到」。

## 修订后 `body`（英文 + recall + 语言锚点）

```python
body = (
    f"(Speak as \"{guest_name}\". Say only what you mean to say — "
    f"do not repeat others' messages, and do not add quotes or prefixes.\n"
    f"This is a group chat: respond by default with short, in-character "
    f"conversational messages. Do not fall back on formal output formats "
    f"such as \"conclusion / evidence / limitations\", \"step-by-step "
    f"deliverables\", or \"to-do lists\" unless the user explicitly asks "
    f"for a document / analysis / code / task deliverable, or you have been "
    f"explicitly assigned work — only then switch to full deliverable mode.\n"
    f"Before answering, review your own conversation history. Your earlier "
    f"turns contain tool-call results — file contents, query results, command "
    f"output — that the user may now be referring to. Ground your reply in "
    f"those prior results rather than guessing or assuming you never saw them. "
    f"If the relevant content appears truncated or missing from history, "
    f"re-fetch it from its source (e.g. re-read the file under ./share/) before "
    f"answering.\n"
    f"Always reply in the same language as the conversation (default: Chinese).)"
)
```

## 精练清单（与英文化同批，按收益排序）

每块「英文化 + 精练」一次到位。下面是高收益块的 before/after 与承重语义核对。token 估算按中文 ~1.5 tok/字、等价英文祈使句粗算，仅示量级。

### 1. `task_rendering.py` full-mode `./task/` 指引（单块最肥，~400 → ~60 token）

**问题**：empty 分支与 with-artifacts 分支各重复「何时落盘 / 命名 / 为什么 / 否定 write_file」四段；`为什么`/`典型场景`/`（例：…）` 全是 rationale。

**after（两分支共用核心，英文祈使）**：

```
## Task workspace ./task/
Read: read_file('./task/<name>'). Write: task_write_artifact(name, content) — NOT write_file (rejected by PathPolicy).
Persist any reusable output (reviews, designs, decisions, code, drafts) as an artifact; in chat keep a one-line summary + file ref.
Name with role + version, e.g. <you>-review-v2.md.
```

承重核对：读工具 / 写工具 / 否定 write_file / 命名习惯 / 概要+引用 —— 五点全在；compact 路径同样收敛为 2 行。

### 2. `render_managed_session_block`（每 MTS 管理者回合）

**问题**：5 条 bullet 各带「否则…」从句（rationale）。

**after（纯祈使，rationale 移注释；保持 5 条顺序）**：

```
<managed_session manager="X" remaining_budget="N">
You autonomously drive this task within budget.
- After reviewing, directly call propose_delegate/propose_panel; here they auto-queue — do NOT wait for user approval.
- Ignore any "awaiting user approval" tool ack in this session.
- For parallel work use spawn_agent_runs (off-budget); do NOT use propose_panel for parallelism.
- No next step? Just finish your turn — the session idles for the user, it does NOT end.
- Goal met? Call task_propose_status("done").
Budget left: N review rounds.
</managed_session>
```

承重核对：自动入队 / 忽略等待 ack / spawn 并发 / 没下一步收尾不结束 / done 收尾 —— 五条全在、**顺序不变**（守 MTS 第②条编号不变量）。

### 3. order-hint（**不可合并**，各自精练）

`_SPEAK_ORDER_HINT_BLOCK`（speak 行为指令）与 `scoring._ORDER_HINT_BLOCK`（分数锚点）按 CLAUDE.md 不变量**不能合并**。speak 版精练：「没轮到你时不要按本职完整发言——只输出一句简短让位句（如…）」→ `Not your turn? Yield in one short line, don't speak in full.`

### 4. 中等收益

| 块 | 精练动作 |
|---|---|
| `render_agent_run_block` | 删「不要写『我先想想』『稍等再说』」举例，保留否定指令 |
| `_speak_instruction_block`（recall 段） | 3 句压 2 句：先回顾历史 tool 结果再答；截断则重读 source |
| `_PANEL_SUMMARY_BLOCK` / `_render_panel_block` | 「不要逐条复述，要结构化归纳」转祈使 |

### 边界（见承重不变量）

精练**不动**功能性字面量（工具名 / 路径 / `Error:` 前缀 / XML 标签名 / 属性名 / raw status 枚举 / JSON 字段）；但 `<current_task status=...>` 的 display label 值随 `TASK_STATUS_DISPLAY` 英文化（见承重不变量）。MTS bullet 顺序不变；每块精练后逐条核对承重语义点。

## 测试计划（复现优先）

> **现状**：`tests/test_render_onboarding_xml.py` 当前只断言 `<speak_instruction>` **标签存在 + 块顺序**，**从不校验块 body 文案**。所以涉及 body 的都是**新增断言 / 新增测试**，不是「改现有断言」—— 否则文案改动会在零回归覆盖下落地。

1. **新增 body 断言（speak_instruction）**：在标签 / 顺序断言之外，新增对 `<speak_instruction>` 内容的断言，匹配英文关键词（`Speak as`、`review your own conversation history`、`tool-call results`、`reply in the same language`）。
2. **两态覆盖**：分别断言 onboarding 与 incremental 输出的 `<speak_instruction>` 都含 recall 段落 —— 钉死「改一处两态生效」。
3. **身份保留**：断言 `Speak as "{guest_name}"` 中 `guest_name` 正确插值。
4. **语言锚点不丢**：断言每轮块 body 含 `reply in the same language`；并保留 / 新增一条「中文房间产出仍中文」的行为回归（至少覆盖 summarizer / persona_summary 仍要求中文输出）。
5. **逐批渲染回归**：批 1-2 各块改完跑现有 `test_render_*` / 装配测试；批 3 工具 schema 改完断言 `properties` 键名 / 形状未变（只 description 文本变）；批 4 工具 return 改完跑 task / handoff / agent_run 工具相关测试，确认 `Error:` 前缀等被调用方依赖的约定未破坏。
6. **双路 err 分叉断言（`_start_agent_run`，原因码 seam）**：① 源头契约——`_start_agent_run` 校验失败返**无参数原因码**（`test_start_agent_run_helper` 断言 `err == AGENT_RUN_ERR_*`）；② 工具侧——`spawn_agent_run(s)` 的 tool result 是**英文** `Error: …`（`test_agent_run_tools` 喂码、断言英文）；③ inbound——`agent_run_start` NOTICE **仍是中文**；④ 分叉钉死——同一码 `_agent_run_err_zh` 含 CJK、`_agent_run_err_en` 不含 CJK，且都不回显原始码字面（`tests/test_agent_run_err_dual_lang.py` 覆盖 6 码 + room_cap「中文带数字 / 英文略数字」）。**Why**：码无参数 + 两路平行 render，省掉脆弱回译；漏渲一个码会让 `str(code)` 把 `target_absent` 这类原始 token 漏给用户 / LLM，故须钉「码覆盖对称」。
7. **精练语义保全断言**：对精练过的块，断言承重语义点的**功能性字面量仍在** —— 如 `./task/` 块含 `task_write_artifact` 且含 `read_file` 且含否定 `write_file`；`<managed_session>` 块含 `propose_delegate` / `spawn_agent_runs` / `task_propose_status` 且 **5 条指令顺序不变**（按出现位置断言）；各块未误删工具名 / 路径 / `Error:` 前缀。**Why**：精练比英文化多语义判断风险，用断言把「删错丢指令」挡在回归层。

## CLAUDE.md / INVARIANTS 同步清单

- [x] `docs/INVARIANTS.md` 新增 `### 9.x P14 系统 prompt 英文化 + 发言前回顾历史` 段，搬入上「承重不变量」。
- [x] `CLAUDE.md`「context 渲染与 prompt 装配」段：补一句「P14 起系统生成 prompt 全量英文化 **+ 逐块精练**（含 context / 功能块 / task 渲染 / 工具 description+return / 便宜模型指令），用户内容不动、输出语言随对话；`_speak_instruction_block` 追加 recall 段落 + 语言锚点，两态共用」。**精练承重约束同步**：精练只删冗余、不丢承重语义、**不改功能性字面量**（工具名 / 路径 / `Error:` 前缀 / XML 标签名·属性名 / raw status 枚举 / JSON 字段；`<current_task status>` 的 display label 例外、随 `TASK_STATUS_DISPLAY` 英文化）、**MTS `<managed_session>` 5 条指令顺序不变**（守第②条编号引用的不变量）。
- [x] `chahua/context_renderer.py` 块内注释（`:420-424`）同步说明英文化 + 新增 recall / 语言锚点段落职责。
- [x] 各批改动若涉及既有不变量措辞（如 MTS `<managed_session>` 块「第 ② 条」编号），英文化后同步 CLAUDE.md / `docs/INVARIANTS.md` 对应条目引用。
- [x] 批 1 改 `format_messages` 分隔符（→ 语言无关 `{display}: {text}`）→ 更新 `room.py::format_messages` docstring + `docs/INVARIANTS.md` 里「X 说：」不变量措辞；扫描内置 `personas/*.md`，仅当某卡确实描述旧格式才改那一句（当前扫描无）；用户 / 社区 persona 不迁移。
- [x] 批 1 改 `task.py` 常量 → 更新 `task.py:48` 「与 `events.js` 同源」docstring 为「有意分叉：后端 LLM 英文 / 前端 `events.js` 用户中文」；确认 `app/renderer/events.js` 的中文 label **不动**。
