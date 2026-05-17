"""P5.3.4：茶客可调的 task-aware 工具集（docs/P5-任务房间.md §6.3 / §13 P5.3.4）。

注册三个 in-process Python 工具给 agentao：

- ``task_list_artifacts()`` —— 列当前 active task 的产物清单
- ``task_propose_decision(summary, supporting_message_ids?)`` —— 提议一条决策
- ``task_propose_open(title, goal)`` —— 提议开新任务

三工具均 ``is_read_only = True``，但**这是为权限层放行而非声明"无副作用"**：

- ``list_artifacts`` 是真无副作用 read-only；
- ``propose_*`` 不直接写 task store / 不落盘，但**会 emit TASK_PROPOSAL envelope**。
  ``is_read_only=True`` 是为了避免 agentao 的权限层把"提议事件"当成写操作拦截，
  不是声称无副作用。

真正的"写入"由用户在 UI"采纳"卡片后走既有 ``ADD_DECISION`` / ``OPEN_TASK`` inbound
（沿用 docs §8 不变量 #3："写权限永远在用户"）。
"""

from __future__ import annotations

from typing import Any, Optional

from agentao.tools import Tool

from .events import ChahuaEventType
from .tasks_store import TasksStore
from .transport_bridge import ChahuaTransport


def _format_size(size: int) -> str:
    """字节数 → 人眼可读。与 ``orchestrator._format_artifact_size`` 同口径（也与
    ``app/renderer/task_panel.js::formatSize`` 同口径）—— 改一处记得改三处。"""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _format_artifacts_md(artifacts: list[dict]) -> str:
    if not artifacts:
        return "（当前任务暂无产物）"
    lines = ["当前任务产物清单（不嵌内容，按需 ls / cat 走 ``./task/`` 读取）："]
    for a in artifacts:
        lines.append(f"- {a['name']} ({_format_size(a['size'])})")
    return "\n".join(lines)


_PROPOSE_ACK_TRUNC = 60
"""propose 工具返回 LLM 的 ack 字符串里 title / summary 截断长度。让 LLM 看到提议
落地（避免反复刷工具），但不喂回大段它自己的 payload。"""


class TaskListArtifactsTool(Tool):
    """读 active task 的产物清单。无 active / 任务消失时返提示而非空。

    工具构造时显式注入 ``tasks_store`` 与 ``transport`` —— 让工具类不直接依赖
    :class:`TeaGuest`，便于单测各 mock 一份。
    """

    def __init__(self, *, tasks_store: TasksStore, transport: ChahuaTransport) -> None:
        super().__init__()
        self._tasks_store = tasks_store
        self._transport = transport

    @property
    def name(self) -> str:
        return "task_list_artifacts"

    @property
    def description(self) -> str:
        return (
            "查看当前任务的产物文件清单（不嵌内容，只列文件名 + 大小）。"
            "需要读取某个产物的真实内容时，用 read_file 走 ./task/<name>。"
            "当前无活跃任务 / 任务已被删除时返提示字符串。"
        )

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    @property
    def is_read_only(self) -> bool:
        return True

    def execute(self, **kwargs: Any) -> str:
        task_id = self._transport.current_task_id
        if task_id is None:
            return "当前无活跃任务，无产物可列。"
        task = self._tasks_store.get_task(task_id)
        if task is None:
            return f"任务 {task_id} 不存在（可能已被删除）。"
        return _format_artifacts_md(self._tasks_store.list_artifacts(task_id))


class _TaskProposeBase(Tool):
    """两个 propose 工具的共同基类 —— 共享 envelope emit 路径。

    子类必须设 ``_kind`` 字面值（``"decision"`` / ``"open"``）—— wire 上下游按这个
    分发，前端"采纳"按钮按 kind 拼对应 inbound（``ADD_DECISION`` / ``OPEN_TASK``）。
    """

    _kind: str

    def __init__(self, *, transport: ChahuaTransport) -> None:
        super().__init__()
        self._transport = transport

    @property
    def is_read_only(self) -> bool:
        return True

    def _emit_proposal(self, payload: dict[str, Any], *, ack_label: str) -> str:
        """统一组 envelope.data + emit + 返 LLM ack。

        ``task_id`` 自动由 ``ChahuaTransport.emit_chahua`` 从 bind 上下文合成进
        ``data.task_id`` —— 这里不重复写键，避免"工具自己读 store.active vs transport
        bind snapshot"两条不同步路径。
        """
        self._transport.emit_chahua(
            ChahuaEventType.TASK_PROPOSAL,
            {
                "proposer": self._transport.guest_name,
                "kind": self._kind,
                "payload": payload,
            },
        )
        return f"已提议「{ack_label[:_PROPOSE_ACK_TRUNC]}」，等用户在 UI 采纳后入库。"


class TaskProposeDecisionTool(_TaskProposeBase):
    _kind = "decision"

    @property
    def name(self) -> str:
        return "task_propose_decision"

    @property
    def description(self) -> str:
        return (
            "提议一条任务决策。提议 ≠ 入库 —— UI 渲染成采纳 / 忽略卡片；"
            "用户点采纳后才走既有 add_decision 入 decisions.jsonl。"
            "summary 一句话（≤ 200 字）；supporting_message_ids 指向决策依据的"
            "transcript 消息 id（可选）。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "决策摘要（≤ 200 字一句话）。",
                },
                "supporting_message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "决策依据的消息 id 列表（可选）。",
                },
            },
            "required": ["summary"],
            "additionalProperties": False,
        }

    def execute(
        self,
        *,
        summary: str,
        supporting_message_ids: Optional[list[str]] = None,
        **_: Any,
    ) -> str:
        payload: dict[str, Any] = {"summary": summary}
        if supporting_message_ids:
            payload["supporting_message_ids"] = list(supporting_message_ids)
        return self._emit_proposal(payload, ack_label=summary)


class TaskProposeOpenTool(_TaskProposeBase):
    _kind = "open"

    @property
    def name(self) -> str:
        return "task_propose_open"

    @property
    def description(self) -> str:
        return (
            "提议开一个新任务。提议 ≠ 入库 —— UI 渲染成采纳 / 忽略卡片；"
            "用户点采纳后才走既有 open_task。title ≤ 60 字；goal 多行 markdown。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "任务标题（≤ 60 字）。"},
                "goal": {"type": "string", "description": "任务目标（多行 markdown）。"},
            },
            "required": ["title", "goal"],
            "additionalProperties": False,
        }

    def execute(self, *, title: str, goal: str, **_: Any) -> str:
        return self._emit_proposal({"title": title, "goal": goal}, ack_label=title)


def register_task_tools(
    agent: Any,
    *,
    tasks_store: TasksStore,
    transport: ChahuaTransport,
) -> None:
    """把三个 task tool 注册到 agentao agent。工厂函数 —— TeaGuest 用一行调用。

    工具构造时注入 ``tasks_store`` / ``transport``，让工具类不直接依赖 :class:`TeaGuest`
    （单测时各 mock 一份就够）。**不引入** ``TaskToolRegistry`` / plugin 抽象 ——
    三个工具类 + 一个工厂函数足够（评审反馈：避免过度设计）。
    """
    agent.tools.register(TaskListArtifactsTool(tasks_store=tasks_store, transport=transport))
    agent.tools.register(TaskProposeDecisionTool(transport=transport))
    agent.tools.register(TaskProposeOpenTool(transport=transport))
