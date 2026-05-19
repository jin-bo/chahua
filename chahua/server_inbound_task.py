"""任务房间 inbound + emit_task_* handlers（P5.2 重构，docs/P5-任务房间.md §7.2）。

P5.1 期间 ``server.py`` 单 ``ChahuaServer`` 类承担 2100+ 行 / 30+ ``_inbound_*``。P5.2
按 inbound feature 切到 4 个 handler 类 —— 本文件持任务房间四个 inbound（open_task /
update_task / attach_artifact / add_decision）+ 两个 task envelope 助手。

:class:`chahua.server.ChahuaServer` 在 ``__init__`` 里实例化为 ``self.task = TaskHandlers(self)``；
本类持 ``self.server`` 反向引用，跨模块协作（``_emit_notice`` / ``_reject_unknown_keys`` 等）
走 ``self.server.xxx`` 显式 hop。
"""

from __future__ import annotations

import logging
from typing import Optional

from ._server_helpers import require_str
from .events import (
    ChahuaEnvelope,
    ChahuaEventType,
    EnvelopeSink,
    NOTICE_LEVEL_ERROR,
)
from .session import ensure_room_share_dir, relink_task_dirs
from .task import MARKED_BY_USER
from .task_event_text import (
    add_decision_text,
    attach_artifact_text,
    clear_artifacts_text,
    close_task_text,
    open_task_text,
)
from .tasks_store import (
    ArtifactSourceMissingError,
    CLOSED_STATUSES,
    NON_TERMINAL_STATUSES,
    TaskAlreadyClosedError,
    TaskNotFoundError,
    build_task_info_payload,
)

_log = logging.getLogger(__name__)


# 任务 inbound type 常量。集中放在 handler 顶让 "type 字面值" 也跟随实现走。``server.py``
# 的 ``_INBOUND_ROUTES`` 表 import 这几个常量做 wire 字符串映射。
INBOUND_OPEN_TASK = "open_task"
INBOUND_UPDATE_TASK = "update_task"
INBOUND_ATTACH_ARTIFACT = "attach_artifact"
INBOUND_ADD_DECISION = "add_decision"
# P5.2.5：多任务管理新增两个 inbound。
INBOUND_SET_ACTIVE_TASK = "set_active_task"
INBOUND_CLOSE_TASK = "close_task"
# 2026-05-19：清空任务产物（/clear task 走的入口）。
INBOUND_CLEAR_TASK_ARTIFACTS = "clear_task_artifacts"


# 任务 inbound payload 白名单。加字段就动这里 —— 任何不在集合里的顶层键 → NOTICE error。
# ``type`` 字段单独豁免（dispatcher 已消费）。``update_task`` 的 patch 字段另设一组
# （嵌套不在顶层白名单里）。
_OPEN_TASK_ALLOWED: frozenset[str] = frozenset({"title", "goal", "owner"})
_UPDATE_TASK_ALLOWED: frozenset[str] = frozenset({"task_id", "patch"})
# P5.2.5：patch 扩 owner / status；与 tasks_store.update_task 形式参数一一对应。
_UPDATE_TASK_PATCH_ALLOWED: frozenset[str] = frozenset(
    {"title", "goal", "owner", "status"}
)
# inbound status 白名单走 :data:`tasks_store.NON_TERMINAL_STATUSES` /
# :data:`tasks_store.CLOSED_STATUSES` 单一真理源 —— 两层 (inbound 校验 + store 业务校验)
# 共享同一集合，避免上下层"忘改一边"。
_ATTACH_ARTIFACT_ALLOWED: frozenset[str] = frozenset({"task_id", "share_rel"})
_ADD_DECISION_ALLOWED: frozenset[str] = frozenset(
    {"task_id", "summary", "supporting_message_ids"}
)
_SET_ACTIVE_TASK_ALLOWED: frozenset[str] = frozenset({"task_id"})
_CLOSE_TASK_ALLOWED: frozenset[str] = frozenset({"task_id", "status"})
_CLEAR_TASK_ARTIFACTS_ALLOWED: frozenset[str] = frozenset({"task_id"})


class TaskHandlers:
    """任务房间四个 inbound + task envelope 助手。

    持 :class:`chahua.server.ChahuaServer` 反向引用做依赖注入 —— 所有跨 server 状态
    （``_session`` / ``_emit_notice`` / ``_reject_unknown_keys`` / ``_notice_persist_failure``）
    走 ``self.server.xxx``，比 mixin MRO 解析更显式。
    """

    def __init__(self, server: "ChahuaServer") -> None:  # type: ignore[name-defined]
        self.server = server

    def _emit_task_envelope(
        self,
        sink: EnvelopeSink,
        *,
        type: ChahuaEventType,
        data: dict,
    ) -> None:
        """连接级任务 envelope（``turn_id`` / ``message_id`` / ``guest_name`` 全 None）。
        P5.1.7 四个 hint 事件与 ``task_info`` 共用 —— 避免每个 callsite 写一遍三个 None。
        """
        sink(
            ChahuaEnvelope(
                room_id=self.server._session.room.name,
                turn_id=None,
                guest_name=None,
                message_id=None,
                type=type,
                data=data,
            )
        )

    def _emit_task_info(self, sink: EnvelopeSink) -> None:
        """下发 ``task_info`` envelope —— 权威快照，前端任务状态以此为准（docs §4.2）。

        每次任务状态变更（open / update / decision / artifact）后重发整份
        ``{tasks, active_task_id}``。空房间也下发（空 tasks），前端用这帧确认任务协议生效。
        Payload shape 集中在 :func:`tasks_store.build_task_info_payload`，与
        :meth:`Orchestrator._kick_detect_new_artifacts` 共享。
        """
        self._emit_task_envelope(
            sink,
            type=ChahuaEventType.TASK_INFO,
            data=build_task_info_payload(self.server._session.tasks_store),
        )

    async def _inbound_open_task(self, data: dict, sink: EnvelopeSink) -> None:
        if not self.server._reject_unknown_keys(
            data, _OPEN_TASK_ALLOWED, where=INBOUND_OPEN_TASK, sink=sink,
        ):
            return
        title = require_str(data, "title", where=INBOUND_OPEN_TASK)
        if title is None:
            return
        goal = require_str(
            data, "goal", where=INBOUND_OPEN_TASK, allow_empty=True,
        )
        if goal is None:
            return
        owner_raw = data.get("owner")
        if owner_raw is not None and not isinstance(owner_raw, str):
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_OPEN_TASK}: owner 必须是 str / null",
            )
            return
        try:
            task = self.server._session.tasks_store.open_task(
                title=title, goal=goal, owner=owner_raw,
            )
        except OSError as e:
            self.server._notice_persist_failure(sink, INBOUND_OPEN_TASK, e)
            return
        relink_task_dirs(self.server._session)
        _log.info("open_task: %r (id=%s)", title, task.id)
        self._emit_task_envelope(
            sink, type=ChahuaEventType.TASK_OPEN, data=task.to_jsonl_dict(),
        )
        self._emit_task_info(sink)
        await self.server._kick_synthesized_user_message(
            open_task_text(task), sink, task_id=task.id,
        )

    async def _inbound_update_task(self, data: dict, sink: EnvelopeSink) -> None:
        if not self.server._reject_unknown_keys(
            data, _UPDATE_TASK_ALLOWED, where=INBOUND_UPDATE_TASK, sink=sink,
        ):
            return
        task_id = require_str(data, "task_id", where=INBOUND_UPDATE_TASK)
        if task_id is None:
            return
        patch_raw = data.get("patch")
        if not isinstance(patch_raw, dict):
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_UPDATE_TASK}: patch 必须是对象",
            )
            return
        extra = set(patch_raw) - _UPDATE_TASK_PATCH_ALLOWED
        if extra:
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=(
                    f"{INBOUND_UPDATE_TASK}: patch 含未知字段 {sorted(extra)!r}；"
                    f"白名单 {sorted(_UPDATE_TASK_PATCH_ALLOWED)!r}"
                ),
            )
            return
        title = patch_raw.get("title")
        goal = patch_raw.get("goal")
        for key, val in (("title", title), ("goal", goal)):
            if val is not None and not isinstance(val, str):
                self.server._emit_notice(
                    sink, level=NOTICE_LEVEL_ERROR,
                    text=f"{INBOUND_UPDATE_TASK}: patch.{key} 必须是 str",
                )
                return
        # title 与 open_task 同口径不接受空串（避免改成"无标题"任务卡）；goal 允许空。
        if title == "":
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_UPDATE_TASK}: patch.title 不能为空",
            )
            return
        # owner：sentinel 区分"不传"与"传 None"。inbound 传 None 表示清归属；str 表示
        # 设值；任何其它类型 → NOTICE。``"owner" in patch_raw`` 与 ``get`` 不同 —— 前者
        # 能区分缺省与显式 None。
        owner_kw: dict = {}
        if "owner" in patch_raw:
            owner_val = patch_raw["owner"]
            if owner_val is not None and not isinstance(owner_val, str):
                self.server._emit_notice(
                    sink, level=NOTICE_LEVEL_ERROR,
                    text=f"{INBOUND_UPDATE_TASK}: patch.owner 必须是 str / null",
                )
                return
            owner_kw["owner"] = owner_val
        status = patch_raw.get("status")
        if status is not None:
            if not isinstance(status, str):
                self.server._emit_notice(
                    sink, level=NOTICE_LEVEL_ERROR,
                    text=f"{INBOUND_UPDATE_TASK}: patch.status 必须是 str",
                )
                return
            if status in CLOSED_STATUSES:
                self.server._emit_notice(
                    sink, level=NOTICE_LEVEL_ERROR,
                    text=(
                        f"{INBOUND_UPDATE_TASK}: patch.status={status!r} 是终结态，"
                        f"请走 {INBOUND_CLOSE_TASK}"
                    ),
                )
                return
            if status not in NON_TERMINAL_STATUSES:
                self.server._emit_notice(
                    sink, level=NOTICE_LEVEL_ERROR,
                    text=(
                        f"{INBOUND_UPDATE_TASK}: patch.status={status!r} 非法；"
                        f"非终结态白名单 {sorted(NON_TERMINAL_STATUSES)!r}"
                    ),
                )
                return
        try:
            task = self.server._session.tasks_store.update_task(
                task_id, title=title, goal=goal, status=status, **owner_kw,
            )
        except TaskNotFoundError as e:
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text=f"更新任务失败：{e}",
            )
            self._emit_task_info(sink)
            return
        except OSError as e:
            self.server._notice_persist_failure(sink, INBOUND_UPDATE_TASK, e)
            return
        _log.info(
            "update_task: id=%s patch_keys=%r", task.id, sorted(patch_raw.keys()),
        )
        # 实际生效的 patch —— inbound 可能传 title=None（不改），不发空字段污染前端 diff。
        applied_patch: dict = {}
        if title is not None:
            applied_patch["title"] = title
        if goal is not None:
            applied_patch["goal"] = goal
        if "owner" in patch_raw:
            applied_patch["owner"] = patch_raw["owner"]
        if status is not None:
            applied_patch["status"] = status
        self._emit_task_envelope(
            sink,
            type=ChahuaEventType.TASK_UPDATE,
            data={"task_id": task.id, "patch": applied_patch},
        )
        self._emit_task_info(sink)

    async def _inbound_set_active_task(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        """切换 active task。``task_id`` 可以是 str 或 null（清回房间级）。

        走前 ``_cancel_and_drain_inflight`` —— 与 add/remove guest 同口径，避免 inflight
        turn 末尾的 message 被错挂到新 active。成功后**不发**独立 hint event（沿
        §4.2 P5.2 设计：切 active 只重发 ``task_info`` 权威快照）。
        """
        if not self.server._reject_unknown_keys(
            data, _SET_ACTIVE_TASK_ALLOWED, where=INBOUND_SET_ACTIVE_TASK, sink=sink,
        ):
            return
        if "task_id" not in data:
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_SET_ACTIVE_TASK}: task_id 必传（可为 null）",
            )
            return
        task_id_raw = data["task_id"]
        if task_id_raw is not None and not isinstance(task_id_raw, str):
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_SET_ACTIVE_TASK}: task_id 必须是 str / null",
            )
            return
        # 无变化（用户在下拉里点了当前 active / 前端 reconnect 重发）—— 早返避免无谓地
        # cancel inflight turn。tasks_store.set_active 自身也是 no-op，但 cancel 必须在
        # 这里挡住，否则用户那点一下会把正在跑的回答给杀了。
        if self.server._session.tasks_store.active_task_id == task_id_raw:
            return
        await self.server._cancel_and_drain_inflight()
        try:
            self.server._session.tasks_store.set_active(task_id_raw)
        except TaskNotFoundError as e:
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text=f"切换任务失败：{e}",
            )
            self._emit_task_info(sink)
            return
        except OSError as e:
            self.server._notice_persist_failure(sink, INBOUND_SET_ACTIVE_TASK, e)
            return
        relink_task_dirs(self.server._session)
        _log.info("set_active_task: %r", task_id_raw)
        self._emit_task_info(sink)

    async def _inbound_close_task(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        """把任务推到终结态（``done`` / ``abandoned``）。

        走前 ``_cancel_and_drain_inflight`` —— 若被关的就是当前 active，inflight turn
        的余生 message 没必要再挂到这个任务。成功后发 ``task_close`` hint event + 重发
        ``task_info`` 权威快照。
        """
        if not self.server._reject_unknown_keys(
            data, _CLOSE_TASK_ALLOWED, where=INBOUND_CLOSE_TASK, sink=sink,
        ):
            return
        task_id = require_str(data, "task_id", where=INBOUND_CLOSE_TASK)
        if task_id is None:
            return
        status = require_str(data, "status", where=INBOUND_CLOSE_TASK)
        if status is None:
            return
        if status not in CLOSED_STATUSES:
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=(
                    f"{INBOUND_CLOSE_TASK}: status={status!r} 非法，"
                    f"必须是 {sorted(CLOSED_STATUSES)!r} 之一"
                ),
            )
            return
        await self.server._cancel_and_drain_inflight()
        try:
            task = self.server._session.tasks_store.close_task(
                task_id, status=status,  # type: ignore[arg-type]
            )
        except (TaskNotFoundError, TaskAlreadyClosedError) as e:
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text=f"关闭任务失败：{e}",
            )
            self._emit_task_info(sink)
            return
        except OSError as e:
            self.server._notice_persist_failure(sink, INBOUND_CLOSE_TASK, e)
            return
        relink_task_dirs(self.server._session)
        _log.info("close_task: id=%s status=%s", task.id, status)
        self._emit_task_envelope(
            sink,
            type=ChahuaEventType.TASK_CLOSE,
            data={
                "task_id": task.id,
                "status": status,
                "closed_at_ms": task.closed_at_ms,
            },
        )
        self._emit_task_info(sink)
        # task_id=task.id（不读 store.active_task_id）：close_task 后 active 可能已被
        # store 清空，但合成消息按"关闭那个任务"的语义归到被关的 task 上。
        await self.server._kick_synthesized_user_message(
            close_task_text(task, status),  # type: ignore[arg-type]
            sink, task_id=task.id,
        )

    async def _inbound_attach_artifact(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        if not self.server._reject_unknown_keys(
            data, _ATTACH_ARTIFACT_ALLOWED, where=INBOUND_ATTACH_ARTIFACT, sink=sink,
        ):
            return
        task_id = require_str(data, "task_id", where=INBOUND_ATTACH_ARTIFACT)
        if task_id is None:
            return
        share_rel = require_str(
            data, "share_rel", where=INBOUND_ATTACH_ARTIFACT,
        )
        if share_rel is None:
            return
        try:
            # 移入 try：room_dir 只读 / 磁盘满时 mkdir 抛 OSError 也不能逃出 handler。
            share_root = ensure_room_share_dir(self.server._session.room_config.room_dir)
            info = self.server._session.tasks_store.attach_artifact(
                task_id, share_rel=share_rel, share_root=share_root,
            )
        except TaskNotFoundError as e:
            # 任务在 UI render 与 click 之间消失 —— 重发 task_info 让前端任务列表复位。
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text=f"挂产物失败：{e}",
            )
            self._emit_task_info(sink)
            return
        except ArtifactSourceMissingError as e:
            # 用户给了错路径 —— 任务列表本身没变化，仅 NOTICE 提示即可。
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text=f"挂产物失败：{e}",
            )
            return
        except OSError as e:
            self.server._notice_persist_failure(sink, INBOUND_ATTACH_ARTIFACT, e)
            return
        _log.info(
            "attach_artifact: task=%s share_rel=%r → %s",
            task_id, share_rel, info["rel"],
        )
        self._emit_task_envelope(
            sink,
            type=ChahuaEventType.TASK_ARTIFACT_ADDED,
            data={
                "task_id": task_id,
                "name": info["name"],
                "size": info["size"],
                "rel": info["rel"],
                # P5.1 仅 user 一种来源；P5.4 茶客自动归集会出现 "<guest_name>" 值。
                "created_by": MARKED_BY_USER,
            },
        )
        self._emit_task_info(sink)
        # task_id 用 inbound 指定值（可能不是当前 active —— 用户在"老任务"卡片上
        # 挂产物的场景）。
        await self.server._kick_synthesized_user_message(
            attach_artifact_text(info["name"]), sink, task_id=task_id,
        )

    async def _inbound_add_decision(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        if not self.server._reject_unknown_keys(
            data, _ADD_DECISION_ALLOWED, where=INBOUND_ADD_DECISION, sink=sink,
        ):
            return
        task_id = require_str(data, "task_id", where=INBOUND_ADD_DECISION)
        if task_id is None:
            return
        summary = require_str(data, "summary", where=INBOUND_ADD_DECISION)
        if summary is None:
            return
        sup_raw = data.get("supporting_message_ids", [])
        if not isinstance(sup_raw, list):
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_ADD_DECISION}: supporting_message_ids 必须是 list",
            )
            return
        supporting = [x for x in sup_raw if isinstance(x, str)]
        # 防 wscat 绕过前端 maxlength 灌长文。
        summary = summary[:200]
        try:
            decision = self.server._session.tasks_store.add_decision(
                task_id, supporting_message_ids=supporting, summary=summary,
            )
        except TaskNotFoundError as e:
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text=f"记决策失败：{e}",
            )
            self._emit_task_info(sink)
            return
        except OSError as e:
            self.server._notice_persist_failure(sink, INBOUND_ADD_DECISION, e)
            return
        _log.info(
            "add_decision: task=%s decision=%s sup=%d",
            task_id, decision.decision_id, len(supporting),
        )
        self._emit_task_envelope(
            sink,
            type=ChahuaEventType.TASK_DECISION_ADDED,
            data=decision.to_jsonl_dict(),
        )
        self._emit_task_info(sink)
        await self.server._kick_synthesized_user_message(
            add_decision_text(decision), sink, task_id=task_id,
        )

    async def _inbound_clear_task_artifacts(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        """删空 ``tasks/<task_id>/artifacts/`` 下所有产物文件（``/clear task`` 入口）。

        范围严格 —— **只删 ``artifacts/`` 一层下的可见文件**。``task.json`` /
        ``decisions.jsonl`` / ``summary.jsonl`` / ``events.jsonl`` / 摘要游标都不动；
        任务本身（含 status / owner / 决策列表）原样保留。**写权限只在用户**口径
        延续——茶客不能走这条 inbound，只能 propose（暂不支持 propose 清产物，与
        propose 写产物不对称是因为"删"动作风险更高，必须用户主动）。

        副作用顺序：
        1. ``tasks_store.clear_artifacts`` 删盘 + 落 ``events.jsonl`` audit。
        2. **重置 :class:`ArtifactDetector` 的 seen 缓存**——否则下一轮 detect 扫到
           ``current_names = {}``、``prev = 老集合`` 走 ``removed_names`` 分支会与
           本帧 ``task_info`` 形成两次广播（无害但冗余）。
        3. emit ``task_info`` 权威快照（artifacts: []）。
        4. ``_kick_synthesized_user_message`` 让茶客看见"用户清空了产物"。

        与 ``clear_room`` 的区别：``clear_room`` 重置整间公共状态 + agent 会话窗口；
        本 inbound 只清单任务产物，transcript / agent.messages 都不动。
        """
        if not self.server._reject_unknown_keys(
            data, _CLEAR_TASK_ARTIFACTS_ALLOWED,
            where=INBOUND_CLEAR_TASK_ARTIFACTS, sink=sink,
        ):
            return
        task_id = require_str(data, "task_id", where=INBOUND_CLEAR_TASK_ARTIFACTS)
        if task_id is None:
            return
        store = self.server._session.tasks_store
        task = store.get_task(task_id)
        if task is None:
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"清空产物失败：task_id={task_id!r} 不存在",
            )
            # 任务在 UI render 与 click 之间消失 —— 重发 task_info 让前端列表复位。
            self._emit_task_info(sink)
            return
        try:
            deleted = store.clear_artifacts(task_id)
        except OSError as e:
            self.server._notice_persist_failure(
                sink, INBOUND_CLEAR_TASK_ARTIFACTS, e,
            )
            return
        # 重置 detector 缓存 —— 防止下一轮 _kick_detect_new_artifacts 把"删走的旧名"
        # 当 removed_names 触发多余广播；同时同名重建时仍能正常 emit。
        self.server._session.orchestrator._artifact_detector.seen[task_id] = set()
        _log.info("clear_task_artifacts: task=%s deleted=%d", task_id, len(deleted))
        self._emit_task_info(sink)
        await self.server._kick_synthesized_user_message(
            clear_artifacts_text(task, len(deleted)), sink, task_id=task_id,
        )

    def _snapshot_active_task_id(self) -> Optional[str]:
        """接帧同步快照当前 active task —— 不能延到 _run_turn 里再读：从这次
        ``create_task`` 到 turn 被调度之间，inbound 队列里排在后面的 ``open_task`` 会改
        active，回追后已经在 transcript 里的用户消息会被错挂到新任务上。
        """
        return self.server._session.orchestrator.snapshot_active_task_id()
