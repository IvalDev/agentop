"""Agent orchestrator: a bounded tool-calling loop over the chat model adapter.

One chat request is one *run*, identified by ``run_id`` (a UUID that is also
the audit correlation id of everything the run does):

1. The conversation is the system policy followed by the user's current
   date, time, and timezone (:mod:`~app.modules.agent.user_time`; UTC when
   the client sent no usable timezone), the client's capped user/assistant
   history, and the new message (:func:`~app.modules.agent.prompt.build_messages`).
   The model is offered only the tools whose required role the user
   satisfies and whose module (when the tool declares one) the tenant has
   active, with JSON schemas from their input models. A run given an
   engine (:mod:`~app.modules.agent.engines`) uses that engine's system
   prompt and only its tools; without one it uses the default policy and
   every visible tool.
2. A reply without tool calls is the final answer. Otherwise each call (at
   most :data:`MAX_TOOL_CALLS_PER_STEP` per reply) runs through
   :func:`~app.modules.tools.executor.execute_tool` as actor ``agent`` with
   the run id as correlation id (tools get the run's ``llm``, document
   ``storage``, the app ``settings``, the per-user-connection
   ``credentials_key``, and the user's resolved ``timezone`` in their
   context), its result goes back as a ``tool`` message,
   and the model is asked again — at most ``Settings.agent_max_steps`` model
   calls per run. When that budget is spent the run stops with
   :data:`STEP_LIMIT_ANSWER`; calls requested on the last step never run.
3. Outcomes the model sees: ``ok`` → delimited output (search hits become
   numbered passages to cite); refusals and failures → a short error code
   (validation errors also name the failing fields, and ``precheck_failed``
   carries the tool's user-safe reason so the model can explain it);
   ``approval_required`` → an approval request is created through the
   approvals service (once per action key per run, with the call's resolved
   approver role) and the model is told it is pending. The loop never
   passes a grant or ``dry_run``: the model cannot approve or rehearse
   anything (decision A7).
4. ``citations`` are the passages whose ``[n]`` markers appear in the
   answer; ``sources`` are all passages shown to the model.

Audit (actor = the user, ``actor_type`` agent, correlation = run id):
``agent.run_started``, then exactly one of ``agent.run_completed``,
``agent.step_limit``, or ``agent.run_failed``; ``agent.invalid_tool_call``
for calls refused before the executor (non-object arguments, over the
per-step cap); every executed call adds the executor's ``tool.*`` event, and
every approval request the run submits adds its ``approval.created`` event
(also ``agent`` with the run id; later ``approval.*`` events of that request
are correlated by the request id). Run events carry metadata only (sizes,
counts, tool names, approval ids, and on ``agent.run_started`` the user's
resolved ``timezone``, ``local_time`` with its UTC offset, and whether the
run fell back to UTC as ``timezone_fallback``): the message, history, and
answer text are never written to the audit log.

Transactions: the loop commits only the approval requests it creates
(``EXTRACT`` tools are read-only); audit events use their own transactions.
"""

import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.agent.citations import CitationIndex
from app.modules.agent.engines import Engine, visible_specs
from app.modules.agent.llm import (
    AssistantMessage,
    ChatMessage,
    LLMError,
    LLMProvider,
    TextDelta,
    ToolCall,
)
from app.modules.agent.prompt import (
    HistoryTurn,
    build_messages,
    fit_passages,
    render_passages,
    render_pending_approval,
    render_tool_error,
    render_tool_output,
)
from app.modules.agent.user_time import UserTime, render_time_context, resolve_user_time
from app.modules.approvals.exceptions import ApprovalError
from app.modules.approvals.service import create_approval_request
from app.modules.audit.models import ActorType
from app.modules.audit.service import persist_event
from app.modules.catalog.registry import ModuleCatalog
from app.modules.catalog.service import active_module_keys
from app.modules.documents.storage import DocumentStorage
from app.modules.retrieval.embeddings import EmbeddingProvider
from app.modules.tools.builtin import SearchDocumentsOutput
from app.modules.tools.exceptions import ToolError
from app.modules.tools.executor import (
    ApprovalRequestPayload,
    ToolExecutionResult,
    execute_tool,
)
from app.modules.tools.registry import ToolRegistry, ToolUser
from packages.contracts.schemas import ChatResponse, PendingApproval, ToolCallSummary

logger = logging.getLogger(__name__)

STEP_LIMIT_ANSWER = (
    "I could not complete this request within the allowed number of steps. "
    "Please narrow the question or try again."
)
#: Tool calls executed from one model reply; extra calls are refused.
MAX_TOOL_CALLS_PER_STEP = 8
_NAME_MAX_CHARS = 64
#: Executor ``error_kind`` → code shown to the model and the client.
_ERROR_CODES = {
    "not_found": "unknown_tool",
    "forbidden": "forbidden",
    "inactive_module": "inactive_module",
    "validation": "invalid_input",
    "precheck": "precheck_failed",
    "execution": "tool_failed",
}
#: Codes whose executor message is safe and useful to the model (field errors,
#: a precheck's reason); every other code is shown without details.
_DETAILED_CODES = frozenset({"invalid_input", "precheck_failed"})

EventType = Literal[
    "run_started", "tool_call", "tool_result", "approval_required", "token", "citations", "done"
]


@dataclass(frozen=True)
class AgentEvent:
    """One step of a run as streamed to clients (``data`` is JSON-ready)."""

    type: EventType
    data: dict[str, Any]


@dataclass(frozen=True)
class AgentDeps:
    """Collaborators of a run (built per request from ``app.state``)."""

    registry: ToolRegistry
    llm: LLMProvider
    embeddings: EmbeddingProvider | None
    settings: Settings
    storage: DocumentStorage | None = None
    #: Fernet key for per-user provider connections (calendar-mvp T3/D3);
    #: ``None`` fails the calendar tools closed with a precheck error.
    credentials_key: str | None = None
    #: Module catalog the engines read their agent profiles from
    #: (``app.state.module_catalog``); ``None`` leaves only ``knowledge``.
    catalog: ModuleCatalog | None = None


def new_run_id() -> str:
    """Opaque run identifier, also used as the audit correlation id."""
    return str(uuid4())


def tool_schemas(
    registry: ToolRegistry,
    user: ToolUser,
    active_modules: set[str],
    engine: Engine | None = None,
) -> list[dict[str, Any]]:
    """Function schemas of the tools ``user`` may use (what the model sees).

    A tool is offered when the user's role satisfies its ``required_role``
    and its module is available: core tools (``spec.module is None``)
    always, module tools only when ``spec.module`` is one of the tenant's
    ``active_modules`` (from
    :func:`~app.modules.catalog.service.active_module_keys`). An ``engine``
    then keeps only its own tools (core tools for ``knowledge``, the
    module's tools for a module engine); ``None`` offers every visible tool.
    """
    schemas = []
    for spec in visible_specs(registry, user, active_modules, engine):
        assert spec.input_model is not None  # registration invariant
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.input_model.model_json_schema(),
                },
            }
        )
    return schemas


def _clip_name(name: str) -> str:
    return name[:_NAME_MAX_CHARS]


class AgentRun:
    """State and event stream of one agent run (see the module docstring).

    ``user_time`` is the requesting user's resolved time (see
    :func:`~app.modules.agent.user_time.resolve_user_time`); ``None`` means
    no timezone was sent and the run uses UTC as a fallback. ``engine``
    (see :mod:`~app.modules.agent.engines`) sets the run's system prompt and
    narrows its tools; ``None`` uses :data:`~app.modules.agent.prompt.SYSTEM_PROMPT`
    and every visible tool. Raises ``ValueError`` at construction for a
    privileged history role.
    """

    def __init__(
        self,
        session: AsyncSession,
        user: ToolUser,
        message: str,
        history: Sequence[HistoryTurn],
        deps: AgentDeps,
        *,
        active_modules: set[str],
        user_time: UserTime | None = None,
        engine: Engine | None = None,
    ) -> None:
        self.run_id = new_run_id()
        self._session = session
        self._user = user
        self._deps = deps
        self._message = message
        self._history_count = len(history)
        self._user_time = user_time if user_time is not None else resolve_user_time(None)
        self._messages = build_messages(
            message,
            history,
            time_context=render_time_context(self._user_time),
            system_prompt=engine.system_prompt if engine is not None else None,
        )
        self._active_modules = active_modules
        self._tools = tool_schemas(deps.registry, user, active_modules, engine)
        self._citations = CitationIndex()
        self._approvals: dict[str, PendingApproval] = {}
        self._summaries: list[ToolCallSummary] = []
        self._answer = ""
        self._stop_reason: Literal["completed", "step_limit"] = "completed"
        self._steps = 0
        self._unexecuted: list[str] = []

    def response(self) -> ChatResponse:
        """The run's result (complete once :meth:`events` is exhausted)."""
        return ChatResponse(
            run_id=self.run_id,
            answer=self._answer,
            stop_reason=self._stop_reason,
            citations=self._citations.cited(self._answer),
            sources=self._citations.sources(),
            pending_approvals=list(self._approvals.values()),
            tool_calls=list(self._summaries),
        )

    async def events(self, *, stream: bool = False) -> AsyncIterator[AgentEvent]:
        """Run the loop, yielding typed events; ``stream`` adds ``token`` deltas.

        Model failures are audited as ``agent.run_failed`` and re-raised
        (:class:`LLMError` for backend problems).
        """
        await self._audit(
            "agent.run_started",
            payload={
                "message_chars": len(self._message),
                "history_messages": self._history_count,
                "timezone": self._user_time.timezone,
                "local_time": self._user_time.now.isoformat(timespec="seconds"),
                "timezone_fallback": self._user_time.fallback,
            },
            result={"tools": [tool["function"]["name"] for tool in self._tools]},
        )
        yield AgentEvent("run_started", {"run_id": self.run_id})
        try:
            async for event in self._loop(stream):
                yield event
        except Exception as exc:
            error = "llm_unavailable" if isinstance(exc, LLMError) else "internal_error"
            await self._audit("agent.run_failed", result={"error": error})
            raise
        response = self.response()
        await self._audit_finish(response)
        yield AgentEvent(
            "citations",
            {
                "citations": [citation.model_dump(mode="json") for citation in response.citations],
                "sources": [source.model_dump(mode="json") for source in response.sources],
            },
        )
        yield AgentEvent("done", response.model_dump(mode="json"))

    async def _loop(self, stream: bool) -> AsyncIterator[AgentEvent]:
        max_steps = self._deps.settings.agent_max_steps
        for step in range(1, max_steps + 1):
            self._steps = step
            reply: AssistantMessage | None = None
            if stream:
                async for item in self._deps.llm.stream_chat(self._messages, self._tools):
                    if isinstance(item, TextDelta):
                        yield AgentEvent("token", {"text": item.text})
                    else:
                        reply = item
                if reply is None:
                    raise LLMError("model stream ended without a final message")
            else:
                reply = await self._deps.llm.chat(self._messages, self._tools)

            if not reply.tool_calls:
                self._answer = reply.content
                return
            if step == max_steps:
                self._stop_reason = "step_limit"
                self._unexecuted = [_clip_name(call.name) for call in reply.tool_calls]
                self._answer = STEP_LIMIT_ANSWER
                if stream:
                    yield AgentEvent("token", {"text": STEP_LIMIT_ANSWER})
                return

            calls = tuple(
                replace(call, id=call.id or f"call_{step}_{index}")
                for index, call in enumerate(reply.tool_calls, start=1)
            )
            self._messages.append(
                ChatMessage(role="assistant", content=reply.content, tool_calls=calls)
            )
            for index, call in enumerate(calls):
                over_cap = index >= MAX_TOOL_CALLS_PER_STEP
                async for event in self._dispatch(f"{step}.{index + 1}", call, over_cap):
                    yield event

    async def _dispatch(
        self, call_id: str, call: ToolCall, over_cap: bool
    ) -> AsyncIterator[AgentEvent]:
        """Run one tool call and append its ``tool`` message to the conversation."""
        name = _clip_name(call.name)
        yield AgentEvent("tool_call", {"call_id": call_id, "tool_name": name})
        new_approval: PendingApproval | None = None
        if over_cap or call.arguments is None:
            error = "too_many_tool_calls" if over_cap else "invalid_arguments"
            detail = (
                f"at most {MAX_TOOL_CALLS_PER_STEP} tool calls run per reply"
                if over_cap
                else "arguments must be a JSON object"
            )
            await self._audit("agent.invalid_tool_call", tool_name=name, result={"error": error})
            summary = ToolCallSummary(call_id=call_id, tool_name=name, status="error", error=error)
            content = render_tool_error(name, error, detail)
        else:
            result = await execute_tool(
                self._deps.registry,
                self._session,
                self._user,
                call.name,
                call.arguments,
                actor_type=ActorType.AGENT,
                correlation_id=self.run_id,
                embeddings=self._deps.embeddings,
                llm=self._deps.llm,
                storage=self._deps.storage,
                active_modules=self._active_modules,
                credentials_key=self._deps.credentials_key,
                settings=self._deps.settings,
                timezone=self._user_time.timezone,
            )
            summary, content, new_approval = await self._handle(call_id, name, result)
        self._summaries.append(summary)
        self._messages.append(
            ChatMessage(role="tool", content=content, tool_name=name, tool_call_id=call.id)
        )
        if new_approval is not None:
            yield AgentEvent("approval_required", new_approval.model_dump(mode="json"))
        yield AgentEvent("tool_result", summary.model_dump(mode="json"))

    async def _handle(
        self, call_id: str, name: str, result: ToolExecutionResult
    ) -> tuple[ToolCallSummary, str, PendingApproval | None]:
        """Map an executor result to (summary, tool message, newly created approval)."""
        if result.status == "ok":
            summary = ToolCallSummary(call_id=call_id, tool_name=name, status="ok")
            return summary, self._render_output(name, result.output), None
        if result.status == "approval_required" and result.approval_request is not None:
            approval, created = await self._submit(result.approval_request)
            if approval is not None:
                summary = ToolCallSummary(
                    call_id=call_id,
                    tool_name=name,
                    status="approval_required",
                    approval_id=approval.approval_id,
                )
                content = render_pending_approval(name, approval.approval_id)
                return summary, content, approval if created else None
            code, detail = "approval_unavailable", None
        else:
            code = _ERROR_CODES.get(result.error_kind or "", "tool_failed")
            detail = result.error if code in _DETAILED_CODES else None
        summary = ToolCallSummary(call_id=call_id, tool_name=name, status="error", error=code)
        return summary, render_tool_error(name, code, detail), None

    def _render_output(self, name: str, output: Any) -> str:
        if isinstance(output, SearchDocumentsOutput):
            shown = fit_passages(output.hits)
            return render_passages([(self._citations.add(hit), hit) for hit in shown])
        return render_tool_output(name, output)

    async def _submit(self, payload: ApprovalRequestPayload) -> tuple[PendingApproval | None, bool]:
        """Create (once per action key) the approval request; ``(approval, created)``."""
        existing = self._approvals.get(payload.action_key)
        if existing is not None:
            return existing, False
        try:
            request = await create_approval_request(
                self._session,
                self._deps.registry,
                self._user,
                payload.tool_name,
                payload.input,
                settings=self._deps.settings,
                actor_type=ActorType.AGENT,
                correlation_id=self.run_id,
            )
            approval = PendingApproval(
                approval_id=request.id, tool_name=request.tool_name, expires_at=request.expires_at
            )
            await self._session.commit()
        except (ApprovalError, ToolError, ValidationError):
            logger.exception("agent could not create an approval request for %s", payload.tool_name)
            await self._session.rollback()
            return None, False
        self._approvals[payload.action_key] = approval
        return approval, True

    async def _audit_finish(self, response: ChatResponse) -> None:
        pending = [str(approval.approval_id) for approval in response.pending_approvals]
        if self._stop_reason == "step_limit":
            await self._audit(
                "agent.step_limit",
                result={
                    "steps": self._steps,
                    "unexecuted_tool_calls": self._unexecuted,
                    "pending_approvals": pending,
                },
            )
            return
        await self._audit(
            "agent.run_completed",
            result={
                "steps": self._steps,
                "answer_chars": len(response.answer),
                "tool_calls": len(response.tool_calls),
                "citations": len(response.citations),
                "pending_approvals": pending,
            },
        )

    async def _audit(
        self,
        action: str,
        *,
        tool_name: str | None = None,
        payload: Any = None,
        result: Any = None,
    ) -> None:
        await persist_event(
            self._session,
            tenant_id=self._user.tenant_id,
            action=action,
            actor_user_id=self._user.id,
            actor_type=ActorType.AGENT,
            resource_type="agent_run",
            resource_id=self.run_id,
            tool_name=tool_name,
            payload=payload,
            result=result,
            correlation_id=self.run_id,
        )


async def run_agent(
    session: AsyncSession,
    user: ToolUser,
    message: str,
    history: Sequence[HistoryTurn],
    deps: AgentDeps,
    *,
    timezone: str | None = None,
) -> ChatResponse:
    """Run the agent to completion and return its response (no streaming).

    The tenant's active module keys are read once per run and gate both the
    tools the model is offered and every ``execute_tool`` call in the loop.
    ``timezone`` is the client's IANA name, resolved with a UTC fallback.
    Raises :class:`LLMError` when the model backend fails.
    """
    active_modules = await active_module_keys(session, user.tenant_id)
    run = AgentRun(
        session,
        user,
        message,
        history,
        deps,
        active_modules=active_modules,
        user_time=resolve_user_time(timezone),
    )
    async for _event in run.events():
        pass
    return run.response()
