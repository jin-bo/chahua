"""P5.3.4：``chahua/task_tools.py`` 三工具 + register 工厂。

§13 P5.3.4 验收：
① 三工具 ``name`` / ``description`` / ``parameters`` 字段齐
② ``list_artifacts.execute()`` 拿 active 任务清单 → 返 markdown
③ ``propose_*.execute()`` 调 ``transport.emit_chahua`` 且 envelope.type = TASK_PROPOSAL
④ active=None 时 list_artifacts 返"无活跃任务"提示而非空
⑤ ``register_task_tools`` 调用后 ``agent.tools.tools`` 含三个 ``task_*``
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from chahua.events import ChahuaEnvelope, ChahuaEventType
from chahua.task_tools import (
    TaskListArtifactsTool,
    TaskProposeDecisionTool,
    TaskProposeOpenTool,
    register_task_tools,
)
from chahua.tasks_store import TasksStore


class _FakeTransport:
    """ChahuaTransport 的最小替身 —— 只暴露 task_tools 用到的三个属性 + emit_chahua。
    避开起真房间 / 真 sink 的成本，单测只验工具行为。"""

    def __init__(self, *, guest_name: str = "A", task_id: Optional[str] = None) -> None:
        self._guest_name = guest_name
        self._task_id = task_id
        self.emitted: list[tuple[ChahuaEventType, dict]] = []

    @property
    def guest_name(self) -> str:
        return self._guest_name

    @property
    def current_task_id(self) -> Optional[str]:
        return self._task_id

    def emit_chahua(self, type: ChahuaEventType, data: Optional[dict] = None, **kwargs) -> None:
        self.emitted.append((type, dict(data or {})))


class _FakeAgentTools:
    """``agent.tools`` 替身 —— 走 :meth:`register` 收一个 dict 就够了。"""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def register(self, tool) -> None:
        self.tools[tool.name] = tool


class _FakeAgent:
    def __init__(self) -> None:
        self.tools = _FakeAgentTools()


# ─── ① 三工具的元数据齐全 ───────────────────────────────────────────────────


def test_three_tools_metadata_complete(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    transport = _FakeTransport()
    tools = [
        TaskListArtifactsTool(tasks_store=store, transport=transport),
        TaskProposeDecisionTool(transport=transport),
        TaskProposeOpenTool(transport=transport),
    ]
    expected_names = {"task_list_artifacts", "task_propose_decision", "task_propose_open"}
    assert {t.name for t in tools} == expected_names
    for t in tools:
        assert isinstance(t.name, str) and t.name.startswith("task_")
        assert isinstance(t.description, str) and len(t.description) > 10
        params = t.parameters
        assert params["type"] == "object"
        assert "properties" in params
        assert params["additionalProperties"] is False
        assert t.is_read_only is True


def test_propose_decision_parameters_schema(tmp_path: Path):
    tool = TaskProposeDecisionTool(transport=_FakeTransport())
    p = tool.parameters
    assert "summary" in p["properties"]
    assert "supporting_message_ids" in p["properties"]
    assert p["required"] == ["summary"]


def test_propose_open_parameters_schema(tmp_path: Path):
    tool = TaskProposeOpenTool(transport=_FakeTransport())
    p = tool.parameters
    assert "title" in p["properties"]
    assert "goal" in p["properties"]
    assert set(p["required"]) == {"title", "goal"}


# ─── ② list_artifacts 拿清单 + markdown ─────────────────────────────────────


def test_list_artifacts_returns_markdown(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    task = store.open_task(title="t", goal="g")
    # 直接往 artifacts/ 写文件 —— attach_artifact 需要 share_root + share_rel
    # 这里只测 list_artifacts 的渲染，绕开 attach 的全套校验
    artifacts_dir = store.artifacts_dir(task.id)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "README.md").write_text("hello", encoding="utf-8")
    transport = _FakeTransport(task_id=task.id)
    tool = TaskListArtifactsTool(tasks_store=store, transport=transport)
    out = tool.execute()
    assert "README.md" in out
    assert "B" in out  # 5 字节 → 渲染成 "5 B"
    assert "./task/" in out


def test_list_artifacts_includes_mtime_and_size(tmp_path: Path):
    """list_artifacts 渲染与 _render_task_block 同口径：每行 `name (size, mtime)`。"""
    store = TasksStore(room_dir=tmp_path)
    task = store.open_task(title="t", goal="g")
    adir = store.artifacts_dir(task.id)
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "a.md").write_text("x", encoding="utf-8")
    tool = TaskListArtifactsTool(
        tasks_store=store,
        transport=_FakeTransport(task_id=task.id),
    )
    out = tool.execute()
    # 行内三段：name / size / mtime（YYYY-MM-DD HH:MM 形如 2026-）
    assert "a.md" in out
    assert "1 B" in out
    assert "20" in out  # 年份开头，避免 timezone 差异锁死


def test_list_artifacts_caps_at_ten(tmp_path: Path):
    """工具列 cap 10 + 多余条数显式回报 —— LLM 知道还有更多在 ./task/。"""
    store = TasksStore(room_dir=tmp_path)
    task = store.open_task(title="t", goal="g")
    adir = store.artifacts_dir(task.id)
    adir.mkdir(parents=True, exist_ok=True)
    for i in range(15):
        (adir / f"file{i:02d}.md").write_text("x", encoding="utf-8")
    tool = TaskListArtifactsTool(
        tasks_store=store,
        transport=_FakeTransport(task_id=task.id),
    )
    out = tool.execute()
    assert "file00.md" in out
    assert "file09.md" in out
    assert "file10.md" not in out  # 越过 cap
    assert "另有 5 个未列" in out


def test_list_artifacts_empty_task(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    task = store.open_task(title="t", goal="g")
    transport = _FakeTransport(task_id=task.id)
    tool = TaskListArtifactsTool(tasks_store=store, transport=transport)
    out = tool.execute()
    assert "暂无产物" in out


# ─── ③ propose_* emit envelope ──────────────────────────────────────────────


def test_propose_decision_emits_envelope(tmp_path: Path):
    transport = _FakeTransport(guest_name="汪小姐", task_id="task_abc")
    tool = TaskProposeDecisionTool(transport=transport)
    ack = tool.execute(summary="用 Electron 做", supporting_message_ids=["msg_1"])
    assert "已提议" in ack
    assert "用 Electron 做" in ack
    assert len(transport.emitted) == 1
    et, data = transport.emitted[0]
    assert et is ChahuaEventType.TASK_PROPOSAL
    assert data["proposer"] == "汪小姐"
    assert data["kind"] == "decision"
    assert data["payload"]["summary"] == "用 Electron 做"
    assert data["payload"]["supporting_message_ids"] == ["msg_1"]


def test_propose_decision_without_supporting(tmp_path: Path):
    transport = _FakeTransport()
    tool = TaskProposeDecisionTool(transport=transport)
    tool.execute(summary="仅 summary")
    _, data = transport.emitted[0]
    assert "supporting_message_ids" not in data["payload"]


def test_propose_open_emits_envelope(tmp_path: Path):
    transport = _FakeTransport(guest_name="A")
    tool = TaskProposeOpenTool(transport=transport)
    ack = tool.execute(title="新任务", goal="多行\ngoal")
    assert "新任务" in ack
    _, data = transport.emitted[0]
    assert data["kind"] == "open"
    assert data["payload"]["title"] == "新任务"
    assert data["payload"]["goal"] == "多行\ngoal"


def test_propose_ack_truncates_long_label(tmp_path: Path):
    """LLM ack 字符串里 summary / title 截到 60 字 —— 避免反喂大段自家 payload。"""
    transport = _FakeTransport()
    tool = TaskProposeDecisionTool(transport=transport)
    long = "决策" * 100  # 200 字
    ack = tool.execute(summary=long)
    assert "已提议「" in ack
    # ack 不会把 200 字全喂回去
    assert ack.count("决策") < 50


# ─── ④ active=None 时 list_artifacts 返提示 ─────────────────────────────────


def test_list_artifacts_no_active_returns_hint(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    transport = _FakeTransport(task_id=None)
    tool = TaskListArtifactsTool(tasks_store=store, transport=transport)
    out = tool.execute()
    assert "当前无活跃任务" in out


def test_list_artifacts_deleted_task_returns_hint(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    # transport 指向 task_id 但 store 里没这条
    transport = _FakeTransport(task_id="task_ghost")
    tool = TaskListArtifactsTool(tasks_store=store, transport=transport)
    out = tool.execute()
    assert "不存在" in out
    assert "task_ghost" in out


# ─── ⑤ register_task_tools 装 3 个 ──────────────────────────────────────────


def test_register_task_tools_installs_three(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    transport = _FakeTransport()
    agent = _FakeAgent()
    register_task_tools(agent, tasks_store=store, transport=transport)
    names = set(agent.tools.tools.keys())
    assert names == {"task_list_artifacts", "task_propose_decision", "task_propose_open"}
