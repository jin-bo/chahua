"""``chahua/handoff.py`` P7.1.1 / P7.3.1 单测 —— ``HandoffItem`` round-trip + 非法 kind。"""

from __future__ import annotations

import dataclasses

import pytest

from chahua.handoff import (
    HANDOFF_ISSUED_BY_USER,
    MAX_PANEL_TARGETS,
    HandoffItem,
    HandoffKind,
)


def test_round_trip_minimal_delegate() -> None:
    item = HandoffItem(kind=HandoffKind.DELEGATE, target="A")
    d = item.to_dict()
    assert d == {
        "kind": "delegate",
        "target": "A",
        "targets": None,
        "summarizer": None,
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
        "targets": None,
        "summarizer": None,
        "issued_by": HANDOFF_ISSUED_BY_USER,
        "reason": None,
        "review_message_id": "msg_abc",
        "created_at_ms": item.created_at_ms,
    }


def test_round_trip_panel_with_summarizer() -> None:
    """P7.3：panel 项 round-trip —— targets 元组转 list，summarizer 非空。"""
    item = HandoffItem(
        kind=HandoffKind.PANEL, targets=("C", "D", "E"), summarizer="F",
    )
    d = item.to_dict()
    assert d == {
        "kind": "panel",
        "target": None,
        "targets": ["C", "D", "E"],
        "summarizer": "F",
        "issued_by": HANDOFF_ISSUED_BY_USER,
        "reason": None,
        "review_message_id": None,
        "created_at_ms": item.created_at_ms,
    }
    # to_dict() 的 targets 是新 list，不是元组别名（envelope/JSON 改不到队列里的项）。
    assert isinstance(d["targets"], list)
    assert d["targets"] is not item.targets


def test_round_trip_panel_without_summarizer() -> None:
    """P7.3：panel 项可缺省 summarizer —— summarizer 为 None。"""
    item = HandoffItem(kind=HandoffKind.PANEL, targets=("C", "D"))
    d = item.to_dict()
    assert d["kind"] == "panel"
    assert d["targets"] == ["C", "D"]
    assert d["summarizer"] is None


def test_delegate_review_targets_summarizer_default_none() -> None:
    """delegate / review 项 targets / summarizer 恒 None（docs §3.1）。"""
    delegate = HandoffItem(kind=HandoffKind.DELEGATE, target="A")
    assert delegate.targets is None
    assert delegate.summarizer is None
    review = HandoffItem(
        kind=HandoffKind.REVIEW, target="B", review_message_id="m1",
    )
    assert review.targets is None
    assert review.summarizer is None


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
    解析 wire 字段时靠这条保护。"""
    with pytest.raises(ValueError):
        HandoffKind("nope")


def test_handoff_kind_value_is_str() -> None:
    assert HandoffKind.DELEGATE.value == "delegate"
    assert HandoffKind.DELEGATE == "delegate"
    assert HandoffKind.REVIEW.value == "review"
    assert HandoffKind.REVIEW == "review"
    assert HandoffKind.PANEL.value == "panel"
    assert HandoffKind.PANEL == "panel"


def test_max_panel_targets_constant() -> None:
    assert MAX_PANEL_TARGETS == 4
