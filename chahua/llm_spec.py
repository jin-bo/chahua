"""LLM 凭据 spec —— 把"环境变量 / room.toml → LLMClient"的两条来路合并到一个数据类。

env 路径走 :meth:`LLMSpec.from_env`；toml 路径走 :meth:`LLMSpec.from_toml`（``[scoring]``
/ ``[summary]`` / ``[[guest]]`` 三处共用）。spec 再喂 :func:`build_client` 构造
:class:`agentao.llm.LLMClient`。

设计文档：[docs/P4-专业茶客配置闭环.md §4.1](../docs/P4-专业茶客配置闭环.md#41-chahuallmspecpy-新)。

**异常约定**：toml 校验失败（all-or-nothing 违反 / model 缺 ``/`` / 未知 provider 等）抛
裸 :class:`ValueError` —— 由 :mod:`chahua.config` 在调用处包成 :class:`RoomConfigError`
加上 toml 路径上下文。这层故意不依赖 config 模块，避免与之循环 import。
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
    # 阿里云 DashScope 的 OpenAI 兼容端点。国际版走 dashscope-intl.aliyuncs.com，
    # 国内用户绝大多数走主域，差异让用户在 toml 里显式写 base_url 覆盖即可。
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}

# 闲聊场景调高，比 agentao 自带的 0.2 暖；可被 LLM_TEMPERATURE 覆盖。
_DEFAULT_TEMPERATURE: float = 1.0


def provider_env_prefix(provider: str) -> str:
    """provider name → 环境变量前缀。``openai`` → ``OPENAI``、``deep-seek`` → ``DEEP_SEEK``。

    公开 —— :func:`build_client` 解析 ``<PREFIX>_API_KEY`` 与 server 端 envelope 渲染
    ``api_key_env`` 默认名两处共用，单点避免漂移。
    """
    return provider.upper().replace("-", "_")


def split_model_id(value: str) -> tuple[str, str]:
    """``"openai/gpt-5.4"`` → ``("openai", "gpt-5.4")``；
    ``"openrouter/qwen/qwen3-coder"`` → ``("openrouter", "qwen/qwen3-coder")``。

    取**第一个** ``/`` 作 provider/model 分隔 —— OpenRouter / LiteLLM 的 model id 普遍
    含 ``/`` 二级路径，必须保留。无 ``/`` 或两侧为空 → :class:`ValueError`。

    返回的 provider 已 lowercase（与 :data:`_DEFAULT_BASE_URLS` 键风格一致）；model 保留
    原大小写。
    """
    if "/" not in value:
        raise ValueError(
            f"model={value!r} 必须形如 '<provider>/<model>'（如 'openai/gpt-5.4'）"
        )
    provider, _, model = value.partition("/")
    if not provider or not model:
        raise ValueError(
            f"model={value!r} 形态不合法 —— '/' 两侧都不能为空"
        )
    return provider.strip().lower(), model.strip()


# toml 表层 LLM 字段的写出 / 解析顺序（model 在前 + 可选三项在后，与设计 §3 示例对齐
# 让用户读 toml 时形态稳定）。公开（无下划线）—— admin 端 emit / 校验都从这里取，
# 避免与 _TOML_LLM_KEYS 漂移。
LLM_TOML_FIELDS: tuple[str, ...] = ("model", "base_url", "api_key_env", "temperature")

# 非字符串字段集 —— admin 端 _render_room_toml 走 scalar literal（数值不引号）。
# 当前只有 temperature 是数值；新增数值字段时这里加，emit 路径自然分流。
LLM_TOML_NUMERIC_FIELDS: frozenset[str] = frozenset({"temperature"})

# from_toml 容忍的字段集。出现其它键 → ValueError（防 typo 静默吞）。
_TOML_LLM_KEYS: frozenset[str] = frozenset(LLM_TOML_FIELDS)

# temperature 合法范围。OpenAI / Anthropic / DeepSeek 都接受 [0, 2]；超出 → 行为不一致
# (有的报 422，有的截断到 1) → 解析期就拒。0 也接受（部分推理模型支持 deterministic）。
_TEMPERATURE_MIN: float = 0.0
_TEMPERATURE_MAX: float = 2.0


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

    @property
    def model_id(self) -> str:
        """``provider/model`` 合并字面量 —— 与 ``room.toml`` 字面、UI 展示同口径。"""
        return f"{self.provider}/{self.model}"

    def default_api_key_env(self) -> str:
        """effective api_key 环境变量名：``api_key_env`` 优先，缺则 ``<PROVIDER>_API_KEY``。"""
        return self.api_key_env or f"{provider_env_prefix(self.provider)}_API_KEY"

    @classmethod
    def try_from_env(cls) -> Optional["LLMSpec"]:
        """从环境变量构造 spec；缺 ``<PREFIX>_MODEL`` 返 ``None``（不炸）。

        P4.9 引入 —— ``[room.llm]`` 可在 toml 里独立配房间默认；env 只是 fallback。
        :meth:`from_env` 仍保留"缺 model 即 SystemExit"语义给老调用方（CLI 端不希望
        默默拿 None 跑），新代码（``build_room_session`` 装配房间默认时）走这条返回
        ``Optional`` 的版本。

        参数语义与 :meth:`from_env` 一致：

        - ``LLM_PROVIDER`` 决定 prefix（未设或空 → ``openai``）。
        - ``<PREFIX>_MODEL`` 缺即返 ``None``。env 路径允许**裸 model**（无 ``/`` 前缀），
          与 toml 路径强制 ``<provider>/<model>`` 的策略故意不一致 —— 命令行 / dotenv
          是单人单 provider 入口，按 ``LLM_PROVIDER`` 推 prefix 已够。
        - ``<PREFIX>_BASE_URL`` 显式给则带入 spec，否则留 ``None`` 让 :func:`build_client`
          按 provider 默认解析。
        - ``<PREFIX>_API_KEY`` / ``LLM_TEMPERATURE`` 留给 :func:`build_client`（缺 key 等
          失败都在那里）；``LLM_TEMPERATURE`` 在这里只读一次存进 spec。

        本方法返回的 spec ``api_key_env=None`` —— ``room_info`` 渲染时能看出"这是 env
        推断的默认 spec"（与 ``[room.llm]`` toml 显式配置形成对照）。
        """
        provider = (os.environ.get("LLM_PROVIDER") or "openai").strip().lower() or "openai"
        prefix = provider_env_prefix(provider)

        model = os.environ.get(f"{prefix}_MODEL")
        if not model:
            return None

        base_url = (os.environ.get(f"{prefix}_BASE_URL") or "").strip() or None

        return cls(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key_env=None,
            temperature=_resolve_temperature_from_env(),
        )

    @classmethod
    def from_env(cls) -> "LLMSpec":
        """:meth:`try_from_env` 的"缺 model 即 SystemExit"版本 —— CLI / 旧调用方专用。

        新代码（``build_room_session``）应走 :meth:`try_from_env`，让 ``[room.llm]``
        toml 配置接管"房间默认"的语义，env 只做 fallback。
        """
        spec = cls.try_from_env()
        if spec is not None:
            return spec
        provider = (os.environ.get("LLM_PROVIDER") or "openai").strip().lower() or "openai"
        prefix = provider_env_prefix(provider)
        known = ", ".join(sorted(_DEFAULT_BASE_URLS.keys()))
        raise SystemExit(
            f"LLM_PROVIDER={provider!r}：缺少环境变量 {prefix}_MODEL。\n"
            f"先复制 .env.example → .env 并填入真实值，或写到 ~/.env，或 shell export。\n"
            f"已知 provider（自带默认 base_url）：{known}。\n"
            f"其它 provider 可用，但 BASE_URL 必须显式给。"
        )

    @classmethod
    def from_toml(
        cls, data: Optional[dict], *, label: str
    ) -> Optional["LLMSpec"]:
        """从 toml 表（``[scoring]`` / ``[summary]`` / ``[[guest]]`` LLM 字段子集）构造 spec。

        §2.3 all-or-nothing：

        - ``data is None`` / ``data == {}`` → ``None``（让调用方走 fallback 链）。
        - ``base_url`` / ``api_key_env`` / ``temperature`` 出现但缺 ``model`` →
          :class:`ValueError`（temperature 也是 model 的可选附加，不允许单独飞）。
        - ``model`` 缺 ``/`` 或两侧为空 → :class:`ValueError`。
        - 未知 provider 且没显式 ``base_url`` → :class:`ValueError`。
        - 出现 :data:`_TOML_LLM_KEYS` 之外的键 → :class:`ValueError`（防 typo 静默吞）。
        - ``temperature`` 非数值 / 越界 [0, 2] / bool → :class:`ValueError`。

        ``label`` 是 ``"[scoring]"`` / ``"[[guest]] 宝总"`` 等人话定位，进异常信息让用户
        一眼能定位 toml 里哪一段错了。

        ``temperature`` 在 P4.8 起接入 toml schema（UI 可编辑），spec 留 None 表示
        "本段没写温度，走 :func:`build_client` 的 ``LLM_TEMPERATURE`` env / 默认"。
        """
        if not data:
            return None
        unknown = set(data) - _TOML_LLM_KEYS
        if unknown:
            raise ValueError(
                f"{label}: 未知字段 {sorted(unknown)}；允许：{sorted(_TOML_LLM_KEYS)}"
            )
        model_raw = data.get("model")
        base_url_raw = data.get("base_url")
        api_key_env_raw = data.get("api_key_env")
        temperature_raw = data.get("temperature")

        if not model_raw and (
            base_url_raw or api_key_env_raw or temperature_raw is not None
        ):
            raise ValueError(
                f"{label}: base_url / api_key_env / temperature 不能单独出现，需同写 model"
            )
        if not model_raw:
            return None
        if not isinstance(model_raw, str):
            raise ValueError(
                f"{label}: model 必须是字符串，得到 {type(model_raw).__name__}"
            )
        try:
            provider, bare_model = split_model_id(model_raw)
        except ValueError as exc:
            raise ValueError(f"{label}: {exc}") from exc

        if base_url_raw is not None and not isinstance(base_url_raw, str):
            raise ValueError(
                f"{label}: base_url 必须是字符串，得到 {type(base_url_raw).__name__}"
            )
        if api_key_env_raw is not None and not isinstance(api_key_env_raw, str):
            raise ValueError(
                f"{label}: api_key_env 必须是字符串，得到 {type(api_key_env_raw).__name__}"
            )

        base_url = (base_url_raw or "").strip() or None
        api_key_env = (api_key_env_raw or "").strip() or None

        if not base_url and provider not in _DEFAULT_BASE_URLS:
            known = ", ".join(sorted(_DEFAULT_BASE_URLS.keys()))
            raise ValueError(
                f"{label}: provider={provider!r} 不在已知列表，必须显式给 base_url；"
                f"已知：{known}"
            )

        # bool 是 int 子类，先剔除避免 True 被当 1.0；TOML 1.0 整数 / 浮点都给。
        temperature: Optional[float]
        if temperature_raw is None:
            temperature = None
        elif isinstance(temperature_raw, bool):
            raise ValueError(
                f"{label}: temperature 必须是数值，得到 bool"
            )
        elif isinstance(temperature_raw, (int, float)):
            temperature = float(temperature_raw)
            if temperature < _TEMPERATURE_MIN or temperature > _TEMPERATURE_MAX:
                raise ValueError(
                    f"{label}: temperature={temperature_raw!r} 越界，要求 "
                    f"[{_TEMPERATURE_MIN}, {_TEMPERATURE_MAX}]"
                )
        else:
            raise ValueError(
                f"{label}: temperature 必须是数值，得到 "
                f"{type(temperature_raw).__name__}"
            )

        return cls(
            provider=provider,
            model=bare_model,
            base_url=base_url,
            api_key_env=api_key_env,
            temperature=temperature,
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
    api_key_env_name = spec.default_api_key_env()
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
