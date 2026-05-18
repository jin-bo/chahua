"""任务事件合成"用户消息"的文案构造（P5.5，docs/P5.5-任务事件全房广播.md §4.4）。

P5.5 把 task open / close / add_decision / attach_artifact / 茶客自动归集这 5 类
事件以"用户在 UI 上做了什么"为口吻合成一条 ``speaker_id="user"`` 的 transcript 行
让茶客在下一轮 prompt 里看见。文案集中到本模块单点维护 —— emoji 与句式有调整只
改这一个文件，防止 5 个调用点漂移。

设计要点：
- 纯函数，无副作用，便于单测。
- 不引入新 emoji 矩阵 / 标点细节验证，每函数 1 个 happy path 即可。
- 不含 ``update_task_text`` —— P5.5 不做 update 广播（要 diff old/new 才能挑出
  实质变更，复杂度不值；推到 §9 后续）。
- 不追踪 artifact 作者 —— 茶客自动归集只知道"目录里冒出新文件"，统一泛化
  "📎 茶客产出：..."，不为此引入跨组件作者追踪。
"""

from __future__ import annotations

from typing import Iterable

from .task import Task, Decision, TaskStatus


def open_task_text(task: Task) -> str:
    # 完整 goal 已在 ``<current_task>`` XML 块里，本文案只给"用户刚操作了"
    # 元描述信号，不重复完整 goal —— 但首屏 transcript 单看这条要能猜任务意图。
    if task.goal:
        return f"📋 用户开启任务【{task.title}】 · 目标：{task.goal}"
    return f"📋 用户开启任务【{task.title}】"


def close_task_text(task: Task, status: TaskStatus) -> str:
    if status == "done":
        return f"✅ 任务【{task.title}】已完成"
    return f"✗ 任务【{task.title}】已放弃"


def add_decision_text(decision: Decision) -> str:
    return f"📌 决策：{decision.summary}"


def attach_artifact_text(name: str) -> str:
    return f"📎 用户挂载产物：{name}"


def detect_artifacts_text(names: Iterable[str]) -> str:
    return f"📎 茶客产出：{', '.join(names)}"
