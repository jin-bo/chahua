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
        self.wfile.write(b"data: [DONE]\n\n")

@pytest.fixture
def fake_openai(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
    server.requests = []  # type: ignore[attr-defined]
    server.tool_script = []  # type: ignore[attr-defined]
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
