"""TasksStore —— 房间任务的持久化 facade（P5.1.4，设计文档 docs/P5-任务房间.md §3 / §7.1）。

`Room` 不知道任务存在；`Orchestrator` / `server` 通过本模块读 / 写当前任务状态。
单点封装的好处：① 服务端 inbound handler 拿到合法 payload 直接调对应方法即可，
不必各自重复"writes task.json + state.json + emit 事件"；② 加载期"双向修复
state.json↔task.json" 集中实现（§7.1 / §10）。

落盘布局（P5.1，docs/P5-任务房间.md §3）：

    rooms/<id>/tasks/
    ├── state.json                 # {active_task_id?}
    └── <task_id>/
        ├── task.json              # 覆写式 tmp+rename
        ├── decisions.jsonl        # append-only
        └── artifacts/             # 文件

P5.2 起在每个 task 目录下追写 ``events.jsonl``（task 级状态 / 字段变更历史，append-only）；
任务级 ``summary.jsonl`` 仍留待 P5.2.12 接 summarizer 时再写。

事件 schema（events.jsonl 一行一条）：

    {"ts_ms": <int>, "kind": "became_active" | "became_inactive"
                             | "closed"           # P5.2.2，payload: {"status": "done"|"abandoned"}
                             | "field_changed"    # P5.2.3，payload: {"field", "before", "after"}
     ...payload}

落盘宽容（§8.1）：加载 events.jsonl 用 :func:`read_jsonl_skip_bad`，未知 ``kind`` 整行
跳过；本模块当前只**写**不**读**（list_events 给上层 UI / 测试用）。

关键约束（docs §7.1 / §7.2）：

- **同一时刻至多 1 个 active**：``open_task`` 创建后自动设为 active，旧 active 留作
  ``status="open"`` 进历史列表。P5.2 起允许多任务共存；P5.1 的 :class:`TaskExistsError`
  保留为类型给老调用方 ``except`` 用，但 ``open_task`` **永不再抛**。
- **加载时双向修复**：
    ① state.json 指向不存在 task → 清回 None；
    ② state.json 缺 / 为空且**有且仅有一个**有效 task.json → 自动设为 active 并回写
       state.json；
    ③ state.json 缺 + 有效 task.json **多于一个** → 不自动选（保持 None），等 UI 让
       用户指定 active（P5.2.6）。
- **入站严格**：调用方（server）保证 payload 合法白名单；本模块只对"业务约束"做最后一道
  guard（如 ``attach_artifact`` 的"share/ 源文件存在"）。
"""

from __future__ import annotations

import dataclasses
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from ._persist import (
    append_jsonl,
    read_jsonl_skip_bad,
    read_json_or_none,
    write_json_atomic,
)
from .events import new_event_id, now_ms
from .task import Decision, Task, TaskStatus

# close_task 仅接受 CLOSED_STATUSES；update_task 仅接受 NON_TERMINAL_STATUSES。
# 两组都是 public —— :mod:`chahua.server_inbound_task` 直接 import 用做 inbound 白名单，
# 避免字面值在两层重复（在 P5.2 重构里已经踩过一次"忘改一边"的坑）。
CLOSED_STATUSES: frozenset[str] = frozenset({"done", "abandoned"})
NON_TERMINAL_STATUSES: frozenset[str] = frozenset({"open", "in_progress", "blocked"})

# events.jsonl `kind` 字段 —— 模块顶 single source（测试 + _append_event 都 import）。
EVENT_KIND_BECAME_ACTIVE = "became_active"
EVENT_KIND_BECAME_INACTIVE = "became_inactive"
EVENT_KIND_CLOSED = "closed"  # payload: {"status": "done"|"abandoned"}
EVENT_KIND_FIELD_CHANGED = "field_changed"  # payload: {"field", "before", "after"}
EVENT_KIND_ARTIFACTS_CLEARED = "artifacts_cleared"  # payload: {"count", "names": [...]}

# update_task 的 owner 参数 sentinel —— ``None`` 是合法值（清空 owner），不能复用 None
# 表示"不改"。沿 :mod:`chahua.orchestrator` 的 ``_UNSET`` 同款 module-private 对象。
_OWNER_UNSET: object = object()

_log = logging.getLogger(__name__)


# 路径常量。room_dir 之下的子树都是 P5.1 新增；与 share/ / guests/ 同级。
_TASKS_DIRNAME = "tasks"
_STATE_FILENAME = "state.json"
_TASK_FILENAME = "task.json"
_DECISIONS_FILENAME = "decisions.jsonl"
_EVENTS_FILENAME = "events.jsonl"  # P5.2.2 起
_ARTIFACTS_DIRNAME = "artifacts"

# OS 自动生成的元数据文件 —— 不入产物清单（避免 macOS Finder / Windows 资源管理器在
# ``tasks/<id>/artifacts/`` 留下的 ``.DS_Store`` / ``._foo.md`` / ``Thumbs.db`` 等被
# ``_kick_detect_new_artifacts`` 当成茶客新产物 emit。AppleDouble companion 文件用前缀
# ``._`` 匹配（copy 到非 HFS 文件系统时 macOS 自动生成 ``._<原名>``）。
_OS_METADATA_FILENAMES = frozenset({
    ".DS_Store", "Thumbs.db", "desktop.ini", "ehthumbs.db",
})


def _is_os_metadata_file(name: str) -> bool:
    """``True`` = OS 自动生成的元数据文件（macOS / Windows），产物扫描时跳过。"""
    if name in _OS_METADATA_FILENAMES:
        return True
    # AppleDouble companion files：``._<filename>``。注意单字符 ``"."`` 不算 —— 极少见
    # 但若 user 真创建个名为 "." 的文件让它通过也无害（``Path.is_file`` 已挡）。
    if name.startswith("._") and len(name) > 2:
        return True
    return False


_INVALID_ARTIFACT_NAME_CHARS = ("/", "\\", "..")


def _validate_artifact_name(name: str) -> Optional[str]:
    """artifact 文件名校验 —— 返回错误描述或 ``None``（合法）。

    四档拒绝：空名 / 含 ``/`` / 含 ``\\`` / 含 ``..`` / 前缀 ``.``。最后一档把 ``.DS_Store`` /
    ``._foo`` 等元数据文件挡在写入门外 —— 与 :func:`_is_os_metadata_file` 同口径，避免
    "写进 artifacts/ 但 detector 又跳过"的鬼影状态。
    """
    if not name or not name.strip():
        return "name 不能为空。"
    if any(ch in name for ch in _INVALID_ARTIFACT_NAME_CHARS):
        return (
            f"name={name!r} 含路径分隔符或 ..，必须是单一文件名（如 ``评审.md``）。"
        )
    if name.startswith("."):
        return (
            f"name={name!r} 以 . 开头（避免与 .DS_Store / Thumbs.db 等"
            "幽灵 artifact 冲突）。"
        )
    return None


class TasksStoreError(Exception):
    """业务约束违例的基类。各种具体错落到下面的子类。"""


class TaskExistsError(TasksStoreError):
    """**保留类型**给老调用方 ``except`` 用；P5.2 起 ``open_task`` 不再抛此错（多任务
    解禁，docs §7.2）。短期内 :mod:`chahua.server_inbound_task` 仍 ``except`` 这个类型
    作为 dead branch，待 P5.2.13 文档收尾时再清。"""


class TaskNotFoundError(TasksStoreError):
    """``update_task`` / ``add_decision`` / ``attach_artifact`` / ``set_active`` /
    ``close_task`` 传了不存在的 task_id。"""


class TaskAlreadyClosedError(TasksStoreError):
    """``close_task`` 被调用在已经 closed 的任务上（防前端误重复点关闭）。"""


class ArtifactSourceMissingError(TasksStoreError):
    """``attach_artifact`` 时 share/ 里没找到指定文件。"""


@dataclass
class TasksStore:
    """房间级任务存储。一个 :class:`chahua.session.RoomSession` 持一个。

    构造期一次性 ``mkdir tasks/``；加载已有 task.json 做双向修复（§7.1）。

    ``_tasks`` / ``_active_task_id`` 是内存状态镜像，所有 mutator 都先改盘再改内存
    （单线程 ws 串行 inbound，无并发；崩在两者之间下次启动靠双向修复兜底）。
    """

    room_dir: Path
    _tasks: dict[str, Task] = field(default_factory=dict, init=False, repr=False)
    _decisions: dict[str, list[Decision]] = field(
        default_factory=dict, init=False, repr=False,
    )
    """每任务 decisions 的内存镜像。``_load`` 时一次性读入；``add_decision`` 同步 append。
    避免 ``list_decisions`` 在每次 ``_emit_task_info`` 时重读 decisions.jsonl。"""

    _active_task_id: Optional[str] = field(default=None, init=False, repr=False)
    _load_warnings: list[str] = field(default_factory=list, init=False, repr=False)
    """加载期发现但需用户感知的状态（非 fatal）。当前只有 P5.2.6 的"多 task + state.json
    未指 active" 一项。Server 在首次 ``_emit_room_snapshot`` / ``_replace_session`` 后
    通过 :meth:`consume_load_warnings` 取走 + emit NOTICE info；每条只 emit 一次。"""

    def __post_init__(self) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._load()

    # ── 路径 helpers ────────────────────────────────────────────────────

    @property
    def tasks_dir(self) -> Path:
        return self.room_dir / _TASKS_DIRNAME

    @property
    def state_path(self) -> Path:
        return self.tasks_dir / _STATE_FILENAME

    def task_dir(self, task_id: str) -> Path:
        return self.tasks_dir / task_id

    def task_json_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / _TASK_FILENAME

    def decisions_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / _DECISIONS_FILENAME

    def events_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / _EVENTS_FILENAME

    def artifacts_dir(self, task_id: str) -> Path:
        return self.task_dir(task_id) / _ARTIFACTS_DIRNAME

    # ── 加载 + 双向修复 ─────────────────────────────────────────────────

    def _load(self) -> None:
        """扫 tasks/*/task.json + 读 state.json；按 §7.1 双向修复后落定内存状态。

        加载策略沿用"宽容" 口径（§8.1）：
        - 单个 task.json 解析失败 / 必需字段缺失 → WARN + 跳过该任务目录，不让一个坏目录
          让整个房间起不来；
        - state.json 不存在 / 解析失败 → 视为 None；
        - 双向修复：
            ① state.json 指向的 task_id 不在内存中 → 清回 None + 回写 state.json
            ② state.json 缺且**有且仅有一个**有效 task → 自动设为 active 并回写。
              多于一个时 P5.1 不会发生（open 拒绝），保留 ``None`` 让上层 NOTICE。
        """
        self._load_task_dirs()
        for tid in self._tasks:
            self._decisions[tid] = self._read_decisions(tid)
        self._resolve_and_repair_active()

    def _load_task_dirs(self) -> None:
        if not self.tasks_dir.is_dir():
            return
        for entry in sorted(self.tasks_dir.iterdir()):
            if not entry.is_dir():
                continue
            data = read_json_or_none(entry / _TASK_FILENAME)
            if not isinstance(data, dict):
                _log.warning("skip task dir %s: no usable task.json", entry)
                continue
            t = Task.from_jsonl(data)
            if t is None:
                continue
            if t.id != entry.name:
                _log.warning(
                    "task.json id=%r != dir name=%r at %s；保留 dir name 为权威",
                    t.id, entry.name, entry,
                )
                # 用目录名当 id —— 避免 id 漂移让 task_dir 找不到文件。
                t = dataclasses.replace(t, id=entry.name)
            self._tasks[t.id] = t

    def _read_decisions(self, task_id: str) -> list[Decision]:
        out: list[Decision] = []
        for obj in read_jsonl_skip_bad(self.decisions_path(task_id)):
            d = Decision.from_jsonl(obj)
            if d is not None:
                out.append(d)
        return out

    def _resolve_and_repair_active(self) -> None:
        """双向修复 state.json ↔ task.json（§7.1 / §7.2，docs §10）：

        ① state.json 指向不存在 task → 清回 None + 回写（+ 多 task 时 warn 让用户选）
        ② state.json **真缺**（文件不存在 / 非 dict / 缺 key）+ 唯一 task → 自动设
           active + 回写
        ③ state.json **真缺** + 多 task（P5.2 起会发生）→ 保持 None + 回写显式 null +
           记 NOTICE info 让用户在 UI 指定 active；不自动选第一个

        **state.json 显式 ``{"active_task_id": null}`` 是合法稳态**（用户关掉最后一个
        active 后的常见落地状态），不进 auto-recover、不 warn —— 区分"用户已表达
        意图"和"系统状态丢失"。``dict.get("active_task_id")`` 拿到 None 时这两种情况
        长得一样，必须用 ``"active_task_id" in state`` 单独判 key 存在性。
        """
        state = read_json_or_none(self.state_path)
        key_present = isinstance(state, dict) and "active_task_id" in state
        active_raw = state["active_task_id"] if key_present else None

        if isinstance(active_raw, str) and active_raw in self._tasks:
            self._active_task_id = active_raw
            return

        self._active_task_id = None

        if isinstance(active_raw, str):
            # 修复 ①：state.json 写了 ghost id（不在已加载任务中）→ 清回 None + 回写
            _log.warning(
                "state.json.active_task_id=%r 不在已加载任务中，清回 None",
                active_raw,
            )
            self._write_state()
            if len(self._tasks) > 1:
                # ghost + multi-task：用户原本指向某个 active，我们清了 —— 提示重选
                self._warn_multi_task_no_active(
                    notice_lead=f"state.json 中的 active 已失效（清回 None）；现有 {len(self._tasks)} 个任务，",
                    log_reason="ghost active 已清",
                )
            return

        if key_present:
            # state.json 显式 ``active_task_id: null`` —— 用户意图就是"无 active"，
            # 不 auto-recover、不 warn。用户关 active task 后的稳态走这条。
            return

        # state.json 真缺（文件不存在 / 非 dict / 缺 key）→ 走 auto-recover
        if len(self._tasks) == 1:
            # 修复 ②
            (only_id,) = self._tasks.keys()
            self._active_task_id = only_id
            _log.info(
                "tasks/state.json 缺 active 但仅一个 task.json，自动恢复 active=%s",
                only_id,
            )
            self._write_state()
            return
        if len(self._tasks) > 1:
            # 修复 ③：不自动选，让用户在 UI 二次确认。回写显式 null —— 下次启动看到
            # 显式 null 就走"用户已表达意图"分支，不再重复 warn。
            self._write_state()
            self._warn_multi_task_no_active(
                notice_lead=f"加载发现 {len(self._tasks)} 个任务但 state.json 未指定 active；",
                log_reason="tasks/state.json 缺 active",
            )
            return
        # len == 0：state.json 真缺 + 无 task —— 写一份显式 null 让下次启动稳定
        self._write_state()

    def _warn_multi_task_no_active(self, *, notice_lead: str, log_reason: str) -> None:
        """多任务 + 无 active 时的 NOTICE info + WARN 日志（修复 ① / ③ 共用）。

        ``notice_lead`` 是给用户的开场（含任务数），与下面"请在任务面板选一个继续，新
        消息暂归房间级。"统一收尾。``log_reason`` 是日志前缀（``ghost active 已清``
        / ``tasks/state.json 缺 active``），区分两条触发路径。
        """
        self._load_warnings.append(
            f"{notice_lead}请在任务面板选一个继续，新消息暂归房间级。"
        )
        _log.warning(
            "%s + 有 %d 个 task.json，保持 active=None 等用户选",
            log_reason, len(self._tasks),
        )

    def _write_state(self) -> None:
        """落 state.json。``active_task_id == None`` 时写 ``{"active_task_id": null}`` —— 显式
        优于"删文件让加载视为缺失"，前者读端拿到的语义稳定。"""
        write_json_atomic(self.state_path, {"active_task_id": self._active_task_id})

    def _write_task(self, task: Task) -> None:
        path = self.task_json_path(task.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, task.to_jsonl_dict())

    def _append_event(self, task_id: str, kind: str, **payload: object) -> None:
        """task 级 events.jsonl append 一条 ``{event_id, ts_ms, kind, ...payload}``。

        ``event_id`` 是 stable id（``evt_<hex>``），给前端 / 未来 audit tooling 引用具体
        变更用；当前 envelope 不下发，只落盘。``append_jsonl`` 父目录不存在不补建
        —— task 目录在 :meth:`open_task` 已经 ``mkdir(parents=True, exist_ok=True)``。
        """
        record: dict = {
            "event_id": new_event_id(),
            "ts_ms": now_ms(),
            "kind": kind,
        }
        record.update(payload)
        append_jsonl(self.events_path(task_id), record)

    # ── 读 API ──────────────────────────────────────────────────────────

    @property
    def active_task_id(self) -> Optional[str]:
        return self._active_task_id

    def list_tasks(self) -> list[Task]:
        """按 created_at_ms 升序返回所有任务。空房间返回 ``[]``。"""
        return sorted(self._tasks.values(), key=lambda t: t.created_at_ms)

    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def get_active_task(self) -> Optional[Task]:
        if self._active_task_id is None:
            return None
        return self._tasks.get(self._active_task_id)

    def list_decisions(self, task_id: str) -> list[Decision]:
        """返回任务的全部决策（按追加顺序）。从内存镜像取 —— 不再每次重读 decisions.jsonl。"""
        return list(self._decisions.get(task_id, ()))

    def consume_load_warnings(self) -> list[str]:
        """取走加载期累积的 warnings（清空内部缓冲）。每条 warning 只能被取走一次。

        Server 在 ``_emit_room_snapshot`` 末尾调一次，把每条转成 NOTICE info envelope
        emit 给前端。:meth:`_resolve_and_repair_active` 当前唯一来源（P5.2.6 多任务无
        active 的情况）。
        """
        out = self._load_warnings
        self._load_warnings = []
        return out

    def list_events(self, task_id: str) -> list[dict]:
        """返回任务的全部 events.jsonl 记录（按追加顺序）。从盘读 —— 该 API 给 UI / 测试
        round-trip 用，不进 ``task_info`` envelope（前端目前不渲染状态历史）。

        坏行按 ``read_jsonl_skip_bad`` 跳过；不存在 → 空 list。
        """
        return list(read_jsonl_skip_bad(self.events_path(task_id)))

    def list_artifacts(self, task_id: str) -> list[dict]:
        """artifacts/ 目录扫，返回 ``[{name, size, mtime_ms, rel}, ...]``，按名升序。

        ``rel`` 与 :meth:`attach_artifact` 返回值同口径 —— ``task_info`` 是权威快照（docs
        §4.2 事件分工），漏 ``rel`` 会让 reconnect / 错过 hint 后 UI 拿不到产物落盘路径。

        不递归 —— P5.1 不支持子目录；后续如要支持先在 attach_artifact 加 reject 子目录。
        """
        out: list[dict] = []
        adir = self.artifacts_dir(task_id)
        if not adir.is_dir():
            return out
        for p in sorted(adir.iterdir()):
            if not p.is_file():
                continue
            if _is_os_metadata_file(p.name):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({
                "name": p.name,
                "size": st.st_size,
                "mtime_ms": int(st.st_mtime * 1000),
                "rel": f"tasks/{task_id}/{_ARTIFACTS_DIRNAME}/{p.name}",
            })
        return out

    # ── 写 API ──────────────────────────────────────────────────────────

    def open_task(
        self,
        *,
        title: str,
        goal: str,
        owner: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Task:
        """创建新任务并设为 active。

        P5.2 起允许多任务共存：新建任务自动成为 active，旧 active 留作 ``status="open"``
        进历史列表（docs §7.2）。P5.1 的 :class:`TaskExistsError` 不再抛。
        """
        task = Task.new(title=title, goal=goal, owner=owner, task_id=task_id)
        # 顺序：先建目录 + 写 task.json，再改内存 + 推 active + 写 state.json + events.jsonl。
        # 任何一步抛 OSError 都要把已落的盘 / 内存回滚到 "open 没发生" 的状态 —— 服务端
        # 把 OSError 当 "开任务失败" 报给前端，本函数也必须让 store 实际未开。
        self.task_dir(task.id).mkdir(parents=True, exist_ok=True)
        self.artifacts_dir(task.id).mkdir(parents=True, exist_ok=True)
        try:
            self._write_task(task)
        except OSError:
            shutil.rmtree(self.task_dir(task.id), ignore_errors=True)
            raise
        prev_active = self._active_task_id
        self._tasks[task.id] = task
        self._decisions[task.id] = []
        try:
            # set_active 也会写 state.json + 两边的 events.jsonl —— 失败时整体回滚到
            # "open 没发生"。set_active 内部如果抛 OSError，state.json 可能已被改 / 也可能
            # 没被改；这里统一按 "失败" 处理，回写 state.json 到 prev_active 兜底。
            self.set_active(task.id)
        except OSError:
            self._tasks.pop(task.id, None)
            self._decisions.pop(task.id, None)
            self._active_task_id = prev_active
            try:
                self._write_state()
            except OSError:
                _log.warning("open_task rollback: state.json 写回 prev_active 失败")
            shutil.rmtree(self.task_dir(task.id), ignore_errors=True)
            raise
        return task

    def set_active(self, task_id: Optional[str]) -> None:
        """切换 active task。

        ``task_id`` 必须是已存在的 task 或 ``None``。等于当前 active 时是 no-op（不写
        state.json 不发事件）。切换时：旧 active 落 ``became_inactive``、新 active 落
        ``became_active``（两条 events.jsonl 行）；state.json 落新值。

        ``TaskNotFoundError`` 在传入 id 不在 ``self._tasks`` 时抛。
        """
        if task_id is not None and task_id not in self._tasks:
            raise TaskNotFoundError(f"task_id={task_id!r} 不存在")
        prev = self._active_task_id
        if prev == task_id:
            return  # no-op
        self._active_task_id = task_id
        try:
            self._write_state()
        except OSError:
            self._active_task_id = prev  # 不写 events.jsonl
            raise
        # state.json 落盘成功后再写 events.jsonl —— 两边失败 events 缺一行可接受（落盘
        # 宽容 §8.1），缺一行不影响 task_info 权威快照（events 是 hint 历史，不参与
        # state 重建）。
        if prev is not None:
            self._append_event(prev, EVENT_KIND_BECAME_INACTIVE)
        if task_id is not None:
            self._append_event(task_id, EVENT_KIND_BECAME_ACTIVE)

    def close_task(self, task_id: str, *, status: TaskStatus) -> Task:
        """把任务状态推到终结态（``"done"`` / ``"abandoned"``）+ 写 closed_at_ms。

        若该任务当前是 active，连带调 :meth:`set_active(None)`（会写 became_inactive 行）。
        已经 closed 的任务再 close → :class:`TaskAlreadyClosedError`。其它 status 值 →
        ``ValueError``（应走 :meth:`update_task`）。
        """
        if status not in CLOSED_STATUSES:
            raise ValueError(
                f"close_task: status={status!r} 不是终结态；in_progress / blocked 走 update_task"
            )
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"task_id={task_id!r} 不存在")
        if task.status in CLOSED_STATUSES:
            raise TaskAlreadyClosedError(
                f"task {task_id!r} 已经 status={task.status!r}，不能重复关闭"
            )
        now = now_ms()
        new_task = dataclasses.replace(
            task, status=status, updated_at_ms=now, closed_at_ms=now,
        )
        self._write_task(new_task)
        self._tasks[task.id] = new_task
        self._append_event(task.id, EVENT_KIND_CLOSED, status=status)
        if self._active_task_id == task.id:
            # set_active(None) 会写 became_inactive 行 + state.json。失败时本函数仍返
            # 新 task（关闭已落盘）；上层应当看 state.json 与 task.status 一致性自愈。
            self.set_active(None)
        return new_task

    def update_task(
        self,
        task_id: str,
        *,
        title: Optional[str] = None,
        goal: Optional[str] = None,
        owner: object = _OWNER_UNSET,
        status: Optional[TaskStatus] = None,
    ) -> Task:
        """更新 title / goal / owner / status。

        P5.2.3 扩展：owner / status 可改。语义：

        - ``title`` / ``goal`` / ``status`` 传 ``None`` = "不改"。
        - ``owner`` 沿 inbound patch 语义可显式置 None（清归属），所以用 sentinel
          ``_OWNER_UNSET`` 区分"不改"与"置 None"。调用方应传 str / None 或 不传。
        - ``status`` 仅接受非终结态 ``{open, in_progress, blocked}``；``done`` / ``abandoned``
          必须走 :meth:`close_task`（保证 ``closed_at_ms`` 落盘）。无效 status → ``ValueError``。
        - 把 closed 任务的 status 改回非终结态（"重开"）→ 清 ``closed_at_ms``。

        每个改变的字段落一行 ``events.jsonl``：
        ``{kind: "field_changed", field, before, after}``。返回更新后的 Task。
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"task_id={task_id!r} 不存在")
        if status is not None and status not in NON_TERMINAL_STATUSES:
            raise ValueError(
                f"update_task: status={status!r} 不可用；终结态 done / abandoned 请走 close_task"
            )
        changes: list[tuple[str, object, object]] = []
        patch: dict = {}
        if title is not None and title != task.title:
            changes.append(("title", task.title, title))
            patch["title"] = title
        if goal is not None and goal != task.goal:
            changes.append(("goal", task.goal, goal))
            patch["goal"] = goal
        if owner is not _OWNER_UNSET and owner != task.owner:
            changes.append(("owner", task.owner, owner))
            patch["owner"] = owner
        if status is not None and status != task.status:
            changes.append(("status", task.status, status))
            patch["status"] = status
            # 重开闭合任务（done/abandoned → in_progress 等）：清 closed_at_ms。
            if task.status in CLOSED_STATUSES:
                patch["closed_at_ms"] = None
        if not changes:
            return task  # no-op，不戳 updated_at_ms 也不写 events
        patch["updated_at_ms"] = now_ms()
        new_task = dataclasses.replace(task, **patch)
        self._write_task(new_task)
        self._tasks[task.id] = new_task
        for field_name, before, after in changes:
            self._append_event(
                task.id, EVENT_KIND_FIELD_CHANGED,
                field=field_name, before=before, after=after,
            )
        return new_task

    def add_decision(
        self,
        task_id: str,
        *,
        supporting_message_ids: Iterable[str],
        summary: str,
    ) -> Decision:
        """追加一条决策。`marked_by` 在 P5.1 固定为 "user"。"""
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"task_id={task_id!r} 不存在")
        d = Decision.new(
            task_id=task_id,
            supporting_message_ids=supporting_message_ids,
            summary=summary,
        )
        path = self.decisions_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl(path, d.to_jsonl_dict())
        self._decisions.setdefault(task_id, []).append(d)
        return d

    def attach_artifact(
        self,
        task_id: str,
        *,
        share_rel: str,
        share_root: Path,
    ) -> dict:
        """从 ``share_root / share_rel`` **拷贝** 到 ``tasks/<id>/artifacts/<name>``。

        copy 不 move —— share/ 副本仍在原位，茶客通过 ``./share/<file>`` 仍能读到（§4.3）。
        同名文件存在时：覆盖目标（用户在 UI 上选了同名的就是想替换）。

        ``share_rel`` 不允许 ``..`` / 绝对路径 —— share_root 之外的源直接 raise，避免
        把任意 OS 路径拷进 task 目录。

        返回 ``{name, size, rel}`` —— 给 server 合成 ``task_artifact_added`` envelope 用。
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"task_id={task_id!r} 不存在")
        # 规整 + 越界检查。前端 upload 流 emit 的 rel 含 ``share/`` 前缀（与上传 envelope
        # 的 ``rel`` 字段同口径）；attach_artifact 把它直接喂进来时要把前缀剥掉，否则与
        # ``share_root``（已经是 ``<room>/share/``）拼出 ``<room>/share/share/<name>``。
        rel_str = share_rel
        for prefix in ("share/", "./share/"):
            if rel_str.startswith(prefix):
                rel_str = rel_str[len(prefix):]
                break
        rel = Path(rel_str)
        if not rel_str or rel.is_absolute() or ".." in rel.parts:
            raise ArtifactSourceMissingError(
                f"share_rel={share_rel!r} 必须是 share/ 内的相对路径"
            )
        src = share_root / rel
        # resolve 一次 + 守 share_root 边界 —— ``..`` 已在 rel.parts 拒了，但 share/ 里
        # 出现的 symlink（手工放进去或茶客工具产出）会绕过那条检查。``shutil.copy2`` /
        # ``Path.is_file`` 都跟随软链，不挡这条边界后果是任意路径外文件被拷进 task。
        try:
            real_src = src.resolve(strict=True)
            real_share = share_root.resolve(strict=True)
        except OSError:
            raise ArtifactSourceMissingError(
                f"share/ 下找不到源文件 {share_rel!r}"
            )
        if not real_src.is_relative_to(real_share):
            raise ArtifactSourceMissingError(
                f"share_rel={share_rel!r} 解析后逃出 share/（symlink 指向 share/ 外）"
            )
        if not real_src.is_file():
            raise ArtifactSourceMissingError(
                f"share/ 下找不到源文件 {share_rel!r}"
            )
        adir = self.artifacts_dir(task_id)
        adir.mkdir(parents=True, exist_ok=True)
        dst_name = real_src.name
        # share/ 上传的文件名也走 artifact name 校验 —— 避免用户把 .DS_Store / Thumbs.db
        # 拖进 share/ 后 attach 进 artifacts/，落地但又被 detector 当元数据跳过的鬼影。
        invalid = _validate_artifact_name(dst_name)
        if invalid is not None:
            raise ArtifactSourceMissingError(f"share_rel={share_rel!r}: {invalid}")
        dst = adir / dst_name
        shutil.copy2(real_src, dst)
        try:
            size = dst.stat().st_size
        except OSError:
            size = 0
        return {
            "name": dst_name,
            "size": size,
            "rel": f"tasks/{task_id}/{_ARTIFACTS_DIRNAME}/{dst_name}",
        }

    def clear_artifacts(self, task_id: str) -> list[str]:
        """删空 ``tasks/<task_id>/artifacts/`` 下所有可见产物文件，返回已删的文件名列表。

        范围严格只删 :meth:`artifacts_dir` 一层下的常规文件 —— 跳过子目录（P5.1 不支持
        子目录，但守卫一下 future-proof）、跳过 :func:`_is_os_metadata_file` 命中的元
        数据条目（``.DS_Store`` / ``._foo`` 等本来就不入 :meth:`list_artifacts`，删它们
        反而把 OS 重新生成的影子文件牵连进来）。**不动 ``task.json`` / ``decisions.jsonl``
        / ``summary.jsonl`` / ``events.jsonl``** —— 只清"产物"那一层，与 ``attach_artifact``
        / ``write_artifact`` 的范围对称。已 closed 任务**本方法不挡** —— 与
        :meth:`write_artifact` 注释"状态守卫由调用方负责"同口径。

        单文件删除失败时：WARN 跳过、不抛、继续删余下文件，返回成功删除的子集 —— 与
        rotation "部分失败不阻断" 同口径。``artifacts_dir`` 不存在 / 任务无产物 → 返回
        空列表（不视作错误）。

        落 ``events.jsonl`` 一条 ``artifacts_cleared{count, names}`` 给未来 audit。
        """
        if task_id not in self._tasks:
            raise TaskNotFoundError(f"task_id={task_id!r} 不存在")
        adir = self.artifacts_dir(task_id)
        if not adir.is_dir():
            return []
        deleted: list[str] = []
        for p in sorted(adir.iterdir()):
            if not p.is_file():
                continue
            if _is_os_metadata_file(p.name):
                continue
            try:
                p.unlink()
            except OSError:
                _log.warning("clear_artifacts: 删除失败 %s", p, exc_info=True)
                continue
            deleted.append(p.name)
        if deleted:
            self._append_event(
                task_id, EVENT_KIND_ARTIFACTS_CLEARED,
                count=len(deleted), names=deleted,
            )
        return deleted

    def write_artifact(self, task_id: str, *, name: str, content: str) -> dict:
        """直接把 ``content`` 写入 ``tasks/<task_id>/artifacts/<name>``。

        与 :meth:`attach_artifact` 区别：attach 从 ``share/`` 拷贝既有文件（copy 不 move），
        本方法从内存字符串落盘新文件。茶客的 ``task_write_artifact`` 工具走这条 ——
        agentao ``WriteFileTool`` 经 ``PathPolicy`` 检查后会拒绝 ``./task/<x>``（软链解析
        到 cwd 外），所以茶客不能走原生写工具，必须通过 chahua 自己的路径。

        ``name`` 走 :func:`_validate_artifact_name`（与 attach_artifact 同口径）；非法时
        抛 :class:`ValueError`。task 不存在抛 :class:`TaskNotFoundError`。已 closed 的
        task **本方法不挡**——状态守卫由调用方负责（``task_write_artifact`` 工具在 store
        外检查 ``CLOSED_STATUSES``，与 attach_artifact "可往 closed 任务追老资料"口径
        对称）。

        返 ``{name, size, rel}`` 与 ``attach_artifact`` 同形 —— 便于上层合成
        ``task_artifact_added`` envelope。
        """
        if task_id not in self._tasks:
            raise TaskNotFoundError(f"task_id={task_id!r} 不存在")
        invalid = _validate_artifact_name(name)
        if invalid is not None:
            raise ValueError(invalid)
        adir = self.artifacts_dir(task_id)
        adir.mkdir(parents=True, exist_ok=True)
        dst = adir / name
        dst.write_text(content, encoding="utf-8")
        return {
            "name": name,
            "size": len(content.encode("utf-8")),
            "rel": f"tasks/{task_id}/{_ARTIFACTS_DIRNAME}/{name}",
        }


def build_task_info_payload(store: TasksStore) -> dict:
    """``task_info`` envelope 的 data payload 单点构造（docs §4.2）。

    被两条路径共用：``TaskHandlers._emit_task_info``（每次入站任务变更后重发）
    和 ``Orchestrator._kick_detect_new_artifacts``（pick 周期末尾扫到新 artifact 后）。
    两边都依赖同一 shape —— 加字段时改这一处，避免漏改导致前端收到两种 shape。
    """
    return {
        "tasks": [
            {
                **t.to_jsonl_dict(),
                "artifacts": store.list_artifacts(t.id),
                "decisions": [d.to_jsonl_dict() for d in store.list_decisions(t.id)],
            }
            for t in store.list_tasks()
        ],
        "active_task_id": store.active_task_id,
    }
