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
- **task / decision 写权限只在用户**（P5.1，P5.4 精细化）。茶客可通过协议标签提议（`task_propose_decision` / `task_propose_open`），UI 渲染成"采纳"按钮等用户点；不允许茶客静默开任务 / 记决策（防 AI 自我接龙生成 50 个空 task）。**artifact 是例外**：茶客直接写 `./task/<name>` 即自动入任务（P5.4 茶客自动归集），不走 propose / 采纳 —— 生成产物本身就是任务执行的核心动作，加用户审批层会阻断正常工作流。
- **`attach_artifact` 是 copy 不是 move**（P5.1）。`share/` 是房间公共桌面 + 茶客 cwd / 历史消息 `<./share/xxx>` 引用 / 同一文件可挂多个任务 —— 移动都会断引用。MVP 一律拷贝；茶客视角 `share/` 永远在原位。后续 GC 入口与 attach 解耦。
- **多任务共存，单时刻最多 1 个 active**（P5.2 起）。`open_task` 自动 `set_active` 到新任务，旧 active 保留为 `status="open"` 进历史列表；`set_active_task {task_id|null}` 走前**强制** `_cancel_and_drain_inflight`（与 add/remove guest 同口径），防止 in-flight turn 末尾的消息错挂到新 active。双向修复：state.json 缺 + 多 task.json → 不自动选，emit NOTICE 让 UI 选 active（P5.1 时代"恰好 1 个 → 自动设"分支保留）。`TaskExistsError` 类型保留给老调用方但永不抛。
- **通道 2 软链：茶客 `./task/` 跟 active task**（P5.2.11）。`session.build_room_session` 装配末尾 + `server_inbound_task` 的 open / set_active / close 三处 active 改变后，每位茶客 `working_directory / "task"` 软链刷到 `tasks/<active>/artifacts/`。active=None 时只解不建。Windows 走 junction（`mklink /J`）兜底，普通用户即可建；`agentao` cwd 边界对软链 / junction 一视同仁拦写。失败 WARN 不阻断 inbound 流程，茶客看不到 `./task/` 但任务本身已切换。`./task/` 与 `./share/` 对仗，但语义不同：share 是双向房间公共桌面、task 是只读任务桌面。
- **任务级 summary.jsonl + cursor 落盘 only**（P5.2.12）。`TaskSummaries` 池在 orchestrator 后台 `_kick_summarize` 末尾对 store 当前每任务跑一次 `maybe_summarize`，写 `tasks/<id>/summary.jsonl`；cursor (`considered_until_seq`) 内存推进，`session.close()` 统一 flush 到 `tasks/<id>/summary_cursor.json`（不每轮写 —— cursor 丢只是下次启动多扫，与 `transcript.jsonl` 不 fsync 同口径）。onboarding 注入是 P5.3 的活 —— P5.2 阶段写文件，没人读。
- **通道 1 两态注入：onboarding / incremental 都贴 task 块**（P5.3.2）。开任务后连续短轮对话几乎全走 incremental，"只 onboarding 注入"会让 task 视野在大多数回合失效。`_render_onboarding` 与 `_render_incremental` 两条路径都吃 `task_id` 形参；onboarding 走 `_render_task_block(compact=False)`（完整块：title / goal / status / owner / 决策 ≤5 / artifact ≤10 / task summary 末 ≤3 段），incremental 走 `compact=True`（1-3 行 header：当前任务 + 目标首行 + 产物路径提示）。**改 task block 拼装时两路径要同步动**，否则 incremental 偏移又会回到"评审反馈说的目标缺席"状态。
- **task_id 经形参透传，不在渲染层读 store**（P5.3.2）。`_run_turn` 入口 snapshot `active_task_id` 后经 `_build_context_for(guest_name, *, task_id)` 一路传到 `_render_task_block(...)`。**`_render_task_block` 是纯函数**，不接 self / 不读 store / 不背状态分支；取数（`tasks_store.get_task` / `list_decisions` / `list_artifacts` / `task_summaries.get`）发生在 `_build_context_for` 内。拼 prompt 时若用户切了 active，本轮仍按 snapshot 渲染，与 message_* envelope 同源；closed task（status ∈ {done, abandoned}）/ 已删除 task 在调用方判后直接不注入，渲染器不参与判断。
- **task block 预算：full ≤300 / compact ≤80 token**（P5.3.1）。超额优先保 title / goal，decisions / artifacts / summary tail 各自截到 5 / 10 / 3 条；compact 路径短路省 IO —— 不调 `list_decisions` / `list_artifacts` / `task_summaries.get`，只用 `get_task` 一次。改 cap 时 `_maybe_render_task_block` 的两步 fetch 必须保住短路，否则每一轮 incremental 都白跑三个 read API。
- **`./task/` 落盘文案分层：compact 极简 / full 详细 + "你判断"软触发**（2026-05-18 文案升级）。`render_task_block` 三处文案（compact / full+artifacts / full empty）有意分层：compact 路径每轮喂，只放"软触发 + 落盘动作 + 边界提醒"三句（**你判断 / 文件写工具 / 聊天里只放一句概要**关键词），类型枚举留给 onboarding；full 路径单次喂 onboarding，分四段（**何时该落盘** / **命名建议** / **为什么** + 类型示例锚点），举例"水文献-评审.md"等带角色身份与版本的命名习惯。**触发用"你判断"软描述，不给字数硬阈值**——让茶客 LLM 自主判断"什么算产物"，类型枚举（评审意见 / 设计方案 / 决策清单 / 代码片段 / 报告草稿）只作示例锚点不作硬规则；"超过 200 字"这类硬阈值会让茶客在边缘场景（如 150 字的精炼意见）误判。**目的**：让茶客知道何时该把结构化输出改写成 `./task/<name>` 文件而非塞回聊天。回归点 `tests/test_render_task_block.py::test_full_block_significantly_longer_than_compact`（full empty ≥ 2× compact / full+artifacts ≥ 1.5× compact）。**禁止把 full 的命名 / 为什么段回流到 compact** —— compact 每轮喂，token 涨幅成本会被 N 茶客 × T 轮放大；**禁止把字数硬阈值塞回任何路径** —— 软触发口径全局一致。
- **通道 3 三个 task tool 均 `is_read_only=True`**（P5.3.4）。`task_list_artifacts` 真无副作用；`task_propose_decision` / `task_propose_open` 不写 task store 也不落盘但**会 emit `TASK_PROPOSAL` envelope** —— 这里 `is_read_only=True` 是为了避免 agentao 权限层把"提议事件"当成写操作拦截，**不是"无副作用"的声明**。读 / 工作区可写 / 完全访问三档下三工具都可调，符合"茶客对 task 的查询不应受权限模式拦截"的口径。
- **propose 不直接写库，采纳后才入库**（P5.3.4 / P5.3.7）。`TASK_PROPOSAL` envelope 仅触发前端 `proposal_card.js` 渲染"采纳 / 忽略"卡片；用户点"采纳"后由前端按 `kind` 拼回既有 inbound（`decision` → `ADD_DECISION` / `open` → `OPEN_TASK`），server handler 零改动。沿用"写权限永远在用户"口径：茶客不能静默开任务 / 记决策。proposal 卡片 session-local，刷新即清；`(proposer, kind, payload-hash)` 去重指纹集合在切房 / clear_room 回环时 reset，避免跨房间残留。
- **`format_messages` 每条消息走 `<message>` 包裹**（P5.x，2026-05-18 起）。``chahua/room.py::format_messages`` 单点定义，输出 ``<message>\n{display} 说：{text}\n</message>`` 序列用 `\n` 连缀。**仅外层包 XML**，内层 ``{display} 说：{text}`` 不变 —— personas/*.md 描述的 "X 说：…"格式仍准确（只多一层 ``<message>`` 包裹）。**为什么必须包**：消息 body 可能含 markdown HR（``---``）/ H2（``## xxx``），单 `\n` 连缀会让"下一条 ``X 说：``"在视觉与 tokenization 上都与上一条 body 内的 markdown 难以区分（典型如评审角色输出长 markdown 报告）。4 个调用点共享：onboarding ``<recent_messages>`` / incremental ``<room_update>`` body / scoring ``<transcript>`` body / summarizer 喂材料。**改这个函数等于改茶客 LLM 看到的消息边界**，回归点在 ``tests/test_render_onboarding_xml.py::test_recent_messages_*``。
- **喂茶客的 context_message：XML 包外层 + Markdown 渲内层**（P5.3.8）。`_render_onboarding` 输出固定 6+1 块 —— `<room>` / `<user_persona>` / `<room_summary>` / `<current_task>` / `<order_hint>` / `<recent_messages>` / `<speak_instruction>`，按"无内容则整块省略"裁剪；`_render_incremental` 输出 `<room_update>` / 可选 `<current_task>` / 可选 `<order_hint>` / `<speak_instruction>` 四块。**`<order_hint>` 与 `<current_task>` 同生共灭**：仅在 `task_block` 非空时注入，无 task 时引用不存在的 `<current_task>` 反而迷惑 LLM；位置固定在 `</current_task>` 之后、`<speak_instruction>` 之前。**speak 阶段的 `_SPEAK_ORDER_HINT_BLOCK`（context_renderer.py）与 scoring 阶段的 `_ORDER_HINT_BLOCK`（scoring.py）措辞不同**：scoring 给数字锚点（≤ 0.3 / ≥ 0.7）让"想接话"分数受顺序约束；speak 给行为指令（"没轮到只输出让位句"）让被选中的茶客知道何时该让麦——两个常量不能合并，scoring 看到"输出让位句"会污染分数计算，speak 看到"调整想接话的程度"等同空指令。**XML 标签是边界承重墙**：USER.md 内部 H2（`## 身份` / `## 忌讳` 等）被 `<user_persona>` 包住后自然降级为段内标题，不再与外层结构同视觉级；任务 status 走 `<current_task status="...">` XML 属性，body 里不再含"状态："行。`_render_task_block` 返回 `(body, status_display)` 二元组，调用方负责拼到 XML 属性。改 `_render_onboarding` / `_render_incremental` / `_render_task_block` 时**保住每个块的开闭标签 + 块顺序**；任何引入新块的改动都要同步加进 `tests/test_render_onboarding_xml.py` 的边界检查。
- **打分 prompt 含极简 `<current_task>` XML 块**（P5.6）。`_run_ai_chain` 入口一次 snapshot `active_task_id` 全程透传，scoring 与 speak 共享同一份；但**不共享 task body renderer**。scoring 走 `_maybe_render_scoring_task_block` → `render_scoring_header`（title + **完整 goal** + status，不含 `./task/` 写入指引），speak 的 `_render_task_block(compact=True)` → `render_task_header`（title + goal 首行 + status）含 P5.4 加的 `"./task/ 是本任务工作目录..."` 发言阶段执行指令 —— 喂打分会把"该写产物吗"的执行语义渗进"想接话吗"的相关性信号。**scoring 与 compact 在 goal 上口径有意不同**：compact 受 ≤80 token 预算约束所以切首行；scoring 看完整 goal 因为 goal 里常写"先 X 再 Y 后 Z"这类角色顺序，砍首行打分模型会把"应该最后说话"的角色（如"Z 最后总结"）误判成"现在就该说话"，scoring 每 pick 周期 1 次渲染、N 个 scorer 共享，goal 膨胀不放大成本。**`<order_hint>` 与 `<current_task>` 同生共灭**：scoring.py 里 `_ORDER_HINT_BLOCK` 显式告诉模型"如果 goal 指定了发言顺序，没轮到你 ≤ 0.3，轮到你 ≥ 0.7"，只在 `task_block` 非空时注入（无 task 时引用 `<current_task>` 反而迷惑 LLM）；XML 标签与 `<context_hint>` 同口径，模型把它当 meta 规则而非 transcript 内容。**每 pick 周期 1 次 `get_task`，N 个 scorer 共享同一 task_block 字符串**（在 `_pick_next_speaker` 内 `scorables` 判定之后、`asyncio.gather` 之前渲染），禁止把 helper 放进 `_score_one`。closed / missing → 空字符串。XML 包装共用 `_wrap_current_task` 单点 helper（onboarding / incremental / scoring 三处都走它），`quoteattr` 处理 status 属性。未来若抽共享函数，只能共享 title / status 这种纯数据格式化层，不能合并 body renderer 也不能合并 goal 渲染（首行 vs 完整两套口径）。
- **task 事件用 user 合成消息进 transcript**（P5.5）。`open_task` / `close_task` / `add_decision` / `attach_artifact` 四个 inbound 末尾走 `server._kick_synthesized_user_message(text, sink, *, task_id)`（紧邻 `_inbound_user_message` / `_run_turn`）合成一条 `speaker_id="user"` 进 transcript，让茶客 LLM 看见 + 触发 orchestrator scoring 循环。串行口径与真用户消息同：先 `_cancel_and_drain_inflight` 再 `create_task(_run_turn)`。**合成消息不发 user envelope**（与真用户消息同口径，靠前端 local echo 显气泡）；本会话内不进聊天气泡，切房 / 刷新后从 `room_history` 重建才出现。**artifact 检测只 append 不 kick**：`ArtifactDetector.detect` 扫到新文件时直接 `self.room.append(USER_SPEAKER_ID, detect_artifacts_text(sorted(new)), task_id=active)`，**不调** `submit_user_message`（已在 in-flight pick 周期末尾会自冲突）；若 AI 链还有后续 pick 自然在下一轮被消费，已到 `max_consecutive_ai_turns` 则留到下次 turn 进 prompt。**P5.5 不广播 `update_task`**（要 diff old/new 太复杂，推到后续）；**`set_active_task` 不广播**（纯 UI 焦点切换，无新信息）；**不追踪 artifact 作者**（统一泛化 "📎 茶客产出：..."）；**不做跨组件 artifact dedupe**（用户 UI 上传 + 茶客扫描可能在 transcript 上重复成两行，接受）。文案集中在 `chahua/task_event_text.py`（5 个纯函数：`open_task_text` / `close_task_text` / `add_decision_text` / `attach_artifact_text` / `detect_artifacts_text`）单点维护，不允许 handler 内 inline 拼字符串。
- **茶客自动归集：./task/ 读用 read_file、写走专用 `task_write_artifact` 工具**（P5.4 + 2026-05-18 PathPolicy 修正）。**纠正 P5.4 一条 stale claim**：CLAUDE.md 早期写"软链对茶客可读可写（agentao cwd 边界对软链解析后位置不拦写）"——这条**与 agentao 0.4.6 的实际行为相反**。`security/path_policy.py::PathPolicy.contain_file` 会 `candidate.parent.resolve()` 跟随 parent chain 上的 symlink、再 `is_relative_to(working_directory)` 检查；茶客 `working_directory=<room>/guests/<name>/`、`./task/` 解析到 `<room>/tasks/<id>/artifacts/` 后**不在**前者之下 → 抛 `PathPolicyError`。**所以**：读 `./task/<name>` 仍 OK（ReadFileTool 不走 contain_file），但**写**必须通过 chahua 自己的 `task_write_artifact(name, content)` 工具（见 `chahua/task_tools.py::TaskWriteArtifactTool`），它直接调 `tasks_store.artifacts_dir(task_id) / name` + `Path.write_text` 绕开 agentao path_policy。**chahua 自己对边界负责**：name 校验拒绝 `/` / `\\` / `..` / 前缀 `.`，杜绝越界写或与 `.DS_Store` / Thumbs.db 等"幽灵 artifact"（P5.4 已用 `_HIDDEN_PREFIXES` 过滤）重名。**回归测试**：`tests/test_task_link_write_path_policy.py` 证明原生 `write_file('./task/<x>')` 被拒；`tests/test_task_tools.py::test_write_artifact_*` 覆盖工具 happy / 守卫 / 名字校验。**`is_read_only=False`**——read-only 权限模式下应当拦住此工具（用户主动选了只读不该让茶客落盘）。茶客把产物写到 `./task/<name>` = 走 `task_write_artifact` = 落到 `tasks/<active>/artifacts/<name>`。**Prompt 文案对应说明**：full / compact / empty 三种 task block 都包含"./task/ 可读写，新产物写到这里自动入任务"，茶客 LLM 据此知道既能读也能写。**新文件感知**：`Orchestrator._kick_detect_new_artifacts(sink, active_task_id)` 在 `_run_ai_chain` 每个 pick 周期末尾紧随 `_kick_summarize` 跑——扫 `tasks/<active>/artifacts/` diff `_seen_artifacts[task_id]`，emit N 条 `task_artifact_added{created_by: ARTIFACT_CREATED_BY_GUEST}` hint + 一帧 `task_info` 权威快照（payload 走 `tasks_store.build_task_info_payload`，与 server_inbound_task 同源）。`_seen_artifacts` 在 `Orchestrator.__init__` 时 seed 现有 open 任务的 artifacts（**只 seed 非 closed**，防止 boot 时旧 artifact 被当新增 + 不让 closed task 堆 dict）；runtime 新任务首次扫时 `.get(..., frozenset())` 兜底；每次扫完同步到当前盘上状态（既加新增也去 GC 掉的旧名）。**用户 UI ``attach_artifact`` 上传走 TaskHandlers 路径，与此函数不同源**——下次 `_kick_detect_new_artifacts` 会把用户上传的文件再 emit 一次 hint，**接受这个重复**（前端 `task_info` 是权威，hint 无 toast 时无感）；不在两个组件间加 sync 通道避免耦合。

## 测试

`pyproject.toml` 已设 `asyncio_mode = "auto"`，async 测试不用加 mark。`tests/` 目前只有一个回归测（`test_orchestrator_cancel_during_scoring.py`，对应 fe52918 修的「scoring 阶段被 cancel 后停止按钮卡死」）。新测要复现 bug 优先。
