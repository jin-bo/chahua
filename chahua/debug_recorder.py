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

import json
import logging
import os
import shutil
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ._persist import (
    append_jsonl,
    read_jsonl_skip_bad,
    write_text_atomic,
)
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
SCORING_PATH_HANDOFF_DELEGATE: str = "handoff_delegate"
"""P7.1 显式 delegate 指派路径（drain loop 跑队列）。"""
SCORING_PATH_HANDOFF_REVIEW: str = "handoff_review"
"""P7.2 显式 review 请审路径（drain loop 跑队列）。"""
SCORING_PATH_HANDOFF_PANEL: str = "handoff_panel"
"""P7.3 显式 panel 圆桌路径（drain loop 跑队列）。整个 panel turn（含末位
summarizer 那一发）的 scoring_path 都是这一条——summarizer 是 turn 的最后一位
speaker，不是独立 turn（docs/P7.3 §3.6）。"""

VALID_SCORING_PATHS: frozenset[str] = frozenset(
    {
        SCORING_PATH_SCORING,
        SCORING_PATH_MENTION,
        SCORING_PATH_BROADCAST,
        SCORING_PATH_HANDOFF_DELEGATE,
        SCORING_PATH_HANDOFF_REVIEW,
        SCORING_PATH_HANDOFF_PANEL,
    }
)

# ``load_index`` 投影出的轻量索引字段（docs/P6.3 §4.1）。
# **不放** scoring.scorables / cooled / 各 guest 分数（每条 ≤ 200B 给 room_history
# 单帧塞 1000 条留余裕）；详情走 :meth:`TurnRecorder.load_turn`。
_INDEX_PROJECTION_FIELDS: tuple[str, ...] = (
    "turn_id", "ts_ms", "task_id", "trigger_kind", "scoring_path", "winners",
    "n_messages",
)

# room_history 下发的 turns_index 硬上限（docs/P6.3 §5.2）。超出仅下最新 N 条，
# 让单帧 ws ~300KB（1000 × 300B）。更老的从盘上 grep，不开"翻页"协议。
TURNS_INDEX_HARD_CAP = 1000


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


def _count_nonempty_lines(path: Path) -> int:
    """文件不存在 → 0。仅扫非空行，不做 JSON parse —— 给 ``TurnRecorder.__init__``
    的 ``_turn_count`` seed 用，hot path 避免 per-line json.loads。
    """
    if not path.is_file():
        return 0
    n = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                n += 1
    return n


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
        max_turns: int = 500,
    ) -> None:
        self.enabled: bool = enabled
        # 不变量：enabled=False ⇒ capture_prompts=False。
        # NOOP_RECORDER 实例两个 attr 都是 False；orchestrator 只用 capture_prompts
        # 单条件分支决定是否走 score_with_prompt。
        self.capture_prompts: bool = enabled and capture_prompts
        # P6.3.B rotation cap。0 = 关 rotation；负值 → 装配层（config.py）已拒，
        # 这里 defensive 也走"关 rotation"语义（不变量"max_turns = 0 关 rotation"）。
        self._max_turns: int = max_turns if max_turns > 0 else 0

        # 唯一持久状态：``<room_dir>/debug/``。``turns.jsonl`` / ``prompts/`` 路径都从这里
        # 派生；``enabled=False`` 时为 None，所有 record_* / flush_turn 在前置守卫就 return。
        self._debug_dir: Optional[Path] = None
        # in-flight turn 记录；None = 当前没有 start_turn 过 / 已 flush / 已 discard。
        self._current: Optional[dict[str, Any]] = None
        # 内存维护的 turn 计数（P6.3.B §9）—— ``__init__`` 期数一次后单调维护，
        # ``flush_turn`` append 成功 +1、``rotate_if_needed`` 删 N 条 -= N。永远不重测盘。
        self._turn_count: int = 0

        if not enabled:
            return
        if room_dir is None:
            raise ValueError("TurnRecorder(enabled=True) requires room_dir")
        # 构造期一次性 mkdir（与 _persist.append_jsonl 的"调用方一次性 mkdir"承诺对齐）。
        self._debug_dir = room_dir / "debug"
        self._debug_dir.mkdir(parents=True, exist_ok=True)
        # 启动期 turn_count seed + 兜底 rotate（P6.3.B §9.1）。**只数非空行**而非
        # json.loads 每条 —— 长跑房间 ~5000 行的 build_room_session 启动期才不被
        # per-line JSON parse 拉慢（坏行被算进 count 顶多让 rotation 多剪 1 条，
        # 与 transcript.jsonl 跳坏行同口径接受）。失败 WARN 不阻断。
        try:
            self._turn_count = _count_nonempty_lines(self._turns_path)
        except Exception:
            _log.warning(
                "TurnRecorder __init__: seed _turn_count from jsonl failed",
                exc_info=True,
            )
            self._turn_count = 0
        self.rotate_if_needed()

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

        append 成功后 ``_turn_count += 1`` 并紧跟 :meth:`rotate_if_needed` ——
        热路径常态是一次 int 比较 + early-return，不引入节流。
        """
        if not self.enabled or self._current is None:
            return
        appended = False
        try:
            append_jsonl(self._turns_path, self._current)
            appended = True
        except Exception:
            _log.warning("TurnRecorder.flush_turn failed", exc_info=True)
        finally:
            self._current = None
        if appended:
            self._turn_count += 1
            # rotate_if_needed 内部 try/except，rotation 失败不阻断房间。
            self.rotate_if_needed()

    # ── 加载入口（P6.3.A）────────────────────────────────────────────────

    def _iter_index_rows(self):
        """``read_jsonl_skip_bad`` 包一层：``enabled=False`` 直返空迭代。

        其它调用方（:meth:`load_index` / :meth:`rotate_if_needed` /
        :meth:`__init__` 期 seed）共享这个入口；改"读哪个文件 / 跳坏行口径"只动一处。
        """
        if not self.enabled:
            return iter(())
        assert self._debug_dir is not None
        return read_jsonl_skip_bad(self._turns_path)

    @staticmethod
    def _project_index_row(row: dict[str, Any]) -> Optional[dict[str, Any]]:
        """投影 ``turns.jsonl`` 行 → 轻量索引 dict（docs §4.1 字段集）。

        ``turn_id`` 缺则跳（坏行；同 ``transcript.jsonl`` 跳坏行口径）。其它字段缺
        → 各自合理 fallback：``ts_ms`` 缺写 ``0``、``winners`` 缺写 ``[]``、
        ``trigger_kind`` 走 ``trigger.kind`` 嵌套字段（jsonl schema 把它放在 trigger 内）。
        """
        turn_id = row.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            return None
        trigger = row.get("trigger") or {}
        scoring = row.get("scoring") or {}
        messages = row.get("messages") or []
        return {
            "turn_id": turn_id,
            "ts_ms": row.get("ts_ms") if isinstance(row.get("ts_ms"), int) else 0,
            "task_id": row.get("task_id"),
            "trigger_kind": (
                trigger.get("kind") if isinstance(trigger, dict) else None
            ),
            "scoring_path": row.get("scoring_path") or SCORING_PATH_SCORING,
            "winners": list(scoring.get("winners") or [])
            if isinstance(scoring, dict) else [],
            "n_messages": (
                len(messages) if isinstance(messages, list) else 0
            ),
        }

    def load_index(self, *, limit: Optional[int] = None) -> list[dict[str, Any]]:
        """读 ``debug/turns.jsonl`` 投影成 ``turns_index``（倒序，最新在前）。

        - ``enabled=False`` → ``[]``。
        - 跳坏行（复用 :func:`chahua._persist.read_jsonl_skip_bad`）。
        - ``limit=None`` → 全扫返全部。**rotation 必须用这条**：截断要看到完整老 turn
          序列，否则 limit 之外的更老 turn 永远删不到、jsonl 仍超 cap（不变量
          "``rotate_if_needed`` 内 ``load_index`` 必须 ``limit=None``"）。
        - ``limit=N`` → 内部 ``deque(maxlen=N)`` 持最新 N 条然后反转返；
          ``room_history`` snapshot 用 ``limit=TURNS_INDEX_HARD_CAP``。
        """
        if not self.enabled:
            return []
        try:
            if limit is None:
                rows = []
                for row in self._iter_index_rows():
                    projected = self._project_index_row(row)
                    if projected is not None:
                        rows.append(projected)
                rows.reverse()
                return rows
            if limit <= 0:
                return []
            # deque(maxlen=N) 自动滚出最老 → 末端是最新 N 条；反转后最新在前。
            tail: deque[dict[str, Any]] = deque(maxlen=limit)
            for row in self._iter_index_rows():
                projected = self._project_index_row(row)
                if projected is not None:
                    tail.append(projected)
            out = list(tail)
            out.reverse()
            return out
        except Exception:
            _log.warning("TurnRecorder.load_index failed", exc_info=True)
            return []

    def load_turn(
        self, turn_id: str
    ) -> tuple[Optional[dict[str, Any]], dict[str, str]]:
        """按 ``turn_id`` 查 jsonl 行 + 读关联 prompt 文件。

        返回 ``(turn_dict | None, {rel: text})``。

        - 顺序扫（不建内存索引）：单房间 ``turns.jsonl`` 撑死几千行，fetch 是用户主动
          操作，不在 hot path；建索引就得加 rotation 失效逻辑，复杂度不值。
        - 找到一条立即停；重复 ``turn_id``（手工编辑 / fsck）取第一条 + WARN。
        - prompt 文件按行内 ``scoring[].prompt_file`` / ``messages[].speak_prompt_file``
          字段读；任意路径校验 ``resolve().is_relative_to(debug_dir)`` ——
          jsonl 自己写下来的字段也防一手（攻击模型：用户编辑 turns.jsonl 塞
          ``"prompt_file": "../../.env"``）。单文件读失败 / 路径越界 → 该 key 整体
          缺省（**不空串**），WARN 跳过。
        """
        if not self.enabled:
            return (None, {})
        assert self._debug_dir is not None
        try:
            hit: Optional[dict[str, Any]] = None
            for row in self._iter_index_rows():
                if row.get("turn_id") != turn_id:
                    continue
                if hit is None:
                    hit = row
                else:
                    _log.warning(
                        "TurnRecorder.load_turn: duplicate turn_id=%r in jsonl; "
                        "using first",
                        turn_id,
                    )
                    break
            if hit is None:
                return (None, {})
            prompts = self._load_prompts_for_row(hit)
            return (hit, prompts)
        except Exception:
            _log.warning("TurnRecorder.load_turn failed", exc_info=True)
            return (None, {})

    def _load_prompts_for_row(
        self, row: dict[str, Any]
    ) -> dict[str, str]:
        """收集 ``row`` 内 ``prompt_file`` / ``speak_prompt_file`` 字段并读盘。

        ``capture_prompts=False`` → 整字典空 dict 直接返。用户把 ``capture_prompts``
        关掉的意图是"我不想再让 prompt 出现在 panel 里"，老 ``=True`` 期残留的 prompt
        文件也要一起隐藏 —— 否则 P6.3 不变量"单 key 严格 enabled && capture_prompts &&
        文件可读三重满足才出现"被破坏（codex 评审第 2 轮 P2 找出）。盘上残文件留给
        ``clear_room`` / rotation / 手工 rm 处理；本函数只管协议层。

        路径校验 + 读失败 → 跳过 + WARN（key 整体缺，不空串）。
        """
        if not self.capture_prompts:
            return {}
        out: dict[str, str] = {}
        scoring = row.get("scoring")
        if isinstance(scoring, dict):
            results = scoring.get("results")
            if isinstance(results, list):
                for entry in results:
                    if not isinstance(entry, dict):
                        continue
                    rel = entry.get("prompt_file")
                    if isinstance(rel, str) and rel:
                        text = self._read_prompt_file_safe(rel)
                        if text is not None:
                            out[rel] = text
        messages = row.get("messages")
        if isinstance(messages, list):
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                rel = msg.get("speak_prompt_file")
                if isinstance(rel, str) and rel:
                    text = self._read_prompt_file_safe(rel)
                    if text is not None:
                        out[rel] = text
        return out

    def _read_prompt_file_safe(self, rel: str) -> Optional[str]:
        """按相对路径读 ``<debug_dir>/<rel>``，校验 ``resolve()`` 落在 ``debug_dir`` 之下。

        路径越界 / IO 失败 → ``None`` + WARN（不空串，让上层 key 整体缺省）。
        """
        assert self._debug_dir is not None
        try:
            abs_path = (self._debug_dir / rel).resolve()
        except (OSError, RuntimeError):
            _log.warning(
                "TurnRecorder._read_prompt_file_safe: resolve %r failed", rel,
                exc_info=True,
            )
            return None
        try:
            debug_root = self._debug_dir.resolve()
        except (OSError, RuntimeError):
            _log.warning(
                "TurnRecorder._read_prompt_file_safe: resolve debug_dir failed",
                exc_info=True,
            )
            return None
        try:
            abs_path.relative_to(debug_root)
        except ValueError:
            _log.warning(
                "TurnRecorder._read_prompt_file_safe: %r escapes debug dir",
                rel,
            )
            return None
        try:
            return abs_path.read_text(encoding="utf-8")
        except OSError:
            _log.warning(
                "TurnRecorder._read_prompt_file_safe: read %r failed", rel,
                exc_info=True,
            )
            return None

    # ── rotation（P6.3.B）─────────────────────────────────────────────────

    def rotate_if_needed(self) -> None:
        """超 ``max_turns`` 时从最老 turn 开始删 jsonl 行 + ``prompts/<turn_id>/`` 子目录。

        - ``enabled=False`` / ``max_turns <= 0`` → early return（不变量
          "``max_turns = 0`` 关 rotation，不关 debug"）。
        - ``_turn_count <= max_turns`` → early return（热路径常态：一次 int 比较）。
        - 否则按事务删 ``n_drop = _turn_count - max_turns`` 个最老 turn_id：
          重写 jsonl（tmp+rename） + 删 ``prompts/<turn_id>/`` 子目录。
        - 失败 try/except 包到底 + WARN —— rotation 失败永远不阻断房间运行。
        """
        if not self.enabled or self._max_turns <= 0:
            return
        if self._turn_count <= self._max_turns:
            return
        try:
            # 必须 limit=None 全扫（不变量）；index 倒序：最新在 index[0]，最老在末尾。
            index = self.load_index(limit=None)
            if len(index) <= self._max_turns:
                # 计数器与盘对齐（理论上不应发生，但 rotate 失败回来再调时这里兜底）。
                self._turn_count = len(index)
                return
            n_drop = len(index) - self._max_turns
            to_delete: set[str] = {
                entry["turn_id"] for entry in index[-n_drop:]
            }
            if not to_delete:
                return
            self._rewrite_jsonl_dropping(to_delete)
            for turn_id in to_delete:
                self._rm_prompt_subdir(turn_id)
            self._turn_count -= len(to_delete)
            _log.info(
                "TurnRecorder rotated %d turns (kept %d, cap=%d)",
                len(to_delete),
                self._turn_count,
                self._max_turns,
            )
        except Exception:
            _log.warning(
                "TurnRecorder.rotate_if_needed failed", exc_info=True
            )

    def _rewrite_jsonl_dropping(self, drop: set[str]) -> None:
        """tmp+rename 整体重写 ``turns.jsonl``，跳过 ``turn_id ∈ drop`` 的行。

        与 ``cursor.json`` / ``write_json_atomic`` 同口径：中途崩溃留下完整旧文件而非半截。
        坏行（``read_jsonl_skip_bad`` 跳过的）天然不进新文件。
        """
        assert self._debug_dir is not None
        path = self._turns_path
        if not path.is_file():
            return
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for row in read_jsonl_skip_bad(path):
                tid = row.get("turn_id")
                if isinstance(tid, str) and tid in drop:
                    continue
                f.write(json.dumps(row, ensure_ascii=False))
                f.write("\n")
        os.replace(tmp, path)

    def _rm_prompt_subdir(self, turn_id: str) -> None:
        """``shutil.rmtree`` 单子目录最多 N+1 个 .txt，整目录删比逐文件 unlink 干净。

        权限 / 文件被打开 → WARN，下次 rotation 再尝试。残留 orphan
        ``prompts/turn_xxx/`` 是可接受的（用户 ls 看到也不会迷惑，turn_id 命名自带语义）。
        """
        assert self._debug_dir is not None
        sub = self._debug_dir / "prompts" / turn_id
        if not sub.is_dir():
            return
        try:
            shutil.rmtree(sub)
        except OSError:
            _log.warning(
                "TurnRecorder._rm_prompt_subdir %r failed", sub, exc_info=True,
            )

    def clear(self) -> None:
        """``clear_room`` 复位调用：删 ``turns.jsonl`` + ``prompts/`` 整子树。

        与 ``Orchestrator.reset_room`` 同口径（``room.clear`` / ``summarizer.clear`` /
        ``cursor.clear``）—— transcript 清了，debug 取证也必须同步清，否则 P6.3.A 的
        ``room_history.turns_index`` / ``fetch_turn_detail`` 仍能把"已清房间"的老
        turn 与 prompt 喂回前端（典型回归：用户点"清空"后 debug panel 重新填上之前的
        历史索引）。

        - ``enabled=False`` → early return。
        - 调用方（``server._clear_room``）已在 inbound 串行循环里 cancel +
          drain 完 in-flight turn，正常路径 ``self._current`` 必为 None；保险起见
          这里仍 reset 一次（损坏状态走 :meth:`discard_turn` 同等语义）。
        - try/except 包到底 + WARN —— 删 IO 失败永远不阻断 clear_room。盘上若残留
          孤儿文件下次同名 clear 会重试。
        """
        if not self.enabled:
            return
        assert self._debug_dir is not None
        self._current = None
        try:
            if self._turns_path.is_file():
                self._turns_path.unlink()
        except OSError:
            _log.warning(
                "TurnRecorder.clear: unlink %r failed", self._turns_path,
                exc_info=True,
            )
        prompts_dir = self._debug_dir / "prompts"
        if prompts_dir.is_dir():
            try:
                shutil.rmtree(prompts_dir)
            except OSError:
                _log.warning(
                    "TurnRecorder.clear: rmtree %r failed", prompts_dir,
                    exc_info=True,
                )
        self._turn_count = 0

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
