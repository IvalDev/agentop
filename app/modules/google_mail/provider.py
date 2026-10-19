"""Gmail provider abstraction: protocol, Google adapter, named registry (G1/G2/G3).

The connection tester, the scope options hook, and the G3 chat tools talk to
this seam, never to Google directly:

- :class:`GmailProvider` — the runtime-checkable protocol every adapter
  fulfils: ``get_profile`` and ``list_labels`` (G1/G2) plus the G3
  operations ``list_messages``, ``get_message``, ``get_message_metadata``
  (G3.1: the per-hit read of the enriched search), ``send_message``, and
  ``modify_message``;
- :class:`GoogleGmailProvider` — the MVP adapter: ``get_profile`` reads the
  mailbox profile through the Gmail API with a Bearer header and nothing
  else, proving the connection end to end and surfacing the connected
  address; ``list_labels`` reads ``users.labelsList`` for the G2 scope
  step; the G3 methods read, send, and relabel messages
  (``users.messagesList``/``Get``/``Send``/``Modify``);
- the registry — provider-name → **factory**, because tokens are per-user
  while the registry is process-wide (same single-process contract as the
  calendar provider registry): :func:`register_provider` overwrites like
  :func:`register_tester <app.modules.onboarding.testers.register_tester>`,
  and :func:`get_provider` raises :class:`UnknownProviderError` for a name
  no module registered (the engine surfaces that as 502).

Errors split by who must react: :class:`GmailAuthenticationError` (401/403
— the stored tokens are dead or revoked) makes the tester answer ``False``
and the scope options hook fail closed; :class:`GmailNotFoundError` (404) is
a :class:`GmailProviderError` the search uses to skip a message deleted
between list and get (every other caller keeps the provider-error mapping);
:class:`GmailProviderError` and
transport errors mean "the call could not run", so the tester lets them
propagate (engine: 502) while the scope options hook maps them to
:class:`ScopeOptionsError
<app.modules.onboarding.options.ScopeOptionsError>` and the tools layer
surfaces them as masked ``tool_failed`` results (never a 500, never a
token leak).
"""

from collections.abc import Callable, Sequence
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

import httpx

#: Gmail API ``users.getProfile`` endpoint for the connected mailbox.
GOOGLE_GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

#: Gmail API ``users.labelsList`` endpoint for the mailbox's labels (G2).
GOOGLE_GMAIL_LABELS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/labels"

#: Gmail API ``users.messages`` collection endpoint (G3: list; send appends ``/send``).
GOOGLE_GMAIL_MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"

#: Headers a ``format=metadata`` read asks for (G3.1: what a search hit shows).
METADATA_HEADERS = ("Subject", "From", "Date")


class GmailProviderError(Exception):
    """The provider answered in a way the caller cannot use (engine: 502)."""


class GmailAuthenticationError(GmailProviderError):
    """The provider rejected the stored tokens (401/403) — connection is dead."""


class GmailNotFoundError(GmailProviderError):
    """The requested resource does not exist (404) — e.g. a message deleted meanwhile."""


@runtime_checkable
class GmailProvider(Protocol):
    """What a Gmail backend must offer the tools, the tester, and G2."""

    async def get_profile(self) -> dict[str, Any]: ...

    async def list_labels(self) -> list[dict[str, Any]]: ...

    async def list_messages(
        self,
        *,
        label_ids: Sequence[str] | None = None,
        query: str | None = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]: ...

    async def get_message(self, message_id: str) -> dict[str, Any]: ...

    async def get_message_metadata(self, message_id: str) -> dict[str, Any]: ...

    async def send_message(
        self, *, raw_mime_b64url: str, thread_id: str | None = None
    ) -> dict[str, Any]: ...

    async def modify_message(
        self,
        *,
        message_id: str,
        add_label_ids: Sequence[str] | None = None,
        remove_label_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]: ...


class GoogleGmailProvider:
    """Gmail adapter bound to one user's access token.

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
        """Map 401/403 to the auth error, 404 to not-found, and the rest to a provider error."""
        if response.status_code in (401, 403):
            raise GmailAuthenticationError(f"Gmail API answered {response.status_code}")
        if response.status_code == 404:
            raise GmailNotFoundError("Gmail API answered 404")
        if response.is_error:
            raise GmailProviderError(f"Gmail API answered {response.status_code}")

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        """Decode the response as a JSON object or raise a provider error."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise GmailProviderError("Gmail API answered non-JSON") from exc
        if not isinstance(payload, dict):
            raise GmailProviderError("Gmail API response is not an object")
        return payload

    @staticmethod
    def _require_id(payload: dict[str, Any]) -> dict[str, Any]:
        """Return the payload when it carries a string ``id`` (message resource)."""
        if not isinstance(payload.get("id"), str):
            raise GmailProviderError("Gmail API response has no id")
        return payload

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

    async def list_messages(
        self,
        *,
        label_ids: Sequence[str] | None = None,
        query: str | None = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """List messages (``users.messagesList``) and return the page's ``messages``.

        ``label_ids`` goes to the API as repeated ``labelIds`` parameters (the
        contract's label filter — never query-syntax labels); ``query`` is the
        free-text ``q``. Gmail omits the ``messages`` key entirely when nothing
        matches, which is the empty page.

        Raises:
            GmailAuthenticationError: the API answered 401/403.
            GmailProviderError: any other non-2xx status, a non-JSON body,
                or a ``messages`` value that is not a list.
            httpx.RequestError: the API could not be reached (engine: 502).
        """
        params: dict[str, Any] = {"maxResults": max_results}
        if label_ids:
            params["labelIds"] = list(label_ids)
        if query is not None:
            params["q"] = query
        response = await self._request("GET", GOOGLE_GMAIL_MESSAGES_URL, params=params)
        self._raise_for_status(response)
        payload = self._json_object(response)
        messages = payload.get("messages", [])
        if not isinstance(messages, list):
            raise GmailProviderError("Gmail API response has no messages list")
        return messages

    async def get_message(self, message_id: str) -> dict[str, Any]:
        """Read one message in full (``users.messagesGet``, ``format=full``).

        The payload carries ``labelIds`` — what the tools' scope enforcement
        checks — plus the MIME ``payload`` the tools flatten for the model.

        Raises:
            GmailAuthenticationError: the API answered 401/403.
            GmailProviderError: any other non-2xx status, a non-JSON body,
                or a payload without a string ``id``.
            httpx.RequestError: the API could not be reached (engine: 502).
        """
        url = f"{GOOGLE_GMAIL_MESSAGES_URL}/{quote(message_id, safe='')}"
        response = await self._request("GET", url, params={"format": "full"})
        self._raise_for_status(response)
        return self._require_id(self._json_object(response))

    async def get_message_metadata(self, message_id: str) -> dict[str, Any]:
        """Read one message's metadata (``users.messagesGet``, ``format=metadata``).

        Cheaper than ``format=full``: no body is downloaded, only ``labelIds``,
        ``snippet``, and the :data:`METADATA_HEADERS` (repeated
        ``metadataHeaders`` parameters) under ``payload.headers``.

        Raises:
            GmailAuthenticationError: the API answered 401/403.
            GmailNotFoundError: the API answered 404 (deleted meanwhile).
            GmailProviderError: any other non-2xx status, a non-JSON body,
                or a payload without a string ``id``.
            httpx.RequestError: the API could not be reached (engine: 502).
        """
        url = f"{GOOGLE_GMAIL_MESSAGES_URL}/{quote(message_id, safe='')}"
        params = {"format": "metadata", "metadataHeaders": list(METADATA_HEADERS)}
        response = await self._request("GET", url, params=params)
        self._raise_for_status(response)
        return self._require_id(self._json_object(response))

    async def send_message(
        self, *, raw_mime_b64url: str, thread_id: str | None = None
    ) -> dict[str, Any]:
        """Send one RFC 822 message (``users.messagesSend``) and return its resource.

        ``raw_mime_b64url`` is the base64url-encoded MIME the tools built;
        ``thread_id`` (when replying) becomes the body's ``threadId``.

        Raises:
            GmailAuthenticationError: the API answered 401/403.
            GmailProviderError: any other non-2xx status, a non-JSON body,
                or a payload without a string ``id``.
            httpx.RequestError: the API could not be reached (engine: 502).
        """
        body: dict[str, Any] = {"raw": raw_mime_b64url}
        if thread_id is not None:
            body["threadId"] = thread_id
        # ``users.messages.send`` is the ``/send`` sub-resource; the bare collection
        # URL is ``users.messages.insert``, which stores a copy without sending it.
        response = await self._request("POST", f"{GOOGLE_GMAIL_MESSAGES_URL}/send", json=body)
        self._raise_for_status(response)
        return self._require_id(self._json_object(response))

    async def modify_message(
        self,
        *,
        message_id: str,
        add_label_ids: Sequence[str] | None = None,
        remove_label_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Change one message's labels (``users.messagesModify``).

        ``add_label_ids``/``remove_label_ids`` become the body's
        ``addLabelIds``/``removeLabelIds``; absent lists are omitted, never
        sent empty.

        Raises:
            GmailAuthenticationError: the API answered 401/403.
            GmailProviderError: any other non-2xx status, a non-JSON body,
                or a payload without a string ``id``.
            httpx.RequestError: the API could not be reached (engine: 502).
        """
        url = f"{GOOGLE_GMAIL_MESSAGES_URL}/{quote(message_id, safe='')}/modify"
        body: dict[str, Any] = {}
        if add_label_ids:
            body["addLabelIds"] = list(add_label_ids)
        if remove_label_ids:
            body["removeLabelIds"] = list(remove_label_ids)
        response = await self._request("POST", url, json=body)
        self._raise_for_status(response)
        return self._require_id(self._json_object(response))


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
    "GOOGLE_GMAIL_MESSAGES_URL",
    "GOOGLE_GMAIL_PROFILE_URL",
    "METADATA_HEADERS",
    "GmailAuthenticationError",
    "GmailNotFoundError",
    "GmailProvider",
    "GmailProviderError",
    "GoogleGmailProvider",
    "UnknownProviderError",
    "get_provider",
    "provider_registry",
    "register_provider",
]
