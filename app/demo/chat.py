"""Minimal console chat client for the demo company (development only).

``python -m app.demo.chat [--user owner|admin|operator|viewer] [--base-url URL] [-m MESSAGE]``

Logs in over HTTP against a running API, sends each message to
``POST /api/v1/chat/stream``, and renders the Server-Sent Events: model
tokens inline, tool calls and results, approval-required notices with the
approval id, and the cited passages after the answer. Without ``-m`` it runs
a small REPL (``/reset`` clears the history, ``/quit`` exits) that replays
the last few turns as ``history``. The HTTP client is injectable so tests
script the API with ``httpx.MockTransport``.
"""

import argparse
import itertools
import json
import os
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, TextIO

import httpx

from app.demo.team import DEMO_TENANT_SLUG, PASSWORD_ENV_VAR, demo_user
from packages.contracts.schemas import CHAT_MESSAGE_MAX_CHARS

DEFAULT_BASE_URL = "http://localhost:8000"
#: Prior messages (user + assistant) replayed with each new message.
HISTORY_LIMIT = 10


class ChatClientError(Exception):
    """Login or chat request failed; the message is safe to print."""


@dataclass(frozen=True)
class SSEEvent:
    """One Server-Sent Event: its ``event:`` type and JSON ``data``."""

    type: str
    data: dict[str, Any]


def iter_sse(lines: Iterable[str]) -> Iterator[SSEEvent]:
    """Parse SSE lines (without line endings) into events.

    Only ``event:`` and ``data:`` fields matter; comments (``: ...``) and other
    fields are ignored, and a blank line ends each event.
    """
    event_type = "message"
    data: list[str] = []
    for line in itertools.chain(lines, [""]):  # a final blank line flushes the last event
        if not line:
            if data:
                try:
                    payload = json.loads("\n".join(data))
                except json.JSONDecodeError as exc:
                    raise ChatClientError(f"malformed {event_type!r} event from the API") from exc
                yield SSEEvent(event_type, payload)
            event_type, data = "message", []
        elif line.startswith("event:"):
            event_type = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:") :].removeprefix(" "))


def _error_detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail", "")
    except (ValueError, AttributeError):
        detail = response.text
    return f"HTTP {response.status_code}: {detail}" if detail else f"HTTP {response.status_code}"


class ChatClient:
    """Thin wrapper over the auth and chat-stream endpoints."""

    def __init__(self, http: httpx.Client) -> None:
        self._http = http
        self._token: str | None = None

    def login(self, email: str, password: str, tenant_slug: str) -> dict[str, Any]:
        """Authenticate and keep the bearer token; return the user payload."""
        try:
            response = self._http.post(
                "/api/v1/auth/login",
                json={"email": email, "password": password, "tenant_slug": tenant_slug},
            )
        except httpx.HTTPError as exc:
            raise ChatClientError(f"cannot reach the API at {self._http.base_url}: {exc}") from exc
        if response.status_code != 200:
            raise ChatClientError(f"login failed for {email}: {_error_detail(response)}")
        body = response.json()
        self._token = body["access_token"]
        return body.get("user", {})

    def stream(self, message: str, history: list[dict[str, str]]) -> Iterator[SSEEvent]:
        """Send one message and yield the run's events as they arrive."""
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        payload = {"message": message, "history": history}
        try:
            with self._http.stream(
                "POST", "/api/v1/chat/stream", json=payload, headers=headers
            ) as response:
                if response.status_code != 200:
                    response.read()
                    raise ChatClientError(f"chat request failed: {_error_detail(response)}")
                yield from iter_sse(response.iter_lines())
        except httpx.HTTPError as exc:
            raise ChatClientError(f"chat stream interrupted: {exc}") from exc


@dataclass
class Renderer:
    """Writes one run's events to a console, keeping lines tidy around tokens."""

    out: TextIO
    streamed: bool = False
    answer: str | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    _mid_line: bool = False

    def handle(self, event: SSEEvent) -> None:
        data = event.data
        if event.type == "token":
            self._write(data.get("text", ""))
            self.streamed = True
        elif event.type == "tool_call":
            self._line(f"[tool] {data.get('tool_name')} (call {data.get('call_id')})")
        elif event.type == "tool_result":
            status = data.get("status")
            error = f": {data['error']}" if data.get("error") else ""
            self._line(f"[tool] {data.get('tool_name')} -> {status}{error}")
        elif event.type == "approval_required":
            approval_id = data.get("approval_id")
            self._line(
                f"[approval required] {data.get('tool_name')}: approval id {approval_id} "
                f"(expires {data.get('expires_at')}). Not executed: an authorized user must "
                f"POST /api/v1/approvals/{approval_id}/approve, then .../execute"
            )
        elif event.type == "citations":
            self.citations = list(data.get("citations", []))
        elif event.type == "done":
            self.answer = data.get("answer", "")
            self.citations = list(data.get("citations", self.citations))
            if not self.streamed:
                self._write(self.answer)
        elif event.type == "error":
            self._line(f"[error] {data.get('message', 'unknown error')}")

    def finish(self) -> None:
        """End the answer line and list the cited passages."""
        self._end_line()
        if self.citations:
            self.out.write("Sources:\n")
            for citation in self.citations:
                self.out.write(
                    f"  [{citation.get('number')}] {citation.get('document_filename')} "
                    f"(chars {citation.get('start_offset')}-{citation.get('end_offset')})\n"
                )
        self.out.flush()

    def _write(self, text: str) -> None:
        self.out.write(text)
        self.out.flush()
        if text:
            self._mid_line = not text.endswith("\n")

    def _line(self, text: str) -> None:
        self._end_line()
        self.out.write(text + "\n")

    def _end_line(self) -> None:
        if self._mid_line:
            self.out.write("\n")
            self._mid_line = False


def run_turn(
    client: ChatClient, message: str, history: list[dict[str, str]], out: TextIO
) -> str | None:
    """Stream one message; return the final answer, or ``None`` when the run failed."""
    renderer = Renderer(out)
    try:
        for event in client.stream(message, history):
            renderer.handle(event)
    finally:
        renderer.finish()
    return renderer.answer


def repl(
    client: ChatClient,
    *,
    read: Callable[[str], str] = input,
    out: TextIO = sys.stdout,
    history_limit: int = HISTORY_LIMIT,
) -> None:
    """Interactive loop: ``/reset`` clears the history, ``/quit`` or EOF exits."""
    history: list[dict[str, str]] = []
    while True:
        try:
            message = read("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            out.write("\n")
            return
        if not message:
            continue
        if message in {"/quit", "/exit"}:
            return
        if message == "/reset":
            history.clear()
            out.write("(history cleared)\n")
            continue
        out.write("agent> ")
        try:
            answer = run_turn(client, message, history, out)
        except ChatClientError as exc:
            out.write(f"[error] {exc}\n")
            continue
        if answer:
            history += [
                {"role": "user", "content": message},
                {"role": "assistant", "content": answer[:CHAT_MESSAGE_MAX_CHARS]},
            ]
            del history[:-history_limit]


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.demo.chat",
        description="Chat with the agent as a demo company user (development only).",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--user", default="owner", choices=["owner", "admin", "operator", "viewer"])
    parser.add_argument("--email", help="log in with this email instead of <user>@demo.test")
    parser.add_argument(
        "--password",
        help=f"default: ${PASSWORD_ENV_VAR}, else the seed's demo-<user>-pass",
    )
    parser.add_argument("--tenant", default=DEMO_TENANT_SLUG)
    parser.add_argument("-m", "--message", help="send one message and exit")
    parser.add_argument(
        "--timeout", type=float, default=180.0, help="seconds to wait for model output"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    """CLI entry point; ``transport`` is a test seam. Returns the exit code."""
    args = _parse_args(argv)
    member = demo_user(args.user)
    email = args.email or member.email
    password = args.password or os.environ.get(PASSWORD_ENV_VAR) or member.default_password
    timeout = httpx.Timeout(10.0, read=args.timeout)
    with httpx.Client(base_url=args.base_url, timeout=timeout, transport=transport) as http:
        client = ChatClient(http)
        try:
            user = client.login(email, password, args.tenant)
            if args.message is not None:
                return 0 if run_turn(client, args.message, [], sys.stdout) is not None else 1
        except ChatClientError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(
            f"Logged in as {email} (role {user.get('role')}). /reset clears history, /quit exits."
        )
        repl(client)
    return 0


if __name__ == "__main__":
    sys.exit(main())
