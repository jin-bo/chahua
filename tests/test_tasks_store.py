"""tasks_store.TasksStore 单测。

P5.1 覆盖 §7.1 / §10 三类核心场景：
- ① 双向修复：state.json 缺 + 单 task.json → 自动恢复 active 并回写
- ② attach_artifact 是 copy 不是 move（share/ 原文件 stat 不变）

P5.2.1 改动（§7.2）：解除"一房间最多 1 任务"限制 —— ``open_task`` 不再抛
``TaskExistsError``，新建自动成为 active、旧 active 进历史列表。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chahua.tasks_store import (
    ArtifactSourceMissingError,
    TaskAlreadyClosedError,
    TaskNotFoundError,
    TasksStore,
)


def test_empty_room_starts_with_no_active(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    assert store.active_task_id is None
    assert store.list_tasks() == []
    assert store.get_active_task() is None


def test_open_task_sets_active_and_creates_dirs(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="写 README", goal="去年的没写完")
    assert store.active_task_id == t.id
    assert store.get_active_task() == t
    assert store.task_json_path(t.id).is_file()
    assert store.artifacts_dir(t.id).is_dir()
    # state.json 回写
    state = json.loads((tmp_path / "tasks" / "state.json").read_text(encoding="utf-8"))
    assert state["active_task_id"] == t.id


def test_open_task_allows_multiple_and_promotes_active(tmp_path: Path):
    """P5.2.1：连开 3 个任务全部成功，state.active 指向最新；旧 active 留 status="open"。"""
    store = TasksStore(room_dir=tmp_path)
    t1 = store.open_task(title="一", goal="...")
    assert store.active_task_id == t1.id
    t2 = store.open_task(title="二", goal="...")
    assert store.active_task_id == t2.id
    t3 = store.open_task(title="三", goal="...")
    assert store.active_task_id == t3.id
    # 旧任务都仍以 status="open" 存在（close 在 P5.2.2 才接）
    ids = {t.id for t in store.list_tasks()}
    assert ids == {t1.id, t2.id, t3.id}
    for tid in (t1.id, t2.id):
        assert store.get_task(tid).status == "open"
    # state.json 落盘指向最新
    state = json.loads((tmp_path / "tasks" / "state.json").read_text(encoding="utf-8"))
    assert state["active_task_id"] == t3.id


def test_open_task_state_lost_still_resets_active(tmp_path: Path):
    """state.json 被删后重装：单 task 仍被自动恢复为 active（§7.1 修复 ②，P5.2 沿用）。"""
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="一", goal="...")
    # 模拟 state.json 损坏：删掉它
    (tmp_path / "tasks" / "state.json").unlink()
    # 重新装配 store —— 此时 state.json 缺，task.json 仍在
    store2 = TasksStore(room_dir=tmp_path)
    # 双向修复 ②：单 task → 自动设 active
    assert store2.active_task_id == t.id
    # P5.2 起第二次 open 不再被拒
    t2 = store2.open_task(title="第二个", goal="...")
    assert store2.active_task_id == t2.id


def test_bidirectional_repair_state_points_to_missing_task(tmp_path: Path, caplog):
    """① state.json 指向不存在 task → 清回 None + 回写。"""
    (tmp_path / "tasks").mkdir(parents=True)
    (tmp_path / "tasks" / "state.json").write_text(
        json.dumps({"active_task_id": "task_ghost"}),
        encoding="utf-8",
    )
    store = TasksStore(room_dir=tmp_path)
    assert store.active_task_id is None
    state = json.loads((tmp_path / "tasks" / "state.json").read_text(encoding="utf-8"))
    assert state["active_task_id"] is None


def test_bidirectional_repair_state_empty_single_task(tmp_path: Path):
    """② state.json 缺 + 唯一 task → 自动恢复 active 并回写。"""
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="一", goal="...")
    # 删掉 state.json
    (tmp_path / "tasks" / "state.json").unlink()
    # 重装 store
    store2 = TasksStore(room_dir=tmp_path)
    assert store2.active_task_id == t.id
    # state.json 已回写
    state = json.loads((tmp_path / "tasks" / "state.json").read_text(encoding="utf-8"))
    assert state["active_task_id"] == t.id


def test_update_task_changes_title_and_goal(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="旧", goal="老目标")
    updated = store.update_task(t.id, title="新", goal="新目标")
    assert updated.title == "新"
    assert updated.goal == "新目标"
    assert updated.updated_at_ms >= t.updated_at_ms
    # owner / status 不变
    assert updated.owner == t.owner
    assert updated.status == "open"


def test_update_task_partial_patch(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="t", goal="g")
    updated = store.update_task(t.id, goal="g2")
    assert updated.title == "t"
    assert updated.goal == "g2"


def test_update_task_not_found(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    with pytest.raises(TaskNotFoundError):
        store.update_task("task_nope", title="x")


# ── P5.2.2: set_active / close_task / events.jsonl ─────────────────────────


def test_set_active_to_missing_task_raises(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    store.open_task(title="t", goal="g")
    with pytest.raises(TaskNotFoundError):
        store.set_active("task_ghost")


def test_set_active_to_same_is_noop(tmp_path: Path):
    """切换到当前 active：不写 state.json 也不发 events.jsonl 记录。"""
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="t", goal="g")
    # open_task 已经写过 became_active 一行
    events_before = store.list_events(t.id)
    store.set_active(t.id)
    events_after = store.list_events(t.id)
    assert events_after == events_before


def test_set_active_logs_both_sides(tmp_path: Path):
    """切换 A → B：A 落 became_inactive，B 落 became_active；state.json 指向 B。"""
    store = TasksStore(room_dir=tmp_path)
    a = store.open_task(title="A", goal="g")
    b = store.open_task(title="B", goal="g")  # 自动成为 active
    # open_task(b) 已经把 a 切走 + b 接上 —— 验证两边都有相应事件
    a_kinds = [e["kind"] for e in store.list_events(a.id)]
    b_kinds = [e["kind"] for e in store.list_events(b.id)]
    assert a_kinds == ["became_active", "became_inactive"]
    assert b_kinds == ["became_active"]
    # 显式切回 a，A 再次 became_active，B 落 became_inactive
    store.set_active(a.id)
    assert [e["kind"] for e in store.list_events(a.id)] == [
        "became_active", "became_inactive", "became_active",
    ]
    assert [e["kind"] for e in store.list_events(b.id)] == [
        "became_active", "became_inactive",
    ]


def test_set_active_to_none(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="t", goal="g")
    store.set_active(None)
    assert store.active_task_id is None
    state = json.loads((tmp_path / "tasks" / "state.json").read_text(encoding="utf-8"))
    assert state["active_task_id"] is None
    # events 行尾是 became_inactive
    kinds = [e["kind"] for e in store.list_events(t.id)]
    assert kinds[-1] == "became_inactive"


def test_close_task_done_sets_state_active_none(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="t", goal="g")
    closed = store.close_task(t.id, status="done")
    assert closed.status == "done"
    assert closed.closed_at_ms is not None
    # 当前 active 被关 → state.active = None
    assert store.active_task_id is None
    # events: became_active（open 时）→ closed → became_inactive
    kinds = [e["kind"] for e in store.list_events(t.id)]
    assert kinds == ["became_active", "closed", "became_inactive"]
    # closed event 带 status payload
    closed_event = next(e for e in store.list_events(t.id) if e["kind"] == "closed")
    assert closed_event["status"] == "done"


def test_close_task_abandoned(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="t", goal="g")
    store.close_task(t.id, status="abandoned")
    assert store.get_task(t.id).status == "abandoned"


def test_close_task_non_active_keeps_active(tmp_path: Path):
    """关闭非 active 任务 —— state.active 不变；只该任务落 closed 事件。"""
    store = TasksStore(room_dir=tmp_path)
    a = store.open_task(title="A", goal="g")
    b = store.open_task(title="B", goal="g")  # B 是 active
    store.close_task(a.id, status="done")
    assert store.active_task_id == b.id
    assert store.get_task(a.id).status == "done"
    # A 没 became_inactive（来自 close 自身），保留 open_task 时的 became_inactive（因为
    # 开 B 时把 A 切走）
    a_kinds = [e["kind"] for e in store.list_events(a.id)]
    assert a_kinds == ["became_active", "became_inactive", "closed"]
    # B 仍是 active —— events 只 became_active 一条
    assert [e["kind"] for e in store.list_events(b.id)] == ["became_active"]


def test_close_already_closed_raises(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="t", goal="g")
    store.close_task(t.id, status="done")
    with pytest.raises(TaskAlreadyClosedError):
        store.close_task(t.id, status="abandoned")


def test_close_task_rejects_non_terminal_status(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="t", goal="g")
    with pytest.raises(ValueError):
        store.close_task(t.id, status="in_progress")  # type: ignore[arg-type]


def test_close_task_not_found(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    with pytest.raises(TaskNotFoundError):
        store.close_task("task_ghost", status="done")


def test_events_jsonl_roundtrip_three_lines(tmp_path: Path):
    """① open A → ② open B（A 切走，B 上）→ ③ close B —— B 的 events.jsonl 3 行可读回。"""
    store = TasksStore(room_dir=tmp_path)
    store.open_task(title="A", goal="g")
    b = store.open_task(title="B", goal="g")
    store.close_task(b.id, status="done")
    # B 的 events：became_active（open）+ closed + became_inactive = 3 行
    events = store.list_events(b.id)
    assert len(events) == 3
    assert [e["kind"] for e in events] == ["became_active", "closed", "became_inactive"]
    # 重装 store 还能读回（落盘宽容）
    store2 = TasksStore(room_dir=tmp_path)
    events2 = store2.list_events(b.id)
    assert events2 == events


def test_add_decision_appends_jsonl(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="t", goal="g")
    d1 = store.add_decision(
        t.id, supporting_message_ids=["m1"], summary="d1"
    )
    d2 = store.add_decision(
        t.id, supporting_message_ids=["m2", "m3"], summary="d2"
    )
    listed = store.list_decisions(t.id)
    assert [x.decision_id for x in listed] == [d1.decision_id, d2.decision_id]
    assert listed[1].supporting_message_ids == ("m2", "m3")


def test_attach_artifact_is_copy_not_move(tmp_path: Path):
    """③ attach 后 share/ 原文件仍存在，目标 task artifacts/ 有副本。"""
    share = tmp_path / "share"
    share.mkdir()
    src = share / "README.md"
    src.write_text("hello", encoding="utf-8")
    src_stat_before = src.stat()

    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="t", goal="g")
    info = store.attach_artifact(
        t.id, share_rel="README.md", share_root=share
    )
    assert info["name"] == "README.md"
    assert info["size"] == 5
    assert info["rel"].endswith("/artifacts/README.md")

    # share/ 原文件未动
    assert src.is_file()
    assert src.read_text(encoding="utf-8") == "hello"
    assert src.stat().st_size == src_stat_before.st_size

    # task artifacts/ 有副本
    dst = store.artifacts_dir(t.id) / "README.md"
    assert dst.is_file()
    assert dst.read_text(encoding="utf-8") == "hello"


def test_attach_artifact_accepts_share_prefix(tmp_path: Path):
    """前端 upload 流 emit 的 rel 形如 "share/<name>"（与 FILE_UPLOADED.data.rel 同口径）；
    attach_artifact 应当接受这个形式，剥掉 ``share/`` 前缀后再与 share_root 拼接。"""
    share = tmp_path / "share"
    share.mkdir()
    (share / "README.md").write_text("hello", encoding="utf-8")
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="t", goal="g")
    info = store.attach_artifact(
        t.id, share_rel="share/README.md", share_root=share,
    )
    assert info["name"] == "README.md"
    assert (share / "README.md").is_file()  # 原文件不动


def test_attach_artifact_accepts_dot_share_prefix(tmp_path: Path):
    """茶客 cwd 里 share/ 是软链 —— ``./share/<name>`` 形式同样应当接受。"""
    share = tmp_path / "share"
    share.mkdir()
    (share / "README.md").write_text("hello", encoding="utf-8")
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="t", goal="g")
    info = store.attach_artifact(
        t.id, share_rel="./share/README.md", share_root=share,
    )
    assert info["name"] == "README.md"


def test_attach_artifact_rejects_symlink_escape(tmp_path: Path):
    """``..`` 已挡，但 share/ 里如果有 symlink 指向外部，shutil.copy2 / is_file 会
    跟随；resolve 后必须仍在 share_root 内。"""
    share = tmp_path / "share"
    share.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    # share/leak -> ../outside/secret.txt
    (share / "leak.txt").symlink_to(outside / "secret.txt")
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="t", goal="g")
    with pytest.raises(ArtifactSourceMissingError):
        store.attach_artifact(
            t.id, share_rel="leak.txt", share_root=share,
        )


def test_attach_artifact_rejects_path_traversal(tmp_path: Path):
    share = tmp_path / "share"
    share.mkdir()
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="t", goal="g")
    with pytest.raises(ArtifactSourceMissingError):
        store.attach_artifact(
            t.id, share_rel="../../etc/passwd", share_root=share
        )


def test_attach_artifact_rejects_absolute_path(tmp_path: Path):
    share = tmp_path / "share"
    share.mkdir()
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="t", goal="g")
    with pytest.raises(ArtifactSourceMissingError):
        store.attach_artifact(
            t.id, share_rel="/etc/passwd", share_root=share
        )


def test_attach_artifact_missing_source(tmp_path: Path):
    share = tmp_path / "share"
    share.mkdir()
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="t", goal="g")
    with pytest.raises(ArtifactSourceMissingError):
        store.attach_artifact(t.id, share_rel="nope.txt", share_root=share)


def test_list_artifacts_after_attach(tmp_path: Path):
    share = tmp_path / "share"
    share.mkdir()
    (share / "a.md").write_text("a", encoding="utf-8")
    (share / "b.md").write_text("bb", encoding="utf-8")
    store = TasksStore(room_dir=tmp_path)
    t = store.open_task(title="t", goal="g")
    store.attach_artifact(t.id, share_rel="a.md", share_root=share)
    store.attach_artifact(t.id, share_rel="b.md", share_root=share)
    arts = store.list_artifacts(t.id)
    assert [a["name"] for a in arts] == ["a.md", "b.md"]
    assert [a["size"] for a in arts] == [1, 2]
    # rel 必须与 attach_artifact 返回值同口径 —— task_info 是权威快照，前端从这里取路径。
    assert arts[0]["rel"] == f"tasks/{t.id}/artifacts/a.md"
    assert arts[1]["rel"] == f"tasks/{t.id}/artifacts/b.md"


def test_open_task_rolls_back_when_state_write_fails(tmp_path: Path, monkeypatch):
    """state.json 写失败 → 内存 + 盘都回滚到"未开"状态，便于重试 / 不污染后续 chat tag。"""
    store = TasksStore(room_dir=tmp_path)
    import chahua.tasks_store as ts_mod
    calls = {"n": 0}
    original = ts_mod.write_json_atomic

    def _fail_second(path, data):
        calls["n"] += 1
        # 第一次（写 task.json）成功；第二次（写 state.json）失败 —— 模拟 state.json 单独 fail
        if calls["n"] >= 2:
            raise OSError(28, "No space left on device")
        original(path, data)

    monkeypatch.setattr(ts_mod, "write_json_atomic", _fail_second)
    with pytest.raises(OSError):
        store.open_task(title="t", goal="g")
    # 回滚验证：内存空、盘上 tasks/<id>/ 已清、state.json 仍是 None。
    assert store.active_task_id is None
    assert store.list_tasks() == []
    state_file = tmp_path / "tasks" / "state.json"
    if state_file.is_file():
        import json
        assert json.loads(state_file.read_text(encoding="utf-8")).get("active_task_id") is None
    # 没有残留 task 目录
    leftover = [p for p in (tmp_path / "tasks").iterdir() if p.is_dir()]
    assert leftover == []


def test_open_task_rolls_back_when_task_json_write_fails(tmp_path: Path, monkeypatch):
    """task.json 写失败 → task dir 立即清，无僵尸目录残留导致后续 _load 误识。"""
    store = TasksStore(room_dir=tmp_path)
    import chahua.tasks_store as ts_mod

    def _always_fail(path, data):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(ts_mod, "write_json_atomic", _always_fail)
    with pytest.raises(OSError):
        store.open_task(title="t", goal="g")
    leftover = [p for p in (tmp_path / "tasks").iterdir() if p.is_dir()]
    assert leftover == []


def test_load_skips_invalid_task_dirs(tmp_path: Path, caplog):
    """坏 task.json 不让整个 store 起不来。"""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    bad = tasks_dir / "task_bad"
    bad.mkdir()
    (bad / "task.json").write_text("not json", encoding="utf-8")
    # 装一个合法 task
    good = tasks_dir / "task_good"
    good.mkdir()
    (good / "task.json").write_text(
        json.dumps({
            "id": "task_good", "title": "g", "goal": "g2",
            "status": "open", "owner": None,
            "created_at_ms": 1, "updated_at_ms": 1, "closed_at_ms": None,
        }),
        encoding="utf-8",
    )
    store = TasksStore(room_dir=tmp_path)
    ids = [t.id for t in store.list_tasks()]
    assert ids == ["task_good"]
