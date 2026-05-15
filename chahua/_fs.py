"""文件系统软链 helper —— ``session.py`` 房间 share / ``persona_assets.py`` 茶客 skills
共用同一套幂等链 + 边角处理。

单点化的回报：Windows broken symlink / ``NotImplementedError`` / 旧 target 清理这几条
edge-case 修一处就同步，不再两边各自漂。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

_log = logging.getLogger(__name__)


def link_dir_idempotent(
    target: Path,
    source: Path,
    *,
    wipe_real_target: bool,
    label: str,
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
        _log.warning("%s: symlink %s → %s 失败：%s", label, target, source, e)
        return False
