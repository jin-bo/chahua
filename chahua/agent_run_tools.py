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

from typing import Any, Callable, ClassVar, Optional

from agentao.tools import Tool


# 单工具调用一批次的上限。docs §「Phases」P11.2：``max_agent_runs_per_tool_call = 4``
# —— 按 P9 5 个后台房算，4 已覆盖典型「3 审稿 + 1 主持」。
MAX_AGENT_RUNS_PER_TOOL_CALL = 4


# Callback 协议：
#   (target: str, instruction: str, task_id: Optional[str], source_guest: Optional[str])
#     -> tuple[Optional[str], Optional[str]]
#       —— 成功返 (run_id, None)；任一校验拒返 (None, error_msg)。
# 由 server 端 ``_make_start_agent_run(runtime)`` 工厂返回，绑定到当前前台 runtime
# （切房 / 同房重建走 ``_attach_runtime_state`` 重新绑定）。
StartAgentRun = Callable[..., tuple[Optional[str], Optional[str]]]


class _SpawnBase(Tool):
    """两个 spawn 工具的共同基类 —— 共享 getter / source_guest 字段 + ``is_read_only``。

    ``get_start_agent_run`` 是一个 **getter**（不是直接的回调）—— 工具实例每次
    ``execute`` 现取一次：

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
        """每次 execute 现取 —— 闭合 lambda 逐次读 ``guest.start_agent_run`` instance attr。

        ``None`` 说明 server 端 ``_attach_runtime_state`` 还没装 —— 比如裸 session
        测试夹具 / build_room_session 阶段（未挂 server）。工具不抛、返 ``Error:``
        字符串（agentao 工具协议）。
        """
        return self._get_start_agent_run()


class SpawnAgentRunTool(_SpawnBase):
    """单条 bg run 直接调度。"""

    _kind = "single"

    @property
    def name(self) -> str:
        return "spawn_agent_run"

    @property
    def description(self) -> str:
        return (
            "立即把一条后台 run 派给某位茶客（与 propose_delegate 的区别：spawn 不等"
            "用户采纳、不阻塞当前回答，target 立刻进入后台执行）。"
            "并行后台执行用本工具或 spawn_agent_runs，**不要**用 propose_panel —— "
            "panel 仍是串行 drain。"
            "MTS 内可用，**绕开 budget 做并发分发**（不扣管理者预算 / 不计连发上限）。"
            "instruction 必填非空，会作为指令喂给 target 茶客；可选 task_id 把 bg run"
            "绑到指定任务上下文。"
            "拒绝条件：target 不在场 / 正忙（前台 / handoff / 已有 bg run）/ 房间 bg "
            "run 数到上限 / task_id 不存在或已关闭。失败时返 ``Error: ...`` 字符串。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "被指派的茶客名。"},
                "instruction": {
                    "type": "string",
                    "description": "派给 target 的指令文字（必填非空）。",
                },
                "task_id": {
                    "type": "string",
                    "description": "可选：绑定的任务 id（须未关闭）。",
                },
            },
            "required": ["target", "instruction"],
            "additionalProperties": False,
        }

    def execute(
        self,
        *,
        target: str,
        instruction: str,
        task_id: Optional[str] = None,
        **_: Any,
    ) -> str:
        cb = self._resolve_callback()
        if cb is None:
            return "Error: bg run 入口未装（session 未挂 server runtime）。"
        if not isinstance(target, str) or not target:
            return "Error: target 必须是非空字符串。"
        if not isinstance(instruction, str) or not instruction.strip():
            return "Error: instruction 必须是非空字符串。"
        instruction = instruction.strip()
        if task_id is not None and (not isinstance(task_id, str) or not task_id):
            return "Error: task_id 若给须为非空字符串。"
        run_id, err = cb(
            target=target,
            instruction=instruction,
            task_id=task_id,
            source_guest=self._source_guest,
        )
        if err is not None:
            return f"Error: {err}"
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
            "立即把一批后台 run 并发派给多位茶客（每位茶客一条 instruction）。"
            "并行后台执行用本工具，**不要**用 propose_panel —— panel 仍是串行 drain。"
            "MTS 内可用，**绕开 budget 做并发分发**（不扣管理者预算 / 不计连发上限）。"
            "runs[*].target 同批次不可重复（一茶客一时刻 1 个 speak）；批次大小 ≤ 4。"
            "若任一条因 target 不在场 / 正忙 / task_id 不存在等被拒，**整批**不创建，"
            "返 ``Error: ...`` 字符串列出原因。成功时返 ``Successfully spawned ...`` 摘要。"
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
                                "description": "被指派的茶客名。",
                            },
                            "instruction": {
                                "type": "string",
                                "description": "派给 target 的指令文字（必填非空）。",
                            },
                            "task_id": {
                                "type": "string",
                                "description": "可选：绑定的任务 id（须未关闭）。",
                            },
                        },
                        "required": ["target", "instruction"],
                        "additionalProperties": False,
                    },
                    "description": "批量 bg run 描述（≤4 条，target 不可重复）。",
                },
            },
            "required": ["runs"],
            "additionalProperties": False,
        }

    def execute(self, *, runs: list[dict[str, Any]], **_: Any) -> str:
        cb = self._resolve_callback()
        if cb is None:
            return "Error: bg run 入口未装（session 未挂 server runtime）。"
        if not isinstance(runs, list) or not runs:
            return "Error: runs 必须是非空数组。"
        if len(runs) > MAX_AGENT_RUNS_PER_TOOL_CALL:
            return (
                f"Error: 单次调用 runs 不能超过 {MAX_AGENT_RUNS_PER_TOOL_CALL} 条"
                f"（当前 {len(runs)} 条）。"
            )
        # 工具层先做 shape + 同批次 dup 校验 —— **整批**校验过再下 server，让失败
        # 时返单条错误信息能定位到具体下标。
        seen: set[str] = set()
        parsed: list[tuple[str, str, Optional[str]]] = []
        for i, item in enumerate(runs):
            if not isinstance(item, dict):
                return f"Error: runs[{i}] 必须是 object。"
            target = item.get("target")
            instruction = item.get("instruction")
            task_id = item.get("task_id")
            if not isinstance(target, str) or not target:
                return f"Error: runs[{i}].target 必须是非空字符串。"
            if not isinstance(instruction, str) or not instruction.strip():
                return f"Error: runs[{i}].instruction 必须是非空字符串。"
            instruction = instruction.strip()
            if task_id is not None and (
                not isinstance(task_id, str) or not task_id
            ):
                return f"Error: runs[{i}].task_id 若给须为非空字符串。"
            if target in seen:
                return (
                    f"Error: runs[{i}].target={target!r} 在本批次重复出现"
                    f"（一茶客一时刻 1 个 speak）。"
                )
            seen.add(target)
            parsed.append((target, instruction, task_id))
        # 下到 server 单条创建。失败短路返 —— 已成功创建的 bg run **不**回滚（一旦
        # `_start_agent_run` 登记进 `agent_runs` + `active_guest_names`，wrapper 已
        # 在 event loop 里跑、cancel 是另一回事）。让茶客看到「前 k 条成功，第 k+1
        # 条因 ... 拒；可分别取消」的明确状态。
        created: list[str] = []
        for i, (target, instruction, task_id) in enumerate(parsed):
            run_id, err = cb(
                target=target,
                instruction=instruction,
                task_id=task_id,
                source_guest=self._source_guest,
            )
            if err is not None:
                if created:
                    return (
                        f"Error: runs[{i}] ({target}): {err}；"
                        f"本批前 {len(created)} 条已创建（run_id: {', '.join(created)}）。"
                    )
                return f"Error: runs[{i}] ({target}): {err}"
            assert run_id is not None  # err is None ↔ run_id is not None
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
