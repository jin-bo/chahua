"""``chahua/task_tools.py`` 四工具 + register 工厂。

验收：
① 四工具 ``name`` / ``description`` / ``parameters`` 字段齐
② ``list_artifacts.execute()`` 拿 active 任务清单 → 返 markdown
③ ``propose_*.execute()`` 调 ``transport.emit_chahua`` 且 envelope.type = TASK_PROPOSAL
④ active=None 时 list_artifacts 返"无活跃任务"提示而非空
⑤ ``register_task_tools`` 调用后 ``agent.tools.tools`` 含四个 ``task_*``
⑥ ``task_write_artifact`` happy path 写入 + name 校验 + active/closed 守卫
   （绕开 agentao PathPolicy 的核心动机 —— 详见 ``test_task_link_write_path_policy.py``）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from chahua.events import ChahuaEnvelope, ChahuaEventType
from chahua.task_tools import (
    TaskListArtifactsTool,
    TaskProposeDecisionTool,
    TaskProposeOpenTool,
    TaskWriteArtifactTool,
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


def test_four_tools_metadata_complete(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    transport = _FakeTransport()
    tools = [
        TaskListArtifactsTool(tasks_store=store, transport=transport),
        TaskProposeDecisionTool(transport=transport),
        TaskProposeOpenTool(transport=transport),
        TaskWriteArtifactTool(tasks_store=store, transport=transport),
    ]
    expected_names = {
        "task_list_artifacts",
        "task_propose_decision",
        "task_propose_open",
        "task_write_artifact",
    }
    assert {t.name for t in tools} == expected_names
    for t in tools:
        assert isinstance(t.name, str) and t.name.startswith("task_")
        assert isinstance(t.description, str) and len(t.description) > 10
        params = t.parameters
        assert params["type"] == "object"
        assert "properties" in params
        assert params["additionalProperties"] is False
    # task_write_artifact 是写工具，is_read_only=False；其余三个 read-only 工具 True
    read_only_map = {t.name: t.is_read_only for t in tools}
    assert read_only_map == {
        "task_list_artifacts": True,
        "task_propose_decision": True,
        "task_propose_open": True,
        "task_write_artifact": False,
    }


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


# ─── ⑤ register_task_tools 装 4 个 ──────────────────────────────────────────


def test_register_task_tools_installs_four(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    transport = _FakeTransport()
    agent = _FakeAgent()
    register_task_tools(agent, tasks_store=store, transport=transport)
    names = set(agent.tools.tools.keys())
    assert names == {
        "task_list_artifacts",
        "task_propose_decision",
        "task_propose_open",
        "task_write_artifact",
    }


# ─── ⑥ task_write_artifact：happy path / name 校验 / 守卫 ──────────────────


def test_write_artifact_happy_path_writes_to_artifacts_dir(tmp_path: Path):
    """正常写入：内容落到 ``tasks/<id>/artifacts/<name>``，返回成功消息含字节数。"""
    store = TasksStore(room_dir=tmp_path)
    task = store.open_task(title="审稿", goal="g")
    tool = TaskWriteArtifactTool(
        tasks_store=store,
        transport=_FakeTransport(task_id=task.id),
    )
    out = tool.execute(name="水文献-评审.md", content="## Research Gap\n不成立")
    assert "Successfully" in out
    assert "./task/水文献-评审.md" in out
    # 文件真落到 artifacts/
    written = store.artifacts_dir(task.id) / "水文献-评审.md"
    assert written.is_file()
    assert written.read_text(encoding="utf-8") == "## Research Gap\n不成立"


def test_write_artifact_overwrites_existing(tmp_path: Path):
    """同名 → 覆盖（与 ``attach_artifact`` 同口径）。"""
    store = TasksStore(room_dir=tmp_path)
    task = store.open_task(title="t", goal="g")
    tool = TaskWriteArtifactTool(
        tasks_store=store,
        transport=_FakeTransport(task_id=task.id),
    )
    tool.execute(name="report.md", content="v1")
    out = tool.execute(name="report.md", content="v2")
    assert "Successfully" in out
    assert (store.artifacts_dir(task.id) / "report.md").read_text(encoding="utf-8") == "v2"


def test_write_artifact_no_active_task_returns_error(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    tool = TaskWriteArtifactTool(
        tasks_store=store,
        transport=_FakeTransport(task_id=None),
    )
    out = tool.execute(name="x.md", content="hi")
    assert out.startswith("Error:")
    assert "无活跃任务" in out


def test_write_artifact_deleted_task_returns_error(tmp_path: Path):
    """transport 指向 task_id 但 store 里没——返 ``不存在`` 错误。"""
    store = TasksStore(room_dir=tmp_path)
    tool = TaskWriteArtifactTool(
        tasks_store=store,
        transport=_FakeTransport(task_id="task_ghost"),
    )
    out = tool.execute(name="x.md", content="hi")
    assert out.startswith("Error:")
    assert "task_ghost" in out
    assert "不存在" in out


def test_write_artifact_closed_task_returns_error(tmp_path: Path):
    """任务已 done / abandoned → 不允许写新产物。"""
    store = TasksStore(room_dir=tmp_path)
    task = store.open_task(title="t", goal="g")
    store.close_task(task.id, status="done")
    tool = TaskWriteArtifactTool(
        tasks_store=store,
        transport=_FakeTransport(task_id=task.id),
    )
    out = tool.execute(name="x.md", content="hi")
    assert out.startswith("Error:")
    assert "done" in out


def test_write_artifact_rejects_path_separator(tmp_path: Path):
    """name 含 ``/`` 拒绝 —— 不允许子目录 / 越界。"""
    store = TasksStore(room_dir=tmp_path)
    task = store.open_task(title="t", goal="g")
    tool = TaskWriteArtifactTool(
        tasks_store=store,
        transport=_FakeTransport(task_id=task.id),
    )
    out = tool.execute(name="sub/dir/x.md", content="hi")
    assert out.startswith("Error:")
    assert "路径分隔符" in out or "单一文件名" in out
    # 文件**未**被写到任何位置
    assert not (store.artifacts_dir(task.id) / "sub").exists()


def test_write_artifact_rejects_parent_dir_traversal(tmp_path: Path):
    """name 含 ``..`` 拒绝 —— 杜绝路径穿越。"""
    store = TasksStore(room_dir=tmp_path)
    task = store.open_task(title="t", goal="g")
    tool = TaskWriteArtifactTool(
        tasks_store=store,
        transport=_FakeTransport(task_id=task.id),
    )
    out = tool.execute(name="../escape.md", content="hi")
    assert out.startswith("Error:")
    assert ".." in out or "单一文件名" in out


def test_write_artifact_rejects_dotfile(tmp_path: Path):
    """name 以 ``.`` 开头拒绝 —— 防 .DS_Store / Thumbs.db 等幽灵 artifact 冲突。"""
    store = TasksStore(room_dir=tmp_path)
    task = store.open_task(title="t", goal="g")
    tool = TaskWriteArtifactTool(
        tasks_store=store,
        transport=_FakeTransport(task_id=task.id),
    )
    out = tool.execute(name=".hidden.md", content="hi")
    assert out.startswith("Error:")
    assert "幽灵" in out or "." in out
    assert not (store.artifacts_dir(task.id) / ".hidden.md").exists()


def test_write_artifact_rejects_empty_name(tmp_path: Path):
    store = TasksStore(room_dir=tmp_path)
    task = store.open_task(title="t", goal="g")
    tool = TaskWriteArtifactTool(
        tasks_store=store,
        transport=_FakeTransport(task_id=task.id),
    )
    out = tool.execute(name="   ", content="hi")
    assert out.startswith("Error:")
    assert "为空" in out


def test_write_artifact_bypasses_path_policy(tmp_path: Path):
    """**核心**：本工具直接调 ``Path.write_text``，不经 agentao PathPolicy。

    对照组：``test_task_link_write_path_policy.test_write_via_task_softlink_blocked_by_path_policy``
    证明走 agentao 原生 write_file 会被 PathPolicy 拒。本测证明 task_write_artifact
    在同样布局下成功落盘。
    """
    store = TasksStore(room_dir=tmp_path)
    task = store.open_task(title="t", goal="g")
    # 不模拟 ./task/ 软链 / 茶客 cwd —— 重点是工具本身不依赖软链链路
    tool = TaskWriteArtifactTool(
        tasks_store=store,
        transport=_FakeTransport(task_id=task.id),
    )
    out = tool.execute(name="bypass.md", content="written via task tool")
    assert "Successfully" in out
    # 写到 artifacts/ 而非 cwd / share/ 等任何 agentao 工作目录子树
    artifact_path = store.artifacts_dir(task.id) / "bypass.md"
    assert artifact_path.is_file()
    assert artifact_path.read_text(encoding="utf-8") == "written via task tool"
