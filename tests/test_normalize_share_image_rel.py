"""P13：``_normalize_share_image_rel`` 纯字符串校验 —— 接受 canonical 图片 rel，
拒非 share/ 前缀 / 非图扩展名 / 空段 / 穿越 / 绝对路径 / 反斜杠。
"""

from __future__ import annotations

import pytest

from chahua.image_input import _normalize_share_image_rel


@pytest.mark.parametrize("rel", [
    "share/x.png",
    "share/a.jpg",
    "share/a.jpeg",
    "share/a.gif",
    "share/a.webp",
    "share/sub/dir/pic.PNG",  # 扩展名大小写无关
    "share/有图.png",          # 非 ASCII 文件名
    "share/.hidden.png",       # 有 stem 的 dotfile（不过度拒）
])
def test_accepts_valid_image_rels(rel: str) -> None:
    assert _normalize_share_image_rel(rel) == rel.strip()


@pytest.mark.parametrize("rel", [
    "x.png",            # 无 share/ 前缀
    "tasks/x.png",      # 非 share/ 根
    "share/a.txt",      # 非图扩展名
    "share/a.svg",      # SVG 不作视觉输入
    "share/noext",      # 无扩展名
    "share/.png",       # 纯扩展名 dotfile（空 stem）
    "share/sub/.jpg",   # 子目录下的空 stem dotfile
    "share/x.",         # 尾点、空扩展名
    "share//x.png",     # 空段
    "share/./x.png",    # . 段
    "share/../x.png",   # .. 段（穿越）
    "/abs/x.png",       # 绝对路径
    "/share/x.png",     # 绝对路径（即便含 share）
    "share\\x.png",     # 反斜杠
    "",                 # 空串
    "   ",              # 全空白
    "share/",           # 只有前缀
])
def test_rejects_invalid_rels(rel: str) -> None:
    assert _normalize_share_image_rel(rel) is None


@pytest.mark.parametrize("rel", [None, 123, b"share/x.png", ["share/x.png"], {}])
def test_rejects_non_str(rel: object) -> None:
    assert _normalize_share_image_rel(rel) is None


def test_strips_surrounding_whitespace() -> None:
    assert _normalize_share_image_rel("  share/x.png  ") == "share/x.png"
