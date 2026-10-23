"""Google Mail provider protocol, Google adapter, and provider registry (G1/G3).

Contract under test (the tester's and tools' HTTP seam, mirroring
``tests/test_calendar_provider.py``):

- :class:`GmailProvider` is a runtime-checkable protocol with
  ``get_profile`` and ``list_labels`` (G2) plus the G3 operations:
  ``list_messages``, ``get_message``, ``get_message_metadata``,
  ``send_message``, ``modify_message``;
  the Google adapter implements all of them;
- ``get_profile`` calls the Gmail API ``users.getProfile`` endpoint with a
  Bearer header and nothing else, and returns the payload including the
  connected ``emailAddress``;
- ``list_messages`` GETs ``users.messagesList`` with ``maxResults`` and,
  when given, repeated ``labelIds`` (never query-syntax labels) and ``q``
  (free text only); an omitted ``messages`` key is the API's empty answer;
- ``get_message`` GETs ``users.messagesGet`` with ``format=full``;
  ``get_message_metadata`` GETs the same endpoint with ``format=metadata``
  and repeated ``metadataHeaders`` (``Subject``, ``From``, ``Date``) — the
  per-hit read behind the enriched ``search_emails`` (G3.1);
- ``send_message`` POSTs ``users.messagesSend`` with the base64url ``raw``
  MIME and an optional ``threadId``; ``modify_message`` POSTs
  ``users.messagesModify`` with ``addLabelIds``/``removeLabelIds``;
- ``get_attachment`` GETs ``users.messages.attachments.get`` and decodes the
  base64url ``data`` (with or without padding) to the attachment's bytes;
- ``send_message_upload`` sends a large RFC 822 message through the resumable
  upload protocol: a POST to the ``/upload`` send URI with
  ``uploadType=resumable``, the ``X-Upload-Content-*`` headers and the
  ``threadId`` metadata, then a PUT of the raw bytes to the session URI the
  ``Location`` header names;
- 401/403 → :class:`GmailAuthenticationError` (the tester answers False);
  404 → :class:`GmailNotFoundError` (a :class:`GmailProviderError`
  subclass, so a search can skip a message deleted between list and get);
  any other failure → :class:`GmailProviderError` (the engine surfaces 502);
  a non-JSON body or a payload missing its key field is a provider error
  too — never a token leak, never a 500 from the tools layer;
- the registry stores *factories* keyed by provider name (tokens are
  per-user, the registry is process-wide): ``register_provider`` overwrites
  like ``register_tester``, ``get_provider`` raises :class:`UnknownProviderError`.
"""

import base64
import json
from typing import Any

import httpx
import pytest

from app.modules.google_mail.provider import (
    GOOGLE_GMAIL_MESSAGES_URL,
    GOOGLE_GMAIL_PROFILE_URL,
    GmailAuthenticationError,
    GmailNotFoundError,
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


def test_gmail_provider_protocol_declares_the_attachment_operations() -> None:
    legacy = (
        "get_profile",
        "list_labels",
        "list_messages",
        "get_message",
        "get_message_metadata",
        "send_message",
        "modify_message",
    )

    def stub(*names: str) -> object:
        return type("Stub", (), {name: lambda self: None for name in names})()

    assert isinstance(stub(*legacy, "get_attachment", "send_message_upload"), GmailProvider)
    assert not isinstance(stub(*legacy, "get_attachment"), GmailProvider)
    assert not isinstance(stub(*legacy, "send_message_upload"), GmailProvider)


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


# --- list_messages (users.messagesList) ----------------------------------------


async def test_list_messages_sends_params_and_returns_messages() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["authorization"] = request.headers["Authorization"]
        seen["label_ids"] = request.url.params.get_list("labelIds")
        seen["query"] = request.url.params.get("q")
        seen["max_results"] = request.url.params.get("maxResults")
        return httpx.Response(200, json={"messages": [{"id": "m1"}], "resultSizeEstimate": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        messages = await provider.list_messages(
            label_ids=["Label_7"], query="invoice", max_results=5
        )

    assert messages == [{"id": "m1"}]
    assert seen["method"] == "GET"
    assert seen["url"].startswith(GOOGLE_GMAIL_MESSAGES_URL)
    assert seen["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert seen["label_ids"] == ["Label_7"]  # repeated param, not query syntax
    assert seen["query"] == "invoice"
    assert seen["max_results"] == "5"


async def test_list_messages_omits_absent_params() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["label_ids"] = request.url.params.get_list("labelIds")
        seen["query"] = request.url.params.get("q")
        # Gmail omits the ``messages`` key entirely when nothing matches.
        return httpx.Response(200, json={"resultSizeEstimate": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        messages = await provider.list_messages()

    assert messages == []
    assert seen["label_ids"] == []
    assert seen["query"] is None


async def test_list_messages_rejects_a_messages_value_that_is_not_a_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": "nope"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError, match="messages"):
            await provider.list_messages()


@pytest.mark.parametrize("status", [401, 403])
async def test_list_messages_maps_auth_statuses_to_authentication_error(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"status": "UNAUTHENTICATED"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailAuthenticationError):
            await provider.list_messages()


async def test_list_messages_maps_other_errors_to_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError):
            await provider.list_messages()


# --- get_message (users.messagesGet) -------------------------------------------


async def test_get_message_requests_format_full_and_returns_the_payload() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url.copy_with(query=None))
        seen["method"] = request.method
        seen["format"] = request.url.params.get("format")
        return httpx.Response(200, json={"id": "m1", "labelIds": ["INBOX"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        message = await provider.get_message("m1")

    assert message == {"id": "m1", "labelIds": ["INBOX"]}
    assert seen["method"] == "GET"
    assert seen["url"] == f"{GOOGLE_GMAIL_MESSAGES_URL}/m1"
    assert seen["format"] == "full"


async def test_get_message_requires_an_id_in_the_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"labelIds": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError, match="id"):
            await provider.get_message("m1")


@pytest.mark.parametrize("status", [401, 403])
async def test_get_message_maps_auth_statuses_to_authentication_error(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"status": "UNAUTHENTICATED"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailAuthenticationError):
            await provider.get_message("m1")


async def test_get_message_maps_other_errors_to_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError):
            await provider.get_message("m1")


async def test_get_message_rejects_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError, match="non-JSON"):
            await provider.get_message("m1")


# --- get_message_metadata (users.messagesGet, format=metadata) -------------------


async def test_get_message_metadata_requests_the_three_headers_and_returns_the_payload() -> None:
    seen: dict[str, Any] = {}
    payload = {
        "id": "m1",
        "threadId": "t-1",
        "labelIds": ["INBOX"],
        "snippet": "Please pay invoice 42.",
        "payload": {"headers": [{"name": "Subject", "value": "Invoice 42"}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url.copy_with(query=None))
        seen["method"] = request.method
        seen["authorization"] = request.headers["Authorization"]
        seen["format"] = request.url.params.get("format")
        seen["headers"] = request.url.params.get_list("metadataHeaders")
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        message = await provider.get_message_metadata("m1")

    assert message == payload
    assert seen["method"] == "GET"
    assert seen["url"] == f"{GOOGLE_GMAIL_MESSAGES_URL}/m1"
    assert seen["authorization"] == f"Bearer {ACCESS_TOKEN}"
    # Metadata only: the body is never downloaded for a search hit.
    assert seen["format"] == "metadata"
    assert seen["headers"] == ["Subject", "From", "Date"]


async def test_get_message_metadata_maps_404_to_not_found_error() -> None:
    """A message deleted between list and get is distinguishable from other failures."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"status": "NOT_FOUND"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailNotFoundError) as caught:
            await provider.get_message_metadata("m1")

    # Still a provider error: callers that do not care keep their mapping.
    assert isinstance(caught.value, GmailProviderError)


async def test_get_message_metadata_requires_an_id_in_the_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"labelIds": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError, match="id"):
            await provider.get_message_metadata("m1")


@pytest.mark.parametrize("status", [401, 403])
async def test_get_message_metadata_maps_auth_statuses_to_authentication_error(
    status: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"status": "UNAUTHENTICATED"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailAuthenticationError):
            await provider.get_message_metadata("m1")


async def test_get_message_metadata_maps_other_errors_to_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError) as caught:
            await provider.get_message_metadata("m1")

    assert not isinstance(caught.value, GmailNotFoundError)


async def test_get_message_metadata_rejects_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError, match="non-JSON"):
            await provider.get_message_metadata("m1")


# --- send_message (users.messagesSend) ------------------------------------------


async def test_send_message_posts_raw_and_returns_the_payload() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = request.read()
        return httpx.Response(200, json={"id": "sent-1", "threadId": "t-9"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        payload = await provider.send_message(raw_mime_b64url="cmF3", thread_id="t-9")

    assert payload == {"id": "sent-1", "threadId": "t-9"}
    assert seen["method"] == "POST"
    assert seen["url"] == f"{GOOGLE_GMAIL_MESSAGES_URL}/send"
    assert b'"raw":"cmF3"' in seen["body"]
    assert b'"threadId":"t-9"' in seen["body"]


async def test_send_message_omits_thread_id_when_absent() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return httpx.Response(200, json={"id": "sent-2"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        await provider.send_message(raw_mime_b64url="cmF3")

    assert b"threadId" not in seen["body"]


async def test_send_message_requires_an_id_in_the_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"threadId": "t-9"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError, match="id"):
            await provider.send_message(raw_mime_b64url="cmF3")


@pytest.mark.parametrize("status", [401, 403])
async def test_send_message_maps_auth_statuses_to_authentication_error(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"status": "UNAUTHENTICATED"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailAuthenticationError):
            await provider.send_message(raw_mime_b64url="cmF3")


async def test_send_message_maps_other_errors_to_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError):
            await provider.send_message(raw_mime_b64url="cmF3")


async def test_send_message_still_posts_one_json_request_to_the_send_endpoint() -> None:
    """The JSON ``raw`` path is unchanged by the resumable upload sharing ``_request``."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        requests.append(request)
        return httpx.Response(200, json={"id": "sent-1", "threadId": "t-9"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        payload = await provider.send_message(raw_mime_b64url="cmF3", thread_id="t-9")

    assert payload == {"id": "sent-1", "threadId": "t-9"}
    assert len(requests) == 1
    (request,) = requests
    assert request.method == "POST"
    assert str(request.url) == f"{GOOGLE_GMAIL_MESSAGES_URL}/send"
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert request.headers["Content-Type"] == "application/json"
    assert "X-Upload-Content-Type" not in request.headers
    assert "X-Upload-Content-Length" not in request.headers
    assert json.loads(request.content) == {"raw": "cmF3", "threadId": "t-9"}


# --- modify_message (users.messagesModify) --------------------------------------


async def test_modify_message_posts_label_changes_and_returns_the_payload() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = request.read()
        return httpx.Response(200, json={"id": "m1", "labelIds": ["Label_9"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        payload = await provider.modify_message(
            message_id="m1", add_label_ids=["Label_9"], remove_label_ids=["INBOX"]
        )

    assert payload == {"id": "m1", "labelIds": ["Label_9"]}
    assert seen["method"] == "POST"
    assert seen["url"] == f"{GOOGLE_GMAIL_MESSAGES_URL}/m1/modify"
    assert b'"addLabelIds":["Label_9"]' in seen["body"]
    assert b'"removeLabelIds":["INBOX"]' in seen["body"]


async def test_modify_message_omits_absent_label_lists() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return httpx.Response(200, json={"id": "m1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        await provider.modify_message(message_id="m1")

    assert b"addLabelIds" not in seen["body"]
    assert b"removeLabelIds" not in seen["body"]


async def test_modify_message_requires_an_id_in_the_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"labelIds": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError, match="id"):
            await provider.modify_message(message_id="m1")


@pytest.mark.parametrize("status", [401, 403])
async def test_modify_message_maps_auth_statuses_to_authentication_error(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"status": "UNAUTHENTICATED"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailAuthenticationError):
            await provider.modify_message(message_id="m1")


async def test_modify_message_maps_other_errors_to_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError):
            await provider.modify_message(message_id="m1")


# --- get_attachment (users.messages.attachments.get) ----------------------------

#: Every byte value, so the base64url form uses ``-``/``_`` and needs padding.
ATTACHMENT_BYTES = bytes(range(256))
ATTACHMENT_PADDED = base64.urlsafe_b64encode(ATTACHMENT_BYTES).decode("ascii")
ATTACHMENT_UNPADDED = ATTACHMENT_PADDED.rstrip("=")


def test_attachment_fixture_exercises_the_url_safe_alphabet_and_padding() -> None:
    assert "-" in ATTACHMENT_PADDED and "_" in ATTACHMENT_PADDED
    assert ATTACHMENT_PADDED.endswith("=") and not ATTACHMENT_UNPADDED.endswith("=")


@pytest.mark.parametrize(
    "data", [ATTACHMENT_PADDED, ATTACHMENT_UNPADDED], ids=["padded", "unpadded"]
)
async def test_get_attachment_gets_the_part_body_and_returns_the_decoded_bytes(
    data: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"attachmentId": "ANGj-d_9", "size": len(ATTACHMENT_BYTES), "data": data}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        content = await provider.get_attachment("m1", "ANGj-d_9")

    assert content == ATTACHMENT_BYTES
    assert len(requests) == 1
    (request,) = requests
    assert request.method == "GET"
    assert str(request.url) == f"{GOOGLE_GMAIL_MESSAGES_URL}/m1/attachments/ANGj-d_9"
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"


async def test_get_attachment_returns_empty_bytes_for_an_empty_part() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"size": 0, "data": ""})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        assert await provider.get_attachment("m1", "a1") == b""


@pytest.mark.parametrize(
    "payload",
    [{"attachmentId": "a1", "size": 3}, {"size": 3, "data": None}, {"size": 3, "data": 123}],
)
async def test_get_attachment_requires_string_data(payload: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError, match="data"):
            await provider.get_attachment("m1", "a1")


@pytest.mark.parametrize(
    "data", ["a", "ab!c", "é"], ids=["bad-length", "bad-alphabet", "non-ascii"]
)
async def test_get_attachment_rejects_data_that_is_not_base64url(data: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"size": 1, "data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError, match="data"):
            await provider.get_attachment("m1", "a1")


async def test_get_attachment_rejects_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError, match="non-JSON"):
            await provider.get_attachment("m1", "a1")


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, GmailAuthenticationError),
        (403, GmailAuthenticationError),
        (404, GmailNotFoundError),
        (500, GmailProviderError),
    ],
)
async def test_get_attachment_maps_http_errors(status: int, error: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": status}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(error) as caught:
            await provider.get_attachment("m1", "a1")

    assert type(caught.value) is error


# --- send_message_upload (users.messagesSend, resumable upload) -----------------

#: The resumable upload URI, spelled out so a constant change cannot hide a drift.
UPLOAD_INITIATION_URL = (
    "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send?uploadType=resumable"
)
#: The session URI Google returns in ``Location`` (its own query must survive verbatim).
SESSION_URL = (
    "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send"
    "?uploadType=resumable&upload_id=xa298sd_sdlkj2"
)
#: An RFC 822 message whose body carries every byte value, CRLF included.
UPLOAD_MIME = b"From: agent@acme.test\r\nSubject: Report\r\n\r\n" + bytes(range(256))


def _upload_handler(
    requests: list[httpx.Request],
    *,
    initiation: httpx.Response | None = None,
    upload: httpx.Response | None = None,
) -> Any:
    """Answer the initiation POST, then the session PUT, recording both requests."""

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        requests.append(request)
        if request.method == "POST":
            return initiation or httpx.Response(200, headers={"Location": SESSION_URL})
        return upload or httpx.Response(201, json={"id": "sent-1", "threadId": "t-9"})

    return handler


@pytest.mark.parametrize("upload_status", [200, 201])
async def test_send_message_upload_initiates_a_session_and_puts_the_raw_message(
    upload_status: int,
) -> None:
    requests: list[httpx.Request] = []
    resource = {"id": "sent-1", "threadId": "t-9", "labelIds": ["SENT"]}
    handler = _upload_handler(requests, upload=httpx.Response(upload_status, json=resource))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        payload = await provider.send_message_upload(mime=UPLOAD_MIME, thread_id="t-9")

    assert payload == resource
    assert [request.method for request in requests] == ["POST", "PUT"]
    initiation, upload = requests

    assert str(initiation.url) == UPLOAD_INITIATION_URL
    assert initiation.url.params.get("uploadType") == "resumable"
    assert initiation.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert initiation.headers["X-Upload-Content-Type"] == "message/rfc822"
    assert initiation.headers["X-Upload-Content-Length"] == str(len(UPLOAD_MIME))
    assert initiation.headers["Content-Type"] == "application/json"
    assert json.loads(initiation.content) == {"threadId": "t-9"}

    assert str(upload.url) == SESSION_URL
    assert upload.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert upload.headers["Content-Type"] == "message/rfc822"
    assert upload.headers["Content-Length"] == str(len(UPLOAD_MIME))
    assert "X-Upload-Content-Type" not in upload.headers
    assert upload.content == UPLOAD_MIME


async def test_send_message_upload_sends_empty_metadata_without_a_thread() -> None:
    requests: list[httpx.Request] = []
    handler = _upload_handler(requests)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        payload = await provider.send_message_upload(mime=UPLOAD_MIME)

    assert payload == {"id": "sent-1", "threadId": "t-9"}
    initiation, upload = requests
    assert str(initiation.url) == UPLOAD_INITIATION_URL
    assert json.loads(initiation.content) == {}
    assert upload.content == UPLOAD_MIME


async def test_send_message_upload_requires_the_session_location() -> None:
    requests: list[httpx.Request] = []
    handler = _upload_handler(requests, initiation=httpx.Response(200))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError, match="Location"):
            await provider.send_message_upload(mime=UPLOAD_MIME)

    assert [request.method for request in requests] == ["POST"]  # nothing uploaded


@pytest.mark.parametrize(
    "location",
    [
        "http://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send?upload_id=x",
        "https://uploads.example.test/upload?upload_id=x",
        "https://googleapis.com.example.test/upload?upload_id=x",
        "/upload/gmail/v1/users/me/messages/send?upload_id=x",
    ],
)
async def test_send_message_upload_refuses_a_session_outside_googleapis(location: str) -> None:
    requests: list[httpx.Request] = []
    handler = _upload_handler(
        requests, initiation=httpx.Response(200, headers={"Location": location})
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError, match="Location"):
            await provider.send_message_upload(mime=UPLOAD_MIME)

    assert [request.method for request in requests] == ["POST"]  # the token never left


async def test_send_message_upload_accepts_the_documented_www_googleapis_session() -> None:
    requests: list[httpx.Request] = []
    location = "https://www.googleapis.com/upload/gmail/v1/users/me/messages/send?upload_id=x"
    handler = _upload_handler(
        requests, initiation=httpx.Response(200, headers={"Location": location})
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        await provider.send_message_upload(mime=UPLOAD_MIME)

    assert str(requests[1].url) == location


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, GmailAuthenticationError), (403, GmailAuthenticationError), (500, GmailProviderError)],
)
async def test_send_message_upload_maps_initiation_errors(
    status: int, error: type[Exception]
) -> None:
    requests: list[httpx.Request] = []
    failure = httpx.Response(status, headers={"Location": SESSION_URL}, json={"error": {}})
    handler = _upload_handler(requests, initiation=failure)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(error) as caught:
            await provider.send_message_upload(mime=UPLOAD_MIME)

    assert type(caught.value) is error
    assert [request.method for request in requests] == ["POST"]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, GmailAuthenticationError),
        (403, GmailAuthenticationError),
        (404, GmailNotFoundError),
        (500, GmailProviderError),
    ],
)
async def test_send_message_upload_maps_upload_errors(status: int, error: type[Exception]) -> None:
    requests: list[httpx.Request] = []
    handler = _upload_handler(requests, upload=httpx.Response(status, json={"error": {}}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(error) as caught:
            await provider.send_message_upload(mime=UPLOAD_MIME)

    assert type(caught.value) is error
    assert [request.method for request in requests] == ["POST", "PUT"]


async def test_send_message_upload_requires_an_id_in_the_message() -> None:
    requests: list[httpx.Request] = []
    handler = _upload_handler(requests, upload=httpx.Response(201, json={"threadId": "t-9"}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError, match="id"):
            await provider.send_message_upload(mime=UPLOAD_MIME)


async def test_send_message_upload_rejects_a_non_json_message() -> None:
    requests: list[httpx.Request] = []
    handler = _upload_handler(requests, upload=httpx.Response(201, text="<html>nope</html>"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError, match="non-JSON"):
            await provider.send_message_upload(mime=UPLOAD_MIME)


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
