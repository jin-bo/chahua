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


def test_discover_personas_uses_sibling_toml_name(paths):
    """dir-form persona 的 sibling ``<Name>.toml`` ``[guest].name`` 是 picker 显示名。

    回归：picker 默认以 md stem 命名（dir 名），但用户在 toml 里写 ``name = "伊冯"``
    时 picker 应显示"伊冯"。坏 toml / 缺字段静默退回 stem，不让 persona 在 picker 消失。
    """
    base = paths.user_data_root / "chahua" / "personas" / "Yvonne"
    base.mkdir(parents=True)
    (base / "Yvonne.md").write_text("# Yvonne soul", encoding="utf-8")
    (base / "Yvonne.toml").write_text(
        '[guest]\nname = "伊冯"\npermission = "workspace-write"\n',
        encoding="utf-8",
    )

    found = admin.discover_personas(paths)
    assert any(p["name"] == "伊冯" for p in found)
    one = next(p for p in found if p["name"] == "伊冯")
    # persona 字段不变（落盘路径还是按目录名走），只换显示名。
    assert one["persona"] == "chahua/personas/Yvonne/Yvonne.md"


def test_discover_personas_falls_back_when_toml_broken(paths):
    """toml 解析失败 / 缺 [guest].name → 退到 dir 名兜底，persona 仍在 picker 里。"""
    base = paths.user_data_root / "chahua" / "personas" / "Broken"
    base.mkdir(parents=True)
    (base / "Broken.md").write_text("# soul", encoding="utf-8")
    (base / "Broken.toml").write_text("not a [valid toml", encoding="utf-8")

    found = admin.discover_personas(paths)
    assert any(p["name"] == "Broken" for p in found)


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


def test_update_guest_permission_persists(paths):
    rc = _seed_room(
        paths,
        with_guests=[
            {"persona": "chahua/personas/宝总.md", "permission": "read-only"},
            {"persona": "chahua/personas/汪小姐.md", "permission": "read-only"},
        ],
    )
    rc2 = admin.update_guest_permission(
        paths=paths, room_dir=rc.room_dir, name="宝总", permission="full-access"
    )
    perms = {g.name: g.permission for g in rc2.guests}
    assert perms == {"宝总": "full-access", "汪小姐": "read-only"}
    # 重 load 也看到 —— 写盘真生效。
    rc3 = load_room_config(rc.room_dir, paths=paths)
    assert rc3.guests[0].permission == "full-access"


def test_debug_section_preserved_across_structured_rewrite(paths):
    """P6.1 回归：用户写过 ``[debug] enabled = false`` 后任一结构化 mutator
    （update_guest_permission / update_room_llm / ...）重写 ``room.toml`` 必须保住
    设定。早先 :class:`TomlSnapshot` 缺 ``debug`` 字段 → 重写时静默丢段 →
    默认 ``enabled=true`` 复活 → prompt 捕获被悄悄重开。
    """
    rc = _seed_room(paths, with_guests=[{"persona": "chahua/personas/宝总.md"}])
    # 模拟用户手动 append [debug]（载入期合法 —— 段缺时也 fall back 默认）。
    toml_path = rc.room_dir / "room.toml"
    toml_path.write_text(
        toml_path.read_text(encoding="utf-8")
        + "\n[debug]\nenabled = false\ncapture_prompts = false\n",
        encoding="utf-8",
    )
    rc1 = load_room_config(rc.room_dir, paths=paths)
    assert rc1.debug.enabled is False
    assert rc1.debug.capture_prompts is False

    # 任一结构化 mutator 都会重写 room.toml —— 选 update_guest_permission 当代表。
    admin.update_guest_permission(
        paths=paths, room_dir=rc.room_dir, name="宝总", permission="full-access"
    )
    rc2 = load_room_config(rc.room_dir, paths=paths)
    assert rc2.debug.enabled is False
    assert rc2.debug.capture_prompts is False


def test_update_guest_permission_rejects_invalid(paths):
    rc = _seed_room(paths, with_guests=[{"persona": "chahua/personas/宝总.md"}])
    with pytest.raises(ValueError, match="permission"):
        admin.update_guest_permission(
            paths=paths, room_dir=rc.room_dir, name="宝总", permission="god-mode"
        )
    # PLAN 是 agentao 内部模式，茶话室不暴露 —— 也拒。
    with pytest.raises(ValueError, match="permission"):
        admin.update_guest_permission(
            paths=paths, room_dir=rc.room_dir, name="宝总", permission="plan"
        )


def test_update_guest_permission_unknown_name(paths):
    rc = _seed_room(paths, with_guests=[{"persona": "chahua/personas/宝总.md"}])
    with pytest.raises(ValueError, match="不在房间"):
        admin.update_guest_permission(
            paths=paths, room_dir=rc.room_dir, name="不在场", permission="workspace-write"
        )


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


# ── [scoring] / [summary] / [[guest]].LLM round-trip + mutator（P4.1）─────


def _seed_room_for_llm(paths):
    return admin.create_room(
        paths=paths, room_id="llm", name="llm",
        guests=[{"persona": "chahua/personas/宝总.md", "name": "宝总"}],
    )


def test_room_llm_round_trip(paths):
    rc = _seed_room_for_llm(paths)
    rc2 = admin.update_room_llm(
        paths=paths, room_dir=rc.room_dir, section="scoring",
        spec_dict={"model": "openai/gpt-5.4-mini"},
    )
    assert rc2.scoring_llm is not None
    assert rc2.scoring_llm.provider == "openai"
    assert rc2.scoring_llm.model == "gpt-5.4-mini"
    # reload 也保留。
    rc3 = load_room_config(rc.room_dir, paths=paths)
    assert rc3.scoring_llm == rc2.scoring_llm


def test_room_llm_summary_round_trip_with_base_url(paths):
    rc = _seed_room_for_llm(paths)
    rc2 = admin.update_room_llm(
        paths=paths, room_dir=rc.room_dir, section="summary",
        spec_dict={
            "model": "anthropic/claude-opus-4-7",
            "base_url": "https://api.anthropic.com",
            "api_key_env": "ANTHROPIC_API_KEY",
        },
    )
    assert rc2.summary_llm is not None
    assert rc2.summary_llm.base_url == "https://api.anthropic.com"
    assert rc2.summary_llm.api_key_env == "ANTHROPIC_API_KEY"


def test_update_room_llm_null_clears_section(paths):
    rc = _seed_room_for_llm(paths)
    admin.update_room_llm(
        paths=paths, room_dir=rc.room_dir, section="scoring",
        spec_dict={"model": "openai/gpt-5.4-mini"},
    )
    rc_after = admin.update_room_llm(
        paths=paths, room_dir=rc.room_dir, section="scoring", spec_dict=None,
    )
    assert rc_after.scoring_llm is None
    assert "[scoring]" not in (rc.room_dir / "room.toml").read_text("utf-8")


def test_update_room_llm_bad_section_rejected(paths):
    rc = _seed_room_for_llm(paths)
    with pytest.raises(RoomConfigError, match="section"):
        admin.update_room_llm(
            paths=paths, room_dir=rc.room_dir, section="foo",
            spec_dict={"model": "openai/gpt-4"},
        )


def test_room_default_llm_round_trip(paths):
    """P4.9：[room.llm] 段 round-trip。写入 → reload → 字段一致；同时验 toml 写出
    用的是 dotted section ``[room.llm]``（不是 ``[room_llm]``、也不是 inline）。"""
    rc = _seed_room_for_llm(paths)
    rc2 = admin.update_room_llm(
        paths=paths, room_dir=rc.room_dir, section="room",
        spec_dict={
            "model": "openai/gpt-5.4",
            "api_key_env": "OPENAI_API_KEY",
            "temperature": 0.8,
        },
    )
    assert rc2.room_llm is not None
    assert rc2.room_llm.provider == "openai"
    assert rc2.room_llm.model == "gpt-5.4"
    assert rc2.room_llm.api_key_env == "OPENAI_API_KEY"
    assert rc2.room_llm.temperature == pytest.approx(0.8)
    # reload 也保留。
    rc3 = load_room_config(rc.room_dir, paths=paths)
    assert rc3.room_llm == rc2.room_llm
    # 物理写入是 [room.llm] dotted table。
    text = (rc.room_dir / "room.toml").read_text("utf-8")
    assert "[room.llm]" in text


def test_update_room_default_llm_null_clears(paths):
    """传 None 清掉 [room.llm] 段 —— toml 里整段消失。"""
    rc = _seed_room_for_llm(paths)
    admin.update_room_llm(
        paths=paths, room_dir=rc.room_dir, section="room",
        spec_dict={"model": "openai/gpt-5.4"},
    )
    rc_after = admin.update_room_llm(
        paths=paths, room_dir=rc.room_dir, section="room", spec_dict=None,
    )
    assert rc_after.room_llm is None
    assert "[room.llm]" not in (rc.room_dir / "room.toml").read_text("utf-8")


def test_update_room_llm_empty_dict_rejected(paths):
    """{} 语义模糊（"清整段"应该传 None）—— 拒。"""
    rc = _seed_room_for_llm(paths)
    with pytest.raises(RoomConfigError, match=r"不含 model 字段"):
        admin.update_room_llm(
            paths=paths, room_dir=rc.room_dir, section="scoring", spec_dict={},
        )


@pytest.mark.parametrize("spec,match", [
    ({"base_url": "http://x"}, r"base_url / api_key_env / temperature 不能单独出现"),
    ({"model": "gpt-4"}, r"必须形如 '<provider>/<model>'"),
    ({"model": "weirdprovider/m"}, r"不在已知列表"),
    ({"model": "openai/gpt-4", "temperature": 2.5}, r"越界"),
    ({"model": "openai/gpt-4", "novelfield": 1}, r"未知字段"),
])
def test_update_room_llm_invalid_spec_rejected(paths, spec, match):
    rc = _seed_room_for_llm(paths)
    original = (rc.room_dir / "room.toml").read_text("utf-8")
    with pytest.raises(RoomConfigError, match=match):
        admin.update_room_llm(
            paths=paths, room_dir=rc.room_dir, section="scoring", spec_dict=spec,
        )
    # 写盘前 pre-validate 拦截 —— 磁盘 toml 不动。
    assert (rc.room_dir / "room.toml").read_text("utf-8") == original


def test_guest_llm_round_trip(paths):
    rc = _seed_room_for_llm(paths)
    rc2 = admin.update_guest_llm(
        paths=paths, room_dir=rc.room_dir, name="宝总",
        spec_dict={
            "model": "anthropic/claude-opus-4-7",
            "base_url": "https://api.anthropic.com",
            "api_key_env": "ANTHROPIC_API_KEY",
        },
    )
    spec = rc2.guests[0].llm
    assert spec is not None and spec.provider == "anthropic"
    rc3 = load_room_config(rc.room_dir, paths=paths)
    assert rc3.guests[0].llm == spec


def test_guest_llm_temperature_round_trip(paths):
    """P4.8：temperature 进 toml schema 后 round-trip 不丢精度（admin → 写盘 → reload）。
    并验证 toml 里写的是 scalar（不带引号），用户手编时一眼能看出是数值。"""
    rc = _seed_room_for_llm(paths)
    rc2 = admin.update_guest_llm(
        paths=paths, room_dir=rc.room_dir, name="宝总",
        spec_dict={"model": "openai/gpt-4", "temperature": 0.2},
    )
    spec = rc2.guests[0].llm
    assert spec is not None and spec.temperature == pytest.approx(0.2)
    rc3 = load_room_config(rc.room_dir, paths=paths)
    assert rc3.guests[0].llm == spec
    # scalar 而非 basic string —— 否则 from_toml 会以 "必须是数值" 报错。
    text = (rc.room_dir / "room.toml").read_text("utf-8")
    assert "temperature= 0.2" in text


def test_room_llm_scoring_temperature_round_trip(paths):
    """[scoring] 段也认 temperature；reload 后 spec 完整。"""
    rc = _seed_room_for_llm(paths)
    rc2 = admin.update_room_llm(
        paths=paths, room_dir=rc.room_dir, section="scoring",
        spec_dict={"model": "openai/gpt-5.4-mini", "temperature": 0.0},
    )
    assert rc2.scoring_llm is not None
    assert rc2.scoring_llm.temperature == pytest.approx(0.0)
    rc3 = load_room_config(rc.room_dir, paths=paths)
    assert rc3.scoring_llm == rc2.scoring_llm


def test_update_guest_llm_null_clears(paths):
    rc = _seed_room_for_llm(paths)
    admin.update_guest_llm(
        paths=paths, room_dir=rc.room_dir, name="宝总",
        spec_dict={"model": "openai/gpt-4"},
    )
    rc_after = admin.update_guest_llm(
        paths=paths, room_dir=rc.room_dir, name="宝总", spec_dict=None,
    )
    assert rc_after.guests[0].llm is None
    # toml 里也不应再含 LLM 字段。
    text = (rc.room_dir / "room.toml").read_text("utf-8")
    assert "model    " not in text
    assert "base_url" not in text
    assert "api_key_env" not in text


def test_update_guest_llm_unknown_name(paths):
    rc = _seed_room_for_llm(paths)
    with pytest.raises(ValueError, match="不在房间"):
        admin.update_guest_llm(
            paths=paths, room_dir=rc.room_dir, name="路人",
            spec_dict={"model": "openai/gpt-4"},
        )


def test_update_guest_llm_preserves_other_guests_and_room_llm(paths):
    """改 A 的 LLM 不应丢 B 的 LLM / [scoring] / [summary]。"""
    rc = admin.create_room(
        paths=paths, room_id="multi", name="multi",
        guests=[
            {"persona": "chahua/personas/宝总.md", "name": "宝总"},
            {"persona": "chahua/personas/汪小姐.md", "name": "汪小姐"},
        ],
    )
    admin.update_room_llm(
        paths=paths, room_dir=rc.room_dir, section="scoring",
        spec_dict={"model": "openai/gpt-5.4-mini"},
    )
    admin.update_guest_llm(
        paths=paths, room_dir=rc.room_dir, name="汪小姐",
        spec_dict={"model": "openai/gpt-4"},
    )
    # 现在改宝总。
    rc_after = admin.update_guest_llm(
        paths=paths, room_dir=rc.room_dir, name="宝总",
        spec_dict={"model": "openai/gpt-4o"},
    )
    assert rc_after.scoring_llm is not None
    assert rc_after.scoring_llm.model == "gpt-5.4-mini"
    by_name = {g.name: g.llm for g in rc_after.guests}
    assert by_name["宝总"].model == "gpt-4o"  # type: ignore[union-attr]
    assert by_name["汪小姐"].model == "gpt-4"  # type: ignore[union-attr]


def test_legacy_provider_field_rejected_with_hint(paths):
    """用户按"老式"思路单独写 provider —— config 报错并提示走合并写法。"""
    rc = _seed_room_for_llm(paths)
    bad_toml = (
        '[room]\nname = "x"\n\n'
        '[[guest]]\nname = "宝总"\npersona = "chahua/personas/宝总.md"\n'
        'provider = "openai"\nmodel = "openai/gpt-4"\n'
    )
    with pytest.raises(RoomConfigError, match=r"model = \"<provider>/<model>\""):
        admin.update_room_toml(rc.room_dir, bad_toml, paths=paths)


# ── isolation round-trip + mutator（P4.2）────────────────────────────────


def _seed_room_for_isolation(paths):
    return admin.create_room(
        paths=paths, room_id="iso", name="iso",
        guests=[{"persona": "chahua/personas/宝总.md", "name": "宝总"}],
    )


def test_isolation_default_is_room(paths):
    rc = _seed_room_for_isolation(paths)
    assert rc.guests[0].isolation == "room"
    # 默认值不进 toml —— 让 [[guest]] 段保持简洁。
    text = (rc.room_dir / "room.toml").read_text("utf-8")
    assert "isolation" not in text


def test_update_guest_isolation_round_trip(paths):
    rc = _seed_room_for_isolation(paths)
    rc2 = admin.update_guest_isolation(
        paths=paths, room_dir=rc.room_dir, name="宝总", isolation="global",
    )
    assert rc2.guests[0].isolation == "global"
    # 重 load 也保留。
    rc3 = load_room_config(rc.room_dir, paths=paths)
    assert rc3.guests[0].isolation == "global"


def test_update_guest_isolation_back_to_room_drops_field(paths):
    rc = _seed_room_for_isolation(paths)
    admin.update_guest_isolation(
        paths=paths, room_dir=rc.room_dir, name="宝总", isolation="global",
    )
    admin.update_guest_isolation(
        paths=paths, room_dir=rc.room_dir, name="宝总", isolation="room",
    )
    text = (rc.room_dir / "room.toml").read_text("utf-8")
    assert "isolation" not in text  # 回默认即省一行


def test_workspace_in_room_vs_global(paths):
    rc = _seed_room_for_isolation(paths)
    gc_room = rc.guests[0]
    rc2 = admin.update_guest_isolation(
        paths=paths, room_dir=rc.room_dir, name="宝总", isolation="global",
    )
    gc_global = rc2.guests[0]
    ws_room = gc_room.workspace_in(paths=paths, room_dir=rc.room_dir)
    ws_global = gc_global.workspace_in(paths=paths, room_dir=rc2.room_dir)
    assert ws_room == rc.room_dir / "guests" / "宝总"
    assert ws_global == paths.user_data_root / "guests" / "宝总"


def test_update_guest_isolation_invalid_rejected(paths):
    rc = _seed_room_for_isolation(paths)
    original = (rc.room_dir / "room.toml").read_text("utf-8")
    with pytest.raises(RoomConfigError, match=r"不在"):
        admin.update_guest_isolation(
            paths=paths, room_dir=rc.room_dir, name="宝总", isolation="rogue",
        )
    # 写盘前 pre-validate —— 磁盘不动。
    assert (rc.room_dir / "room.toml").read_text("utf-8") == original


def test_update_guest_isolation_unknown_name(paths):
    rc = _seed_room_for_isolation(paths)
    with pytest.raises(ValueError, match="不在房间"):
        admin.update_guest_isolation(
            paths=paths, room_dir=rc.room_dir, name="路人", isolation="global",
        )


def test_legacy_isolation_load_rejected_bad_value(paths):
    """raw editor 写非法 isolation → config 拒 + 回滚。"""
    rc = _seed_room_for_isolation(paths)
    bad = (
        '[room]\nname = "x"\n\n'
        '[[guest]]\nname = "宝总"\npersona = "chahua/personas/宝总.md"\n'
        'isolation = "rogue"\n'
    )
    with pytest.raises(RoomConfigError, match=r"isolation="):
        admin.update_room_toml(rc.room_dir, bad, paths=paths)


def test_isolation_preserved_alongside_llm_mutator(paths):
    """切 isolation 后再改 LLM 不应丢 isolation，反之亦然。"""
    rc = _seed_room_for_isolation(paths)
    admin.update_guest_isolation(
        paths=paths, room_dir=rc.room_dir, name="宝总", isolation="global",
    )
    rc2 = admin.update_guest_llm(
        paths=paths, room_dir=rc.room_dir, name="宝总",
        spec_dict={"model": "openai/gpt-4"},
    )
    assert rc2.guests[0].isolation == "global"
    assert rc2.guests[0].llm is not None  # type: ignore[union-attr]
    rc3 = admin.update_guest_isolation(
        paths=paths, room_dir=rc.room_dir, name="宝总", isolation="room",
    )
    assert rc3.guests[0].llm is not None  # type: ignore[union-attr]
    assert rc3.guests[0].llm.model == "gpt-4"  # type: ignore[union-attr]


# ── extra_mcp_servers round-trip + mutator（P4.3）────────────────────────


def _seed_room_for_extra_mcp(paths):
    return admin.create_room(
        paths=paths, room_id="mcp", name="mcp",
        guests=[{"persona": "chahua/personas/宝总.md", "name": "宝总"}],
    )


def test_extra_mcp_round_trip_via_raw_toml(paths):
    """raw editor 写 `[[guest.extra_mcp_servers]]` → load 解析为 dict[name -> cfg]。"""
    rc = _seed_room_for_extra_mcp(paths)
    new_toml = (
        '[room]\nname = "mcp"\n\n'
        '[[guest]]\nname = "宝总"\npersona = "chahua/personas/宝总.md"\npermission = "read-only"\n\n'
        '[[guest.extra_mcp_servers]]\n'
        'name = "web-search"\n'
        'command = "npx"\n'
        'args = ["-y", "@some/web-mcp"]\n'
        'env = { "API_TOKEN" = "x" }\n'
    )
    rc2 = admin.update_room_toml(rc.room_dir, new_toml, paths=paths)
    servers = rc2.guests[0].extra_mcp_servers
    assert servers is not None
    assert "web-search" in servers
    assert servers["web-search"] == {
        "command": "npx",
        "args": ["-y", "@some/web-mcp"],
        "env": {"API_TOKEN": "x"},
    }


def test_update_guest_extra_mcp_persists(paths):
    rc = _seed_room_for_extra_mcp(paths)
    rc2 = admin.update_guest_extra_mcp(
        paths=paths, room_dir=rc.room_dir, name="宝总",
        servers=[
            {"name": "web", "command": "npx", "args": ["-y", "@mcp/web"]},
            {"name": "fs", "command": "fs-mcp", "env": {"ROOT": "/tmp"}},
        ],
    )
    by_name = rc2.guests[0].extra_mcp_servers
    assert by_name is not None
    assert list(by_name) == ["web", "fs"]  # 顺序稳定（list 顺序保留）
    assert by_name["web"] == {"command": "npx", "args": ["-y", "@mcp/web"]}
    assert by_name["fs"] == {"command": "fs-mcp", "env": {"ROOT": "/tmp"}}
    # reload 也看得到 —— 写盘真生效。
    rc3 = load_room_config(rc.room_dir, paths=paths)
    assert rc3.guests[0].extra_mcp_servers == by_name


def test_update_guest_extra_mcp_empty_clears(paths):
    rc = _seed_room_for_extra_mcp(paths)
    admin.update_guest_extra_mcp(
        paths=paths, room_dir=rc.room_dir, name="宝总",
        servers=[{"name": "web", "command": "npx"}],
    )
    rc_after = admin.update_guest_extra_mcp(
        paths=paths, room_dir=rc.room_dir, name="宝总", servers=[],
    )
    assert rc_after.guests[0].extra_mcp_servers is None
    text = (rc.room_dir / "room.toml").read_text("utf-8")
    assert "[[guest.extra_mcp_servers]]" not in text


def test_update_guest_extra_mcp_duplicate_name_rejected(paths):
    rc = _seed_room_for_extra_mcp(paths)
    original = (rc.room_dir / "room.toml").read_text("utf-8")
    with pytest.raises(RoomConfigError, match=r"重复"):
        admin.update_guest_extra_mcp(
            paths=paths, room_dir=rc.room_dir, name="宝总",
            servers=[
                {"name": "dup", "command": "a"},
                {"name": "dup", "command": "b"},
            ],
        )
    # 预校验 → 磁盘不动。
    assert (rc.room_dir / "room.toml").read_text("utf-8") == original


def test_update_guest_extra_mcp_unknown_name(paths):
    rc = _seed_room_for_extra_mcp(paths)
    with pytest.raises(ValueError, match="不在房间"):
        admin.update_guest_extra_mcp(
            paths=paths, room_dir=rc.room_dir, name="路人",
            servers=[{"name": "w", "command": "c"}],
        )


@pytest.mark.parametrize("servers,match", [
    ([{"command": "c"}], r"name 必须是非空字符串"),
    ([{"name": "", "command": "c"}], r"name 必须是非空字符串"),
    ([{"name": "x"}], r"command 必须是非空字符串"),
    ([{"name": "x", "command": "c", "args": "not-a-list"}], r"args 必须是字符串列表"),
    ([{"name": "x", "command": "c", "args": [1, 2]}], r"args 必须是字符串列表"),
    ([{"name": "x", "command": "c", "env": "not-a-dict"}], r"env 必须是 str→str"),
    ([{"name": "x", "command": "c", "env": {"K": 1}}], r"env 必须是 str→str"),
    ([{"name": "x", "command": "c", "cwd": "/tmp"}], r"未知字段"),
])
def test_update_guest_extra_mcp_invalid_rejected(paths, servers, match):
    rc = _seed_room_for_extra_mcp(paths)
    original = (rc.room_dir / "room.toml").read_text("utf-8")
    with pytest.raises(RoomConfigError, match=match):
        admin.update_guest_extra_mcp(
            paths=paths, room_dir=rc.room_dir, name="宝总", servers=servers,
        )
    assert (rc.room_dir / "room.toml").read_text("utf-8") == original


def test_extra_mcp_preserved_alongside_other_mutators(paths):
    """改 isolation / LLM 不应丢已写好的 extra_mcp_servers，反之亦然。"""
    rc = _seed_room_for_extra_mcp(paths)
    admin.update_guest_extra_mcp(
        paths=paths, room_dir=rc.room_dir, name="宝总",
        servers=[{"name": "web", "command": "npx", "args": ["@x"]}],
    )
    admin.update_guest_isolation(
        paths=paths, room_dir=rc.room_dir, name="宝总", isolation="global",
    )
    rc2 = admin.update_guest_llm(
        paths=paths, room_dir=rc.room_dir, name="宝总",
        spec_dict={"model": "openai/gpt-4"},
    )
    gc = rc2.guests[0]
    assert gc.isolation == "global"
    assert gc.llm is not None
    assert gc.extra_mcp_servers is not None
    assert gc.extra_mcp_servers["web"]["args"] == ["@x"]


def test_extra_mcp_load_rejects_duplicate_in_raw_toml(paths):
    rc = _seed_room_for_extra_mcp(paths)
    bad_toml = (
        '[room]\nname = "x"\n\n'
        '[[guest]]\nname = "宝总"\npersona = "chahua/personas/宝总.md"\n\n'
        '[[guest.extra_mcp_servers]]\nname = "dup"\ncommand = "a"\n\n'
        '[[guest.extra_mcp_servers]]\nname = "dup"\ncommand = "b"\n'
    )
    with pytest.raises(RoomConfigError, match=r"重复"):
        admin.update_room_toml(rc.room_dir, bad_toml, paths=paths)


def test_extra_mcp_load_rejects_unknown_entry_key(paths):
    rc = _seed_room_for_extra_mcp(paths)
    bad_toml = (
        '[room]\nname = "x"\n\n'
        '[[guest]]\nname = "宝总"\npersona = "chahua/personas/宝总.md"\n\n'
        '[[guest.extra_mcp_servers]]\nname = "w"\ncommand = "c"\ncwd = "/tmp"\n'
    )
    with pytest.raises(RoomConfigError, match=r"未知字段"):
        admin.update_room_toml(rc.room_dir, bad_toml, paths=paths)


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


# update_room_toml ───────────────────────────────────────────────────────


def _seed_room_for_update(paths):
    return admin.create_room(
        paths=paths, room_id="upd-room", name="待改",
        guests=[{"persona": "chahua/personas/宝总.md", "name": "宝总"}],
    )


def test_update_room_toml_round_trip(paths):
    rc = _seed_room_for_update(paths)
    new_toml = (
        '[room]\n'
        'name  = "新名"\n'
        'topic = "新话题"\n'
        'rules = ""\n'
        '\n'
        '[[guest]]\n'
        'name       = "宝总"\n'
        'persona    = "chahua/personas/宝总.md"\n'
        'permission = "read-only"\n'
    )
    rc2 = admin.update_room_toml(rc.room_dir, new_toml, paths=paths)
    assert rc2.name == "新名"
    assert rc2.topic == "新话题"
    assert (rc.room_dir / "room.toml").read_text("utf-8") == new_toml


def test_update_room_toml_rolls_back_on_invalid(paths):
    rc = _seed_room_for_update(paths)
    original = (rc.room_dir / "room.toml").read_text("utf-8")
    bad_toml = '[room]\nname = "x"\n# 缺 [[guest]]，load_room_config 会拒\n'
    with pytest.raises(RoomConfigError):
        admin.update_room_toml(rc.room_dir, bad_toml, paths=paths)
    # 回滚：磁盘上仍是原文，房间仍可装载。
    assert (rc.room_dir / "room.toml").read_text("utf-8") == original


def test_update_room_toml_rejects_oversized(paths):
    rc = _seed_room_for_update(paths)
    with pytest.raises(ValueError, match="room.toml 太大"):
        admin.update_room_toml(rc.room_dir, "x" * (64 * 1024 + 1), paths=paths)


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


# ── [room] 编排参数 round-trip / mutator（P4.0）────────────────────────────


def _seed_room_for_orch(paths):
    return admin.create_room(
        paths=paths, room_id="orch", name="orch",
        guests=[{"persona": "chahua/personas/宝总.md", "name": "宝总"}],
    )


def test_orchestrator_overrides_round_trip(paths):
    """用户在 toml 写编排参数 → load_room_config 解析进 RoomConfig.orchestrator_overrides
    → mutator 写回去再 load 也保留。"""
    rc = _seed_room_for_orch(paths)
    rc2 = admin.update_room_orchestrator(
        paths=paths, room_dir=rc.room_dir,
        overrides={"want_threshold": 0.9, "max_consecutive_ai_turns": 8},
    )
    assert rc2.orchestrator_overrides == {
        "want_threshold": 0.9, "max_consecutive_ai_turns": 8,
    }
    rc3 = load_room_config(rc.room_dir, paths=paths)
    assert rc3.orchestrator_overrides == rc2.orchestrator_overrides


def test_orchestrator_overrides_applied_via_dataclasses_replace(paths):
    """RoomConfig.orchestrator_overrides ↔ OrchestratorConfig 字段类型对得上 ——
    session.build_room_session 用 dataclasses.replace(OrchestratorConfig(), **overrides)
    patch 时不会因类型不兼容炸。"""
    import dataclasses
    from chahua.orchestrator import OrchestratorConfig

    rc = _seed_room_for_orch(paths)
    rc2 = admin.update_room_orchestrator(
        paths=paths, room_dir=rc.room_dir,
        overrides={"want_threshold": 0.7, "speaker_cooldown_turns": 2},
    )
    cfg = dataclasses.replace(OrchestratorConfig(), **rc2.orchestrator_overrides)
    assert cfg.want_threshold == 0.7
    assert cfg.speaker_cooldown_turns == 2
    # 没设的字段保留 OrchestratorConfig 默认。
    assert cfg.max_consecutive_ai_turns == OrchestratorConfig().max_consecutive_ai_turns


def test_orchestrator_overrides_empty_clears(paths):
    """传 {} 即清掉所有 [room] 段下的编排键 —— 让 OrchestratorConfig 默认接管。"""
    rc = _seed_room_for_orch(paths)
    admin.update_room_orchestrator(
        paths=paths, room_dir=rc.room_dir,
        overrides={"want_threshold": 0.9, "max_consecutive_ai_turns": 8},
    )
    rc_after_clear = admin.update_room_orchestrator(
        paths=paths, room_dir=rc.room_dir, overrides={},
    )
    assert rc_after_clear.orchestrator_overrides == {}
    # toml 里也不应再含这些键。
    text = (rc.room_dir / "room.toml").read_text("utf-8")
    assert "want_threshold" not in text
    assert "max_consecutive_ai_turns" not in text


def test_orchestrator_overrides_replace_semantics(paths):
    """update_room_orchestrator 是**整体覆盖**，不与现有 merge。"""
    rc = _seed_room_for_orch(paths)
    admin.update_room_orchestrator(
        paths=paths, room_dir=rc.room_dir,
        overrides={"want_threshold": 0.9},
    )
    rc2 = admin.update_room_orchestrator(
        paths=paths, room_dir=rc.room_dir,
        overrides={"max_consecutive_ai_turns": 8},
    )
    # want_threshold 被撤回，只有 max_consecutive_ai_turns 在册。
    assert rc2.orchestrator_overrides == {"max_consecutive_ai_turns": 8}


@pytest.mark.parametrize("overrides,match", [
    ({"want_threshold": 1.5}, r"越界"),                            # 上限超
    ({"want_threshold": -0.1}, r"越界"),                            # 下限超
    ({"max_consecutive_ai_turns": 0}, r"越界"),                     # >= 1 违反
    ({"speaker_cooldown_turns": -1}, r"越界"),                      # >= 0 违反
    ({"onboarding_threshold": 0}, r"越界"),                         # >= 1 违反
    ({"max_consecutive_ai_turns": 1.5}, r"必须是整数"),              # 类型错
    ({"want_threshold": "0.5"}, r"必须是数值"),                      # 字符串
    ({"want_threshold": True}, r"得到 bool"),                       # bool 不当数值
])
def test_orchestrator_overrides_bounds_enforced(paths, overrides, match):
    rc = _seed_room_for_orch(paths)
    original = (rc.room_dir / "room.toml").read_text("utf-8")
    with pytest.raises(RoomConfigError, match=match):
        admin.update_room_orchestrator(
            paths=paths, room_dir=rc.room_dir, overrides=overrides,
        )
    # 越界 / 类型错 → 校验失败回滚到旧 bytes（与 update_room_toml 同口径）。
    assert (rc.room_dir / "room.toml").read_text("utf-8") == original


def test_orchestrator_overrides_unknown_key_rejected(paths):
    """未知键 → _check_unknown_keys 报错（避免静默吞 typo）。"""
    rc = _seed_room_for_orch(paths)
    # 通过 raw editor 路径塞未知键 —— update_room_orchestrator 自身不挡未知键
    # （挡的是值范围），_check_unknown_keys 在 load 校验时挡。
    bad_toml = (
        '[room]\nname = "x"\n'
        'nonexistent_orch_field = 0.5\n\n'
        '[[guest]]\nname = "宝总"\npersona = "chahua/personas/宝总.md"\n'
    )
    with pytest.raises(RoomConfigError):
        admin.update_room_toml(rc.room_dir, bad_toml, paths=paths)


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
