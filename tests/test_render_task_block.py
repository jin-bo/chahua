"""P5.3.1：``_render_task_block`` 纯字符串 renderer 测试。

不背状态分支（closed / 不存在 task 在 P5.3.2 调用方处理），所以这里不测 closed 路径
—— 那是 P5.3.2 wiring 层的责任。这里只测拼字符串：
① 全字段 task full 块齐
② full vs compact 输出格式
③ owner=None 跳"负责人"行
④ decisions / artifacts / summary tail 各自截断
⑤ goal 多行 compact 只取首行
⑥ ./task/ 落盘文案：compact 极简 vs full 详细分层（鼓励长内容落盘）

XML 化重构后：``_render_task_block`` 返回 ``(body, status_display)`` 元组，
status 由调用方拼到 ``<current_task status="...">`` XML 属性里，body 不再含状态行。
"""

from __future__ import annotations

from chahua.summarizer import SummarySpan
from chahua.task import Decision, Task
from chahua.task_rendering import render_task_block as _render_task_block


def _task(title: str = "写 README", goal: str = "把 README 写完", *, owner=None, status="open"):
    return Task(
        id="task_abc",
        title=title,
        goal=goal,
        status=status,
        owner=owner,
        created_at_ms=0,
        updated_at_ms=0,
        closed_at_ms=None,
    )


def _decision(summary: str, did: str = "dec_x") -> Decision:
    return Decision(
        decision_id=did,
        task_id="task_abc",
        supporting_message_ids=(),
        summary=summary,
        marked_by="user",
        ts_ms=0,
    )


def _summary_span(text: str, start: int = 1, end: int = 20) -> SummarySpan:
    return SummarySpan(start_seq=start, end_seq=end, text=text)


def _artifact(name: str, size: int = 2300, mtime_ms: int = 1715850000000) -> dict:
    return {
        "name": name,
        "size": size,
        "mtime_ms": mtime_ms,
        "rel": f"tasks/task_abc/artifacts/{name}",
    }


# ─── ① 全字段 full 块齐 ───────────────────────────────────────────────────────


def test_full_block_all_fields_present():
    task = _task(owner="汪小姐", status="in_progress")
    body, status_display = _render_task_block(
        task,
        decisions=[_decision("用 Electron")],
        artifacts=[_artifact("README.md")],
        summary_tail=[_summary_span("- 老金说想做个茶话室")],
        compact=False,
    )
    assert "标题：写 README" in body
    assert "目标：" in body and "把 README 写完" in body
    assert status_display == "进行中"
    # status 不再出现在 body 里——由调用方拼到 <current_task status="..."> 属性
    assert "状态：" not in body
    assert "负责人：汪小姐" in body
    assert "近期决策" in body and "用 Electron" in body
    assert "当前产物" in body and "README.md" in body
    # artifact label 强调 ./task/ 读路径 + 写走 task_write_artifact 工具
    assert "./task/" in body
    assert "task_write_artifact" in body
    assert "自动入任务" in body
    assert "任务近期进展" in body and "老金说想做个茶话室" in body


# ─── ② full vs compact 输出格式 ──────────────────────────────────────────────


def test_compact_block_short_header():
    task = _task()  # 默认 goal 非空 → 完整 3 行
    body, status_display = _render_task_block(task, [], [], [], compact=True)
    lines = body.split("\n")
    assert len(lines) == 3
    assert lines[0] == "标题：写 README"
    assert lines[1] == "目标：把 README 写完"
    # compact 文案告诉茶客 ./task/ 读 + 写走 task_write_artifact 工具
    assert "./task/" in lines[2]
    assert "task_write_artifact" in lines[2]
    assert "自动入任务" in lines[2]
    # compact 不含 full 块的小标题
    assert "近期决策" not in body
    assert "当前产物" not in body
    assert "任务近期进展" not in body
    assert "状态：" not in body
    # status_display 仍然返回，给调用方拼 XML 属性用
    assert status_display == "未开始"


def test_full_block_no_artifacts_no_decisions_no_summary():
    # 空 decisions / artifacts / summary_tail → 对应小节整段省略，不出现空"近期决策："等
    # 但 full 模式无 artifact 时会渲一行 ./task/ 当前为空 + task_write_artifact 落盘指引
    task = _task(owner=None)
    body, _ = _render_task_block(task, [], [], [], compact=False)
    assert "近期决策" not in body
    assert "任务近期进展" not in body
    assert "标题：写 README" in body
    assert "目标：" in body
    # 不变量：full 模式始终告诉茶客 ./task/ + task_write_artifact 工具 + 当前为空
    assert "./task/" in body
    assert "task_write_artifact" in body
    assert "当前为空" in body
    assert "自动入任务" in body


# ─── ③ owner=None 跳"负责人"行 ────────────────────────────────────────────────


def test_full_block_owner_none_skips_owner_line():
    task = _task(owner=None, status="open")
    body, status_display = _render_task_block(task, [], [], [], compact=False)
    assert status_display == "未开始"
    assert "状态：" not in body
    assert "负责人" not in body


def test_full_block_owner_user_renders_raw():
    # 渲染层不做 display name 映射；"user" 字面值原样喂给 LLM（wiring 层若要换 display
    # name 应在调用方传 task 前替换 owner，不在本函数）。
    task = _task(owner="user")
    body, _ = _render_task_block(task, [], [], [], compact=False)
    assert "负责人：user" in body


# ─── ④ decisions / artifacts / summary tail 各自截断 ─────────────────────────


def test_decisions_truncated_to_5_keep_last():
    decisions = [_decision(f"决策{i}", did=f"dec_{i}") for i in range(8)]
    body, _ = _render_task_block(_task(), decisions, [], [], compact=False)
    # 取末 5 条
    assert "决策3" in body  # 末 5 个：3,4,5,6,7
    assert "决策7" in body
    assert "决策2" not in body  # 第 3 条往前被砍
    assert "近期决策（最近 5 条）" in body


def test_artifacts_truncated_to_10_keep_first():
    artifacts = [_artifact(f"file{i:02d}.md") for i in range(15)]
    body, _ = _render_task_block(_task(), [], artifacts, [], compact=False)
    assert "file00.md" in body  # 前 10 个
    assert "file09.md" in body
    assert "file10.md" not in body
    assert "file14.md" not in body


def test_summary_tail_truncated_to_3_keep_last():
    spans = [_summary_span(f"- 摘要{i}", start=i, end=i + 1) for i in range(6)]
    body, _ = _render_task_block(_task(), [], [], spans, compact=False)
    assert "摘要3" in body  # 末 3 个：3,4,5
    assert "摘要5" in body
    assert "摘要2" not in body


# ─── ⑤ goal 多行 compact 只取首行 ────────────────────────────────────────────


def test_compact_goal_multiline_first_line_only():
    task = _task(goal="第一行目标\n第二行细节\n第三行更多细节")
    body, _ = _render_task_block(task, [], [], [], compact=True)
    assert "目标：第一行目标" in body
    assert "第二行细节" not in body
    assert "第三行更多细节" not in body


def test_compact_empty_goal_skips_goal_line():
    task = _task(goal="")
    body, _ = _render_task_block(task, [], [], [], compact=True)
    assert "标题：写 README" in body
    assert "目标：" not in body
    assert "./task/" in body


# ─── ⑥ 文案升级：compact 极简 vs full 详细 ──────────────────────────────────


def test_compact_artifact_hint_is_concise():
    """compact 路径每轮喂，文案极简 —— 软触发"你判断" + 专用工具名 + 边界提醒三句，
    不含 full 的类型枚举 / 命名示例 / 落盘原因等长内容。
    """
    body, _ = _render_task_block(_task(), [], [], [], compact=True)
    # 软触发 + 落盘动作（指明专用工具名）+ 边界提醒
    assert "你判断" in body  # 软触发（让 Agent 自主判断，不给硬阈值）
    assert "task_write_artifact" in body  # 落盘动作（专用工具名，绕开 PathPolicy）
    assert "聊天里只放一句概要" in body  # 边界提醒
    # 极简：不含 full 才有的类型枚举 / 命名建议 / 为什么详细段
    assert "评审意见" not in body  # 类型枚举留给 full（onboarding 看过即可）
    assert "命名建议" not in body
    assert "何时该落盘" not in body
    assert "为什么" not in body  # full 路径才解释"为什么要落盘"
    # 不再给字数硬阈值（让 Agent 自主判断）
    assert "200 字" not in body


def test_full_empty_artifact_hint_has_four_sections():
    """full 路径无 artifact 时给详细版：触发 / 命名 / 为什么 四段，鼓励 onboarding
    阶段就建立"长内容落盘"习惯。触发条件用软描述"你判断"，类型枚举只作锚点。
    """
    body, _ = _render_task_block(_task(owner=None), [], [], [], compact=False)
    # 四段标题（markdown 加粗）
    assert "**何时该落盘**" in body
    assert "**命名建议**" in body
    assert "**为什么**" in body
    # 软触发（不再给"超过 200 字"硬阈值——茶客自主判断）
    assert "你判断" in body
    assert "200 字" not in body
    # 类型枚举作为锚点示例（不是硬规则）
    assert "评审意见" in body or "决策清单" in body or "报告草稿" in body
    # 命名示例（含角色 placeholder + 版本号）
    assert "你的名字" in body
    # 原有不变量（向后兼容老断言）仍在
    assert "./task/" in body
    assert "task_write_artifact" in body  # 专用工具名替代旧"可读写"措辞
    assert "当前为空" in body
    assert "自动入任务" in body  # 包含在"自动入任务产物清单"里


def test_full_with_artifacts_includes_when_to_save_again_hint():
    """full 路径已经有 artifact 时，提醒"何时该再落盘 + 命名习惯"，避免茶客把后续
    报告又只塞回聊天。同样用"你判断"软触发，不给字数硬阈值。
    """
    body, _ = _render_task_block(
        _task(),
        [],
        [_artifact("水文献-评审-v1.md")],
        [],
        compact=False,
    )
    # 已有产物清单照常渲染
    assert "当前产物" in body
    assert "水文献-评审-v1.md" in body
    # 升级文案：催"再落盘"
    assert "再落盘" in body or "何时该再落盘" in body
    # 软触发（不再 "200 字"）
    assert "你判断" in body
    assert "200 字" not in body
    assert "命名建议" in body
    # 边界提醒仍在
    assert "./share/" in body


def test_full_block_significantly_longer_than_compact():
    """compact 极简 vs full 详细的分层验证 —— 同 task 同上下文下，full 显著长于 compact。

    阈值：
    - full empty ≥ compact 的 2×（onboarding 空产物，最积极催落盘）。
    - full with artifacts ≥ compact 的 1.5×（已有产物作为视觉证据，催"再落盘"文案略短）。
    这是文案分层的硬底线 —— 防止后续维护时把 full 的内容回流到 compact 让每轮都涨 token，
    或反过来把 full 削得跟 compact 没差。
    """
    compact_body, _ = _render_task_block(_task(), [], [], [], compact=True)
    full_empty_body, _ = _render_task_block(_task(owner=None), [], [], [], compact=False)
    full_with_artifacts_body, _ = _render_task_block(
        _task(), [], [_artifact("a.md")], [], compact=False,
    )
    assert len(full_empty_body) >= 2 * len(compact_body), (
        f"full empty ({len(full_empty_body)}) 应至少是 compact ({len(compact_body)}) 的 2×"
    )
    assert len(full_with_artifacts_body) >= 1.5 * len(compact_body), (
        f"full with artifacts ({len(full_with_artifacts_body)}) "
        f"应至少是 compact ({len(compact_body)}) 的 1.5×"
    )
