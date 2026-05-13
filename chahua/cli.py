"""茶话室 P0 CLI —— 端到端验证 agentao 接线、人格注入、多 provider、流式输出。

P0 范围：

- 单茶客（宝总），写死人格 ``personas/宝总.md``。
- 单房间（``rooms/p0-test``），内存 transcript。
- 用户身份从仓库顶层 ``USER.md`` 解析（``## 显示名`` 驱动 prompt 提示符）。
- LLM 配置从 ``.env`` / 环境变量读取（``LLM_PROVIDER`` 选哪家，按前缀 ``<PROVIDER>_*``）。
- 流式输出：每个 ``LLM_TEXT`` chunk 实时打到 stdout。

不在 P0 范围：意愿打分、cursor / onboarding 阈值、持久化、WebSocket、Electron。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

from agentao.llm import LLMClient
from dotenv import load_dotenv

from .guest import TeaGuest
from .room import Room
from .user_md import USER_SPEAKER_ID, UserConfig, load_user_md


# ── 路径与配置 ─────────────────────────────────────────────────────────────


def _repo_root() -> Path:
    """仓库根目录 = 包目录的父目录。

    用 ``__file__`` 而不是 cwd —— 用户从任何地方 ``chahua`` 都能找到 USER.md 和 personas。
    """
    return Path(__file__).resolve().parent.parent


# 已知 provider 的默认 base_url —— 用户没显式配 *_BASE_URL 时兜底。
# 只列**真正 OpenAI-兼容**的端点；Anthropic 原生 API 不兼容 OpenAI chat-completions，
# 用户要接 Anthropic 自己经 proxy/litellm 时显式给 *_BASE_URL。
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

    选哪家 provider 由 ``LLM_PROVIDER`` 决定（未设置或空字符串时默认 ``openai``）。
    然后按前缀读 ``<PROVIDER>_API_KEY/_BASE_URL/_MODEL``。
    """
    provider = (os.environ.get("LLM_PROVIDER") or "openai").strip().lower() or "openai"
    prefix = provider.upper().replace("-", "_")

    api_key = os.environ.get(f"{prefix}_API_KEY")
    base_url = os.environ.get(f"{prefix}_BASE_URL") or DEFAULT_BASE_URLS.get(provider)
    model = os.environ.get(f"{prefix}_MODEL")

    # ollama 本地跑通常不需要 API_KEY；LLMClient 不接受空字符串，给个占位。
    if provider == "ollama" and not api_key:
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


# ── 喂养 prompt 拼装 ───────────────────────────────────────────────────────


def _strip_top_h1(md: str) -> str:
    """去掉 USER.md 的首行 ``# xxx``（一级标题）；其余原样保留。"""
    lines = md.splitlines()
    if lines and lines[0].lstrip().startswith("# "):
        lines = lines[1:]
        # 一级标题后紧跟的空行也吃掉，避免拼出来视觉上有大空隙。
        while lines and not lines[0].strip():
            lines = lines[1:]
    return "\n".join(lines)


def _render_onboarding(
    *, user_config: UserConfig, room: Room
) -> str:
    """房间 onboarding 块：房间名 + 在场者 + 可选用户介绍。无内容时返回空串。

    P1 接 cursor + 阈值触发 onboarding（§3.2.2 / §3.2.3）后，这函数继续被复用。
    """
    display = user_config.display_name
    parts: list[str] = [f"[群聊·{room.name}]"]
    if room.topic:
        parts.append(f"当前话题：{room.topic}")

    participants_rendered = ", ".join(
        display if p == USER_SPEAKER_ID else p for p in room.participants
    )
    parts.append(f"当前在场：{participants_rendered}（含人类参与者）。")

    if user_config.has_persona and user_config.full_md:
        body = _strip_top_h1(user_config.full_md).strip()
        parts.append(f"关于「{display}」（房间里的人类参与者）：\n{body}")

    return "\n".join(parts) + "\n"


def _render_user_turn(
    *, display_name: str, new_user_text: str, guest_name: str
) -> str:
    """单轮喂养块：``<显示名> 说：<text>`` + 发言指令。"""
    return (
        f"{display_name} 说：{new_user_text}\n"
        f"\n（请以「{guest_name}」的身份发言。只说你要说的内容，不要复述别人的话，"
        f"不要加引号或前缀。）"
    )


# ── REPL ───────────────────────────────────────────────────────────────────


async def _one_turn(guest: TeaGuest, ctx_message: str) -> str:
    """跑一轮：调 guest.speak，流式打到 stdout，返回完整文本。"""

    def _on_chunk(c: str) -> None:
        sys.stdout.write(c)
        sys.stdout.flush()

    print(f"{guest.name}: ", end="", flush=True)
    text = await guest.speak(ctx_message, on_chunk=_on_chunk)
    print()
    return text


def _print_banner(
    *, user_config: UserConfig, guest: TeaGuest, room: Room, provider: str
) -> None:
    src = user_config.source
    print("─" * 60)
    print(f"茶话室 P0 · 房间：{room.name}")
    print(
        f"你的身份：{user_config.display_name}"
        + (f"  ({src})" if src else "  (USER.md 未找到，回退 '用户')")
    )
    print(
        f"在场茶客：{guest.name}   provider：{provider}   模型：{guest.agent.llm.model}"
    )
    print("输入消息回车发送；空行或 /quit 退出；/info 看权限状态。")
    print("─" * 60)


def _print_permission_info(guest: TeaGuest) -> None:
    eng = guest.agent.permission_engine
    runner = guest.agent.tool_runner
    print(
        f"  {guest.name}.permission = {guest.permission}  "
        f"(engine.mode={eng.active_mode.value}, "
        f"tool_runner.readonly={runner.readonly_mode})"
    )


async def _repl() -> int:
    repo_root = _repo_root()
    # 凭据查找顺序（高 → 低）：
    #   1. shell export 的环境变量（永远最高，load_dotenv 默认 override=False）
    #   2. 项目根 .env —— 本仓库专用
    #   3. ~/.env —— 用户全局共享（多个 agentao 宿主项目复用同一份）
    load_dotenv(repo_root / ".env")
    load_dotenv(Path.home() / ".env")

    user_config = load_user_md(repo_root=repo_root)
    llm_client, provider = _make_llm_client()

    room = Room(name="深夜茶话室", topic="随便聊")
    room.add_participant(USER_SPEAKER_ID)

    guest_name = "宝总"
    persona_path = repo_root / "chahua" / "personas" / f"{guest_name}.md"
    persona_md = persona_path.read_text(encoding="utf-8")

    guest = TeaGuest(
        name=guest_name,
        persona_md=persona_md,
        llm_client=llm_client,
        working_directory=repo_root / "rooms" / "p0-test" / "guests" / guest_name,
        permission="read-only",
    )
    room.add_participant(guest_name)

    _print_banner(user_config=user_config, guest=guest, room=room, provider=provider)

    is_first_turn = True
    try:
        while True:
            try:
                raw = input(f"{user_config.display_name}> ")
            except EOFError:
                break
            text = raw.strip()
            if not text or text in ("/quit", "/exit", ":q"):
                break
            if text == "/info":
                _print_permission_info(guest)
                continue

            room.append(USER_SPEAKER_ID, text)

            onboarding = _render_onboarding(user_config=user_config, room=room) if is_first_turn else ""
            ctx = onboarding + _render_user_turn(
                display_name=user_config.display_name,
                new_user_text=text,
                guest_name=guest_name,
            )

            try:
                reply = await _one_turn(guest, ctx)
            except KeyboardInterrupt:
                print("\n[中断本轮发言]")
                continue
            except Exception as e:
                print(f"\n[{guest_name} 沉默了一下，发言失败：{e}]")
                continue

            room.append(guest_name, reply)
            is_first_turn = False
    finally:
        guest.close()

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
