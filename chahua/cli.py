"""茶话室 CLI（P0 → P2.3）。

P0 范围（已落）：agentao 接线、人格注入、多 provider、流式输出、read-only 拦截
P1 新增：意愿打分、@ 路由、增量喂养、摘要、USER.md 偏好注入
P1 调优：top-1~2 抢话、want_threshold 0.45
P1.5 新增：``chahua --room <dir>``，房间配置走 ``room.toml``（``chahua.config``）；
逐茶客可独立配 ``permission``（默认 read-only，可选 workspace-write / full-access）。
P2.1 新增：transcript.jsonl / summary.jsonl / cursor.json 三件落盘 + 启动续聊。
P2.2 新增：事件走 :class:`chahua.events.ChahuaEnvelope` —— CLI 由
:class:`_CliRenderer` 消费 envelope。
P2.3：房间装配逻辑抽到 :mod:`chahua.session`，CLI 与 :mod:`chahua.server`
（``chahua-server`` 入口）共用同一套 bring-up；本文件保持纯 REPL。

不在 P2.3 范围：Electron（P3）、逐茶客 provider/model / isolation / [scoring]/
[summary] / transport（P4）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from ._paths import Paths, resolve_under
from .config import RoomConfigError
from .events import (
    STATUS_CANCELLED,
    STATUS_ERROR,
    STATUS_OK,
    ChahuaEnvelope,
    ChahuaEventType,
)
from .guest import TeaGuest
from .scoring import ScoreKind
from .session import (
    DEFAULT_ROOM_REL,
    RoomSession,
    build_room_session,
    load_env_files,
)

_log = logging.getLogger(__name__)


# ── REPL 渲染 ──────────────────────────────────────────────────────────────


def _print_banner(session: RoomSession, *, paths: Paths) -> None:
    user_config = session.user_config
    src = user_config.source
    # 显示相对 user_data_root 的形式，全绝对路径在 banner 里太长；房间在 user_data 外
    # 则用绝对路径。
    try:
        room_dir_display: Path = session.room_config.room_dir.relative_to(paths.user_data_root)
    except ValueError:
        room_dir_display = session.room_config.room_dir
    print("─" * 60)
    head = f"茶话室 · 房间：{session.room_config.name}  ({room_dir_display})"
    if session.room.latest_seq > 0:
        head += f"  · 续聊（已有 {session.room.latest_seq} 条记录）"
    print(head)
    print(
        f"你的身份：{user_config.display_name}"
        + (f"  ({src})" if src else "  (USER.md 未找到，回退 '用户')")
    )
    # 把权限挂在每位名字后面，让 workspace-write / full-access 一眼看到。
    parts = [
        f"{g.name}({g.permission})" if g.permission != "read-only" else g.name
        for g in session.guests
    ]
    # 全茶客共享一份 LLMClient（房间默认 spec）；P4.1 起每茶客可独立配 client，
    # 那时 banner 应改成"按 guest 列模型"。
    spec = session.room_default_spec
    print(
        f"在场茶客：{'、'.join(parts)}   "
        f"房间默认模型：{spec.provider}/{spec.model}"
    )
    print("输入回车发送；空行 / /quit 退出；/help 看全部命令；@<名字> 直接点茶客。")
    print("─" * 60)


def _print_permission_info(guests: list[TeaGuest]) -> None:
    for g in guests:
        eng = g.agent.permission_engine
        runner = g.agent.tool_runner
        print(
            f"  {g.name}.permission = {g.permission}  "
            f"(engine.mode={eng.active_mode.value}, "
            f"tool_runner.readonly={runner.readonly_mode})"
        )


def _print_guest_caps(session, name: str, *, view: str) -> None:
    """打印某位茶客 agent 的 tools 或可用 skills（``/tools`` / ``/skills`` 共用）。

    与 WebSocket 端 ``_inbound_list_guest_caps`` 共调 :meth:`TeaGuest.describe_capabilities`
    这一个投影 —— CLI 有直接 Python 访问，不走 inbound / envelope 往返。
    """
    guest = session.orchestrator.get_guest(name)
    if guest is None:
        print(f"[{view}：茶客 {name!r} 不在场]")
        return
    caps = guest.describe_capabilities()
    if view == "skills":
        skills = caps["skills"]
        print(f"{name} 可用 skills（{len(skills)}）：")
        for s in skills:
            desc = f" —— {s['description']}" if s["description"] else ""
            print(f"  {s['name']}{desc}")
        if not skills:
            print("  （无）")
        return
    tools = caps["tools"]
    print(f"{name} 注册的 tools（{len(tools)}，权限模式：{caps['permission']}）：")
    for t in tools:
        ro = "read-only" if t["is_read_only"] else "write"
        desc = f" —— {t['description'].splitlines()[0]}" if t["description"] else ""
        print(f"  {t['name']} ({ro}){desc}")


# Web 与 CLI 两面 help 各自列本面支持的命令（CLI 端含 ``/info`` / ``/quit`` REPL
# 专属；Web 端 ``HELP_TEXT`` 含 ``/task``）。加命令时改本面的这份即可。
_HELP_LINES: tuple[str, ...] = (
    "chahua 系统命令：",
    "  /help 或 /?         显示本帮助",
    "  /info               显示每位茶客的权限模式",
    "  /quit /exit :q      退出 REPL",
    "  /clear 或 /new      清空整间房间聊天（重置 transcript / 摘要 / 茶客会话窗口）",
    "  /clear task         清空当前任务的全部产物（仅删 artifacts/，任务本身保留）",
    "  /tools <名字>       查看某位茶客 agent 注册的工具",
    "  /skills <名字>      查看某位茶客可用的 skills",
    "  @<名字>             绕过打分直接点名一位茶客",
)


def _print_help() -> None:
    for line in _HELP_LINES:
        print(line)


_KIND_BADGE: dict[str, str] = {
    ScoreKind.MENTION.value: "@",
    ScoreKind.COOLDOWN.value: "冷却",
    ScoreKind.ERROR.value: "失败",
}


def _format_scores(scores: list[dict]) -> str:
    """把一轮打分明细排成 ``宝总=0.72, 玲子=@, 爷叔=冷却`` 这样。
    输入是 ``turn_start.data.scores``（已经走过 :func:`_score_to_dict` 序列化）。
    """
    parts: list[str] = []
    for r in scores:
        kind = r.get("kind", "")
        badge = _KIND_BADGE.get(kind)
        name = r.get("guest_name", "")
        score = r.get("score", 0.0)
        parts.append(f"{name}={badge}" if badge else f"{name}={score:.2f}")
    return ", ".join(parts)


class _CliRenderer:
    """把 envelope 流渲染成与 P1 视觉一致的打字流。

    每次 ``submit_user_message`` 复用一个实例 —— 实例只在 turn 边界保留少量状态
    （本轮打分明细，供 message_start 时回放）。本身不持任何 I/O 资源。
    """

    def __init__(self) -> None:
        self._current_scores: list[dict] = []

    def __call__(self, env: ChahuaEnvelope) -> None:
        t = env.type
        if t is ChahuaEventType.TURN_START:
            self._current_scores = env.data.get("scores", [])
            return
        if t is ChahuaEventType.MESSAGE_START:
            scores = _format_scores(self._current_scores)
            print(f"\n[选中 {env.guest_name}  打分：{scores}]")
            print(f"{env.guest_name}: ", end="", flush=True)
            return
        if t is ChahuaEventType.MESSAGE_DELTA:
            chunk = env.data.get("chunk", "")
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            return
        if t is ChahuaEventType.MESSAGE_END:
            if env.status == STATUS_OK:
                print()  # 流式没主动换行，由这里收尾
            elif env.status == STATUS_CANCELLED:
                print("  [中断]")
            elif env.status == STATUS_ERROR:
                err = env.data.get("error") or "未知错误"
                print(f"  [出错：{err}]")
            return
        # TURN_END / GUEST_THINKING / TOOL_START / TOOL_COMPLETE：CLI 默认静默。
        # turn_end.next 字段（"ai"/"user"/"idle"）留给 UI 用；CLI 改在
        # submit_user_message 返回后按 transcript 增量判断"暂无人接话"。


# ── 命令行解析 ─────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="chahua",
        description="多 Agent 群聊「茶话室」交互式 CLI（P2.3）",
    )
    parser.add_argument(
        "--room",
        type=Path,
        default=DEFAULT_ROOM_REL,
        help=(
            f"房间目录，含 room.toml（默认 {DEFAULT_ROOM_REL}）。"
            f"相对路径相对 user_data_root（CHAHUA_USER_DATA 或 dev 仓库根），绝对路径原样。"
        ),
    )
    return parser.parse_args(argv)


# ── REPL ───────────────────────────────────────────────────────────────────


async def _repl(args: argparse.Namespace) -> int:
    paths = Paths.from_env()
    load_env_files(paths)

    room_dir = resolve_under(paths.user_data_root, args.room)
    try:
        session = build_room_session(room_dir, paths=paths)
    except RoomConfigError as e:
        print(f"房间配置错误：\n{e}", file=sys.stderr)
        return 2

    _print_banner(session, paths=paths)

    renderer = _CliRenderer()
    try:
        while True:
            try:
                raw = input(f"\n{session.user_config.display_name}> ")
            except EOFError:
                break
            text = raw.strip()
            if not text or text in ("/quit", "/exit", ":q"):
                break
            if text == "/info":
                _print_permission_info(session.guests)
                continue
            if text in ("/help", "/?"):
                _print_help()
                continue
            if text in ("/clear", "/new"):
                # 与 WebSocket 端 ``_clear_room`` 同口径（orchestrator.reset_room +
                # recorder.clear）；CLI 无 envelope sink 复位 UI，靠下一次 input 提示
                # 让用户知道房间已重新起步。
                session.orchestrator.reset_room()
                session.recorder.clear()
                print("[已清空房间记录 —— 下一句重新走 onboarding]")
                continue
            if text in ("/clear task", "/clear-task"):
                # 仅清当前 active 任务的 artifacts/。任务本身（决策 / 状态 / 摘要）保留，
                # 与 ``/clear``（清整间房间）作用域有意不同。
                store = session.tasks_store
                active_id = store.active_task_id if store is not None else None
                if active_id is None:
                    print("[/clear task：当前没有 active 任务]")
                    continue
                task = store.get_task(active_id)
                if task is None:
                    print(f"[/clear task：task_id={active_id!r} 已不存在]")
                    continue
                # CLI 即时输入语境无误触场景，不加 input("确认?") 拦截；WS 路径有
                # window.confirm 兜底。
                deleted = store.clear_artifacts(active_id)
                session.orchestrator._artifact_detector.forget(active_id)
                print(
                    f"[已清空任务「{task.title}」的产物（{len(deleted)} 个文件）"
                    f" —— 任务本身保留]"
                )
                continue
            if text == "/tools" or text == "/skills" or text.startswith(("/tools ", "/skills ")):
                # /tools / /skills <名字>：只读查茶客 agent 的 tools / 可用 skills。
                is_tools = text == "/tools" or text.startswith("/tools ")
                cmd = "/tools" if is_tools else "/skills"
                name = text[len(cmd):].strip()
                if not name:
                    print(f"[{cmd} 后面要跟茶客名]")
                    continue
                _print_guest_caps(
                    session, name, view="tools" if is_tools else "skills",
                )
                continue

            before_seq = session.room.latest_seq
            try:
                await session.orchestrator.submit_user_message(text, sink=renderer)
            except KeyboardInterrupt:
                print("\n[中断本回合]")
                continue
            except Exception as e:
                # 留 stacktrace 到日志，让 LLM 失败 / agentao 内部异常可追溯。
                _log.exception("submit_user_message failed")
                print(f"\n[本回合失败：{e}]")
                continue
            # 一回合 0 ~ max_consecutive_ai_turns 条茶客发言。用户消息算 1，所以
            # ``latest_seq - before_seq - 1`` 是 AI 接话条数（成功落 transcript 的）。
            if session.room.latest_seq - before_seq - 1 == 0:
                print("[（暂无人接话，可继续说或 @ 某位茶客）]")
    finally:
        session.close()

    print("茶话室关张，回头见。")
    return 0


def main() -> None:
    """``chahua`` 命令入口。"""
    args = _parse_args(sys.argv[1:])
    try:
        rc = asyncio.run(_repl(args))
    except KeyboardInterrupt:
        print()
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
