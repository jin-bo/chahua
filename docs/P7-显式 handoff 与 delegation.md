# P7：显式 handoff / delegation

> 目标：在意愿打分（适合"想接话"的自然讨论）之上，**新增一层确定性发言指派通道**，
> 让"完成复杂任务"这类需要明确分工的场景不再赌 LLM 的"想不想接话"。
> 用户（以及茶客 propose + 用户采纳）能直接说"下一句你（A）说" / "请 B 审一下 A 刚才那段" /
> "C/D/E 都给一条独立意见再让 F 汇总"。

**状态：设计中（2026-05-19）**。本文为方案草案；落地拆分见 §7。

---

## 1. 现状速写 & 关键观察

| 现有 | 与"显式 handoff"的关系 |
|---|---|
| `Orchestrator._pick_next_speaker` 意愿打分 → 前 1~2 名发言 | handoff = **跳过打分**的确定性路径，与之并列而非取代 |
| `@提及` 走确定性路由不进打分（CLAUDE.md 不变量） | 已经是 handoff 的"轻量版"：用户消息首轮（`_consecutive_ai_turns == 0`）确定性给 mention 单点 winner，跳过 scoring；A 完成后**回到 scoring**，下一轮其他茶客可能接话。P7 把这层放大成"队列 + 不回落 scoring"（反向评审 v3-#2） |
| `Task.owner: Optional[str]` 字段已存在但**闲置** | **P7.5（可选）** 阶段才填充：delegate 时可选同步设 owner + scoring owner bonus；**P7.1 不动** |
| `task_propose_decision` / `task_propose_open` + `TASK_PROPOSAL` envelope | propose-then-adopt 模式现成；新增 `task_propose_handoff` 同口径复用前端卡片基础设施 |
| `_cancel_and_drain_inflight`（add/remove guest / set_active_task 同口径） | `handoff_clear` 始终走；`handoff_delegate` / `_panel` **仅在** in-flight 是 user-turn 时走（§4.4 / 反向评审 v3-#1） |
| `[room]` / `[scoring]` / `[summary]` toml 段 | P7 范围内**不新增 toml 段**（panel 用模块常量 `MAX_PANEL_TARGETS=4`）；未来若用户真有诉求再走 P4 four-touch checklist |
| debug recorder `turns.jsonl` | 队列状态进每 turn metadata，回放时能解释"为什么是 X 发言不是 Y" |

**最重要的判断**：handoff 是**调度层的增量**，不是新的对话原语 —— 仍走同一根 `transcript.jsonl`，
仍由 `Orchestrator._run_turn` 主导，茶客视角不变（看到的还是"一根线"的群聊上下文 + onboarding/incremental
context）。**它只是 `_pick_next_speaker` 入口前多了一层"确定性队列"**。这条决定了它能复用 90% 的现有
承重墙，也是 P7 风险可控的根因。

---

## 2. 三种动作的形态

### 2.1 delegate（交给）—— "下一句你（A）说"

```
用户触发：UI 茶客侧栏 "交给…" / 聊天框 /delegate @A <可选内部备注>
AI propose：茶客调 task_propose_handoff(kind="delegate", target="A", reason=...)
            → emit TASK_PROPOSAL → 前端卡片 → 用户点采纳 → 进队
```

- **效果**：handler 行为依据当前 in-flight 类型而定（反向评审 v3-#1 保护队列语义）：
  - in-flight 是 user-turn → `_cancel_and_drain_inflight()` 抢占（与 user_message 互相抢
    占同一槽位的语义不冲突，因为 user_message 一旦进入 in-flight 就 drop 后续 user_message，
    但 delegate 是显式抢占指令）→ 入队 + 启动 drain。
  - in-flight 是 handoff drain → **只入队尾、不取消** —— drain 内 while 循环自然在
    当前项结束后看到新项。这条是让"队列"在 P7.1 真有意义的关键（否则连点 N 次 delegate
    永远只剩最后一个被执行）。
  - 无 in-flight → 入队 + 启动 drain。
  下一轮**强制** A 发言（无视打分）。本轮独占（A 单点 winner）。**注意 user-turn 路径是
  cancel + drain，不是自然 drain**（反向评审 v2-#5）—— 老 turn 不会跑完就被打断。
- **`reason` / 备注的边界**（反向评审 #2）：`<可选内部备注>` 与 `HandoffItem.reason` 字段
  **不进茶客 prompt** —— 仅进 debug record metadata（回放时解释"为什么当时指派 A"）
  + 队列预览小条（"➡️ 下一句：A（理由：xxx）"）。UI 文案明确写"内部备注（用于自己回看 /
  调试，不发给茶客）"，避免用户误把它当成"给 A 的指令"。茶客视角看到的仍是群聊
  context + onboarding/incremental，不见 reason。**P7.4 茶客 propose 时 reason 进卡片**
  让用户判断要不要采纳，仍不进 target 茶客的 prompt。
- **与 task.owner 联动（P7.5 才做）**：P7.1 阶段 delegate **不碰** `task.owner`。
  未来 P7.5 可选阶段提供"同时设为负责人"勾选 + scoring owner bonus（建议 +0.15），形成
  "任务负责人"的连续语义。详见 §7 阶段化路线。
- **本质**（反向评审 v3-#2 校准）：与 `@提及` **都是单点 winner**（本轮不并列其他茶客），
  区别只在 ① 是否带 user transcript（@ 带、delegate 不带）+ ② A 完成后是否回落 scoring
  （@ 回落允许群聊继续，delegate **不**回落等用户）。**不是**"@ 软引导 / delegate 硬指派"
  这种程度差异。详见 §4.3.1。

### 2.2 review（请审）—— "B 审一下 A 刚才那段"

```
用户触发：UI 上点某条茶客发言的"请审…"按钮 → 选目标茶客（自带 message_id）
AI propose：茶客调 task_propose_handoff(kind="review", target="B", message_id=...)
```

- **效果**：与 delegate 共用确定性 pick 通道，**差异只在 prompt 注入**：
  - 通过 `ContextRenderer.build_context_for(..., extra_blocks=[review_block])` 传入一段
    `<review_target>` extra block，在 onboarding / incremental 两条路径里都**统一放到
    `<speak_instruction>` 之前**（见 §4.6）。
  - 块内容：
    - 被审消息 body（用 `<message>` 包装，与 `format_messages` 同源）
    - 若被审消息有 `task_id` → 附"当前任务 artifacts 列表"（即 `list_artifacts(task_id)`），
      **不**承诺"这条消息绑定的 artifacts"——transcript Message 没有 artifact 绑定字段
      （`debug_recorder` 的 `artifact_paths` 是取证数据，不是业务真相）。
    - "请给出建议 / 通过 / 打回，并说明理由"指引
- **scope 只有一档：`message`**（P7.2 收窄）：
  - 用户路径强制从"消息气泡 → 请审…"进入，自带 `message_id`；不开 `last` / `task` /
    slash 的 `@msg_id` 三套 scope。
  - `last` 容易和用户预期错位（"刚那条"到底指哪条？跨 turn 怎么算？）—— 测试面与
    误触发风险都不值。`task` 维度的 review 留到后续阶段（先用消息粒度走通）。

### 2.3 panel（汇总）—— "C/D/E 各说一条，F 汇总"

```
用户触发：UI "圆桌" 模式 → 多选茶客 + 可选汇总者
        / 聊天框 /panel @C @D @E --summarizer=@F
AI propose：暂不开放（见 §4.1）—— 茶客自己拉圆桌的成本/收益不对称
```

- **效果**：所有指定茶客在**同一逻辑轮次**内**串行**各发一次（绕开"单 turn 1~2 个 speak"上限），
  完成后**自动**让 summarizer 茶客发一条 consolidate；无 summarizer 则等用户。
- **数据结构口径**：panel 是**一个** HandoffItem（持 `targets: tuple[str, ...]`，`to_dict()` 转 list 给 envelope；见 §3.1），不拆 N 项 ——
  对应 orchestrator 的一个 turn / 一个 `turn_start` / `winners=targets`，与 P6 现有 turn 语义对齐。
  拆 N 项 → 变成 N 个 pick 周期 / N 个 turn / debug 抽屉显示 N 条独立 turn，"圆桌讨论"
  语义就崩了。
- **summarizer 入队由 inbound handler 一次性做**（评审反馈 v4-#2）：接收 `handoff_panel`
  inbound 时，handler **原子性入队两项**：①panel item + ②summarizer delegate item，
  两项共享 `panel_group_id`。**不允许** orchestrator 在 panel 完成后再"生成"
  summarizer item ——`HandoffItem` 不保存 summarizer 字段，orchestrator 跑完 panel
  后不知道给谁入队；让 orchestrator 持 `summarizer` 状态会污染队列的"自描述"语义，
  与既有"queue 全部信息在 items 里"口径冲突。
- **关键决策：串行执行 + UI 标注"并行讨论中"**，不真并行 emit —— 见 §4.2。
- **token 成本上限**：P7.3 起步阶段**硬编码** `MAX_PANEL_TARGETS = 4`（写在常量，不进 toml）；
  当用户开始 hit 上限时，再加 `[panel]` toml 段（见 §7 阶段化）。
- **panel + summarizer 与 `max_consecutive_ai_turns` 的关系**（评审反馈 #1）：
  panel 一项 N 个 targets 跑一个 turn，summarizer 是**下一个** delegate 项跑**另一个** turn ——
  也就是说 panel+summarizer 一共消耗 `N + 1` 轮 `_consecutive_ai_turns` 配额。
  默认 `max_consecutive_ai_turns = 4` 时，4 人 panel 把预算用完，summarizer 会被推迟（等下一次
  用户触发） / 实质不跑，与"自动汇总"承诺冲突。
  **修法（不加新配置）**：handoff_panel inbound 校验时按
  `effective_max = MAX_PANEL_TARGETS if summarizer is None else max_consecutive_ai_turns - 1`，
  最终 cap = `min(MAX_PANEL_TARGETS, effective_max)`；超出 → NOTICE error + 丢帧（提示用户
  减少 targets 或去掉 summarizer）。`max_consecutive_ai_turns ≤ 1` 时禁用 panel+summarizer 组合
  （此时根本跑不下"N 人 + 汇总"）。

---

## 3. 数据契约

### 3.1 内存：确定性发言队列

```python
# 按阶段加 kind 值（反向评审 v2-#4）—— P7.1 / P7.2 / P7.3 各阶段进 enum 时再加：
#   class HandoffKind(StrEnum):
#       DELEGATE = "delegate"                     # P7.1
#       # REVIEW = "review"                       # P7.2 阶段加（review 一档）
#       # PANEL  = "panel"                        # P7.3 阶段加（panel + summarizer）
# Literal 形式（如下）的同步更新口径相同：P7.1 仅 "delegate"，P7.2/P7.3 阶段扩 Literal。
#
# 下面这个**最终形态**（review / panel 都进 enum 后）作为契约参考，**P7.1 实现按阶段收**：
@dataclass(frozen=True)
class HandoffItem:
    kind: Literal["delegate", "review", "panel"]   # P7.1 实际只接 "delegate"
    # delegate / review: 单一目标
    target: Optional[str] = None
    # panel: 多目标；一项对应一个 turn / 一次 pick 返回 winners=list(targets)。
    # **用 tuple 不用 list**（评审反馈 v7-#4）：dataclass(frozen=True) 只锁字段重绑
    # 不锁内部容器；如果用 list 即使 frozen 也能被外部 caller 改掉队列里的 targets。
    # to_dict() 时再转 list 给 envelope。
    targets: Optional[tuple[str, ...]] = None
    issued_by: str = "user"        # "user" 或茶客名（propose 采纳后实际仍是 user）
    reason: Optional[str] = None   # 触发说明，进 debug record metadata
    review_message_id: Optional[str] = None   # review 专用（P7.2 只支持 message scope）
    created_at_ms: int = 0
    # panel 完成后自动入队的 summarizer delegate 是**另一个** HandoffItem，
    # 不作为本项字段；保留下面这个仅用于前端"队列预览"分组显示：
    panel_group_id: Optional[str] = None      # panel 与其 summarizer 共享一个 group_id

    def to_dict(self) -> dict:
        """送 envelope 用；targets 元组转 list 让前端 / json 兼容。"""
        d = dataclasses.asdict(self)
        if self.targets is not None:
            d["targets"] = list(self.targets)
        return d

class Orchestrator:
    _handoff_queue: deque[HandoffItem]   # FIFO；新 handoff 入队尾；pick 时取队首
```

- **队列状态不落盘**：与 in-flight `_current` 同口径，crash/重启即丢；用户重新指派即可。
  落盘的复杂度（崩溃恢复 / 与 transcript 顺序一致性）远大于"重启后用户重新点一次"的代价。
- **debug 落盘：复用 P6 现有 `scoring_path` 字段**，**不新增** `pick_source`。
  `debug_recorder.py` 的 `VALID_SCORING_PATHS` 白名单**按阶段加**（反向评审 v2-#4）：
  - **P7.1**：`{scoring, mention, broadcast, handoff_delegate}` —— 只加 delegate 一档。
  - **P7.2**：再加 `handoff_review`。
  - **P7.3**：再加 `handoff_panel`。
  `turn_start` envelope 的 `scoring_path` 字段同步扩 enum。前端调试抽屉 / `turns_index` 现有
  渲染逻辑零改动。同一概念两套字段（旧 `scoring_path` + 新 `pick_source`）的对齐成本不值。
- **`ScoreResult.kind` 复用 `ScoreKind.MENTION`，不新增 enum 值**（评审反馈 #5）。
  - 现有 `ScoreKind` 只有 `scored / mention / cooldown / error`；`_run_ai_chain` 用
    `kind == MENTION` 判是否绕过 inner cap（参考 `chahua/orchestrator.py:344`）—— handoff
    与 mention 在"确定性、跳过打分、绕过 cap"语义上同路。
  - **handoff 为每个 winner 生成 ScoreResult**（评审反馈 v5-#2 更新）：
    `ScoreResult(guest_name=name, score=1.0, kind=ScoreKind.MENTION,
    raw="handoff_delegate")`（P7.1）/ `"handoff_review"`（P7.2 加）/ `"handoff_panel"`
    （P7.3 加）—— 反向评审 v2-#4 同口径**按阶段加** `raw` 值；真正分类走 `scoring_path`
    字段（已扩 enum）。**不**填 `results=[]` —— 否则 `turn_start.data.scores` 与调试抽屉
    缺"谁被指派"的现成结构，前端要为 handoff path 走特殊渲染分支。
  - **好处**：JS 侧 `ScoreKind` enum / 调试抽屉的 kind 渲染逻辑零改动，inner cap 绕过逻辑
    自然成立；`turn_start.data.scores` 直接展示 winners 名单。**代价**：score 字段对 handoff
    项是冗余 1.0，可接受 —— score 维度是为打分而存在的，handoff 本就不打分，1.0 在
    UI 上仅作占位。

### 3.2 inbound 协议（user → server）

```jsonc
// 用户直接触发（不走 propose）
{ "type": "handoff_delegate", "target": "A", "reason": "..." }
// 注：reason 是**内部备注**，仅进 debug record + 队列预览，**不进茶客 prompt**（§2.1 / 反向评审 #2）
// 注：P7.5 之后才加 set_owner 字段；P7.1 inbound 不接收
{ "type": "handoff_review",   "target": "B", "message_id": "msg_..." }   // P7.2：只接 message_id
{ "type": "handoff_panel",    "targets": ["C","D","E"], "summarizer": "F" /* 可选 */ }
{ "type": "handoff_clear" }   // 取消当前 in-flight（若有）+ 清队列剩余项（§4.4）
```

- **入站严格**（同 `room.toml` / `open_task` 口径）：未知字段 → NOTICE error + 丢帧。
- **`handoff_review` 只接 `message_id`**（P7.2 收窄，见 §4.5）；不解析 `scope` 字段。
  未来若开 `scope=task` 等其它档，再单开 inbound `type`（与现有"未来 scope 不入当前协议"
  口径一致，避免协议字段冗余）。
- **`handoff_clear` 语义**（反向评审 #4 + 反向评审 v3-#3 简化）：handler 无差别走
  `_cancel_and_drain_inflight()` + 清队列：
  - **始终**：取消当前 in-flight（若有，无论 user-turn 还是 handoff drain，与 cancel /
    add_guest / set_active_task 同口径）→ 清空队列剩余项 → emit
    `handoff_cleared{items_dropped: [...]}`。
  - **为什么不区分 in-flight 类型**：反向评审 v2-#3 引入的"`_inflight_kind == handoff` 才
    cancel"asymmetric 是为了保护一个**实际不存在的场景**（"用户发普通消息打断 handoff
    drain 后留下残队列"）—— 现状 `chahua/server.py:646` 在 `_inflight_alive()` 时直接
    drop user_message（单 in-flight 严格策略），普通消息根本不会切换 turn 类型，所以
    asymmetric 是为不存在的场景买保险，反而增加复杂度。简化版承认"用户点全部取消是
    nuclear 操作 → cancel 一切"。
  - **`_inflight_kind` 字段不用于 clear**，仅 `_inbound_handoff_delegate` /
    `_inbound_handoff_panel` 用来判"in-flight 是 handoff 时只 append、不抢占"（反向评审
    v3-#1）。
  - UI 按钮文案对应用"**全部取消**" + tooltip"取消当前发言 + 清空后续队列"——不要写
    "清空队列" / "清后续"（让用户以为 A 会说完再清）。
  - **不做"只清后续，当前 handoff 继续跑完"的 partial cancel**——与 `_cancel_and_drain_inflight`
    承重墙冲突 + UX 上需要用户在队列 X 按钮和 composer cancel 按钮之间切换才能"全停"。

### 3.3 envelope 增量（server → user）

| 事件 type | 何时 emit | data 关键字段 |
|---|---|---|
| `handoff_enqueued` | 队列入项后 | `queue: [i.to_dict() for i in snapshot]` —— **权威快照**（dataclass 序列化后的 dict 列表）；前端 `handoffQueueState` **整体替换**而非按单项增量拼。可选 `item: i.to_dict()` 标注本次新入项供 UI 高亮，但状态不能靠它推（panel inbound 一次入两项时 `item` 语义不清 —— 建议默认只发 `queue`，需要高亮另开字段） |
| `handoff_consumed` | `turn_start` 后、首个 speaker 执行前 | `item: i.to_dict()`（dataclass → dict）。**`turn_id` 走 envelope 顶层**（评审反馈 v8-#2：与 `turn_start` / `turn_end` 同口径，不在 data 里重复塞一份），关联取证靠顶层字段 |
| `handoff_cleared` | `handoff_clear` inbound handler 末尾（无差别 `_cancel_and_drain_inflight` 之后，反向评审 v3-#3 简化） | `items_dropped: [i.to_dict() for i in dropped]` —— 与 `handoff_enqueued.queue` 同口径，dataclass 必须 `to_dict()` 后入 envelope。**只列被丢的队列项**；若 in-flight 被 cancel 则 cancel 路径自己 emit `turn_end status=cancelled` + `message_end status=cancelled`，不重复 emit 进 `items_dropped` |
| `TASK_PROPOSAL`（已有） | 茶客 propose handoff 时 | `kind: "handoff"` + `payload: {kind:"delegate"|..., target...}` |

**TASK_INFO 是权威快照**口径在 P7 不变：handoff 队列是**调度层瞬态**，不进 task_info；
前端拿 `handoff_enqueued` / `handoff_consumed` 维护本地 "队列预览" UI 即可，刷新即清。

### 3.4 执行驱动：`enqueue_handoff` + `run_pending_handoff`（**P7 承重墙**）

**问题**：现有 turn 执行入口只有一条 —— `submit_user_message()` → append user transcript →
`_run_ai_chain()`。handoff **不追加** user transcript（指派不是用户在群里发了一条消息），
所以仅 `enqueue` 不会自然跑起来；必须显式有一条"跑队列"的驱动路径。

**职责拆分（评审反馈 #4）**：

| 职责 | 谁负责 | 备注 |
|---|---|---|
| 内存队列变更（append / popleft / clear） | `Orchestrator.enqueue_handoff` / `clear_handoff_queue` / drain loop —— 纯方法，无 sink | 返回队列快照供 server emit |
| `handoff_enqueued` envelope | **server inbound handler** | server 持 sink，handler 拿到 enqueue 返回的 snapshot 后 emit |
| `handoff_consumed` envelope | **orchestrator**（在 `turn_start` **之后** emit，带刚 mint 的 `turn_id`） | turn_id 在 orchestrator 内 mint；pop 时 turn_id 还不存在（评审反馈 #1） |
| `handoff_cleared` envelope | **server inbound handler** | `handoff_clear` inbound 同口径 |

```python
# chahua/orchestrator.py
def enqueue_handoff(self, item: HandoffItem) -> list[HandoffItem]:
    """入队；不启动执行；不 emit envelope。
    返回当前队列快照（list[HandoffItem]），供 server emit handoff_enqueued。"""
    self._handoff_queue.append(item)
    return list(self._handoff_queue)

async def run_pending_handoff(self, sink, *, task_id: Optional[str]) -> None:
    """专职消费 handoff 队列。无队列直接返回；队列空 / cap 撞顶 → 收尾返回，
    **不回落到普通 scoring**（与 _run_ai_chain 严格分流，见下方"严格分流"段）。
    与 _run_turn 同根但不追加 user transcript。"""
    if not self._handoff_queue:
        return

    # **入口清零计数一次**（评审反馈 #2 + #3 合并修法）：
    # handoff 是用户显式触发但不走 submit_user_message → 不会自动清
    # _consecutive_ai_turns / _rounds_without_user_or_mention。上一轮 AI 已到上限时
    # while 直接挡掉 → handoff 永不执行。这里按"用户显式触发"语义清零一次，
    # 与 submit_user_message 同口径。
    self._consecutive_ai_turns = 0
    self._rounds_without_user_or_mention = 0

    # **drain loop 在本函数内闭环**，不递归调 run_pending_handoff —— 否则需要
    # reset_budget 参数判"第一次 vs drain 续跑"，工程上太容易把预算重置掉，
    # panel + summarizer cap 失效（评审反馈 #2）。
    # **不再维护 `last_open_turn_id`**（反向评审 #3 修订）：每个 turn 末尾**一次性决定** next
    # ("ai" 或 "user")，不发 speculative `next="ai"` 再补帧 `next="user"`——少一帧 envelope
    # + UI"停止"按钮不闪。
    while self._handoff_queue:
        # P7.1 阶段**只支持 kind == "delegate"**，cost / winners / scoring_path 都按
        # delegate 形态写死（反向评审 v3-#4）；panel / review 的分支留到 P7.2 / P7.3
        # 各自小节再展开，不在 P7.1 伪代码里提前铺。
        item = self._handoff_queue[0]
        cost = 1   # P7.3 加 panel 时改为 `len(item.targets) if item.kind == "panel" else 1`
        if self._consecutive_ai_turns + cost > self.config.max_consecutive_ai_turns:
            # 队首留着不 pop，下次用户触发 + run_pending_handoff 入口清零后再跑。
            # 与"入口清零一次"语义一致：这一轮的预算不够，下一轮重新分配。
            break

        item = self._handoff_queue.popleft()
        winners = [item.target]   # P7.3 加 panel 时改为
                                  # `[item.target] if item.kind != "panel" else list(item.targets)`

        # **执行顺序与 _run_ai_chain 对齐**（评审反馈 v3-#1）：
        # `TurnRecorder.start_turn` / `record_scoring` 只写 in-flight 落盘，
        # **不 emit envelope**；真正的 turn_start envelope 由 `_emit_turn(TURN_START)` 发。
        turn_id = new_turn_id()
        # trigger 整把 `item` 字典塞进去（评审反馈 v6-#2）：reason / target / issued_by /
        # panel_group_id 等随现有 trigger dict 落盘 debug；turns_index 仍只投影 trigger.kind，
        # 不扩 recorder API。
        self._recorder.start_turn(
            turn_id=turn_id, task_id=task_id,
            trigger={"kind": "handoff", "handoff_item": item.to_dict()},
        )
        # 为每个 winner 生成 ScoreResult（评审反馈 v5-#2）：复用 ScoreKind.MENTION，
        # raw 区分 handoff_delegate 等；让调试抽屉 / turn_start.data.scores 有现成
        # "谁被指派"的结构（results=[] 会让前端缺这块）。scoring_path 仍是权威分类。
        scoring_path = "handoff_delegate"   # P7.2 / P7.3 改成 f"handoff_{item.kind}"
        score_results = [
            ScoreResult(guest_name=name, score=1.0,
                        kind=ScoreKind.MENTION, raw=scoring_path)
            for name in winners
        ]
        # record_scoring 现有签名 results=list[tuple[ScoreResult, Optional[str]]]
        # （prompt 字符串，handoff 不打分所以传 None）—— 评审反馈 v6-#1。
        # threshold=None：与 mention / broadcast 确定性路径同口径（见
        # orchestrator.py:435-450），表示"不走阈值打分"；threshold=0.0 会在
        # debug 上误导成"有阈值，只是阈值为 0"。—— 评审反馈 v7-#1。
        self._recorder.record_scoring(
            threshold=None, scorables=[], cooled=[],
            results=[(r, None) for r in score_results],
            winners=winners, scoring_path=scoring_path,
        )
        # _emit_turn 现有签名只接 data: dict —— 评审反馈 v6-#2。
        # winners 已在 scores 内表达；不扩 _emit_turn 签名。
        self._emit_turn(
            sink, turn_id=turn_id, type=ChahuaEventType.TURN_START,
            data={
                "scores": [_score_to_dict(r) for r in score_results],
                "scoring_path": scoring_path,
            },
        )

        # turn_start envelope 已出，turn_id 已确定 —— emit handoff_consumed 的
        # 最早合法时机（评审反馈 v2-#1）。
        # **EnvelopeSink 是 callable**（评审反馈 v7-#1）：直接 sink(ChahuaEnvelope(...))，
        # 无 envelope() helper、无 .emit() 方法。orchestrator 内已经走这条口径
        # （见 _emit_turn）；handoff_consumed 走同一根 sink，turn_id 字段非 None。
        sink(ChahuaEnvelope(
            room_id=self.room.name, turn_id=turn_id,
            guest_name=None, message_id=None,
            type=ChahuaEventType.HANDOFF_CONSUMED,
            data={"item": item.to_dict()},
        ))

        # 串行 speak winners + cancel fixup（评审反馈 v4-#4）：
        # **复用既有 `_let_speak` helper**（评审反馈 v6-#4 / v7-#2）—— P7.1 不抽
        # `_speak_one`；P7.2 真需要 `extra_blocks` 再给 `_let_speak` 加形参。
        # **cancel fixup 在本函数内补**，wrapper 层只 swallow，不再做补偿。
        try:
            for name in winners:
                await self._let_speak(name, turn_id=turn_id, sink=sink,
                                      task_id=task_id)
                self._consecutive_ai_turns += 1
        except asyncio.CancelledError:
            # speak 阶段被 cancel：turn_start 已 emit、message_* 可能在流；
            # 必须补 turn_end(cancelled) 让 UI 收回"停止"按钮，再让 recorder 落
            # 取证行（避免 in-flight turn 漂着），最后 re-raise 给 wrapper swallow。
            # 状态走 messages[].status 表达（recorder 已记，不改 flush_turn API）。
            self._emit_cancel_fixup(sink, turn_id=turn_id)
            self._recorder.flush_turn()        # 评审反馈 v5-#1：无参，现有 API
            raise

        # 正常完成本 turn —— **顺序严格对齐 `_run_ai_chain:376-399`**
        # （反向评审 v2-#1 修正）：
        #   ① peek 下一项算 next_state → ② `_emit_turn(TURN_END, next=...)` →
        #   ③ `flush_turn()` → ④ `_kick_summarize()` / `_tick_cooldown()` /
        #   `_kick_detect_new_artifacts(sink, task_id)`。
        # **禁止**把三个 hook 放到 `turn_end` 之前——`_kick_detect_new_artifacts`
        # 会 emit `task_artifact_added` + `task_info`，若早于 `turn_end` 到前端，
        # UI 收到"任务面板更新"时旧 turn 还没结束，状态错位（与 `_run_ai_chain`
        # 不一致还会让前端针对两种 turn 写两份顺序处理逻辑）。
        # 一次性决定 next（反向评审 #3 留存）：peek 下一项 + 算 cost + 比 cap，
        # 一次 emit `turn_end`，不再发 speculative `next="ai"` 再补 `next="user"` 帧。
        if self._handoff_queue:
            next_cost = 1   # P7.3 加 panel 时改为
                            # `len(next_item.targets) if next_item.kind == "panel" else 1`
            has_next = (self._consecutive_ai_turns + next_cost
                        <= self.config.max_consecutive_ai_turns)
        else:
            has_next = False
        next_state = "ai" if has_next else "user"
        self._emit_turn(
            sink, turn_id=turn_id, type=ChahuaEventType.TURN_END,
            data={"next": next_state},
        )
        self._recorder.flush_turn()            # 评审反馈 v5-#1：无参
        # 三个 pick-周期 hook 与 _run_ai_chain:396-399 同口径
        # （反向评审 #1）：缺 `_kick_detect_new_artifacts` → handoff turn 写
        # `./task/<x>` 后 task_info 不刷新、P5.4 通道 3 自动归集失效；
        # 缺 `_kick_summarize` / `_tick_cooldown` → 摘要不增量 / 冷却不递减。
        self._kick_summarize()
        self._tick_cooldown()
        self._kick_detect_new_artifacts(sink, task_id)

        if not has_next:
            # 队列空或下一项 cap 撞顶 —— 队首留到下次用户触发；本 drain 收尾返回。
            # **不回落到 scoring**：这是 §4.3 "下一轮恢复打分"的精确语义——
            # "下一轮"指**下次用户触发**，不是同一 AI 链内立刻接着打分。
            return
```

```python
# chahua/server.py（_INBOUND_ROUTES 注册）

# server 持 _inflight_kind 与 _inflight_turn_task 同生命周期（反向评审 v2-#3 引入，
# 反向评审 v3-#1 收紧用途，反向评审 v4-#1 补 synthesized 入口）：
#   self._inflight_turn_task: Optional[asyncio.Task] = None
#   self._inflight_kind: Optional[Literal["user", "handoff"]] = None
# - **所有 `_run_turn(...)` 入口都标 "user"**（反向评审 v4-#1）：
#   - `_inbound_user_message`（chahua/server.py:632）→ 起 task 前 `self._inflight_kind = "user"`
#   - `_kick_synthesized_user_message`（chahua/server.py:444，task handler / artifact 检测
#     合成"用户消息"）→ 起 task 前 `self._inflight_kind = "user"`
#   - **凡是新增创建 `_run_turn` task 的入口都必须同步设置**，漏一个就会让 delegate 在该
#     合成 turn 跑期间把 `_inflight_kind` 误判成 None → 只 append 不启动 handoff drain →
#     队列挂到下次用户触发才跑。
# - `_inbound_handoff_*` (delegate/panel) → 起 task 前 `self._inflight_kind = "handoff"`
# - `_run_turn` / `_run_handoff_turn` wrapper 的 finally **同槽清两个**：
#       self._inflight_turn_task = None; self._inflight_kind = None
#   （`_run_turn` 现状 chahua/server.py:495 只清第一个，P7.1.6 需要补第二行）
# - **`_inflight_kind` 只用于 `_inbound_handoff_delegate` / `_inbound_handoff_panel`** 判
#   "in-flight 是否已是 handoff drain"（决定 cancel 还是 append）。`_inbound_handoff_clear`
#   走无差别 cancel（反向评审 v3-#3 简化，见 §3.3）。
# - `_cancel_and_drain_inflight` 自身保持"无差别 cancel"语义不变。

# 所有 handoff_* inbound handler 模板：
async def _inbound_handoff_delegate(self, payload, sink):
    # 1. 入参严格校验（require_str / check_keys_whitelist）+ target ∈ active_guests
    # 2. **条件性** cancel（反向评审 v3-#1 保护队列语义）：
    #    - in-flight 是 user-turn → 抢占（与 user_message 同口径，老路径）
    #    - in-flight 是 handoff drain → 不动，让 drain 自然消费新 item
    #    - 无 in-flight → 不需要 cancel
    if self._inflight_kind == "user":
        await self._cancel_and_drain_inflight()
    # 3. 入队（始终）—— 即使 in-flight 是 handoff drain，新项 append 到队尾
    item = HandoffItem(kind="delegate", target=target, reason=reason)
    snapshot = self._session.orchestrator.enqueue_handoff(item)
    # 4. emit enqueued（snapshot 是 list[HandoffItem] dataclass，emit 前必须 to_dict()）
    self._emit_handoff_envelope(
        sink, type=ChahuaEventType.HANDOFF_ENQUEUED,
        data={"queue": [i.to_dict() for i in snapshot]},
    )
    # 5. **条件性** 启动 wrapper：
    #    - 已有 handoff drain 在跑（_inflight_turn_task is not None）→ 不启动新 task，
    #      drain loop while 内自然在当前项结束后看到新项。
    #    - 无 in-flight（None，含上一步刚 cancel 完 user-turn 后的状态）→ 启动 wrapper。
    #    判 `is None` 而不是 `done()`：wrapper finally 总是先清 slot 再让 task 完成，
    #    所以 `done() == True` ↔ slot 已 None；判 None 即覆盖所有 fresh-start 情况。
    if self._inflight_turn_task is None:
        self._inflight_kind = "handoff"
        self._inflight_turn_task = asyncio.create_task(
            self._run_handoff_turn(sink, task_id=active))

async def _inbound_handoff_clear(self, payload, sink):
    # 1. 入参严格校验（payload 只允许 {"type": "handoff_clear"}，未知字段 NOTICE error）
    # 2. 无差别 cancel（反向评审 v3-#3 简化）—— "全部取消"是 nuclear 操作，
    #    与 cancel / add_guest / set_active_task 同口径，不区分 in-flight 类型。
    await self._cancel_and_drain_inflight()
    # 3. 清队列剩余（始终）
    dropped = self._session.orchestrator.clear_handoff_queue()
    # 4. emit cleared
    self._emit_handoff_envelope(
        sink, type=ChahuaEventType.HANDOFF_CLEARED,
        data={"items_dropped": [i.to_dict() for i in dropped]},
    )
    # clear 不挂 wrapper（队列已空，没有要 drain 的）

# server 侧 envelope 小 helper（评审反馈 v7-#1 / v8-#1）：method 直接挂在 ChahuaServer 上
# （inbound handler 是 ChahuaServer 自身的方法，无需走 self.server.* 反向引用 ——
# 那是 TaskHandlers 等 handler 子模块才用的模式）。
def _emit_handoff_envelope(
    self, sink: EnvelopeSink, *, type: ChahuaEventType, data: dict,
) -> None:
    """连接级 handoff envelope（turn_id / message_id / guest_name 全 None）。
    handoff_enqueued / handoff_cleared 共用 —— 避免每个 callsite 写三个 None。
    handoff_consumed 由 orchestrator 自己发（envelope 顶层 turn_id），不走这个 helper。"""
    sink(ChahuaEnvelope(
        room_id=self._session.room.name,
        turn_id=None, guest_name=None, message_id=None,
        type=type, data=data,
    ))

async def _inbound_handoff_panel(self, payload, sink):
    # 同上 1/2/4/5 步骤；**第 3 步特殊**（评审反馈 v4-#2）：
    # **原子性入队两项**：panel item + 可选 summarizer delegate item，共享 panel_group_id。
    # 不让 orchestrator 在 panel 跑完后再"生成"summarizer——HandoffItem 不存 summarizer
    # 字段，跑完 panel 后 orchestrator 无从得知。
    group_id = new_panel_group_id()
    # payload 里 targets 是 list[str]，HandoffItem.targets 形为 tuple[str, ...]
    # （§3.1 frozen dataclass 不可挂可变引用）—— inbound 这层做转换，不需要 helper
    panel_item = HandoffItem(kind="panel", targets=tuple(targets), panel_group_id=group_id)
    # 队列变更只走 Orchestrator 公有方法 —— 严禁 inbound 直接读
    # self._session.orchestrator._handoff_queue（私有字段、口径会被边路绕过）。
    # snapshot 取最后一次 enqueue_handoff 的返回值即为入队后全队。
    snapshot = self._session.orchestrator.enqueue_handoff(panel_item)
    if summarizer:  # 可选
        summ_item = HandoffItem(kind="delegate", target=summarizer, panel_group_id=group_id)
        snapshot = self._session.orchestrator.enqueue_handoff(summ_item)
    self._emit_handoff_envelope(sink, type=ChahuaEventType.HANDOFF_ENQUEUED,
                                data={"queue": [i.to_dict() for i in snapshot], ...})
    # ... 启动 wrapper（第 5 步）

# **必须**走 wrapper（评审反馈 v3-#3），不直接 create_task(self._session.orchestrator.run_pending_handoff(...))：
# 否则 CancelledError / 异常不被 swallow → "Task exception was never retrieved" WARN；
# 且 _inflight_turn_task 不会被 finally 清，与既有 user-turn 路径口径不一致。
async def _run_handoff_turn(
    self, sink: EnvelopeSink, *, task_id: Optional[str]
) -> None:
    """承载一次 handoff drain。结构照搬 _run_turn() —— cancel-safe + finally 清
    _inflight_turn_task / _inflight_kind，让 cancel / busy 判定与 user-turn 同口径。"""
    try:
        await self._session.orchestrator.run_pending_handoff(sink, task_id=task_id)
    except asyncio.CancelledError:
        # **swallow**，与 _run_turn 同口径（评审反馈 v4-#1）：让 task 正常完成，
        # 避免以 cancelled 状态冒出去触发 "Task exception was never retrieved"。
        # cancel fixup（turn_end(cancelled) + flush_turn）由 run_pending_handoff
        # 内部 try/except 负责（见上方伪代码 speak 段落）。
        _log.info("handoff drain cancelled by user")
    except Exception:  # pragma: no cover —— 防 task 失踪
        _log.exception("handoff drain failed")
    finally:
        # 反向评审 v2-#3：`_inflight_kind` 与 `_inflight_turn_task` 同生命周期 ——
        # 同一个 finally 清两个槽位，让后续 inbound 判 in-flight 类型时拿到一致状态。
        # `_run_turn`（user-turn wrapper）的 finally 同口径清两个槽。
        self._inflight_turn_task = None
        self._inflight_kind = None
```

**严格分流（评审反馈 #3 修法）**：

`run_pending_handoff` 与 `_run_ai_chain` 是**两条并列的执行路径**，**不互相回落**：
- `submit_user_message` → `_run_ai_chain`（走 scoring，可连续多轮 AI 接力）
- `_inbound_handoff_*` → `run_pending_handoff`（专消费 handoff 队列，队列空就停，
  下一轮要等用户）

这条选择把"delegate 下一轮恢复打分"的语义钉死在 **"下一次用户触发"**，而不是
"同一 AI 链内立刻"。**好处**：
- delegate A 说完 → 队列空 → 停，等用户。其他茶客**不会**立刻接话 —— 与"硬指派"
  语义一致。用户想让 B 接话就再点一次 delegate；想恢复群聊就发一句话。
- 实现上 `run_pending_handoff` 是一个独立 loop，不与 `_run_ai_chain` 的 while
  纠缠，可测性高。
- 不需要"reset_budget 参数"或"递归调用"等增加心智负担的设计。

**关于 `_compute_trigger`**：当前用 `_consecutive_ai_turns == 0` 判 `user_msg`
vs `ai_chain`。handoff 进入 `run_pending_handoff` 时已清零，会被误判为 `user_msg`。
**修法**：handoff drain loop 不调用 `_compute_trigger`，**直接传**
`trigger={"kind": "handoff"}` 给 `start_turn`（见上方伪代码），不依赖 `_compute_trigger`。

**panel 一项跑一个 turn**：winners 在 drain loop 内直接从 `HandoffItem` 取，**不走打分**
（不调 `_pick_next_speaker`）；panel 一项的 N 个 targets 在同一 turn 内串行 speak，turn
结束后下一项（通常是 summarizer delegate）走下一次 while 迭代。

### 3.5 toml 配置

P7.3 起步阶段：`MAX_PANEL_TARGETS = 4` 作为模块常量，**不进 toml**。

未来若用户需求出现（hit 上限频繁 / 需要预设 default summarizer / 需要关闭"平行意见"
prompt hint），再补 `[panel]` 段并走 P4 four-touch checklist（`config.py::PanelConfig` /
`session.py` 透传 / `admin_toml.py::_render_room_toml` round-trip / CLAUDE.md 不变量延伸）。
**在用户开始抱怨之前不开 toml**，避免增加 admin UI / round-trip 测试面。

---

## 4. 关键设计决策（with rationale）

### 4.1 谁能触发？user only vs AI propose

**结论：用户直接触发 + 茶客可 propose-then-adopt**，但 P7 落地**先开 user，AI propose 推到后面阶段**。

- 沿用"写权限永远在用户"（P5.1 不变量）：茶客 propose → 用户点采纳 → 才入队。
  茶客**不能静默** delegate / panel，否则 AI 自我接龙开 50 个 panel 把 token 成本打穿
  ——与"防 AI 自我接龙生成 50 个空 task"同口径。
- propose 复用 `TASK_PROPOSAL` envelope，前端 `proposal_card.js` 加 `kind: "handoff"` 分支
  渲卡片，**server handler 零改动**（同 P5.3.7 决策口径）。
- **先开 user 直接触发**：UI 路径短，触发频率高，先把队列调度 / cancel 同步打磨稳；
  AI propose 是放大器，等基础稳了再加。

### 4.2 panel：并行 vs 串行

**结论：串行执行 + UI 标注"并行讨论中" + prompt 提示"平行意见"**。

真并行（同时 emit message_start 给 N 个茶客）的问题：
- 真并行写 transcript → message 顺序乱 → 后说的茶客看不见前说的 → "讨论"语义崩。
- 隔离 transcript（每位看自己的视图） → 互相听不见 → 退化成"N 个独立提问"，不再是 panel。
- 共享 transcript 但乱序 → 互相污染，茶客 LLM 会因为"前面有人说了 X"调整自己的输出，
  而那个 X 其实是后说的 → 因果颠倒。

串行执行的弱点："并行"是假的，先发言的会影响后发言的认知。**用 prompt 缓解**：
panel 茶客 incremental context 注入 `<panel_context>` 块（与 `<review_target>` 同位置），
告诉 LLM"你正在和 X/Y/Z 一起平行回答，请独立给出你的观点，不要重复前面茶客已说的内容"。
这不完美但工程零冲突、体验接近圆桌、回放与 transcript 顺序一致。

### 4.3 delegate 与"想接话"的张力

**结论：delegate 仅本轮独占；队列空后停住，下一次用户触发才恢复打分**（评审反馈 v9 措辞统一；
不再用"下一轮"这种容易被扫读误解为"同一 AI 链下一回合"的字眼）。

- handoff 走独立 drain loop（§3.4），队列空就 emit `turn_end(next="user")` 返回，
  **不回落到 `_run_ai_chain` 的 scoring**。delegate A 说完后其他茶客**不会**自动接话 ——
  必须等用户发一句普通消息 / 再点一次 delegate / 触发其他 inbound。
- 如果允许 delegate 持续独占多轮，群聊就退化成 1v1 私聊，丢掉茶话室的核心气质。
  **仅本轮独占 + 之后等用户**保留群聊弹性 —— 用户想连续指派 A 就连点几次 delegate；
  想恢复群聊就发一句话；想让 A 长期负责，那是 `task.owner` 该解决的问题（P7.5：
  scoring bonus 让 A 持续更容易接话，但不绝对独占）。
- 这条把 delegate / owner / 群聊三套机制的语义分清，避免功能重叠：
  - delegate = "本轮硬指派 A"
  - task.owner = "未来轮 A 软优先"（不强制）
  - 群聊 scoring = "用户发话后正常打分"

#### 4.3.1 delegate vs `@`：表面像，本质两件事

最深的区别在**"用户有没有在群里发话"**：

- **`@A 你怎么看？`** = 用户**开口说了一句话**，话里点了 A → 群聊里的"喂，A"。带着一句
  用户消息进 transcript。
- **`/delegate @A`**（或 UI 按钮） = 用户**没说话**，只是按了一个调度按钮 → 不进 transcript，
  仅指派下一句给 A。

围绕这个差异展开成八个维度：

| 维度 | `@A`（既有） | `/delegate @A`（P7.1） |
|---|---|---|
| **触发载体** | 用户消息文本里的 `@名字` 子串 | 显式 inbound 帧 `handoff_delegate` / UI 按钮 / `/delegate` slash |
| **是否带用户发言** | 是（"@A 你怎么看"本身是一条 user message，进 transcript） | 否（指派动作不进 transcript，纯调度） |
| **何时生效** | **仅 AI 链第一轮**认（`_consecutive_ai_turns == 0`）；AI 间接力时自动忽略 —— 防茶客互相点名接龙 | **任何时刻**都能从 cap 状态激活；入口清零 `_consecutive_ai_turns`（§3.4） |
| **强制程度** | A **单点 winner**（`_find_user_mention` 命中后 `_pick_next_speaker` 直接返回 `[A]`，不再跑 scoring，同轮**不并列**其他茶客；见 `chahua/orchestrator.py:439-450`）；下一轮 AI 接力时 `@` 失效（CLAUDE.md 不变量"AI 间接力时不再认 @"），其他茶客可能在打分后接话 | A **独占本轮**；其他茶客本轮不发言；下一轮**不回落 scoring**（§3.4 严格分流），等用户触发 |
| **与 in-flight 关系** | 不取消正在跑的 turn —— 现状 user_message 在 `_inflight_alive()` 时 drop（`chahua/server.py:646`，单 in-flight 严格策略，P7 不改） | **条件性** cancel（§4.4 / 反向评审 v3-#1）：in-flight 是 user-turn → cancel + drain；in-flight 是 handoff drain → 只 append 队尾、不动；无 in-flight → 直接启动 |
| **AI 能否用** | 茶客**可以**在发言里写 `@别人`，但 orchestrator 默认不认（CLAUDE.md 不变量"AI 间接力时不再认 @"） | 茶客**只能** propose（`task_propose_handoff`，P7.4），用户采纳才入队 |
| **debug 字段** | `scoring_path = "mention"`，`ScoreKind.MENTION` | `scoring_path = "handoff_delegate"`，`ScoreKind.MENTION` + `raw="handoff_delegate"`（§3.1 / 评审反馈 #5：复用 enum，区分走 path） |
| **执行驱动** | 走正常 `_run_ai_chain`，由 `submit_user_message` 触发（有用户消息） | 走 `run_pending_handoff`（不追加 user transcript，§3.4） |

**一句话区分**（反向评审 v3-#2 收紧，与 `chahua/orchestrator.py:439-450` 对齐）：

> `@A` = 用户**发了一条带点名的消息**；首轮（`_consecutive_ai_turns == 0`）确定性给 A
> **单点 winner**（不并列其他茶客）；A 说完后**回到 scoring**，下一轮其他茶客可能接话。
>
> `/delegate @A` = 用户**不发群聊消息**，直接调度 A **单点 winner**；A 说完后**不回落
> scoring**（§3.4 严格分流），等用户下次触发。

**两者都不让本轮并列发言** —— @A 和 `/delegate A` 都是确定性单点路由，区别只在
**是否带 user transcript** + **A 完成后是否回落 scoring**，不是"程度差异"。

**为什么不合并**：
- 把 `@` 升级成"`delegate` 同款不回落 scoring"会破坏群聊连续性 —— @A 之后用户期待的是
  "A 接话、然后大家正常往下聊"，不让别人接会让讨论卡死、群聊退化成"用户 → A、用户 → B"
  的 1v1 私聊连发。
- 把 `/delegate` 降级成"`@` 同款回落 scoring"则丢掉它的用武之地 —— 用户**就是**想
  确定性把任务交给 A、不想 A 说完又被打分插话时（典型场景：A 是这个任务的指定执行者），
  回落 scoring 帮不上。

**语法糖关系**：`/delegate @A` 里的 `@A` **只是 target 解析的语法糖**（复用 mention parser
拿到名字），与 `@A` 在群聊消息里的语义无关 —— 看到这种写法不要混淆。UI 触发路径根本不
经过 `@` 解析。

### 4.4 队列与 cancel / in-flight 的同步

入队前是否 `_cancel_and_drain_inflight` **按场景区分**（反向评审 v3-#1 保护队列语义）：

| inbound | `_inflight_kind == "user"` | `_inflight_kind == "handoff"` | `_inflight_kind is None` |
|---|---|---|---|
| `handoff_delegate` / `handoff_panel` | ✅ cancel + drain | ❌ 不动（drain 自然消费新项） | ❌ 不需要 |
| `handoff_clear` | ✅ cancel + drain | ✅ cancel + drain | ❌ 不需要 |
| `user_message`（参考，非 P7 改动） | drop（现状 `chahua/server.py:646`） | drop | 新建 user-turn |
| `cancel` 按钮（参考） | ✅ cancel | ✅ cancel | no-op |

**为什么 delegate 在 handoff drain 时不 cancel**：否则连点 N 次 delegate 永远只剩最后一项
被执行——前一项总被下一项 cancel——"队列"语义崩。drain loop 的 while 已经在串行消费
队列；新项 append 到队尾、drain 自然能看到。

**为什么 clear 始终 cancel**：UI 按钮文案"全部取消"是 nuclear 操作；区分 in-flight 类型
反而增加 asymmetry 心智成本（反向评审 v3-#3）。

**clear / cancel 路径保护**：cancel 路径自己 emit `turn_end status=cancelled` +
`message_end status=cancelled`，与 user-turn 的 cancel 走同口径。delegate / panel
**不**调 cancel 时，老 in-flight handoff drain 完成后 emit 正常 `turn_end` 然后顺势消费
新项，前端两个 inflight 同时存在的风险被 wrapper 的 single-slot `_inflight_turn_task`
天然防住（一个进程槽位、一个 task）。

### 4.5 review 的 scope：只支持 `message`（P7.2 收窄）

- **只支持 `scope=message`**：用户从"消息气泡 → 请审…"按钮触发，自带 `message_id`。
- **不开** `scope=last` —— "刚那条"歧义太大（跨 turn 怎么算？被审消息正好是 user 怎么办？）
  误触发风险与测试面都不值。
- **不开** `scope=task` —— "全任务过一遍"语义重，会拉一长串 artifacts / decisions 进 prompt；
  先用消息粒度走通，等用户场景明确再加。
- **`scope=@msg_id` slash 命令也不开** —— UI 路径已经覆盖所有合法 message_id 场景；
  手输 message_id 没有用户场景。**附带要做的最小消息锚 UI**：右键消息气泡 → 复制 message id
  仅用于调试，不入正式触发路径。

### 4.6 prompt 注入：统一走 `extra_blocks` 参数

**问题**：原文写"插在 `<recent_messages>` 与 `<speak_instruction>` 之间"，但
`_render_incremental` 路径里根本**没有** `<recent_messages>` 块 —— 它只有 `<room_update>`
（见 `chahua/context_renderer.py`）。两条路径不能用同一位置描述。

**正确口径**：

```python
# chahua/context_renderer.py
def build_context_for(
    self, guest_name: str, last_seen: int,
    *, task_id: Optional[str] = None,
    extra_blocks: list[str] | None = None,   # ← 新增
) -> str:
    # ... 走 onboarding 或 incremental 分支 ...
    # 两条路径都在 <speak_instruction> 之前插 extra_blocks（保序，无内容则不插）
```

- **onboarding / incremental 两条路径都接 extra_blocks，注入位都在 `<speak_instruction>`
  之前**。否则首次被 review/panel 指派的茶客（走 onboarding 分支）看不到临时块，行为分裂
  —— 原文"不进 onboarding"是错的，要 fix。
- **临时块（review / panel）vs 永久块（current_task / order_hint）口径不同**：
  - `<current_task>` / `<order_hint>` 走"有任务时永远注入"，由 `_render_*` 内部根据
    `task_id` 是否非空决定 —— 与 extra_blocks 是**两个独立机制**，不冲突。
  - `<review_target>` / `<panel_context>` 是"本轮临时指派"，由 orchestrator 在 pick 时
    根据 `HandoffItem` 现场合成块字符串，通过 `extra_blocks` 参数传入 —— 渲染器**不知道**
    这些块的语义，纯转发。
- **注入顺序**：`extra_blocks` 列表内顺序即输出顺序；调用方（orchestrator）保证顺序，
  渲染器不重排。

---

## 5. UI 增量

> **阶段分布**（反向评审 v4-#4 收紧）：本章按 P7.1 / P7.2 / P7.3 阶段标注，
> **P7.1 only 实施 §5.1.1 + §5.3 + §5.4**（⇨ delegate 按钮 + 队列预览 + 调试抽屉），
> §5.1.2 多选圆桌 / §5.2 消息气泡按钮 / §5.2 message id 复制是 **P7.2 / P7.3** 的事，
> 留在本章是为契约完整性，**不要在 P7.1 PR2 里实施**。

### 5.1 茶客侧栏

#### 5.1.1 ⇨ delegate 按钮（**P7.1**）
- 每位茶客头像右下角加"⇨" hover 按钮 → 弹"交给…"小菜单（带可选**内部备注**输入）
- **备注输入文案**（反向评审 #2）：placeholder 写"内部备注（用于自己回看 / 调试，**不发给茶客**）"，
  不要写"指令" / "说明你想让 A 做什么"等会让用户误以为茶客能看到的措辞。
- **handoff drain 跑期间不 disable**（反向评审 v4-#2）：drain 中 delegate 应 append 到
  队尾、不抢占；只在 ws 断开 / target 不在场 / popover 提交中三种情况 disable，详见
  P7.1.9 checklist。正常 tooltip"加入队列"，让用户知道点了会进队而不是立刻独占。

#### 5.1.2 多选圆桌（**P7.3**，不在 P7.1 实施）
- 多选模式（长按 / Shift+点击）→ 下方出现 "圆桌讨论" 按钮 → 弹 panel 配置（summarizer 下拉）

### 5.2 消息气泡（**P7.2**，不在 P7.1 实施）
- 茶客发言气泡 hover 显示"请审…"按钮 → 弹茶客选择菜单
- 右键菜单加"复制 message id"（P7.2 落地附带的最小消息锚 UI）

### 5.3 队列预览（**P7.1**）
聊天框上方（@提及 hint 同区）出现"➡️ 下一句：A（用户指派）"小条；多项时显示 "A → B → 汇总:F"
（"汇总:F" 字样要 P7.3 panel 才会出现；P7.1 阶段只会是 delegate 序列）。
若 A 项有 `reason` 备注，hover 队列项显示 "理由：xxx"（**仅 UI 显示**，与"备注不发给茶客"语义对齐）。
右侧 ✕ 按钮触发 `handoff_clear` —— 按钮文案用"**全部取消**" tooltip 写
"取消当前发言 + 清空后续队列"（反向评审 v3-#3 简化语义后名实相符）。**不要**写"清空队列" /
"清后续"等让用户以为"A 会说完再清"的措辞。

### 5.4 调试抽屉（**P7.1**，按阶段渐进扩 enum）
- 复用 P6 已有的 `scoring_path` 标识，按阶段扩 enum 后 turn 行旁显示：
  - **P7.1**：`scoring` / `mention` / `broadcast` / `handoff_delegate`
  - **P7.2**：再加 `handoff_review`
  - **P7.3**：再加 `handoff_panel`
- 让"为什么是 X 发言"一眼能看出；详情面板里**若**有 `<review_target>` / `<panel_context>`
  prompt block 注入（P7.2 / P7.3 才会出现），prompts 列表自然能看到。**不新增
  `pick_source` 字段**（评审反馈 #3）。

---

## 6. 落盘布局

P7 **不新增持久化目录、不新增 toml 字段、不 bump schema_version**。

- 队列：内存瞬态（§3.1）。
- debug 落盘：**复用现有** `scoring_path` 字段扩 enum（§3.1 / `debug_recorder.py::VALID_SCORING_PATHS`），
  不新增字段。可选 `handoff_item` 子字段（kind/target(s)/issued_by/panel_group_id）随取证一并落，
  未知字段忽略口径吃下。
- toml：P7.3 起 `MAX_PANEL_TARGETS = 4` 是模块常量，不进 toml（§3.5）。

未来若需要 `[panel]` toml，走 P4 `[room.llm]` 同口径 four-touch checklist
（`config.py::PanelConfig` → `session.py` 透传 → `admin_toml.py` round-trip → CLAUDE.md
不变量延伸），但**等到用户实际抱怨 4 不够、或需要预设 default summarizer 才开**。

---

## 7. 阶段化路线

**简化口径（评审反馈 2026-05-19）**：每阶段范围尽量窄，只做让该动作"能跑起来"的最小集，
配置项 / 联动字段都推到后续阶段。

| 阶段 | 内容 | 依赖 | 说明 |
|---|---|---|---|
| **P7.1** | **delegate only**：内存队列；`enqueue_handoff` + `run_pending_handoff`；server `_inbound_handoff_delegate`；扩展 `scoring_path` enum；UI 茶客侧栏 ⇨ 按钮 + 队列预览；cancel 同步 | 无 | **不碰** task.owner / AI propose / `[panel]` toml |
| **P7.2** | review：UI 消息气泡"请审…"按钮触发（自带 `message_id`，`scope=message` 唯一档）；`extra_blocks` 参数 + `<review_target>` 块（onboarding/incremental 两条路径都接） | P7.1 | **不开** `last` / `task` / slash `@msg_id`；右键复制 message_id 仅调试用 |
| **P7.3** | panel：HandoffItem 持 `targets` 元组（`to_dict` 转 list） 一项跑一个 turn；串行 + `<panel_context>` 注入；`MAX_PANEL_TARGETS = 4` **硬编码常量**；summarizer 作为下一个 delegate 入队；UI 圆桌模式 | P7.1 | **不开** `[panel]` toml / `default_summarizer` / `parallel_prompt_hint` 开关 |
| **P7.4** | AI propose handoff（`task_propose_handoff` 工具 + `TASK_PROPOSAL` kind="handoff" + 前端卡片 kind 分支） | P7.1–7.3 | 最后开；基础动作稳了再放大 |
| **P7.5（可选）** | delegate 联动 `task.owner`（勾选"同时设为负责人" + scoring owner bonus） | P7.1 + task | 独立小项，与 P7.1 解耦可后补 |
| **`[panel]` toml** | 不属于任何阶段；用户开始 hit 上限 / 需要预设 summarizer 时再加 | P7.3 | 走 P4 four-touch checklist |

---

## 8. 关键不变量（沿 [CLAUDE.md](../CLAUDE.md) 口径，落地时同步追写）

- **handoff 是调度层增量，不改对话原语**。仍走一根 `transcript.jsonl`；茶客视角不变。
- **执行驱动是显式入口**（§3.4）：`enqueue_handoff` 只入队不启动；`run_pending_handoff`
  才跑队列；仅 enqueue 不会自然跑起来。
- **`run_pending_handoff` 与 `_run_ai_chain` 严格分流，不互相回落**（§3.4 / §4.3，
  评审反馈 #3 收紧）：handoff drain loop 专消费 handoff 队列，**队列空 / 下一项 cap 撞顶就停 +
  emit `turn_end(next="user")` 后 return**，**不回落到 scoring**。下一次 scoring 必须由用户消息
  触发——这是"delegate 硬指派之后等用户"的工程基础。
- **drain loop 每轮 turn 末尾的 5 步顺序严格对齐 `_run_ai_chain:376-399`**
  （§3.4，反向评审 #1 + 反向评审 v2-#1 修正）：
  ① peek 下一项 / 算 `cost` / 比 cap → 得到 `next_state` ("ai" / "user") →
  ② `_emit_turn(TURN_END, data={"next": next_state})` →
  ③ `_recorder.flush_turn()` →
  ④ `_kick_summarize()` / `_tick_cooldown()` / `_kick_detect_new_artifacts(sink, task_id)` →
  ⑤ `if not has_next: return`。
  **禁止把三个 hook 放到 `turn_end` 之前**——`_kick_detect_new_artifacts` 会 emit
  `task_artifact_added` + `task_info`，早于 `turn_end` 到前端时 UI 收到"任务面板更新"
  时旧 turn 还没结束，状态错位、与 `_run_ai_chain` 不一致还会让前端针对两种 turn 写两份
  顺序处理逻辑。缺 `_kick_detect_new_artifacts` → handoff turn 写 `./task/<x>` 后 task_info
  不刷新、P5.4 通道 3 自动归集失效；缺 `_kick_summarize` / `_tick_cooldown` → 摘要不增量 /
  冷却不递减；三个 hook 要么一起加要么一起删，禁止只跑两个。
- **drain loop 每轮 turn 末尾一次性 emit `turn_end`**（§3.4，反向评审 #3）：
  ① 步算出的 `next_state` 直接进 ② 步 emit。**禁止**两帧重叠（先 `next="ai"` 末尾再补
  `next="user"`）——少一帧 envelope，UI"停止"按钮状态不闪。判定 next 的 cap 检查与
  while 入口处的 cap 检查逻辑同口径（`_consecutive_ai_turns + cost > max` → "user"），
  算 `cost` 时 `len(targets) if panel else 1` 两处保持一致。`has_next == False` 时
  hook 跑完后立即 return，不走下次 while 迭代。
- **drain loop 在 `run_pending_handoff` 内闭环**（§3.4，评审反馈 #2 简化修法）：
  while 内每个 turn 串行执行 + 计数累加 + 检查 cap；**不递归调** `run_pending_handoff`、
  **不引入 `reset_budget` 参数**。计数全程不重置——这是 §2.3 "panel + summarizer ≤
  max_consecutive_ai_turns" 的工程基础。
- **`run_pending_handoff` 入口清零计数一次**（§3.4，评审反馈 #2）：上一轮 AI 已到 cap 时
  不清零会被 while 立刻挡掉、handoff 永不执行。**只在入口清一次**，drain loop 内不再清。
- **`handoff_consumed` 必须在 `turn_start` 之后 emit**（§3.4，评审反馈 #1）：`turn_id` 走
  envelope **顶层**（与 turn_start / turn_end 同口径），而顶层 `turn_id` 在 `_emit_turn(TURN_START)`
  之前不存在；不能在 popleft 时 emit。`data` 只放 `item`，不在 data 里重复塞 `turn_id`。
- **panel + summarizer 总轮数受 `max_consecutive_ai_turns` 约束**（§2.3，评审反馈 #1）：
  `effective_targets ≤ min(MAX_PANEL_TARGETS, max_consecutive_ai_turns - 1)`（有 summarizer 时）
  / `min(MAX_PANEL_TARGETS, max_consecutive_ai_turns)`（无 summarizer）；超出 → inbound
  NOTICE error。`max_consecutive_ai_turns ≤ 1` 时禁用 panel+summarizer 组合。
- **envelope emit 职责拆分**（§3.4 / §3.3，评审反馈 #4 + v3-#4）：`enqueue_handoff` 不带 sink、
  不 emit；`handoff_enqueued` / `handoff_cleared` 由 **server inbound handler** emit；
  `handoff_consumed` 由 **orchestrator** 在 `run_pending_handoff` drain loop 内
  **mint turn_id + emit turn_start 之后** emit；`turn_id` 走 envelope **顶层**（与
  turn_start / turn_end 同口径，不在 data 里重复塞一份，评审反馈 v8-#2），`data` 只放
  `item`。**不**在 popleft 时 emit —— pop 时 turn_id 还不存在。
- **turn_start envelope 由 `_emit_turn(TURN_START)` 发，不是 recorder**（§3.4，评审反馈 v3-#1）。
  `TurnRecorder.start_turn` / `record_scoring` 只写 in-flight 落盘；handoff drain loop 必须
  按既有 `_run_ai_chain` 顺序走：`new_turn_id → recorder.start_turn → recorder.record_scoring
  → _emit_turn(TURN_START) → emit handoff_consumed → speak winners`。
- **cap 检查按 item cost 算，不是 cap=1**（§3.4，评审反馈 v3-#2）：panel 一项 = N 次 speak，
  pop 前算 `cost = len(targets) if panel else 1`；`_consecutive_ai_turns + cost > max` 时
  **不 pop、直接收尾返回**，队首留到下次用户触发。否则 panel 在 drain 中段执行会越过上限。
- **server 必经 `_run_handoff_turn` wrapper**（§3.4，评审反馈 v3-#3）：不直接
  `create_task(self._session.orchestrator.run_pending_handoff(...))`。wrapper 结构照搬 `_run_turn`：cancel-safe
  + finally 清 `_inflight_turn_task`。否则"Task exception was never retrieved" WARN +
  cancel/busy 判定与 user-turn 不一致。
- **wrapper swallow CancelledError、不 re-raise**（§3.4，评审反馈 v4-#1）：与 `_run_turn`
  同口径。cancel 补偿（`turn_end(cancelled)` + `flush_turn()`，状态走 `messages[].status`）由
  `run_pending_handoff` 内 speak 段 try/except 负责，wrapper 只做日志 + 清 task 槽。
- **panel 的 summarizer 由 inbound handler 原子入队**（§2.3 / §3.4，评审反馈 v4-#2）：
  接收 `handoff_panel` 时一次性入队 panel item + 可选 summarizer delegate item，共享
  `panel_group_id`。**不允许** orchestrator 在 panel 跑完后再"生成"summarizer item ——
  `HandoffItem` 不存 summarizer 字段，orchestrator 跑完后无从得知；让 orchestrator 持
  `summarizer` 状态会污染队列的"自描述"语义。
- **`ScoreResult.kind` 复用 `ScoreKind.MENTION`**（§3.1，评审反馈 #5）；handoff 在
  `raw="handoff_delegate"` / `"handoff_review"` / `"handoff_panel"` 维度区分，真正归类
  走 `scoring_path` enum。不新增 `ScoreKind` 值，绕过 inner cap 的现有逻辑自然成立。
- **`HandoffKind` enum + `SCORING_PATH_HANDOFF_*` 按阶段加，不提前铺**（反向评审 #6 部分采纳）：
  P7.1 只加 `delegate` 一档（enum 值 + `SCORING_PATH_HANDOFF_DELEGATE` + frozenset 白名单一条）；
  `review` 留给 P7.2 加、`panel` 留给 P7.3 加，加值时一并在 frozenset 加白名单。**例外**：
  envelope 类型 `HANDOFF_ENQUEUED` / `HANDOFF_CONSUMED` / `HANDOFF_CLEARED` 三个 P7.1 必须
  全加（delegate 自己每轮都 emit `HANDOFF_CONSUMED`，handler 自己发 `HANDOFF_ENQUEUED` /
  `HANDOFF_CLEARED`），不分阶段。
- **`reason` 是内部备注、不进茶客 prompt**（§2.1 / 反向评审 #2）：`HandoffItem.reason` 字段
  只进 debug record metadata + 队列预览小条 hover 提示；茶客视角看到的是群聊 context +
  onboarding/incremental，**永远不见 reason**。UI 文案配套写"内部备注（用于自己回看 /
  调试，**不发给茶客**）"——禁止写"指令" / "说明你想让 A 做什么"等会让用户误以为茶客
  能看到的措辞。P7.4 茶客 propose handoff 时 reason 进 `TASK_PROPOSAL` 卡片给用户判断
  是否采纳，仍**不**进 target 茶客的 prompt。
- **`handoff_clear` 始终 cancel + clear**（§3.3 / §4.4 / §5.3，反向评审 #4 + 反向评审 v3-#3 简化）：
  - 无差别 `_cancel_and_drain_inflight()`（与 cancel 按钮 / add_guest / set_active_task 同口径）
    + 清队列剩余项 + emit `handoff_cleared{items_dropped: [...]}`。
  - **不**区分 in-flight 类型——反向评审 v2-#3 引入的 asymmetric 是为一个**实际不存在的场景**
    （"user_message 打断 handoff drain 后留下残队列"）买保险；现状 `chahua/server.py:646` 在
    `_inflight_alive()` 时直接 drop user_message，普通消息不会切换 turn 类型。
  - UI 按钮文案"**全部取消**" + tooltip"取消当前发言 + 清空后续队列"——名实相符。
  - `items_dropped` envelope 只列被丢的队列项，**不**重复列被 cancel 的 in-flight item
    （已在标准 cancel 路径 emit `turn_end status=cancelled` + `message_end status=cancelled`）。
  - **不**做"只清后续 / 当前 handoff 继续跑"的 partial cancel——与 `_cancel_and_drain_inflight`
    承重墙冲突 + UX 上需要用户在队列 X 按钮和 composer cancel 按钮之间切换才能"全停"。
- **`handoff_delegate` / `handoff_panel` 入队前 cancel 是条件性的**（§4.4 / 反向评审 v3-#1
  保护队列语义）：
  - `_inflight_kind == "user"` → `_cancel_and_drain_inflight()` 抢占（与 user_message 互相
    抢占同槽位的传统语义一致）。
  - `_inflight_kind == "handoff"` → **不动 in-flight，只 append 到队尾**——否则连点 N 次
    delegate 永远只剩最后一项被执行（前一项总被下一项 cancel），"队列"语义崩。
  - `_inflight_kind is None` → 无 in-flight，不需要 cancel。
  - 工程基础：server 持 `_inflight_kind ∈ {"user", "handoff", None}` 与 `_inflight_turn_task`
    同生命周期（wrapper finally 同槽清两个）；`_inflight_kind` **仅** delegate / panel
    inbound 用来判 in-flight 类型，`_cancel_and_drain_inflight` 自身保持无差别 cancel 语义。
- **`handoff_delegate` / `handoff_panel` 启动 wrapper 是条件性的**（§3.4，反向评审 v3-#1）：
  enqueue 后判 `if self._inflight_turn_task is None: 启动新 wrapper`；已有 handoff drain 在
  跑时不启动新 task，drain 内 while 循环自然在当前项结束后看到队尾新项。判 `is None` 而不是
  `done()`：wrapper finally 先清 slot 再让 task 完成，所以 `done() == True` ↔ slot 已 None。
- **delegate 仅本轮独占；队列空后停住，下一次用户消息触发时恢复普通 scoring**（评审反馈
  v8-#1 措辞修正）。**禁止在 drain 末尾自动回落到 `_run_ai_chain`**——"下一轮"等价于"下一次
  用户消息触发的 turn"，不是 drain 之后的当前 turn。持续独占由 `task.owner` 的 scoring bonus
  解决，不靠队列。
- **panel = 一个 HandoffItem，跑一个 turn，winners=targets**（§2.3 / §3.1）。**不**拆 N 项 /
  不开 N 个 turn —— 否则"圆桌讨论"语义崩、UI/debug 显示散成 N 条独立 turn。
- **panel 串行执行**，并行只是 UI 标注；茶客 prompt 注入"平行意见"提示缓解先发言污染后者。
- **`MAX_PANEL_TARGETS` 是模块常量不是 toml**（P7.3 起步阶段，缺省 4）；
  未来若开 `[panel]` toml 走 P4 four-touch checklist。
- **写权限永远在用户**：茶客只能 `task_propose_handoff` propose、不能静默入队。
- **入站严格、未知字段 NOTICE error**：handoff_* inbound 走 `room.toml` / `open_task` 同口径。
- **队列状态不落盘**：与 in-flight `_current` 同瞬态语义；崩溃即丢，用户重指即可。
- **TASK_INFO 不收 handoff 队列**：队列是调度层瞬态，由 `handoff_enqueued` / `handoff_consumed`
  hint envelope 维护前端预览；TASK_INFO 仍是 task 状态的权威快照。
- **debug 字段复用 `scoring_path` 扩 enum，不新增 `pick_source`**（§3.1 / §6）。
  同一概念两套字段的对齐成本不值。
- **临时 prompt 块走 `extra_blocks` 参数，onboarding/incremental 两条路径都接**（§4.6），
  注入位都在 `<speak_instruction>` 之前。不允许"只进 incremental 不进 onboarding"——
  会让首次被指派的茶客行为分裂。
- **review 只支持 `scope=message`**（P7.2 收窄）；`last` / `task` / slash `@msg_id` 都不开。
- **review prompt 不承诺"被审消息绑定的 artifacts"**（§2.2）—— transcript Message 没有
  artifact 绑定字段；最多附"当前任务 artifacts 列表"（如该消息有 task_id）。

---

## 9. 明确不做的事

- **不做"自动 handoff 编排"**（i.e., AI 根据上下文自动决定该 delegate 给谁）。这相当于
  让 LLM 做调度器，与"打分→pick"的设计哲学相反，且不可解释。P7 范围内 handoff 始终是
  user 触发或 user 采纳的 propose，**永远显式**。
  - **例外形态留给 P8（任务剧本 / workflow）**：用户在 `open_task` 时**显式声明** workflow
    步骤（draft → review → consolidate ...），调度器按剧本批量入队 handoff items。这仍是
    "用户触发，只是 deferred"，调度器**执行剧本不做意图推理**，符合 deterministic + explainable
    哲学；不属于本节禁止的"AI 自动编排"。形态参考 §草案讨论 2026-05-19，独立成 P8 后另起 doc。
  - **明确不做的子项还包括**：基于"用户发言"的 NLU/正则意图识别自动触发 handoff
    （`@提及` 已经是这条路的最干净形态，再加自然语言意图识别会假阳性 + 不稳定 + 不可解释）。
    要扩展只走显式 slash 命令（`/delegate` / `/review` / `/panel`）。
- **不做消息线程化 / 分裂子流**。沿 P5 决策口径：聊天仍是线性主流，task / handoff 都只是
  打 tag。Slack thread 那种分裂会破坏茶客之间"听见对方"的语义。
- **不做跨房间 handoff**。茶客是房间局部资源，跨房间指派的语义太复杂（cwd / 记忆 / 任务都在
  房间内），不开这条路径。
- **不让队列持久化**。崩溃恢复一致性的复杂度（与 transcript 顺序 / inflight 状态对齐）远大于
  "重启后用户重新点一次"的代价。
- **不做 delegate 链式独占**（"A 说完自动给 B 然后给 C 不准任何人插嘴"）。这是 panel 的事，
  delegate 故意保持单轮粒度，避免两个机制功能重叠。

---

## 10. 主要风险 & 兜底

| 风险 | 兜底 |
|---|---|
| panel 中后说的茶客被前面茶客污染（"平行"是假的） | prompt 注入"独立给出观点不要重复"；UI 标注"并行讨论中"让用户知道这是逻辑并行；接受 inevitable trade-off |
| delegate 给一个已退场 / 不存在的茶客 | 入队时校验 `target in active_guests`；非法 → NOTICE error 不入队 |
| review 取到的不是用户预期的"那条" | P7.2 收窄到只支持 `scope=message`（强制从消息气泡 "请审…" 按钮进入，自带 `message_id`），不开 `last` / `task`；UI 队列预览显示"将审：{display}"二次确认 |
| 用户连点 N 次 delegate 把队列堆爆 | P7.1 **不设软上限**（反向评审 v3-#5：替换队尾会静默丢已排队指派，解释成本高于价值）；若未来真出现"队列堆到几十项 UI 渲不下"的反馈，再走 P4 four-touch checklist 加 `[handoff].max_queue_len` toml + **硬拒** + NOTICE error（不替换） |
| 用户点"全部取消"以为 A 会说完再清，结果当前 A 也被打断 | `handoff_clear` 始终 cancel + clear（§3.3 / 反向评审 v3-#3 简化）：A 的 message_end 走 `status=cancelled` 与既有 cancel 同口径；UI 文案"**全部取消**" + tooltip"取消当前发言 + 清空后续队列"，名实相符 |
| 连点 N 次 delegate 队列里前一项被下一项 cancel 掉，剩最后一个执行 | `_inbound_handoff_delegate` 在 `_inflight_kind == "handoff"` 时**不**调 `_cancel_and_drain_inflight`，只 append 到队尾（§4.4 / 反向评审 v3-#1）；drain loop while 内自然消费新项 |
| panel summarizer 茶客 LLM crash / 超时 | 与普通 speak 失败同口径 —— `message_end status=error` + envelope 通知；队列后续项继续，不卡死 |
| 队列与 add/remove guest 竞态（用户 delegate 给 A 同时把 A 删了） | `add_guest` / `remove_guest` inbound 自身走 `_cancel_and_drain_inflight`（既有口径，砍掉跑中的 drain）；`_inbound_handoff_delegate` 入队时校验 `target in active_guests`，非法 NOTICE error 不入队；通过/未通过两条路都不留 dangling 项 |
| 与 `@提及`的关系混乱（用户：到底用 @ 还是 /delegate？） | 文档/UI tooltip 明确（反向评审 v3-#2）：`@A` = 用户带消息单点路由，A 完后**回到 scoring**（其他茶客可接话）；`/delegate A` = 用户不带消息单点调度，A 完后**不回落 scoring**（等用户）；两者**都不让本轮并列发言**；@ 不入 handoff 队列 |

---

## 11. 与 P5 / P6 系统的对接点

| 现有承重墙 | P7 怎么蹭 |
|---|---|
| `@提及`确定性路由（CLAUDE.md 打分不变量） | delegate 与 @ 都是单点路由（反向评审 v3-#2 同口径）；delegate 复用 mention parser 解析 target，但不带 user message + 不回落 scoring（§4.3.1） |
| `Task.owner` 闲置字段 | P7.5 可选阶段填充：delegate 时勾选可同步设 owner |
| `task_propose_*` 工具家族 | P7.4 新增 `task_propose_handoff`，complete the family |
| `TASK_PROPOSAL` envelope + `proposal_card.js` | 复用，前端加 `kind="handoff"` 分支 |
| `_cancel_and_drain_inflight` | `handoff_clear` 始终走（§4.4 / 反向评审 v3-#3）；`handoff_delegate` / `handoff_panel` **仅在** `_inflight_kind == "user"` 时走（§4.4 / 反向评审 v3-#1）；自身保持无差别 cancel 语义 |
| `[room.llm]` / `[scoring]` toml + four-touch checklist | `[panel]` toml 暂不开（P7.3 用模块常量）；未来真需要再走同一 checklist |
| debug recorder `scoring_path` + `VALID_SCORING_PATHS` 白名单 | **按阶段扩 enum**（反向评审 v2-#4）：P7.1 加 `handoff_delegate` / P7.2 加 `handoff_review` / P7.3 加 `handoff_panel`；不新增 `pick_source` 字段 |
| `_render_onboarding` / `_render_incremental` 固定块结构 | 不动；通过新增 `extra_blocks` 参数注入临时块，两条路径都在 `<speak_instruction>` 之前 |
| `format_messages` 的 `<message>` 包装 | review prompt 内被审消息复用同一包装函数，保证 LLM 看到的消息边界一致 |

---

## 12. P7.1 commit checklist

把 §7 P7.1 拆成 12 个独立可 review / 可回滚的 commit，按依赖顺序排。每个 commit 要求：
① 单跑 `uv run pytest` 不挂；② 单装 dev Electron 不卡 ws 握手（即使新 UI 缺失，旧路径不能坏）。

**约定**：依赖列写 commit 编号或 "无"；commit message 前缀按 `chahua: P7.1.<n> ...` 沿用 P4 / P5 / P6 习惯。

**粒度口径**（评审反馈 v5-#4）：下表 12 项是**最细粒度**列法，实施者可按"同一文件 / 同一意图的相邻改动"
就近合并到 8~9 个 commit，**不必为了"可回滚"拆到每个小常量一条**。推荐合并候选：
- P7.1.2 + P7.1.3 —— debug enum 三常量 + events 三个 type，纯类型/常量定义，一条 commit 合理
- P7.1.6 + P7.1.7 —— server inbound handler + `_run_handoff_turn` wrapper，都在 `server.py`、紧耦合
- P7.1.8 + P7.1.10 —— 前端 state（events.js 常量 + `handoffQueueState`）+ 队列预览小条 UI，state 与首个消费者紧耦合
- P7.1.9 + P7.1.11 —— 茶客侧栏 ⇨ 按钮 + 调试抽屉适配，都是"看得见的 UI" 中间态无观察价值

合并后 PR 边界（PR1 = backend / PR2 = frontend / PR3 = doc）不变，每个 PR 的 commit 数减少；
**禁止跨 PR 边界合并**（otherwise PR1 没单独跑过 dev Electron 就上前端，回滚粒度退化）。

| # | 范围 | 依赖 | 验收线索 | commit |
|---|---|---|---|---|
| **P7.1.1** | 新建 `chahua/handoff.py`：`HandoffItem` dataclass + `HandoffKind` enum（**P7.1 仅加 `delegate` 一档**，`review` / `panel` 值留给 P7.2 / P7.3 阶段进 enum 时再加 —— 反向评审 #6 部分采纳：P7.1 不消费的 enum 值不提前铺）；`to_dict()` 序列化（送 envelope 用）。**纯模块，不接 wiring** | 无 | 单测：HandoffItem round-trip / 非法 kind 进 enum → ValueError | `57040fb` |
| **P7.1.2** | `chahua/debug_recorder.py` 扩 `VALID_SCORING_PATHS` enum：**P7.1 只加 `SCORING_PATH_HANDOFF_DELEGATE`**（`_HANDOFF_REVIEW` / `_HANDOFF_PANEL` 留给 P7.2 / P7.3 阶段加 —— 反向评审 #6 部分采纳；这两条常量 P7.1 没有 callsite 消费）；白名单 frozenset 同步加一条 | 无 | 单测：`record_scoring(scoring_path="handoff_delegate")` 不挂；非法 path 仍降级为 `SCORING_PATH_SCORING` | `57040fb` |
| **P7.1.3** | `chahua/events.py` 加 3 个新 `ChahuaEventType`：`HANDOFF_ENQUEUED` / `HANDOFF_CONSUMED` / `HANDOFF_CLEARED`；`schema_version` 不动。**不加 `new_handoff_id()` helper**（评审反馈 v5-#3：当前协议只有 `handoff_clear` 全清、无单项取消、`HandoffItem` 也无 id 字段——过度设计；队列预览按数组顺序维护即可，未来真做单项取消再加） | 无 | grep：value 字符串与 §3.3 envelope 表对齐 | `57040fb` |
| **P7.1.4** | `chahua/orchestrator.py` 加 `self._handoff_queue: deque[HandoffItem]` 内存字段；新增 `enqueue_handoff(item)` 返回 snapshot / `clear_handoff_queue()` 返回被丢项 list —— **纯方法，无 sink、无 emit**。reset_room / 切房路径需把队列清掉（与 in-flight 同口径） | P7.1.1 | 单测：① enqueue → snapshot 一致；② clear → 返回值含被丢项；③ reset_room 后队列空 | `57040fb` |
| **P7.1.5 (承重墙)** | `chahua/orchestrator.py` 加 `run_pending_handoff(sink, *, task_id)` drain loop（§3.4 伪代码）：入口清零 `_consecutive_ai_turns` / `_rounds_without_user_or_mention`；while 内 peek 队首算 `cost`、超 cap 不 pop 直接 break；mint turn_id → `recorder.start_turn(turn_id, task_id, trigger={"kind": "handoff", "handoff_item": item.to_dict()})`（**`trigger` 必须带 `handoff_item`**，否则 reason / target / issued_by / panel_group_id 全丢；评审反馈 v6-#2） → 为每 winner 生成 `ScoreResult(score=1.0, kind=MENTION, raw="handoff_delegate")`（评审反馈 v5-#2）→ `record_scoring(threshold=None, results=[(r, None) for r in score_results], winners=winners, scoring_path="handoff_delegate", ...)`（**`threshold=None`** 与 mention / broadcast 同口径，**不要写 0.0**；评审反馈 v7-#1。**注意 tuple 列表形**，prompt 传 None；评审反馈 v6-#1）→ `_emit_turn(TURN_START, data={"scores": [...], "scoring_path": ...})`（**用既有 `data: dict` 签名，不扩 kwargs**；评审反馈 v6-#2） → emit `HANDOFF_CONSUMED` envelope（**顶层 `turn_id`**，`data` 只含 `item`，不在 data 里重复塞 turn_id；评审反馈 v8-#2） → **复用既有 `_let_speak(...)` helper** 串行跑 winners（评审反馈 v6-#4：不抽 `_speak_one`，P7.2 真需要 extra_blocks 再给 `_let_speak` 加形参） + cancel fixup（try/except CancelledError → `_emit_cancel_fixup` + `self._recorder.flush_turn()` + raise，**flush_turn 无参**，状态走 messages[].status 表达，评审反馈 v5-#1）→ **末尾 5 步顺序严格对齐 `_run_ai_chain:376-399`**（反向评审 v2-#1 修正）：① peek 下一项 + 算 cost + 比 cap → `next_state` → ② 一次性 emit `_emit_turn(TURN_END, data={"next": next_state})`（不再发 speculative `next="ai"` 再补 `next="user"` 帧，反向评审 #3） → ③ `self._recorder.flush_turn()` → ④ `_kick_summarize()` / `_tick_cooldown()` / `_kick_detect_new_artifacts(sink, task_id)`（与 `_run_ai_chain:396-399` 同口径，反向评审 #1；**禁止把 hook 放到 turn_end 之前**——否则 `task_artifact_added` 早于 `turn_end`，前端状态错位） → ⑤ `if not has_next: return`；**不**回落 scoring | P7.1.2, P7.1.3, P7.1.4 | 单测：① 空队列直接返回；② 单条 delegate 跑通 + turn_id 一致；③ cap 撞顶不 pop 队首；④ speak 中段 cancel → `turn_end(cancelled)` + recorder 落一行（messages[].status="cancelled"）+ 队列剩余项保留；⑤ 队列空后**不回落 scoring**（mock `_run_ai_chain` 不被调）；⑥ `turn_start.data.scores` 含 winner 名；⑦ **正常完成单条 delegate 时 `_kick_detect_new_artifacts` 被调 1 次**（mock 验证 / 或写一个测：drain 前在 `tasks/<active>/artifacts/` 放一个新文件，drain 完成后断言 `task_artifact_added` envelope 已 emit）；⑧ **每个 turn 末尾只 emit 一帧 `turn_end`**（不再有 ai + user 两帧重叠）；⑨ **`turn_end` envelope 顺序在 `_kick_detect_new_artifacts` 触发的 `task_artifact_added` envelope 之前**（mock sink 记录 emit 顺序断言；与 `_run_ai_chain` 同口径） | `57040fb` |
| **P7.1.6** | `chahua/server.py`：① 加 `self._inflight_kind: Optional[Literal["user","handoff"]] = None` 与 `_inflight_turn_task` 同槽（反向评审 v2-#3 / v3-#1）；② **所有创建 `_run_turn` task 的入口都标 "user"**（反向评审 v4-#1）：`_inbound_user_message`（chahua/server.py:632）+ `_kick_synthesized_user_message`（chahua/server.py:444）；`_run_turn` wrapper finally 多清一行 `self._inflight_kind = None`（现状只清 `_inflight_turn_task`）；③ 加 `_inbound_handoff_delegate` / `_inbound_handoff_clear` handler + `_INBOUND_ROUTES` 表两行：payload 严格白名单（`{type, target, reason?}` 等，未知字段 → NOTICE error 丢帧）；④ **`_inbound_handoff_delegate` 条件性 cancel + 条件性启动 wrapper**（反向评审 v3-#1）：`if self._inflight_kind == "user": await self._cancel_and_drain_inflight()` → `snapshot = self._session.orchestrator.enqueue_handoff(item)` → `self._emit_handoff_envelope(sink, type=HANDOFF_ENQUEUED, data={"queue": [...]})` → `if self._inflight_turn_task is None: self._inflight_kind = "handoff"; self._inflight_turn_task = asyncio.create_task(self._run_handoff_turn(...))`；in-flight 是 handoff drain 时**不** cancel、**不**启 task（drain 内 while 自然消费新项）；⑤ **`_inbound_handoff_clear` 无差别**（反向评审 v3-#3 简化）：`await self._cancel_and_drain_inflight()` → `dropped = self._session.orchestrator.clear_handoff_queue()` → `self._emit_handoff_envelope(sink, type=HANDOFF_CLEARED, data={"items_dropped": [...]})`；clear 不挂 wrapper（队列已空，没有要 drain 的） | P7.1.5 | 单测：① 未知字段 → NOTICE error；② target 不存在 → NOTICE error 不入队；③ 合法 delegate inbound → `handoff_enqueued` envelope + `_inflight_turn_task` 非 None + `_inflight_kind == "handoff"`；④ handoff drain 中 `handoff_clear` → in-flight 被 cancel + `items_dropped` 含剩余；⑤ **handoff drain 中再 delegate B → 不 cancel 当前 + B append 队尾**（核心保护队列回归测，反向评审 v3-#1）；⑥ user-turn 跑期间 delegate → cancel user-turn + 启动 handoff drain；⑦ **synthesized user-turn（`_kick_synthesized_user_message`）跑期间 delegate → 也按 user-turn 路径走（cancel + 启动 handoff drain）**（反向评审 v4-#1 回归测；模拟一个 task handler 触发 synth turn + 紧跟 delegate）；⑧ 无 in-flight 时 `handoff_clear` 直接清队列不挂 task | `57040fb` |
| **P7.1.7** | `chahua/server.py` 加 `_run_handoff_turn(sink, *, task_id)` wrapper（照搬 `_run_turn`：try `await self._session.orchestrator.run_pending_handoff(...)` / except CancelledError swallow + `_log.info` / except Exception swallow + log.exception / finally **同槽清** `self._inflight_turn_task = None; self._inflight_kind = None`，反向评审 v2-#3）；inbound handler 第 5 步走它而不是直接 create_task | P7.1.6 | 单测：① 正常跑完 `_inflight_turn_task` 与 `_inflight_kind` 都被清；② 中途 cancel → swallow 不 reraise + 两槽都清；③ 内部异常 → swallow + log + 两槽都清；④ busy 判定 (`_inflight_turn_task is not None and not done()`) 与 user-turn 同口径 | `57040fb` |
| **P7.1.8** | 前端 `app/renderer/events.js` 加 3 个 `EventType` 常量（`HANDOFF_ENQUEUED` / `HANDOFF_CONSUMED` / `HANDOFF_CLEARED`） + `Inbound.HANDOFF_DELEGATE` / `HANDOFF_CLEAR`；`renderer.js` 收 `handoff_*` 时维护本地 `handoffQueueState`（数组 + group_id 分组）；刷新 / 切房时 reset | P7.1.6 | dev：F12 console 打 `__chahua_handoff_state` 能看到队列；切房后清零 | `519ad47` |
| **P7.1.9** | 茶客侧栏 ⇨ hover 按钮：每位茶客卡片右下角加 "⇨ 交给" 按钮（hover 才显）；点击 → 弹小 popover（可选**内部备注** textarea + 确认按钮）→ 发 `handoff_delegate` inbound；**handoff drain 跑期间不 disable**（反向评审 v4-#2：drain 中 delegate 应 append 到队尾、不抢占；UI disable 反而让"连点 N 次看队列"的回归路径走不通）；只在以下情况 disable + tooltip：① ws 断开（"连接断开"）；② target 不在 active_guests（"茶客不在场"）；③ popover 自身提交中（防双击）。**正常 tooltip 文案"加入队列"**（而不是"交给"），让用户知道点了会进队而不是立刻独占 | P7.1.8 | dev：开 2 茶客；点 A 的 ⇨ → 输入备注 → 提交 → A 下一句独占发言；**A 跑期间再点 B ⇨ → B 进队尾**（核心保护回归点，反向评审 v3-#1）；ws 断开时按钮灰掉 | `220cd1d` |
| **P7.1.10** | composer 上方队列预览小条：`handoffQueueState` 非空时显示 "➡️ 下一句：A（用户指派）"；多项依次显示 "A → B → C"；右侧 ✕ 按钮触发 `handoff_clear` inbound；空队列时小条整体隐藏不占位 | P7.1.9 | dev：连点 3 次 delegate 看小条；点 ✕ 看队列清空；用户发普通消息时小条不显（in-flight non-handoff） | `f58f936` |
| **P7.1.11** | 调试抽屉 turn 索引行复用 §6 已有的 `scoring_path` 字段渲染："scoring" / "mention" / "broadcast" 沿用既有图标，"handoff_delegate" 显示成 "[指派] {winner}"；详情面板里 trigger.kind="handoff" 时顶部加 "由用户指派" 提示条 | P7.1.6 (后端) + P7.1.8 (前端常量) | dev：跑 delegate 后调试抽屉看 turn 行；展开看 trigger 提示 | `9360fa0` |
| **P7.1.12** | doc 同步：① 更新 `CLAUDE.md` "关键不变量" 加 P7.1 几条（drain loop 严格分流不回落 scoring / wrapper swallow cancel 与 _run_turn 同口径 / cap 检查按 item cost 算）；② 本文件 §12 表里补 commit hash 列；③ `docs/CHANGELOG`（如有）加 P7.1 | 全部 P7.1.* | 跑 `uv run pytest` 全绿；dev Electron 走一遍：开任务 → 点 ⇨ 交给 A → A 独占发言 → 队列空后不接力 → 用户发话恢复群聊；中途点 ✕ 清队列；用户在 in-flight 时点取消按钮 cancel 整轮 | _本 PR_ |

**实际落地 commit 映射**（P7.1.12 回填）：PR1（P7.1.1~P7.1.7）按"同文件 / 同意图相邻改动"
合并为单 commit `57040fb`；PR2 每步独立 commit，P7.1.8~P7.1.10 各带一条 `/simplify` 跟进
（`194f403` / `b1a05c4` / `ed32823`）；合入后另有两条 codex-review 修复 `a89e70a`（handoff
队列只在真正切房时清）/ `1843964`（断线清 handoff 队列预览）。

**分批 PR 建议**：12 个 commit 分 3 个 PR 推：

- **PR 1（后端骨架）**：P7.1.1 ~ P7.1.7 —— 全部 server / orchestrator / debug 接入，UI 没动。
  合入后 ws 上能看到 `handoff_*` envelope 但前端忽略，user-turn 老路径完全不破。
- **PR 2（前端表面）**：P7.1.8 ~ P7.1.11 —— UI 出现：⇨ 按钮 + 队列预览 + 调试抽屉适配。
  PR 1 必须先合，否则前端发 `handoff_delegate` 服务端不识别走 WARN。
- **PR 3（doc + 闭环）**：P7.1.12 —— CLAUDE.md 不变量 / 表回填 / E2E walkthrough。

**P7.1 完结条件**：上面 12 个 commit 全部合入 + E2E walkthrough 通过 + CLAUDE.md 加 4 条不变量。
做完 P7.1 才动 P7.2 review，不抢跑（P7.2 改动面在 `<review_target>` 块注入 + UI 气泡按钮，
与 P7.1 解耦但**复用** drain loop + wrapper —— P7.1 的承重墙稳了才能让 P7.2 加 kind 不出乱）。
