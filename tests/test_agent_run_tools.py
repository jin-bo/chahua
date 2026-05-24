"""P11.2 C11：``spawn_agent_run`` / ``spawn_agent_runs`` 工具回归。

覆盖：
- 所有拒绝路径（target / instruction / task_id / 同批次重复 / 批次大小 / 上限）。
- 成功创建路径。
- 工具 ``is_read_only=False``（read-only 模式应被拦）。
- ``start_agent_run is None`` 时工具返 ``Error:`` 不抛（裸 session / 未挂 server）。
- **getter 闭合回归**：同 Tool 实例在 attach runtime A 后跑 → A；切换到 B 后同一
  Tool 实例下次 call 自动走 B。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from chahua.agent_run import AgentRun
from chahua.agent_run_tools import (
    MAX_AGENT_RUNS_PER_TOOL_CALL,
    SpawnAgentRunTool,
    SpawnAgentRunsTool,
    register_agent_run_tools,
)


# ── 测试夹具 ─────────────────────────────────────────────────────────────────


class _StubCallback:
    """``StartAgentRun`` 桩：记录每次调用 args，按 ``next_error`` 返成功 / 失败。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.next_errors: list[Optional[str]] = []  # FIFO，空时返成功
        self._counter = 0

    def __call__(
        self, *,
        target: str,
        instruction: str,
        task_id: Optional[str],
        source_guest: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        self.calls.append({
            "target": target,
            "instruction": instruction,
            "task_id": task_id,
            "source_guest": source_guest,
        })
        if self.next_errors:
            err = self.next_errors.pop(0)
            if err is not None:
                return None, err
        self._counter += 1
        return f"run_stub_{self._counter}", None


class _FakeGuest:
    """模拟 ``TeaGuest`` 的 ``start_agent_run`` 槽位 + agent.tools 容器。

    工具按 ``register_agent_run_tools(agent, source_guest=..., get_start_agent_run=...)``
    注册；getter 闭包 ``lambda: self.start_agent_run`` 让闭包逐次读 instance attr。
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.start_agent_run: Optional[_StubCallback] = None
        self.agent = _FakeAgent()
        register_agent_run_tools(
            self.agent,
            source_guest=self.name,
            get_start_agent_run=lambda: self.start_agent_run,
        )


class _FakeTools:
    def __init__(self) -> None:
        self.registered: list[Any] = []

    def register(self, tool: Any) -> None:
        self.registered.append(tool)


class _FakeAgent:
    def __init__(self) -> None:
        self.tools = _FakeTools()


def _single(guest: _FakeGuest) -> SpawnAgentRunTool:
    """从 _FakeGuest 拿到 spawn_agent_run 实例。"""
    for tool in guest.agent.tools.registered:
        if isinstance(tool, SpawnAgentRunTool):
            return tool
    raise AssertionError("spawn_agent_run not registered")


def _batch(guest: _FakeGuest) -> SpawnAgentRunsTool:
    """从 _FakeGuest 拿到 spawn_agent_runs 实例。"""
    for tool in guest.agent.tools.registered:
        if isinstance(tool, SpawnAgentRunsTool):
            return tool
    raise AssertionError("spawn_agent_runs not registered")


# ── 工具属性 ─────────────────────────────────────────────────────────────────


async def test_spawn_tools_are_not_read_only() -> None:
    """write 操作（创建 bg run 改房间运行态）—— read-only 权限模式应拦住。"""
    guest = _FakeGuest("Carol")
    assert _single(guest).is_read_only is False
    assert _batch(guest).is_read_only is False


async def test_spawn_tools_register_both() -> None:
    """``register_agent_run_tools`` 两件套同时注册。"""
    guest = _FakeGuest("Carol")
    kinds = {type(t) for t in guest.agent.tools.registered}
    assert SpawnAgentRunTool in kinds
    assert SpawnAgentRunsTool in kinds


# ── 拒绝路径：start_agent_run is None（裸 session）──────────────────────────────


async def test_single_returns_error_when_callback_none() -> None:
    """``start_agent_run`` 槽位是 None（未挂 server）—— 工具返 Error: 不抛。"""
    guest = _FakeGuest("Carol")
    assert guest.start_agent_run is None
    out = await _single(guest).async_execute(target="Bob", instruction="x")
    assert out.startswith("Error:")
    assert "bg run 入口未装" in out


async def test_batch_returns_error_when_callback_none() -> None:
    guest = _FakeGuest("Carol")
    out = await _batch(guest).async_execute(runs=[{"target": "Bob", "instruction": "x"}])
    assert out.startswith("Error:")
    assert "bg run 入口未装" in out


# ── 拒绝路径：单条工具 shape 校验 ────────────────────────────────────────────


async def test_single_rejects_empty_target() -> None:
    guest = _FakeGuest("Carol")
    guest.start_agent_run = _StubCallback()
    out = await _single(guest).async_execute(target="", instruction="x")
    assert out == "Error: target 必须是非空字符串。"


async def test_single_rejects_empty_instruction() -> None:
    guest = _FakeGuest("Carol")
    guest.start_agent_run = _StubCallback()
    out = await _single(guest).async_execute(target="Bob", instruction="   ")
    assert out == "Error: instruction 必须是非空字符串。"


async def test_single_rejects_bad_task_id() -> None:
    guest = _FakeGuest("Carol")
    guest.start_agent_run = _StubCallback()
    out = await _single(guest).async_execute(target="Bob", instruction="x", task_id="")
    assert out == "Error: task_id 若给须为非空字符串。"


async def test_single_passes_through_callback_error() -> None:
    """server 端 ``_start_agent_run`` 返 err（target 不在场 / busy / cap） → 工具
    返 ``Error: <err>``，不抛、不重试。"""
    guest = _FakeGuest("Carol")
    cb = _StubCallback()
    cb.next_errors.append("target='Bob' 不在场")
    guest.start_agent_run = cb
    out = await _single(guest).async_execute(target="Bob", instruction="x")
    assert out == "Error: target='Bob' 不在场"
    # 即便拒，也调过 callback 一次（让 server 端能做统一上限 / busy 校验）。
    assert len(cb.calls) == 1


# ── 拒绝路径：batch 工具 shape 校验 ──────────────────────────────────────────


async def test_batch_rejects_empty_runs() -> None:
    guest = _FakeGuest("Carol")
    guest.start_agent_run = _StubCallback()
    assert await _batch(guest).async_execute(runs=[]) == "Error: runs 必须是非空数组。"


async def test_batch_rejects_over_size_cap() -> None:
    guest = _FakeGuest("Carol")
    guest.start_agent_run = _StubCallback()
    runs = [
        {"target": f"G{i}", "instruction": "x"}
        for i in range(MAX_AGENT_RUNS_PER_TOOL_CALL + 1)
    ]
    out = await _batch(guest).async_execute(runs=runs)
    assert out.startswith("Error: 单次调用 runs 不能超过")
    assert str(MAX_AGENT_RUNS_PER_TOOL_CALL) in out


async def test_batch_at_size_cap_passes_through() -> None:
    """**边界等于不拒**：批次大小 == ``MAX_AGENT_RUNS_PER_TOOL_CALL`` 应放行。"""
    guest = _FakeGuest("Carol")
    cb = _StubCallback()
    guest.start_agent_run = cb
    runs = [
        {"target": f"G{i}", "instruction": "x"}
        for i in range(MAX_AGENT_RUNS_PER_TOOL_CALL)
    ]
    out = await _batch(guest).async_execute(runs=runs)
    assert out.startswith("Successfully spawned")
    assert len(cb.calls) == MAX_AGENT_RUNS_PER_TOOL_CALL


async def test_batch_rejects_dup_target_in_same_batch() -> None:
    """同批次重复 target —— 一茶客一时刻 1 个 speak。"""
    guest = _FakeGuest("Carol")
    cb = _StubCallback()
    guest.start_agent_run = cb
    runs = [
        {"target": "Bob", "instruction": "x"},
        {"target": "Bob", "instruction": "y"},
    ]
    out = await _batch(guest).async_execute(runs=runs)
    assert out.startswith("Error: runs[1].target=")
    assert "本批次重复" in out
    # 拒在工具层 —— 一条 callback 都没调（整批未启动）。
    assert cb.calls == []


async def test_batch_rejects_non_object_item() -> None:
    guest = _FakeGuest("Carol")
    guest.start_agent_run = _StubCallback()
    out = await _batch(guest).async_execute(runs=["not-an-object"])
    assert out == "Error: runs[0] 必须是 object。"


async def test_batch_rejects_missing_instruction() -> None:
    guest = _FakeGuest("Carol")
    guest.start_agent_run = _StubCallback()
    runs = [{"target": "Bob", "instruction": "   "}]
    out = await _batch(guest).async_execute(runs=runs)
    assert out == "Error: runs[0].instruction 必须是非空字符串。"


# ── 成功路径 ─────────────────────────────────────────────────────────────────


async def test_single_success_passes_source_guest() -> None:
    guest = _FakeGuest("Carol")
    cb = _StubCallback()
    guest.start_agent_run = cb
    out = await _single(guest).async_execute(
        target="Bob", instruction="审一下方案", task_id="t1",
    )
    assert out.startswith("Successfully spawned bg run")
    assert "→ Bob" in out
    assert cb.calls == [
        {
            "target": "Bob",
            "instruction": "审一下方案",
            "task_id": "t1",
            "source_guest": "Carol",
        }
    ]


async def test_single_strips_instruction_whitespace() -> None:
    """``instruction`` strip 前后空白 —— 茶客 LLM 偶尔会带换行。"""
    guest = _FakeGuest("Carol")
    cb = _StubCallback()
    guest.start_agent_run = cb
    await _single(guest).async_execute(target="Bob", instruction="  审一下  \n")
    assert cb.calls[0]["instruction"] == "审一下"


async def test_batch_success_returns_all_run_ids() -> None:
    guest = _FakeGuest("Carol")
    cb = _StubCallback()
    guest.start_agent_run = cb
    runs = [
        {"target": "Bob", "instruction": "审 A"},
        {"target": "Alice", "instruction": "审 B"},
    ]
    out = await _batch(guest).async_execute(runs=runs)
    assert out.startswith("Successfully spawned 2 bg run(s)")
    assert "run_stub_1" in out and "run_stub_2" in out
    # source_guest 由工具固定（注册时绑），target / instruction 来自 runs。
    assert {c["target"] for c in cb.calls} == {"Bob", "Alice"}
    assert all(c["source_guest"] == "Carol" for c in cb.calls)


async def test_batch_partial_success_reports_created() -> None:
    """第 k+1 条拒时返 Error，前 k 条已创建的 run_id 写进报错信息（让茶客 / 用户
    能后续 cancel 那些已起的 run）。"""
    guest = _FakeGuest("Carol")
    cb = _StubCallback()
    cb.next_errors = [None, "target='X' 正忙"]
    guest.start_agent_run = cb
    runs = [
        {"target": "Bob", "instruction": "x"},
        {"target": "X", "instruction": "y"},
    ]
    out = await _batch(guest).async_execute(runs=runs)
    assert out.startswith("Error: runs[1]")
    assert "target='X' 正忙" in out
    assert "run_stub_1" in out
    assert "本批前 1 条已创建" in out


# ── getter 闭合回归（核心契约）───────────────────────────────────────────────


async def test_getter_picks_up_runtime_swap() -> None:
    """同一 Tool 实例先走 callback A，guest.start_agent_run 改为 B 后下次 call
    自动走 B —— 模拟切房 ``_attach_runtime_state`` 重写槽位。"""
    guest = _FakeGuest("Carol")
    cb_a = _StubCallback()
    cb_b = _StubCallback()

    guest.start_agent_run = cb_a
    tool = _single(guest)
    await tool.async_execute(target="Bob", instruction="x")
    assert len(cb_a.calls) == 1
    assert len(cb_b.calls) == 0

    # 模拟切房：server _attach_runtime_state 重写槽位。
    guest.start_agent_run = cb_b
    await tool.async_execute(target="Bob", instruction="y")
    assert len(cb_a.calls) == 1  # 没被再调
    assert len(cb_b.calls) == 1


async def test_getter_handles_unbinding_back_to_none() -> None:
    """attach → call → 解绑 → call —— 解绑后回到 ``Error:`` 行为，不残留 A 的引用。"""
    guest = _FakeGuest("Carol")
    cb = _StubCallback()

    guest.start_agent_run = cb
    tool = _single(guest)
    out_ok = await tool.async_execute(target="Bob", instruction="x")
    assert out_ok.startswith("Successfully spawned")

    guest.start_agent_run = None
    out_err = await tool.async_execute(target="Bob", instruction="y")
    assert out_err.startswith("Error: bg run 入口未装")
    assert len(cb.calls) == 1  # 第二次没走到 cb
