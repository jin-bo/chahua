"""``/clear task`` 与 ``clear_task_artifacts`` inbound 回归（2026-05-19）。

口径（CLAUDE.md "``/clear task`` 仅清单任务产物" 不变量）：
- 只删 ``tasks/<id>/artifacts/`` 下可见文件；``task.json`` / ``decisions.jsonl`` /
  ``summary.jsonl`` / ``events.jsonl`` / 状态 / 摘要游标都不动。
- 同步重置 ``ArtifactDetector.seen[task_id] = set()``，避免下一轮 detect 走多余的
  ``removed_names`` 分支。
- 落 ``events.jsonl`` ``artifacts_cleared{count, names}`` audit 行。
- ``transcript`` 通过 ``_kick_synthesized_user_message`` 追一条"用户清空了产物"
  让茶客看见；inbound 端 emit ``task_info`` 权威快照。
"""

from __future__ import annotations

import pytest

from chahua.events import ChahuaEventType
from chahua.server_inbound_task import INBOUND_CLEAR_TASK_ARTIFACTS
from chahua.tasks_store import EVENT_KIND_ARTIFACTS_CLEARED


@pytest.fixture
def srv_with_task(task_inbound_srv):
    """复用 ``task_inbound_srv`` 夹具：装真房间 + monkeypatch 掉 AI 链。"""
    session, srv = task_inbound_srv
    task = session.tasks_store.open_task(title="评审 PR-123", goal="跑 review")
    yield session, srv, task


def _write_fake_artifact(session, task_id: str, name: str, body: bytes = b"x") -> None:
    adir = session.tasks_store.artifacts_dir(task_id)
    adir.mkdir(parents=True, exist_ok=True)
    (adir / name).write_bytes(body)


async def test_clear_task_artifacts_removes_files_and_keeps_task(srv_with_task):
    session, srv, task = srv_with_task
    _write_fake_artifact(session, task.id, "评审-v1.md", b"hello")
    _write_fake_artifact(session, task.id, "评审-v2.md", b"world")
    decision = session.tasks_store.add_decision(
        task.id, supporting_message_ids=[], summary="先按 v1 提交",
    )
    assert len(session.tasks_store.list_artifacts(task.id)) == 2

    captured = []
    await srv._handle_inbound(
        {"type": INBOUND_CLEAR_TASK_ARTIFACTS, "task_id": task.id},
        lambda env: captured.append(env.to_dict()),
    )

    # artifacts/ 目录被删空。
    assert session.tasks_store.list_artifacts(task.id) == []
    # 但 task.json 还在（task 本身 / 决策不动）。
    assert session.tasks_store.get_task(task.id) is not None
    decisions = session.tasks_store.list_decisions(task.id)
    assert len(decisions) == 1
    assert decisions[0].decision_id == decision.decision_id

    # task_info 权威快照里这个任务的 artifacts 应当也是空。
    infos = [e for e in captured if e["type"] == ChahuaEventType.TASK_INFO.value]
    assert infos, "应该 emit 一帧 task_info"
    tasks_payload = infos[-1]["data"]["tasks"]
    target = next(t for t in tasks_payload if t["id"] == task.id)
    assert target["artifacts"] == []


async def test_clear_task_artifacts_writes_audit_event(srv_with_task):
    session, srv, task = srv_with_task
    _write_fake_artifact(session, task.id, "a.md")
    _write_fake_artifact(session, task.id, "b.md")

    await srv._handle_inbound(
        {"type": INBOUND_CLEAR_TASK_ARTIFACTS, "task_id": task.id},
        lambda _env: None,
    )

    events = session.tasks_store.list_events(task.id)
    cleared = [e for e in events if e["kind"] == EVENT_KIND_ARTIFACTS_CLEARED]
    assert len(cleared) == 1
    assert cleared[0]["count"] == 2
    assert sorted(cleared[0]["names"]) == ["a.md", "b.md"]


async def test_clear_task_artifacts_resets_detector_seen(srv_with_task):
    """detector seen 缓存必须重置 —— 否则下一轮 detect 会触发多余 removed_names 路径。"""
    session, srv, task = srv_with_task
    _write_fake_artifact(session, task.id, "leak.md")
    # 手工把"上轮扫到过"灌进缓存（模拟茶客在 boot 后写了一文件，detector 已记录过）。
    session.orchestrator._artifact_detector.seen[task.id] = {"leak.md"}

    await srv._handle_inbound(
        {"type": INBOUND_CLEAR_TASK_ARTIFACTS, "task_id": task.id},
        lambda _env: None,
    )

    seen = session.orchestrator._artifact_detector.seen.get(task.id)
    assert seen == set()


async def test_clear_task_artifacts_missing_task_emits_notice(srv_with_task):
    session, srv, _task = srv_with_task
    captured = []
    await srv._handle_inbound(
        {"type": INBOUND_CLEAR_TASK_ARTIFACTS, "task_id": "task_ghost"},
        lambda env: captured.append(env.to_dict()),
    )
    notices = [e for e in captured if e["type"] == ChahuaEventType.NOTICE.value]
    assert any("不存在" in n["data"].get("text", "") for n in notices), notices


async def test_clear_task_artifacts_unknown_key_rejected(srv_with_task):
    session, srv, task = srv_with_task
    captured = []
    await srv._handle_inbound(
        {
            "type": INBOUND_CLEAR_TASK_ARTIFACTS,
            "task_id": task.id,
            "bogus": "yes",
        },
        lambda env: captured.append(env.to_dict()),
    )
    notices = [e for e in captured if e["type"] == ChahuaEventType.NOTICE.value]
    assert any("未知字段" in n["data"].get("text", "") for n in notices), notices
    # 入站严格 —— 帧被丢弃，artifacts 不该被动过。
    _write_fake_artifact(session, task.id, "still-here.md")
    assert any(a["name"] == "still-here.md" for a in session.tasks_store.list_artifacts(task.id))


def test_store_clear_artifacts_skips_metadata_files(env_paths):
    """``.DS_Store`` 这类 OS 元数据文件不该被 unlink —— 与 list_artifacts 同口径。"""
    from chahua import admin
    from chahua.session import build_room_session

    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[{"persona": "chahua/personas/宝总.md", "name": "宝总"}],
    )
    session = build_room_session(rc.room_dir, env_paths)
    try:
        task = session.tasks_store.open_task(title="t", goal="g")
        adir = session.tasks_store.artifacts_dir(task.id)
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "real.md").write_text("x", encoding="utf-8")
        (adir / ".DS_Store").write_bytes(b"junk")
        (adir / "._companion").write_bytes(b"junk")

        deleted = session.tasks_store.clear_artifacts(task.id)
        assert deleted == ["real.md"]
        # 元数据文件被保留。
        assert (adir / ".DS_Store").exists()
        assert (adir / "._companion").exists()
    finally:
        session.close()
