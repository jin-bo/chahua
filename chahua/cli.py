"""茶话室 CLI —— P0 端到端验证 + P1 调度（意愿打分、@ 路由、增量喂养）。

P0 范围（已落）：
- agentao 接线、人格注入、多 provider、流式输出、read-only 拦截

P1 新增：
- 三位茶客（宝总 / 玲子 / 爷叔），同一 :class:`LLMClient`（P2 才接 room.toml 分流不同模型）
- :class:`Orchestrator` 驱动意愿打分主循环、@ 路由、增量喂养
- 摘要每 N 条 transcript 增量产出，注入 onboarding
- USER.md 偏好注入到打分 prompt

不在 P1 范围：WebSocket、Electron、room.toml、持久化（transcript.jsonl / cursor.json / summary.jsonl）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from agentao.llm import LLMClient
from dotenv import load_dotenv

from .cursor import GuestCursor
from .guest import TeaGuest
from .orchestrator import Orchestrator, OrchestratorConfig
from .room import Room
from .scoring import IntentScorer, ScoreKind, ScoreResult
from .summarizer import Summarizer
from .user_md import USER_SPEAKER_ID, UserConfig, load_user_md

_log = logging.getLogger(__name__)


# 房间 ID —— P0 时叫 p0-test，P1 起改 p1-test 以避免和 P0 数据混（虽然 P1 也还没真落盘）。
ROOM_ID = "p1-test"
ROOM_NAME = "深夜茶话室"
ROOM_TOPIC = "随便聊"
ROOM_RULES = "保持中文、单条不超过 200 字、不复述别人的话。"

# P1 默认三位茶客；persona 放 chahua/personas/<name>.md。
# 顺序也是 onboarding 里显示在场列表的顺序。
GUEST_NAMES: tuple[str, ...] = ("宝总", "玲子", "爷叔")


# ── 路径与配置 ─────────────────────────────────────────────────────────────


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
    *, repo_root: Path, room_id: str, llm_client: LLMClient
) -> list[tuple[TeaGuest, str]]:
    """按 :data:`GUEST_NAMES` 顺序构造茶客，返回 ``[(guest, persona_md), ...]``。"""
    out: list[tuple[TeaGuest, str]] = []
    for name in GUEST_NAMES:
        persona_path = repo_root / "chahua" / "personas" / f"{name}.md"
        persona_md = persona_path.read_text(encoding="utf-8")
        guest = TeaGuest(
            name=name,
            persona_md=persona_md,
            llm_client=llm_client,
            working_directory=repo_root / "rooms" / room_id / "guests" / name,
            permission="read-only",
        )
        out.append((guest, persona_md))
    return out


# ── REPL 渲染 ──────────────────────────────────────────────────────────────


def _print_banner(
    *, user_config: UserConfig, guests: list[TeaGuest], room: Room, provider: str
) -> None:
    src = user_config.source
    print("─" * 60)
    print(f"茶话室 P1 · 房间：{room.name}")
    print(
        f"你的身份：{user_config.display_name}"
        + (f"  ({src})" if src else "  (USER.md 未找到，回退 '用户')")
    )
    names = "、".join(g.name for g in guests)
    # 三位茶客同一 LLMClient → 共一个 model 字段；P2 接 room.toml 后这里改成各自的。
    model = guests[0].agent.llm.model
    print(f"在场茶客：{names}   provider：{provider}   模型：{model}")
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


_KIND_BADGE: dict[ScoreKind, str] = {
    ScoreKind.MENTION: "@",
    ScoreKind.COOLDOWN: "冷却",
    ScoreKind.ERROR: "失败",
}


def _format_scores(scores: list[ScoreResult]) -> str:
    """把一轮打分明细排成 ``宝总=0.72, 玲子=@, 爷叔=冷却`` 这样。"""
    parts: list[str] = []
    for r in scores:
        badge = _KIND_BADGE.get(r.kind)
        parts.append(
            f"{r.guest_name}={badge}" if badge else f"{r.guest_name}={r.score:.2f}"
        )
    return ", ".join(parts)


def _on_turn_start(name: str, scores: list[ScoreResult]) -> None:
    """打"\n[选中 X 打分：...]\nX: "，流式回调写到 ":" 后面。"""
    print(f"\n[选中 {name}  打分：{_format_scores(scores)}]")
    print(f"{name}: ", end="", flush=True)


def _on_chunk(c: str) -> None:
    sys.stdout.write(c)
    sys.stdout.flush()


# ── REPL ───────────────────────────────────────────────────────────────────


async def _repl() -> int:
    repo_root = _repo_root()
    # 凭据查找顺序（高 → 低）：shell export > 项目 .env > ~/.env
    # load_dotenv 默认 override=False，shell 的值不会被覆盖。
    load_dotenv(repo_root / ".env")
    load_dotenv(Path.home() / ".env")

    user_config = load_user_md(repo_root=repo_root)
    llm_client, provider = _make_llm_client()

    room = Room(name=ROOM_NAME, topic=ROOM_TOPIC, rules=ROOM_RULES)
    room.add_participant(USER_SPEAKER_ID)

    guest_entries = _build_guests(
        repo_root=repo_root, room_id=ROOM_ID, llm_client=llm_client
    )
    guests = [g for g, _ in guest_entries]

    orchestrator = Orchestrator(
        room=room,
        user_config=user_config,
        scorer=IntentScorer(llm_client),
        summarizer=Summarizer(llm_client),
        cursor=GuestCursor(),
        config=OrchestratorConfig(),
    )
    for guest, persona_md in guest_entries:
        orchestrator.register(guest, persona_md)

    _print_banner(
        user_config=user_config, guests=guests, room=room, provider=provider
    )

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
                await orchestrator.submit_user_message(
                    text, on_chunk=_on_chunk, on_turn_start=_on_turn_start
                )
            except KeyboardInterrupt:
                print("\n[中断本回合]")
                continue
            except Exception as e:
                print(f"\n[本回合失败：{e}]")
                continue
            # 一回合 0 ~ max_consecutive_ai_turns 条茶客发言。
            # 用户消息也算进 latest_seq，所以 -1 还要 > 0 才是真有茶客接话。
            ai_replies = room.latest_seq - before_seq - 1
            if ai_replies == 0:
                print("[（暂无人接话，可继续说或 @ 某位茶客）]")
            else:
                print()  # 流式输出不主动换行，由这里收尾
    finally:
        for guest in guests:
            try:
                guest.close()
            except Exception:
                # 关 agent 失败不应阻止其他茶客清理 —— 但要留痕，免得调试时一无所知。
                _log.exception("guest %s close failed", guest.name)

    print("茶话室关张，回头见。")
    return 0


def main() -> None:
    """``chahua`` 命令入口。"""
    try:
        rc = asyncio.run(_repl())
    except KeyboardInterrupt:
        print()
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
