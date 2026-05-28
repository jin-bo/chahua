"""P12.5：persona / 房间级 MCP server 的 env / headers / args 变量插值回归。

承重不变量（见 docs/P12.5）：
- persona / 房间两层的 ``$VAR`` / ``${VAR}`` 在 ``_merged_mcp_configs`` 一次性展开；
- 文件加载层（``load_mcp_config`` 结果）**不**二次展开；
- 缺变量 → 空串 + WARN，不报错；
- 信任门已置 None 时不展开任何 persona server；
- raw 配置不被原地改写（room snapshot 投影还要用字面 ``${VAR}``）。
"""

from __future__ import annotations

import logging

import pytest

from chahua import guest as guest_mod
from chahua.guest import _merged_mcp_configs


@pytest.fixture
def no_file_layer(monkeypatch):
    """把 agentao 文件加载层 stub 成空 —— 测试只关心 persona / 房间两层，且避免开发机
    真实 ``~/.agentao/mcp.json`` 污染断言。"""
    monkeypatch.setattr(guest_mod, "load_mcp_config", lambda **kw: {})


def test_persona_env_braced_expanded(no_file_layer, tmp_path, monkeypatch):
    monkeypatch.setenv("AMAP_MAPS_API_KEY", "real-key")
    persona = {"amap": {"command": "npx", "env": {"AMAP_MAPS_API_KEY": "${AMAP_MAPS_API_KEY}"}}}
    merged = _merged_mcp_configs(tmp_path, persona)
    assert merged["amap"]["env"]["AMAP_MAPS_API_KEY"] == "real-key"


def test_bare_dollar_form_expanded(no_file_layer, tmp_path, monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    persona = {"srv": {"env": {"K": "$FOO"}}}
    merged = _merged_mcp_configs(tmp_path, persona)
    assert merged["srv"]["env"]["K"] == "bar"


def test_concatenation_expanded(no_file_layer, tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "xyz")
    persona = {"srv": {"env": {"AUTH": "Bearer ${TOK}"}}}
    merged = _merged_mcp_configs(tmp_path, persona)
    assert merged["srv"]["env"]["AUTH"] == "Bearer xyz"


def test_hardcoded_value_untouched(no_file_layer, tmp_path):
    """无变量引用形态的值原样保留 —— 老 persona 硬写 key 仍可用（迁移兼容）。"""
    persona = {"srv": {"env": {"K": "sk-hardcoded-literal"}}}
    merged = _merged_mcp_configs(tmp_path, persona)
    assert merged["srv"]["env"]["K"] == "sk-hardcoded-literal"


def test_args_expanded_non_str_preserved(no_file_layer, tmp_path, monkeypatch):
    monkeypatch.setenv("REGION", "cn")
    persona = {"srv": {"args": ["-y", "${REGION}", 123]}}
    merged = _merged_mcp_configs(tmp_path, persona)
    assert merged["srv"]["args"] == ["-y", "cn", 123]


def test_headers_expanded(no_file_layer, tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "abc")
    persona = {"srv": {"url": "https://x", "headers": {"Authorization": "Bearer ${TOK}"}}}
    merged = _merged_mcp_configs(tmp_path, persona)
    assert merged["srv"]["headers"]["Authorization"] == "Bearer abc"


def _guest_warnings(caplog):
    return [
        r for r in caplog.records
        if r.name == "chahua.guest" and r.levelno >= logging.WARNING
    ]


def test_missing_var_empty_and_warns(no_file_layer, tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    persona = {"amap": {"env": {"K": "${MISSING_VAR}"}}}
    with caplog.at_level(logging.WARNING, logger="chahua.guest"):
        merged = _merged_mcp_configs(tmp_path, persona)
    assert merged["amap"]["env"]["K"] == ""
    assert "MISSING_VAR" in caplog.text
    assert "amap" in caplog.text


def test_empty_env_var_warns(no_file_layer, tmp_path, monkeypatch, caplog):
    """变量已设置但为空串 —— 同样展开成空、同样 WARN（否则空 API key 静默失败无诊断）。"""
    monkeypatch.setenv("EMPTY_KEY", "")
    persona = {"amap": {"env": {"K": "${EMPTY_KEY}"}}}
    with caplog.at_level(logging.WARNING, logger="chahua.guest"):
        merged = _merged_mcp_configs(tmp_path, persona)
    assert merged["amap"]["env"]["K"] == ""
    assert "EMPTY_KEY" in caplog.text


def test_present_var_substitutes_and_no_warn(no_file_layer, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("PRESENT", "v")
    persona = {"srv": {"env": {"K": "${PRESENT}"}}}
    with caplog.at_level(logging.WARNING, logger="chahua.guest"):
        merged = _merged_mcp_configs(tmp_path, persona)
    assert merged["srv"]["env"]["K"] == "v"        # 真展开了
    assert _guest_warnings(caplog) == []           # 且没有 WARN


def test_mixed_ref_warns_only_missing(no_file_layer, tmp_path, monkeypatch, caplog):
    """一个值里既有已设变量又有缺失变量 —— 只对缺失的 WARN，已设的不 WARN。"""
    monkeypatch.setenv("TOK", "xyz")
    monkeypatch.delenv("NOPE", raising=False)
    persona = {"srv": {"env": {"AUTH": "Bearer ${TOK}-${NOPE}"}}}
    with caplog.at_level(logging.WARNING, logger="chahua.guest"):
        merged = _merged_mcp_configs(tmp_path, persona)
    assert merged["srv"]["env"]["AUTH"] == "Bearer xyz-"
    warnings = _guest_warnings(caplog)
    assert len(warnings) == 1
    assert "NOPE" in warnings[0].getMessage()
    assert "TOK" not in warnings[0].getMessage()


def test_repeated_missing_var_warns_once(no_file_layer, tmp_path, monkeypatch, caplog):
    """同名缺失变量在一个值里出现多次只 WARN 一次（去噪）。"""
    monkeypatch.delenv("FOO", raising=False)
    persona = {"srv": {"env": {"K": "$FOO-$FOO-$FOO"}}}
    with caplog.at_level(logging.WARNING, logger="chahua.guest"):
        _merged_mcp_configs(tmp_path, persona)
    assert len(_guest_warnings(caplog)) == 1


def test_file_loaded_layer_not_re_expanded(tmp_path, monkeypatch):
    """文件加载层（load_mcp_config 已展开）不被 _merged_mcp_configs 二次展开。"""
    monkeypatch.setenv("SHOULD_NOT_EXPAND", "oops")
    monkeypatch.setattr(
        guest_mod,
        "load_mcp_config",
        lambda **kw: {"filesrv": {"env": {"K": "${SHOULD_NOT_EXPAND}"}}},
    )
    merged = _merged_mcp_configs(tmp_path, None)
    assert merged["filesrv"]["env"]["K"] == "${SHOULD_NOT_EXPAND}"


def test_room_level_expanded(no_file_layer, tmp_path, monkeypatch):
    monkeypatch.setenv("RK", "room-key")
    merged = _merged_mcp_configs(
        tmp_path, None, room_level_servers={"rsrv": {"env": {"K": "${RK}"}}}
    )
    assert merged["rsrv"]["env"]["K"] == "room-key"
    assert merged["rsrv"]["trust"] is True


def test_untrusted_persona_none_no_servers(no_file_layer, tmp_path):
    """信任门把 persona_servers 置 None 时不展开任何 persona server。"""
    merged = _merged_mcp_configs(tmp_path, None)
    assert merged == {}


def test_raw_config_not_mutated(no_file_layer, tmp_path, monkeypatch):
    """传入的 raw persona 配置不被原地改写 —— snapshot 投影还要用字面 ${VAR}。"""
    monkeypatch.setenv("FOO", "bar")
    env_dict = {"K": "${FOO}"}
    persona = {"srv": {"env": env_dict}}
    _merged_mcp_configs(tmp_path, persona)
    assert env_dict["K"] == "${FOO}"
    assert persona["srv"]["env"]["K"] == "${FOO}"


def test_trust_flag_preserved_on_persona(no_file_layer, tmp_path, monkeypatch):
    """persona server 显式 trust 字段在展开后仍保留（不被插值逻辑吃掉）。"""
    monkeypatch.setenv("FOO", "bar")
    persona = {"srv": {"env": {"K": "${FOO}"}, "trust": False}}
    merged = _merged_mcp_configs(tmp_path, persona)
    assert merged["srv"]["trust"] is False
    assert merged["srv"]["env"]["K"] == "bar"
