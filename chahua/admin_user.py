"""USER.md / 头像 PNG mutator + raw ``room.toml`` 编辑器入口。

P6.x 重构：从 :mod:`chahua.admin` 抽出。USER.md 与头像走"user_data_root 优先 +
room override 兜底"的写盘路径；raw ``room.toml`` 编辑走 :func:`update_room_toml`
（结构化 mutator 走 :mod:`chahua.admin_room` / :mod:`chahua.admin_guest`，两条路径
共用 :func:`chahua.admin_room._write_text_and_validate` 的"写 + 校验 + 回滚旧 bytes"
helper）。

外部 import 通过 :mod:`chahua.admin` reexport，老路径全部保留可用。
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Optional

from ._paths import Paths
from ._persist import write_bytes_atomic, write_text_atomic
from .admin_room import _write_text_and_validate
from .config import RoomConfig, read_avatar_data_uri


# USER.md 体量上限：64KB —— USER.md 本质是几段偏好 + 自我介绍，正常用户写 1KB 不到。
# 设上限是防御浏览器端 textarea 被脚本灌进去 / 误粘整个文档；超就拒，不静默截断。
_USER_MD_MAX_BYTES = 64 * 1024
# 头像上限：1.5MB（前端已经 canvas 压缩到 256px PNG，正常 ~50KB；上限是兜底防御客户端
# 没压就直传整张原图 + base64 体积膨胀 4/3 倍）。
_AVATAR_MAX_BYTES = 1_500_000
# PNG 文件头 magic bytes（RFC 2083 §3.1）。`with_suffix(".png")` 的搜索约定让我们只接
# 受 PNG —— 其他格式（JPEG / WebP）写进 USER.png 会让 sidebar 渲染 broken image。
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# room.toml 体量上限：64KB。正常房间 < 2KB；上限挡误粘整个文档。
_ROOM_TOML_MAX_BYTES = 64 * 1024


def _user_md_target(paths: Paths, source: Optional[Path]) -> Path:
    """USER.md 写盘目标：优先沿用 load_user_md 命中的 source（保留用户已有布局，
    比如 room 级 USER.md 或 explicit override）；都没有则落到 user_data_root/USER.md。
    """
    return source if source is not None else paths.user_data_root / "USER.md"


def update_user_md(paths: Paths, content: str, *, source: Optional[Path] = None) -> Path:
    """覆盖 USER.md 内容，返回实际写入的路径。

    内容超 :data:`_USER_MD_MAX_BYTES` → ValueError（防误粘整文档）。
    tmp + rename 保证写一半被 kill 不会留下半截 md。
    """
    encoded = content.encode("utf-8")
    if len(encoded) > _USER_MD_MAX_BYTES:
        raise ValueError(
            f"USER.md 太大（{len(encoded)} bytes，上限 {_USER_MD_MAX_BYTES}）"
        )
    target = _user_md_target(paths, source)
    write_text_atomic(target, content)
    return target


def parse_png_data_uri(data_uri: str) -> bytes:
    """`data:image/png;base64,...` → raw PNG bytes。

    严格验：必须是 PNG data URI、base64 合法、magic bytes 命中、体积在上限内。
    任何一项不过 → ValueError，调用方决定怎么 emit 给前端。
    """
    prefix = "data:image/png;base64,"
    if not data_uri.startswith(prefix):
        raise ValueError("仅接受 PNG data URI（前端 canvas.toDataURL('image/png')）")
    try:
        png = base64.b64decode(data_uri[len(prefix):], validate=True)
    except binascii.Error as e:
        raise ValueError(f"base64 解码失败：{e}") from e
    if len(png) > _AVATAR_MAX_BYTES:
        raise ValueError(
            f"头像太大（{len(png)} bytes，上限 {_AVATAR_MAX_BYTES}）"
        )
    if not png.startswith(_PNG_MAGIC):
        raise ValueError("文件头不是 PNG（magic bytes 不匹配）")
    return png


def update_room_toml(room_dir: Path, content: str, *, paths: Paths) -> RoomConfig:
    """覆盖 ``room_dir/room.toml`` 全文 + 重加载校验；失败回滚到旧文本。

    与 add_guest / update_guest_permission 那条「结构化 mutator」路径并存 —— 这里是
    「raw editor」入口（前端 textarea 直编），让用户能改 [room] / [[guest]] 任何字段，
    包括 add/remove_guest 无法直接表达的 rules 修订。底层共用 :func:`_write_text_and_validate`
    的"写 + 校验 + 回滚旧 bytes" helper。
    """
    encoded = content.encode("utf-8")
    if len(encoded) > _ROOM_TOML_MAX_BYTES:
        raise ValueError(
            f"room.toml 太大（{len(encoded)} bytes，上限 {_ROOM_TOML_MAX_BYTES}）"
        )
    return _write_text_and_validate(room_dir, content, paths=paths)


def update_user_avatar(
    paths: Paths, png_bytes: bytes, *, source: Optional[Path] = None
) -> Path:
    """覆盖用户头像 PNG，返回实际写入的路径。

    sibling 约定：`USER.md` 同名 sibling `.png`。source 缺省时落 user_data_root/USER.png。
    写完清 :func:`read_avatar_data_uri` 的 lru_cache —— 否则 sidebar 还会显示旧编码。
    """
    md_target = _user_md_target(paths, source)
    target = md_target.with_suffix(".png")
    write_bytes_atomic(target, png_bytes)
    # 没法按 key evict（lru_cache API 限制），全清最便宜：6 张图（5 茶客 + 1 用户）
    # 重 base64 不到 1ms。前提是用户头像也走同一 cache —— 见 user_md.py 的委托。
    read_avatar_data_uri.cache_clear()
    return target
