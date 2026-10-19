"""LLM adapter tests: request/response mapping, streaming, failures, privacy guard.

Every HTTP adapter test runs over ``httpx.MockTransport``; no test touches a
network. Covers the Ollama adapter (``/api/chat``, NDJSON streaming), the
opt-in OpenAI-compatible adapter (``/chat/completions``, SSE streaming), the
scripted fake, and the provider factory's external-inference guard.
"""

import json
import logging

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.modules.agent.llm import (
    AssistantMessage,
    ChatMessage,
    FakeLLMProvider,
    LLMConfigurationError,
    LLMError,
    OllamaChatProvider,
    OpenAICompatibleChatProvider,
    TextDelta,
    ToolCall,
    build_llm_provider,
)

API_KEY = "xai-TEST-KEY-0123456789-do-not-leak"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": "Search documents.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    }
]

CONVERSATION = [
    ChatMessage(role="system", content="policy"),
    ChatMessage(role="user", content="What is the invoice total?"),
    ChatMessage(
        role="assistant",
        content="",
        tool_calls=(ToolCall(name="search_documents", arguments={"query": "invoice"}, id="c1"),),
    ),
    ChatMessage(
        role="tool",
        content="<<<DOCUMENT [1]>>>",
        tool_name="search_documents",
        tool_call_id="c1",
    ),
]


def _ollama(handler) -> OllamaChatProvider:
    return OllamaChatProvider(
        base_url="http://ollama.test",
        model="qwen2.5:7b-instruct",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )


def _openai(handler) -> OpenAICompatibleChatProvider:
    return OpenAICompatibleChatProvider(
        base_url="https://llm.test/v1",
        model="grok-test",
        api_key=SecretStr(API_KEY),
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )


def _ndjson(*chunks: dict) -> bytes:
    return "".join(json.dumps(chunk) + "\n" for chunk in chunks).encode()


def _sse(*chunks: dict | str) -> bytes:
    """SSE body: a comment line, then one ``data:`` event per chunk."""
    events = [chunk if isinstance(chunk, str) else json.dumps(chunk) for chunk in chunks]
    return b": keep-alive\n\n" + "".join(f"data: {event}\n\n" for event in events).encode()


def _completion(message: dict) -> dict:
    return {"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}


# --- Ollama -----------------------------------------------------------------


async def test_ollama_chat_maps_messages_and_tools_to_the_request() -> None:
    """The request carries model, role-tagged messages, tool calls, and tools."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "Hi"}})

    reply = await _ollama(handler).chat(CONVERSATION, TOOLS)

    assert reply == AssistantMessage(content="Hi")
    [request] = seen
    assert request.url == "http://ollama.test/api/chat"
    body = json.loads(request.content)
    assert body["model"] == "qwen2.5:7b-instruct"
    assert body["stream"] is False
    assert body["tools"] == TOOLS
    assert body["messages"] == [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "What is the invoice total?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "search_documents", "arguments": {"query": "invoice"}}}
            ],
        },
        {"role": "tool", "content": "<<<DOCUMENT [1]>>>", "tool_name": "search_documents"},
    ]


async def test_ollama_chat_parses_tool_calls_including_string_and_broken_arguments() -> None:
    """Object and JSON-string arguments parse; unparseable ones become ``None``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "search_documents", "arguments": {"query": "a"}}},
                        {"function": {"name": "search_documents", "arguments": '{"query": "b"}'}},
                        {"function": {"name": "search_documents", "arguments": "{not json"}},
                        {"function": {"name": "search_documents", "arguments": "[1, 2]"}},
                        {"function": {"name": "search_documents"}},
                        "garbage",
                    ],
                }
            },
        )

    reply = await _ollama(handler).chat(CONVERSATION, TOOLS)

    assert reply.content == ""
    assert [(call.name, call.arguments) for call in reply.tool_calls] == [
        ("search_documents", {"query": "a"}),
        ("search_documents", {"query": "b"}),
        ("search_documents", None),
        ("search_documents", None),
        ("search_documents", {}),
        ("", None),
    ]


async def test_ollama_stream_yields_deltas_then_the_complete_message() -> None:
    """NDJSON chunks become text deltas plus one final assembled message."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=_ndjson(
                {"message": {"role": "assistant", "content": "The total "}, "done": False},
                {"message": {"role": "assistant", "content": ""}, "done": False},
                {"message": {"role": "assistant", "content": "is 100 [1]."}, "done": False},
                {"message": {"role": "assistant", "content": ""}, "done": True},
            ),
        )

    items = [item async for item in _ollama(handler).stream_chat(CONVERSATION, TOOLS)]

    assert seen[0]["stream"] is True
    assert items == [
        TextDelta("The total "),
        TextDelta("is 100 [1]."),
        AssistantMessage(content="The total is 100 [1]."),
    ]


async def test_ollama_stream_collects_streamed_tool_calls() -> None:
    """Tool calls delivered in a stream chunk end up on the final message."""

    def handler(request: httpx.Request) -> httpx.Response:
        call = {"function": {"name": "search_documents", "arguments": {"query": "x"}}}
        return httpx.Response(
            200,
            content=_ndjson(
                {"message": {"role": "assistant", "content": "", "tool_calls": [call]}},
                {"message": {"role": "assistant", "content": ""}, "done": True},
            ),
        )

    items = [item async for item in _ollama(handler).stream_chat(CONVERSATION, TOOLS)]

    assert items == [
        AssistantMessage(
            content="", tool_calls=(ToolCall(name="search_documents", arguments={"query": "x"}),)
        )
    ]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"error": "model crashed"}),
        httpx.Response(404, json={"error": "model not found"}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"message": "not an object"}),
        httpx.Response(200, json={"message": {"role": "assistant", "content": 42}}),
    ],
    ids=["server-error", "missing-model", "invalid-json", "bad-message", "bad-content"],
)
async def test_ollama_chat_failures_raise_llm_error(response: httpx.Response) -> None:
    """HTTP errors and unusable payloads are reported as ``LLMError``."""
    with pytest.raises(LLMError):
        await _ollama(lambda request: response).chat(CONVERSATION, TOOLS)


async def test_ollama_transport_failure_raises_llm_error() -> None:
    """A connection failure is an ``LLMError``, not a raw httpx exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(LLMError):
        await _ollama(handler).chat(CONVERSATION, TOOLS)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, content=b""),
        httpx.Response(200, content=_ndjson({"message": {"content": "partial"}, "done": False})),
        httpx.Response(200, content=_ndjson({"error": "out of memory"})),
        httpx.Response(200, content=b"{broken\n"),
    ],
    ids=["http-error", "no-done-chunk", "error-chunk", "invalid-line"],
)
async def test_ollama_stream_failures_raise_llm_error(response: httpx.Response) -> None:
    """A failed, truncated, or corrupt stream is reported as ``LLMError``."""
    provider = _ollama(lambda request: response)

    with pytest.raises(LLMError):
        async for _ in provider.stream_chat(CONVERSATION, TOOLS):
            pass


# --- OpenAI-compatible (opt-in external provider) ---------------------------


async def test_openai_chat_request_shape() -> None:
    """URL, bearer header, model, tools, and id-threaded tool messages."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion({"role": "assistant", "content": "Hi"}))

    reply = await _openai(handler).chat(CONVERSATION, TOOLS)

    assert reply == AssistantMessage(content="Hi")
    [request] = seen
    assert request.url == "https://llm.test/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    body = json.loads(request.content)
    assert body["model"] == "grok-test"
    assert body["stream"] is False
    assert body["tools"] == TOOLS
    system, user, assistant, tool = body["messages"]
    assert system == {"role": "system", "content": "policy"}
    assert user == {"role": "user", "content": "What is the invoice total?"}
    [call] = assistant["tool_calls"]
    assert (call["id"], call["type"], call["function"]["name"]) == (
        "c1",
        "function",
        "search_documents",
    )
    assert json.loads(call["function"]["arguments"]) == {"query": "invoice"}
    assert tool == {"role": "tool", "tool_call_id": "c1", "content": "<<<DOCUMENT [1]>>>"}


async def test_openai_chat_omits_an_empty_tool_list() -> None:
    """OpenAI-compatible APIs reject ``tools: []``; no tools means no key."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_completion({"role": "assistant", "content": "ok"}))

    await _openai(handler).chat(CONVERSATION[:2], [])

    assert "tools" not in seen[0]


async def test_openai_chat_parses_tool_calls_with_string_arguments() -> None:
    """String arguments parse defensively; malformed JSON yields ``None``."""

    def handler(request: httpx.Request) -> httpx.Response:
        def call(call_id: str, arguments: str) -> dict:
            return {
                "id": call_id,
                "type": "function",
                "function": {"name": "search_documents", "arguments": arguments},
            }

        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                call("a", '{"query": "invoice"}'),
                call("b", ""),
                call("c", '{"query": '),
                call("d", '"just a string"'),
            ],
        }
        return httpx.Response(200, json=_completion(message))

    reply = await _openai(handler).chat(CONVERSATION, TOOLS)

    assert reply.content == ""
    assert [(call.id, call.arguments) for call in reply.tool_calls] == [
        ("a", {"query": "invoice"}),
        ("b", {}),
        ("c", None),
        ("d", None),
    ]


async def test_openai_stream_accumulates_text_and_tool_call_deltas() -> None:
    """SSE deltas: text streams out; tool-call fragments merge by index."""
    seen: list[dict] = []

    def delta(**fields) -> dict:
        return {"choices": [{"index": 0, "delta": fields}]}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                delta(role="assistant", content="Let me "),
                delta(content="check."),
                delta(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call_a",
                            "type": "function",
                            "function": {"name": "search_documents", "arguments": ""},
                        }
                    ]
                ),
                delta(tool_calls=[{"index": 0, "function": {"arguments": '{"query":'}}]),
                delta(
                    tool_calls=[
                        {
                            "index": 1,
                            "id": "call_b",
                            "function": {"name": "search_documents", "arguments": "{bad"},
                        }
                    ]
                ),
                delta(tool_calls=[{"index": 0, "function": {"arguments": ' "total"}'}}]),
                {"choices": []},
                "[DONE]",
            ),
        )

    items = [item async for item in _openai(handler).stream_chat(CONVERSATION, TOOLS)]

    assert seen[0]["stream"] is True
    assert items[:2] == [TextDelta("Let me "), TextDelta("check.")]
    assert items[2] == AssistantMessage(
        content="Let me check.",
        tool_calls=(
            ToolCall(name="search_documents", arguments={"query": "total"}, id="call_a"),
            ToolCall(name="search_documents", arguments=None, id="call_b"),
        ),
    )
    assert len(items) == 3


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": {"message": "invalid api key"}}),
        httpx.Response(429, json={"error": {"message": "rate limited"}}),
        httpx.Response(500, json={"error": {"message": "boom"}}),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"error": {"message": "bad request"}}),
        httpx.Response(200, content=b"<html>proxy error</html>"),
    ],
    ids=["unauthorized", "rate-limited", "server-error", "no-choices", "error-body", "not-json"],
)
async def test_openai_chat_failures_raise_llm_error_without_the_key(
    response: httpx.Response, caplog: pytest.LogCaptureFixture
) -> None:
    """Every failure is an ``LLMError`` whose message never carries the key."""
    caplog.set_level(logging.DEBUG)

    with pytest.raises(LLMError) as raised:
        await _openai(lambda request: response).chat(CONVERSATION, TOOLS)

    assert API_KEY not in str(raised.value)
    assert API_KEY not in repr(raised.value)
    assert API_KEY not in caplog.text


async def test_openai_timeout_raises_llm_error() -> None:
    """A read timeout is mapped to ``LLMError``."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(LLMError, match="timed out"):
        await _openai(handler).chat(CONVERSATION, TOOLS)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, content=b""),
        httpx.Response(200, content=_sse({"choices": [{"index": 0, "delta": {"content": "x"}}]})),
        httpx.Response(200, content=_sse({"error": {"message": "overloaded"}})),
        httpx.Response(200, content=b"data: {broken\n\n"),
    ],
    ids=["http-error", "no-done-marker", "error-chunk", "invalid-chunk"],
)
async def test_openai_stream_failures_raise_llm_error(response: httpx.Response) -> None:
    """A failed, truncated, or corrupt SSE stream is reported as ``LLMError``."""
    provider = _openai(lambda request: response)

    with pytest.raises(LLMError):
        async for _ in provider.stream_chat(CONVERSATION, TOOLS):
            pass


#: A provider error body (Groq's shape) whose free text echoes user content.
TOOL_USE_FAILED = {
    "error": {
        "message": "Tool call validation failed: send_email to ana@example.com SECRET-7f3a",
        "type": "invalid_request_error",
        "code": "tool_use_failed",
        "failed_generation": '{"name": "send_email", "arguments": {"body": "SECRET-7f3a"}}',
    }
}
LEAKS = ("SECRET-7f3a", "ana@example.com", "send_email", "validation", "failed_generation")


def _streamed(status: int, body: object) -> httpx.Response:
    """A response whose body is unread until consumed, as over a real network."""

    async def chunks():
        yield json.dumps(body).encode()

    return httpx.Response(status, headers={"content-type": "application/json"}, content=chunks())


async def _stream_error(provider) -> str:
    with pytest.raises(LLMError) as raised:
        async for _ in provider.stream_chat(CONVERSATION, TOOLS):
            pass
    return str(raised.value)


async def test_openai_http_error_names_the_provider_error_type_and_code_only() -> None:
    """The provider's error type and code reach the error text; its message never does."""
    with pytest.raises(LLMError) as raised:
        await _openai(lambda request: _streamed(400, TOOL_USE_FAILED)).chat(CONVERSATION, [])

    text = str(raised.value)
    assert text == (
        "chat completions request failed: request failed "
        "(HTTP 400, type=invalid_request_error, code=tool_use_failed)"
    )
    assert not any(leak in text for leak in LEAKS)


async def test_openai_stream_http_error_names_the_provider_error_type_and_code_only() -> None:
    """A streamed request reads the error body first, so its type and code are kept too."""
    text = await _stream_error(_openai(lambda request: _streamed(400, TOOL_USE_FAILED)))

    assert text == (
        "chat completions stream failed: request failed "
        "(HTTP 400, type=invalid_request_error, code=tool_use_failed)"
    )
    assert not any(leak in text for leak in LEAKS)


async def test_openai_http_error_keeps_the_status_hint_next_to_the_error_code() -> None:
    body = {"error": {"message": "Invalid API Key", "type": "invalid_request_error"}}

    with pytest.raises(LLMError) as raised:
        await _openai(lambda request: _streamed(401, body)).chat(CONVERSATION, TOOLS)

    assert str(raised.value) == (
        "chat completions request failed: authentication failed "
        "(HTTP 401, type=invalid_request_error)"
    )


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"message": "x", "type": "has spaces SECRET-7f3a", "code": "x" * 65}},
        {"error": {"type": "invalid_request_error\nSECRET-7f3a", "code": ""}},
        {"error": {"type": 7, "code": ["tool_use_failed"]}},
        {"error": "SECRET-7f3a in a plain string"},
        {"detail": {"type": "invalid_request_error"}},
        ["SECRET-7f3a"],
    ],
    ids=["unsafe-values", "newline-and-empty", "not-strings", "string-error", "no-error", "list"],
)
async def test_openai_http_error_drops_unsafe_error_fields(body: object) -> None:
    """Anything but a short identifier-like string is left out, never echoed."""
    with pytest.raises(LLMError) as raised:
        await _openai(lambda request: _streamed(400, body)).chat(CONVERSATION, TOOLS)

    assert str(raised.value) == "chat completions request failed: request failed (HTTP 400)"


async def test_openai_http_error_with_a_non_json_body_names_only_the_status() -> None:
    response = httpx.Response(502, content=b"<html>SECRET-7f3a bad gateway</html>")

    text = await _stream_error(_openai(lambda request: response))

    assert text == "chat completions stream failed: request failed (HTTP 502)"


async def test_ollama_http_errors_use_the_same_safe_error_detail() -> None:
    """Ollama's plain-text error never shows; a JSON error object gives its type and code."""
    plain = {"error": "model 'SECRET-7f3a' not found"}
    typed = {"error": {"message": "SECRET-7f3a", "type": "api_error", "code": "bad_model"}}

    with pytest.raises(LLMError) as raised:
        await _ollama(lambda request: _streamed(404, plain)).chat(CONVERSATION, TOOLS)
    streamed = await _stream_error(_ollama(lambda request: _streamed(400, typed)))

    assert str(raised.value) == "ollama chat failed: endpoint or model not found (HTTP 404)"
    assert streamed == (
        "ollama chat stream failed: request failed (HTTP 400, type=api_error, code=bad_model)"
    )


def test_openai_provider_never_shows_its_key() -> None:
    """``repr`` and attribute dumps of the provider keep the key masked."""
    provider = _openai(lambda request: httpx.Response(200))

    assert API_KEY not in repr(provider)
    assert API_KEY not in str(vars(provider))


# --- Fake -------------------------------------------------------------------


async def test_fake_provider_replays_its_script_and_records_calls() -> None:
    """Scripted replies run in order; callables see the conversation so far."""
    provider = FakeLLMProvider(
        [
            AssistantMessage(content="", tool_calls=(ToolCall("search_documents", {}),)),
            lambda messages, tools: AssistantMessage(content=f"{len(messages)} messages"),
        ]
    )

    first = await provider.chat(CONVERSATION[:2], TOOLS)
    second = await provider.chat(CONVERSATION, TOOLS)

    assert first.tool_calls[0].name == "search_documents"
    assert second.content == "4 messages"
    assert [len(call.messages) for call in provider.calls] == [2, 4]
    assert provider.calls[0].tools == TOOLS
    with pytest.raises(LLMError):
        await provider.chat(CONVERSATION, TOOLS)


async def test_fake_provider_streams_its_reply_in_pieces() -> None:
    """The fake stream re-assembles exactly into the scripted reply."""
    provider = FakeLLMProvider([AssistantMessage(content="The total is 100 [1].")])

    items = [item async for item in provider.stream_chat(CONVERSATION, TOOLS)]

    deltas = [item.text for item in items if isinstance(item, TextDelta)]
    assert len(deltas) > 1
    assert "".join(deltas) == "The total is 100 [1]."
    assert items[-1] == AssistantMessage(content="The total is 100 [1].")


# --- Factory and privacy guard ----------------------------------------------


def test_build_llm_provider_selects_the_adapter() -> None:
    """``llm_provider`` picks the adapter; the Ollama one reuses ``ollama_base_url``."""
    assert isinstance(build_llm_provider(Settings(llm_provider="fake")), FakeLLMProvider)
    ollama = build_llm_provider(
        Settings(llm_provider="ollama", ollama_base_url="http://gpu:11434", llm_model="m")
    )
    assert isinstance(ollama, OllamaChatProvider)
    assert (ollama.base_url, ollama.model) == ("http://gpu:11434", "m")
    with pytest.raises(LLMConfigurationError, match="llm_provider"):
        build_llm_provider(Settings(llm_provider="openai"))


def test_settings_defaults_keep_inference_local() -> None:
    """Defaults: local Ollama, a tool-calling model, no external inference."""
    settings = Settings()

    assert settings.llm_model == "qwen2.5:7b-instruct"
    assert settings.agent_max_steps == 6
    assert settings.llm_timeout_seconds > 0
    assert settings.allow_external_llm is False
    assert settings.llm_api_key is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"llm_api_key": API_KEY}, "allow_external_llm"),
        ({"allow_external_llm": True}, "llm_api_key"),
        ({"allow_external_llm": True, "llm_api_key": ""}, "llm_api_key"),
        ({"allow_external_llm": True, "llm_api_key": API_KEY, "llm_base_url": ""}, "llm_base_url"),
    ],
    ids=["not-allowed", "no-key", "empty-key", "no-base-url"],
)
def test_external_provider_fails_fast_without_explicit_opt_in(
    overrides: dict, message: str
) -> None:
    """Selecting the external provider needs the opt-in flag, a key, and a URL."""
    settings = Settings(
        **{"llm_provider": "openai_compatible", "llm_base_url": "https://llm.test/v1", **overrides}
    )

    with pytest.raises(LLMConfigurationError, match=message) as raised:
        build_llm_provider(settings)

    assert API_KEY not in str(raised.value)


def test_app_startup_refuses_an_unapproved_external_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``create_app`` fails fast instead of silently sending data out."""
    from app.main import create_app

    monkeypatch.setenv("AA_LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("AA_LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("AA_LLM_API_KEY", API_KEY)

    with pytest.raises(LLMConfigurationError, match="allow_external_llm"):
        create_app()


def test_enabled_external_provider_warns_once_without_the_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Opting in logs one WARNING that data leaves the machine; the key stays secret."""
    settings = Settings(
        llm_provider="openai_compatible",
        llm_base_url="https://api.x.ai/v1",
        llm_model="grok-test",
        llm_api_key=API_KEY,
        allow_external_llm=True,
    )

    with caplog.at_level(logging.DEBUG):
        provider = build_llm_provider(settings)

    assert isinstance(provider, OpenAICompatibleChatProvider)
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "api.x.ai" in warnings[0].getMessage()
    assert "leave" in warnings[0].getMessage()
    assert API_KEY not in caplog.text
    assert API_KEY not in repr(settings)
