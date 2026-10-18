"""Gmail provider abstraction: protocol, Google adapter, named registry (G1/G2).

The connection tester and the scope options hook talk to this seam, never to
Google directly. G1 ships the call the tester needs; G2 adds the label read
the mailbox-scope step renders; G3 extends the protocol and the adapter with
the read/action tools:

- :class:`GmailProvider` — the runtime-checkable protocol every adapter
  fulfils (``get_profile`` and, since G2, ``list_labels``);
- :class:`GoogleGmailProvider` — the MVP adapter: ``get_profile`` reads the
  mailbox profile through the Gmail API with a Bearer header and nothing
  else, proving the connection end to end and surfacing the connected
  address; ``list_labels`` reads ``users.labelsList`` for the G2 scope step;
- the registry — provider-name → **factory**, because tokens are per-user
  while the registry is process-wide (same single-process contract as the
  calendar provider registry): :func:`register_provider` overwrites like
  :func:`register_tester <app.modules.onboarding.testers.register_tester>`,
  and :func:`get_provider` raises :class:`UnknownProviderError` for a name
  no module registered (the engine surfaces that as 502).

Errors split by who must react: :class:`GmailAuthenticationError` (401/403
— the stored tokens are dead or revoked) makes the tester answer ``False``
and the scope options hook fail closed; :class:`GmailProviderError` and
transport errors mean "the call could not run", so the tester lets them
propagate (engine: 502) while the scope options hook maps them to
:class:`ScopeOptionsError
<app.modules.onboarding.options.ScopeOptionsError>`.
"""

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

import httpx

#: Gmail API ``users.getProfile`` endpoint for the connected mailbox.
GOOGLE_GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

#: Gmail API ``users.labelsList`` endpoint for the mailbox's labels (G2).
GOOGLE_GMAIL_LABELS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/labels"


class GmailProviderError(Exception):
    """The provider answered in a way the caller cannot use (engine: 502)."""


class GmailAuthenticationError(GmailProviderError):
    """The provider rejected the stored tokens (401/403) — connection is dead."""


@runtime_checkable
class GmailProvider(Protocol):
    """What a Gmail backend must offer the tools, the tester, and G2."""

    async def get_profile(self) -> dict[str, Any]: ...

    async def list_labels(self) -> list[dict[str, Any]]: ...


class GoogleGmailProvider:
    """Gmail adapter bound to one user's access token.

    The token arrives at construction — the process-wide registry stores this
    class as a factory, and each verification/tool call builds an instance for
    one user's credentials, which then die with the call.
    """

    def __init__(self, access_token: str, client: httpx.AsyncClient | None = None) -> None:
        self._access_token = access_token
        self._client = client

    async def _request(self, method: str, url: str) -> httpx.Response:
        """Send one authenticated request (owned or injected client)."""
        headers = {"Authorization": f"Bearer {self._access_token}"}
        if self._client is not None:
            return await self._client.request(method, url, headers=headers)
        async with httpx.AsyncClient() as owned_client:
            return await owned_client.request(method, url, headers=headers)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        """Map 401/403 to the auth error and any other failure to a provider error."""
        if response.status_code in (401, 403):
            raise GmailAuthenticationError(f"Gmail API answered {response.status_code}")
        if response.is_error:
            raise GmailProviderError(f"Gmail API answered {response.status_code}")

    async def get_profile(self) -> dict[str, Any]:
        """Read the mailbox profile (``users.getProfile``) and return its payload.

        Raises:
            GmailAuthenticationError: the API answered 401/403.
            GmailProviderError: any other non-2xx status, a non-JSON body,
                or a payload without an ``emailAddress`` string.
            httpx.RequestError: the API could not be reached (engine: 502).
        """
        response = await self._request("GET", GOOGLE_GMAIL_PROFILE_URL)
        self._raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GmailProviderError("Gmail API answered non-JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("emailAddress"), str):
            raise GmailProviderError("Gmail API response has no emailAddress")
        return payload

    async def list_labels(self) -> list[dict[str, Any]]:
        """Read the mailbox labels (``users.labelsList``) and return ``labels``.

        The G2 scope step renders these (system + user labels) as its choices.

        Raises:
            GmailAuthenticationError: the API answered 401/403.
            GmailProviderError: any other non-2xx status, a non-JSON body,
                or a payload without a ``labels`` list.
            httpx.RequestError: the API could not be reached (engine: 502).
        """
        response = await self._request("GET", GOOGLE_GMAIL_LABELS_URL)
        self._raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GmailProviderError("Gmail API answered non-JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("labels"), list):
            raise GmailProviderError("Gmail API response has no labels list")
        return payload["labels"]


#: Process-wide registry: provider name → factory building a per-user adapter.
provider_registry: dict[str, Callable[..., GmailProvider]] = {}


class UnknownProviderError(Exception):
    """No factory registered under that provider name (engine: 502)."""


def register_provider(provider_key: str, factory: Callable[..., GmailProvider]) -> None:
    """Register ``factory`` under ``provider_key`` (overwrites, like register_tester)."""
    provider_registry[provider_key] = factory


def get_provider(provider_key: str) -> Callable[..., GmailProvider]:
    """Return the factory registered under ``provider_key``.

    Raises:
        UnknownProviderError: no factory for that name — one fixed message,
            so a misconfigured provider never echoes internals.
    """
    try:
        return provider_registry[provider_key]
    except KeyError:
        raise UnknownProviderError(f"unknown Gmail provider: {provider_key!r}") from None


__all__ = [
    "GOOGLE_GMAIL_LABELS_URL",
    "GOOGLE_GMAIL_PROFILE_URL",
    "GmailAuthenticationError",
    "GmailProvider",
    "GmailProviderError",
    "GoogleGmailProvider",
    "UnknownProviderError",
    "get_provider",
    "provider_registry",
    "register_provider",
]
