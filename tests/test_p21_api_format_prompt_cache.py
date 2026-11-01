"""P21 LLM 协议格式与提示缓存（docs/P21-LLM 协议格式与提示缓存.md §6）。

- ``LLMSpec`` 新增 ``api_format`` / ``prompt_cache``：env（``<PREFIX>_API_FORMAT`` /
  ``<PREFIX>_PROMPT_CACHE``）与 toml 两入口、校验、``build_client`` 透传。
- 管理端写回 / room_info / guest 白名单闭环。
- ``speak`` 遇 agentao ``llm_error`` 回合走 error message_end，不进 transcript。

``set_llm_credentials`` 的两条在 ``tests/test_set_llm_credentials.py``；
真 agentao 恒 400 的一条在 ``tests/test_agentao_contract.py``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chahua import llm_spec
from chahua.admin_toml import _llm_spec_to_dict
from chahua.config import _ALLOWED_GUEST_KEYS
from chahua.events import STATUS_ERROR, ChahuaEventType
from chahua.guest import TeaGuest
from chahua.llm_spec import LLM_TOML_FIELDS, LLMSpec, build_client
from chahua.room import Room
from chahua.server_room_snapshot import _llm_summary
from chahua.transport_bridge import ChahuaTransport


# ── env ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def claude_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("CLAUDE_BASE_URL", "https://api.anthropic.com/v1")
    monkeypatch.setenv("CLAUDE_API_KEY", "sk-x")
    for k in ("CLAUDE_API_FORMAT", "CLAUDE_PROMPT_CACHE", "LLM_PROMPT_CACHE"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_env_defaults_are_none(claude_env):
    spec = LLMSpec.try_from_env()
    assert spec.api_format is None and spec.prompt_cache is None


def test_env_reads_prefixed_keys_and_normalizes(claude_env):
    claude_env.setenv("CLAUDE_API_FORMAT", " Anthropic-Messages ")
    claude_env.setenv("CLAUDE_PROMPT_CACHE", "ANTHROPIC")
    spec = LLMSpec.try_from_env()
    assert spec.api_format == "anthropic-messages"
    assert spec.prompt_cache == "anthropic"


@pytest.mark.parametrize("off", ["off", "", "  ", "none", "OFF"])
def test_env_prompt_cache_off_values(claude_env, off):
    claude_env.setenv("CLAUDE_PROMPT_CACHE", off)
    assert LLMSpec.try_from_env().prompt_cache is None


def test_env_global_llm_prompt_cache_ignored(claude_env):
    """全局开关在桌面注入切 provider 后会残留到新端点 —— 只认 <PREFIX>_PROMPT_CACHE。"""
    claude_env.setenv("LLM_PROMPT_CACHE", "anthropic")
    assert LLMSpec.try_from_env().prompt_cache is None


def test_env_other_prefix_not_read(claude_env):
    claude_env.setenv("OPENAI_API_FORMAT", "openai-responses")
    assert LLMSpec.try_from_env().api_format is None


@pytest.mark.parametrize("key,val", [
    ("CLAUDE_API_FORMAT", "anthropic"),
    ("CLAUDE_API_FORMAT", "gemini-api"),
    ("CLAUDE_PROMPT_CACHE", "openai"),
])
def test_env_invalid_value_exits(claude_env, key, val):
    claude_env.setenv(key, val)
    with pytest.raises(SystemExit) as ei:
        LLMSpec.try_from_env()
    assert key in str(ei.value)


def test_build_client_passes_fields(claude_env):
    claude_env.setenv("CLAUDE_API_FORMAT", "anthropic-messages")
    claude_env.setenv("CLAUDE_PROMPT_CACHE", "anthropic")
    client = build_client(LLMSpec.try_from_env())
    assert client.api_format == "anthropic-messages"
    assert client.prompt_cache == "anthropic"


def test_build_client_defaults_unchanged(claude_env):
    client = build_client(LLMSpec.try_from_env())
    assert client.api_format == "openai-completions"
    assert client.prompt_cache is None


def test_whitelists_match_agentao():
    """chahua 自持白名单；agentao 升级增删取值时这里先红，逼一次人工同步。"""
    from agentao.llm._api_format import API_FORMATS
    from agentao.llm._cache_control import CACHE_CONTROL_FORMATS

    assert set(llm_spec.API_FORMATS) == set(API_FORMATS)
    assert set(llm_spec.PROMPT_CACHE_FORMATS) == set(CACHE_CONTROL_FORMATS)


# ── toml ─────────────────────────────────────────────────────────────────────


def test_toml_parses_and_normalizes():
    spec = LLMSpec.from_toml(
        {"model": "openai/gpt-6-astra", "api_format": "OpenAI-Responses", "prompt_cache": "off"},
        label="[room.llm]",
    )
    assert spec.api_format == "openai-responses"
    assert spec.prompt_cache is None


def test_toml_does_not_fall_back_to_env(claude_env):
    """端点属性按 section 生效：toml 段没写就是默认，不读 env。"""
    claude_env.setenv("CLAUDE_API_FORMAT", "anthropic-messages")
    claude_env.setenv("CLAUDE_PROMPT_CACHE", "anthropic")
    spec = LLMSpec.from_toml({"model": "claude/claude-sonnet-5", "base_url": "https://x/v1"}, label="s")
    client = build_client(spec)
    assert client.api_format == "openai-completions"
    assert client.prompt_cache is None


@pytest.mark.parametrize("data", [
    {"model": "openai/m", "api_format": "responses"},
    {"model": "openai/m", "prompt_cache": "yes"},
    {"model": "openai/m", "api_format": 1},
    {"api_format": "openai-responses"},
    {"prompt_cache": "anthropic"},
])
def test_toml_invalid(data):
    with pytest.raises(ValueError):
        LLMSpec.from_toml(data, label="[scoring]")


def test_fields_registered_everywhere():
    assert {"api_format", "prompt_cache"} <= set(LLM_TOML_FIELDS)
    assert set(LLM_TOML_FIELDS) <= _ALLOWED_GUEST_KEYS


def test_admin_writeback_roundtrip():
    spec = LLMSpec.from_toml(
        {"model": "claude/claude-sonnet-5", "base_url": "https://x/v1",
         "api_format": "anthropic-messages", "prompt_cache": "anthropic"},
        label="s",
    )
    out = _llm_spec_to_dict(spec)
    assert out["api_format"] == "anthropic-messages" and out["prompt_cache"] == "anthropic"
    assert LLMSpec.from_toml(out, label="s") == spec
    bare = _llm_spec_to_dict(LLMSpec.from_toml({"model": "openai/m"}, label="s"))
    assert "api_format" not in bare and "prompt_cache" not in bare


def test_room_info_summary_carries_fields():
    spec = LLMSpec.from_toml(
        {"model": "openai/m", "api_format": "openai-responses"}, label="s",
    )
    d = _llm_summary(spec=spec, source="room")
    assert d["api_format"] == "openai-responses"
    assert d["prompt_cache"] is None


def test_room_toml_end_to_end(tmp_path: Path, monkeypatch):
    from chahua._paths import ENV_APP_ROOT, ENV_USER_DATA_ROOT, Paths
    from chahua.config import load_room_config

    monkeypatch.setenv(ENV_APP_ROOT, str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv(ENV_USER_DATA_ROOT, str(tmp_path))

    room_dir = tmp_path / "r"
    room_dir.mkdir()
    (room_dir / "room.toml").write_text(
        '[room]\nname = "r"\n\n[room.llm]\nmodel = "claude/claude-sonnet-5"\n'
        'base_url = "https://api.anthropic.com/v1"\napi_format = "anthropic-messages"\n'
        'prompt_cache = "anthropic"\n\n'
        '[[guest]]\nname = "A"\npersona = "chahua/personas/宝总/宝总.md"\nmodel = "openai/gpt-6-astra"\n'
        'api_format = "openai-responses"\n',
        encoding="utf-8",
    )
    rc = load_room_config(room_dir, paths=Paths.from_env())
    assert rc.room_llm.api_format == "anthropic-messages"
    assert rc.room_llm.prompt_cache == "anthropic"
    assert rc.guests[0].llm.api_format == "openai-responses"
    assert rc.guests[0].llm.prompt_cache is None


# ── speak：上游调用失败不是发言 ───────────────────────────────────────────────


class _Outcome:
    def __init__(self, reason):
        self.incomplete_reason = reason
        self.status = "ok"


def _guest(text: str, outcome) -> TeaGuest:
    room = Room(name="r")
    g = TeaGuest.__new__(TeaGuest)
    g.name = "A"
    g.room = room
    from chahua.debug_recorder import NOOP_RECORDER

    g._recorder = NOOP_RECORDER
    g._share_dir = None
    g._transport = ChahuaTransport(room_id="r", guest_name="A")
    agent = type("FakeAgent", (), {})()

    async def _arun(*_a, **_k):
        agent.last_turn = outcome
        return text

    agent.arun = _arun
    g.agent = agent  # type: ignore[attr-defined]
    room.add_participant("A")
    return g


async def test_llm_error_turn_is_failure_not_message():
    notice = "[LLM API error: Error code: 400 - bad]"
    g = _guest(notice, _Outcome("llm_error"))
    envs: list = []
    msg = await g.speak("ctx", turn_id="turn_x", sink=envs.append)
    assert msg is None
    assert g.room.last_message() is None  # 没进 transcript
    [end] = [e for e in envs if e.type == ChahuaEventType.MESSAGE_END]
    assert end.status == STATUS_ERROR
    assert end.data["error"] == notice


@pytest.mark.parametrize("outcome", [_Outcome(None), _Outcome("max_iterations"), None])
async def test_other_outcomes_unchanged(outcome):
    g = _guest("hello", outcome)
    envs: list = []
    msg = await g.speak("ctx", turn_id="turn_x", sink=envs.append)
    assert msg is not None and msg.text == "hello"
