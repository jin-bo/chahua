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
from .session import ensure_room_share_dir
from .task import MARKED_BY_USER
from .tasks_store import (
    ArtifactSourceMissingError,
    TaskExistsError,
    TaskNotFoundError,
)

_log = logging.getLogger(__name__)


# 任务 inbound type 常量。集中放在 handler 顶让 "type 字面值" 也跟随实现走。``server.py``
# 的 ``_INBOUND_ROUTES`` 表 import 这几个常量做 wire 字符串映射。
INBOUND_OPEN_TASK = "open_task"
INBOUND_UPDATE_TASK = "update_task"
INBOUND_ATTACH_ARTIFACT = "attach_artifact"
INBOUND_ADD_DECISION = "add_decision"


# 任务 inbound payload 白名单。加字段就动这里 —— 任何不在集合里的顶层键 → NOTICE error。
# ``type`` 字段单独豁免（dispatcher 已消费）。``update_task`` 的 patch 字段另设一组
# （嵌套不在顶层白名单里）。
_OPEN_TASK_ALLOWED: frozenset[str] = frozenset({"title", "goal", "owner"})
_UPDATE_TASK_ALLOWED: frozenset[str] = frozenset({"task_id", "patch"})
_UPDATE_TASK_PATCH_ALLOWED: frozenset[str] = frozenset({"title", "goal"})
_ATTACH_ARTIFACT_ALLOWED: frozenset[str] = frozenset({"task_id", "share_rel"})
_ADD_DECISION_ALLOWED: frozenset[str] = frozenset(
    {"task_id", "summary", "supporting_message_ids"}
)


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
        ``{tasks, active_task_id}``。task entry 字段沿 :meth:`Task.to_jsonl_dict`，外加
        ``artifacts`` / ``decisions`` 子列表，让前端一次拿全。空房间也下发（空 tasks），
        前端用这帧确认任务协议生效。
        """
        store = self.server._session.tasks_store
        tasks_payload = [
            {
                **t.to_jsonl_dict(),
                "artifacts": store.list_artifacts(t.id),
                "decisions": [d.to_jsonl_dict() for d in store.list_decisions(t.id)],
            }
            for t in store.list_tasks()
        ]
        self._emit_task_envelope(
            sink,
            type=ChahuaEventType.TASK_INFO,
            data={
                "tasks": tasks_payload,
                "active_task_id": store.active_task_id,
            },
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
        except TaskExistsError as e:
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text=f"开任务失败：{e}",
            )
            # 重发让前端按 tasks.length 把 "+新任务" 按钮 disable（防前端误判）。
            self._emit_task_info(sink)
            return
        except OSError as e:
            self.server._notice_persist_failure(sink, INBOUND_OPEN_TASK, e)
            return
        _log.info("open_task: %r (id=%s)", title, task.id)
        self._emit_task_envelope(
            sink, type=ChahuaEventType.TASK_OPEN, data=task.to_jsonl_dict(),
        )
        self._emit_task_info(sink)

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
                    "P5.1 只支持 title / goal"
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
        try:
            task = self.server._session.tasks_store.update_task(
                task_id, title=title, goal=goal,
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
        _log.info("update_task: id=%s title=%r goal=%r", task.id, title, goal)
        applied_patch: dict = {}
        if title is not None:
            applied_patch["title"] = title
        if goal is not None:
            applied_patch["goal"] = goal
        self._emit_task_envelope(
            sink,
            type=ChahuaEventType.TASK_UPDATE,
            data={"task_id": task.id, "patch": applied_patch},
        )
        self._emit_task_info(sink)

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

    def _snapshot_active_task_id(self) -> Optional[str]:
        """接帧同步快照当前 active task —— 不能延到 _run_turn 里再读：从这次
        ``create_task`` 到 turn 被调度之间，inbound 队列里排在后面的 ``open_task`` 会改
        active，回追后已经在 transcript 里的用户消息会被错挂到新任务上。
        """
        return self.server._session.orchestrator.snapshot_active_task_id()
