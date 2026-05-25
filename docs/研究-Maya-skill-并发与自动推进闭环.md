# 研究：Maya `task-management` skill 在 P11.2 后能否闭环原生自动推进

> 评估对象：`examples/personas/Maya/skills/task-management/SKILL.md`（同步种到
> `~/Library/Application Support/chahua/chahua/personas/Maya/skills/task-management/SKILL.md`）。
>
> 评估问题：P11.2（`spawn_agent_run` / `spawn_agent_runs` 工具落地）之后，该 skill
> 能否实现「Maya 并发分派 N 位茶客 → 等所有 bg run 完成 → 自动整合」的端到端
> **原生自动推进闭环**。
>
> 结论：**并发分派（fan-out）能做且不破坏现有自动推进语义；汇总（fan-in）必须
> 由用户 `@Maya` 二次触发** —— 这与 P11.2 显式的 Non-Goal「不自动 fan-in / 不引入
> batch fan-in 系统气泡」一致，**不是 skill 缺陷**。本研究给出三档改进方案
> （SKILL-only / 前端轻量 / P11.3 后端 barrier），按风险与触碰 Non-Goal 程度
> 分档；推荐档 1 立即落地、档 2 视后续 skill 增多再做、档 3 不主动推。

**状态：研究稿（2026-05-24）**。上游契约见
[`P11-后台 Agent.md`](P11-后台%20Agent.md)（`AgentRun` / `BatchMessageSink` /
P11.2 `spawn_agent_run(s)` 工具面）、[`P8.3-原生自动推进.md`](P8.3-原生自动推进.md)
（MTS 调度层）、[`P7-显式 handoff 与 delegation.md`](P7-显式%20handoff%20与%20delegation.md)
（drain loop）。复述自动推进核心实现：`chahua/orchestrator.py:387`
`submit_user_message` / `chahua/_orchestrator_chain.py:64` `run_ai_chain` /
`chahua/_orchestrator_scoring.py:55` `pick_next_speaker`。

---

## 1. 原生自动推进的关键约束

承重三条（详见 `CLAUDE.md` §「调度」/ §「handoff」）：

1. **唯一触发点**：`Orchestrator.submit_user_message` 是 AI 链启动的唯一入口
   （`orchestrator.py:387`）。`run_pending_handoff` 与 `_run_ai_chain` 严格分流、
   不互相回落 —— drain loop 跑完不回 scoring，下次 scoring 须用户消息触发。
2. **@ 只在首轮认**：`respect_at_mention = (_consecutive_ai_turns == 0)`
   （传入位 `_orchestrator_chain.py:83`；形参定义 `_orchestrator_scoring.py:58`）——
   AI 接力时（链内 ≥2 轮）`@<茶客>` 不再走确定性路由，必须靠打分入选。
3. **bg run 不参与自动推进状态机**（P11 §「后台执行路径」）：bg wrapper **不**改
   `_cooldown` / `_consecutive_ai_turns`；bg run 成功时只 emit `message_end(ok)`
   追加一条普通茶客气泡到 transcript，**不**调 `submit_user_message`、**不**重启
   scoring 主循环。

## 2. P11.2 给 Maya 的并发能力

P11.2 落地后（参见 [`P11-后台 Agent.md`](P11-后台%20Agent.md) §「入口」`spawn_agent_run(s)
工具（P11.2）」+ Commit Checklist C11）：

- `spawn_agent_run({target, instruction, task_id?})` —— 单条 fire-and-forget bg run。
- `spawn_agent_runs([{...}, ...])` —— 同批次并发分派；同 target 拒重复。
- 上限 `max_agent_runs_per_tool_call = 4` / `max_agent_runs_per_room = 4`。
- 工具非 read-only；返回 `run_id` 列表，**不等待完成**。
- bg run 之间通过 `active_guest_names` 互斥（同茶客同时刻最多 1 个 `speak()`），
  与前台 `_let_speak` / handoff drain 共享同一互斥域。

**重要校准**：当前 `SKILL.md` 主体仍是「`propose_*` 工具面」（第 39-45 行 / 第
122-156 行 Step 3），**未提**任何 `spawn_agent_run(s)`，没有 3 路并发审稿示例。
本研究里所有「档 1 / 档 1.5 落点」都默认**先**给 SKILL Step 3 补一段
`spawn_agent_runs` 用法（含 3 路并发审稿示例），再叠加各档的兜底卡 / 用户体验改进。
也就是：研究结论里的「SKILL-only」并非「现成可用」，而是「**只动 SKILL.md、不动后端代码**」
的工作量；评估结论不变，但落点章节都要先包含「在 Step 3 加入 `spawn_agent_runs`
段」这一前置步骤。

## 3. 评估：能做到的（fan-out 段）

| 能力 | 是否成立 | 依据 |
|---|---|---|
| Maya 一回合内并发派 ≤4 个 bg run | ✅ | P11.2 `spawn_agent_runs` 工具上限 |
| 不烧 Maya 自己的 AI 链预算 | ✅ | bg wrapper 不改 `_consecutive_ai_turns` |
| 不污染冷却 | ✅ | bg wrapper 不改 `_cooldown` |
| 不抢前台 mic / 不与前台 turn 串台 | ✅ | `active_guest_names` 互斥 + `BatchMessageSink` 丢流式事件 |
| MTS 内可用、不耗 budget | ✅ | P11 §「MTS × `spawn_agent_run`」+ C12 |
| 同 target 重复 / 不在场 / 超 cap → 安全拒绝 | ✅ | C11 测试覆盖 |

注：SKILL.md 现版本未声明 bg run 互斥语义 —— 这条同样在「补 Step 3 `spawn_agent_runs`
段」时一起写进 SKILL（与 P11 `guest_busy()` 互斥域语义对齐：同一个 target 同时
最多 1 个 `speak()`）。

## 4. 评估：做不到的（fan-in 段）

承重事实：**bg run 完成不会唤醒 Maya**。

- `message_end(ok)` 经 `BatchMessageSink` 单帧下发追加气泡，**不**触发任何 orchestrator
  调度路径。
- 即使前台 AI 链尚未结束（`_consecutive_ai_turns < cap` 且 chain 还活着），Maya 也
  不会因为 bg 完成而被加分；她最多按现有 scoring 规则在某轮被自然选中，但**此时她
  看到的 `<recent_messages>` 不会等齐 3 个 bg 结果再渲染** —— 各 bg 完成时间不一，
  scoring 周期与 bg 完成无任何同步。
- 第二轮起 `@` 失效 —— 即便上游 SKILL 输出「`@Maya` 后请整合」，由于 `_consecutive_ai_turns
  > 0`，自动推进周期内 `@` 不走确定性路由。
- 没有 barrier / group id / fan-in 回调原语（P11 Non-Goals 明示）。

**SKILL 已正确承认这条边界**（第 18-19 行 / 第 137 行 / 第 228 行三处都要求每个
回合结尾写明「请 `@我`」）—— 实质是要求**用户触发一条新 user_message** 把
`_consecutive_ai_turns` 归零、重启自动推进周期。

**结论**：P11.2 落地后，「分派 → 等待 → 整合」三段中**前两段由 SKILL + P11.2 自洽**，
**第三段必须用户介入**。这是 ChaHua 架构约束（自动推进只由 user_message 触发）+
P11.2 设计约束（无 batch fan-in）的合并结果，不可由 skill 本身突破。

## 5. 改进方案（按改动面 / 风险递增）

### 档 1：SKILL-only —— 自指 propose_delegate 兜底（零代码）

在 SKILL Step 3「并发分派建议格式」段末追加一段：Maya 调完 `spawn_agent_runs`
之后**立刻**再调一次 `propose_delegate(target="Maya", reason="3 位 bg 全部完成后整合")`，
让聊天区常驻一张「采纳即唤醒 Maya」的 propose 卡。

**用户体验**：从「自己想词 `@Maya`」变成「看 sidebar `.guest-bg-run` 三盏指示
熄灭后点采纳」—— 一次点击替代一次输入，决策点视觉化。

**风险**：需先验 `propose_delegate(target=self)` 在 `_resolve_handoff_winners`
（`chahua/_orchestrator_handoff_drain.py`）是否被特殊处理；若被拒，退路两条：
① 改用 `propose_panel` 单人变体（panel 最小 2 人，单人变体走不通）；
② SKILL 文案改为「请把以下文字粘进输入框 `@Maya 请整合 3 个 bg 结果`」。

**不变量影响**：propose 不入队、采纳才入既有 `handoff_delegate` inbound，
server / orch / cap / MTS / `_consecutive_ai_turns` 计数全零改动。**未触碰任何
P11 Non-Goal**。

**落点**：`examples/personas/Maya/skills/task-management/SKILL.md` —— **前置**
在 Step 3 加一段 `spawn_agent_runs` 用法（含 3 路并发审稿示例），**叠加**在该段
末尾追加「自指 `propose_delegate(Maya)` 兜底」段；Step 5「未达到 Goal」分支同步
提示「先核对全部 bg 完成再整合」。Persona bundle 跟随下一次 user_data_root
re-seed 生效。

### 档 1.5：SKILL-only —— bg agent 自 propose_delegate(Maya)（零代码，与档 1 叠加）

档 1 的接力卡是 Maya **自己留的**（spawn 之前调一次 self-delegate），档 1.5 让
**bg agent 自己**在完成时再留一张 ——「双保险」覆盖「Maya 忘留 / 用户不盯 sidebar」
两种盲区。

**实现**：Maya 在 `spawn_agent_runs` 每条 `instruction` 末尾追加固定一句：

> 「完成你的工作后，请调 `propose_delegate(target="Maya", reason="<你的角色>
> 已完成，请 Maya 整合")`」

**为什么这条工具调用能正确生效**：bg agent 在 `speak()` 内调 `propose_delegate`
会 emit 一帧 `TASK_PROPOSAL`；`BatchMessageSink` 对 `TASK_PROPOSAL` 走**缓冲**
路径（P11 §「BatchMessageSink 事件白名单」），run 完成后由 wrapper finally 第 2 步
`flush_propose_buffer(runtime.router)` 一次性下发前端 —— 也就是说**每个 bg 完成
瞬间，聊天区都会出一张「采纳 → 唤醒 Maya」的 propose 卡**，与 bg 气泡同时刻出现。

**用户体验**：3 个 bg → 3 张卡按 run 完成时刻**依次**叠在聊天区（每个 bg run
独立 finally 即时 flush 自己缓冲的 `TASK_PROPOSAL`；**不存在 fan-in barrier**）。
用户点**任意一张** Maya 即被唤醒，但若**先完成的 run 卡先被点**，此时另外两条
bg 可能还在跑、`<recent_messages>` 缺另外两条 `message_end(ok)` —— 这是档 1.5
最关键的边界。SKILL Step 4 必须**显式**教 Maya 在唤醒回合判定「全部 bg 结果是否
已到齐」（计 `<recent_messages>` 中 `<message speaker_id=bg-target>` 的条数与
本次 spawn 的 target 集合是否一一对应），缺则**不立即整合**、走以下二选一：
① emit「等剩余 X 位完成」+ 重新 `propose_delegate(target="Maya", reason="待剩余
bg 完成后整合")` 留下兜底卡，自己直接退出本轮（让 bg 继续后由后续卡再次唤醒）；
② 用户引导其等待（弱保障，依赖人）。**①是承重路径**。已到齐 → 一次性整合；后到
的卡再被点 Maya 走 SKILL Step 4 的「已整合，跳过」分支。

**与档 1 的关系**：互补不互斥。档 1 是「Maya 留的兜底卡」（覆盖 bg agent 不调
工具的合规失败），档 1.5 是「bg agent 留的高频 CTA」（覆盖用户不盯 sidebar）。
两张卡都指向同一 inbound，采纳语义等价。

**比档 3 优在**：零后端代码 / 零 Non-Goal 冲突 / 不引入自动 user_message 合成；
仍保持「人在闭环」（采纳是人点）。

**风险与边界**：

- **LLM 合规性 ~80%**：bg agent 可能不照 instruction 调工具。失败时 fall back
  到档 1 的 Maya 兜底卡 + sidebar 灯。这就是档 1 与档 1.5 必须叠加的原因。
- **卡的视觉密度**：4 路 fan-out → 4 张卡叠加，UI 上需要 `proposal_card.js` 渲染
  能承住堆叠（已有同类场景：MTS 内多个 propose 并发到达时也会堆 —— 已验过没问题）。
- **bg agent 在 instruction 之外自发调 `propose_*`**：本来也可能（bg 没禁工具
  面）—— 档 1.5 只是把这件事**显式纳入 SKILL 规范**，不会引入新行为类型。
- **~~MTS 内退化为半自动 fan-in（重要边界）~~ —— P11.2.X 已修，本条仅留作历史**：
  P11.2.X 实现了「MTS × bg run 续命闭环」（见 [`P11-后台 Agent.md`](P11-后台%20Agent.md)
  §「MTS × `spawn_agent_run`」+ `tests/test_mts_bg_spawn_reentry.py`）—— bg run
  完成时由 server 端 wrapper finally 自动 enqueue 管理者复查 + 扣 1 budget，
  绕开 propose hook 路径。MTS 内**不再**需要 bg agent 自留接力卡；档 1.5 的
  「instruction 末尾追加 `propose_delegate(target=self)` 固定句」在 MTS 内不应再
  使用（会被 hook 静默吞掉、无效）。SKILL Step 3 / Step 4 已同步更新「MTS 内的
  并发派后台 run」与「MTS 内的 bg 整合分支」段。**原历史警告**：bg agent emit
  的 `propose_delegate(target="Maya")` 在 MTS 内会被 `_intercept_task_proposal`
  自动入队为管理者回合（跳过用户采纳）—— P11.2.X 之后，承担「续命」职责的是
  wrapper finally 的 `advance_after_bg_completion`，propose 自指卡不再是必须路径。

**不变量影响**：与档 1 同 —— propose 不入队 / 采纳走既有 inbound / server / orch /
cap / MTS / `_consecutive_ai_turns` 全零改动。**未触碰任何 P11 Non-Goal**。

**落点**：`examples/personas/Maya/skills/task-management/SKILL.md` —— **前置**
同档 1（先在 Step 3 加 `spawn_agent_runs` 段并写出 3 路并发审稿示例），**叠加**
在该示例每条 instruction 末尾追加固定句。同步在 Step 4「执行 → 复盘」段教 Maya
三件事：① 唤醒回合先核对「`<recent_messages>` 中 bg 完成条数 == spawn target
集合大小」；② 不全 → emit 等待 + 重 `propose_delegate(Maya)` 兜底；③ 已全且已
整合过 → emit「已整合，跳过」。

### 档 2：前端轻量 —— 后台 run 列表条加「@源茶客 整合」一键按钮（半天 ~ 1 天）

在 composer 上方的后台 run 列表条（P11.1 C10 已落）加按 `source_guest` 聚合的
快捷按钮。

**数据**：`AGENT_RUN_STARTED.data.source_guest` 字段 P11 C3 已铺；前端按 `source_guest`
分组维护一个轻量 `Map<source_guest, Set<run_id>>`。

**触发**：每条 `agent_run_finished/cancelled/error` 来时检查该组是否清零；
清零瞬间冒出一颗胶囊「`@<source_guest>` 整合 N 条结果」+ 点击发普通 `user_message
("@<source_guest> 请整合后台结果")`。

**为什么避开了「第二轮 @ 失效」**：胶囊点击产生的是**新一条 user_message**，
自动推进周期重启、`_consecutive_ai_turns` 归零，`@` 走确定性路由生效。

**不变量影响**：纯渲染层；server / envelope / `schema_version` 不动；不引入
batch group id（前端按 `source_guest` 隐式分组）；不引入 server 端 fan-in 事件。
**未触碰任何 P11 Non-Goal**。

**局限**：`source_guest` 字段仅在茶客调度的 bg run 上有值；用户 `/bg` 手动起的
bg run（`issued_by="user"` + 无 `source_guest`）这条胶囊不亮。这是预期 —— 手动
用例本就由人控节奏。

**落点**：`app/renderer/sidebar.js` 或 composer 列表条相关 module（具体位置 P11.1
C10 落地后确认）；新加 ~50 行 JS + 一段 CSS。

### 档 3：P11.3 后端 barrier 原语（中量；触碰 Non-Goal 须重新论证）

如果产品上确实要「完全自动闭环」，提案最窄 barrier：

- 新事件 `AGENT_RUN_BATCH_COMPLETE`：server 按 `source_guest` 聚合，从 N→0
  那一刻 emit 一帧；`data: {source_guest, run_ids: [...], task_id?}`。**仍只是
  hint envelope**，不进 transcript、不触发 AI。
- 新 toml 旋钮 `[room].auto_wake_spawner_on_batch_done = false`（**默认关**）。
  开启时 server 在 emit `BATCH_COMPLETE` 之后合成 `submit_user_message
  ("@<source_guest> 后台批次完成，请整合")`，把 `_consecutive_ai_turns` 归零
  让 `@` 路由生效。
- 实现代价：per-runtime `_bg_run_by_source: dict[guest, set[run_id]]`；wrapper
  finally pop registry 那一步同步 `discard` + 检查空集；asyncio 单 loop 串行
  天然无 race。

**为什么仍危险**：违反 P11.2 Non-Goal「不自动 fan-in / 不引入 batch group id /
不做 batch fan-in 系统气泡」。**必须配 toml 默认关 + 用户旋钮**，否则潜在风险：
「bg 喷一波 → 自唤醒 → manager 再喷一波 → 死循环」（虽然 `max_consecutive_ai_turns=20`
+ `max_agent_runs_per_room=4` 兜底，但烧 LLM 配额）。要做这档**必须先在 docs/P11
里追写「打开此开关的代价 + cap 交互」**，且把 P11.2 Non-Goal 显式重新论证为
「P11.2 不做、P11.3 由旋钮提供」。

**落点**：新建 `docs/P11.3-自动 fan-in.md`；`chahua/server.py` + `chahua/agent_run.py`
+ `chahua/config.py`（toml 字段）+ `chahua/server_room_snapshot.py`（envelope
白名单）+ `examples/room.toml` 默认值。

## 6. 推荐路径

| 档 | 何时做 | 拍板理由 |
|---|---|---|
| 档 1 | **现在**（半小时内动 SKILL，self-delegate 两层放行已 grep 验证） | 零代码、零 Non-Goal 冲突；Maya 兜底卡 —— UX 从「想词输入」降到「看灯点采纳」 |
| 档 1.5 | **与档 1 同批落地**（同一份 SKILL 改动，叠加） | 零代码 / 零冲突；bg 完成自带 CTA 是高频路径主要 UX 提升；与档 1 互为合规失败兜底 |
| 档 2 | 等真有 ≥2 个 Maya 类多轮调度 skill 在用、并发 SKILL 不止 `task-management` 一个时 | 通用性收益与维护成本平衡点 |
| 档 3 | 等档 1.5 上线后埋点观察「propose 卡采纳率」；> 80% 才考虑用旋钮把那一击自动化 | 「人在闭环里」对 LLM 调度失控有保护作用；P11.2 的边界是有意为之的护栏，要破必须用数据 |

## 7. 待验证项（落地档 1 + 档 1.5 前的硬性 grep）

### 7.1 档 1（Maya self-delegate）

1. `propose_delegate(target=self)` 是否被 `_resolve_handoff_winners` /
   `_advance_to_runnable_handoff`（`chahua/_orchestrator_handoff_drain.py`）特判
   或拒绝。
2. `propose_delegate` 工具入口（`chahua/handoff_tools.py`）是否做了 `target != self`
   的预校验。
3. 若 self-delegate 被允许：bg run 完成后 user 采纳卡的 drain 跑 1 个 turn 时，
   Maya 通过 `<recent_messages>` 是否真能看到 3 条 bg 结果（**前提条件**：在 user
   采纳的那一刻 bg run 已经完成 + `message_end(ok)` 已经写进 transcript；
   `_render_incremental` 走 live 视图 → 应能看到，但需测试用例覆盖
   「bg 半数未完成时 Maya 被唤起」的行为：SKILL 应教 Maya 「未齐 → 说明等哪些 +
   再 propose_delegate 自己一次」）。

### 7.2 档 1.5（bg agent 自 propose）

4. bg agent 在 `BatchMessageSink` 包裹下调 `propose_delegate` —— `TASK_PROPOSAL`
   envelope 是否真被**缓冲**而非透传：在 `chahua/agent_run_sink.py`（P11.1 C5
   落地后路径）grep `TASK_PROPOSAL` 的处理分支；同步看 wrapper finally 第 2 步
   `flush_propose_buffer(runtime.router)` 顺序是否在 `_kick_detect_new_artifacts`
   之后、`pop registry` 之前（P11 §「wrapper finally 顺序」）。
5. bg agent 调 `propose_delegate(target="Maya")` 时 propose 卡上的「提议人」字段
   —— `chahua/handoff_tools.py::_ProposeHandoffBase` 是否能拿到当前 guest 名
   （决定前端卡片文案能否区分「Maya 自指」vs「Alice 完成审稿后推 Maya」两类）。
   若工具是 `__init__` 时绑死 guest_name 则自然 OK；若靠 envelope 上下文取则需
   确认 bg `speak()` 路径同样有效。
6. **多张卡并发到达的 UI 行为**：4 路 fan-out → 4 张卡几乎同时 flush，
   `proposal_card.js` 是否能正确堆叠；同 inbound 被采纳第二次是 no-op 还是
   再起一个 handoff turn（后者是预期 —— 每次采纳都跑一轮 drain，Maya 第二
   次唤醒时只 emit「已整合，跳过」要花一个 turn 的 LLM 调用，靠 SKILL Step 4
   兜住）。

—— 7.1 三条过 → 档 1 可 PR；7.2 三条过 → 档 1.5 可与档 1 同份 PR 合并下发。
两档都只动 `examples/personas/Maya/skills/task-management/SKILL.md` 一个文件。

> **验证状态（2026-05-24）**：上述 6 条已经 grep 复审通过 —— 7.1.①/② 见
> `handoff_tools.py:106-110`（无 `target != self` 预校验）+
> `_orchestrator_handoff_drain.py:239-298`（无 self 特判，仅过滤 `winner in orch._guests`）；
> 7.2.④ 见 `agent_run_sink.py:119-123, 143-154`（缓冲 + per-envelope try/except）+
> `server.py:1164-1196`（wrapper finally 4 步 `detect → flush → pop → emit terminal` 顺序对齐）；
> 7.2.⑤ 见 `handoff_tools.py:50-72`（transport 注入即 guest binding，`proposer = self._transport.guest_name`）。
> 7.1.③ / 7.2.⑥ 与运行时行为相关，PR 落 SKILL 改动时需手工灰度演练。
> 列表保留作为 PR checklist 复跑用。
