"""P10.2：把前端 ``flint_core.test.mjs`` 接进 ``uv run pytest``（决策P10.2-12）。

本仓**没有 CI、也没有第二个 JS 测试入口**，``uv run pytest`` 是唯一常跑的门 —— 不接进来的
JS 用例的命运和 P10.1 那 12 项 marked 用例一样：跑过一次，然后消失。故这里用 subprocess
起一次 ``node --test``，非 0 即 fail、把 node 的输出原样贴进 pytest 报告。

被测文件是 ``app/renderer/flint_core.mjs``（纯逻辑、零浏览器 API）；DOM 半边
（``flint_chart.js``）走 P10.2 §7 的 Electron 手测，不在这里。

``.mjs`` 而非 ``.js`` 是承重的：``app/package.json`` 没有 ``"type": "module"`` 且**不能加**
（``main`` 是 Electron 主进程的 CommonJS），于是 ``renderer/*.js`` 在 Node 眼里是 CJS，
``.js`` 直接 import 在 Node 18 上是 SyntaxError；而本机 Node 22.7+ 的语法探测会猜成 ESM
跑过去 —— 本机绿、下限红，最坏的那种假绿。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_FILE = REPO_ROOT / "app" / "renderer" / "flint_core.test.mjs"


def test_flint_core_node_suite() -> None:
    node = shutil.which("node")
    if node is None:
        # 唯一允许的 skip：没有 node 就没有任何办法跑 JS。
        pytest.skip("未安装 node —— 前端用例只在有 node 的环境跑（打包与开发环境必有）")
    if not (REPO_ROOT / "app" / "node_modules" / "flint-chart").is_dir():
        # **有 node 却缺依赖时 fail 而不是 skip**（评审修）：`uv sync` 从不装 app/node_modules，
        # 若这里也 skip，一台装了 node 但没跑过 `npm install` 的机器上 `uv run pytest` 会打出
        # 一片绿而这 30+ 条用例根本没跑（pytest 不加 -rs 连 skip 原因都不显示）——
        # 那正是决策P10.2-12 要防的「写下的用例只是文档」。修法是一行 npm install。
        pytest.fail("app/node_modules 未安装，前端用例无法运行 —— 请先 `cd app && npm install`")

    proc = subprocess.run(
        [node, "--test", str(TEST_FILE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        pytest.fail(f"node --test 失败（returncode={proc.returncode}）：\n{proc.stdout}\n{proc.stderr}")
