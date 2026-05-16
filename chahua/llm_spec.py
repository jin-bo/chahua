"""LLM 凭据 spec —— 把"环境变量 / room.toml → LLMClient"的两条来路合并到一个数据类。

env 路径走 :meth:`LLMSpec.from_env`，spec 喂 :func:`build_client` 构造
:class:`agentao.llm.LLMClient`。toml 路径与 ``split_model_id`` / ``from_toml`` 留到 P4.1
同 PR 加（schema 解析与 mutator 消费绑同一 PR，避免 "parser 接受了但 session 不用"
的中间态）。

设计文档：[docs/P4-专业茶客配置闭环.md §4.1](../docs/P4-专业茶客配置闭环.md#41-chahuallmspecpy-新)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from agentao.llm import LLMClient


# 已知 provider 的默认 base_url。在 _DEFAULT_BASE_URLS 内的 provider 可省 base_url；
# 其它 provider 必须显式给 base_url，否则 build_client 抛 SystemExit。
_DEFAULT_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
}

# 闲聊场景调高，比 agentao 自带的 0.2 暖；可被 LLM_TEMPERATURE 覆盖。
_DEFAULT_TEMPERATURE: float = 1.0


def _provider_prefix(provider: str) -> str:
    """provider name → 环境变量前缀。``openai`` → ``OPENAI``、``deep-seek`` → ``DEEP_SEEK``。"""
    return provider.upper().replace("-", "_")


def _resolve_temperature_from_env() -> Optional[float]:
    """``LLM_TEMPERATURE`` env 解析。空 / 未设 → ``None``；非法 → :class:`SystemExit`。

    与"spec 自带 temperature 优先"的语义解耦 —— 调用方负责 spec.temperature 兜底。
    """
    raw = (os.environ.get("LLM_TEMPERATURE") or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise SystemExit(
            f"LLM_TEMPERATURE={raw!r} 解析失败：必须是浮点数（如 0.7）。"
        ) from exc


@dataclass(frozen=True, slots=True)
class LLMSpec:
    """一组装配 :class:`LLMClient` 所需的"声明性"参数。

    - ``provider``：已 lowercase（``openai`` / ``anthropic`` / …）。
    - ``model``：**裸 model id**，不带 ``provider/`` 前缀（``gpt-5.4-mini``）。toml 表层
      值形如 ``"openai/gpt-5.4-mini"`` 由 P4.1 的 ``split_model_id`` 拆分；env 路径直接读
      ``<PREFIX>_MODEL``，本来就是裸的。
    - ``base_url``：``None`` 表示"按 provider 默认"（由 :func:`build_client` 解析）。
    - ``api_key_env``：``None`` 表示"按 ``<PROVIDER>_API_KEY`` 约定"。
    - ``temperature``：``None`` 表示"按 ``LLM_TEMPERATURE`` 环境变量或 ``_DEFAULT_TEMPERATURE``"。

    `None` 与"显式同名默认值"故意区分 —— 让 ``room_info`` envelope / UI 表单一眼能
    判断"这位茶客是不是用了非默认配置"。
    """

    provider: str
    model: str
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    temperature: Optional[float] = None

    @classmethod
    def from_env(cls) -> "LLMSpec":
        """从环境变量构造 spec：

        - ``LLM_PROVIDER`` 决定 prefix（未设或空 → ``openai``）。
        - ``<PREFIX>_MODEL`` 必给（缺 → :class:`SystemExit`）。env 路径允许**裸 model**
          （无 ``/`` 前缀），与 toml 路径强制 ``<provider>/<model>`` 的策略故意不一致 ——
          命令行 / dotenv 是单人单 provider 入口，按 ``LLM_PROVIDER`` 推 prefix 已够。
        - ``<PREFIX>_BASE_URL`` 显式给则带入 spec，否则留 ``None`` 让 :func:`build_client`
          按 provider 默认解析。
        - ``<PREFIX>_API_KEY`` / ``LLM_TEMPERATURE`` 留给 :func:`build_client`（缺 key 等
          失败都在那里）；``LLM_TEMPERATURE`` 在这里只读一次存进 spec，避免 build_client
          再读一遍。

        本方法返回的 spec ``api_key_env=None`` —— ``room_info`` 渲染时能看出"这是 env
        推断的默认 spec"。
        """
        provider = (os.environ.get("LLM_PROVIDER") or "openai").strip().lower() or "openai"
        prefix = _provider_prefix(provider)

        model = os.environ.get(f"{prefix}_MODEL")
        if not model:
            known = ", ".join(sorted(_DEFAULT_BASE_URLS.keys()))
            raise SystemExit(
                f"LLM_PROVIDER={provider!r}：缺少环境变量 {prefix}_MODEL。\n"
                f"先复制 .env.example → .env 并填入真实值，或写到 ~/.env，或 shell export。\n"
                f"已知 provider（自带默认 base_url）：{known}。\n"
                f"其它 provider 可用，但 BASE_URL 必须显式给。"
            )

        base_url = (os.environ.get(f"{prefix}_BASE_URL") or "").strip() or None

        return cls(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key_env=None,
            temperature=_resolve_temperature_from_env(),
        )


def build_client(spec: LLMSpec) -> LLMClient:
    """按 ``spec`` 构造 :class:`LLMClient`。缺 key / base_url / temperature 不合法 →
    :class:`SystemExit`（CLI / server 同口径，进程早炸早好）。

    - api_key 走 ``spec.api_key_env``（缺则按 ``<PROVIDER>_API_KEY`` 约定）。ollama
      provider 缺 key 时用占位 ``"ollama"`` —— 本地 server 不强制鉴权，给个非空让
      LLMClient 校验过。
    - base_url 走 ``spec.base_url`` 优先；缺则按 provider 查 :data:`_DEFAULT_BASE_URLS`；
      仍缺即 SystemExit（提示需显式 base_url）。
    - temperature 走 ``spec.temperature`` 优先；缺则按 ``LLM_TEMPERATURE`` 环境变量；
      仍缺走 :data:`_DEFAULT_TEMPERATURE`。
    """
    api_key_env_name = spec.api_key_env or f"{_provider_prefix(spec.provider)}_API_KEY"
    api_key = os.environ.get(api_key_env_name)
    if spec.provider == "ollama" and not api_key:
        api_key = "ollama"
    if not api_key:
        raise SystemExit(
            f"LLM provider={spec.provider!r}：缺少环境变量 {api_key_env_name}。\n"
            f"先复制 .env.example → .env 并填入真实值，或写到 ~/.env，或 shell export。"
        )

    base_url = spec.base_url or _DEFAULT_BASE_URLS.get(spec.provider)
    if not base_url:
        known = ", ".join(sorted(_DEFAULT_BASE_URLS.keys()))
        raise SystemExit(
            f"LLM provider={spec.provider!r} 不在已知列表，必须显式给 base_url。\n"
            f"已知 provider（自带默认 base_url）：{known}。"
        )

    if spec.temperature is not None:
        temperature: float = spec.temperature
    else:
        from_env = _resolve_temperature_from_env()
        temperature = from_env if from_env is not None else _DEFAULT_TEMPERATURE

    return LLMClient(
        api_key=api_key,
        base_url=base_url,
        model=spec.model,
        temperature=temperature,
    )
