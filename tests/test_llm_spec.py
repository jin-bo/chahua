"""``chahua.llm_spec`` 的 ``split_model_id`` 与 ``LLMSpec.from_toml``（P4.1）回归。

env 路径与 ``build_client`` 的 SystemExit 由 session 端 / CLI 启动跑实测覆盖；这里只
盯 toml schema 解析与 all-or-nothing 校验（设计 §2.3）。
"""

from __future__ import annotations

import os

import pytest

from chahua.llm_spec import (
    LLMSpec,
    _DEFAULT_BASE_URLS,
    build_client,
    canonical_provider,
    split_model_id,
)


# ── split_model_id ────────────────────────────────────────────────────────


@pytest.mark.parametrize("value,expected", [
    ("openai/gpt-5.4", ("openai", "gpt-5.4")),
    ("anthropic/claude-opus-4-7", ("anthropic", "claude-opus-4-7")),
    # 多 / 取首位 —— OpenRouter / LiteLLM 普遍有二级路径。
    ("openrouter/qwen/qwen3-coder", ("openrouter", "qwen/qwen3-coder")),
    # provider 大小写归一。
    ("OpenAI/gpt-4", ("openai", "gpt-4")),
    # 周围空白被剥。
    (" openai / gpt-4 ", ("openai", "gpt-4")),
])
def test_split_model_id_valid(value, expected):
    assert split_model_id(value) == expected


@pytest.mark.parametrize("bad", [
    "gpt-5.4",         # 无 /
    "/gpt-4",          # 左空
    "openai/",         # 右空
    "/",               # 两边空
])
def test_split_model_id_invalid(bad):
    with pytest.raises(ValueError):
        split_model_id(bad)


# ── gemini / google provider（已知列表 + 别名归一）─────────────────────────


def test_gemini_in_known_providers():
    """``gemini`` 进已知 provider 列表，自带默认 base_url（可省 base_url）。"""
    assert "gemini" in _DEFAULT_BASE_URLS
    assert _DEFAULT_BASE_URLS["gemini"].startswith("https://generativelanguage.googleapis.com")


@pytest.mark.parametrize("value,expected", [
    ("gemini/gemini-2.5-pro", ("gemini", "gemini-2.5-pro")),
    # google 是 gemini 的别名，归一到 canonical gemini。
    ("google/gemini-2.5-pro", ("gemini", "gemini-2.5-pro")),
    ("GOOGLE/gemini-2.5-flash", ("gemini", "gemini-2.5-flash")),
    (" google / gemini-2.5-pro ", ("gemini", "gemini-2.5-pro")),
])
def test_split_model_id_google_alias(value, expected):
    assert split_model_id(value) == expected


def test_canonical_provider_alias():
    assert canonical_provider("google") == "gemini"
    assert canonical_provider("GOOGLE") == "gemini"
    assert canonical_provider("openai") == "openai"      # 非别名原样
    assert canonical_provider(" Anthropic ") == "anthropic"


def test_from_toml_google_normalizes_to_gemini():
    """``google/...`` toml 写法归一到 gemini —— 单一 canonical provider，
    env 前缀 / 默认 base_url / 展示字面量都走 gemini。"""
    spec = LLMSpec.from_toml({"model": "google/gemini-2.5-pro"}, label="[[guest]] x")
    assert spec.provider == "gemini"
    assert spec.model == "gemini-2.5-pro"
    assert spec.model_id == "gemini/gemini-2.5-pro"
    assert spec.default_api_key_env() == "GEMINI_API_KEY"


def test_from_toml_gemini_no_base_url_needed():
    """gemini 在已知列表，不写 base_url 也能过 from_toml（不抛未知 provider）。"""
    spec = LLMSpec.from_toml({"model": "gemini/gemini-2.5-flash"}, label="[[guest]] x")
    assert spec.base_url is None


def test_build_client_gemini(monkeypatch):
    """gemini spec 缺 base_url 时用默认端点 + GEMINI_API_KEY 建 client 成功。"""
    monkeypatch.setenv("GEMINI_API_KEY", "sk-gemini-test")
    spec = LLMSpec.from_toml({"model": "google/gemini-2.5-pro"}, label="[[guest]] x")
    client = build_client(spec)  # 不抛 SystemExit
    assert client is not None


# ── LLMSpec.from_toml ─────────────────────────────────────────────────────


def test_from_toml_none_returns_none():
    assert LLMSpec.from_toml(None, label="[scoring]") is None


def test_from_toml_empty_dict_returns_none():
    assert LLMSpec.from_toml({}, label="[scoring]") is None


def test_from_toml_model_only():
    spec = LLMSpec.from_toml({"model": "openai/gpt-4"}, label="[scoring]")
    assert spec == LLMSpec(provider="openai", model="gpt-4")


def test_from_toml_full_spec():
    spec = LLMSpec.from_toml(
        {
            "model": "anthropic/claude-opus-4-7",
            "base_url": "https://api.anthropic.com",
            "api_key_env": "ANTHROPIC_API_KEY",
        },
        label="[[guest]] 宝总",
    )
    assert spec == LLMSpec(
        provider="anthropic", model="claude-opus-4-7",
        base_url="https://api.anthropic.com", api_key_env="ANTHROPIC_API_KEY",
    )


def test_from_toml_unknown_provider_with_explicit_base_url_ok():
    """anthropic 不在 _DEFAULT_BASE_URLS 内 —— 必须显式给 base_url。"""
    spec = LLMSpec.from_toml(
        {"model": "anthropic/claude-opus-4-7", "base_url": "https://api.anthropic.com"},
        label="[scoring]",
    )
    assert spec is not None and spec.provider == "anthropic"


def test_from_toml_temperature_accepted():
    """P4.8 起 temperature 进 toml schema —— UI 可编辑，spec 直接持值，build_client
    走 spec.temperature 优先于 LLM_TEMPERATURE env / _DEFAULT_TEMPERATURE。"""
    spec = LLMSpec.from_toml(
        {"model": "openai/gpt-4", "temperature": 0.2}, label="[scoring]"
    )
    assert spec is not None
    assert spec.temperature == pytest.approx(0.2)


@pytest.mark.parametrize("raw", [0, 0.0, 1, 2, 2.0])
def test_from_toml_temperature_bounds_inclusive(raw):
    spec = LLMSpec.from_toml(
        {"model": "openai/gpt-4", "temperature": raw}, label="[scoring]"
    )
    assert spec is not None and spec.temperature == pytest.approx(float(raw))


@pytest.mark.parametrize("bad,match", [
    (-0.1, r"越界"),
    (2.01, r"越界"),
    ("0.5", r"必须是数值"),
    (True, r"必须是数值，得到 bool"),
])
def test_from_toml_temperature_invalid(bad, match):
    with pytest.raises(ValueError, match=match):
        LLMSpec.from_toml(
            {"model": "openai/gpt-4", "temperature": bad}, label="[scoring]"
        )


# all-or-nothing 反例（§2.3）─────────────────────────────────────────────


@pytest.mark.parametrize("data,match", [
    ({"base_url": "http://x"}, r"base_url / api_key_env / temperature 不能单独出现"),
    ({"api_key_env": "K"}, r"base_url / api_key_env / temperature 不能单独出现"),
    ({"base_url": "http://x", "api_key_env": "K"}, r"base_url / api_key_env / temperature 不能单独出现"),
    ({"temperature": 0.5}, r"base_url / api_key_env / temperature 不能单独出现"),
])
def test_from_toml_all_or_nothing(data, match):
    with pytest.raises(ValueError, match=match):
        LLMSpec.from_toml(data, label="[scoring]")


def test_from_toml_model_no_slash_raises():
    with pytest.raises(ValueError, match=r"必须形如 '<provider>/<model>'"):
        LLMSpec.from_toml({"model": "gpt-4"}, label="[scoring]")


def test_from_toml_unknown_provider_no_base_url_raises():
    with pytest.raises(ValueError, match=r"不在已知列表"):
        LLMSpec.from_toml({"model": "weirdprovider/m"}, label="[scoring]")


@pytest.mark.parametrize("data,match", [
    ({"model": 123}, r"model 必须是字符串"),
    ({"model": "openai/gpt-4", "base_url": 123}, r"base_url 必须是字符串"),
    ({"model": "openai/gpt-4", "api_key_env": 123}, r"api_key_env 必须是字符串"),
])
def test_from_toml_type_errors(data, match):
    with pytest.raises(ValueError, match=match):
        LLMSpec.from_toml(data, label="[scoring]")


def test_from_toml_label_threaded_into_error():
    with pytest.raises(ValueError, match=r"\[\[guest\]\] 宝总"):
        LLMSpec.from_toml({"model": "gpt-4"}, label="[[guest]] 宝总")


# ── LLMSpec.try_from_env（P4.9）────────────────────────────────────────────


def _clear_llm_env(monkeypatch):
    """剔掉所有可能让 ``try_from_env`` 拿到值的 env —— host shell 里 export 的
    ``LLM_TEMPERATURE`` / ``OPENAI_MODEL`` 等会污染测试，必须 unset 才能让断言
    确定性地比较 ``LLMSpec`` 默认值。"""
    for k in list(os.environ):
        if (
            k == "LLM_PROVIDER"
            or k == "LLM_TEMPERATURE"
            or k.endswith("_MODEL")
            or k.endswith("_BASE_URL")
        ):
            monkeypatch.delenv(k, raising=False)


def test_try_from_env_missing_model_returns_none(monkeypatch):
    """缺 <PREFIX>_MODEL → None（让装配层 fall back 到 [room.llm] toml）；
    不像 from_env 直接 SystemExit。"""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    assert LLMSpec.try_from_env() is None


def test_try_from_env_with_model_returns_spec(monkeypatch):
    """带 model 时返回完整 spec —— 与 from_env 同结果。"""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4")
    spec = LLMSpec.try_from_env()
    assert spec == LLMSpec(provider="openai", model="gpt-5.4")
