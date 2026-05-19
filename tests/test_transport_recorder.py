"""``ChahuaTransport._handle`` 与 ``TurnRecorder`` 的集成（P6.1，docs/P6 §6）。

回归 codex review 发现的 gap：transport 路径漏了 ``task_write_artifact`` 的 artifact
派生 → ``messages[].artifact_paths`` 在真运行时永远空。

覆盖：

- TOOL_START 帧驱动 ``record_tool_start`` + ``classify_tool_source`` + ``task_write_artifact``
  自动派生 artifact 路径。
- TOOL_COMPLETE 帧合并到同 ``call_id`` 的 entry。
- 非 ``task_write_artifact`` 工具 / ``task_id=None`` / ``args=None`` / ``args.name`` 缺 →
  不派生（守卫齐全）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agentao.transport import AgentEvent, EventType

from chahua._persist import read_jsonl_skip_bad
from chahua.debug_recorder import (
    TOOL_SOURCE_BUILTIN,
    TOOL_SOURCE_MCP,
    TurnRecorder,
)
from chahua.events import STATUS_OK
from chahua.transport_bridge import ChahuaTransport


def _bind_handle_tool_start(
    transport: ChahuaTransport,
    recorder: TurnRecorder,
    *,
    tool: str,
    args: dict | None,
    call_id: str = "c1",
    task_id: str | None = "task_t",
    message_id: str = "msg_m",
) -> None:
    """fake 一个 TOOL_START 帧进 ``_handle``，等价 agentao 真实事件路径。"""
    with transport.bind(
        sink=lambda _env: None,
        turn_id="turn_x",
        message_id=message_id,
        task_id=task_id,
        recorder=recorder,
    ):
        transport._handle(  # noqa: SLF001 — 单测有意走内部入口
            AgentEvent(
                type=EventType.TOOL_START,
                data={"tool": tool, "args": args, "call_id": call_id},
            )
        )


def _start_turn_message(rec: TurnRecorder, message_id: str = "msg_m") -> None:
    rec.start_turn(
        turn_id="turn_x", task_id="task_t",
        trigger={"kind": "user_msg", "ref_seq": 0},
    )
    rec.record_message_start(
        message_id=message_id, guest="A", speak_prompt="",
    )


def _flush_and_read(rec: TurnRecorder, tmp_path: Path) -> dict:
    rec.record_message_end(message_id="msg_m", status=STATUS_OK, seq=1)
    rec.flush_turn()
    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    assert len(rows) == 1
    return rows[0]


def test_task_write_artifact_derives_artifact_path(tmp_path: Path):
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    _start_turn_message(rec)
    transport = ChahuaTransport(room_id="r", guest_name="A")
    _bind_handle_tool_start(
        transport, rec,
        tool="task_write_artifact",
        args={"name": "draft.md", "content": "..."},
    )
    row = _flush_and_read(rec, tmp_path)
    msg = row["messages"][0]
    # tool_calls 含一条 builtin 来源
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["source"] == TOOL_SOURCE_BUILTIN
    # artifact 路径派生进 messages[].artifact_paths
    assert msg["artifact_paths"] == ["tasks/task_t/artifacts/draft.md"]


def test_mcp_tool_classified_and_no_artifact(tmp_path: Path):
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    _start_turn_message(rec)
    transport = ChahuaTransport(room_id="r", guest_name="A")
    _bind_handle_tool_start(
        transport, rec,
        tool="mcp__github__create_issue",
        args={"title": "x"},
    )
    row = _flush_and_read(rec, tmp_path)
    msg = row["messages"][0]
    assert msg["tool_calls"][0]["source"] == TOOL_SOURCE_MCP
    assert msg["tool_calls"][0]["mcp_server"] == "github"
    # 非 task_write_artifact → 不派生
    assert msg["artifact_paths"] == []


def test_no_task_id_skips_artifact_derivation(tmp_path: Path):
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    _start_turn_message(rec)
    transport = ChahuaTransport(room_id="r", guest_name="A")
    _bind_handle_tool_start(
        transport, rec,
        tool="task_write_artifact",
        args={"name": "x.md", "content": "..."},
        task_id=None,
    )
    row = _flush_and_read(rec, tmp_path)
    # task_id=None → 即便工具匹配也不派生
    assert row["messages"][0]["artifact_paths"] == []


@pytest.mark.parametrize(
    "args",
    [
        None,
        {},
        {"name": ""},
        {"name": 123},          # 非 str
        {"content": "no name"}, # 漏 name
        "not a dict",           # 非 dict
    ],
)
def test_bad_args_skips_artifact_derivation(tmp_path: Path, args):
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    _start_turn_message(rec)
    transport = ChahuaTransport(room_id="r", guest_name="A")
    _bind_handle_tool_start(
        transport, rec,
        tool="task_write_artifact",
        args=args,
    )
    row = _flush_and_read(rec, tmp_path)
    assert row["messages"][0]["artifact_paths"] == []


@pytest.mark.parametrize(
    "bad_name",
    [
        "../etc.md",       # 含 ``..`` —— 路径穿越
        "dir/x.md",        # 含 ``/`` —— 含路径分隔
        "dir\\x.md",       # 含 ``\\``
        ".hidden",         # 前缀 ``.`` —— .DS_Store 同口径
        ".",               # 单点
    ],
)
def test_invalid_artifact_name_skips_derivation(tmp_path: Path, bad_name: str):
    """name 不合法 → ``TasksStore.write_artifact`` 会拒、根本不会落盘，调试日志记一条
    "幽灵 artifact" 会误导用户去查根本不存在的文件。与 ``_validate_artifact_name`` 同口径。
    """
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    _start_turn_message(rec)
    transport = ChahuaTransport(room_id="r", guest_name="A")
    _bind_handle_tool_start(
        transport, rec,
        tool="task_write_artifact",
        args={"name": bad_name, "content": "..."},
    )
    row = _flush_and_read(rec, tmp_path)
    assert row["messages"][0]["artifact_paths"] == []


def test_tool_complete_merges_into_same_call_id(tmp_path: Path):
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    _start_turn_message(rec)
    transport = ChahuaTransport(room_id="r", guest_name="A")
    with transport.bind(
        sink=lambda _env: None,
        turn_id="turn_x", message_id="msg_m", task_id="task_t", recorder=rec,
    ):
        transport._handle(AgentEvent(  # noqa: SLF001
            type=EventType.TOOL_START,
            data={"tool": "task_write_artifact",
                  "args": {"name": "x.md", "content": "..."},
                  "call_id": "c1"},
        ))
        transport._handle(AgentEvent(  # noqa: SLF001
            type=EventType.TOOL_COMPLETE,
            data={"tool": "task_write_artifact", "call_id": "c1",
                  "status": "ok", "duration_ms": 42, "error": None},
        ))
    row = _flush_and_read(rec, tmp_path)
    tc = row["messages"][0]["tool_calls"][0]
    assert tc["status"] == "ok"
    assert tc["duration_ms"] == 42
    # start 帧 args/source 保留，未被 complete 帧覆盖
    assert tc["args"] == {"name": "x.md", "content": "..."}
    assert tc["source"] == TOOL_SOURCE_BUILTIN
