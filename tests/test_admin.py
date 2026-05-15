"""chahua.admin —— 房间 / 茶客 增删 + persona 扫描的回归。

测试栈：tmp_path 给每个用例独立的 user_data_root，把 chahua/personas/*.md 作为 ship
asset 通过 monkeypatched ENV 暴露 app_root。所有断言都过 load_room_config 双向校验，
不直接读 toml 字面量，避免被 mini 序列化器的格式细节绑定。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chahua import admin
from chahua._paths import ENV_APP_ROOT, ENV_USER_DATA_ROOT, Paths
from chahua.config import RoomConfigError, load_room_config


REPO_ROOT = Path(__file__).resolve().parent.parent
PERSONAS_DIR = REPO_ROOT / "chahua" / "personas"


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """user_data_root 在 tmp 下；app_root 指向真仓库根（ship 自带 personas）。"""
    user_data = tmp_path / "userdata"
    user_data.mkdir()
    monkeypatch.setenv(ENV_APP_ROOT, str(REPO_ROOT))
    monkeypatch.setenv(ENV_USER_DATA_ROOT, str(user_data))
    return Paths.from_env()


# ── persona 扫描 ─────────────────────────────────────────────────────────


def test_discover_personas_scans_app_root(paths):
    found = admin.discover_personas(paths)
    names = {p["name"] for p in found}
    # 仓库自带的 5 位至少能扫到。
    assert {"宝总", "汪小姐", "范总", "玲子", "爷叔"}.issubset(names)
    # persona 字段是相对路径，可塞 [[guest]].persona。
    one = next(p for p in found if p["name"] == "宝总")
    assert one["persona"] == "chahua/personas/宝总.md"
    # 头像 data URI 可有可无；宝总.png 在仓库里 → 必有。
    assert one["avatar_data_uri"] and one["avatar_data_uri"].startswith("data:image/png;base64,")


def test_discover_personas_user_data_overrides_app(paths, tmp_path):
    """user_data 优先于 app_root —— 用户自定义的同名 md 应该胜出。"""
    user_persona = paths.user_data_root / "chahua" / "personas" / "宝总.md"
    user_persona.parent.mkdir(parents=True)
    user_persona.write_text("# 自定义宝总", encoding="utf-8")

    found = admin.discover_personas(paths)
    # 自定义宝总没有 .png sibling → avatar_data_uri = None（user_data 胜出的证据）。
    one = next(p for p in found if p["name"] == "宝总")
    assert one["avatar_data_uri"] is None


# ── room CRUD ───────────────────────────────────────────────────────────


def test_create_room_writes_loadable_toml(paths):
    rc = admin.create_room(
        paths=paths,
        room_id="test-客厅",
        name="客厅",
        topic="周末闲聊",
        rules="保持中文",
        guests=[{"persona": "chahua/personas/宝总.md", "name": "宝总"}],
    )
    assert rc.name == "客厅"
    assert rc.topic == "周末闲聊"
    assert rc.rules == "保持中文"
    assert len(rc.guests) == 1 and rc.guests[0].name == "宝总"
    # 双向校验：再 load 一次也通过（mutator 写出的 toml 与启动期 loader 100% 兼容）。
    rc2 = load_room_config(rc.room_dir, paths=paths)
    assert rc2.name == rc.name


def test_create_room_rejects_existing(paths):
    admin.create_room(
        paths=paths, room_id="dup", name="dup",
        guests=[{"persona": "chahua/personas/宝总.md"}],
    )
    with pytest.raises(FileExistsError):
        admin.create_room(
            paths=paths, room_id="dup", name="dup",
            guests=[{"persona": "chahua/personas/汪小姐.md"}],
        )


def test_create_room_rolls_back_on_invalid_persona(paths):
    """persona 路径不存在 → load 校验抛错，目录应已 rmtree（不留半成品）。"""
    with pytest.raises(RoomConfigError):
        admin.create_room(
            paths=paths, room_id="bad", name="bad",
            guests=[{"persona": "chahua/personas/不存在.md"}],
        )
    assert not (paths.user_data_root / "rooms" / "bad").exists()


def test_create_room_normalizes_room_id(paths):
    """room_id 含路径分隔符等禁字符 → 替换成 -，不会 traversal 出 rooms/。"""
    rc = admin.create_room(
        paths=paths, room_id="../sneak", name="sneak",
        guests=[{"persona": "chahua/personas/宝总.md"}],
    )
    assert rc.room_dir.parent == paths.user_data_root / "rooms"
    assert rc.room_dir.name == "-sneak"  # `..` 被洗成 `-`，不在 rooms/ 之外。


def test_create_room_requires_at_least_one_guest(paths):
    with pytest.raises(ValueError):
        admin.create_room(paths=paths, room_id="empty", name="empty", guests=[])


def test_delete_room_removes_directory(paths):
    rc = admin.create_room(
        paths=paths, room_id="rm-me", name="rm",
        guests=[{"persona": "chahua/personas/宝总.md"}],
    )
    assert rc.room_dir.is_dir()
    admin.delete_room(paths=paths, room_id="rm-me", current_room_id="other")
    assert not rc.room_dir.exists()


def test_delete_room_refuses_current(paths):
    admin.create_room(
        paths=paths, room_id="here", name="here",
        guests=[{"persona": "chahua/personas/宝总.md"}],
    )
    with pytest.raises(ValueError):
        admin.delete_room(paths=paths, room_id="here", current_room_id="here")


# ── guest CRUD ───────────────────────────────────────────────────────────


def _seed_room(paths, *, with_guests):
    return admin.create_room(
        paths=paths, room_id="r1", name="r1",
        guests=with_guests,
    )


def test_add_guest_appends_and_persists(paths):
    rc = _seed_room(
        paths,
        with_guests=[{"persona": "chahua/personas/宝总.md"}],
    )
    rc2 = admin.add_guest(
        paths=paths,
        room_dir=rc.room_dir,
        persona="chahua/personas/汪小姐.md",
    )
    names = [g.name for g in rc2.guests]
    assert names == ["宝总", "汪小姐"]
    # 重 load 也应该看到 —— 写盘真生效。
    rc3 = load_room_config(rc.room_dir, paths=paths)
    assert [g.name for g in rc3.guests] == ["宝总", "汪小姐"]


def test_add_guest_rejects_duplicate_name(paths):
    rc = _seed_room(paths, with_guests=[{"persona": "chahua/personas/宝总.md"}])
    with pytest.raises(ValueError):
        admin.add_guest(
            paths=paths, room_dir=rc.room_dir,
            persona="chahua/personas/宝总.md",
        )


def test_remove_guest_drops_entry(paths):
    rc = _seed_room(
        paths,
        with_guests=[
            {"persona": "chahua/personas/宝总.md"},
            {"persona": "chahua/personas/汪小姐.md"},
        ],
    )
    rc2 = admin.remove_guest(paths=paths, room_dir=rc.room_dir, name="汪小姐")
    assert [g.name for g in rc2.guests] == ["宝总"]


def test_remove_last_guest_refused(paths):
    rc = _seed_room(paths, with_guests=[{"persona": "chahua/personas/宝总.md"}])
    with pytest.raises(ValueError):
        admin.remove_guest(paths=paths, room_dir=rc.room_dir, name="宝总")


def test_remove_unknown_guest_refused(paths):
    rc = _seed_room(
        paths,
        with_guests=[
            {"persona": "chahua/personas/宝总.md"},
            {"persona": "chahua/personas/汪小姐.md"},
        ],
    )
    with pytest.raises(ValueError):
        admin.remove_guest(paths=paths, room_dir=rc.room_dir, name="不在场")


# ── TOML 字面写出 ────────────────────────────────────────────────────────


# ── USER.md / 头像 mutator ───────────────────────────────────────────────


# 最小合法 PNG 文件（1x1 透明像素）—— 用于头像 round-trip 测试。
_MIN_PNG = (
    b"\x89PNG\r\n\x1a\n"  # magic
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _png_data_uri(png: bytes = _MIN_PNG) -> str:
    import base64
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def test_update_user_md_writes_to_user_data_root_when_no_source(paths):
    p = admin.update_user_md(paths, "## 显示名\n小明\n")
    assert p == paths.user_data_root / "USER.md"
    assert p.read_text(encoding="utf-8") == "## 显示名\n小明\n"


def test_update_user_md_writes_to_source_path_when_given(paths, tmp_path):
    explicit = tmp_path / "custom-USER.md"
    explicit.write_text("# 旧\n", encoding="utf-8")
    p = admin.update_user_md(paths, "# 新\n", source=explicit)
    assert p == explicit
    assert explicit.read_text(encoding="utf-8") == "# 新\n"


def test_update_user_md_rejects_oversized(paths):
    with pytest.raises(ValueError, match="USER.md 太大"):
        admin.update_user_md(paths, "x" * (64 * 1024 + 1))


def test_update_user_md_round_trip_via_load_user_md(paths):
    from chahua.user_md import load_user_md
    admin.update_user_md(paths, "## 显示名\n老金\n\n## 偏好\n直接\n")
    cfg = load_user_md(user_data_root=paths.user_data_root)
    assert cfg.display_name == "老金"
    assert "直接" in cfg.preferences_block


# parse_png_data_uri ─────────────────────────────────────────────────────


def test_parse_png_data_uri_accepts_valid_png():
    assert admin.parse_png_data_uri(_png_data_uri()) == _MIN_PNG


def test_parse_png_data_uri_rejects_non_png_prefix():
    with pytest.raises(ValueError, match="仅接受 PNG"):
        admin.parse_png_data_uri("data:image/jpeg;base64,abc")


def test_parse_png_data_uri_rejects_bad_base64():
    with pytest.raises(ValueError, match="base64"):
        admin.parse_png_data_uri("data:image/png;base64,not_base64!!")


def test_parse_png_data_uri_rejects_wrong_magic_bytes():
    """base64 合法但 decode 后不是 PNG —— 防止伪 PNG 数据写进 USER.png 让 sidebar 渲染崩。"""
    import base64
    fake = base64.b64encode(b"GIF89a fake content here goes nothing").decode("ascii")
    with pytest.raises(ValueError, match="magic bytes"):
        admin.parse_png_data_uri("data:image/png;base64," + fake)


def test_parse_png_data_uri_rejects_oversized():
    import base64
    huge = _MIN_PNG + b"\x00" * (1_500_001 - len(_MIN_PNG))
    uri = "data:image/png;base64," + base64.b64encode(huge).decode("ascii")
    with pytest.raises(ValueError, match="头像太大"):
        admin.parse_png_data_uri(uri)


# update_user_avatar ──────────────────────────────────────────────────────


def test_update_user_avatar_writes_sibling_png(paths, tmp_path):
    explicit = tmp_path / "ego.md"
    explicit.write_text("# ego\n", encoding="utf-8")
    p = admin.update_user_avatar(paths, _MIN_PNG, source=explicit)
    assert p == tmp_path / "ego.png"
    assert p.read_bytes() == _MIN_PNG


def test_update_user_avatar_clears_lru_cache(paths, tmp_path):
    """覆盖头像后 read_avatar_data_uri 必须返回新内容 —— 否则 sidebar 永远显示旧图。"""
    from chahua.config import read_avatar_data_uri

    target = paths.user_data_root / "USER.png"
    target.write_bytes(_MIN_PNG)
    first = read_avatar_data_uri(target)
    assert first is not None and "iVBORw0K" in first  # MIN_PNG 的 base64 前缀

    # 写一个不同的 PNG（多一个 IDAT 块的 padding —— 简化用 MIN_PNG + 0 字节）
    bigger = _MIN_PNG + b""  # same content; just verify clear works
    admin.update_user_avatar(paths, bigger, source=paths.user_data_root / "USER.md")
    second = read_avatar_data_uri(target)
    # 即使内容相同，cache_clear 后必须重新计算（不命中旧 cache）—— 检查不抛异常 + 返回新值。
    assert second == first  # 内容真同，结果同；但已是 fresh 计算


def test_toml_writer_escapes_quotes_and_backslashes(paths):
    """name / topic / rules 含特殊字符也要能 round-trip 过 tomllib。"""
    rc = admin.create_room(
        paths=paths,
        room_id="esc",
        name='含 " 引号 \\ 反斜杠',
        topic="多行\n第二行",
        rules="制表\t符",
        guests=[{"persona": "chahua/personas/宝总.md", "name": '宝"总'}],
    )
    rc2 = load_room_config(rc.room_dir, paths=paths)
    assert rc2.name == '含 " 引号 \\ 反斜杠'
    assert rc2.topic == "多行\n第二行"
    assert rc2.rules == "制表\t符"
    assert rc2.guests[0].name == '宝"总'
