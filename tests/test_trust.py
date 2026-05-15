"""chahua.trust —— MCP 信任清单持久化 回归。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chahua._paths import ENV_APP_ROOT, ENV_USER_DATA_ROOT, Paths
from chahua.trust import is_mcp_trusted, list_trusted, set_mcp_trust


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def paths(tmp_path, monkeypatch):
    user_data = tmp_path / "userdata"
    user_data.mkdir()
    monkeypatch.setenv(ENV_APP_ROOT, str(REPO_ROOT))
    monkeypatch.setenv(ENV_USER_DATA_ROOT, str(user_data))
    return Paths.from_env()


def test_default_is_untrusted(paths):
    """文件还没建 → 任何 persona 都返 False。"""
    assert list_trusted(paths) == frozenset()
    assert not is_mcp_trusted(paths, "chahua/personas/Yvonne/Yvonne.md")


def test_set_and_revoke_round_trip(paths):
    rel = "chahua/personas/Yvonne/Yvonne.md"
    set_mcp_trust(paths, rel, True)
    assert is_mcp_trusted(paths, rel)
    set_mcp_trust(paths, rel, False)
    assert not is_mcp_trusted(paths, rel)


def test_set_trust_persists_across_calls(paths):
    """信任写盘后 list_trusted 再读应该看到（同一进程也算）。"""
    set_mcp_trust(paths, "a", True)
    set_mcp_trust(paths, "b", True)
    assert list_trusted(paths) == frozenset({"a", "b"})


def test_set_trust_atomically_no_duplicates(paths):
    """重复 set True 不该让清单里出现两条 'a'。"""
    set_mcp_trust(paths, "a", True)
    set_mcp_trust(paths, "a", True)
    assert list_trusted(paths) == frozenset({"a"})


def test_set_trust_rejects_empty_persona_rel(paths):
    with pytest.raises(ValueError):
        set_mcp_trust(paths, "", True)


def test_is_mcp_trusted_handles_empty_persona_rel(paths):
    """空串 永远 False —— 防误传"""
    assert not is_mcp_trusted(paths, "")


def test_corrupt_trust_file_treated_as_empty(paths):
    """JSON 坏掉 → 按未信任处理，不让用户失去全部 trust 但也别让 server 启动失败。"""
    (paths.user_data_root / ".chahua-trusted-mcp.json").write_text("{ broken json", encoding="utf-8")
    assert list_trusted(paths) == frozenset()
    assert not is_mcp_trusted(paths, "x")


def test_trust_file_format_is_stable(paths):
    """落盘格式：version + sorted trusted —— git diff 友好（用户备份 user_data 时不抖）。"""
    set_mcp_trust(paths, "b", True)
    set_mcp_trust(paths, "a", True)
    raw = (paths.user_data_root / ".chahua-trusted-mcp.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["version"] == 1
    assert data["trusted"] == ["a", "b"]  # sorted
