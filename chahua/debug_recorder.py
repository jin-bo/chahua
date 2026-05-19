"""房间级调试 / 取证落盘 facade（P6.1，docs/P6-调试与回放.md §1）。

Orchestrator / TeaGuest / ChahuaTransport 在 splice 点调本对象，让"每一轮可展开看"：
谁被候选 / 各自分数 / 为什么被选 / prompt 喂了哪些内容 / 用了哪些工具 / 哪些 MCP / 产物在哪。

**只看取证（view-only）**，不重跑；落盘到 ``<room_dir>/debug/``：

- ``turns.jsonl`` —— 每个 pick 周期一行，含候选 / 分数 / winners / messages / tool_calls。
- ``prompts/<turn_id>/scoring_<guest>.txt`` —— 每位被打分茶客一份完整 prompt。
- ``prompts/<turn_id>/speak_<message_id>.txt`` —— 每位发言茶客一份完整 context_message。

**不变量**（docs/P6 §不变量延伸）：

- ``recorder.enabled=False ⇒ capture_prompts=False`` —— 单条件分支决定行为。
- 所有 ``record_*`` 方法 try/except 包到底，日志 WARN，**房间正常跑下去**（debug 是
  辅助能力，绝不能让它把生产路径挂掉）。
- cancel / 异常路径仍 ``flush_turn`` 一行；``discard_turn`` 仅供 recorder 自身状态损坏
  时使用（in-flight 数据已损坏 / 部分写入失败）。
- ``capture_prompts=False`` 时不创建 ``prompts/`` 目录，jsonl 行内 ``prompt_file`` /
  ``speak_prompt_file`` 字段缺省（不空串）。
- 同一 pick 周期串行执行（Orchestrator while 循环），所以一份 in-flight
  ``self._current: Optional[dict]`` 够用，无需锁。

**注入**：``chahua/session.py::build_room_session`` 内构造一份 TurnRecorder 注入 orchestrator
与每位 guest（ctor 注入，无 setter）。测试夹具用 :data:`NOOP_RECORDER`。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ._persist import append_jsonl, write_text_atomic
from .events import (
    STATUS_CANCELLED,
    STATUS_ERROR,
    STATUS_OK,
    now_ms,
)
from .scoring import ScoreResult

_log = logging.getLogger(__name__)

# turns.jsonl 行 schema 版本（docs/P6 §数据模型）。同 transcript / summary.jsonl
# 一样落"schema_version"字段为未来扩展留余地。
TURNS_SCHEMA_VERSION = 1

# scoring_path 取值。turn 级字段，区分本周期走的是哪条 pick 路径（docs §数据模型）。
SCORING_PATH_SCORING: str = "scoring"
"""常规 LLM 打分（``threshold ≠ null``）。"""
SCORING_PATH_MENTION: str = "mention"
"""单 ``@`` 路由（results 中仅命中那位 ``kind="mention"``）。"""
SCORING_PATH_BROADCAST: str = "broadcast"
"""``@all`` / ``@所有人``（results 中所有 guest 都是 ``kind="mention"``）。"""

VALID_SCORING_PATHS: frozenset[str] = frozenset(
    {SCORING_PATH_SCORING, SCORING_PATH_MENTION, SCORING_PATH_BROADCAST}
)


@dataclass(frozen=True, slots=True)
class PickDebugMeta:
    """``Orchestrator._pick_next_speaker`` → :meth:`TurnRecorder.record_scoring` 的字段集。

    收编 4 个曾经走 ``dict[str, Any]`` 透传的字段（``threshold`` / ``scorables`` /
    ``cooled`` / ``scoring_path``）—— 上层 record_scoring 调用按属性取值，typo 不再
    静默成 ``KeyError`` 在运行期才暴露。

    ``threshold=None``：mention / broadcast / 全员冷却路径（不走打分）；
    ``scorables=[]`` 同理。``scoring_path`` 取值见 ``SCORING_PATH_*`` 常量。
    """

    threshold: Optional[float]
    scorables: list[str] = field(default_factory=list)
    cooled: list[str] = field(default_factory=list)
    scoring_path: str = SCORING_PATH_SCORING

# tool_calls[].source 取值（docs §数据模型 + 不变量"_classify_tool_source 仅 best-effort"）。
TOOL_SOURCE_BUILTIN: str = "builtin"
TOOL_SOURCE_MCP: str = "mcp"
TOOL_SOURCE_UNKNOWN: str = "unknown"


def classify_tool_source(tool: str) -> tuple[str, Optional[str]]:
    """agentao MCP 工具命名约定 ``mcp__<server>__<tool_name>`` 启发式判别（P6.1）。

    单点 helper：``transport_bridge.ChahuaTransport._handle`` 在 TOOL_START 帧落
    recorder 时调用。命名约定一旦变只动这一处（不变量"_classify_tool_source 仅
    best-effort"）。

    返回 ``(source, mcp_server)``：

    - ``tool.startswith("mcp__")`` → ``("mcp", "<server>")``；server 为空 → ``("mcp", None)``。
    - 非空且非 mcp 前缀 → ``("builtin", None)``。
    - 空字符串 → ``("unknown", None)``。
    """
    if tool.startswith("mcp__"):
        rest = tool[len("mcp__"):]
        server, _, _ = rest.partition("__")
        return (TOOL_SOURCE_MCP, server or None)
    if tool:
        return (TOOL_SOURCE_BUILTIN, None)
    return (TOOL_SOURCE_UNKNOWN, None)


class TurnRecorder:
    """房间级 debug 落盘 facade。一房间一实例；同房间 pick 周期串行，无并发态。

    构造期一次性 ``mkdir(debug/)``；``prompts/<turn_id>/`` 在 :meth:`start_turn` 时
    按需创建（``capture_prompts=False`` 整路径不建）。

    所有 ``record_*`` 方法对 ``enabled=False`` 早 return；try/except 内层吞所有异常
    并 WARN —— recorder 失败永远不阻断房间运行。
    """

    def __init__(
        self,
        room_dir: Optional[Path],
        *,
        enabled: bool,
        capture_prompts: bool,
    ) -> None:
        self.enabled: bool = enabled
        # 不变量：enabled=False ⇒ capture_prompts=False。
        # NOOP_RECORDER 实例两个 attr 都是 False；orchestrator 只用 capture_prompts
        # 单条件分支决定是否走 score_with_prompt。
        self.capture_prompts: bool = enabled and capture_prompts

        # 唯一持久状态：``<room_dir>/debug/``。``turns.jsonl`` / ``prompts/`` 路径都从这里
        # 派生；``enabled=False`` 时为 None，所有 record_* / flush_turn 在前置守卫就 return。
        self._debug_dir: Optional[Path] = None
        # in-flight turn 记录；None = 当前没有 start_turn 过 / 已 flush / 已 discard。
        self._current: Optional[dict[str, Any]] = None

        if not enabled:
            return
        if room_dir is None:
            raise ValueError("TurnRecorder(enabled=True) requires room_dir")
        # 构造期一次性 mkdir（与 _persist.append_jsonl 的"调用方一次性 mkdir"承诺对齐）。
        self._debug_dir = room_dir / "debug"
        self._debug_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _turns_path(self) -> Path:
        """``<room_dir>/debug/turns.jsonl``。调用方需先确保 ``enabled=True``。"""
        assert self._debug_dir is not None
        return self._debug_dir / "turns.jsonl"

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def start_turn(
        self,
        *,
        turn_id: str,
        task_id: Optional[str],
        trigger: dict[str, Any],
    ) -> None:
        """开一帧 in-flight turn 记录。``trigger`` = ``{"kind": "user_msg"|"ai_chain",
        "ref_seq": int|None}``（docs §数据模型）。

        重复 :meth:`start_turn` 但没 :meth:`flush_turn` —— 旧 in-flight 被覆盖
        WARN（理论上 orchestrator 不该这么调）。``prompts/<turn_id>/`` 子目录由
        :meth:`_write_prompt_file` 在首次写入时按需创建（``write_text_atomic`` 自带
        parent.mkdir），避免空 turn 目录污染。
        """
        if not self.enabled:
            return
        try:
            if self._current is not None:
                _log.warning(
                    "TurnRecorder.start_turn called with in-flight turn %s; overwriting",
                    self._current.get("turn_id"),
                )
            self._current = {
                "schema_version": TURNS_SCHEMA_VERSION,
                "turn_id": turn_id,
                "ts_ms": now_ms(),
                "task_id": task_id,
                "trigger": dict(trigger),
                "scoring": None,
                "scoring_path": SCORING_PATH_SCORING,  # 默认；record_scoring 可覆写
                "messages": [],
            }
        except Exception:
            _log.warning("TurnRecorder.start_turn failed", exc_info=True)
            self._current = None

    def _write_prompt_file(self, rel: str, content: str) -> Optional[str]:
        """``capture_prompts=True`` 且 ``content`` 非空时落盘到 ``<debug>/<rel>``，
        返回 ``rel``（写 jsonl 时塞进 ``prompt_file`` / ``speak_prompt_file``）；
        其它情况返回 ``None``（调用方据此**整字段缺省**，不空串 —— 不变量
        "piggyback / 落盘 prompt_file 严格 enabled && capture_prompts 双开才出现"）。

        IO 失败 WARN 后吞，返 ``None`` —— recorder 失败不阻断房间运行。
        """
        if not self.capture_prompts or not content:
            return None
        assert self._debug_dir is not None  # capture_prompts=True ⇒ enabled
        abs_path = self._debug_dir / rel
        try:
            write_text_atomic(abs_path, content)
            return rel
        except Exception:
            _log.warning(
                "TurnRecorder write prompt %s failed", abs_path, exc_info=True
            )
            return None

    def record_scoring(
        self,
        *,
        threshold: Optional[float],
        scorables: list[str],
        cooled: list[str],
        results: list[tuple[ScoreResult, Optional[str]]],
        winners: list[str],
        scoring_path: str = SCORING_PATH_SCORING,
    ) -> None:
        """记本周期的打分结果。``results`` 是 ``(ScoreResult, prompt|None)`` 序列。

        ``capture_prompts=True`` 且 ``prompt`` 非空时落盘到
        ``prompts/<turn_id>/scoring_<guest>.txt`` 并把相对路径塞进
        ``results[].prompt_file``；否则字段缺省（不空串）。
        """
        if not self.enabled or self._current is None:
            return
        try:
            if scoring_path not in VALID_SCORING_PATHS:
                _log.warning(
                    "TurnRecorder.record_scoring got unknown scoring_path=%r; "
                    "falling back to 'scoring'",
                    scoring_path,
                )
                scoring_path = SCORING_PATH_SCORING
            turn_id = self._current["turn_id"]
            entries: list[dict[str, Any]] = []
            for result, prompt in results:
                entry: dict[str, Any] = {
                    "guest": result.guest_name,
                    "score": result.score,
                    "kind": result.kind.value,
                    "raw": result.raw or "",
                }
                prompt_file = self._write_prompt_file(
                    f"prompts/{turn_id}/scoring_{result.guest_name}.txt",
                    prompt or "",
                )
                if prompt_file is not None:
                    entry["prompt_file"] = prompt_file
                entries.append(entry)
            self._current["scoring"] = {
                "threshold": threshold,
                "scorables": list(scorables),
                "cooled": list(cooled),
                "results": entries,
                "winners": list(winners),
            }
            self._current["scoring_path"] = scoring_path
        except Exception:
            _log.warning("TurnRecorder.record_scoring failed", exc_info=True)

    def record_message_start(
        self,
        *,
        message_id: str,
        guest: str,
        speak_prompt: str,
    ) -> None:
        """记一位茶客开讲。``speak_prompt`` 是已拼好的 context_message。

        ``capture_prompts=False`` 时不持有 prompt 引用，落盘缺 ``speak_prompt_file``
        字段（不空串）。
        """
        if not self.enabled or self._current is None:
            return
        try:
            entry: dict[str, Any] = {
                "message_id": message_id,
                "guest": guest,
                # 悲观初始化为 ERROR；record_message_end 在 finally 路径覆写真状态。
                "status": STATUS_ERROR,
                "seq": None,
                "tool_calls": [],
                "artifact_paths": [],
            }
            prompt_file = self._write_prompt_file(
                f"prompts/{self._current['turn_id']}/speak_{message_id}.txt",
                speak_prompt,
            )
            if prompt_file is not None:
                entry["speak_prompt_file"] = prompt_file
            self._current["messages"].append(entry)
        except Exception:
            _log.warning(
                "TurnRecorder.record_message_start failed", exc_info=True
            )

    def record_tool_start(
        self,
        *,
        message_id: str,
        call_id: Optional[str],
        tool: str,
        args: Any,
        source: Optional[str],
        mcp_server: Optional[str],
    ) -> None:
        """记一次工具调用的 START 帧。同 ``call_id`` 再次出现（极不应发生）→ 覆盖。

        ``message_id`` / ``call_id=None`` 的 entry 总新建（call_id=None 永远不参与合并）。
        complete 帧后续由 :meth:`record_tool_complete` 按 ``call_id`` 找到本 entry 合并。
        """
        if not self.enabled or self._current is None:
            return
        try:
            msg = self._find_message(message_id)
            if msg is None:
                return
            # call_id=None 不参与合并 —— 永远新建一条；非 None 时若已存在则覆盖
            # （理论上不发生，agentao 不会重发同 call_id 的 start）。
            existing = self._find_tool_call(msg, call_id)
            if existing is None:
                msg["tool_calls"].append(
                    {
                        "call_id": call_id,
                        "tool": tool or "",
                        "args": args,
                        "source": source,
                        "mcp_server": mcp_server,
                        "status": "started",
                        "duration_ms": None,
                        "error": None,
                    }
                )
                return
            existing.update(
                {
                    "tool": tool or existing.get("tool", ""),
                    "args": args,
                    "source": source,
                    "mcp_server": mcp_server,
                }
            )
        except Exception:
            _log.warning("TurnRecorder.record_tool_start failed", exc_info=True)

    def record_tool_complete(
        self,
        *,
        message_id: str,
        call_id: Optional[str],
        status: Optional[str],
        duration_ms: Optional[int],
        error: Optional[str],
    ) -> None:
        """记一次工具调用的 COMPLETE 帧。按 ``call_id`` 找前序 start entry 合并；
        ``call_id=None`` 或前序 start 缺失 → 自起一条 entry（保 args=None 占位）。

        merge 时只覆盖 ``status`` / ``duration_ms`` / ``error`` —— complete 帧不重传
        ``tool`` / ``args`` / ``source`` / ``mcp_server``，避免错位（agentao 的 complete
        event 这些字段可能缺）。
        """
        if not self.enabled or self._current is None:
            return
        try:
            msg = self._find_message(message_id)
            if msg is None:
                return
            existing = self._find_tool_call(msg, call_id)
            if existing is None:
                msg["tool_calls"].append(
                    {
                        "call_id": call_id,
                        "tool": "",
                        "args": None,
                        "source": None,
                        "mcp_server": None,
                        "status": status,
                        "duration_ms": duration_ms,
                        "error": error,
                    }
                )
                return
            if status is not None:
                existing["status"] = status
            if duration_ms is not None:
                existing["duration_ms"] = duration_ms
            if error is not None:
                existing["error"] = error
        except Exception:
            _log.warning(
                "TurnRecorder.record_tool_complete failed", exc_info=True
            )

    def record_artifact_path(self, *, message_id: str, path: str) -> None:
        """记一条本消息派生的产物路径（``tasks/<task_id>/artifacts/<name>`` 格式）。

        派生表显式枚举：MVP 仅 ``task_write_artifact``（docs §不变量"artifact 派生表
        显式枚举"）。本方法只做去重 append；派生映射由 transport / orchestrator 维护。
        """
        if not self.enabled or self._current is None:
            return
        try:
            msg = self._find_message(message_id)
            if msg is None:
                return
            if path not in msg["artifact_paths"]:
                msg["artifact_paths"].append(path)
        except Exception:
            _log.warning(
                "TurnRecorder.record_artifact_path failed", exc_info=True
            )

    def record_message_end(
        self,
        *,
        message_id: str,
        status: str,
        seq: Optional[int],
    ) -> None:
        """记一位茶客发言结束 —— 覆写 status / seq。

        TeaGuest.speak 的 ``finally`` 块单点调用；``status ∈ {ok, cancelled, error}``。
        """
        if not self.enabled or self._current is None:
            return
        try:
            msg = self._find_message(message_id)
            if msg is None:
                return
            if status not in (STATUS_OK, STATUS_CANCELLED, STATUS_ERROR):
                _log.warning(
                    "TurnRecorder.record_message_end: unknown status=%r", status
                )
            msg["status"] = status
            msg["seq"] = seq
        except Exception:
            _log.warning(
                "TurnRecorder.record_message_end failed", exc_info=True
            )

    def flush_turn(self) -> None:
        """把 in-flight turn 写一行到 ``turns.jsonl`` 并清空。

        **cancel / 异常路径也走这里**（不变量"cancel / 异常路径仍 flush 一行"）——
        半截 prompt / 工具调用 / partial message 都是取证证据，丢了反而无证可查。
        各 ``messages[].status`` 已能逐条区分 ``ok`` / ``cancelled`` / ``error``。
        """
        if not self.enabled or self._current is None:
            return
        try:
            append_jsonl(self._turns_path, self._current)
        except Exception:
            _log.warning("TurnRecorder.flush_turn failed", exc_info=True)
        finally:
            self._current = None

    def discard_turn(self) -> None:
        """**仅供 recorder 自身状态损坏时调** —— 丢 in-flight 不写 jsonl。

        正常 cancel / error 路径不调（用 :meth:`flush_turn` 保半截取证）。本方法
        最终落点是"in-flight ``_current`` 字段缺关键字段 / 序列化失败"等边界场景；
        外部 orchestrator / guest **不应** 直接调用。
        """
        self._current = None

    # ── 内部 helper ─────────────────────────────────────────────────────────

    def _find_message(self, message_id: str) -> Optional[dict[str, Any]]:
        """按 ``message_id`` 在 in-flight ``messages`` 列表里查 —— 调用方负责确保
        ``self._current`` 非 None。"""
        assert self._current is not None
        for m in self._current["messages"]:
            if m["message_id"] == message_id:
                return m
        return None

    @staticmethod
    def _find_tool_call(
        msg: dict[str, Any], call_id: Optional[str]
    ) -> Optional[dict[str, Any]]:
        """按 ``call_id`` 在一条 message 的 ``tool_calls`` 里查（``call_id=None``
        永远不匹配 —— 每帧自起一个 entry，避免合并错位）。"""
        if call_id is None:
            return None
        for tc in msg["tool_calls"]:
            if tc["call_id"] == call_id:
                return tc
        return None


# 测试夹具与 enabled=False 装配共用的占位 recorder。所有方法 no-op；构造期不动盘。
# 同 :data:`chahua.events.NOOP_SINK` 风格 —— 让"无 debug 消费者"的调用方无 None 分支。
NOOP_RECORDER: TurnRecorder = TurnRecorder(
    None, enabled=False, capture_prompts=False
)
