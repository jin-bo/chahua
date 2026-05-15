"""房间 / 茶客 增删改 + persona 扫描。

`chahua.config` 只读 `room.toml`；本模块管所有把 toml 写回去 / 建房 / 拆房的可变操作。
保持两边职责清晰 —— 加载期严格白名单（错配置宁可炸），运行时 mutator 走这里。

**TOML 写出口径**：自带一个**只覆盖本仓库 schema** 的 mini 序列化器（`[room]` + `[[guest]]`，
basic string 转义）。不引 `tomli_w` 是因为：

- 我们的 schema 极简（两段），手写 ~20 行；引依赖反而把 wheel 体积撑大；
- 任何手工注释 / 字段顺序在重写时无论如何都会丢，标准库 `tomllib` 也只读不带注释 —— 不
  欠用户"保留注释"的承诺，文档里写明"通过 UI 改了房间后 room.toml 会被规范化"即可。

**改完即重载**：所有 mutator 函数返回 `RoomConfig`（已经 `load_room_config` 跑过一遍校验）。
写盘失败 / 再加载失败都 raise 出去；调用方（server.py）负责 emit 错误给前端。
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import shutil
import tomllib
from pathlib import Path
from typing import Optional, Sequence

from ._paths import Paths
from ._persist import write_bytes_atomic, write_text_atomic
from .config import RoomConfig, RoomConfigError, load_room_config, read_avatar_data_uri
from .permissions import DEFAULT_MODE, VALID_MODES, is_valid_mode
from .persona_assets import persona_relative, search_roots

_log = logging.getLogger(__name__)


# ── persona 扫描 ──────────────────────────────────────────────────────────


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


# ── TOML 写 ──────────────────────────────────────────────────────────────


def _toml_basic_string(s: str) -> str:
    """转义一段字符串为 TOML basic string（双引号包裹）。

    覆盖 TOML 1.0 spec 要求的最小转义集 + 控制字符走 `\\uXXXX`。聊天室人名 / topic
    几乎都是 BMP 中日文 + 标点 + 偶尔表情，basic string 比 multi-line literal 更省事。
    """
    out: list[str] = []
    for c in s:
        if c == "\\":
            out.append("\\\\")
        elif c == "\"":
            out.append("\\\"")
        elif c == "\b":
            out.append("\\b")
        elif c == "\t":
            out.append("\\t")
        elif c == "\n":
            out.append("\\n")
        elif c == "\f":
            out.append("\\f")
        elif c == "\r":
            out.append("\\r")
        elif ord(c) < 0x20 or c == "\x7f":
            out.append(f"\\u{ord(c):04X}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def write_room_toml(
    room_dir: Path,
    *,
    name: str,
    topic: str,
    rules: str,
    guests: Sequence[dict],
) -> None:
    """把 room.toml 整个重写。原文件如有手工注释 / 排版会被丢弃（见模块 docstring 说明）。

    `guests` 每项必含 `name` / `persona`；`permission` 缺省 :data:`DEFAULT_MODE`。
    """
    lines: list[str] = ["[room]"]
    lines.append(f"name  = {_toml_basic_string(name)}")
    lines.append(f"topic = {_toml_basic_string(topic or '')}")
    lines.append(f"rules = {_toml_basic_string(rules or '')}")
    for g in guests:
        gname = str(g["name"])
        gpersona = str(g["persona"])
        gpermission = str(g.get("permission") or DEFAULT_MODE)
        lines.append("")
        lines.append("[[guest]]")
        lines.append(f"name       = {_toml_basic_string(gname)}")
        lines.append(f"persona    = {_toml_basic_string(gpersona)}")
        lines.append(f"permission = {_toml_basic_string(gpermission)}")
    text = "\n".join(lines) + "\n"
    write_text_atomic(room_dir / "room.toml", text)


# ── room.toml 读现状（mutator 用，区别于 config.load_room_config 的严格校验路径）─


def _read_existing_for_mutate(room_dir: Path, paths: Paths) -> dict:
    """读 room.toml 当前内容，返回标准化的 dict 形态（喂给 write_room_toml）。

    走 :func:`chahua.config.load_room_config` 而非裸 tomllib —— 保证 mutator 路径
    上看到的快照与启动期等价（白名单严格）；用户如果手 edit 出非法 toml，本函数也会
    抛 RoomConfigError，避免 mutator 把更乱的 toml 写回去。
    """
    rc = load_room_config(room_dir, paths=paths)
    return _room_config_to_dict(rc, paths)


def _room_config_to_dict(rc: RoomConfig, paths: Paths) -> dict:
    """`RoomConfig` → mutator 内部用的 dict 形态。

    `persona` 字段尽量还原成"相对 chahua/personas/"的形式 —— 用户在 toml 里那么写，
    我们 mutator 写回去也保持一致；但 `RoomConfig.GuestConfig.persona_path` 已 resolve
    成绝对路径，丢了原始相对写法。回退策略：如果 absolute path 在某个搜索根下，重算
    相对值；否则保留 absolute（兼容用户硬编绝对路径的极少数场景）。
    """
    return {
        "name": rc.name,
        "topic": rc.topic,
        "rules": rc.rules,
        "guests": [
            {
                "name": gc.name,
                "persona": _persona_to_relative(gc.persona_path, paths),
                "permission": gc.permission,
            }
            for gc in rc.guests
        ],
    }


def _persona_to_relative(persona_path: Path, paths: Paths) -> str:
    """绝对 persona_path → 相对 `chahua/personas/<x>.md`（如可能），保持 toml 写法稳定。

    单点委托给 :func:`chahua.persona_assets.persona_relative` —— admin 写 toml 与
    trust 索引、room_info envelope 三处的 persona 字符串 key 共享同一口径，避免漂移。
    """
    return persona_relative(persona_path, paths)


# ── room CRUD ────────────────────────────────────────────────────────────


def create_room(
    *,
    paths: Paths,
    room_id: str,
    name: str,
    topic: str = "",
    rules: str = "",
    guests: Sequence[dict],
) -> RoomConfig:
    """新建一个房间，落盘 + 校验。返回 `RoomConfig`。

    `room_id` 走 :func:`normalize_room_id` 规范化；目录已存在 → 拒绝（不覆盖用户已有
    房间）。`guests` 至少 1 位（与 `_build_guests` 的硬约束一致）。

    创建后立即 `load_room_config` 校验：persona 文件不存在等问题在这里就抛 ——
    避免把坏房间留在磁盘上。校验失败时 rmtree 回滚。
    """
    room_id = normalize_room_id(room_id)
    if not name.strip():
        raise ValueError("房间名不能为空")
    if not guests:
        raise ValueError("至少要有一位茶客")

    # name 缺省 = persona 文件名（与 add_guest 同口径）。前端可只传 persona 一个字段。
    normalized_guests: list[dict] = []
    for g in guests:
        gname = (g.get("name") or "").strip() or Path(str(g["persona"])).stem
        normalized_guests.append({
            "name": gname,
            "persona": str(g["persona"]),
            "permission": g.get("permission") or DEFAULT_MODE,
        })

    room_dir = paths.user_data_root / "rooms" / room_id
    # mkdir(exist_ok=False) 自身就会 raise FileExistsError；不另设 if exists 兜底。
    room_dir.mkdir(parents=True, exist_ok=False)
    try:
        write_room_toml(
            room_dir, name=name, topic=topic, rules=rules, guests=normalized_guests
        )
        return load_room_config(room_dir, paths=paths)
    except Exception:
        # 校验 / 写失败 → 回滚整个目录，避免半成品房间挂在 sidebar 列表里。
        shutil.rmtree(room_dir, ignore_errors=True)
        raise


def delete_room(*, paths: Paths, room_id: str, current_room_id: Optional[str]) -> None:
    """删除一个房间目录。当前房间不能删 —— 调用方需先切走。

    rmtree 整个目录，包含 transcript / cursor / summary / guests/ 子工作区。
    用户 confirm 已在前端做；这里只防御"删自己脚下的椅子"。
    """
    room_id = normalize_room_id(room_id)
    if room_id == current_room_id:
        raise ValueError(f"不能删除当前房间：{room_id}（请先切到别的房间）")
    # rmtree 自身在路径不存在时抛 FileNotFoundError；不另设 is_dir 兜底。
    shutil.rmtree(paths.user_data_root / "rooms" / room_id)


# ── guest CRUD ───────────────────────────────────────────────────────────


def add_guest(
    *,
    paths: Paths,
    room_dir: Path,
    persona: str,
    name: Optional[str] = None,
    permission: str = DEFAULT_MODE,
) -> RoomConfig:
    """往现有房间加一位茶客。返回新的 `RoomConfig`。

    `name` 缺省 = persona 文件名（不带 .md）。重名拒绝。persona 文件不存在 / 权限非法
    等问题靠校验路径（重新 `load_room_config`）兜底。
    """
    snapshot = _read_existing_for_mutate(room_dir, paths)
    if name is None or not name.strip():
        name = Path(persona).stem
    if any(g["name"] == name for g in snapshot["guests"]):
        raise ValueError(f"已有同名茶客：{name!r}")

    new_guest = {"name": name, "persona": persona, "permission": permission}
    new_guests = list(snapshot["guests"]) + [new_guest]
    return _rewrite_and_validate(room_dir, snapshot, new_guests, paths)


def remove_guest(*, paths: Paths, room_dir: Path, name: str) -> RoomConfig:
    """从现有房间移除一位茶客。返回新的 `RoomConfig`。

    最后一位茶客不能移（房间必须至少 1 位 —— `_build_guests` 的硬约束）。
    名字不在册 → ValueError。

    `guests/<name>/` 工作区不删 —— 茶客被移除后用户也许想要保留聊天产物 / 它的工具
    输出。需要的话用户自己手动 rm 那个子目录。
    """
    snapshot = _read_existing_for_mutate(room_dir, paths)
    new_guests = [g for g in snapshot["guests"] if g["name"] != name]
    if len(new_guests) == len(snapshot["guests"]):
        raise ValueError(f"茶客 {name!r} 不在房间里")
    if not new_guests:
        raise ValueError("不能移除最后一位茶客（房间至少要有 1 人）")
    return _rewrite_and_validate(room_dir, snapshot, new_guests, paths)


def update_guest_permission(
    *, paths: Paths, room_dir: Path, name: str, permission: str
) -> RoomConfig:
    """改一位茶客的 permission，返回新的 ``RoomConfig``。

    ``permission`` 必须在 :data:`chahua.permissions.VALID_MODES` 内（PLAN 等隐藏模式拒）；
    名字不在册 → ValueError。改完即 reload —— 单点在写盘到生效之间不存在"半个状态"。

    本函数只动 ``room.toml``。session 重装让新 permission 真正生效（茶客 close + 重建
    agentao instance）是调用方的事（server._update_guest_permission 走 _replace_session）。
    """
    if not is_valid_mode(permission):
        raise ValueError(f"permission={permission!r} 不在 {VALID_MODES} 内")
    snapshot = _read_existing_for_mutate(room_dir, paths)
    new_guests: list[dict] = []
    found = False
    for g in snapshot["guests"]:
        if g["name"] == name:
            new_guests.append({**g, "permission": permission})
            found = True
        else:
            new_guests.append(g)
    if not found:
        raise ValueError(f"茶客 {name!r} 不在房间里")
    return _rewrite_and_validate(room_dir, snapshot, new_guests, paths)


def _rewrite_and_validate(
    room_dir: Path, snapshot: dict, new_guests: list[dict], paths: Paths
) -> RoomConfig:
    """共用：写新 toml + 重新加载校验；失败回滚到 snapshot。"""
    write_room_toml(
        room_dir,
        name=snapshot["name"],
        topic=snapshot["topic"],
        rules=snapshot["rules"],
        guests=new_guests,
    )
    try:
        return load_room_config(room_dir, paths=paths)
    except RoomConfigError:
        # 回滚：把 snapshot 写回去，让 toml 与启动期看到的一致。
        write_room_toml(
            room_dir,
            name=snapshot["name"],
            topic=snapshot["topic"],
            rules=snapshot["rules"],
            guests=snapshot["guests"],
        )
        raise


# ── USER.md / 头像 mutator ────────────────────────────────────────────────


# USER.md 体量上限：64KB —— USER.md 本质是几段偏好 + 自我介绍，正常用户写 1KB 不到。
# 设上限是防御浏览器端 textarea 被脚本灌进去 / 误粘整个文档；超就拒，不静默截断。
_USER_MD_MAX_BYTES = 64 * 1024
# 头像上限：1.5MB（前端已经 canvas 压缩到 256px PNG，正常 ~50KB；上限是兜底防御客户端
# 没压就直传整张原图 + base64 体积膨胀 4/3 倍）。
_AVATAR_MAX_BYTES = 1_500_000
# PNG 文件头 magic bytes（RFC 2083 §3.1）。`with_suffix(".png")` 的搜索约定让我们只接
# 受 PNG —— 其他格式（JPEG / WebP）写进 USER.png 会让 sidebar 渲染 broken image。
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _user_md_target(paths: Paths, source: Optional[Path]) -> Path:
    """USER.md 写盘目标：优先沿用 load_user_md 命中的 source（保留用户已有布局，
    比如 room 级 USER.md 或 explicit override）；都没有则落到 user_data_root/USER.md。
    """
    return source if source is not None else paths.user_data_root / "USER.md"


def update_user_md(paths: Paths, content: str, *, source: Optional[Path] = None) -> Path:
    """覆盖 USER.md 内容，返回实际写入的路径。

    内容超 :data:`_USER_MD_MAX_BYTES` → ValueError（防误粘整文档）。
    tmp + rename 保证写一半被 kill 不会留下半截 md。
    """
    encoded = content.encode("utf-8")
    if len(encoded) > _USER_MD_MAX_BYTES:
        raise ValueError(
            f"USER.md 太大（{len(encoded)} bytes，上限 {_USER_MD_MAX_BYTES}）"
        )
    target = _user_md_target(paths, source)
    write_text_atomic(target, content)
    return target


def parse_png_data_uri(data_uri: str) -> bytes:
    """`data:image/png;base64,...` → raw PNG bytes。

    严格验：必须是 PNG data URI、base64 合法、magic bytes 命中、体积在上限内。
    任何一项不过 → ValueError，调用方决定怎么 emit 给前端。
    """
    prefix = "data:image/png;base64,"
    if not data_uri.startswith(prefix):
        raise ValueError("仅接受 PNG data URI（前端 canvas.toDataURL('image/png')）")
    try:
        png = base64.b64decode(data_uri[len(prefix):], validate=True)
    except binascii.Error as e:
        raise ValueError(f"base64 解码失败：{e}") from e
    if len(png) > _AVATAR_MAX_BYTES:
        raise ValueError(
            f"头像太大（{len(png)} bytes，上限 {_AVATAR_MAX_BYTES}）"
        )
    if not png.startswith(_PNG_MAGIC):
        raise ValueError("文件头不是 PNG（magic bytes 不匹配）")
    return png


# room.toml 体量上限：64KB。正常房间 < 2KB；上限挡误粘整个文档。
_ROOM_TOML_MAX_BYTES = 64 * 1024


def update_room_toml(room_dir: Path, content: str, *, paths: Paths) -> RoomConfig:
    """覆盖 ``room_dir/room.toml`` 全文 + 重加载校验；失败回滚到旧文本。

    与 add_guest / update_guest_permission 那条「结构化 mutator」路径并存 —— 这里是
    「raw editor」入口（前端 textarea 直编），让用户能改 [room] / [[guest]] 任何字段，
    包括 add/remove_guest 无法直接表达的 rules 修订。
    """
    encoded = content.encode("utf-8")
    if len(encoded) > _ROOM_TOML_MAX_BYTES:
        raise ValueError(
            f"room.toml 太大（{len(encoded)} bytes，上限 {_ROOM_TOML_MAX_BYTES}）"
        )
    toml_path = room_dir / "room.toml"
    # snapshot 老文本用于校验失败回滚；room_dir 已存在意味着 toml 也应在（否则 session
    # 装不起来），但防御性兜底 missing 情形。
    try:
        old_bytes = toml_path.read_bytes()
    except FileNotFoundError:
        old_bytes = None
    write_text_atomic(toml_path, content)
    try:
        return load_room_config(room_dir, paths=paths)
    except RoomConfigError:
        if old_bytes is not None:
            write_bytes_atomic(toml_path, old_bytes)
        else:
            toml_path.unlink(missing_ok=True)
        raise


def update_user_avatar(
    paths: Paths, png_bytes: bytes, *, source: Optional[Path] = None
) -> Path:
    """覆盖用户头像 PNG，返回实际写入的路径。

    sibling 约定：`USER.md` 同名 sibling `.png`。source 缺省时落 user_data_root/USER.png。
    写完清 :func:`read_avatar_data_uri` 的 lru_cache —— 否则 sidebar 还会显示旧编码。
    """
    md_target = _user_md_target(paths, source)
    target = md_target.with_suffix(".png")
    write_bytes_atomic(target, png_bytes)
    # 没法按 key evict（lru_cache API 限制），全清最便宜：6 张图（5 茶客 + 1 用户）
    # 重 base64 不到 1ms。前提是用户头像也走同一 cache —— 见 user_md.py 的委托。
    read_avatar_data_uri.cache_clear()
    return target
