"""P8.2-roster-b：persona 摘要 LLM 生成 + 中央内容寻址缓存。

覆盖：① 内容寻址 hash ② 缓存读写 round-trip / gen_version 失效 / 损坏视作 miss
③ 三级解析（手写 → 缓存 → 无）④ LLM 生成 + 截断 ⑤ schedule 无 loop 跳过 / 有
loop 写缓存。
"""

from __future__ import annotations

import json
from pathlib import Path

from chahua import persona_summary
from chahua._paths import Paths


# ─── fake LLMClient（chat_oneshot 只用 resp.choices[0].message.content）──────


class _FakeLLM:
    def __init__(self, content: str = "擅长成本测算与预算评审") -> None:
        self._content = content
        self.calls = 0

    def chat(self, *, messages, max_tokens):  # noqa: ANN001 - 测试替身
        self.calls += 1
        msg = type("M", (), {"content": self._content})()
        choice = type("C", (), {"message": msg})()
        return type("R", (), {"choices": [choice]})()


def _paths(tmp_path: Path) -> Paths:
    return Paths(app_root=tmp_path, user_data_root=tmp_path)


# ─── ① 内容寻址 hash ────────────────────────────────────────────────────────


def test_persona_hash_stable_and_content_sensitive():
    md = "# 范总\n擅长成本测算"
    assert persona_summary.persona_hash(md) == persona_summary.persona_hash(md)
    assert persona_summary.persona_hash(md) != persona_summary.persona_hash(md + "x")


# ─── ② 缓存读写 ─────────────────────────────────────────────────────────────


def test_read_cached_summary_miss_returns_none(tmp_path):
    assert persona_summary.read_cached_summary(_paths(tmp_path), "# 范总") is None


def test_cache_round_trip(tmp_path):
    paths, md = _paths(tmp_path), "# 范总\n擅长成本测算"
    persona_summary._write_cache(paths, persona_summary.persona_hash(md), "成本专家")
    assert persona_summary.read_cached_summary(paths, md) == "成本专家"


def test_cache_gen_version_mismatch_is_miss(tmp_path):
    paths, md = _paths(tmp_path), "# 范总"
    path = persona_summary._cache_path(paths, persona_summary.persona_hash(md))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"gen_version": persona_summary.GEN_VERSION + 99, "summary": "旧"}),
        encoding="utf-8",
    )
    assert persona_summary.read_cached_summary(paths, md) is None


def test_cache_corrupt_file_is_miss(tmp_path):
    paths, md = _paths(tmp_path), "# 范总"
    path = persona_summary._cache_path(paths, persona_summary.persona_hash(md))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert persona_summary.read_cached_summary(paths, md) is None


# ─── ③ 三级解析 ─────────────────────────────────────────────────────────────


def test_resolve_handwritten_wins(tmp_path):
    """手写优先 —— 即使缓存有别的值也用手写。"""
    paths, md = _paths(tmp_path), "# 范总"
    persona_summary._write_cache(paths, persona_summary.persona_hash(md), "缓存值")
    assert persona_summary.resolve_guest_summary("手写值", md, paths=paths) == "手写值"


def test_resolve_falls_through_to_cache(tmp_path):
    paths, md = _paths(tmp_path), "# 范总"
    persona_summary._write_cache(paths, persona_summary.persona_hash(md), "缓存值")
    assert persona_summary.resolve_guest_summary(None, md, paths=paths) == "缓存值"


def test_resolve_both_miss_returns_none(tmp_path):
    assert persona_summary.resolve_guest_summary(None, "# 范总", paths=_paths(tmp_path)) is None


# ─── ④ LLM 生成 + 截断 ──────────────────────────────────────────────────────


async def test_generate_persona_summary(tmp_path):
    out = await persona_summary.generate_persona_summary(
        "# 范总\n做成本测算", llm=_FakeLLM("擅长成本测算")
    )
    assert out == "擅长成本测算"


async def test_generate_clamps_long_output(tmp_path):
    out = await persona_summary.generate_persona_summary(
        "# 范总", llm=_FakeLLM("能" * 60)
    )
    assert out == "能" * persona_summary.SUMMARY_MAXLEN + "…"


def test_clamp_summary_first_line_and_truncation():
    assert persona_summary.clamp_summary("  第一行\n第二行  ") == "第一行"
    assert persona_summary.clamp_summary("能" * 100) == "能" * 40 + "…"
    assert persona_summary.clamp_summary("") == ""


# ─── ⑤ schedule_generation ──────────────────────────────────────────────────


def test_schedule_generation_no_loop_skips(tmp_path):
    """sync 上下文无 event loop → WARN 跳过、不抛、不写缓存。"""
    paths, md = _paths(tmp_path), "# 范总"
    persona_summary.schedule_generation(
        [(md, "范总")], paths=paths, llm=_FakeLLM()
    )
    assert persona_summary.read_cached_summary(paths, md) is None


async def test_schedule_generation_writes_cache(tmp_path):
    paths, md = _paths(tmp_path), "# 范总\n擅长成本测算"
    llm = _FakeLLM("成本测算专家")
    persona_summary.schedule_generation([(md, "范总")], paths=paths, llm=llm)

    tasks = list(persona_summary._inflight.values())
    assert tasks, "应已起后台生成 task"
    for t in tasks:
        await t

    assert persona_summary.read_cached_summary(paths, md) == "成本测算专家"


async def test_schedule_generation_skips_already_cached(tmp_path):
    """缓存已命中 → 不再起 task、不调 LLM。"""
    paths, md = _paths(tmp_path), "# 范总"
    persona_summary._write_cache(paths, persona_summary.persona_hash(md), "已有")
    llm = _FakeLLM()
    persona_summary.schedule_generation([(md, "范总")], paths=paths, llm=llm)
    assert persona_summary.persona_hash(md) not in persona_summary._inflight
    assert llm.calls == 0
