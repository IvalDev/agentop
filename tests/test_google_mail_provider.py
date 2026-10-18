"""Google Mail provider protocol, Google adapter, and provider registry (G1/G3).

Contract under test (the tester's and tools' HTTP seam, mirroring
``tests/test_calendar_provider.py``):

- :class:`GmailProvider` is a runtime-checkable protocol with
  ``get_profile`` and ``list_labels`` (G2) plus the four G3 operations:
  ``list_messages``, ``get_message``, ``send_message``, ``modify_message``;
  the Google adapter implements all of them;
- ``get_profile`` calls the Gmail API ``users.getProfile`` endpoint with a
  Bearer header and nothing else, and returns the payload including the
  connected ``emailAddress``;
- ``list_messages`` GETs ``users.messagesList`` with ``maxResults`` and,
  when given, repeated ``labelIds`` (never query-syntax labels) and ``q``
  (free text only); an omitted ``messages`` key is the API's empty answer;
- ``get_message`` GETs ``users.messagesGet`` with ``format=full``;
- ``send_message`` POSTs ``users.messagesSend`` with the base64url ``raw``
  MIME and an optional ``threadId``; ``modify_message`` POSTs
  ``users.messagesModify`` with ``addLabelIds``/``removeLabelIds``;
- 401/403 → :class:`GmailAuthenticationError` (the tester answers False);
  any other failure → :class:`GmailProviderError` (the engine surfaces 502);
  a non-JSON body or a payload missing its key field is a provider error
  too — never a token leak, never a 500 from the tools layer;
- the registry stores *factories* keyed by provider name (tokens are
  per-user, the registry is process-wide): ``register_provider`` overwrites
  like ``register_tester``, ``get_provider`` raises :class:`UnknownProviderError`.
"""

from typing import Any

import httpx
import pytest

from app.modules.google_mail.provider import (
    GOOGLE_GMAIL_MESSAGES_URL,
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
    assert seen["url"] == GOOGLE_GMAIL_MESSAGES_URL
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
