import asyncio
import json
from types import SimpleNamespace

import aiohttp
from aiohttp import web
import pytest

from opencodex_desktop import codex_setup, config, router
from opencodex_desktop.discovery import OpenAIAuth, Route
from opencodex_desktop.landing import render_landing
from opencodex_desktop.token_usage import TokenUsage, _LIMIT


@pytest.mark.parametrize("usage, expected", [
    ({"input_tokens": 120, "input_tokens_details": {"cached_tokens": 100}, "output_tokens": 15}, (120, 100, 15)),
    ({"prompt_tokens": 120, "prompt_tokens_details": {"cached_tokens": 100}, "completion_tokens": 15}, (120, 100, 15)),
    ({"input_tokens": 20, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10, "output_tokens": 15}, (130, 100, 15)),
    ({"input_tokens": 20, "cache_creation_input_tokens": 10, "output_tokens": 15}, (30, None, 15)),
    ({"input_tokens": 0, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 0}, (0, 0, 0)),
    ({"input_tokens": 20}, (20, None, None)),
    ({"input_tokens": -1, "output_tokens": "12", "input_tokens_details": {"cached_tokens": True}}, (None, None, None)),
    ({}, (None, None, None)),
])
def test_usage_formats_and_missing_values(usage, expected):
    capture = TokenUsage("vendor/model")
    capture.event({"id": "resp_1", "usage": {**usage, "email": "secret@example.com"}, "output": "private answer"})
    row = capture.result(200)
    assert tuple(row[k] for k in ("input_tokens", "cached_tokens", "output_tokens")) == expected
    assert row["response_id"] == "resp_1" and row["status"] == "completed"
    assert "secret" not in json.dumps(capture.usage) and "private" not in json.dumps(row)


@pytest.mark.parametrize("crlf", [False, True])
def test_fragmented_sse_keeps_final_usage_without_output_text(crlf):
    events = [
        {"type": "response.created", "response": {"id": "resp_1", "usage": None}},
        {"type": "response.output_text.delta", "delta": "private text"},
        {"type": "response.completed", "response": {"id": "resp_1", "usage": {
            "input_tokens": 120, "input_tokens_details": {"cached_tokens": 100}, "output_tokens": 15}}},
    ]
    stream = b": keepalive\n\ndata: nonsense\n\n" + b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events) + b"data: [DONE]\n\n"
    if crlf:
        stream = stream.replace(b"\n", b"\r\n")
    capture = TokenUsage("gpt-test", streaming=True)
    for i in range(0, len(stream), 7):
        capture.feed(stream[i:i + 7])
    row = capture.result(200)
    assert row["input_tokens"] == 120 and row["cached_tokens"] == 100 and row["output_tokens"] == 15
    assert row["status"] == "completed" and row["response_id"] == "resp_1"
    assert "private text" not in json.dumps(row)


def test_anthropic_stream_merges_usage_from_start_and_delta():
    capture = TokenUsage("anthropic/claude-test", streaming=True)
    for event in [
        {"type": "message_start", "message": {"id": "msg_1", "usage": {
            "input_tokens": 20, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10, "output_tokens": 0}}},
        {"type": "message_delta", "usage": {"output_tokens": 15}},
        {"type": "message_stop"},
    ]:
        capture.feed(b"data: " + json.dumps(event).encode() + b"\n\n")
    row = capture.result(200)
    assert row["input_tokens"] == 130 and row["output_tokens"] == 15 and row["cached_tokens"] == 100
    assert row["status"] == "completed"


def test_parser_is_bounded_and_recovers_after_oversized_frame():
    capture = TokenUsage("test", streaming=True)
    capture.feed(b"data: " + b"x" * (_LIMIT + 1))
    assert len(capture._buffer) <= _LIMIT and not capture._data
    capture.feed(b"\n\ndata: {\"type\":\"response.completed\",\"response\":{\"usage\":{\"input_tokens\":7}}}\n\n")
    assert capture.result(200)["input_tokens"] == 7
    capture = TokenUsage("test")
    capture.feed(b"x" * (_LIMIT + 1))
    assert not capture._buffer and capture.result(200)["input_tokens"] is None


def test_malformed_usage_does_not_break_response_forwarding():
    capture = TokenUsage("test", streaming=True)
    for data in (b"not json", b"\xff", b"[" * 2000 + b"]" * 2000, b"null",
                 b'{"response": "bad shape"}', b'{"usage": {"input_tokens_details": []}}'):
        capture.feed(b"data: " + data + b"\n\n")
    assert capture.result(200)["input_tokens"] is None


def test_failed_and_incomplete_responses_do_not_invent_zero_usage():
    capture = TokenUsage("test", streaming=True)
    assert capture.result(200)["status"] == "incomplete"
    capture.event({"type": "response.failed", "response": {"id": "resp_failed", "error": {"message": "secret"}}})
    row = capture.result(200)
    assert row["status"] == "failed" and row["input_tokens"] is None
    assert "secret" not in json.dumps(row)
    assert TokenUsage("test").result(400)["status"] == "failed"


def test_history_is_bounded_and_ui_escapes_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "load_routes", lambda: [])
    monkeypatch.setattr(router, "router_token", lambda: "private-token")
    r = router.Router(SimpleNamespace(providers=[]))
    for _ in range(102):
        cap = TokenUsage("vendor/<unsafe>")
        cap.event({"id": "<script>", "usage": {"input_tokens": 0}})
        r.record_usage(cap, 200)
    assert len(r.message_usage) == 100
    assert r.message_usage[0]["sequence"] == 102 and r.message_usage[-1]["sequence"] == 3
    page = render_landing("1", "http://localhost/v1", [], messages=list(r.message_usage))
    assert "Token usage per response" in page and 'id="message-rows"' in page
    assert "vendor/&lt;unsafe&gt;" in page and "vendor/<unsafe>" not in page
    assert "&lt;script&gt;" in page and "private-token" not in page
    assert 'title="Not reported by upstream">—' in page
    assert '<td class="num">0</td>' in page


@pytest.mark.parametrize("wire, streaming, client", [
    ("responses", True, "codex"), ("responses", False, "codex"),
    ("chat", True, "codex"), ("anthropic", False, "codex"),
    ("anthropic", True, "claude"), ("anthropic", False, "claude-native"),
    ("responses", True, "claude-gpt"), ("responses", False, "claude-gpt"),
])
def test_router_records_usage_once_and_preserves_wire_bytes(tmp_path, monkeypatch, wire, streaming, client):
    monkeypatch.setattr(router, "router_token", lambda: "test-token")
    monkeypatch.setattr(router, "fetch_codex_usage", lambda _: None)
    monkeypatch.setattr(codex_setup, "CATALOG", tmp_path / "absent-catalog")
    usage = {"input_tokens": 120, "output_tokens": 15, "input_tokens_details": {"cached_tokens": 100}}
    seen = []

    async def run():
        async def mock(req):
            seen.append(await req.json())
            if wire == "responses":
                payload = {"id": "resp_1", "usage": usage, "output": []}
                events = [{"type": "response.completed", "response": payload}]
            elif wire == "chat":
                payload = {"id": "chat_1", "choices": [{"message": {"content": "private answer"}}], "usage": {
                    "prompt_tokens": 120, "completion_tokens": 15, "prompt_tokens_details": {"cached_tokens": 100}}}
                events = []
            else:
                payload = {"id": "msg_1", "content": [{"type": "text", "text": "private answer"}], "usage": {
                    "input_tokens": 20, "cache_read_input_tokens": 100, "output_tokens": 15}}
                events = [
                    {"type": "message_start", "message": {"id": "msg_1", "usage": payload["usage"]}},
                    {"type": "message_delta", "usage": {"output_tokens": 15}}, {"type": "message_stop"},
                ]
            # GPT translation always asks Responses upstream for a stream.
            if (streaming and client != "codex") or (streaming and wire == "responses") or client == "claude-gpt":
                response = web.StreamResponse(headers={"content-type": "text/event-stream"})
                await response.prepare(req)
                data = b"".join(b"data: " + json.dumps(e).encode() + b"\r\n\r\n" for e in events)
                for i in range(0, len(data), 7):
                    await response.write(data[i:i + 7])
                await response.write_eof()
                return response
            return web.json_response(payload)

        upstream = web.Application()
        upstream.router.add_post("/{tail:.*}", mock)
        upstream_runner = web.AppRunner(upstream)
        await upstream_runner.setup()
        site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1"
        route = Route(slug="vendor/test", provider="vendor", upstream_model="test", base_url=base,
                      wire=wire, context_window=100000, context_source="test")
        monkeypatch.setattr(router, "load_routes", lambda: [route])
        monkeypatch.setattr(router, "read_openai_auth", lambda _: OpenAIAuth(mode="apikey", api_key="test-key", upstream=base))
        cfg = SimpleNamespace(codex_home=tmp_path, providers=[config.ProviderCfg(
            name="vendor", base_url=base, api_key="test-key", anthropic_url=base)], base_url="http://local/v1")
        monkeypatch.setattr(router, "HEALTH_INTERVAL", 0)
        monkeypatch.setattr(router, "ANTHROPIC_BASE", base)
        app = router.build_app(cfg)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        if client == "codex":
            body = {"model": route.slug, "stream": streaming, "input": [{"role": "user", "content": "private prompt"}]}
            path = "/v1/responses"
            headers = {"x-opencodex-token": "test-token"}
        else:
            model = "gpt-test" if client == "claude-gpt" else ("claude-test" if client == "claude-native" else route.slug)
            body = {"model": model, "stream": streaming, "max_tokens": 50,
                    "messages": [{"role": "user", "content": "private prompt"}]}
            path = "/v1/messages"
            headers = {"anthropic-version": "2023-06-01", "x-api-key": "test-token"}
            if client == "claude-native":
                headers["x-api-key"] = "client-anthropic-key"
                headers["x-opencodex-token"] = "test-token"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url + path, json=body, headers=headers) as response:
                    assert response.status == 200, await response.text()
                    result = await response.read()
                async with session.get(url + "/api/overview") as response:
                    overview_text = await response.text()
                    rows = json.loads(overview_text)["messages"]
                    assert len(rows) == 1
                    row = rows[0]
                    assert row["input_tokens"] == 120 and row["cached_tokens"] == 100 and row["output_tokens"] == 15
                    assert row["status"] == "completed"
                    assert row["model"] == body["model"]
                    assert all(s not in overview_text for s in ("private prompt", "private answer", "test-token", "test-key", "client-anthropic-key"))
                async with session.get(url + "/") as response:
                    page = await response.text()
                    assert "Token usage per response" in page and row["response_id"] in page
                if wire == "responses" and client == "codex":
                    expected = {"id": "resp_1", "usage": usage, "output": []}
                    if streaming:
                        assert result == b"data: " + json.dumps({"type": "response.completed", "response": expected}).encode() + b"\r\n\r\n"
                    else:
                        assert json.loads(result) == expected
        finally:
            await runner.cleanup()
            await upstream_runner.cleanup()
    asyncio.run(run())
