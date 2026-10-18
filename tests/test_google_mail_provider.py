"""Google Mail provider protocol, Google adapter, and provider registry (G1).

Contract under test (the tester's HTTP seam, mirroring
``tests/test_calendar_provider.py``):

- :class:`GmailProvider` is a runtime-checkable protocol with
  ``get_profile``; the Google adapter implements it (G3 extends both with
  the read/action tools);
- ``get_profile`` calls the Gmail API ``users.getProfile`` endpoint with a
  Bearer header and nothing else, and returns the payload including the
  connected ``emailAddress``;
- 401/403 → :class:`GmailAuthenticationError` (the tester answers False);
  any other failure → :class:`GmailProviderError` (the engine surfaces 502);
  a non-JSON body or a payload without ``emailAddress`` is a provider error
  too;
- the registry stores *factories* keyed by provider name (tokens are
  per-user, the registry is process-wide): ``register_provider`` overwrites
  like ``register_tester``, ``get_provider`` raises :class:`UnknownProviderError`.
"""

from typing import Any

import httpx
import pytest

from app.modules.google_mail.provider import (
    GOOGLE_GMAIL_PROFILE_URL,
    GmailAuthenticationError,
    GmailProvider,
    GmailProviderError,
    GoogleGmailProvider,
    UnknownProviderError,
    get_provider,
    provider_registry,
    register_provider,
)

ACCESS_TOKEN = "ya29.test-token"


@pytest.fixture(autouse=True)
def provider_registry_clean() -> Any:
    """The process-wide provider registry starts and ends empty per test."""
    provider_registry.clear()
    yield
    provider_registry.clear()


def test_google_adapter_satisfies_the_gmail_provider_protocol() -> None:
    assert isinstance(GoogleGmailProvider(ACCESS_TOKEN), GmailProvider)


async def test_get_profile_sends_bearer_and_returns_the_payload() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["authorization"] = request.headers["Authorization"]
        return httpx.Response(200, json={"emailAddress": "agent@acme.test", "messagesTotal": 42})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        profile = await provider.get_profile()

    assert profile == {"emailAddress": "agent@acme.test", "messagesTotal": 42}
    assert seen["method"] == "GET"
    assert seen["url"] == GOOGLE_GMAIL_PROFILE_URL
    assert seen["authorization"] == f"Bearer {ACCESS_TOKEN}"


@pytest.mark.parametrize("status", [401, 403])
async def test_get_profile_maps_auth_statuses_to_authentication_error(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"status": "UNAUTHENTICATED"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailAuthenticationError):
            await provider.get_profile()


async def test_get_profile_maps_other_errors_to_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError):
            await provider.get_profile()


async def test_get_profile_rejects_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError, match="non-JSON"):
            await provider.get_profile()


async def test_get_profile_requires_the_connected_address() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messagesTotal": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError, match="emailAddress"):
            await provider.get_profile()


def test_registry_stores_factories_and_overwrites() -> None:
    def factory(access_token: str) -> GoogleGmailProvider:
        return GoogleGmailProvider(access_token)

    register_provider("google", factory)
    assert get_provider("google") is factory

    register_provider("google", factory)  # overwrites, like register_tester
    assert get_provider("google") is factory


def test_get_provider_raises_for_unknown_name() -> None:
    with pytest.raises(UnknownProviderError, match="unknown Gmail provider"):
        get_provider("caldav")
