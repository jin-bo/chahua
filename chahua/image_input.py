"""P13 视觉图像输入 helper —— 把本轮用户图从 ``share/`` rel 懒读成 agentao ``images``。

两函数职责分明（见 docs/P13-多模态图像输入.md 不变量）：

- :func:`_normalize_share_image_rel` —— **纯字符串校验、无 IO**。要求 ``share/`` 前缀、
  扩展名在白名单、路径段非 ``""`` / ``.`` / ``..``、无绝对路径 / 反斜杠；任一不满足
  返回 ``None``。inbound 筛图 + resolve 双点复用此校验。
- :func:`resolve_images` —— **模块函数、负责 IO**（读盘 / 查 size / base64 / WARN）。
  ``TeaGuest.speak`` 调 ``resolve_images(self._share_dir, images_rel)``，不是 TeaGuest
  method —— server inbound 与 guest 跨层共用，小模块是最小共享面，不让 server import guest。

承重点（与不变量对齐）：

- **降级归 agentao**：本模块只把 ``{data, mimeType, _source}`` 列表交给 ``arun(images=)``；
  模型拒图后「换成文本引用并重试」由 agentao ``_runner.py`` / ``_retry.py`` 完成。本模块
  不写 ``_is_image_unsupported`` 同义物、不维护视觉能力表。
- **base64 懒读不入库**：bytes 只在 speak 时从 ``share/`` 实路径现读现传，绝不进
  transcript / envelope。
- **`_source` 直接用 canonical rel**（``share/x.png``）—— 让 agentao 降级文本
  ``- share/x.png (image/png)`` 与 prompt 里 ``<./share/x.png>`` 标记口径一致。
- **读盘点必做 symlink 逃逸检查**：段形校验是字符串层防穿越，symlink 逃逸是文件系统层
  防穿越（chahua 的 ``share/`` 本就是软链拼装），两层都要 —— 与 ``download_file`` 同口径
  （``resolve()`` 两侧 + ``relative_to``）。
- **限额对齐 agentao**：``MAX_IMAGE_BYTES`` / ``MAX_IMAGES_PER_TURN`` 直接 import，不另立常量。
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Iterable, Optional

from agentao.media_limits import MAX_IMAGE_BYTES, MAX_IMAGES_PER_TURN

_log = logging.getLogger(__name__)

# 与前端 preview 内嵌白名单一致（chat_view.js）。SVG 不作视觉输入（可内嵌 ``<script>``）。
_EXT_TO_MIME: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

_SHARE_PREFIX = "share/"


def _normalize_share_image_rel(rel: object) -> Optional[str]:
    """纯字符串校验：合法的 ``share/<...>.<ext>`` 图片 rel → canonical 形态；否则 ``None``。

    拒：非 str；无 ``share/`` 前缀；扩展名不在白名单；含绝对路径（``/`` 开头）/ 反斜杠；
    任一路径段为 ``""`` / ``.`` / ``..``（防空段 + 穿越）。**无 IO** —— 不碰文件系统，
    inbound 筛图（同步接帧上下文）与 ``resolve_images`` 读盘前都复用此校验。

    返回的 canonical rel 仍带 ``share/`` 前缀（``share/x.png``）—— ``_source`` 直接用它，
    与 prompt 里 ``<./share/x.png>`` 标记口径一致。
    """
    if not isinstance(rel, str):
        return None
    s = rel.strip()
    if not s or "\\" in s or s.startswith("/"):
        return None
    if not s.startswith(_SHARE_PREFIX):
        return None
    parts = s.split("/")
    # 每段非空、非 ``.`` / ``..``（``share//x`` / ``share/./x`` / ``share/../x`` 全拒）。
    if any(p in ("", ".", "..") for p in parts):
        return None
    # 末段必须形如 ``<非空 stem>.<白名单扩展名>``。``rpartition`` 取最后一个 ``.``：
    # 空 stem（``share/.png`` 这种纯扩展名 dotfile）/ 无扩展名（``share/noext``）/
    # 非图扩展名（``share/x.txt``）全拒。
    stem, dot, ext = parts[-1].rpartition(".")
    if not dot or not stem or ext.lower() not in _EXT_TO_MIME:
        return None
    return s


def resolve_images(
    share_dir: Optional[Path], rels: Iterable[str],
) -> list[dict[str, str]]:
    """把本轮图片 rel 列表懒读成 agentao ``images`` 形态 ``[{data, mimeType, _source}]``。

    每项：``data`` = base64 字符串，``mimeType`` 由扩展名映射，``_source`` = canonical rel。

    防御逐层（任一失败跳过该图、绝不炸 speak）：

    - ``share_dir is None`` 且 ``rels`` 非空 → 返回 ``[]`` + WARN 一次（裸构 TeaGuest /
      异常装配的兼容路径，视觉输入退化为无图）—— 同 ``active_guest_names is None`` 跳过口径。
    - rel 经 :func:`_normalize_share_image_rel` 校验失败 → 跳过。
    - 剥 ``share/`` 前缀再拼（``rel`` 是 ``share/x.png``，``share_dir`` 已是
      ``<room>/share/`` —— 直接拼会成 ``<room>/share/share/x.png``）。
    - symlink 逃逸：``target.resolve()`` ∉ ``share_dir.resolve()`` 子树 → 跳过。
    - 缺文件 / 非普通文件 → 跳过；单图 > ``MAX_IMAGE_BYTES`` → 拒 + WARN。
    - 整轮累计 > ``MAX_IMAGES_PER_TURN`` 张 → 截断 + WARN（不静默）。
    """
    rels = list(rels)
    if not rels:
        return []
    if share_dir is None:
        _log.warning("resolve_images: share_dir is None，本轮 %d 张图退化为无图", len(rels))
        return []

    share_root = share_dir.resolve()
    out: list[dict[str, str]] = []
    for rel in rels:
        if len(out) >= MAX_IMAGES_PER_TURN:
            _log.warning(
                "resolve_images: 单轮图片超过上限 %d，截断剩余", MAX_IMAGES_PER_TURN,
            )
            break
        norm = _normalize_share_image_rel(rel)
        if norm is None:
            _log.warning("resolve_images: 跳过非法图片 rel %r", rel)
            continue
        sub = norm[len(_SHARE_PREFIX):]  # normalize 已保证前缀存在
        try:
            target = (share_dir / sub).resolve()
            # 两侧都 resolve（share_dir 经软链接到达）+ relative_to —— 文件系统层防穿越。
            target.relative_to(share_root)
        except (OSError, ValueError):
            _log.warning("resolve_images: 路径解析 / 逃逸检查失败，跳过 %r", rel)
            continue
        if not target.is_file():
            _log.warning("resolve_images: 文件缺失或非普通文件，跳过 %r", rel)
            continue
        try:
            size = target.stat().st_size
        except OSError:
            _log.warning("resolve_images: stat 失败，跳过 %r", rel)
            continue
        if size == 0:
            # 上传层允许 0 字节文件落 share/。空 ``data`` 会让 agentao 的图片预校验抛
            # ValueError（``_runner.py``：``not data`` → raise），且这不是「模型不支持
            # 视觉」错、走不到 reactive 文本回退 —— 整条 speak 会以 message_end(error)
            # 收场。当作无效图跳过，退回 ``<./share/...>`` 文本标记（已在 prompt 里）。
            _log.warning("resolve_images: 图片 %r 为空文件（0 字节），跳过", rel)
            continue
        if size > MAX_IMAGE_BYTES:
            _log.warning(
                "resolve_images: 图片 %r %d 字节超过上限 %d，拒",
                rel, size, MAX_IMAGE_BYTES,
            )
            continue
        try:
            data = target.read_bytes()
        except OSError:
            _log.warning("resolve_images: 读盘失败，跳过 %r", rel)
            continue
        ext = norm.rsplit(".", 1)[-1].lower()
        out.append({
            "data": base64.b64encode(data).decode("ascii"),
            "mimeType": _EXT_TO_MIME[ext],
            "_source": norm,
        })
    return out
