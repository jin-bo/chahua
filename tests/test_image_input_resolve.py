"""P13：``resolve_images`` IO 行为 —— 懒读 base64 + ext→mime、限额、缺文件 / 逃逸跳过、
share_dir=None 兜底、symlink 逃逸拦截。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from agentao.media_limits import MAX_IMAGE_BYTES, MAX_IMAGES_PER_TURN

from chahua.image_input import resolve_images


def _make_png(share_dir: Path, name: str, data: bytes = b"\x89PNG\r\n\x1a\n") -> None:
    (share_dir / name).parent.mkdir(parents=True, exist_ok=True)
    (share_dir / name).write_bytes(data)


def test_resolves_single_image(tmp_path: Path) -> None:
    share = tmp_path / "share"
    share.mkdir()
    _make_png(share, "x.png", b"hello")
    out = resolve_images(share, ["share/x.png"])
    assert out == [{
        "data": base64.b64encode(b"hello").decode("ascii"),
        "mimeType": "image/png",
        "_source": "share/x.png",
    }]


def test_source_is_canonical_rel(tmp_path: Path) -> None:
    """``_source`` 直接用 canonical rel（带 share/ 前缀）—— 与 prompt 文本标记口径一致。"""
    share = tmp_path / "share"
    share.mkdir()
    _make_png(share, "p.jpg")
    out = resolve_images(share, ["share/p.jpg"])
    assert out[0]["_source"] == "share/p.jpg"
    assert out[0]["mimeType"] == "image/jpeg"


def test_empty_rels_returns_empty(tmp_path: Path) -> None:
    assert resolve_images(tmp_path / "share", []) == []


def test_share_dir_none_with_rels_returns_empty(tmp_path: Path) -> None:
    """share_dir=None + 非空 rels → [] + WARN，不炸。"""
    assert resolve_images(None, ["share/x.png"]) == []


def test_share_dir_none_empty_rels(tmp_path: Path) -> None:
    assert resolve_images(None, []) == []


def test_missing_file_skipped(tmp_path: Path) -> None:
    share = tmp_path / "share"
    share.mkdir()
    assert resolve_images(share, ["share/nope.png"]) == []


def test_invalid_rel_skipped(tmp_path: Path) -> None:
    share = tmp_path / "share"
    share.mkdir()
    _make_png(share, "x.png")
    # 非图 / 非 share 前缀的 rel 被 normalize 过滤掉。
    out = resolve_images(share, ["share/x.txt", "x.png", "share/x.png"])
    assert [o["_source"] for o in out] == ["share/x.png"]


def test_zero_byte_image_skipped(tmp_path: Path) -> None:
    """0 字节图片跳过 —— 空 base64 会让 agentao 预校验抛 ValueError、整条 speak 失败。"""
    share = tmp_path / "share"
    share.mkdir()
    (share / "empty.png").write_bytes(b"")
    assert resolve_images(share, ["share/empty.png"]) == []


def test_oversize_image_rejected(tmp_path: Path) -> None:
    share = tmp_path / "share"
    share.mkdir()
    _make_png(share, "big.png", b"\x00" * (MAX_IMAGE_BYTES + 1))
    assert resolve_images(share, ["share/big.png"]) == []


def test_truncates_over_limit(tmp_path: Path) -> None:
    share = tmp_path / "share"
    share.mkdir()
    rels = []
    for i in range(MAX_IMAGES_PER_TURN + 3):
        _make_png(share, f"i{i}.png", bytes([i % 256]))
        rels.append(f"share/i{i}.png")
    out = resolve_images(share, rels)
    assert len(out) == MAX_IMAGES_PER_TURN


def test_symlink_escape_blocked(tmp_path: Path) -> None:
    """``share/x.png`` 本身是指向房间外的符号链接 → resolve() + relative_to 拦下、跳过。"""
    share = tmp_path / "share"
    share.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.png"
    secret.write_bytes(b"topsecret")
    link = share / "x.png"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    assert resolve_images(share, ["share/x.png"]) == []


def test_symlink_within_share_allowed(tmp_path: Path) -> None:
    """share/ 内部的软链接（resolve 后仍落 share/ 子树）放行 —— 不误杀合法软链拼装。"""
    share = tmp_path / "share"
    share.mkdir()
    real = share / "real.png"
    real.write_bytes(b"img")
    link = share / "x.png"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    out = resolve_images(share, ["share/x.png"])
    assert len(out) == 1
    assert out[0]["_source"] == "share/x.png"
