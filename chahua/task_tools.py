"""P5.3.4：茶客可调的 task-aware 工具集（docs/P5-任务房间.md §6.3 / §13 P5.3.4）。

注册五个 in-process Python 工具给 agentao：

- ``task_list_artifacts()`` —— 列当前 active task 的产物清单
- ``task_propose_decision(summary, supporting_message_ids?)`` —— 提议一条决策
- ``task_propose_open(title, goal)`` —— 提议开新任务
- ``task_propose_status(status, reason?)`` —— 提议改任务状态（P8.2）
- ``task_write_artifact(name, content)`` —— **写**任务产物（绕开 agentao PathPolicy）

四个 read-only 工具（list / propose_*）均 ``is_read_only = True``，但**这是为权限层
放行而非声明"无副作用"**：

- ``list_artifacts`` 是真无副作用 read-only；
- ``propose_*`` 不直接写 task store / 不落盘，但**会 emit TASK_PROPOSAL envelope**。
  ``is_read_only=True`` 是为了避免 agentao 的权限层把"提议事件"当成写操作拦截，
  不是声称无副作用。
- 真正的"决策/开任务写入"由用户在 UI"采纳"卡片后走既有 ``ADD_DECISION`` / ``OPEN_TASK``
  inbound（沿用 docs §8 不变量 #3："写权限永远在用户"）。

**``task_write_artifact`` 是例外**：``is_read_only = False``，茶客直接写到
``tasks/<active>/artifacts/<name>``。设计动机：agentao 的 ``PathPolicy.contain_file``
会跟随 parent chain 上的 symlink 解析后做 ``is_relative_to(working_directory)`` 检查；
``./task/`` 软链解析到 ``<room>/tasks/<id>/artifacts/`` 后**不在**茶客 working_directory
（``<room>/guests/<name>/``）之下，所以通过 agentao 原生 ``write_file('./task/<name>')``
会被拒。本工具委托 :meth:`TasksStore.write_artifact` 直接落盘绕开 ``PathPolicy``——
name 校验与 ``attach_artifact`` 共享 :func:`tasks_store._validate_artifact_name`。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Optional

from agentao.tools import Tool

from .events import (
    TASK_PROPOSAL_KIND_DECISION,
    TASK_PROPOSAL_KIND_OPEN,
    TASK_PROPOSAL_KIND_STATUS,
    ChahuaEventType,
)
from .task import (
    TASK_STATUS_DISPLAY,
    TASK_STATUS_OPEN,
    format_artifact_mtime,
    format_artifact_size,
)
from .tasks_store import (
    CLOSED_STATUSES,
    NON_TERMINAL_STATUSES,
    TaskNotFoundError,
    TasksStore,
)

# P8.2：``task_propose_status`` 可提议的状态值 —— 全部状态去掉创建态 ``open``
# （茶客不能提议把任务"退回未开始"）。采纳时前端按终结与否分流到 close_task /
# update_task（docs/P8 §4）。
_PROPOSE_STATUS_VALUES: frozenset[str] = (
    NON_TERMINAL_STATUSES - {TASK_STATUS_OPEN}
) | CLOSED_STATUSES
# 单点 sorted 列表 —— parameters 的 enum 与 execute 的错误文案共用，杜绝两处 sorted 漂移。
_PROPOSE_STATUS_VALUES_SORTED: list[str] = sorted(_PROPOSE_STATUS_VALUES)

if TYPE_CHECKING:
    # P6.1：transport_bridge.py 反向 import 本模块的 TASK_WRITE_ARTIFACT_TOOL_NAME
    # 派生 ``messages[].artifact_paths`` —— 用 TYPE_CHECKING 把 ChahuaTransport 这条
    # 仅用作类型注解的 import 推后到检查期，破除运行期循环。``from __future__ import
    # annotations`` 已开，签名里的 ``transport: ChahuaTransport`` 在运行期是字符串。
    from .transport_bridge import ChahuaTransport

# artifact 清单 / propose ack 上限。与 orchestrator._render_task_block 完整块的 cap
# 同步（docs §6.1 "list_artifacts 返 markdown 清单同 §6.1 注入格式"）—— 改一处记得
# 改两处。LLM 通过工具拿清单时若任务产物 >10 个，截尾部分让 LLM 知道"还有 N 个"。
_LIST_ARTIFACTS_CAP = 10
_PROPOSE_ACK_TRUNC = 60


def _format_artifacts_md(artifacts: list[dict]) -> str:
    """与 :func:`chahua.orchestrator._render_task_block` 的 artifact 段同格式：
    ``- {name} ({size}, {mtime})``。空清单 / 超 cap 时分别 fallback。"""
    if not artifacts:
        return "(no artifacts in the current task yet)"
    head = artifacts[:_LIST_ARTIFACTS_CAP]
    lines = ["Current task artifacts (no inline content; read via ./task/ with read_file):"]
    for a in head:
        lines.append(
            f"- {a['name']} "
            f"({format_artifact_size(a['size'])}, {format_artifact_mtime(a['mtime_ms'])})"
        )
    if len(artifacts) > _LIST_ARTIFACTS_CAP:
        lines.append(f"… ({len(artifacts) - _LIST_ARTIFACTS_CAP} more not listed)")
    return "\n".join(lines)


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
            "List the current task's artifact files (names + sizes, no inline "
            "content). To read an artifact's actual content, use read_file on "
            "./task/<name>. Returns a notice string when there is no active task "
            "or the task was deleted."
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
            return "No active task; no artifacts to list."
        task = self._tasks_store.get_task(task_id)
        if task is None:
            return f"Task {task_id} does not exist (may have been deleted)."
        return _format_artifacts_md(self._tasks_store.list_artifacts(task_id))


class _TaskProposeBase(Tool):
    """两个 propose 工具的共同基类 —— 共享 envelope emit 路径。

    子类必须设 ``_kind`` 字面值（``TASK_PROPOSAL_KIND_*``）—— wire 上下游按这个
    分发，前端"采纳"按钮按 kind 拼对应 inbound（``ADD_DECISION`` / ``OPEN_TASK``）。
    """

    _kind: ClassVar[str]

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
        return (
            f"Proposed \"{ack_label[:_PROPOSE_ACK_TRUNC]}\"; "
            "committed only after the user accepts in the UI."
        )


class TaskProposeDecisionTool(_TaskProposeBase):
    _kind = TASK_PROPOSAL_KIND_DECISION

    @property
    def name(self) -> str:
        return "task_propose_decision"

    @property
    def description(self) -> str:
        return (
            "Propose a task decision. Proposing is not committing — the UI renders "
            "an accept / dismiss card; only after the user accepts does it go through "
            "the existing add_decision into decisions.jsonl. summary is one sentence "
            "(≤ 200 chars); supporting_message_ids point to the transcript message "
            "ids backing the decision (optional)."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Decision summary (one sentence, ≤ 200 chars).",
                },
                "supporting_message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of message ids backing the decision (optional).",
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
    _kind = TASK_PROPOSAL_KIND_OPEN

    @property
    def name(self) -> str:
        return "task_propose_open"

    @property
    def description(self) -> str:
        return (
            "Propose opening a new task. Proposing is not committing — the UI "
            "renders an accept / dismiss card; only after the user accepts does it "
            "go through the existing open_task. title ≤ 60 chars; goal is multi-line "
            "markdown."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Task title (≤ 60 chars)."},
                "goal": {"type": "string", "description": "Task goal (multi-line markdown)."},
            },
            "required": ["title", "goal"],
            "additionalProperties": False,
        }

    def execute(self, *, title: str, goal: str, **_: Any) -> str:
        return self._emit_proposal({"title": title, "goal": goal}, ack_label=title)


class TaskProposeStatusTool(_TaskProposeBase):
    """P8.2：提议把当前任务改成某个状态（docs/P8 §4）。

    与 ``task_propose_decision`` / ``task_propose_open`` 同基类、同口径——只 emit
    ``TASK_PROPOSAL``、不写库。采纳后前端按状态分流：非终结态走 ``update_task``、
    终结态（done / abandoned）走 ``close_task``。补的是真实能力缺口：茶客此前**无法**
    提议把任务设为 done / review / blocked，只能在聊天里口头提醒用户去面板手点。
    """

    _kind = TASK_PROPOSAL_KIND_STATUS

    @property
    def name(self) -> str:
        return "task_propose_status"

    @property
    def description(self) -> str:
        return (
            "Propose changing the current task to a status. Proposing is not "
            "committing — the UI renders an accept / dismiss card; only after the "
            "user accepts is the status actually changed. status values are in the "
            "enum (excludes the creation state open); reason is a one-sentence "
            "rationale (optional, shown only on the accept card, not stored). Use to "
            "propose marking done when achieved, blocked when stuck, review when a "
            "re-check is needed, etc."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": _PROPOSE_STATUS_VALUES_SORTED,
                    "description": "Target status.",
                },
                "reason": {
                    "type": "string",
                    "description": "Rationale for the status change (one sentence, optional).",
                },
            },
            "required": ["status"],
            "additionalProperties": False,
        }

    def execute(self, *, status: str, reason: str = "", **_: Any) -> str:
        # parameters 已声明 enum，但 LLM 可能无视约束传任意值 —— 显式再校验。
        if status not in _PROPOSE_STATUS_VALUES:
            return f"Error: status must be one of {_PROPOSE_STATUS_VALUES_SORTED!r}."
        # 改状态提议必须挂某个任务——前端采纳卡按 data.task_id 拼 update_task /
        # close_task inbound，无 active 时发出去的卡片必然不可采纳（codex P2）。
        if self._transport.current_task_id is None:
            return "Error: no active task; cannot propose a task status change."
        payload: dict[str, Any] = {"status": status}
        if reason:
            payload["reason"] = reason
        label = TASK_STATUS_DISPLAY.get(status, status)
        return self._emit_proposal(payload, ack_label=f"mark the task as {label}")


# ── 写产物工具（绕开 agentao PathPolicy）────────────────────────────────────


# 暴露给 transport_bridge 派生 ``messages[].artifact_paths`` 用（docs/P6 §6）。
# 改名等于改 P6 debug 派生表 —— 单点常量避免散落字面值。
TASK_WRITE_ARTIFACT_TOOL_NAME = "task_write_artifact"


class TaskWriteArtifactTool(Tool):
    """把内容写入当前 active task 的 ``artifacts/<name>``，绕开 agentao PathPolicy。

    **为什么需要本工具**：agentao 的 ``security/path_policy.py::PathPolicy.contain_file``
    会跟随 parent chain 上的 symlink 解析后做 ``is_relative_to(working_directory)`` 检查；
    茶客 working_directory = ``<room>/guests/<name>/``、``./task/`` 软链解析到
    ``<room>/tasks/<id>/artifacts/`` 后**不在**前者之下，所以茶客调原生 ``write_file
    ('./task/<x>')`` 会返 ``"Error: PathPolicy: refused ..."``。本工具走
    :meth:`TasksStore.write_artifact` 直接落盘，不经过 agentao 工具栈的 path_policy。

    **不 emit envelope**：让 ``Orchestrator._kick_detect_new_artifacts`` 在下个 pick
    周期末尾扫到自然 emit ``task_artifact_added`` + ``task_info``——复用既有自动归集
    路径，与"用户 UI 上传 + 茶客扫描"同口径。

    **is_read_only = False**：写操作。read-only 权限模式应当拦住此工具。
    """

    def __init__(self, *, tasks_store: TasksStore, transport: ChahuaTransport) -> None:
        super().__init__()
        self._tasks_store = tasks_store
        self._transport = transport

    @property
    def name(self) -> str:
        return TASK_WRITE_ARTIFACT_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            "Write content to the current task's artifact ./task/<name>; "
            "auto-added to the task artifact list. Use to persist structured "
            "outputs such as reviews / designs / decision lists / code snippets / "
            "report drafts. name must be a single filename (no /, no .., no leading "
            ".). Overwrites the same-named file by default; with append=true, "
            "appends to the end (creates the file if missing) — use append for "
            "incremental additions to ledger / log artifacts, avoiding a "
            "read-all-then-rewrite. Returns an ``Error: ...`` string when there is "
            "no active task / the task is closed / name is invalid. **Use this tool "
            "to write** — a plain write_file('./task/<x>') is rejected by the "
            "permission boundary (PathPolicy)."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Artifact filename (single filename, no /, no .., no "
                        "leading .). Prefer role identity + version, e.g. "
                        "``review-v2.md`` / ``decisions-v2.md`` / ``report-final.md``."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Content to write (full file content in overwrite mode; the appended fragment in append mode).",
                },
                "append": {
                    "type": "boolean",
                    "description": "Append to the end of the file instead of overwriting (default false).",
                },
            },
            "required": ["name", "content"],
            "additionalProperties": False,
        }

    @property
    def is_read_only(self) -> bool:
        return False

    def execute(
        self, *, name: str, content: str, append: bool = False, **_: Any
    ) -> str:
        task_id = self._transport.current_task_id
        if task_id is None:
            return "Error: no active task; cannot write an artifact."
        task = self._tasks_store.get_task(task_id)
        if task is None:
            return f"Error: task {task_id} does not exist (may have been deleted)."
        if task.status in CLOSED_STATUSES:
            return f"Error: task {task_id} is already {task.status}; cannot write a new artifact."
        try:
            result = self._tasks_store.write_artifact(
                task_id, name=name, content=content, append=append,
            )
        except ValueError as e:
            return f"Error: {e}"
        except TaskNotFoundError as e:
            return f"Error: {e}"
        except OSError as e:
            return f"Error: write failed - {e}"
        action = "appended to" if append else "wrote"
        return (
            f"Successfully {action} ./task/{result['name']}"
            f" (file is now {result['size']} bytes)."
        )


def register_task_tools(
    agent: Any,
    *,
    tasks_store: TasksStore,
    transport: ChahuaTransport,
) -> None:
    """把五个 task tool 注册到 agentao agent。工厂函数 —— TeaGuest 用一行调用。

    工具构造时注入 ``tasks_store`` / ``transport``，让工具类不直接依赖 :class:`TeaGuest`
    （单测时各 mock 一份就够）。**不引入** ``TaskToolRegistry`` / plugin 抽象 ——
    五个工具类 + 一个工厂函数足够（评审反馈：避免过度设计）。
    """
    agent.tools.register(TaskListArtifactsTool(tasks_store=tasks_store, transport=transport))
    agent.tools.register(TaskProposeDecisionTool(transport=transport))
    agent.tools.register(TaskProposeOpenTool(transport=transport))
    agent.tools.register(TaskProposeStatusTool(transport=transport))
    agent.tools.register(TaskWriteArtifactTool(tasks_store=tasks_store, transport=transport))
