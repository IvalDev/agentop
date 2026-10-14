"""Chat API: ``POST /api/v1/chat`` (JSON) and ``POST /api/v1/chat/stream`` (SSE).

Any authenticated role may chat; the tenant always comes from the user, and
the tools offered to the model are filtered by that user's role. The model
backend failing maps to 502 (JSON) or a typed ``error`` event (SSE) with a
generic message.

SSE wire format: ``event: <type>\\ndata: <json>\\n\\n`` with the types
``run_started``, ``tool_call``, ``approval_required``, ``tool_result``,
``token``, ``citations``, ``done`` (its data is the full
:class:`~packages.contracts.schemas.ChatResponse`), and ``error``. ``token``
events carry the model's text as it streams; ``done.answer`` is
authoritative.
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import Database, get_session
from app.core.deps import get_settings
from app.modules.agent.llm import LLMError
from app.modules.agent.service import AgentDeps, AgentRun, run_agent
from app.modules.documents.router import StorageDep
from app.modules.identity.deps import CurrentUser, get_current_user
from app.modules.retrieval.router import EmbeddingsDep
from packages.contracts.schemas import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

_LLM_UNAVAILABLE = "language model unavailable"


def get_agent_deps(
    request: Request, settings: SettingsDep, embeddings: EmbeddingsDep, storage: StorageDep
) -> AgentDeps:
    """Collaborators of a run from ``app.state`` (registry, model, embeddings, storage)."""
    return AgentDeps(
        registry=request.app.state.tool_registry,
        llm=request.app.state.llm,
        embeddings=embeddings,
        settings=settings,
        storage=storage,
    )


AgentDepsDep = Annotated[AgentDeps, Depends(get_agent_deps)]


@router.post("", response_model=ChatResponse)
async def chat(
    body: ChatRequest, session: SessionDep, user: CurrentUserDep, deps: AgentDepsDep
) -> ChatResponse:
    """Run the agent to completion and return the answer with citations."""
    try:
        return await run_agent(session, user, body.message, body.history, deps)
    except LLMError as exc:
        logger.warning("chat run failed: %s", exc)
        raise HTTPException(status_code=502, detail=_LLM_UNAVAILABLE) from exc


@router.post("/stream")
async def chat_stream(
    body: ChatRequest, request: Request, user: CurrentUserDep, deps: AgentDepsDep
) -> StreamingResponse:
    """Run the agent, streaming its events as Server-Sent Events."""
    db: Database = request.app.state.db  # present: get_current_user needed it
    return StreamingResponse(
        _event_stream(db, user, body, deps),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _event_stream(
    db: Database, user: CurrentUser, body: ChatRequest, deps: AgentDeps
) -> AsyncIterator[str]:
    """SSE frames of one run; its own session lives exactly as long as the stream."""
    async with db.sessionmaker() as session:
        run = AgentRun(session, user, body.message, body.history, deps)
        try:
            async for event in run.events(stream=True):
                yield _frame(event.type, event.data)
        except LLMError as exc:
            logger.warning("chat stream failed: %s", exc)
            yield _frame("error", {"message": _LLM_UNAVAILABLE})
        except Exception:
            logger.exception("chat stream failed")
            yield _frame("error", {"message": "internal error"})


def _frame(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"
