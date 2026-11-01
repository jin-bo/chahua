"""P20：单条茶客消息内的模型调用记录缓冲（docs/P20-茶客调用消耗取证.md §4–§5）。

:class:`chahua.transport_bridge.ChahuaTransport` 每次 ``bind()`` 持一份，按事件喂：

- ``LLM_RETRY``          → 追加到 ``_pending_retries``（总先于同一次调用的 COMPLETED 到达）
- ``LLM_CALL_COMPLETED`` → 白名单抽取一条调用记录，整批吸收 ``_pending_retries``
- ``COMPACTION_SETTLED`` → 白名单抽取一条压缩记录

**计量单位**是一次「主循环模型调用」= agentao 一次 ``chat_stream()``（重试在其内部），
不是一次 HTTP 请求；``duration_ms`` 含重试与退避，usage 只来自最终响应（§3.1）。

:meth:`finalize` 是唯一收尾点，**幂等**：首次调用把未被吸收的重试补成一条
``status="cancelled"`` 占位、套上限、冻结成快照；之后原样返回同一份（深拷贝），
冻结后到达的事件丢弃 —— 保证 ``message_end.data`` 与 ``turns.jsonl`` 同一份（§5）。

全部值白名单抽取 + 类型清洗：上游新增键不透传，类型不对的值落 ``None``。
"""

from __future__ import annotations

import copy
import math
import threading
from typing import Any, Optional

from agentao.transport import EventType

MAX_LLM_CALLS = 64
MAX_RETRIES_PER_CALL = 8
MAX_COMPACTIONS = 8
MAX_STR_LEN = 300

STATUS_CANCELLED_PLACEHOLDER = "cancelled"

# 值类型：int（拒 bool）/ str / bool / number（int|float，拒 bool）
_INT, _STR, _BOOL, _NUM = "int", "str", "bool", "num"

# 调用记录白名单。``error_*`` 只在有值时写（成功调用省略该键）。
_CALL_FIELDS: tuple[tuple[str, str], ...] = (
    ("status", _STR),
    ("duration_ms", _INT),
    ("first_token_ms", _INT),
    ("first_reasoning_ms", _INT),
    ("finish_reason", _STR),
    ("finish_reason_reported", _BOOL),
    ("prompt_tokens", _INT),
    ("completion_tokens", _INT),
    ("cache_read_tokens", _INT),
    ("cache_creation_tokens", _INT),
    ("reasoning_tokens", _INT),
)
_CALL_ERROR_FIELDS = ("error_class", "error_message")

_RETRY_FIELDS: tuple[tuple[str, str], ...] = (
    ("retry", _INT),
    ("max_retries", _INT),
    ("delay_s", _NUM),
    ("reason", _STR),
)

_COMPACTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("trigger", _STR),
    ("kind", _STR),
    ("reason", _STR),
    ("status", _STR),
    ("pre_msgs", _INT),
    ("post_msgs", _INT),
    ("pre_tokens_history", _INT),
    ("post_tokens_history", _INT),
    ("duration_ms", _INT),
    ("detail", _STR),
)

HANDLED_EVENT_TYPES = frozenset({
    EventType.LLM_RETRY,
    EventType.LLM_CALL_COMPLETED,
    EventType.COMPACTION_SETTLED,
})


def _clean(value: Any, kind: str) -> Any:
    if kind == _INT:
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    if kind == _NUM:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        # NaN / inf 进 json.dumps 会写出非法 JSON 字面量，前端 JSON.parse 整帧失败。
        return value if ok and math.isfinite(value) else None
    if kind == _BOOL:
        return value if isinstance(value, bool) else None
    if isinstance(value, str):
        return value[:MAX_STR_LEN]
    return None


def _extract(data: dict, fields: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    """只抽 ``data`` 里**出现过**的白名单键（缺键保持缺键 = 未知，不伪造 None）。"""
    return {key: _clean(data[key], kind) for key, kind in fields if key in data}


def clean_retry_fields(data: Any) -> dict[str, Any]:
    """P20.1：``LLM_RETRY`` 载荷 → ``guest_status`` 的 retry 字段（与调用记录同一白名单 + 清洗）。

    清洗后为 ``None`` 的键直接省略 —— 前端缺键即不显示，不必再判 null。
    """
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in _extract(data, _RETRY_FIELDS).items() if v is not None}


class LlmCallRecords:
    """一条消息的调用记录缓冲。

    **加锁**：``on_event`` 跑在 agentao 的工作线程（``arun`` 把回合提交到线程池），
    ``finalize`` / ``reset`` 跑在事件循环；不加锁时「检查冻结 → 追加」与「消费挂起重试 →
    冻结」可能交错，同一次调用被记两次或在冻结后漏进活缓冲。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._reset_unlocked()

    def _reset_unlocked(self) -> None:
        self._calls: list[dict[str, Any]] = []
        self._pending_retries: list[dict[str, Any]] = []
        self._compactions: list[dict[str, Any]] = []
        self._truncated = False
        self._snapshot: Optional[dict[str, Any]] = None

    # ── 喂事件 ──────────────────────────────────────────────────────────

    def on_event(self, event_type: Any, data: Any) -> None:
        if not isinstance(data, dict):
            return
        with self._lock:
            self._on_event_unlocked(event_type, data)

    def _on_event_unlocked(self, event_type: Any, data: dict) -> None:
        if self._snapshot is not None:
            return
        if event_type is EventType.LLM_RETRY:
            self._pending_retries.append(_extract(data, _RETRY_FIELDS))
        elif event_type is EventType.LLM_CALL_COMPLETED:
            call = _extract(data, _CALL_FIELDS)
            for key in _CALL_ERROR_FIELDS:
                val = _clean(data.get(key), _STR)
                if val:
                    call[key] = val
            self._append_call(call)
        elif event_type is EventType.COMPACTION_SETTLED:
            if len(self._compactions) >= MAX_COMPACTIONS:
                self._truncated = True
                return
            self._compactions.append(_extract(data, _COMPACTION_FIELDS))

    def _append_call(self, call: dict[str, Any]) -> None:
        retries, self._pending_retries = self._pending_retries, []
        if retries:
            if len(retries) > MAX_RETRIES_PER_CALL:
                retries = retries[:MAX_RETRIES_PER_CALL]
                self._truncated = True
            call["retries"] = retries
        if len(self._calls) >= MAX_LLM_CALLS:
            self._truncated = True
            return
        self._calls.append(call)

    # ── 收尾 ────────────────────────────────────────────────────────────

    def finalize(self) -> dict[str, Any]:
        """幂等：首次生成并冻结快照，之后返回同一份的深拷贝。"""
        with self._lock:
            return self._finalize_unlocked()

    def _finalize_unlocked(self) -> dict[str, Any]:
        if self._snapshot is None:
            if self._pending_retries:
                # 重试后没等到终结事件（通常上游会发 error COMPLETED 吸收它们，这是兜底）
                self._append_call({"status": STATUS_CANCELLED_PLACEHOLDER})
            snap: dict[str, Any] = {"llm_calls": self._calls}
            if self._compactions:
                snap["compactions"] = self._compactions
            if self._truncated:
                snap["llm_records_truncated"] = True
            # 冻结时深拷贝：快照与活缓冲不共享 list，晚到事件（取消清理超时的 worker 线程）
            # 即便越过冻结检查也改不到已发出的快照。
            self._snapshot = copy.deepcopy(snap)
        return copy.deepcopy(self._snapshot)
