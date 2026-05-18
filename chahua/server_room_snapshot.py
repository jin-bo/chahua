"""``room_info`` / ``room_history`` / ``room_snapshot`` envelope 拼装与下发。

``ChahuaServer`` 保留 1-line wrapper 维持现有调用方接口（``server_inbound_*``
handler / 测试通过 ``srv._emit_room_*`` 访问）；helper（``_llm_summary`` 等）也
经 ``chahua.server`` re-export 给测试用。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal, Optional, TYPE_CHECKING

from . import admin, trust
from .config import ORCH_FIELD_BOUNDS
from .events import (
    ChahuaEnvelope,
    ChahuaEventType,
    EnvelopeSink,
    NOTICE_LEVEL_INFO,
)
from .orchestrator_config import OrchestratorConfig
from .persona_assets import discover_assets, persona_relative
from .session import discover_rooms

if TYPE_CHECKING:
    from .server import ChahuaServer

_log = logging.getLogger(__name__)


# `_llm_summary` 的 ``source`` 字段：guest 段是 ``guest``/``room_default``，房间段是
# ``room``/``default``。``room`` = toml 里有写 / ``default`` = 走上游 fallback（room 默认段
# 自身的 default 表示 env 推断，scoring/summary 的 default 表示落到 room_default）。
LlmSource = Literal["guest", "room_default", "room", "default"]


def _read_room_toml(room_dir: Path) -> str:
    """读 ``room_dir/room.toml`` 原文。读盘失败 → 空串（room_info 不会因为这个炸掉）。"""
    toml_path = room_dir / "room.toml"
    try:
        return toml_path.read_text("utf-8")
    except OSError:
        _log.exception("read_room_toml: %s 读盘失败", toml_path)
        return ""


def _mcp_summary_list(
    servers: Optional[dict[str, dict]],
) -> list[dict]:
    """``{name -> cfg}`` → 前端 popover 展示用 list。

    只挑 ``name`` / ``command`` / ``args`` —— ``env`` 含可能敏感的 token，**绝不下发**。
    """
    if not servers:
        return []
    return [
        {
            "name": name,
            "command": str(cfg.get("command", "")),
            "args": [str(a) for a in (cfg.get("args") or [])],
        }
        for name, cfg in servers.items()
    ]


def _llm_summary(*, spec, source: LlmSource) -> dict:
    """``LLMSpec`` → 前端 envelope 字典。

    **绝不下发 ``api_key`` 本身**：envelope 一旦塞 key，前端 devtools / log dump 都能
    看到，跨用户 user_data 共享更糟（设计 §2.2）。``api_key_ready`` 是 server 端探测
    出的 bool —— ``ollama`` 本地不强鉴权所以永远 ready。

    ``temperature`` 走 ``spec.temperature``（原始值，None 表示本段没显式写 → UI 看到
    "继承默认"语义；custom 模式才填具体值）。
    """
    api_key_env = spec.default_api_key_env()
    api_key_ready = spec.provider == "ollama" or bool(os.environ.get(api_key_env))
    return {
        "model": spec.model_id,
        "base_url": spec.base_url,
        "api_key_env": api_key_env,
        "api_key_ready": api_key_ready,
        "temperature": spec.temperature,
        "source": source,
    }


def _orchestrator_effective_dict(config: OrchestratorConfig) -> dict[str, object]:
    """从当前 :class:`OrchestratorConfig` 实例摘出公开给前端的编排字段。

    键集派生自 :data:`chahua.config.ORCH_FIELD_BOUNDS` —— 与 config 解析认得的字段
    一一对应。其余 ``OrchestratorConfig`` 字段（``threshold_decay_per_turn`` /
    ``onboarding_recent_messages`` / ``summary_block_size`` 等）是内部调参，不进 toml
    schema 也不下发 —— 减少 envelope 噪音，避免用户以为它们也可改。
    """
    return {k: getattr(config, k) for k in ORCH_FIELD_BOUNDS}


# ── envelope 装配 / 下发 ─────────────────────────────────────────────────


def emit_room_info(server: "ChahuaServer", sink: EnvelopeSink) -> None:
    """ws 连上即下发一次 ``room_info`` —— 前端拿来装 sidebar / @ 补全候选。

    - **绝不下发 ``api_key`` 本身**：只出 ``api_key_env`` 名 + ``api_key_ready`` bool
      （设计 §2.2，避免前端 devtools / log dump 泄露）。
    - **persona vs 房间级 MCP 拆两块**：trust 门控不对称 —— persona sidecar 走 trust
      清单（持续生效），``[[guest.extra_mcp_servers]]`` 是用户在自己 toml 里手写自动
      信任。前端按各自语义渲染。``effective_mcp_names`` 是合并后实际装载（房间级覆盖
      persona 同名），纯展示。
    """
    rc = server._session.room_config
    # 一次性加载 trust 清单 —— 后面每 guest 走 ``in`` 查，避免 N 次 disk read。
    trusted_personas = trust.list_trusted(server._paths)
    guests: list[dict] = []
    for gc in rc.guests:
        assets = discover_assets(gc.persona_path)
        persona_rel = persona_relative(gc.persona_path, server._paths)
        persona_mcp_trusted = bool(
            assets.has_mcp and persona_rel in trusted_personas
        )
        persona_mcp_servers = _mcp_summary_list(assets.mcp_servers)
        room_mcp_servers = _mcp_summary_list(gc.extra_mcp_servers)
        # effective 装载顺序：persona (受信任时) + 房间级覆盖同名 —— 与
        # chahua.guest._merged_mcp_configs 的合并语义一致。
        effective: dict[str, None] = {}
        if persona_mcp_trusted:
            for s in persona_mcp_servers:
                effective[s["name"]] = None
        for s in room_mcp_servers:
            effective[s["name"]] = None
        workspace_path = gc.workspace_in(
            paths=server._paths, room_dir=rc.room_dir,
        )
        guests.append({
            "name": gc.name,
            "permission": gc.permission,
            "isolation": gc.isolation,
            "workspace_path": str(workspace_path),
            "avatar_data_uri": gc.read_avatar_data_uri(),
            "persona_rel": persona_rel,
            "persona_mcp_trusted": persona_mcp_trusted,
            "persona_mcp_servers": persona_mcp_servers,
            "room_mcp_servers": room_mcp_servers,
            "effective_mcp_names": list(effective),
            "llm": _llm_summary(
                spec=server._session.guest_specs[gc.name],
                source="guest" if gc.llm is not None else "room_default",
            ),
            "skills_available": list(assets.skills_available),
        })
    sink(
        ChahuaEnvelope(
            room_id=server._session.room.name,
            turn_id=None,
            guest_name=None,
            message_id=None,
            type=ChahuaEventType.ROOM_INFO,
            data={
                "room_name": rc.name,
                "topic": rc.topic,
                "guests": guests,
                "user_display_name": server._session.user_config.display_name,
                "user_avatar_data_uri": server._session.user_config.read_avatar_data_uri(),
                # 完整 USER.md 原文 + source 路径 —— 前端"编辑配置"modal 拿来 prefill
                # textarea。无 USER.md（user_config.full_md=None）→ 空串 + source=null，
                # 编辑器从空白起步，保存时 server 落到 user_data_root/USER.md。
                "user_md_content": server._session.user_config.full_md or "",
                "user_md_source": (
                    str(server._session.user_config.source)
                    if server._session.user_config.source is not None
                    else None
                ),
                # 房间 room.toml 原文 + 路径 —— 前端「更改房间配置」modal prefill。
                # 读盘失败（理论上不会，session 已经成功 load 过一次）兜底空串。
                "room_toml_content": _read_room_toml(rc.room_dir),
                "room_toml_source": str(rc.room_dir / "room.toml"),
                # 编排参数：effective = 当前 Orchestrator 实际跑的值（默认 + 用户 override 合并后），
                # overrides_keys = room.toml [room] 段里用户实际写过的键。前端按 keys 算出
                # "哪些是默认 / 哪些是用户改过的"，无需自己重算默认值。
                "orchestrator": _orchestrator_effective_dict(
                    server._session.orchestrator.config
                ),
                "orchestrator_overrides_keys": sorted(rc.orchestrator_overrides),
                # 房间级 LLM section：与 guests[].llm 同构。
                # ``source`` 是 "room" 表示 [scoring]/[summary] 段在 toml 里有写；
                # "default" 表示该段缺失走的是房间默认。
                # 房间默认 LLM —— "继承房间默认" UI 标签的字面来源（P4.5 modal）。
                # P4.9 起 source = "room" 表示 [room.llm] 在 toml 里有写；"default"
                # 表示走 env 推断（LLMSpec.try_from_env）。UI 据此区分"用户在房间
                # 里配的默认" vs "全局 env 默认"。
                "room_default_llm": _llm_summary(
                    spec=server._session.room_default_spec,
                    source="room" if rc.room_llm is not None else "default",
                ),
                "scoring_llm": _llm_summary(
                    spec=server._session.scoring_spec,
                    source="room" if rc.scoring_llm is not None else "default",
                ),
                "summary_llm": _llm_summary(
                    spec=server._session.summary_spec,
                    source="room" if rc.summary_llm is not None else "default",
                ),
                "current_room_id": rc.room_dir.name,
                "rooms_available": discover_rooms(server._paths),
                # 已在场茶客的 name 也在 personas_available 里 —— 前端按
                # guests[].name 去重显示，避免 picker 列出重复人选。
                "personas_available": admin.discover_personas(server._paths),
            },
        )
    )


def emit_room_history(server: "ChahuaServer", sink: EnvelopeSink) -> None:
    """ws 连上 / room_info 之后立刻下发 transcript.jsonl 历史。

    一帧把 ``Room._messages`` 全部塞下去（``Message.to_jsonl_dict()`` 同款字段：
    ``seq / speaker_id / text / ts_ms / message_id``）。前端按 ``speaker_id``：
    ``"user"`` → 用户气泡（取 ``user_avatar_data_uri``）；其余 → 茶客气泡，名字
    即 ``speaker_id``、头像走 guests 名册查找（不在册的茶客退化成无头像，正常）。

    所有历史一次性下发的取舍：实现简单；目前 transcript 体量（数百条）单帧没压力；
    将来 ws 默认 max_size（~1MB）不够时再改成分页 / 后向滚动懒拉。
    """
    msgs = server._session.room.messages_since(0)
    sink(
        ChahuaEnvelope(
            room_id=server._session.room.name,
            turn_id=None,
            guest_name=None,
            message_id=None,
            type=ChahuaEventType.ROOM_HISTORY,
            data={"messages": [m.to_jsonl_dict() for m in msgs]},
        )
    )


def emit_room_snapshot(server: "ChahuaServer", sink: EnvelopeSink) -> None:
    """下发 room_info + room_history + task_info —— 前端拿来全量复位 sidebar + 消息区
    + 任务面板。

    三处调用：首次连接（``_serve_one``）、换房成功（``_switch_room``）、清空房间
    （``_clear_room``）。前端 ``renderSidebar`` 一帧 ``messagesEl.replaceChildren()``
    清屏，再用 ``room_history`` 回放，最后用 ``task_info`` 装任务面板。
    ``_switch_room`` 的失败兜底只单发 room_info 让 sidebar 状态复位，不走这个 helper。

    ``task_info`` 即使房间没任务也下发（空 ``{tasks: [], active_task_id: null}``），
    让前端用这帧确认"P5.1 任务协议生效"，避免老 sidecar 不下发的情况下前端误判。

    P5.2.6 起末尾追发 ``TasksStore`` 加载期累积的 warnings（如"多 task 但无 active"），
    每条转 NOTICE info envelope。warning 是 consume 一次性的，不会重复 emit。
    """
    emit_room_info(server, sink)
    emit_room_history(server, sink)
    server.task._emit_task_info(sink)
    for warning in server._session.tasks_store.consume_load_warnings():
        server._emit_notice(sink, level=NOTICE_LEVEL_INFO, text=warning)
