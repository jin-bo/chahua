"""验证 P5.4 文档"./task/ 软链对茶客可读可写"声明是否成立。

CLAUDE.md 声明：
    `./task/` 软链对茶客**可读可写**（agentao cwd 边界对软链解析后位置不拦写）

但 agentao 0.4.6 的 ``security/path_policy.py::PathPolicy.contain_file`` 会：
1. ``candidate.parent.resolve()`` 跟随 parent chain 上的 symlink
2. ``resolved.is_relative_to(project_root)`` 检查解析后路径是否在 working_directory 下

茶客 working_directory = ``<room>/guests/<name>/``、task 软链解析到 ``<room>/tasks/<id>/artifacts/``——
后者 **不在** 前者之下，所以 ``contain_file`` 应该抛 ``PathPolicyError``。

本测试直接调 agentao ``WriteFileTool`` 验证：

① 读 ``./task/x.md``（ReadFileTool 不走 contain_file）→ 应该成功
② 写 ``./task/x.md``（WriteFileTool 调 contain_file）→ 应该返 ``"Error: PathPolicy..."``
③ 写到 cwd 直接子文件 ``./x.md`` → 正常成功（对照组，验证 working_directory 装的对）

若 ② 成立 → P5.4 文案推荐茶客"用文件写工具落到 `./task/<name>`"实际**会失败**，
``_kick_detect_new_artifacts`` 永远扫不到茶客产物。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentao.tools.file_ops import ReadFileTool, WriteFileTool


def _setup_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """搭一份模拟茶客 cwd 布局：

    - ``<tmp>/room/guests/test_guest/`` —— 茶客 working_directory
    - ``<tmp>/room/tasks/t1/artifacts/`` —— 任务 artifacts 真实目录
    - ``<tmp>/room/guests/test_guest/task`` → 软链指向上者

    返回 ``(guest_wd, artifacts_dir, task_link)``。
    """
    room = tmp_path / "room"
    guest_wd = room / "guests" / "test_guest"
    artifacts_dir = room / "tasks" / "t1" / "artifacts"
    guest_wd.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    task_link = guest_wd / "task"
    task_link.symlink_to(artifacts_dir, target_is_directory=True)
    return guest_wd, artifacts_dir, task_link


def test_baseline_write_to_cwd_child_succeeds(tmp_path: Path):
    """对照组：写到 cwd 直接子文件不被拦——证明 working_directory 装得对。"""
    guest_wd, _, _ = _setup_layout(tmp_path)
    tool = WriteFileTool()
    tool.working_directory = guest_wd
    result = tool.execute("./direct.md", "hello-direct")
    assert "Error" not in result, f"对照组应成功，但: {result}"
    assert (guest_wd / "direct.md").read_text() == "hello-direct"


def test_read_via_task_softlink_works(tmp_path: Path):
    """读 ``./task/<name>``：ReadFileTool 不调 PathPolicy.contain_file，应能读出来。

    这条不是 bug，是设计 —— 让茶客能看到 artifacts/ 现状（list / read），
    问题只在写路径。
    """
    guest_wd, artifacts_dir, _ = _setup_layout(tmp_path)
    (artifacts_dir / "preexisting.md").write_text("from outside")
    tool = ReadFileTool()
    tool.working_directory = guest_wd
    result = tool.execute("./task/preexisting.md")
    assert "from outside" in result, f"读应成功，但: {result!r}"
    assert "PathPolicy" not in result
    assert "Error" not in result


def test_write_via_task_softlink_blocked_by_path_policy(tmp_path: Path):
    """**核心验证**：写 ``./task/<name>`` 被 PathPolicy 拦截。

    若此 assert 成立 → P5.4 文档 / CLAUDE.md / task block 文案推荐茶客"用文件
    写工具落到 ./task/<name>"实际**不可行**，需走方案 A（chahua 专属
    task_write_artifact 工具绕开 agentao path_policy）。

    若此 assert 失败（即写竟然成功）→ 说明 path_policy 没拦 / 链没生效，
    P5.4 文案是对的，本 bug 不存在。
    """
    guest_wd, artifacts_dir, _ = _setup_layout(tmp_path)
    tool = WriteFileTool()
    tool.working_directory = guest_wd
    result = tool.execute("./task/blocked.md", "hello-task")

    # 写应被拒
    assert "Error" in result or "refused" in result, (
        f"预期 write_file('./task/...') 被 PathPolicy 拦截，但实际: {result!r}"
    )
    assert "PathPolicy" in result or "outside" in result, (
        f"错误信息应来自 PathPolicy，但实际: {result!r}"
    )
    # 文件**未**被写到 artifacts/
    assert not (artifacts_dir / "blocked.md").exists(), (
        "PathPolicy 应阻止写入，但 artifacts/ 下已出现该文件"
    )


def test_write_via_share_softlink_also_blocked(tmp_path: Path):
    """对比验证：``./share/`` 软链同样被 PathPolicy 拦截（与 ./task/ 同病）。

    用 ``share/`` 链做参照说明这不是任务房间特有的问题，而是所有"指向 cwd 外的
    软链"都中招。
    """
    guest_wd, _, _ = _setup_layout(tmp_path)
    # 加一条 share 软链：<room>/guests/test_guest/share → <room>/share
    share_real = tmp_path / "room" / "share"
    share_real.mkdir(parents=True, exist_ok=True)
    (guest_wd / "share").symlink_to(share_real, target_is_directory=True)

    tool = WriteFileTool()
    tool.working_directory = guest_wd
    result = tool.execute("./share/blocked.md", "hello-share")
    assert "Error" in result or "refused" in result, (
        f"预期 write_file('./share/...') 被 PathPolicy 拦截，但实际: {result!r}"
    )
    assert not (share_real / "blocked.md").exists()
