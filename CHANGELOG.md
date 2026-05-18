# Changelog

格式参考 [Keep a Changelog](https://keepachangelog.com/)；版本号遵循 [SemVer](https://semver.org/)。
完整发布说明见 [`docs/releases/`](docs/releases/)。

## [Unreleased]

### Added
- **P5.6 打分结合任务要求**（2026-05-17，详见 [`docs/P5.6-打分结合任务要求.md`](docs/P5.6-打分结合任务要求.md)）：scoring prompt 加极简 `<current_task>` XML 块，让"想接话"看见当前任务。每 pick 周期 1 次 `get_task`，N 个 scorer 共享同一字符串。

### Changed
- **`chahua/server.py` 按 inbound feature 拆 handler 类**（2026-05-17，P5.2 重构起步，详见 [`docs/P5-任务房间.md`](docs/P5-任务房间.md) §7.2）：`server.py` 2116 → 1022 行（-52%）。30+ 个 `_inbound_*` handler 切到 4 个文件 `server_inbound_{admin,task,io,settings}.py`；模块顶共享小工具集中到 `_server_helpers.py`。**先用多继承 mixin 装配，同日再换成组合**：`ChahuaServer.__init__` 实例化 `self.admin` / `self.io` / `self.settings` / `self.task` 四个 slot，handler 持 `self.server` 反向引用。原 `_INBOUND_HANDLERS` 表先改成 `(slot, method_name)` 元组字典，随后再简化为模块级 `_INBOUND_ROUTES: dict[str, str]`（属性路径字符串如 `"admin._inbound_add_guest"`），`__init__` 时 `_bind_inbound_handlers(self)` 走 `operator.attrgetter` 一次性解析成 `self._inbound_handlers` bound-method 字典；dispatch 退化为单次 dict 查（之前是双 getattr）。属性路径无 `.` 表示 method 在 `ChahuaServer` 自身（cancel / switch_room / clear_room / user_message）。外部 API（`from chahua.server import ChahuaServer` / 主入口）零变化，383 测试全过；模块级 `_INBOUND_HANDLERS` 已重命名为 `_INBOUND_ROUTES`（私有符号，无外部依赖）。`_UPLOAD_MAX_BYTES` 与 `ensure_room_share_dir` 等模块级符号位置改变，monkeypatch 测试需要点到 `chahua.server_inbound_io` / `chahua.server_inbound_task` 而不是 `chahua.server`；`object.__new__(ChahuaServer)` 跳 `__init__` 的测试夹具需要手工 `srv.task = TaskHandlers(srv)` 等装回 slot。

### Added
- **P5.4 任务房间 —— 茶客自动归集 + XML 化 context_message**（2026-05-17）：把"茶客把产物写到 `./task/`"从被动告知升级为主动感知，并把喂茶客的 user message 改成 XML 标签包外层 + Markdown 渲内层。513 测试全过（P5.3 收线时 483 → P5.4 + Codex review fixes +30）。
  - **茶客自动归集**：`./task/` 软链对茶客**可读可写**（之前 P5.2 文档表述"只读"已修正——`agentao` cwd 边界对软链解析后位置不拦），茶客直接写 `./task/<name>` 即写到 `tasks/<active>/artifacts/<name>`。`Orchestrator._kick_detect_new_artifacts(sink, active_task_id)` 在 `_run_ai_chain` 每个 pick 周期末尾紧随 `_kick_summarize` 跑，扫 `tasks/<active>/artifacts/` diff `_seen_artifacts[task_id]`，emit N 条 `task_artifact_added{created_by="guest"}` hint + 一帧 `task_info` 权威快照（payload 走新 `tasks_store.build_task_info_payload`，与 `server_inbound_task._emit_task_info` 同源）。`__init__` 时只 seed 非 closed 任务的 artifacts（防 boot 旧 artifact 当新增 + 不堆 closed task dict）；runtime 新任务首次扫时 `.get(..., frozenset())` 兜底。新增 `chahua/task.py::ARTIFACT_CREATED_BY_GUEST = "guest"` 常量；用户 UI `attach_artifact` 走 `MARKED_BY_USER = "user"` 对仗。用户 UI 上传与 `_kick_detect_new_artifacts` 不同源，下次扫会把用户上传的文件再 emit 一次 hint —— **接受这个重复**（`task_info` 是权威，hint 无 toast 时无感）。
  - **XML 化 context_message**：`_render_onboarding` 输出固定 5+1 块 —— `<room>` / `<user_persona>` / `<room_summary>` / `<current_task>` / `<recent_messages>` / `<speak_instruction>`，按"无内容则整块省略"裁剪；`_render_incremental` 输出 `<room_update>` / 可选 `<current_task>` / `<speak_instruction>` 三块。XML 标签是边界承重墙：USER.md 内部 H2（`## 身份` / `## 忌讳` 等）被 `<user_persona>` 包住后自然降级为段内标题，不再与外层结构同视觉级；任务 status 走 `<current_task status="...">` XML 属性，body 不再含"状态："行。`_render_task_block` 返回 `(body, status_display)` 二元组，调用方拼到 XML 属性。"在场"行用"显示名（人类用户）"后缀标注用户（替代原"含人类参与者"句尾笼统标记）。
  - **XML 属性 quoteattr 防注入**（Codex review 第 2 轮发现）：room.name / `user_config.display_name` / `status_display` 等用户配置字段裸插入 XML 属性时，含 `"` / `<` / `&` 可破坏 `<room name="...">` 边界（被构造成 `</room><inject ...>` 形篡改外层结构）。改 `xml.sax.saxutils.quoteattr` 统一转义，4 处 attribute 插入点（`<room name>` / `<user_persona display_name>` / `<room_update name>` / `<current_task status>`）全过。ASCII-safe 输入仍走双引号包裹（兼容旧测试 assertion）。
- **任务产物点击下载 + 路径白名单 tighten**（2026-05-17）：任务面板里的 artifacts 列表项点击 / Enter / Space 触发 `download_file` inbound，renderer 收 `FILE_DOWNLOAD` envelope 后 base64 → Blob → `<a download>` 走浏览器原生下载。白名单**双层校验**：① 前缀 `share/` 或 `tasks/`；② `tasks/` 必须严格落在 `tasks/<id>/artifacts/<name>+` 形（≥4 段 + `segments[2] == "artifacts"`），挡掉 `state.json` / `<id>/task.json` / `decisions.jsonl` / `events.jsonl` 等内部元数据；③ resolve 后必须仍落在**具体白名单子目录**（`share/` 或 `tasks/<id>/artifacts/`），不只是 `room_dir` 子树 —— 防 `share/foo -> ../tasks/state.json` 形 symlink 逃逸到元数据（Codex review 第 2 轮发现）。200MB 上限与上传同口径；恒发 `FILE_DOWNLOAD`（成功 / 失败）失败同发 NOTICE error。
- **P5.3 任务房间 —— 茶客侧感知（通道 1 + 通道 3）**（2026-05-17，详见 [`docs/P5-任务房间.md`](docs/P5-任务房间.md) §7.3 / §13）：把 P5.2 落盘但没人读的任务级 summary 和茶客对 task 的零感知一起接通；7 个主路径 commit 分 3 个 PR，483 测试全过（P5.2 收线时 440 → P5.3 +43）。
  - **通道 1：prompt 注入两态都贴 task 块**（PR 1，P5.3.1~2）。开任务后茶客 onboarding（完整块 ≤300 token：title / goal / status / owner / 决策 ≤5 / artifact ≤10 / task summary 末 ≤3 段）与 incremental（compact 块 ≤80 token：当前任务 + 目标首行 + 产物路径提示）两条路径都注入 task 视野。`_render_task_block(task, decisions, artifacts, summary_tail, *, compact)` 拆成纯字符串 renderer（不接 self / 不读 store / 不背状态分支），取数留给 `_build_context_for(guest_name, *, task_id)`；`_run_turn` snapshot 的 `active_task_id`（P5.1.8 已立）经形参一路传到渲染器。closed task / 不存在 task 在 `_build_context_for` 判后直接不注入。compact 路径短路省 IO：只调一次 `get_task`，跳过 decisions / artifacts / summary 三个 read API。`TASK_STATUS_DISPLAY` / `TASK_UNTITLED` / `format_artifact_size` / `format_artifact_mtime` 收到 `chahua/task.py` 作单一来源，前端 events.js 镜像引用。
  - **通道 3 协议层：三个 task-aware 工具**（PR 2，P5.3.3~5）。新增 `ChahuaEventType.TASK_PROPOSAL` envelope（hint，schema_version 不动）；新建 `chahua/task_tools.py` 三个 `Tool` 子类 + `register_task_tools(agent, *, tasks_store, transport)` 工厂：`task_list_artifacts` 返当前 active task 的 markdown 清单（active=None 返提示串），`task_propose_decision(summary, supporting_message_ids?)` / `task_propose_open(title, goal)` 直接走 `transport.emit_chahua(TASK_PROPOSAL, ...)`，工具返 LLM 的 string 明示"已提议，等用户采纳"避免反复刷 propose。三工具均 `is_read_only=True`（为权限层放行 ≠ 无副作用，propose 仍 emit envelope）。`TeaGuest.__init__` 加 `tasks_store: TasksStore` 形参，`apply_permission_mode` 后一行 `register_task_tools(...)` 装配；`session._build_guests` 改签名传入 store。`ChahuaTransport` 加 `guest_name` / `current_task_id` 公共 property 供工具读 bind 时 snapshot，老任务的 propose 在协议层就不可能错挂到新 active。
  - **通道 3 前端闭环：proposal 卡片 + 采纳回环**（PR 3，P5.3.6~7）。`app/renderer/proposal_card.js` 工厂渲染米黄底米褐边卡片，挂到当前发言茶客气泡下方：`<proposer> 提议（等用户确认）` + payload preview（decision = summary + 引用消息数 / open = title + goal）+ "采纳 / 忽略"双按钮。"采纳"按 `kind` 分发回 inbound：`decision` → `Inbound.ADD_DECISION`（带 envelope 中 `task_id` + summary + supporting_message_ids），`open` → `Inbound.OPEN_TASK`（title + goal）；payload 不合法时按钮 disabled + tooltip 解释。`(proposer, kind, payload-hash)` 指纹集合 session-local 去重，连刷同一 propose 只渲染一张；切房 / `clear_room` 回环时 reset。server handler 零改动 —— 沿用"写权限永远在用户"口径，茶客不能静默开任务 / 记决策。`events.js` 加 `TASK_PROPOSAL` event + `TaskProposalKind` 常量；renderer.js dispatch 处单行接入。
- **P5.2 任务房间多任务**（2026-05-17，详见 [`docs/P5-任务房间.md`](docs/P5-任务房间.md) §7.2）：把 P5.1 故意压住的三个状态面（切 active / 关闭任务 / 多任务共存）一次全打开，并落地通道 2 + 任务级摘要落盘。440 测试全过（P5.1 收线时 ~400 → P5.2 +40）。
  - **多任务共存，单时刻最多 1 个 active**：`open_task` 自动 `set_active` 到新任务，旧 active 留作 `status="open"` 进历史列表；`TaskExistsError` 类型保留给老调用方但永不抛。新 inbound `set_active_task {task_id|null}` / `close_task {task_id, status}` 走前**强制** `_cancel_and_drain_inflight`（防 in-flight turn 末尾消息错挂到新 active）；`update_task` patch 白名单扩 `owner` / `status`。新事件 `task_close`；切 active 仍**不发独立事件**，沿用"重发 `task_info` 全量快照"口径。双向修复扩展：state.json 缺 + 多 task.json → 不自动选 + emit NOTICE 让 UI 选。
  - **通道 2：茶客 `./task/` 软链跟 active task**：每位茶客 `working_directory / "task"` 软链指向 `tasks/<active>/artifacts/`。装配末尾 + open / set_active / close 三处 active 改变后各 relink 一次；active=None 时只解不建。Windows 走 `mklink /J` junction 兜底（普通用户即可，`agentao` cwd 边界对软链/junction 同等拦写）。失败 WARN 不阻断 inbound，茶客看不到 `./task/` 但任务本身已切换。`./task/` 与 `./share/` 对仗，但前者只读 / 后者双向房间公共桌面。`chahua/_fs.py` `link_dir_idempotent` 加 `windows_junction_fallback` 选项 + 新增 `unlink_managed_link`（Windows 用 `FILE_ATTRIBUTE_REPARSE_POINT` 位判 junction，不靠 realpath 异化比对）。
  - **任务级 `summary.jsonl` + cursor 落盘 only**：`TaskSummarizer(Summarizer)` 子类 `_collect_block` 过滤 `task_id` + 独立 cursor (`considered_until_seq`)；`TaskSummaries` 池在 orchestrator `_kick_summarize` 末尾对 store 当前每任务跑一次 `maybe_summarize`，写 `tasks/<id>/summary.jsonl`。cursor 内存推进、`session.close()` 统一 flush 到 `tasks/<id>/summary_cursor.json`（不每轮写，崩溃 cursor 丢只是下次启动多扫一次）。onboarding 注入是 P5.3 的活 —— P5.2 阶段写文件、没人读。`Summarizer.maybe_summarize` 拆 `_collect_block` / `_summarize_block` 两个 hook 给子类化用。
  - **前端 UI**：composer chip 升级成下拉（列所有 status=open 任务 + "🗨 房间级" 选项；当前 active 高亮，点击 `set_active_task`）；任务面板加历史任务折叠列表（status != active 灰卡）+ status / owner 下拉 + "完成 / 放弃"快捷按钮；非 active 任务的消息气泡左上灰 chip 显示任务名，点击进 filter 视图（仅渲染 `task_id == X`）。composer 整体重做 ChatGPT/Gemini 风圆角卡片（顶部文件 card / 中段 textarea / 底栏附件 + 任务 chip + 圆形发送 ↑）。
  - **`server.py` 按 inbound feature 拆 handler 类**（同日 P5.2 重构起步）：`server.py` 2116 → 1022 行（-52%）。30+ 个 `_inbound_*` handler 切到 `server_inbound_{admin,task,io,settings}.py` 四个文件；模块顶共享小工具集中到 `_server_helpers.py`。组合而非多继承：`ChahuaServer.__init__` 实例化 `self.admin` / `self.io` / `self.settings` / `self.task` 四个 slot，handler 持 `self.server` 反向引用。`_INBOUND_ROUTES: dict[str, str]` 用属性路径（如 `"admin._inbound_add_guest"`），`__init__` 时 `operator.attrgetter` 一次解析成 `_inbound_handlers` bound-method 字典；dispatch 是单次 dict 查。`object.__new__(ChahuaServer)` 跳 `__init__` 的测试夹具需手工 `srv.task = TaskHandlers(srv)` 等装回 slot。
- **P5.1 任务房间 MVP**（2026-05-16，详见 [`docs/P5-任务房间.md`](docs/P5-任务房间.md)）：把房间从纯聊天容器升级为带任务的工作容器。**P5.1 严守"一房间最多 1 任务"窄路径**：开任务 → 消息打 tag → 拷贝产物 → 记决策；切换 / 关闭 / 多任务延后到 P5.2。
  - **核心模型**：`tasks/state.json`（`active_task_id` 覆写式 tmp+rename）+ `tasks/<id>/{task.json, decisions.jsonl, artifacts/}`。`transcript.jsonl` 每条多可选 `task_id` 字段（缺 = 房间级，向后兼容）。加载期双向修复 state.json↔task.json：state 指向不存在 → 清回 None；state 缺但只有一个 task.json → 自动设为 active 并回写。
  - **入站严格 / 落盘宽容**：inbound 白名单严格、未知键 NOTICE error 丢帧；jsonl 加载未知字段 warn 忽略、必需缺才跳条。两条规则有意不对称（保护数据不被前端瞎写 vs 保护跨版本向前兼容）。
  - **新 wire 帧**：上行 `open_task` / `update_task` / `attach_artifact` / `add_decision`；下行 `task_info`（权威快照，状态变更后重发整份）+ 4 个 hint（`task_open` / `task_update` / `task_decision_added` / `task_artifact_added`，给前端 flash 高亮，不进状态本身）。`schema_version` 不 bump —— 新 envelope.data.task_id 字段老前端忽略不报错。
  - **写权限永远在用户**：茶客通过协议提议（P5.3 起），UI 渲染成"采纳"按钮等用户点；P5.1 入口（"+ 新任务" 按钮 / 右键消息「📌 标为决策」/ pill 上 📎 拷贝 / `/task <title>` 斜杠命令）全是用户直接动作。
  - **`attach_artifact` 是 copy 不是 move**：`share/` 是房间公共桌面 + 历史消息 / 茶客 cwd 都引用，移动会断引用。茶客视角 `share/` 永远在原位。
  - **任务面板 UI**：右侧栏（折叠状态记 `localStorage('chahua.taskPanel.collapsed')`）；当前任务卡（title / goal 支持 inline 编辑，contenteditable + blur 触发 update_task）+ 产物列表 + 决策列表。composer 顶端 "📋 \<title\>" 静态 pill（chip），active 切换时跟着。`+ 新任务` 按钮在 `task_info.tasks.length > 0` 时 disabled（看任务存在性而非 active，防 state.json 丢失时悄悄允许开第二个）。
  - **新增模块**：`chahua/task.py` / `chahua/tasks_store.py`（后端）；`app/renderer/{task_state, task_panel}.js`（前端）。`chahua/events.py` 加 5 个新 `ChahuaEventType` + `new_id` / `now_ms` 公共 helper（task / decision / msg / turn 共用 mint 口径）。
  - **附带上传链路修缮**：`_UPLOAD_MAX_BYTES` 2MB → 200MB；`_upload_file` 恒发 `FILE_UPLOADED` envelope（成功 / 失败都发）让前端串行上传循环靠 echo 推进；renderer 端 `upload.js` 串行 for-of + pendingEchoes FIFO + dropPending；ws 断 / 切房时拒掉 await echo 让 finally 清 `isUploading`。
- **P4 专业茶客配置闭环**（2026-05-16，详见 [`docs/P4-专业茶客配置闭环.md`](docs/P4-专业茶客配置闭环.md)）：把 P1.5 全数硬拒到 P4 的 `room.toml` 字段一次落地。
  - `[room]` 编排参数 `want_threshold` / `max_consecutive_ai_turns` / `speaker_cooldown_turns` / `onboarding_threshold` 接入；改值热替不重建茶客。
  - `[scoring]` / `[summary]` / `[[guest]]` LLM 字段：`model = "<provider>/<model>"` 合并写法（OpenRouter / LiteLLM 二级路径保留）；all-or-nothing 校验（`base_url` / `api_key_env` 不能单独出现）；section 级 fallback 链 `[summary]` → `[scoring]` → 房间默认。每位茶客可独立配 LLM client（之前 P1.5 全茶客共用一个）。
  - `[[guest]].isolation = "room"|"global"` 决定茶客 cwd：`<room_dir>/guests/<name>/` vs `<user_data_root>/guests/<name>/`。切换不自动迁移 `.agentao/memory.db`（UI 切换前 confirm 提醒）。
  - `[[guest.extra_mcp_servers]]` 数组段：用户在自己 toml 里手写的 MCP server，**自动信任**（区别于 persona sidecar `mcp.json` 走 trust 门控）；同名时房间级覆盖 persona 同名。
  - UI：两个新的「详细设置…」modal（茶客 modal `guest_settings.js` + 房间 modal `room_settings.js`，共用 `llm_section_form.js` 工具），从 sidebar popover 入口打开。raw textarea 编辑器保留作 power-user 兜底。表单 diff 提交：单字段非法只回滚那一项。
  - 新 wire 帧：`update_room_orchestrator` / `update_room_llm` / `update_guest_llm` / `update_guest_isolation` / `update_guest_extra_mcp`。
  - `room_info` envelope 扩展：拆 `persona_mcp_servers` / `room_mcp_servers` / `effective_mcp_names`（合并后实际装载顺序）；每 guest + 房间级 `llm` / `scoring_llm` / `summary_llm` / `room_default_llm` 摘要（含 `api_key_env` 名 + `api_key_ready` bool；**API key 本身永不下发**）。
  - 新增模块：`chahua/llm_spec.py`（`LLMSpec` / `from_env` / `from_toml` / `build_client` / `split_model_id`）；`app/renderer/{guest_settings,room_settings,llm_section_form}.js`。
  - **P4.8 LLM section 加 `temperature` 字段**（2026-05-16）：`[scoring]` / `[summary]` / `[[guest]]` 三段都接入。`from_toml` all-or-nothing 同步扩到 temperature；越界 `[0, 2]` / bool / 非数值解析期就拒；spec 持原始 `None` 表示"继承默认"（`LLM_TEMPERATURE` env / `_DEFAULT_TEMPERATURE`）。admin emit 走 scalar literal（数值不引号），`_render_llm_field` 按 `LLM_TOML_NUMERIC_FIELDS` 分流。`room_info.llm.temperature` 下发原始值。UI 表单复用 `createLlmSection`，三处 modal 各加一个 number input；客户端预检 0~2 避免来回 echo。顺手把阿里云 DashScope (qwen) 接入 `_DEFAULT_BASE_URLS`。
  - **P4.9 `[room.llm]` 房间默认模型**（2026-05-16）：之前房间默认只能改 `LLM_PROVIDER` env，桌面 App 用户得开终端动 .env。现在 toml 里加 `[room.llm]` dotted table（与 `[scoring]` / `[summary]` 同套 LLMSpec schema），房间详细设置 modal 顶部一站编辑。fallback 改三层：`[room.llm]` → env → `RoomConfigError`（错误信息同时列两条 fix 路径）。`[room.llm]` 显式配置时 env 完全被忽略，避免 shell `LLM_TEMPERATURE` 偷胜覆盖 toml 意图。`LLMSpec.try_from_env()` 新增「缺 model 返 None」版本；`from_env()` 委托给它 + None 时抛 SystemExit 保留旧调用方语义。`admin.update_room_llm(section="room")` 与 inbound 同入口扩展；`room_info.room_default_llm.source = "room"` 表示 toml 有写 / `"default"` 表示 env 推断。
- **双击 popover + raw room.toml 编辑器**（2026-05-15）：sidebar 删「清空聊天 / 编辑配置 / 换头像」三个按钮；双击房间名弹「更改房间配置 / 清空聊天」、双击用户头像/显示名弹「编辑配置 / 换头像」。「更改房间配置」是新功能：textarea 直接编辑当前房间 `room.toml`，服务端 `load_room_config` 校验失败回滚旧字节 + emit notice。新增 wire 帧上行 `update_room_toml`；`room_info` envelope 携带 `room_toml_content` / `room_toml_source` 给 modal prefill。新增 `admin.update_room_toml(room_dir, content, *, paths)`。详见 `docs/DESIGN.md` §3.11。
- **persona MCP & skills 装载 + MCP 信任门**（2026-05-15）：persona 目录可放 sibling `mcp.json`（MCP servers）和 `skills/`（agentao skills 目录）。skills 启动时通过 `_fs.link_dir_idempotent` 软链到茶客 `working_directory`；MCP 因为带来「任意可执行」风险**必须用户在 popover 上显式勾选「信任此 persona 的 MCP」才装载**。信任记录跨房间持久化在 `user_data_root/.chahua/persona-trust.json`。room_info envelope 在每位 guest 上附 `mcp_servers`（command + args 摘要）/ `mcp_trusted` / `skills_available`；新 inbound `set_persona_mcp_trust`。新增模块：`chahua/persona_assets.py` / `chahua/trust.py`。
- **「话题讨论你」打分加档**（2026-05-15）：scoring 阶段检测 transcript 里对**第三方**茶客的提及（非 `@`，是文本里"宝总你怎么看"这种），给被提及的茶客 score 一个额外加成档。让相关茶客更主动接话；@ 直接路由的确定性路径不受影响。
- **persona elonmusk + Yvonne / 唐三藏 精修**（2026-05-15）：新增 `examples/personas/elonmusk/`（含头像 / prompt / sibling toml）；唐三藏 / Yvonne prompt 重写。persona picker 现在读 sibling `<Name>.toml` 里的 `[guest].name` —— 目录叫 `Yvonne` 但显示「伊冯」之类的场景生效。
- **房间共享文件**：每间房 `rooms/<room>/share/` 为共享目录，每位茶客的 `<guest_workdir>/share` 软链到此（Windows 无符号链接权限时降级为 WARN）。Composer 左侧附件按钮支持多文件上传，文件随下一条用户消息以 `<./share/xxx>` 形式进入 transcript 与茶客上下文。单文件 2MB 上限；文件名经 `sanitize_fs_name` 洗过，traversal / 绝对路径双向拒绝；详见 `docs/DESIGN.md` §3.10。
- **多行输入**：composer 由 `<input>` 切 `<textarea>`，Enter 发送、Shift+Enter 换行，textarea 内容增长自适应高度到 200px 切滚动；IME composition / `@` 补全 dropdown 行为不变。
- 新增 wire 帧：上行 `upload_file`（`filename` + `content_b64`），下行 `file_uploaded`（`rel` + `name` + `size` + `original`）；`user_message` 新增可选 `files: [str]` 字段。
- `chahua._fs.link_dir_idempotent`：共享的 symlink 幂等 helper（房间 share / persona skills 共用，单点处理 broken symlink + Windows 无权限边角）。

### Changed
- **scoring / summarizer `_MAX_TOKENS` 抬高**（2026-05-15）：scoring 128 → 1024，summarizer 512 → 2048。Gemini 2.5 Flash 系列把 thinking budget 也算进 `max_tokens`，旧值会让 visible output 在 `{"score":` 处被 length 截断（Finish Reason: length, Completion Tokens: 4）。对 OpenAI / Claude / DeepSeek 等非 thinking 模型不增加实际 token 消耗。
- **popover 重构**（2026-05-15）：抽出 `positionPopoverByAnchor` + `attachPopoverDismissHandlers`，permission popover 与新的 action popover 共用；CSS 改 `.popover` 底座 + `.permission-popover` / `.action-popover` 修饰类，去除 `className = "action-popover permission-popover"` 双类样式借用。
- `_materialize`（persona skills）改用 `_fs.link_dir_idempotent` + 失败 copytree 兜底，去重约 25 行边角处理代码。

### Fixed
- **`@` 含空格的茶客名路由**（2026-05-15）：`@唐 三藏` 这种含空格的名字之前会被 mention 正则切断到第一个空格；修正口径以 guests 名册做最长前缀匹配。

---

首次公开发布。详见 [`docs/releases/v0.1.0.md`](docs/releases/v0.1.0.md)。

### Added
- 多 Agent 群聊核心：意愿打分调度 + 三茶客「黄河路」默认房（宝总 / 汪小姐 / 范总）+ `@` 补全 + `@broadcast`
- agentao 集成：每个茶客独立 `working_directory` / memory / sessions
- CLI 模式 `uv run chahua`（REPL + `/info` + `/quit`）
- Electron 桌面壳：sidecar + WebSocket + 流式打字机 + markdown 渲染（gfm + DOMPurify）+ 气泡复制
- 茶客侧栏：头像 + permission V 标 + 打分徽章
- 房间能力：进 Room 显示历史 / 切换房间 / 清空聊天 / 停止当前 turn / 断线退避重连
- 持久化：`transcript.jsonl` / `summary.jsonl` / `cursor.json` / 茶客私有 `.agentao/memory.db`
- 首启动 seed `app/templates/` → userData（dev 跳过；packaged 写 `.chahua-seeded` marker）
- macOS 打包：`npm run build:python` + `npm run build:mac` 出未签名 `.dmg`（~130MB，内嵌 python + chahua + agentao）
- 跨平台 graceful shutdown：stdin EOF 替 SIGINT
- sidecar 日志落盘到 `~/Library/Logs/chahua/`
- LICENSE (MIT) + `docs/INTRODUCTION.md`

### Changed
- agentao 依赖从本地 path-editable → PyPI `agentao>=0.4.6`
- 聊天布局换气泡（茶客左 / 用户右镜像）；打分徽章从主聊天挪到 sidebar 茶客名右侧
- 缺省茶室切到 `p3-黄河路`；宝总改 full-access
- 拆 `app_root` / `user_data_root`
- 意愿打分 `want_threshold` 0.55 → 0.45

### Fixed
- orchestrator 中途 pick None 后「停止」按钮卡死
