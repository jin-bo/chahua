"""茶客自动归集（P5.4）：每个 pick 周期末尾扫 active task 的 ``artifacts/``，
diff 上次扫到的文件名集合，emit ``task_artifact_added`` hint + 一帧 ``task_info``。

从 :mod:`chahua.orchestrator` 抽出。Orchestrator 持一个
:class:`ArtifactDetector` 实例，``_run_ai_chain`` 末尾调 :meth:`detect`；
保留 ``_seen_artifacts`` / ``_kick_detect_new_artifacts`` 转发属性维持测试入口稳定。

设计要点：
- 初始化只 seed open / in_progress / blocked 任务的 artifacts —— closed task 永远不会
  被 :meth:`detect` 读到（前置过滤），seed 进来纯浪费 readdir 还堆 dict。
- 用户走 UI ``attach_artifact`` 上传时 seen 缓存不同步更新 —— 下次 :meth:`detect` 扫到那些
  文件会重复 emit hint；接受（前端以 ``task_info`` 为权威，hint 仅可选 toast，重复无感），
  不在两个组件间加 sync 通道避免耦合。
"""

from __future__ import annotations

from typing import Optional

from .events import ChahuaEnvelope, ChahuaEventType, EnvelopeSink, emit_to_sink
from .task import ARTIFACT_CREATED_BY_GUEST
from .tasks_store import CLOSED_STATUSES, TasksStore, build_task_info_payload


class ArtifactDetector:
    """track 每 task 上次扫到的 artifact 名集合，并 emit 新增 hint。"""

    def __init__(self, *, room_id: str, tasks_store: Optional[TasksStore]) -> None:
        self.room_id = room_id
        self.tasks_store = tasks_store
        # task_id → 上次扫到的文件名 set。boot 时按非 closed 任务的现存 artifact seed。
        self.seen: dict[str, set[str]] = {}
        if tasks_store is not None:
            for t in tasks_store.list_tasks():
                if t.status in CLOSED_STATUSES:
                    continue
                self.seen[t.id] = {a["name"] for a in tasks_store.list_artifacts(t.id)}

    def detect(self, sink: EnvelopeSink, active_task_id: Optional[str]) -> None:
        """扫 active task 的 ``artifacts/``，emit 茶客新写入的产物（P5.4）。

        Emit 顺序：N 条 ``task_artifact_added`` hint（per file）+ 一帧 ``task_info``
        权威快照（payload 走 :func:`tasks_store.build_task_info_payload`，与
        ``server_inbound_task.TaskHandlers._emit_task_info`` 共享）。
        """
        if active_task_id is None or self.tasks_store is None:
            return
        task = self.tasks_store.get_task(active_task_id)
        if task is None or task.status in CLOSED_STATUSES:
            return
        artifacts = self.tasks_store.list_artifacts(active_task_id)
        current_names = {a["name"] for a in artifacts}
        prev = self.seen.get(active_task_id, frozenset())
        new_names = current_names - prev
        removed_names = prev - current_names
        if not new_names and not removed_names:
            return
        # 同步缓存到当前盘上状态：既要记入新增，也要去除已被 GC 的旧名（不去除会让
        # 同名重建时不 emit）。
        self.seen[active_task_id] = current_names

        def emit(event_type: ChahuaEventType, data: dict) -> None:
            emit_to_sink(sink, ChahuaEnvelope(
                room_id=self.room_id,
                turn_id=None, guest_name=None, message_id=None,
                type=event_type, data=data,
            ))

        for artifact in (a for a in artifacts if a["name"] in new_names):
            emit(ChahuaEventType.TASK_ARTIFACT_ADDED, {
                "task_id": active_task_id,
                "name": artifact["name"],
                "size": artifact["size"],
                "rel": artifact["rel"],
                "created_by": ARTIFACT_CREATED_BY_GUEST,
            })
        if new_names:
            emit(
                ChahuaEventType.TASK_INFO,
                build_task_info_payload(self.tasks_store),
            )
