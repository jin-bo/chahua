"""P20 / P20.1：把 ``llm_calls_core.test.mjs`` / ``live_status_core.test.mjs`` 接进 ``uv run pytest``。

沿 ``tests/test_frontend_flint.py`` 先例（本仓无 CI，``uv run pytest`` 是唯一常跑的门）。
两个 ``.mjs`` 都零 npm 依赖，所以只在没装 node 时 skip，不查 ``app/node_modules``。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDERER = REPO_ROOT / "app" / "renderer"


@pytest.mark.parametrize("name", ["llm_calls_core.test.mjs", "live_status_core.test.mjs"])
def test_renderer_core_node_suite(name: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node —— 前端用例只在有 node 的环境跑（打包与开发环境必有）")
    proc = subprocess.run(
        [node, "--test", str(RENDERER / name)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        pytest.fail(f"node --test 失败（returncode={proc.returncode}）：\n{proc.stdout}\n{proc.stderr}")
