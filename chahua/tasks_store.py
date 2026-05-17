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
from .events import now_ms
from .task import Decision, Task, TaskStatus

# close_task 仅接受这两个"终结态"；update_task 仅接受非终结态。
# 同一对常量给 server inbound 白名单 import，避免字面值散落多处。
_CLOSED_STATUSES: frozenset[str] = frozenset({"done", "abandoned"})
_NON_TERMINAL_STATUSES: frozenset[str] = frozenset({"open", "in_progress", "blocked"})

# update_task 的 owner 参数 sentinel —— ``None`` 是合法值（清空 owner），不能复用 None
# 表示"不改"。Python 没有 ``...`` 之外的轻量 marker，单独建一个 module-private 对象。
_OWNER_UNSET: object = object()

_log = logging.getLogger(__name__)


# 路径常量。room_dir 之下的子树都是 P5.1 新增；与 share/ / guests/ 同级。
_TASKS_DIRNAME = "tasks"
_STATE_FILENAME = "state.json"
_TASK_FILENAME = "task.json"
_DECISIONS_FILENAME = "decisions.jsonl"
_EVENTS_FILENAME = "events.jsonl"  # P5.2.2 起
_ARTIFACTS_DIRNAME = "artifacts"


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
        state = read_json_or_none(self.state_path)
        active_raw = (
            state.get("active_task_id")
            if isinstance(state, dict) else None
        )
        if isinstance(active_raw, str) and active_raw in self._tasks:
            self._active_task_id = active_raw
            return
        self._active_task_id = None
        if active_raw is not None:
            _log.warning(
                "state.json.active_task_id=%r 不在已加载任务中，清回 None",
                active_raw,
            )
        # 双向修复 ②：state 空 + 唯一 task → 恢复
        if len(self._tasks) == 1:
            (only_id,) = self._tasks.keys()
            self._active_task_id = only_id
            _log.info(
                "tasks/state.json 缺 active 但仅一个 task.json，自动恢复 active=%s",
                only_id,
            )
            self._write_state()
        elif isinstance(active_raw, str):
            # ①：state 指向不存在 → 清回 None 并回写
            self._write_state()

    def _write_state(self) -> None:
        """落 state.json。``active_task_id == None`` 时写 ``{"active_task_id": null}`` —— 显式
        优于"删文件让加载视为缺失"，前者读端拿到的语义稳定。"""
        write_json_atomic(self.state_path, {"active_task_id": self._active_task_id})

    def _write_task(self, task: Task) -> None:
        path = self.task_json_path(task.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, task.to_jsonl_dict())

    def _append_event(self, task_id: str, kind: str, **payload: object) -> None:
        """task 级 events.jsonl append 一条 ``{ts_ms, kind, ...payload}``。

        unknown task_id 在调用方就该过滤掉；这里不再校验（active_changed / closed 都
        是 mutator 里现场拼，不会传错 id）。``append_jsonl`` 父目录不存在不补建 —— task
        目录在 :meth:`open_task` 已经 ``mkdir(parents=True, exist_ok=True)``。
        """
        record: dict = {"ts_ms": now_ms(), "kind": kind}
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
            self._append_event(prev, "became_inactive")
        if task_id is not None:
            self._append_event(task_id, "became_active")

    def close_task(self, task_id: str, *, status: TaskStatus) -> Task:
        """把任务状态推到终结态（``"done"`` / ``"abandoned"``）+ 写 closed_at_ms。

        若该任务当前是 active，连带调 :meth:`set_active(None)`（会写 became_inactive 行）。
        已经 closed 的任务再 close → :class:`TaskAlreadyClosedError`。其它 status 值 →
        ``ValueError``（应走 :meth:`update_task`）。
        """
        if status not in _CLOSED_STATUSES:
            raise ValueError(
                f"close_task: status={status!r} 不是终结态；in_progress / blocked 走 update_task"
            )
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"task_id={task_id!r} 不存在")
        if task.status in _CLOSED_STATUSES:
            raise TaskAlreadyClosedError(
                f"task {task_id!r} 已经 status={task.status!r}，不能重复关闭"
            )
        now = now_ms()
        new_task = dataclasses.replace(
            task, status=status, updated_at_ms=now, closed_at_ms=now,
        )
        self._write_task(new_task)
        self._tasks[task.id] = new_task
        self._append_event(task.id, "closed", status=status)
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
        if status is not None and status not in _NON_TERMINAL_STATUSES:
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
            if task.status in _CLOSED_STATUSES:
                patch["closed_at_ms"] = None
        if not changes:
            return task  # no-op，不戳 updated_at_ms 也不写 events
        patch["updated_at_ms"] = now_ms()
        new_task = dataclasses.replace(task, **patch)
        self._write_task(new_task)
        self._tasks[task.id] = new_task
        for field_name, before, after in changes:
            self._append_event(
                task.id, "field_changed",
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


