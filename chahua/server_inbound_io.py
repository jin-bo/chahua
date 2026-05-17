"""房间 IO inbound mixin —— upload / export / persona import（P5.2 重构，docs §7.2）。

依赖核心 ``ChahuaServer`` 提供：``self._session`` / ``self._paths`` / ``self._emit_notice``
/ ``self._emit_room_info``。
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


# 单文件上限。WS 入站帧 max=300MB（_WS_MAX_INBOUND_BYTES），base64 4/3 膨胀 → 原始
# 文件极限 ~225MB。设 200MB 让 JSON quoting + 字段开销有头。改大要同步抬 ws max_size。
_UPLOAD_MAX_BYTES = 200 * 1024 * 1024


def _import_success_text(result: "persona_import.ImportedPersona") -> str:
    """导入成功的 notice 文案 —— 含 persona 名 + 头像状态 + sidecar 文件数提示。"""
    parts = [f"已导入 persona「{result.name}」"]
    parts.append("（含头像）" if result.has_avatar else "（无头像）")
    if result.extras:
        parts.append(
            f"另有 {len(result.extras)} 个 sidecar 文件被保留（mcp.json / skills 等，目前运行时尚未消费）"
        )
    return "".join(parts)


class IOHandlersMixin:
    """upload / export / persona import inbound 集合。"""

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
            share_dir = ensure_room_share_dir(self._session.room_config.room_dir)
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
                room_id=self._session.room.name,
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
            self._emit_notice(sink, level=NOTICE_LEVEL_ERROR, text=str(e))
            self._emit_room_info(sink)
            return
        except Exception as e:
            _log.exception("%s 意外错", label)
            self._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text=f"导入失败（内部错误）：{e}"
            )
            self._emit_room_info(sink)
            return
        _log.info("%s → %s", label, result.persona_rel)
        self._emit_notice(
            sink, level=NOTICE_LEVEL_INFO, text=_import_success_text(result)
        )
        self._emit_room_info(sink)

    def _export_room(self, sink: EnvelopeSink) -> None:
        # read-only：不动 session、不写盘。导出物只活在用户的 Downloads/ 里（renderer
        # 端走 Blob + <a download>），房间目录 transcript.jsonl / summary.jsonl 不动。
        msgs = self._session.room.messages_since(0)
        filename, content = exporter.format_room_markdown(
            self._session.room_config,
            msgs,
            self._session.user_config.display_name,
        )
        sink(
            ChahuaEnvelope(
                room_id=self._session.room.name,
                turn_id=None,
                guest_name=None,
                message_id=None,
                type=ChahuaEventType.ROOM_EXPORT,
                data={"filename": filename, "markdown": content},
            )
        )
        _log.info(
            "export_room: room=%r %d msg → %s (%d bytes)",
            self._session.room.name, len(msgs), filename, len(content),
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
            lambda: persona_import.import_from_folder(self._paths, Path(src)),
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
            lambda: persona_import.import_from_github(self._paths, url),
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
        self._emit_notice(sink, level=NOTICE_LEVEL_ERROR, text=text)
        sink(
            ChahuaEnvelope(
                room_id=self._session.room.name,
                turn_id=None, guest_name=None, message_id=None,
                type=ChahuaEventType.FILE_UPLOADED,
                data={"original": original, "error": text},
            )
        )

    async def _inbound_export_room(self, data: dict, sink: EnvelopeSink) -> None:
        # read-only：不动 session、不挡 inflight turn。
        self._export_room(sink)
