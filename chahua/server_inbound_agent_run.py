"""P11.1 C8 + P11.2 C11：``agent_run_start`` / ``agent_run_cancel`` inbound handlers.

设计见 [`docs/P11-后台 Agent.md`](../docs/P11-后台 Agent.md) §「入口」。

:class:`AgentRunHandlers` 是 server 的第 6 个 slot —— 与 :class:`HandoffHandlers`
平行（**不**塞进 handoff slot：bg run 不是调度层产物、走独立 wrapper）。本类持
``self.server`` 反向引用，跨模块协作经 ``self.server.xxx``，与 handoff / task
slot 同口径。

承重不变量：

- ``instruction`` 必填非空（用户手动 / 工具 / 斜杠命令都不允许默认值兜底）。
- ``target`` 校验三道：在场 / 不 busy（``RoomRuntime.guest_busy``）/ 房间 bg run
  数未到 ``MAX_AGENT_RUNS_PER_ROOM`` 上限。
- ``task_id`` 可选；若给则必须存在且未关闭。
- 登记顺序：``agent_runs[run_id]=run`` + ``active_guest_names.add(target)``
  **同步发生**于 ``asyncio.create_task`` 之前，让同 target 的第二条 inbound 在
  wrapper 进 ``speak`` 之前就被 ``guest_busy`` 拒；``agent_run_tasks`` 紧随其后。
- ``agent_run_cancel``：从 ``agent_run_tasks`` 查 task → ``.cancel()``，**不**动
  注册表（wrapper finally 自己 pop / discard）；run_id 未知 → INFO + 静默放过
  （race：用户点 cancel 时 run 刚好自然完成）。

P11.2 C11 起 inbound 与 ``spawn_agent_run(s)`` 工具共用 ``server._start_agent_run``
helper —— 上面 4 道校验 + 登记 + emit started 都收敛到 server 单一路径，本 handler
只做 payload 白名单 / 字段类型校验 + 翻译 error 到 NOTICE。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import logging

from .agent_run import AGENT_RUN_ISSUED_BY_USER
from ._server_helpers import require_str as _require_str
from .events import (
    EnvelopeSink,
    NOTICE_LEVEL_ERROR,
)

if TYPE_CHECKING:
    from .server import ChahuaServer


_log = logging.getLogger(__name__)


# Wire inbound type 字面值（``server.py::_INBOUND_ROUTES`` 引用）。
INBOUND_AGENT_RUN_START = "agent_run_start"
INBOUND_AGENT_RUN_CANCEL = "agent_run_cancel"


# Payload 白名单——任何不在集合里的顶层键 → NOTICE error + 丢帧（docs §8.1
# "入站严格 / 落盘宽容"）。
_AGENT_RUN_START_ALLOWED = frozenset({"type", "target", "instruction", "task_id"})
_AGENT_RUN_CANCEL_ALLOWED = frozenset({"type", "run_id"})


# 每房间 bg run 数硬上限。docs §「Phases」P11.2：``max_agent_runs_per_room=4`` ——
# C8 inbound 与 C11 ``spawn_agent_run(s)`` 工具共用同一上限。
MAX_AGENT_RUNS_PER_ROOM = 4


class AgentRunHandlers:
    """``agent_run_start`` / ``agent_run_cancel`` inbound slot.

    持 :class:`chahua.server.ChahuaServer` 反向引用做依赖注入 —— 所有跨 server 状态
    （``_session`` / ``_emit_notice`` / ``_reject_unknown_keys`` /
    ``_foreground_runtime`` / ``_start_agent_run``）走 ``self.server.xxx``。
    """

    def __init__(self, server: "ChahuaServer") -> None:
        self.server = server

    async def _inbound_agent_run_start(
        self, data: dict, sink: EnvelopeSink,
    ) -> None:
        """``{"type":"agent_run_start","target":"<name>","instruction":"<text>",
        "task_id":"<id>"?}`` —— 用户手动 / ``/bg`` 斜杠命令的统一入口。

        校验顺序：① 白名单 → ② 必填字段类型 → ③ ``_start_agent_run`` 4 道（在场 /
        task / busy / cap）。任一道拒 → NOTICE error + 丢帧。

        通过即由 ``_start_agent_run`` 同步登记 + ``asyncio.create_task`` +
        emit ``AGENT_RUN_STARTED``（顺序：先 add 再 create_task，让第二条同 target
        inbound 在 wrapper 启动前就被 guest_busy 拒）。
        """
        srv = self.server
        if not srv._reject_unknown_keys(
            data, _AGENT_RUN_START_ALLOWED,
            where=INBOUND_AGENT_RUN_START, sink=sink,
        ):
            return
        target = _require_str(data, "target", where=INBOUND_AGENT_RUN_START)
        if target is None:
            return
        instruction = _require_str(
            data, "instruction", where=INBOUND_AGENT_RUN_START,
        )
        if instruction is None:
            return
        instruction = instruction.strip()
        if not instruction:
            srv._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_AGENT_RUN_START}: instruction 不能为空",
            )
            return

        task_id = data.get("task_id")
        if task_id is not None and not isinstance(task_id, str):
            srv._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_AGENT_RUN_START}: task_id 必须是 str 或缺省",
            )
            return

        runtime = srv._foreground_runtime
        # P11.2 C11：4 道校验 + 登记 + emit started 共调 server 路径。
        _run, err = srv._start_agent_run(
            runtime,
            target=target,
            instruction=instruction,
            task_id=task_id,
            issued_by=AGENT_RUN_ISSUED_BY_USER,
            source_guest=None,
        )
        if err is not None:
            srv._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_AGENT_RUN_START}: {err}",
            )
            return

    async def _inbound_agent_run_cancel(
        self, data: dict, sink: EnvelopeSink,
    ) -> None:
        """``{"type":"agent_run_cancel","run_id":"<id>"}`` —— 只 cancel 指定 run。

        不动注册表（wrapper finally 自己 pop / discard）；前台 cancel 按钮也不
        取消 bg run，反之亦然。run_id 未知 → INFO + 静默放过（race：用户点
        cancel 时 run 刚好自然完成）。
        """
        srv = self.server
        if not srv._reject_unknown_keys(
            data, _AGENT_RUN_CANCEL_ALLOWED,
            where=INBOUND_AGENT_RUN_CANCEL, sink=sink,
        ):
            return
        run_id = _require_str(data, "run_id", where=INBOUND_AGENT_RUN_CANCEL)
        if run_id is None:
            return

        runtime = srv._foreground_runtime
        task = runtime.agent_run_tasks.get(run_id)
        if task is None:
            _log.info(
                "agent_run_cancel: run_id=%r unknown or already finished",
                run_id,
            )
            return
        if not task.done():
            task.cancel()
        # wrapper finally 自己走 emit AGENT_RUN_CANCELLED + pop + discard。
