# P11：后台 Agent —— 同房间 Agent 并行执行

> 同一房间内允许多个 Agent 后台并行执行。后台 run 由用户手动指派（弹窗 / `/bg`）或茶客调度（`spawn_agent_run(s)` 工具）。执行过程不流式刷聊天区，完成后一次性把结果作为普通茶客气泡进 transcript。

## Summary

现有架构「一个房间只有一个 in-flight」撑不起并行后台执行。P11 在前台 in-flight 之外新增 run 级运行态，多个不同茶客可在同一房间并发跑 bg run。

bg run 执行期间不下发流式事件；完成时 `message_end(ok)` 一次性下发，前端「无 start 直接 end」兜底渲气泡。中间产生的 `TASK_PROPOSAL` 缓冲到 run 完成后 flush；artifact 走 wrapper finally 的 detect 路径统一产出（不实时 emit）。

## 承重不变量

- **每个 `TeaGuest` 实例同时刻最多 1 个 `speak()` 调用**——前台 / handoff drain / bg run 三者互斥。
- **bg run 成功进 transcript；失败 / 取消只推终态通知，不污染 transcript**。
- 前台 scoring / handoff drain 单 in-flight 语义不变。
- 不并行化 `handoff_review/panel/MTS`——并发审稿用 `spawn_agent_runs`。

## 运行态

### `AgentRun`（running-only）

字段：`run_id`（`run_<hex>`）、`room_id`、`guest_name`、`instruction`、`task_id?`、`issued_by`（`"user"`/`"agent"`）、`source_guest?`、`created_at_ms`。

**不**含 `status` / `error`——注册表只装 running，终态信息留在 envelope 里，不在内存维护「已完成」垃圾。不 clone 茶客实例，一个 run 绑定现有 `TeaGuest`。

### `RoomRuntime` 扩展

- `agent_runs: dict[run_id, AgentRun]`（只装 running）
- `agent_run_tasks: dict[run_id, asyncio.Task]`
- `active_guest_names: set[str]`——「已占用 / 即将 speak」的茶客名。`guest_busy(name) = name in active_guest_names`，是唯一数据源。
- `has_active_runs() = bool(agent_runs)`。
- `busy_alive() = inflight_alive() or has_active_runs()`——**仅** P9「runtime 生命周期 / 房间 busy 展示」分支用，**不**用于前台 turn 控制（见下方分流）。

**分流口径**（窄替换，防止误伤 P11 之外的语义）：

替换为 `busy_alive()`（runtime 该不该活 / 房间该不该标 busy）：

- `server.py::_switch_room` 旧前台 demote 判断（busy → 转后台续跑，idle → close + pop）。否则前台只有 bg run、无 foreground turn 时切房会被当 idle 误关。
- `server.py::_maybe_self_destruct_background_runtime` 自毁判断。否则后台只剩 bg run 会被立刻自毁。
- `server.py::_rooms_available_with_busy` / `emit_room_info` 房间 busy 标志。让有 bg run 的房间在房间列表里也亮 busy。

**保持 `inflight_alive()`**（前台 turn 控制语义；P11 明确不被 bg run 影响）：

- `server.py:867` 前台 `cancel` 按钮：只停当前回答，不取消 bg run（P11 设计前提）。
- `server.py:1071` `user_message` 单 in-flight 防御：判定「现在能不能起新 turn」与 bg run 无关——只有 bg run 时新 user_message 仍应正常入。
- 其它任何「停 / 不让起前台 turn」语义点。

替换前 grep `inflight_alive()` 走 case-by-case 判断属于哪一类，**禁止整批 sed**。

`active_guest_names` 维护（**add 必须先于任何 await**）：

- 前台 / handoff `_let_speak`：进 `speak()` 前 `add(name)`、finally `discard(name)`。
- bg run：**inbound / 工具校验通过、登记 `agent_runs[run_id]=run` 那一瞬间同步 `add(target)`**（先于 `create_task`），wrapper 外层 finally `discard`。这样「登记 → create_task → wrapper 进 speak」整段间隙都被 set 覆盖，第二个 inbound 不会漏过 `guest_busy()`。

**数据线（实现落点）**：`ScoringOps.let_speak` 只拿到 `sink`（`chahua/_orchestrator_scoring.py:274`），不知道自己挂在哪个 `RoomRuntime`。引一条最窄的注入：

- `Orchestrator` 加可选属性 `active_guest_names: set[str] | None = None`。
- 每次构造 `RoomRuntime` 后立即 `runtime.session.orchestrator.active_guest_names = runtime.active_guest_names`（两者同一对象引用）。当前两处构造点：`server.py::_session.setter`（首次赋值 / 同房重建）与 `server.py::_switch_room`（切房目标 runtime）。抽一个 `_attach_runtime_state(runtime)` helper 在两处都调，避免漏写；不为此引入 runtime factory / 状态机。**C2 阶段** helper 内只做这条 `active_guest_names` 注入，**C11 阶段**再扩展遍历 `runtime.session.guests` 绑定 `guest.start_agent_run = self._make_start_agent_run(runtime)`——单点 helper 承所有 per-runtime 注入。
- `ScoringOps.let_speak` 在 `await guest.speak(...)` 外包 `try: add → finally: discard`；`active_guest_names is None`（旧测试夹具裸构 `Orchestrator`）时跳过——保持 P11 前测试零改。
- handoff drain 走同 `_let_speak`，不用额外 wiring。

**不**新加锁 / 状态机 / 跨层事件；server 直接拿 `runtime.active_guest_names` 做 `guest_busy()` 校验，与 orch 端是同一个 set 实例，asyncio 单 loop 串行保证读写无 race。

## 入口

### `agent_run_start` inbound

payload `{target: str, instruction: str, task_id?: str}`。

- `instruction` 必填非空（用户手动入口也必填，不给默认句兜底）。
- 服务端校验：target 在场 / `task_id` 若给须未关闭 / `guest_busy(target) == False` / `len(agent_runs) < max_agent_runs_per_room`。
- 通过即登记 + `add(target)` + emit `agent_run_started`。

字段名定为 `instruction`（**不**复用 P7 / P8.3 的 `reason`——后者是不进 prompt 的内部备注）。前端文案明确「这段文字会作为指令发给被指派茶客」。

### `agent_run_cancel {run_id}`

只 cancel 指定 run，不影响其它 bg run / 前台 in-flight。前台 `cancel` 按钮也不取消 bg run（后台是用户自己创建的并行工作，不该被「停止当前回答」误杀）。

### `/bg <茶客> <指令>` 斜杠命令

`app/renderer/commands.js` 加本地命令：解析首个空格后的余串为 instruction，本地校验非空 / target 在前端 roster，通过即发同一条 `agent_run_start`。`/help` 文案补一行。CLI REPL 不实现（无后台 UI 列表）。

### `spawn_agent_run(s)` 工具（P11.2）

`chahua/agent_run_tools.py` 模块。参数面只支持 `{target, instruction, task_id?}`，**不**引入 `agent_run_spec` / `review_message_id` / `propose_agent_runs`（真实需求出现再加）。

- 非 read-only（写操作）。
- 拒绝条件同 inbound + 同批次重复 target。
- 上限：`max_agent_runs_per_tool_call = 4`、`max_agent_runs_per_room = 4`（按 P9 5 个后台房算，4 已覆盖典型「3 审稿 + 1 主持」）。
- 工具返回「已创建后台 run + run_id 列表」，不等待完成。

## envelope

### 4 个新 `ChahuaEventType`

`AGENT_RUN_STARTED` / `AGENT_RUN_FINISHED` / `AGENT_RUN_CANCELLED` / `AGENT_RUN_ERROR`。

data：`{run_id, guest_name, task_id?, issued_by, source_guest?, instruction_preview?}`；错误态加 `{error}`。

- 不进 transcript；不 bump `SCHEMA_VERSION`（沿用 P7/P8.3/P9 同口径，旧前端忽略未知 type）。
- 加入 P9 后台里程碑白名单。

### `room_info.data.background_runs` 整批权威字段

`emit_room_info` 携带前台 runtime 的 `agent_runs.values()` 投影。

- 前端每次收到 `room_info` 即覆盖式 `applyBackgroundRuns(...)`，是状态权威源。
- 实时 `agent_run_started/*_terminal` 只做增量。
- 切回房 / 重连 / clear 都重新到 `room_info`，整批 set 清除幽灵指示（即便实时 terminal 帧在断连中丢了）。
- 缺省（旧 server）→ 前端按 `[]`，schema 不 bump。

## 后台执行路径

新增 `_run_agent_background(runtime, run)` wrapper，`asyncio.create_task` 启动。不走 `run_pending_handoff()`（handoff 是队列串行）、也不走 `_let_speak()`，wrapper 自己补回承载语义：

1. 直接调 `TeaGuest.speak(context_message, sink=BatchMessageSink(...), record_debug=False)`；
2. 成功拿到 `msg` 后 `orch.cursor.set(guest_name, msg.seq)`——补 `_let_speak` 的 cursor 推进，否则 bg 茶客下次 onboarding 把自己的产物当未读重喂；
3. **不**改 `_cooldown` / `_consecutive_ai_turns`。

context 装配复用 `Orchestrator._build_context_for(guest, task_id=run.task_id, extra_blocks=[render_agent_run_block(...)])`：

- `task_id` 启动时**冻结**，其它字段（`<recent_messages>` / `<room>` / `<room_summary>`）走 live 视图（与 handoff drain 口径一致）。
- 新增纯函数 `render_agent_run_block(instruction, issued_by, source_guest=None)` 输出 `<agent_run_task>` 块，注入位 `<speak_instruction>` 之前；块内说明指令来源。

### wrapper finally 顺序

**两层 try/finally + 每步 best-effort 兜底**：

外层 finally 唯一职责：`runtime.active_guest_names.discard(guest_name)`，保证内部任一步抛异常 target 必定释放。

terminal 类型由一个局部变量 `terminal_type` 决定（**不**扩展 `AgentRun.status`，注册表仍 running-only）。`TeaGuest.speak()` 普通异常吞掉返 `None`、取消会重抛 `CancelledError`、成功返 `Message`（`chahua/guest.py:292`），按此三态分流：

- 初值 `terminal_type = ERROR`。
- `except CancelledError`：`terminal_type = CANCELLED`，**重抛**（让 asyncio 任务被正确标记 cancelled）。
- `msg is not None`：`terminal_type = FINISHED`。
- `msg is None`：保持 `ERROR`。

内层 finally 按序：

1. `_kick_detect_new_artifacts(runtime.router, run.task_id)`——绕开 BatchMessageSink，envelope 直发；
2. flush 缓冲 `TASK_PROPOSAL` 到 `runtime.router`；
3. `agent_runs.pop` / `agent_run_tasks.pop`（**无条件**，先于 emit terminal——避免 snapshot 在间隙重投死 run）；
4. emit terminal envelope（根据 `terminal_type` 选 `AGENT_RUN_FINISHED` / `_CANCELLED` / `_ERROR`）。

外层 finally：先 `discard(guest_name)`，然后调一次 `self._maybe_self_destruct_background_runtime(runtime)`——后台 runtime 上「只剩 bg run」收尾时由 wrapper 自己触发自毁（P9 既有触发点 `_run_turn` / `_run_handoff_turn` finally 没覆盖纯 bg run 场景），方法内部 `router.mode == background and not busy_alive()` 判定，前台 runtime 自然 no-op。

每步实现：内层 1/2/4 各自 `try/except + WARN`（`_kick_detect_new_artifacts` / emit 现状未统一 catch，不能假定安全）；3 无 catch 直接 pop（`dict.pop(k, None)` 不会抛）。外层自毁调用也 `try/except + WARN`，确保不挡 `discard` 之后的任何兜底。

```python
runtime.agent_runs[run_id] = run  # 入口同步 add(target)
terminal_type = AGENT_RUN_ERROR
msg = None
try:
    try:
        try:
            msg = await speak(...)
            if msg is not None:
                terminal_type = AGENT_RUN_FINISHED
        except asyncio.CancelledError:
            terminal_type = AGENT_RUN_CANCELLED
            raise
    finally:
        try: orch._kick_detect_new_artifacts(runtime.router, run.task_id)
        except Exception: log.warning(..., exc_info=True)
        try: flush_propose_buffer(runtime.router)
        except Exception: log.warning(..., exc_info=True)
        runtime.agent_runs.pop(run_id, None)
        runtime.agent_run_tasks.pop(run_id, None)
        try: emit_terminal(terminal_type, msg)
        except Exception: log.warning(..., exc_info=True)
finally:
    runtime.active_guest_names.discard(guest_name)
    try: self._maybe_self_destruct_background_runtime(runtime)
    except Exception: log.warning(..., exc_info=True)
```

### `BatchMessageSink` 事件白名单

| 类型 | 处理 |
|---|---|
| `message_start` / `message_delta` / `tool_*` / `agent_event` / `turn_*` | 丢 |
| `message_end(ok)` | 放（补 `data.run_id`） |
| `message_end(error/cancelled)` | 丢（由 `agent_run_error/cancelled` 表达） |
| `task_artifact_added` / `task_info` | 丢（统一由 wrapper finally detect 直发） |
| `TASK_PROPOSAL` | 缓冲，run 完成后 flush |
| `NOTICE` | 放 |

链路：`speak` → `ChahuaTransport.bind` → `BatchMessageSink` → `RoomEventRouter`（P9 fg/bg 过滤）→ outbound queue → server `_writer` 单 writer 串行 `ws.send`。

复用 server 现有单 writer + queue 模型，**不引入 `ws_send_lock`**。

### TurnRecorder 串台修复

`TeaGuest.speak` 无条件用 guest 持有的 recorder 调 `record_message_start/end`（`chahua/guest.py:272/318`），与前台 turn 并发时会把 bg message 串进前台 debug turn。

**修法**：`speak(..., record_debug: bool = True)` 参数，bg wrapper 传 `False`，内部把 `self._recorder` 临时替换为 `NOOP_RECORDER`（`chahua/debug_recorder.py:893` 已有），bind / record 全部落空。

代价：P11.1 bg run 不进 debug 视图。后续真要 debug 取证再升级（per-run TurnRecorder 等），P11.1 不写空头承诺。

## 取消与清理

新增 `RoomRuntime.cancel_and_drain_agent_runs()`：cancel `agent_runs` 全部 task → 走 `asyncio.gather(*tasks, return_exceptions=True)` 等所有 wrapper finally 跑完。**所有清理路径必须显式调它**，否则前台 bg run 漏 drain → 重连复用 session 时后台任务还在写 transcript / router。

**职责边界**：helper 只保证「wrapper finally 跑完 + 不向清理调用方传播取消」。wrapper 内 `CancelledError` 重抛是为了让 asyncio task 正确标 cancelled（影响 `task.cancelled()` 状态），但这条异常**不能冒泡到** `clear_room` / `remove_guest` / `_replace_session` / `aclose` / `_serve_one` 的 finally——否则会中断后续 `reset_room` / `close`。`return_exceptions=True` 把 `CancelledError` 一并收成结果项；非 `CancelledError` 的异常 WARN + 继续 drain 下一个，不抛。

`clear_room` / `remove_guest` / `_replace_session` / `aclose` 顺序——**先同步发出所有 cancel，再 await drain**，防止「drain bg run 期间前台 inflight 仍在跑、通过工具新建 bg run」的 race：

1. `runtime.cancel_inflight()`：**只** `task.cancel()`、**不** await，立刻阻断前台继续产出 / 调度新 bg run（含 `spawn_agent_run` 工具）。
2. cancel `agent_run_tasks` 全部 task（同样只 cancel 不 await）。
3. `await cancel_and_drain_agent_runs()`：`gather(*agent_run_tasks, return_exceptions=True)` 等所有 wrapper finally 跑完——此时步骤 1 已让前台不会再投新 bg run，drain 集合稳定，**不需要循环 drain**。
4. `await runtime.cancel_and_drain_inflight()`（既有 helper，重复 cancel 幂等）。
5. 才进行 `reset_room` / `close` 等。

不引入 `closing` 状态标志 / 锁——「先取消所有生产者，再等待收尾」用顺序本身表达。

顺序错位后果：① 先 drain bg run 后 cancel 前台 → drain 期间前台再起新 bg run，reset 时漏清；② 先 reset 后 cancel → bg run finally 写入「已清空」房间。

- **切走**：旧前台 runtime 转后台续跑（P9），bg run 继续，里程碑穿透 background 过滤。切房 demote 判断走 `busy_alive()`（前台只有 bg run、无 foreground turn 也算 busy，转后台不 close）。**切走不走上面的清理顺序**（旧前台是 demote 不是 close），bg run 不被 cancel。
- **ws 断开（前台 runtime）**：`_serve_one` 的 `finally` 只走步骤 1–4——`cancel_inflight()` → cancel `agent_run_tasks` → `await cancel_and_drain_agent_runs()` → `await cancel_and_drain_inflight()`，**不**做步骤 5 的 `reset_room` / `close`。前台 session / runtime 跨连接复用语义不变（P9）；只清 bg 任务态与前台 turn task，session 留给下次 ws 接上来继续用。
- **ws 断开（后台 runtime）**：`_aclose_background_runtimes()` 对每个后台 runtime 走 `_aclose_one_runtime`，走完整 5 步（前台 inflight 这一步对后台等价于 drain handoff turn / MTS），最后 close + 移出 `_runtimes`——后台 runtime 仅在真有活时存在，断连即清。
- **后台自毁**：`_maybe_self_destruct_background_runtime` 走 `busy_alive()`——bg run 跑完之前不自毁。
- 不跨 app 重启恢复。
- cancel 后盘上残留的 artifact 不自动清理；前台下一轮 detect 把它们当新增产物 emit 一次（无害）。

## MTS × `spawn_agent_run`

- MTS `_intercept_task_proposal` 只拦 `TASK_PROPOSAL`，**不**拦 `agent_run_*`。
- 管理者 `spawn_*` 创建 bg run 时**不立即扣 budget**、**不增 `_consecutive_ai_turns`**，但仍占 `max_agent_runs_per_room`。
- **P11.2.X 续命闭环**：管理者 spawn 的 bg run（`source_guest == ms.manager_guest`）在 `_start_agent_run` 时刻被打上 `mts_managed=True` 快照。每条这样的 bg run 完成时（wrapper finally step ⑤），自动 `enqueue_handoff(DELEGATE, target=manager)` + `budget-=1` + emit `managed_session_advanced`；若房间已无 inflight，主动起 `_run_handoff_turn` 消费新 enqueue 项。drain 收尾兜底带 `_has_pending_mts_bg()` 守卫，仍有 manager-attributed bg 未完成时不触发 `MANAGER_FINISHED` —— 安静退出 drain 让 bg 完成回调来 wake。
- **budget 语义**：从「每个 worker→manager 桥扣 1」扩展为「每个 manager→worker（含 bg）→manager 桥扣 1」—— spawn N 个 bg ≈ 用 N budget；启动 MTS 时 budget 须够覆盖预期 spawn 总数 + 后续接力次数。budget=0 时上一轮 manager 仍能跑完（`stop_reason` 在 turn 开始前还是 None）；turn 末尾 advance_after_turn 走 `BUDGET_EXHAUSTED` 收尾。
- **退化保护**：bg 跑期间 MTS 被独立结束（user_cancel / task_closed / budget_exhausted）时，wrapper finally 续命的 None 守卫返 False，跳过续命；spawn 时刻的 `mts_managed=True` 只反映「spawn 时 MTS 活」，不强求「完成时仍活」。
- 工具描述 / MTS 启动提示 / SKILL.md MTS 分支同步说明「MTS 中可用 `spawn_agent_runs`：每位 bg 完成会自动调度管理者复查一次，扣 1 budget」。SKILL 不再教「bg 完成后自留 `propose_delegate(target=self)` 兜底卡」—— MTS 内系统自动续命，自留卡反而被 hook 静默吞掉。

## 前端

### 列表条 + 弹窗 + 命令

- 「指派下一句」**单人选择**时显示「后台运行」复选框（多人圆桌不显示）；placeholder「指令（必填，会发给被指派茶客）」。
- 勾选后台发送 `agent_run_start`，不占用主停止按钮。
- composer `/bg <茶客> <指令>` 斜杠命令。
- composer 上方持久显示后台 run 列表条：`茶客名 · 来源 · 指令预览(≤30字) · 取消按钮`。

### 左侧栏茶客行指示

- `sidebar.js` 每位茶客行新增 `.guest-bg-run` 元素（靠左、贴头像），与既有 `.guest-score`（靠右）共存。
- `sidebar.applyBackgroundRuns(runs)` 单点 API，内部 `bgRunByName: Map<guest_name, run_id>`（guest 唯一性 → value 单 id 不用 set）。
- 头像 popover 增加「× 取消后台 run」入口（条件显隐），等价于发 `agent_run_cancel {run_id}`；不加独立按钮。
- 事件驱动：`room_info.data.background_runs` 整批覆盖（权威）；实时 `agent_run_started/*_terminal` 增量。

### 事件职责分叉

- `message_end(ok)` **只**追加普通茶客气泡。
- `agent_run_finished` **只**移除运行列表条 + sidebar 指示。
- `agent_run_cancelled/error` 同上移除 + 弹中央系统气泡，**不**生成结果气泡。

服务端 wrapper finally 顺序（detect → flush propose → remove registry → emit terminal → discard busy）保证 `agent_run_finished` 在补缓冲事件之后到达。

## Phases

### P11.1：用户手动后台指派

- `AgentRun` + `RoomRuntime` 扩展（`agent_runs` / `agent_run_tasks` / `active_guest_names` / `has_active_runs` / `guest_busy`）。
- 4 个 `ChahuaEventType` 枚举 + `room_info.data.background_runs` 字段（加入 P9 后台里程碑白名单）。
- `agent_run_start` / `agent_run_cancel` inbound。
- `BatchMessageSink` + `_run_agent_background` wrapper + `render_agent_run_block`。
- `TeaGuest.speak(..., record_debug)` 参数。
- 前台 / handoff `_let_speak` 维护 `active_guest_names`。
- `/bg` 斜杠命令（Web 端）+ 弹窗「后台运行」选项。
- sidebar `.guest-bg-run` 指示 + 头像 popover 取消入口。
- 后台 run 列表条 + 事件职责分叉。

### P11.2：Agent 调度并发

- `chahua/agent_run_tools.py`：`spawn_agent_run` / `spawn_agent_runs`，参数面 `{target, instruction, task_id?}`。
- 上限 4 + 4。
- 工具描述补「并行后台执行用 `spawn_agent_runs`，不要用 `propose_panel`」。

### 与 `propose_*` 的关系

`propose_delegate/review/panel` 保留：用户采纳后走前台 handoff drain（panel 仍是**串行**）。并行后台请用 `spawn_agent_runs`，不扩展 `propose_panel` 语义。MTS 继续 hook `propose_delegate/panel` 入队，不变。

### 后续（不在 P11 内）

- 自动 fan-in（所有 bg run 完成后自动让某茶客总结）：另开阶段设计 run group / barrier / callback。
- bg run debug 取证。

## Non-Goals

- 不并行化 `handoff_review/panel/MTS`。
- 不跨 app 重启恢复 bg run。
- 不保证指派顺序输出（完成顺序提交）。
- 不展示后台 run 的工具流式细节。
- 不支持同茶客多实例并行（guest 唯一性）。
- 不承诺跨 run 的 `room.append + cursor + recorder + artifact registry` 原子性——asyncio 单 loop 串行已够；真要原子需把锁接到 `TeaGuest.speak` 收尾段且前台同走，改动面与回归风险都不值。
- 不为 bg run 录 debug turn（P11.1）。
- 不解决 bg run 期间用户切权限的 race；UI 警示「切权限只对未开始的 tool call 生效」。
- 不自动清理 cancel 后的半成品 artifact。
- 不引入 batch/group id、不做 batch fan-in 系统气泡。
- 不引入 `agent_run_spec` / `review_message_id` / `propose_agent_runs`。
- 不引入新锁（`ws_send_lock` / `append_lock`）。

## Test Plan

### 后端

- 同房间两条不同 target 的 `agent_run_start` 并行 running。
- 同 target 重复 `agent_run_start` → 第二条被拒（NOTICE error）。
- 前台 `@Bob` 期间 `agent_run_start target=Bob` 被拒；bg `target=Bob` 期间前台 scoring 跳过 Bob；handoff panel 含 Bob 时 `spawn_agent_run target=Bob` 被拒。
- **前台 Bob.speak() 进行中**（`ScoringOps.let_speak` 已 `add("Bob")`、`await guest.speak(...)` 尚未返回）→ `agent_run_start target=Bob` 必被 `guest_busy("Bob")` 拒；speak 结束 finally `discard("Bob")` 后下一条同 target 才放过。
- `Orchestrator.active_guest_names` 注入：server 端 `runtime.active_guest_names` 与 orch 端是同一 set 实例；旧测试夹具裸构 `Orchestrator`（`active_guest_names is None`）时 `_let_speak` 不抛、不修改 set。
- **race 回归**：登记 `agent_runs` 与 `add(target)` 同步发生（先于 `create_task`），第二条 inbound 在 wrapper 进 speak 之前到达仍被拒。
- `spawn_agent_runs` 拒绝：空 instruction / 不在场 / 重复 / `guest_busy` / `len + 批次 > max_agent_runs_per_room`（边界等于不拒）/ `> max_agent_runs_per_tool_call`。
- bg run 期间不下发 `message_start/delta/tool_*/agent_event/turn_*`，只在成功时下发 `message_end`。
- bg run 期间 `TASK_PROPOSAL` 被缓冲，run 完成后经 `runtime.router` flush。
- bg run 期间 `task_artifact_added/task_info` 被 BatchMessageSink 丢，由 wrapper finally `_kick_detect_new_artifacts(runtime.router, run.task_id)` 统一产出。
- **wrapper finally 顺序**：detect → flush propose → 移除 run_id → emit terminal → discard active_guest_names。
- wrapper finally 任一步抛异常仍 discard active_guest_names + pop registry（registry pop 无条件）。
- **`terminal_type` 三分流**：`speak()` 返 `Message` → `AGENT_RUN_FINISHED`；返 `None`（普通异常被吞）→ `AGENT_RUN_ERROR`；`CancelledError` → `AGENT_RUN_CANCELLED` 且重抛（asyncio task 标 cancelled）。不扩展 `AgentRun.status`。
- bg wrapper 成功后 `cursor.set(guest_name, msg.seq)`；不改 `_cooldown` / `_consecutive_ai_turns`。
- bg run 与前台 turn 并发时前台 `TurnRecorder._current` 不被 bg 串入（NOOP_RECORDER）。
- `agent_run_cancel {run_id}` 只取消指定 run；前台 `cancel` 不取消 bg run。
- `clear_room` / `remove_guest` / `_replace_session` 按「先同步发 cancel（前台 inflight + 全部 bg run）→ await drain bg → await drain 前台 inflight → reset」5 步顺序，不会写入已 reset 房间。
- **drain 期间防新 bg run race**：前台 inflight 正跑 `spawn_agent_run` 工具瞬间触发 `clear_room`——清理路径步骤 1 先 cancel 前台 inflight，工具调用被中断、不再有新 bg run 入 `agent_runs`；步骤 3 drain 集合稳定，reset 后 `agent_runs` 为空。
- 切房：bg run 继续；`agent_run_finished` 穿透 P9 background 过滤。
- **`busy_alive()` 覆盖**：前台只有 bg run、无 foreground turn → 切房走 demote 转后台（非 close + pop）；后台只剩 bg run → 不自毁；`emit_room_info` 该房 `busy=true`。
- **`inflight_alive()` 保留**（不被 bg run 污染）：前台只有 bg run、无 foreground turn → 前台 `cancel` 按钮判定「无前台回答可停」按既有空操作处理（不取消 bg run）；同时新 `user_message` **正常入**起新 turn（不被单 in-flight 防御误 drop）。
- **bg wrapper finally 触发自毁**：后台 runtime 上「只有 bg run、没有 foreground inflight」的纯 bg 场景，bg wrapper finally 调一次 `_maybe_self_destruct_background_runtime(runtime)`；最后一个 bg run 结束 → runtime 移出 `_runtimes` + emit `room_background_finished`（前台 runtime 同调用 no-op）。回归：触发顺序在 `discard(guest_name)` 之后（防自毁中途看见 `active_guest_names` 残留）。
- **ws 断开（前台 runtime）**：`_serve_one` finally 走步骤 1–4——cancel 前台 inflight + bg tasks → drain bg → drain inflight，**不** reset/close 前台 session（P9 跨连接复用语义不变）；断开后前台 runtime 上任何 bg / turn 任务不再写 transcript / router。
- **ws 断开（后台 runtime）**：随后 `_aclose_background_runtimes()` 对每个后台 runtime 走 5 步 + close + 移出 `_runtimes`。
- **`cancel_and_drain_agent_runs` 不传播取消**：wrapper 内 `CancelledError` 重抛 → task `cancelled()==True`，但 helper 走 `gather(..., return_exceptions=True)` 吞掉，调用方（`clear_room` / `aclose` / `_serve_one` finally）继续往下跑 `reset_room` / `close` 不被打断；wrapper finally 内非 `CancelledError` 异常 WARN 后继续 drain 下一个。
- 两个 bg run 同 `message_end` 时 server 单 writer 串行 `ws.send`，帧不交错。
- MTS 管理者 `spawn_agent_runs`：bg run 创建成功，不耗 budget、不计 `_consecutive_ai_turns`。
- bg run context：`task_id` 启动时冻结；`<recent_messages>` 走 live 视图。
- `room_info.data.background_runs` 缺省（旧 server）→ 前端按 `[]`，schema 不 bump。

### 前端

- 单人指派可选「后台运行」；多人圆桌不显示。
- `/bg Bob 帮我看下方案` 触发 `agent_run_start`；缺指令 / 缺茶客名 setStatus error；`/help` 含 `/bg`。
- 多个后台 run 在列表条同时显示，可分别取消；主停止按钮不变。
- 成功：`message_end(ok)` 追加气泡；`agent_run_finished` 移除列表条 + sidebar 指示（两件事职责分开）。
- 失败 / 取消：无结果气泡；移除列表条 + 中央系统气泡。
- sidebar `.guest-bg-run` 指示亮 / 熄跟随 bg run；同茶客先后两 run 状态切换正确。
- 切回房 / 重连 ws：`room_info.data.background_runs` 整批覆盖式重建 sidebar 指示 + 运行列表条。
- **幽灵指示回归**：模拟 `agent_run_finished` 断连丢帧 → 重连后 `room_info` 整批 set 清掉旧指示。
- 头像 popover「× 取消后台 run」条件显隐 + 点击发 `agent_run_cancel`。
- 多人圆桌期 `.guest-score`（右）+ `.guest-bg-run`（左）并存，互不覆盖。

## Commit Checklist

每个 commit **独立编译 + 测试通过 + 可单独 review**。前后依赖按编号严格递增；C1–C3 无行为变化的纯铺垫，可先合主分支。

### P11.1：用户手动后台指派

**C1 — `AgentRun` + `RoomRuntime` 扩展（纯铺垫）**

- 新文件 `chahua/agent_run.py`：`AgentRun` dataclass（无 `status`）。
- `chahua/server.py::RoomRuntime`：加 `agent_runs` / `agent_run_tasks` / `active_guest_names`；加 `has_active_runs()` / `busy_alive()` / `cancel_inflight()` / `cancel_and_drain_agent_runs()`（`gather(..., return_exceptions=True)`）。
- 不接入任何调用方。
- 测试：`RoomRuntime` 新 helper 单测（空 dict / 含一个 mock cancelled task 都正常返回）。

**C2 — `Orchestrator.active_guest_names` 注入**

- `chahua/orchestrator.py`：加 `active_guest_names: set[str] | None = None`。
- `chahua/server.py`：抽 `_attach_runtime_state(runtime)`，在 `_session.setter` 和 `_switch_room` 两处 `RoomRuntime` 构造后调。
- `chahua/_orchestrator_scoring.py::let_speak`：`await guest.speak(...)` 外包 `try: add → finally: discard`，`None` 跳过。
- 测试：① 裸构 `Orchestrator(active_guest_names=None)` `_let_speak` 不抛；② server 注入后两端是同一 set 引用；③ 前台 `Bob.speak()` 进行中 set 含 `"Bob"`、finally `discard`。

**C3 — `ChahuaEventType` + `background_runs` 字段 + 投影 helper**

- `chahua/events.py`：4 个 enum（`AGENT_RUN_STARTED/FINISHED/CANCELLED/ERROR`）+ envelope `data` 形状（`run_id / guest_name / task_id? / issued_by / source_guest? / instruction_preview? / error?`）。
- `chahua/server_room_snapshot.py::emit_room_info`：加 `data.background_runs`，**直接接 `runtime.agent_runs.values()` 投影**（C1 已加 `agent_runs` 字段，此时为空 dict，自然输出 `[]`）——单点投影 helper（`_project_agent_runs(runtime) -> list[dict]`），后续 C8 创建 run 时该字段自动跟随、无需补改。**不**引入独立 store。
- 加入 P9 后台里程碑白名单。
- 测试：envelope round-trip + `room_info` 字段存在（C3 时刻 = `[]`；后续 C8 测试时改成验非空投影格式）。

**C4 — `TeaGuest.speak(record_debug=False)` 参数**

- `chahua/guest.py::speak`：加 `record_debug: bool = True`；`False` 时把 `self._recorder` 临时替换为 `NOOP_RECORDER`。
- 测试：`record_debug=False` 与前台 turn 并发时 `TurnRecorder._current` 不被串入。

**C5 — `BatchMessageSink` + `render_agent_run_block`**

- 新文件 `chahua/agent_run_sink.py`：`BatchMessageSink`（白名单表 + propose 缓冲 + `flush_to(router)`）。
- `chahua/context_renderer.py`：加 `render_agent_run_block(instruction, issued_by, source_guest=None)`，注入位 `<speak_instruction>` 之前。
- 测试：白名单分流（`message_start/delta/tool_*/agent_event/turn_*` 丢 / `message_end(ok)` 放 + 补 `data.run_id` / `error/cancelled` 丢 / `task_artifact_added/task_info` 丢 / `TASK_PROPOSAL` 缓冲 / `NOTICE` 放）。

**C6 — `_run_agent_background` wrapper**

- **落 `chahua/server.py`**：wrapper 需要 `_maybe_self_destruct_background_runtime` / `runtime.router` / `runtime.agent_runs` / server 生命周期，放外部模块会把内部细节参数化、回头难收。`chahua/agent_run.py` 只承载 `AgentRun` dataclass，不放 wrapper。
- 按 §「后台执行路径」伪码实现两层 try/finally。
- `terminal_type` 三分流（`Message → FINISHED` / `None → ERROR` / `CancelledError → CANCELLED + raise`）。
- 内层 finally：detect → flush propose → pop registry → emit terminal（1/2/4 各 `try/except + WARN`；3 直接 pop）。
- 外层 finally：`discard(guest_name)` → `_maybe_self_destruct_background_runtime(runtime)`（`try/except + WARN`）。
- 成功后 `orch.cursor.set(guest_name, msg.seq)`；**不**改 `_cooldown` / `_consecutive_ai_turns`。
- 测试：finally 顺序 / 三种 terminal 路径 / cursor 推进 / 自毁触发顺序在 `discard` 之后。

**C7 — `busy_alive()` 窄替换（3 处）**

- `_switch_room` demote 判断 / `_maybe_self_destruct_background_runtime` / `_rooms_available_with_busy`：`inflight_alive` → `busy_alive`。
- **显式不动**：`server.py:867` cancel、`server.py:1071` `user_message` 单 in-flight 防御。
- 测试：前台只有 bg run 时切房 demote、后台不自毁、`room_info` `busy=true`；同时 cancel 按钮空操作、`user_message` 正常入。

**C8 — `agent_run_start` / `agent_run_cancel` inbound**

- 新文件 `chahua/server_inbound_agent_run.py`：新 slot `agent_run`，与 handoff slot 平行（不塞进 handoff）。
- `chahua/server.py` `_INBOUND_ROUTES`：注册；`__init__` 装 `self.agent_run = AgentRunHandlers(self)`。
- 校验：target 在场 / `task_id` 若给未关闭 / `guest_busy(target)==False` / `len(agent_runs) < max_agent_runs_per_room`。
- 通过即：登记同步 `add(target)` + `agent_runs[run_id]=run` → `asyncio.create_task(_run_agent_background)` → `agent_run_tasks[run_id]=task` → emit `AGENT_RUN_STARTED`（顺序：先 `add` 再 `create_task`）。
- `agent_run_cancel`：从 `agent_run_tasks` 查 → `.cancel()`，不动注册表（wrapper finally 自己 pop）。
- 测试：所有拒绝路径 + race 回归（第二条 inbound 在 wrapper 进 speak 之前到达仍被拒）+ `agent_run_cancel` 只取消指定 run。

**C9 — 清理 5 步 + ws 断开分流（关键：保留 MTS 顺序）**

- `clear_room` / `remove_guest` / `_replace_session` / `aclose`：5 步（`cancel_inflight` → cancel `agent_run_tasks` → drain bg → drain inflight → reset/close）。
- `_serve_one` finally **只对前台 runtime 走 1–4**，**不** reset/close 前台 session；后台 runtime 走 `_aclose_one_runtime`（完整 5 步 + close + pop）。
- **MTS 顺序保留**：既有 `_serve_one` / `_aclose_one_runtime` 调用 `end_managed_session(...)` 的位置维持在 cancel/drain **之前**，**不能因为加 5 步顺序误删 / 误后移**——P11 5 步嵌在 `end_managed_session` 之后跑。
- 测试：① drain 期间防新 bg run race（前台跑 `spawn_agent_run` 工具瞬间触发 `clear_room`）；② 切走不 cancel bg run；③ 前台 ws 断开走 1–4 不 reset session；④ 后台 ws 断开走 5 步 + close + pop；⑤ MTS 房 ws 断开仍先 `end_managed_session(user_cancel)` 再走 P11 步骤。

**C10 — 前端（/bg + 弹窗 + sidebar + 列表条 + 事件分叉）**

- `app/renderer/commands.js`：`/bg <茶客> <指令>` + `/help` 文案 + 本地校验。
- 弹窗：单人指派显示「后台运行」复选框；多人圆桌不显示；placeholder「指令（必填，会发给被指派茶客）」。
- `app/renderer/sidebar.js`：`.guest-bg-run`（靠左）+ `applyBackgroundRuns(runs)` + `bgRunByName: Map`。
- 头像 popover：「× 取消后台 run」条件显隐 → 发 `agent_run_cancel`。
- composer 上方后台 run 列表条：`茶客名 · 来源 · 指令预览(≤30字) · 取消按钮`。
- 事件分叉：`message_end(ok)` 只追加气泡；`agent_run_finished` 只移列表条 + sidebar；`agent_run_cancelled/error` 移列表条 + sidebar + 中央系统气泡。
- 切回房 / 重连 / clear：`room_info.data.background_runs` 整批覆盖。
- 测试：手动 e2e + 幽灵指示回归（模拟 `agent_run_finished` 断连丢帧 → 重连 `room_info` 覆盖式清）+ `.guest-score`（右）与 `.guest-bg-run`（左）并存。

### P11.2：Agent 调度并发

**C11 — `spawn_agent_run` / `spawn_agent_runs` 工具（窄回调注入，不扩 session 装配）**

- 新文件 `chahua/agent_run_tools.py`：`register_agent_run_tools(agent, *, source_guest, get_start_agent_run)`——`get_start_agent_run: Callable[[], Callable | None]` 是一个 getter，工具实例只持这个 getter，**每次** tool call 现取 `cb = get_start_agent_run()` → `cb is None` 返 `Error: bg run 入口未装`、`cb is not None` 调 `cb(target, instruction, task_id, source_guest)`。getter 解决「Tool 实例无法直接读 `TeaGuest.start_agent_run` 槽位最新值 / 切房 re-bind 后看不见新回调」的闭合问题。
- `TeaGuest` 持 `start_agent_run: Optional[Callable] = None` 槽位；`TeaGuest.__init__` 注册工具时传 `get_start_agent_run=lambda: self.start_agent_run`——闭包逐次读 instance attr，server 改一处 guest 槽位、所有工具实例立刻见新值。**不**把 `server` / `RoomRuntime` 注入 `register_agent_run_tools`。
- `chahua/server.py::_attach_runtime_state(runtime)`（C2 已抽出）扩展：遍历 `runtime.session.guests`，`guest.start_agent_run = self._make_start_agent_run(runtime)`。回调内部走 `_start_agent_run(runtime, *, target, instruction, task_id, source_guest, batch_index, batch_size)` 私有 helper，**C8 inbound 与 C11 工具共调**——校验/登记单一路径。
- 工具参数面**仅** `{target, instruction, task_id?}`（不引 `agent_run_spec` / `review_message_id` / `propose_agent_runs`）。
- 拒绝：同 C8 inbound + 同批次重复 target + `len > max_agent_runs_per_tool_call(=4)` + `len + 现有 > max_agent_runs_per_room(=4)`。
- 返回 run_id 列表，不等待完成。
- `chahua/guest.py::TeaGuest.__init__`：在既有工具注册段（与 `register_task_tools` / `register_handoff_tools` 并列）调 `register_agent_run_tools(agent, source_guest=self.name, get_start_agent_run=lambda: self.start_agent_run)`——只挂工具 + getter，不绑 runtime（runtime 在 C2 的 `_attach_runtime_state` 内绑）。`build_room_session` 装配层**不**触碰工具注册。
- 测试：① 所有拒绝路径（边界等于不拒）；② 成功创建；③ 工具非 read-only；④ `start_agent_run is None`（裸 session 无 server）时工具返 `Error:` 不抛；⑤ **getter 闭合回归**：先 attach runtime A → 工具调用走 A；切房 attach runtime B（同 guest 槽位被改写）→ 同一 Tool 实例下次 call 自动走 B、不残留 A。

**C12 — MTS × `spawn_agent_run` 边界**

- 确认 `_orchestrator_managed_session.ManagedSessionOps.intercept_task_proposal` **只**拦 `TASK_PROPOSAL`、不碰 `AGENT_RUN_*`。
- `spawn_*` 工具描述 + MTS 启动提示加一句「MTS 中可用 `spawn_agent_runs` 绕开 budget 做并发分发」。
- 测试：MTS 管理者调 `spawn_agent_runs` → bg run 创建成功、budget 未减、`_consecutive_ai_turns` 未增、仍占 `max_agent_runs_per_room`。

### 贯穿提醒（每个 commit 自查）

- **不引入**：新锁（`ws_send_lock` / `append_lock`）/ `closing` 标志 / 状态机 / `AgentRun.status` 字段 / batch group / fan-in。
- **MTS 清理顺序绝不动**：C9 嵌入时 `end_managed_session(...)` 仍在 cancel/drain 之前调，新加 5 步顺序跑在 MTS end 之后。
- **`schema_version` 不 bump**：新 envelope type 走「旧前端忽略未知 type」口径。
- **每 commit 跑全量 `uv run pytest`**：P9 后台 runtime / handoff / MTS 既有回归不能因为 `busy_alive` 替换或清理顺序改动而红。
