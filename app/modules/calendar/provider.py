"""Calendar provider abstraction: protocol, Google adapter, named registry (D7).

The chat tools and the connection tester talk to this seam, never to Google
directly:

- :class:`CalendarProvider` — the runtime-checkable protocol every adapter
  fulfils (``list_events`` / ``create_event`` / ``delete_event``);
- :class:`GoogleCalendarProvider` — the MVP adapter: ``list_events`` reads,
  ``create_event`` inserts, and ``delete_event`` removes events through the
  Calendar API with a Bearer header and nothing else. Create sends
  ``dateTime`` ISO 8601 values with offset (all-day ``date`` events are out
  of the MVP scope);
- the registry — provider-name → **factory**, because tokens are per-user
  while the registry is process-wide (same single-process contract as the
  tool registry and the tester registry): ``register_provider`` overwrites
  like :func:`register_tester <app.modules.onboarding.testers.register_tester>`,
  and :func:`get_provider` raises :class:`UnknownProviderError` for a name no
  module registered (the engine surfaces that as 502).

Errors split by who must react: :class:`CalendarAuthenticationError` (401/403
— the stored tokens are dead or revoked) makes the tester answer ``False``;
:class:`CalendarProviderError` and transport errors mean "the check could not
run", so the tester lets them propagate and the engine answers 502.
"""

from collections.abc import Callable, Sequence
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

import httpx

#: Calendar API collection endpoint; ``{calendar_id}`` is percent-encoded.
GOOGLE_CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"


class CalendarProviderError(Exception):
    """The provider answered in a way the caller cannot use (engine: 502)."""


class CalendarAuthenticationError(CalendarProviderError):
    """The provider rejected the stored tokens (401/403) — connection is dead."""


@runtime_checkable
class CalendarProvider(Protocol):
    """What a calendar backend must offer the tools and the tester."""

    async def list_events(
        self,
        *,
        calendar_id: str = "primary",
        max_results: int = 10,
        time_min: str | None = None,
        time_max: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def create_event(
        self,
        *,
        calendar_id: str = "primary",
        summary: str,
        start: str,
        end: str,
        description: str | None = None,
        attendees: Sequence[str] | None = None,
    ) -> dict[str, Any]: ...

    async def delete_event(self, *, calendar_id: str = "primary", event_id: str) -> None: ...


class GoogleCalendarProvider:
    """Google Calendar adapter bound to one user's access token.

    The token arrives at construction — the process-wide registry stores this
    class as a factory, and each verification/tool call builds an instance for
    one user's credentials, which then die with the call.
    """

    def __init__(self, access_token: str, client: httpx.AsyncClient | None = None) -> None:
        self._access_token = access_token
        self._client = client

    async def _request(
        self, method: str, url: str, *, params: dict[str, Any] | None = None, json: Any = None
    ) -> httpx.Response:
        """Send one authenticated request (owned or injected client)."""
        headers = {"Authorization": f"Bearer {self._access_token}"}
        if self._client is not None:
            return await self._client.request(
                method, url, params=params, json=json, headers=headers
            )
        async with httpx.AsyncClient() as owned_client:
            return await owned_client.request(
                method, url, params=params, json=json, headers=headers
            )

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        """Map 401/403 to the auth error and any other failure to a provider error."""
        if response.status_code in (401, 403):
            raise CalendarAuthenticationError(f"calendar API answered {response.status_code}")
        if response.is_error:
            raise CalendarProviderError(f"calendar API answered {response.status_code}")

    async def list_events(
        self,
        *,
        calendar_id: str = "primary",
        max_results: int = 10,
        time_min: str | None = None,
        time_max: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch upcoming events of one calendar (``items`` of the API page).

        ``time_min``/``time_max`` are optional RFC 3339 bounds passed as the
        API's ``timeMin``/``timeMax`` (the chat tool's ``from``/``to`` range).

        Raises:
            CalendarAuthenticationError: the API answered 401/403.
            CalendarProviderError: any other non-2xx status, a non-JSON body,
                or a payload without an ``items`` list.
            httpx.RequestError: the API could not be reached (engine: 502).
        """
        url = GOOGLE_CALENDAR_EVENTS_URL.format(calendar_id=quote(calendar_id, safe=""))
        params: dict[str, Any] = {
            "maxResults": max_results,
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        if time_min is not None:
            params["timeMin"] = time_min
        if time_max is not None:
            params["timeMax"] = time_max
        response = await self._request("GET", url, params=params)
        self._raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise CalendarProviderError("calendar API answered non-JSON") from exc
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise CalendarProviderError("calendar API response has no items list")
        return items

    async def create_event(
        self,
        *,
        calendar_id: str = "primary",
        summary: str,
        start: str,
        end: str,
        description: str | None = None,
        attendees: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Insert one event (``dateTime`` ISO 8601 with offset) and return the API payload.

        Raises:
            CalendarAuthenticationError: the API answered 401/403.
            CalendarProviderError: any other non-2xx status or a non-JSON body.
            httpx.RequestError: the API could not be reached (engine: 502).
        """
        url = GOOGLE_CALENDAR_EVENTS_URL.format(calendar_id=quote(calendar_id, safe=""))
        body: dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
        }
        if description is not None:
            body["description"] = description
        if attendees:
            body["attendees"] = [{"email": email} for email in attendees]
        response = await self._request("POST", url, json=body)
        self._raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise CalendarProviderError("calendar API answered non-JSON") from exc
        if not isinstance(payload, dict):
            raise CalendarProviderError("calendar API response is not an object")
        return payload

    async def delete_event(self, *, calendar_id: str = "primary", event_id: str) -> None:
        """Delete one event (the API answers 204 on success).

        Raises:
            CalendarAuthenticationError: the API answered 401/403.
            CalendarProviderError: any other non-2xx status (404 included:
                the tool surfaces it as a failed cancellation).
            httpx.RequestError: the API could not be reached (engine: 502).
        """
        url = GOOGLE_CALENDAR_EVENTS_URL.format(calendar_id=quote(calendar_id, safe=""))
        response = await self._request("DELETE", f"{url}/{quote(event_id, safe='')}")
        self._raise_for_status(response)


#: Process-wide registry: provider name → factory building a per-user adapter.
provider_registry: dict[str, Callable[..., CalendarProvider]] = {}


class UnknownProviderError(Exception):
    """No factory registered under that provider name (engine: 502)."""


def register_provider(provider_key: str, factory: Callable[..., CalendarProvider]) -> None:
    """Register ``factory`` under ``provider_key`` (overwrites, like register_tester)."""
    provider_registry[provider_key] = factory


def get_provider(provider_key: str) -> Callable[..., CalendarProvider]:
    """Return the factory registered under ``provider_key``.

    Raises:
        UnknownProviderError: no factory for that name — one fixed message,
            so a misconfigured provider never echoes internals.
    """
    try:
        return provider_registry[provider_key]
    except KeyError:
        raise UnknownProviderError(f"unknown calendar provider: {provider_key!r}") from None


__all__ = [
    "CalendarAuthenticationError",
    "CalendarProvider",
    "CalendarProviderError",
    "GoogleCalendarProvider",
    "UnknownProviderError",
    "get_provider",
    "provider_registry",
    "register_provider",
]
