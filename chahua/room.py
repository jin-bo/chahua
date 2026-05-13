"""Room —— 房间公共 transcript（设计文档 §3.2 / §3.7 / §3.8）。

P0 是纯内存 transcript。P2.1 加 ``transcript.jsonl`` 落盘 / 启动加载 —— 设计要点：

- ``speaker_id`` 存**稳定 ID**（``"user"`` / 茶客名），渲染时再查 display_name。
  改 USER.md 改名不污染历史。
- ``seq`` 单调递增，从 1 开始。``guest_cursor`` 用它判断"上次喂到哪了"。
- ``message_id`` 是茶话室自己分配的、整条流式消息共享的 ID（前端 envelope 用，§3.5.1）；
  在 :meth:`append` 阶段就生成，不依赖外部传入。
- **加载时跳过坏行**（最后一行被截断是常见情况），见 :mod:`chahua._persist`。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

from ._persist import append_jsonl, read_jsonl_skip_bad
from .events import new_message_id

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Message:
    """房间公共 transcript 的一条记录。"""

    seq: int
    speaker_id: str
    """稳定 ID。`"user"` 或茶客 name。**不存 display_name**，渲染时再解析（§3.8.3）。"""

    text: str
    ts_ms: int
    """毫秒级时间戳，前端排序用。"""

    message_id: str

    def to_jsonl_dict(self) -> dict:
        """落盘形态。字段顺序固定，方便人眼扫 jsonl。"""
        return {
            "seq": self.seq,
            "speaker_id": self.speaker_id,
            "message_id": self.message_id,
            "ts_ms": self.ts_ms,
            "text": self.text,
        }

    @classmethod
    def from_jsonl(cls, obj: dict) -> Optional["Message"]:
        """从 jsonl 行重建。字段缺失 / 类型错 → ``None`` + WARN（行被跳过）。"""
        try:
            return cls(
                seq=int(obj["seq"]),
                speaker_id=str(obj["speaker_id"]),
                text=str(obj["text"]),
                ts_ms=int(obj["ts_ms"]),
                message_id=str(obj["message_id"]),
            )
        except (KeyError, TypeError, ValueError):
            _log.warning("skip malformed transcript record: %r", obj)
            return None


@dataclass
class Room:
    """房间。transcript 在内存里维护，可选同步落盘到 ``transcript_path``。"""

    name: str
    """房间显示名，如「深夜茶话室」。"""

    topic: str = ""
    rules: str = ""
    """房间规则的自由文本，注入 onboarding 用。"""

    transcript_path: Optional[Path] = None
    """``rooms/<id>/transcript.jsonl``。``None`` = 纯内存（测试 / 早期 CLI）。"""

    _messages: list[Message] = field(default_factory=list, init=False, repr=False)
    _participants: list[str] = field(default_factory=list, init=False, repr=False)
    """参与者顺序保留：用户在最前（speaker_id="user"），茶客按加入顺序。"""

    def __post_init__(self) -> None:
        if self.transcript_path is None:
            return
        # transcript.jsonl 一次性加载到内存（设计文档 §3.7 已说明数据本地明文）。
        # 一次性 mkdir 而不是每次 append 都 mkdir(exist_ok=True)。
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        self._load_from_disk()

    # ── 持久化加载 ──────────────────────────────────────────────────────────

    def _load_from_disk(self) -> None:
        """从 ``transcript_path`` 读已有发言，按 seq 升序填进 ``_messages``。

        非严格 seq 校验：jsonl 落盘顺序就是 seq 顺序（append-only + 单进程），所以
        正常情况无须排序；但兼容：如果发现 seq 乱序或重复，按 seq 排序去重，最后一条
        留下。重叠的 ``message_id`` 不再判重（罕见到不值得处理）。

        **不**自动把历史 speaker 加进 ``_participants`` —— 若 room.toml 换了茶客，
        旧名字不该突然回到 onboarding 的 "当前在场" 里（也不该绕过 append() 的
        防假冒校验）。调用方负责按当前 roster 调 :meth:`add_participant`。
        历史消息里那些不在当前 participants 里的 speaker_id，渲染时走 fallback
        （:func:`format_messages` 用稳定 ID 字面显示）。
        """
        assert self.transcript_path is not None
        by_seq: dict[int, Message] = {}
        for obj in read_jsonl_skip_bad(self.transcript_path):
            m = Message.from_jsonl(obj)
            if m is not None:
                by_seq[m.seq] = m
        if by_seq:
            self._messages = [by_seq[s] for s in sorted(by_seq)]

    # ── 参与者管理 ──────────────────────────────────────────────────────────

    def add_participant(self, speaker_id: str) -> None:
        """把一个稳定 ID 加进参与者列表。重复加无副作用（幂等）。"""
        if speaker_id not in self._participants:
            self._participants.append(speaker_id)

    @property
    def participants(self) -> tuple[str, ...]:
        """当前在场的稳定 ID 列表（顺序保留）。返回 tuple 避免外部改。"""
        return tuple(self._participants)

    # ── transcript 读写 ─────────────────────────────────────────────────────

    def append(
        self,
        speaker_id: str,
        text: str,
        *,
        message_id: Optional[str] = None,
    ) -> Message:
        """追加一条发言，返回带 seq / message_id 的 :class:`Message`。

        ``speaker_id`` 必须在 :attr:`participants` 里 —— 不允许凭空冒出一个名字，
        这是防 "茶客 A 在 transcript 里假冒茶客 B" 的最便宜兜底（注入打分以外的另一类风险）。

        ``message_id`` 可由调用方传入 —— :meth:`TeaGuest.speak` 在 envelope
        message_start 时就分配 ID，要求与最终落 transcript 的 ID 一致以便前端把
        流式 chunk 与持久化 record 串起来。``None`` 时本函数兜底分配。

        ``transcript_path`` 非空时同步追加一行 jsonl；写失败抛 OSError 让上层看见 ——
        持久化掉队会让重启后游标错位，比"假装写成功"诚实。
        """
        if speaker_id not in self._participants:
            raise ValueError(
                f"speaker_id={speaker_id!r} 不在房间参与者 {self._participants} 中；"
                "请先 add_participant"
            )

        msg = Message(
            seq=len(self._messages) + 1,
            speaker_id=speaker_id,
            text=text,
            ts_ms=int(time.time() * 1000),
            message_id=message_id or new_message_id(),
        )
        self._messages.append(msg)
        if self.transcript_path is not None:
            append_jsonl(self.transcript_path, msg.to_jsonl_dict())
        return msg

    def messages_since(self, last_seq: int) -> list[Message]:
        """返回 ``seq > last_seq`` 的所有消息。``last_seq=0`` 表示"从头"。"""
        # seq 从 1 起密集递增 → _messages[i].seq == i + 1，直接切片 O(k)。
        offset = max(last_seq, 0)
        return list(self._messages[offset:])

    def last_message(self) -> Optional[Message]:
        """最后一条消息；空房间返回 None。"""
        return self._messages[-1] if self._messages else None

    @property
    def latest_seq(self) -> int:
        """最后一条消息的 seq；空房间返回 0。"""
        return self._messages[-1].seq if self._messages else 0

    def __len__(self) -> int:
        return len(self._messages)


def format_messages(
    messages: Iterable[Message], display_for: Mapping[str, str]
) -> str:
    """渲染 ``<display_name> 说：<text>`` 多行块。

    所有喂养 prompt（onboarding 末段原文 / 增量 / 打分 / 摘要）走同一格式 —— 茶客
    人格卡（``personas/*.md``）里也明示这格式，所以**任何变更要同步改 personas**。
    单点定义在这里，避免 4+ 处复制。
    """
    return "\n".join(
        f"{display_for.get(m.speaker_id, m.speaker_id)} 说：{m.text}"
        for m in messages
    )
