"""debug 取证**读取 / 回放**路径（P6.3.A）—— 与 :mod:`chahua.debug_recorder` 的
**记录**路径（P6）对称的一半。

本模块是一组**纯读盘** free function：只吃 ``turns_path`` / ``debug_dir`` /
``capture_prompts`` 等只读入参，绝不碰 in-flight ``_current`` 等可变记录态，所以从
``TurnRecorder`` 抽出来独立成模块。:class:`chahua.debug_recorder.TurnRecorder` 的
``load_index`` / ``load_turn`` 是本模块函数的薄转发（公开 API 不变：调用方仍走
``recorder.load_index(...)`` / ``recorder.load_turn(...)``）。

**不变量**（与记录侧共享，docs/P6.3 §不变量）：

- ``enabled=False`` → 读取一律空（``[]`` / ``(None, {})``）；调用方在 ``TurnRecorder``
  侧先判 ``enabled`` 再进本模块。
- 跳坏行（复用 :func:`chahua._persist.read_jsonl_skip_bad`），同 ``transcript.jsonl`` 口径。
- prompt 文件路径双校验 ``resolve().is_relative_to(debug_dir)`` —— jsonl 自己写下来的
  字段也防一手（攻击模型：用户编辑 turns.jsonl 塞 ``"prompt_file": "../../.env"``）。
- prompt 单 key 严格 ``capture_prompts && 文件可读`` 双满足才出现；任一不成立整 key 缺省
  （**不空串**）。
"""

from __future__ import annotations

import logging
from collections import deque
from pathlib import Path
from typing import Any, Optional

from ._persist import read_jsonl_skip_bad

_log = logging.getLogger(__name__)


def iter_index_rows(turns_path: Path):
    """``read_jsonl_skip_bad`` 的薄包装 —— 给 :func:`load_index` / :func:`load_turn`
    共享同一"读哪个文件 / 跳坏行口径"入口。

    调用方需先确保 ``enabled=True``（``turns_path`` 由 ``debug_dir`` 派生）。
    """
    return read_jsonl_skip_bad(turns_path)


def project_index_row(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """投影 ``turns.jsonl`` 行 → 轻量索引 dict（docs §4.1 字段集）。

    ``turn_id`` 缺则跳（坏行；同 ``transcript.jsonl`` 跳坏行口径）。其它字段缺
    → 各自合理 fallback：``ts_ms`` 缺写 ``0``、``winners`` 缺写 ``[]``、
    ``trigger_kind`` 走 ``trigger.kind`` 嵌套字段（jsonl schema 把它放在 trigger 内）。
    """
    # 延迟 import 取协议常量单一真源：模块顶层 import 会与 debug_recorder ↔ _debug_query
    # 形成环（debug_recorder 先 import 本模块、常量在其后才定义）；project_index_row 只在
    # 运行期被调，此刻两模块均已就绪。命中已加载模块仅一次 dict 查询，可忽略。
    from .debug_recorder import SCORING_PATH_SCORING

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


def load_index(
    turns_path: Path, *, limit: Optional[int] = None
) -> list[dict[str, Any]]:
    """读 ``debug/turns.jsonl`` 投影成 ``turns_index``（倒序，最新在前）。

    - 跳坏行（复用 :func:`chahua._persist.read_jsonl_skip_bad`）。
    - ``limit=None`` → 全扫返全部。**rotation 必须用这条**：截断要看到完整老 turn
      序列，否则 limit 之外的更老 turn 永远删不到、jsonl 仍超 cap（不变量
      "``rotate_if_needed`` 内 ``load_index`` 必须 ``limit=None``"）。
    - ``limit=N`` → 内部 ``deque(maxlen=N)`` 持最新 N 条然后反转返；
      ``room_history`` snapshot 用 ``limit=TURNS_INDEX_HARD_CAP``。

    ``enabled`` 守卫 + try/except WARN 由 ``TurnRecorder.load_index`` 外层负责。
    """
    if limit is None:
        rows = []
        for row in iter_index_rows(turns_path):
            projected = project_index_row(row)
            if projected is not None:
                rows.append(projected)
        rows.reverse()
        return rows
    if limit <= 0:
        return []
    # deque(maxlen=N) 自动滚出最老 → 末端是最新 N 条；反转后最新在前。
    tail: deque[dict[str, Any]] = deque(maxlen=limit)
    for row in iter_index_rows(turns_path):
        projected = project_index_row(row)
        if projected is not None:
            tail.append(projected)
    out = list(tail)
    out.reverse()
    return out


def load_turn(
    turns_path: Path, debug_dir: Path, turn_id: str, *, capture_prompts: bool
) -> tuple[Optional[dict[str, Any]], dict[str, str]]:
    """按 ``turn_id`` 查 jsonl 行 + 读关联 prompt 文件。

    返回 ``(turn_dict | None, {rel: text})``。

    - 顺序扫（不建内存索引）：单房间 ``turns.jsonl`` 撑死几千行，fetch 是用户主动
      操作，不在 hot path；建索引就得加 rotation 失效逻辑，复杂度不值。
    - 找到一条立即停；重复 ``turn_id``（手工编辑 / fsck）取第一条 + WARN。
    - prompt 文件按行内 ``scoring[].prompt_file`` / ``messages[].speak_prompt_file``
      字段读；任意路径校验 ``resolve().is_relative_to(debug_dir)``。单文件读失败 /
      路径越界 → 该 key 整体缺省（**不空串**），WARN 跳过。

    ``enabled`` 守卫 + try/except WARN 由 ``TurnRecorder.load_turn`` 外层负责。
    """
    hit: Optional[dict[str, Any]] = None
    for row in iter_index_rows(turns_path):
        if row.get("turn_id") != turn_id:
            continue
        if hit is None:
            hit = row
        else:
            _log.warning(
                "load_turn: duplicate turn_id=%r in jsonl; using first",
                turn_id,
            )
            break
    if hit is None:
        return (None, {})
    prompts = load_prompts_for_row(debug_dir, hit, capture_prompts=capture_prompts)
    return (hit, prompts)


def load_prompts_for_row(
    debug_dir: Path, row: dict[str, Any], *, capture_prompts: bool
) -> dict[str, str]:
    """收集 ``row`` 内 ``prompt_file`` / ``speak_prompt_file`` 字段并读盘。

    ``capture_prompts=False`` → 整字典空 dict 直接返。用户把 ``capture_prompts``
    关掉的意图是"我不想再让 prompt 出现在 panel 里"，老 ``=True`` 期残留的 prompt
    文件也要一起隐藏 —— 否则 P6.3 不变量"单 key 严格 enabled && capture_prompts &&
    文件可读三重满足才出现"被破坏（codex 评审第 2 轮 P2 找出）。盘上残文件留给
    ``clear_room`` / rotation / 手工 rm 处理；本函数只管协议层。

    路径校验 + 读失败 → 跳过 + WARN（key 整体缺，不空串）。
    """
    if not capture_prompts:
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
                    text = read_prompt_file_safe(debug_dir, rel)
                    if text is not None:
                        out[rel] = text
    messages = row.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            rel = msg.get("speak_prompt_file")
            if isinstance(rel, str) and rel:
                text = read_prompt_file_safe(debug_dir, rel)
                if text is not None:
                    out[rel] = text
    return out


def read_prompt_file_safe(debug_dir: Path, rel: str) -> Optional[str]:
    """按相对路径读 ``<debug_dir>/<rel>``，校验 ``resolve()`` 落在 ``debug_dir`` 之下。

    路径越界 / IO 失败 → ``None`` + WARN（不空串，让上层 key 整体缺省）。
    """
    try:
        abs_path = (debug_dir / rel).resolve()
    except (OSError, RuntimeError):
        _log.warning(
            "read_prompt_file_safe: resolve %r failed", rel,
            exc_info=True,
        )
        return None
    try:
        debug_root = debug_dir.resolve()
    except (OSError, RuntimeError):
        _log.warning(
            "read_prompt_file_safe: resolve debug_dir failed",
            exc_info=True,
        )
        return None
    try:
        abs_path.relative_to(debug_root)
    except ValueError:
        _log.warning(
            "read_prompt_file_safe: %r escapes debug dir", rel,
        )
        return None
    try:
        return abs_path.read_text(encoding="utf-8")
    except OSError:
        _log.warning(
            "read_prompt_file_safe: read %r failed", rel,
            exc_info=True,
        )
        return None
