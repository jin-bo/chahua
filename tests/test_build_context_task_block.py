"""P5.3.2：`_build_context_for(*, task_id)` wiring + 两路径 task block 注入。

§13 P5.3.2 验收 7 条 × 路径变体 = 10 个用例：
① 开任务 + onboarding 路径 → prompt 含完整块
② 同任务 + incremental 路径 → prompt 含 compact 块
③ task_id=None → 两路径各一个用例
④ closed task（done / abandoned）→ 两路径各一个用例
⑤ task_id 指向已删除任务 → 一个用例（两路径同源逻辑）
⑥ 拼 prompt 时改 store.active 不影响本轮（snapshot 隔离）
⑦ 房间级 summaries 仍在 onboarding 输出里（有 / 无 task 两条变体）

不测纯 renderer 的字符串细节（那是 P5.3.1 测试的领域）—— 这里只测 wiring：
是否调 renderer / 是否注入 / snapshot 隔离 / 房间级摘要不变。
"""

from __future__ import annotations

from pathlib import Path

from chahua.cursor import GuestCursor
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.summarizer import SummarySpan
from chahua.tasks_store import TasksStore
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import NoopScorer, NoopSummarizer


def _build_orch_with_store(
    tmp_path: Path,
    *,
    summaries: list[SummarySpan] | None = None,
) -> tuple[Orchestrator, TasksStore, Room]:
    """组一只接 TasksStore 的 Orchestrator + 房间 + store —— 与 conftest.build_orch
    分开，因为后者不接 store 且按 register(StubGuest) 装茶客（这里只需 participants
    名字进 _display_map，不需要 speak 路径）。"""
    room = Room(name="t")
    room.add_participant(USER_SPEAKER_ID)
    store = TasksStore(room_dir=tmp_path)
    orch = Orchestrator(
        room=room,
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
        scorer=NoopScorer(),
        summarizer=NoopSummarizer(summaries=summaries),
        cursor=GuestCursor(),
        config=OrchestratorConfig(
            onboarding_threshold=20,
            summary_block_size=999,
        ),
        tasks_store=store,
    )
    room.add_participant("A")
    room.add_participant("B")
    return orch, store, room


# ─── ① 开任务 + onboarding 路径 → prompt 含完整块 ────────────────────────────


def test_onboarding_with_task_id_includes_full_block(tmp_path: Path):
    orch, store, room = _build_orch_with_store(tmp_path)
    task = store.open_task(title="写 README", goal="把 README 写完", owner="A")
    # last_seen=0 触发 onboarding
    ctx = orch._build_context_for("A", task_id=task.id)
    assert '<room name="t">' in ctx
    assert "</room>" in ctx
    assert '<current_task status="未开始">' in ctx
    assert "</current_task>" in ctx
    assert "标题：写 README" in ctx
    assert "目标：" in ctx and "把 README 写完" in ctx
    assert "负责人：A" in ctx
    # P5.4：full 模式无 artifact 时仍告诉茶客 ./task/ 可读写 + 怎么贡献产物
    assert "./task/" in ctx
    assert "可读写" in ctx
    assert "当前为空" in ctx
    assert "自动入任务" in ctx


# ─── ② 同任务 + incremental 路径 → prompt 含 compact 块 ──────────────────────


def test_incremental_with_task_id_includes_compact_block(tmp_path: Path):
    orch, store, room = _build_orch_with_store(tmp_path)
    task = store.open_task(title="写 README", goal="把 README 写完", owner="A")
    # cursor 推到 latest_seq 后再发一条增量消息 → last_seen != 0 + 增量未超阈值
    room.append(USER_SPEAKER_ID, "先聊一句")
    orch.cursor.set("A", room.latest_seq)  # 让 A 把 seq=1 看过
    room.append(USER_SPEAKER_ID, "再聊一句")  # 增量 1 条 → incremental
    ctx = orch._build_context_for("A", task_id=task.id)
    assert '<room_update name="t">' in ctx
    assert "</room_update>" in ctx
    assert '<current_task status="未开始">' in ctx
    assert "标题：写 README" in ctx
    assert "目标：把 README 写完" in ctx
    # P5.4：compact 模式告诉茶客 ./task/ 可读写 + 自动入任务
    assert "./task/" in ctx
    assert "可读写" in ctx
    assert "自动入任务" in ctx
    # compact 不带 full 块小标题
    assert "近期决策" not in ctx
    # status 走 XML 属性，body 里不再含"状态："行
    assert "状态：" not in ctx


# ─── ③ task_id=None → 两路径都不含 task 块 ────────────────────────────────────


def test_task_id_none_onboarding_no_task_block(tmp_path: Path):
    orch, store, _ = _build_orch_with_store(tmp_path)
    store.open_task(title="开了的任务", goal="g")
    ctx = orch._build_context_for("A", task_id=None)
    # store 里虽然有任务，但本轮没 snapshot 到 → 不注入
    assert "开了的任务" not in ctx
    assert "<current_task" not in ctx
    assert "./task/" not in ctx


def test_task_id_none_incremental_no_task_block(tmp_path: Path):
    orch, store, room = _build_orch_with_store(tmp_path)
    store.open_task(title="开了的任务", goal="g")
    room.append(USER_SPEAKER_ID, "前情")
    orch.cursor.set("A", room.latest_seq)
    room.append(USER_SPEAKER_ID, "后情")
    ctx = orch._build_context_for("A", task_id=None)
    assert "开了的任务" not in ctx
    assert "<current_task" not in ctx


# ─── ④ closed task（done / abandoned）→ 两路径都不含 task 块 ────────────────


def test_closed_task_onboarding_skips_block(tmp_path: Path):
    orch, store, _ = _build_orch_with_store(tmp_path)
    task = store.open_task(title="已完成的", goal="g", owner="A")
    store.close_task(task.id, status="done")
    ctx = orch._build_context_for("A", task_id=task.id)
    assert "已完成的" not in ctx
    assert "<current_task" not in ctx


def test_abandoned_task_incremental_skips_block(tmp_path: Path):
    orch, store, room = _build_orch_with_store(tmp_path)
    task = store.open_task(title="已放弃的", goal="g", owner="A")
    store.close_task(task.id, status="abandoned")
    room.append(USER_SPEAKER_ID, "x")
    orch.cursor.set("A", room.latest_seq)
    room.append(USER_SPEAKER_ID, "y")
    ctx = orch._build_context_for("A", task_id=task.id)
    assert "已放弃的" not in ctx
    assert "<current_task" not in ctx


# ─── ⑤ task_id 指向已删除任务 → 两路径都不含 task 块 ─────────────────────────


def test_unknown_task_id_skips_block(tmp_path: Path):
    orch, store, _ = _build_orch_with_store(tmp_path)
    ctx = orch._build_context_for("A", task_id="task_nonexistent")
    assert "<current_task" not in ctx
    assert "./task/" not in ctx


# ─── ⑥ snapshot 隔离：拼 prompt 时改 store.active 不影响本轮 ──────────────────


def test_snapshot_task_id_isolation(tmp_path: Path):
    """`_build_context_for(task_id=t1.id)` 不读 store.active —— 中途切到 t2 也按 t1 渲染。"""
    orch, store, _ = _build_orch_with_store(tmp_path)
    t1 = store.open_task(title="任务一", goal="目标一")
    t2 = store.open_task(title="任务二", goal="目标二")
    # store 当前 active 是 t2（open_task 自动 set_active），但我们传 t1.id snapshot
    assert store.active_task_id == t2.id
    ctx = orch._build_context_for("A", task_id=t1.id)
    assert "标题：任务一" in ctx
    assert "任务二" not in ctx
    assert "目标一" in ctx
    assert "目标二" not in ctx


# ─── ⑦ 房间级 summaries 仍在 onboarding 输出里 ───────────────────────────────


def test_room_summaries_preserved_in_onboarding(tmp_path: Path):
    """task 块落在 ``<room_summary>`` 之后 / ``<recent_messages>`` 之前；
    房间级摘要渲染逻辑不变（XML 化后包在 ``<room_summary>`` 标签里）。"""
    summaries = [
        SummarySpan(start_seq=1, end_seq=10, text="- 老金在群里招呼了一声"),
        SummarySpan(start_seq=11, end_seq=20, text="- A 说今天聊 README"),
    ]
    orch, store, _ = _build_orch_with_store(tmp_path, summaries=summaries)
    task = store.open_task(title="写 README", goal="g")
    ctx = orch._build_context_for("A", task_id=task.id)
    # 房间级摘要段仍在
    assert "<room_summary>" in ctx and "</room_summary>" in ctx
    assert "老金在群里招呼了一声" in ctx
    assert "A 说今天聊 README" in ctx
    # task 块也在
    assert "标题：写 README" in ctx
    # 顺序：<room_summary> 在 <current_task> 之前
    assert ctx.index("<room_summary>") < ctx.index("<current_task")


def test_room_summaries_preserved_when_no_task(tmp_path: Path):
    """task_id=None 不影响房间级 summaries 渲染。"""
    summaries = [SummarySpan(start_seq=1, end_seq=10, text="- 一些梗概")]
    orch, _, _ = _build_orch_with_store(tmp_path, summaries=summaries)
    ctx = orch._build_context_for("A", task_id=None)
    assert "<room_summary>" in ctx
    assert "一些梗概" in ctx
    assert "<current_task" not in ctx
