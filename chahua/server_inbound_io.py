"""房间 IO handlers —— upload / export / persona import（P5.2 重构，docs §7.2）。

P5.2 重构：mixin 多继承换成组合。:class:`chahua.server.ChahuaServer` 在 ``__init__``
里 ``self.io = IOHandlers(self)``；本类持 ``self.server`` 反向引用，跨 server 状态
（``_session`` / ``_paths`` / ``_emit_notice`` / ``_emit_room_info``）走 ``self.server.xxx``。
"""

from __future__ import annotations

import base64
import binascii
import logging
from pathlib import Path
from typing import Callable

from . import persona_import
from ._persist import write_bytes_atomic
from ._server_helpers import require_str
from .admin import sanitize_fs_name
from .events import (
    ChahuaEnvelope,
    ChahuaEventType,
    EnvelopeSink,
    NOTICE_LEVEL_ERROR,
    NOTICE_LEVEL_INFO,
)
from . import exporter
from .session import ROOM_SHARE_DIRNAME, ensure_room_share_dir

_log = logging.getLogger(__name__)


INBOUND_IMPORT_PERSONA_FOLDER = "import_persona_folder"
INBOUND_IMPORT_PERSONA_GITHUB = "import_persona_github"
INBOUND_UPLOAD_FILE = "upload_file"
INBOUND_EXPORT_ROOM = "export_room"
INBOUND_DOWNLOAD_FILE = "download_file"


# 单文件上限。WS 入站帧 max=300MB（_WS_MAX_INBOUND_BYTES），base64 4/3 膨胀 → 原始
# 文件极限 ~225MB。设 200MB 让 JSON quoting + 字段开销有头。改大要同步抬 ws max_size。
_UPLOAD_MAX_BYTES = 200 * 1024 * 1024

# 下载同上限：encode 后帧大小经 ws 出口（无单独 outbound 上限，只有底层 WebSocket
# max_size 制约对端 inbound）—— 与上传同口径让用户心智一致："能传上来的也能拉回去"。
_DOWNLOAD_MAX_BYTES = _UPLOAD_MAX_BYTES

# 允许下载的 rel 前缀白名单。``share/`` = 房间公共桌面；``tasks/<id>/artifacts/`` = 任务
# 产物。其它路径（room.toml / transcript.jsonl / .agentao/... / tasks/state.json /
# tasks/<id>/task.json / decisions.jsonl / events.jsonl）一律拒，防穿越读盘。
#
# 注意：``tasks/`` 前缀**单独不够**——`startswith("tasks/")` 会放进 ``tasks/state.json``
# / ``tasks/<id>/task.json`` 等内部元数据。所以 ``_download_file`` 额外强制 ``tasks/``
# 路径必须是 ``tasks/<id>/artifacts/<name>`` 形 4 段且段[2] == "artifacts"。
_DOWNLOAD_ALLOWED_PREFIXES = ("share/", "tasks/")


def _import_success_text(result: "persona_import.ImportedPersona") -> str:
    """导入成功的 notice 文案 —— 含 persona 名 + 头像状态 + sidecar 文件数提示。"""
    parts = [f"已导入 persona「{result.name}」"]
    parts.append("（含头像）" if result.has_avatar else "（无头像）")
    if result.extras:
        parts.append(
            f"另有 {len(result.extras)} 个 sidecar 文件被保留（mcp.json / skills 等，目前运行时尚未消费）"
        )
    return "".join(parts)


class IOHandlers:
    """upload / export / persona import inbound 集合。"""

    def __init__(self, server: "ChahuaServer") -> None:  # type: ignore[name-defined]
        self.server = server

    def _upload_file(
        self, *, filename: str, content_b64: str, sink: EnvelopeSink
    ) -> None:
        """把前端传上来的文件落到房间共享目录 ``<room_dir>/share/``。

        - 文件名经 :func:`sanitize_fs_name` 洗一遍（防 ``../`` traversal 写到 share 外）。
          洗完为空 / 全点 → 拒。
        - base64 解码失败 / 超 :data:`_UPLOAD_MAX_BYTES` → 拒。
        - 重名直接覆盖 —— 用户主动选了同名文件意味着想替换，比"自动加 (1)"明确。
        - 写盘走 :func:`write_bytes_atomic` —— tmp+rename，写一半被 kill 不留残骸。

        **每次入站请求恒发一条 ``file_uploaded`` envelope**（成功 / 失败都发）——
        前端的串行上传循环靠这条 echo 推进队列，没 echo 就永远 await 卡住。data 形态：

        - 成功：``{rel, name, size, original}``
        - 失败：``{original, error}`` —— 无 ``rel``，前端 onServerEcho 跳过 pill 追加。

        失败路径同时 emit 一条 ``notice(level=error)`` 让用户看见可读原因（与 persona
        导入失败口径一致）。
        """
        original = filename
        try:
            safe_name = sanitize_fs_name(filename, label="filename")
        except ValueError as e:
            self._fail_upload(sink, original=original, text=f"文件名非法：{e}")
            return
        try:
            data = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            self._fail_upload(
                sink, original=original, text=f"上传失败（base64 解码）：{e}",
            )
            return
        if len(data) > _UPLOAD_MAX_BYTES:
            self._fail_upload(
                sink, original=original,
                text=f"文件太大（{len(data)} bytes，上限 {_UPLOAD_MAX_BYTES}）",
            )
            return
        try:
            share_dir = ensure_room_share_dir(self.server._session.room_config.room_dir)
            target = share_dir / safe_name
            write_bytes_atomic(target, data)
        except Exception as e:
            _log.exception("upload_file: 写盘失败 name=%r", safe_name)
            self._fail_upload(sink, original=original, text=f"上传失败：{e}")
            return
        rel = f"{ROOM_SHARE_DIRNAME}/{safe_name}"
        _log.info("upload_file: %s (%d bytes)", rel, len(data))
        sink(
            ChahuaEnvelope(
                room_id=self.server._session.room.name,
                turn_id=None,
                guest_name=None,
                message_id=None,
                type=ChahuaEventType.FILE_UPLOADED,
                data={
                    "rel": rel,
                    "name": safe_name,
                    "size": len(data),
                    "original": original,
                },
            )
        )

    def _run_import(
        self,
        label: str,
        op: Callable[[], "persona_import.ImportedPersona"],
        sink: EnvelopeSink,
    ) -> None:
        """跑一次 persona import + 统一 notice/room_info emit。

        统一三态：``PersonaImportError`` 拿用户可见原因；其它异常吞成"内部错误"避免
        把 traceback / 路径泄到前端；成功打 success 文案。失败也重发 room_info 让前端
        modal 状态复位（与 add_guest / create_room 同款）。
        """
        try:
            result = op()
        except persona_import.PersonaImportError as e:
            _log.info("%s 失败：%s", label, e)
            self.server._emit_notice(sink, level=NOTICE_LEVEL_ERROR, text=str(e))
            self.server._emit_room_info(sink)
            return
        except Exception as e:
            _log.exception("%s 意外错", label)
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text=f"导入失败（内部错误）：{e}"
            )
            self.server._emit_room_info(sink)
            return
        _log.info("%s → %s", label, result.persona_rel)
        self.server._emit_notice(
            sink, level=NOTICE_LEVEL_INFO, text=_import_success_text(result)
        )
        self.server._emit_room_info(sink)

    def _export_room(self, sink: EnvelopeSink) -> None:
        # read-only：不动 session、不写盘。导出物只活在用户的 Downloads/ 里（renderer
        # 端走 Blob + <a download>），房间目录 transcript.jsonl / summary.jsonl 不动。
        msgs = self.server._session.room.messages_since(0)
        filename, content = exporter.format_room_markdown(
            self.server._session.room_config,
            msgs,
            self.server._session.user_config.display_name,
        )
        sink(
            ChahuaEnvelope(
                room_id=self.server._session.room.name,
                turn_id=None,
                guest_name=None,
                message_id=None,
                type=ChahuaEventType.ROOM_EXPORT,
                data={"filename": filename, "markdown": content},
            )
        )
        _log.info(
            "export_room: room=%r %d msg → %s (%d bytes)",
            self.server._session.room.name, len(msgs), filename, len(content),
        )

    async def _inbound_import_persona_folder(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        src = require_str(data, "path", where=INBOUND_IMPORT_PERSONA_FOLDER)
        if src is None:
            return
        # 导入不动 session，无需 cancel inflight。
        self._run_import(
            f"import_persona_folder src={src!r}",
            lambda: persona_import.import_from_folder(self.server._paths, Path(src)),
            sink,
        )

    async def _inbound_import_persona_github(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        url = require_str(data, "url", where=INBOUND_IMPORT_PERSONA_GITHUB)
        if url is None:
            return
        self._run_import(
            f"import_persona_github url={url!r}",
            lambda: persona_import.import_from_github(self.server._paths, url),
            sink,
        )

    async def _inbound_upload_file(self, data: dict, sink: EnvelopeSink) -> None:
        # 任意校验失败也要 emit FILE_UPLOADED(error) —— 前端串行上传循环靠 echo 推进
        # 队列；inbound 早返不发 echo 会让循环永挂（典型 case：零字节文件 content_b64
        # 为空，require_str 没 allow_empty 时返 None）。
        raw_filename = data.get("filename")
        # 用 raw 当 echo.original：哪怕是 None / 非 str，转 str 也好让前端能匹配到 pill
        # 占位（虽然 valid 上传里不会触发；这条路径是 wscat 直发的兜底）。
        echo_original = (
            raw_filename if isinstance(raw_filename, str) else ""
        )
        filename = require_str(data, "filename", where=INBOUND_UPLOAD_FILE)
        if filename is None:
            self._fail_upload(sink, original=echo_original, text="文件名缺失或非法")
            return
        content_b64 = data.get("content_b64")
        if not isinstance(content_b64, str):
            self._fail_upload(
                sink, original=echo_original, text="content_b64 缺失或非 str",
            )
            return
        # 上传不动 session、不挡 inflight turn —— 让正在跑的 turn 自然结束；
        # 文件落房间共享目录，下一条 user_message 才把它带进上下文。
        # 允许 content_b64 == ""（零字节文件）—— _upload_file 内 base64.b64decode("") = b""。
        self._upload_file(filename=filename, content_b64=content_b64, sink=sink)

    def _fail_upload(
        self, sink: EnvelopeSink, *, original: str, text: str,
    ) -> None:
        """上传请求失败的统一回吐：NOTICE 给用户看 + FILE_UPLOADED(error) 让前端推进队列。

        ``_upload_file`` 内部错误路径与 ``_inbound_upload_file`` 的早返路径共用 —— 任何
        UPLOAD_FILE 入帧都必须以一条 FILE_UPLOADED envelope 收尾（成功 / 失败）。
        """
        self.server._emit_notice(sink, level=NOTICE_LEVEL_ERROR, text=text)
        sink(
            ChahuaEnvelope(
                room_id=self.server._session.room.name,
                turn_id=None, guest_name=None, message_id=None,
                type=ChahuaEventType.FILE_UPLOADED,
                data={"original": original, "error": text},
            )
        )

    async def _inbound_export_room(self, data: dict, sink: EnvelopeSink) -> None:
        # read-only：不动 session、不挡 inflight turn。
        self._export_room(sink)

    def _download_file(self, *, rel: str, sink: EnvelopeSink) -> None:
        """按 ``rel`` 把房间内文件读出 + base64 后回吐 ``FILE_DOWNLOAD``。

        - 仅接受 :data:`_DOWNLOAD_ALLOWED_PREFIXES` 前缀的 rel。绝对路径 / 包含 ``..``
          / 反斜杠都直接拒；解析后路径必须落在 ``room_dir`` 子树内（symlink 解析后再校
          验，防 share/ 内放符号链接指出房间）。
        - 超 :data:`_DOWNLOAD_MAX_BYTES` 拒（base64 后帧太大触发对端 ws 断线噩梦）。
        - **每次入站请求恒发一条** ``FILE_DOWNLOAD`` envelope（成功 / 失败），失败路径
          同时 emit NOTICE error 给用户看，与 upload 对仗。
        """
        original_rel = rel
        if not isinstance(rel, str) or not rel:
            self._fail_download(sink, rel=original_rel, text="rel 缺失或非字符串")
            return
        # 反斜杠 / 绝对路径 / 空段 / .. 全部拒。Windows 上 PurePosix 拆出来的段不会含 \\，
        # 显式过滤避免误把 "share\\evil" 当成 share/ 子文件。
        if "\\" in rel or rel.startswith("/"):
            self._fail_download(sink, rel=original_rel, text="rel 含非法字符或绝对路径")
            return
        if not rel.startswith(_DOWNLOAD_ALLOWED_PREFIXES):
            self._fail_download(
                sink, rel=original_rel,
                text=f"rel 不在白名单前缀内（仅允许 {'/'.join(_DOWNLOAD_ALLOWED_PREFIXES)}）",
            )
            return
        # 拆段做 traversal 检查 —— 任何空段 / "." / ".." 都拒。
        segments = rel.split("/")
        if any(seg in ("", ".", "..") for seg in segments):
            self._fail_download(sink, rel=original_rel, text="rel 含非法路径段")
            return
        # tasks/ 前缀必须严格落在 tasks/<id>/artifacts/<name>+ 形（≥4 段，segments[2] ==
        # "artifacts"）—— 防 tasks/state.json / tasks/<id>/task.json / decisions.jsonl /
        # events.jsonl 等内部元数据被 download_file 套出去。share/ 不做段数限制（多层子
        # 目录是合法用法，且无敏感元数据混在 share/ 下）。
        if segments[0] == "tasks" and (len(segments) < 4 or segments[2] != "artifacts"):
            self._fail_download(
                sink, rel=original_rel,
                text="rel 必须形如 tasks/<id>/artifacts/<name>",
            )
            return
        try:
            room_dir = self.server._session.room_config.room_dir
            target = (room_dir / rel).resolve()
            # resolve 后必须仍落在**具体白名单子目录**下，而不只是 room_dir 子树 ——
            # 否则 ``share/x -> ../tasks/state.json`` 形的 symlink 会绕过段形检查
            # （rel 看起来是 share/x，resolve 后是 tasks/state.json，仍在 room_dir 内）。
            # share/ → ``room_dir/share``；tasks/<id>/artifacts/<...> → ``room_dir/tasks/<id>/artifacts``。
            if segments[0] == "share":
                allowed_root = (room_dir / "share").resolve()
            else:
                allowed_root = (
                    room_dir / "tasks" / segments[1] / "artifacts"
                ).resolve()
            target.relative_to(allowed_root)
        except (OSError, ValueError) as e:
            self._fail_download(sink, rel=original_rel, text=f"路径解析失败：{e}")
            return
        if not target.is_file():
            self._fail_download(sink, rel=original_rel, text="文件不存在或不是普通文件")
            return
        try:
            size = target.stat().st_size
        except OSError as e:
            self._fail_download(sink, rel=original_rel, text=f"读盘失败：{e}")
            return
        if size > _DOWNLOAD_MAX_BYTES:
            self._fail_download(
                sink, rel=original_rel,
                text=f"文件太大（{size} bytes，上限 {_DOWNLOAD_MAX_BYTES}）",
            )
            return
        try:
            data = target.read_bytes()
        except OSError as e:
            self._fail_download(sink, rel=original_rel, text=f"读盘失败：{e}")
            return
        content_b64 = base64.b64encode(data).decode("ascii")
        _log.info("download_file: %s (%d bytes)", rel, size)
        sink(
            ChahuaEnvelope(
                room_id=self.server._session.room.name,
                turn_id=None, guest_name=None, message_id=None,
                type=ChahuaEventType.FILE_DOWNLOAD,
                data={
                    "rel": rel,
                    "name": target.name,
                    "size": size,
                    "content_b64": content_b64,
                },
            )
        )

    def _fail_download(self, sink: EnvelopeSink, *, rel: str, text: str) -> None:
        """NOTICE error + FILE_DOWNLOAD(error) —— 与 _fail_upload 同口径。"""
        self.server._emit_notice(sink, level=NOTICE_LEVEL_ERROR, text=text)
        sink(
            ChahuaEnvelope(
                room_id=self.server._session.room.name,
                turn_id=None, guest_name=None, message_id=None,
                type=ChahuaEventType.FILE_DOWNLOAD,
                data={"rel": rel if isinstance(rel, str) else "", "error": text},
            )
        )

    async def _inbound_download_file(self, data: dict, sink: EnvelopeSink) -> None:
        # 不动 session、不挡 inflight turn —— 纯读盘。
        raw_rel = data.get("rel")
        rel = raw_rel if isinstance(raw_rel, str) else ""
        self._download_file(rel=rel, sink=sink)
