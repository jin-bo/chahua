"""P11.2 C11：茶客侧 ``spawn_agent_run`` / ``spawn_agent_runs`` 工具。

设计见 [`docs/P11-后台 Agent.md`](../docs/P11-后台 Agent.md) §「``spawn_agent_run(s)``
工具（P11.2）」。

茶客主动调度后台并发：``propose_*`` 是「等用户采纳」的串行调度（panel 也仍是串行
drain），``spawn_agent_run(s)`` 是「立即创建并发后台 run」的直接调度。MTS 管理者
在「绕开 budget 并发分发」时也用这个工具。

两个工具共享同一份 server 端 ``_start_agent_run`` 路径（与 C8 inbound 同一）—— 4 道
校验 + 登记 + emit ``AGENT_RUN_STARTED`` 全部走 server 单点。本模块只负责：

- 工具参数面：仅 ``{target, instruction, task_id?}``（**不**引入
  ``agent_run_spec`` / ``review_message_id`` / ``propose_agent_runs`` —— 真实需求出现
  再加）。
- 两道工具层校验：同批次重复 target / 批次大小 ``> max_agent_runs_per_tool_call``。
- 经 ``get_start_agent_run`` getter 现取回调（解决「同 Tool 实例跨切房被 re-bind」
  的闭合问题：server 端 ``_attach_runtime_state`` 改 ``guest.start_agent_run`` 槽位，
  闭包 ``lambda: self.start_agent_run`` 每次 ``execute`` 读 instance attr，立刻见新值）。
- 工具 ``is_read_only = False``：写操作（创建 bg run 改房间运行态）；read-only 权限
  模式应拦住此工具。

返回：成功 → ``"Successfully spawned bg run(s): <run_id>, ..."``；任一拒 →
``"Error: ..."`` 字符串（agentao 工具协议：返字符串就是 LLM-visible result，不抛
异常）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, ClassVar, Optional

from agentao.tools.base import AsyncToolBase

from .agent_run import (
    AGENT_RUN_ERR_MTS_MANAGER,
    AGENT_RUN_ERR_ROOM_CAP,
    AGENT_RUN_ERR_TARGET_ABSENT,
    AGENT_RUN_ERR_TARGET_BUSY,
    AGENT_RUN_ERR_TASK_CLOSED,
    AGENT_RUN_ERR_TASK_NOT_FOUND,
)


_log = logging.getLogger(__name__)


# 单工具调用一批次的上限。docs §「Phases」P11.2：``max_agent_runs_per_tool_call = 4``
# —— 按 P9 5 个后台房算，4 已覆盖典型「3 审稿 + 1 主持」。
MAX_AGENT_RUNS_PER_TOOL_CALL = 4


def _agent_run_err_en(code: str, *, target: str, task_id: object) -> str:
    """把 ``_start_agent_run`` 的无参数原因码（``agent_run.AgentRunError``）本地化成
    **英文** tool-result 文案（回灌发起调用茶客的 LLM）。

    P14：inbound NOTICE 路径走平行的中文 render（``server_inbound_agent_run.
    _agent_run_err_zh``，用户可见保持中文）—— 同一原因码两语言由构造保证同步
    （docs/P14「双路 err」）。所有插值参数在工具侧都可得（room_cap 不带具体上限数字：
    LLM 不需要精确值，用户那侧的中文 NOTICE 仍带数字）。
    """
    if code == AGENT_RUN_ERR_TARGET_ABSENT:
        return f"target={target!r} is not in the room"
    if code == AGENT_RUN_ERR_TASK_NOT_FOUND:
        return f"task_id={task_id!r} does not exist"
    if code == AGENT_RUN_ERR_TASK_CLOSED:
        return f"task_id={task_id!r} is closed; cannot bind a bg run"
    if code == AGENT_RUN_ERR_TARGET_BUSY:
        return (
            f"target={target!r} is busy (foreground / handoff / existing bg run); "
            "try again later"
        )
    if code == AGENT_RUN_ERR_ROOM_CAP:
        return "the room is at its background-run limit; try again later"
    if code == AGENT_RUN_ERR_MTS_MANAGER:
        return (
            f"target={target!r} is the current MTS manager; a bg run targeting the "
            "manager would pollute the managed queue (auto-enqueued by the intercept "
            f"hook) — use @{target} or stop the MTS first"
        )
    return str(code)  # 未知码兜底：原样回显（不该发生）


# Callback 协议：
#   (target: str, instruction: str, task_id: Optional[str], source_guest: Optional[str])
#     -> tuple[Optional[str], Optional[str]]
#       —— 成功返 (run_id, None)；任一校验拒返 (None, error_msg)。
# 由 server 端 ``_make_start_agent_run(runtime)`` 工厂返回，绑定到当前前台 runtime
# （切房 / 同房重建走 ``_attach_runtime_state`` 重新绑定）。
StartAgentRun = Callable[..., tuple[Optional[str], Optional[str]]]


class _SpawnBase(AsyncToolBase):
    """两个 spawn 工具的共同基类 —— 共享 getter / source_guest 字段 + ``is_read_only``。

    **必须继承 ``AsyncToolBase``**（不是 ``Tool``）：``_start_agent_run`` 内部调
    ``asyncio.create_task`` 启 bg wrapper，create_task 要求**当前线程有 running event
    loop**。agentao 的 ``Tool.execute`` 由 ``ToolExecutor`` 在 ``loop.run_in_executor``
    的工作线程内同步调用 —— 工作线程没有 running loop，create_task 会
    ``RuntimeError: no running event loop``。``AsyncToolBase.async_execute`` 走
    ``run_coroutine_threadsafe`` 桥回 host 事件循环，create_task 才工作。

    ``get_start_agent_run`` 是一个 **getter**（不是直接的回调）—— 工具实例每次
    ``async_execute`` 现取一次：

    - 工具实例在 ``TeaGuest.__init__`` 时注册，此时 ``self.start_agent_run`` 还是 None；
    - server ``_attach_runtime_state(runtime)`` 后回填 ``guest.start_agent_run = cb``；
    - 切房时 ``_attach_runtime_state`` 再次重写槽位，同一 Tool 实例下次 call 自动看到
      新 runtime 的回调（**闭合契约**：getter ``lambda: self.start_agent_run`` 让闭包
      逐次读 instance attr）。
    """

    _kind: ClassVar[str]  # 子类设 "single" / "batch" 仅用于错误文案

    def __init__(
        self,
        *,
        source_guest: str,
        get_start_agent_run: Callable[[], Optional[StartAgentRun]],
    ) -> None:
        super().__init__()
        self._source_guest = source_guest
        self._get_start_agent_run = get_start_agent_run

    @property
    def is_read_only(self) -> bool:
        return False

    def _resolve_callback(self) -> Optional[StartAgentRun]:
        """每次 async_execute 现取 —— 闭合 lambda 逐次读 ``guest.start_agent_run`` instance attr。

        ``None`` 说明 server 端 ``_attach_runtime_state`` 还没装 —— 比如裸 session
        测试夹具 / build_room_session 阶段（未挂 server）。工具不抛、返 ``Error:``
        字符串（agentao 工具协议）。
        """
        return self._get_start_agent_run()

    def _safe_call(
        self,
        cb: StartAgentRun,
        *,
        target: str,
        instruction: str,
        task_id: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        """调 server ``_start_agent_run`` 并把任何抛出转成 ``(None, error_msg)``。

        agentao 工具协议要求 ``async_execute`` 返字符串（含 ``Error:``）而**不抛**异常
        —— 否则 tool_runner 走 ``f'Error executing {fn}: {exc}'`` 兜底，丢掉本工具
        的 ``Error: ...`` 格式 + 暴露 stack/repr。``_start_agent_run`` 内 ``emit``
        / ``create_task`` 都可能抛（ws 半断、loop 故障）；本 helper 是 contract 守门。
        """
        try:
            return cb(
                target=target,
                instruction=instruction,
                task_id=task_id,
                source_guest=self._source_guest,
            )
        except Exception as exc:  # noqa: BLE001 — agentao 工具协议要求不抛
            _log.warning(
                "spawn tool: cb() raised — translating to Error: tool result",
                exc_info=True,
            )
            return None, f"internal error ({type(exc).__name__}): {exc}"


class SpawnAgentRunTool(_SpawnBase):
    """单条 bg run 直接调度。"""

    _kind = "single"

    @property
    def name(self) -> str:
        return "spawn_agent_run"

    @property
    def description(self) -> str:
        return (
            "Immediately dispatch one background run to a guest (vs propose_delegate: "
            "spawn does NOT wait for user approval and does NOT block your current "
            "reply — target starts executing in the background right away). For "
            "parallel background execution use this tool or spawn_agent_runs — do NOT "
            "use propose_panel (panel still drains serially). Usable inside an MTS, "
            "**bypassing budget for parallel dispatch** (does not spend the manager's "
            "budget / does not count toward the consecutive-turn cap). instruction is "
            "required and non-empty, fed to target as its instruction; optional "
            "task_id binds the bg run to a task context. Rejected when: target absent "
            "/ busy (foreground / handoff / existing bg run) / room bg-run count at "
            "cap / task_id missing or closed. Returns an ``Error: ...`` string on "
            "failure."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Name of the guest to dispatch to."},
                "instruction": {
                    "type": "string",
                    "description": "Instruction text given to target (required, non-empty).",
                },
                "task_id": {
                    "type": "string",
                    "description": "Optional: bound task id (must not be closed).",
                },
            },
            "required": ["target", "instruction"],
            "additionalProperties": False,
        }

    async def async_execute(
        self,
        *,
        target: str,
        instruction: str,
        task_id: Optional[str] = None,
        **_: Any,
    ) -> str:
        cb = self._resolve_callback()
        if cb is None:
            return "Error: bg run entry not wired (session has no server runtime)."
        if not isinstance(target, str) or not target:
            return "Error: target must be a non-empty string."
        if not isinstance(instruction, str) or not instruction.strip():
            return "Error: instruction must be a non-empty string."
        instruction = instruction.strip()
        if task_id is not None and (not isinstance(task_id, str) or not task_id):
            return "Error: task_id, if given, must be a non-empty string."
        run_id, err = self._safe_call(
            cb, target=target, instruction=instruction, task_id=task_id,
        )
        if err is not None:
            return f"Error: {_agent_run_err_en(err, target=target, task_id=task_id)}"
        return f"Successfully spawned bg run {run_id} → {target}."


class SpawnAgentRunsTool(_SpawnBase):
    """批量 bg run 直接调度（同批次一次性原子校验）。

    上限 ``max_agent_runs_per_tool_call = 4``；批次本身 + 房间现有 bg run 数仍受
    ``max_agent_runs_per_room = 4`` 约束（在 server ``_start_agent_run`` 第四道
    校验）。同批次 ``target`` 重复直接拒（一茶客一时刻最多 1 个 speak）。
    """

    _kind = "batch"

    @property
    def name(self) -> str:
        return "spawn_agent_runs"

    @property
    def description(self) -> str:
        return (
            "Immediately dispatch a batch of background runs to several guests "
            "concurrently (one instruction per guest). For parallel background "
            "execution use this tool — do NOT use propose_panel (panel still drains "
            "serially). Usable inside an MTS, **bypassing budget for parallel "
            "dispatch** (does not spend the manager's budget / does not count toward "
            "the consecutive-turn cap). runs[*].target must not repeat within a batch "
            "(one speak per guest at a time); batch size ≤ 4. **Shape errors "
            "(missing target/instruction / duplicate in batch / over the limit) "
            "reject the whole batch before any dispatch**; **during per-item "
            "creation** if item k+1 is rejected by the server (target absent / busy / "
            "task_id closed), the first k already-started bg runs are NOT rolled "
            "back, and it returns ``Error: runs[k+1] ...; first k of this batch "
            "created`` listing the started run_ids (cancel individually with "
            "``agent_run_cancel``). On success returns a ``Successfully spawned N bg "
            "run(s): ...`` summary."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "runs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "target": {
                                "type": "string",
                                "description": "Name of the guest to dispatch to.",
                            },
                            "instruction": {
                                "type": "string",
                                "description": "Instruction text given to target (required, non-empty).",
                            },
                            "task_id": {
                                "type": "string",
                                "description": "Optional: bound task id (must not be closed).",
                            },
                        },
                        "required": ["target", "instruction"],
                        "additionalProperties": False,
                    },
                    "description": "Batch of bg-run specs (≤4, target must not repeat).",
                },
            },
            "required": ["runs"],
            "additionalProperties": False,
        }

    async def async_execute(self, *, runs: list[dict[str, Any]], **_: Any) -> str:
        cb = self._resolve_callback()
        if cb is None:
            return "Error: bg run entry not wired (session has no server runtime)."
        if not isinstance(runs, list) or not runs:
            return "Error: runs must be a non-empty array."
        if len(runs) > MAX_AGENT_RUNS_PER_TOOL_CALL:
            return (
                f"Error: a single call may not exceed {MAX_AGENT_RUNS_PER_TOOL_CALL} "
                f"runs (got {len(runs)})."
            )
        # 工具层先做 shape + 同批次 dup 校验 —— **整批**校验过再下 server，让失败
        # 时返单条错误信息能定位到具体下标。
        seen: set[str] = set()
        parsed: list[tuple[str, str, Optional[str]]] = []
        for i, item in enumerate(runs):
            if not isinstance(item, dict):
                return f"Error: runs[{i}] must be an object."
            target = item.get("target")
            instruction = item.get("instruction")
            task_id = item.get("task_id")
            if not isinstance(target, str) or not target:
                return f"Error: runs[{i}].target must be a non-empty string."
            if not isinstance(instruction, str) or not instruction.strip():
                return f"Error: runs[{i}].instruction must be a non-empty string."
            instruction = instruction.strip()
            if task_id is not None and (
                not isinstance(task_id, str) or not task_id
            ):
                return f"Error: runs[{i}].task_id, if given, must be a non-empty string."
            if target in seen:
                return (
                    f"Error: runs[{i}].target={target!r} is duplicated in this batch "
                    "(one speak per guest at a time)."
                )
            seen.add(target)
            parsed.append((target, instruction, task_id))
        # 下到 server 单条创建。失败短路返 —— 已成功创建的 bg run **不**回滚（一旦
        # `_start_agent_run` 登记进 `agent_runs` + `active_guest_names`，wrapper 已
        # 在 event loop 里跑、cancel 是另一回事）。让茶客看到「前 k 条成功，第 k+1
        # 条因 ... 拒；可分别取消」的明确状态。这与 description 一致 —— description
        # 显式标注 shape 错全拒、create 错 partial-success。
        created: list[str] = []
        for i, (target, instruction, task_id) in enumerate(parsed):
            run_id, err = self._safe_call(
                cb, target=target, instruction=instruction, task_id=task_id,
            )
            if err is not None:
                err_en = _agent_run_err_en(err, target=target, task_id=task_id)
                if created:
                    return (
                        f"Error: runs[{i}] ({target}): {err_en}; "
                        f"first {len(created)} of this batch created "
                        f"(run_id: {', '.join(created)})."
                    )
                return f"Error: runs[{i}] ({target}): {err_en}"
            # err is None ↔ run_id is not None；显式护栏（assert 在 -O 模式被剥）。
            if run_id is None:
                return (
                    f"Error: runs[{i}] ({target}): server returned an empty run_id "
                    "(invariant violation)"
                )
            created.append(run_id)
        return (
            f"Successfully spawned {len(created)} bg run(s): "
            f"{', '.join(created)}."
        )


def register_agent_run_tools(
    agent: Any,
    *,
    source_guest: str,
    get_start_agent_run: Callable[[], Optional[StartAgentRun]],
) -> None:
    """把 ``spawn_agent_run`` / ``spawn_agent_runs`` 注册到 agentao agent。

    工厂签名刻意只吃 ``source_guest`` + ``get_start_agent_run``，**不**注入 server /
    RoomRuntime —— 工具实例只持闭包 getter，不持 runtime 引用，让切房后 server 改
    一处 ``guest.start_agent_run`` 槽位即可让所有工具实例立刻见新值（闭合契约）。

    与 :func:`chahua.task_tools.register_task_tools` /
    :func:`chahua.handoff_tools.register_handoff_tools` 并列调 —— ``TeaGuest.__init__``
    里一行注册。
    """
    agent.tools.register(
        SpawnAgentRunTool(
            source_guest=source_guest,
            get_start_agent_run=get_start_agent_run,
        )
    )
    agent.tools.register(
        SpawnAgentRunsTool(
            source_guest=source_guest,
            get_start_agent_run=get_start_agent_run,
        )
    )
