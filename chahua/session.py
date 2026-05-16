"""房间会话装配（P2.3 抽出）。

P0~P2.2 期把 room.toml + USER.md + 凭据 → :class:`Room` + :class:`Orchestrator`
+ 三茶客 的装配段写在 ``cli._repl`` 里。P2.3 收成 :func:`build_room_session`，
CLI 与 :mod:`chahua.server` 共用同一口径；同时给 SDK-style 调用（未来 P4 / 第三方
嵌入）留了不经 argparse / 不经 ``input`` 的入口。
"""

from __future__ import annotations

import dataclasses
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agentao.llm import LLMClient
from dotenv import load_dotenv

from ._fs import link_dir_idempotent
from ._paths import Paths
from .config import RoomConfig, load_room_config
from .cursor import GuestCursor
from .guest import TeaGuest
from .llm_spec import LLMSpec, build_client
from .orchestrator import Orchestrator, OrchestratorConfig
from .persona_assets import discover_assets, persona_relative
from .room import Room
from .scoring import IntentScorer
from .summarizer import Summarizer
from .trust import is_mcp_trusted
from .user_md import USER_SPEAKER_ID, UserConfig, load_user_md

_log = logging.getLogger(__name__)


# 默认房间目录（相对 :attr:`Paths.user_data_root`）。CLI 与 server 共用，避免两边各自硬编。
DEFAULT_ROOM_REL: Path = Path("rooms/p1-test")


def load_env_files(paths: Paths) -> None:
    """凭码查找顺序（高 → 低）：shell export > ``user_data_root/.env`` > ``~/.env``。

    ``load_dotenv`` 默认 ``override=False``，shell 的值不会被覆盖。打包路径上
    ``user_data_root`` = ``~/Library/Application Support/chahua/``，与 dev 仓库根的
    ``.env`` 自动解耦。
    """
    load_dotenv(paths.user_data_root / ".env")
    load_dotenv(Path.home() / ".env")


# 房间共享子目录名。room.toml 不可改 —— 所有茶客 cwd 下都靠这个软链名访问。
ROOM_SHARE_DIRNAME = "share"


def ensure_room_share_dir(room_dir: Path) -> Path:
    """房间共享目录 ``<room_dir>/share/``。茶客 ``work_dir/share`` 都链到这里，
    UI 上传的文件也落这里 —— 房间的"公共桌面"。

    单点 helper：server upload 入口与 session 装配各调一次，``mkdir(exist_ok=True)``
    幂等。
    """
    share = room_dir / ROOM_SHARE_DIRNAME
    share.mkdir(parents=True, exist_ok=True)
    return share


def _link_guest_share(guest_workdir: Path, room_share: Path) -> None:
    """``<guest_workdir>/share`` → ``room_share`` 软链。

    Windows 普通用户没 ``SeCreateSymbolicLinkPrivilege`` 时静默 WARN —— 茶客看不到
    房间共享文件。**不** copytree 兜底：share/ 必须双向实时同步，"快照"反而误导。
    """
    link_dir_idempotent(
        guest_workdir / ROOM_SHARE_DIRNAME,
        room_share,
        wipe_real_target=False,
        label=f"guest {guest_workdir.name} share",
    )


def _build_guests(
    room_config: RoomConfig,
    llm_client: LLMClient,
    room: Room,
    paths: Paths,
) -> list[tuple[TeaGuest, str]]:
    """按 ``room.toml`` 里 ``[[guest]]`` 顺序构造茶客。

    每位茶客的 ``working_directory`` 走 :meth:`GuestConfig.workspace_in`（约定
    ``<room_dir>/guests/<name>/``）。同时把 ``work_dir/share`` 链到房间共享目录
    ``<room_dir>/share/`` —— 用户上传的文件落房间根，茶客在自己 cwd 下用 ``./share/xxx``
    就能 Read（agentao 工具受 working_directory 约束，share 必须挂在 cwd 子树）。

    persona sibling 的 ``mcp.json`` / ``skills/`` 走两套不同的信任策略：

    - **skills**：只要 ``<persona_dir>/skills/`` 存在就装载（SKILL.md 是 prompt，
      不直接执行任何东西；read-only 茶客读了也只能"建议"，跑不出真破坏）。无 sibling
      时不传 SkillManager，Agentao 自己起默认实例。
    - **mcp**：默认不装载。仅当用户在 UI 里勾过"信任此 persona 的 MCP"
      （:func:`chahua.trust.is_mcp_trusted` 返回 True）才把 ``mcpServers`` 喂给
      Agentao —— mcp.json 里的 ``command`` + ``args`` 是任意可执行，未经用户判断
      不该自动启动。
    """
    room_share = ensure_room_share_dir(room_config.room_dir)
    out: list[tuple[TeaGuest, str]] = []
    for gc in room_config.guests:
        persona_md = gc.read_persona()
        assets = discover_assets(gc.persona_path)
        if assets.has_mcp:
            persona_rel = persona_relative(gc.persona_path, paths)
            if not is_mcp_trusted(paths, persona_rel):
                _log.info(
                    "guest %s: persona %s 带 mcp.json 但未受信任，跳过 MCP 装载",
                    gc.name, persona_rel,
                )
                # PersonaAssets frozen → dataclasses.replace 出一份 mcp_servers=None
                # 的副本喂 TeaGuest；skills_dir / skills_available 不动（skills 不进信任门控）。
                assets = dataclasses.replace(assets, mcp_servers=None)
        guest = TeaGuest(
            name=gc.name,
            persona_md=persona_md,
            llm_client=llm_client,
            working_directory=gc.workspace_in(room_config.room_dir),
            room=room,
            permission=gc.permission,
            assets=assets,
        )
        # TeaGuest.__init__ 已经 mkdir 了 working_directory，share 软链放这里安全。
        _link_guest_share(guest.working_directory, room_share)
        out.append((guest, persona_md))
    return out


@dataclass(frozen=True, slots=True)
class RoomSession:
    """一次房间装配的全部句柄。

    ``frozen=True`` 防止调用方重绑 ``orchestrator`` / ``room`` 等核心句柄（茶客生命周期
    都挂在它们上面，重绑会让 LLM client / transcript 文件指针错位）。装配后调 :meth:`close`
    释放每位茶客；CLI 走 ``try / finally``，server 在服务退出前调一次即可。
    """

    room: Room
    orchestrator: Orchestrator
    guests: list[TeaGuest]
    user_config: UserConfig
    room_config: RoomConfig
    room_default_spec: LLMSpec
    """房间默认 LLM spec（env 推断的 :meth:`LLMSpec.from_env`）。

    "单 provider 字符串"在 P4 多 client 后表达不了"每茶客可异构"的事实；这里持 spec
    是 banner / 启动日志 / 未来 envelope 展示房间默认配置的唯一数据源。**不缓存
    :class:`LLMClient` 对象引用** —— 茶客实例本身已持 client（agentao 内部），
    ``RoomSession`` 只持声明性 spec。"""

    def close(self) -> None:
        for guest in self.guests:
            try:
                guest.close()
            except Exception:
                # 一位关失败不该阻止其他茶客清理 —— 但要留痕。
                _log.exception("guest %s close failed", guest.name)

    def reload_user_config(self, paths: Paths) -> None:
        """重 load USER.md，原地替换 session + orchestrator 上的 user_config。

        ``RoomSession`` 整体 ``frozen=True`` 是为了挡 orchestrator / room / guests 这些
        持 in-flight 状态的句柄被外部 rebind；``user_config`` 是纯数据（display_name /
        preferences_block / full_md），换它不会让 LLM client / transcript 文件指针错位。
        所以这里走 ``object.__setattr__`` 受控 bypass，比"为了改一字段重建 5 个 Agentao
        实例 + 重 load 整本 transcript"省得多。

        调用者：``server._update_user_md`` —— 用户在 UI 编辑了 USER.md 之后。
        """
        new_uc = load_user_md(
            user_data_root=paths.user_data_root,
            room_dir=self.room_config.room_dir,
            explicit=self.room_config.user_md_override,
        )
        object.__setattr__(self, "user_config", new_uc)
        self.orchestrator.user_config = new_uc
        # _display_map 缓存了 USER_SPEAKER_ID → display_name；改名要清缓存才生效。
        self.orchestrator._display_for = None


def discover_rooms(paths: Paths) -> list[dict]:
    """扫 ``user_data_root/rooms/*/room.toml``，返回 ``[{room_id, name, topic}, ...]``，
    按 id 升序。

    ``room_id`` = 房间目录名（P4 加 ``[room].id`` 字段前的稳定 ID 占位 —— 同
    :mod:`chahua.events` 里 envelope ``room_id`` 也用 ``room.name`` 占位）。
    ``name`` / ``topic`` 取自 toml ``[room]`` 段；解析失败的房间跳过 + WARN，
    一个坏 toml 不该让整个房间列表崩。

    给 P3.2.x 切换房间 wire 用，server 端 ``_emit_room_info`` 调一次塞进
    ``rooms_available`` 字段。房间永远在 ``user_data_root`` 下 —— 打包后 app bundle
    不带 rooms（首启动会从 templates/ 拷一份默认房进 user_data）。
    """
    rooms_dir = paths.user_data_root / "rooms"
    if not rooms_dir.is_dir():
        return []
    results: list[dict] = []
    for entry in sorted(rooms_dir.iterdir()):
        toml_path = entry / "room.toml"
        if not toml_path.is_file():
            continue
        try:
            with toml_path.open("rb") as f:
                data = tomllib.load(f)
        except Exception:
            _log.warning("discover_rooms: skip %s (toml parse failed)", toml_path)
            continue
        room_section = data.get("room", {}) if isinstance(data.get("room"), dict) else {}
        results.append({
            "room_id": entry.name,
            "name": str(room_section.get("name") or entry.name),
            "topic": str(room_section.get("topic", "")),
        })
    return results


def build_room_session(
    room_dir: Path,
    paths: Paths,
    *,
    orchestrator_config: Optional[OrchestratorConfig] = None,
) -> RoomSession:
    """读 ``room_dir/room.toml`` 装配整套房间会话。LLM 凭据走环境变量。

    抛 :class:`chahua.config.RoomConfigError` 当 toml 缺/错；抛 SystemExit 当 LLM
    凭据缺失（与 P0~P2.2 行为一致 —— 缺凭据没法跑，早炸早好）。
    """
    room_config = load_room_config(room_dir, paths=paths)
    user_config = load_user_md(
        user_data_root=paths.user_data_root,
        room_dir=room_config.room_dir,
        explicit=room_config.user_md_override,
    )
    # P4.1 起这里会按 room.toml 的 [[guest]] / [scoring] / [summary] 装多套 client；
    # 现在 spec 单一，三处（茶客 / 打分 / 摘要）复用同一 client。
    room_default_spec = LLMSpec.from_env()
    llm_client = build_client(room_default_spec)

    # 持久化文件全部塞在 room_dir 下 —— 删房间一并清掉（设计文档 §3.7）。
    transcript_path = room_config.room_dir / "transcript.jsonl"
    summary_path = room_config.room_dir / "summary.jsonl"
    cursor_path = room_config.room_dir / "cursor.json"

    room = Room(
        name=room_config.name,
        topic=room_config.topic,
        rules=room_config.rules,
        transcript_path=transcript_path,
    )
    room.add_participant(USER_SPEAKER_ID)

    guest_entries = _build_guests(room_config, llm_client, room, paths)
    guests = [g for g, _ in guest_entries]

    orchestrator = Orchestrator(
        room=room,
        user_config=user_config,
        scorer=IntentScorer(llm_client),
        summarizer=Summarizer(llm_client, summary_path=summary_path),
        cursor=GuestCursor(cursor_path=cursor_path),
        config=orchestrator_config or OrchestratorConfig(),
    )
    for guest, persona_md in guest_entries:
        orchestrator.register(guest, persona_md)

    return RoomSession(
        room=room,
        orchestrator=orchestrator,
        guests=guests,
        user_config=user_config,
        room_config=room_config,
        room_default_spec=room_default_spec,
    )
