"""P19：真实 ``Agentao`` 契约测试 —— 只在网络边界打桩。

其余 speak 相关用例全把 ``agent.arun`` 换成 ``AsyncMock``，替身不会跟着 agentao 演进。
这里起一个本地假 OpenAI 兼容 SSE 端点，让 ``TeaGuest.speak`` → ``Agentao.arun`` →
``LLMClient.chat_stream`` → openai SDK → ``ChahuaTransport`` → envelope 整条真栈跑一遍，
升级 agentao 时接口 / 事件 payload 漂移会在这里先炸。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from chahua import admin
from chahua.events import ChahuaEventType
from chahua.session import build_room_session

_REPLY_CHUNKS = ["侬好", "，", "阿拉来哉"]


def _sse(obj) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # 静音
        pass

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)  # type: ignore[attr-defined]
        # P20：脚本化的可重试错误（429 + Retry-After），弹完再正常回复；
        # ``fail_forever`` 让每次都 429（退避期间取消用）。
        fail = self.server.fail_script  # type: ignore[attr-defined]
        if fail or self.server.fail_forever is not None:  # type: ignore[attr-defined]
            retry_after = fail.pop(0) if fail else self.server.fail_forever  # type: ignore[attr-defined]
            raw = json.dumps({"error": {"message": "slow down", "type": "rate_limit"}}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", str(retry_after))
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        base = {"id": "chatcmpl-p19", "object": "chat.completion.chunk",
                "created": 0, "model": body.get("model", "m")}
        if not body.get("stream"):
            payload = {**base, "object": "chat.completion", "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "".join(_REPLY_CHUNKS)},
            }]}
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def _delta(delta, finish=None):
            self.wfile.write(_sse({**base, "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish}]}))

        script = self.server.tool_script  # type: ignore[attr-defined]
        if script:
            # 脚本化的一轮工具调用；弹掉后下一次请求回落到纯文本收尾
            name, arguments = script.pop(0)
            _delta({"role": "assistant", "content": None, "tool_calls": [{
                "index": 0, "id": "call_p19", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }]})
            _delta({}, "tool_calls")
        else:
            _delta({"role": "assistant", "content": ""})
            for chunk in _REPLY_CHUNKS:
                _delta({"content": chunk})
            _delta({}, "stop")
        usage = self.server.usage  # type: ignore[attr-defined]
        if usage is not None:
            # stream_options.include_usage 的末尾纯 usage 帧（choices 为空）
            self.wfile.write(_sse({**base, "choices": [], "usage": usage}))
        self.wfile.write(b"data: [DONE]\n\n")

@pytest.fixture
def fake_openai(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
    server.requests = []  # type: ignore[attr-defined]
    server.tool_script = []  # type: ignore[attr-defined]
    server.fail_script = []  # type: ignore[attr-defined]  # 每项 = Retry-After 秒数
    server.fail_forever = None  # type: ignore[attr-defined]  # 非 None = 恒 429 的 Retry-After
    server.usage = None  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # 本机若配了 HTTP(S)_PROXY，httpx 会把 127.0.0.1 也送去代理 → 假端点收不到请求
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _build(env_paths, **guest_extra):
    rc = admin.create_room(
        paths=env_paths, room_id="p19", name="p19",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总", **guest_extra}],
    )
    return build_room_session(rc.room_dir, env_paths)


async def test_real_agentao_speak_roundtrip(env_paths, fake_openai):
    session = _build(env_paths)
    envelopes = []
    try:
        guest = session.orchestrator.get_guest("宝总")
        assert guest is not None

        msg = await guest.speak("<room>用户说：侬好</room>", turn_id="turn_p19",
                                sink=envelopes.append)

        expected = "".join(_REPLY_CHUNKS)
        assert msg is not None and msg.text == expected

        # 真请求确实打到了边界：带 persona system prompt + 工具 schema
        assert fake_openai.requests, "LLM 端点没收到请求"
        req = fake_openai.requests[0]
        tool_names = {t["function"]["name"] for t in req.get("tools", [])}
        assert {"task_list_artifacts", "propose_delegate"} <= tool_names

        # message_start / message_end 成对、message_id 与 transcript 一致
        kinds = [e.type for e in envelopes]
        assert kinds[0] == ChahuaEventType.MESSAGE_START
        assert kinds[-1] == ChahuaEventType.MESSAGE_END
        assert kinds.count(ChahuaEventType.MESSAGE_START) == 1
        assert kinds.count(ChahuaEventType.MESSAGE_END) == 1
        assert {e.message_id for e in envelopes} == {msg.message_id}
        end = envelopes[-1]
        assert end.status == "ok"
        assert end.data["text"] == expected

        # agentao 流式 LLM_TEXT 事件经 transport 桥成增量 envelope，拼回全文
        streamed = "".join(
            e.data["chunk"] for e in envelopes
            if e.type == ChahuaEventType.MESSAGE_DELTA
        )
        assert streamed == expected
    finally:
        session.close()


def _tool_envelopes(envelopes):
    start = [e for e in envelopes if e.type == ChahuaEventType.TOOL_START]
    done = [e for e in envelopes if e.type == ChahuaEventType.TOOL_COMPLETE]
    return start, done


async def test_real_agentao_tool_round_bridges_tool_events(env_paths, fake_openai):
    """工具轮：TOOL_START / TOOL_COMPLETE payload 经 transport 桥出，结果回灌第二次请求。"""
    fake_openai.tool_script.append(("task_list_artifacts", {}))
    session = _build(env_paths)
    envelopes = []
    try:
        guest = session.orchestrator.get_guest("宝总")
        msg = await guest.speak("ctx", turn_id="turn_p19", sink=envelopes.append)

        assert msg is not None and msg.text == "".join(_REPLY_CHUNKS)
        start, done = _tool_envelopes(envelopes)
        assert len(start) == 1 and len(done) == 1
        assert start[0].data["tool"] == "task_list_artifacts"
        assert done[0].data["tool"] == "task_list_artifacts"
        assert {e.message_id for e in envelopes} == {msg.message_id}

        assert len(fake_openai.requests) == 2
        tool_msgs = [m for m in fake_openai.requests[1]["messages"] if m["role"] == "tool"]
        assert len(tool_msgs) == 1 and tool_msgs[0]["tool_call_id"] == "call_p19"
    finally:
        session.close()


async def test_real_agentao_readonly_guest_blocks_write(env_paths, fake_openai, tmp_path):
    """read-only 双 API：真实 tool_runner 拦住 ``write_file``，盘上不落文件。"""
    target = tmp_path / "p19-should-not-exist.txt"
    fake_openai.tool_script.append(
        ("write_file", {"path": str(target), "file_path": str(target), "content": "x"})
    )
    session = _build(env_paths, permission="read-only")
    envelopes = []
    try:
        guest = session.orchestrator.get_guest("宝总")
        assert guest.permission == "read-only"
        msg = await guest.speak("ctx", turn_id="turn_p19", sink=envelopes.append)

        assert msg is not None  # 被拒后茶客照常收尾，message 成对
        assert not target.exists()
        tool_msgs = [m for m in fake_openai.requests[1]["messages"] if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        denial = tool_msgs[0]["content"].lower()
        assert "read-only" in denial or "denied" in denial, denial
    finally:
        session.close()


# ── P20：调用记录真依赖契约（docs/P20 §8 第 11–12 条） ─────────────────────────

_USAGE = {
    "prompt_tokens": 1234, "completion_tokens": 56, "total_tokens": 1290,
    "prompt_tokens_details": {"cached_tokens": 1024},
}


def _p20_recorder(guest, tmp_path):
    from chahua.debug_recorder import TurnRecorder

    rec = TurnRecorder(tmp_path / "p20", enabled=True, capture_prompts=False)
    rec.start_turn(turn_id="turn_p20", task_id=None,
                   trigger={"kind": "user_msg", "ref_seq": 0})
    guest._recorder = rec
    return rec


def _p20_disk_message(tmp_path):
    from chahua._persist import read_jsonl_skip_bad

    rows = list(read_jsonl_skip_bad(tmp_path / "p20" / "debug" / "turns.jsonl"))
    assert len(rows) == 1
    return rows[0]["messages"][0]


def _message_end(envelopes):
    ends = [e for e in envelopes if e.type == ChahuaEventType.MESSAGE_END]
    assert len(ends) == 1
    return ends[0]


async def test_real_agentao_llm_call_usage_and_retry(env_paths, fake_openai, tmp_path):
    """真栈：一次 429 重试后成功 + 工具轮 → 两次主循环调用；usage 字段名与上游一致。"""
    fake_openai.usage = _USAGE
    fake_openai.fail_script.append(0)
    fake_openai.tool_script.append(("task_list_artifacts", {}))
    session = _build(env_paths)
    envelopes = []
    try:
        guest = session.orchestrator.get_guest("宝总")
        rec = _p20_recorder(guest, tmp_path)
        msg = await guest.speak("ctx", turn_id="turn_p20", sink=envelopes.append)
        assert msg is not None
        rec.flush_turn()

        calls = _message_end(envelopes).data["llm_calls"]
        # 3 次 HTTP（429 + 工具轮 + 收尾）= 2 次主循环调用：重试在 chat_stream 内部
        assert len(fake_openai.requests) == 3
        assert len(calls) == 2, calls
        first, second = calls
        assert first["status"] == "ok" and first["finish_reason"] == "tool_calls"
        assert len(first["retries"]) == 1
        assert first["retries"][0]["retry"] == 1
        assert "retries" not in second
        for call in calls:
            assert call["prompt_tokens"] == 1234
            assert call["completion_tokens"] == 56
            assert call["cache_read_tokens"] == 1024  # prompt_tokens 的组成部分
        assert _p20_disk_message(tmp_path)["llm_calls"] == calls
    finally:
        session.close()


async def test_real_agentao_cancel_during_backoff_records_once(env_paths, fake_openai, tmp_path):
    """退避期间取消：该调用只记一次（上游 error COMPLETED 吸收重试，或占位，二者不并存）。"""
    import asyncio

    fake_openai.fail_forever = 30  # Retry-After 30s：取消必然落在退避等待里
    session = _build(env_paths)
    envelopes = []
    try:
        guest = session.orchestrator.get_guest("宝总")
        rec = _p20_recorder(guest, tmp_path)
        task = asyncio.create_task(
            guest.speak("ctx", turn_id="turn_p20", sink=envelopes.append)
        )
        for _ in range(200):
            if fake_openai.requests:
                break
            await asyncio.sleep(0.02)
        assert fake_openai.requests, "LLM 端点没收到请求"
        await asyncio.sleep(0.3)  # 让 429 回到客户端、进入退避
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=15)
        rec.flush_turn()

        end = _message_end(envelopes)
        assert end.status == "cancelled"
        calls = end.data["llm_calls"]
        assert len(calls) == 1, calls
        assert calls[0]["status"] in ("error", "cancelled")
        assert len(calls[0]["retries"]) >= 1
        assert _p20_disk_message(tmp_path)["llm_calls"] == calls
    finally:
        session.close()


async def test_real_agentao_live_status_retry_then_idle(env_paths, fake_openai):
    """P20.1 真栈：一次 429 → guest_status retrying，随后 idle，且都先于 message_end。"""
    fake_openai.fail_script.append(0)
    session = _build(env_paths)
    envelopes = []
    try:
        guest = session.orchestrator.get_guest("宝总")
        msg = await guest.speak("ctx", turn_id="turn_p201", sink=envelopes.append)
        assert msg is not None
        types = [e.type for e in envelopes]
        statuses = [e for e in envelopes if e.type == ChahuaEventType.GUEST_STATUS]
        assert [e.data["state"] for e in statuses] == ["retrying", "idle"], statuses
        assert statuses[0].data["retry"] == 1
        assert isinstance(statuses[0].data.get("reason"), str)
        end_idx = types.index(ChahuaEventType.MESSAGE_END)
        assert all(envelopes.index(e) < end_idx for e in statuses)
        assert all(e.message_id == msg.message_id for e in statuses)
    finally:
        session.close()
