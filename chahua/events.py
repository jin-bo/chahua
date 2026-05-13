"""茶话室前端事件 envelope（设计文档 §3.5）。

所有推到前端（CLI / P2.3 WebSocket / P3 Electron）的事件都套同一 envelope：

    {schema_version, room_id, message_id, guest_name, turn_id, seq, type, status, ts, data}

agentao 的原生 :class:`agentao.transport.AgentEvent` **不带** 这些字段（它只知道自己
那个 Agentao 实例在跑一轮），所以 envelope 是茶话室在外层合成的。事件类型的"消息"
与"轮次"边界也是茶话室定义的，与 agentao 的 ``TURN_BEGIN/END`` 不一一对应（agentao
的轮次是 LLM-iteration 级，§3.5 明确不向前端转发）。

**type 与发生位置**：

- ``room_info``：``server.py`` 在 ws 连上时一次性下发，给前端装 sidebar / @ 补全；
  不属于任何 turn（``turn_id`` 为 ``None``）。
- ``turn_start`` / ``turn_end``：orchestrator 合成；turn 对应一次 pick（top-1~2 抢话）。
- ``message_start`` / ``message_end``：``TeaGuest.speak()`` 的 ``try / except / finally``
  外层合成 —— 保证 start 必有 end（status 三选一）。
- ``message_delta`` / ``guest_thinking`` / ``tool_start`` / ``tool_complete``：
  ``ChahuaTransport`` 从对应 agentao 事件转译。

**turn_id 可空**：从 P3.2.2 起，连接级事件（``room_info`` 等）``turn_id=None``；
其余事件路径不变，仍由 orchestrator/transport 在 bind 时塞具体值。

**room_id**：P2.2 没有稳定 room id（room.toml 没字段），envelope 里塞 ``room.name``。
P4 加 ``[room].id`` 后换稳定 ID（display name 可变；envelope 路由不该依赖）。

**id mint**：:func:`new_message_id` 与 :func:`new_turn_id` 同根，由茶话室所有 ID 来源
集中在本模块；``room.py`` 也走这个 helper（不再各自 ``secrets.token_hex``）。
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Optional

_log = logging.getLogger(__name__)


# Envelope wire 版本。前端反序列化时若 != 期望值应回退到兼容模式或拒绝（P2.3 起生效）。
SCHEMA_VERSION = 1


class ChahuaEventType(str, Enum):
    """前端事件类型。值是 JSON-friendly 字符串，envelope 落盘 / 上线时直接用 ``.value``。"""

    ROOM_INFO = "room_info"
    TURN_START = "turn_start"
    MESSAGE_START = "message_start"
    MESSAGE_DELTA = "message_delta"
    MESSAGE_END = "message_end"
    TURN_END = "turn_end"
    GUEST_THINKING = "guest_thinking"
    TOOL_START = "tool_start"
    TOOL_COMPLETE = "tool_complete"


# status 三态。仅 ``message_end`` / ``turn_end`` 有意义；其余事件一律 OK 占位。
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"


def _new_id(prefix: str, n_bytes: int = 10) -> str:
    return f"{prefix}_{secrets.token_hex(n_bytes)}"


def new_turn_id() -> str:
    """``turn_<10字节 hex>`` —— 与 :func:`new_message_id` 同长度，方便扫读。"""
    return _new_id("turn")


def new_message_id() -> str:
    """``msg_<10字节 hex>``。前端 envelope 与 transcript.jsonl 共用同一 ID。"""
    return _new_id("msg")


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class ChahuaEnvelope:
    """前端事件 envelope。一条流式消息共享同一 ``message_id``；一次 pick 共享同一 ``turn_id``。

    ``seq`` 仅在 ``message_end(status=ok)`` 时有意义（其它情况为 ``None``）—— 它指向
    transcript.jsonl 里这条发言的房间序号；失败 / 取消的发言不入 transcript。
    ``guest_name`` 在 ``turn_start`` / ``turn_end`` 这种轮级事件上为 ``None``（轮维度
    跨多位茶客，没单一归属；前端按 ``turn_id`` 聚合）。
    """

    room_id: str
    turn_id: Optional[str]
    guest_name: Optional[str]
    message_id: Optional[str]
    type: ChahuaEventType
    status: str = STATUS_OK
    ts_ms: int = field(default_factory=_now_ms)
    data: Mapping[str, Any] = field(default_factory=dict)
    seq: Optional[int] = None

    def to_dict(self) -> dict:
        """JSON-safe wire 形态（P2.3 WebSocket 推到前端时直接 ``json.dumps``）。"""
        return {
            "schema_version": SCHEMA_VERSION,
            "room_id": self.room_id,
            "turn_id": self.turn_id,
            "guest_name": self.guest_name,
            "message_id": self.message_id,
            "type": self.type.value,
            "status": self.status,
            "ts_ms": self.ts_ms,
            "seq": self.seq,
            "data": dict(self.data),
        }


EnvelopeSink = Callable[[ChahuaEnvelope], None]
"""消费 envelope 的回调。CLI / WebSocket server / 测试 fixture 都实现这个签名。

约束：sink **不应抛异常**。:func:`emit_to_sink` 把 sink 异常吞掉 + WARN（agentao
transport 契约要求 on_event 不抛），但 sink 端应自管错误以保观察性。
"""


def emit_to_sink(sink: EnvelopeSink, env: ChahuaEnvelope) -> None:
    """把 envelope 投到 ``sink``；sink 抛异常一律 WARN + 吞 —— 所有 emit 路径必须走这里
    才能保住"sink 不能把生产者挂掉"的契约。
    """
    try:
        sink(env)
    except Exception:
        _log.exception("envelope sink raised on %s; dropped", env.type.value)


# 一个永远不抛的占位 sink，给"无 UI 消费者"的调用方用（测试 / 程序化驱动）。
NOOP_SINK: EnvelopeSink = lambda _env: None
