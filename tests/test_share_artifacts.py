"""P10.2 / P10.3 房间公共桌面 share/ artifact 自动归集 + 注册表回归测试。

覆盖 review 闭环：

1. ``_normalize_share_rel`` —— 形态接受 / 拒绝、与 ``_list_share_rels`` 段策略对称。
2. ``_list_share_rels`` —— 目录 symlink 不下钻、dot-prefix 段全跳、max-file 软上限。
3. ``MessageArtifactRegistry`` —— 旧 jsonl 行兼容、严格 isinstance 防 None/list 当 mid、
   bool size 拒、round-trip persist+reload。
4. ``ArtifactDetector._detect_share_artifacts`` —— baseline 仅 stat 成功后提交、同 rel
   重写 emit、detect 末尾 GC share/ 路径 stale pending。
5. ``Orchestrator.mark_share_seen`` 公开访问器。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from chahua.artifact_detector import (
    _SHARE_SCAN_MAX_FILES,
    ArtifactDetector,
    _list_share_rels,
)
from chahua.cursor import GuestCursor
from chahua.events import ChahuaEnvelope, ChahuaEventType
from chahua.message_artifacts import ArtifactRef, MessageArtifactRegistry
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.tasks_store import TasksStore
from chahua.transport_bridge import _normalize_share_rel
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import NoopScorer, NoopSummarizer


# ── _normalize_share_rel ──────────────────────────────────────────────────


class TestNormalizeShareRel:
    def test_accepts_bare(self) -> None:
        assert _normalize_share_rel("share/foo.png") == "share/foo.png"

    def test_accepts_with_dot_slash(self) -> None:
        assert _normalize_share_rel("./share/foo.png") == "share/foo.png"

    def test_accepts_subdir(self) -> None:
        assert _normalize_share_rel("share/sub/bar.pdf") == "share/sub/bar.pdf"

    def test_accepts_multi_dot_slash(self) -> None:
        # P10.3 修：循环剥 './' —— LLM 异形输入 ``././share/foo``
        assert _normalize_share_rel("././share/foo.png") == "share/foo.png"
        assert _normalize_share_rel("./././share/x.png") == "share/x.png"

    def test_rejects_dot_dot(self) -> None:
        assert _normalize_share_rel("share/../etc") is None
        assert _normalize_share_rel("share/foo/../bar") is None

    def test_rejects_absolute(self) -> None:
        assert _normalize_share_rel("/abs/share/x") is None

    def test_rejects_backslash(self) -> None:
        # Windows-style path 整段拒。
        assert _normalize_share_rel("share\\foo.png") is None

    def test_rejects_non_share_prefix(self) -> None:
        assert _normalize_share_rel("task/foo.png") is None
        assert _normalize_share_rel("../share/foo.png") is None

    def test_rejects_no_filename(self) -> None:
        assert _normalize_share_rel("share") is None
        assert _normalize_share_rel("share/") is None

    def test_rejects_empty_or_none(self) -> None:
        assert _normalize_share_rel("") is None
        assert _normalize_share_rel(None) is None
        assert _normalize_share_rel(42) is None

    def test_rejects_dot_prefix_segment(self) -> None:
        # P10.3 修：段 startswith('.') 整族拒，与 _list_share_rels 对齐。
        assert _normalize_share_rel("share/.cache/foo.png") is None
        assert _normalize_share_rel("share/.git/HEAD") is None
        assert _normalize_share_rel("share/sub/.hidden.png") is None
        # 但合法子目录的 dot-prefix 文件也拒（更严的对齐）。
        assert _normalize_share_rel("share/.DS_Store") is None


# ── _list_share_rels ──────────────────────────────────────────────────────


class TestListShareRels:
    def test_none_returns_empty(self) -> None:
        assert _list_share_rels(None) == {}

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert _list_share_rels(tmp_path / "no-such") == {}

    def test_empty_dir(self, tmp_path: Path) -> None:
        share = tmp_path / "share"
        share.mkdir()
        assert _list_share_rels(share) == {}

    def test_flat_files(self, tmp_path: Path) -> None:
        share = tmp_path / "share"
        share.mkdir()
        (share / "a.png").write_bytes(b"x")
        (share / "b.pdf").write_bytes(b"y")
        # P10.3：返回值现在是 dict[rel, (mtime_ns, size)]；这里比较 keys。
        assert set(_list_share_rels(share)) == {"share/a.png", "share/b.pdf"}

    def test_returns_stamp_tuple(self, tmp_path: Path) -> None:
        # P10.3 review §1/§2：基线带 (mtime_ns, size) 让 rewrite 检测能区分真变化。
        share = tmp_path / "share"
        share.mkdir()
        (share / "a.png").write_bytes(b"hello")
        stamps = _list_share_rels(share)
        mtime_ns, size = stamps["share/a.png"]
        assert size == 5
        assert mtime_ns > 0

    def test_nested_subdir(self, tmp_path: Path) -> None:
        share = tmp_path / "share"
        sub = share / "sub"
        sub.mkdir(parents=True)
        (sub / "z.gif").write_bytes(b"x")
        assert set(_list_share_rels(share)) == {"share/sub/z.gif"}

    def test_dot_prefix_file_skipped(self, tmp_path: Path) -> None:
        share = tmp_path / "share"
        share.mkdir()
        (share / "foo.png").write_bytes(b"x")
        (share / ".DS_Store").write_bytes(b"y")
        (share / ".foo.png.tmp").write_bytes(b"z")  # write_bytes_atomic 残骸
        assert set(_list_share_rels(share)) == {"share/foo.png"}

    def test_dot_prefix_dir_pruned(self, tmp_path: Path) -> None:
        # P10.3 修：dot-prefix 目录整族剪枝，下面的文件全跳。
        share = tmp_path / "share"
        cache = share / ".cache"
        cache.mkdir(parents=True)
        (cache / "renders.png").write_bytes(b"x")
        (share / "ok.pdf").write_bytes(b"y")
        assert set(_list_share_rels(share)) == {"share/ok.pdf"}

    def test_directory_symlink_not_followed(self, tmp_path: Path) -> None:
        # P10.3 修：os.walk(followlinks=False)。建外目录与符号链接到 share/。
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"hush")
        share = tmp_path / "share"
        share.mkdir()
        (share / "real.png").write_bytes(b"x")
        try:
            (share / "leak").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")
        rels = set(_list_share_rels(share))
        assert "share/real.png" in rels
        # 外目录文件不被枚举进来。
        assert not any(r.startswith("share/leak/") for r in rels)

    def test_file_symlink_skipped(self, tmp_path: Path) -> None:
        outside = tmp_path / "elsewhere.txt"
        outside.write_bytes(b"x")
        share = tmp_path / "share"
        share.mkdir()
        (share / "real.png").write_bytes(b"y")
        try:
            (share / "alias.png").symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")
        assert set(_list_share_rels(share)) == {"share/real.png"}

    def test_invalid_basename_skipped(self, tmp_path: Path) -> None:
        share = tmp_path / "share"
        share.mkdir()
        # 实际文件名不可能含 '/' 但可以是首字符点 / 其它 validator 拒的。
        (share / "ok.png").write_bytes(b"x")
        (share / ".hidden").write_bytes(b"y")
        assert set(_list_share_rels(share)) == {"share/ok.png"}

    def test_max_file_cap(self, tmp_path: Path) -> None:
        share = tmp_path / "share"
        share.mkdir()
        # 多创几个，确保上限把后续截掉；不真造 5000 个文件（CI 慢），改用单元覆盖
        # 上限语义：用 monkey-patch 让上限 == 3。
        import chahua.artifact_detector as ad

        original = ad._SHARE_SCAN_MAX_FILES
        try:
            ad._SHARE_SCAN_MAX_FILES = 3
            for i in range(5):
                (share / f"f{i}.png").write_bytes(b"x")
            rels = ad._list_share_rels(share)
            assert len(rels) == 3
        finally:
            ad._SHARE_SCAN_MAX_FILES = original

    def test_cap_default_high_enough(self) -> None:
        # 烟测：默认上限不能太小。
        assert _SHARE_SCAN_MAX_FILES >= 1000


# ── MessageArtifactRegistry ───────────────────────────────────────────────


class TestMessageArtifactRegistry:
    def test_legacy_form_loads(self, tmp_path: Path) -> None:
        # P10.2 前的形：{message_id, task_id, name, size}（无 rel）→ 派生 rel。
        p = tmp_path / "m.jsonl"
        p.write_text(json.dumps({
            "message_id": "m1", "task_id": "t1", "name": "x.png", "size": 100,
        }) + "\n")
        r = MessageArtifactRegistry(store_path=p)
        refs = r.for_message("m1")
        assert len(refs) == 1
        assert refs[0].rel == "tasks/t1/artifacts/x.png"
        assert refs[0].name == "x.png"
        assert refs[0].size == 100
        assert refs[0].task_id == "t1"  # 派生

    def test_new_form_loads(self, tmp_path: Path) -> None:
        p = tmp_path / "m.jsonl"
        p.write_text(json.dumps({
            "message_id": "m2", "rel": "share/y.pdf", "name": "y.pdf", "size": 200,
        }) + "\n")
        r = MessageArtifactRegistry(store_path=p)
        refs = r.for_message("m2")
        assert refs[0].rel == "share/y.pdf"
        assert refs[0].task_id is None  # share/ 无任务归属

    def test_bool_size_rejected(self, tmp_path: Path) -> None:
        # P10.3 修：`int(True) == 1` 不抛错，必须显式拒 bool。
        p = tmp_path / "m.jsonl"
        p.write_text(json.dumps({
            "message_id": "m", "rel": "share/x", "name": "x", "size": True,
        }) + "\n")
        r = MessageArtifactRegistry(store_path=p)
        refs = r.for_message("m")
        assert refs[0].size == 0  # 不是 1！

    def test_non_str_mid_skipped(self, tmp_path: Path) -> None:
        # P10.3 修：str(None) == 'None' 不该当合法 mid。
        p = tmp_path / "m.jsonl"
        p.write_text(json.dumps({
            "message_id": None, "rel": "share/x", "name": "x", "size": 1,
        }) + "\n")
        r = MessageArtifactRegistry(store_path=p)
        assert r.by_message == {}

    def test_non_str_name_skipped(self, tmp_path: Path) -> None:
        p = tmp_path / "m.jsonl"
        p.write_text(json.dumps({
            "message_id": "m", "rel": "share/x", "name": ["a"], "size": 1,
        }) + "\n")
        r = MessageArtifactRegistry(store_path=p)
        assert r.by_message == {}

    def test_round_trip(self, tmp_path: Path) -> None:
        p = tmp_path / "m.jsonl"
        r1 = MessageArtifactRegistry(store_path=p)
        r1.persist(message_id="m", rel="share/sub/z.gif", name="z.gif", size=42)
        # 重新加载。
        r2 = MessageArtifactRegistry(store_path=p)
        refs = r2.for_message("m")
        assert refs[0].rel == "share/sub/z.gif"
        assert refs[0].size == 42

    def test_pending_pop(self, tmp_path: Path) -> None:
        r = MessageArtifactRegistry(store_path=tmp_path / "m.jsonl")
        r.record_pending(rel="share/foo.png", name="foo.png", message_id="m1")
        assert r.consume_pending(rel="share/foo.png") == "m1"
        # 第二次 pop 返 None。
        assert r.consume_pending(rel="share/foo.png") is None

    def test_pending_overwrites(self, tmp_path: Path) -> None:
        r = MessageArtifactRegistry(store_path=tmp_path / "m.jsonl")
        r.record_pending(rel="share/foo.png", name="foo.png", message_id="m1")
        r.record_pending(rel="share/foo.png", name="foo.png", message_id="m2")
        assert r.consume_pending(rel="share/foo.png") == "m2"

    def test_clear_truncates(self, tmp_path: Path) -> None:
        p = tmp_path / "m.jsonl"
        r = MessageArtifactRegistry(store_path=p)
        r.persist(message_id="m", rel="share/x", name="x", size=1)
        r.clear()
        assert r.by_message == {}
        # 文件被截 0 字节。
        assert p.read_text() == ""


# ── ArtifactDetector._detect_share_artifacts ───────────────────────────────


class _Sink:
    def __init__(self) -> None:
        self.envs: list[ChahuaEnvelope] = []

    def __call__(self, env: ChahuaEnvelope) -> None:
        self.envs.append(env)


def _make_detector(
    tmp_path: Path, *, with_registry: bool = True
) -> tuple[ArtifactDetector, MessageArtifactRegistry | None, Path]:
    room = Room(name="r")
    share = tmp_path / "share"
    share.mkdir()
    registry = (
        MessageArtifactRegistry(store_path=tmp_path / "m.jsonl")
        if with_registry else None
    )
    det = ArtifactDetector(
        room=room,
        tasks_store=None,
        share_dir=share,
        message_artifacts=registry,
    )
    return det, registry, share


class TestDetectShareArtifacts:
    def test_no_share_dir_noop(self, tmp_path: Path) -> None:
        det = ArtifactDetector(
            room=Room(name="r"),
            tasks_store=None,
            share_dir=None,
            message_artifacts=None,
        )
        sink = _Sink()
        det.detect(sink, active_task_id=None)
        assert sink.envs == []

    def test_baseline_seeded(self, tmp_path: Path) -> None:
        det, _, share = _make_detector(tmp_path)
        # 预放一个文件，detector 启动时把它视为已知。
        (share / "old.png").write_bytes(b"x")
        # 重建 detector 让它再扫一次（init 之后 share_seen 已固定）。
        det2 = ArtifactDetector(
            room=Room(name="r"), tasks_store=None,
            share_dir=share, message_artifacts=None,
        )
        sink = _Sink()
        det2.detect(sink, active_task_id=None)
        assert sink.envs == []  # 已在基线，不 emit

    def test_new_file_emits(self, tmp_path: Path) -> None:
        det, _, share = _make_detector(tmp_path)
        (share / "fresh.png").write_bytes(b"x")
        sink = _Sink()
        det.detect(sink, active_task_id=None)
        room_emits = [e for e in sink.envs if e.type is ChahuaEventType.ROOM_ARTIFACT_ADDED]
        assert len(room_emits) == 1
        assert room_emits[0].data["rel"] == "share/fresh.png"
        assert room_emits[0].data["name"] == "fresh.png"
        assert room_emits[0].data["size"] == 1
        # 第二次 detect 不再 emit。
        sink2 = _Sink()
        det.detect(sink2, active_task_id=None)
        assert sink2.envs == []

    def test_mark_share_seen_suppresses(self, tmp_path: Path) -> None:
        det, _, share = _make_detector(tmp_path)
        (share / "user-upload.png").write_bytes(b"x")
        det.mark_share_seen("share/user-upload.png")
        sink = _Sink()
        det.detect(sink, active_task_id=None)
        assert sink.envs == []

    def test_pending_attaches_originated_message_id(self, tmp_path: Path) -> None:
        det, registry, share = _make_detector(tmp_path)
        assert registry is not None
        registry.record_pending(rel="share/a.png", name="a.png", message_id="mid_42")
        (share / "a.png").write_bytes(b"hello")
        sink = _Sink()
        det.detect(sink, active_task_id=None)
        room_emits = [e for e in sink.envs if e.type is ChahuaEventType.ROOM_ARTIFACT_ADDED]
        assert len(room_emits) == 1
        assert room_emits[0].data["originated_message_id"] == "mid_42"
        # pending 已被消费。
        assert registry.consume_pending(rel="share/a.png") is None
        # by_message 落了一行。
        assert any(r.rel == "share/a.png" for r in registry.for_message("mid_42"))

    def test_pending_gc_on_failed_write(self, tmp_path: Path) -> None:
        # P10.3 修：detect 末尾清理 share/ 路径下"文件没落盘"的 pending。
        det, registry, share = _make_detector(tmp_path)
        assert registry is not None
        # 工具发了 tool_start 但实际写盘失败，文件不在 share/ 里。
        registry.record_pending(rel="share/ghost.png", name="ghost.png", message_id="m1")
        sink = _Sink()
        det.detect(sink, active_task_id=None)
        # 没 emit（文件不在盘上）。
        assert all(e.type is not ChahuaEventType.ROOM_ARTIFACT_ADDED for e in sink.envs)
        # pending 被 GC。
        assert "share/ghost.png" not in registry.pending

    def test_rewrite_emits_via_pending(self, tmp_path: Path) -> None:
        # P10.3 修：同 rel 重写时，pending 有新 mid → emit。
        det, registry, share = _make_detector(tmp_path)
        assert registry is not None
        (share / "doc.md").write_bytes(b"v1")
        # 重建 detector 让 doc.md 进基线。
        det = ArtifactDetector(
            room=Room(name="r"), tasks_store=None,
            share_dir=share, message_artifacts=registry,
        )
        assert "share/doc.md" in det.share_seen
        # 茶客 m2 改 doc.md。
        registry.record_pending(rel="share/doc.md", name="doc.md", message_id="m2")
        (share / "doc.md").write_bytes(b"v2-longer")
        sink = _Sink()
        det.detect(sink, active_task_id=None)
        room_emits = [e for e in sink.envs if e.type is ChahuaEventType.ROOM_ARTIFACT_ADDED]
        assert len(room_emits) == 1
        assert room_emits[0].data["originated_message_id"] == "m2"
        assert room_emits[0].data["size"] == len(b"v2-longer")

    def test_stat_race_does_not_poison_baseline(self, tmp_path: Path) -> None:
        # P10.3 修：share_seen 在 stat 成功后才 commit；模拟 stat 抛 OSError。
        # 现在 stat 跑在 ``_list_share_rels`` 内部，patch ``os.stat`` 让 race.png 抛错。
        det, _, share = _make_detector(tmp_path)
        (share / "race.png").write_bytes(b"x")
        import chahua.artifact_detector as ad

        original_os_stat = ad.os.stat
        thrown = {"once": False}

        def stat_first_throws(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            if not thrown["once"] and str(path).endswith("race.png"):
                thrown["once"] = True
                raise FileNotFoundError(path)
            return original_os_stat(path, *args, **kwargs)

        ad.os.stat = stat_first_throws  # type: ignore[assignment]
        try:
            sink = _Sink()
            det.detect(sink, active_task_id=None)
            # 没 emit（stat 失败 → race.png 不入 current）。
            assert all(
                e.type is not ChahuaEventType.ROOM_ARTIFACT_ADDED for e in sink.envs
            )
            # 关键不变量：share_seen 不包含这个 rel，下次 detect 仍能拿到 emit。
            assert "share/race.png" not in det.share_seen
            sink2 = _Sink()
            det.detect(sink2, active_task_id=None)
            room_emits = [
                e for e in sink2.envs if e.type is ChahuaEventType.ROOM_ARTIFACT_ADDED
            ]
            assert len(room_emits) == 1
        finally:
            ad.os.stat = original_os_stat  # type: ignore[assignment]

    def test_failed_write_no_rewrite_emit(self, tmp_path: Path) -> None:
        """P10.3 review §1/§2：``write_file`` 失败/取消 → 旧文件还在、pending 留着,
        但指纹没变 → 不应误触发 rewrite emit，且 stale pending 末尾被 GC。"""
        det, registry, share = _make_detector(tmp_path)
        assert registry is not None
        # 让 stable.md 进入基线。
        (share / "stable.md").write_bytes(b"v1")
        det = ArtifactDetector(
            room=Room(name="r"), tasks_store=None,
            share_dir=share, message_artifacts=registry,
        )
        # 茶客 m_failed 调 write_file —— tool_start 触发 pending，但实际工具失败,
        # 没改盘上文件（mtime/size 不变）。
        registry.record_pending(
            rel="share/stable.md", name="stable.md", message_id="m_failed",
        )
        sink = _Sink()
        det.detect(sink, active_task_id=None)
        # 没 emit —— 文件指纹没变（无 "v1" 之外的内容）。
        room_emits = [
            e for e in sink.envs if e.type is ChahuaEventType.ROOM_ARTIFACT_ADDED
        ]
        assert room_emits == []
        # 关键不变量：stale pending 被 GC，未来同 rel 的真重写不会绑这条死消息。
        assert "share/stable.md" not in registry.pending

    def test_mark_seen_seeds_stamp_after_user_upload(self, tmp_path: Path) -> None:
        """P10.3 review §4：``mark_seen`` 不只记 name，必须同步写指纹基线 ——
        否则用户上传后茶客对同名 artifact 的失败/no-op 写盘会被 rewrite 检测当
        "stamp != None" 误发，错把用户文件归给那条失败消息。"""
        from chahua.tasks_store import TasksStore as _TS

        room = Room(name="t")
        store = _TS(room_dir=tmp_path)
        task = store.open_task(title="T", goal="g")
        registry = MessageArtifactRegistry(store_path=tmp_path / "m.jsonl")
        det = ArtifactDetector(
            room=room, tasks_store=store,
            share_dir=tmp_path / "share",
            message_artifacts=registry,
        )
        # 模拟用户走 attach_artifact 上传文件到任务（盘上落、mark_seen 同步指纹）。
        artifacts_dir = store.artifacts_dir(task.id)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "user.md").write_text("user uploaded", encoding="utf-8")
        det.mark_seen(task.id, "user.md")
        # 茶客后续调 task_write_artifact 同名 —— pending 记下，但调用失败，文件
        # 没动。
        rel = f"tasks/{task.id}/artifacts/user.md"
        registry.record_pending(rel=rel, name="user.md", message_id="m_failed")
        sink = _Sink()
        det.detect(sink, active_task_id=task.id)
        # 不应误把用户上传的 user.md 归给 m_failed。
        task_emits = [
            e for e in sink.envs if e.type is ChahuaEventType.TASK_ARTIFACT_ADDED
        ]
        assert task_emits == []
        # stale pending 也被 GC。
        assert rel not in registry.pending

    def test_failed_task_write_no_rewrite_emit(self, tmp_path: Path) -> None:
        """P10.3 review §1：``task_write_artifact`` 失败时同样不该误发 rewrite。"""
        from chahua.cursor import GuestCursor
        from chahua.orchestrator import OrchestratorConfig
        from chahua.task import TASK_UNTITLED
        from chahua.tasks_store import TasksStore as _TS

        room = Room(name="t")
        room.add_participant(USER_SPEAKER_ID)
        store = _TS(room_dir=tmp_path)
        task = store.open_task(title="T", goal="g")
        # 茶客先成功写一版。
        store.write_artifact(task.id, name="report.md", content="v1")
        registry = MessageArtifactRegistry(store_path=tmp_path / "m.jsonl")
        det = ArtifactDetector(
            room=room, tasks_store=store,
            share_dir=tmp_path / "share",  # 不存在不影响 task 路径
            message_artifacts=registry,
        )
        # 茶客 m_failed 调 task_write_artifact —— pending 落表，但工具失败,
        # 文件指纹（mtime_ms, size）不变。
        rel = f"tasks/{task.id}/artifacts/report.md"
        registry.record_pending(rel=rel, name="report.md", message_id="m_failed")
        sink = _Sink()
        det.detect(sink, active_task_id=task.id)
        task_emits = [
            e for e in sink.envs if e.type is ChahuaEventType.TASK_ARTIFACT_ADDED
        ]
        assert task_emits == []
        # stale pending GC。
        assert rel not in registry.pending


# ── P10.4：shell / MCP 工具 TOOL_START/COMPLETE 前后 diff 回填 pending ────


class _TransportSink:
    """轻量 sink；transport.emit_chahua 在 bind 期会推 envelope 进来。"""

    def __init__(self) -> None:
        self.envs: list[ChahuaEnvelope] = []

    def __call__(self, env: ChahuaEnvelope) -> None:
        self.envs.append(env)


def _make_transport_with_share(
    tmp_path: Path,
) -> tuple[Any, MessageArtifactRegistry, Path]:
    """share/ + registry + transport 一起搭好，share/ 已 mkdir。"""
    from chahua.transport_bridge import ChahuaTransport

    share = tmp_path / "share"
    share.mkdir()
    registry = MessageArtifactRegistry(store_path=tmp_path / "m.jsonl")
    transport = ChahuaTransport(
        room_id="r", guest_name="A",
        message_artifacts=registry,
        share_dir=share,
    )
    return transport, registry, share


def _fire_tool_start(
    transport: Any, *, tool: str, call_id: str = "c1",
) -> None:
    from agentao.transport import AgentEvent, EventType
    transport._handle(  # noqa: SLF001 — 单测有意走内部入口
        AgentEvent(
            type=EventType.TOOL_START,
            data={"tool": tool, "args": {}, "call_id": call_id},
        )
    )


def _fire_tool_complete(
    transport: Any, *, tool: str, call_id: str = "c1",
    status: str = "ok",
) -> None:
    from agentao.transport import AgentEvent, EventType
    transport._handle(  # noqa: SLF001
        AgentEvent(
            type=EventType.TOOL_COMPLETE,
            data={"tool": tool, "call_id": call_id, "status": status},
        )
    )


class TestShellMcpToolDiff:
    """P10.4：shell / MCP 工具的"前后 diff 回填 pending"路径。"""

    def test_shell_new_share_file_records_pending(
        self, tmp_path: Path,
    ) -> None:
        """shell 工具运行期间往 share/ 新写文件 → pending 收到该 rel → msg_x。"""
        transport, registry, share = _make_transport_with_share(tmp_path)
        sink = _TransportSink()
        with transport.bind(
            sink=sink, turn_id="t1", message_id="msg_x",
        ):
            _fire_tool_start(transport, tool="run_shell_command")
            # 在 shell 工具"运行期间"模拟它调 cp/echo 落了个文件。
            (share / "out.png").write_bytes(b"\x89PNG\r\n")
            _fire_tool_complete(transport, tool="run_shell_command")
        assert registry.pending.get("share/out.png") == "msg_x"

    def test_mcp_new_share_file_records_pending(
        self, tmp_path: Path,
    ) -> None:
        """MCP 工具同样走 diff 路径 —— 任何 mcp__* 前缀都接。"""
        transport, registry, share = _make_transport_with_share(tmp_path)
        sink = _TransportSink()
        with transport.bind(
            sink=sink, turn_id="t1", message_id="msg_y",
        ):
            _fire_tool_start(
                transport, tool="mcp__github__upload_artifact", call_id="c2",
            )
            (share / "release.tar.gz").write_bytes(b"tarball")
            _fire_tool_complete(
                transport, tool="mcp__github__upload_artifact", call_id="c2",
            )
        assert registry.pending.get("share/release.tar.gz") == "msg_y"

    def test_non_tracked_tool_no_pending(self, tmp_path: Path) -> None:
        """不在白名单的工具（如 read_file）—— 哪怕期间有文件出现也不 record_pending。
        snapshot 没存就不会被 diff。"""
        transport, registry, share = _make_transport_with_share(tmp_path)
        sink = _TransportSink()
        with transport.bind(
            sink=sink, turn_id="t1", message_id="msg_z",
        ):
            _fire_tool_start(transport, tool="read_file", call_id="c3")
            (share / "side.png").write_bytes(b"png")
            _fire_tool_complete(transport, tool="read_file", call_id="c3")
        # diff 路径完全 no-op；ArtifactDetector 的 cycle-end 扫描仍会扫到这个文件，
        # 但不会有 originated_message_id（pending 没条目）。
        assert registry.pending == {}

    def test_no_file_change_no_pending(self, tmp_path: Path) -> None:
        """shell 跑完盘上没新增 / 没变 → pending 空。snapshot 仍取了 + diff
        了，但 diff 结果为空集，不该误 record。"""
        # 先放一个已有文件作为 baseline。
        transport, registry, share = _make_transport_with_share(tmp_path)
        (share / "stable.md").write_bytes(b"v1")
        sink = _TransportSink()
        with transport.bind(
            sink=sink, turn_id="t1", message_id="msg_a",
        ):
            _fire_tool_start(transport, tool="run_shell_command", call_id="c4")
            # shell 跑了但没碰 share/。
            _fire_tool_complete(
                transport, tool="run_shell_command", call_id="c4",
            )
        assert registry.pending == {}

    def test_file_overwrite_records_pending(self, tmp_path: Path) -> None:
        """shell 把已有文件改了 mtime/size → 算 rewrite，pending 收到该消息。"""
        import os
        transport, registry, share = _make_transport_with_share(tmp_path)
        target = share / "report.md"
        target.write_bytes(b"v1")
        # 提前把 mtime 拨回过去，避免快照与重写在同一 nanosecond。
        past = target.stat().st_mtime - 5
        os.utime(target, (past, past))
        sink = _TransportSink()
        with transport.bind(
            sink=sink, turn_id="t1", message_id="msg_b",
        ):
            _fire_tool_start(transport, tool="run_shell_command", call_id="c5")
            target.write_bytes(b"v2-longer-bytes")
            _fire_tool_complete(
                transport, tool="run_shell_command", call_id="c5",
            )
        assert registry.pending.get("share/report.md") == "msg_b"

    def test_file_deleted_no_pending(self, tmp_path: Path) -> None:
        """shell 删了文件 —— 后快照少了 rel，diff 只看 after 里 vs before 的差异：
        删除的 rel 不在 after，不会被 record。"""
        transport, registry, share = _make_transport_with_share(tmp_path)
        victim = share / "victim.txt"
        victim.write_bytes(b"bye")
        sink = _TransportSink()
        with transport.bind(
            sink=sink, turn_id="t1", message_id="msg_c",
        ):
            _fire_tool_start(transport, tool="run_shell_command", call_id="c6")
            victim.unlink()
            _fire_tool_complete(
                transport, tool="run_shell_command", call_id="c6",
            )
        assert registry.pending == {}

    def test_tool_complete_without_start_noop(self, tmp_path: Path) -> None:
        """TOOL_COMPLETE 单飞（TOOL_START 漏发 / 不在白名单）—— pop 返 None，不抛。"""
        transport, registry, share = _make_transport_with_share(tmp_path)
        sink = _TransportSink()
        with transport.bind(
            sink=sink, turn_id="t1", message_id="msg_d",
        ):
            (share / "ghost.png").write_bytes(b"png")
            _fire_tool_complete(
                transport, tool="run_shell_command", call_id="never_started",
            )
        assert registry.pending == {}

    def test_bind_exit_keeps_snapshot_for_late_complete(
        self, tmp_path: Path,
    ) -> None:
        """P10.4 review §6 修：bind 退出后 snapshot **不清** —— 让晚到的
        TOOL_COMPLETE 能用 entry 里捕获的 mid 完成 record_pending、不丢归属。
        call_id 跨 bind 唯一（agentao 全局），没有"下次 bind 撞 stale"风险。"""
        transport, _registry, _share = _make_transport_with_share(tmp_path)
        sink = _TransportSink()
        with transport.bind(
            sink=sink, turn_id="t1", message_id="msg_e",
        ):
            _fire_tool_start(transport, tool="run_shell_command", call_id="c7")
            assert "c7" in transport._tool_call_snapshots  # noqa: SLF001
        # bind 退出 → snapshot 仍在，等 TOOL_COMPLETE 自然 pop。
        assert "c7" in transport._tool_call_snapshots  # noqa: SLF001
        # diff_baseline 还是要清——是 per-bind 的滚动快照。
        assert transport._diff_baseline is None  # noqa: SLF001

    def test_no_message_artifacts_disabled(self, tmp_path: Path) -> None:
        """message_artifacts=None（老调用方 / 早期 CLI）→ TOOL_START 不存
        snapshot、TOOL_COMPLETE 也走不到 consume，整条路径 no-op。"""
        from chahua.transport_bridge import ChahuaTransport
        share = tmp_path / "share"
        share.mkdir()
        transport = ChahuaTransport(
            room_id="r", guest_name="A",
            message_artifacts=None,
            share_dir=share,
        )
        sink = _TransportSink()
        with transport.bind(
            sink=sink, turn_id="t1", message_id="msg_f",
        ):
            _fire_tool_start(transport, tool="run_shell_command", call_id="c8")
            (share / "out.png").write_bytes(b"png")
            _fire_tool_complete(
                transport, tool="run_shell_command", call_id="c8",
            )
        assert transport._tool_call_snapshots == {}  # noqa: SLF001

    def test_shell_writes_into_active_task_artifacts(
        self, tmp_path: Path,
    ) -> None:
        """shell 写到 active task 的 ``tasks/<id>/artifacts/<name>`` 同样回填 pending
        —— 跟 share/ 一条路径，bind 时传 task_id 即可。"""
        from chahua.tasks_store import TasksStore as _TS
        from chahua.transport_bridge import ChahuaTransport

        share = tmp_path / "share"
        share.mkdir()
        store = _TS(room_dir=tmp_path)
        task = store.open_task(title="T", goal="g")
        registry = MessageArtifactRegistry(store_path=tmp_path / "m.jsonl")
        transport = ChahuaTransport(
            room_id="r", guest_name="A",
            message_artifacts=registry,
            share_dir=share,
            tasks_store=store,
        )
        sink = _TransportSink()
        with transport.bind(
            sink=sink, turn_id="t1", message_id="msg_g", task_id=task.id,
        ):
            _fire_tool_start(transport, tool="run_shell_command", call_id="c9")
            # 通过 store 直接落盘模拟 shell 命令产物。
            store.write_artifact(task.id, name="result.bin", content="hello")
            _fire_tool_complete(
                transport, tool="run_shell_command", call_id="c9",
            )
        rel = f"tasks/{task.id}/artifacts/result.bin"
        assert registry.pending.get(rel) == "msg_g"


# ── P10.4 code review §1/§2/§5/§6/§7/§8：fix-regression tests ────────────


class TestMarkShareSeenRelValidation:
    """code review §2 修：mark_share_seen 拒非 share/ rel，避免污染 baseline。"""

    def test_non_share_rel_rejected(self, tmp_path: Path) -> None:
        det, _, _ = _make_detector(tmp_path)
        before = dict(det._share_stamps)  # noqa: SLF001
        det.mark_share_seen("tasks/x/artifacts/foo.md")
        det.mark_share_seen("garbage")
        det.mark_share_seen("")
        # 三个非 share/ rel 都不该进 baseline。
        assert det._share_stamps == before  # noqa: SLF001

    def test_share_rel_still_accepted(self, tmp_path: Path) -> None:
        det, _, share = _make_detector(tmp_path)
        (share / "ok.png").write_bytes(b"png")
        det.mark_share_seen("share/ok.png")
        assert "share/ok.png" in det._share_stamps  # noqa: SLF001


class TestMessageArtifactsRelValidation:
    """code review §7 修：_load_from_disk 拒篡改 rel（traversal / 异形）。"""

    def test_load_skips_traversal_rel(self, tmp_path: Path) -> None:
        p = tmp_path / "m.jsonl"
        p.write_text(
            json.dumps({
                "message_id": "m1", "rel": "../../etc/passwd",
                "name": "passwd", "size": 0,
            }) + "\n"
            + json.dumps({
                "message_id": "m2", "rel": "share/legit.png",
                "name": "legit.png", "size": 4,
            }) + "\n",
            encoding="utf-8",
        )
        r = MessageArtifactRegistry(store_path=p)
        # traversal 那行被跳，合法行还在。
        assert r.by_message.get("m1") is None
        assert r.by_message.get("m2") is not None

    def test_load_skips_share_traversal_rel(self, tmp_path: Path) -> None:
        """``share/../../foo`` 经 ``room_dir / r.rel`` 可解析到 share 外，
        必须在加载时拒掉（与 ``_normalize_share_rel`` 同口径）。"""
        p = tmp_path / "m.jsonl"
        p.write_text(
            json.dumps({
                "message_id": "m1", "rel": "share/../../some-file",
                "name": "some-file", "size": 0,
            }) + "\n"
            + json.dumps({
                "message_id": "m2", "rel": "share/legit.png",
                "name": "legit.png", "size": 4,
            }) + "\n",
            encoding="utf-8",
        )
        r = MessageArtifactRegistry(store_path=p)
        assert r.by_message.get("m1") is None
        assert r.by_message.get("m2") is not None

    def test_load_skips_share_empty_segment(self, tmp_path: Path) -> None:
        """``share//foo`` 空段也拒。"""
        p = tmp_path / "m.jsonl"
        p.write_text(
            json.dumps({
                "message_id": "m1", "rel": "share//foo",
                "name": "foo", "size": 0,
            }) + "\n",
            encoding="utf-8",
        )
        r = MessageArtifactRegistry(store_path=p)
        assert r.by_message == {}

    def test_load_skips_unknown_root(self, tmp_path: Path) -> None:
        p = tmp_path / "m.jsonl"
        p.write_text(
            json.dumps({
                "message_id": "m1", "rel": "weird/foo", "name": "foo", "size": 0,
            }) + "\n",
            encoding="utf-8",
        )
        r = MessageArtifactRegistry(store_path=p)
        assert r.by_message == {}

    def test_load_skips_tasks_without_artifacts_segment(
        self, tmp_path: Path,
    ) -> None:
        p = tmp_path / "m.jsonl"
        p.write_text(
            json.dumps({
                "message_id": "m1", "rel": "tasks/x/foo", "name": "foo", "size": 0,
            }) + "\n",
            encoding="utf-8",
        )
        r = MessageArtifactRegistry(store_path=p)
        assert r.by_message == {}


class TestNonActiveTaskPendingGC:
    """code review §5 修：detect 出口扫非活 task 的 pending stale 条目。"""

    def test_closed_task_pending_purged(self, tmp_path: Path) -> None:
        from chahua.tasks_store import TasksStore as _TS
        room = Room(name="r")
        store = _TS(room_dir=tmp_path)
        task_a = store.open_task(title="A", goal="g")
        # 切到 B（A 仍 open 但不活跃）。
        task_b = store.open_task(title="B", goal="g")
        # A 关掉。
        store.close_task(task_a.id, status="done")
        registry = MessageArtifactRegistry(store_path=tmp_path / "m.jsonl")
        # A 的残留 pending（来自 close 前的 task_write_artifact）。
        rel_a = f"tasks/{task_a.id}/artifacts/foo.md"
        registry.record_pending(rel=rel_a, name="foo.md", message_id="m_old")
        det = ArtifactDetector(
            room=room, tasks_store=store,
            share_dir=tmp_path / "share", message_artifacts=registry,
        )
        det.detect(_Sink(), active_task_id=task_b.id)
        # A 已 closed → A 的 pending stale → 应被 sweep。
        assert rel_a not in registry.pending

    def test_missing_task_pending_purged(self, tmp_path: Path) -> None:
        from chahua.tasks_store import TasksStore as _TS
        room = Room(name="r")
        store = _TS(room_dir=tmp_path)
        task_b = store.open_task(title="B", goal="g")
        registry = MessageArtifactRegistry(store_path=tmp_path / "m.jsonl")
        # 指向一个根本不存在的 task_id。
        rel_ghost = "tasks/task_ghost/artifacts/x.md"
        registry.record_pending(rel=rel_ghost, name="x.md", message_id="m_g")
        det = ArtifactDetector(
            room=room, tasks_store=store,
            share_dir=tmp_path / "share", message_artifacts=registry,
        )
        det.detect(_Sink(), active_task_id=task_b.id)
        assert rel_ghost not in registry.pending

    def test_open_non_active_task_pending_kept(self, tmp_path: Path) -> None:
        """非 active 但仍 open 的 task 的 pending 不该被 sweep —— 用户可能切回。"""
        from chahua.tasks_store import TasksStore as _TS
        room = Room(name="r")
        store = _TS(room_dir=tmp_path)
        task_a = store.open_task(title="A", goal="g")
        task_b = store.open_task(title="B", goal="g")  # active 切到 B
        registry = MessageArtifactRegistry(store_path=tmp_path / "m.jsonl")
        rel_a = f"tasks/{task_a.id}/artifacts/foo.md"
        registry.record_pending(rel=rel_a, name="foo.md", message_id="m_a")
        det = ArtifactDetector(
            room=room, tasks_store=store,
            share_dir=tmp_path / "share", message_artifacts=registry,
        )
        det.detect(_Sink(), active_task_id=task_b.id)
        # A 仍 open → 留着，等用户切回。
        assert rel_a in registry.pending


class TestToolDiffFailureRollback:
    """code review §8 修：失败 / 取消 TOOL_COMPLETE 时 rollback 预落的 pending。"""

    def test_task_write_artifact_failure_rolls_back_new_rel(
        self, tmp_path: Path,
    ) -> None:
        """task_write_artifact 失败时（status != "ok"）预落的全新 rel pending 被回滚。"""
        from chahua.tasks_store import TasksStore as _TS
        from chahua.transport_bridge import ChahuaTransport
        from agentao.transport import AgentEvent, EventType
        share = tmp_path / "share"
        share.mkdir()
        store = _TS(room_dir=tmp_path)
        task = store.open_task(title="T", goal="g")
        registry = MessageArtifactRegistry(store_path=tmp_path / "m.jsonl")
        transport = ChahuaTransport(
            room_id="r", guest_name="A",
            message_artifacts=registry,
            share_dir=share, tasks_store=store,
        )
        sink = _TransportSink()
        with transport.bind(
            sink=sink, turn_id="t1", message_id="msg_fail", task_id=task.id,
        ):
            # 模拟 agentao 派发 TOOL_START（task_write_artifact "fresh.md"）。
            transport._handle(AgentEvent(  # noqa: SLF001
                type=EventType.TOOL_START,
                data={
                    "tool": "task_write_artifact",
                    "args": {"name": "fresh.md", "content": "v1"},
                    "call_id": "c_fail",
                },
            ))
            rel = f"tasks/{task.id}/artifacts/fresh.md"
            # TOOL_START 落了 pending。
            assert registry.pending.get(rel) == "msg_fail"
            # 写盘失败：status != "ok"。
            transport._handle(AgentEvent(  # noqa: SLF001
                type=EventType.TOOL_COMPLETE,
                data={
                    "tool": "task_write_artifact", "call_id": "c_fail",
                    "status": "error", "error": "boom",
                },
            ))
        # 关键不变量：失败时 pending 被回滚，避免被未来 detect 的 new_names 误绑。
        assert rel not in registry.pending

    def test_write_file_failure_rolls_back_share_pending(
        self, tmp_path: Path,
    ) -> None:
        """write_file 失败时预落的 share/ pending 同样被回滚。"""
        from chahua.transport_bridge import ChahuaTransport
        from agentao.transport import AgentEvent, EventType
        share = tmp_path / "share"
        share.mkdir()
        registry = MessageArtifactRegistry(store_path=tmp_path / "m.jsonl")
        transport = ChahuaTransport(
            room_id="r", guest_name="A",
            message_artifacts=registry, share_dir=share,
        )
        with transport.bind(
            sink=_TransportSink(), turn_id="t1", message_id="msg_fail2",
        ):
            transport._handle(AgentEvent(  # noqa: SLF001
                type=EventType.TOOL_START,
                data={
                    "tool": "write_file",
                    "args": {"file_path": "./share/new.png", "content": "x"},
                    "call_id": "c_fail2",
                },
            ))
            assert registry.pending.get("share/new.png") == "msg_fail2"
            transport._handle(AgentEvent(  # noqa: SLF001
                type=EventType.TOOL_COMPLETE,
                data={
                    "tool": "write_file", "call_id": "c_fail2",
                    "status": "error", "error": "perm denied",
                },
            ))
        assert "share/new.png" not in registry.pending

    def test_ok_status_keeps_pending(self, tmp_path: Path) -> None:
        """成功 TOOL_COMPLETE 不该 rollback pending —— 由 detector 正常 consume。"""
        from chahua.transport_bridge import ChahuaTransport
        from agentao.transport import AgentEvent, EventType
        share = tmp_path / "share"
        share.mkdir()
        registry = MessageArtifactRegistry(store_path=tmp_path / "m.jsonl")
        transport = ChahuaTransport(
            room_id="r", guest_name="A",
            message_artifacts=registry, share_dir=share,
        )
        with transport.bind(
            sink=_TransportSink(), turn_id="t1", message_id="msg_ok",
        ):
            transport._handle(AgentEvent(  # noqa: SLF001
                type=EventType.TOOL_START,
                data={
                    "tool": "write_file",
                    "args": {"file_path": "./share/ok.png", "content": "x"},
                    "call_id": "c_ok",
                },
            ))
            transport._handle(AgentEvent(  # noqa: SLF001
                type=EventType.TOOL_COMPLETE,
                data={"tool": "write_file", "call_id": "c_ok", "status": "ok"},
            ))
        # 成功路径：pending 仍在，等 detector consume。
        assert registry.pending.get("share/ok.png") == "msg_ok"

    def test_rollback_skips_when_overwritten_by_other_mid(
        self, tmp_path: Path,
    ) -> None:
        """rollback 严格 mid 校验：race 中被新 mid 覆盖了，rollback 不该误删。"""
        from chahua.transport_bridge import ChahuaTransport
        from agentao.transport import AgentEvent, EventType
        share = tmp_path / "share"
        share.mkdir()
        registry = MessageArtifactRegistry(store_path=tmp_path / "m.jsonl")
        transport = ChahuaTransport(
            room_id="r", guest_name="A",
            message_artifacts=registry, share_dir=share,
        )
        with transport.bind(
            sink=_TransportSink(), turn_id="t1", message_id="msg_old",
        ):
            transport._handle(AgentEvent(  # noqa: SLF001
                type=EventType.TOOL_START,
                data={
                    "tool": "write_file",
                    "args": {"file_path": "./share/race.png", "content": "x"},
                    "call_id": "c_race",
                },
            ))
            # 模拟另一条消息抢先覆写了 pending。
            registry.record_pending(
                rel="share/race.png", name="race.png", message_id="msg_new",
            )
            # 老 msg_old 的工具失败 → 试图 rollback。
            transport._handle(AgentEvent(  # noqa: SLF001
                type=EventType.TOOL_COMPLETE,
                data={
                    "tool": "write_file", "call_id": "c_race",
                    "status": "error",
                },
            ))
        # 严格 mid 校验：pending 是 msg_new，不是 msg_old → 不动。
        assert registry.pending.get("share/race.png") == "msg_new"


class TestRollingBaselinePerf:
    """code review §1 修：单次 bind 内多个 shell/MCP 调用复用 baseline，
    perf 上把"每个 call 两次扫盘"压成"首次 + 每个 COMPLETE 一次"。
    """

    def test_baseline_reused_across_calls(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        from chahua import transport_bridge as tb
        from chahua.transport_bridge import ChahuaTransport
        from agentao.transport import AgentEvent, EventType
        share = tmp_path / "share"
        share.mkdir()
        (share / "baseline.txt").write_text("v1")
        registry = MessageArtifactRegistry(store_path=tmp_path / "m.jsonl")
        transport = ChahuaTransport(
            room_id="r", guest_name="A",
            message_artifacts=registry, share_dir=share,
        )
        scan_count = {"n": 0}
        orig = tb._list_share_rels
        def counting(*a, **kw):
            scan_count["n"] += 1
            return orig(*a, **kw)
        monkeypatch.setattr(tb, "_list_share_rels", counting)
        with transport.bind(
            sink=_TransportSink(), turn_id="t1", message_id="msg_p",
        ):
            for i in range(3):
                cid = f"c{i}"
                transport._handle(AgentEvent(  # noqa: SLF001
                    type=EventType.TOOL_START,
                    data={"tool": "run_shell_command", "args": {}, "call_id": cid},
                ))
                transport._handle(AgentEvent(  # noqa: SLF001
                    type=EventType.TOOL_COMPLETE,
                    data={"tool": "run_shell_command", "call_id": cid, "status": "ok"},
                ))
        # 3 个 shell call。老路径 = 6 次扫；新路径 = 1（首次 START） + 3（每次 COMPLETE）
        # = 4 次。严格上限：≤ calls + 1，把 perf bug 锁定。
        assert scan_count["n"] <= 4

    def test_baseline_cleared_on_bind_exit(self, tmp_path: Path) -> None:
        """bind 退出后下一次 bind 应该重新扫盘 baseline —— 跨 speak 不共享。"""
        from chahua.transport_bridge import ChahuaTransport
        from agentao.transport import AgentEvent, EventType
        share = tmp_path / "share"
        share.mkdir()
        registry = MessageArtifactRegistry(store_path=tmp_path / "m.jsonl")
        transport = ChahuaTransport(
            room_id="r", guest_name="A",
            message_artifacts=registry, share_dir=share,
        )
        with transport.bind(
            sink=_TransportSink(), turn_id="t1", message_id="m1",
        ):
            transport._handle(AgentEvent(  # noqa: SLF001
                type=EventType.TOOL_START,
                data={"tool": "run_shell_command", "args": {}, "call_id": "c1"},
            ))
            assert transport._diff_baseline is not None  # noqa: SLF001
        # bind 退出 → baseline 清空，下次 bind 必重扫。
        assert transport._diff_baseline is None  # noqa: SLF001


class TestLateToolCompleteAttribution:
    """code review §6 修：snapshot entry 捕获 message_id / task_id，
    晚到的 TOOL_COMPLETE 即便 bind 已退出仍能正确归属。
    """

    def test_complete_after_bind_exit_still_records_pending(
        self, tmp_path: Path,
    ) -> None:
        from chahua.transport_bridge import ChahuaTransport
        from agentao.transport import AgentEvent, EventType
        share = tmp_path / "share"
        share.mkdir()
        registry = MessageArtifactRegistry(store_path=tmp_path / "m.jsonl")
        transport = ChahuaTransport(
            room_id="r", guest_name="A",
            message_artifacts=registry, share_dir=share,
        )
        # 模拟病态时序：在 bind 期 START，bind 退出后才到 COMPLETE。
        with transport.bind(
            sink=_TransportSink(), turn_id="t1", message_id="msg_late",
        ):
            transport._handle(AgentEvent(  # noqa: SLF001
                type=EventType.TOOL_START,
                data={"tool": "run_shell_command", "args": {}, "call_id": "c_late"},
            ))
            # bind 期间真创建了新文件。
            (share / "late.bin").write_bytes(b"x")
        # bind 已退出 —— self._message_id 是 None。常理"丢归属"会让 pending 没被 record。
        # P10.4 修：snapshot 捕获了 mid，bind 外仍能录。
        transport._handle(AgentEvent(  # noqa: SLF001
            type=EventType.TOOL_COMPLETE,
            data={"tool": "run_shell_command", "call_id": "c_late", "status": "ok"},
        ))
        assert registry.pending.get("share/late.bin") == "msg_late"

    def test_late_complete_does_not_poison_next_bind_baseline(
        self, tmp_path: Path,
    ) -> None:
        """code review §1.2 修：晚到 TOOL_COMPLETE（bind 已退）不能复活
        ``_diff_baseline``。否则下次 bind 的首个 shell/MCP TOOL_START 会跳过
        重扫盘，把两次 bind 之间凭空出现的文件误归到下条消息。"""
        from chahua.transport_bridge import ChahuaTransport
        from agentao.transport import AgentEvent, EventType
        share = tmp_path / "share"
        share.mkdir()
        registry = MessageArtifactRegistry(store_path=tmp_path / "m.jsonl")
        transport = ChahuaTransport(
            room_id="r", guest_name="A",
            message_artifacts=registry, share_dir=share,
        )
        # 第一次 bind：跑一个 shell 调用，bind 期内 START、bind 外才 COMPLETE。
        with transport.bind(
            sink=_TransportSink(), turn_id="t1", message_id="msg_1",
        ):
            transport._handle(AgentEvent(  # noqa: SLF001
                type=EventType.TOOL_START,
                data={"tool": "run_shell_command", "args": {}, "call_id": "c1"},
            ))
        # bind 已退出。COMPLETE 晚到 —— 不应回填 baseline。
        transport._handle(AgentEvent(  # noqa: SLF001
            type=EventType.TOOL_COMPLETE,
            data={"tool": "run_shell_command", "call_id": "c1", "status": "ok"},
        ))
        assert transport._diff_baseline is None  # noqa: SLF001
        # 两次 bind 之间用户通过其它途径上传了一个文件。
        (share / "between.bin").write_bytes(b"y")
        # 第二次 bind：首个 shell TOOL_START 必须重扫盘、把 between.bin 纳入 before，
        # 这样它不会被算成下条消息的"新增"。
        with transport.bind(
            sink=_TransportSink(), turn_id="t2", message_id="msg_2",
        ):
            transport._handle(AgentEvent(  # noqa: SLF001
                type=EventType.TOOL_START,
                data={"tool": "run_shell_command", "args": {}, "call_id": "c2"},
            ))
            transport._handle(AgentEvent(  # noqa: SLF001
                type=EventType.TOOL_COMPLETE,
                data={"tool": "run_shell_command", "call_id": "c2", "status": "ok"},
            ))
        # between.bin 应**不**被错挂到 msg_2 —— 它在 msg_2 的 TOOL_START 之前就在盘上。
        assert "share/between.bin" not in registry.pending


# ── Orchestrator.mark_share_seen ──────────────────────────────────────────


class TestOrchestratorMarkShareSeen:
    def test_public_accessor_works(self, tmp_path: Path) -> None:
        room = Room(name="t")
        room.add_participant(USER_SPEAKER_ID)
        share = tmp_path / "share"
        share.mkdir()
        orch = Orchestrator(
            room=room,
            user_config=UserConfig(display_name="老金", full_md=None, source=None),
            scorer=NoopScorer(),
            summarizer=NoopSummarizer(),
            cursor=GuestCursor(),
            config=OrchestratorConfig(summary_block_size=999),
            share_dir=share,
        )
        orch.mark_share_seen("share/user-upload.png")
        assert "share/user-upload.png" in orch._artifact_detector.share_seen
