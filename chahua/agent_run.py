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


# ── P14: bg-run 拒绝原因码（双语 seam）─────────────────────────────────────────
# ``_start_agent_run`` 校验失败时返**无参数的原因码**（不再返成形中文句）；两个消费方
# 各自本地化 —— 所有插值参数（target / task_id / 房间 cap）在两个 render 点都可得：
#   - ``agent_run_start`` inbound → 中文 NOTICE（用户可见，P14 范围外，保持中文）
#   - ``spawn_agent_run(s)`` 工具 → 英文 ``Error:``（回灌 LLM，P14 英文化）
# 码无参数 → 避免脆弱的「中文句再正则回译」，两种语言由构造保证同步（docs/P14「双路 err」）。
AgentRunError = Literal[
    "target_absent", "task_not_found", "task_closed",
    "target_busy", "room_cap", "mts_manager",
]
AGENT_RUN_ERR_TARGET_ABSENT: AgentRunError = "target_absent"
AGENT_RUN_ERR_TASK_NOT_FOUND: AgentRunError = "task_not_found"
AGENT_RUN_ERR_TASK_CLOSED: AgentRunError = "task_closed"
AGENT_RUN_ERR_TARGET_BUSY: AgentRunError = "target_busy"
AGENT_RUN_ERR_ROOM_CAP: AgentRunError = "room_cap"
AGENT_RUN_ERR_MTS_MANAGER: AgentRunError = "mts_manager"


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

    mts_managed: bool = False
    """spawn 时刻 MTS 快照：``True`` 当且仅当 ``_start_agent_run`` 创建本 run 时
    房间 MTS 活、且 ``source_guest == ms.manager_guest``。bg wrapper finally 凭此
    决定是否在 run 结束后续命 MTS（enqueue 管理者复查 + 扣 1 budget），与 P11.2.X
    「每 bg 完成 → 调度管理者复查一次」语义对齐。

    **spawn 时刻冻结**，不跟随后续 MTS 状态变化 —— bg 跑期间 MTS 若被
    cancel/budget_exhausted 结束，wrapper finally 的「MTS 仍活」守卫会跳过续命。
    """

    mts_manager_at_spawn: Optional[str] = None
    """spawn 时刻 MTS 的 ``manager_guest`` 身份（当 ``mts_managed=True`` 时必填）。
    code-review F3 修：MTS 跑期间用户可能 stop + 重启新 MTS 换主，``advance_after_bg_completion``
    必须用本字段 vs 当前 ``ms.manager_guest`` 校验「这条 bg 对应的还是原管理者吗」，
    不匹配则跳过续命（不能把 Bob 复查任务硬塞给原本 Alice 该处理的工作）。``None``
    对应 ``mts_managed=False``。"""

    mts_session_id_at_spawn: Optional[int] = None
    """spawn 时刻 MTS 的会话身份（``ManagedSession`` 对象 ``id()``）。**Codex round 5
    P2**：仅校验 ``manager_guest`` 不够 —— 用户 stop MTS A、重启 MTS B 用同一管理者
    时旧守卫会通过、A 的 bg 完成时会污染 B 的 budget / queue。本字段冻结 spawn 时的
    ``ManagedSession`` 对象 id，``advance_after_bg_completion`` 比对 ``id(current_ms)``
    严格区分两个 MTS 实例。``None`` 对应 ``mts_managed=False``。"""

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
    mts_managed: bool = False,
    mts_manager_at_spawn: Optional[str] = None,
    mts_session_id_at_spawn: Optional[int] = None,
) -> AgentRun:
    """工厂 —— mint ``run_id`` + 打时间戳。

    inbound / ``spawn_*`` 工具走同一入口构造 ``AgentRun``，保证 ``run_id`` 形态与
    时间戳口径统一。``mts_managed`` / ``mts_manager_at_spawn`` / ``mts_session_id_at_spawn``
    由 ``_start_agent_run`` 在 spawn 时刻按 MTS 快照传入（三者一致：``mts_managed=True``
    时 manager 与 session_id 都非 None）。
    """
    return AgentRun(
        run_id=new_agent_run_id(),
        room_id=room_id,
        guest_name=guest_name,
        instruction=instruction,
        task_id=task_id,
        issued_by=issued_by,
        source_guest=source_guest,
        mts_managed=mts_managed,
        mts_manager_at_spawn=mts_manager_at_spawn,
        mts_session_id_at_spawn=mts_session_id_at_spawn,
        created_at_ms=now_ms(),
    )
