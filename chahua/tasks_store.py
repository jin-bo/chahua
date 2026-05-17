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

不写 `events.jsonl` / `<id>/summary.jsonl` —— 留待 P5.2 接 status / 摘要时再加。

P5.1 关键约束（docs §7.1）：

- **一房间最多 1 个有效任务**：``open_task`` 在 ``tasks/*/task.json`` 已存在任意有效
  任务时 raise :class:`TaskExistsError`。判定按 task.json 存在性扫，**不依赖 state.json**
  —— state.json 可能丢失 / 被清空，但旧 task.json 是"已有任务"的真凭据。
- **加载时双向修复**：
    ① state.json 指向不存在 task → 清回 None；
    ② state.json 缺 / 为空且**有且仅有一个**有效 task.json → 自动设为 active 并回写
  state.json。"多于一个" 在 P5.1 不会发生（open 拒绝条款），P5.2 起再细化。
- **入站严格**：调用方（server）保证 payload 合法白名单；本模块只对"业务约束"做最后一道
  guard（如 ``open_task`` 的"已有任务"、``attach_artifact`` 的"share/ 源文件存在"）。
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
from .task import Decision, Task

_log = logging.getLogger(__name__)


# 路径常量。room_dir 之下的子树都是 P5.1 新增；与 share/ / guests/ 同级。
_TASKS_DIRNAME = "tasks"
_STATE_FILENAME = "state.json"
_TASK_FILENAME = "task.json"
_DECISIONS_FILENAME = "decisions.jsonl"
_ARTIFACTS_DIRNAME = "artifacts"


class TasksStoreError(Exception):
    """业务约束违例的基类。各种具体错落到下面的子类。"""


class TaskExistsError(TasksStoreError):
    """``open_task`` 在房间已有任意有效任务时 raise。P5.1 单任务限制（§7.1）。"""


class TaskNotFoundError(TasksStoreError):
    """``update_task`` / ``add_decision`` / ``attach_artifact`` 传了不存在的 task_id。"""


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

        P5.1 业务约束：房间已有任意有效任务时 raise :class:`TaskExistsError`。判定按
        ``self._tasks`` 内存镜像，与启动期加载结果一致（§7.1 "判定按 tasks/*/task.json
        存在性扫"）。
        """
        if self._tasks:
            existing = next(iter(self._tasks))
            raise TaskExistsError(
                f"房间已有任务 {existing!r}；P5.1 单任务限制，请等 P5.2 支持多任务"
            )
        task = Task.new(title=title, goal=goal, owner=owner, task_id=task_id)
        # 顺序：先建目录 + 写 task.json，再改内存 + 推 active + 写 state.json。
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
        self._active_task_id = task.id
        try:
            self._write_state()
        except OSError:
            self._active_task_id = prev_active
            self._tasks.pop(task.id, None)
            self._decisions.pop(task.id, None)
            shutil.rmtree(self.task_dir(task.id), ignore_errors=True)
            raise
        return task

    def update_task(
        self,
        task_id: str,
        *,
        title: Optional[str] = None,
        goal: Optional[str] = None,
    ) -> Task:
        """更新 title / goal。P5.1 不接 owner / status —— 调用方传了会被本签名挡掉。

        ``None`` 表示"不改这个字段"，与 inbound payload 的 patch 语义一致。
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"task_id={task_id!r} 不存在")
        if title is None and goal is None:
            return task  # no-op
        patch: dict = {"updated_at_ms": now_ms()}
        if title is not None:
            patch["title"] = title
        if goal is not None:
            patch["goal"] = goal
        new_task = dataclasses.replace(task, **patch)
        self._write_task(new_task)
        self._tasks[task.id] = new_task
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


