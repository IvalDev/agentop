"""Chat model abstraction (decision A5: local inference behind an adapter).

The agent loop only talks to :class:`LLMProvider`. Shipped adapters:

- :class:`OllamaChatProvider` — the production default: Ollama's
  ``POST {base_url}/api/chat`` with ``tools``; streaming is NDJSON.
- :class:`OpenAICompatibleChatProvider` — an opt-in development door to an
  external OpenAI-compatible API (``POST {base_url}/chat/completions``,
  bearer key, SSE streaming). Prompts and document content leave the
  machine, so :func:`build_llm_provider` refuses it unless
  ``Settings.allow_external_llm`` is set.
- :class:`FakeLLMProvider` — a deterministic scripted model for tests and
  offline development.

Every backend failure surfaces as :class:`LLMError`, whose message names only
the failure class (HTTP status, timeout, unusable payload) — never request
headers, credentials, or response bodies. Misconfiguration surfaces as
:class:`LLMConfigurationError` when the provider is built (app startup).

``ToolCall.arguments`` is the parsed JSON object, ``{}`` when the model sent
none, or ``None`` when it sent something that is not a JSON object; the agent
loop reports ``None`` back to the model instead of executing anything.
"""

import json
import logging
import re
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from app.core.config import Settings

logger = logging.getLogger(__name__)

Role = Literal["system", "user", "assistant", "tool"]
ToolSchema = dict[str, Any]

#: Reply of the ``fake`` provider when the app runs without a real model.
OFFLINE_REPLY = "The assistant is running with the offline fake model; no answer is available."

_STATUS_HINTS = {
    401: "authentication failed",
    403: "access denied",
    404: "endpoint or model not found",
    429: "rate limited",
}


class LLMError(Exception):
    """The chat backend failed or returned output the agent cannot use."""


class LLMConfigurationError(ValueError):
    """The configured chat provider cannot be built (raised at startup)."""


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation requested by the model (``id`` may be empty)."""

    name: str
    arguments: dict[str, Any] | None
    id: str = ""


@dataclass(frozen=True)
class ChatMessage:
    """One conversation turn sent to the model.

    ``tool_calls`` is set on assistant turns that requested tools; tool
    turns carry the tool's ``tool_name`` (Ollama) and ``tool_call_id``
    (OpenAI-compatible APIs thread results by id).
    """

    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_name: str | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True)
class AssistantMessage:
    """The model's complete reply: text and/or tool calls."""

    content: str
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class TextDelta:
    """A piece of reply text, streamed before the complete message."""

    text: str


StreamItem = TextDelta | AssistantMessage


class LLMProvider(Protocol):
    """Chat model with tool calling, in one-shot and streaming flavors.

    ``stream_chat`` yields zero or more :class:`TextDelta` and then exactly
    one :class:`AssistantMessage` (the complete reply), or raises
    :class:`LLMError`.
    """

    async def chat(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSchema]
    ) -> AssistantMessage: ...

    def stream_chat(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSchema]
    ) -> AsyncIterator[StreamItem]: ...


# --- Parsing shared by the HTTP adapters ---------------------------------------


def _parse_arguments(raw: Any) -> dict[str, Any] | None:
    """JSON-object arguments as a dict; missing/blank → ``{}``; anything else → ``None``."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _parse_tool_call(raw: Any) -> ToolCall:
    """Map one provider tool call; malformed entries keep the loop informed."""
    if not isinstance(raw, dict):
        return ToolCall(name="", arguments=None)
    call_id = raw.get("id") if isinstance(raw.get("id"), str) else ""
    function = raw.get("function")
    if not isinstance(function, dict):
        return ToolCall(name="", arguments=None, id=call_id)
    name = function.get("name")
    return ToolCall(
        name=name if isinstance(name, str) else "",
        arguments=_parse_arguments(function.get("arguments")),
        id=call_id,
    )


def _assistant_message(raw: Any) -> AssistantMessage:
    """Map a provider message object (Ollama and OpenAI share this shape)."""
    if not isinstance(raw, dict):
        raise LLMError("model response has no message object")
    content = raw.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise LLMError("model message content is not text")
    calls = raw.get("tool_calls") or []
    if not isinstance(calls, list):
        raise LLMError("model tool_calls is not a list")
    return AssistantMessage(content=content, tool_calls=tuple(map(_parse_tool_call, calls)))


def _failure(operation: str, exc: Exception) -> LLMError:
    """Typed error naming only the failure class (no headers, keys, or bodies)."""
    if isinstance(exc, httpx.TimeoutException):
        return LLMError(f"{operation} timed out")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        hint = _STATUS_HINTS.get(status, "request failed")
        return LLMError(f"{operation} failed: {hint} (HTTP {status})")
    if isinstance(exc, httpx.HTTPError):
        return LLMError(f"{operation} failed: {type(exc).__name__}")
    return LLMError(f"{operation} returned an unusable response ({type(exc).__name__})")


# --- Ollama -----------------------------------------------------------------------


def _ollama_message(message: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {"function": {"name": call.name, "arguments": call.arguments or {}}}
            for call in message.tool_calls
        ]
    if message.role == "tool" and message.tool_name is not None:
        payload["tool_name"] = message.tool_name
    return payload


class OllamaChatProvider:
    """HTTP adapter for Ollama's ``POST {base_url}/api/chat`` (decision A5).

    ``transport`` is a test seam for ``httpx.MockTransport``.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self._timeout = timeout
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url, timeout=self._timeout, transport=self._transport
        )

    def _payload(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSchema], *, stream: bool
    ) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [_ollama_message(message) for message in messages],
            "tools": list(tools),
            "stream": stream,
        }

    async def chat(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSchema]
    ) -> AssistantMessage:
        try:
            async with self._client() as client:
                response = await client.post(
                    "/api/chat", json=self._payload(messages, tools, stream=False)
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise _failure("ollama chat", exc) from exc
        if not isinstance(body, dict) or "error" in body:
            raise LLMError("ollama chat returned an error")
        return _assistant_message(body.get("message"))

    async def stream_chat(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSchema]
    ) -> AsyncIterator[StreamItem]:
        parts: list[str] = []
        calls: list[ToolCall] = []
        payload = self._payload(messages, tools, stream=True)
        try:
            async with (
                self._client() as client,
                client.stream("POST", "/api/chat", json=payload) as response,
            ):
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if not isinstance(chunk, dict) or "error" in chunk:
                        raise LLMError("ollama chat stream reported an error")
                    message = _assistant_message(chunk.get("message", {}))
                    if message.content:
                        parts.append(message.content)
                        yield TextDelta(message.content)
                    calls.extend(message.tool_calls)
                    if chunk.get("done") is True:
                        yield AssistantMessage(content="".join(parts), tool_calls=tuple(calls))
                        return
        except (httpx.HTTPError, ValueError) as exc:
            raise _failure("ollama chat stream", exc) from exc
        raise LLMError("ollama chat stream ended before completion")


# --- OpenAI-compatible (external, opt-in) ------------------------------------------


def _openai_message(message: ChatMessage) -> dict[str, Any]:
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id or "",
            "content": message.content,
        }
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments or {})},
            }
            for call in message.tool_calls
        ]
    return payload


@dataclass
class _PartialToolCall:
    """Tool call assembled from streamed fragments sharing one ``index``."""

    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)

    def merge(self, fragment: dict[str, Any]) -> None:
        if isinstance(fragment.get("id"), str) and fragment["id"]:
            self.id = fragment["id"]
        function = fragment.get("function")
        if isinstance(function, dict):
            if isinstance(function.get("name"), str):
                self.name += function["name"]
            if isinstance(function.get("arguments"), str):
                self.arguments.append(function["arguments"])

    def build(self) -> ToolCall:
        return ToolCall(
            name=self.name, arguments=_parse_arguments("".join(self.arguments)), id=self.id
        )


class OpenAICompatibleChatProvider:
    """HTTP adapter for an OpenAI-compatible ``POST {base_url}/chat/completions``.

    Development-only door to external inference (for example xAI Grok at
    ``https://api.x.ai/v1``). The key is held as a :class:`SecretStr`, sent
    only in the ``Authorization`` header, and kept out of ``repr`` and every
    error message. ``transport`` is a test seam for ``httpx.MockTransport``.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: SecretStr,
        timeout: float,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url.strip():
            raise LLMConfigurationError("openai_compatible provider needs llm_base_url")
        if not api_key.get_secret_value().strip():
            raise LLMConfigurationError("openai_compatible provider needs llm_api_key")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self.base_url!r}, model={self.model!r})"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self._timeout,
            transport=self._transport,
            headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
        )

    def _payload(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSchema], *, stream: bool
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_openai_message(message) for message in messages],
            "stream": stream,
        }
        if tools:  # these APIs reject an empty tools array
            payload["tools"] = list(tools)
        return payload

    async def chat(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSchema]
    ) -> AssistantMessage:
        try:
            async with self._client() as client:
                response = await client.post(
                    "/chat/completions", json=self._payload(messages, tools, stream=False)
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise _failure("chat completions request", exc) from exc
        if not isinstance(body, dict) or "error" in body:
            raise LLMError("chat completions request returned an error")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise LLMError("chat completions response has no choices")
        return _assistant_message(choices[0].get("message"))

    async def stream_chat(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSchema]
    ) -> AsyncIterator[StreamItem]:
        parts: list[str] = []
        calls: dict[int, _PartialToolCall] = {}
        payload = self._payload(messages, tools, stream=True)
        try:
            async with (
                self._client() as client,
                client.stream("POST", "/chat/completions", json=payload) as response,
            ):
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue  # blank separators, comments, other SSE fields
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        ordered = tuple(calls[index].build() for index in sorted(calls))
                        yield AssistantMessage(content="".join(parts), tool_calls=ordered)
                        return
                    delta = _stream_delta(json.loads(data))
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        parts.append(text)
                        yield TextDelta(text)
                    for fragment in delta.get("tool_calls") or []:
                        if not isinstance(fragment, dict) or not isinstance(
                            fragment.get("index", 0), int
                        ):
                            raise LLMError("chat completions stream sent a malformed tool call")
                        calls.setdefault(fragment.get("index", 0), _PartialToolCall()).merge(
                            fragment
                        )
        except (httpx.HTTPError, ValueError) as exc:
            raise _failure("chat completions stream", exc) from exc
        raise LLMError("chat completions stream ended before [DONE]")


def _stream_delta(chunk: Any) -> dict[str, Any]:
    """The first choice's ``delta`` of one SSE chunk (``{}`` for empty chunks)."""
    if not isinstance(chunk, dict) or "error" in chunk:
        raise LLMError("chat completions stream reported an error")
    choices = chunk.get("choices") or []
    if not isinstance(choices, list):
        raise LLMError("chat completions stream sent malformed choices")
    if not choices or not isinstance(choices[0], dict):
        return {}
    delta = choices[0].get("delta")
    return delta if isinstance(delta, dict) else {}


# --- Fake -----------------------------------------------------------------------------

Reply = AssistantMessage | Callable[[list[ChatMessage], list[ToolSchema]], AssistantMessage]


@dataclass(frozen=True)
class RecordedCall:
    """What the fake model received on one call."""

    messages: list[ChatMessage]
    tools: list[ToolSchema]


class FakeLLMProvider:
    """Deterministic scripted model for tests and offline development.

    Each call consumes the next reply of the script: an
    :class:`AssistantMessage`, or a callable receiving the conversation and
    tools (to assert on them or answer from them). An exhausted script uses
    ``fallback`` when given and raises :class:`LLMError` otherwise.
    """

    def __init__(self, replies: Iterable[Reply] = (), *, fallback: Reply | None = None) -> None:
        self._replies: deque[Reply] = deque(replies)
        self._fallback = fallback
        self.calls: list[RecordedCall] = []

    async def chat(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSchema]
    ) -> AssistantMessage:
        recorded = RecordedCall(messages=list(messages), tools=list(tools))
        self.calls.append(recorded)
        if self._replies:
            reply = self._replies.popleft()
        elif self._fallback is not None:
            reply = self._fallback
        else:
            raise LLMError("fake LLM script exhausted")
        if isinstance(reply, AssistantMessage):
            return reply
        return reply(recorded.messages, recorded.tools)

    async def stream_chat(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSchema]
    ) -> AsyncIterator[StreamItem]:
        reply = await self.chat(messages, tools)
        for piece in re.findall(r"\S+\s*|\s+", reply.content):
            yield TextDelta(piece)
        yield reply


# --- Factory -------------------------------------------------------------------------


def build_llm_provider(settings: Settings) -> LLMProvider:
    """Construct the adapter named by ``settings.llm_provider`` (fails fast)."""
    if settings.llm_provider == "fake":
        return FakeLLMProvider(fallback=AssistantMessage(content=OFFLINE_REPLY))
    if settings.llm_provider == "ollama":
        return OllamaChatProvider(
            base_url=settings.ollama_base_url,
            model=settings.llm_model,
            timeout=settings.llm_timeout_seconds,
        )
    if settings.llm_provider == "openai_compatible":
        return _build_external_provider(settings)
    raise LLMConfigurationError(
        f"unknown llm_provider {settings.llm_provider!r}; "
        "expected 'ollama', 'fake', or 'openai_compatible'"
    )


def _build_external_provider(settings: Settings) -> OpenAICompatibleChatProvider:
    """The external provider behind the explicit privacy opt-in."""
    if not settings.allow_external_llm:
        raise LLMConfigurationError(
            "llm_provider 'openai_compatible' sends prompts and document content to an "
            "external service; set allow_external_llm=true (AA_ALLOW_EXTERNAL_LLM) to permit it"
        )
    api_key = settings.llm_api_key
    if api_key is None or not api_key.get_secret_value().strip():
        raise LLMConfigurationError(
            "llm_provider 'openai_compatible' requires llm_api_key (AA_LLM_API_KEY)"
        )
    if not settings.llm_base_url.strip():
        raise LLMConfigurationError(
            "llm_provider 'openai_compatible' requires llm_base_url (AA_LLM_BASE_URL)"
        )
    provider = OpenAICompatibleChatProvider(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=api_key,
        timeout=settings.llm_timeout_seconds,
    )
    host = urlsplit(provider.base_url).netloc or provider.base_url
    logger.warning(
        "external LLM provider enabled: prompts and document content will leave this "
        "machine for %s (model %s)",
        host,
        provider.model,
    )
    return provider
