"""房间 transcript → markdown 导出。

服务端拼好整段 markdown + 安全文件名，渲染端拿到后走 Blob/a-download 下载到用户机器。
**不写盘** —— 导出物只活在用户的 'Downloads/...' 里，与 session 持久化 (transcript.jsonl /
summary.jsonl) 解耦，避免给房间目录留 untracked 副本。

格式：H1 房间名 + 元信息 blockquote + 每条消息 H2 标题（speaker · 时间）+ 原文（markdown
源直接 paste，不重新转义 —— 茶客的代码块 / 列表保留原样）。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable

from .config import RoomConfig
from .room import Message
from .user_md import USER_SPEAKER_ID


# 文件名同名碰撞由浏览器 `<a download>` 的默认 (N) 后缀机制兜底，这里不做去重。
# `\w` 在 Python 3 str 模式下默认 UNICODE，已涵盖 CJK 字符 + 字母 + 数字 + 下划线。
_FS_UNSAFE_RE = re.compile(r"[^\w\-]+")


def _safe_filename_token(name: str) -> str:
    # 比 admin.sanitize_fs_name 严 —— 那条是 FS 写文件的安全边界（denylist 控制字符），
    # 这里是 <a download> 给出的建议名（whitelist 收紧到 ASCII-clean + CJK），跨 OS
    # 兼容更稳。
    cleaned = _FS_UNSAFE_RE.sub("_", name).strip("_")
    return cleaned or "untitled"


def _format_ts(ts_ms: int) -> str:
    # 本地时区显示 —— 导出物给用户自己看，不必走 UTC。秒精度足够（聊天里同秒多条消息
    # 不常见，且 jsonl 的 seq 字段已经隐含因果顺序）。
    return datetime.fromtimestamp(ts_ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S")


def format_room_markdown(
    rc: RoomConfig,
    messages: Iterable[Message],
    user_display_name: str,
) -> tuple[str, str]:
    """返回 ``(filename, content)``。

    filename 形如 ``茶话室-<room_name>-YYYYMMDD-HHMMSS.md`` —— 多次导出不会互相覆盖。
    """
    msgs = list(messages)
    now = datetime.now()
    lines: list[str] = [f"# 房间「{rc.name}」", ""]
    if rc.topic:
        lines.append(f"> 主题：{rc.topic}")
    lines.append(f"> 导出时间：{now.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"> 消息数：{len(msgs)}")
    lines.append("")
    lines.append("---")
    lines.append("")
    for m in msgs:
        speaker = (
            user_display_name if m.speaker_id == USER_SPEAKER_ID else m.speaker_id
        )
        lines.append(f"## {speaker} · {_format_ts(m.ts_ms)}")
        lines.append("")
        lines.append(m.text)
        lines.append("")
    content = "\n".join(lines)
    filename = (
        f"茶话室-{_safe_filename_token(rc.name)}-"
        f"{now.strftime('%Y%m%d-%H%M%S')}.md"
    )
    return filename, content
