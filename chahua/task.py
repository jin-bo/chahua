"""Task 数据模型（P5.1.1，设计文档 docs/P5-任务房间.md §2.1 / §8.1）。

P5.1 把房间从"纯聊天"升级为"带任务的工作容器"的最小数据契约。本模块只定义
:class:`Task` / :class:`Decision` 两个值对象 + 落盘 / 加载 helpers，**不接 wiring**
（持久化目录扫描、active 指针、open 拒绝等放 :mod:`chahua.tasks_store`）。

设计约束（docs/P5-任务房间.md §8.1，"入站严格 / 落盘宽容"）：

- 落盘加载：**未知字段 warn 后忽略**，必需字段缺失才返 ``None`` 跳过该条 —— 同
  :mod:`chahua.room` 跳坏行口径，保护跨版本前向兼容（新版加字段不让老版启动炸）。
- 入站校验：**白名单严格**，未知键直接 raise / NOTICE error —— 在 :mod:`chahua.server`
  那一层（P5.1.7）负责，不在本模块。

P5.1 仅允许 ``status="open"`` —— ``in_progress`` / ``blocked`` / ``done`` / ``abandoned``
保留状态字面值但不在 MVP 通过 ``update_task`` 改（见 §4.3 / §7.1）。这里**允许**所有
五种状态字面被加载（旧 task.json 用过 / P5.2 起会用），但 P5.1 写盘只产 ``"open"``。
"""

from __future__ import annotations

import logging
import typing
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Literal, Optional

from .events import new_id, now_ms

_log = logging.getLogger(__name__)


TaskStatus = Literal["open", "in_progress", "blocked", "done", "abandoned"]
_VALID_STATUSES: frozenset[str] = frozenset(typing.get_args(TaskStatus))

# 单点常量 —— 避免 "open" / "user" 字面值散落多处。
TASK_STATUS_OPEN: TaskStatus = "open"
MARKED_BY_USER = "user"
"""decisions.marked_by 的唯一合法值（P5.1）；P5.3 起茶客的 propose 也仍通过 UI"采纳"
转译成 user 入库（设计文档 §6.3 / §8 不变量 #3）。"""

TASK_UNTITLED = "(无标题)"
"""title 为空时的展示 fallback。与 ``app/renderer/events.js::TASK_UNTITLED`` 同源
（JS 那份镜像 Python；新增标题策略两边同步）。"""

TASK_STATUS_DISPLAY: dict[TaskStatus, str] = {
    "open": "未开始",
    "in_progress": "进行中",
    "blocked": "被阻塞",
    "done": "已完成",
    "abandoned": "已放弃",
}
"""``TaskStatus`` → 中文 label。与 ``app/renderer/events.js::TASK_STATUS_OPTIONS`` 同源
（JS 那份镜像 Python；本模块 Literal 顺序与 JS options 顺序一致）。

新增 status 时：① 改本 dict；② 改 ``TaskStatus`` Literal；③ 改 JS 的 ``TASK_STATUS_OPTIONS``。
缺哪一项不会启动失败，但 LLM prompt / UI 任一面会暴露原始 enum 字面值。"""


# ── artifact 字段格式化 ──────────────────────────────────────────────────────
#
# 与 ``app/renderer/task_panel.js`` 的 ``formatSize`` / ``formatTs`` 同口径 —— 改一处
# 记得改两处（JS 那份在另一进程，Python 这两份是单源）。
# orchestrator 的 task block 与 task_tools 的 task_list_artifacts 工具都走这俩。


def format_artifact_size(size: int) -> str:
    """字节数 → 人眼可读（B / KB / MB）。"""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def format_artifact_mtime(mtime_ms: int) -> str:
    """``YYYY-MM-DD HH:MM`` 本地时区。"""
    return datetime.fromtimestamp(mtime_ms / 1000).strftime("%Y-%m-%d %H:%M")


def new_task_id() -> str:
    """``task_<10字节 hex>`` —— 与 :func:`chahua.events.new_message_id` 同长度，扫读易认。"""
    return new_id("task")


def new_decision_id() -> str:
    """``dec_<10字节 hex>``。decisions.jsonl 一行一个，前端 envelope 与持久化 record 共用。"""
    return new_id("dec")


@dataclass(frozen=True, slots=True)
class Task:
    """房间任务的元数据。

    落盘：`rooms/<room>/tasks/<id>/task.json`，覆写式（tmp+rename）。状态历史在 P5.2
    起由 `events.jsonl` 记录；P5.1 不写。
    """

    id: str
    title: str
    goal: str
    status: TaskStatus
    owner: Optional[str]
    """speaker_id（``"user"`` 或茶客 name）；``None`` = 全员负责 / 无指定。
    P5.1 锁定为创建时值，不通过 ``update_task`` 改（见 §5.1）。"""

    created_at_ms: int
    updated_at_ms: int
    closed_at_ms: Optional[int] = None
    """``status in ("done", "abandoned")`` 才有值；P5.1 不会到这步。"""

    def to_jsonl_dict(self) -> dict:
        """落盘形态。字段顺序固定方便人眼扫；``closed_at_ms`` 为 ``None`` 时仍写 ``null``。"""
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "status": self.status,
            "owner": self.owner,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "closed_at_ms": self.closed_at_ms,
        }

    @classmethod
    def from_jsonl(cls, obj: dict) -> Optional["Task"]:
        """从 task.json 重建。未知字段 warn 后忽略；必需字段缺 / 类型错 → ``None`` + WARN（该条跳过）。

        ``status`` 不在 :data:`_VALID_STATUSES` 时降级到 ``"open"`` + WARN —— 同样的
        "宽容加载"口径，不为单字段不合法让整个任务消失。
        """
        try:
            tid = str(obj["id"])
            title = str(obj["title"])
            goal = str(obj["goal"])
            status_raw = str(obj["status"])
            created_at_ms = int(obj["created_at_ms"])
            updated_at_ms = int(obj["updated_at_ms"])
        except (KeyError, TypeError, ValueError):
            _log.warning("skip malformed task.json record: %r", obj)
            return None
        if status_raw not in _VALID_STATUSES:
            _log.warning(
                "task %s: status=%r 不在合法集，降级为 'open'", tid, status_raw
            )
            status: TaskStatus = "open"
        else:
            status = status_raw  # type: ignore[assignment]
        owner_raw = obj.get("owner")
        owner = str(owner_raw) if isinstance(owner_raw, str) else None
        closed_raw = obj.get("closed_at_ms")
        closed_at_ms: Optional[int] = None
        if isinstance(closed_raw, int):
            closed_at_ms = closed_raw
        elif closed_raw is not None:
            _log.warning(
                "task %s: closed_at_ms=%r 非 int / null，忽略", tid, closed_raw
            )
        # 其它键（未知字段）按 §8.1 落盘宽容，不报告 —— warn 每次启动都刷会很吵。
        return cls(
            id=tid,
            title=title,
            goal=goal,
            status=status,
            owner=owner,
            created_at_ms=created_at_ms,
            updated_at_ms=updated_at_ms,
            closed_at_ms=closed_at_ms,
        )

    @classmethod
    def new(
        cls,
        *,
        title: str,
        goal: str,
        owner: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> "Task":
        """构造一个新任务，``status="open"`` + 时间戳自动填。

        ``task_id`` 通常缺省让本函数 mint；测试 / 重建场景可显式传。

        **不能并入 __init__ 默认值** —— ``from_jsonl`` 重建时不该 mint 新 ID / 戳新时间。
        """
        now = now_ms()
        return cls(
            id=task_id or new_task_id(),
            title=title,
            goal=goal,
            status=TASK_STATUS_OPEN,
            owner=owner,
            created_at_ms=now,
            updated_at_ms=now,
            closed_at_ms=None,
        )


@dataclass(frozen=True, slots=True)
class Decision:
    """一条任务决策记录。append-only 落盘在 `tasks/<id>/decisions.jsonl`。"""

    decision_id: str
    task_id: str
    supporting_message_ids: tuple[str, ...]
    """指向房间 transcript 的 message_id 列表。P5.1 由用户在 UI "标为决策" 时勾选。"""

    summary: str
    """≤ 200 字一句话（前端 textarea + maxlength 控制；server 端再次截断以防 wscat 绕过）。"""

    marked_by: str
    """``"user"`` —— P5.1 仅用户能采纳决策。P5.3 起茶客可 ``task_propose_decision``，
    UI "采纳" 转译回 ``add_decision``，``marked_by`` 仍写 ``"user"``。"""

    ts_ms: int

    def to_jsonl_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "task_id": self.task_id,
            "supporting_message_ids": list(self.supporting_message_ids),
            "summary": self.summary,
            "marked_by": self.marked_by,
            "ts_ms": self.ts_ms,
        }

    @classmethod
    def from_jsonl(cls, obj: dict) -> Optional["Decision"]:
        try:
            did = str(obj["decision_id"])
            tid = str(obj["task_id"])
            summary = str(obj["summary"])
            marked_by = str(obj["marked_by"])
            ts_ms = int(obj["ts_ms"])
            sup_raw = obj["supporting_message_ids"]
        except (KeyError, TypeError, ValueError):
            _log.warning("skip malformed decisions.jsonl record: %r", obj)
            return None
        if not isinstance(sup_raw, list):
            _log.warning(
                "decision %s: supporting_message_ids 非 list（%r），降级为空",
                did, type(sup_raw),
            )
            sup_raw = []
        # 过滤非 str 元素 + 去重保序
        seen: set[str] = set()
        sup: list[str] = []
        for x in sup_raw:
            if isinstance(x, str) and x not in seen:
                seen.add(x)
                sup.append(x)
        return cls(
            decision_id=did,
            task_id=tid,
            supporting_message_ids=tuple(sup),
            summary=summary,
            marked_by=marked_by,
            ts_ms=ts_ms,
        )

    @classmethod
    def new(
        cls,
        *,
        task_id: str,
        supporting_message_ids: Iterable[str],
        summary: str,
        marked_by: str = MARKED_BY_USER,
        decision_id: Optional[str] = None,
    ) -> "Decision":
        return cls(
            decision_id=decision_id or new_decision_id(),
            task_id=task_id,
            supporting_message_ids=tuple(supporting_message_ids),
            summary=summary,
            marked_by=marked_by,
            ts_ms=now_ms(),
        )
