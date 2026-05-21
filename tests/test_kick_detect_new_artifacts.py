"""P5.4 茶客自动归集：``Orchestrator._kick_detect_new_artifacts`` 测试。

茶客通过 ``./task/`` 软链直接写文件到 ``tasks/<active>/artifacts/`` 后，
orchestrator 在每个 pick 周期末尾扫一次 diff，emit ``task_artifact_added`` hint
和 ``task_info`` 权威快照。

覆盖 10 类：
① active_task_id=None → no-op
② 无 tasks_store → no-op
③ closed task（done / abandoned）→ no-op
④ unknown task_id → no-op
⑤ boot 时已有 artifact → init seed 后首扫不 emit
⑥ 茶客新写文件 → emit hint（含 created_by=guest）+ task_info；P5.8 起不 room.append
⑦ 同一 task 多文件同时新增 → N 条 hint + 1 条 task_info
⑧ seen 同步：文件被删后再同名重建 → 第二次 emit
⑨ mark_seen 后 detect 不再 emit 该文件（P5.8 §5.4 防用户上传重复）
⑩ mark_seen 增量 add 不整组覆盖 —— 已 seen 的旧文件不被新 mark 牵连重发
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from chahua.cursor import GuestCursor
from chahua.events import ChahuaEnvelope, ChahuaEventType
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.tasks_store import TasksStore
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import NoopScorer, NoopSummarizer


def _build_orch(tmp_path: Path) -> tuple[Orchestrator, TasksStore, Room]:
    room = Room(name="t")
    room.add_participant(USER_SPEAKER_ID)
    room.add_participant("A")
    store = TasksStore(room_dir=tmp_path)
    orch = Orchestrator(
        room=room,
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
        scorer=NoopScorer(),
        summarizer=NoopSummarizer(),
        cursor=GuestCursor(),
        config=OrchestratorConfig(summary_block_size=999),
        tasks_store=store,
    )
    return orch, store, room


class _CapturingSink:
    """累积 sink。"""

    def __init__(self) -> None:
        self.envelopes: list[ChahuaEnvelope] = []

    def __call__(self, env: ChahuaEnvelope) -> None:
        self.envelopes.append(env)

    def types(self) -> list[str]:
        return [e.type.value for e in self.envelopes]

    def of_type(self, t: ChahuaEventType) -> list[ChahuaEnvelope]:
        return [e for e in self.envelopes if e.type == t]


def _write_artifact(store: TasksStore, task_id: str, name: str, content: bytes = b"x") -> Path:
    """模拟茶客直接往 ``./task/`` 软链对应的 ``artifacts/`` 目录写文件。"""
    p = store.artifacts_dir(task_id) / name
    p.write_bytes(content)
    return p


# ─── ① active_task_id=None → no-op ────────────────────────────────────────────


def test_no_active_task_no_op(tmp_path: Path):
    orch, store, _ = _build_orch(tmp_path)
    task = store.open_task(title="t", goal="g")
    _write_artifact(store, task.id, "foo.pdf")
    sink = _CapturingSink()
    orch._kick_detect_new_artifacts(sink, active_task_id=None)
    assert sink.envelopes == []


# ─── ② 无 tasks_store → no-op（防御性兜底）────────────────────────────────────


def test_no_tasks_store_no_op(tmp_path: Path):
    room = Room(name="t")
    room.add_participant(USER_SPEAKER_ID)
    orch = Orchestrator(
        room=room,
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
        scorer=NoopScorer(),
        summarizer=NoopSummarizer(),
        cursor=GuestCursor(),
        config=OrchestratorConfig(),
        tasks_store=None,
    )
    sink = _CapturingSink()
    orch._kick_detect_new_artifacts(sink, active_task_id="task_x")
    assert sink.envelopes == []


# ─── ③ closed task → no-op ────────────────────────────────────────────────────


def test_closed_task_no_op(tmp_path: Path):
    orch, store, _ = _build_orch(tmp_path)
    task = store.open_task(title="t", goal="g")
    _write_artifact(store, task.id, "foo.pdf")  # 在 close 之前写入
    store.close_task(task.id, status="done")
    sink = _CapturingSink()
    orch._kick_detect_new_artifacts(sink, active_task_id=task.id)
    assert sink.envelopes == []


# ─── ④ unknown task_id → no-op ────────────────────────────────────────────────


def test_unknown_task_id_no_op(tmp_path: Path):
    orch, _, _ = _build_orch(tmp_path)
    sink = _CapturingSink()
    orch._kick_detect_new_artifacts(sink, active_task_id="task_does_not_exist")
    assert sink.envelopes == []


# ─── ⑤ boot 时已有 artifact → init seed 后首扫不 emit ─────────────────────────


def test_boot_existing_artifacts_seeded_not_emitted(tmp_path: Path):
    """boot 路径：用户上次 attach 过的 artifact 留在盘上，新 session 启动后第一次
    扫不该把它当 'new' emit（启动时 task_info 已经把它给前端了）。"""
    room = Room(name="t")
    room.add_participant(USER_SPEAKER_ID)
    store = TasksStore(room_dir=tmp_path)
    task = store.open_task(title="t", goal="g")
    # 在 orchestrator init 之前写文件 —— 模拟"上次 session 落盘的产物"
    _write_artifact(store, task.id, "existing.pdf")
    # 现在才 init orchestrator —— __init__ 应该 seed _seen_artifacts 含 existing.pdf
    orch = Orchestrator(
        room=room,
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
        scorer=NoopScorer(),
        summarizer=NoopSummarizer(),
        cursor=GuestCursor(),
        config=OrchestratorConfig(),
        tasks_store=store,
    )
    assert "existing.pdf" in orch._seen_artifacts[task.id]
    sink = _CapturingSink()
    orch._kick_detect_new_artifacts(sink, active_task_id=task.id)
    assert sink.envelopes == []


# ─── ⑥ 茶客新写文件 → emit hint（含 created_by=guest）+ task_info ─────────────


def test_new_artifact_emits_hint_and_task_info(tmp_path: Path):
    orch, store, _ = _build_orch(tmp_path)
    task = store.open_task(title="审稿", goal="g")
    # init 时 task 没 artifact，seen[task.id] = {}
    # 茶客在 turn 内写文件
    payload = "# 审稿".encode("utf-8")
    _write_artifact(store, task.id, "审稿记录.md", content=payload)
    sink = _CapturingSink()
    orch._kick_detect_new_artifacts(sink, active_task_id=task.id)

    # 1 条 task_artifact_added hint
    added = sink.of_type(ChahuaEventType.TASK_ARTIFACT_ADDED)
    assert len(added) == 1
    assert added[0].data["task_id"] == task.id
    assert added[0].data["name"] == "审稿记录.md"
    assert added[0].data["created_by"] == "guest"
    assert added[0].data["rel"].endswith("审稿记录.md")
    assert added[0].data["size"] == len(payload)

    # 1 条 task_info 权威快照（紧跟在 hint 之后）
    infos = sink.of_type(ChahuaEventType.TASK_INFO)
    assert len(infos) == 1
    payload = infos[0].data
    assert payload["active_task_id"] == task.id
    # task_info 里那条 task 的 artifacts 列表包含新文件
    matching = [t for t in payload["tasks"] if t["id"] == task.id]
    assert len(matching) == 1
    names = [a["name"] for a in matching[0]["artifacts"]]
    assert "审稿记录.md" in names

    # 顺序：hint 在 task_info 之前
    assert sink.envelopes[0].type == ChahuaEventType.TASK_ARTIFACT_ADDED
    assert sink.envelopes[-1].type == ChahuaEventType.TASK_INFO

    # P5.8：detect 只 emit envelope，不再 room.append 合成 user 消息。
    assert orch.room.messages_since(0) == []

    # _seen_artifacts 已更新
    assert orch._seen_artifacts[task.id] == {"审稿记录.md"}

    # 第二次扫不再 emit（已 seen）
    sink2 = _CapturingSink()
    orch._kick_detect_new_artifacts(sink2, active_task_id=task.id)
    assert sink2.envelopes == []


# ─── ⑦ 多文件同时新增 → N 条 hint + 1 条 task_info ────────────────────────────


def test_multiple_new_artifacts_one_task_info(tmp_path: Path):
    orch, store, _ = _build_orch(tmp_path)
    task = store.open_task(title="审稿", goal="g")
    _write_artifact(store, task.id, "a.md")
    _write_artifact(store, task.id, "b.md")
    _write_artifact(store, task.id, "c.md")
    sink = _CapturingSink()
    orch._kick_detect_new_artifacts(sink, active_task_id=task.id)

    added = sink.of_type(ChahuaEventType.TASK_ARTIFACT_ADDED)
    assert len(added) == 3
    names = {e.data["name"] for e in added}
    assert names == {"a.md", "b.md", "c.md"}
    # 只一条 task_info 总刷新
    infos = sink.of_type(ChahuaEventType.TASK_INFO)
    assert len(infos) == 1


# ─── ⑧ seen 同步：文件被删后再同名重建 → 第二次 emit ──────────────────────────


def test_seen_resyncs_on_disk_deletion(tmp_path: Path):
    """文件 GC 后又同名重建是合法路径（茶客覆盖产物）。seen 同步到当前盘上状态
    保证下次重建会被识别为新增。"""
    orch, store, _ = _build_orch(tmp_path)
    task = store.open_task(title="t", goal="g")
    p = _write_artifact(store, task.id, "draft.md", content=b"v1")

    # 第一次：emit 一次
    sink = _CapturingSink()
    orch._kick_detect_new_artifacts(sink, active_task_id=task.id)
    assert len(sink.of_type(ChahuaEventType.TASK_ARTIFACT_ADDED)) == 1

    # 用户从盘上删 draft.md（GC / 手工清理），下次扫 seen 同步去掉它
    p.unlink()
    sink2 = _CapturingSink()
    orch._kick_detect_new_artifacts(sink2, active_task_id=task.id)
    # 没新文件 → 没 emit，但 seen 已不含 draft.md
    assert sink2.envelopes == []
    assert "draft.md" not in orch._seen_artifacts[task.id]

    # 茶客再次写同名 draft.md（不同内容）
    _write_artifact(store, task.id, "draft.md", content=b"v2-rewritten")
    sink3 = _CapturingSink()
    orch._kick_detect_new_artifacts(sink3, active_task_id=task.id)
    added = sink3.of_type(ChahuaEventType.TASK_ARTIFACT_ADDED)
    assert len(added) == 1
    assert added[0].data["name"] == "draft.md"
    assert added[0].data["size"] == len(b"v2-rewritten")


# ─── ⑨ mark_seen 后 detect 不再 emit 该文件 ──────────────────────────────────


def test_mark_seen_suppresses_emit(tmp_path: Path):
    """用户 attach_artifact 上传后服务端调 mark_seen 同步缓存 —— 下一轮 detect 不该把
    这个用户上传的文件当 new_names 重发 task_artifact_added（P5.8 §5.4 防重复 +
    防 created_by 错挂 guest）。"""
    orch, store, _ = _build_orch(tmp_path)
    task = store.open_task(title="t", goal="g")
    _write_artifact(store, task.id, "用户上传.md")
    orch._artifact_detector.mark_seen(task.id, "用户上传.md")
    sink = _CapturingSink()
    orch._kick_detect_new_artifacts(sink, active_task_id=task.id)
    assert sink.envelopes == []


# ─── ⑩ mark_seen 增量 add，不整组覆盖 ────────────────────────────────────────


def test_mark_seen_is_incremental(tmp_path: Path):
    """mark_seen 走 setdefault().add()，不能 self.seen[id]={name} 整组覆盖 —— 否则
    已 seen 的旧文件会因新 mark 丢失、下一轮被重新当新产物再发一轮。"""
    orch, store, _ = _build_orch(tmp_path)
    task = store.open_task(title="t", goal="g")
    _write_artifact(store, task.id, "旧.md")
    # 首扫记下 旧.md
    orch._kick_detect_new_artifacts(_CapturingSink(), active_task_id=task.id)
    assert orch._seen_artifacts[task.id] == {"旧.md"}
    # 用户上传 新.md → mark_seen 增量加入
    _write_artifact(store, task.id, "新.md")
    orch._artifact_detector.mark_seen(task.id, "新.md")
    assert orch._seen_artifacts[task.id] == {"旧.md", "新.md"}
    # 旧.md 与 新.md 都已 seen → detect 不 emit
    sink = _CapturingSink()
    orch._kick_detect_new_artifacts(sink, active_task_id=task.id)
    assert sink.envelopes == []
