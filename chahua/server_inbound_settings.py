"""USER.md / 头像 / room.toml 设置 handlers（P5.2 重构，docs §7.2）。

P5.2 重构：mixin 多继承换成组合。:class:`chahua.server.ChahuaServer` 在 ``__init__``
里 ``self.settings = SettingsHandlers(self)``；本类持 ``self.server`` 反向引用，跨 server
状态走 ``self.server.xxx``。
"""

from __future__ import annotations

import logging

from . import admin
from ._server_helpers import require_str
from .config import RoomConfigError
from .events import EnvelopeSink, NOTICE_LEVEL_ERROR

_log = logging.getLogger(__name__)


INBOUND_UPDATE_USER_MD = "update_user_md"
INBOUND_UPDATE_ROOM_TOML = "update_room_toml"
INBOUND_UPDATE_USER_AVATAR = "update_user_avatar"


class SettingsHandlers:
    """USER.md / 头像 / room.toml 三种设置写操作。"""

    def __init__(self, server: "ChahuaServer") -> None:  # type: ignore[name-defined]
        self.server = server

    def _update_user_md(self, *, content: str, sink: EnvelopeSink) -> None:
        """覆盖 USER.md + 原地 reload user_config（不重装整个 session）+ 重发 snapshot。

        优先沿用 user_config.source（若用户用了 room 级 USER.md 或 explicit override，
        编辑就改那个；不偷偷新建 user_data_root/USER.md 让两份并存）。

        在 ``reload_user_config`` 之前不需要 cancel inflight —— ``_handle_inbound`` 已经
        cancel 过了，且 user_config 是纯数据 swap，没有"半个茶客"的中间态。
        """
        try:
            admin.update_user_md(
                self.server._paths,
                content,
                source=self.server._session.user_config.source,
            )
        except Exception:
            _log.exception("update_user_md 失败")
            self.server._emit_room_snapshot(sink)
            return
        self.server._session.reload_user_config(self.server._paths)
        _log.info("update_user_md: %d 字节已落盘", len(content))
        self.server._emit_room_snapshot(sink)

    def _update_room_toml(self, *, content: str, sink: EnvelopeSink) -> None:
        """覆盖当前房间 room.toml 全文 + 重装 session + 重发 snapshot。

        校验失败（语法 / 白名单 / persona 找不到）→ emit error notice + 重发当前 snapshot
        让前端 UI 复位；admin.update_room_toml 已经把磁盘内容回滚到旧 toml。
        """
        room_dir = self.server._session.room_config.room_dir
        try:
            admin.update_room_toml(room_dir, content, paths=self.server._paths)
        except (ValueError, RoomConfigError) as e:
            _log.warning("update_room_toml: room=%r 校验失败：%s", room_dir.name, e)
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text=f"房间配置保存失败：{e}"
            )
            self.server._emit_room_snapshot(sink)
            return
        except Exception:
            _log.exception("update_room_toml: room=%r 失败", room_dir.name)
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text="房间配置保存失败（详见服务端日志）"
            )
            self.server._emit_room_snapshot(sink)
            return
        if not self.server._replace_session(room_dir, sink, label="update_room_toml"):
            return
        _log.info("update_room_toml: room=%r 已落盘", room_dir.name)
        self.server._emit_room_snapshot(sink)

    def _update_user_avatar(self, *, data_uri: str, sink: EnvelopeSink) -> None:
        """覆盖 USER.png；不重装 session（avatar 不是 UserConfig 字段，靠 sidebar 重发即可）。

        admin 层 cache_clear 已让下次 read_avatar_data_uri 拿到新文件；这里只发 room_info
        让前端拿到新 user_avatar_data_uri，transcript 不动 —— 用 _emit_room_info 而非全
        snapshot，省一次历史回放。
        """
        try:
            png_bytes = admin.parse_png_data_uri(data_uri)
            admin.update_user_avatar(
                self.server._paths,
                png_bytes,
                source=self.server._session.user_config.source,
            )
        except Exception:
            _log.exception("update_user_avatar 失败")
            self.server._emit_room_info(sink)
            return
        _log.info("update_user_avatar: %d 字节已落盘", len(png_bytes))
        self.server._emit_room_info(sink)

    async def _inbound_update_user_md(self, data: dict, sink: EnvelopeSink) -> None:
        # content 允许空串：用户清空 USER.md 也算合法状态。
        content = require_str(
            data, "content", where=INBOUND_UPDATE_USER_MD, allow_empty=True
        )
        if content is None:
            return
        await self.server._cancel_and_drain_all_foreground()
        self._update_user_md(content=content, sink=sink)

    async def _inbound_update_room_toml(self, data: dict, sink: EnvelopeSink) -> None:
        # room.toml 内容理论上不该为空，但 admin 层会用 RoomConfigError 拦住，校验
        # 责任不在这层；allow_empty 让传 "" 也走到 admin 拿到结构化错误。
        content = require_str(
            data, "content", where=INBOUND_UPDATE_ROOM_TOML, allow_empty=True
        )
        if content is None:
            return
        await self.server._cancel_and_drain_all_foreground()
        self._update_room_toml(content=content, sink=sink)

    async def _inbound_update_user_avatar(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        data_uri = require_str(data, "data_uri", where=INBOUND_UPDATE_USER_AVATAR)
        if data_uri is None:
            return
        # 头像写不动 session，无需 cancel inflight —— 让正在跑的 turn 自然结束。
        self._update_user_avatar(data_uri=data_uri, sink=sink)
