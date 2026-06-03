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
    task = _task(owner="汪小姐", status="doing")
    body, status_display = _render_task_block(
        task,
        decisions=[_decision("用 Electron")],
        artifacts=[_artifact("README.md")],
        summary_tail=[_summary_span("- 老金说想做个茶话室")],
        compact=False,
    )
    assert "Title: 写 README" in body
    assert "Goal:" in body and "把 README 写完" in body
    assert status_display == "in progress"
    # status 不再出现在 body 里——由调用方拼到 <current_task status="..."> 属性
    assert "状态：" not in body
    assert "Owner: 汪小姐" in body
    assert "Recent decisions" in body and "用 Electron" in body
    assert "Current artifacts:" in body and "README.md" in body
    # artifact 指引强调 ./task/ 读路径 + 写走 task_write_artifact 工具（P14 精练后承重点）
    assert "./task/" in body
    assert "read_file" in body
    assert "task_write_artifact" in body
    assert "NOT write_file" in body
    assert "Recent task progress:" in body and "老金说想做个茶话室" in body


# ─── ② full vs compact 输出格式 ──────────────────────────────────────────────


def test_compact_block_short_header():
    task = _task()  # 默认 goal 非空 → header 2 行 + ./task/ 指引
    body, status_display = _render_task_block(task, [], [], [], compact=True)
    lines = body.split("\n")
    assert lines[0] == "Title: 写 README"
    assert lines[1] == "Goal: 把 README 写完"
    # compact 文案告诉茶客 ./task/ 读 + 写走 task_write_artifact 工具（P14 精练 2 行）
    guidance = "\n".join(lines[2:])
    assert "./task/" in guidance
    assert "read_file" in guidance
    assert "task_write_artifact" in guidance
    assert "NOT write_file" in guidance
    assert "summary + file ref" in guidance  # 概要 + 引用承重点
    # compact 不含 full 块的小标题
    assert "Recent decisions" not in body
    assert "Current artifacts:" not in body
    assert "Recent task progress:" not in body
    assert "状态：" not in body
    # status_display 仍然返回，给调用方拼 XML 属性用
    assert status_display == "not started"


def test_full_block_no_artifacts_no_decisions_no_summary():
    # 空 decisions / artifacts / summary_tail → 对应小节整段省略，不出现空"近期决策："等
    # 但 full 模式无 artifact 时会渲一行 ./task/ 当前为空 + task_write_artifact 落盘指引
    task = _task(owner=None)
    body, _ = _render_task_block(task, [], [], [], compact=False)
    assert "Recent decisions" not in body
    assert "Recent task progress:" not in body
    assert "Title: 写 README" in body
    assert "Goal:" in body
    # 不变量：full 模式始终告诉茶客 ./task/ + task_write_artifact 工具 + 当前为空
    assert "./task/" in body
    assert "read_file" in body
    assert "task_write_artifact" in body
    assert "No artifacts yet" in body


# ─── ③ owner=None 跳"负责人"行 ────────────────────────────────────────────────


def test_full_block_owner_none_skips_owner_line():
    task = _task(owner=None, status="open")
    body, status_display = _render_task_block(task, [], [], [], compact=False)
    assert status_display == "not started"
    assert "状态：" not in body
    assert "Owner" not in body


def test_full_block_owner_user_renders_raw():
    # 渲染层不做 display name 映射；"user" 字面值原样喂给 LLM（wiring 层若要换 display
    # name 应在调用方传 task 前替换 owner，不在本函数）。
    task = _task(owner="user")
    body, _ = _render_task_block(task, [], [], [], compact=False)
    assert "Owner: user" in body


# ─── ④ decisions / artifacts / summary tail 各自截断 ─────────────────────────


def test_decisions_truncated_to_5_keep_last():
    decisions = [_decision(f"决策{i}", did=f"dec_{i}") for i in range(8)]
    body, _ = _render_task_block(_task(), decisions, [], [], compact=False)
    # 取末 5 条
    assert "决策3" in body  # 末 5 个：3,4,5,6,7
    assert "决策7" in body
    assert "决策2" not in body  # 第 3 条往前被砍
    assert "Recent decisions (last 5):" in body


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
    assert "Goal: 第一行目标" in body
    assert "第二行细节" not in body
    assert "第三行更多细节" not in body


def test_compact_empty_goal_skips_goal_line():
    task = _task(goal="")
    body, _ = _render_task_block(task, [], [], [], compact=True)
    assert "Title: 写 README" in body
    assert "Goal:" not in body
    assert "./task/" in body


# ─── ⑥ 文案升级：compact 极简 vs full 详细 ──────────────────────────────────


def test_compact_artifact_hint_is_concise():
    """compact 路径每轮喂，文案极简（P14 精练后 2 行祈使）——承重点：专用工具名 +
    边界否定 + 概要引用；不含 full 才有的「持久化类型清单 / 命名示例」长内容。
    """
    body, _ = _render_task_block(_task(), [], [], [], compact=True)
    # 承重点：落盘动作（专用工具名，绕开 PathPolicy）+ 否定 write_file + 概要引用
    assert "task_write_artifact" in body
    assert "NOT write_file" in body
    assert "summary + file ref" in body
    # 极简：不含 full 才有的类型清单 / 命名示例
    assert "reviews, designs" not in body  # 持久化类型清单留给 full
    assert "Name with role" not in body  # 命名示例留给 full


def test_full_detailed_guidance_has_persist_and_naming():
    """full 路径给详细版（P14 精练后）：承重点除 compact 的三点外，多「持久化类型清单」
    与「命名示例」，鼓励 onboarding 阶段就建立长内容落盘习惯。
    """
    body, _ = _render_task_block(_task(owner=None), [], [], [], compact=False)
    # full 专属：持久化类型清单 + 命名示例（含角色 placeholder + 版本号）
    assert "Persist any reusable output" in body
    assert "reviews, designs" in body
    assert "Name with role + version" in body
    assert "<you>" in body
    # 承重点仍在
    assert "./task/" in body
    assert "read_file" in body
    assert "task_write_artifact" in body
    assert "NOT write_file" in body
    assert "No artifacts yet" in body


def test_full_with_artifacts_keeps_guidance():
    """full 路径已经有 artifact 时，产物清单 + 落盘指引同时在 —— 避免茶客把后续
    报告又只塞回聊天。
    """
    body, _ = _render_task_block(
        _task(),
        [],
        [_artifact("水文献-评审-v1.md")],
        [],
        compact=False,
    )
    # 已有产物清单照常渲染
    assert "Current artifacts:" in body
    assert "水文献-评审-v1.md" in body
    # 落盘指引（承重点 + 命名示例）仍在
    assert "task_write_artifact" in body
    assert "Persist any reusable output" in body
    assert "Name with role + version" in body


def test_full_block_longer_than_compact():
    """compact 极简 vs full 详细的分层验证 —— P14 精练后两者都收敛，但 full 仍比
    compact 长（full 多「持久化类型清单 + 命名示例」，with-artifacts 再多产物清单）。

    这是文案分层的底线 —— 防止后续维护把 full 的内容回流到 compact 让每轮都涨 token，
    或反过来把 full 削得跟 compact 没差。精练后不再用 2×/1.5× 硬倍数（已失效），改为
    严格「full > compact」。
    """
    compact_body, _ = _render_task_block(_task(), [], [], [], compact=True)
    full_empty_body, _ = _render_task_block(_task(owner=None), [], [], [], compact=False)
    full_with_artifacts_body, _ = _render_task_block(
        _task(), [], [_artifact("a.md")], [], compact=False,
    )
    assert len(full_empty_body) > len(compact_body), (
        f"full empty ({len(full_empty_body)}) 应长于 compact ({len(compact_body)})"
    )
    assert len(full_with_artifacts_body) > len(compact_body), (
        f"full with artifacts ({len(full_with_artifacts_body)}) "
        f"应长于 compact ({len(compact_body)})"
    )
