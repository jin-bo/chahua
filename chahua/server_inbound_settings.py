"""USER.md / 头像 / room.toml 设置 handlers（P5.2 重构，docs §7.2）。

P5.2 重构：mixin 多继承换成组合。:class:`chahua.server.ChahuaServer` 在 ``__init__``
里 ``self.settings = SettingsHandlers(self)``；本类持 ``self.server`` 反向引用，跨 server
状态走 ``self.server.xxx``。
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

from . import admin
from ._server_helpers import require_str
from .config import RoomConfigError
from .events import EnvelopeSink, NOTICE_LEVEL_ERROR, NOTICE_LEVEL_INFO
from .llm_spec import (
    LLMSpec,
    build_client,
    canonical_provider,
    provider_env_prefix,
)

_log = logging.getLogger(__name__)


INBOUND_UPDATE_USER_MD = "update_user_md"
INBOUND_UPDATE_ROOM_TOML = "update_room_toml"
INBOUND_UPDATE_USER_AVATAR = "update_user_avatar"
# P15：desktop 登录态运行期注入 LLM 凭证。带 raw api_key —— 严格白名单、handler
# 禁 log raw payload，详见 docs/P15。
INBOUND_SET_LLM_CREDENTIALS = "set_llm_credentials"

_SET_LLM_CREDENTIALS_KEYS = frozenset({"provider", "model", "base_url", "api_key"})

# os.environ 快照 sentinel：区分「key 原本未设」与「key 旧值为空串」，回滚时前者 del、
# 后者写回空串。
_ENV_UNSET = object()


def _snapshot_env(keys: tuple[str, ...]) -> dict[str, object]:
    """快照若干 env key 的旧值，缺省记 :data:`_ENV_UNSET`。供失败回滚（全内存、无磁盘 IO）。"""
    return {k: os.environ.get(k, _ENV_UNSET) for k in keys}


def _redact_base_url(base_url: str) -> str:
    """base_url → 仅 scheme://host[:port] 供日志，剥 userinfo / path / query / fragment。

    P15 承重：base_url 可能藏凭证（``https://user:pass@host`` 的 userinfo、
    ``...?token=...`` 的 query），整 URL 进 ``sidecar.log`` 是泄漏面。日志只留 host 级。
    空 → ``"<default>"``；解析不出 host（畸形）→ ``"<redacted>"``，绝不回落到打原值。
    """
    if not base_url:
        return "<default>"
    try:
        parts = urlsplit(base_url)
        host = parts.hostname  # 已剥 userinfo
        if not host:
            return "<redacted>"
        netloc = f"{host}:{parts.port}" if parts.port else host
        scheme = parts.scheme or "?"
        return f"{scheme}://{netloc}"
    except Exception:
        return "<redacted>"


def _restore_env(snapshot: dict[str, object]) -> None:
    """按 :func:`_snapshot_env` 的快照逐个还原 env：原本未设 → ``del``，否则写回旧值。"""
    for k, v in snapshot.items():
        if v is _ENV_UNSET:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v  # type: ignore[assignment]


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

    async def _inbound_set_llm_credentials(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        """P15：desktop 登录态运行期注入 LLM 凭证 —— 只改 ``os.environ`` + 热重建，绝不写盘。

        流程：严格白名单 → 校验（``provider``/``model`` 走 ``require_str``；``api_key`` 走
        自写敏感校验；``base_url`` 可缺/空串）→ 快照 env 旧值 → 写 ``os.environ`` →
        ``_cancel_and_drain_all_foreground`` + ``_replace_session`` 热重建 → 成功重发
        room snapshot / 失败恢复 env 旧值（保留旧内存 session）。

        **承重**：raw ``api_key`` 只入进程内存 environ，绝不落盘 / 进 envelope / 进日志；
        handler 全程禁 log raw payload。详见 docs/P15。
        """
        where = INBOUND_SET_LLM_CREDENTIALS

        def _reject(text: str) -> None:
            self.server._emit_notice(sink, level=NOTICE_LEVEL_ERROR, text=text)

        # 严格白名单：未知键 → NOTICE error 丢帧（敏感帧不静默吞误发字段）。
        if not self.server._reject_unknown_keys(
            data, _SET_LLM_CREDENTIALS_KEYS, where=where, sink=sink
        ):
            return
        # provider / model 不敏感 —— 失败可走 require_str（它 %r 打值对它们无害）。
        provider = require_str(data, "provider", where=where)
        model = require_str(data, "model", where=where)
        if provider is None or model is None:
            _reject(f"{where}: provider / model 必填且非空")
            return
        # model 去空白后再用（与 base_url 同口径）——裸 model 末尾空格会原样进
        # <PREFIX>_MODEL / model_id，到首次 LLM 调用才报错。
        model = model.strip()
        if not model:
            _reject(f"{where}: model 必填且非空")
            return
        # api_key 敏感：redact=True 让 require_str 失败只 log 名 + 类型，绝不打值。
        api_key = require_str(data, "api_key", where=where, redact=True)
        if api_key is None:
            _reject(f"{where}: api_key 必填且非空")
            return
        # base_url 可缺 / 空串（走 _DEFAULT_BASE_URLS）；给则必须是 str。base_url 不敏感。
        base_url_raw = data.get("base_url")
        if base_url_raw is not None and not isinstance(base_url_raw, str):
            _log.warning(
                "ignoring %s: base_url 必须是 str / 缺省，收到 %r",
                where, type(base_url_raw),
            )
            _reject(f"{where}: base_url 必须是字符串或缺省")
            return
        base_url = (base_url_raw or "").strip()

        # provider 归一（google→gemini）后推 env 前缀，写 LLM_PROVIDER 让 try_from_env
        # 据它推回同一前缀 —— 漏写 LLM_PROVIDER 会前缀错配。provider token 必须干净：
        # 含空白 / `/` 会拼出畸形 env 前缀（如 `OPEN AI_MODEL`）与 model_id，且 env 路径
        # 不像 toml 走 split_model_id 校验 —— 本 inbound 自己挡，否则静默产坏配置。
        provider_canon = canonical_provider(provider)
        if (
            not provider_canon
            or any(c.isspace() for c in provider_canon)
            or "/" in provider_canon
        ):
            _reject(f"{where}: provider 不合法（不能含空白或 '/'）")
            return
        prefix = provider_env_prefix(provider_canon)

        # 显式 [room.llm] 完全遮蔽 env 注入 —— 写 env 不改本房装配。短路掉
        # cancel-drain + 热重建（否则白白杀掉在飞 turn / bg run / MTS），只写 env +
        # 刷 snapshot + info 提醒。仍写 env：供用户日后删 [room.llm] 时接管。
        shadowed = self.server._session.room_config.room_llm is not None

        # 改 env 前快照将动 key 的旧值（含「原本未设」sentinel），供失败 / 异常回滚。
        env_keys = (
            "LLM_PROVIDER",
            f"{prefix}_MODEL",
            f"{prefix}_BASE_URL",
            f"{prefix}_API_KEY",
        )
        snapshot = _snapshot_env(env_keys)

        applied = False
        try:
            try:
                os.environ["LLM_PROVIDER"] = provider_canon
                os.environ[f"{prefix}_MODEL"] = model
                os.environ[f"{prefix}_API_KEY"] = api_key
                if base_url:
                    os.environ[f"{prefix}_BASE_URL"] = base_url
                else:
                    # 空 / 缺 → 删残留，让 build_client 走 _DEFAULT_BASE_URLS。
                    os.environ.pop(f"{prefix}_BASE_URL", None)
            except ValueError as e:
                # env 名 / 值含非法字符 → os.environ 赋值抛 ValueError：provider 含 `=`
                # 拼出非法 env 名 `FOO=BAR_MODEL`；model / api_key / base_url 含 NUL →
                # 「embedded null byte」。转 NOTICE error 别让异常逃逸断 ws；finally 回滚
                # 已写入的部分。`e` 文案不含 value（NUL 错无值、名错只露 provider 派生
                # 前缀，非 api_key），可安全入日志。
                _log.warning("set_llm_credentials: env 赋值失败：%s", e)
                _reject(f"{where}: 凭证含非法字符（env 不可用），已拒绝")
                return  # finally 见 applied=False → 回滚 env

            # 只 log redacted —— 绝不整帧 / api_key 进日志；base_url 剥到 host 级
            # （可能藏 userinfo / ?token=... 凭证）。
            _log.info(
                "set_llm_credentials: provider=%s model=%s base_url=%s "
                "api_key=<redacted>",
                provider_canon, model, _redact_base_url(base_url),
            )

            if shadowed:
                # 本房显式 [room.llm] 优先：注入对本房装配是 no-op，短路掉全量重建
                # （否则白白杀在飞 turn / bg run / 强停 MTS）。**但 env 默认仍须现验**——
                # 否则坏凭证（未知 provider 缺 base_url / 缺 key）被悄悄接受，日后 fallback
                # 房 / 删本房 [room.llm] 重建即炸。用 try_from_env + build_client 校验一遍
                # （无网络、不动 session），失败走 finally 回滚 env，与非遮蔽路经由
                # _replace_session 校验同效。
                try:
                    spec = LLMSpec.try_from_env()
                    if spec is None:
                        raise SystemExit(f"缺 {prefix}_MODEL")
                    build_client(spec)
                except (Exception, SystemExit) as e:
                    _log.warning(
                        "set_llm_credentials: 遮蔽房 env 默认校验失败：%s", e
                    )
                    _reject(f"{where}: 凭证无法装配，已回滚：{e}")
                    return  # finally 见 applied=False → 回滚 env
                # 不动 session，只刷 snapshot（env 默认展示更新）+ info 提醒「本房未受
                # 影响」。env 写保留供日后删段接管。
                applied = True
                self.server._emit_room_snapshot(sink)
                self.server._emit_notice(
                    sink, level=NOTICE_LEVEL_INFO,
                    text=(
                        "本房显式 [room.llm] 配置优先，desktop 注入只更新了 env 默认，"
                        "本房未受影响。"
                    ),
                )
                return

            await self.server._cancel_and_drain_all_foreground()
            room_dir = self.server._session.room_config.room_dir
            # _replace_session 返 False（装配失败）已 emit NOTICE error；env 由 finally
            # 回滚。它若**抛**异常（build 之后的 setter / MTS-end 路径），同样回滚。
            if not self.server._replace_session(room_dir, sink, label=where):
                return
            applied = True
            # 成功：重发 room snapshot —— room_info.room_default_llm 从 os.environ 实时
            # 探测 model / api_key_ready，同时刷新 scoring / summary / 茶客 fallback。
            self.server._emit_room_snapshot(sink)
        finally:
            if not applied:
                # 装配失败 / 中途异常：恢复 env 旧值（无磁盘 IO），让进程态与存活的旧
                # 内存 session 一致。
                _restore_env(snapshot)

    async def _inbound_update_user_avatar(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        data_uri = require_str(data, "data_uri", where=INBOUND_UPDATE_USER_AVATAR)
        if data_uri is None:
            return
        # 头像写不动 session，无需 cancel inflight —— 让正在跑的 turn 自然结束。
        self._update_user_avatar(data_uri=data_uri, sink=sink)
