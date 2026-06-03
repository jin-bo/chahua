"""P14 «双路 err»：``_start_agent_run`` 的无参数原因码在两个消费方分叉本地化。

承重不变量（docs/P14「双路 err」+ 测试计划第 6 条）：同一个拒绝场景，
- ``spawn_agent_run(s)`` 工具 → tool result 是**英文** ``Error: ...``（回灌 LLM）；
- ``agent_run_start`` inbound → 前端 NOTICE 是**中文**（用户可见，P14 范围外保持中文）。

源头 ``_start_agent_run`` 返**无参数原因码**（``agent_run.AgentRunError``），两路各自
本地化 —— 避免「中文句再正则回译」的脆弱耦合，两语言由构造保证同步。本测直接钉两个
平行 renderer 在同一码上的语言分叉（端到端的 source→code 已由
``test_start_agent_run_helper`` 覆盖、tool→英文由 ``test_agent_run_tools`` 覆盖）。
"""

from __future__ import annotations

import re

import pytest

from chahua.agent_run import (
    AGENT_RUN_ERR_MTS_MANAGER,
    AGENT_RUN_ERR_ROOM_CAP,
    AGENT_RUN_ERR_TARGET_ABSENT,
    AGENT_RUN_ERR_TARGET_BUSY,
    AGENT_RUN_ERR_TASK_CLOSED,
    AGENT_RUN_ERR_TASK_NOT_FOUND,
)
from chahua.agent_run_tools import _agent_run_err_en
from chahua.server_inbound_agent_run import _agent_run_err_zh

_ALL_CODES = [
    AGENT_RUN_ERR_TARGET_ABSENT,
    AGENT_RUN_ERR_TASK_NOT_FOUND,
    AGENT_RUN_ERR_TASK_CLOSED,
    AGENT_RUN_ERR_TARGET_BUSY,
    AGENT_RUN_ERR_ROOM_CAP,
    AGENT_RUN_ERR_MTS_MANAGER,
]

_CJK = re.compile(r"[一-鿿]")


@pytest.mark.parametrize("code", _ALL_CODES)
def test_each_code_renders_chinese_for_notice_english_for_tool(code: str) -> None:
    """同一原因码：中文 renderer 含 CJK（NOTICE 给用户）、英文 renderer 不含 CJK
    （tool result 给 LLM）。两路都不回显原始码字面。"""
    zh = _agent_run_err_zh(code, target="Bob", task_id="t1")
    en = _agent_run_err_en(code, target="Bob", task_id="t1")

    assert _CJK.search(zh), f"中文 NOTICE 应含 CJK: {zh!r}"
    assert not _CJK.search(en), f"英文 tool result 不应含 CJK: {en!r}"
    # 两路都真正本地化了，不是把原始码字面（如 'target_absent'）漏出去。
    assert zh != code and en != code
    assert code not in en


def test_target_absent_diverges_by_language() -> None:
    """以 target 不在场为例：同一码，NOTICE 中文 / tool 英文，措辞分叉。"""
    code = AGENT_RUN_ERR_TARGET_ABSENT
    assert "不在场" in _agent_run_err_zh(code, target="Bob", task_id=None)
    assert "is not in the room" in _agent_run_err_en(code, target="Bob", task_id=None)


def test_room_cap_notice_keeps_number_tool_omits() -> None:
    """room_cap：用户 NOTICE 保留具体上限数字；LLM 文案不需要精确数字。"""
    zh = _agent_run_err_zh(AGENT_RUN_ERR_ROOM_CAP, target="Bob", task_id=None)
    en = _agent_run_err_en(AGENT_RUN_ERR_ROOM_CAP, target="Bob", task_id=None)
    assert "上限" in zh and any(c.isdigit() for c in zh)
    assert "background-run limit" in en
