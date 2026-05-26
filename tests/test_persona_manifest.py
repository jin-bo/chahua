"""``chahua.persona_manifest`` 单元测（P12 C1）。

覆盖：合法解析 / schema_version / 未知键 / permission / isolation / 坏 toml / 缺可选字段 /
bytes 入口 / 非 UTF-8 → PersonaManifestError。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chahua.persona_manifest import (
    SCHEMA_VERSION,
    PersonaManifest,
    PersonaManifestError,
    load_persona_manifest,
    parse_persona_manifest_bytes,
)


# ── load_persona_manifest（文件入口）─────────────────────────────────────


def _write(tmp_path: Path, body: str) -> Path:
    persona_dir = tmp_path / "Yvonne"
    persona_dir.mkdir()
    (persona_dir / "persona.toml").write_text(body, encoding="utf-8")
    return persona_dir


def test_load_happy_full(tmp_path: Path) -> None:
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
display_name = "伊冯"
summary = "市场调研顾问"

[defaults.guest]
permission = "workspace-write"
isolation = "global"
""",
    )
    m = load_persona_manifest(persona_dir)
    assert m == PersonaManifest(
        schema_version=1,
        display_name="伊冯",
        summary="市场调研顾问",
        defaults_guest_permission="workspace-write",
        defaults_guest_isolation="global",
    )


def test_load_happy_minimal(tmp_path: Path) -> None:
    persona_dir = _write(tmp_path, "schema_version = 1\n")
    m = load_persona_manifest(persona_dir)
    assert m is not None
    assert m.schema_version == 1
    assert m.display_name is None
    assert m.summary is None
    assert m.defaults_guest_permission is None
    assert m.defaults_guest_isolation is None


def test_load_file_missing(tmp_path: Path) -> None:
    persona_dir = tmp_path / "Yvonne"
    persona_dir.mkdir()
    assert load_persona_manifest(persona_dir) is None


def test_load_persona_dir_missing(tmp_path: Path) -> None:
    # persona_dir 本身不存在也应返 None（不抛）—— 调用方先验目录在不在不是本函数职责。
    assert load_persona_manifest(tmp_path / "nope") is None


# ── schema_version 校验 ─────────────────────────────────────────────────


def test_schema_version_missing(tmp_path: Path) -> None:
    persona_dir = _write(tmp_path, "display_name = 'X'\n")
    with pytest.raises(PersonaManifestError, match="schema_version"):
        load_persona_manifest(persona_dir)


def test_schema_version_wrong_value(tmp_path: Path) -> None:
    persona_dir = _write(tmp_path, "schema_version = 2\n")
    with pytest.raises(PersonaManifestError, match="schema_version=2"):
        load_persona_manifest(persona_dir)


def test_schema_version_wrong_type_str(tmp_path: Path) -> None:
    persona_dir = _write(tmp_path, 'schema_version = "1"\n')
    with pytest.raises(PersonaManifestError, match="schema_version"):
        load_persona_manifest(persona_dir)


def test_schema_version_bool_rejected(tmp_path: Path) -> None:
    # bool 是 int 子类，必须先剔除 —— 防止 `schema_version = true` 被当成 1 静默接收。
    persona_dir = _write(tmp_path, "schema_version = true\n")
    with pytest.raises(PersonaManifestError, match="schema_version"):
        load_persona_manifest(persona_dir)


# ── 严格白名单 ──────────────────────────────────────────────────────────


def test_unknown_top_key(tmp_path: Path) -> None:
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
version = "0.1.0"
""",
    )
    with pytest.raises(PersonaManifestError, match="version"):
        load_persona_manifest(persona_dir)


def test_unknown_defaults_key(tmp_path: Path) -> None:
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
[defaults.room]
foo = "bar"
""",
    )
    with pytest.raises(PersonaManifestError, match=r"\[defaults\]"):
        load_persona_manifest(persona_dir)


def test_unknown_defaults_guest_key(tmp_path: Path) -> None:
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
[defaults.guest]
model = "anthropic/claude-sonnet-4"
""",
    )
    with pytest.raises(PersonaManifestError, match=r"\[defaults\.guest\]"):
        load_persona_manifest(persona_dir)


def test_no_future_pocket_reserved(tmp_path: Path) -> None:
    # P12 不预留 [requires] / [[example_tasks]] / [trust] —— 写了就拒。
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
[requires]
chahua = ">=0.1.5"
""",
    )
    with pytest.raises(PersonaManifestError, match="requires"):
        load_persona_manifest(persona_dir)


# ── 字段类型 / 值校验 ───────────────────────────────────────────────────


def test_display_name_empty_string_becomes_none(tmp_path: Path) -> None:
    # 与 admin_persona._read_persona_toml_name 的 legacy 口径一致：空 → None。
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
display_name = ""
""",
    )
    m = load_persona_manifest(persona_dir)
    assert m is not None
    assert m.display_name is None


def test_display_name_whitespace_only_becomes_none(tmp_path: Path) -> None:
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
display_name = "   "
""",
    )
    m = load_persona_manifest(persona_dir)
    assert m is not None
    assert m.display_name is None


def test_display_name_stripped(tmp_path: Path) -> None:
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
display_name = "  伊冯  "
""",
    )
    m = load_persona_manifest(persona_dir)
    assert m is not None
    assert m.display_name == "伊冯"


def test_summary_empty_becomes_none(tmp_path: Path) -> None:
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
summary = ""
""",
    )
    m = load_persona_manifest(persona_dir)
    assert m is not None
    assert m.summary is None


def test_display_name_wrong_type(tmp_path: Path) -> None:
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
display_name = 123
""",
    )
    with pytest.raises(PersonaManifestError, match="display_name"):
        load_persona_manifest(persona_dir)


def test_permission_wrong_type(tmp_path: Path) -> None:
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
[defaults.guest]
permission = 1
""",
    )
    with pytest.raises(PersonaManifestError, match="permission"):
        load_persona_manifest(persona_dir)


def test_permission_invalid_mode(tmp_path: Path) -> None:
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
[defaults.guest]
permission = "wat-mode"
""",
    )
    with pytest.raises(PersonaManifestError, match="wat-mode"):
        load_persona_manifest(persona_dir)


def test_summary_wrong_type(tmp_path: Path) -> None:
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
summary = 42
""",
    )
    with pytest.raises(PersonaManifestError, match="summary"):
        load_persona_manifest(persona_dir)


def test_defaults_not_a_table(tmp_path: Path) -> None:
    # `defaults = "x"` 在 toml 语法上合法但语义错 —— 顶层 defaults 必须是 table。
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
defaults = "x"
""",
    )
    with pytest.raises(PersonaManifestError, match=r"\[defaults\]"):
        load_persona_manifest(persona_dir)


def test_defaults_guest_not_a_table(tmp_path: Path) -> None:
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
[defaults]
guest = "x"
""",
    )
    with pytest.raises(PersonaManifestError, match=r"\[defaults\.guest\]"):
        load_persona_manifest(persona_dir)


def test_isolation_wrong_type(tmp_path: Path) -> None:
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
[defaults.guest]
isolation = 1
""",
    )
    with pytest.raises(PersonaManifestError, match="isolation"):
        load_persona_manifest(persona_dir)


def test_isolation_invalid(tmp_path: Path) -> None:
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
[defaults.guest]
isolation = "per_guest"
""",
    )
    with pytest.raises(PersonaManifestError, match="per_guest"):
        load_persona_manifest(persona_dir)


def test_defaults_only_present_no_guest(tmp_path: Path) -> None:
    # [defaults] 段在但里面没 [guest] —— 不抛，字段全 None。
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
[defaults]
""",
    )
    m = load_persona_manifest(persona_dir)
    assert m is not None
    assert m.defaults_guest_permission is None
    assert m.defaults_guest_isolation is None


def test_defaults_guest_empty(tmp_path: Path) -> None:
    # [defaults.guest] 段在但 permission/isolation 都没写。
    persona_dir = _write(
        tmp_path,
        """\
schema_version = 1
[defaults.guest]
""",
    )
    m = load_persona_manifest(persona_dir)
    assert m is not None
    assert m.defaults_guest_permission is None
    assert m.defaults_guest_isolation is None


# ── 坏 toml 语法 ───────────────────────────────────────────────────────


def test_load_rejects_miscased_filename(tmp_path: Path) -> None:
    """跨平台 case-strict 守卫：dir 里只有 ``Persona.toml``（错名）→ PersonaManifestError。

    与 :func:`chahua.persona_import._validate_manifest_pre_write` 的 C4 守卫同口径。
    macOS APFS / Windows NTFS 上 ``Path('persona.toml').is_file()`` 会匹配错名，没守卫
    会让接受/拒绝行为跨平台漂（reviewer Codex P3）。
    """
    persona_dir = tmp_path / "Yvonne"
    persona_dir.mkdir()
    # 故意写错大小写 —— 注意 macOS APFS case-insensitive，无法同时存在两个名字。
    (persona_dir / "Persona.toml").write_text(
        "schema_version = 1\n", encoding="utf-8"
    )
    with pytest.raises(PersonaManifestError, match="大小写不对"):
        load_persona_manifest(persona_dir)


def test_load_bad_toml_syntax(tmp_path: Path) -> None:
    persona_dir = _write(tmp_path, "schema_version = 1\n[unclosed\n")
    with pytest.raises(PersonaManifestError, match="TOML"):
        load_persona_manifest(persona_dir)


# ── parse_persona_manifest_bytes（bytes 入口）─────────────────────────


def test_bytes_happy() -> None:
    blob = b"""\
schema_version = 1
display_name = "Y"
[defaults.guest]
permission = "workspace-write"
"""
    m = parse_persona_manifest_bytes(blob)
    assert m.display_name == "Y"
    assert m.defaults_guest_permission == "workspace-write"


def test_bytes_non_utf8_raises_manifest_error() -> None:
    # 锁住「所有失败统一包装」契约：UnicodeDecodeError 必须包成 PersonaManifestError，
    # persona_import 才能单 catch 一类异常。
    with pytest.raises(PersonaManifestError, match="UTF-8"):
        parse_persona_manifest_bytes(b"\xff\xfe nope")


def test_bytes_bad_toml_raises_manifest_error() -> None:
    with pytest.raises(PersonaManifestError, match="TOML"):
        parse_persona_manifest_bytes(b"[unclosed\n")


def test_bytes_unknown_top_key_raises() -> None:
    blob = b"""\
schema_version = 1
version = "0.1.0"
"""
    with pytest.raises(PersonaManifestError, match="version"):
        parse_persona_manifest_bytes(blob)


def test_bytes_and_file_same_semantics(tmp_path: Path) -> None:
    # 同一份 manifest 两条入口结果应一致。
    body = """\
schema_version = 1
display_name = "X"
summary = "hello"

[defaults.guest]
permission = "workspace-write"
isolation = "room"
"""
    persona_dir = _write(tmp_path, body)
    m_file = load_persona_manifest(persona_dir)
    m_bytes = parse_persona_manifest_bytes(body.encode("utf-8"))
    assert m_file == m_bytes


def test_schema_version_constant_is_1() -> None:
    # 钉住 P12 阶段「当前唯一合法 schema_version = 1」契约。
    assert SCHEMA_VERSION == 1
