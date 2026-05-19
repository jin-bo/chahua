"""persona 扫描 + 文件系统名规范化。

P6.x 重构：从 :mod:`chahua.admin` 抽出。本模块只做"找到磁盘上有哪些人格 / 把
任意字符串规范成合法目录名"两件事 —— 不涉及 room.toml 写出层（那是 :mod:`chahua.admin_room`
与 :mod:`chahua.admin_guest`）。

外部 import 通过 :mod:`chahua.admin` reexport（``from chahua.admin import
PERSONAS_REL_DIR, sanitize_fs_name`` 等老路径全部保留可用），单元测试也走 facade
路径，本文件**不**承诺其他模块直接 import 自己。
"""

from __future__ import annotations

import logging
import re
import tomllib
from pathlib import Path
from typing import Optional

from ._paths import Paths
from .config import read_avatar_data_uri
from .persona_assets import search_roots

_log = logging.getLogger(__name__)


# room.toml 里写法约定就是 `chahua/personas/<name>.md` —— discover 也按这个前缀走，
# 找到的文件相对路径里 persona_rel 字段直接可塞回 [[guest]].persona。
# 公开（无下划线）—— chahua.persona_import 也按这个前缀写盘，单点常量避免漂移。
PERSONAS_REL_DIR = Path("chahua/personas")


def _search_roots(paths: Paths) -> tuple[Path, Path, Path]:
    """与 :meth:`Paths.find_in_data_then_app` 同序的三档 root。

    单点定义委托给 :func:`chahua.persona_assets.search_roots` —— admin.discover_personas
    与 persona_relative 共享同一口径，避免漂移。
    """
    return search_roots(paths)


def _read_persona_toml_name(md_path: Path) -> Optional[str]:
    """从 persona md 的 sibling ``<stem>.toml`` 读 ``[guest].name`` —— picker 的"显示
    用名"来源。缺文件 / 解析失败 / 字段缺失都返 ``None``，调用方走 stem 兜底。

    导入 persona 时（:mod:`chahua.persona_import`）允许带一份 sidecar toml 写明
    人格的"对外名字"（如目录叫 ``Yvonne``，toml 里 ``name = "伊冯"``），picker / 添加
    入房间时显示用 toml 名。

    解析失败一律 WARN + 返 None：不让坏 toml 让整个 persona 在 picker 里消失。
    """
    toml_path = md_path.with_suffix(".toml")
    if not toml_path.is_file():
        return None
    try:
        with toml_path.open("rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as e:
        _log.warning("persona sidecar toml 解析失败，跳过：%s（%s）", toml_path, e)
        return None
    guest = data.get("guest")
    if not isinstance(guest, dict):
        return None
    name = guest.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip()
    return name or None


def discover_personas(paths: Paths) -> list[dict]:
    """扫所有 persona 候选，按 name 升序、user_data 优先 dedup。

    返回 `[{persona, name, avatar_data_uri}, ...]`：

    - `persona`：相对路径字符串 `chahua/personas/<dir>.md`（flat）或
      `chahua/personas/<dir>/<dir>.md`（dir form，import 出来的包），可塞
      `[[guest]].persona` 字段。
    - `name`：picker 显示用 + 加入房间后 `[[guest]].name` 字段值。优先取 sibling
      `<dir>.toml` 里 `[guest].name`（如 Yvonne.toml 写 ``name = "伊冯"``）；缺
      sidecar 时退到文件名 stem（同时也是大多数内置 persona 的形态）。
    - `avatar_data_uri`：与 md sibling 同名 `.png`；缺图返 `None`。

    给前端 sidebar"添加茶客"picker 用。

    **两种磁盘布局**：flat（`<Name>.md` 直接在 `personas/` 下）+ dir form（`<Name>/<Name>.md`
    在子目录里，sibling 还可能有 `mcp.json` / `skills/`）。dir form 由
    :func:`chahua.persona_import.import_from_folder` / :func:`import_from_github` 写出来；
    flat 是 ship-with-app 的内置 persona 形态。同名时 user_data 胜（dedup by display name）。
    """
    seen_by_name: dict[str, dict] = {}
    # 后写后覆盖等价 user_data 胜 —— 反序遍历保证 user_data 在 dict 里最后落定。
    for root in reversed(_search_roots(paths)):
        personas_dir = root / PERSONAS_REL_DIR
        if not personas_dir.is_dir():
            continue
        # flat：`<Name>.md`
        for md in sorted(personas_dir.glob("*.md")):
            name = _read_persona_toml_name(md) or md.stem
            seen_by_name[name] = {
                "persona": str(PERSONAS_REL_DIR / md.name),
                "name": name,
                "avatar_data_uri": read_avatar_data_uri(md.with_suffix(".png")),
            }
        # dir form：`<Name>/<Name>.md`（与目录同名）；找不到同名 md 时退化为目录里
        # 唯一一份 `*.md`，与 persona_import._derive_name 的兼容口径一致。
        for sub in sorted(personas_dir.iterdir()):
            if not sub.is_dir():
                continue
            md = sub / f"{sub.name}.md"
            if not md.is_file():
                # 兼容：目录名 ≠ md 文件名时，取目录里唯一一份 *.md。
                mds = [p for p in sub.iterdir() if p.is_file() and p.suffix.lower() == ".md"]
                if len(mds) != 1:
                    continue
                md = mds[0]
            name = _read_persona_toml_name(md) or sub.name
            seen_by_name[name] = {
                "persona": str(PERSONAS_REL_DIR / sub.name / md.name),
                "name": name,
                "avatar_data_uri": read_avatar_data_uri(md.with_suffix(".png")),
            }
    return [seen_by_name[n] for n in sorted(seen_by_name)]


# ── room_id 规范化 ────────────────────────────────────────────────────────


# 文件系统不友好的字符全部丢掉 / 替换。中文 / 拉丁 / 数字 / 破折号 / 下划线保留。
# 不允许 `..` / 绝对路径 / 路径分隔符 —— 防止 traversal 写出 rooms/ 目录外。
_FS_NAME_FORBIDDEN = re.compile(r"[\x00-\x1f<>:\"/\\|?*]")
_FS_NAME_TRIM = re.compile(r"^[.\s]+|[.\s]+$")


def sanitize_fs_name(raw: str, *, label: str = "name") -> str:
    """把任意字符串规范成合法目录名。空 / 全点 → ``ValueError``。

    规则：禁用字符 → ``-``；首尾去空白和点；空 / ``.`` / ``..`` → 拒（防 traversal）。
    room_id 与 persona 名（:mod:`chahua.persona_import`）共用同一套 FS 兼容规则。
    """
    s = _FS_NAME_FORBIDDEN.sub("-", raw)
    s = _FS_NAME_TRIM.sub("", s)
    if not s or s in (".", ".."):
        raise ValueError(f"{label} 非法（去掉文件系统禁用字符后为空）：{raw!r}")
    return s


def normalize_room_id(raw: str) -> str:
    """``sanitize_fs_name`` 的 room_id 化身。错误信息走 ``label="room_id"``。"""
    return sanitize_fs_name(raw, label="room_id")
