# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目语义

「茶话室」(chahua) = 多 Agent 群聊桌面 App。用户和多个由 [agentao](https://github.com/jin-bo/agentao) 驱动的 AI「茶客」在同一聊天室对话（像微信群，可 `@`、茶客之间也能接话）。房间不只是聊天容器 —— 还是带任务的工作容器（**P5 任务房间**：开任务 / propose 决策待用户采纳 / `./task/<name>` 自动归集 artifact / 多任务共存单时刻 1 active）+ 自带取证视图（**P6 调试与回放**：每轮可展开看候选 / 分数 / prompt / 工具 / 产物，落盘 `rooms/<id>/debug/`）。

**两个运行形态共享同一套 Python 后端**：
- CLI REPL：`uv run chahua`（最快验证 LLM 凭据 / `room.toml`）
- Electron 桌面壳：`cd app && npm run dev` —— main 进程拉起 `chahua-server` sidecar，本地 WebSocket 通信

两条路径都走 `chahua.session.build_room_session()` 装配房间。

## 常用命令

```bash
uv sync                                       # Python 依赖（按 pyproject.toml 拉 agentao≥0.4.6）
uv run chahua                                 # CLI（默认入 rooms/p1-test）
uv run chahua --room rooms/p3-黄河路
uv run chahua-server --host 127.0.0.1 --port 7860 --room rooms/p3-黄河路  # 独跑 sidecar
uv run pytest                                 # 全量测试
uv run pytest tests/test_xxx.py -v            # 单测
cd app && npm run dev                         # Electron dev（首次需 npm install）
cd app && npm run build:python && npm run build:mac  # 打包 → dist/茶话室-<ver>-mac-arm64.dmg
FORCE=1 npm run build:python                  # 强制清重建 python-bundle
```

`pyproject.toml` 设 `asyncio_mode = "auto"` —— async 测试无需 `@pytest.mark.asyncio`。

## 架构要点

### 双层进程 / 双层路径

```
Electron main (Node)  ─ spawn ─→  chahua-server (Python sidecar)
       │                                │
       └─ stdio: ["pipe", ...]           └─ WebSocket 127.0.0.1:<random-free-port>
       └─ before-quit → stop()           └─ stdin EOF watcher → graceful 关停
```

- **app_root vs user_data_root**：dev 模式同源仓库根；packaged 模式 app_root=`.app/Contents/Resources`、user_data_root=`~/Library/Application Support/chahua/`。Electron main export `CHAHUA_APP_ROOT` / `CHAHUA_USER_DATA`，Python 端 `Paths.from_env()` 接住。**改路径解析必须双根都顾**（persona 走 `find_in_data_then_app`：user_data 优先回退到 app）。
- **sidecar ready 信号**：`server.py` 起来后打 `监听 ws://...`，`sidecar.js` 正则 `/监听\s+ws:\/\//` 匹配后 resolve。两边同步。
- **首启动 seed**：dev 跳过；packaged 拷 `app/templates/` → user_data_root，`.chahua-seeded` 幂等。

### Python 后端模块分工

职责一句话；承重契约见「关键不变量」段，实现细节看代码。

- `session.py` — 房间装配。CLI 与 server 共用 `build_room_session()` / `discover_rooms()` / `load_env_files()`。
- `config.py` — `room.toml` 解析，**白名单严格**（未知字段 `RoomConfigError`）。字段：`[room]` / `[room.llm]` / `[scoring]` / `[summary]` / `[[guest]]` / `[[guest.extra_mcp_servers]]`。
- `llm_spec.py` — `LLMSpec` + `from_env()` / `try_from_env()` / `from_toml()` 三入口 + `build_client()` 出口。toml 强制 `model = "<provider>/<model>"`；env 允许裸 model。`[scoring]` / `[summary]` / `[[guest]]` 各走自己 spec，缺即 fallback 链回房间默认。
- `guest.py` — `TeaGuest`（包 `agentao.Agentao`）。`speak()` 外层 try/except/finally 保 `message_start` 必配对 `message_end`（status: ok/cancelled/error），envelope 与 transcript 共享同一 message_id。
- `orchestrator.py` + `_orchestrator_{chain,handoff_drain,handoff_queue,managed_session,scoring,consts}.py` — 意愿打分主循环。房间新消息 → 并发裸 LLMClient 打分 → 取 ≥ `want_threshold` 前 1~2 名发言 → 没人过阈值等用户。`@<名字>` 走确定性路由不打分。**slot 重构**：主类按子域拆 5 个 slot（AIChainOps / HandoffDrainOps / HandoffQueueOps / ManagedSessionOps / ScoringOps），`_install_orchestrator_slots(orch)` 单点装配，公开 import 路径不动。**主类必须保留 `_run_ai_chain` / `_intercept_task_proposal` 同名 method**（被 `monkeypatch.setattr` 替换 / `set_task_proposal_hook` 取 bound method）。
- `scoring.py` — 轻量打分。transcript 不可信，输出严格 JSON、解析失败降级 0、score clamp `[0,1]`。
- `room.py` + `cursor.py` + `_persist.py` — 持久化层。`transcript.jsonl` / `summary.jsonl` append-only **加载跳坏行**；`cursor.json` tmp+rename。**不做 fsync**。
- `events.py` — `ChahuaEnvelope`（含 `schema_version`）。agentao 原生 `AgentEvent` 不带 room_id / guest_name / message_id，envelope 是茶话室外层合成。
- `transport_bridge.py` — `ChahuaTransport`（`SdkTransport` 子类），agentao 事件 → envelope。
- `summarizer.py` — cheap LLM 增量产 `summary.jsonl`，onboarding 窗口拼用。
- `permissions.py` — **read-only 双 API 同步**：统一 `apply_permission_mode(agent, mode_str)`，别处禁单调 `set_mode`。
- `task.py` + `tasks_store.py` — 任务数据模型 + `tasks/state.json` + `tasks/<id>/{task.json, decisions.jsonl, artifacts/}` 持久化。`build_task_info_payload` 是 `task_info` envelope 投影单一来源。**入站严格 / 落盘宽容**。
- `task_tools.py` — 5 个茶客侧 task 工具：`task_list_artifacts`（read-only 真无副作用）/ `task_propose_{decision,open,status}`（`is_read_only=True` 但**会 emit `TASK_PROPOSAL`**，权限层放行 ≠ 无副作用）/ `task_write_artifact`（绕开 `PathPolicy` 让茶客 `./task/<name>` 落盘，`is_read_only=False`；name 校验拒 `/` `\` `..` `.` 前缀）。
- `handoff_tools.py` — 3 个茶客侧 handoff propose：`propose_delegate` / `propose_review` / `propose_panel`，同 `task_propose_*` 口径。**不带 `task_` 前缀**（房间级调度，非任务域）。`propose_review` 把 `reviewee` 名解析成最近发言 `message_id` 冻结。
- `task_rendering.py` — task block prompt 纯函数。取数（`get_task` / `list_decisions` / `list_artifacts`）由调用方 `_build_context_for` 完成；scoring 与 speak compact 在 goal 上口径有意不同。
- `context_renderer.py` + `artifact_detector.py` — 前者输出 6+1 块 XML 结构（`<room>` / `<user_persona>` / `<room_summary>` / `<current_task>` / `<order_hint>` / `<recent_messages>` / `<speak_instruction>`），`quoteattr` 防注入。后者每 pick 周期扫 `tasks/<active>/artifacts/` diff `_seen_artifacts`，emit `task_artifact_added` + 重发 `task_info`；只 seed 非 closed 任务。
- `persona_summary.py` — persona 能力摘要 LLM 生成 + 内容寻址缓存（`<hash>.json`，hash=persona md sha256）。三级解析（手写 `[[guest]].summary` → 缓存 → None）；后台预热经 `get_running_loop()` 守卫。失败 WARN 不阻断。
- `debug_recorder.py` — P6 取证落盘。`TurnRecorder` 由 orchestrator / guest / transport_bridge 三处喂。`max_turns=500` rotation 按 turn_id 整组删（最小事务），`_turn_count` 内存维护永不重测盘。**rotation 失败永不阻断房间**。
- `server_room_snapshot.py` — `emit_room_snapshot` 单点装配：`emit_room_info` / `emit_room_history` / `_emit_task_info` / 末尾补 MTS 快照。`turns_index` 严格 `enabled=True` 才挂（≤1000 倒序）。`rooms_available` 每房带 `busy=busy_alive()`（含 bg run）；P11 加 `background_runs` 字段。
- `server_entry.py` — `chahua-server` CLI 入口。argparse / 端口分配 / stdin EOF watcher。
- `admin.py` + `admin_{guest,persona,room,user,toml}.py` — admin 按域拆分：guest（添删改 + LLM）/ persona（MCP trust / skills）/ room（创建 / 切房 / update_room_{llm,toml}）/ user（USER.md / 头像）/ toml（`_render_room_toml` 结构化重写）。
- `persona_assets.py` + `trust.py` — persona sibling `mcp.json` + `skills/` 装载；MCP 走信任门（`persona-trust.json` + UI popover）。skills 软链 + copytree 兜底。
- `persona_manifest.py` — P12 `persona.toml` 解析。`PersonaManifest` frozen dataclass + 严格白名单（顶层 / `[defaults]` / `[defaults.guest]` 三级）。两条入口共享私有 `_parse_dict`：`load_persona_manifest(persona_dir)`（文件，缺即 None）/ `parse_persona_manifest_bytes(blob)`（in-memory dry-run，给 `persona_import._write_files` 落盘前用）。**全失败统一 PersonaManifestError**（含 UnicodeDecodeError / TOMLDecodeError / OSError）。`_opt_str` 用 strip + 空→None 对齐 legacy `_read_persona_toml_name`。承重契约见「persona 包 manifest（P12）」子段。
- `server.py` + `server_inbound_{admin,task,io,settings,handoff,agent_run}.py` + `_server_helpers.py` — ws 生命周期 / 帧路由 / room snapshot 在 `server.py`；30+ `_inbound_*` handler 切到 **6 个 handler 类**（`AdminHandlers` / `IOHandlers` / `SettingsHandlers` / `TaskHandlers` / `HandoffHandlers` / `AgentRunHandlers`）—— **组合而非多继承**。`_install_handler_slots(srv)` 是 slot 装配唯一真理源；`_INBOUND_ROUTES` 帧字符串 → 属性路径映射在 `server.py` 顶层维护，`__init__` 经 `operator.attrgetter` 一次性解析。**改 `_inbound_*` 先看 slot 归属**：admin / task / io / settings / handoff（+ MTS）/ agent_run（bg run）。测试夹具用 `object.__new__(ChahuaServer)` 时必须 `_install_handler_slots(srv)` 装回 slot。
- `agent_run.py` + `agent_run_sink.py` — P11 后台 agent run：`AgentRun` frozen dataclass + `BatchMessageSink`（envelope 白名单过滤 + `TASK_PROPOSAL` 缓冲到 finally + `run_id` 注入 + `flush_to(router)`）。`room_runtime.py` 加 `agent_runs` / `agent_run_tasks` / `active_guest_names` + `has_active_runs()` / `busy_alive()` / `guest_busy()` + `cancel_and_drain_agent_runs()`（末尾 defensive sweep 防 pre-start cancel race）。`server.py::_run_agent_background` 是 wrapper，与 `_run_turn` 平行的 bg 执行入口（5 步 finally 见不变量）。

### WebSocket 线协议

下行：每帧一条 JSON = `ChahuaEnvelope.to_dict()`。

上行：每帧一条 JSON：`user_message` / `cancel` / `switch_room` / `clear_room` / `fetch_turn_detail` / `list_guest_caps` / `agent_run_start` / `agent_run_cancel` + handoff / MTS / task / admin / io / settings 等（见 `_INBOUND_ROUTES`）。未知 `type` WARN 后忽略；非 JSON / 二进制帧 → `close(UNSUPPORTED_DATA)`。

### 跨平台 sidecar 关停

`sidecar.js::stop()`：先 `child.stdin.end()` → Python `_watch_stdin_eof` 收 EOF 走 graceful；2s grace 兜底 `forceKillTree(child)` —— Windows 走 `taskkill /F /T /PID`（dev 链路 `electron → uv.exe → python.exe`，SIGKILL 直接子会留孤儿），POSIX 走 `SIGKILL`。Windows 的 `connect_read_pipe(sys.stdin)` 在 ProactorEventLoop 经常 `WinError 6` 静默失败，tree-kill 是兜底正解。

## 关键不变量

承重契约，编辑代码必须遵守。本节只留「规则 + 关键 why」；**完整 rationale 见 `docs/DESIGN.md` §9**，改不变量两处同步。

### 配置与 LLM 装配

- **`room.toml` 未知字段必报错**。配错宁可炸，typo 别静默吞。
- **`[room].name` 跨房唯一**。envelope `room_id` 用 `room.name` 占位，P9 前端据它分流前台/后台；同名会让后台房 envelope 污染前台。`create_room` / `update_room_toml` 都拒重名。
- **toml `model` 形如 `<provider>/<model>`**。首个 `/` 拆 provider，二级路径保留进 model；无 `/` → 错。CLI / dotenv 例外允许裸 model。
- **LLM section 整段写或整段不写**。`[room.llm]` / `[scoring]` / `[summary]` / `[[guest]]` 四件套 all-or-nothing；fallback 走 section 级（缺整段回上一档），不做字段级 overlay。
- **房间默认 LLM 两层 fallback**：`[room.llm]` → `LLMSpec.try_from_env()`，都缺 → 错。toml 显式配置时 env 完全忽略。
- **API key 永不进 toml / envelope**。toml 最多写 `api_key_env`；envelope 只下发 `api_key_env` 名 + `api_key_ready` bool。
- **`[[guest.extra_mcp_servers]]` 自动信任，persona sidecar `mcp.json` 走 trust 门**。前者用户手写 toml = 意图；后者可能从 GitHub 导入任意可执行须 UI 勾选。同名房间级覆盖 persona。
- **isolation 切换不自动迁移记忆**。`isolation` 决定茶客 cwd；切换后旧路径 `.agentao/memory.db` / `sessions/` 原样保留。

### persona 包 manifest（P12）

- **`persona.toml` 顶层 `schema_version` 必填且当前唯一合法值 = 1**。缺 / != 1 → `PersonaManifestError`。**Why**：第三方/社区包跨版本兼容的唯一锚点。未来加字段不 bump；破坏性变更才 bump。
- **严格白名单：未知键 → `PersonaManifestError`**。顶层 / `[defaults]` / `[defaults.guest]` 三级都校验。与 `room.toml` 同口径。**不**预留「未来口袋」—— P12.2 真要加 `[requires]`，到时白名单加一行就行。
- **`[defaults.guest]` 字段子集严格 ⊂ `_ALLOWED_GUEST_KEYS` 且不含 `name` / `persona` / LLM 四件套**。**Why**：persona 包是可分发资产；带 LLM 配置等于把作者本地 env 变量名泄漏给装它的人。`name` / `persona` 由 picker 自动填，不在 manifest。
- **`[defaults.guest]` + 顶层 `summary` 仅在「加入房间」一次性 inflate，之后房间 `room.toml` 与 manifest 解绑**。作者更新 manifest **不**自动重 inflate；想让新默认生效需用户从房间删除该茶客再重新加入（npm `package.json` 同款语义）。
- **picker 三级 fallback：`persona.toml`.display_name → 老 `<stem>.toml`.`[guest].name` → 文件名 stem**。`persona.toml` 与 `<stem>.toml` 同时存在时取前者。**display_name 缺 / 空 / 全空白时不跳第 2 档**（与 `_read_persona_toml_name` legacy 口径一致）。summary 仅 manifest 提供 —— 坏 manifest 时 summary 与 display_name 一起退（防出现「display 来自 legacy 但 summary 来自 manifest」不一致状态）。
- **permission / isolation 默认值合一仅在 admin 层；frontend → inbound → admin 全链路用 `None` 表示「用户未显式选」**。frontend `modals.js` 不送 `permission` 字段、inbound `_inbound_add_guest` / `_inbound_create_room` 保留 None、admin `_build_guest_with_manifest_defaults` 三级 coalesce（入参 > manifest > `DEFAULT_MODE`）一律 `is not None`，**禁** `or DEFAULT_MODE`。**Why**：`or` 会把 `""` 这类显式坏值静默吞 → 用户 typo 永不可见、P12 等于白做。（注：现存 `_render_room_toml:257` + `config.py:595` 跨 phase `or DEFAULT_MODE` 是 P12 之前既有行为，breadcrumb 测在 `test_admin_guest_inflate.py::test_empty_permission_swallowed_by_renderer_breadcrumb` 钉住。）
- **消费路径严格 fail-fast；discover 是唯一 `WARN+None` 例外**。`add_guest` / `create_room` / `persona_import` 遇坏 manifest → `PersonaManifestError` 向上抛 → inbound `_emit_notice(level=error)`；`create_room` 事务性：所有 guest 的 manifest 先解析成功才 `mkdir(exist_ok=False)`，任一坏 → 目录不建。`persona_import._validate_manifest_pre_write` 在 `_write_files` 之前对采集到 files 里**根级小写 `persona.toml`** 字节做 dry-run；`Persona.toml` 等错名同样拒（跨平台 case-insensitive FS 旁路守卫）。`discover_personas` 是唯一允许 `try/except + WARN + None` 的路径 —— 可见性优先，让 picker 不至于因为坏 manifest 整个 persona 消失。
- **仅 dir-form 才查 manifest，flat-form 跳过**。flat-form md 的 parent 是 `chahua/personas/` 本身；误置一份根级 `persona.toml` 会让所有内置 persona 共享同份 `[defaults.guest]` —— 两处守卫同语义不同写法：`admin_room._build_guest_with_manifest_defaults` 内联判定 `md_abs.parent.name != PERSONAS_REL_DIR.name`；`admin_persona._resolve_display_and_summary` 接 `is_dir_form: bool` 形参，由 `discover_personas` 的两段循环（flat `glob('*.md')` vs dir-form `iterdir`）分别传值。

### 权限、持久化、事件契约

- **read-only 必双 API 设**。`PermissionEngine.set_mode()` 与 `tool_runner.set_readonly_mode()` 同设，统一走 `apply_permission_mode`。
- **persistent jsonl 加载跳坏行**。最后一行截断不应让用户失去整个房间历史。不做 fsync。
- **envelope `message_start` / `message_end` 必成对**。`TeaGuest.speak()` 外层 try/except/finally 是承重墙。
- **打分输入不可信**。score 严格 JSON + 解析失败降级 0 + clamp `[0,1]`；`@提及` 走确定性路由不进打分。
- **`download_file.purpose` 仅前端分流，server 行为对取值无差异**。inbound 可选 `purpose ∈ {download, preview}`（默认 download），envelope 原样回声；server 无白名单。前端 `purpose === "preview"` 单分支判断；preview 失败塞 `<img>.alt` + `.artifact-image-error` class（不弹下载 alert）。

### 任务房间

- **入站严格、落盘宽容**。inbound 字段白名单严格（未知键 → NOTICE error 丢帧）；落盘加载未知字段 warn 后忽略、仅必需字段缺才跳坏该条。两条规则有意不对称（防前端瞎写 vs 跨版本兼容）。
- **`task_id` 只活在 `envelope.data`**。`message_start` / `message_end` 的 `data.task_id` 是可选标签，envelope 顶层与 `schema_version` 不动。
- **`TASK_INFO` 是权威快照，其它 4 个是 hint**。任意 task 变更后 server 重发整份；`task_open` / `task_update` / `task_decision_added` / `task_artifact_added` 仅局部反馈。
- **task / decision 写权限只在用户**。茶客只能 propose；UI 渲采纳按钮等用户点。例外：artifact 茶客写 `./task/<name>` 自动归集，不走采纳。
- **`attach_artifact` 是 copy 不是 move**。`share/` 是公共桌面 + 历史消息引用，move 会断引用；同文件可挂多任务。
- **多任务共存，单时刻最多 1 active**。`open_task` 自动 `set_active`，旧 active 留 `status="open"`；`set_active_task` 走前强制 `_cancel_and_drain_inflight`。
- **通道 2 软链：茶客 `./task/` 跟 active task**。`build_room_session` 末尾 + open/set_active/close 三处刷新软链到 `tasks/<active>/artifacts/`；active=None 只解不建。Windows 走 junction。失败 WARN 不阻断。
- **task summary cursor 落盘 only**。`TaskSummaries` 在 `_kick_summarize` 末尾每任务跑；cursor 内存推进、`session.close()` 统一 flush。
- **`task_propose_status` 采纳按终结态分流**。非终结态 → `update_task {patch:{status}}`；终结态（`done`/`abandoned`）→ `close_task {status}`。提议取值 = 全部状态 − `{open}`；`reason` 是 propose-only 不进 inbound。
- **propose 不写库、采纳才入库**。`TASK_PROPOSAL` 仅触发前端「采纳/忽略」卡片；采纳由 `proposal_card.js::buildAcceptInbound` 按 `kind` 拼回既有 inbound，server handler 零改动。卡片 session-local，刷新即清。
- **task 事件是 UI 系统气泡，不进 transcript / 不触发 AI**。`open_task` / `close_task` / `add_decision` / `attach_artifact` / `clear_task_artifacts` + 自动归集只 emit `TASK_*` hint + `TASK_INFO`，不合成 user 消息、不起 `_run_turn`。代价：task 操作不再唤醒房间，要茶客介入用 `@提及` / handoff / 用户消息。
- **`./task/` 读用 `read_file`、写走 `task_write_artifact`**。agentao `PathPolicy.contain_file` 跟随 symlink 解析后 `./task/` 不在茶客 workdir 之下 → 原生 `write_file` 被拒；`task_write_artifact` 直接调 `tasks_store.artifacts_dir` 绕开 path_policy。`is_read_only=False`（read-only 模式应拦住）。
- **新文件感知 + 用户上传须 `mark_seen`**。`_kick_detect_new_artifacts` 每 pick 周期末尾扫，emit `task_artifact_added` + `task_info`；用户 UI `attach_artifact` 拷文件后**必须 `ArtifactDetector.mark_seen(task_id, name)`**（增量 add 禁整组覆盖），否则下轮 `detect()` 把用户上传当茶客新产物重发。
- **`/clear task` 仅清任务产物，作用域严格**。只删 `tasks/<task_id>/artifacts/` 可见文件，`task.json` / 决策 / 状态 / 摘要游标都不动。茶客无 propose 入口。
- **`message_artifacts.jsonl` 与 transcript 同生命周期（P10）**。`MessageArtifactRegistry` per-room；`reset_room` 同步 clear；`/clear task` 不清注册表。加载跳坏行 + 严格 `isinstance(str)` 校验 mid/name + 显式拒 bool size + rel 必落 `share/` 或 `tasks/<id>/artifacts/` 已知 root + **两 root 都做空段 / `.` / `..` segment 校验**（防 `share/../../foo` 解析逃出 share/）。
- **`task_artifact_added.data.originated_message_id` 仅由 `task_write_artifact` 调用路径派生（P10）**。`transport_bridge._maybe_record_artifact_path` 是唯一写入点；`ArtifactDetector.detect` 是唯一消费点。可选字段，缺省时旧前端忽略仍渲老气泡，`schema_version` 不 bump。
- **shell / MCP 工具走 TOOL_START / TOOL_COMPLETE 前后 diff 回填 pending（P10）**。`task_write_artifact` / `write_file` / `replace` 三个 args-known 工具直接经 `_maybe_record_artifact_path` 落 pending；shell / MCP 类靠两次扫盘 diff 回填。**滚动 baseline 仅在 bind 内更新**（`_message_id is None` 时不写 `_diff_baseline`）。失败 TOOL_COMPLETE 走 `_rollback_pre_pending` 回滚 args-known 路径预落的 pending。

### context 渲染与 prompt 装配

- **`format_messages` 每条消息走 `<message>` 包裹**。`room.py::format_messages` 单点定义；消息 body 可能含 markdown HR/H2，不包时下一条消息边界难辨。4 个调用点共享。
- **喂茶客 context_message：XML 包外层 + Markdown 渲内层**。`_render_onboarding` 6+1 块、`_render_incremental` 4 块，无内容整块省略。`<order_hint>` 与 `<current_task>` 同生共灭（仅 `task_block` 非空才注入）。新块同步加进 `tests/test_render_onboarding_xml.py`。
- **通道 1 两态注入：onboarding / incremental 都贴 task 块**。两条路径都吃 `task_id`；只 onboarding 注入会让 task 视野在多数短轮失效。
- **task_id 经形参透传，不在渲染层读 store**。`_run_turn` snapshot `active_task_id` → `_build_context_for(*, task_id)` → `_render_task_block`（纯函数）。closed / 已删 task 调用方判后不注入。
- **task block 预算：full ≤300 / compact ≤80 token**。compact 路径短路省 IO —— 只 `get_task` 一次。
- **`./task/` 落盘文案分层：compact 极简 / full 详细**。compact 三句（软触发 + 落盘动作 + 边界提醒）；full 单次喂 onboarding 分四段。触发用「你判断」软描述，不给字数硬阈值。
- **打分 prompt 含极简 `<current_task>` 块，与 speak 不共享 body renderer**。scoring 走 `render_scoring_header`（title + **完整 goal** + status）、speak compact 走 `render_task_header`（goal 首行）。每 pick 周期 1 次 `get_task`，N scorer 共享。
- **speak 与 scoring 的 order-hint 常量不能合并**。`_SPEAK_ORDER_HINT_BLOCK`（行为指令）与 `_ORDER_HINT_BLOCK`（数字锚点）措辞不同；互换会污染对方阶段。

### debug 取证与回放

- **历史 turn 索引：room_history 严格 `enabled=True` 才挂 `turns_index`**。≤1000 倒序，关闭时整字段缺省（前端用 `undefined` 区分关了 vs 空）。`TURN_DETAIL.data.prompts` 字段始终存在，内部 key 三重满足（enabled && capture_prompts && 文件可读）才出现。
- **`fetch_turn_detail` 的 `turn_id` 严格 `^turn_[0-9a-f]+$`**。在 inbound 入口拒穿越。缺失 / debug 关 → `found=false` 不发 NOTICE（rotation 清掉是预期场景）。
- **历史详情走统一 evict + 还原索引行**。前端 `MAX_TURNS_IN_MEMORY=50` 对实时 + 历史详情共同生效，索引行常驻不 evict。evict 历史详情靠 `swapRowBackToIndex` 还原轻量索引行。
- **rotation 按 turn_id 整组删 + 失败永不阻断**。`turns.jsonl` 行 + `prompts/<turn_id>/` 是最小事务；所有 IO try/except + WARN。
- **rotation 触发点固定两处 + 内存计数即权威**。`__init__` 末尾 + `flush_turn` 成功后；`_turn_count` 数一次后单调维护永不重测盘。`max_turns=0` 关 rotation 不关 debug；负数 → `RoomConfigError`。
- **`max_turns` 配置闭环必经四点**：`config.py` 定义 → `session.py` 装 → `admin.py::_debug_config_to_dict` + `admin_toml.py::_render_room_toml` 回写 → 本不变量。漏一点字段被静默吞。
- **`clear_room` 同步擦 debug 取证**。`reset_room` 后 `server._clear_room` 调 `recorder.clear()` 擦 `turns.jsonl` + `prompts/`。
- **`clear_room` 同步清茶客 agentao 进程内会话窗口**。`reset_room` 末尾对每位茶客 `agent.clear_history()`，否则 `agent.messages` 仍累 clear 前对话。盘上 `memory.db` 跨重启长期记忆不动。异常按茶客隔离 WARN。

### 切房与后台 runtime（P9）

- **多 `RoomRuntime` 注册表 + 单前台指针**。server 持 `_runtimes` + `_foreground_id`；一个 room_id 最多 1 个 runtime。`RoomEventRouter` 是 per-room 可变路由 sink：切房只翻 `router.mode`（foreground 全量 / background 里程碑白名单），in-flight turn 调用栈捕获的是同一 router，路由自动跟随。
- **切房两阶段、不 cancel**。阶段一准备目标 runtime（任何失败即 `return`、不碰旧前台 —— 切房失败原子）；阶段二 demote 旧前台：busy → 转后台续跑、idle → close 移出。同房重建仍走 `_replace_session` + 先 `_cancel_and_drain_inflight`（与切房不 cancel 分流）。**前台房有 `isolation=global` 茶客切走前必 cancel+drain**（跨房共享 cwd 的软链会被目标房 retarget），故后台 runtime 永不含 global 茶客。
- **后台 runtime 仅在真有 in-flight 活时存在**。bg turn / handoff drain / bg run 跑完，wrapper finally 调 `_maybe_self_destruct_background_runtime` 自毁。切回竞态：`_switch_room` 先翻 `mode=foreground`，自毁判定随即不成立。
- **清理遍历整个注册表 + 幂等**。`aclose()` cancel+drain+close 全部；`_serve_one` finally 走 `_aclose_background_runtimes()` 清后台、留前台供重连复用；带 MTS 的 runtime **先 `end_managed_session` 再 cancel drain**。重复 cancel/close 同一 runtime 静默放过。**ws 真正断开才清后台 runtime**（连同 handoff 队列 / MTS / bg run），不跨 ws 断连 / app 重启。
- **`MAX_BACKGROUND_ROOMS = 5` 软上限**。`_inbound_switch_room` 在 `_switch_room` 之后调 `_enforce_background_room_limit`：超限淘汰 `background_since_ms` 最小者（强制拆除 + 补一帧 `room_background_finished` + emit info NOTICE）。**软上限**：超限淘汰最早项、不拒绝切房。

### handoff（调度层）

- **handoff 是调度层增量，不改对话原语**。delegate 仍走一根 `transcript.jsonl`。执行驱动是显式入口：`enqueue_handoff` 只入队、`run_pending_handoff` 才跑。队列不落盘（瞬态）；`reset_room` 清队列；切房旧前台 busy 时转后台续跑、队列随后台 runtime 继续 drain，ws 断开才清。
- **`run_pending_handoff` 与 `_run_ai_chain` 严格分流，不互相回落**。drain 队列空 / cap 撞顶就停 + emit `turn_end(next="user")`，不回落 scoring；下次 scoring 须用户消息触发。`submit_user_message` 是唯一编排两者的入口（先 drain 再 chain）。
- **drain loop 每轮 turn 末尾 5 步严格对齐 `_run_ai_chain`**：① peek 算 `cost` 比 cap 得 `next_state` → ② `turn_end(next=next_state)` → ③ `flush_turn` → ④ `_kick_summarize` / `_tick_cooldown` / `_kick_detect_new_artifacts` → ⑤ `if not has_next: return`。三个 hook 不能放 `turn_end` 之前。
- **cap 检查按 item cost 算**。delegate / review cost=1，panel = `len(targets)(+summarizer)`；`_consecutive_ai_turns + cost > max` 不 pop、直接收尾。`run_pending_handoff` 入口清零计数一次，drain loop 内不再清零。
- **server 必经 `_run_handoff_turn` wrapper**。不直接 `create_task(run_pending_handoff)`：wrapper 照搬 `_run_turn` —— swallow `CancelledError`、finally 同槽清 `(_inflight_turn_task, _inflight_kind)`。
- **`_inflight_kind` 三态 + 入队前 cancel 条件性**。`∈ {"user", "handoff", None}`，所有 `_run_turn` task 标 `"user"`。delegate inbound：`=="user"` → cancel 抢占；`=="handoff"` → 只 append 队尾；`None` → 不 cancel。
- **`handoff_clear` 始终 cancel + clear**。无差别 `_cancel_and_drain_inflight()` + 清队列 + emit `handoff_cleared{items_dropped}`，不 partial cancel。
- **handoff envelope emit 职责拆分；`reason` 不进茶客 prompt**。`enqueue_handoff` / `clear_handoff_queue` 纯方法不 emit；`handoff_enqueued` / `handoff_cleared` 由 server emit、`handoff_consumed` 由 orchestrator 在 `TURN_START` 后 emit。
- **review 与 delegate 共用调度层，差异只在 prompt 注入**。drain 同套 cap / cancel / 5 步 / wrapper / `cost=1`；`item.kind` 只分 `scoring_path` 与 `extra_blocks` 两值。
- **review 只支持 `scope=message`，inbound 三道校验**：① `target` 非空 str；② `target` 在场；③ `message_id` 非空且 `room.message_by_id` 命中（drain 时被审消息保证存在的工程基础）。白名单严格 `{type, target, message_id}`。
- **`extra_blocks` 临时块两渲染路径都接，注入位 `<speak_instruction>` 之前**。`build_context_for` 的 `extra_blocks` 对 onboarding / incremental 同步生效；临时块（`<review_target>`）与永久块（`<current_task>` / `<order_hint>`）是两机制。
- **`<review_target>` 只含被审消息原文 + 审阅指引**。`_render_review_block` 现场合成，被审消息走 `format_messages`；不附产物清单。review 是单轮一次性，不做接力链。
- **「请审…」入口只挂带 `message_id` 的气泡**。按钮显隐纯由 `data-message-id` 决定；茶客气泡仅 `status=ok` 后才挂；用户本地 echo 气泡没有，要等 `room_history` 重建。
- **panel = 一个自描述 `HandoffItem`、跑一个 turn**。`HandoffItem` 持 `targets: tuple[str,...]`（≥2）+ `summarizer: Optional[str]`；drain 一次 pop、一个 `turn_id` 串行 speak `len(targets)(+1)` 次。summarizer 是 `winners[-1]`、跑同一 turn。
- **panel 串行执行，「并行」只是 UI 标注 + prompt 提示**。N(+1) 位一个 turn 依次 speak，后发言者看得见前者；`<panel_context>` 块缓解先发言污染。
- **drain loop `kind` 三路分流走三个纯函数**。`_handoff_cost` / `_resolve_handoff_winners` / `_build_winner_blocks`；`_build_winner_blocks` 必须在 runtime 过滤之后调。speak 循环 `zip(winners, winner_blocks)`。
- **panel `cost` 两档 cap 检查 + 跑不起来的队首项就地 drop**。`_advance_to_runnable_handoff` 弹掉死项（cost > max / panel 欠员 / target 删光），每弹一项 WARN + 重发 `HANDOFF_ENQUEUED`。撞预算的项不弹、`break` 等下次。
- **inbound `handoff_panel` 五道校验**：① `targets` 是 `list[非空 str]`；② `len ≥ 2`；③ 无重复且全在场；④ `summarizer`（若有）在场且 `not in targets`；⑤ `len(targets) ≤ min(MAX_PANEL_TARGETS=4, max - has_summarizer)`。
- **handoff propose 复用 `TASK_PROPOSAL` + flat kind**。`data.kind ∈ {decision, open, handoff_delegate, handoff_review, handoff_panel}`，前端 `proposal_card.js` 单层 switch。不新增 envelope 类型、不 bump `schema_version`。
- **propose 不入队、不碰调度层；采纳才走既有 `handoff_*` inbound**。`buildAcceptInbound` 只挑 inbound 白名单内的键，propose-only 字段绝不漏进 inbound。delegate 采纳 `reason` 带 `"<proposer> 提议："` 前缀。
- **`propose_review` propose 时把 `reviewee` 名解析成 `message_id` 并冻结**。用 `room.latest_message_by_speaker_id`，MVP 只支持「reviewee 最近一条」。reviewee 没发过言 → `Error:` 不 emit。
- **采纳后的 `HandoffItem` 与用户直接触发不可区分**。`issued_by` 恒 `HANDOFF_ISSUED_BY_USER`；茶客不能 propose `handoff_clear`（destructive）。propose 永远等用户点，无自动 / 超时采纳。

### 托管任务会话（MTS，P8.3）

- **MTS 是瞬态运行态，每房间最多 1 个，不落盘**（P9 起全局可有多个后台 MTS）。crash / `reset_room` / ws 断开即清；切房旧前台 busy 时转后台续跑，MTS 在后台自驱直到自然收尾。只经 `managed_session_start` inbound 开启，无自动 / 超时 / 茶客 propose 开启。
- **断线即结束 MTS（切房不结束）**。`_serve_one` finally 先 `_maybe_end_managed_session(user_cancel)` 再 cancel drain，否则 drain cancel 后 MTS 既不推进也无人停、重连快照让前端按钮对死调度复活。**`emit_room_snapshot` 重投 MTS**：前台房 `_managed_session` 非空时末尾补一帧 `managed_session_started`（`budget` 是剩余值）。
- **MTS 跑在 handoff drain loop 上，不新开调度路径**。`run_pending_handoff` 每轮 turn 跑完调 `_advance_managed_session_after_turn`，再走既有 5 步；`_managed_session is None` 时该调用立即返回。`_advance` 按「刚跑完是不是管理者回合」分流：管理者回合不做事；worker 回合且队列空 → `budget-=1` + 回调 `delegate(manager)` + emit `managed_session_advanced`。
- **`manager_finished` 只由 drain 收尾兜底产生**。覆盖「管理者没派活 / 调用链续不下去」；6 个 reason：`manager_finished` / `budget_exhausted` / `task_closed` / `cap_reached` / `user_stopped` / `user_cancel`。
- **结束 MTS 必清 `_handoff_queue`**。`end_managed_session(sink, reason)` 任意路径都清待跑项 + 重发空队列快照。`managed_session_stop` 不取消当前 turn（自然跑完）；`handoff_clear` / `cancel` 中途介入时一并结束 MTS（`user_cancel`）。
- **MTS 内只自动入队管理者的 `handoff_delegate` / `handoff_panel` 提议**。`Orchestrator._intercept_task_proposal`（经 `set_task_proposal_hook` 注入）拦下 envelope 不下发前端。非 MTS / 非管理者 / review / decision / status 照常渲卡。
- **管理者 MTS 回合注入 `<managed_session>` 临时块**。`render_managed_session_block(manager, budget)` 经 `extra_blocks` 注入；worker 回合 / 非 MTS delegate 无块。块第 ② 条显式作废 `propose_*` 工具 ack 的「等用户采纳」等待语义。
- **`managed_session_*` 是 hint 型事件**，不进 transcript、不触发 AI；不 bump `schema_version`。`budget` 计管理者复查回合数（kickoff 不耗）；`max_consecutive_ai_turns` 是硬护栏 MTS 不能越过。

### 后台 Agent（P11，bg run / 并行执行）

- **`active_guest_names` 是 `guest_busy()` 唯一数据源**。server 端 `runtime.active_guest_names` 与 orch 端 `orchestrator.active_guest_names` 是同一 set 实例（经 `_attach_runtime_state(runtime)` 注入）。**add 必先于任何 `await`**：bg run inbound 校验通过即同步 `agent_runs[run_id]=run` + `active_guest_names.add(target)`（先于 `create_task`）；前台 / handoff `let_speak` 在 `await guest.speak(...)` 外包 `try: add → finally: discard`。`active_guest_names is None`（裸构 Orchestrator）整段跳过。
- **bg run 占用的茶客不接 `@`、不参与打分**。`find_user_mention` 命中 busy 名整个 `@` token 忽略（`@busy @ok` 仍能路由到 ok）；broadcast winners 过滤掉 busy；scoring 路径 `scorables` / `cooled` 都排除 busy（第三档桶，bg run 跑完自然回 scorables）。
- **`busy_alive() = inflight_alive() or has_active_runs()` 仅 P9「runtime 生命周期 / 房间 busy 展示」分支用**。3 处替换：`_switch_room` demote / `_maybe_self_destruct_background_runtime` / `_rooms_available_with_busy`。**不用于前台 turn 控制** —— `_run_turn` / handoff drain 的 cancel/drain 仍走 `inflight_alive()`。
- **5 步清理（cancel_and_drain_all）race-free**：① 同步 cancel 前台 turn → ② 同步 cancel 全部 `agent_run_tasks` → ③ `await gather(*bg_tasks, return_exceptions=True)` → ④ `await inflight_task` → ⑤ `session.close()`。先同步 cancel 两段再 await drain：drain 期间不会有新 bg run 进入。带 MTS 的 runtime **先 `end_managed_session(user_cancel)` 再 cancel drain**。
- **bg wrapper finally 6 步**：`detect_new_artifacts` → `BatchMessageSink.flush_to(router)` → `agent_runs.pop(run_id)` → emit `AGENT_RUN_{FINISHED,CANCELLED,ERROR}` → **`advance_after_bg_completion`（MTS 续命，仅 `run.mts_managed=True`）** → `active_guest_names.discard(guest_name)` → `_maybe_self_destruct_background_runtime`。**terminal envelope 必在 propose flush 之后**；**MTS 续命必在 terminal 之后**（让前端先收到 bg done 再收到 manager advanced）；**自毁必在 discard 之后**（防读到残留）。任一 step 抛异常仍保证后续 step 执行。
- **MTS × bg run 续命闭环（P11.2.X）**：`AgentRun.mts_managed` + `mts_manager_at_spawn` 由 `_start_agent_run` 在 spawn 时刻按 `(MTS 活, source_guest == ms.manager_guest)` 快照（**spawn 时刻冻结**，不跟随后续 MTS 状态；manager 身份冻结防 MTS 换主后把复查塞错人）。bg wrapper finally 第 ⑤ 步**仅 `terminal_type == AGENT_RUN_FINISHED`** 走 `_continue_mts_after_bg`（cancelled / error 终态跳过 —— 不算管理者复查触发）；该 helper 调 `ManagedSessionOps.advance_after_bg_completion(mts_manager_at_spawn=...)`：MTS 仍活 + 当前 manager 与冻结值一致 + `stop_reason` 不命中 + budget>0 → `budget-=1` + enqueue `DELEGATE(target=manager)` + emit `managed_session_advanced` 返 True；任一守卫不过返 False（其中 stop_reason 命中走 `end_managed_session(reason)`，budget<=0 走 `end_managed_session(BUDGET_EXHAUSTED)`）。续 True 后按 inflight 分流：无 inflight → 起 `_run_handoff_turn` drain；handoff drain 已跑 → 不动；user turn 在跑 → `inflight_task.add_done_callback` 等 user 跑完起 drain（避免 stranded delegate）。`create_task` 失败 → `end_managed_session(user_cancel)` 兜底防悬挂。`run_pending_handoff` 收尾兜底两处 MANAGER_FINISHED 触发都加 `_has_pending_mts_bg()` 守卫。**budget 语义扩展**：「manager→worker（含 bg）→manager 桥扣 1」—— spawn N 个 bg ≈ 用 N budget；同回合 spawn + propose(worker) 会双扣（worker 桥 + bg 桥各 1）。`_has_pending_mts_bg` 经 `_attach_runtime_state` 绑 bound method 到 orchestrator，**承重契约**：orch ↔ runtime 严格 1:1，未来若 session 跨 runtime 复用必须同步经 `_attach_runtime_state` 重绑，否则旧 orch 闭合旧 runtime → drain 守卫看错 dict。裸构 orch 默认 `lambda: False` 保 P11.2.X 之前行为零改。`_start_agent_run` 拒 `target == ms.manager_guest`（防 bg 内 propose 经 intercept hook 污染 MTS 队列）。`/clear room` 与切房 global-guest 分支显式 `_maybe_end_managed_session(user_cancel)` 前置防 step ⑤ 抢跑。
- **pre-start cancel race 兜底 sweep**。`create_task(wrapper)` 后立即 cancel 时 wrapper 没进过 finally —— `cancel_and_drain_agent_runs` 末尾遍历残留 `agent_runs` discard guest_name、再 clear 两 dict。**discard 单条 + 最后 clear** 而非整 set clear（前台 / handoff `let_speak` 也写同一 set）。
- **`BatchMessageSink` 白名单 + run_id 注入**。`_DROP` 显式拒 HANDOFF_* / MANAGED_SESSION_* / AGENT_RUN_*（bg run 不参与 handoff / MTS 调度）；`_PASS` 只放 NOTICE；其余裹 `_with_run_id` 注入。`TASK_PROPOSAL` 缓冲到 wrapper finally 才 `flush_to(router)`（per-envelope try + finally clear buffer，flush 中途异常不漏 buffer），保证 terminal envelope 后于缓冲 propose 到达。
- **bg run 不污染前台取证：`speak(record_debug=False)`**。wrapper 期间 `self._recorder` 替成 `NOOP_RECORDER`，不写 `debug/turns.jsonl` / `debug/prompts/`。
- **bg run inbound `agent_run_start` 四道校验**：① `target` 在场；② `task_id` 若给须 `store.get_task(task_id)` 命中；③ `guest_busy(target) == False`；④ `len(agent_runs) < MAX_AGENT_RUNS_PER_ROOM (=4)`。校验完成与 `agent_runs[run_id]=run` + `active_guest_names.add(target)` 必须先于 `create_task`。
- **4 个 AGENT_RUN_* 事件不 bump `schema_version`**。沿用「旧前端忽略未知 type」口径。`background_runs` 字段加在 `emit_room_info`，前端 `bg_run_bar.applyAll` 整批权威覆盖 + sidebar 指示同步刷；前端 `barEl.hidden = true/false` 切显隐（**不是 `style.display`**，HTML `hidden` 优先级高过 inline display）。

### 茶客能力 introspection

- **`/tools` `/skills` 走单一共享投影 `TeaGuest.describe_capabilities()`**。WebSocket `_inbound_list_guest_caps` 与 CLI `_print_guest_caps` 共调。tools / skills 是 `__init__` 时一次注册的静态集合。查茶客实例必经 `Orchestrator.get_guest(name)`（活字典），不读 `RoomSession.guests`（boot 快照）。`view` 经 `GUEST_CAPS_INFO` 原样回声。
- **能力花名册：装配期一次性解析的不可变快照**。`build_room_session` 解析 `roster: dict[guest→summary]` 三级（手写 → 缓存 → 无），传给 Orchestrator → ContextRenderer。运行期增删茶客整体重建 session，故 renderer 持的是不可变快照，**不做运行期增量更新**。只进 onboarding 的 `<room>` 块「在场」行，**不进**每轮 `<room_update>`。后台生成的新摘要**下次重建 session 才生效**。

### 聊天界面渲染（P10）

- **mermaid 渲染只在 message_end 全文到位时调一次**。流式 delta 期间禁调 —— 半截 mermaid 抛 parse error 闪烁刷屏。`renderMermaidIn` 入口收敛于 `renderGuestText` / `endStreamingMessage` / `task_panel` goal —— 流式 `appendDelta` 不调。失败保留原 `<pre>` + `.mermaid-error` class。
- **mermaid SVG 走手工 sanitize，不能换 DOMPurify**。DOMPurify 对 `<foreignObject>` 强制清空内容，mermaid v11 节点 label 走 `<foreignObject>+HTML` 会被剥光。安全靠 mermaid 自带 sanitize + Electron CSP + 手工剥 `on*` / `javascript:` 三层兜底。
- **挂件渲染按 rel 去重**。`attachArtifactToBubble` 按 `[data-rel="..."]` 查重，防 live + history 双触发。
- **图片预览懒拉、不 eager 内嵌**。`task_artifact_added` 不带 base64；前端渲占位 `<img>` 后发 `download_file purpose=preview`，server 回包后 `resolveArtifactPreview` 灌字节。同 rel 可挂多份。SVG 走 pill 下载链不内嵌（可内嵌 `<script>`）；白名单 `{png, jpg, jpeg, gif, webp}` 内嵌。
- **切房 / clear 必清 pending preview**。`renderSidebar` 调 `clearPendingArtifactPreviews()`，否则 `messagesEl.replaceChildren` 后等待中的 `<img>` 节点已被摘走，preview 字节回包无处灌。

## 测试

`pyproject.toml` 已设 `asyncio_mode = "auto"`，async 测试不用加 mark。`tests/` 当前 ~90 文件 / 1000+ 测，覆盖 orchestrator / scoring / handoff / MTS / task / artifact / persona / server inbound / room runtime / P11 bg run 等。fixture 共享 `tests/conftest.py`（`build_orch` 裸构 Orchestrator / `SpeakingStubGuest` 走真 speak / `task_inbound_srv` 装真房间 + monkeypatch `_run_ai_chain` no-op）。**复现 bug 优先**，先写失败用例再修。跑全量 `uv run pytest`（~45s），单测 `uv run pytest tests/test_xxx.py -v`。
