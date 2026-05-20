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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

from ._persist import append_jsonl, read_jsonl_skip_bad
from .events import new_message_id, now_ms

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

    task_id: Optional[str] = None
    """消息所属任务的 ID；``None`` = 房间级闲聊。旧 transcript.jsonl 行无此字段（加载为
    ``None``），与"新建任务后才开始挂 task_id"的零侵入迁移口径一致（docs §7.1 / §10）。"""

    def to_jsonl_dict(self) -> dict:
        """落盘形态。字段顺序固定，方便人眼扫 jsonl。

        ``task_id == None`` 时**不写**这个键 —— 旧 transcript 兼容、未启用任务功能的房间
        不会因为加一列空字段把 jsonl 行宽炸开一倍。
        """
        out: dict = {
            "seq": self.seq,
            "speaker_id": self.speaker_id,
            "message_id": self.message_id,
            "ts_ms": self.ts_ms,
            "text": self.text,
        }
        if self.task_id is not None:
            out["task_id"] = self.task_id
        return out

    @classmethod
    def from_jsonl(cls, obj: dict) -> Optional["Message"]:
        """从 jsonl 行重建。必需字段缺失 / 类型错 → ``None`` + WARN（行被跳过）。

        ``task_id`` 可缺（旧行）或为 ``null``；非 str 类型 → WARN 后视为 ``None`` —— 不
        因单消息的 tag 字段不合法让这条历史发言消失（沿用 P5.1 落盘宽容）。
        """
        try:
            seq = int(obj["seq"])
            speaker_id = str(obj["speaker_id"])
            text = str(obj["text"])
            ts_ms = int(obj["ts_ms"])
            message_id = str(obj["message_id"])
        except (KeyError, TypeError, ValueError):
            _log.warning("skip malformed transcript record: %r", obj)
            return None
        task_id_raw = obj.get("task_id")
        task_id: Optional[str] = None
        if task_id_raw is not None:
            if isinstance(task_id_raw, str):
                task_id = task_id_raw
            else:
                _log.warning(
                    "message %s: task_id=%r 非 str / null，忽略",
                    message_id, task_id_raw,
                )
        return cls(
            seq=seq,
            speaker_id=speaker_id,
            text=text,
            ts_ms=ts_ms,
            message_id=message_id,
            task_id=task_id,
        )


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
        task_id: Optional[str] = None,
    ) -> Message:
        """追加一条发言，返回带 seq / message_id 的 :class:`Message`。

        ``speaker_id`` 必须在 :attr:`participants` 里 —— 不允许凭空冒出一个名字，
        这是防 "茶客 A 在 transcript 里假冒茶客 B" 的最便宜兜底（注入打分以外的另一类风险）。

        ``message_id`` 可由调用方传入 —— :meth:`TeaGuest.speak` 在 envelope
        message_start 时就分配 ID，要求与最终落 transcript 的 ID 一致以便前端把
        流式 chunk 与持久化 record 串起来。``None`` 时本函数兜底分配。

        ``task_id`` 把消息归到某个任务；``None`` = 房间级闲聊。orchestrator / server 在
        append 入口按 ``TasksStore.active_task_id`` 自动取值（docs §4.4），:class:`Room`
        本身不知道任务存在 —— 只是把传入值原样落盘。

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
            ts_ms=now_ms(),
            message_id=message_id or new_message_id(),
            task_id=task_id,
        )
        self._messages.append(msg)
        if self.transcript_path is not None:
            append_jsonl(self.transcript_path, msg.to_jsonl_dict())
        return msg

    def clear(self) -> None:
        """清空 transcript —— 内存 + 落盘文件。``_participants`` 不动（房间还在，茶客
        还在场，只是历史话都没了）。

        落盘走 ``open("w")`` 截断而非 ``unlink`` —— 与构造期 ``mkdir`` 同样的简化口径，
        下一次 ``append`` 不用再判文件是否存在；同时也避免 Windows 下文件被监控工具占用时
        ``unlink`` 失败。
        """
        self._messages.clear()
        if self.transcript_path is not None:
            # 截断为空文件，下次 append_jsonl 直接写一行。
            with self.transcript_path.open("w", encoding="utf-8"):
                pass

    def messages_since(self, last_seq: int) -> list[Message]:
        """返回 ``seq > last_seq`` 的所有消息。``last_seq=0`` 表示"从头"。"""
        # seq 从 1 起密集递增 → _messages[i].seq == i + 1，直接切片 O(k)。
        offset = max(last_seq, 0)
        return list(self._messages[offset:])

    def last_message(self) -> Optional[Message]:
        """最后一条消息；空房间返回 None。"""
        return self._messages[-1] if self._messages else None

    def message_by_id(self, message_id: str) -> Optional[Message]:
        """按 ``message_id`` 线性查找；未命中返回 ``None``。

        P7.2 review（请审）用户手点低频动作，O(n) 扫一遍 transcript 远低于"维护
        ``_by_message_id`` dict 在 append / clear / load 三处同步"的冗余状态成本
        （CLAUDE.md "能 derive 就不缓存" 口径）。
        """
        for m in self._messages:
            if m.message_id == message_id:
                return m
        return None

    @property
    def latest_seq(self) -> int:
        """最后一条消息的 seq；空房间返回 0。"""
        return self._messages[-1].seq if self._messages else 0

    def __len__(self) -> int:
        return len(self._messages)


def format_messages(
    messages: Iterable[Message], display_for: Mapping[str, str]
) -> str:
    """渲染 ``<message>\\n<display_name> 说：<text>\\n</message>`` 块序列。

    所有喂养 prompt（onboarding 末段原文 / 增量 / 打分 / 摘要）走同一格式 —— 茶客
    人格卡（``personas/*.md``）里也明示 "X 说：…"格式（内部内容不变，外层多一层
    ``<message>`` 包裹），所以**任何变更要同步检查 personas**。单点定义在这里，避免
    4+ 处复制。

    外层 ``<message>`` 包是边界承重墙：消息 body 可能含 markdown HR（``---``）、H2
    （``## xxx``）等结构化内容，单纯用 ``\\n`` 分隔会让"下一条 `X 说：` 起始"在视觉
    与 tokenization 上都与"上一条 body 内的 markdown"难以区分。XML 包外层 + Markdown
    渲内层是茶话室的标准口径（CLAUDE.md 关键不变量）。
    """
    return "\n".join(
        f"<message>\n{display_for.get(m.speaker_id, m.speaker_id)} 说：{m.text}\n</message>"
        for m in messages
    )
