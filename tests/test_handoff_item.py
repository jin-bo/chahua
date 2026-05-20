"""``chahua/handoff.py`` P7.1.1 单测 —— ``HandoffItem`` round-trip + 非法 kind。"""

from __future__ import annotations

import dataclasses

import pytest

from chahua.handoff import HANDOFF_ISSUED_BY_USER, HandoffItem, HandoffKind


def test_round_trip_minimal_delegate() -> None:
    item = HandoffItem(kind=HandoffKind.DELEGATE, target="A")
    d = item.to_dict()
    assert d == {
        "kind": "delegate",
        "target": "A",
        "issued_by": HANDOFF_ISSUED_BY_USER,
        "reason": None,
        "review_message_id": None,
        "created_at_ms": item.created_at_ms,
    }
    assert d["created_at_ms"] > 0


def test_round_trip_review() -> None:
    """P7.2：review 项 round-trip —— target + review_message_id 非空、reason 恒 None。"""
    item = HandoffItem(
        kind=HandoffKind.REVIEW, target="B", review_message_id="msg_abc",
    )
    d = item.to_dict()
    assert d == {
        "kind": "review",
        "target": "B",
        "issued_by": HANDOFF_ISSUED_BY_USER,
        "reason": None,
        "review_message_id": "msg_abc",
        "created_at_ms": item.created_at_ms,
    }


def test_delegate_review_message_id_defaults_none() -> None:
    assert HandoffItem(kind=HandoffKind.DELEGATE, target="A").review_message_id is None


def test_reason_is_serialized() -> None:
    """``reason`` 进 dict（给 debug record 用）；它**不**进茶客 prompt 这条不变量
    由 drain loop / context_renderer 单独保证，不在本 dataclass 测里覆盖。"""
    item = HandoffItem(
        kind=HandoffKind.DELEGATE, target="A", reason="先让 A 收尾",
    )
    assert item.to_dict()["reason"] == "先让 A 收尾"


def test_handoff_item_is_frozen() -> None:
    item = HandoffItem(kind=HandoffKind.DELEGATE, target="A")
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.target = "B"  # type: ignore[misc]


def test_handoff_kind_enum_rejects_unknown_value() -> None:
    """非法 kind 字符串进 ``HandoffKind`` → ``ValueError`` —— inbound handler
    解析 wire 字段时靠这条保护。``panel`` 留给 P7.3 加。"""
    for bad in ("panel", "nope"):
        with pytest.raises(ValueError):
            HandoffKind(bad)


def test_handoff_kind_value_is_str() -> None:
    assert HandoffKind.DELEGATE.value == "delegate"
    assert HandoffKind.DELEGATE == "delegate"
    assert HandoffKind.REVIEW.value == "review"
    assert HandoffKind.REVIEW == "review"
