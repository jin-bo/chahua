"""``ChahuaServer._emit_room_info`` envelope shape 回归（P4.4）。

测两件事：

1. ``_mcp_summary_list`` / ``_llm_summary`` 两个纯函数 helper —— envelope 字段的数据
   shape 都汇到它们这里，单测它们就锁住"shape 不变"的契约。

2. ``_emit_room_info`` 的合并逻辑：拆 ``persona_mcp_servers`` / ``room_mcp_servers``
   两块、计 ``effective_mcp_names`` 顺序（房间级覆盖 persona 同名 + 未受信任 persona
   不进 effective）、每 guest llm 的 ``source`` 区分（``"guest"`` vs ``"room_default"``）
   + ``api_key_ready`` 走 env 探测、``room.scoring_llm`` / ``summary_llm`` 同构、
   ``api_key`` 本身**绝不下发**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chahua import admin, trust
from chahua._paths import ENV_APP_ROOT, ENV_USER_DATA_ROOT, Paths
from chahua.llm_spec import LLMSpec
from chahua.server import ChahuaServer, _llm_summary, _mcp_summary_list


REPO_ROOT = Path(__file__).resolve().parent.parent


# ── 纯函数 helper ─────────────────────────────────────────────────────────


def test_mcp_summary_list_strips_env_and_normalizes():
    """``env`` 含可能敏感 token —— 不下发到前端。仅留 name / command / args。"""
    out = _mcp_summary_list({
        "web": {"command": "npx", "args": ["-y", "@x"], "env": {"TOKEN": "secret"}},
        "fs": {"command": "fs-mcp"},
    })
    assert out == [
        {"name": "web", "command": "npx", "args": ["-y", "@x"]},
        {"name": "fs", "command": "fs-mcp", "args": []},
    ]


def test_mcp_summary_list_handles_none_and_empty():
    assert _mcp_summary_list(None) == []
    assert _mcp_summary_list({}) == []


def test_llm_summary_assembles_provider_slash_model_and_default_env_name(monkeypatch):
    """spec.api_key_env=None → 默认 ``<PROVIDER>_API_KEY`` 约定写回 envelope。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-xx")
    spec = LLMSpec(provider="openai", model="gpt-5.4", base_url=None, api_key_env=None)
    out = _llm_summary(spec=spec, source="room_default")
    assert out["model"] == "openai/gpt-5.4"
    assert out["api_key_env"] == "OPENAI_API_KEY"
    assert out["api_key_ready"] is True
    assert out["source"] == "room_default"
    # api_key 本身永不下发。
    assert "api_key" not in out


def test_llm_summary_api_key_ready_false_when_env_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    spec = LLMSpec(provider="anthropic", model="claude-opus-4-7")
    assert _llm_summary(spec=spec, source="guest")["api_key_ready"] is False


def test_llm_summary_ollama_always_ready(monkeypatch):
    """ollama 本地不强鉴权 —— api_key_ready 永远 True 让 UI 不显"未配置"。"""
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    spec = LLMSpec(provider="ollama", model="llama3")
    assert _llm_summary(spec=spec, source="guest")["api_key_ready"] is True


def test_llm_summary_custom_api_key_env_name(monkeypatch):
    monkeypatch.setenv("CORP_KEY", "x")
    spec = LLMSpec(provider="openai", model="gpt-4", api_key_env="CORP_KEY")
    out = _llm_summary(spec=spec, source="guest")
    assert out["api_key_env"] == "CORP_KEY"
    assert out["api_key_ready"] is True


# ── _emit_room_info 整体（要真 RoomSession）────────────────────────────


@pytest.fixture
def env_paths(tmp_path, monkeypatch):
    """user_data_root 在 tmp 下；app_root 指真仓库（带 ship persona）。"""
    user_data = tmp_path / "userdata"
    user_data.mkdir()
    monkeypatch.setenv(ENV_APP_ROOT, str(REPO_ROOT))
    monkeypatch.setenv(ENV_USER_DATA_ROOT, str(user_data))
    # LLM env 装好让 build_room_session 不 SystemExit。
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4")
    # 测试用的自定义 api_key_env —— 装它仅是为了让 build_client 不在装配期 SystemExit；
    # `api_key_ready` 字段是 envelope 端用 os.environ.get(api_key_env) 自己再判一遍的，
    # 不会因这里"已设"就误判（个别 assert 会 delenv 强制 ready=False）。
    monkeypatch.setenv("ANTHROPIC_KEY", "sk-anthro-stub")
    return Paths.from_env()


def _emit_and_capture(env_paths, room_dir) -> dict:
    """对指定 room_dir 装一只 RoomSession + 调 ``_emit_room_info`` 收 envelope ``data``。"""
    from chahua.session import build_room_session

    session = build_room_session(room_dir, env_paths)
    try:
        srv = object.__new__(ChahuaServer)
        srv._session = session  # type: ignore[attr-defined]
        srv._paths = env_paths  # type: ignore[attr-defined]
        captured: list[dict] = []
        srv._emit_room_info(lambda env: captured.append(env.to_dict()))
        assert len(captured) == 1
        return captured[0]["data"]
    finally:
        session.close()


def _capture_room_info(env_paths) -> dict:
    """造一只双茶客 + 部分自定义 LLM / 房间级 MCP 的 RoomSession，调 ``_emit_room_info``。"""
    rc = admin.create_room(
        paths=env_paths, room_id="info", name="info",
        guests=[
            {"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"},
            {"persona": "chahua/personas/汪小姐/汪小姐.md", "name": "汪小姐"},
        ],
    )
    admin.update_guest_extra_mcp(
        paths=env_paths, room_dir=rc.room_dir, name="宝总",
        servers=[{"name": "web", "command": "npx", "args": ["-y", "@mcp/web"]}],
    )
    admin.update_guest_llm(
        paths=env_paths, room_dir=rc.room_dir, name="汪小姐",
        spec_dict={"model": "openai/gpt-4o", "api_key_env": "ANTHROPIC_KEY"},
    )
    admin.update_room_llm(
        paths=env_paths, room_dir=rc.room_dir, section="scoring",
        spec_dict={"model": "openai/gpt-5.4-mini"},
    )
    return _emit_and_capture(env_paths, rc.room_dir)


def test_emit_room_info_splits_persona_and_room_mcp(env_paths):
    data = _capture_room_info(env_paths)
    by_name = {g["name"]: g for g in data["guests"]}

    g = by_name["宝总"]
    assert g["persona_mcp_servers"] == []          # 宝总 persona 是 flat 形态，无 sidecar
    assert g["persona_mcp_trusted"] is False
    assert g["room_mcp_servers"] == [
        {"name": "web", "command": "npx", "args": ["-y", "@mcp/web"]},
    ]
    assert g["effective_mcp_names"] == ["web"]     # 只有房间级一条


def test_emit_room_info_per_guest_llm_source(env_paths):
    data = _capture_room_info(env_paths)
    by_name = {g["name"]: g for g in data["guests"]}

    # 汪小姐写了自定义 LLM（用了非默认 api_key_env "ANTHROPIC_KEY"）。
    assert by_name["汪小姐"]["llm"]["source"] == "guest"
    assert by_name["汪小姐"]["llm"]["model"] == "openai/gpt-4o"
    assert by_name["汪小姐"]["llm"]["api_key_env"] == "ANTHROPIC_KEY"
    assert by_name["汪小姐"]["llm"]["api_key_ready"] is True

    # 宝总没写 → 走房间默认。
    assert by_name["宝总"]["llm"]["source"] == "room_default"
    assert by_name["宝总"]["llm"]["model"] == "openai/gpt-5.4"
    assert by_name["宝总"]["llm"]["api_key_ready"] is True


def test_emit_room_info_room_llm_sections(env_paths):
    data = _capture_room_info(env_paths)
    assert data["scoring_llm"]["source"] == "room"
    assert data["scoring_llm"]["model"] == "openai/gpt-5.4-mini"
    # summary 段缺 → 走 scoring fallback。
    assert data["summary_llm"]["source"] == "default"
    assert data["summary_llm"]["model"] == "openai/gpt-5.4-mini"


def test_emit_room_info_room_default_llm_from_env(env_paths):
    """P4.9：默认情况（toml 没写 [room.llm]）→ room_default_llm.source = "default"
    表示走 env 推断；UI 据此把 "在 room.toml 写明" radio 显示为 unchecked。"""
    data = _capture_room_info(env_paths)
    assert data["room_default_llm"]["source"] == "default"
    assert data["room_default_llm"]["model"] == "openai/gpt-5.4"


def test_emit_room_info_room_default_llm_from_toml(env_paths):
    """[room.llm] 在 toml 里写过 → source = "room"，model 反映 toml 段，不是 env。"""
    rc = admin.create_room(
        paths=env_paths, room_id="rdef", name="rdef",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )
    admin.update_room_llm(
        paths=env_paths, room_dir=rc.room_dir, section="room",
        spec_dict={"model": "openai/gpt-5.4-mini", "temperature": 0.3},
    )
    data = _emit_and_capture(env_paths, rc.room_dir)
    assert data["room_default_llm"]["source"] == "room"
    assert data["room_default_llm"]["model"] == "openai/gpt-5.4-mini"
    assert data["room_default_llm"]["temperature"] == pytest.approx(0.3)


def test_emit_room_info_does_not_leak_api_key(env_paths, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "leaky-sk-secret-DONT-LEAK")
    data = _capture_room_info(env_paths)
    serialized = repr(data)
    assert "leaky-sk-secret-DONT-LEAK" not in serialized
    # envelope 也不应有任何 ``api_key`` 字段（只有 ``api_key_env`` / ``api_key_ready``）。
    for g in data["guests"]:
        assert "api_key" not in g["llm"]
    assert "api_key" not in data["scoring_llm"]
    assert "api_key" not in data["summary_llm"]


def test_emit_room_info_effective_mcp_respects_trust(env_paths):
    """未受信任的 persona MCP 不进 ``effective_mcp_names``；
    一旦勾信任，persona 与房间级合并、同名时房间级覆盖（顺序：persona 先 → 房间级追加）。"""
    # 装一个 dir-form persona 带 sidecar mcp.json。
    persona_dir = env_paths.user_data_root / "chahua" / "personas" / "Sidecar"
    persona_dir.mkdir(parents=True)
    (persona_dir / "Sidecar.md").write_text("# soul", encoding="utf-8")
    import json
    (persona_dir / "mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "shared": {"command": "persona-shared"},
                "persona-only": {"command": "persona-p"},
            }
        }),
        encoding="utf-8",
    )

    rc = admin.create_room(
        paths=env_paths, room_id="sidecar", name="sidecar",
        guests=[{"persona": "chahua/personas/Sidecar/Sidecar.md", "name": "Sidecar"}],
    )
    admin.update_guest_extra_mcp(
        paths=env_paths, room_dir=rc.room_dir, name="Sidecar",
        servers=[
            {"name": "shared", "command": "room-shared"},
            {"name": "room-only", "command": "room-r"},
        ],
    )

    g = _emit_and_capture(env_paths, rc.room_dir)["guests"][0]
    # persona 没勾信任 → effective 里只剩房间级。
    assert g["persona_mcp_trusted"] is False
    assert sorted(g["effective_mcp_names"]) == ["room-only", "shared"]

    # 勾信任 persona MCP。
    trust.set_mcp_trust(
        env_paths,
        persona_rel="chahua/personas/Sidecar/Sidecar.md",
        trusted=True,
    )
    g2 = _emit_and_capture(env_paths, rc.room_dir)["guests"][0]
    assert g2["persona_mcp_trusted"] is True
    # 顺序：persona (shared, persona-only) → 房间级追加 (room-only)；shared 房间级覆盖
    # persona 同名，但 name key 已在；dedup 后顺序保留 persona 先 + 房间级新名后。
    assert g2["effective_mcp_names"] == ["shared", "persona-only", "room-only"]
