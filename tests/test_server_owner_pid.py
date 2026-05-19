"""``_owner_pid_from_env`` —— Windows owner-PID 解析的纯函数回归。

Electron sidecar 拉起 chahua-server 时通过 ``CHAHUA_PARENT_PID`` env var 传自己的
PID，sidecar 用它走 ``OpenProcess(SYNCHRONIZE) + WaitForSingleObject`` 监 owner 退出。
本测覆盖三档：env 缺失、env 是垃圾、env 是合法 PID。
"""

from __future__ import annotations

import pytest

from chahua.server_entry import _owner_pid_from_env


def test_env_unset_returns_zero(monkeypatch):
    """env 缺 → 0（独立 ``uv run chahua-server`` 时常态，不装 watcher）。"""
    monkeypatch.delenv("CHAHUA_PARENT_PID", raising=False)
    assert _owner_pid_from_env() == 0


def test_env_garbage_returns_zero(monkeypatch):
    """env 是非数字 → 0 + debug 日志，不抛。"""
    monkeypatch.setenv("CHAHUA_PARENT_PID", "not-a-pid")
    assert _owner_pid_from_env() == 0


def test_env_negative_returns_zero(monkeypatch):
    """env 是负数 / 0 → 0（仅正 PID 可监控）。"""
    monkeypatch.setenv("CHAHUA_PARENT_PID", "-1")
    assert _owner_pid_from_env() == 0
    monkeypatch.setenv("CHAHUA_PARENT_PID", "0")
    assert _owner_pid_from_env() == 0


def test_env_valid_pid(monkeypatch):
    """env 是正整数 → 原样返。"""
    monkeypatch.setenv("CHAHUA_PARENT_PID", "12345")
    assert _owner_pid_from_env() == 12345


def test_env_with_whitespace_rejected(monkeypatch):
    """前后空格 —— ``int("  42  ")`` 在 CPython 是合法的，但我们走 ``int(raw)`` 不
    strip，所以 ``"  42  "`` 也能解析。这测试钉死当前行为，行为变了再调测。
    """
    monkeypatch.setenv("CHAHUA_PARENT_PID", "  42  ")
    assert _owner_pid_from_env() == 42
