"""``chahua-server`` 命令入口（argv 解析 / serve / 父进程 watch / Windows tree-kill）。

P6.x 重构：从 :mod:`chahua.server` 抽出 —— 让 ``server.py`` 收敛到 ``ChahuaServer`` 类
+ 路由表 + slot 装配，本模块独自处理"进程生命周期"层（与房间业务无耦合）。

为兼容历史导入路径，:mod:`chahua.server` 末尾 ``from .server_entry import ...``
再 reexport ``main`` / ``_owner_pid_from_env`` —— pyproject 的
``chahua-server = "chahua.server:main"`` 与 ``tests/test_server_owner_pid.py`` 都
继续可用。

关停三条触发路径（**改动这里前先看 docs P3.3.2.d**）：

1. SIGINT / SIGTERM ``add_signal_handler``（POSIX）—— CLI 用户 Ctrl-C / kill <pid>。
2. ``KeyboardInterrupt`` —— 上层 :func:`main` catch；``asyncio.run`` 自带 cancel。
3. **stdin EOF watcher**（POSIX）或 **parent-pid watcher**（Windows）——
   Electron 关 sidecar 调 ``child.stdin.end()``，sidecar 这边读到 EOF 即 stop；Windows
   ``ProactorEventLoop`` 上 ``connect_read_pipe(sys.stdin)`` 常拿 ``WinError 6`` 静默
   失败，改走 ``OpenProcess + WaitForSingleObject`` 监 ``CHAHUA_PARENT_PID``。
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

from ._paths import Paths, resolve_under
from .config import RoomConfigError
from .server import (
    ChahuaServer,
    DEFAULT_HOST,
    DEFAULT_PORT,
)
from .session import (
    DEFAULT_ROOM_REL,
    build_room_session,
    load_env_files,
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="chahua-server",
        description="多 Agent 群聊「茶话室」WebSocket server（P2.3）",
    )
    parser.add_argument(
        "--room",
        type=Path,
        default=DEFAULT_ROOM_REL,
        help=(
            f"房间目录，含 room.toml（默认 {DEFAULT_ROOM_REL}）。"
            f"相对路径相对 user_data_root（CHAHUA_USER_DATA 或 dev 仓库根），"
            f"绝对路径原样。"
        ),
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"绑定地址（默认 {DEFAULT_HOST}，仅本机回环）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            f"监听端口（默认从 CHAHUA_WS_PORT 读，未设则 {DEFAULT_PORT}）"
        ),
    )
    return parser.parse_args(argv)


async def _serve(args: argparse.Namespace) -> int:
    paths = Paths.from_env()
    load_env_files(paths)

    room_dir = resolve_under(paths.user_data_root, args.room)
    try:
        session = build_room_session(room_dir, paths=paths)
    except RoomConfigError as e:
        print(f"房间配置错误：\n{e}", file=sys.stderr)
        return 2

    # 端口优先级：CLI > env > 默认。
    port = args.port
    if port is None:
        env_port = os.environ.get("CHAHUA_WS_PORT")
        port = int(env_port) if env_port else DEFAULT_PORT

    # 启动日志打出两个 root —— 打包后区分 dev / packaged 路径走的是哪条，排查方便。
    if paths.app_root != paths.user_data_root:
        print(f"app_root      : {paths.app_root}", file=sys.stderr)
        print(f"user_data_root: {paths.user_data_root}", file=sys.stderr)

    server = ChahuaServer(session, host=args.host, port=port, paths=paths)
    stop = asyncio.Event()

    # 三条触发 stop 的路径见模块 docstring。
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    # 平台分流：POSIX 走 stdin EOF；Windows 走 parent-pid watcher。
    stdin_watcher_task: Optional[asyncio.Task] = None
    if os.name != "nt" and not sys.stdin.isatty():
        stdin_watcher_task = asyncio.create_task(_watch_stdin_eof(stop))
    parent_watcher_task: Optional[asyncio.Task] = None
    if os.name == "nt":
        owner_pid = _owner_pid_from_env()
        if owner_pid > 0:
            parent_watcher_task = asyncio.create_task(
                _watch_parent_process(stop, owner_pid)
            )

    try:
        await server.serve_forever(stop)
    finally:
        # 关 server 持有的当前 session（换房后 self._session 已不是局部 `session`）。
        server.close()
        if stdin_watcher_task and not stdin_watcher_task.done():
            stdin_watcher_task.cancel()
        if parent_watcher_task and not parent_watcher_task.done():
            parent_watcher_task.cancel()
    return 0


async def _watch_stdin_eof(stop: asyncio.Event) -> None:
    """监 sys.stdin EOF，作为跨平台 sidecar 优雅关停信号。

    Electron main 进程关 sidecar 前调 ``child.stdin.end()`` 关 stdin pipe 的写端
    （sidecar.js:stop）；Python 这边读到 EOF 即 set stop，server.serve_forever 返回
    → 整套 graceful 关。Windows 的 ``child.kill("SIGINT")`` 实际是
    TerminateProcess（不 graceful），全靠这条路径替代。

    ``connect_read_pipe`` 在 Unix / Windows ProactorEventLoop 都支持；少数边角设置下
    可能失败（如 dev tty 模式调到这里，但我们已经在调用方 ``isatty()`` 过滤；保留
    try/except 兜底 OSError / NotImplementedError）。
    """
    log = logging.getLogger(__name__)
    loop = asyncio.get_running_loop()
    try:
        reader = asyncio.StreamReader(loop=loop)
        protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    except (OSError, NotImplementedError) as e:
        log.debug("stdin watcher disabled: %s", e)
        return

    # 父进程往 stdin 写东西不该触发关停（保留未来"命令通道"扩展空间，比如 main
    # 想塞个 ``{"type":"switch_room"}`` 进来）—— 只 EOF（空 bytes）才 set stop。
    while not stop.is_set():
        try:
            data = await reader.read(1024)
        except asyncio.CancelledError:
            return
        if not data:
            log.info("stdin EOF received; shutting down")
            stop.set()
            return


def _owner_pid_from_env() -> int:
    """Electron 通过 ``CHAHUA_PARENT_PID`` 显式喂自己的 PID 给 sidecar 用作 owner。

    返回 0 = 没有可监控的 owner（独立跑 ``uv run chahua-server`` 时常态）。**不**回退
    到 ``os.getppid()`` —— dev 模式 ppid 指向 wrapper（``uv.exe`` / shell），监它退出
    会让 sidecar 在不该退的时刻退（比如 PowerShell 关掉但 Electron 还活着）。
    """
    raw = os.environ.get("CHAHUA_PARENT_PID")
    if not raw:
        return 0
    try:
        pid = int(raw)
    except ValueError:
        logging.getLogger(__name__).debug("invalid CHAHUA_PARENT_PID=%r", raw)
        return 0
    return pid if pid > 0 else 0


async def _watch_parent_process(stop: asyncio.Event, parent_pid: int) -> None:
    """Windows 下监 owner 进程退出 → set stop。

    OpenProcess + WaitForSingleObject 的同步阻塞调用走 ``asyncio.to_thread`` 丢到
    执行线程，await 完成后回到事件循环主线程继续 set stop —— 不必走
    ``call_soon_threadsafe``。task 在 ``_serve.finally`` 里 cancel；CancelledError
    silent return，避免进程正常退出时这边补 ERROR 日志。
    """
    log = logging.getLogger(__name__)
    try:
        await asyncio.to_thread(_wait_for_parent_exit_windows, parent_pid)
    except asyncio.CancelledError:
        return
    except Exception as e:
        log.debug("parent watcher disabled: %s", e)
        return
    if not stop.is_set():
        log.info("parent process exited; shutting down")
        stop.set()


def _wait_for_parent_exit_windows(parent_pid: int) -> None:
    """阻塞等待 Windows owner 进程退出。仅由 :func:`_watch_parent_process` via to_thread 调用。

    ctypes argtypes / restype 必须显式声明：64 位 Windows 上 ``HANDLE`` 是指针
    （8 字节），ctypes 默认按 C ``int``（4 字节）截断，handle 高位被砍后
    ``WaitForSingleObject`` 拿到坏 handle 立刻返 ``WAIT_FAILED`` 让 sidecar 启动
    秒退。这是隐性 bug，不写 argtypes 在小 PID 下偶然能跑、handle 高位非零时翻车。
    """
    if parent_pid <= 0:
        return

    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    SYNCHRONIZE = 0x00100000
    INFINITE = 0xFFFFFFFF

    handle = k32.OpenProcess(SYNCHRONIZE, False, parent_pid)
    if not handle:
        # PID 不存在 / 权限不足 —— 没法监控就不监，不当 ERROR（owner 已经死了
        # 也走这条路径，调用方靠 stop 没被 set 来推断）。
        return
    try:
        k32.WaitForSingleObject(handle, INFINITE)
    finally:
        k32.CloseHandle(handle)


def main() -> None:
    """``chahua-server`` 命令入口。"""
    args = _parse_args(sys.argv[1:])
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        rc = asyncio.run(_serve(args))
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
