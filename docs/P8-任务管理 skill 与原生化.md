# P8：任务管理 —— skill 形态与原生化

> 目标：让 ChaHua 房间能「被管理」—— 某位茶客承担协调职责，把任务 Goal 拆成实施
> 计划、按房间茶客情况分工、检查 Goal 是否达成、未达成时复盘并调整计划。
>
> P8 的技术路线是 **「skill + artifact 约定 + 少量状态提议」**，不是新增一个计划
> 子系统：
> - **P8.1（已落地）**：`examples/personas/Maya/skills/task-management/SKILL.md` ——
>   茶客侧任务管理工作手册（随 Maya persona bundle），完全跑在 P5~P7 已有原语上，
>   零后端改动。这是 **P8 的主方案**。
> - **P8.2（已落地，最小增强）**：只新增一个 `task_propose_status` 工具，补「茶客
>   无法提议把任务设为 done / blocked / review」这一个真实能力缺口。
> - **P8.3（已落地）**：原生自动推进 —— 管理者茶客在被指派茶客执行完后自动
>   获得下一回合。难点在 orchestrator 回合模型，与计划是否结构化无关。完整设计见
>   [`P8.3-原生自动推进.md`](P8.3-原生自动推进.md)。

**状态：P8.1 / P8.2 / P8.3 均已落地（2026-05-21，经评审收敛）**。上游契约见 [`P5-任务房间.md`](P5-任务房间.md)
（Task / Decision / Artifact 模型、`task_*` 工具、propose / 采纳机制）/ [`P7-显式
handoff 与 delegation.md`](P7-显式%20handoff%20与%20delegation.md)（handoff drain loop
回合模型）/ [`P7.4-茶客 propose handoff.md`](P7.4-茶客%20propose%20handoff.md)
（`TASK_PROPOSAL` flat kind + 采纳拼回 inbound 的口径）。

---

## 0. 本阶段做 / 不做

**做**：

- **P8.1**（已落地为 persona-bundled skill）：`examples/personas/Maya/skills/task-management/SKILL.md` 茶客侧
  任务管理工作手册，跑在现有原语上。
- **P8.2**（已落地）：新增**一个**茶客侧工具 `task_propose_status(status,
  reason)`，复用 `TASK_PROPOSAL` envelope，采纳后按状态分流到既有 `update_task` /
  `close_task` inbound（§4）。

**不做（评审收敛，§3 论证）**：

- **不**给 Task 加 `plan_items` / `PlanItem` / `Evaluation` / `acceptance_criteria` /
  `last_evaluation` —— Task 维持轻状态容器（id / title / goal / status / owner /
  decisions / artifacts），结构化计划留在 artifact。
- **不**加 `plan_auto_accept`「整任务自动采纳」开关 —— 计划走 artifact 本就无审批
  摩擦，这个机制解决的是结构化 `plan_items` 自造的问题（§3.2）。
- **不**加 `task_propose_plan` —— 由 `task_write_artifact` 覆盖写计划 artifact 替代。
- **不**加 `task_get_guests` —— 房间级 introspection 不属任务域、且引出晚绑定
  accessor（§3.3）。
- **不**加 `task_get_info` —— 与 `<current_task>` prompt 注入 / `task_list_artifacts`
  重叠。
- **不**新增 `set_plan` inbound、**不**改任务面板渲染计划表。
- **不**做 P8.3 原生自动推进 —— 留草案（§6）。

---

## 1. 背景：skill 形态是主方案

任务管理本质是「拆解 + 分工 + 验收 + 迭代」的协调工作流。ChaHua 在 P5~P7 已经把所需
原语全部备齐：

- **拆解 / 计划 / 验收 / 复盘的持久化** —— `task_write_artifact` 把结构化 markdown
  落到 `tasks/<active>/artifacts/`（计划 / 分析 / 验收报告 / 复盘各一个文件），下一轮
  `<current_task>` 块照常呈现，茶客可 `read_file ./task/<name>` 读回（P5.4 茶客自动
  归集 + 直写例外）。
- **分工** —— `propose_delegate` / `propose_review` / `propose_panel` 提议调度，
  用户采纳后走 P7.1~7.3 既有 `handoff_*` inbound（P7.4 propose 机制）。
- **决策入库** —— `task_propose_decision` 提议、用户采纳。

原语齐全，任务管理就做成一份**纯 prompt 资产**（skill）—— 不动后端。这与 ChaHua
「能 derive 不加冗余状态」「Task 是轻状态容器、复杂过程留在 artifact」的口径一致。
P8.1 的 `SKILL.md` 即此方案，已落地。

## 2. 回合模型约束

ChaHua 是回合制群聊：**茶客每次 `speak()` 只跑一个 turn，发言结束控制权回到用户**，
房间不会自动让某位茶客「醒来」：

- task 事件不进 transcript、不触发 AI（P5.8）。
- `propose_*` 被采纳后走 `handoff_*` drain loop，drain 以 `turn_end(next="user")`
  收尾，**不回落 scoring**（P7.1 不变量）。被指派茶客发言完，控制权回到用户，**不会
  自动回到管理者茶客**。

**推论**：「分析 → 计划 → 指派 → 执行 → 复查 → 调整计划 → 再指派」这个闭环**无法由
管理者茶客自主跑完**。skill 据此把它切成「每回合推进一步 + 在回合结尾交接回用户」，
`SKILL.md` 明确告诉茶客每次只推进一个 step。要让闭环自动转起来需要改 orchestrator
调度层 —— 那是 P8.3（§6），**不是**给 Task 加结构化字段能解决的。

## 3. 评审收敛：为什么不做结构化计划子系统

本文早期版本设计过一套较重的 P8.2（结构化 `plan_items` / `Evaluation` 字段 + 4 个新
工具 + `set_plan` inbound + `plan_auto_accept` 自动采纳开关 + 任务面板计划表）。
经评审（2026-05-21）判定**过度设计**，收敛为本文当前版本。三条核心理由：

### 3.1 Task 应维持轻状态容器

现有 Task 模型刻意轻：id / title / goal / status / owner + decisions + artifacts。
一次性加 `PlanItem` / `Evaluation` 两个值对象 + 四个字段，扩张面太大，破坏「Task 轻、
复杂过程留 artifact」的边界。计划 / 验收 / 复盘作为 markdown artifact 已能完整持久化、
且对茶客可读可写、对用户可在任务面板打开查看。

### 3.2 `plan_auto_accept` 解决的是自造的问题

「整任务自动采纳」开关本是为了解决「计划频繁变更、每改一步都要用户采纳」的摩擦。
但**这个摩擦只在把 `plan_items` 升成结构化字段、要求走 propose / 采纳后才出现**。
计划若维持 artifact 形态，`task_write_artifact` 是 P5.4 明确的「茶客可直写」例外
——本就无需采纳、无摩擦。`plan_items` 制造摩擦、`plan_auto_accept` 再补摩擦，是
循环设计。把 `plan_items` 拿掉，`plan_auto_accept` 连同它的 Task 字段 / 前端自动
写 inbound / UI 开关 / 系统气泡 / 权限论证一并消失。

### 3.3 4 个候选工具砍掉 3 个

- `task_propose_plan` —— 由 `task_write_artifact` 覆盖写计划 artifact 替代。
- `task_get_guests` —— 房间级 introspection，不该塞进 `task_tools.py`（与「handoff
  工具不带 `task_` 前缀因属房间级」口径冲突）；且要穿透 orchestrator runtime 花名册，
  引出晚绑定 accessor，把工具注册生命周期复杂化。分工时茶客可依赖 onboarding /
  上下文里的房间成员信息；用户要查能力有现成的 `/tools` `/skills`。
- `task_get_info` —— 与 `<current_task>` prompt 注入、`task_list_artifacts` 部分
  重叠。`plan_items` 砍掉后更没有「完整计划表超 token 预算需按需拉」的动机。

**保留的只有 `task_propose_status`** —— 它补的是真实能力缺口：茶客目前**无法**提议把
任务设为 `done` / `review` / `blocked`，只能在聊天里口头提醒用户去任务面板手点。

## 4. P8.2：唯一的小增强 —— `task_propose_status`

`chahua/task_tools.py` 新增一个工具，扩 `_TaskProposeBase`（与 `task_propose_decision`
/ `task_propose_open` 同基类）。

| 项 | 设计 |
| --- | --- |
| 工具 | `task_propose_status(status, reason)` |
| `is_read_only` | `True` —— 为权限层放行；不写库不落盘，但 emit `TASK_PROPOSAL`（同 `task_propose_*` 口径，P5.3.4） |
| `status` 取值 | `ready` / `doing` / `blocked` / `review` / `done` / `abandoned`（不含创建态 `open`） |
| envelope | 复用 `TASK_PROPOSAL`；新增 flat kind `TASK_PROPOSAL_KIND_STATUS = "status"`（`events.py` + `events.js` 两处常量同步，P7.4 flat kind 口径） |
| 采纳后 inbound | **按终结态分流，复用两个既有 inbound**——前端 `proposal_card.js` 的 `buildAcceptInbound` 加一个 `status` 分支，按 `status` 落到：① 非终结态（`ready` / `doing` / `blocked` / `review`）→ `update_task {type, task_id, patch:{status}}`；② 终结态（`done` / `abandoned`）→ `close_task {type, task_id, status}`。**不能都走 `update_task`**——`update_task` 入站校验明确拒绝终结态（`server_inbound_task.py`：`patch.status ∈ CLOSED_STATUSES` → NOTICE error），终结态必经 `close_task`。`update_task` 的 status 走 `patch` 子对象、不是顶层字段 |
| `reason` | 提议理由，仅显示在采纳卡片上，不入库（采纳走 `update_task` / `close_task` 白名单，`reason` 不在内 —— 同 `task_propose_decision` 的 `supporting_message_ids` 之外字段不漏进 inbound 的口径） |

**改动面**：`task_tools.py` 一个工具类 + `register_task_tools` 一行 + `events.py` /
`events.js` 一个常量 + `proposal_card.js` 一个 `renderPayloadPreview` / 一个
`buildAcceptInbound` 分支（分支内按 `status` 分流 `update_task` / `close_task`）。
**不改** Task 模型、不改 `tasks_store`、**不加任何 inbound**（终结态 / 非终结态都
复用现有写路径）、不改任务面板。测试覆盖两类状态各一条（非终结态拼出 `update_task`、
终结态拼出 `close_task`）。

采纳后的任务状态写入**不触发 AI**（P5.8 task 事件口径）—— 非终结态走 `update_task`、
终结态走 `close_task`，两条都只 emit hint + `task_info`，房间静默等用户下一条消息。

## 5. 能力花名册（P8.2-roster-a / -b）

> 与 P8.2 的 `task_propose_status` 解耦、单独排期。能力花名册是必要能力，但**不是
> 一个「小阶段」** —— 拆成两段落地：**P8.2-roster-a** 手写 `[[guest]].summary` +
> `<room>` 渲染（可独立完整落地）；**P8.2-roster-b** LLM 生成缓存（独立增强，作为
> 「补全缓存」旁路）。LLM 生成是用户拍板要做的（2026-05-21），但**约束硬性**：不为
> 它把房间装配链路异步化，不让房间启动强依赖 LLM（§5.3）。

### 5.1 问题

茶客要「把工作安排 / `@` / `propose_review` 给最合适的人」，却**看不到别的茶客擅长
什么**。`<room>` 块只有一行 `在场：用户, A, B, …`（`context_renderer.py:238` 的 `在场`
行 = `room.participants` 名字列表），不带 persona / 工具 / skills；`<user_persona>`
装的是**用户**的 USER.md，不是其它茶客的人设。茶客今天只能靠名字的角色语义 +
`<recent_messages>` 里谁说过什么这两个弱信号选人。

### 5.2 P8.2-roster-a：手写 `[[guest]].summary` + `<room>` 渲染

第一段，可独立完整落地、不依赖 LLM：

- `room.toml` 的 `[[guest]]` 新增**可选**字段 `summary: str`。
- `<room>` 块 `在场` 行渲染：
  - 有摘要：`- 范总 —— 擅长成本测算与预算评审`
  - 无摘要：`- 范总`（= 当前行为，优雅降级）
- **只进 onboarding 的 `<room>`，不进每轮的 `<room_update>`** —— 摘要是稳定信息，
  onboarding 一次注入足够；进 incremental 会被 N 茶客 × T 轮放大 token。
- 摘要渲染时硬截断到一行（≤ ~40 字），防 persona 里写长句撑破 `<room>` 块版式。

落地点：`config.py`（`[[guest]]` 白名单加可选 `summary` + 解析，缺省 `None`）/
room snapshot + `admin_toml.py`（`summary` 进 round-trip，P4「配置闭环四点」口径，
否则结构化重写 toml 丢字段）/ `context_renderer.py`（`在场` 行按摘要有无分支 + 截断）。
测试：有 / 无 `summary` 两种渲染 + toml round-trip 不丢字段。

### 5.3 P8.2-roster-b：LLM 生成缓存（旁路增强，不进主链路）

第二段，独立增强。把摘要来源从「手写 / 名字」两级扩成三级 —— 中间插一层 LLM 生成。
**关键约束：LLM 生成只是「补全缓存」的旁路，不进房间装配主链路、不改回合模型。**

摘要解析（`<room>` 渲染时，每位 guest）：

1. 有 `[[guest]].summary` → 用它，**不触发任何 LLM 调用**。
2. 否则查中央缓存（§见下「缓存」）：命中且 `gen_version` 未变 → 用缓存摘要。
3. 缓存 miss / stale → `<room>` 此次**先只列名字**；若摘要尚未被任何触发点预热过，
   则**后台**请求一次生成（兜底，见下「生成时机」）。
4. 后台生成把 `{persona_hash, gen_version, summary}` 写入中央缓存。
5. 摘要在**下一次 onboarding 渲染**时才生效（命中第 2 步）—— 具体是：下次重建
   session、或新加入 guest 走 onboarding 时。**同一 session 内的「下一轮」不会生效**
   —— 摘要只进 onboarding 的 `<room>`、不进每轮 `<room_update>`（§5.2），后台补好
   缓存也不会即时刷进进行中的会话窗口。

**生成时机 —— 房间装配（`build_room_session`，已落地口径）**：

- `build_room_session` 解析花名册时，对手写与缓存**都 miss** 的 persona 收集成候选，
  调 `persona_summary.schedule_generation(...)` 后台预热中央缓存。生成在后台 task 跑，
  不阻塞装配 / boot；摘要在**下一次重建 session** 时进 `<room>`（见「摘要解析」第 5 步）。
- **admin 加 / 改 guest 自动覆盖** —— admin 改 guest 走 `_replace_session` → 重跑
  `build_room_session`，装配触发点天然覆盖，无需单独 hook。
- **persona 导入暂不单独预热** —— 导入只落 persona 文件、不动当前房间；该 persona 被
  加进任何房间时由装配触发点补缓存。「导入即预热」是纯延迟优化（少等一次重建），
  本期不做，留作后续 follow-up。

设计早期版本想做「导入时 / admin 时」独立预热 hook + 注入式
`schedule_persona_summary_generation` 间接层。落地时收敛掉 —— 见下方工程契约。

工程契约 —— 把评审担心的复杂度写实：

- **不改 `build_room_session` 为 async** —— 现有 `build_room_session` 是同步函数，
  为摘要生成把它异步化会逼整条房间装配链路异步化、改动面失控。
- **后台生成走 `schedule_generation` + `get_running_loop()` 守卫，不需注入式调度层**
  —— `build_room_session` 是同步函数，但它的三个调用点（CLI `_repl` /
  `server_entry._serve` / `server._replace_session`）**实测都在 `async def` 内**，运行期
  必有 event loop。`persona_summary.schedule_generation` 内 `asyncio.get_running_loop()`
  守卫：有 loop → `create_task` 后台生成；无 loop（sync 单测装配）→ WARN 跳过、不抛。
  实跑都会预热，sync 单测优雅降级。早期设计的注入式 `schedule_persona_summary_
  generation(...)` 间接层被这条守卫取代 —— 既然实跑必有 loop，间接层是多余抽象。
  后台 task 持引用（模块级 `_inflight` dict）防 GC、按 persona hash 去重（多房间并发
  调度同一 persona 只跑一次）。**不**用 `asyncio.gather` 嵌进同步装配链。
- **boot 不被 LLM 强依赖** —— 缓存 miss 时房间照常启动、`<room>` 先只列名字；摘要
  在后台补，**下一次 onboarding** 才生效（见上「摘要解析」第 5 步）。首轮可能只有
  名字，**接受**。
- **缓存命中零 LLM 调用** —— 稳态下 boot / onboarding 不调 LLM。
- **中央缓存、内容寻址** —— 缓存落 `user_data_root/.chahua/persona-summaries/`，**按
  persona md 内容 hash 寻址**（键 = hash + `gen_version`，值含 `summary`）。**不**放
  guest 工作目录 —— 因为「导入角色时生成」发生在 persona 还没绑进任何房间 / 没有
  guest 工作目录之前，缓存必须是 persona 级。中央化还顺带两个好处：① 写在
  `user_data_root`（可写），bundled persona（app_root 只读）也能缓存；② 同一 persona
  被多个房间用只生成一次（content-addressed 天然去重）。persona 改了 hash 变 → 自动
  失效重生成；`gen_version` 是模块常量，改生成 prompt 时 bump 一次即让所有旧缓存
  失效。缓存是 **cache**（可重生成、丢了不影响正确性），不算「冗余 state」。
- **生成用 summary effective spec 的已建 client，不另建** —— 复用 `build_room_session`
  装配期已建好的 `summary_client`（`[summary]` → scoring → 房间默认 fallback 链）。
  **不另 `build_client`** —— agentao `LLMClient.__init__` 会 evict 共享 logger 的文件
  handler，多建一份会静默 detach 茶客日志。`[summary]` 不保证便宜，文档不承诺 cheap。
- **失败只 WARN、不 NOTICE、不阻断房间** —— 生成失败（无 key / 网络 / 解析）→ 退回
  只列名字 + WARN，不弹 NOTICE、不阻断 boot（与「recorder / rotation 失败不阻断房间」
  同口径）。`chat_oneshot` 对所有异常兜底返 `""`。

落地点（已实现）：新增 `persona_summary.py`（内容 hash / 中央缓存读写 / LLM 生成 /
`schedule_generation` / `resolve_guest_summary` 三级解析）+ `session.py`
（`build_room_session` 解析花名册 + 调 `schedule_generation` 预热）+ `context_renderer.py`
（`clamp_summary` 单点共用、`<room>` 在场行按 roster dict 渲染）。测试
`tests/test_persona_summary.py`：内容寻址 hash / 缓存 round-trip / `gen_version` 失效 /
损坏视作 miss / 三级解析 / LLM 生成 + 截断 / `schedule_generation` 无 loop 跳过 / 有 loop
写缓存 / 已缓存不重跑。

这套机制有工程量，但**不进 task tool、不影响 P8.2 的 `task_propose_status`、不改房间
装配链路为 async、不改回合模型**；稳态缓存命中时不调用 LLM。

## 6. P8.3（草案）：原生自动推进

> **完整设计与落地见 [`P8.3-原生自动推进.md`](P8.3-原生自动推进.md)（2026-05-21 已落地）。**
> 本节保留为方向草案，记录「自动推进难在哪、需要什么」；P8.3 据此落地为
> 「托管任务会话（MTS）」运行态 + drain loop `_advance_managed_session_after_turn`
> 步 + 管理者提议在 MTS 内由后端 hook 自动入队。

「原生自动推进」= 管理者茶客发起一次 handoff 后，被指派茶客执行完，控制权**自动回到
管理者**复查，而不是回到用户。难点**不在** Task 数据结构、**不在** `plan_items` 是否
结构化 —— 在 orchestrator 的回合模型（§2）。即使计划仍是 markdown artifact，管理者
也能 `task_list_artifacts` / `read_file ./task/<plan>.md` 读回计划继续推进。

核心只需在 P7 handoff drain loop 附近加一条调度规则：

1. 管理者茶客发起一次 handoff，记录 `manager_guest`。
2. 被指派茶客完成发言后，不直接 `next="user"`。
3. 若仍有 active task 且未触发轮数 / 取消 / 错误上限，自动 `enqueue` 一次
   `delegate(manager_guest)`。
4. 管理者复查结果，再决定下一步。

建议把它设计成一个**很窄**的 P8.3 —— 「managed task session」运行态：

- 运行态字段：`task_id` / `manager_guest` / `remaining_budget` / `enabled`。
- **不落盘** —— 与 `_handoff_queue` 同瞬态语义（P7.1），刷新 / 切房即停。
- 每次 handoff item 完成后，若 session 仍有效，把管理者放回队列。
- **停止条件**：达到 `max_consecutive_ai_turns` / 任务关闭 / 用户取消 / 目标 guest
  不存在 / 工具错误。
- 由用户**显式启动 / 停止**，避免 AI 自我无限循环。

结论：**不做结构化 `plan_items` 不阻塞原生自动推进**。自动推进需要的是「谁是管理者、
何时回调、预算与停止条件」——都是调度层运行态；计划可以一直留在 artifact 里。

## 7. 评审遗留与口径

P8.1 SKILL.md 评审已落实的修订（2026-05-21）：

- 明确「skill 不能自主多回合循环」—— 每回合只推进一步、结尾交接回用户（§2）。
- 抽走 SKILL.md 里早期的「Task 数据结构建议 / Task Tools 建议」两节 —— 设计分析不进
  运行时 prompt。本文 §3~§6 即这些讨论的归宿。
- 补 `propose_review` 约束；软化「任务管理者」身份声明；触发短语拆到 `when-to-use` 键。

`task_write_artifact` 后续加了 `append` 形参（2026-05-21，参考 agentao `WriteFileTool`
的 `append: bool`）—— 早期 SKILL.md 误写「追加或写入」、评审中一版修正为「只能覆盖
写、要 append 得 read-modify-write」，`append` 落地后两种说法都不再准确。现行口径：
**末尾增量补内容用 `append=true`；改文件中段仍 read-modify-write + `append=false` 整体
写回**。SKILL.md（§可用工具策略 / Step 4.3 / 失败复盘模板）已据此同步。

P8.2 落地后，`SKILL.md` Step 5「判断 Goal」可补一句：达成时用 `task_propose_status
("done", ...)` 提议关闭，取代现在「口头提醒用户去面板点 done」。这是 §4 落地后的
小幅 SKILL.md 跟进，**不**引入对 artifact 工作流的其它改动。

## 8. 阶段拆分

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| **P8.1** | `examples/personas/Maya/skills/task-management/SKILL.md` 茶客侧任务管理 skill（随 Maya persona bundle），零后端改动 | 已落地 |
| **P8.2** | `task_propose_status` 工具 + `TASK_PROPOSAL_KIND_STATUS` flat kind + `proposal_card.js` 一个 `status` 分支（采纳按终结态分流 `update_task` / `close_task`）+ SKILL.md Step 5 跟进 + 测试（两类状态各覆盖） | 已落地 |
| **P8.2-roster-a** | 能力花名册（§5.2）—— 可选 `[[guest]].summary` room.toml 字段（走 P4 配置闭环四点）+ `context_renderer.py` 的 `<room>` 渲染（手写摘要 / 退回名字两级）。不依赖 LLM | 已落地 |
| **P8.2-roster-b** | LLM 生成缓存（§5.3）—— `persona_summary.py`：persona 摘要后台生成、落 `user_data_root` 中央内容寻址缓存（hash 键 + `gen_version`）。`build_room_session` 装配时对缓存 miss 的 persona 调 `schedule_generation` 预热（`get_running_loop()` 守卫，无 loop 跳过）。`build_room_session` 不改 async、boot 不阻塞、缓存命中零调用 | 已落地 |
| **P8.3** | 原生自动推进 —— 「托管任务会话（MTS）」运行态 + handoff drain loop `_advance_managed_session_after_turn` 步 + 管理者提议经 `task_proposal_hook` 在 MTS 内自动入队 + `<managed_session>` prompt 块 + 任务面板托管按钮 / 状态条，详见 [`P8.3-原生自动推进.md`](P8.3-原生自动推进.md) | 已落地 |
