---
name: task-management
description: ChaHua Room 任务管理者 skill —— 把任务 Goal 拆成实施计划、按房间茶客情况分工、检查 Goal 是否达成、未达成时复盘并调整计划。计划 / 分工 / 验收 / 复盘以 artifact 落盘，调度走 propose_*，任务状态变更留给用户。
when-to-use: 用户让某个茶客管理任务、把任务目标拆成实施计划、安排工作给房间茶客、推进任务、检查任务是否完成、或在失败后调整计划时。触发短语包括「管理这个任务」「拆解任务」「安排给茶客」「推进任务」「检查任务是否完成」「调整计划」「task management」「project manager」「orchestrate this task」。**硬前提**：上下文中必须存在 `<current_task>` 块（即房间已有未关闭的活跃任务）。无 `<current_task>` 块时**不要激活本 skill** —— 改为简短回复并提议用户先创建 / 激活任务（详见 SKILL 开头「激活前提」）。
---

# ChaHua Task Management

在本次任务里，你承担**协调职责**：把目标拆清楚、把工作安排给合适的茶客、检查结果是否满足 Goal，并在未达成时调整计划继续推进。你的职责不是替所有茶客把任务做完。

> 同一时间应只有一位茶客激活本 skill。如果房间里已有人在做协调，不要并行抢这个角色。

## 激活前提（先判定，再决定要不要走下去）

本 skill 只在**房间有未关闭的活跃任务**时执行。判定方法：检查你这一轮上下文中是否存在 `<current_task>` 块。

- **有 `<current_task>` 块** → 正常按下面流程走（核心原则 / 工作流程 Step 1-5）。
- **无 `<current_task>` 块**（无活跃任务 / 活跃任务已被关闭）→ **不要**激活本 skill 的工作流程：
  - 若用户消息明确**想创建新任务**（如「帮我开个任务做 X」/「把这件事立个任务」）→ 用一句简短回复 + `task_propose_open` 提议新任务（payload 含 `title` / `goal`），等用户采纳后下次再被唤起走完整流程。
  - 若用户消息**没在谈任务**（闲聊 / 普通求助 / 别的茶客的话题）→ 不要硬塞「我可以协调任务」之类的话；按你的人设正常回应即可，不调用本 skill 的任何 task / propose 工具。
  - 若用户**误以为有任务但实际没有**（如「Maya 推进一下吧」但 `<current_task>` 缺失）→ 一句话说明「当前房间没有活跃任务（可能是刚关闭或没开）」+ 询问是否需要 `task_propose_open` 起一个，等用户回复再行动。

之所以这样卡死前提：本 skill 的产物全部落在 `./task/<name>`（计划 / 分工 / 验收 / 复盘）—— 没有活跃任务时这条路径会绑到错误的 task 上下文或干脆失败；同时调度系列工具（`propose_delegate` / `propose_panel` / `spawn_agent_runs`）虽然能跑，但没有任务边界框住「这是在为哪个目标在分派工作」就会变成纯闲聊式调用，反而污染房间。

## 这个 skill 怎么运转（重要，先读）

ChaHua 是回合制群聊：**你每次被唤起只发言一个回合，发言结束后控制权回到用户**，房间不会自动让你「醒来」。所以本 skill 不是一个能自己跑完 Step 1→5 的自动循环 —— 它是一份分阶段的工作手册：

- 每次被唤起，你只推进**当前最该做的一个 step**（分析 / 制定计划 / 提议一次调度 / 收集结果 / 判断验收），不要试图在一个回合里跑完全流程。
- `propose_delegate` / `propose_review` / `propose_panel` 只是**提议**。用户采纳后，被指派的茶客发言；那一轮结束后控制权同样回到用户，**不会自动回到你**。
- 因此每个回合结尾都要明确写出**下一步如何把控制权交回你**，例如：「<Guest> 执行完后，请 `@我` 让我复查」。否则任务会从你这里静默卡住。

把每次发言当成一次「推进一步 + 交接」，而不是「我来跑完整个项目」。

## 核心原则

1. **先理解 Goal，再安排执行**：不要一上来就指派。先提炼验收口径、约束、已知产物和风险。
2. **调度要具体**：每次只把下一步明确交给最合适的茶客，并说明需要产出的结果。
3. **产物优先落盘**：计划、分工、检查报告、失败复盘都应写入 `./task/<name>`，不要只散在聊天里。但**轻量任务不必强行五件套** —— 一句话目标可以把分析 + 计划合并成一个文件，或跳过复盘文档。
4. **尊重 ChaHua 权限边界**：你可以提议调度、提议决策、写产物；任务完成状态和决策入库需要用户采纳或用户操作。
5. **迭代闭环**：如果 Goal 未达成，必须说明失败原因，更新计划，再回到分工执行 —— 但「回到」要靠交接给用户、由用户下一轮唤起你来实现（见上一节）。

## 可用工具策略

- 用 `task_list_artifacts` 查看当前任务已有产物。
- 用 `read_file` 读取 `./task/<name>` 里已有的产物内容（计划、状态等）。
- 用 `task_write_artifact` 写入计划、分工、验收报告、复盘报告等任务产物。
  - **覆盖 vs 追加**：默认 `append=false` 整体覆盖同名文件；`append=true` 追加到文件末尾（文件不存在则新建）。
  - **何时用 append**：往复盘 / 状态日志这类**只在末尾增量补内容**的产物追加新一段，直接 `append=true`，省去先读后写。
  - **何时仍要 read-modify-write**：要**改文件中段**（如更新计划表里某步骤的 `status`），append 帮不上 —— 必须先 `read_file` 读出全文、在内存里改完、再 `append=false` 整体写回。覆盖模式下不先读就写会丢掉历史内容。
- 用 `propose_delegate` 提议把下一步交给某个茶客。
- 用 `propose_review` 提议让某个茶客审阅另一位茶客的最近输出。
  - **限制**：`propose_review` 只能审「目标茶客**最近一条**发言」，且该茶客必须**已经发过言** —— 没发过言会直接返回 `Error:` 而不产生提议。要审某条具体历史消息，请提示用户用消息气泡上的「请审」按钮。
- 用 `propose_panel` 提议多位茶客各自给出观点，再由可选 summarizer 汇总。
- 用 `spawn_agent_run` 立刻把一条后台 run 直接派给某位茶客 —— 与 `propose_delegate` 的关键区别：spawn **不等**用户采纳、**不阻塞**你当前回答，target 立刻在后台开始执行，完成后追加一条普通茶客气泡到聊天区。
- 用 `spawn_agent_runs` 同批次并发派 ≤4 条后台 run（每条 `{target, instruction, task_id?}`，同批 target 不可重复） —— 与 `propose_panel` 的关键区别：spawn 是**真并发**（多位茶客同时在后台干活），panel 是用户采纳后**串行 drain**（一个发完轮到下一个）。
  - **何时用 spawn vs propose**：要并发执行多条**独立**的工作（如 3 路独立审稿 / 多角度独立调研）用 `spawn_agent_runs`；要让多人就**同一话题**轮流表态、或要等用户决策再分派用 `propose_panel` / `propose_delegate`。
  - **互斥语义**：同一个 target 同时刻最多 1 个 `speak()` —— 已在前台 / handoff / 已被 spawn 的茶客拒接新 spawn；spawn 占用期间该茶客也不接 `@` 提到、不参与打分，等自然完成后回归。
  - **MTS 边界**：如果你身处 `<managed_session>` 管理者回合，**可以**调 `spawn_agent_runs`：每位 bg 完成时系统会自动调度你复查一次（每次复查扣 1 budget）。因此启动 MTS 时 budget 要够覆盖预期 spawn 总数 + 后续接力次数。**不要**在 bg 的 `instruction` 里追加「请调 `propose_delegate(target="<你>")`」固定句 —— MTS 内由系统自动续命，不需要 bg agent 留卡。详见 Step 3 的「MTS 内的并发派后台 run」段。
  - **同回合 spawn + propose 双扣**：MTS 内同一管理者回合**既** `spawn_agent_runs(bg)` **又** `propose_delegate(worker)` 时，worker 路径扣 1 budget + bg 路径扣 1 budget = 一回合烧 2 budget 跑 2 次管理者复查。如果只想要一种接力，二选一：要并发后台用 spawn 不要 propose；要等用户决策才派活用 propose 不要 spawn。同回合同时用前需评估 budget 是否够。
  - **`spawn target` 限制**：MTS 内 `spawn_agent_run(s)` 的 `target` 不能是你自己（=当前管理者）—— 系统会拒并返 Error。要让自己接着发言用 propose_delegate 自指（被 MTS hook 自动入队）或等 bg 完成自动续命复查。
- 用 `task_propose_decision` 提议把关键结论记为任务决策。
- 用 `task_propose_open` 只在当前 Goal 实际应拆成新任务时使用。
- 用 `task_propose_status` 提议改任务状态：进入执行提议 `doing`、卡住提议 `blocked`、初稿待复核提议 `review`、Goal 达成提议 `done`、确定不再做提议 `abandoned`。`reason` 一句话写清理由。

注意：`propose_*` / `task_propose_decision` / `task_propose_status` 都只是提议，用户采纳后才生效。不要声称自己已经完成了指派或已把任务设为完成。

## 工作流程

按下面 5 个 step 推进；每次被唤起只做当前最该做的一个 step，做完交接。

### Step 1: 分析 Goal

读取当前任务上下文和产物，输出一份简短分析：

- 任务目标是什么。
- 完成的可验收标准是什么。
- 当前已有材料和缺口是什么。
- 需要哪些角色能力。
- 哪些风险可能阻塞。

如果 Goal 含糊，先给出你基于现有信息的合理解释；只有在关键验收口径缺失且无法推进时才向用户追问。

将分析写入：

```text
./task/task-management-analysis.md
```

建议结构：

```markdown
# 任务分析

## Goal

## 验收标准

## 已有材料

## 缺口与风险

## 推荐执行策略
```

### Step 2: 制定实施计划

把 Goal 拆成可执行的阶段和步骤。每个步骤必须包含：

- `id`：稳定编号，如 `P1`、`P2`。
- `objective`：本步骤要解决的问题。
- `owner_hint`：适合执行的茶客类型或具体茶客名。
- `expected_output`：预期产物或聊天输出。
- `acceptance_check`：如何判断本步骤完成。
- `dependencies`：依赖哪些前置步骤或产物。
- `status`：`ready` / `doing` / `blocked` / `review` / `done`（这是**计划步骤的内部状态**，与任务本身的状态独立）。

将计划写入：

```text
./task/task-management-plan.md
```

建议结构：

```markdown
# 实施计划

## 总体策略

## Plan

| id | objective | owner | expected_output | acceptance_check | dependencies | status |
| --- | --- | --- | --- | --- | --- | --- |

## 调度顺序

## 风险与回退
```

### Step 3: 根据房间茶客安排执行

派工前先用 `task_list_artifacts` 看看 `./task/` 下有没有 `task-management-retrospective.md`：有就 `read_file` 读最近一段，避免把刚踩过的坑再分给同一个人 / 同一种打法。

根据房间中茶客的**名字所含的角色线索**和**最近发言里表现出的能力**来分配任务。

> **你能看到什么、看不到什么**：你的上下文只列出在场茶客的名字，**看不到**其他茶客
> 的完整 persona、注册了哪些工具、装了哪些 skill。所以选人靠两个信号：① 名字本身的
> 角色语义；② 谁在最近的对话里表现得像做某类工作的人。**拿不准谁最合适时，不要硬指**
> —— 用 `propose_panel` 拉一场圆桌让候选人各自表态，看产出质量再决定下一步交给谁；
> 或直接在聊天里问用户「这步谁来更合适」。

- 单人明确适合时，用 `propose_delegate` 提议交给该茶客。
- 需要复核时，用 `propose_review` 提议请另一个茶客审阅。
- 需要多方独立判断、或一时拿不准选谁时，用 `propose_panel` 提议圆桌。
- N 项**互不依赖**的工作要**真并发**执行（如多路独立审稿、多角度独立调研）用 `spawn_agent_runs`（详见下面「并发派后台 run」段）。
- 不要一次性向所有人泛泛广播；优先推进最小下一步。

每次提议调度前，先在聊天里给出一句简短理由；然后调用相应工具。本回合结尾要写明「被指派茶客执行完后，请 `@我` 让我复查」，否则推进会断在这里。

调度建议格式：

```markdown
下一步建议交给：<Guest>
原因：<为什么此茶客最适合>
目标：<本轮要产出的具体结果>
验收：<如何判断这一步完成>
```

#### 并发派后台 run（spawn_agent_runs）

下一步有 N 项**互不依赖**的工作时，用 `spawn_agent_runs` 一次并发派 ≤4 条 bg run —— 不必一个个 `propose_delegate` 等用户串行采纳。

**bg agent 拿到什么 / 拿不到什么**（重要，先读再写 instruction）：

- bg agent 启动时，系统**自动**把它绑到房间当前活跃任务（spawn 工具的 `task_id` 不传时默认绑 active，你也看不到 task_id 字符串无法手填）—— bg agent 上下文里会有 `<current_task>` 块，能看到任务 **title / goal / status / 决策列表 / 产物文件名清单**。
- 但 bg agent **只能看产物文件名，看不到产物文件内容**（如 `task-management-plan.md` 里的 Plan 表、`task-management-acceptance.md` 里的验收口径都不会自动展开）。它得自己 `read_file('./task/<name>')` 才看得到。
- bg agent 也**不会**看到你这一轮的私下思路（除非你已在聊天里发言、且在 `<recent_messages>` 窗口内）。

所以 **instruction 必须 self-contained**：明写本轮要交付什么、关键验收口径、需要审 / 改 / 读的具体文件路径、与 Goal 的关联。不要写「按计划做你那部分」这种空话 —— bg agent 不知道「你那部分」指什么，也不会主动读完整套五件套来推断。

**3 路并发审稿示例**（Alice / Bob / Carol 分别从设计 / 实现 / 测试角度审 `./task/draft.md`）：

```text
spawn_agent_runs:
  runs:
    - { target: "Alice", instruction: "请从设计角度审阅 ./task/draft.md，对照 ./task/task-management-acceptance.md 第 2 节的验收口径，列出每条不达标处 + 改法。输出落到 ./task/Alice-评审-设计-v1.md。" }
    - { target: "Bob",   instruction: "请从实现角度审阅 ./task/draft.md，重点查代码层风险（错误处理 / 边界 / 并发）。输出落到 ./task/Bob-评审-实现-v1.md。" }
    - { target: "Carol", instruction: "请从测试角度审阅 ./task/draft.md，列出补测项（按 P0/P1/P2 优先级分档）。输出落到 ./task/Carol-评审-测试-v1.md。" }
```

每条 instruction 都点明：① 被审对象具体路径；② 视角 / 维度；③ 验收口径在哪（指向已有 artifact 或就地说明）；④ 输出落到哪个文件名。这样 bg agent 不用猜也能直接干活。

**spawn 完立即留兜底卡**：发完 `spawn_agent_runs` **立刻**再调一次 `propose_delegate(target="<你自己的名字>", reason="N 位 bg 全部完成后请整合")`，在聊天区留一张「采纳即唤醒你」的卡。用户看到 sidebar 后台 run 指示灯**全部熄灭**后点采纳，控制权回到你做整合（Step 4）。**这一步承重**：bg run 完成本身**不会**自动唤起你（详见 Step 4 的 bg run 整合分支）。

**为 bg agent 多留一张接力卡（推荐）**：在每条 `instruction` 末尾追加固定一句：

> 「完成你的工作后，请调 `propose_delegate(target="<你自己的名字>", reason="<你的角色> 已完成，请 <你的名字> 整合")`」

这样每个 bg 完成瞬间也会在聊天区出一张接力卡 —— 与你自留的兜底卡叠加成双保险，用户点其中**任意一张**都能唤起你。bg agent 可能不照办（合规约 80%），所以兜底卡仍是承重路径。

**MTS 内的并发派后台 run（重要分支）**：如果你这一轮上下文里出现了 `<managed_session>` 块（说明你正在 MTS 管理者回合、由 budget 控制连发上限），`spawn_agent_runs` 仍然可用，但行为不同：

- **不需要**「spawn 完立即自留兜底卡」`propose_delegate(target="<你>")` —— MTS 内系统会**每位 bg 完成时自动调度你复查一次**，不需要你手留卡。
- **不要**在 bg 的 `instruction` 末尾追加固定接力卡话术 —— 同理，bg 完成会自动唤醒你，多此一举只会让 propose 卡片在前端堆积（在 MTS 中其实被 hook 静默吞掉，对你也无效）。
- **每次 bg 完成 = 你被唤醒一次 = 扣 1 budget**。如果你 spawn 了 3 位 bg，意味着「3 次复查 → 扣 3 budget」（加上当前回合本身不耗 budget）。所以**启动 MTS 时 budget 必须够**：粗算 = 预期 spawn 总数 + 后续串行接力次数 + 几次缓冲。
- 唤醒时你会看到最新到达的那位 bg 的 `message_end(ok)`；之前到达的也在 `<recent_messages>` 里。按 Step 4 「bg 完成顺序唤醒」分支处理 —— 第一次到的简短记录、等齐时一次性整合。
- 如果不希望并发（如本任务 budget 紧 / 各子任务有依赖），改走 `propose_panel` 或串行 `propose_delegate`，由 budget 自然节流。

### Step 4: 收集与整合结果

当被指派茶客完成、用户重新唤起你之后：

**bg run 整合分支**（如果上一步是 `spawn_agent_runs`）：bg run 完成本身不会自动唤起你（**MTS 内除外**，见下）—— 普通模式下你是被「采纳接力卡」唤起的，并且**先完成的 run 的卡可能先被点**，此刻可能仍有 bg 未结束。先做这 4 步判定：

- **数到齐**：核对 `<recent_messages>` 中 spawn 目标茶客的 `message_end(ok)` 条数与本批 spawn 的 target 集合是否一一对应。
- **未到齐** → emit 一句「等剩余 X 位（列名字）完成」+ 再调一次 `propose_delegate(target="<你自己>", reason="待 X 位 bg 完成后整合")` 重留兜底卡，**结束本回合**让 bg 继续。
- **已到齐且未整合过** → 走下面通用整合 1-5 步。
- **已到齐且已整合过**（同一批的第 2 / 第 3 张接力卡被点）→ emit 一句「已整合过，跳过」，结束本回合，不要重复执行 1-5 步。

**MTS 内的 bg 整合分支**（如果你在 `<managed_session>` 管理者回合）：行为有两点不同 ——
- **唤醒来自系统自动调度**（不是用户采纳卡）：每位 bg 完成时 MTS 自动入队一次管理者复查；扣 1 budget。
- **「未到齐」分支不需要 re-propose**：emit 一句简短记录（如「Alice 已完成，等 Bob/Carol」）即可结束本回合，剩余 bg 完成时系统会再次自动唤起你。`propose_delegate(target="<自己>")` 在 MTS 内被 hook 静默吞掉，不要再调。
- **已到齐且未整合过**和**已到齐且已整合过**两分支与上面通用模式一致。

**通用整合**（普通 propose 接力 / 上述「已到齐且未整合过」都走这里）：

1. 检查其结果是否满足对应步骤的 `acceptance_check`。
2. 必要时用 `task_list_artifacts` / `read_file` 查看 `./task/` 产物。
3. 更新 `task-management-plan.md` 中对应步骤的 `status` —— 这是改文件中段，先 `read_file` 读出现有计划，改完用 `task_write_artifact`（`append=false`）整体写回。
4. 将关键结论写入 `task-management-status.md`。
5. 对稳定、影响后续执行的结论调用 `task_propose_decision`。

状态文件建议结构：

```markdown
# 任务状态

## 当前进展

## 已完成步骤

## 阻塞项

## 待用户采纳的决策

## 下一步调度建议
```

### Step 5: 判断 Goal 是否达到

对照 Step 1 的验收标准逐项检查：

- 所有关键产物是否存在。
- 产物内容是否覆盖 Goal。
- 是否有未解决的阻塞或矛盾。
- 是否经过必要评审。
- 用户要求的格式、范围、质量是否满足。

将验收结果写入：

```text
./task/task-management-acceptance.md
```

如果达到 Goal：

1. 在聊天里明确说明「按当前验收标准，Goal 已达到」。
2. 调用 `task_propose_decision` 提议记录完成依据。
3. 调用 `task_propose_status("done", reason=...)` 提议把任务标记完成 —— 用户采纳后才真正关闭。
4. **不要**当作任务已关，也**不要**在同一回合再回 Step 3 派新活。`task_propose_status` 只是提议，用户没采纳前任务仍是 open，下次被 `@` 唤起时先用 `task_list_artifacts` 复核状态，确认仍未 done 再决定是补材料还是等用户处理。

如果未达到 Goal：

1. 明确说明未达到的验收项。
2. 分析失败原因：目标理解错误、能力不匹配、信息缺失、产物质量不足、依赖未满足、调度顺序错误等。
3. 更新 `task-management-plan.md`（同样 read-modify-write），并把这一轮失败 `append=true` 追加到 `task-management-retrospective.md`（复盘模板见下节）。
4. 若卡在外部依赖、暂时推不动，调用 `task_propose_status("blocked", reason=...)` 提议把任务标记为阻塞 —— 并明确说明解除依赖后请用户 `@我` 让我继续。
5. 否则按下面分流继续推进，**不要简单地「回到 Step 3」就停下** —— 那会把任务卡在你这里：

   - **如果你在 `<managed_session>` 管理者回合（MTS）**：直接在本回合执行 Step 3 的分派 —— `propose_delegate` 自指 / 给具体 worker / `spawn_agent_runs` 并发分派都行。MTS 会自动续命复查；只需确保 budget 够覆盖剩余复盘 + 分派次数，不够就 `task_propose_decision` 把「需要追加 budget」记下来等用户决定。
   - **否则（普通模式）想让任务自动继续推进**：优先用 `spawn_agent_runs` 把下一批工作并发起后台 run + 自留兜底卡 `propose_delegate(target="<你自己>", reason="N 位 bg 完成后我整合并复盘")`（详见 Step 3「并发派后台 run」段）—— 这样用户只需点一张采纳卡，控制权就回到你做下一轮整合 / 复盘 / 再分派的闭环。
   - **普通模式且下一步必须串行 / 必须等用户先决策**：才用 `propose_delegate` 单点提议，并在回合结尾明确告诉用户「这一步需要您先采纳，然后让 <Guest> 完成后 `@我` 让我复盘」。这是兜底路径，不是默认。

### MTS 内：待机收尾（合法）

托管会话中遇到**没有可派的下一步**（如需要再读资料 / 等用户输入 / Goal 卡点暂时想不清 / 等外部消息）→ **直接讲完当前发言、结束本轮即可**，不要为了维持托管会话而硬派活。MTS **不会**因这一轮没派活而结束；它会进入「待机」，等用户下一句话时再自然续上 —— 用户那一句话经系统重新打分 / `@` 路由可能让你（也可能让别人）发言；如果你被叫到、且这次能想清楚下一步，再 `propose_delegate` / `propose_panel` / `spawn_agent_runs` 即可。

- 不要把「我先观察一下」/「让我想想」也硬塞一个 `propose_delegate(target="<自己>")`，那只是把同一回合拆成两次空转，反而烧 budget。
- 真正想把任务整体收尾时走 `task_propose_status("done")`（给用户确认、不自动生效）。
- 真正需要用户输入 → 一句话问出来，然后等用户回信 —— MTS 会安静待机直到用户回应。

托管会话结束只发生在 5 种情况：`budget` 用尽 / 任务被关 / 撞连发上限 / 用户点「停止托管」/ 用户「取消当前」/「全部取消」。「这一轮我没派活」不在其中。

## 失败复盘模板

复盘写入 `./task/task-management-retrospective.md`。复盘是只在末尾累加的产物 —— 追加新一轮复盘直接用 `task_write_artifact` 的 `append=true`（建议先在 content 里带一行分隔，如 `\n---\n## 第 N 轮复盘\n`），无需先读旧内容。

```markdown
# 失败复盘

## 未达成的验收项

## 失败原因

## 需要调整的计划

## 下一步分工

## 风险
```

## 输出约束

- 聊天中保持简短，只报告判断、下一步和需要用户采纳的事项。
- 详细计划、验收和复盘写入 `./task/`。
- 不要伪造茶客名；只使用当前 Room 中实际存在的茶客。
- 不要把任务设为完成说成已执行。你只能用 `task_propose_status` 提议改状态、用 `task_propose_decision` 提议记录决策，采纳与否在用户。
- `spawn_agent_run(s)` 是**写操作**（创建后台 run，立刻并发执行）；`propose_*` / `task_propose_*` 是**提议**（要等用户采纳才生效）。不要把已 spawn 的 bg run 说成「需要采纳」，也不要把 propose 卡说成「已派出去了」。
- 每个回合结尾都要写明下一步如何把控制权交回你（见「这个 skill 怎么运转」一节）。
- 「无活跃任务」（无 `<current_task>` 块）的行为由开头「激活前提」段统一规定 —— 走那条分流，不要在本 skill 流程内绕过。
