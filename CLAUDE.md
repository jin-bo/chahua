# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目语义

「茶话室」(chahua) = 多 Agent 群聊桌面 App。用户和多个由 [agentao](https://github.com/jin-bo/agentao) 驱动的 AI「茶客」在同一聊天室对话（像微信群，可 `@`、茶客之间也能接话）。房间不只是聊天容器 —— 还是带任务的工作容器（**P5 任务房间**：开任务 / 茶客 propose 决策待用户采纳 / 茶客直接写 `./task/<name>` 自动归集为 artifact / 多任务共存单时刻 1 active）+ 自带取证视图（**P6 调试与回放**：每一轮可展开看候选 / 分数 / prompt / 工具 / 产物，落盘到 `rooms/<id>/debug/` 跨重启可翻）。

**两个运行形态共享同一套 Python 后端**：
- CLI REPL：`uv run chahua`（最快验证 LLM 凭据 / `room.toml`）
- Electron 桌面壳：`cd app && npm run dev` —— main 进程拉起 `chahua-server` sidecar，本地 WebSocket 通信

两条路径都走 `chahua.session.build_room_session()` 装配房间，bring-up 不重复。

## 常用命令

```bash
# Python 依赖（uv 按 pyproject.toml 从 PyPI 拉 agentao≥0.4.6）
uv sync

# CLI 跑（默认入 rooms/p1-test）
uv run chahua
uv run chahua --room rooms/p3-黄河路

# 独跑 sidecar（便于在 Electron 外抓 server 日志）
uv run chahua-server --host 127.0.0.1 --port 7860 --room rooms/p3-黄河路

# 测试
uv run pytest                                # 全部
uv run pytest tests/test_orchestrator_cancel_during_scoring.py -k cancel  # 单测

# Electron dev（首次需 npm install）
cd app && npm run dev

# 打包（仅 macOS 已实测；Windows 接缝在但需 Windows 主机）
cd app
npm run build:python      # 拉 python-build-standalone + 装 chahua/agentao 到 python-bundle/
npm run build:mac         # → app/dist/茶话室-<ver>-mac-arm64.dmg
FORCE=1 npm run build:python   # 强制清重建 python-bundle
```

`pyproject.toml` 设 `asyncio_mode = "auto"` —— 所有 `async def` 测试无需 `@pytest.mark.asyncio`。

## 架构要点

### 双层进程 / 双层路径

```
Electron main (Node)  ─ spawn ─→  chahua-server (Python sidecar)
       │                                │
       └─ stdio: ["pipe", ...]           └─ WebSocket 127.0.0.1:<random-free-port>
       └─ before-quit → stop()           └─ stdin EOF watcher → graceful 关停
```

- **app_root vs user_data_root**：dev 模式两根同源仓库根；packaged 模式 app_root=`.app/Contents/Resources`、user_data_root=`~/Library/Application Support/chahua/`。Electron main export `CHAHUA_APP_ROOT` / `CHAHUA_USER_DATA` 给 sidecar，Python 端 `chahua._paths.Paths.from_env()` 接住。**改路径解析必须双根都顾**（persona 走 `find_in_data_then_app`：user_data 优先，回退到 app）。
- **sidecar ready 信号是一行 print**：`server.py` 起来后打 `监听 ws://...`，`app/main/sidecar.js` 的正则 `/监听\s+ws:\/\//` 匹配后才 resolve。措辞改动两边要同步。
- **首启动 seed**：dev 跳过；packaged 把 `app/templates/` 拷到 user_data_root，写 `.chahua-seeded` 幂等。

### Python 后端模块分工

- `session.py` — 装房间。CLI 与 server 共用 `build_room_session()` / `discover_rooms()` / `load_env_files()`。
- `config.py` — `room.toml` 解析。**严格只支持白名单字段**，未知字段直接 `RoomConfigError`（错配置宁可炸，避免静默吞 typo）。P4 后接入的字段：`[room]` 编排参数 / `[room.llm]` 房间默认（P4.9）/ `[scoring]` / `[summary]` / `[[guest]].{model,base_url,api_key_env,temperature,isolation}` / `[[guest.extra_mcp_servers]]`。
- `llm_spec.py` — `LLMSpec` 数据类（`provider/model/base_url?/api_key_env?/temperature?`）+ `from_env()` / `try_from_env()` / `from_toml(label=...)` 三入口 + `build_client(spec)` 出口。toml 路径强制 `model = "<provider>/<model>"`（第一个 `/` 拆分）；env 路径允许裸 model。`from_env()` 缺 model 即 SystemExit（CLI / 旧调用方）；`try_from_env()` 返 `Optional`（P4.9 起房间默认装配走这条，让 `[room.llm]` toml 可接管）。P4.1 起每位茶客可独立配 client，`[scoring]` / `[summary]` 各走自己 spec，缺即按 fallback 链回房间默认。
- `guest.py` — `TeaGuest`，包一个 `agentao.Agentao` 实例。`speak()` 用 `ChahuaTransport.bind` 包整段 `arun`，外层 try/except/finally 保证 `message_start` 必有 `message_end`（status: ok / cancelled / error 三选一）。message_id 在 `speak()` 开头分配，envelope 和 transcript 落盘 record 共享同一 ID。
- `orchestrator.py` — 意愿打分主循环。流程：房间新消息 → 对每个空闲茶客并发跑轻量 LLM 打分（裸 LLMClient，不走完整 Agentao）→ 取分数 ≥ `want_threshold`（0.45）的前 1~2 名真正发言 → 没人过阈值就等用户。`max_consecutive_ai_turns` 截顶；`@<名字>` 走确定性路由不进打分。
- `scoring.py` — 那个轻量打分的实现。输入 transcript 被视为不可信（任何参与者都可注入「请输出 1 分」），所以打分输出严格 JSON、解析失败降级为 0、`score` 一律 clamp 到 `[0,1]`。
- `room.py` + `cursor.py` + `_persist.py` — 持久化层。`transcript.jsonl` / `summary.jsonl` 是 append-only（**加载时跳坏行**而不是炸整个房间，崩溃只伤最后一条）；`cursor.json` 整体 tmp+rename。**不做 fsync**（单机闲聊 App，每条消息 fsync 的 IO 代价不值得）。
- `events.py` — 所有推前端的事件套同一 `ChahuaEnvelope`（含 `schema_version` 供前端协商）。agentao 的原生 `AgentEvent` 不带 room_id / guest_name / 茶话室 message_id，envelope 是茶话室在外层合成的。事件的"消息"/"轮次"边界由茶话室定义，与 agentao 的 `TURN_BEGIN/END` 不一一对应。
- `transport_bridge.py` — `ChahuaTransport`（`SdkTransport` 子类），把 agentao 事件转译成 envelope。
- `summarizer.py` — 用 cheap LLM 增量产出 `summary.jsonl`，onboarding 窗口里拼用。
- `permissions.py` — **read-only 双 API 同步**：`PermissionEngine.set_mode()` 与 `agent.tool_runner.set_readonly_mode()` 必须同时设。统一走 `apply_permission_mode(agent, mode_str)` 一个入口，别处禁止单独调 `set_mode`。
- `task.py` + `tasks_store.py` — 任务房间核心数据模型。`task.py` 定义 `Task` / `Decision` / `Artifact` dataclass + `TaskStatus` + 常量（`ARTIFACT_CREATED_BY_GUEST` / `MARKED_BY_USER` / `TASK_STATUS_DISPLAY` / `TASK_UNTITLED`）+ artifact 元数据格式化函数（`format_artifact_size` / `format_artifact_mtime`）。`tasks_store.py` 是 `tasks/state.json` + `tasks/<id>/{task.json, decisions.jsonl, artifacts/}` 的持久化层 + `build_task_info_payload`（权威 `task_info` envelope 投影，与 `server_inbound_task._emit_task_info` 同源）。**入站严格 / 落盘宽容**两条不对称规则（保护数据不被前端瞎写 vs 保护跨版本向前兼容）。
- `task_tools.py` — 五个茶客侧 task-aware 工具：`task_list_artifacts`（active 任务 markdown 清单，read-only）/ `task_propose_decision` / `task_propose_open` / `task_propose_status`（P8.2，提议改任务状态，取值是全部状态去掉创建态 `open`；非法 status 返 `Error:` 不 emit）（这三个 propose 工具 `is_read_only=True` 但**会 emit `TASK_PROPOSAL` envelope** —— 为权限层放行 ≠ 无副作用；提议触发前端"采纳 / 忽略"卡片，写权限仍永远在用户）/ `task_write_artifact`（绕开 agentao `PathPolicy` 让茶客 `./task/<name>` 落盘的官方通道，`is_read_only=False` 让 read-only 模式拦住；name 校验拒 `/` / `\\` / `..` / 前缀 `.`）。
- `handoff_tools.py` — 三个茶客侧 handoff propose 工具（P7.4）：`propose_delegate` / `propose_review` / `propose_panel`，均 `is_read_only=True` 但**会 emit `TASK_PROPOSAL` envelope**（为权限层放行 ≠ 无副作用，同 `task_propose_*` 口径）。共享 `_ProposeHandoffBase`；`register_handoff_tools(agent, *, transport, room)` 工厂。**不带 `task_` 前缀**——handoff 是房间级调度、非任务域。`propose_review` 用注入的 `room` 把 `reviewee` 名解析成最近一条发言的 `message_id`。采纳走前端 `proposal_card.js` 拼回既有 `handoff_*` inbound，server 零改动。
- `task_rendering.py` — task block prompt 渲染纯函数：`render_task_header` / `render_scoring_header` / `render_task_block(compact|full)` —— 不接 self / 不读 store / 不背状态分支，取数（`get_task` / `list_decisions` / `list_artifacts` / `task_summaries.get`）由调用方 `_build_context_for` 完成；scoring 与 speak compact 在 goal 上口径有意不同（完整 goal vs 首行）。
- `context_renderer.py` + `artifact_detector.py` — `context_renderer.py` 输出固定 6+1 块 XML 结构的 `_render_onboarding` / `_render_incremental`（`<room>` / `<user_persona>` / `<room_summary>` / `<current_task>` / `<order_hint>` / `<recent_messages>` / `<speak_instruction>`），`quoteattr` 防 XML 属性注入；`_SPEAK_ORDER_HINT_BLOCK` 与 scoring 阶段的 `_ORDER_HINT_BLOCK` 措辞不同不能合并。`artifact_detector.py` 扫 `tasks/<active>/artifacts/` diff `_seen_artifacts[task_id]` —— 每个 pick 周期末尾跑、emit `task_artifact_added` hint + 重发 `task_info` 权威快照；只 seed 非 closed 任务（防 boot 旧 artifact 当新增）。
- `persona_summary.py` — P8.2-roster-b：persona 能力摘要 LLM 生成 + 中央内容寻址缓存（`user_data_root/.chahua/persona-summaries/<hash>.json`，`hash` = persona md sha256、值含 `gen_version`）。`resolve_guest_summary` 三级解析（手写 `[[guest]].summary` → 缓存 → `None`）；`schedule_generation` 后台预热（`get_running_loop()` 守卫，无 loop 跳过）。所有失败 WARN 不阻断房间。`clamp_summary`（一行 ≤40 字）被 `context_renderer.py` `<room>` 花名册渲染共用。
- `debug_recorder.py` — P6 取证落盘核心。`TurnRecorder.start_turn` / `record_scoring` / `record_message_start` / `flush_turn` 等 hook 由 orchestrator / guest / transport_bridge 三处串行喂；落 `debug/turns.jsonl` 行 + `debug/prompts/<turn_id>/*.txt`。`max_turns = 500` 默认 rotation 按 turn_id 整组删（jsonl 行 + prompts 子目录最小事务），触发点固定 `__init__` 末尾（boot 兜底）+ `flush_turn` 后；`_turn_count` 内存维护永不重测盘。所有 IO try/except + WARN，**rotation 失败永远不阻断房间运行**。`SCORING_PATH_{SCORING,MENTION,BROADCAST}` 三常量 + `VALID_SCORING_PATHS` 白名单。`clear()` 单点擦 `debug/turns.jsonl` + `debug/prompts/` 整子树（`server._clear_room` 同步调）。
- `server_room_snapshot.py` — `emit_room_snapshot` 单点装配房间快照：`emit_room_info` / `emit_room_history`（含 P6.3.A 严格 `enabled=True` 才挂的 `turns_index`，倒序 ≤ `TURNS_INDEX_HARD_CAP=1000`）/ `_emit_task_info` / 末尾补发 MTS 快照（P9 9.3.2，前台房 `_managed_session` 非空时）。`turns_index` 与 piggyback 字段同口径——关闭 debug 时字段缺省（不下发空数组）。`emit_room_info` 的 `rooms_available` 每房带 `busy` 标志（P9 9.3.2，`_rooms_available_with_busy`，`room_id ∈ _runtimes 且 inflight_alive()`）。
- `server_entry.py` — CLI 入口分离（`chahua-server` 命令）。argparse / 端口分配 / `ChahuaServer.serve_forever` 拉起 + stdin EOF watcher 装配。`server.py` 本体只关心 ws 生命周期 / 帧路由 / room snapshot。
- `admin.py` + `admin_{guest,persona,room,user}.py` + `admin_toml.py` — admin 业务层按域拆分（**P5/P6 重构**，admin.py 从 812 行删减回归核心）：`admin_guest.py`（添删改茶客 + LLM 字段 update）/ `admin_persona.py`（persona MCP trust / skills 装载）/ `admin_room.py`（房间创建 / 切房 / `update_room_llm` / `update_room_toml`）/ `admin_user.py`（USER.md / 头像）/ `admin_toml.py`（`_render_room_toml` 结构化重写，与 `_debug_config_to_dict` 等 snapshot 函数配合）。
- `persona_assets.py` + `trust.py` — persona 目录 sibling `mcp.json` + `skills/` 装载，MCP 因「任意可执行」走信任门：`trust.py` 持久化 `user_data_root/.chahua/persona-trust.json`，UI popover 勾选触发 `set_persona_mcp_trust` inbound。skills 走 `_fs.link_dir_idempotent` 软链到茶客 `working_directory`，失败 copytree 兜底。
- `server.py` + `server_inbound_{admin,task,io,settings,handoff}.py` + `_server_helpers.py` — ws 生命周期 / 帧路由 / room snapshot 留在 `server.py`；30+ 个 `_inbound_*` 帧 handler 按 feature 切到 5 个 handler 类（P5.2 重构 4 个，P7 inbound 拆模块时加 handoff）。`ChahuaServer.__init__` 实例化 `self.admin = AdminHandlers(self)` / `self.io = IOHandlers(self)` / `self.settings = SettingsHandlers(self)` / `self.task = TaskHandlers(self)` / `self.handoff = HandoffHandlers(self)` 五个 slot —— **组合而非多继承**，每个 handler 持 `self.server` 反向引用，跨 server 状态（`_session` / `_emit_notice` / `_replace_session` 等）走 `self.server.xxx` 显式 hop。slot 装配唯一真理源是 `_install_handler_slots(srv)`，加 slot 一处加完。`_INBOUND_ROUTES` 帧字符串 → 属性路径（如 `"admin._inbound_add_guest"` / `"_inbound_cancel"`）映射只在 `server.py` 顶层维护；`__init__` 时 `_bind_inbound_handlers(self)` 走 `operator.attrgetter` 一次性解析成 `self._inbound_handlers` bound-method 字典，dispatch 是单次 dict 查；属性路径无 `.` 表示 method 在 `ChahuaServer` 自身（cancel / switch_room / clear_room / user_message 4 个核心，加 fetch_turn_detail / list_guest_caps）。**改动 `_inbound_*` handler 时先看 slot 归属**：admin（guest/room/persona/permission）/ task（open/update/attach/decision + emit_task_*）/ io（upload/export/persona import）/ settings（USER.md / 头像 / room.toml）/ handoff（delegate/review/panel/clear + P8.3 托管会话 `managed_session_start`/`stop` + 调度尾段 `_enqueue_handoff_and_maybe_start`）。`server_inbound_handoff.py` 还持 `INBOUND_HANDOFF_*` / `INBOUND_MANAGED_SESSION_*` / `INFLIGHT_KIND_*` 常量，`server.py` 顶 import 再导出（旧 `from chahua.server import …` 路径不变）。模块顶 payload 校验小工具 `require_str` / `check_keys_whitelist` 等在 `_server_helpers.py`，所有 handler 模块 import 自这里（避免 handler ←→ server.py 循环 import）。**测试夹具**：用 `object.__new__(ChahuaServer)` 跳 `__init__` 时必须手工把用到的 slot 装回去 —— 优先调 `_install_handler_slots(srv)`，或 `srv.task = TaskHandlers(srv)` 等。

### WebSocket 线协议（`chahua/server.py`）

下行：每帧一条 JSON = `ChahuaEnvelope.to_dict()`。

上行：每帧一条 JSON，识别四种 `type`：
- `user_message`：`{"type": "user_message", "text": "..."}`
- `cancel`：取消当前在跑的 turn task
- `switch_room`：切换房间（在上一条 user_message 的 submit 之后才到达，单客户端串行 inbound 循环保证）
- `clear_room`：清空当前房间历史

未知 `type` WARN 后忽略（前端协议升级期发未来 type 不会被踢线）。非 JSON / 二进制帧 → `close(UNSUPPORTED_DATA)`。

### 跨平台 sidecar 关停

`app/main/sidecar.js` 的 `stop()`：先 `child.stdin.end()` 让 Python 端 `_watch_stdin_eof` 收 EOF 走 graceful；2s grace 兜底 `forceKillTree(child)` —— Windows 走 `taskkill /F /T /PID`（杀整棵树，因为 dev 链路是 `electron → uv.exe → python.exe`，SIGKILL 只杀直接子会留孤儿），POSIX 走 `SIGKILL`。改这块要意识到 Windows `connect_read_pipe(sys.stdin)` 在 ProactorEventLoop 上经常拿 `WinError 6` 静默失败，graceful 路径不可靠，tree-kill 是兜底正解。

## 关键不变量

承重契约，编辑代码必须遵守。本节只留「规则 + 关键 why」；**完整 rationale / P-版本溯源 / 实现细节见 `docs/DESIGN.md` §9**，改不变量时两处同步。

### 配置与 LLM 装配

- **`room.toml` 未知字段必报错**。配错宁可炸，否则 typo 静默吞掉查不到。
- **`[room].name` 跨房唯一**。envelope 顶层 `room_id` 用 `room.name` 占位，P9 前端据它分流前台/后台房间里程碑——同名会让后台房的 `message_end`/`task_info` 污染当前前台房。`admin.create_room` 与 `admin.update_room_toml`（raw 编辑）两入口都拒重名（`_other_room_names` 扫 `rooms/*/room.toml`）。手改 toml 绕过校验属「配错」。
- **toml `model` 值形如 `<provider>/<model>`**。首个 `/` 拆 provider，二级路径（`openrouter/qwen/qwen3-coder`）保留进 model；无 `/` → `RoomConfigError`。CLI / dotenv 例外允许裸 model。
- **LLM section 整段写或整段不写**。`[room.llm]` / `[scoring]` / `[summary]` / `[[guest]]` 四件套 all-or-nothing：`base_url` / `api_key_env` / `temperature` 不能脱离 `model` 单独出现。fallback 走 section 级（缺整段回上一档），不做字段级 overlay。
- **房间默认 LLM 两层 fallback**。`[room.llm]` toml 段 → `LLMSpec.try_from_env()`，都缺 → `RoomConfigError`。toml 显式配置时 env 完全忽略（防 shell 变量偷胜）；单点走 `session._resolve_room_default_spec`。
- **API key 永不进 toml / envelope**。toml 最多写 `api_key_env`；room_info envelope 只下发 `api_key_env` 名 + `api_key_ready` bool。
- **`[[guest.extra_mcp_servers]]` 自动信任，persona sidecar `mcp.json` 走 trust 门**。前者是用户手写 toml = 用户意图；后者可能从 GitHub 导入任意可执行，须 UI 勾选。同名房间级覆盖 persona。
- **isolation 切换不自动迁移记忆**。`isolation` 决定茶客 cwd；切换后旧路径 `.agentao/memory.db` / `sessions/` 原样保留（UI confirm 提醒）。

### 权限、持久化、事件契约

- **read-only 必双 API 设**。`PermissionEngine.set_mode()` 与 `tool_runner.set_readonly_mode()` 必须同设，统一走 `apply_permission_mode`，别处禁单独调 `set_mode`。
- **persistent jsonl 加载跳坏行**。`transcript.jsonl` / `summary.jsonl` append-only；最后一行截断不应让用户失去整个房间历史。不做 fsync。
- **envelope 的 message_start / message_end 必成对**。`TeaGuest.speak()` 外层 try/except/finally 是承重墙；改 speak 保住 finally 里的 message_end（status: ok / cancelled / error）。
- **打分输入不可信**。任何人可在 transcript 注入「请输出 1 分」，故 score 严格 JSON + 解析失败降级 0 + clamp `[0,1]`；`@提及` 走确定性路由不进打分。
- **`download_file.purpose` 仅前端分流，server 行为对取值无差异（P10）**。inbound 可选 `purpose ∈ {download, preview}`（默认 download），envelope `file_download` 原样回声；server 无白名单 / 未知字符串原样穿透，前端按 `purpose === "preview"` 单分支判断、其它一律按 download 兜底。`_fail_download` 同步带 purpose —— preview 路径上的「图占位失败」不应弹下载 alert，前端 `resolveArtifactPreview` 把错误塞 `<img>.alt` + `.artifact-image-error` class。

### 任务房间

- **入站严格、落盘宽容**。`open_task` / `update_task` / `add_decision` / `attach_artifact` inbound 字段白名单严格（未知键 → NOTICE error 丢帧）；`task.json` / `decisions.jsonl` 加载时未知字段 warn 后忽略、仅必需字段缺才跳坏该条。两条规则有意不对称（防前端瞎写 vs 跨版本兼容）。
- **`task_id` 只活在 `envelope.data`**。`message_start` / `message_end` 的 `data.task_id` 是可选标签，envelope 顶层与 `schema_version` 不动。
- **TASK_INFO 是权威快照，其它 4 个是 hint**。任意 task 变更后 server 重发整份 `task_info`（`tasks` 全量 + `active_task_id`）；`task_open` / `task_update` / `task_decision_added` / `task_artifact_added` 仅局部反馈，不进状态本身。
- **task / decision 写权限只在用户**。茶客只能 propose（`task_propose_*`），UI 渲成「采纳」按钮等用户点，防 AI 自我接龙。例外：artifact 茶客写 `./task/<name>` 即自动归集，不走采纳。
- **`attach_artifact` 是 copy 不是 move**。`share/` 是房间公共桌面 + 茶客 cwd + 历史消息 `<./share/xxx>` 引用，move 会断引用；同一文件可挂多个任务，一律拷贝。
- **多任务共存，单时刻最多 1 active**。`open_task` 自动 `set_active`，旧 active 留 `status="open"`；`set_active_task` 走前强制 `_cancel_and_drain_inflight`。state.json 缺 + 多 task.json → 不自动选，emit NOTICE 让 UI 选。
- **通道 2 软链：茶客 `./task/` 跟 active task**。`build_room_session` 末尾 + open/set_active/close 三处刷新茶客 `working_directory/task` 软链到 `tasks/<active>/artifacts/`；active=None 只解不建。Windows 走 junction。失败 WARN 不阻断。
- **任务级 summary.jsonl + cursor 落盘 only**。`TaskSummaries` 在 `_kick_summarize` 末尾每任务跑一次 `maybe_summarize`；cursor 内存推进、`session.close()` 统一 flush（丢了下次多扫，与 transcript 不 fsync 同口径）。
- **四个 read-only task tool 均 `is_read_only=True`**。`task_list_artifacts` 真无副作用；`task_propose_decision` / `task_propose_open` / `task_propose_status` 不写库不落盘但**会 emit `TASK_PROPOSAL` envelope** —— `is_read_only=True` 是为权限层放行、不是「无副作用」声明。
- **`task_propose_status` 采纳按终结态分流**。非终结态（`ready`/`doing`/`blocked`/`review`）→ `update_task {patch:{status}}`；终结态（`done`/`abandoned`）→ `close_task {status}`（入站校验拒 `update_task` 带终结态）。提议取值 = 全部状态 − `{open}`；`reason` 是 propose-only 不进 inbound。
- **propose 不写库，采纳才入库**。`TASK_PROPOSAL` envelope 仅触发前端「采纳/忽略」卡片；采纳由 `proposal_card.js::buildAcceptInbound` 按 `kind` 拼回既有 inbound，server handler 零改动。卡片 session-local，刷新即清；去重指纹切房/clear 时 reset。
- **task 事件是 UI 系统气泡，不进 transcript / 不触发 AI**。`open_task` / `close_task` / `add_decision` / `attach_artifact` / `clear_task_artifacts` + 茶客自动归集只 emit `TASK_*` hint + `TASK_INFO`，不合成 user 消息、不起 `_run_turn`；前端渲居中系统气泡，刷新/切房不重建。代价：task 操作不再唤醒房间，要茶客介入用 `@提及` / handoff / 用户消息。文案唯一来源是前端 `formatTaskEventNotice`。
- **`./task/` 读用 `read_file`、写走 `task_write_artifact` 工具**。agentao `PathPolicy.contain_file` 跟随 symlink 解析后 `./task/` 不在茶客 workdir 之下 → 原生 `write_file` 被拒；`task_write_artifact` 直接调 `tasks_store.artifacts_dir` 绕开 path_policy，name 校验拒 `/` `\` `..` 前缀 `.`。`is_read_only=False`（read-only 模式应拦住）。
- **新文件感知 + 用户上传须 `mark_seen`**。`_kick_detect_new_artifacts` 每 pick 周期末尾扫 `tasks/<active>/artifacts/` diff `_seen_artifacts`，emit `task_artifact_added` hint + `task_info`；`_seen_artifacts` 只 seed 非 closed 任务。用户 UI `attach_artifact` 走另一路径，拷文件后**必须调 `ArtifactDetector.mark_seen(task_id, name)`**（增量 add，禁整组覆盖），否则下轮 `detect()` 把用户上传当茶客新产物重发气泡。
- **`/clear task` 仅清任务产物，作用域严格**。只删 `tasks/<task_id>/artifacts/` 可见文件，`task.json` / 决策 / 状态 / 摘要游标都不动。副作用顺序：`clear_artifacts` 删盘 + `events.jsonl` audit 行 → `ArtifactDetector.forget(task_id)` → emit `task_artifacts_cleared` hint → emit `task_info`。写权限只在用户，茶客无 propose 入口。
- **`message_artifacts.jsonl` 与 transcript 同生命周期（P10）**。`MessageArtifactRegistry` per-room 落 `rooms/<id>/message_artifacts.jsonl`；`reset_room` 同步 clear。`/clear task` **不**清注册表（artifact 文件被删但 message_id 仍真实存在于 transcript，挂件视觉无害）。加载跳坏行 + 严格 `isinstance(str)` 校验 mid/name（`str(None)` == "None" 不可静默当合法 mid）+ 显式拒 bool size（`int(True)==1` 不抛错）+ rel 必落 `share/` 或 `tasks/<id>/artifacts/` 已知 root，**两 root 都做空段 / `.` / `..` segment 校验**——share/ 形态与 `_normalize_share_rel` 对称，否则 `share/../../foo` 可经 `room_dir / r.rel` 解析逃出 share/。
- **`task_artifact_added.data.originated_message_id` 仅由 `task_write_artifact` 工具调用路径派生（P10）**。`transport_bridge._maybe_record_artifact_path` 是唯一写入点；`ArtifactDetector.detect` 是唯一消费点（`consume_pending` 取出 + 持久化）。用户上传 / 跨周期遗留 / 茶客绕开预期工具 → 字段缺省，前端系统气泡兜底。`originated_message_id` 是可选字段，envelope schema 不动，`schema_version` 不 bump；缺省时旧前端忽略未知字段仍渲老系统气泡。`emit_room_history` 按 message_id 查 registry，空 → 不写 `originated_artifacts`（同 `task_id is None` 不写键口径）。
- **shell / MCP 工具走 TOOL_START / TOOL_COMPLETE 前后 diff 回填 pending（P10）**。`task_write_artifact` / `write_file` / `replace` 三个 args-known 写盘工具直接经 `_maybe_record_artifact_path` 落 pending；shell / MCP 类工具靠两次扫盘 diff 把新增 / 真重写文件回填 pending。`_tool_call_snapshots` entry 捕获 message_id / task_id —— 即便晚到 TOOL_COMPLETE 时 bind 已退出，仍能用捕获值完成 record_pending 不丢归属。**滚动 baseline 仅在 bind 内更新**：`_consume_tool_diff` 若发现 `self._message_id is None`（bind 已退），不再写 `_diff_baseline`，否则两次 bind 之间凭空出现的文件会被下次首个 shell/MCP TOOL_START 错算成下条消息的新增。失败 TOOL_COMPLETE（`status != "ok"`）走 `_rollback_pre_pending`，回滚 args-known 路径在 TOOL_START 预落的 pending。

### context 渲染与 prompt 装配

- **`format_messages` 每条消息走 `<message>` 包裹**。`room.py::format_messages` 单点定义，仅外层包 XML、内层 `{display} 说：{text}` 不变。消息 body 可能含 markdown HR/H2，不包时下一条消息边界难辨。4 个调用点共享（onboarding / incremental / scoring / summarizer）。
- **喂茶客 context_message：XML 包外层 + Markdown 渲内层**。`_render_onboarding` 固定 6+1 块、`_render_incremental` 4 块，无内容整块省略。`<order_hint>` 与 `<current_task>` 同生共灭（仅 `task_block` 非空才注入）。改渲染器保住每块开闭标签 + 块顺序，新块同步加进 `tests/test_render_onboarding_xml.py`。
- **通道 1 两态注入：onboarding / incremental 都贴 task 块**。`_render_onboarding`（`compact=False` 完整块）与 `_render_incremental`（`compact=True` 1-3 行）两条路径都吃 `task_id`；只 onboarding 注入会让 task 视野在多数短轮失效。改 task block 拼装两路径同步动。
- **task_id 经形参透传，不在渲染层读 store**。`_run_turn` 入口 snapshot `active_task_id` → `_build_context_for(*, task_id)` → `_render_task_block`（纯函数，取数在 `_build_context_for` 内）。本轮按 snapshot 渲染与 message_* envelope 同源；closed / 已删 task 调用方判后不注入。
- **task block 预算：full ≤300 / compact ≤80 token**。超额优先保 title / goal；compact 路径短路省 IO —— 只 `get_task` 一次，不调 `list_decisions` / `list_artifacts` / `task_summaries.get`。
- **`./task/` 落盘文案分层：compact 极简 / full 详细**。compact 每轮喂只放「软触发 + 落盘动作 + 边界提醒」三句；full 单次喂 onboarding 分四段。触发用「你判断」软描述，不给字数硬阈值。禁止把 full 的命名/为什么段回流 compact，禁止把硬阈值塞回任何路径。回归点 `tests/test_render_task_block.py`。
- **打分 prompt 含极简 `<current_task>` 块，与 speak 不共享 body renderer**。scoring 走 `render_scoring_header`（title + **完整 goal** + status，无 `./task/` 指引）、speak compact 走 `render_task_header`（goal 首行）—— goal 口径有意不同（scoring 看完整 goal 含角色顺序、compact 受 token 预算切首行）。每 pick 周期 1 次 `get_task`，N scorer 共享同一字符串。
- **speak 与 scoring 的 order-hint 常量不能合并**。`_SPEAK_ORDER_HINT_BLOCK`（context_renderer.py，行为指令「没轮到只输出让位句」）与 `_ORDER_HINT_BLOCK`（scoring.py，数字锚点 ≤0.3 / ≥0.7）措辞不同；互换会污染对方阶段。

### debug 取证与回放

- **历史 turn 索引：room_history 严格 `enabled=True` 才挂 `turns_index`**。`emit_room_history` 仅 `recorder.enabled` 且 index 非空才挂（≤1000 倒序），关闭时整字段缺省（前端用 `undefined` 区分关了 vs 空）。`TURN_DETAIL.data.prompts` 字段始终存在，内部 key 三重满足（enabled && capture_prompts && 文件可读）才出现。
- **`fetch_turn_detail` 的 `turn_id` 严格 `^turn_[0-9a-f]+$`**。`server.py::_TURN_ID_RE` 在 inbound 入口拒穿越（路径片段直接拼接）；改 `events.new_turn_id` 形态时同步动 regex。缺失 / debug 关 → `found=false` 不发 NOTICE（rotation 清掉是预期场景）。
- **历史详情走统一 evict + 还原索引行**。前端 `MAX_TURNS_IN_MEMORY=50` 对实时 turn + 历史详情共同生效，索引行常驻不 evict。evict 实时 turn 整节点 remove；evict 历史详情靠 `swapRowBackToIndex` 还原轻量索引行 + `turnNodes.delete`，不删 DOM 节点。改 evict/fetch/swap 保住 `record.fromHistory` 分支。
- **rotation 按 turn_id 整组删 + 失败永不阻断**。`turns.jsonl` 行 + `prompts/<turn_id>/` 子目录是最小事务；jsonl tmp+rename 整体重写；所有 IO try/except + WARN，rotation 失败不阻断房间。
- **rotation 触发点固定两处 + 内存计数即权威**。`TurnRecorder.__init__` 末尾 + `flush_turn` 成功后；`_turn_count` `__init__` 数一次后单调维护、永不重测盘。`rotate_if_needed` 内 `load_index` 必 `limit=None`。`max_turns=0` 关 rotation 不关 debug；负数 → `RoomConfigError`。
- **`max_turns` 配置闭环必经四点**：`config.py::DebugConfig` 定义+parse → `session.py::build_room_session` 装 TurnRecorder 透传 → `admin.py::_debug_config_to_dict` + `admin_toml.py::_render_room_toml` snapshot/write-back → 本节不变量。漏任一点字段被静默吞。新加 `[debug]` 字段按此 checklist。
- **`clear_room` 同步擦 debug 取证**。`reset_room` 后 `server._clear_room` 单点调 `recorder.clear()` 擦 `turns.jsonl` + `prompts/`，否则 UI 点完「清空」仍见老索引行。回归 `tests/test_clear_room_wipes_debug.py`。
- **`clear_room` 同步清茶客 agentao 进程内会话窗口**。`reset_room` 末尾对每位茶客 `agent.clear_history()`，否则 `agent.messages` 仍累着 clear 前对话、onboarding 前后矛盾。两层记忆分清：进程内 `agent.messages` 跟 transcript 同生命周期须清；盘上 `memory.db` 跨重启长期记忆不动。异常按茶客隔离 WARN。回归 `tests/test_clear_room_clears_agent_history.py`。

### 切房与后台 runtime（P9）

- **多 `RoomRuntime` 注册表 + 单前台指针**。server 持 `_runtimes: dict[room_id, RoomRuntime]` + `_foreground_id`，`_session` 读点走 `_foreground_runtime.session`。一个 room_id 最多 1 个 `RoomRuntime`（不允许同房前台 + 后台并存）。`RoomEventRouter` 是 per-room **可变路由 sink**：turn / drain 拿的是稳定的 `runtime.router` 对象、不是裸 ws sink —— 切房只翻 `router.mode`（foreground 全量透传 / background 只放行里程碑白名单），in-flight turn 调用栈捕获的还是同一 router、路由自动跟着变，无需触碰运行中的 turn。
- **切房两阶段、不 cancel**。`_switch_room` 阶段一准备目标 runtime（不在注册表就 `build_room_session`，**任何失败即 `return`、不碰旧前台** —— 切房失败原子性）；阶段二 demote 旧前台：busy → 转后台续跑（`router.mode=background`、留注册表）、idle → close 移出。同房重建（加删茶客 / 改 LLM / 权限 / trust）仍走 `_replace_session` + 先 `_cancel_and_drain_inflight`，与切房的「不 cancel」分流。例外：前台房有 `isolation=global` 茶客时切走前必 cancel+drain（其跨房共享 cwd 的 `share/` `task/` 软链会被目标房 retarget、与后台 turn 撞车），故后台 runtime 永不含 global 茶客。
- **后台 runtime 仅在真有 in-flight 活时存在**。后台 turn / handoff drain 跑完，`_run_turn` / `_run_handoff_turn` 的 finally 调 `_maybe_self_destruct_background_runtime` 自毁（emit `room_background_finished` + close + 移出注册表）。切回竞态：`_switch_room` 先翻 `mode=foreground`，自毁判定随即不成立（两者都在事件循环单线程内、无 await 交错）。
- **清理遍历整个注册表 + 幂等**。正常退出走 `async def aclose()` 对全部 runtime cancel+drain+close；`_serve_one` 的 finally 走 `_aclose_background_runtimes()` 清后台、留前台供重连复用；带 MTS 的 runtime **先 `end_managed_session` 再 cancel drain**（绝不留死调度）；清完即移出 `_runtimes`，重复 cancel/close 同一 runtime 静默放过。ws 真正断开才清后台 runtime（连同 handoff 队列 / MTS）—— 后台续跑只跨「房间切换」，不跨 ws 断连 / app 重启。
- **`MAX_BACKGROUND_ROOMS = 5` 软上限**。`_inbound_switch_room` 在 `_switch_room` 之后调 `_enforce_background_room_limit`：后台 runtime 数超上限即淘汰 `background_since_ms` 最小（最早转入后台）者 —— 走强制拆除路径 `_aclose_one_runtime`，拆除后补一帧 `room_background_finished`（放拆除之后发，让被 cancel 的 turn 的 `turn_end` / 自毁帧先到，前端「进行中」徽标只「亮→灭」不闪烁）+ emit info `NOTICE`。**软上限**：超限淘汰最早项、不拒绝切房。`background_since_ms` demote 时写、切回前台清回 `None`。

### handoff（调度层）

- **handoff 是调度层增量，不改对话原语**。delegate（确定性发言指派）仍走一根 `transcript.jsonl`。执行驱动是显式入口：`enqueue_handoff` 只入队，`run_pending_handoff` 才跑。`HandoffItem` 队列不落盘（瞬态，crash 即丢），`reset_room` 清队列；**切房（P9）旧前台 busy 时转后台续跑，`_handoff_queue` 随后台 runtime 继续 drain、不清**，ws 真正断开才清后台 runtime（连同队列）。`HandoffKind` enum / `SCORING_PATH_HANDOFF_*` 按阶段加。
- **`run_pending_handoff` 与 `_run_ai_chain` 严格分流，不互相回落**。drain loop 队列空 / cap 撞顶就停 + emit `turn_end(next="user")`，不回落 scoring；下次 scoring 须用户消息触发。`@A` 完后回 scoring、`/delegate A` 完后不回落。
- **drain loop 每轮 turn 末尾 5 步严格对齐 `_run_ai_chain`**：① peek 算 `cost` 比 cap 得 `next_state` → ② 一次性 `turn_end(next=next_state)` → ③ `flush_turn` → ④ `_kick_summarize` / `_tick_cooldown` / `_kick_detect_new_artifacts` → ⑤ `if not has_next: return`。三个 hook 不能放 `turn_end` 之前、要么一起加要么一起删。
- **cap 检查按 item cost 算，不是 cap=1**。delegate / review cost=1，panel = `len(targets)(+summarizer)`；`_consecutive_ai_turns + cost > max` 不 pop、直接收尾。`run_pending_handoff` 入口清零计数一次，drain loop 内不再清零、不递归。
- **server 必经 `_run_handoff_turn` wrapper**。不直接 `create_task(run_pending_handoff)`：wrapper 照搬 `_run_turn` —— swallow `CancelledError`、`finally` 同槽清 `(_inflight_turn_task, _inflight_kind)`。cancel 补偿由 `run_pending_handoff` 内 speak 段负责。
- **`_inflight_kind` 三态 + 入队前 cancel 条件性**。`∈ {"user", "handoff", None}`，所有 `_run_turn` task 标 `"user"`。delegate inbound：`=="user"` → cancel 抢占；`=="handoff"` → 只 append 队尾；`None` → 不 cancel。启动 wrapper `if _inflight_turn_task is None` 才启。
- **`handoff_clear` 始终 cancel + clear**。无差别 `_cancel_and_drain_inflight()` + 清队列 + emit `handoff_cleared{items_dropped}`，不做 partial cancel。`items_dropped` 不重复列被 cancel 的 in-flight item。
- **handoff envelope emit 职责拆分；`reason` 不进茶客 prompt**。`enqueue_handoff` / `clear_handoff_queue` 纯方法不 emit；`handoff_enqueued` / `handoff_cleared` 由 server handler emit、`handoff_consumed` 由 orchestrator 在 `TURN_START` 之后 emit。`HandoffItem.reason` 是内部备注，永不进 prompt。
- **review 与 delegate 共用调度层，差异只在 prompt 注入**。drain loop 走同一套 cap / cancel / 5 步 / wrapper / `cost=1`；`item.kind` 只分 `scoring_path` 与 `extra_blocks` 两值，控制流一字不动。不为 review 单开 drain 路径。
- **review 只支持 `scope=message`，inbound 三道校验**：① `target` 非空 str；② `target` 在场；③ `message_id` 非空且 `room.message_by_id` 命中。第三道是「drain 时被审消息保证存在」的工程基础。白名单严格 `{type, target, message_id}`。
- **`extra_blocks` 临时块两条渲染路径都接，注入位在 `<speak_instruction>` 之前**。`build_context_for` 的 `extra_blocks` 对 `_render_onboarding` / `_render_incremental` 同步生效；临时块（`<review_target>`）与永久块（`<current_task>` / `<order_hint>`）是两个独立机制。`extra_blocks=None` 时输出与 P7.1 一致。
- **`<review_target>` 只含被审消息原文 + 审阅指引**。`_render_review_block` 现场合成，被审消息走 `format_messages` 包装；不附产物清单（任务上下文由 `<current_task>` 统一给）。inbound 已校验 message_id，drain 时 `message_by_id` 查不到即 bug，用 `assert` 兜底。review 是单轮一次性，不做「审→改→复审」接力链。
- **「请审…」入口只挂带 `message_id` 的气泡**。按钮显隐纯由 `data-message-id` 决定，不特判 user / 茶客。茶客发言气泡恒有 message_id（仅 `status=ok` 后才挂）；用户本地 echo 气泡没有，要等 `room_history` 重建（被接受的边角缺口）。
- **panel = 一个自描述 `HandoffItem`、跑一个 turn**。`HandoffItem` 持 `targets: tuple[str,...]`（≥2）+ `summarizer: Optional[str]`；drain 一次 pop、一个 `turn_id` 串行 speak `len(targets)(+1)` 次。summarizer 是 item 自己的字段、`winners[-1]`、跑同一 turn —— 不拆独立 item、不引 `panel_group_id`。
- **panel 串行执行，「并行」只是 UI 标注 + prompt 提示**。N(+1) 位一个 turn 依次 speak，后发言者在 `<recent_messages>` 看得见前者；`<panel_context>` 块缓解先发言污染。不做真并行 emit。
- **drain loop `kind` 三路分流走三个纯函数**。`_handoff_cost` / `_resolve_handoff_winners` / `_build_winner_blocks`；`_build_winner_blocks` 必须在 runtime 过滤之后调（panel `<panel_context>` 按存活 panelist 渲染）。speak 循环 `zip(winners, winner_blocks)`，turn 边界 / cancel fixup 一字不动。
- **panel `cost` 两档 cap 检查 + 跑不起来的队首项就地 drop**。`_advance_to_runnable_handoff` 是 drain 主体与 `has_next` lookahead 共用 helper：弹掉死项（cost > max / panel 欠员 / target 删光）、每弹一项 WARN + 重发 `HANDOFF_ENQUEUED`。撞预算的项不弹、`break` 等下次；死项必 pop 不能 break。
- **inbound `handoff_panel` 五道校验**：① `targets` 是 `list[非空 str]`；② `len ≥ 2`；③ 无重复且全在场；④ `summarizer`（若有）在场且 `not in targets`；⑤ cap 数学 `len(targets) ≤ min(MAX_PANEL_TARGETS=4, max - has_summarizer)`。构造一个 `HandoffItem(kind=PANEL)`，与 delegate / review 共用 `_enqueue_handoff_and_maybe_start`。
- **handoff propose 复用 `TASK_PROPOSAL` envelope + flat kind**。`data.kind ∈ {decision, open, handoff_delegate, handoff_review, handoff_panel}` 平铺值，前端 `proposal_card.js` 单层 switch。不新增 envelope 类型、不 bump `schema_version`、不新增持久化目录 / toml 字段。
- **propose 不入队、不碰调度层；采纳才走既有 `handoff_*` inbound**。`buildAcceptInbound` 只挑 inbound 白名单内的键，propose-only 字段（`reviewee` / `snippet`）绝不漏进 inbound。delegate 采纳时 `reason` 带 `"<proposer> 提议："` 前缀。
- **handoff propose 工具落 `handoff_tools.py`，不带 `task_` 前缀**。`propose_delegate` / `propose_review` / `propose_panel`，`is_read_only=True` 为权限层放行（仍 emit `TASK_PROPOSAL`）。`register_handoff_tools` 与 `register_task_tools` 并列不合并；`_ProposeHandoffBase` 与 `_TaskProposeBase` 是有意的平行基类。
- **`propose_review` 在 propose 时把 `reviewee` 名解析成 `message_id` 并冻结**。用 `room.latest_message_by_speaker_id(reviewee)`，MVP 只支持「reviewee 最近一条」。reviewee 没发过言 → 返 `Error:` 不 emit。propose 工具不校验在场 / cap，留采纳时 inbound 兜底。
- **采纳后的 `HandoffItem` 与用户直接触发不可区分**。`issued_by` 恒 `HANDOFF_ISSUED_BY_USER`；茶客不能 propose `handoff_clear`（destructive）。propose 永远等用户点，无自动 / 超时采纳。

### 托管任务会话（MTS，P8.3）

- **MTS 是瞬态运行态，每房间最多 1 个，不落盘**（P9 起全局可有多个后台 MTS）。`ManagedSession`（`task_id` / `manager_guest` / `budget`）挂 `Orchestrator._managed_session`，与 `_handoff_queue` 同语义：crash / `reset_room` / ws 断开即清；**切房（P9）改为转后台续跑** —— 旧前台 busy 时 MTS 不随切房结束、留后台跑到 budget/task/cap 自然收尾。不跨重启恢复。`_managed_session is None` 即「无托管」，不另存 `enabled` bool。只经 `managed_session_start` inbound 开启，无自动 / 超时 / 茶客 propose 开启。
- **断线即结束 MTS（切房不结束，P9）**。MTS 活着 ⟺ drain task 在跑；`_serve_one` 的 `finally`（断线 / 连接关闭必经）先 `_maybe_end_managed_session(user_cancel)` 再 `_cancel_and_drain_inflight()` cancel 掉前台 drain，随后 `_aclose_background_runtimes()` 把切走后仍在后台的 runtime（连同其 MTS）一并 end + cancel。否则 drain 被 cancel 后 MTS 既不自然推进也无人能停，重连快照还会让前端「托管中」按钮对着死调度复活（Codex review P2）。**P9 起切房不再结束 MTS** —— 旧前台 busy 转后台续跑，MTS 在后台自驱直到自然收尾；只有 ws 真正断开才强制结束（含后台 MTS）。**`emit_room_snapshot` 重投 MTS（P9 9.3.2）**：前台房 orchestrator `_managed_session` 非空时末尾补发一帧 `managed_session_started`（`budget` 是当前剩余值），让切回托管中的后台房能自给自足重建前端状态条 / 「停止托管」按钮，不依赖前端缓存。单点 `orchestrator.emit_managed_session_snapshot(sink)`。
- **MTS 跑在 handoff drain loop 上，不新开调度路径**。`run_pending_handoff` drain 每轮 turn 跑完调一次 `_advance_managed_session_after_turn(item, sink)`，再照常走既有 peek / turn_end / 收尾，既有「5 步」内部顺序一字不动；`_managed_session is None` 时该调用立即返回，非 MTS 房间零行为变化。`_advance` 按「刚跑完的是不是管理者回合」分流：管理者回合不做事（其 delegate/panel 提议已被 hook 入队）；worker 回合且队列空 → `budget-=1` + 回调 `delegate(manager)` + emit `managed_session_advanced`。停止守卫 `_managed_session_stop_reason()` 先判（budget → task_closed → cap，谁先命中报谁）。
- **`manager_finished` 只由 drain 收尾兜底产生**（`run_pending_handoff` `has_next` 为假处），覆盖「管理者没派活 / 调用链续不下去（target 不在场被 drop、发言抛错）」；不公开 `target_missing` / `error` reason。6 个会 emit 的 reason：`manager_finished` / `budget_exhausted` / `task_closed` / `cap_reached` / `user_stopped` / `user_cancel`。
- **结束 MTS 必清 `_handoff_queue`**。`end_managed_session(sink, reason)` 任意路径都清掉待跑 handoff 项 + 重发空队列快照——保证 MTS 结束后房间不再自动说话，也使 `budget_exhausted` / `task_closed` / `cap_reached` 不会多跑一个已入队的 worker。`managed_session_stop` 不取消当前 in-flight turn（自然跑完）；`handoff_clear` / `cancel` 中途介入时一并结束 MTS（`user_cancel`）。
- **MTS 内只自动入队管理者的 `handoff_delegate` / `handoff_panel` 提议**。实现是 `ChahuaTransport.set_task_proposal_hook` 注入的 `Orchestrator._intercept_task_proposal`：`emit_chahua` 遇 `TASK_PROPOSAL` 先过 hook，命中即 `enqueue_handoff` 并**拦下该 envelope 不下发前端**（不渲采纳卡）。非 MTS / 非管理者 / review / decision / status 提议照常渲卡。`handoff_tools.py` / `proposal_card.js` / `TeaGuest.speak()` 签名零改动。MTS item 的 `issued_by` 恒 `HANDOFF_ISSUED_BY_USER`，provenance 经 `reason` 前缀「托管 · <manager> 指派」保留。
- **管理者 MTS 回合注入 `<managed_session>` 临时块**。`_build_winner_blocks` 对「delegate 指向管理者」的项经 `extra_blocks` 注入 `render_managed_session_block(manager, budget)`（context_renderer.py 纯函数）；worker 回合 / 非 MTS delegate 无块。块第 ② 条显式作废 `propose_*` 工具 ack 的「等用户采纳」等待语义（为保 `handoff_tools.py` 零改动不改工具 ack）。
- **`managed_session_*` 是 hint 型事件**，不进 transcript、不触发 AI；新增事件类型不 bump `schema_version`。`budget` 计管理者复查回合数（kickoff 不耗），是用户旋钮；`max_consecutive_ai_turns` 是硬护栏，MTS 不能越过。两 inbound（`managed_session_start` / `stop`）归 handoff slot。

### 茶客能力 introspection

- **`/tools` `/skills` 走单一共享投影 `TeaGuest.describe_capabilities()`**。WebSocket `_inbound_list_guest_caps` 与 CLI `_print_guest_caps` 共调，禁各拼一份。tools / 可用 skills 是 `__init__` 时一次注册的静态集合。查茶客实例必经 `Orchestrator.get_guest(name)`（活字典），不读 `RoomSession.guests`（boot 快照）。`view`（tools/skills）经 `GUEST_CAPS_INFO` 原样回声，前端按响应自带 view 裁剪，不靠可变全局态。inbound 白名单严格 `{type, guest}`、handler 归核心层、`schema_version` 不 bump。
- **能力花名册：装配期一次性解析的不可变快照**（P8.2-roster）。`build_room_session` 解析 `roster: dict[guest→summary]` 三级（手写 `[[guest]].summary` → `persona_summary` 中央缓存 → 无），传给 `Orchestrator` → `ContextRenderer`。运行期增删茶客整体重建 session，故 renderer 持的是不可变快照，**不**做运行期增量更新。只进 onboarding 的 `<room>` 块「在场」行（任一茶客有摘要 → bullet 花名册；全员无 → 退回逗号单行），**不**进每轮 `<room_update>`（稳定信息、避免 N×T token 放大）。roster-b 后台生成的新摘要**下次重建 session 才生效**——同 session 内不刷。`[[guest]].summary` 走 P4 配置闭环四点（config 解析 / `_room_config_to_dict` snapshot / `_render_room_toml` 回写 / 本不变量）。

### 聊天界面渲染（P10）

- **mermaid 渲染只在 message_end 全文到位时调一次**。流式 delta 期间禁调 —— 半截 ` ```mermaid graph TD; A --> ` 会让 mermaid 抛 parse error 闪烁刷屏。`renderMermaidIn` 入口收敛于 `renderGuestText`（history / appendBubble）/ `endStreamingMessage` 全文路径 / `task_panel` goal —— 流式 `appendDelta` 路径不调。失败保留原 `<pre>` + `.mermaid-error` class + `dataset.mermaidError`，CSS `::before` 红边显示错误；下条消息仍可渲。
- **mermaid SVG 走手工 sanitize，不能换 DOMPurify**。DOMPurify 对 `<foreignObject>` 强制清空内容，mermaid v11 节点 label 走 `<foreignObject>+HTML` 会被剥光。安全语义靠 mermaid 自带 sanitize + Electron CSP + 手工剥 `on*` / `javascript:` 三层兜底。
- **挂件渲染按 rel 去重**。`attachArtifactToBubble` 按 `[data-rel="..."]` 查重，防 live + history 双触发（切回房瞬间可能 history 重放 + 末尾 live envelope 撞同 rel）。
- **图片预览懒拉、不 eager 内嵌**。`task_artifact_added` envelope 不带 base64；前端渲占位 `<img>` 后发 `download_file purpose=preview`，server 回包后 `resolveArtifactPreview` 灌字节。同 rel 可挂多份（用户上传后茶客再写同名），回包一次性填所有等待者。SVG 走 pill 下载链、不内嵌预览（SVG 可内嵌 `<script>`，保守不渲）；其它图片白名单 `{png, jpg, jpeg, gif, webp}` 走内嵌。
- **切房 / clear 必清 pending preview**。`renderSidebar` 调 `clearPendingArtifactPreviews()` —— 否则 `messagesEl.replaceChildren` 后等待中的 `<img>` 节点已被摘走，preview 字节回包无处灌，pending Map 持续涨。

## 测试

`pyproject.toml` 已设 `asyncio_mode = "auto"`，async 测试不用加 mark。`tests/` 目前只有一个回归测（`test_orchestrator_cancel_during_scoring.py`，对应 fe52918 修的「scoring 阶段被 cancel 后停止按钮卡死」）。新测要复现 bug 优先。
