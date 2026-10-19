"""Console chat client: login, SSE parsing, rendering, REPL history, and CLI.

Every test scripts the API with ``httpx.MockTransport``; no server runs.
"""

import io
import json
from collections.abc import Callable

import httpx
import pytest

from app.demo.chat import ChatClient, ChatClientError, iter_sse, main, repl, run_turn

APPROVAL_ID = "6f1c2a9e-3d4b-4c8a-9e1f-0a2b3c4d5e6f"
DOCUMENT_ID = "0b6d3f5e-1a2c-4e8f-9a0b-1c2d3e4f5a6b"
CHUNK_ID = "9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c6b"
TOKEN = "header.payload.signature"


def _frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


CITATION = {
    "number": 1,
    "document_id": DOCUMENT_ID,
    "document_filename": "factura_TIS-2026-0150_transporte.txt",
    "chunk_id": CHUNK_ID,
    "chunk_index": 0,
    "start_offset": 0,
    "end_offset": 640,
    "content": "FACTURA TIS-2026-0150 ...",
}


def _done(answer: str) -> dict:
    return {
        "run_id": "run-1",
        "answer": answer,
        "stop_reason": "completed",
        "citations": [CITATION],
        "sources": [CITATION],
        "pending_approvals": [
            {
                "approval_id": APPROVAL_ID,
                "tool_name": "send_note",
                "expires_at": "2026-10-14T10:00:00Z",
            }
        ],
        "tool_calls": [],
    }


ANSWER = "La factura TIS-2026-0150 suma 1.452,00 € [1]."
SCRIPTED_STREAM = "".join(
    [
        ": keep-alive comment\n\n",
        _frame("run_started", {"run_id": "run-1"}),
        _frame("tool_call", {"call_id": "1.1", "tool_name": "search_documents"}),
        _frame(
            "tool_result",
            {
                "call_id": "1.1",
                "tool_name": "search_documents",
                "status": "ok",
                "error": None,
                "approval_id": None,
            },
        ),
        _frame("tool_call", {"call_id": "2.1", "tool_name": "send_note"}),
        _frame(
            "approval_required",
            {
                "approval_id": APPROVAL_ID,
                "tool_name": "send_note",
                "expires_at": "2026-10-14T10:00:00Z",
            },
        ),
        _frame(
            "tool_result",
            {
                "call_id": "2.1",
                "tool_name": "send_note",
                "status": "approval_required",
                "error": None,
                "approval_id": APPROVAL_ID,
            },
        ),
        _frame("token", {"text": "La factura TIS-2026-0150 "}),
        _frame("token", {"text": "suma 1.452,00 € [1]."}),
        _frame("citations", {"citations": [CITATION], "sources": [CITATION]}),
        _frame("done", _done(ANSWER)),
    ]
)


class FakeApi:
    """Scripted API: records requests and answers login and chat stream calls."""

    def __init__(self, stream_body: str | Callable[[int], str] = SCRIPTED_STREAM) -> None:
        self.stream_body = stream_body
        self.logins: list[dict] = []
        self.chats: list[tuple[dict, str | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/api/v1/auth/login":
            self.logins.append(body)
            return httpx.Response(
                200, json={"access_token": TOKEN, "user": {"role": "viewer", "tenant_id": 1}}
            )
        if request.url.path == "/api/v1/chat/stream":
            self.chats.append((body, request.headers.get("authorization")))
            text = self.stream_body
            if callable(text):
                text = text(len(self.chats))
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        return httpx.Response(404, json={"detail": "not found"})


def _client(api: Callable[[httpx.Request], httpx.Response]) -> ChatClient:
    http = httpx.Client(transport=httpx.MockTransport(api), base_url="http://api.test")
    return ChatClient(http)


def test_iter_sse_parses_typed_frames_and_skips_comments() -> None:
    events = list(iter_sse(SCRIPTED_STREAM.splitlines()))

    assert [event.type for event in events] == [
        "run_started",
        "tool_call",
        "tool_result",
        "tool_call",
        "approval_required",
        "tool_result",
        "token",
        "token",
        "citations",
        "done",
    ]
    assert events[-1].data["answer"] == ANSWER


def test_turn_renders_tokens_tool_calls_approvals_and_citations() -> None:
    api = FakeApi()
    client = _client(api)
    client.login("viewer@demo.test", "demo-viewer-pass", "demo")
    out = io.StringIO()

    answer = run_turn(client, "¿Cuánto suma la factura de transporte?", [], out)

    rendered = out.getvalue()
    assert answer == ANSWER
    assert ANSWER in rendered  # tokens are written inline, in order
    assert "[tool] search_documents" in rendered
    assert "[tool] send_note -> approval_required" in rendered
    assert f"[approval required] send_note: approval id {APPROVAL_ID}" in rendered
    assert f"/api/v1/approvals/{APPROVAL_ID}/approve" in rendered
    assert "[1] factura_TIS-2026-0150_transporte.txt (chars 0-640)" in rendered
    assert rendered.index("[1] factura_TIS") > rendered.index(ANSWER)
    body, authorization = api.chats[0]
    assert authorization == f"Bearer {TOKEN}"
    assert body == {"message": "¿Cuánto suma la factura de transporte?", "history": []}


def test_turn_prints_the_final_answer_when_no_tokens_were_streamed() -> None:
    client = _client(
        FakeApi(_frame("run_started", {"run_id": "r"}) + _frame("done", _done(ANSWER)))
    )
    client.login("owner@demo.test", "demo-owner-pass", "demo")
    out = io.StringIO()

    assert run_turn(client, "hola", [], out) == ANSWER
    assert ANSWER in out.getvalue()


def test_turn_renders_a_stream_error_event() -> None:
    stream = _frame("run_started", {"run_id": "r"}) + _frame(
        "error", {"message": "language model unavailable"}
    )
    client = _client(FakeApi(stream))
    client.login("owner@demo.test", "demo-owner-pass", "demo")
    out = io.StringIO()

    assert run_turn(client, "hola", [], out) is None
    assert "[error] language model unavailable" in out.getvalue()


def test_login_with_wrong_credentials_raises_a_client_error() -> None:
    client = _client(lambda request: httpx.Response(401, json={"detail": "invalid credentials"}))

    with pytest.raises(ChatClientError, match="invalid credentials"):
        client.login("owner@demo.test", "wrong", "demo")


def test_chat_http_error_raises_a_client_error() -> None:
    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(200, json={"access_token": TOKEN, "user": {}})
        return httpx.Response(422, json={"detail": "message too long"})

    client = _client(api)
    client.login("owner@demo.test", "demo-owner-pass", "demo")

    with pytest.raises(ChatClientError, match="422"):
        run_turn(client, "x", [], io.StringIO())


def test_repl_keeps_a_short_history_and_supports_reset() -> None:
    api = FakeApi(lambda turn: _frame("done", _done(f"respuesta {turn}")))
    client = _client(api)
    client.login("owner@demo.test", "demo-owner-pass", "demo")
    inputs = iter(["uno", "dos", "", "tres", "/reset", "cuatro", "/quit", "never sent"])

    repl(client, read=lambda prompt: next(inputs), out=io.StringIO(), history_limit=2)

    histories = [body["history"] for body, _ in api.chats]
    assert [body["message"] for body, _ in api.chats] == ["uno", "dos", "tres", "cuatro"]
    assert histories[0] == []
    assert histories[1] == [
        {"role": "user", "content": "uno"},
        {"role": "assistant", "content": "respuesta 1"},
    ]
    assert histories[2] == [
        {"role": "user", "content": "dos"},
        {"role": "assistant", "content": "respuesta 2"},
    ]
    assert histories[3] == []


def test_repl_stops_on_end_of_input() -> None:
    client = _client(FakeApi())

    def read(prompt: str) -> str:
        raise EOFError

    repl(client, read=read, out=io.StringIO())


def test_main_one_shot_logs_in_as_the_chosen_demo_role(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AA_DEMO_PASSWORD", raising=False)
    api = FakeApi()

    exit_code = main(["--user", "viewer", "-m", "hola"], transport=httpx.MockTransport(api))

    assert exit_code == 0
    assert api.logins == [
        {"email": "viewer@demo.test", "password": "demo-viewer-pass", "tenant_slug": "demo"}
    ]
    assert ANSWER in capsys.readouterr().out


def test_main_reports_an_unreachable_api(capsys: pytest.CaptureFixture[str]) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    exit_code = main(["-m", "hola"], transport=httpx.MockTransport(refuse))

    assert exit_code == 1
    assert "cannot reach" in capsys.readouterr().err
