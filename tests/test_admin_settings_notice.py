"""茶客 / 房间详细设置失败时必发 NOTICE error 提醒用户（bugfix）。

历史问题：admin 更新 handler 的 catch-all 只 `_log.exception` + 重发 snapshot，
用户看到设置悄悄回滚、毫无反馈。两条失败路径：
  ① toml 校验失败（``admin.update_*`` 抛 ValueError）—— handler catch-all。
  ② 会话重建失败（``build_room_session`` → ``build_client`` 缺 API key 抛
     **SystemExit**，不是 Exception 子类）—— ``_replace_session`` 兜底。
两条都必须 emit NOTICE error。
"""

from __future__ import annotations

import pytest

from chahua.events import ChahuaEventType, NOTICE_LEVEL_ERROR


def _notices(events):
    return [
        e for e in events
        if e.type == ChahuaEventType.NOTICE
        and e.data.get("level") == NOTICE_LEVEL_ERROR
    ]


async def test_update_guest_llm_bad_spec_emits_notice(task_inbound_srv):
    """toml 校验失败（未知 provider 且没给 base_url）→ NOTICE error 提醒用户。"""
    session, srv = task_inbound_srv
    events: list = []
    sink = events.append

    # 未知 provider + 无 base_url → admin.update_guest_llm 抛 ValueError。
    srv.admin._update_guest_llm(
        name="宝总",
        spec_dict={"model": "nosuchvendor/some-model"},
        sink=sink,
    )

    errs = _notices(events)
    assert errs, "设置失败必须 emit 一条 NOTICE error"
    assert "模型设置未生效" in errs[0].data["text"]


async def test_replace_session_systemexit_emits_notice(task_inbound_srv, monkeypatch):
    """模型本身合法（toml 校验过）但会话重建抛 **SystemExit**（build_client 缺 API
    key 的真实形态）→ `_replace_session` 必须兜住 SystemExit（它不是 Exception 子类！）
    + emit NOTICE，而不是让异常逸出 / 设置悄悄回滚。"""
    import chahua.server as server_mod

    session, srv = task_inbound_srv
    events: list = []
    sink = events.append

    def _boom(*a, **k):
        raise SystemExit("LLM provider='gemini'：缺少环境变量 GEMINI_API_KEY。")

    monkeypatch.setattr(server_mod, "build_room_session", _boom)

    # toml 写入成功（gemini 合法），随后 _replace_session → build_room_session → SystemExit。
    srv.admin._update_guest_llm(
        name="宝总",
        spec_dict={"model": "gemini/gemini-2.5-pro"},
        sink=sink,
    )

    errs = _notices(events)
    assert errs, "会话重建失败（SystemExit）必须被兜住 + emit NOTICE，而不是悄悄回滚"
    assert "设置未生效" in errs[0].data["text"]


async def test_update_guest_llm_ok_no_error_notice(task_inbound_srv):
    """正常更新（openai，env 已配 key）不该误报 NOTICE error。"""
    session, srv = task_inbound_srv
    events: list = []
    sink = events.append

    srv.admin._update_guest_llm(
        name="宝总",
        spec_dict={"model": "openai/gpt-5.4-mini"},
        sink=sink,
    )

    assert not _notices(events), "成功更新不该 emit error NOTICE"
