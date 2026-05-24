"""P11：后台 Agent run —— ``AgentRun`` 运行态值对象。

设计见 [`docs/P11-后台 Agent.md`](../docs/P11-后台 Agent.md) §「运行态」。

P11 把房间从「一个 in-flight」升级为「多个并发 bg run」。``AgentRun`` 是 bg run
的**运行期** dataclass：注册表 ``RoomRuntime.agent_runs`` 只装 running 项，
**不**含 ``status`` / ``error`` 字段 —— 终态信息留在 envelope 里下发，内存里不
维护「已完成」垃圾。

不 clone 茶客实例：一个 run 持茶客 ``guest_name``、运行时按名查活的 ``TeaGuest``
（与前台 ``ScoringOps.get_guest`` 同路径），不打额外引用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from .events import new_id, now_ms

# 谁触发了这条 bg run。
# - "user"：UI 弹窗 / ``/bg`` 斜杠命令 / ``agent_run_start`` inbound。
# - "agent"：茶客调 ``spawn_agent_run(s)`` 工具（P11.2）。
IssuedBy = Literal["user", "agent"]
AGENT_RUN_ISSUED_BY_USER: IssuedBy = "user"
AGENT_RUN_ISSUED_BY_AGENT: IssuedBy = "agent"


def new_agent_run_id() -> str:
    """``run_<10字节 hex>`` —— 与 :func:`chahua.events.new_message_id` 同长度，扫读易认。"""
    return new_id("run")


@dataclass(frozen=True, slots=True)
class AgentRun:
    """单条 bg run 的元数据。

    ``frozen=True`` 锁住所有字段 —— bg run 启动时全部确定，运行期不改写。终态信息
    走 envelope 下发，不回写 ``AgentRun`` 自身（设计不变量：注册表 running-only）。
    """

    run_id: str
    room_id: str
    guest_name: str
    """被指派的茶客名（不是 source_guest）。"""

    instruction: str
    """指令文字 —— 进 prompt 的 ``<agent_run_task>`` 块，必填非空（入站校验）。"""

    task_id: Optional[str] = None
    """启动时**冻结**的任务上下文。``None`` = 不绑任何任务。"""

    issued_by: IssuedBy = AGENT_RUN_ISSUED_BY_USER
    """谁触发了这条 run。"""

    source_guest: Optional[str] = None
    """``issued_by == "agent"`` 时的发起茶客名（``spawn_*`` 调用方）；``"user"`` 时为 ``None``。"""

    created_at_ms: int = field(default_factory=now_ms)
    """创建时间戳（Unix ms）。``default_factory`` 在每次实例化时调用 :func:`now_ms` —
    保证直接构造 ``AgentRun(...)`` 也能拿到真实时间戳（不靠 :func:`create` 工厂兜底），
    消除「epoch-0 默认值漏到 envelope」的脚枪。"""


def create(
    *,
    room_id: str,
    guest_name: str,
    instruction: str,
    task_id: Optional[str] = None,
    issued_by: IssuedBy = AGENT_RUN_ISSUED_BY_USER,
    source_guest: Optional[str] = None,
) -> AgentRun:
    """工厂 —— mint ``run_id`` + 打时间戳。

    inbound / ``spawn_*`` 工具走同一入口构造 ``AgentRun``，保证 ``run_id`` 形态与
    时间戳口径统一。
    """
    return AgentRun(
        run_id=new_agent_run_id(),
        room_id=room_id,
        guest_name=guest_name,
        instruction=instruction,
        task_id=task_id,
        issued_by=issued_by,
        source_guest=source_guest,
        created_at_ms=now_ms(),
    )
