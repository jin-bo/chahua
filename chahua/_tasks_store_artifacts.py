""":class:`ArtifactOps` —— :class:`TasksStore` 的产物（artifacts/）子域 slot。

设计见 [`docs/P5-任务房间.md`](../docs/P5-任务房间.md) §4.3。把原 ``tasks_store.py`` 里
``list_artifacts`` / ``attach_artifact`` / ``clear_artifacts`` / ``write_artifact`` 4 个
产物方法 + 配套的 ``_validate_artifact_name`` / ``_is_os_metadata_file`` 校验 helper +
``_ARTIFACTS_DIRNAME`` / ``_OS_METADATA_FILENAMES`` / ``EVENT_KIND_ARTIFACTS_CLEARED`` 常量
搬到本模块。:class:`TasksStore` 保留 4 个薄转发方法走 ``self._artifacts_ops.*``，外部调
用方（``server_inbound_task`` / ``task_tools`` / ``context_renderer`` / ``artifact_detector``
/ ``transport_bridge`` / ``cli`` + 测试）继续 ``store.list_artifacts(...)`` 等公开接口零改动。

承重不变量（CLAUDE.md 任务房间 §）：

- artifact 名校验四档（空 / 含 ``/`` / 含 ``\\`` / 含 ``..`` / 前缀 ``.``）—— attach / write
  共用 :func:`_validate_artifact_name`，与 detector 跳过 :func:`_is_os_metadata_file`
  口径一致（避免「落盘后又被 detector 跳过」的鬼影 artifact）。
- ``attach_artifact`` 是 **copy 不是 move**；source resolve 后必须 ``is_relative_to``
  ``share_root``（拒 symlink 逃逸）；返 ``{name, size, rel}``。
- ``clear_artifacts`` 范围严格：只删 ``tasks/<id>/artifacts/`` 一层下的常规文件，不动
  ``task.json`` / ``decisions.jsonl`` / ``events.jsonl`` / ``summary.jsonl``；已 closed
  任务本方法不挡（与 :meth:`write_artifact` 同口径，状态守卫由调用方负责）。
- ``write_artifact`` 是茶客 ``task_write_artifact`` 工具的落盘路径 —— 绕开 agentao
  ``PathPolicy``（茶客原生 write_file 解析 ``./task/`` 软链后落到 cwd 外被拒）。
- 循环 import：本模块依赖 :class:`tasks_store.TaskNotFoundError` /
  :class:`tasks_store.ArtifactSourceMissingError`，``tasks_store.py`` 必须在 import 本模块**之前**
  定义这两个 exception。Python partial-module loading 保证本模块 import 到 ``tasks_store``
  时只能看到已定义符号 —— 顺序违反会变 ``ImportError``。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ._persist import append_jsonl
from .events import new_event_id, now_ms

if TYPE_CHECKING:
    from .tasks_store import TasksStore

_log = logging.getLogger(__name__)


# ── 路径 / 事件 / 校验常量 ─────────────────────────────────────────────────

_ARTIFACTS_DIRNAME = "artifacts"

# events.jsonl `kind` —— :meth:`ArtifactOps.clear_artifacts` 落盘用，tasks_store 顶
# events 常量族成员之一。
EVENT_KIND_ARTIFACTS_CLEARED = "artifacts_cleared"  # payload: {"count", "names": [...]}

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


class ArtifactOps:
    """:class:`TasksStore` 的产物子域。持 store 反向引用做依赖注入 —— 跨子域访问
    （``store._tasks`` 存在性校验 / ``store.artifacts_dir(task_id)`` 路径 /
    ``store._append_event`` events.jsonl audit）走 ``self.store.xxx``，与 server 的
    handler slot 同口径。
    """

    def __init__(self, store: "TasksStore") -> None:
        self.store = store

    def list_artifacts(self, task_id: str) -> list[dict]:
        """artifacts/ 目录扫，返回 ``[{name, size, mtime_ms, rel}, ...]``，按名升序。

        ``rel`` 与 :meth:`attach_artifact` 返回值同口径 —— ``task_info`` 是权威快照（docs
        §4.2 事件分工），漏 ``rel`` 会让 reconnect / 错过 hint 后 UI 拿不到产物落盘路径。

        不递归 —— P5.1 不支持子目录；后续如要支持先在 attach_artifact 加 reject 子目录。
        """
        out: list[dict] = []
        adir = self.store.artifacts_dir(task_id)
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
        # 延迟 import 避 import-loop —— ArtifactSourceMissingError / TaskNotFoundError
        # 定义在 tasks_store.py 中，本模块从那里 import 仅在函数体里走 partial-load 安全。
        from .tasks_store import ArtifactSourceMissingError, TaskNotFoundError

        task = self.store._tasks.get(task_id)
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
        adir = self.store.artifacts_dir(task_id)
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

        范围严格只删 ``store.artifacts_dir`` 一层下的常规文件 —— 跳过子目录（P5.1 不支持
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
        from .tasks_store import TaskNotFoundError

        if task_id not in self.store._tasks:
            raise TaskNotFoundError(f"task_id={task_id!r} 不存在")
        adir = self.store.artifacts_dir(task_id)
        if not adir.is_dir():
            return []
        deleted: list[str] = []
        for p in adir.iterdir():
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
            self.store._append_event(
                task_id, EVENT_KIND_ARTIFACTS_CLEARED,
                count=len(deleted), names=deleted,
            )
        return deleted

    def write_artifact(
        self, task_id: str, *, name: str, content: str, append: bool = False,
    ) -> dict:
        """直接把 ``content`` 写入 ``tasks/<task_id>/artifacts/<name>``。

        与 :meth:`attach_artifact` 区别：attach 从 ``share/`` 拷贝既有文件（copy 不 move），
        本方法从内存字符串落盘。茶客的 ``task_write_artifact`` 工具走这条 ——
        agentao ``WriteFileTool`` 经 ``PathPolicy`` 检查后会拒绝 ``./task/<x>``（软链解析
        到 cwd 外），所以茶客不能走原生写工具，必须通过 chahua 自己的路径。

        ``append=False``（默认）整体覆盖写；``append=True`` 追加到文件末尾（文件不存在则
        新建）—— 与 agentao ``WriteFileTool`` 的 ``append`` 形参同口径，给「往复盘 /
        日志类产物增量补内容」省去「先 read 全文再整体写回」的往返。

        ``name`` 走 :func:`_validate_artifact_name`（与 attach_artifact 同口径）；非法时
        抛 :class:`ValueError`。task 不存在抛 :class:`TaskNotFoundError`。已 closed 的
        task **本方法不挡**——状态守卫由调用方负责（``task_write_artifact`` 工具在 store
        外检查 ``CLOSED_STATUSES``，与 attach_artifact "可往 closed 任务追老资料"口径
        对称）。

        返 ``{name, size, rel}`` 与 ``attach_artifact`` 同形（``size`` 是操作后的磁盘
        文件总字节数，append 模式下含原有内容）—— 便于上层合成 ``task_artifact_added``
        envelope。
        """
        from .tasks_store import TaskNotFoundError

        if task_id not in self.store._tasks:
            raise TaskNotFoundError(f"task_id={task_id!r} 不存在")
        invalid = _validate_artifact_name(name)
        if invalid is not None:
            raise ValueError(invalid)
        adir = self.store.artifacts_dir(task_id)
        adir.mkdir(parents=True, exist_ok=True)
        dst = adir / name
        if append:
            with dst.open("a", encoding="utf-8") as f:
                f.write(content)
        else:
            dst.write_text(content, encoding="utf-8")
        return {
            "name": name,
            "size": dst.stat().st_size,
            "rel": f"tasks/{task_id}/{_ARTIFACTS_DIRNAME}/{name}",
        }
