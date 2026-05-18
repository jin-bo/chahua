"""task 块的字符串渲染（P5.3.1，docs §6.1）—— 纯函数层。

不读 store、不接 self、不背状态分支。closed task / task 不存在的判断全部在调用方
（:meth:`Orchestrator._resolve_renderable_task`）里做 —— 取到不该注入的 task 时
调用方直接跳过这里的函数，本模块只负责"把已取好的纯数据拼成喂 LLM 的字符串"。

两态：``compact=False`` 走 onboarding 完整块（在"近期梗概"与"最近原文"之间插入）；
``compact=True`` 走 incremental 短 header（1-3 行）。
``task.owner`` 直接渲染原值（raw speaker_id，"user" 或茶客名）—— display name 映射不
属于渲染层，调用方按需在传入前做。

P5.6：scoring 走 :func:`render_scoring_header`（**完整 goal**，含多行流程编排信息），
speak compact 走 :func:`render_task_header`（**仅 goal 首行**，受 ≤80 token 预算约束）。
两者都返 ``(lines, status_display)`` 二元组，body 渲染不共享 —— speak compact 会追加
``./task/`` 写入指引行，scoring 不追加，避免把"该写产物吗"的执行语义渗进打分相关性信号。

scoring 喂完整 goal 是有意：goal 里常写"先 X 再 Y 后 Z"这类角色顺序，砍到首行打分
模型只看到 "X"，会把"应该最后说话"的角色误判成"现在就该说话"。每 pick 周期 1 次渲染，
N 个 scorer 共享，goal 膨胀不放大成本。
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
    """speak compact 的头两行 + status_display —— 仅 goal 首行（P5.6）。

    输出 ``["标题：...", "目标：<first line>"]``（goal 为空时只输出标题行）+ status
    枚举显示名。**不含** ``./task/`` 写入指引 —— 那是发言阶段执行语义，调用方
    （``render_task_block(compact=True)``）自己追加。

    compact body 受 ≤80 token 预算约束（CLAUDE.md 关键不变量），所以 goal 切首行；
    打分路径要看到完整流程信息，走独立的 :func:`render_scoring_header`。

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


def render_scoring_header(task: Task) -> tuple[list[str], str]:
    """scoring 路径的头 N 行 + status_display —— 渲染**完整 goal**（P5.6 修订）。

    输出 ``["标题：...", "目标：", <goal line 1>, <goal line 2>, ...]``（goal 为空时
    只输出标题行）+ status 枚举显示名。多行 goal 用"目标：\\n<full>"形态保留换行，与
    full 模式（``render_task_block(compact=False)``）的 goal 渲染口径一致。

    与 :func:`render_task_header` 区别：后者切 goal 首行喂 speak compact（≤80 token 预算），
    本函数喂打分 prompt —— goal 里常写"先 X 再 Y 后 Z"这类角色顺序，砍到首行打分模型
    会把"应该最后说话"的角色（如"Z 最后总结"）误判成"现在就该说话"。每 pick 周期 1 次
    渲染，N 个 scorer 共享同一字符串，goal 膨胀不放大成本。

    **不含** ``./task/`` 写入指引 —— 那是发言阶段执行语义，混进打分会把"该写产物吗"
    的执行信号污染"想接话吗"的相关性信号。
    """
    title = task.title or TASK_UNTITLED
    status_display = TASK_STATUS_DISPLAY.get(task.status, task.status)
    lines = [f"标题：{title}"]
    if task.goal:
        goal = task.goal.strip()
        if goal:
            if "\n" in goal:
                lines.append("目标：")
                lines.extend(goal.split("\n"))
            else:
                lines.append(f"目标：{goal}")
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
        # P5.4 + 文案升级：./task/ 是任务工作目录，**读用 read_file('./task/<name>')、
        # 写用 task_write_artifact(name, content)**。compact 路径每轮喂，文案极简 ——
        # 软触发"你判断" + 落盘动作 + 边界提醒三句，类型枚举留给 onboarding 的 full
        # 文案。scoring 路径不追加此行（走 render_task_header 后直接 join）。
        # **不是普通 write_file**：./task/ 软链解析到 cwd 外，原生 write_file 会被
        # agentao PathPolicy 拒；专用 task_write_artifact 工具绕开（见 task_tools.py）。
        lines.append(
            "./task/ 是本任务工作目录（读：./task/<name>；写：用 task_write_artifact "
            "工具）。你判断本轮输出值得作为任务产物时，调 task_write_artifact(name, content) "
            "落盘（自动入任务），聊天里只放一句概要 + 文件引用；"
            "**不要用普通 write_file('./task/<x>')**（会被 PathPolicy 拒）。"
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
        # P5.4 + 文案升级：./task/ 可读、专用 task_write_artifact 工具写。已经有产物落盘
        # 时提醒"何时该再落盘 + 命名习惯"——避免茶客把后续报告又只塞回聊天。触发条件用
        # "你判断"软描述（不给字数硬阈值，让茶客自主判断），类型枚举保留作为锚点。
        parts.append(
            f"当前产物（./task/ 读用 read_file('./task/<name>')、"
            f"写用 task_write_artifact 工具）：\n{bullets}\n\n"
            "**何时该再落盘**：你判断本轮输出值得作为新的任务产物（典型如评审意见、"
            "设计方案、决策清单、代码片段、报告草稿等需要跨轮引用 / 后续合并 / 出 PDF "
            "的结构化内容）时，调 `task_write_artifact(name, content)` 落到 ./task/<name>，"
            "自动入任务产物清单；聊天发言只保留一句概要 + 文件引用。"
            "**命名建议**：带角色身份与版本（如 `<你的名字>-评审-v2.md`）。"
            "**不要用普通 write_file('./task/<x>')**（会被 PathPolicy 拒）；"
            "写到 cwd / `./share/` 等别处也不算入任务产物。"
        )
    else:
        # full 模式无 artifact 时给"催落盘"详细版 —— 触发条件 / 落盘示例 / 命名建议 / 为什么
        # 四段。onboarding 单次注入，token 涨幅可接受；incremental 路径走 compact 不受影响。
        # 触发用"你判断"软描述，不给字数硬阈值。**写产物走专用 task_write_artifact 工具**：
        # 原生 write_file('./task/<x>') 会被 agentao PathPolicy 拒（详见 task_tools.py
        # ``TaskWriteArtifactTool`` 类 docstring + tests/test_task_link_write_path_policy.py）。
        parts.append(
            "./task/ 是本任务工作目录（读：read_file('./task/<name>')；"
            "写：task_write_artifact 工具，当前为空）。\n\n"
            "**何时该落盘**：你判断本轮输出值得作为任务产物保留时（典型场景：评审意见、"
            "设计方案、决策清单、代码片段、报告草稿等需要跨轮引用 / 后续合并 / 出 PDF "
            "的结构化内容），调 `task_write_artifact(name, content)` 把内容写到 ./task/<name>，"
            "自动入任务产物清单；不要只放在聊天发言里。聊天发言只保留一句概要 + 文件引用"
            "（例：\"已写入 `./task/水文献-评审.md`，核心问题：Research Gap 不成立 / 引用错位\"）。\n\n"
            "**命名建议**：带角色身份与版本（如 `<你的名字>-评审.md` / "
            "`决策清单-v2.md` / `report-final.md`）。\n\n"
            "**为什么**：聊天发言流过 transcript 不可索引；落盘到 ./task/ 才进任务产物"
            "清单，供后续合并 / 出 PDF / 跨轮引用。"
            "**不要用普通 write_file('./task/<x>')**——./task/ 软链解析到 cwd 外，"
            "agentao PathPolicy 会以 \"refused ... outside project_root\" 拒绝。"
            "写到 cwd / `./share/` 等别处也不会自动归集进任务产物清单。"
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
