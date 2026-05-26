"""persona.toml manifest 解析（P12）。

dir-form persona 包的可选清单文件。声明 ``schema_version`` / ``display_name`` /
``summary`` / ``[defaults.guest]``。**严格白名单 + 全失败统一 PersonaManifestError**。

承重契约见 ``docs/P12-persona 包 manifest.md``「承重不变量」段：

- schema_version 必填且当前唯一合法值 = 1（跨版本兼容锚点）。
- 严格白名单：顶层 / ``[defaults]`` / ``[defaults.guest]`` 三级，未知键即拒。
- ``[defaults.guest]`` 字段子集 ⊂ ``_ALLOWED_GUEST_KEYS``，不含 ``name`` / ``persona`` /
  LLM 四件套（防 API key env 名泄漏）。
- 容错策略：``discover_personas`` 是唯一允许 try/except + WARN + None 的调用方
  （可见性优先）；``add_guest`` / ``create_room`` / ``persona_import`` 等消费路径必须
  让异常冒出 —— 由 inbound handler 转 NOTICE 给用户。
- 文件入口 :func:`load_persona_manifest` 与 bytes 入口 :func:`parse_persona_manifest_bytes`
  共享私有 :func:`_parse_dict`，保证语义完全一致。bytes 入口 P12 给 ``persona_import``
  在 ``_write_files`` 落盘前做 dry-run 校验，避免坏 manifest 留半成品目录。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import VALID_ISOLATION
from .permissions import VALID_MODES, is_valid_mode

SCHEMA_VERSION = 1
"""当前唯一合法 schema_version。未来加字段**不** bump；破坏性变更才 bump。"""

# 严格白名单（mirror :mod:`chahua.config` 的 ``_ALLOWED_*`` 风格）。
# 不预留「未来口袋」字段 —— P12.2 真要加段，到时白名单加一行。
_ALLOWED_TOP_KEYS: frozenset[str] = frozenset(
    {"schema_version", "display_name", "summary", "defaults"}
)
_ALLOWED_DEFAULTS_KEYS: frozenset[str] = frozenset({"guest"})
_ALLOWED_DEFAULTS_GUEST_KEYS: frozenset[str] = frozenset({"permission", "isolation"})


class PersonaManifestError(ValueError):
    """``persona.toml`` 解析错误。消息含来源 + 字段路径。"""


@dataclass(frozen=True)
class PersonaManifest:
    schema_version: int
    display_name: Optional[str]
    summary: Optional[str]
    defaults_guest_permission: Optional[str]
    """已校验在 :data:`chahua.permissions.VALID_MODES` 内（如非 None）。"""

    defaults_guest_isolation: Optional[str]
    """已校验在 :data:`chahua.config.VALID_ISOLATION` 内（如非 None）。"""


def load_persona_manifest(persona_dir: Path) -> Optional[PersonaManifest]:
    """读 ``persona_dir/persona.toml``。文件不在 → ``None``；解析失败 / 字段非法 →
    :class:`PersonaManifestError`。

    **容错策略**：:func:`chahua.admin_persona.discover_personas` 是唯一允许
    ``try/except + WARN + None`` 的调用方（可见性优先）。``add_guest`` /
    ``create_room`` / ``persona_import`` 等消费路径**必须**让异常原样冒出 ——
    由 inbound handler 转 NOTICE 给用户（承重不变量第 7 条）。
    """
    toml_path = persona_dir / "persona.toml"
    if not toml_path.is_file():
        return None
    try:
        with toml_path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise PersonaManifestError(f"{toml_path}: TOML 解析失败：{e}") from e
    except UnicodeDecodeError as e:
        raise PersonaManifestError(f"{toml_path}: 非 UTF-8 编码") from e
    except OSError as e:
        raise PersonaManifestError(f"{toml_path}: 读取失败：{e}") from e
    return _parse_dict(data, source=str(toml_path))


def parse_persona_manifest_bytes(blob: bytes) -> PersonaManifest:
    """从已读到内存的 toml bytes 校验 + 构造 :class:`PersonaManifest`。

    :mod:`chahua.persona_import` 在 ``_write_files`` 落盘前对采集到的 ``files`` 列表里
    的 ``persona.toml`` 字节做 dry-run 校验，避免坏 manifest 留下半成品目录（下次重导
    会撞 "目录已存在"）。

    **所有失败统一包装成** :class:`PersonaManifestError`，包括 blob 不是合法 UTF-8 时
    的 ``UnicodeDecodeError``。这样 ``persona_import`` 单 catch 一类异常就覆盖所有
    manifest 解析失败路径。

    内部走 ``tomllib.loads(blob.decode("utf-8"))`` —— ``tomllib.loads`` 只接受 ``str``，
    TOML 规范固定 UTF-8 编码、显式 decode 不引入歧义。
    """
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError as e:
        raise PersonaManifestError("persona.toml: 非 UTF-8 编码") from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise PersonaManifestError(f"persona.toml: TOML 解析失败：{e}") from e
    return _parse_dict(data, source="persona.toml")


def _parse_dict(data: object, *, source: str) -> PersonaManifest:
    """共享私有：``dict`` → :class:`PersonaManifest`，严格白名单 + 字段校验。

    ``source`` 进错误消息，让用户能定位是哪份 manifest 出问题。
    """
    if not isinstance(data, dict):
        raise PersonaManifestError(
            f"{source}: 顶层必须是 table，收到 {type(data).__name__}"
        )

    unknown = sorted(set(data) - _ALLOWED_TOP_KEYS)
    if unknown:
        raise PersonaManifestError(
            f"{source}: 未知顶层键 {unknown}（允许：{sorted(_ALLOWED_TOP_KEYS)}）"
        )

    if "schema_version" not in data:
        raise PersonaManifestError(f"{source}: 缺 schema_version 字段")
    sv = data["schema_version"]
    # bool 是 int 子类，要先剔除。
    if isinstance(sv, bool) or not isinstance(sv, int) or sv != SCHEMA_VERSION:
        raise PersonaManifestError(
            f"{source}: schema_version={sv!r} 不是当前唯一合法值 {SCHEMA_VERSION}"
        )

    display_name = _opt_str(data, "display_name", source=source)
    summary = _opt_str(data, "summary", source=source)

    perm: Optional[str] = None
    iso: Optional[str] = None
    defaults = data.get("defaults")
    if defaults is not None:
        if not isinstance(defaults, dict):
            raise PersonaManifestError(
                f"{source}: [defaults] 必须是 table，收到 {type(defaults).__name__}"
            )
        unknown = sorted(set(defaults) - _ALLOWED_DEFAULTS_KEYS)
        if unknown:
            raise PersonaManifestError(
                f"{source}: [defaults] 未知键 {unknown}"
                f"（允许：{sorted(_ALLOWED_DEFAULTS_KEYS)}）"
            )
        guest = defaults.get("guest")
        if guest is not None:
            if not isinstance(guest, dict):
                raise PersonaManifestError(
                    f"{source}: [defaults.guest] 必须是 table，收到 {type(guest).__name__}"
                )
            unknown = sorted(set(guest) - _ALLOWED_DEFAULTS_GUEST_KEYS)
            if unknown:
                raise PersonaManifestError(
                    f"{source}: [defaults.guest] 未知键 {unknown}"
                    f"（允许：{sorted(_ALLOWED_DEFAULTS_GUEST_KEYS)}）"
                )
            if "permission" in guest:
                perm = guest["permission"]
                if not isinstance(perm, str):
                    raise PersonaManifestError(
                        f"{source}: [defaults.guest].permission 必须是 str，"
                        f"收到 {type(perm).__name__}"
                    )
                if not is_valid_mode(perm):
                    raise PersonaManifestError(
                        f"{source}: [defaults.guest].permission={perm!r} "
                        f"不在 {VALID_MODES} 内"
                    )
            if "isolation" in guest:
                iso = guest["isolation"]
                if not isinstance(iso, str):
                    raise PersonaManifestError(
                        f"{source}: [defaults.guest].isolation 必须是 str，"
                        f"收到 {type(iso).__name__}"
                    )
                if iso not in VALID_ISOLATION:
                    raise PersonaManifestError(
                        f"{source}: [defaults.guest].isolation={iso!r} "
                        f"不在 {sorted(VALID_ISOLATION)} 内"
                    )

    return PersonaManifest(
        schema_version=sv,
        display_name=display_name,
        summary=summary,
        defaults_guest_permission=perm,
        defaults_guest_isolation=iso,
    )


def _opt_str(data: dict, key: str, *, source: str) -> Optional[str]:
    """读可选 str 字段，缺 → None；非 str → :class:`PersonaManifestError`。"""
    if key not in data:
        return None
    v = data[key]
    if not isinstance(v, str):
        raise PersonaManifestError(
            f"{source}: {key} 必须是 str，收到 {type(v).__name__}"
        )
    return v
