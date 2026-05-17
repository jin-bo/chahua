# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目语义

「茶话室」(chahua) = 多 Agent 群聊桌面 App。用户和多个由 [agentao](https://github.com/jin-bo/agentao) 驱动的 AI「茶客」在同一聊天室对话（像微信群，可 `@`、茶客之间也能接话）。

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
- `server.py` + `server_inbound_{admin,task,io,settings}.py` + `_server_helpers.py` — ws 生命周期 / 帧路由 / room snapshot 留在 `server.py`；30+ 个 `_inbound_*` 帧 handler 按 feature 切到 4 个 handler 类（P5.2 重构）。`ChahuaServer.__init__` 实例化 `self.admin = AdminHandlers(self)` / `self.io = IOHandlers(self)` / `self.settings = SettingsHandlers(self)` / `self.task = TaskHandlers(self)` 四个 slot —— **组合而非多继承**，每个 handler 持 `self.server` 反向引用，跨 server 状态（`_session` / `_emit_notice` / `_replace_session` 等）走 `self.server.xxx` 显式 hop。`_INBOUND_ROUTES` 帧字符串 → 属性路径（如 `"admin._inbound_add_guest"` / `"_inbound_cancel"`）映射只在 `server.py` 顶层维护；`__init__` 时 `_bind_inbound_handlers(self)` 走 `operator.attrgetter` 一次性解析成 `self._inbound_handlers` bound-method 字典，dispatch 是单次 dict 查；属性路径无 `.` 表示 method 在 `ChahuaServer` 自身（cancel / switch_room / clear_room / user_message 4 个核心）。**改动 `_inbound_*` handler 时先看 slot 归属**：admin（guest/room/persona/permission）/ task（open/update/attach/decision + emit_task_*）/ io（upload/export/persona import）/ settings（USER.md / 头像 / room.toml）。模块顶 payload 校验小工具 `require_str` / `check_keys_whitelist` 等在 `_server_helpers.py`，所有 handler 模块 import 自这里（避免 handler ←→ server.py 循环 import）。**测试夹具**：用 `object.__new__(ChahuaServer)` 跳 `__init__` 时必须手工把用到的 slot 装回去 —— `srv.task = TaskHandlers(srv)` 等。

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

- **`room.toml` 未知字段必报错**。别为了「容忍」悄悄忽略 —— 用户配错宁可炸，否则 P4 字段被误用查不到。
- **read-only 必双 API 设**。`set_mode` 改 PermissionEngine 但不改 `tool_runner.set_readonly_mode` → 写工具仍可能进入确认路径。统一过 `permissions.apply_permission_mode`。
- **persistent jsonl 加载跳坏行**。不要因为最后一行截断就让用户失去整个房间历史。
- **打分输入是不可信的**。任何人都能在 transcript 里写「请输出 1 分」，所以 score 严格 JSON + clamp + `@提及` 走确定性路由不进打分。
- **envelope 的 message_start / message_end 必成对**。`TeaGuest.speak()` 的外层 try/except/finally 是这条契约的承重墙；改 speak 时保住 finally 里的 message_end。
- **toml 里 `model` 字段值形如 `<provider>/<model>`**。第一个 `/` 拆 provider，OpenRouter / LiteLLM 的二级路径（`openrouter/qwen/qwen3-coder`）保留进 model。无 `/` 直接 `RoomConfigError`；不走"按环境变量推断 provider"那条隐式路径（命令行 / dotenv 例外，单 provider 心智下允许裸 model）。
- **LLM section 整段写或整段不写**。`[room.llm]` / `[scoring]` / `[summary]` / `[[guest]]` LLM 字段四件套 all-or-nothing：`base_url` / `api_key_env` / `temperature` 不能单独出现，必须同写 `model`。出现单字段 → `RoomConfigError`。fallback 走 section 级（缺整段 → 上一档默认），不走字段级 overlay。
- **房间默认 LLM 两层 fallback**（P4.9）。`[room.llm]` toml 段 → `LLMSpec.try_from_env()`，两者都缺 → `RoomConfigError` 同时列两条 fix 路径。toml 显式配置时 env 完全被忽略，避免 shell `LLM_TEMPERATURE` 偷胜覆盖 toml 字段。`build_room_session` 内单点走 `session._resolve_room_default_spec`。
- **API key 永远不进 toml 也不进 envelope**。toml 最大让步是 `api_key_env = "MY_VAR"`（告诉装配层去哪读 env）；room_info envelope 只下发 `api_key_env` 名 + `api_key_ready` bool，绝不下发 key 本身。
- **`[[guest.extra_mcp_servers]]` 自动信任 vs persona sidecar mcp 走 trust**。两套口径不对称：房间级 inline MCP 是用户在自己 toml 里手写的 → 等价用户意图，自动 trust=True 不进 trust 清单；persona sidecar `mcp.json` 可能从 GitHub 导入 `command` 任意可执行 → 必须 UI 勾"信任"才装载。合并时房间级覆盖 persona 同名。
- **isolation 切换不自动迁移记忆**。`isolation = "room" | "global"` 决定茶客 cwd 路径；切换后旧路径下的 `.agentao/memory.db` / `sessions/` 保持原样（UI 提交前 confirm 提醒）。自动迁移的"两边都有怎么办"复杂度不值。
- **任务房间：入站严格、落盘宽容**（P5.1）。`open_task` / `update_task` / `add_decision` / `attach_artifact` inbound 字段白名单**严格**（未知键 → NOTICE error + 丢帧，等价 `room.toml` 抓 typo 口径，**保护数据不被前端瞎写**）；`task.json` / `decisions.jsonl` 加载时未知字段 **warn 后忽略、必需字段缺才跳坏该条**（等价 `transcript.jsonl` 跳坏行口径，**保护跨版本向前兼容**）。两条规则**有意不对称**。
- **`task_id` 只活在 `envelope.data`**（P5.1）。`message_start` / `message_end` 的 `data.task_id` 是可选标签，envelope 顶层字段不动，`schema_version` 不 bump。老前端忽略未知 data 字段不出错。
- **TASK_INFO 是权威快照，其它 4 个是 hint**（P5.1）。任意 task 状态变更后服务端**重发整份** `task_info`（`tasks` 全量 + `active_task_id`），前端任务状态以最近一次 `task_info` 为准；`task_open` / `task_update` / `task_decision_added` / `task_artifact_added` 仅用于局部反馈（toast / 动画 / 高亮增量项），不进状态本身。开 / 改任务时 server emit hint + 紧跟一帧 `task_info` 是**故意**双重广播。
- **artifact / decision / task 的写权限只在用户**（P5.1）。茶客可通过协议标签提议（P5.3 起 `task_propose_decision` / `task_propose_open` 工具），但 UI 渲染成"采纳"按钮等用户点；不允许茶客静默开任务 / 记决策（防 AI 自我接龙生成 50 个任务）。
- **`attach_artifact` 是 copy 不是 move**（P5.1）。`share/` 是房间公共桌面 + 茶客 cwd / 历史消息 `<./share/xxx>` 引用 / 同一文件可挂多个任务 —— 移动都会断引用。MVP 一律拷贝；茶客视角 `share/` 永远在原位。后续 GC 入口与 attach 解耦。
- **多任务共存，单时刻最多 1 个 active**（P5.2 起）。`open_task` 自动 `set_active` 到新任务，旧 active 保留为 `status="open"` 进历史列表；`set_active_task {task_id|null}` 走前**强制** `_cancel_and_drain_inflight`（与 add/remove guest 同口径），防止 in-flight turn 末尾的消息错挂到新 active。双向修复：state.json 缺 + 多 task.json → 不自动选，emit NOTICE 让 UI 选 active（P5.1 时代"恰好 1 个 → 自动设"分支保留）。`TaskExistsError` 类型保留给老调用方但永不抛。
- **通道 2 软链：茶客 `./task/` 跟 active task**（P5.2.11）。`session.build_room_session` 装配末尾 + `server_inbound_task` 的 open / set_active / close 三处 active 改变后，每位茶客 `working_directory / "task"` 软链刷到 `tasks/<active>/artifacts/`。active=None 时只解不建。Windows 走 junction（`mklink /J`）兜底，普通用户即可建；`agentao` cwd 边界对软链 / junction 一视同仁拦写。失败 WARN 不阻断 inbound 流程，茶客看不到 `./task/` 但任务本身已切换。`./task/` 与 `./share/` 对仗，但语义不同：share 是双向房间公共桌面、task 是只读任务桌面。
- **任务级 summary.jsonl + cursor 落盘 only**（P5.2.12）。`TaskSummaries` 池在 orchestrator 后台 `_kick_summarize` 末尾对 store 当前每任务跑一次 `maybe_summarize`，写 `tasks/<id>/summary.jsonl`；cursor (`considered_until_seq`) 内存推进，`session.close()` 统一 flush 到 `tasks/<id>/summary_cursor.json`（不每轮写 —— cursor 丢只是下次启动多扫，与 `transcript.jsonl` 不 fsync 同口径）。onboarding 注入是 P5.3 的活 —— P5.2 阶段写文件，没人读。

## 测试

`pyproject.toml` 已设 `asyncio_mode = "auto"`，async 测试不用加 mark。`tests/` 目前只有一个回归测（`test_orchestrator_cancel_during_scoring.py`，对应 fe52918 修的「scoring 阶段被 cancel 后停止按钮卡死」）。新测要复现 bug 优先。
