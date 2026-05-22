"""P9 阶段 9.2.2c：``_switch_room`` 两阶段换房 + 后台 runtime 自毁回归。

覆盖（docs/P9 §3 / §5）：

- 切走 idle 旧前台 → close + 移出注册表。
- 切走 busy 旧前台 → 转后台续跑（router=background、留注册表、记时间戳、不 cancel）。
- 切到注册表里已有的后台 runtime → 复用、不重建。
- 切到不在注册表的房间 → build_room_session 装配新 runtime。
- 切房失败原子性：目标目录不存在 / build 抛错 → 旧前台 / 前台指针一字不动。
- 同房 noop。
- 后台 runtime turn 跑完自毁；前台 / 仍有活（handoff 队列 / MTS）的不自毁。
- 切回竞态：mode 先翻 foreground，自毁判定随即不成立。
- ``_aclose_background_runtimes``：ws 断开清后台、留前台。
"""

from __future__ import annotations

import asyncio

import chahua.server as server_mod
from chahua.events import ChahuaEventType, NOOP_SINK
from chahua.room_runtime import (
    ROUTER_MODE_BACKGROUND,
    ROUTER_MODE_FOREGROUND,
    RoomEventRouter,
    RoomRuntime,
)
from chahua.server import ChahuaServer


# ── fakes ──────────────────────────────────────────────────────────────────


class _FakeTask:
    """够用的假 in-flight task：``inflight_alive`` 只读 ``.done()``。"""

    def __init__(self) -> None:
        self._done = False

    def done(self) -> bool:
        return self._done

    def cancel(self) -> None:
        self._done = True


class _FakeOrch:
    def __init__(self) -> None:
        self.has_pending_handoff = False
        self.managed_session: object | None = None


class _FakeGuest:
    def __init__(self, isolation: str) -> None:
        self.isolation = isolation


class _FakeSession:
    def __init__(
        self, name: str, *, guests: tuple = (), room_name: str | None = None,
    ) -> None:
        # ``name`` 是房间目录名（注册表 key）；``room.name`` 默认同名，可经
        # ``room_name`` 与目录名拉开 —— envelope 顶层 room_id 走 ``room.name``。
        self.room_config = type(
            "_RC", (), {
                "room_dir": type("_D", (), {"name": name})(),
                "guests": list(guests),
            },
        )()
        self.room = type("_Room", (), {"name": room_name or name})()
        self.orchestrator = _FakeOrch()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _runtime(
    room_id: str, *, mode: str = ROUTER_MODE_FOREGROUND, guests: tuple = (),
    room_name: str | None = None,
) -> RoomRuntime:
    return RoomRuntime(
        room_id=room_id,
        session=_FakeSession(  # type: ignore[arg-type]
            room_id, guests=guests, room_name=room_name,
        ),
        router=RoomEventRouter(NOOP_SINK, mode=mode),
    )


def _make_busy(runtime: RoomRuntime) -> _FakeTask:
    """挂一个未完成的假 task，让 ``inflight_alive()`` 返 True。"""
    task = _FakeTask()
    runtime.set_inflight(task, "user")  # type: ignore[arg-type]
    return task


def _server(tmp_path, *runtimes: RoomRuntime) -> ChahuaServer:
    srv = object.__new__(ChahuaServer)
    srv._paths = type("_P", (), {"user_data_root": tmp_path})()
    srv._runtimes = {rt.room_id: rt for rt in runtimes}
    srv._foreground_id = runtimes[0].room_id
    srv._snapshots: list[object] = []
    srv._room_infos: list[object] = []
    srv._emit_room_snapshot = lambda sink: srv._snapshots.append(sink)  # type: ignore[method-assign]
    srv._emit_room_info = lambda sink: srv._room_infos.append(sink)  # type: ignore[method-assign]
    return srv


def _sink(env: object) -> None:
    pass


# ── 切走 idle / busy 旧前台 ─────────────────────────────────────────────────


def test_switch_away_idle_foreground_closes_old(tmp_path) -> None:
    """旧前台 idle → close + 移出注册表（后台 runtime 仅在有活时存在）。"""
    fg, target = _runtime("A"), _runtime("B", mode=ROUTER_MODE_BACKGROUND)
    srv = _server(tmp_path, fg, target)

    srv._switch_room("B", _sink)

    assert fg.session.closed
    assert "A" not in srv._runtimes
    assert srv._foreground_id == "B"
    assert target.router.mode == ROUTER_MODE_FOREGROUND
    assert len(srv._snapshots) == 1


def test_switch_away_busy_foreground_goes_background(tmp_path) -> None:
    """旧前台 busy → 转后台续跑：留注册表、router=background、记时间戳、不 cancel。"""
    fg, target = _runtime("A"), _runtime("B", mode=ROUTER_MODE_BACKGROUND)
    srv = _server(tmp_path, fg, target)
    task = _make_busy(fg)

    srv._switch_room("B", _sink)

    assert "A" in srv._runtimes  # 留在注册表续跑
    assert fg.router.mode == ROUTER_MODE_BACKGROUND
    assert fg.background_since_ms is not None
    assert not fg.session.closed  # 不 close
    assert not task.done()  # 不 cancel —— 后台续跑
    assert srv._foreground_id == "B"


# ── 复用 / 新建目标 runtime ─────────────────────────────────────────────────


def test_switch_reuses_background_runtime(tmp_path) -> None:
    """切到注册表里已有的（之前切走留下的）后台 runtime → 复用、不重建。"""
    fg = _runtime("A")
    bg = _runtime("B", mode=ROUTER_MODE_BACKGROUND)
    bg.background_since_ms = 12345  # 之前转后台时记的时间戳
    srv = _server(tmp_path, fg, bg)

    srv._switch_room("B", _sink)

    assert srv._runtimes["B"] is bg  # 同一对象，没被重建
    assert bg.router.mode == ROUTER_MODE_FOREGROUND
    # 切回前台 → background_since_ms 清回 None（前台房恒 None 的不变量）。
    assert bg.background_since_ms is None


def test_switch_builds_new_runtime(tmp_path, monkeypatch) -> None:
    """目标不在注册表 → build_room_session 装配新 runtime 后切过去。"""
    (tmp_path / "rooms" / "B").mkdir(parents=True)
    fg = _runtime("A")
    srv = _server(tmp_path, fg)
    new_session = _FakeSession("B")

    monkeypatch.setattr(
        server_mod, "build_room_session", lambda room_dir, *, paths: new_session,
    )
    srv._switch_room("B", _sink)

    assert srv._foreground_id == "B"
    assert srv._runtimes["B"].session is new_session
    assert srv._runtimes["B"].router.mode == ROUTER_MODE_FOREGROUND
    assert fg.session.closed  # 旧前台 idle → close
    assert len(srv._snapshots) == 1


# ── 切房失败原子性 ──────────────────────────────────────────────────────────


def test_switch_atomicity_missing_dir(tmp_path) -> None:
    """目标房目录不存在 → 旧前台 / 前台指针一字不动，只重发 room_info。"""
    fg = _runtime("A")
    srv = _server(tmp_path, fg)

    srv._switch_room("ghost", _sink)

    assert srv._foreground_id == "A"
    assert srv._runtimes == {"A": fg}
    assert not fg.session.closed
    assert len(srv._room_infos) == 1
    assert srv._snapshots == []


def test_switch_atomicity_build_fails(tmp_path, monkeypatch) -> None:
    """目标目录在但 build_room_session 抛错 → 旧前台不动，切房未发生。"""
    (tmp_path / "rooms" / "B").mkdir(parents=True)
    fg = _runtime("A")
    srv = _server(tmp_path, fg)

    def _boom(room_dir, *, paths):
        raise RuntimeError("build failed")

    monkeypatch.setattr(server_mod, "build_room_session", _boom)
    srv._switch_room("B", _sink)

    assert srv._foreground_id == "A"
    assert srv._runtimes == {"A": fg}
    assert not fg.session.closed
    assert len(srv._room_infos) == 1
    assert srv._snapshots == []


def test_switch_same_room_noop(tmp_path) -> None:
    """切到当前前台 → noop，不重建不发快照。"""
    fg = _runtime("A")
    srv = _server(tmp_path, fg)

    srv._switch_room("A", _sink)

    assert srv._runtimes == {"A": fg}
    assert srv._snapshots == []
    assert srv._room_infos == []


# ── 后台 runtime 自毁 ───────────────────────────────────────────────────────


def test_self_destruct_background_idle(tmp_path) -> None:
    """后台 runtime idle（无 in-flight / handoff / MTS）→ close + 移出注册表。"""
    fg = _runtime("A")
    bg = _runtime("B", mode=ROUTER_MODE_BACKGROUND)
    srv = _server(tmp_path, fg, bg)

    srv._maybe_self_destruct_background_runtime(bg)

    assert bg.session.closed
    assert "B" not in srv._runtimes


def test_self_destruct_emits_room_background_finished_with_room_name(tmp_path) -> None:
    """room_background_finished 的 envelope room_id 必是 room.name，不是目录名。

    所有里程碑事件（turn_* / message_end / managed_session_*）envelope 顶层 room_id
    都用 room.name；room_background_finished 若用 runtime.room_id（目录名），目录名
    ≠ room.name 时前端 backgroundActiveRooms 的 delete 会落空、徽标永不清除。
    """
    captured: list = []
    fg = _runtime("A")
    # 目录名 "p3-bg"、room.name "黄河路" —— 故意拉开（shipped 房间的常态）。
    bg = _runtime("p3-bg", mode=ROUTER_MODE_BACKGROUND, room_name="黄河路")
    bg.router.ws_sink = captured.append
    srv = _server(tmp_path, fg, bg)

    srv._maybe_self_destruct_background_runtime(bg)

    assert len(captured) == 1
    env = captured[0]
    assert env.type is ChahuaEventType.ROOM_BACKGROUND_FINISHED
    # room.name，不是注册表 key / 目录名 "p3-bg"。
    assert env.room_id == "黄河路"


def test_no_self_destruct_foreground(tmp_path) -> None:
    """前台 runtime（mode=foreground）一律不自毁。"""
    fg = _runtime("A")
    srv = _server(tmp_path, fg)

    srv._maybe_self_destruct_background_runtime(fg)

    assert not fg.session.closed
    assert "A" in srv._runtimes


def test_no_self_destruct_when_inflight_alive(tmp_path) -> None:
    """后台 runtime 的 in-flight turn / drain 还在跑 → 不自毁，留着续跑。"""
    fg = _runtime("A")
    bg = _runtime("B", mode=ROUTER_MODE_BACKGROUND)
    _make_busy(bg)  # 后台 turn / handoff drain 仍在跑
    srv = _server(tmp_path, fg, bg)

    srv._maybe_self_destruct_background_runtime(bg)

    assert not bg.session.closed
    assert "B" in srv._runtimes


def test_self_destruct_reclaims_cap_stalled_queue(tmp_path) -> None:
    """后台 runtime in-flight 槽空但 _handoff_queue 还有 cap 撞顶残留项 → 仍自毁。

    后台房间无 inbound 驱动，残留项无人 drain；若因「队列非空」不回收会泄漏到
    ws 断开。残留项是瞬态，丢弃可接受（code-review 跟进的 cap-stall 泄漏修复）。
    """
    fg = _runtime("A")
    bg = _runtime("B", mode=ROUTER_MODE_BACKGROUND)
    bg.session.orchestrator.has_pending_handoff = True  # cap 撞顶残留
    srv = _server(tmp_path, fg, bg)

    srv._maybe_self_destruct_background_runtime(bg)

    assert bg.session.closed
    assert "B" not in srv._runtimes


def test_switch_back_race_no_destroy(tmp_path) -> None:
    """切回竞态：runtime 切回（mode 翻 foreground）后，turn 收尾的自毁判定不成立。"""
    fg = _runtime("A")
    bg = _runtime("B", mode=ROUTER_MODE_BACKGROUND)
    srv = _server(tmp_path, fg, bg)

    # 模拟 §5.2：用户在后台 turn 收尾瞬间切回 B —— _switch_room 先翻 mode。
    srv._switch_room("B", _sink)
    assert bg.router.mode == ROUTER_MODE_FOREGROUND

    # 后台 turn 的 finally 此刻才跑到自毁判定 —— mode 已是 foreground，不回收。
    srv._maybe_self_destruct_background_runtime(bg)

    assert not bg.session.closed
    assert "B" in srv._runtimes


# ── global-isolation 茶客：不后台续跑 ───────────────────────────────────────


def test_foreground_global_guest_detected(tmp_path) -> None:
    """前台房含 isolation=global 茶客 → _foreground_session_has_global_guest True。"""
    fg = _runtime("A", guests=(_FakeGuest("room"), _FakeGuest("global")))
    srv = _server(tmp_path, fg)

    assert srv._foreground_session_has_global_guest() is True


def test_foreground_all_room_isolated(tmp_path) -> None:
    """前台房茶客全 room-isolated → False（可后台续跑）。"""
    fg = _runtime("A", guests=(_FakeGuest("room"), _FakeGuest("room")))
    srv = _server(tmp_path, fg)

    assert srv._foreground_session_has_global_guest() is False


async def test_switch_away_global_guest_drains_then_closes(tmp_path) -> None:
    """前台房有 global 茶客且 busy → _inbound_switch_room 先 cancel+drain，
    _switch_room 据此把它当 idle 关掉（不转后台 —— 否则 share/task 软链撞车）。"""
    fg = _runtime("A", guests=(_FakeGuest("global"),))
    bg = _runtime("B", mode=ROUTER_MODE_BACKGROUND)
    srv = _server(tmp_path, fg, bg)
    task = asyncio.create_task(asyncio.sleep(3600))
    fg.set_inflight(task, "user")

    await srv._inbound_switch_room({"type": "switch_room", "room_id": "B"}, _sink)

    assert task.cancelled()  # global 茶客房不后台续跑 —— in-flight turn 被 drain
    assert fg.session.closed  # 切走即 close
    assert "A" not in srv._runtimes
    assert srv._foreground_id == "B"


# ── _delete_room：拒删后台续跑中的房间 ──────────────────────────────────────


def test_delete_room_rejects_background_runtime(tmp_path, monkeypatch) -> None:
    """切走后仍在后台续跑的房间不能删 —— rmtree 会抽走后台 turn 正在写的文件。"""
    from chahua.server_inbound_admin import AdminHandlers, admin

    fg = _runtime("A")
    bg = _runtime("B", mode=ROUTER_MODE_BACKGROUND)
    srv = _server(tmp_path, fg, bg)
    notices: list[tuple[str, str]] = []
    srv._emit_notice = lambda sink, *, level, text: notices.append((level, text))  # type: ignore[method-assign]
    deleted: list[str] = []
    monkeypatch.setattr(
        admin, "delete_room",
        lambda **kw: deleted.append(kw["room_id"]),
    )

    AdminHandlers(srv)._delete_room(room_id="B", sink=_sink)

    assert notices and notices[0][0] == "error"  # 拒绝 + error notice
    assert "B" in srv._runtimes  # 后台 runtime 没被动
    assert deleted == []  # admin.delete_room（rmtree）没被调到 —— 守卫在它之前拦下


# ── _aclose_background_runtimes（ws 断开清后台、留前台）──────────────────────


async def test_aclose_background_keeps_foreground(tmp_path) -> None:
    """ws 断开：后台 runtime 全清，前台 runtime 保留供重连。"""
    fg = _runtime("A")
    bg1 = _runtime("B", mode=ROUTER_MODE_BACKGROUND)
    bg2 = _runtime("C", mode=ROUTER_MODE_BACKGROUND)
    srv = _server(tmp_path, fg, bg1, bg2)

    await srv._aclose_background_runtimes()

    assert "A" in srv._runtimes and not fg.session.closed
    assert "B" not in srv._runtimes and bg1.session.closed
    assert "C" not in srv._runtimes and bg2.session.closed
