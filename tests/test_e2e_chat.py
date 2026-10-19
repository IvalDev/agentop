"""End-to-end chat over the seeded demo company, driven through HTTP only.

The demo tenant comes from the real seed (:func:`app.demo.seed.seed_demo`:
synthetic fixtures, local storage, fake embeddings) on top of the two-tenant
``seeded_db``, so another tenant (Acme) exists for isolation checks. The
model is a scripted :class:`FakeLLMProvider`. The app's registry holds the
production ``search_documents`` plus one test-only ACTION tool,
``record_payment_note`` (operator, approval required), installed on this
app's ``state.tool_registry`` only — never on ``default_registry``.

Every scenario runs on its own fresh database and app.
"""

import json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.core.db import Database
from app.demo.seed import seed_demo
from app.demo.team import DEMO_TENANT_SLUG, demo_user
from app.main import create_app
from app.modules.agent.llm import AssistantMessage, ChatMessage, FakeLLMProvider, ToolCall
from app.modules.documents.extract import extract_text
from app.modules.documents.models import Document
from app.modules.documents.storage import LocalDocumentStorage
from app.modules.identity.models import UserRole
from app.modules.retrieval.embeddings import FakeEmbeddingProvider
from app.modules.tools import builtin
from app.modules.tools.registry import (
    ApprovalPolicy,
    ToolContext,
    ToolKind,
    ToolRegistry,
    ToolSpec,
)

DIM = 64
JWT_SECRET = "test-secret-0123456789-abcdefghijklmnop"

INVOICE = "TIS-2026-0150"
INVOICE_FILE = "factura_TIS-2026-0150_transporte.txt"
INVOICE_TOTAL = "Total factura: 1.452,00 €"
QUESTION = "¿Cuál es el total de la factura TIS-2026-0150 de transportes?"
CITED_ANSWER = "La factura TIS-2026-0150 suma 1.452,00 € [1]."
REQUEST = "Registra una nota de pago para la factura TIS-2026-0150, por favor."
CHAIN_ANSWER = "He enviado la nota de pago de la factura TIS-2026-0150 a aprobación [1]."
NOTE = {"invoice_number": INVOICE, "note": "Pago programado por transferencia."}
ACME_OWNER = ("owner@acme.test", "owner-pass", "acme")


class PaymentNoteInput(BaseModel):
    """Strict input of the test-only ``record_payment_note`` tool."""

    model_config = ConfigDict(extra="forbid")

    invoice_number: str = Field(pattern=r"^[A-Z]{1,4}-\d{4}-\d{4}$")
    note: str = Field(min_length=1, max_length=500)


@dataclass
class PaymentNoteSpy:
    """Handler double: records each run as (tenant id, user id, input)."""

    calls: list[tuple[int, int, PaymentNoteInput]] = field(default_factory=list)

    async def __call__(self, context: ToolContext, validated_input: PaymentNoteInput) -> dict:
        self.calls.append((context.tenant_id, context.user.id, validated_input))
        return {"recorded": True, "invoice_number": validated_input.invoice_number}


@dataclass(frozen=True)
class Demo:
    """The wired app, its client, and the seeded demo tenant."""

    app: FastAPI
    client: AsyncClient
    db: Database
    storage: LocalDocumentStorage
    spy: PaymentNoteSpy
    tenant_id: int


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        jwt_secret=JWT_SECRET,
        access_token_expire_minutes=30,
        embedding_provider="fake",
        embedding_dim=DIM,
        llm_provider="fake",
        agent_max_steps=4,
        approval_ttl_seconds=3600,
        storage_base_dir=str(tmp_path / "storage"),
    )


@pytest.fixture
async def demo(seeded_db: Database, settings: Settings) -> AsyncIterator[Demo]:
    """Seed the demo company, then serve the real app with a test-local registry."""
    storage = LocalDocumentStorage(settings.storage_base_dir)
    embeddings = FakeEmbeddingProvider(dimension=DIM)
    summary = await seed_demo(seeded_db, storage=storage, embeddings=embeddings, settings=settings)
    assert summary.ok, summary.documents

    spy = PaymentNoteSpy()
    registry = ToolRegistry()
    registry.register(builtin.SEARCH_DOCUMENTS)
    registry.register(
        ToolSpec(
            name="record_payment_note",
            description="Record a payment note on a supplier invoice (needs approval).",
            kind=ToolKind.ACTION,
            approval_policy=ApprovalPolicy.REQUIRE_APPROVAL,
            required_role=UserRole.OPERATOR,
            handler=spy,
            input_model=PaymentNoteInput,
        )
    )
    application = create_app()
    application.state.db = seeded_db
    application.state.settings = settings
    application.state.embeddings = embeddings
    application.state.storage = storage
    application.state.tool_registry = registry
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield Demo(application, client, seeded_db, storage, spy, summary.tenant_id)


# --- Helpers ---------------------------------------------------------------------


@dataclass(frozen=True)
class Caller:
    """An authenticated caller: auth headers plus its user id."""

    headers: dict[str, str]
    user_id: int


async def _login(client: AsyncClient, email: str, password: str, tenant_slug: str) -> Caller:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "tenant_slug": tenant_slug},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return Caller({"Authorization": f"Bearer {body['access_token']}"}, body["user"]["id"])


async def _as_demo(client: AsyncClient, role: str) -> Caller:
    member = demo_user(role)
    return await _login(client, member.email, member.default_password, DEMO_TENANT_SLUG)


def _search_turn(query: str = INVOICE) -> AssistantMessage:
    arguments = {"query": query, "mode": "keyword"}
    return AssistantMessage(content="", tool_calls=(ToolCall("search_documents", arguments),))


def _note_turn() -> AssistantMessage:
    return AssistantMessage(content="", tool_calls=(ToolCall("record_payment_note", NOTE),))


def _final(text: str, *, shown: Sequence[str]) -> Callable:
    """A final answer, given only after the model was shown every ``shown`` text."""

    def respond(messages: list[ChatMessage], tools: list[dict]) -> AssistantMessage:
        tool_messages = "\n".join(message.content for message in messages if message.role == "tool")
        for text_shown in shown:
            assert text_shown in tool_messages
        return AssistantMessage(content=text)

    return respond


def _chain_script() -> FakeLLMProvider:
    """Search the invoice, request the payment note, then answer citing [1]."""
    return FakeLLMProvider(
        [
            _search_turn(),
            _note_turn(),
            _final(CHAIN_ANSWER, shown=(INVOICE_TOTAL, "submitted for human approval")),
        ]
    )


async def _run_chain(demo: Demo, operator: Caller) -> dict:
    """The demo operator asks for a payment note; returns the chat response body."""
    demo.app.state.llm = _chain_script()
    response = await demo.client.post(
        "/api/v1/chat", json={"message": REQUEST}, headers=operator.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _approve_and_execute(demo: Demo, admin: Caller, approval_id: str) -> dict:
    approved = await demo.client.post(
        f"/api/v1/approvals/{approval_id}/approve", headers=admin.headers
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    executed = await demo.client.post(
        f"/api/v1/approvals/{approval_id}/execute", headers=admin.headers
    )
    assert executed.status_code == 200, executed.text
    return executed.json()


async def _audit(demo: Demo, auditor: Caller, **filters: str) -> list[dict]:
    """The tenant's events matching ``filters``, oldest first."""
    response = await demo.client.get(
        "/api/v1/audit", params={**filters, "limit": 200}, headers=auditor.headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["next_cursor"] is None
    return list(reversed(body["events"]))


async def _stored_text(demo: Demo, document_id: UUID) -> tuple[int, str]:
    """The owning tenant and the stored text of a document (no HTTP route serves it)."""
    async with demo.db.sessionmaker() as session:
        document = await session.get(Document, document_id)
        text = extract_text(await demo.storage.get(document.storage_key), document.content_type)
        return document.tenant_id, text


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return events


# --- Scenarios ---------------------------------------------------------------------


async def test_rag_answer_cites_the_demo_invoice_verifiably(demo: Demo) -> None:
    """Search → cited answer whose offsets locate the fact in the stored invoice."""
    operator = await _as_demo(demo.client, "operator")
    demo.app.state.llm = FakeLLMProvider(
        [_search_turn(), _final(CITED_ANSWER, shown=(INVOICE_TOTAL,))]
    )

    response = await demo.client.post(
        "/api/v1/chat", json={"message": QUESTION}, headers=operator.headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["answer"], body["stop_reason"]) == (CITED_ANSWER, "completed")
    assert [(call["tool_name"], call["status"]) for call in body["tool_calls"]] == [
        ("search_documents", "ok")
    ]
    assert body["pending_approvals"] == []
    [citation] = body["citations"]
    assert (citation["number"], citation["document_filename"]) == (1, INVOICE_FILE)
    document_id = UUID(citation["document_id"])
    owner_tenant, stored = await _stored_text(demo, document_id)
    assert owner_tenant == demo.tenant_id
    cited = stored[citation["start_offset"] : citation["end_offset"]]
    assert cited == citation["content"]
    assert INVOICE_TOTAL in cited
    for source in body["sources"]:
        document = await demo.client.get(
            f"/api/v1/documents/{source['document_id']}", headers=operator.headers
        )
        assert document.status_code == 200, source["document_filename"]
    assert demo.spy.calls == []


async def test_agent_action_waits_for_approval_and_runs_exactly_once(demo: Demo) -> None:
    """pending → hidden from the viewer → approved and executed by the admin → 409."""
    operator = await _as_demo(demo.client, "operator")
    viewer = await _as_demo(demo.client, "viewer")
    admin = await _as_demo(demo.client, "admin")

    body = await _run_chain(demo, operator)

    assert body["answer"] == CHAIN_ANSWER
    [pending] = body["pending_approvals"]
    approval_id = pending["approval_id"]
    assert pending["tool_name"] == "record_payment_note"
    assert [(call["tool_name"], call["status"]) for call in body["tool_calls"]] == [
        ("search_documents", "ok"),
        ("record_payment_note", "approval_required"),
    ]
    assert body["tool_calls"][1]["approval_id"] == approval_id
    assert demo.spy.calls == []

    listed = await demo.client.get("/api/v1/approvals", headers=viewer.headers)
    assert listed.status_code == 200
    assert approval_id not in {item["id"] for item in listed.json()["approvals"]}
    hidden = await demo.client.get(f"/api/v1/approvals/{approval_id}", headers=viewer.headers)
    assert hidden.status_code == 403
    refused = await demo.client.post(
        f"/api/v1/approvals/{approval_id}/approve", headers=viewer.headers
    )
    assert refused.status_code == 403

    request = await demo.client.get(f"/api/v1/approvals/{approval_id}", headers=operator.headers)
    assert request.status_code == 200
    assert request.json()["status"] == "pending"
    assert request.json()["input"] == NOTE
    assert request.json()["requested_by"] == operator.user_id

    executed = await _approve_and_execute(demo, admin, approval_id)

    assert executed["status"] == "executed"
    assert executed["executed_by"] == admin.user_id
    assert executed["execution_result"] == {"recorded": True, "invoice_number": INVOICE}
    assert demo.spy.calls == [(demo.tenant_id, admin.user_id, PaymentNoteInput(**NOTE))]
    again = await demo.client.post(
        f"/api/v1/approvals/{approval_id}/execute", headers=admin.headers
    )
    assert again.status_code == 409
    assert len(demo.spy.calls) == 1


async def test_audit_trail_links_the_run_and_the_approval(demo: Demo) -> None:
    """Acceptance criterion 6: who did what, with which tool, input, and result."""
    operator = await _as_demo(demo.client, "operator")
    admin = await _as_demo(demo.client, "admin")
    owner = await _as_demo(demo.client, "owner")
    body = await _run_chain(demo, operator)
    run_id = body["run_id"]
    approval_id = body["pending_approvals"][0]["approval_id"]
    await _approve_and_execute(demo, admin, approval_id)

    run_events = await _audit(demo, owner, correlation_id=run_id)

    assert [event["action"] for event in run_events] == [
        "agent.run_started",
        "tool.executed",
        "tool.approval_required",
        "approval.created",
        "agent.run_completed",
    ]
    by_action = {event["action"]: event for event in run_events}
    for event in run_events:
        assert (event["actor_type"], event["actor_user_id"]) == ("agent", operator.user_id)
    search = by_action["tool.executed"]
    assert search["tool_name"] == "search_documents"
    assert search["payload"]["query"] == INVOICE
    assert search["result"]["output"]["hits"][0]["document_filename"] == INVOICE_FILE
    required = by_action["tool.approval_required"]
    assert (required["tool_name"], required["payload"]) == ("record_payment_note", NOTE)
    assert required["result"]["required_role"] == "operator"
    created = by_action["approval.created"]
    assert (created["resource_type"], created["resource_id"]) == ("approval_request", approval_id)
    assert created["payload"] == NOTE
    assert created["result"]["action_key"] == required["result"]["action_key"]
    assert by_action["agent.run_completed"]["result"]["pending_approvals"] == [approval_id]

    approval_events = await _audit(demo, owner, correlation_id=approval_id)

    assert [event["action"] for event in approval_events] == [
        "approval.approved",
        "tool.executed",
        "approval.executed",
    ]
    for event in approval_events:
        assert (event["actor_type"], event["actor_user_id"]) == ("user", admin.user_id)
    executed_tool = approval_events[1]
    assert executed_tool["tool_name"] == "record_payment_note"
    assert executed_tool["payload"] == NOTE
    assert executed_tool["result"]["output"] == {"recorded": True, "invoice_number": INVOICE}
    lifecycle = await _audit(demo, owner, resource_id=approval_id)
    assert [event["action"] for event in lifecycle] == [
        "approval.created",
        "approval.approved",
        "approval.executed",
    ]

    for event in run_events + approval_events:
        if event["action"].startswith("tool."):
            assert event["actor_user_id"] is not None, event
            assert event["tool_name"], event
            assert event["payload"] is not None and event["result"] is not None, event
    agent_events = await _audit(demo, owner, action_prefix="agent.")
    assert {event["correlation_id"] for event in agent_events} == {run_id}
    stored = json.dumps(agent_events, ensure_ascii=False)
    for text in (REQUEST, CHAIN_ANSWER):
        assert text not in stored


async def test_other_tenant_sees_nothing_of_the_demo_run(demo: Demo) -> None:
    """Acme cannot read the demo trail, approvals, or cited documents, nor search them."""
    operator = await _as_demo(demo.client, "operator")
    admin = await _as_demo(demo.client, "admin")
    acme = await _login(demo.client, *ACME_OWNER)
    body = await _run_chain(demo, operator)
    run_id = body["run_id"]
    approval_id = body["pending_approvals"][0]["approval_id"]
    [citation] = body["citations"]

    assert await _audit(demo, acme, correlation_id=run_id) == []
    assert await _audit(demo, acme, resource_id=approval_id) == []
    acme_trail = await _audit(demo, acme)
    assert {run_id, approval_id}.isdisjoint(event["correlation_id"] for event in acme_trail)
    listed = await demo.client.get("/api/v1/approvals", headers=acme.headers)
    assert approval_id not in {item["id"] for item in listed.json()["approvals"]}
    for method, path in (
        ("GET", f"/api/v1/approvals/{approval_id}"),
        ("POST", f"/api/v1/approvals/{approval_id}/approve"),
        ("POST", f"/api/v1/approvals/{approval_id}/execute"),
        ("GET", f"/api/v1/documents/{citation['document_id']}"),
    ):
        response = await demo.client.request(method, path, headers=acme.headers)
        assert response.status_code == 404, path

    demo.app.state.llm = FakeLLMProvider(
        [_search_turn(), _final("No encuentro esa factura.", shown=("No matching passages",))]
    )
    acme_chat = await demo.client.post(
        "/api/v1/chat", json={"message": QUESTION}, headers=acme.headers
    )

    assert acme_chat.status_code == 200, acme_chat.text
    assert acme_chat.json()["sources"] == acme_chat.json()["citations"] == []
    request = await demo.client.get(f"/api/v1/approvals/{approval_id}", headers=admin.headers)
    assert request.json()["status"] == "pending"
    assert demo.spy.calls == []


async def test_stream_emits_the_approval_chain_as_typed_events(demo: Demo) -> None:
    """The same chain over SSE: tool steps, approval_required, tokens, citations, done."""
    operator = await _as_demo(demo.client, "operator")
    admin = await _as_demo(demo.client, "admin")
    demo.app.state.llm = _chain_script()

    response = await demo.client.post(
        "/api/v1/chat/stream", json={"message": REQUEST}, headers=operator.headers
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(response.text)
    kinds = [kind for kind, _ in events]
    first_token = kinds.index("token")
    assert kinds[:first_token] == [
        "run_started",
        "tool_call",
        "tool_result",
        "tool_call",
        "approval_required",
        "tool_result",
    ]
    assert set(kinds[first_token:-2]) == {"token"}
    assert kinds[-2:] == ["citations", "done"]
    run_id = events[0][1]["run_id"]
    tool_calls = [data for kind, data in events if kind == "tool_call"]
    results = [data for kind, data in events if kind == "tool_result"]
    approval = dict(events)["approval_required"]
    done = events[-1][1]
    assert [call["tool_name"] for call in tool_calls] == ["search_documents", "record_payment_note"]
    assert [result["status"] for result in results] == ["ok", "approval_required"]
    assert approval["tool_name"] == "record_payment_note"
    assert results[1]["approval_id"] == approval["approval_id"]
    streamed = "".join(data["text"] for kind, data in events if kind == "token")
    assert streamed == done["answer"] == CHAIN_ANSWER
    assert done["run_id"] == run_id
    assert [item["approval_id"] for item in done["pending_approvals"]] == [approval["approval_id"]]
    assert [item["number"] for item in dict(events)["citations"]["citations"]] == [1]

    stored = await demo.client.get(
        f"/api/v1/approvals/{approval['approval_id']}", headers=admin.headers
    )
    assert stored.json()["status"] == "pending"
    assert demo.spy.calls == []
