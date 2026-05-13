"""茶话室 CLI（P0 → P2.2）。

P0 范围（已落）：agentao 接线、人格注入、多 provider、流式输出、read-only 拦截
P1 新增：意愿打分、@ 路由、增量喂养、摘要、USER.md 偏好注入
P1 调优：top-1~2 抢话、want_threshold 0.45
P1.5 新增：``chahua --room <dir>``，房间配置走 ``room.toml``（``chahua.config``）；
逐茶客可独立配 ``permission``（默认 read-only，可选 workspace-write / full-access）。
P2.1 新增：transcript.jsonl / summary.jsonl / cursor.json 三件落盘 + 启动续聊。
P2.2 新增：事件走 :class:`chahua.events.ChahuaEnvelope` —— CLI 由
:class:`_CliRenderer` 消费 envelope，把 message_start/delta/end / turn_start/end
渲染成与 P1 视觉一致的打字流。

不在 P2.2 范围：WebSocket server（P2.3）、Electron（P3）、逐茶客 provider/model /
isolation / [scoring]/[summary] / transport（P4）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from agentao.llm import LLMClient
from dotenv import load_dotenv

from ._paths import resolve_under
from .config import RoomConfig, RoomConfigError, load_room_config
from .cursor import GuestCursor
from .events import (
    STATUS_CANCELLED,
    STATUS_ERROR,
    STATUS_OK,
    ChahuaEnvelope,
    ChahuaEventType,
)
from .guest import TeaGuest
from .orchestrator import Orchestrator, OrchestratorConfig
from .room import Room
from .scoring import IntentScorer, ScoreKind
from .summarizer import Summarizer
from .user_md import USER_SPEAKER_ID, UserConfig, load_user_md

_log = logging.getLogger(__name__)

# 默认房间目录（相对 repo_root）。``chahua`` 不带 ``--room`` 就用这份 shipped 配置。
DEFAULT_ROOM_REL: Path = Path("rooms/p1-test")


# ── 路径与 LLM 客户端 ────────────────────────────────────────────────────────


def _repo_root() -> Path:
    """仓库根目录 = 包目录的父目录。"""
    return Path(__file__).resolve().parent.parent


DEFAULT_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
}


def _make_llm_client() -> tuple[LLMClient, str]:
    """从环境变量构造 LLMClient，返回 ``(client, provider_name)``。

    选哪家由 ``LLM_PROVIDER`` 决定（未设或空字符串时默认 ``openai``）。
    然后按前缀读 ``<PROVIDER>_API_KEY/_BASE_URL/_MODEL``。
    """
    provider = (os.environ.get("LLM_PROVIDER") or "openai").strip().lower() or "openai"
    prefix = provider.upper().replace("-", "_")

    api_key = os.environ.get(f"{prefix}_API_KEY")
    base_url = os.environ.get(f"{prefix}_BASE_URL") or DEFAULT_BASE_URLS.get(provider)
    model = os.environ.get(f"{prefix}_MODEL")

    if provider == "ollama" and not api_key:
        # ollama 本地不强制 API_KEY，给个占位让 LLMClient 校验过。
        api_key = "ollama"

    required: list[tuple[str, str | None]] = [
        (f"{prefix}_API_KEY", api_key),
        (f"{prefix}_MODEL", model),
    ]
    if not base_url:
        required.append((f"{prefix}_BASE_URL", base_url))
    missing = [n for n, v in required if not v]
    if missing:
        known = ", ".join(sorted(DEFAULT_BASE_URLS.keys()))
        raise SystemExit(
            f"LLM_PROVIDER={provider!r}：缺少环境变量 {', '.join(missing)}。\n"
            f"先复制 .env.example → .env 并填入真实值，或写到 ~/.env，或 shell export。\n"
            f"已知 provider（自带默认 base_url）：{known}。\n"
            f"其它 provider 可用，但 BASE_URL 必须显式给。"
        )

    return LLMClient(api_key=api_key, base_url=base_url, model=model), provider


# ── 茶客装配 ───────────────────────────────────────────────────────────────


def _build_guests(
    room_config: RoomConfig, llm_client: LLMClient, room: Room
) -> list[tuple[TeaGuest, str]]:
    """按 ``room.toml`` 里 ``[[guest]]`` 顺序构造茶客。

    每位茶客的 ``working_directory`` 走 :meth:`GuestConfig.workspace_in`（约定
    ``<room_dir>/guests/<name>/``）—— 路径口径与 ``.gitignore`` 里裸 ``guests/``
    行对应。``isolation=global`` 把 workspace 提到 repo 顶层 ``guests/<name>/`` 是
    P4 的事，P1.5 全员是 ``isolation=room``。
    """
    out: list[tuple[TeaGuest, str]] = []
    for gc in room_config.guests:
        persona_md = gc.read_persona()
        guest = TeaGuest(
            name=gc.name,
            persona_md=persona_md,
            llm_client=llm_client,
            working_directory=gc.workspace_in(room_config.room_dir),
            room=room,
            permission=gc.permission,
        )
        out.append((guest, persona_md))
    return out


# ── REPL 渲染 ──────────────────────────────────────────────────────────────


def _print_banner(
    *,
    user_config: UserConfig,
    guests: list[TeaGuest],
    room_config: RoomConfig,
    room: Room,
    provider: str,
    repo_root: Path,
) -> None:
    src = user_config.source
    # 显示相对 repo_root 的形式，全绝对路径在 banner 里太长；房间在仓库外则用绝对路径。
    try:
        room_dir_display: Path = room_config.room_dir.relative_to(repo_root)
    except ValueError:
        room_dir_display = room_config.room_dir
    print("─" * 60)
    head = f"茶话室 · 房间：{room_config.name}  ({room_dir_display})"
    if room.latest_seq > 0:
        head += f"  · 续聊（已有 {room.latest_seq} 条记录）"
    print(head)
    print(
        f"你的身份：{user_config.display_name}"
        + (f"  ({src})" if src else "  (USER.md 未找到，回退 '用户')")
    )
    # 把权限挂在每位名字后面，让 workspace-write / full-access 一眼看到。
    parts = [
        f"{g.name}({g.permission})" if g.permission != "read-only" else g.name
        for g in guests
    ]
    # 三位茶客同一 LLMClient → 共一个 model 字段；P4 接 room.toml provider/model 后改成各自的。
    model = guests[0].agent.llm.model
    print(f"在场茶客：{'、'.join(parts)}   provider：{provider}   模型：{model}")
    print("输入回车发送；空行 / /quit 退出；/info 看权限状态；@<名字> 直接点茶客。")
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
        description="多 Agent 群聊「茶话室」CLI（P2.2）",
    )
    parser.add_argument(
        "--room",
        type=Path,
        default=DEFAULT_ROOM_REL,
        help=(
            f"房间目录，含 room.toml（默认 {DEFAULT_ROOM_REL}）。"
            f"相对路径相对 repo_root，绝对路径原样。"
        ),
    )
    return parser.parse_args(argv)


# ── REPL ───────────────────────────────────────────────────────────────────


async def _repl(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    # 凭据查找顺序（高 → 低）：shell export > 项目 .env > ~/.env
    # load_dotenv 默认 override=False，shell 的值不会被覆盖。
    load_dotenv(repo_root / ".env")
    load_dotenv(Path.home() / ".env")

    room_dir = resolve_under(repo_root, args.room)
    try:
        room_config = load_room_config(room_dir, repo_root=repo_root)
    except RoomConfigError as e:
        print(f"房间配置错误：\n{e}", file=sys.stderr)
        return 2

    user_config = load_user_md(
        repo_root=repo_root,
        room_dir=room_config.room_dir,
        explicit=room_config.user_md_override,
    )
    llm_client, provider = _make_llm_client()

    # 持久化文件全部塞在 room_dir 下 —— 删房间一并清掉（设计文档 §3.7）。
    transcript_path = room_config.room_dir / "transcript.jsonl"
    summary_path = room_config.room_dir / "summary.jsonl"
    cursor_path = room_config.room_dir / "cursor.json"

    room = Room(
        name=room_config.name,
        topic=room_config.topic,
        rules=room_config.rules,
        transcript_path=transcript_path,
    )
    room.add_participant(USER_SPEAKER_ID)

    guest_entries = _build_guests(room_config, llm_client, room)
    guests = [g for g, _ in guest_entries]

    orchestrator = Orchestrator(
        room=room,
        user_config=user_config,
        scorer=IntentScorer(llm_client),
        summarizer=Summarizer(llm_client, summary_path=summary_path),
        cursor=GuestCursor(cursor_path=cursor_path),
        config=OrchestratorConfig(),
    )
    for guest, persona_md in guest_entries:
        orchestrator.register(guest, persona_md)

    _print_banner(
        user_config=user_config,
        guests=guests,
        room_config=room_config,
        room=room,
        provider=provider,
        repo_root=repo_root,
    )

    renderer = _CliRenderer()
    try:
        while True:
            try:
                raw = input(f"\n{user_config.display_name}> ")
            except EOFError:
                break
            text = raw.strip()
            if not text or text in ("/quit", "/exit", ":q"):
                break
            if text == "/info":
                _print_permission_info(guests)
                continue

            before_seq = room.latest_seq
            try:
                await orchestrator.submit_user_message(text, sink=renderer)
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
            if room.latest_seq - before_seq - 1 == 0:
                print("[（暂无人接话，可继续说或 @ 某位茶客）]")
    finally:
        for guest in guests:
            try:
                guest.close()
            except Exception:
                # 关 agent 失败不应阻止其他茶客清理 —— 但要留痕。
                _log.exception("guest %s close failed", guest.name)

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
