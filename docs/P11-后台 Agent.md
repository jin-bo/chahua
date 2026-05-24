# P11：后台 Agent —— 同房间 Agent 并行执行

> 目标：在同一个房间内允许多个 Agent 后台并行执行。后台 run 可以由用户手动指派创建，也可以由正在发言的 Agent 调度创建（例如 Maya 在「审稿委员会」中同时调度多个审稿智能体）。每个后台 Agent run 独立运行、独立取消、执行过程不流式刷聊天区；完成后一次性把最终结果作为普通茶客气泡提交到房间 transcript，并推送给前端显示。

## 评审结论

只做「隐藏流式、完成后显示」不够。那仍然占用现有单 `inflight_task`，同房间里不能同时跑多个 Agent，也不能让用户继续安排更多后台工作。P11 必须拆掉“一个房间只有一个在飞执行”的假设，新增 run 级运行态。

P11 的边界是：

- 做同房间多后台 Agent 并行执行。
- 同一 room 内允许多个不同 Agent 并行；同一个 Agent 在同一 room 内最多一个 active background run。
- 保留普通前台聊天 / scoring / handoff drain 的单 in-flight 语义。
- 后台并行分阶段实现：先支持用户手动单人指派，再支持 Agent 通过调度工具 fan-out 多个后台 run。
- 不把既有 `handoff_review` / `handoff_panel` / MTS 直接并行化；Agent 调度并发走新的后台 run 通道。
- 成功结果进入 transcript；失败 / 取消只推终态通知，不污染 transcript。

## Summary

用户在「指派下一句」里勾选「后台运行」，或 Agent 调用并发调度工具后，服务端创建一个或多个 `AgentRun`，立即返回前端并显示后台任务状态。多个后台 run 可以在同一房间同时存在。

后台 run 执行时不推 `message_start` / `message_delta` / tool 流式事件；完成时放行最终 `message_end(status=ok, data.text=...)`，前端现有“无 start 直接 end”的兜底路径会一次性追加普通茶客气泡。该气泡已落 transcript，可被后续上下文、请审和任务归档使用。

## Key Changes

- 新增 `AgentRun` 运行态：
  - 字段：`run_id`、`room_id`、`guest_name`、`task`、`status`、`delivery="batch"`、`task_id`、`created_at_ms`、`completed_at_ms?`、`error?`。
  - run id 使用 `run_<hex>`，不要复用 `turn_id` / `message_id`。
  - `status` 值：`running` / `done` / `cancelled` / `error`。
  - P11 不 clone 茶客实例；一个 run 绑定一个现有 `TeaGuest`。

- 扩展 `RoomRuntime`：
  - 保留现有 `inflight_task` / `inflight_kind`，继续表示前台普通 turn 或 handoff drain。
  - 新增 `agent_runs: dict[str, AgentRun]`，表示同房间后台并行 run。
  - 新增 guest 级唯一性不变量：同一个 `guest_name` 在 `running` 状态下最多出现一次。
  - 新增 `append_lock: asyncio.Lock` 或等价提交锁，串行化后台 run 的最终 transcript append / cursor 推进 / artifact detect 收尾。
  - `inflight_alive()` 仍只描述前台 in-flight；新增 `busy_alive()` 或 `has_active_runs()` 用于房间列表和关闭清理。

- 新增后台指派 inbound：
  - 推荐新增 `agent_run_start`，payload：`{target: str, reason?: str, task_id?: str}`。
  - 前端「后台运行」走 `agent_run_start`，不要再塞进 `handoff_delegate background=true`，避免把并行 run 绑死在 handoff drain loop。
  - 服务端校验 target 在场、无未知字段、可选 `task_id` 存在且未关闭。
  - 若同一 room 内该 target 已有 running background run，则拒绝创建并 emit `NOTICE error`。
  - 启动后 emit `agent_run_started {run_id, guest_name, task_id?}`。

- 新增 Agent 调度工具：
  - 新模块建议为 `chahua/agent_run_tools.py`，注册给茶客使用。
  - `spawn_agent_run(target, instruction, task_id?)`：调度一个后台 Agent run。
  - `spawn_agent_runs(items)`：批量调度多个后台 Agent run，适合 Maya 同时指派多个审稿智能体。
  - 工具是调度型写操作：会创建运行态、消耗模型调用、影响 transcript；权限上不能标成 read-only。
  - 工具只能调度当前房间在场茶客，不能调度自己以外的外部 agent，也不能跨房间调度。
  - 工具必须拒绝同一批次里的重复 target；也必须拒绝当前已有 running background run 的 target。
  - 工具返回给调用者的文本只确认「已创建后台 run」，不等待被调度 Agent 完成。

- 新增 run 级 envelope：
  - `agent_run_started`
  - `agent_run_finished`
  - `agent_run_cancelled`
  - `agent_run_error`
  - data 至少包含 `{run_id, guest_name, task_id?, issued_by, instruction_preview?}`；错误态带 `{error}`。
  - 这些是 UI 状态事件，不进 transcript。

- 后台执行路径：
  - 新增 `_run_agent_background(runtime, run, prompt_context)` wrapper，由 `asyncio.create_task` 启动并登记到 `runtime.agent_runs`。
  - 不走 `run_pending_handoff()`，因为 handoff drain 是队列串行模型。
  - 直接调用目标 `TeaGuest.speak(...)`，但 sink 使用 `BatchMessageSink(runtime.router, run_id=...)`。
  - `BatchMessageSink` 丢弃高频流式事件，放行最终 `message_end`；放行时给 `data.run_id` 补 run id，方便前端关联状态。
  - 因为 P11 复用现有 `TeaGuest` 实例，不允许同一个 guest 并发进入两个 `speak()` 调用；guest 级唯一性校验是硬约束。

- prompt / 上下文：
  - 后台 run 启动时 snapshot 当前 active task id 与当前 transcript 视图。
  - 后台 run 不参与 scoring、不改变 cooldown、不推进 `_consecutive_ai_turns`。
  - `instruction` 是本次后台 Agent run 的工作说明，必须进入该 Agent prompt；不要只放 debug。
  - 用户手动入口的 `reason` 映射为 `instruction`；若为空，默认指令为「请基于当前房间上下文给出你的处理结果」。
  - Agent 调度入口必须显式提供 `instruction`，为空则拒绝创建 run。
  - prompt 中应标明 `issued_by`：用户手动指派或某个 Agent 调度，便于被调度者理解任务来源。

- transcript 提交顺序：
  - 后台 run 并行执行，但最终写 transcript 必须串行。
  - P11 采用“完成顺序提交”：哪个 run 先成功完成，哪个先 append 到 transcript。
  - 提交锁覆盖 `room.append` 后的收尾动作，避免两个 run 同时写 transcript / artifact registry / debug flush 产生交错。
  - 后续如需要“指派顺序提交”，另开阶段设计等待队列；P11 不做。

- 取消与清理：
  - 新增 `agent_run_cancel {run_id}`，只取消指定后台 run。
  - 现有 `cancel` 仍只取消前台 in-flight，不默认取消所有后台 run。
  - `clear_room`、删除/移除茶客、同房间重建 session 时必须 cancel+drain 所有后台 run。
  - 切房时后台 run 跟随 P9 的 `RoomRuntime` 留在后台继续跑；房间列表 busy 状态应同时看前台 in-flight 和 `agent_runs`。

- 前端：
  - 「指派下一句」弹窗在单人选择时显示「后台运行」复选框。
  - 勾选后台后发送 `agent_run_start`，不占用主发送按钮的停止态。
  - 聊天区或 composer 上方显示后台 run 列表：茶客名、运行中状态、取消按钮。
  - 收到最终 `message_end` 时一次性追加普通茶客气泡；收到 `agent_run_finished` 后移除运行中状态。
  - 失败 / 取消显示系统气泡或状态提示，不生成普通茶客气泡。

## Phases

### P11.1：运行态与用户手动并行指派

- 新增 `AgentRun`、`RoomRuntime.agent_runs`、run 级 envelope、`agent_run_start` / `agent_run_cancel`。
- 前端单人「后台运行」入口接入 `agent_run_start`。
- 多个用户手动后台 run 可以并行，但 targets 必须是不同茶客。
- 完成结果按完成顺序进入 transcript。

### P11.2：Agent 调度并发

- 新增 `spawn_agent_run` / `spawn_agent_runs` 工具，注册给茶客。
- 工具调用直接创建后台 run，不走前端采纳卡；这是调度动作，不是提议。
- 限制单次批量数量，例如 `max_agent_runs_per_tool_call = 4`，避免一个 Agent 误触发大量并发模型调用。
- 限制房间总后台 run 数，例如 `max_agent_runs_per_room = 8`，超过则工具返回错误，不创建新 run。
- 同一批次内 target 不得重复；已在 running 的 target 不得再次创建 run。
- 典型场景：Maya 在「审稿委员会」中调用 `spawn_agent_runs`，同时让「水文献」「魏理论」「贾数据」分别审稿；三者完成后各自作为普通茶客气泡回到 transcript，Maya 后续可基于这些结果再做总结。

### P11.2.1：与 `propose_delegate` / `propose_panel` / `propose_review` 的关系

现有 `propose_*` 仍保留，但需要重新讲清边界：

- `propose_delegate`：茶客建议“下一句交给谁”，用户采纳后走前台 handoff drain，适合需要用户确认的接话安排。
- `propose_panel`：茶客建议“发起圆桌”，用户采纳后仍是同一个 handoff item 内串行发言，不是并行执行。
- `propose_review`：茶客建议“请 reviewer 审 reviewee 最近一条消息”，用户采纳后走 message-id 锚定的前台 review。
- `spawn_agent_run(s)`：茶客在已授权语境下直接创建后台 run，不等待用户采纳，适合 Maya 这类管理者 fan-out 多个审稿智能体。

优化方向不是把 `propose_*` 删除，而是把“调度 payload / 校验 / 目标解析”抽成共享层：

- 新增 `agent_run_spec` 数据结构：`{target, instruction, task_id?, source?, review_message_id?}`。
- `spawn_agent_run(s)` 直接消费 `agent_run_spec` 并创建后台 run。
- 未来可以新增 `propose_agent_runs(specs)`，用于“茶客建议并发调度，但仍需用户采纳”的中间形态；P11 先不做。
- `propose_review` 已有的 reviewee→message_id 解析可复用到后台审稿：`spawn_agent_runs` 的 item 可选 `reviewee` 或 `review_message_id`，实现时冻结成具体 `review_message_id` 后放入 prompt。
- `propose_panel` 不再承担“并行圆桌”的语义；如果 Agent 想并行收集多方意见，应使用 `spawn_agent_runs`。

迁移建议：

- 保留 `propose_delegate/review/panel` 名称和前端 proposal card，不破坏 P7/P8.3。
- 在工具描述里补一句：需要并行后台执行时使用 `spawn_agent_runs`，不要用 `propose_panel`。
- MTS 仍可继续 hook `propose_delegate` / `propose_panel` 入队；P11 不改变 MTS 闭环。
- 后续若 MTS 也要并发化，再让管理者用 `spawn_agent_runs`，而不是把 `propose_panel` 改成并行。

### P11.3：汇总与后续推进

- P11.3 不新增自动 fan-in 调度，只依赖现有 scoring / 用户指派 / Agent 再次调度。
- 若需要“所有后台审稿完成后自动让 Maya 总结”，另开后续阶段设计 run group / barrier / callback。
- P11 只保证并发执行和结果回流，不保证工作流编排闭环。

## Non-Goals

- 不让普通用户消息与前台 scoring 链并行；用户普通发送仍受现有 `inflight_task` 保护。
- 不直接并行化 `handoff_review` / `handoff_panel` / MTS；Agent 想做并发审稿应使用 `spawn_agent_runs`。
- 不做跨 app 重启恢复；后台 run 是进程内瞬态。
- 不保证指派顺序输出；P11 明确采用完成顺序提交。
- 不把后台 run 的工具流式细节展示到聊天区。
- 不做自动等待所有 run 完成后的 callback / barrier；这属于后续 run group 设计。
- 不支持同一茶客多实例并行；如未来需要，另开阶段设计 `AgentRunSession` / guest clone 机制。

## Test Plan

- 后端：
  - 同一房间连续发送两个 `agent_run_start`，产生两个不同 `run_id`，两个 task 同时处于 running。
  - 同一房间对同一个 target 重复发送 `agent_run_start`，第二个请求被拒绝。
  - Agent 调用 `spawn_agent_runs` 能一次创建多个后台 run，且返回 run id 列表。
  - `spawn_agent_runs` 拒绝空 instruction、不在场 target、重复 target、已有 running run 的 target、超过房间并发上限。
  - 两个 run 完成后都进入 transcript，顺序等于完成顺序。
  - 后台 run 执行期间不下发 `message_start/message_delta/tool_*`，只在成功时下发最终 `message_end`。
  - `agent_run_cancel {run_id}` 只取消指定 run，不影响其它后台 run 和前台 in-flight。
  - `clear_room` / remove guest / same-room rebuild 会取消并清空所有后台 run。
  - 切房后原房间后台 run 继续运行，完成后按 P9 后台里程碑推送，切回可从 history 看到结果。

- 前端：
  - 单人指派可选择后台运行，多人圆桌不显示该入口。
  - 多个后台 run 同时显示在运行列表，可分别取消。
  - 后台 run 不切换主发送按钮为停止态。
  - 成功完成后一次性出现普通茶客气泡，并从运行列表移除。
  - 失败 / 取消显示终态提示，不出现可请审的普通结果气泡。
  - Agent 调度产生的后台 run 与用户手动产生的 run 使用同一个运行列表展示，并标明调度来源。

## Assumptions

- 后台 Agent run 使用完成时的 transcript append 顺序作为事实顺序。
- 后台 run 的上下文以启动时快照为准；并行 run 不互相看到对方尚未完成的中间状态。
- 成功结果是普通茶客消息；失败 / 取消不是消息。
- P11 覆盖两类入口：用户手动单人后台指派、Agent 工具调度一个或多个后台 run。
- Agent 调度并发不是 handoff propose，不需要用户采纳；它的权限边界由工具权限和并发上限控制。
- P11 不创建同一茶客的多个实例；并行粒度是「多个不同茶客」。
