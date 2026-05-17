# Changelog

格式参考 [Keep a Changelog](https://keepachangelog.com/)；版本号遵循 [SemVer](https://semver.org/)。
完整发布说明见 [`docs/releases/`](docs/releases/)。

## [Unreleased]

### Added
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
