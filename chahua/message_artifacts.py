"""消息 ↔ 茶客产物关联（"在气泡后挂图片 / 下载链"持久化层）。

`transport_bridge` 在写盘工具命中时把 ``rel → message_id`` 压进 :attr:`pending`；
:class:`ArtifactDetector` 在 pick 周期末扫盘后 :meth:`consume_pending` 取出，命中即
:meth:`persist` 一行到 ``rooms/<id>/message_artifacts.jsonl`` 并把
``originated_message_id`` 挂到 ``task_artifact_added`` / ``room_artifact_added``
envelope —— 前端据此把图片 / 下载链挂到对应气泡末尾、不再走系统气泡兜底
（envelope_router）。

P10.2 起 join key 从 ``(task_id, name)`` 换成 ``rel``，让任务产物
（``tasks/<id>/artifacts/<name>``）和房间公共桌面产物（``share/<...>``）走同一管线：

- 任务产物：rel = ``tasks/<task_id>/artifacts/<name>``，:meth:`ArtifactRef.task_id`
  返回 ``task_id`` 给前端任务面板挂钩。
- 房间产物：rel = ``share/<...>``（多层子目录合法，与 ``download_file`` 白名单同口径），
  :meth:`ArtifactRef.task_id` 返 ``None``。

加载阶段读 jsonl 重建 :attr:`by_message`，``emit_room_history`` 透传给前端 ——
切回房间 / 刷新后历史挂件能还原（设计选择 A=是）。

持久化口径与 ``transcript.jsonl`` 一致：append-only、不 fsync、加载跳坏行；
未知字段 warn 后忽略（落盘宽容）。仅必需字段缺才跳该条。

**向前兼容**：P10.2 前的 jsonl 行（``{message_id, task_id, name, size}``，无 ``rel``
字段）按 ``tasks/<task_id>/artifacts/<name>`` 公式补出 ``rel`` 后加载。
新写盘的行恒含 ``rel`` 字段。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ._persist import append_jsonl, read_jsonl_skip_bad

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """单条「某消息产出的产物」的不可变快照。

    ``rel`` 是 join key + 下载白名单段（``share/<...>`` 或
    ``tasks/<id>/artifacts/<name>``）；落盘的 ``size`` 是写入瞬时大小，前端只用它做
    下载按钮副文本；真正下载时 server 再读盘取最新 size。``task_id`` 是从 ``rel``
    现场派生（非存储字段）—— 任务产物有值、房间公共桌面（share/）产物为 ``None``。
    """

    rel: str
    name: str
    size: int

    @property
    def task_id(self) -> Optional[str]:
        """从 ``rel`` 派生：``tasks/<id>/artifacts/<name>`` 形 → ``<id>``；
        ``share/<...>`` 形 → ``None``（房间公共桌面，不归属任务）。"""
        if not self.rel.startswith("tasks/"):
            return None
        parts = self.rel.split("/")
        if len(parts) >= 4 and parts[2] == "artifacts":
            return parts[1]
        return None

    def to_payload(self) -> dict:
        """投影给 ``room_history`` envelope 用 —— 字段同 ``task_artifact_added`` /
        ``room_artifact_added`` 的相关键，前端单一 ``attachArtifactToBubble`` 渲染入口。
        ``task_id`` 始终带（房间产物为 ``None``）。"""
        return {
            "rel": self.rel,
            "name": self.name,
            "size": self.size,
            "task_id": self.task_id,
        }


@dataclass
class MessageArtifactRegistry:
    """房间级注册表：跟踪「哪条消息产出哪些 artifact」。

    一个 :class:`chahua.session.RoomSession` 持一份，与 ``transcript.jsonl`` 同
    生命周期。``store_path=None`` = 纯内存（测试 / 早期 CLI）。
    """

    store_path: Optional[Path] = None

    by_message: dict[str, list[ArtifactRef]] = field(default_factory=dict)
    """message_id → 该消息流式期间产出的 artifact 列表。构造时从 ``store_path`` 加载；
    后续 :meth:`persist` 增量追加。"""

    pending: dict[str, str] = field(default_factory=dict)
    """``rel → message_id`` 流水线缓存：``transport_bridge`` 在写盘工具
    ``tool_start`` 时写入（``task_write_artifact`` 写 ``tasks/<id>/artifacts/<name>``、
    原生 ``write_file`` 写 ``./share/<...>`` 时落入此表）；``ArtifactDetector.detect``
    在 emit envelope 时 :meth:`consume_pending` 取出 + 落盘。

    存在的理由：detect 跑在 pick 周期末，扫盘 diff 算 ``new_rels`` —— 这时早已
    没有「哪个 tool 调用产出了哪个文件」的线索了（``tool_complete`` 已发完）。
    用一个内存 dict 把工具调用 → message_id 的关联从 ``tool_start`` 跨到 detect 段。
    """

    def __post_init__(self) -> None:
        if self.store_path is None:
            return
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        assert self.store_path is not None
        for obj in read_jsonl_skip_bad(self.store_path):
            # P10.3 修：必须严格 isinstance(str) —— ``str(None)`` 会硅化成字面值
            # ``"None"`` 并悄悄当合法 mid/name 用，污染 by_message。空 str 也拒。
            mid_raw = obj.get("message_id")
            name_raw = obj.get("name")
            if not isinstance(mid_raw, str) or not mid_raw:
                _log.warning("skip message_artifacts line (message_id 非 str): %r", obj)
                continue
            if not isinstance(name_raw, str) or not name_raw:
                _log.warning("skip message_artifacts line (name 非 str): %r", obj)
                continue
            mid = mid_raw
            name = name_raw
            # 兼容旧形：``{message_id, task_id, name, size}``（P10.2 前）
            # → 按 ``tasks/<id>/artifacts/<name>`` 补 ``rel``；新形含 ``rel`` 直接用。
            rel_raw = obj.get("rel")
            if isinstance(rel_raw, str) and rel_raw:
                rel = rel_raw
            else:
                tid_raw = obj.get("task_id")
                if not isinstance(tid_raw, str) or not tid_raw:
                    _log.warning(
                        "skip message_artifacts line (无 rel 也无 task_id): %r", obj,
                    )
                    continue
                rel = f"tasks/{tid_raw}/artifacts/{name}"
            # P10.4 review §7 修：rel 必须落在已知 root（``share/`` 或 ``tasks/``）。
            # 防御 jsonl 被篡改 / 损坏行携带 ``../../`` 之类的 traversal —— 加载后
            # ``emit_room_history`` 直接 ``room_dir / r.rel`` 解析、过 ``is_file()``，
            # 没 traversal 守卫；下游 ``_download_file`` 虽有自己的 whitelist 拦真下载，
            # 这里加一道入口校验作为 defense-in-depth，与 ``_normalize_share_rel`` /
            # tasks_store 的 name 校验同纪律。tasks 形再确认含 ``artifacts/`` 段，
            # 拒 ``tasks/../foo`` / ``tasks/x/y/z``（非 artifacts 中段）等异形 rel。
            if rel.startswith("share/"):
                # 与 ``_normalize_share_rel`` 对称：拒空段 / ``.`` / ``..`` /
                # ``\`` —— 防 ``share/../../some-file`` 这类被篡改 / 损坏 jsonl
                # 行在 ``emit_room_history`` 的 ``(room_dir / r.rel).is_file()``
                # 解析里逃出 share/ 目录（下游 ``_download_file`` 虽有 whitelist
                # 拦真下载，但破历史挂件 + 路径逃逸的存在检查 = 不可接受）。
                seg = rel.split("/")
                if "\\" in rel or any(s in ("", ".", "..") for s in seg):
                    _log.warning(
                        "skip message_artifacts line (share/ rel 形态异常): %r",
                        rel,
                    )
                    continue
            elif rel.startswith("tasks/"):
                seg = rel.split("/")
                if (
                    len(seg) < 4
                    or seg[2] != "artifacts"
                    or any(s in ("", ".", "..") for s in seg)
                ):
                    _log.warning(
                        "skip message_artifacts line (tasks/ rel 形态异常): %r",
                        rel,
                    )
                    continue
            else:
                _log.warning(
                    "skip message_artifacts line (rel 不属 share/ 或 tasks/): %r",
                    rel,
                )
                continue
            size_raw = obj.get("size", 0)
            # P10.3 修：``isinstance(True, int)`` 是 True、``int(True) == 1`` 不抛错，
            # 所以光 try/except 接 ValueError 拦不住 bool。显式拒 bool。
            if isinstance(size_raw, bool):
                _log.warning(
                    "message_artifacts %s: size=%r 是 bool，置 0", rel, size_raw,
                )
                size = 0
            else:
                try:
                    size = int(size_raw)
                except (TypeError, ValueError):
                    _log.warning(
                        "message_artifacts %s: size=%r 非 int，置 0",
                        rel, size_raw,
                    )
                    size = 0
            self.by_message.setdefault(mid, []).append(
                ArtifactRef(rel=rel, name=name, size=size)
            )

    def record_pending(
        self, *, rel: str, name: str, message_id: str
    ) -> None:
        """``transport_bridge`` 在写盘工具 ``tool_start`` 时调（``task_write_artifact``
        或 ``write_file('./share/<x>')``）。

        同 ``rel`` 后写覆盖前写 —— 同一条消息内多次写同文件，最后一次的 message_id
        仍是这条消息本身，覆盖无害；跨消息同 rel 覆写时新 message_id 替换旧的，
        符合「这次是新消息产出的版本」直觉。``name`` 当前只用于派生未来 envelope 字段；
        与 rel 的 basename 一致由调用方保证。
        """
        self.pending[rel] = message_id

    def consume_pending(self, *, rel: str) -> Optional[str]:
        """``ArtifactDetector.detect`` 在 emit envelope 前调，取 + 删。

        命不中 → ``None``（用户上传 / 跨周期遗留 / 茶客绕开预期工具直接写盘）。
        """
        return self.pending.pop(rel, None)

    def persist(
        self, *, message_id: str, rel: str, name: str, size: int
    ) -> ArtifactRef:
        """记录一条 message → artifact 关联，同步落盘。

        重复调（同 message_id + rel）会重复落盘 / 重复进 in-memory list —— 调用方
        （``ArtifactDetector.detect``）已用 diff 保证不重复。前端读时按 rel 去重兜底。
        """
        ref = ArtifactRef(rel=rel, name=name, size=size)
        self.by_message.setdefault(message_id, []).append(ref)
        if self.store_path is not None:
            append_jsonl(self.store_path, {
                "message_id": message_id,
                "rel": rel,
                "name": name,
                "size": size,
            })
        return ref

    def for_message(self, message_id: str) -> list[ArtifactRef]:
        """``emit_room_history`` 按 message 投影时用。返回的 list 是内部 list 的
        copy（外部改不污染注册表）。"""
        return list(self.by_message.get(message_id, ()))

    def clear(self) -> None:
        """``reset_room`` / ``clear_room`` 入口调。截断文件不 unlink ——
        与 ``transcript.clear()`` 同口径。"""
        self.by_message.clear()
        self.pending.clear()
        if self.store_path is not None:
            with self.store_path.open("w", encoding="utf-8"):
                pass
