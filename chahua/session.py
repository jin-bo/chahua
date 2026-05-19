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
from typing import Any, Optional

from agentao.llm import LLMClient
from dotenv import load_dotenv

from ._fs import link_dir_idempotent, unlink_managed_link
from ._paths import Paths
from .config import RoomConfig, RoomConfigError, load_room_config
from .cursor import GuestCursor
from .debug_recorder import TurnRecorder
from .guest import TeaGuest
from .llm_spec import LLMSpec, build_client
from .orchestrator import Orchestrator, OrchestratorConfig
from .persona_assets import discover_assets, persona_relative
from .room import Room
from .scoring import IntentScorer
from .summarizer import Summarizer, TaskSummaries
from .tasks_store import TasksStore
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

# 茶客 cwd 下的任务桌面软链名（P5.2.11 通道 2）。与 ``share/`` 对仗 —— 茶客 prompt
# 里写 ``./task/<file>`` 是固定口径，room.toml 不可改。active=None 时整链消失。
TASK_LINK_DIRNAME = "task"


def ensure_room_share_dir(room_dir: Path) -> Path:
    """房间共享目录 ``<room_dir>/share/``。茶客 ``work_dir/share`` 都链到这里，
    UI 上传的文件也落这里 —— 房间的"公共桌面"。

    单点 helper：server upload 入口与 session 装配各调一次，``mkdir(exist_ok=True)``
    幂等。
    """
    share = room_dir / ROOM_SHARE_DIRNAME
    share.mkdir(parents=True, exist_ok=True)
    return share


def relink_task_dirs(session: "RoomSession") -> None:
    """把每位茶客 cwd 下 ``./task/`` 刷到当前 active task 的 ``artifacts/``（通道 2）。

    ``active = None`` 仅解链；切到新任务先解再建。装配末尾调一次（初始 active）；
    server inbound 改 active 的三处（``open_task`` / ``set_active_task`` / ``close_task``）
    也各调一次 —— 集中在 server 层而不是 tasks_store 内部，因为 store 不该知道
    guest workspace 这层概念（docs §6.2 / §7.2 P5.2 通道 2）。

    Windows fallback junction（mklink /J）—— 普通用户即可建，agentao ``working_directory``
    cwd 边界对软链 / junction 一视同仁拦写。失败 WARN 不阻断，但茶客就看不到
    ``./task/`` 内容。
    """
    store = session.tasks_store
    active = store.active_task_id
    # artifacts/ 是 per-task 而非 per-guest —— hoist 出循环避免 N 次 stat。
    artifacts: Optional[Path] = None
    if active is not None:
        artifacts = store.artifacts_dir(active)
        artifacts.mkdir(parents=True, exist_ok=True)
    for guest in session.guests:
        link_path = guest.working_directory / TASK_LINK_DIRNAME
        label = f"guest {guest.name} task link"
        if artifacts is None:
            unlink_managed_link(link_path, label=label)
            continue
        link_dir_idempotent(
            link_path, artifacts,
            wipe_real_target=False,
            label=label,
            windows_junction_fallback=True,
        )


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
    guest_clients: dict[str, LLMClient],
    room: Room,
    paths: Paths,
    *,
    tasks_store: TasksStore,
    recorder: TurnRecorder,
) -> list[tuple[TeaGuest, str]]:
    """按 ``room.toml`` 里 ``[[guest]]`` 顺序构造茶客。

    每位茶客的 ``working_directory`` 走 :meth:`GuestConfig.workspace_in` ——
    ``isolation == "room"`` 时 ``<room_dir>/guests/<name>/``、``"global"`` 时
    ``<user_data_root>/guests/<name>/``。同时把 ``work_dir/share`` 软链到房间共享目录
    ``<room_dir>/share/`` —— 用户上传的文件落房间根，茶客在自己 cwd 下用 ``./share/xxx``
    就能 Read（agentao 工具受 working_directory 约束，share 必须挂在 cwd 子树）。
    isolation=global 茶客在不同房间装配时 ``link_dir_idempotent`` 会自动 retarget 这个
    share 软链到当前房间。

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
            llm_client=guest_clients[gc.name],
            working_directory=gc.workspace_in(
                paths=paths, room_dir=room_config.room_dir,
            ),
            room=room,
            tasks_store=tasks_store,
            permission=gc.permission,
            assets=assets,
            room_level_mcp=gc.extra_mcp_servers,
            recorder=recorder,
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
    """房间默认 LLM spec。P4.9 起优先取 ``[room.llm]`` toml 段，缺则 fall back 到 env
    推断（:meth:`LLMSpec.try_from_env`）。两者都缺 → ``build_room_session`` 抛
    :class:`RoomConfigError`。

    "单 provider 字符串"在 P4 多 client 后表达不了"每茶客可异构"的事实；这里持 spec
    是 banner / 启动日志 / room_info envelope 展示房间默认配置的唯一数据源。**不缓存
    :class:`LLMClient` 对象引用** —— 茶客实例本身已持 client（agentao 内部），
    ``RoomSession`` 只持声明性 spec。"""

    scoring_spec: LLMSpec
    """打分用的 effective LLM spec。fallback 链：``rc.scoring_llm`` → ``room_default_spec``。"""

    summary_spec: LLMSpec
    """摘要用的 effective LLM spec。fallback 链：``rc.summary_llm`` → :attr:`scoring_spec`
    （进而 → room_default）。"""

    guest_specs: dict[str, LLMSpec]
    """每位茶客的 effective LLM spec（``gc.llm`` 或 fallback 到 room_default）。
    keys 与 :attr:`guests` 的 ``name`` 一一对应；envelope 渲染时按这个出"哪位茶客用什么"。"""

    tasks_store: TasksStore
    """房间级任务存储。整个 session 生命周期复用一份。server inbound handler / orchestrator
    写 transcript 时取 ``active_task_id`` 都走这个单例 —— 别在外面再 ``TasksStore(room_dir=...)``
    实例化（会绕过内存镜像导致双 source 不一致）。"""

    task_summaries: TaskSummaries
    """per-task summarizer 池。``close()`` 一并 flush 所有 task cursor。业务流不取这个
    handle —— orchestrator 自己持引用做 ``_summarize_safe`` 的尾巴。"""

    def close(self) -> None:
        # cursor flush 是廉价同步 IO，先做完再走茶客 close（后者偶发卡 event loop）。
        self.task_summaries.close()
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

    def swap_room_config(self, new_rc: RoomConfig) -> None:
        """热替换 ``room_config`` + 同步 ``orchestrator.config`` —— 编排参数 mutator 用，
        避免推倒整个 session 重建。

        ``OrchestratorConfig`` 的每个字段都被 :class:`chahua.orchestrator.Orchestrator`
        在热循环里通过 ``self.config.X`` 读取，所以一次 attribute swap 就让阈值 / 上限
        / 冷却下次迭代当场生效，无需 cancel in-flight turn / 重建茶客 / 重新 onboarding。
        ``frozen=True`` 走 ``object.__setattr__`` bypass，与 :meth:`reload_user_config`
        同口径 —— 纯数据替换不破坏 in-flight 状态。

        **只适合编排参数 / 用户配置之类的"声明性"字段更新**；茶客增减 / persona 切换 /
        permission 改这些需要重建 ``TeaGuest`` 的，仍走 ``server._replace_session``。
        """
        object.__setattr__(self, "room_config", new_rc)
        self.orchestrator.set_config(
            _make_orchestrator_config(new_rc.orchestrator_overrides)
        )


def _resolve_room_default_spec(room_config: RoomConfig) -> LLMSpec:
    """房间默认 LLM 的两层 fallback：``[room.llm]`` toml 段 → env 推断。

    两者都缺 → :class:`RoomConfigError` 提示"二选一"。错误信息列两条路径让用户一眼
    知道修哪里，不让默认空缺时下游再炸 ``build_client``（那一炸只指向 OPENAI_MODEL
    缺失，没提"也可以在 room.toml 写 [room.llm]"这条路）。

    `[room.llm]` 显式配置时 env 完全被忽略 —— 同 toml 文件里写 ``temperature=0.5``，
    shell 里再 export ``LLM_TEMPERATURE=1.5`` 不应"偷偷胜"覆盖用户在 toml 的意图。
    """
    if room_config.room_llm is not None:
        return room_config.room_llm
    env_spec = LLMSpec.try_from_env()
    if env_spec is not None:
        return env_spec
    raise RoomConfigError(
        f"{room_config.room_dir / 'room.toml'}: 房间默认 LLM 未配置。\n"
        f"二选一：(1) 在 room.toml 里写 [room.llm]，至少含 model = "
        f"\"<provider>/<model>\"；(2) 设环境变量 LLM_PROVIDER + <PREFIX>_MODEL（如 "
        f"OPENAI_MODEL）。详见 .env.example。"
    )


def _make_orchestrator_config(
    overrides: dict[str, Any], *, explicit: Optional[OrchestratorConfig] = None,
) -> OrchestratorConfig:
    """显式 SDK 入参优先；其次 patch room.toml [room] 编排字段到默认上；都没有走默认。

    `build_room_session` 与 `RoomSession.swap_room_config` 共用 —— 单点保证装配期 vs
    热更新期 走同一 OrchestratorConfig 派生口径。
    """
    if explicit is not None:
        return explicit
    if overrides:
        return dataclasses.replace(OrchestratorConfig(), **overrides)
    return OrchestratorConfig()


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
    # P4.1：按 room.toml 装多套 client。fallback 链：
    #   - room_default: rc.room_llm or env（P4.9）
    #   - guest:    gc.llm or room_default
    #   - scoring:  rc.scoring_llm or room_default
    #   - summary:  rc.summary_llm or scoring（仍缺则 → room_default，由前一行链上来）
    # `build_client` 在缺 api_key 等时 SystemExit —— 让用户早炸而不是跑到第一次发言才出问题。
    room_default_spec = _resolve_room_default_spec(room_config)
    scoring_spec = room_config.scoring_llm or room_default_spec
    summary_spec = room_config.summary_llm or scoring_spec

    # 同一 spec 复用一份 client —— LLMSpec 是 frozen+slots dataclass，hashable。
    # 常见单 provider 场景下 5 茶客 + scoring + summary = 7 处用同一 spec，没 cache
    # 会装 7 份 LLMClient；agentao.LLMClient.__init__ 会 evict shared 'agentao' logger
    # 上一份 _agentao_llm_file_handler，导致先装的几位茶客日志被静默 detach。
    clients_by_spec: dict[LLMSpec, LLMClient] = {}

    def _client_for(spec: LLMSpec) -> LLMClient:
        cached = clients_by_spec.get(spec)
        if cached is None:
            cached = build_client(spec)
            clients_by_spec[spec] = cached
        return cached

    guest_specs: dict[str, LLMSpec] = {}
    guest_clients: dict[str, LLMClient] = {}
    for gc in room_config.guests:
        spec = gc.llm or room_default_spec
        guest_specs[gc.name] = spec
        guest_clients[gc.name] = _client_for(spec)

    scoring_client = _client_for(scoring_spec)
    summary_client = _client_for(summary_spec)

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

    tasks_store = TasksStore(room_dir=room_config.room_dir)
    task_summaries = TaskSummaries(summary_client, tasks_store=tasks_store)

    # P6.1：``[debug] enabled = false`` 时 ``TurnRecorder.__init__`` 早 return，
    # 不动盘；同 NOOP_RECORDER 风格但保留 toml 的 capture_prompts 字段（即便
    # enabled=False 时 ``capture_prompts`` 实际效果固定 False，不变量"recorder.
    # enabled=False ⇒ capture_prompts=False"）。
    recorder = TurnRecorder(
        room_dir=room_config.room_dir,
        enabled=room_config.debug.enabled,
        capture_prompts=room_config.debug.capture_prompts,
    )

    guest_entries = _build_guests(
        room_config, guest_clients, room, paths,
        tasks_store=tasks_store, recorder=recorder,
    )
    guests = [g for g, _ in guest_entries]

    effective_orch_config = _make_orchestrator_config(
        room_config.orchestrator_overrides, explicit=orchestrator_config
    )

    orchestrator = Orchestrator(
        room=room,
        user_config=user_config,
        scorer=IntentScorer(scoring_client),
        summarizer=Summarizer(summary_client, summary_path=summary_path),
        cursor=GuestCursor(cursor_path=cursor_path),
        config=effective_orch_config,
        tasks_store=tasks_store,
        task_summaries=task_summaries,
        recorder=recorder,
    )
    for guest, persona_md in guest_entries:
        orchestrator.register(guest, persona_md)

    session = RoomSession(
        room=room,
        orchestrator=orchestrator,
        guests=guests,
        user_config=user_config,
        room_config=room_config,
        room_default_spec=room_default_spec,
        scoring_spec=scoring_spec,
        summary_spec=summary_spec,
        guest_specs=guest_specs,
        tasks_store=tasks_store,
        task_summaries=task_summaries,
    )
    relink_task_dirs(session)
    return session
