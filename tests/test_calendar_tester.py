"""Calendar connection tester and registration entry points (D8, T1).

Contract under test:

- a missing, non-string, or empty ``access_token`` answers ``False`` **before**
  any provider lookup (documented fail-closed contract of this tester);
- a working provider answers ``True`` after exactly one ``list_events`` call
  for the primary calendar with ``max_results=1`` (enough to prove access);
- :class:`CalendarAuthenticationError` (401/403) answers ``False`` — the
  tokens are dead; every other failure (provider error, transport error,
  unknown provider) propagates so the engine can surface 502;
- :func:`register_google_provider` and :func:`register_calendar_tester` wire
  the process-wide registries when a caller (tests, or the future allowlist
  change) asks for it — importing :mod:`app.modules.calendar.registry` alone
  must not touch them.

Every registry mutation in this file is rolled back per test: the production
registries stay empty, mirroring the ``default_catalog`` decision.
"""

from typing import Any

import httpx
import pytest

from app.modules.calendar.provider import (
    CalendarAuthenticationError,
    CalendarProviderError,
    GoogleCalendarProvider,
    UnknownProviderError,
    get_provider,
    provider_registry,
    register_provider,
)
from app.modules.calendar.registry import register_calendar_tester, register_google_provider
from app.modules.calendar.testers import calendar_connection_tester
from app.modules.onboarding.testers import ConnectionContext, connection_testers

ACCESS_TOKEN = "ya29.tester-token"


@pytest.fixture(autouse=True)
def registries_clean() -> Any:
    """Both process-wide registries start and end empty per test."""
    provider_registry.clear()
    connection_testers.clear()
    yield
    provider_registry.clear()
    connection_testers.clear()


def _context(
    credentials: dict[str, str],
    *,
    provider: str = "google",
    data: dict[str, Any] | None = None,
) -> ConnectionContext:
    return ConnectionContext(
        tenant_id=1,
        user_id=1,
        module_key="calendar",
        provider=provider,
        credentials=credentials,
        data=data or {},
    )


class _FakeProvider:
    """Protocol-shaped double: records calls, then returns or raises."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def list_events(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return []


async def test_missing_empty_or_non_string_token_fails_closed() -> None:
    # The provider registry is EMPTY on purpose: a token problem must be
    # decided before any lookup could raise UnknownProviderError.
    for credentials in ({}, {"access_token": ""}, {"access_token": 7}):  # type: ignore[dict-item]
        assert await calendar_connection_tester(_context(credentials)) is False


async def test_working_provider_returns_true() -> None:
    fake = _FakeProvider()
    register_provider("google", lambda access_token: fake)

    assert await calendar_connection_tester(_context({"access_token": ACCESS_TOKEN})) is True
    assert len(fake.calls) == 1


async def test_passes_token_and_requests_one_primary_event() -> None:
    captured: dict[str, Any] = {}
    fake = _FakeProvider()

    def factory(access_token: str) -> _FakeProvider:
        captured["access_token"] = access_token
        return fake

    register_provider("google", factory)
    await calendar_connection_tester(_context({"access_token": ACCESS_TOKEN}))

    assert captured["access_token"] == ACCESS_TOKEN
    assert fake.calls == [{"calendar_id": "primary", "max_results": 1}]


async def test_authentication_error_returns_false() -> None:
    register_provider(
        "google", lambda access_token: _FakeProvider(CalendarAuthenticationError("401"))
    )

    assert await calendar_connection_tester(_context({"access_token": ACCESS_TOKEN})) is False


async def test_provider_error_propagates() -> None:
    register_provider("google", lambda access_token: _FakeProvider(CalendarProviderError("500")))

    with pytest.raises(CalendarProviderError):
        await calendar_connection_tester(_context({"access_token": ACCESS_TOKEN}))


async def test_transport_error_propagates() -> None:
    def factory(access_token: str) -> _FakeProvider:
        return _FakeProvider(
            httpx.ConnectError("refused", request=httpx.Request("GET", "https://google.test"))
        )

    register_provider("google", factory)
    with pytest.raises(httpx.RequestError):
        await calendar_connection_tester(_context({"access_token": ACCESS_TOKEN}))


async def test_unknown_provider_propagates() -> None:
    with pytest.raises(UnknownProviderError):
        await calendar_connection_tester(
            _context({"access_token": ACCESS_TOKEN}, provider="caldav")
        )


def test_registry_entry_points_wire_the_process_wide_registries() -> None:
    register_google_provider()
    assert get_provider("google") is GoogleCalendarProvider

    register_calendar_tester()
    assert connection_testers["calendar"] is calendar_connection_tester
