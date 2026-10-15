"""Calendar provider protocol, Google adapter, and provider registry (D7).

Contract under test (T1):

- :class:`CalendarProvider` is a runtime-checkable protocol with
  ``list_events`` / ``create_event`` / ``delete_event``; the Google adapter
  implements ``list_events`` only (create/cancel land with T3) and raises
  ``NotImplementedError`` for the other two;
- ``list_events`` calls the Calendar API with a Bearer header and nothing
  else, percent-encodes the calendar id into the path, requests
  ``singleEvents=true`` + ``orderBy=startTime``, and returns the ``items``
  list;
- 401/403 → :class:`CalendarAuthenticationError` (the tester answers False);
  any other failure → :class:`CalendarProviderError` (the engine surfaces
  502); a non-JSON body or missing ``items`` is a provider error too;
- the registry stores *factories* keyed by provider name (tokens are
  per-user, the registry is process-wide): ``register_provider`` overwrites
  like ``register_tester``, ``get_provider`` raises ``UnknownProviderError``.
"""

from typing import Any

import httpx
import pytest

from app.modules.calendar.provider import (
    CalendarAuthenticationError,
    CalendarProvider,
    CalendarProviderError,
    GoogleCalendarProvider,
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


def test_google_adapter_satisfies_the_calendar_provider_protocol() -> None:
    assert isinstance(GoogleCalendarProvider(ACCESS_TOKEN), CalendarProvider)


async def test_list_events_sends_bearer_path_and_query_params() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["Authorization"]
        return httpx.Response(200, json={"items": [{"id": "e1"}, {"id": "e2"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarProvider(ACCESS_TOKEN, client=client)
        items = await provider.list_events(calendar_id="team@example.com", max_results=7)

    assert items == [{"id": "e1"}, {"id": "e2"}]
    assert seen["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert (
        "https://www.googleapis.com/calendar/v3/calendars/team%40example.com/events" in seen["url"]
    )
    assert "maxResults=7" in seen["url"]
    assert "singleEvents=true" in seen["url"]
    assert "orderBy=startTime" in seen["url"]
    # Nothing but the Bearer credential: no cookies, no API key.
    assert "key=" not in seen["url"]


@pytest.mark.parametrize("status_code", [401, 403])
async def test_list_events_auth_failure_raises_authentication_error(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"code": status_code}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(CalendarAuthenticationError):
            await provider.list_events()


async def test_list_events_server_failure_raises_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "backend"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(CalendarProviderError) as excinfo:
            await provider.list_events()
    assert type(excinfo.value) is CalendarProviderError  # not the auth subclass


async def test_list_events_non_json_body_raises_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>oops</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(CalendarProviderError):
            await provider.list_events()


async def test_list_events_transport_error_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(httpx.RequestError):
            await provider.list_events()


async def test_create_and_delete_are_t3_stubs() -> None:
    provider = GoogleCalendarProvider(ACCESS_TOKEN)
    with pytest.raises(NotImplementedError):
        await provider.create_event()
    with pytest.raises(NotImplementedError):
        await provider.delete_event()


def test_register_provider_round_trip_overwrites_and_unknown_raises() -> None:
    register_provider("google", GoogleCalendarProvider)
    assert get_provider("google") is GoogleCalendarProvider
    factory = get_provider("google")
    assert isinstance(factory(access_token="fresh"), CalendarProvider)

    def other_factory(access_token: str) -> CalendarProvider:
        return GoogleCalendarProvider(access_token)

    register_provider("google", other_factory)
    assert get_provider("google") is other_factory

    with pytest.raises(UnknownProviderError):
        get_provider("caldav")
