"""共享的路径解析 helper（P3.3.2：拆 app_root / user_data_root）。

茶话室原先一切相对 ``repo_root`` 解析 —— dev 模式下 ``chahua/``、``rooms/``、
``USER.md``、``.env`` 全在仓库根下混着。打包后必须拆两半：

- **app_root**：只读 asset 根 —— ``chahua/personas/*.md/png`` 这种 ship 自带的资源。
  打包后 = ``Contents/Resources/...``；dev = 仓库根。
- **user_data_root**：用户数据根 —— ``rooms/``、``USER.md``、``.env``、用户自定义
  ``personas/``。打包后 = ``app.getPath('userData')``（macOS
  ``~/Library/Application Support/chahua/``）；dev = 仓库根（与 app_root 同源）。

dev 兼容：两个 env 都不设 → 都回退到包目录的父（仓库根），行为与 P3.3.1 之前 100% 一致。
Electron main 进程显式 export ``CHAHUA_APP_ROOT`` / ``CHAHUA_USER_DATA`` 给 sidecar，
两根独立解析。

persona 搜索：``room.toml`` 里 ``persona = "chahua/personas/宝总.md"`` 这种相对路径
按 ``find_in_data_then_app`` 双根搜 —— user_data 优先（用户可 override），fall through
到 app（bundle 自带）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union


# env var 名 —— Electron main 进程 + CI / 调试时 export 用。
ENV_APP_ROOT = "CHAHUA_APP_ROOT"
ENV_USER_DATA_ROOT = "CHAHUA_USER_DATA"


def resolve_under(base: Path, path: Union[str, Path]) -> Path:
    """绝对路径原样返回；相对路径拼到 ``base`` 下。

    **不**调用 ``.resolve()`` —— 留给调用方决定（``load_room_config`` 要解析符号链接
    和 ``..``，banner 显示则保留原写法）。
    """
    p = Path(path)
    return p if p.is_absolute() else (base / p)


def _dev_fallback_root() -> Path:
    """env 缺时的回退 = 包目录的父（dev 仓库根）。

    打包后 ``chahua/`` 包在 site-packages / asar 里，``parent.parent`` 不是用户数据 ——
    所以打包路径上 main 进程**必须**显式 export 两个 env，不能漏。
    """
    return Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class Paths:
    """app + user data 两个 root 的捆绑。一处构造、到处透传，省得每函数加两个参数。"""

    app_root: Path
    user_data_root: Path

    @classmethod
    def from_env(cls) -> "Paths":
        """从 :data:`ENV_APP_ROOT` / :data:`ENV_USER_DATA_ROOT` 构造；缺则 dev fallback。

        ``.resolve()`` 一次性吃掉 ``..`` 和符号链接，下游打 banner / 算 ``relative_to``
        都按归一形态走。
        """
        dev = _dev_fallback_root()
        env_app = os.environ.get(ENV_APP_ROOT)
        env_user = os.environ.get(ENV_USER_DATA_ROOT)
        return cls(
            app_root=Path(env_app).resolve() if env_app else dev,
            user_data_root=Path(env_user).resolve() if env_user else dev,
        )

    def find_in_data_then_app(self, rel: Union[str, Path]) -> Optional[Path]:
        """相对路径按 ``user_data_root → app_root`` 顺序搜，首个存在的返回。

        - 绝对路径：原样返回（仍校验文件存在；不存在返 ``None``）。
        - 相对路径：``user_data_root / rel`` 优先（用户自带 / override），fall through
          到 ``app_root / rel``（ship 自带）。两个都不存在返 ``None``。

        持久化 asset 类（personas / templates）走这里；用户独占数据（transcript /
        cursor / summary）不该走 —— 它们只在 ``user_data_root`` 下生成 + 读写。
        """
        p = Path(rel)
        if p.is_absolute():
            return p if p.is_file() else None
        for root in (self.user_data_root, self.app_root):
            candidate = root / p
            if candidate.is_file():
                return candidate
        return None
