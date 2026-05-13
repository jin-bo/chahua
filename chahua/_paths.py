"""共享的路径解析 helper。

chahua 里多处需要「相对 repo_root 解析路径」（cli 的 ``--room``、config 的 persona、
user_md 的 ``explicit``）。idiom 之前被复制了三次且 ``.resolve()`` 策略不一致 ——
集中到这里一处。
"""

from __future__ import annotations

from pathlib import Path
from typing import Union


def resolve_under(base: Path, path: Union[str, Path]) -> Path:
    """绝对路径原样返回；相对路径拼到 ``base`` 下。

    **不**调用 ``.resolve()`` —— 留给调用方决定（``load_room_config`` 要解析符号链接
    和 ``..``，banner 显示则保留原写法）。
    """
    p = Path(path)
    return p if p.is_absolute() else (base / p)
