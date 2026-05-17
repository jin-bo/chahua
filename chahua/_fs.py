"""文件系统软链 helper —— ``session.py`` 房间 share / ``persona_assets.py`` 茶客 skills
共用同一套幂等链 + 边角处理。

单点化的回报：Windows broken symlink / ``NotImplementedError`` / 旧 target 清理这几条
edge-case 修一处就同步，不再两边各自漂。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

_log = logging.getLogger(__name__)


def link_dir_idempotent(
    target: Path,
    source: Path,
    *,
    wipe_real_target: bool,
    label: str,
    windows_junction_fallback: bool = False,
) -> bool:
    """让 ``target`` 软链到 ``source``，幂等。返回 True = 软链已就绪。

    ``target`` 已是指向 ``source`` 的软链 → 零写操作返 True。其它情况：

    - ``target`` 是别的软链 / 旧 broken 链 → unlink 后重链。
    - ``target`` 是真文件 / 真目录：
        ``wipe_real_target=True`` → 删后重链（独占内部位置如 ``.agentao/skills/``）。
        ``wipe_real_target=False`` → WARN 跳过返 False（用户工作区下的 share/ 等，
        不偷偷删用户数据）。

    所有失败（清理 / mkdir / symlink）走同一路径：WARN 返 False。Windows 普通用户
    没 ``SeCreateSymbolicLinkPrivilege`` 时 ``symlink_to`` 抛 ``OSError`` —— 调用方
    据返回值决定是否走 copy 兜底。

    ``windows_junction_fallback=True`` 时 Windows symlink 失败后再试 ``mklink /J``
    建 junction（reparse point，普通用户即可建，agentao cwd 边界仍能拦住越界 IO）。
    POSIX 上不走 fallback —— OSError 直接 WARN。任务房间 ``./task/`` 软链用这条；
    ``./share/`` 既有口径保持 WARN（broken share 比"意外突然能写"更安全）。
    """
    if target.is_symlink():
        try:
            if target.readlink() == source:
                return True
        except OSError:
            pass
        try:
            target.unlink()
        except OSError as e:
            _log.warning("%s: 清理旧软链 %s 失败：%s", label, target, e)
            return False
    elif target.exists():
        if not wipe_real_target:
            _log.warning(
                "%s: %s 是真文件/目录而非软链，跳过（保护用户数据）", label, target
            )
            return False
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as e:
            _log.warning("%s: 清理旧 %s 失败：%s", label, target, e)
            return False

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.symlink_to(source, target_is_directory=True)
        return True
    except (OSError, NotImplementedError) as e:
        if windows_junction_fallback and os.name == "nt":
            try:
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(target), str(source)],
                    check=True, capture_output=True,
                )
                return True
            except (OSError, subprocess.CalledProcessError) as e2:
                _log.warning(
                    "%s: junction fallback %s → %s 失败：%s", label, target, source, e2,
                )
                return False
        _log.warning("%s: symlink %s → %s 失败：%s", label, target, source, e)
        return False


_WINDOWS_REPARSE_POINT_ATTR = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


def unlink_managed_link(target: Path, *, label: str) -> bool:
    """删一个本模块管理的软链 / junction。target 不存在 / 不是链时 no-op 返 True。

    POSIX 走 ``is_symlink() → unlink()``；Windows 上 junction 是 reparse point，
    ``is_symlink()`` 返 False 但 ``lstat().st_file_attributes`` 带 reparse 位，
    走 ``rmdir()`` 拆链不动 target。真目录 / 真文件 WARN 跳过（保护用户数据）。
    """
    if target.is_symlink():
        try:
            target.unlink()
            return True
        except OSError as e:
            _log.warning("%s: unlink %s 失败：%s", label, target, e)
            return False
    if not target.exists():
        return True
    if os.name == "nt" and target.is_dir():
        try:
            attrs = getattr(target.lstat(), "st_file_attributes", 0)
            if attrs & _WINDOWS_REPARSE_POINT_ATTR:
                target.rmdir()
                return True
        except OSError as e:
            _log.warning("%s: rmdir junction %s 失败：%s", label, target, e)
            return False
    _log.warning("%s: %s 不是软链/junction，跳过（保护用户数据）", label, target)
    return False
