"""P15：desktop 登录态运行期注入 LLM 凭证（``set_llm_credentials`` inbound）。

承重断言（docs/P15「测试计划」）：
  - 严格白名单：未知键 / 缺必需字段 → NOTICE error 丢帧；``base_url`` 缺/空合法。
  - 注入后 ``os.environ`` 含 ``LLM_PROVIDER`` + ``<PREFIX>_{MODEL,API_KEY}``；``base_url``
    给则写、缺则 del。
  - snapshot 刷新：成功后重发 room_info，``room_default_llm`` 为新 model / api_key_ready，
    且不新增专用 ack 帧。
  - 绝不写盘：``room.toml`` 字节不变。
  - env 回滚：装配失败 → env 还原到注入前（含「原本未设 → 注入后失败 → del 回未设」）。
  - 日志不泄漏：任何 log 调用格式化结果不含 raw api_key（含 api_key 类型非法这一路）。
  - 粒度：显式 ``[[guest]].llm`` 钉住；fallback guest 跟随新默认。
"""

from __future__ import annotations

import logging
import os

import pytest

from chahua.events import (
    ChahuaEventType,
    NOTICE_LEVEL_ERROR,
    NOTICE_LEVEL_INFO,
)


@pytest.fixture(autouse=True)
def _restore_environ():
    """handler 直接写 ``os.environ``（绕 monkeypatch），测后整体还原防泄漏到别的测。"""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def _errors(events):
    return [
        e for e in events
        if e.type == ChahuaEventType.NOTICE
        and e.data.get("level") == NOTICE_LEVEL_ERROR
    ]


def _room_infos(events):
    return [e for e in events if e.type == ChahuaEventType.ROOM_INFO]


async def _send(srv, sink, **payload):
    await srv.settings._inbound_set_llm_credentials(payload, sink)


async def test_wire_frame_with_type_accepted(task_inbound_srv, monkeypatch):
    """真实线帧带 `type` 字段经 _handle_inbound 分派后必被接受 —— 严格白名单走
    check_keys_whitelist，它已扣 `type`，不会把 dispatcher 留下的 `type` 当未知键拒。"""
    session, srv = task_inbound_srv
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    events: list = []
    # 完整线帧（含 type）走真实分派入口，而非直调 handler。
    await srv._handle_inbound(
        {
            "type": "set_llm_credentials",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "api_key": "sk-x",
        },
        events.append,
    )
    assert not _errors(events), "带 type 的合法线帧不该被白名单拒"
    assert os.environ["DEEPSEEK_MODEL"] == "deepseek-chat"
    assert _room_infos(events), "合法注入必重发 room snapshot"


async def test_unknown_key_rejected(task_inbound_srv, monkeypatch):
    """未知顶层键 → NOTICE error 丢帧，env 一字不动。"""
    session, srv = task_inbound_srv
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    events: list = []
    await _send(
        srv, events.append,
        provider="deepseek", model="deepseek-chat", api_key="sk-x",
        scope="global",  # 未知键
    )
    assert _errors(events), "未知键必须 NOTICE error"
    assert "DEEPSEEK_API_KEY" not in os.environ
    assert not _room_infos(events), "丢帧不该重发 snapshot"


@pytest.mark.parametrize("missing", ["provider", "model", "api_key"])
async def test_missing_required_field_rejected(task_inbound_srv, missing):
    """缺 provider / model / api_key 任一 → NOTICE error。"""
    session, srv = task_inbound_srv
    payload = {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-x"}
    del payload[missing]
    events: list = []
    await _send(srv, events.append, **payload)
    assert _errors(events), f"缺 {missing} 必须 NOTICE error"


async def test_inject_writes_env_and_replaces_session(task_inbound_srv, monkeypatch):
    """成功注入：env 写齐 + base_url 给则写 + 重发 room_info 带新 model / api_key_ready，
    且无专用 ack 帧。"""
    session, srv = task_inbound_srv
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    events: list = []
    await _send(
        srv, events.append,
        provider="deepseek", model="deepseek-chat",
        base_url="https://api.deepseek.com/v1", api_key="sk-secret",
    )
    assert not _errors(events)
    assert os.environ["LLM_PROVIDER"] == "deepseek"
    assert os.environ["DEEPSEEK_MODEL"] == "deepseek-chat"
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-secret"
    assert os.environ["DEEPSEEK_BASE_URL"] == "https://api.deepseek.com/v1"

    infos = _room_infos(events)
    assert infos, "成功必重发 room_info snapshot"
    rdl = infos[-1].data["room_default_llm"]
    assert rdl["model"] == "deepseek/deepseek-chat"
    assert rdl["api_key_ready"] is True
    # 未新增专用 ack 帧：只复用既有 room snapshot 帧类型。
    types = {e.type for e in events}
    assert ChahuaEventType.ROOM_INFO in types
    assert all(
        t in {
            ChahuaEventType.ROOM_INFO,
            ChahuaEventType.ROOM_HISTORY,
            ChahuaEventType.TASK_INFO,
            ChahuaEventType.NOTICE,
        }
        for t in types
    ), f"不该出现专用 ack 帧：{types}"


async def test_base_url_optional_uses_default(task_inbound_srv, monkeypatch):
    """base_url 缺 → 不设 DEEPSEEK_BASE_URL，靠 _DEFAULT_BASE_URLS 仍装配成功。"""
    session, srv = task_inbound_srv
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://stale.example/v1")  # 残留
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    events: list = []
    await _send(
        srv, events.append,
        provider="deepseek", model="deepseek-chat", api_key="sk-x",
    )
    assert not _errors(events)
    # 缺 base_url → 残留被 del，走默认表。
    assert "DEEPSEEK_BASE_URL" not in os.environ
    # snapshot 里 base_url=None ⟺ spec 没带 base_url、装配走 _DEFAULT_BASE_URLS，
    # 钉死 stale 'https://stale.example/v1' 没漏进 spec。
    rdl = _room_infos(events)[-1].data["room_default_llm"]
    assert rdl["base_url"] is None


async def test_no_disk_write(task_inbound_srv, monkeypatch):
    """绝不写盘：room.toml 字节注入前后完全一致。"""
    session, srv = task_inbound_srv
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    toml_path = session.room_config.room_dir / "room.toml"
    before = toml_path.read_bytes()
    events: list = []
    await _send(
        srv, events.append,
        provider="deepseek", model="deepseek-chat", api_key="sk-x",
    )
    assert not _errors(events)
    assert toml_path.read_bytes() == before, "P15 绝不写 toml"


async def test_env_rollback_on_assembly_failure(task_inbound_srv, monkeypatch):
    """装配失败（_replace_session → SystemExit）→ env 还原到注入前（含「原本未设」→ del）。"""
    import chahua.server as server_mod

    session, srv = task_inbound_srv
    # 注入前 DEEPSEEK_* 全未设。
    for k in ("DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    before_provider = os.environ.get("LLM_PROVIDER")

    def _boom(*a, **k):
        raise SystemExit("缺 key")

    monkeypatch.setattr(server_mod, "build_room_session", _boom)
    events: list = []
    await _send(
        srv, events.append,
        provider="deepseek", model="deepseek-chat", api_key="sk-x",
    )
    assert _errors(events), "装配失败必 NOTICE error"
    # 「原本未设」的 DEEPSEEK_* 被 del 回未设。
    assert "DEEPSEEK_MODEL" not in os.environ
    assert "DEEPSEEK_API_KEY" not in os.environ
    assert "DEEPSEEK_BASE_URL" not in os.environ
    # LLM_PROVIDER 还原旧值。
    assert os.environ.get("LLM_PROVIDER") == before_provider


async def test_log_never_leaks_api_key(task_inbound_srv, monkeypatch, caplog):
    """任何 log 格式化结果不含 raw api_key 明文 —— 含「api_key 类型非法」这一路（钉死
    走自写校验只 log 类型、不 %r 打值）。"""
    session, srv = task_inbound_srv
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    secret = "sk-TOPSECRET-DO-NOT-LOG"

    with caplog.at_level(logging.DEBUG):
        events: list = []
        # ① 正常注入。
        await _send(
            srv, events.append,
            provider="deepseek", model="deepseek-chat", api_key=secret,
        )
        # ② api_key 类型非法（int）—— 自写校验必只 log 类型。
        await _send(
            srv, events.append,
            provider="deepseek", model="deepseek-chat", api_key=12345,
        )

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in blob, "raw api_key 绝不进日志"
    assert "12345" not in blob, "非法 api_key 值绝不进日志（只 log 类型）"
    # 正向断言：成功路径确实走到了 redacted info 行（否则上面的负向断言会因
    # 「日志压根没记 api_key」而 vacuously pass）。
    assert "api_key=<redacted>" in blob, "成功注入必记一条 redacted info 行"


async def test_log_never_leaks_base_url_secrets(task_inbound_srv, monkeypatch, caplog):
    """base_url 里的 userinfo / query 凭证绝不进日志 —— 只留 scheme://host[:port]。"""
    session, srv = task_inbound_srv
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    # base_url 同时带 userinfo 密码 + query token。
    url = "https://user:s3cr3t-pass@gw.example.com:8443/v1?token=tok-LEAK-123"

    with caplog.at_level(logging.DEBUG):
        events: list = []
        await _send(
            srv, events.append,
            provider="openai", model="gpt-x", base_url=url, api_key="sk-x",
        )

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert not _errors(events)
    assert "s3cr3t-pass" not in blob, "base_url userinfo 密码绝不进日志"
    assert "tok-LEAK-123" not in blob, "base_url query token 绝不进日志"
    assert "user:" not in blob, "base_url userinfo 绝不进日志"
    # host 级仍可见（便于排障）。
    assert "gw.example.com" in blob
    # 但全 URL 仍原样进了 os.environ（agentao 要拿完整 URL 发请求）。
    assert os.environ["OPENAI_BASE_URL"] == url


async def test_fallback_sections_follow_new_default(task_inbound_srv, monkeypatch):
    """没写自己 LLM 段的 guest / scoring / summary 注入后跟随新房间默认（fallback）。"""
    session, srv = task_inbound_srv
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    events: list = []
    await _send(
        srv, events.append,
        provider="deepseek", model="deepseek-chat", api_key="sk-x",
    )
    assert not _errors(events)
    info = _room_infos(events)[-1].data
    # scoring / summary 没显式段 → 跟随新默认。
    assert info["scoring_llm"]["model"] == "deepseek/deepseek-chat"
    assert info["summary_llm"]["model"] == "deepseek/deepseek-chat"
    # 没写 llm 的茶客也跟随。
    guest = next(g for g in info["guests"] if g["name"] == "宝总")
    assert guest["llm"]["model"] == "deepseek/deepseek-chat"


async def test_explicit_guest_llm_pinned(task_inbound_srv, monkeypatch):
    """显式配了自己 LLM 段的茶客注入后钉住不变（fallback 跟随但显式段不动）。"""
    session, srv = task_inbound_srv
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    # 先把宝总钉到 openai（env_paths 已配 OPENAI_API_KEY）→ 写进 toml。
    srv.admin._update_guest_llm(
        name="宝总", spec_dict={"model": "openai/gpt-5.4-mini"}, sink=lambda e: None,
    )
    events: list = []
    await _send(
        srv, events.append,
        provider="deepseek", model="deepseek-chat", api_key="sk-x",
    )
    assert not _errors(events)
    info = _room_infos(events)[-1].data
    # 房间默认切到 deepseek，但显式钉了 openai 的宝总不变。
    assert info["room_default_llm"]["model"] == "deepseek/deepseek-chat"
    guest = next(g for g in info["guests"] if g["name"] == "宝总")
    assert guest["llm"]["model"] == "openai/gpt-5.4-mini"


async def test_explicit_room_llm_emits_info_notice(task_inbound_srv, monkeypatch):
    """本房显式 [room.llm] → 注入成功后追发 info NOTICE 提醒「本房未受影响」。"""
    session, srv = task_inbound_srv
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    # 写 [room.llm]=openai（走既有 admin 帧）。
    srv.admin._update_room_llm(
        section="room", spec_dict={"model": "openai/gpt-5.4-mini"},
        sink=lambda e: None,
    )
    events: list = []
    await _send(
        srv, events.append,
        provider="deepseek", model="deepseek-chat", api_key="sk-x",
    )
    assert not _errors(events)
    infos = [
        e for e in events
        if e.type == ChahuaEventType.NOTICE
        and e.data.get("level") == NOTICE_LEVEL_INFO
    ]
    assert infos, "显式 [room.llm] 应追发 info NOTICE"
    assert "未受影响" in infos[0].data["text"]


async def test_explicit_room_llm_skips_rebuild(task_inbound_srv, monkeypatch):
    """本房显式 [room.llm] → env 注入是 no-op，必短路掉 cancel-drain + 热重建（否则
    白白杀在飞 turn / bg run / MTS）。"""
    session, srv = task_inbound_srv
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    srv.admin._update_room_llm(
        section="room", spec_dict={"model": "openai/gpt-5.4-mini"},
        sink=lambda e: None,
    )
    called = {"drain": 0, "replace": 0}

    async def _spy_drain():
        called["drain"] += 1

    def _spy_replace(*a, **k):
        called["replace"] += 1
        return True

    monkeypatch.setattr(srv, "_cancel_and_drain_all_foreground", _spy_drain)
    monkeypatch.setattr(srv, "_replace_session", _spy_replace)
    events: list = []
    await _send(
        srv, events.append,
        provider="deepseek", model="deepseek-chat", api_key="sk-x",
    )
    assert called["drain"] == 0, "遮蔽房不该 cancel-drain 前台"
    assert called["replace"] == 0, "遮蔽房不该热重建 session"
    # env 仍写了（供日后删 [room.llm] 接管）。
    assert os.environ["DEEPSEEK_MODEL"] == "deepseek-chat"


async def test_explicit_room_llm_invalid_creds_rolled_back(task_inbound_srv, monkeypatch):
    """遮蔽房短路热重建，但 env 默认仍现验 —— 坏凭证（未知 provider 缺 base_url）必
    NOTICE error + 回滚 env，绝不悄悄接受坏 env 默认（Codex P2）。"""
    session, srv = task_inbound_srv
    for k in ("CUSTOMVENDOR_MODEL", "CUSTOMVENDOR_BASE_URL", "CUSTOMVENDOR_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    before_provider = os.environ.get("LLM_PROVIDER")
    # 写 [room.llm]=openai → 后续注入对本房是 no-op（遮蔽）。
    srv.admin._update_room_llm(
        section="room", spec_dict={"model": "openai/gpt-5.4-mini"},
        sink=lambda e: None,
    )
    events: list = []
    # 未知 provider + 无 base_url → build_client 抛 SystemExit。
    await _send(
        srv, events.append,
        provider="customvendor", model="x", api_key="sk-x",
    )
    assert _errors(events), "遮蔽房坏 env 默认必 NOTICE error"
    # env 回滚：原本未设的 CUSTOMVENDOR_* 被 del，LLM_PROVIDER 还原。
    assert "CUSTOMVENDOR_MODEL" not in os.environ
    assert "CUSTOMVENDOR_API_KEY" not in os.environ
    assert os.environ.get("LLM_PROVIDER") == before_provider


@pytest.mark.parametrize("provider", ["open ai", "openrouter/qwen", "  ", "a\tb"])
async def test_dirty_provider_rejected(task_inbound_srv, monkeypatch, provider):
    """provider 含空白 / '/' → NOTICE error 丢帧，env 不动（不拼畸形前缀 / model_id）。"""
    session, srv = task_inbound_srv
    before = dict(os.environ)
    events: list = []
    await _send(
        srv, events.append,
        provider=provider, model="some-model", api_key="sk-x",
    )
    assert _errors(events), f"脏 provider={provider!r} 必 NOTICE error"
    assert os.environ == before, "拒绝路径绝不动 env"


@pytest.mark.parametrize(
    "payload",
    [
        # provider 含 '=' → 拼出非法 env 名 'FOO=BAR_MODEL'。
        {"provider": "foo=bar", "model": "x", "api_key": "sk-x"},
        # model 含 NUL → os.environ 赋值 ValueError「embedded null byte」。
        {"provider": "deepseek", "model": "x\x00y", "api_key": "sk-x"},
        # api_key 含 NUL。
        {"provider": "deepseek", "model": "x", "api_key": "sk-\x00"},
        # base_url 含 NUL。
        {
            "provider": "deepseek", "model": "x", "api_key": "sk-x",
            "base_url": "https://h\x00/v1",
        },
    ],
)
async def test_env_unsafe_value_rejected_no_crash(task_inbound_srv, monkeypatch, payload):
    """env 不可用的值（provider 含 '='、任意字段含 NUL）→ NOTICE error 丢帧 + env 回滚，
    绝不让 os.environ 赋值的 ValueError 逃逸断 ws（Codex P2）。"""
    session, srv = task_inbound_srv
    for k in ("FOO=BAR_MODEL", "DEEPSEEK_MODEL", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    before = dict(os.environ)
    events: list = []
    # 不该抛 —— 必走 NOTICE error 路径。
    await _send(srv, events.append, **payload)
    assert _errors(events), f"env 不可用值必 NOTICE error：{payload!r}"
    assert os.environ == before, "拒绝路径必回滚 env 到注入前"


async def test_model_whitespace_stripped(task_inbound_srv, monkeypatch):
    """model 前后空白被 strip 后写入 —— 与 base_url 同口径，不漏进 <PREFIX>_MODEL。"""
    session, srv = task_inbound_srv
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    events: list = []
    await _send(
        srv, events.append,
        provider="deepseek", model="  deepseek-chat  ", api_key="sk-x",
    )
    assert not _errors(events)
    assert os.environ["DEEPSEEK_MODEL"] == "deepseek-chat"
    assert _room_infos(events)[-1].data["room_default_llm"]["model"] == (
        "deepseek/deepseek-chat"
    )


async def test_env_rollback_when_replace_session_raises(task_inbound_srv, monkeypatch):
    """_replace_session **抛**异常（非 False 返回）→ env 仍被 finally 回滚，不留半改写。"""
    session, srv = task_inbound_srv
    for k in ("DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    before_provider = os.environ.get("LLM_PROVIDER")

    def _raise(*a, **k):
        raise RuntimeError("setter exploded post-build")

    monkeypatch.setattr(srv, "_replace_session", _raise)
    events: list = []
    with pytest.raises(RuntimeError):
        await _send(
            srv, events.append,
            provider="deepseek", model="deepseek-chat", api_key="sk-x",
        )
    # finally 回滚：原本未设的 DEEPSEEK_* 被 del 回未设，LLM_PROVIDER 还原。
    assert "DEEPSEEK_MODEL" not in os.environ
    assert "DEEPSEEK_API_KEY" not in os.environ
    assert os.environ.get("LLM_PROVIDER") == before_provider
