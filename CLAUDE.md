# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目语义

「茶话室」(chahua) = 多 Agent 群聊桌面 App。用户和多个由 [agentao](https://github.com/jin-bo/agentao) 驱动的 AI「茶客」在同一聊天室对话（像微信群，可 `@`、茶客之间也能接话）。房间不只是聊天容器 —— 还是带任务的工作容器（**P5 任务房间**：开任务 / propose 决策待用户采纳 / `./task/<name>` 自动归集 artifact / 多任务共存单时刻 1 active）+ 自带取证视图（**P6 调试与回放**：每轮可展开看候选 / 分数 / prompt / 工具 / 产物，落盘 `rooms/<id>/debug/`）。

**两个运行形态共享同一套 Python 后端**，都走 `chahua.session.build_room_session()` 装配房间：
- CLI REPL：`uv run chahua`（最快验证 LLM 凭据 / `room.toml`）
- Electron 桌面壳：`cd app && npm run dev` —— main 进程拉起 `chahua-server` sidecar，本地 WebSocket 通信

## 常用命令

```bash
uv sync                                       # Python 依赖（按 pyproject.toml 拉 agentao≥0.4.18）
uv run chahua                                 # CLI（默认入 rooms/p1-test）
uv run chahua --room rooms/p3-黄河路
uv run chahua-server --host 127.0.0.1 --port 7860 --room rooms/p3-黄河路  # 独跑 sidecar
uv run pytest                                 # 全量测试（~45s）
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

- **app_root vs user_data_root**：dev 同源仓库根；packaged app_root=`.app/Contents/Resources`、user_data_root=`~/Library/Application Support/chahua/`。Electron export `CHAHUA_APP_ROOT` / `CHAHUA_USER_DATA`，Python `Paths.from_env()` 接住。**改路径解析双根都顾**（persona 走 `find_in_data_then_app`：user_data 优先回退 app）。
- **sidecar ready 信号**：`server.py` 打 `监听 ws://...`，`sidecar.js` 正则 `/监听\s+ws:\/\//` 匹配后 resolve。
- **首启动 seed**：dev 跳过；packaged 拷 `app/templates/` → user_data_root，`.chahua-seeded` 幂等。
- **跨平台 sidecar 关停**：`sidecar.js::stop()` 先 `child.stdin.end()` → Python `_watch_stdin_eof` 收 EOF graceful；2s grace 兜底 `forceKillTree`（Windows `taskkill /F /T`，POSIX `SIGKILL`）。Windows `connect_read_pipe(sys.stdin)` 常 `WinError 6` 静默失败，tree-kill 是兜底正解。

### Python 后端模块分工

职责一句话；承重契约见「关键不变量」段，实现细节看代码。

- `session.py` — 房间装配。CLI 与 server 共用 `build_room_session()`。
- `config.py` — `room.toml` 解析，白名单严格（未知字段 `RoomConfigError`）。
- `llm_spec.py` — `LLMSpec` 三入口（`from_env` / `try_from_env` / `from_toml`）+ `build_client()`。toml 强制 `model="<provider>/<model>"`，env 允许裸 model。各 section 走自己 spec，缺即 fallback 回房间默认。
- `guest.py` — `TeaGuest`（包 `agentao.Agentao`）。`speak()` 外层 try/except/finally 保 `message_start`/`message_end` 成对，envelope 与 transcript 共享 message_id。P13 视觉：`speak(images_rel=())` → `resolve_images` → `arun(images=...)`，懒读现传。
- `image_input.py` — P13 视觉 helper（server inbound 与 guest 共用）。`_normalize_share_image_rel`（纯校验）+ `resolve_images`（IO：normalize → symlink 围栏 → 读 bytes → base64+MIME）。限额 `from agentao.media_limits`。
- `orchestrator.py` + `_orchestrator_{chain,handoff_drain,handoff_queue,managed_session,scoring,consts}.py` — 意愿打分主循环：并发打分 → 取 ≥ `want_threshold` 前 1~2 名发言 → 无人过阈值等用户；`@<名字>` 确定性路由不打分。5 个 slot 经 `_install_orchestrator_slots(orch)` 单点装配，公开 import 路径不动；主类保留 `_run_ai_chain` / `_intercept_task_proposal` 同名 method（供 monkeypatch / hook 取 bound method）。
- `scoring.py` — 轻量打分。transcript 不可信，严格 JSON、解析失败降级 0、score clamp `[0,1]`。
- `mentions.py` + `orchestrator_config.py` — 前者 `@` 提及解析（broadcast token / 白名单边界防 URL·email 误配 / 全 Unicode 空白）；后者编排参数 frozen dataclass（拆 context_renderer↔orchestrator 循环 import）。均从 orchestrator 抽出，原 import 路径 re-export 不动。
- `room.py` + `cursor.py` + `_persist.py` — 持久化层。`transcript.jsonl` / `summary.jsonl` append-only 加载跳坏行；`cursor.json` tmp+rename。不做 fsync。
- `events.py` — `ChahuaEnvelope`（含 `schema_version`）。茶话室外层合成 room_id / guest_name / message_id（agentao 原生 `AgentEvent` 不带）。
- `transport_bridge.py` — `ChahuaTransport`（`SdkTransport` 子类），agentao 事件 → envelope；P10 artifact 反查 `_maybe_record_artifact_path`。
- `summarizer.py` — cheap LLM 增量产 `summary.jsonl`，onboarding 窗口拼用。
- `permissions.py` — read-only 双 API 同步：统一 `apply_permission_mode(agent, mode_str)`，别处禁单调 `set_mode`。
- `task.py` + `tasks_store.py` — 任务数据模型 + `tasks/state.json` + `tasks/<id>/{task.json, decisions.jsonl, artifacts/}`。`build_task_info_payload` 是 `task_info` 投影单一来源。入站严格 / 落盘宽容。
- `task_tools.py` — 5 个茶客侧 task 工具：`task_list_artifacts`（真无副作用）/ `task_propose_{decision,open,status}`（`is_read_only=True` 但 emit `TASK_PROPOSAL`）/ `task_write_artifact`（绕 `PathPolicy` 让 `./task/<name>` 落盘，`is_read_only=False`；name 拒 `/` `\` `..` `.` 前缀）。
- `handoff_tools.py` — 3 个茶客侧 handoff propose：`propose_delegate` / `propose_review` / `propose_panel`。不带 `task_` 前缀（房间级调度）。`propose_review` 把 `reviewee` 名解析成最近发言 `message_id` 冻结。
- `task_rendering.py` — task block prompt 纯函数。取数由调用方 `_build_context_for` 完成；scoring 与 speak compact 在 goal 口径有意不同。
- `context_renderer.py` + `artifact_detector.py` — 前者输出 6+1 块 XML（`<room>` / `<user_persona>` / `<room_summary>` / `<current_task>` / `<order_hint>` / `<recent_messages>` / `<speak_instruction>`），`quoteattr` 防注入。后者每 pick 周期扫 active artifacts diff `_seen_artifacts`，emit `task_artifact_added` + 重发 `task_info`。
- `user_md.py` — USER.md（用户角色卡）解析，三级回退：`[room].user_md` 显式路径 → `rooms/<id>/USER.md` → 仓库顶层；`<user_persona>` 块取数来源。
- `persona_summary.py` — persona 能力摘要 LLM 生成 + 内容寻址缓存（`<hash>.json`）。三级解析（手写 `summary` → 缓存 → None）；失败 WARN 不阻断。
- `debug_recorder.py` — P6 取证落盘。`TurnRecorder` 由 orchestrator / guest / transport_bridge 三处喂。`max_turns=500` rotation 按 turn_id 整组删，`_turn_count` 内存维护永不重测盘。rotation 失败永不阻断房间。
- `server_room_snapshot.py` — `emit_room_snapshot` 单点装配：`emit_room_info` / `emit_room_history` / `_emit_task_info` / 末尾补 MTS 快照。`turns_index` 严格 `enabled=True` 才挂；`rooms_available` 每房带 `busy=busy_alive()`；P11 加 `background_runs`。
- `persona_import.py` — 本地 / GitHub 导入 persona 包 + P12.6 provenance 生命周期。provenance 落 `.chahua-source.json`；`update_persona(force=)` 走 `_replace_dir_atomic` 原子 swap。`_GitHubError` 子类 `PersonaImportError` 带 `.code` 分流 404/403。
- `persona_assets.py` + `trust.py` — persona sibling `mcp.json` + `skills/` 装载；MCP 走信任门（`persona-trust.json` + UI popover）。skills 软链 + copytree 兜底。
- `mcp_thread.py` — P17 thread-backed MCP 管理器：agentao 同步 `McpClientManager` 在 ws 事件循环内 `run_until_complete` 会炸，全部 MCP async 工作挪到常驻线程（owner-task 同 task 进出 exit stack，见 P17 不变量）。
- `persona_manifest.py` — P12 `persona.toml` 解析。`PersonaManifest` frozen dataclass（P12.6 加可选 `version` 纯展示字段）+ 三级严格白名单；文件与 dry-run 两入口共享 `_parse_dict`。全失败统一 `PersonaManifestError`。
- `server.py` + `server_inbound_{admin,task,io,settings,handoff,agent_run}.py` + `_server_helpers.py` — ws 生命周期 / 帧路由 / room snapshot 在 `server.py`；30+ `_inbound_*` 切到 6 个 handler 类（组合非多继承）。`_install_handler_slots(srv)` 是装配唯一真理源；`_INBOUND_ROUTES` 帧 → 属性路径映射在 `server.py` 顶层。**改 `_inbound_*` 先看 slot 归属**。
- `agent_run.py` + `agent_run_sink.py` + `agent_run_tools.py` — P11 后台 agent run：`AgentRun` frozen dataclass + `BatchMessageSink`（白名单过滤 + `TASK_PROPOSAL` 缓冲到 finally + `run_id` 注入）+ P11.2 茶客侧 `spawn_agent_run(s)` 工具（立即起并发后台 run，区别于等用户采纳的 `propose_*`）。`server.py::_run_agent_background` 是与 `_run_turn` 平行的 bg 执行 wrapper。
- `handoff.py` — 调度层数据模型。本模块是「调度数据形状」单一来源。
- `room_runtime.py` — P9 多 runtime 注册表。`RoomRuntime` 持运行态 + 谓词（`busy_alive` / `guest_busy` / `guest_in_bg_run`）。`_attach_runtime_state(runtime)` 把 `active_guest_names` 与 `_has_pending_mts_bg` 注入 orchestrator —— orch ↔ runtime 严格 1:1。
- `message_artifacts.py` — P10 `MessageArtifactRegistry`。per-room 落 `rooms/<id>/message_artifacts.jsonl`，把 artifact rel path 反查回 originating message_id。`reset_room` clear / `/clear task` 不清。
- `exporter.py` — 房间 transcript → markdown 导出（`export_room` inbound）。服务端拼整段 markdown + 安全文件名，前端 Blob 下载到用户机器，不写房间目录。

### WebSocket 线协议

- **下行**：每帧一条 JSON = `ChahuaEnvelope.to_dict()`。
- **上行**：每帧一条 JSON，`type` 全集见 `server.py` 顶层 `_INBOUND_ROUTES`。未知 `type` WARN 后忽略；非 JSON / 二进制帧 → `close(UNSUPPORTED_DATA)`。`set_llm_credentials` 严格白名单 `{provider, model, base_url?, api_key}`，未知键 NOTICE error 丢帧（敏感帧不静默吞）。

## 关键不变量

承重契约，编辑代码必须遵守。本节只留「规则 + 关键 why」；**完整 rationale / P-版本溯源 / 回归测试见 `docs/INVARIANTS.md`**，改不变量两处同步。

### 配置与 LLM 装配

- **`room.toml` 未知字段必报错**。typo 别静默吞。
- **`[room].name` 跨房唯一**。envelope `room_id` 用 `room.name`，P9 前端据它分流前台/后台；同名让后台 envelope 污染前台。`create_room` / `update_room_toml` 拒重名。
- **toml `model` 形如 `<provider>/<model>`**。首个 `/` 拆 provider，无 `/` → 错。CLI / dotenv 例外允许裸 model。
- **LLM section 整段写或整段不写**。`[room.llm]` / `[scoring]` / `[summary]` / `[[guest]]` all-or-nothing；fallback 走 section 级（缺整段回上一档），不做字段级 overlay。
- **房间默认 LLM 两层 fallback**：`[room.llm]` → `LLMSpec.try_from_env()`，都缺 → 错。toml 显式配置时 env 完全忽略。
- **API key 永不进 toml / envelope**。toml 最多写 `api_key_env`；envelope 只下发 `api_key_env` 名 + `api_key_ready` bool。
- **P15 desktop 登录态注入 LLM 凭证：纯 env、绝不写 toml、绝不进日志**。`set_llm_credentials`（前台房专用，严格白名单 `{provider, model, base_url?, api_key}`）只改 `os.environ` → cancel 全前台 → `_replace_session` 热重建；改 env 前快照旧值，失败 / 异常逐个回滚 + 保留旧 session。粒度只动「房间默认」，显式 LLM section 钉住。raw key 只活在进程 environ（`api_key` 校验 `redact=True`；`base_url` 日志剥到 `scheme://host`）。启动期凭证是硬依赖：host 必须 spawn 前注入全套 LLM env。详见 `docs/P15-桌面登录态自动注入 LLM 配置.md` + `docs/INVARIANTS.md §P15`。
- **`[[guest.extra_mcp_servers]]` 自动信任，persona sidecar `mcp.json` 走 trust 门**。前者用户手写=意图；后者可能 GitHub 导入任意可执行须 UI 勾选。同名房间级覆盖 persona。
- **isolation 切换不自动迁移记忆**。`isolation` 决定茶客 cwd；切换后旧路径 `.agentao/memory.db` / `sessions/` 原样保留。

### 发言权重与手动模式（P16）

承重契约；完整 rationale / 反向评审溯源见 `docs/P16-发言权重与手动模式.md`，改不变量两处同步。

- **`schedule_mode` 只换 auto-pick 第 3 档；`@mention` / `@broadcast` 所有档下字面不变**（前两档确定性路由 score=1.0，不打分、不吃 talkativeness）。`manual` 档第 3 档恒返 `[]` + 零打分 LLM 调用。
- **调度档与 handoff / MTS 正交**——它们走 drain loop 不经 auto-pick，`manual` 房照常工作。
- **`talkativeness` 仅偏置 `scoring` 档 auto-pick：`effective = clamp(base × talk, 0, 1)`**。**绝不**作用 `@`/broadcast/handoff/MTS/cooldown、**绝不**进打分 prompt；仅 `ScoreKind.SCORED` 被偏置。实时 `data.scores` 只发 effective；`record_scoring` 仅 talk≠1.0 时补 `base_score`/`talkativeness`；加字段不 bump `schema_version`。
- **默认 coalesce：`None`→`1.0`，`0.0`=合法哑茶客，禁 `or 1.0`**。范围 `[0.0,4.0]` + 拒 NaN/inf（TOML 允许 `nan` 字面量，`score×NaN` 永久静默茶客）。
- **两旋钮走 `swap_room_config` 轻热替，不走 `_replace_session`**、不 cancel in-flight。`schedule_mode` 经 `_make_orchestrator_config`（`explicit` 入参非空时完全优先）；talkativeness 经 `update_talkativeness` 只刷已存在 name（增删走 `_replace_session`）。配置闭环必经四点：`config.py` 定义 → `session.py` 穿参 → admin mutator + `admin_toml.py` 回写（默认值不写盘，显式 `0.0` 保留）→ 本不变量。

### persona 包 manifest（P12）

承重契约完整版见 `docs/P12-persona 包 manifest.md`「承重不变量」段，改不变量两处同步。

- **`persona.toml` 顶层 `schema_version` 必填且唯一合法值=1**；加字段不 bump，破坏性变更才 bump。
- **三级严格白名单，未知键 → `PersonaManifestError`**；`[defaults.guest]` 严格 ⊂ `_ALLOWED_GUEST_KEYS` 且不含 `name` / `persona` / LLM 四件套（防泄漏作者本地 env 变量名）。
- **`[defaults.guest]` + 顶层 `summary` 仅「加入房间」时一次性 inflate**，之后与 manifest 解绑（npm `package.json` 语义）。
- **picker display_name 三级 fallback**：manifest.display_name → `<stem>.toml`.`[guest].name` → 文件名 stem。
- **permission / isolation 默认值合一仅在 admin 层；全链路 `None` 表「未显式选」**，coalesce 一律 `is not None`，**禁** `or DEFAULT_MODE`（吞 `""` 等坏值）。
- **消费路径严格 fail-fast；`discover_personas` 是唯一 `WARN+None` 例外**。`persona_import` 在写盘前对根级 `persona.toml` 字节 dry-run。
- **仅 dir-form 才查 manifest，flat-form 跳过**（两处守卫）；`_try_p12_1_dir_form_rewrite` shim 自动把内置 flat-form 路径升级 dir-form。

### Personas 更新（P12.6）

承重契约见 `docs/P12.6-Personas 更新.md`「承重不变量」段，改不变量两处同步。

- **provenance 是消费侧安装元数据，住 `.chahua-source.json`，绝不进 `persona.toml`**。进 `_SKIP_NAMES`，更新时由 importer 新鲜写。
- **provenance 读容错（唯一允许 `WARN+降级` 的 persona 路径，与 manifest fail-fast 有意相反）**。坏 provenance 不让 persona 消失，只去「更新」、留「删除」（`status="source_unavailable"`）。
- **`version` 是纯展示字段，绝不参与 `status` 判定**——「变没变」只由 commit SHA / content_hash 定，不解析 semver。
- **更新 = 全量替换 + `_replace_dir_atomic` 原子 swap，绝不原地改写**；manifest dry-run 在 swap 之前。本地已改 → 默认拒，须 `force=True`（`status` 与 `local_modified` 两正交维度）。
- **更新/删除不影响在跑的房间**，下次 session 重建才生效。
- **「已安装」列表 = `user_data_root` 下所有 dir-form persona；provenance 只 gate「检查/更新」不 gate「列出/删除」**。删除只 `rmtree` 一个目录，`_user_persona_dir` 强校验防穿越；检查/更新（网络型）走 `asyncio.to_thread` 不阻塞 WS 循环。
- **`PERSONAS_INSTALLED` 是权威全量快照，按需发，绝不进 `room_info`**。4 个新 inbound + 1 envelope 不 bump `schema_version`。

### 权限、持久化、事件契约

- **read-only 必双 API 设**。`PermissionEngine.set_mode()` 与 `tool_runner.set_readonly_mode()` 同设，统一走 `apply_permission_mode`。
- **persistent jsonl 加载跳坏行**。最后一行截断不应让用户失去整个房间历史。不做 fsync。
- **envelope `message_start` / `message_end` 必成对**。`TeaGuest.speak()` 外层 try/except/finally 是承重墙。
- **打分输入不可信**。严格 JSON + 解析失败降级 0 + clamp `[0,1]`；`@提及` 走确定性路由不进打分。
- **`download_file.purpose` 仅前端分流，server 行为对取值无差异**。inbound 可选 `purpose ∈ {download, preview}`（默认 download），envelope 原样回声，server 无白名单。前端 `purpose==="preview"` 单分支；preview 失败塞 `<img>.alt` + `.artifact-image-error`。

### 视觉图像输入（P13）

完整 rationale 见 `docs/INVARIANTS.md` §P13，改不变量两处同步。

- **降级归 agentao，chahua 不复制**。chahua 只把 `images=[...]` 传进 `arun()`；不写 `_is_image_unsupported`、不维护 per-provider 视觉能力表。
- **视觉附图纯瞬态，base64 懒读不入库**。`images_rel` 形参沿当前 turn 透传即弃，不动 `Message` / transcript / envelope / `schema_version`；transcript 只留 `<attachment .../>` 文本标记（非视觉茶客 / 打分 / 历史轮次靠它）。debug 记 rel 不记 bytes。
- **附图范围 = 本轮触发用户消息，且只进 `_run_ai_chain` 第一周期**（本地 `first_cycle` flag，非被 pre-drain 污染的 `_consecutive_ai_turns==0`）；接力 / drain / dormant kickoff / handoff / MTS / bg run 一律退文本标记。**打分永不吃图**。
- **图类型按扩展名白名单 `{png,jpg,jpeg,gif,webp}` 映射 MIME，不扩协议、不嗅探**；inbound 筛图 + resolve 双点共用 `_normalize_share_image_rel`。
- **读盘双层防穿越（段形校验 + symlink 两侧 resolve）+ 0 字节跳过**（空 base64 会让 agentao 预校验抛错整条 speak 失败）。

### 任务房间

- **入站严格、落盘宽容**。inbound 白名单未知键 → NOTICE error 丢帧；落盘未知字段 warn 后忽略。
- **`task_id` 只活在 `envelope.data`**，顶层与 `schema_version` 不动。
- **`TASK_INFO` 是权威快照，其它 4 个是 hint**——任意 task 变更后重发整份。
- **task / decision 写权限只在用户**，茶客只能 propose；例外：artifact 写 `./task/<name>` 自动归集，不走采纳。
- **`attach_artifact` 是 copy 不是 move**（`share/` 被历史消息引用，move 断引用）。
- **多任务共存，单时刻最多 1 active**。`open_task` 自动 `set_active`；`set_active_task` 走前强制 `_cancel_and_drain_inflight`。
- **通道 2 软链：茶客 `./task/` 跟 active task**。装配末尾 + open/set_active/close 三处刷新；active=None 只解不建；Windows 走 junction。
- **task summary cursor 内存推进、`session.close()` 统一 flush**。
- **`task_propose_status` 采纳按终结态分流**：非终结 → `update_task`；`done`/`abandoned` → `close_task`。
- **propose 不写库、采纳才入库**。采纳由 `proposal_card.js::buildAcceptInbound` 拼回既有 inbound，server handler 零改动。
- **task 事件是 UI 系统气泡，不进 transcript / 不触发 AI**（代价：task 操作不唤醒房间，要茶客介入用 `@` / handoff / 用户消息）。
- **`./task/` 读用 `read_file`、写走 `task_write_artifact`**（symlink 解析后出 workdir，原生 `write_file` 被 `PathPolicy` 拒；专用工具直调 `tasks_store.artifacts_dir` 绕开）。
- **用户上传须 `ArtifactDetector.mark_seen`**，否则下轮 `detect()` 把用户上传当茶客新产物重发。
- **`/clear task` 只删 `tasks/<task_id>/artifacts/` 可见文件**，task.json / 决策 / 状态 / 摘要游标都不动。
- **`message_artifacts.jsonl` 与 transcript 同生命周期（P10）**：`reset_room` 同步 clear、`/clear task` 不清；加载跳坏行 + 严格校验。
- **`originated_message_id` 仅由 `task_write_artifact` 路径派生（P10）**；shell / MCP 工具走 TOOL_START / TOOL_COMPLETE 前后扫盘 diff 回填 pending，**滚动 baseline 仅在 bind 内更新**，失败走 `_rollback_pre_pending`。

### context 渲染与 prompt 装配

- **P14 起系统生成 prompt 全量英文化 + 逐块精练**。用户内容不动、输出语言随对话（塑造茶客回复的块带语言锚点）；`_speak_instruction_block` 带 recall 段落。**精练承重约束**：只删冗余、**不改功能性字面量**（工具名 / 路径 / `Error:` 前缀 / XML 标签·属性名 / raw status 枚举 / JSON 字段）、**MTS `<managed_session>` 5 条指令顺序不变**。
- **`format_messages` 每条消息走 `<message>` 包裹**，`room.py` 单点定义 4 调用点共享；分隔符语言无关 `{display}: {text}`。
- **context_message：XML 包外层 + Markdown 渲内层**。onboarding 6+1 块、incremental 4 块，无内容整块省略；新块同步加 `tests/test_render_onboarding_xml.py`。
- **通道 1 两态注入：onboarding / incremental 都贴 task 块**（只 onboarding 注入会让 task 视野在多数短轮失效）。
- **task_id 经形参透传，不在渲染层读 store**（`_render_task_block` 纯函数）；closed / 已删 task 不注入。
- **task block 预算：full ≤300 / compact ≤80 token**，compact 短路省 IO。`./task/` 落盘文案分层 compact 极简 / full 详细（承重语义点回归 `tests/test_render_task_block.py`）。
- **打分 prompt 有自己的极简 `<current_task>`（`render_scoring_header`），与 speak compact（`render_task_header`）口径有意不同**；每 pick 周期 1 次 `get_task`，N scorer 共享。
- **speak 与 scoring 的 order-hint 常量不能合并**（措辞不同，互换污染对方阶段）。

### debug 取证与回放

- **`turns_index` 严格 `enabled=True` 才挂**（≤1000 倒序）；`TURN_DETAIL.data.prompts` 字段始终存在，内部 key 三重满足（enabled && capture_prompts && 文件可读）才出现。
- **`fetch_turn_detail` 的 `turn_id` 严格 `^turn_[0-9a-f]+$`** 拒穿越；缺失 / debug 关 → `found=false` 不发 NOTICE。
- **历史详情统一 evict + 还原索引行**：`MAX_TURNS_IN_MEMORY=50` 实时+历史共用，索引行常驻不 evict。
- **rotation 按 turn_id 整组删 + 失败永不阻断**；触发点固定 `__init__` 末尾 + `flush_turn` 成功后两处，`_turn_count` 内存计数即权威；`max_turns=0` 关 rotation 不关 debug、负数 → `RoomConfigError`。
- **`max_turns` 配置闭环必经四点**（config 定义 → session 装 → admin + toml 回写 → 本不变量），漏一点字段被静默吞。
- **`clear_room` 同步擦 debug 取证 + 清茶客 `agent.clear_history()`**（盘上 `memory.db` 不动，异常按茶客隔离 WARN）。

### 切房与后台 runtime（P9）

- **多 `RoomRuntime` 注册表 + 单前台指针**。`RoomEventRouter` 是 per-room 可变路由 sink：切房只翻 `router.mode`（foreground 全量 / background 里程碑白名单），in-flight turn 路由自动跟随。
- **切房两阶段、不 cancel**：阶段一准备目标 runtime（失败即 `return`，切房原子）；阶段二 demote 旧前台（busy → 转后台续跑、idle → close）。同房重建走 `_replace_session` + 先 cancel+drain。**前台房有 `isolation=global` 茶客切走前必 cancel+drain**（共享 cwd 软链会被 retarget），后台 runtime 永不含 global 茶客。
- **后台 runtime 仅在真有 in-flight 活或 dormant MTS 时存在**，wrapper finally 自毁；`busy_alive` 把 `has_managed_session()` 计入 busy；切回竞态由 `_switch_room` 先翻 `mode=foreground` 化解。
- **清理遍历整个注册表 + 幂等**；带 MTS 先 `end_managed_session` 再 cancel drain。**ws 真正断开才清后台 runtime**。
- **`MAX_BACKGROUND_ROOMS = 5` 软上限**：超限淘汰 `background_since_ms` 最小者 + NOTICE，不拒绝切房。

### handoff（调度层）

- **handoff 是调度层增量，不改对话原语**。`enqueue_handoff` 只入队、`run_pending_handoff` 才跑；队列瞬态不落盘，`reset_room` 清、ws 断开清、切房 busy 转后台续 drain。
- **drain 与 `_run_ai_chain` 严格分流不互相回落**（队列空 / cap 撞顶就停 + `turn_end(next="user")`）；`submit_user_message` 是唯一编排两者的入口（先 drain 再 chain）。
- **drain 每轮末尾 5 步严格对齐 `_run_ai_chain`**：① peek 算 cost → ② `turn_end` → ③ `flush_turn` → ④ summarize/cooldown/detect 三 hook → ⑤ 无下一项 return。hook 不能放 `turn_end` 之前。
- **cap 按 item cost 算**（delegate/review=1，panel=`len(targets)(+summarizer)`），超预算不 pop 直接收尾；入口清零计数一次，loop 内不再清。
- **server 必经 `_run_handoff_turn` wrapper**（swallow `CancelledError` + finally 清槽）。`_inflight_kind` 三态 `{"user","handoff",None}`：delegate inbound 按其 cancel 抢占 / 只 append / 不 cancel；`handoff_clear` 始终无差别 cancel + 清队列。
- **emit 职责拆分**：`handoff_enqueued` / `handoff_cleared` server 发、`handoff_consumed` orchestrator 发；`reason` 不进茶客 prompt。
- **review 与 delegate 共用调度层，差异只在 `extra_blocks` prompt 注入**（临时块注入位 `<speak_instruction>` 之前，与永久块两机制）。review 只支持 `scope=message`、inbound 三道校验；`<review_target>` 只含被审原文 + 指引，单轮一次性；「请审…」按钮显隐纯由 `data-message-id` 决定。
- **panel = 一个自描述 `HandoffItem` 跑一个 turn，串行执行**（「并行」只是 UI 标注；后发言者看得见前者，`<panel_context>` 缓解先发言污染）。summarizer 是 `winners[-1]`；inbound 五道校验（`MAX_PANEL_TARGETS=4`）。
- **drain `kind` 三路分流走三个纯函数** `_handoff_cost` / `_resolve_handoff_winners` / `_build_winner_blocks`（runtime 过滤之后调）；跑不起来的队首项 `_advance_to_runnable_handoff` 就地 drop + WARN，撞预算的不弹、`break` 等下次。
- **propose 复用 `TASK_PROPOSAL` flat kind、不入队；采纳才走既有 `handoff_*` inbound**，`issued_by` 恒 USER、与用户直接触发不可区分；无自动 / 超时采纳，茶客不能 propose `handoff_clear`。`propose_review` propose 时把 reviewee 冻结成最近发言 `message_id`（没发过言 → `Error:` 不 emit）。

### 托管任务会话（MTS，P8.3 / P8.4）

完整明细见 `docs/INVARIANTS.md §P8.3/P8.4` + `docs/P8.4-MTS 待机闭环.md`，改不变量两处同步。

- **MTS 是瞬态运行态，每房最多 1 个，不落盘**；只经 `managed_session_start` 开启。**断线即结束（切房不结束）**：`_serve_one` finally 先 `_maybe_end_managed_session(user_cancel)` 再 cancel drain；snapshot 对前台活 MTS 补帧 `managed_session_started`（budget 是剩余值）。
- **跑在 handoff drain loop 上，不新开调度路径**：每轮跑完调 `_advance_managed_session_after_turn`——管理者回合没派活 = **dormant 不是 finished**；worker 回合且队列空 → `budget-=1` + `delegate(manager)`。drain 收尾队列空 + MTS 活 → 保持 dormant 不终结。
- **5 个停止 reason**（`MANAGER_FINISHED` 已退役）：`budget_exhausted` / `task_closed` / `cap_reached` / `user_stopped` / `user_cancel`——终结只能因有界资源或用户显式行为；`task_closed` 含「MTS 任务不再 active」。
- **dormant 复活：`submit_user_message` pre-enqueue manager DELEGATE kickoff + 跳过 `_run_ai_chain`（P8.4.7）**；manager 不在场/正忙 → 走常规 chain；chain 后 re-drain（`reset_cap=False`）保留。
- **`managed_session_start` 拒收 bg-run busy manager（②b，只查 `guest_in_bg_run`）**——否则 kickoff 被 busy-winner 守卫静默 drop，UI 收到 started 却永无 kickoff。
- **proposal intercept：MTS 内只自动入队管理者的 delegate / panel 提议**（`_intercept_task_proposal` 拦 envelope 不下发前端）；summarizer 校验用 `busy - {manager}` 放行「workers 讨论我汇总」（P8.4.9）；**自指派 early swallow（P8.4.11）**：`target == manager` 调下游前直接吞。
- **dormant 期 task 状态变更经 `check_after_task_change(sink)` 主动收尾**（4 个 task inbound 末尾调一次）。
- **结束 MTS 必清 `_handoff_queue`**；`handoff_clear` / `cancel` 中途介入一并结束（`user_cancel`）。
- **管理者回合注入 `<managed_session>` 临时块**（worker 回合 / 非 MTS delegate 无块）；`managed_session_*` 是 hint 型事件不进 transcript、不 bump `schema_version`；budget 计管理者复查回合（kickoff 不耗），`max_consecutive_ai_turns` 是硬护栏；前端 dormant 子态由三源派生不发新 envelope。

### 后台 Agent（P11，bg run / 并行执行）

完整明细见 `docs/INVARIANTS.md §P11`，改不变量两处同步。

- **`active_guest_names` 是 `guest_busy()` 唯一数据源**（orch ↔ runtime 同一 set，经 `_attach_runtime_state` 注入）。**add 必先于任何 `await`**（先于 `create_task`）；前台/handoff `let_speak` 包 `try: add → finally: discard`。
- **`guest_busy`（此刻占用）vs `guest_in_bg_run`（不可抢占）二分**：handoff inbound 准入必须用后者（用全集会误判可抢占的前台发言，破坏 cancel+drain 抢占）；drain 自身 busy-winner 守卫仍用全集。
- **bg run 占用的茶客不接 `@`、不参与打分**（mention / broadcast / scoring 三处都排除 busy）。
- **`busy_alive()` 仅 P9「runtime 生命周期 / busy 展示」用，不用于前台 turn 控制**——`_run_turn` / drain 的 cancel/drain 仍走 `inflight_alive()`。
- **5 步清理 race-free**：同步 cancel 前台 → 同步 cancel 全部 bg → await gather(bg) → await inflight → close；带 MTS 先 `end_managed_session(user_cancel)`。pre-start cancel race 由 `cancel_and_drain_agent_runs` 末尾 sweep 兜底（discard 单条 + 最后 clear）。
- **bg wrapper finally 6 步顺序锁死**：detect → sink.flush → pop run → terminal envelope → MTS 续命 → discard + 自毁；任一步抛异常仍保后续。
- **MTS × bg 续命按 spawn 时刻快照**（`mts_managed` / `mts_manager_at_spawn` 冻结，不跟随后续状态）；`_start_agent_run` 拒 `target==manager`；续命只认 `AGENT_RUN_FINISHED`，之后按 inflight 分流起 drain。budget 语义：spawn N 个 bg ≈ 用 N budget。
- **`BatchMessageSink` 白名单 + `run_id` 注入**；`TASK_PROPOSAL` 缓冲到 wrapper finally 才 flush（terminal envelope 先到）。bg run 取证走 `NOOP_RECORDER` 不污染前台。
- **inbound `agent_run_start` 四道校验**（在场 / task_id 命中 / 不 busy / `MAX_AGENT_RUNS_PER_ROOM=4`），校验 + 两 dict 写入必须先于 `create_task`。
- **4 个 AGENT_RUN_* 不 bump `schema_version`**；`background_runs` 挂 `emit_room_info`，前端 `barEl.hidden`（不是 `style.display`）切显隐。

### 茶客能力 introspection

- **`/tools` `/skills` 走单一共享投影 `TeaGuest.describe_capabilities()`**。WebSocket `_inbound_list_guest_caps` 与 CLI `_print_guest_caps` 共调。tools/skills 是 `__init__` 一次注册的静态集合。查茶客实例必经 `Orchestrator.get_guest(name)`（活字典），不读 `RoomSession.guests`（boot 快照）。
- **能力花名册：装配期一次性解析的不可变快照**。`build_room_session` 解析 `roster: dict[guest→summary]` 三级，传给 Orchestrator → ContextRenderer。运行期增删茶客整体重建 session，故不做运行期增量更新。只进 onboarding `<room>` 块「在场」行，**不进**每轮 `<room_update>`。后台生成的新摘要下次重建 session 才生效。

### 聊天界面渲染（P10）

见 `app/CLAUDE.md`（动 `app/` 下文件时自动加载）。

### 只读长期记忆（GuanLan MCP，P17）

承重契约见 `docs/P17-只读长期记忆-GuanLan-MCP.md`，改不变量两处同步。

- **纯只读消费，chahua 不碰 GuanLan 写路径**。写入策展全在 chahua 外（人工 `guanlan ingest`）；是第三层记忆，正交于会话窗口与茶客私有 `.agentao/memory.db`。
- **主线 agent-pull：persona `mcp.json` 裸 `url`（agentao ≥0.4.14 默认按 Streamable HTTP 连）+ trust 门**；`[[guest.extra_mcp_servers]]` 白名单仍 stdio-only。
- **召回成本靠现有不变量有界**：只在胜出茶客 `speak()` 发生，打分从不调工具。
- **召回内容按不可信数据处理**。MCP result 原样回 `agent.messages`、chahua 不包裹转义——第 1 跳靠 persona 纪律 + 挂记忆茶客 `permission="read-only"` 兜炸半径；第 2 跳已被 `format_messages` 转义 + 打分不可信兜住。
- **`mcp_thread` owner-task：同一 task 进出 exit stack**，**禁**拆成两个 `run_coroutine_threadsafe` task（anyio task group 跨 task 出栈抛 cancel-scope 错，回归测钉死）。孙博士示例不打包 / 不 seed，不 bump `schema_version`。

### 消息回溯与历史搜索（P18）

承重契约见 `docs/P18-消息回溯与历史搜索导出.md`，改不变量两处同步。M1（当前房搜索）+ 回溯（撤回未回复末条）已落地；导出过滤 / 全局·正则搜索仍为设计。

- **transcript 主体 append-only，撤回是唯一全量重写入口**。`Room.truncate_last()` 经 `write_jsonl_atomic`（tmp+rename）整体重写——**先写盘后弹内存**；Room 只弹末条、语义无知，谁能撤全由 server 判。
- **撤回是 server 权威、免 message_id、无 payload**（严格空白名单）。**免 id 是承重**：本地 echo 气泡落盘前无 id，带 id 会让「刚发完立刻撤回」挂不上。
- **撤回三段硬校验不得放宽**：① 房间 idle 用 `busy_alive()` **而非 `_inflight_alive()`**（后者不含 bg run，会 orphan 一条即将到达的茶客回复）；② 末条存在且是 user；③「其后无茶客回复」由②蕴含。
- **主状态截 transcript，cursor 与各茶客 `agent.messages` 一律不动**（顺手 `clear_history` / 回退游标即为 bug）。旁路收尾同 tick 原子、顺序固定：**先 `cancel_pending_summarize()`**（防在跑摘要把陈旧 span append 回盘绕过截断）→ `Summarizer.truncate_after` → `TaskSummaries.truncate_after`（**遍历 store 全量任务**并立即 flush）→ `TurnRecorder.drop_turns_from`；summary 收尾包 try/except；末尾 `_emit_room_snapshot` 全量重发。
- **搜索纯只读，不碰任何状态**。`search_room` → `SEARCH_RESULTS`；`re.escape`+`IGNORECASE` 原文子串匹配、query 长度封顶、results 只带 `speaker_id`；不挡 inflight。
- **前端撤回按钮动态挂唯一「当前可撤回」用户气泡**：MutationObserver + `endStreamingMessage` 双触发，跳过 `.sys`/`.error`；`jumpToMessage` 对隐藏目标返 false 让调用方兜底。

## 测试

fixture 共享 `tests/conftest.py`（`build_orch` 裸构 Orchestrator / `SpeakingStubGuest` 走真 speak / `task_inbound_srv` 装真房间 + monkeypatch `_run_ai_chain` no-op）。**复现 bug 优先**，先写失败用例再修。
