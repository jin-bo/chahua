"""P8.2-roster-b：persona 能力摘要 —— LLM 生成 + 中央内容寻址缓存。

把 ``<room>`` 花名册的摘要来源扩成三级：手写 ``[[guest]].summary``（roster-a）→
本模块的 LLM 生成缓存 → 只列名字。**LLM 生成只是「补全缓存」的旁路** —— 不阻塞
boot、缓存命中零 LLM 调用。

缓存按 persona md 内容 hash 寻址（值含 ``gen_version``），落
``user_data_root/.chahua/persona-summaries/<hash>.json``：

- 内容寻址 → persona 改了 hash 变、自动失效重生成；同一 persona 多房间共用只生成一次。
- 落在可写的 ``user_data_root`` → bundled persona（``app_root`` 只读）也能缓存。
- ``GEN_VERSION`` 是模块常量；改生成 prompt 时 bump，旧缓存读出 version 不匹配即视作
  miss、自动重生成。

所有失败（无凭据 / 网络 / 解析 / IO）一律 WARN + 退回「无摘要」，**绝不抛、不阻断
房间**（与「recorder / rotation 失败不阻断房间」同口径）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

from agentao.llm import LLMClient

from ._llm_oneshot import chat_oneshot
from ._paths import Paths
from ._persist import write_json_atomic

_log = logging.getLogger(__name__)

# 生成 prompt / 口径变更时 bump —— 旧缓存读出 gen_version 不匹配即视作 miss、自动重生成。
GEN_VERSION = 1

# 摘要硬上限：一行、≤ 该字数。<room> 花名册每位一行，长句会撑破版式。手写摘要
# （roster-a）与 LLM 生成结果共用同一上限。
SUMMARY_MAXLEN = 40

_CACHE_SUBPATH = (".chahua", "persona-summaries")
_GEN_MAX_TOKENS = 80
# prompt 里的「30 字」是软目标；SUMMARY_MAXLEN=40 是 clamp_summary 的硬兜底 —— 留 10 字
# 裕度容忍 LLM 略超，超出仍会被截断。
# P14：指令英文化，但**输出必须保留中文 ≤30 字、结尾不加标点**（摘要进 <room> 花名册）。
_SYSTEM_PROMPT = (
    "You are generating a capability roster for a multi-person chat room. Below is "
    "one participant's persona card. In ONE sentence IN CHINESE (≤ 30 Chinese "
    "characters, no trailing punctuation), summarize their most central capability "
    "or role, for others in the room to reference when dividing work. Output only "
    "that one sentence — no explanation, no quotes."
)


def clamp_summary(s: str) -> str:
    """取首行 + 截断到 :data:`SUMMARY_MAXLEN` 字。空串安全返空。

    手写摘要（roster-a）与 LLM 生成结果共用 —— 单点保证 ``<room>`` 花名册每位一行。
    """
    line = s.strip().split("\n", 1)[0].strip()
    if len(line) > SUMMARY_MAXLEN:
        return line[:SUMMARY_MAXLEN] + "…"
    return line


def persona_hash(persona_md: str) -> str:
    """persona md 全文 sha256 hex —— 内容寻址缓存键。"""
    return hashlib.sha256(persona_md.encode("utf-8")).hexdigest()


def _cache_path(paths: Paths, h: str) -> Path:
    return paths.user_data_root.joinpath(*_CACHE_SUBPATH, f"{h}.json")


def read_cached_summary(paths: Paths, persona_md: str) -> Optional[str]:
    """中央缓存查 persona 摘要。命中且 ``gen_version`` 匹配 → 摘要字符串；否则
    （miss / stale / 损坏 / IO 错）→ ``None``。读路径绝不抛。"""
    path = _cache_path(paths, persona_hash(persona_md))
    # 故意不走 _persist.read_json_or_none —— 它对坏 JSON 会 WARN；缓存文件损坏是良性
    # 事件（重新生成即可），静默 miss 不该惊扰用户。
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("gen_version") != GEN_VERSION:
        return None
    summary = obj.get("summary")
    return summary if isinstance(summary, str) and summary else None


def _write_cache(paths: Paths, h: str, summary: str) -> None:
    path = _cache_path(paths, h)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, {"gen_version": GEN_VERSION, "summary": summary})
    except OSError:
        _log.warning("persona summary 缓存写入失败：%s", path, exc_info=True)


def resolve_guest_summary(
    handwritten: Optional[str], persona_md: str, *, paths: Paths
) -> Optional[str]:
    """花名册摘要三级解析：手写 ``[[guest]].summary`` → 中央缓存 → ``None``。

    手写优先且**不触发任何 LLM / 缓存查**。返回 ``None`` 表示该 persona 还没摘要 ——
    调用方据此把它列进后台生成候选。
    """
    if handwritten:
        return handwritten
    return read_cached_summary(paths, persona_md)


async def generate_persona_summary(persona_md: str, *, llm: LLMClient) -> str:
    """让 LLM 读 persona md 产一句话能力摘要。失败 / 空 → ``""``（``chat_oneshot``
    已对所有异常兜底）。"""
    raw = await chat_oneshot(
        llm,
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": persona_md},
        ],
        max_tokens=_GEN_MAX_TOKENS,
        log_label="persona summary",
    )
    return clamp_summary(raw)


async def _ensure_cached(
    persona_md: str, *, paths: Paths, llm: LLMClient, label: str
) -> None:
    """单 persona 后台生成：缓存已命中（并发房间已写 / 本就有）即返回，否则生成 +
    写缓存。全程不抛 —— 作为裸 task 跑，异常逃逸会触发 asyncio 警告。"""
    if read_cached_summary(paths, persona_md) is not None:
        return
    summary = await generate_persona_summary(persona_md, llm=llm)
    if summary:
        _write_cache(paths, persona_hash(persona_md), summary)
        _log.info("persona summary 已生成并缓存：%s —— %s", label, summary)


# 在跑的后台生成 task，按 persona hash 索引：① 多房间并发调度同一 persona 只跑一次
# ② 持 task 引用防 GC（asyncio 不持引用的 task 可能被中途回收）。
_inflight: dict[str, "asyncio.Task[None]"] = {}


def schedule_generation(
    items: list[tuple[str, str]], *, paths: Paths, llm: LLMClient
) -> None:
    """后台预热若干 persona 的摘要缓存。``items`` = ``[(persona_md, label), ...]``。

    无运行中的 event loop（如 sync 测试装配）→ WARN 跳过、不抛。已命中缓存 / 已在跑
    的同一 persona 跳过。生成在后台 task 跑，不阻塞调用方 —— 房间装配 / boot 不被 LLM
    拖慢；摘要在下一次重建 session 时才进 ``<room>``（见 docs/P8 §5.3）。
    """
    if not items:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _log.warning(
            "persona summary：无运行 event loop，跳过 %d 个 persona 的摘要预热",
            len(items),
        )
        return
    for persona_md, label in items:
        h = persona_hash(persona_md)
        if h in _inflight or read_cached_summary(paths, persona_md) is not None:
            continue
        task = loop.create_task(
            _ensure_cached(persona_md, paths=paths, llm=llm, label=label)
        )
        _inflight[h] = task
        task.add_done_callback(lambda t, h=h: _inflight.pop(h, None))
