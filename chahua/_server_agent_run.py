""":class:`AgentRunOps` —— ChahuaServer 的 P11 bg run / MTS 续命 slot.

设计见 [`docs/P11-后台 Agent.md`](../docs/P11-后台 Agent.md)。把原 ``server.py`` 里
``_start_agent_run`` / ``_run_agent_background`` + 整套 MTS 续命链（7 个 ``_continue_*``
/ ``_defer_*`` / ``_dispatch_*`` / ``_run_*`` / ``_start_*``）搬到本模块，``ChahuaServer``
保留 13 个薄转发方法（``_make_start_agent_run`` / ``_start_agent_run`` /
``_run_agent_background`` / ``_emit_agent_run_{started,terminal}`` /
``_handle_mts_bg_terminal_non_finished`` / ``_continue_mts_after_bg`` /
``_defer_mts_continuation_to_inflight`` / ``_defer_mts_non_finished_cleanup_to_inflight``
/ ``_run_mts_continuation_advance`` / ``_dispatch_mts_drain_or_defer`` /
``_defer_mts_dispatch_to_inflight`` / ``_start_mts_continuation_drain``）—— 保
``server._start_agent_run(...)`` / 测试夹具 ``srv._run_agent_background = fake`` 调用
口径不变。

slot 装配：经 :func:`chahua.server._install_handler_slots` 单点装到 ``srv._agent_run_ops``
（与 ``srv.admin`` / ``srv.handoff`` 等 inbound slot 平行；区别：bg run 不是 inbound
路由产物、不挂 ``_INBOUND_ROUTES``，是 server 自身的执行 wrapper / MTS 续命子域）。

承重不变量（搬迁前/搬迁后行为零差异，逐字保留 docs/P11 关键不变量原文注释）：

- ``_start_agent_run`` 4 道校验 + MTS-manager 互斥 → 同步登记 ``agent_runs`` +
  ``active_guest_names.add(target)`` **先于** ``asyncio.create_task`` —— race-safe
  by sequential-no-await。
- bg wrapper 6 步 finally（detect → flush propose → pop registry → emit terminal
  → MTS 续命 → discard guest_name → self_destruct_background_runtime）按序，
  任一步 try/except 不阻挡后续。
- MTS × bg run 续命闭环：F1 仅 ``AGENT_RUN_FINISHED`` 终态触发；deferral 链 counter
  保护 ``has_pending_mts_bg`` 守 MTS / busy_alive 守 runtime。F6 兜底
  ``end_managed_session(user_cancel)`` 防 MTS 悬挂。
- ``advance_after_bg_completion`` 每个 bg run 只跑一次（Codex round 7 P2）—— 与
  re-defer / re-dispatch 分离收敛在 :meth:`run_mts_continuation_advance` 单入口。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable, Optional

from .agent_run import (
    AGENT_RUN_ERR_MTS_MANAGER,
    AGENT_RUN_ERR_ROOM_CAP,
    AGENT_RUN_ERR_TARGET_ABSENT,
    AGENT_RUN_ERR_TARGET_BUSY,
    AGENT_RUN_ERR_TASK_CLOSED,
    AGENT_RUN_ERR_TASK_NOT_FOUND,
    AGENT_RUN_ISSUED_BY_AGENT,
    AgentRun,
    IssuedBy,
    create as create_agent_run,
)
from .agent_run_sink import BatchMessageSink
from .context_renderer import render_agent_run_block
from .events import (
    ChahuaEnvelope,
    ChahuaEventType,
    new_turn_id,
)
from .handoff import MANAGED_SESSION_REASON_USER_CANCEL
from .room_runtime import RoomRuntime
from .server_inbound_agent_run import MAX_AGENT_RUNS_PER_ROOM
from .server_inbound_handoff import INFLIGHT_KIND_HANDOFF
from .tasks_store import CLOSED_STATUSES

if TYPE_CHECKING:
    from .server import ChahuaServer

_log = logging.getLogger(__name__)


class AgentRunOps:
    """P11 bg run 执行 wrapper + MTS 续命链 slot.

    持 :class:`chahua.server.ChahuaServer` 反向引用 —— 与 inbound handler slot 同
    口径（``self.server.xxx``）。跨槽位调用：
    - ``self.server._run_handoff_turn`` —— MTS 续命起 drain 时复用 server 端 wrapper
      （cancel-safe + finally 同槽清 ``(_inflight_turn_task, _inflight_kind)``）。
    - ``self.server._maybe_self_destruct_background_runtime`` —— 后台 runtime 自毁
      判定，多个 finally 调用点共用。
    """

    def __init__(self, server: "ChahuaServer") -> None:
        self.server = server

    def make_start_agent_run(
        self, runtime: RoomRuntime,
    ) -> Callable[..., tuple[Optional[str], Optional[str]]]:
        """工厂：返回绑定 ``runtime`` 的 ``start_agent_run`` 回调。

        回调签名 ``(target, instruction, task_id, source_guest) -> (run_id, error)``：
        成功返 ``(run_id, None)``；任一校验拒返 ``(None, err_code)`` —— ``err_code`` 是
        ``agent_run.AgentRunError`` 无参数原因码（P14），工具侧本地化成英文 ``Error: ...``
        作为 tool result，inbound 侧本地化成中文 NOTICE error。

        ``issued_by`` 在工具路径恒 ``"agent"``（spawn 工具是茶客调用产物）；inbound
        路径不经此回调、直接调 ``_start_agent_run`` 传 ``"user"``。

        **task_id 默认值**：调用方传 ``None`` 时闭包会默认填房间当前 active task_id
        （经 ``orchestrator.snapshot_active_task_id()``）。原因：茶客 prompt 上下文里
        看不到 task_id 字符串值（``<current_task>`` 块只暴露 ``status`` 属性，不暴露 id），
        ``spawn_agent_run`` 工具的可选 ``task_id`` 参数对茶客事实上不可达；默认绑 active
        让管理者派出去的 bg agent 自动拿到 ``<current_task>`` 上下文（plan / status 文件
        清单），否则 bg agent 只能凭 instruction 字面摸黑工作。房间无活跃任务时
        ``snapshot_active_task_id()`` 返 ``None``，行为与改前一致。**inbound 不走此闭包**
        （直接调 ``_start_agent_run``），UI 端的 ``agent_run_start`` 帧仍按用户显式意图
        传 task_id，不做默认。
        """
        def start(
            *,
            target: str,
            instruction: str,
            task_id: Optional[str] = None,
            source_guest: Optional[str] = None,
        ) -> tuple[Optional[str], Optional[str]]:
            if task_id is None:
                task_id = runtime.session.orchestrator.snapshot_active_task_id()
            # 经 ``self.server._start_agent_run`` forwarder 而非直调 ``self.start_agent_run``
            # —— 让 ``srv._start_agent_run = fake`` 测试 / debug monkeypatch 在 inbound
            # C8 与 guest-tool spawn 两条路径上效果对称（pre-refactor 闭包行为）。
            run, err = self.server._start_agent_run(
                runtime,
                target=target,
                instruction=instruction,
                task_id=task_id,
                issued_by=AGENT_RUN_ISSUED_BY_AGENT,
                source_guest=source_guest,
            )
            if err is not None:
                return None, err
            assert run is not None  # err is None ↔ run is not None
            return run.run_id, None

        return start

    def start_agent_run(
        self,
        runtime: RoomRuntime,
        *,
        target: str,
        instruction: str,
        task_id: Optional[str],
        issued_by: IssuedBy,
        source_guest: Optional[str],
    ) -> tuple[Optional[AgentRun], Optional[str]]:
        """C8 inbound + C11 ``spawn_*`` 工具共调的单一登记 / 启动路径。

        校验顺序与 ``server_inbound_agent_run`` 一致：① target 在场 → ② task_id
        若给须未关闭 → ③ ``guest_busy(target) == False`` → ④ 房间 bg run 数未到
        ``MAX_AGENT_RUNS_PER_ROOM`` 上限。返 ``(None, err_code)`` 任一校验拒（P14：
        ``err_code`` 是 ``agent_run.AgentRunError`` 无参数原因码），调用方负责本地化
        成 NOTICE（中文）/ tool result（英文）。

        成功路径同步登记 ``agent_runs[run_id]=run`` + ``active_guest_names.add(target)``
        **先于** ``asyncio.create_task`` —— 让同 target 的第二条请求在 wrapper 进
        speak 之前就被 ``guest_busy`` 拒（race 不变量，docs §「运行态」）。

        emit ``AGENT_RUN_STARTED`` 走 ``runtime.router``（与 wrapper 的
        ``_emit_agent_run_terminal`` 同口径）；前端 ``room_info.background_runs``
        快照 + 实时 envelope 两条线一致。
        """
        # P14：校验失败返**无参数原因码**，由调用方按消费方语言本地化（inbound→中文
        # NOTICE / tool→英文 Error），见 ``agent_run.AgentRunError`` 注释。
        # 1. target 在场。
        if target not in runtime.session.orchestrator.guest_names:
            return None, AGENT_RUN_ERR_TARGET_ABSENT

        # 2. task_id 若给须存在且未关闭。变量名 ``task_obj`` 避免与下方
        #    ``asyncio.create_task`` 返回值 ``task`` 冲突（同函数内同名 shadow 是
        #    重构脚枪）。
        if task_id is not None:
            task_obj = runtime.session.tasks_store.get_task(task_id)
            if task_obj is None:
                return None, AGENT_RUN_ERR_TASK_NOT_FOUND
            if task_obj.status in CLOSED_STATUSES:
                return None, AGENT_RUN_ERR_TASK_CLOSED

        # 3. target 不 busy（含前台 / handoff / 已有 bg run）。
        if runtime.guest_busy(target):
            return None, AGENT_RUN_ERR_TARGET_BUSY

        # 4. 房间 bg run 数上限。
        if len(runtime.agent_runs) >= MAX_AGENT_RUNS_PER_ROOM:
            return None, AGENT_RUN_ERR_ROOM_CAP

        # P11.2.X 快照 MTS 状态：读 ``managed_session`` 公开 @property（code-review F9
        # 改：原 ``getattr(orch, '_managed_session', None)`` 默认值会静默吞 @property
        # body 内的 AttributeError 让真初始化 bug 退化为「mts_managed 永远 False」；
        # 改直接走 @property 让 init bug 立即抛出，测试 fake 必须显式装一致的 attr）。
        ms = runtime.session.orchestrator.managed_session

        # 5. F12：MTS 活时拒绝 ``target == manager_guest`` —— 否则该 bg run 期间 bg agent
        # 调 ``propose_delegate`` / ``propose_panel`` 会满足 ``proposer == manager`` 被
        # ``intercept_task_proposal`` 自动入 MTS 队列污染托管会话；且本 bg 是 user 手起
        # （``mts_managed=False`` 因 ``source_guest`` 通常 None）→ 不会触发 step ⑤ 续命，
        # 工作量错配。MTS 内要让管理者本人发言，请用前台 ``@manager`` 或 stop MTS 再交互。
        if (
            ms is not None
            and target == ms.manager_guest
        ):
            return None, AGENT_RUN_ERR_MTS_MANAGER

        # ── 校验通过：登记 + 启动 wrapper + emit started ──
        # P11.2.X：spawn 时刻 MTS 快照 —— 当且仅当 MTS 活、且 source_guest 就是当前
        # 管理者本人时，标记本 bg 为「manager-attributed」。bg wrapper finally 凭此
        # 决定要不要在 run 结束时续命 MTS（enqueue 管理者复查 + 扣 1 budget）。
        # 用户手起的 bg（issued_by="user", source_guest=None）天然 False；其他茶客
        # spawn 本管理者也不算 manager-attributed —— 只有管理者自己 spawn 才算。
        mts_managed = (
            ms is not None
            and source_guest is not None
            and source_guest == ms.manager_guest
        )
        # F3：冻结 spawn 时刻 manager 身份，wrapper finally 凭此校验「跑完时还是同一个
        # 管理者吗」—— 防 MTS 跑期间换主把 Alice 的复查塞给 Bob。
        # Codex round 5 P2：再加 session_id（``ManagedSession`` 对象 id）严格区分跨
        # MTS 实例 —— 同管理者重启新 MTS 时旧 bg 不能续命到新 MTS（污染 budget/queue）。
        mts_manager_at_spawn = ms.manager_guest if mts_managed else None
        mts_session_id_at_spawn = ms.session_id if mts_managed else None
        run = create_agent_run(
            room_id=runtime.room_id,
            guest_name=target,
            instruction=instruction,
            task_id=task_id,
            issued_by=issued_by,
            source_guest=source_guest,
            mts_managed=mts_managed,
            mts_manager_at_spawn=mts_manager_at_spawn,
            mts_session_id_at_spawn=mts_session_id_at_spawn,
        )
        # 登记顺序：**先** ``active_guest_names.add(target)`` —— ``guest_busy``
        # 的唯一数据源，CLAUDE.md §「运行态」明确「add 必先于任何 await」。再
        # ``agent_runs[run_id]=run`` 填注册表，最后 ``asyncio.create_task`` 起
        # wrapper。当前段三步同步执行无 await，race-safe；显式按顺序写还能让未来
        # 维护者在两步之间加 await 时立刻意识到「需要把 add 提到 await 之前」。
        runtime.active_guest_names.add(target)
        runtime.agent_runs[run.run_id] = run
        # 经 server forwarder 起 wrapper —— 测试夹具可直接 ``srv._run_agent_background
        # = fake`` 替换（instance attr 优先于 class forwarder），不必触碰 slot。
        bg_task = asyncio.create_task(
            self.server._run_agent_background(runtime, run),
        )
        runtime.agent_run_tasks[run.run_id] = bg_task
        # emit 走 runtime.router —— 与 wrapper terminal 同口径，前台 ws_sink 落
        # 同一帧通道；BatchMessageSink 显式 DROP AGENT_RUN_STARTED 防穿越。
        # 经 ``self.server._emit_agent_run_started`` forwarder（非直调 slot）—— 与
        # ``self.server._start_agent_run`` 同口径，让监控 / 测试的 spy override 一致生效。
        self.server._emit_agent_run_started(runtime, run)
        return run, None

    def emit_agent_run_started(
        self, runtime: RoomRuntime, run: AgentRun,
    ) -> None:
        """构造并下发 ``AGENT_RUN_STARTED`` envelope（走 ``runtime.router``）。

        data 字段与 :func:`chahua.server_room_snapshot._project_agent_runs` 投影 /
        :meth:`emit_agent_run_terminal` 同构 —— 前端单点渲染逻辑共用。
        ``instruction_preview`` 截 30 字（与 inbound C8 emit 同长度，前端列表条
        固定预算）。
        """
        preview = run.instruction
        if len(preview) > 30:
            preview = preview[:30]
        data: dict = {
            "run_id": run.run_id,
            "guest_name": run.guest_name,
            "issued_by": run.issued_by,
            "instruction_preview": preview,
            "created_at_ms": run.created_at_ms,
        }
        if run.task_id is not None:
            data["task_id"] = run.task_id
        if run.source_guest is not None:
            data["source_guest"] = run.source_guest
        runtime.router(
            ChahuaEnvelope(
                room_id=runtime.session.room.name,
                turn_id=None,
                guest_name=run.guest_name,
                message_id=None,
                type=ChahuaEventType.AGENT_RUN_STARTED,
                data=data,
            )
        )

    async def run_agent_background(
        self, runtime: RoomRuntime, run: AgentRun,
    ) -> None:
        """P11：单条 bg run 的执行 wrapper —— 由 inbound (C8) / 工具 (C11) 经
        ``asyncio.create_task`` 启动，挂在 ``runtime.agent_run_tasks[run.run_id]``。

        承载语义：bg run **不走** ``run_pending_handoff()``（handoff 是队列串行），
        **也不走** ``_let_speak()`` (前台 turn 才用)，wrapper 自己补回：
        ① 直接调 ``TeaGuest.speak(..., record_debug=False, sink=BatchMessageSink)``；
        ② 成功拿到 ``msg`` 后 ``orch.cursor.set(guest_name, msg.seq)`` —— 补
        ``_let_speak`` 的 cursor 推进；否则 bg 茶客下次 onboarding 把自己的产物
        当未读重喂。
        ③ **不**改 ``_cooldown`` / ``_consecutive_ai_turns`` —— bg run 与前台调度
        独立，绝不污染前台节奏；
        ④ ``task_id`` 启动时**冻结**（用 ``run.task_id``），其它字段（``<recent_messages>``
        / ``<room>`` / ``<room_summary>``）走 live 视图，与 handoff drain 口径一致。

        wrapper 顺序见 docs/P11 §「wrapper finally 顺序」（两层 try/finally + 每步
        best-effort）：

        外层 finally 唯一职责：``runtime.active_guest_names.discard(guest_name)`` ——
        保证任一步抛异常 target 必定释放（前台 / 同 target 第二条 inbound 能解锁）。
        随后 ``_maybe_self_destruct_background_runtime`` 让「只剩 bg run」收尾的
        后台 runtime 自毁(P9 既有触发点没覆盖纯 bg run 场景)。

        内层 finally 按序：
          ① detect new artifacts —— 经 router 直发（绕开 BatchMessageSink）；
          ② flush propose 缓冲；
          ③ pop ``agent_runs`` / ``agent_run_tasks``（**无条件**，先于 emit terminal
             —— 避免快照在间隙重投死 run）；
          ④ emit terminal envelope（AGENT_RUN_FINISHED / _CANCELLED / _ERROR）。

        terminal_type 三分流：``speak`` 返 ``msg`` → FINISHED；``CancelledError``
        重抛 → CANCELLED（让 asyncio task 正确 cancelled）；``msg is None`` 或
        其它异常 → ERROR。**不**扩展 ``AgentRun.status``，注册表仍 running-only。

        每步 try/except + WARN —— 不阻挡 finally 后续步骤；最坏情况 emit terminal
        失败仍跑外层 discard（前端经 ``room_info.background_runs`` 切回房重建一致性）。
        """
        run_id = run.run_id
        guest_name = run.guest_name
        terminal_type = ChahuaEventType.AGENT_RUN_ERROR
        msg = None
        # batch_sink 在「context 构建失败前」可能为 None —— finally flush 阶段必须先判。
        batch_sink: Optional[BatchMessageSink] = None
        try:
            try:
                guest = runtime.session.orchestrator.get_guest(guest_name)
                if guest is None:
                    # 极小竞态：登记 run 与 wrapper 启动之间该茶客被移除；记录后由外层
                    # finally 走 emit ERROR + discard，与「正常异常路径」语义一致。
                    raise RuntimeError(
                        f"bg run target {guest_name!r} not in orchestrator"
                    )
                # context_message：onboarding 走 cursor.get，附 <agent_run_task> 块
                # 注入指令。task_id 已经在 AgentRun 上冻结，本轮所有 message_* /
                # transcript 落盘也带这个 tag（P5/P6 口径）。
                ctx = runtime.session.orchestrator._build_context_for(
                    guest_name,
                    task_id=run.task_id,
                    extra_blocks=[
                        render_agent_run_block(
                            instruction=run.instruction,
                            issued_by=run.issued_by,
                            source_guest=run.source_guest,
                        ),
                    ],
                )
                batch_sink = BatchMessageSink(
                    run_id=run_id, downstream=runtime.router,
                )
                turn_id = new_turn_id()
                try:
                    msg = await guest.speak(
                        ctx,
                        turn_id=turn_id,
                        sink=batch_sink,
                        task_id=run.task_id,
                        record_debug=False,  # bg run 不进 debug 视图（P11.1 §「TurnRecorder 串台修复」）
                    )
                    if msg is not None:
                        terminal_type = ChahuaEventType.AGENT_RUN_FINISHED
                        # 补 _let_speak 的 cursor 推进 —— bg 茶客下次 onboarding 不重喂自己。
                        # 不改 _cooldown / _consecutive_ai_turns（设计 §「后台执行路径」）。
                        runtime.session.orchestrator.cursor.set(guest_name, msg.seq)
                except asyncio.CancelledError:
                    terminal_type = ChahuaEventType.AGENT_RUN_CANCELLED
                    raise
            except asyncio.CancelledError:
                # 重抛到 wrapper 外层让 asyncio task 标 cancelled —— 内层 finally 已跑。
                raise
            except Exception:
                # 非 cancel 异常（如 get_guest=None race / _build_context_for 失败）—— log
                # + swallow，让 terminal_type 保持 ERROR、外层 finally 正常收尾 +
                # 注册表清空。不 swallow 会让 asyncio task 抛出未捕获异常 + 前端经
                # room_info 重建时仍能见死 run。
                _log.exception("bg run %s: wrapper inner crashed", run_id)
            finally:
                # ① detect new artifacts —— 经 router 直发（绕开 batch_sink）。
                try:
                    runtime.session.orchestrator._kick_detect_new_artifacts(
                        runtime.router, run.task_id,
                    )
                except Exception:
                    _log.warning(
                        "bg run %s: _kick_detect_new_artifacts failed",
                        run_id, exc_info=True,
                    )
                # ② flush propose 缓冲 —— context 构建失败前 batch_sink 仍是 None,
                # 跳过即可（没有缓冲可 flush）。
                if batch_sink is not None:
                    try:
                        batch_sink.flush_to(runtime.router)
                    except Exception:
                        _log.warning(
                            "bg run %s: flush_propose_buffer failed",
                            run_id, exc_info=True,
                        )
                # ③ pop 注册表（先于 emit terminal——避免 snapshot 在间隙重投死 run）。
                runtime.agent_runs.pop(run_id, None)
                runtime.agent_run_tasks.pop(run_id, None)
                # ④ emit terminal envelope。经 ``self.server._emit_agent_run_terminal``
                # forwarder 让 spy override 与 ``_emit_agent_run_started`` 同口径。
                try:
                    self.server._emit_agent_run_terminal(
                        runtime, run, terminal_type, msg,
                    )
                except Exception:
                    _log.warning(
                        "bg run %s: emit terminal failed", run_id, exc_info=True,
                    )
                # ⑤ P11.2.X MTS 续命：本 run 是 manager-attributed（spawn 时刻管理者
                # 本人 spawn 且 MTS 活）且**正常完成**（非 CANCELLED / ERROR）→ 扣 1
                # budget + enqueue 管理者复查 + emit advanced；若房间已无 inflight →
                # 主动起 _run_handoff_turn 消费；若 user turn 在跑 → 注册 done_callback
                # 等 user turn 完成后再起 drain。MTS 已结束 / 换主 / stop_reason 命中
                # → ``advance_after_bg_completion`` 守卫返 False 跳过续命。
                #
                # 守卫细节（code-review F1/F4/F6/F8）：
                # - F1：CANCELLED / ERROR 终态不续命。用户主动 cancel 了 bg、或 bg 内
                #   部 crash —— 这两类「不算管理者复查触发」（reason 文案『已完成』也
                #   不诚实）。FINISHED 才是「真正交付了产物」可触发管理者复查。
                # - F4：user turn 在跑时不要硬抢 inflight 槽，但 stranded 复查项不能等
                #   下次 user_message 才被动 pick up（budget 已扣、advanced 已 emit）。
                #   用 ``add_done_callback`` 让 user turn 完成后立刻起 drain。
                # - F6：``create_task`` 失败（如 event loop closing）→ MTS 状态已不一
                #   致（budget 减、enqueue、advanced 发完但 drain 起不来）→ 兜底
                #   ``end_managed_session(user_cancel)`` 让 MTS 显式 terminate 而非
                #   悬挂，前端能看到 ENDED 而非永久『等待中』。
                # - F8：原 ``assert ms is not None`` 在 python -O 下被剥光，TOCTOU 也
                #   可race。改 ``if ms is None: return`` 显式分支。
                if run.mts_managed:
                    if terminal_type is ChahuaEventType.AGENT_RUN_FINISHED:
                        self.server._continue_mts_after_bg(
                            runtime, run, run_id=run_id, guest_name=guest_name,
                        )
                    else:
                        # Codex round 8 P2 / P8.4 §3.2：CANCELLED / ERROR 终态 ——
                        # 无 review 可加。本 bg 若是最后一个 pending mts bg + 队列空
                        # → MTS 保持 dormant（P8.4 退役 ``MANAGER_FINISHED`` 后不再
                        # 兜底终结）；队列有项 → 起 drain 让既有 5 reason 收尾。
                        self.server._handle_mts_bg_terminal_non_finished(
                            runtime, run_id=run_id,
                        )
        finally:
            # 外层 finally 唯一职责：discard guest_name + 自毁判定。
            runtime.active_guest_names.discard(guest_name)
            try:
                self.server._maybe_self_destruct_background_runtime(runtime)
            except Exception:
                _log.warning(
                    "bg run %s: self destruct failed", run_id, exc_info=True,
                )

    def handle_mts_bg_terminal_non_finished(
        self, runtime: RoomRuntime, *, run_id: str,
    ) -> None:
        """Codex round 8 / 9 P2 修：mts_managed bg 以 CANCELLED / ERROR 终态结束的兜底。

        ``advance_after_bg_completion`` 不能用（没有 review 该 enqueue），但需要保证
        MTS 不被这条 bg 永久守在「等 review」状态。本方法按以下顺序判断：

        - inflight 有 → **defer 到 inflight done** 后递归 re-evaluate。**Codex round 9
          P2**：原版假设「inflight 自然 evaluate」只对 handoff drain 成立 —— ``_run_turn``
          (user turn) 没有 MTS guard 逻辑，user turn 结束只清 inflight 不动 MTS，会让
          MTS 永久卡。Defer 到 inflight done 强制 re-evaluate。
        - 仍有其他 pending mts bg（agent_runs 中 mts_managed=True 的 / counter>0）
          → 等它们完成回调时统一评估，不动。
        - 队列还有项 → 起一个 drain 让 ``run_pending_handoff`` 跑完（队列项跑完自然
          触发 advance_after_turn → stop_reason → end_managed_session）。
        - **P8.4 §3.2**：队列空 + 无 inflight + 无其他 mts bg + MTS 仍活 → **保持
          dormant，不终结 MTS**。原 P8.3 行为是 ``end_managed_session(MANAGER_FINISHED)``
          ——「最后一个 bg 失败 / 取消，管理者没有任何后续可做」终结语义。P8.4 退役
          ``MANAGER_FINISHED`` 后改为待机：管理者「这一棒断了」≠「事情完事」，由用户
          下一句话 / 重派 bg / 显式 stop 决定下一步。用户看到 bg 失败气泡 + 状态条
          仍「待机中」，自然反应即可。
        """
        if runtime.inflight_alive():
            # Codex round 9 P2: 不能 return 走人，必须 defer 重 evaluate。
            self.server._defer_mts_non_finished_cleanup_to_inflight(
                runtime, run_id=run_id,
            )
            return
        if runtime.has_pending_mts_bg():
            return
        ms = runtime.session.orchestrator.managed_session
        if ms is None:
            return
        if runtime.session.orchestrator._handoff_queue:
            # 队列有其他项，起 drain 让 run_pending_handoff 跑完，自然走 stop_reason 收尾。
            try:
                self.server._start_mts_continuation_drain(runtime, ms.task_id, run_id)
            except Exception:
                _log.warning(
                    "bg run %s: trigger drain after non-FINISHED failed",
                    run_id, exc_info=True,
                )
            return
        # P8.4：保持 dormant —— 不再 end_managed_session(MANAGER_FINISHED)。
        _log.info(
            "bg run %s: non-FINISHED terminal, no follow-up — MTS stays dormant",
            run_id,
        )

    def defer_mts_non_finished_cleanup_to_inflight(
        self, runtime: RoomRuntime, *, run_id: str,
    ) -> None:
        """Codex round 9 P2 helper：把 ``handle_mts_bg_terminal_non_finished``
        的再评估延迟到当前 inflight done 后跑。

        counter += 1 让 ``has_pending_mts_bg()`` 持续守住 MTS / busy_alive 守住
        runtime；callback 中递归调 ``handle_mts_bg_terminal_non_finished``
        （它可能再 defer，链长被 inflight 完成自然封顶）。
        """
        inflight_task = runtime.inflight_task
        if inflight_task is None or inflight_task.done():
            # 极端 race：被调用前 inflight 又清了 → 直接 evaluate（避免无谓 defer）。
            self.server._handle_mts_bg_terminal_non_finished(runtime, run_id=run_id)
            return
        runtime.pending_mts_continuations += 1
        runtime_ref = runtime
        run_id_snap = run_id

        def _on_inflight_done(_done_task: object) -> None:
            # Codex round 10 P2：必须**先**减 counter 再调 handler ——
            # handler 内部 ``runtime.has_pending_mts_bg()`` 会读 counter；如果
            # counter 还含本 callback 自身，handler 自检 has_pending=True 直接 return
            # → MTS 卡死。先减让 has_pending 正确反映「还有别的 bg / 别的 defer 吗」。
            # 若 handler 触发新 defer，那个 defer 会再 +1 counter，与现在的 -1 平衡。
            runtime_ref.pending_mts_continuations -= 1
            try:
                self.server._handle_mts_bg_terminal_non_finished(
                    runtime_ref, run_id=run_id_snap,
                )
            except Exception:
                _log.warning(
                    "bg run %s: deferred non-FINISHED cleanup failed",
                    run_id_snap, exc_info=True,
                )
            try:
                self.server._maybe_self_destruct_background_runtime(runtime_ref)
            except Exception:
                _log.warning(
                    "bg run %s: deferred non-FINISHED self_destruct failed",
                    run_id_snap, exc_info=True,
                )

        inflight_task.add_done_callback(_on_inflight_done)

    def continue_mts_after_bg(
        self,
        runtime: RoomRuntime,
        run: AgentRun,
        *,
        run_id: str,
        guest_name: str,
    ) -> None:
        """P11.2.X step ⑤ 实现：MTS 续命 + 起 drain。

        合 code-review F1 / F4 / F6 / F8 / F9 + **Codex P2 deferral** 六项修订 ——
        调用方已守卫 ``run.mts_managed == True`` + ``terminal_type == AGENT_RUN_FINISHED``，
        本方法只关心后续路由。任一步抛异常都吞 WARN，外层 finally 继续走。

        路由（按 inflight 状态分流）:
        - **无 inflight** → 立即 ``run_mts_continuation_advance``（同步跑 advance +
          起 drain）。这是「单 bg / drain 已 idle」的快速路径。
        - **有 inflight（handoff drain or user turn）** → 延迟到 inflight done 后再
          跑 advance + 起 drain；同时 ``runtime.pending_mts_continuations += 1`` 让
          P11.2.X ``has_pending_mts_bg()`` 谓词仍返 True（dispatch / non-FINISHED
          兜底据此判断「还有 bg 在跑、再等等」；P8.4 drain 收尾已不依赖该谓词，
          dormant 是默认行为）。

        **为何 handoff drain 也要 defer**（Codex P2 finding）：原版让 handoff drain
        在 peek 阶段自然消费 enqueue 的 review；但若 bg 在 drain 跑 manager turn 时
        completion，``advance_after_bg_completion`` 会同步 decrement budget。drain 的
        ``advance_after_turn`` 在 manager turn 结束时调 ``stop_reason()`` 看到
        ``budget <= 0`` → 触发 ``end_managed_session(BUDGET_EXHAUSTED)`` → 清队列
        → review 丢失。延迟到 drain done 后再 decrement / enqueue 就避开了这段窗口
        （那时 drain 已退出，advance_after_turn 不会再跑）。

        异常兜底（F6）：deferred 路径 / 直接路径任一步抛 → ``end_managed_session
        (user_cancel)`` 防 MTS 悬挂。
        """
        inflight_task = runtime.inflight_task
        if inflight_task is None or inflight_task.done():
            # 无 inflight → 立即跑（drain 已 idle，advance 安全无并发）。
            self.server._run_mts_continuation_advance(
                runtime, run, run_id=run_id, guest_name=guest_name,
            )
            return
        # 有 inflight（handoff 或 user turn）→ 延迟到 done。
        self.server._defer_mts_continuation_to_inflight(
            runtime, run, run_id=run_id, guest_name=guest_name,
        )

    def defer_mts_continuation_to_inflight(
        self,
        runtime: RoomRuntime,
        run: AgentRun,
        *,
        run_id: str,
        guest_name: str,
    ) -> None:
        """Schedule ``run_mts_continuation_advance`` 等当前 inflight done 后再跑。

        counter += 1 让 ``has_pending_mts_bg()`` 守住 MTS / busy_alive 守住 runtime；
        callback 中跑 advance 后 counter -= 1 + 补一次 self_destruct 判定（round 4
        P2-B）。**Codex round 6 P2**：本 helper 也被 ``run_mts_continuation_advance``
        递归调用 —— 当 advance 后发现 inflight 被新 user turn race 抢占（典型：上一
        inflight done 与 deferred callback 之间用户发了新消息），就再 defer 到新
        inflight 的 done，避免 review stranded。

        递归终止：每次只挂一个 done_callback 到当前 inflight；inflight 必有限完成
        （asyncio 任务），链长被用户消息频率自然封顶。
        """
        inflight_task = runtime.inflight_task
        if inflight_task is None or inflight_task.done():
            # 极端 race：被调用前 inflight 已清 → 直接跑 advance。
            self.server._run_mts_continuation_advance(
                runtime, run, run_id=run_id, guest_name=guest_name,
            )
            return
        runtime.pending_mts_continuations += 1
        runtime_ref = runtime
        run_ref = run
        run_id_snap = run_id
        guest_name_snap = guest_name

        def _on_inflight_done(_done_task: object) -> None:
            try:
                self.server._run_mts_continuation_advance(
                    runtime_ref, run_ref,
                    run_id=run_id_snap, guest_name=guest_name_snap,
                )
            except Exception:
                _log.warning(
                    "bg run %s: deferred MTS continuation failed",
                    run_id_snap, exc_info=True,
                )
            finally:
                runtime_ref.pending_mts_continuations -= 1
                # Codex round 4 P2-B：counter 归 0 后必须再补一次自毁判定 ——
                # 否则当 callback 返 False 不起新 drain（MTS 已 end / 换主 / budget
                # 全消耗），busy_alive 此刻为 False 但 inflight 的 finally 早已跑过
                # self_destruct（被 counter>0 拒），无人再触发 → background runtime
                # 留死。``_maybe_self_destruct_background_runtime`` 自带「前台 runtime
                # 不动」+「busy 不动」的双层守卫，重复调用安全幂等。
                try:
                    self.server._maybe_self_destruct_background_runtime(runtime_ref)
                except Exception:
                    _log.warning(
                        "bg run %s: deferred self_destruct check failed",
                        run_id_snap, exc_info=True,
                    )

        inflight_task.add_done_callback(_on_inflight_done)

    def run_mts_continuation_advance(
        self,
        runtime: RoomRuntime,
        run: AgentRun,
        *,
        run_id: str,
        guest_name: str,
    ) -> None:
        """跑 ``advance_after_bg_completion`` 一次 + 转交 dispatch（start drain 或
        re-defer）。

        **承重契约（Codex round 7 P2）**：advance **每个 bg run 只能跑一次** —— 否则
        会重复扣 budget 重复 enqueue review。本方法跑 advance 后立即 hand off 给
        :meth:`dispatch_mts_drain_or_defer`，后者只做调度（start drain / 再次 defer）
        不再 touch advance。re-defer 的 callback 直接调 ``dispatch_*``，不会回到
        本方法。

        共用于直接路径（无 inflight，``continue_mts_after_bg`` 直调）和 deferred 路径
        （inflight done callback）—— 两路径都是「advance 还没跑」入口。
        """
        try:
            ms_ops = runtime.session.orchestrator._mts_ops
            continued = ms_ops.advance_after_bg_completion(
                runtime.router,
                bg_guest_name=guest_name,
                mts_manager_at_spawn=run.mts_manager_at_spawn,
                mts_session_id_at_spawn=run.mts_session_id_at_spawn,
            )
            if not continued:
                return
            self.server._dispatch_mts_drain_or_defer(
                runtime, run, run_id=run_id, guest_name=guest_name,
            )
        except Exception:
            _log.warning(
                "bg run %s: MTS 续命失败 — ending MTS to avoid hang", run_id,
                exc_info=True,
            )
            # F6 兜底：advance 已成功但 drain 起不来 → end MTS 防悬挂。
            try:
                runtime.session.orchestrator._mts_ops.end_managed_session(
                    runtime.router, reason=MANAGED_SESSION_REASON_USER_CANCEL,
                )
            except Exception:
                _log.warning(
                    "bg run %s: MTS recovery end_managed_session failed",
                    run_id, exc_info=True,
                )

    def dispatch_mts_drain_or_defer(
        self,
        runtime: RoomRuntime,
        run: AgentRun,
        *,
        run_id: str,
        guest_name: str,
    ) -> None:
        """advance 已跑完，按 inflight 状态决定起 drain 还是再 defer。

        **Codex round 7 P2**：与 ``run_mts_continuation_advance`` 拆开 —— 后者负责
        advance 一次，本方法只做调度。re-defer 时 callback 调本方法（**不**回到
        advance），保证 advance 全 lifecycle 跑一次。

        - MTS 已 None（advance 后 TOCTOU） → log + return（无可继续）。
        - 无 inflight → 起 drain。
        - inflight 仍活（race：新 user turn 抢占）→ 再次 defer 到新 inflight done，
          counter+=1 守住 MTS；新 inflight 跑完后 callback 再走 dispatch（递归终止
          条件：新 inflight 必有限完成）。
        """
        # F8：assert → if/return 显式判断（python -O 安全 + TOCTOU 兜底）。
        ms = runtime.session.orchestrator.managed_session
        if ms is None:
            _log.warning(
                "bg run %s: advance_after_bg_completion returned True but "
                "MTS now None (TOCTOU); skipping drain start", run_id,
            )
            return
        if runtime.inflight_alive():
            # Codex round 6 P2: race — 新 user turn 抢占 inflight，再 defer。
            self.server._defer_mts_dispatch_to_inflight(
                runtime, run, run_id=run_id, guest_name=guest_name,
            )
            return
        self.server._start_mts_continuation_drain(runtime, ms.task_id, run_id)

    def defer_mts_dispatch_to_inflight(
        self,
        runtime: RoomRuntime,
        run: AgentRun,
        *,
        run_id: str,
        guest_name: str,
    ) -> None:
        """advance 已跑完，inflight 被 race 抢占 → defer dispatch（不再 advance）。

        与 :meth:`defer_mts_continuation_to_inflight` 区别：前者 callback 走完整
        advance + dispatch 链（首次 defer，advance 未跑），本方法 callback 只走
        ``dispatch_mts_drain_or_defer``（advance 已跑、只剩调度）。
        """
        inflight_task = runtime.inflight_task
        if inflight_task is None or inflight_task.done():
            # 极端：调用前 inflight 又清了 → 直接 dispatch（不 advance）。
            self.server._dispatch_mts_drain_or_defer(
                runtime, run, run_id=run_id, guest_name=guest_name,
            )
            return
        runtime.pending_mts_continuations += 1
        runtime_ref = runtime
        run_ref = run
        run_id_snap = run_id
        guest_name_snap = guest_name

        def _on_inflight_done(_done_task: object) -> None:
            try:
                self.server._dispatch_mts_drain_or_defer(
                    runtime_ref, run_ref,
                    run_id=run_id_snap, guest_name=guest_name_snap,
                )
            except Exception:
                _log.warning(
                    "bg run %s: re-deferred MTS dispatch failed",
                    run_id_snap, exc_info=True,
                )
            finally:
                runtime_ref.pending_mts_continuations -= 1
                try:
                    self.server._maybe_self_destruct_background_runtime(runtime_ref)
                except Exception:
                    _log.warning(
                        "bg run %s: re-deferred self_destruct check failed",
                        run_id_snap, exc_info=True,
                    )

        inflight_task.add_done_callback(_on_inflight_done)

    def start_mts_continuation_drain(
        self, runtime: RoomRuntime, task_id: str, run_id: str,
    ) -> None:
        """启动新的 ``_run_handoff_turn`` 消费 MTS 续命刚 enqueue 的复查项。

        分离方法是为「无 inflight」与「user-turn done_callback」两条路径共用，且让
        ``asyncio.create_task`` 失败时的 F6 兜底（end_managed_session）落到单点。
        ``create_task`` 在事件循环 closing / loop 异常时可能抛 ``RuntimeError``。
        """
        try:
            bg_drain = asyncio.create_task(
                self.server._run_handoff_turn(runtime, task_id=task_id),
            )
            runtime.set_inflight(bg_drain, INFLIGHT_KIND_HANDOFF)
        except Exception:
            _log.warning(
                "bg run %s: create MTS continuation drain failed", run_id,
                exc_info=True,
            )
            raise  # 上抛让外层 continue_mts_after_bg 走 F6 兜底

    def emit_agent_run_terminal(
        self,
        runtime: RoomRuntime,
        run: AgentRun,
        terminal_type: ChahuaEventType,
        msg,  # Optional[Message]
    ) -> None:
        """构造并下发 AGENT_RUN_FINISHED / _CANCELLED / _ERROR envelope。

        data 字段与 :func:`chahua.server_room_snapshot._project_agent_runs` 投影
        同构，让前端 envelope 与 ``room_info.background_runs`` 单点渲染逻辑共用。
        """
        data: dict = {
            "run_id": run.run_id,
            "guest_name": run.guest_name,
            "issued_by": run.issued_by,
        }
        if run.task_id is not None:
            data["task_id"] = run.task_id
        if run.source_guest is not None:
            data["source_guest"] = run.source_guest
        # ERROR 路径补 reason：speak() 返 None 表示「被吞的异常」—— 没有具体错文，
        # 用占位 "speak_failed" 让前端能稳定渲一句话；详细异常已 _log.exception。
        if terminal_type is ChahuaEventType.AGENT_RUN_ERROR:
            data["error"] = "speak_failed"
        # 与 ROOM_BACKGROUND_FINISHED 同口径：envelope.room_id 用 room.name
        # （前端按 room.name 路由前台/后台房间里程碑）。
        runtime.router(
            ChahuaEnvelope(
                room_id=runtime.session.room.name,
                turn_id=None,
                guest_name=run.guest_name,
                message_id=msg.message_id if msg is not None else None,
                type=terminal_type,
                data=data,
            )
        )
