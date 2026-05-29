"""P12.6 已安装 persona 管理 inbound 回归（走 task_inbound_srv 夹具）。

覆盖 list / update（成功 / 无 provenance 失败 / force 语义）/ delete（成功 / 失败）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chahua import persona_import
from chahua.events import ChahuaEventType, NOTICE_LEVEL_ERROR, NOTICE_LEVEL_INFO

PERSONAS = "chahua/personas"


def _import_folder(srv, tmp_path: Path, name="Yvonne", body="# SOUL v1") -> Path:
    src = tmp_path / "src" / name
    src.mkdir(parents=True)
    (src / f"{name}.md").write_text(body, encoding="utf-8")
    (src / "persona.toml").write_text(
        'schema_version = 1\nversion = "1.0.0"\n', encoding="utf-8"
    )
    persona_import.import_from_folder(srv._paths, src)
    return src


def _installed(srv, name="Yvonne") -> Path:
    return srv._paths.user_data_root / PERSONAS / name


def _capture():
    out: list[dict] = []
    return out, (lambda env: out.append(env.to_dict()))


def _of_type(captured, t) -> list[dict]:
    return [e for e in captured if e["type"] == t.value]


# ── list ──────────────────────────────────────────────────────────────────


async def test_inbound_list(task_inbound_srv, tmp_path) -> None:
    session, srv = task_inbound_srv
    _import_folder(srv, tmp_path, "Yvonne")
    # 无 provenance 的 dir-form。
    manual = _installed(srv, "Manual")
    manual.mkdir(parents=True)
    (manual / "Manual.md").write_text("# m", encoding="utf-8")

    captured, sink = _capture()
    await srv._handle_inbound({"type": "list_installed_personas"}, sink)

    frames = _of_type(captured, ChahuaEventType.PERSONAS_INSTALLED)
    assert len(frames) == 1
    rows = {r["name"]: r for r in frames[0]["data"]["personas"]}
    assert set(rows) == {"Yvonne", "Manual"}
    assert rows["Yvonne"]["status"] == "unknown"
    assert rows["Manual"]["status"] == "source_unavailable"


# ── update ──────────────────────────────────────────────────────────────────


async def test_inbound_update_success(task_inbound_srv, tmp_path) -> None:
    session, srv = task_inbound_srv
    src = _import_folder(srv, tmp_path, "Yvonne")
    (src / "Yvonne.md").write_text("# SOUL v2", encoding="utf-8")

    captured, sink = _capture()
    await srv._handle_inbound({"type": "update_persona", "name": "Yvonne"}, sink)

    notices = _of_type(captured, ChahuaEventType.NOTICE)
    assert any(n["data"]["level"] == NOTICE_LEVEL_INFO for n in notices)
    assert _of_type(captured, ChahuaEventType.ROOM_INFO)  # picker 刷新
    frames = _of_type(captured, ChahuaEventType.PERSONAS_INSTALLED)
    assert frames
    row = next(r for r in frames[-1]["data"]["personas"] if r["name"] == "Yvonne")
    assert row["status"] == "up_to_date"
    assert row["local_modified"] is False
    # 内容确实更新了。
    assert (_installed(srv) / "Yvonne.md").read_text(encoding="utf-8") == "# SOUL v2"


async def test_inbound_update_no_provenance_fails(task_inbound_srv) -> None:
    session, srv = task_inbound_srv
    manual = _installed(srv, "Manual")
    manual.mkdir(parents=True)
    (manual / "Manual.md").write_text("# m", encoding="utf-8")

    captured, sink = _capture()
    await srv._handle_inbound({"type": "update_persona", "name": "Manual"}, sink)

    notices = _of_type(captured, ChahuaEventType.NOTICE)
    assert any(n["data"]["level"] == NOTICE_LEVEL_ERROR for n in notices)
    # 旧目录不动。
    assert (manual / "Manual.md").read_text(encoding="utf-8") == "# m"
    # 失败也重发列表。
    assert _of_type(captured, ChahuaEventType.PERSONAS_INSTALLED)


async def test_inbound_update_local_modified_requires_force(task_inbound_srv, tmp_path) -> None:
    session, srv = task_inbound_srv
    _import_folder(srv, tmp_path, "Yvonne")
    (_installed(srv) / "Yvonne.md").write_text("本地手改", encoding="utf-8")

    # 无 force → 拒。
    captured, sink = _capture()
    await srv._handle_inbound({"type": "update_persona", "name": "Yvonne"}, sink)
    notices = _of_type(captured, ChahuaEventType.NOTICE)
    assert any(n["data"]["level"] == NOTICE_LEVEL_ERROR for n in notices)
    assert (_installed(srv) / "Yvonne.md").read_text(encoding="utf-8") == "本地手改"

    # force:true → 覆盖回源内容。
    captured2, sink2 = _capture()
    await srv._handle_inbound(
        {"type": "update_persona", "name": "Yvonne", "force": True}, sink2
    )
    assert any(
        n["data"]["level"] == NOTICE_LEVEL_INFO
        for n in _of_type(captured2, ChahuaEventType.NOTICE)
    )
    assert (_installed(srv) / "Yvonne.md").read_text(encoding="utf-8") == "# SOUL v1"


# ── delete ──────────────────────────────────────────────────────────────────


async def test_inbound_delete_success(task_inbound_srv, tmp_path) -> None:
    session, srv = task_inbound_srv
    _import_folder(srv, tmp_path, "Yvonne")

    captured, sink = _capture()
    await srv._handle_inbound({"type": "delete_persona", "name": "Yvonne"}, sink)

    assert any(
        n["data"]["level"] == NOTICE_LEVEL_INFO
        for n in _of_type(captured, ChahuaEventType.NOTICE)
    )
    assert _of_type(captured, ChahuaEventType.ROOM_INFO)
    frames = _of_type(captured, ChahuaEventType.PERSONAS_INSTALLED)
    assert frames
    names = {r["name"] for r in frames[-1]["data"]["personas"]}
    assert "Yvonne" not in names
    assert not _installed(srv).exists()


async def test_inbound_delete_traversal_fails(task_inbound_srv, tmp_path) -> None:
    session, srv = task_inbound_srv
    _import_folder(srv, tmp_path, "Yvonne")

    captured, sink = _capture()
    await srv._handle_inbound({"type": "delete_persona", "name": "../Yvonne"}, sink)

    assert any(
        n["data"]["level"] == NOTICE_LEVEL_ERROR
        for n in _of_type(captured, ChahuaEventType.NOTICE)
    )
    assert _installed(srv).is_dir()  # 没删掉
