"""Google Mail connection tester and registration entry points (google-mail G1).

Contract under test (mirrors ``tests/test_calendar_tester.py``):

- a missing, non-string, or empty ``access_token`` answers ``False`` **before**
  any provider lookup (documented fail-closed contract of this tester);
- a working provider answers ``True`` after exactly one ``get_profile`` call
  (enough to prove access end to end);
- :class:`GmailAuthenticationError` (401/403) answers ``False`` — the
  tokens are dead; every other failure (provider error, transport error,
  unknown provider) propagates so the engine can surface 502;
- :func:`register_google_mail_provider` and :func:`register_google_mail_tester`
  wire the process-wide registries — ``app.main`` calls them once at import
  time (reviewed google-mail allowlist change); importing
  :mod:`app.modules.google_mail.registry` alone must not touch them.

Every registry mutation this file adds beyond the production wiring is rolled
back per test, so the process-wide registries keep exactly the entries
``app.main`` installed.
"""

from typing import Any

import httpx
import pytest

from app.modules.google_mail.provider import (
    GmailAuthenticationError,
    GmailProviderError,
    GoogleGmailProvider,
    UnknownProviderError,
    get_provider,
    provider_registry,
    register_provider,
)
from app.modules.google_mail.registry import (
    register_google_mail_provider,
    register_google_mail_tester,
)
from app.modules.google_mail.testers import google_mail_connection_tester
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
        module_key="google_mail",
        provider=provider,
        credentials=credentials,
        data=data or {},
    )


class _FakeProvider:
    """Protocol-shaped double: records calls, then returns or raises."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def get_profile(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"emailAddress": "agent@acme.test"}


async def test_missing_empty_or_non_string_token_fails_closed() -> None:
    # The provider registry is EMPTY on purpose: a token problem must be
    # decided before any lookup could raise UnknownProviderError.
    for credentials in ({}, {"access_token": ""}, {"access_token": 7}):  # type: ignore[dict-item]
        assert await google_mail_connection_tester(_context(credentials)) is False


async def test_working_provider_returns_true() -> None:
    fake = _FakeProvider()
    register_provider("google", lambda access_token: fake)

    assert await google_mail_connection_tester(_context({"access_token": ACCESS_TOKEN})) is True
    assert len(fake.calls) == 1


async def test_passes_token_and_requests_exactly_one_profile_read() -> None:
    captured: dict[str, Any] = {}
    fake = _FakeProvider()

    def factory(access_token: str) -> _FakeProvider:
        captured["access_token"] = access_token
        return fake

    register_provider("google", factory)
    await google_mail_connection_tester(_context({"access_token": ACCESS_TOKEN}))

    assert captured["access_token"] == ACCESS_TOKEN
    assert fake.calls == [{}]


async def test_authentication_error_returns_false() -> None:
    register_provider("google", lambda access_token: _FakeProvider(GmailAuthenticationError("401")))

    assert await google_mail_connection_tester(_context({"access_token": ACCESS_TOKEN})) is False


async def test_provider_error_propagates() -> None:
    register_provider("google", lambda access_token: _FakeProvider(GmailProviderError("500")))

    with pytest.raises(GmailProviderError):
        await google_mail_connection_tester(_context({"access_token": ACCESS_TOKEN}))


async def test_transport_error_propagates() -> None:
    def factory(access_token: str) -> _FakeProvider:
        return _FakeProvider(
            httpx.ConnectError("refused", request=httpx.Request("GET", "https://google.test"))
        )

    register_provider("google", factory)
    with pytest.raises(httpx.RequestError):
        await google_mail_connection_tester(_context({"access_token": ACCESS_TOKEN}))


async def test_unknown_provider_propagates() -> None:
    with pytest.raises(UnknownProviderError):
        await google_mail_connection_tester(
            _context({"access_token": ACCESS_TOKEN}, provider="caldav")
        )


def test_registry_entry_points_wire_the_process_wide_registries() -> None:
    register_google_mail_provider()
    assert get_provider("google") is GoogleGmailProvider

    register_google_mail_tester()
    assert connection_testers["google_mail"] is google_mail_connection_tester
