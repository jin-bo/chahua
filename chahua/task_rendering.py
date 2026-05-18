"""task 块的字符串渲染（P5.3.1，docs §6.1）—— 纯函数层。

不读 store、不接 self、不背状态分支。closed task / task 不存在的判断全部在调用方
（:meth:`Orchestrator._resolve_renderable_task`）里做 —— 取到不该注入的 task 时
调用方直接跳过这里的函数，本模块只负责"把已取好的纯数据拼成喂 LLM 的字符串"。

两态：``compact=False`` 走 onboarding 完整块（在"近期梗概"与"最近原文"之间插入）；
``compact=True`` 走 incremental 短 header（1-3 行）。
``task.owner`` 直接渲染原值（raw speaker_id，"user" 或茶客名）—— display name 映射不
属于渲染层，调用方按需在传入前做。

P5.6：scoring 与 speak compact 共享 :func:`render_task_header` 这一层（title / goal
first line / status 的纯数据格式化），但 body 渲染不共享 —— speak compact 会追加
``./task/`` 写入指引行，scoring 不追加，避免把"该写产物吗"的执行语义渗进打分相关性信号。
"""

from __future__ import annotations

from xml.sax.saxutils import quoteattr

from .scoring import ScoreResult
from .summarizer import SummarySpan
from .task import (
    TASK_STATUS_DISPLAY,
    TASK_UNTITLED,
    Decision,
    Task,
    format_artifact_mtime,
    format_artifact_size,
)

_FULL_DECISIONS_CAP = 5
"""完整块最多渲染几条决策（取最近 N 条）；超出截断。预算 ≈ 5 × 50 字。"""

_FULL_ARTIFACTS_CAP = 10
"""完整块 artifact 清单条数上限（首 N 个按名字排序的产物）。"""

_FULL_SUMMARY_TAIL_CAP = 3
"""完整块"任务近期进展"取 task summary 末几段。"""


def wrap_current_task(task_block: tuple[str, str]) -> str:
    """``(body, status_display)`` → ``<current_task status="...">{body}</current_task>``。

    onboarding / incremental / scoring 三条路径共用同一段 XML 拼装，单点 helper 避免漏改。
    ``quoteattr`` 处理 status 属性，避免 status_display 里含 ``"`` / ``<`` / ``&`` 时破坏
    XML 边界。
    """
    body, status_display = task_block
    return f"<current_task status={quoteattr(status_display)}>\n{body}\n</current_task>"


def render_task_header(task: Task) -> tuple[list[str], str]:
    """compact body 的头两行 + status_display —— scoring 与 speak compact 共享（P5.6）。

    输出 ``["标题：...", "目标：<first line>"]``（goal 为空时只输出标题行）+ status
    枚举显示名。**不含** ``./task/`` 写入指引 —— 那是发言阶段执行语义，speak compact
    自己在调用方追加，scoring 不追加。

    full 模式（``render_task_block(compact=False)``）独立路径，含 owner / full goal /
    decisions / artifacts / summary —— 与 compact header 重叠仅一两行，不强行共享。
    """
    title = task.title or TASK_UNTITLED
    status_display = TASK_STATUS_DISPLAY.get(task.status, task.status)
    lines = [f"标题：{title}"]
    first_line = task.goal.split("\n", 1)[0].strip() if task.goal else ""
    if first_line:
        lines.append(f"目标：{first_line}")
    return lines, status_display


def render_task_block(
    task: Task,
    decisions: list[Decision],
    artifacts: list[dict],
    summary_tail: list[SummarySpan],
    *,
    compact: bool,
) -> tuple[str, str]:
    """把任务上下文渲染成给茶客 LLM 的文本块（P5.3.1）。

    返回 ``(body, status_display)`` 元组 —— ``status_display`` 由调用方拼到
    ``<current_task status="...">`` XML 属性里，body 不再含状态行。
    """
    if compact:
        lines, status_display = render_task_header(task)
        # P5.4：./task/ 是任务工作目录，可读写。茶客新产物**必须**写到 ./task/<name>。
        # 写到 cwd / ./share/ 等别处不会自动归集进任务产物清单。scoring 路径不追加此行
        # （走 render_task_header 后直接 join）。
        lines.append(
            "./task/ 是本任务工作目录（可读写）。任务产物务必写到 ./task/<name>，"
            "自动入任务；写到 cwd / ./share/ 等别处不算入任务产物。"
        )
        return "\n".join(lines), status_display

    title = task.title or TASK_UNTITLED
    status_display = TASK_STATUS_DISPLAY.get(task.status, task.status)
    parts: list[str] = [f"标题：{title}"]
    if task.owner:
        parts.append(f"负责人：{task.owner}")
    if task.goal:
        parts.append(f"目标：\n{task.goal}")

    if decisions:
        recent = decisions[-_FULL_DECISIONS_CAP:]
        bullets = "\n".join(f"- {d.summary}" for d in recent)
        parts.append(f"近期决策（最近 {len(recent)} 条）：\n{bullets}")

    if artifacts:
        head = artifacts[:_FULL_ARTIFACTS_CAP]
        bullets = "\n".join(
            f"- {a['name']} ({format_artifact_size(a['size'])}, "
            f"{format_artifact_mtime(a['mtime_ms'])})"
            for a in head
        )
        # P5.4：./task/ 可读写。茶客**必须**把新产物写到 ./task/<name>，否则不会进任务清单。
        parts.append(
            "当前产物（./task/ 是本任务工作目录，可读写；"
            "任务产物务必写到 ./task/<name>，自动入任务；"
            f"写到 cwd / ./share/ 等别处不算入任务产物）：\n{bullets}"
        )
    else:
        # full 模式无 artifact 时仍要明示 ./task/ 的存在 + 写权限 + 别处不算 —— 避免茶客
        # 把产物写到 cwd / ./share/ 后纳闷"为什么没出现在任务里"。
        parts.append(
            "./task/ 是本任务工作目录（可读写，当前为空）。"
            "任务产物务必写到 ./task/<name>，自动入任务；"
            "写到 cwd / ./share/ 等别处不会自动归集进任务产物清单。"
        )

    if summary_tail:
        tail = summary_tail[-_FULL_SUMMARY_TAIL_CAP:]
        bullets = "\n\n".join(s.text for s in tail)
        parts.append(f"任务近期进展：\n{bullets}")

    return "\n\n".join(parts), status_display


def score_to_dict(r: ScoreResult) -> dict:
    """:class:`ScoreResult` → JSON-safe dict（``turn_start.data.scores`` 里塞 N 条）。"""
    return {
        "guest_name": r.guest_name,
        "score": r.score,
        "kind": r.kind.value,
    }
