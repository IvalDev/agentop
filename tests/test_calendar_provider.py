"""Calendar provider protocol, Google adapter, and provider registry (D7).

Contract under test (T1 + T3):

- :class:`CalendarProvider` is a runtime-checkable protocol with
  ``list_events`` / ``create_event`` / ``delete_event``; the Google adapter
  implements all three (T3 turned the create/cancel stubs into real calls);
- ``list_events`` calls the Calendar API with a Bearer header and nothing
  else, percent-encodes the calendar id into the path, requests
  ``singleEvents=true`` + ``orderBy=startTime``, sends ``timeZone`` only when
  a ``time_zone`` is given, and returns the ``items`` list;
- ``create_event`` POSTs an event body (summary, ``dateTime`` start/end plus
  ``timeZone`` when given, optional description and attendees as ``{email}``
  objects) to the events collection and returns the created payload;
  ``delete_event`` DELETEs one
  event resource and accepts a 204 (or any 2xx) as success;
- 401/403 → :class:`CalendarAuthenticationError` (the tester answers False);
  any other failure → :class:`CalendarProviderError` (the engine surfaces
  502); a non-JSON body or missing ``items`` is a provider error too;
- the registry stores *factories* keyed by provider name (tokens are
  per-user, the registry is process-wide): ``register_provider`` overwrites
  like ``register_tester``, ``get_provider`` raises ``UnknownProviderError``.
"""

import json
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
    assert "timeZone" not in seen["url"]  # no time_zone given: Google's default applies
    # Nothing but the Bearer credential: no cookies, no API key.
    assert "key=" not in seen["url"]


async def test_list_events_sends_the_time_zone_query_param_when_given() -> None:
    """Google then expresses the returned event times in the user's zone."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarProvider(ACCESS_TOKEN, client=client)
        await provider.list_events(time_zone="Europe/Madrid")

    assert seen["params"]["timeZone"] == "Europe/Madrid"


async def test_list_events_passes_the_time_range_as_timemin_timemax() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"items": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarProvider(ACCESS_TOKEN, client=client)
        await provider.list_events(
            time_min="2026-10-20T08:00:00+02:00", time_max="2026-10-20T18:00:00+02:00"
        )

    assert "timeMin=2026-10-20T08%3A00%3A00%2B02%3A00" in seen["url"]
    assert "timeMax=2026-10-20T18%3A00%3A00%2B02%3A00" in seen["url"]


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


async def test_create_event_posts_event_body_and_returns_payload() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "evt1", "html_link": "https://cal/evt1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarProvider(ACCESS_TOKEN, client=client)
        payload = await provider.create_event(
            summary="Standup",
            start="2026-10-20T10:00:00+02:00",
            end="2026-10-20T10:30:00+02:00",
            description="Daily sync",
            attendees=["ana@example.com", "luis@example.com"],
        )

    assert payload == {"id": "evt1", "html_link": "https://cal/evt1"}
    assert seen["method"] == "POST"
    assert seen["url"] == "https://www.googleapis.com/calendar/v3/calendars/primary/events"
    assert seen["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert seen["body"] == {
        "summary": "Standup",
        "start": {"dateTime": "2026-10-20T10:00:00+02:00"},
        "end": {"dateTime": "2026-10-20T10:30:00+02:00"},
        "description": "Daily sync",
        "attendees": [{"email": "ana@example.com"}, {"email": "luis@example.com"}],
    }


async def test_create_event_sends_the_time_zone_on_start_and_end_when_given() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "evt4"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarProvider(ACCESS_TOKEN, client=client)
        await provider.create_event(
            summary="Lunch",
            start="2026-10-26T14:00:00+01:00",
            end="2026-10-26T15:00:00+01:00",
            time_zone="Europe/Madrid",
        )

    assert seen["body"] == {
        "summary": "Lunch",
        "start": {"dateTime": "2026-10-26T14:00:00+01:00", "timeZone": "Europe/Madrid"},
        "end": {"dateTime": "2026-10-26T15:00:00+01:00", "timeZone": "Europe/Madrid"},
    }


async def test_create_event_percent_encodes_the_calendar_id() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": "evt2"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarProvider(ACCESS_TOKEN, client=client)
        await provider.create_event(
            calendar_id="team@example.com",
            summary="Review",
            start="2026-10-21T09:00:00Z",
            end="2026-10-21T10:00:00Z",
        )

    assert (
        seen["url"] == "https://www.googleapis.com/calendar/v3/calendars/team%40example.com/events"
    )


async def test_create_event_omits_optional_fields_when_absent() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "evt3"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarProvider(ACCESS_TOKEN, client=client)
        await provider.create_event(
            summary="Focus", start="2026-10-22T14:00:00Z", end="2026-10-22T15:00:00Z"
        )

    assert seen["body"] == {
        "summary": "Focus",
        "start": {"dateTime": "2026-10-22T14:00:00Z"},
        "end": {"dateTime": "2026-10-22T15:00:00Z"},
    }


@pytest.mark.parametrize("status_code", [401, 403])
async def test_create_event_auth_failure_raises_authentication_error(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"code": status_code}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(CalendarAuthenticationError):
            await provider.create_event(
                summary="X", start="2026-10-20T10:00:00Z", end="2026-10-20T11:00:00Z"
            )


async def test_create_event_server_failure_raises_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "backend"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(CalendarProviderError) as excinfo:
            await provider.create_event(
                summary="X", start="2026-10-20T10:00:00Z", end="2026-10-20T11:00:00Z"
            )
    assert type(excinfo.value) is CalendarProviderError


async def test_create_event_non_json_body_raises_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>oops</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(CalendarProviderError):
            await provider.create_event(
                summary="X", start="2026-10-20T10:00:00Z", end="2026-10-20T11:00:00Z"
            )


async def test_delete_event_sends_delete_with_encoded_ids_and_accepts_204() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["Authorization"]
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarProvider(ACCESS_TOKEN, client=client)
        result = await provider.delete_event(event_id="evt_123")

    assert result is None
    assert seen["method"] == "DELETE"
    assert seen["url"] == "https://www.googleapis.com/calendar/v3/calendars/primary/events/evt_123"
    assert seen["authorization"] == f"Bearer {ACCESS_TOKEN}"


async def test_delete_event_percent_encodes_calendar_and_event_ids() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarProvider(ACCESS_TOKEN, client=client)
        await provider.delete_event(calendar_id="team@example.com", event_id="evt 456/x")

    assert (
        seen["url"] == "https://www.googleapis.com/calendar/v3/calendars/team%40example.com"
        "/events/evt%20456%2Fx"
    )


@pytest.mark.parametrize("status_code", [401, 403])
async def test_delete_event_auth_failure_raises_authentication_error(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"code": status_code}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(CalendarAuthenticationError):
            await provider.delete_event(event_id="evt1")


@pytest.mark.parametrize("status_code", [400, 404, 500])
async def test_delete_event_failure_raises_provider_error(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"code": status_code}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(CalendarProviderError) as excinfo:
            await provider.delete_event(event_id="evt1")
    assert type(excinfo.value) is CalendarProviderError


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
