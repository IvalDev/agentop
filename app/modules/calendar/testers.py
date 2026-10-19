"""Connection tester of the calendar module: one real read proves access (D8).

Receives the decrypted secret answers plus the non-secret ones (in memory for
this call only, per the :class:`ConnectionContext
<app.modules.onboarding.testers.ConnectionContext>` contract) and answers:

- ``False`` — no usable ``access_token`` (missing, empty, or not a string), or
  the provider rejected the tokens (401/403). The token check runs **before**
  any provider lookup, so a not-yet-registered provider can never turn a
  credential problem into a 502;
- ``True`` — exactly one ``list_events`` call against the primary calendar
  (``max_results=1``) succeeded: enough to prove the connection end to end,
  cheap enough for a settings page;
- raises — unknown provider, provider errors, network failures: the check
  itself could not run, and the onboarding engine owns that mapping (502),
  not this function.

No refresh in T1: a dead access token surfaces as ``False`` and T4's health
seam owns reconnection. The tester never logs or persists the context.
"""

from app.modules.calendar.provider import CalendarAuthenticationError, get_provider
from app.modules.onboarding.testers import ConnectionContext


async def calendar_connection_tester(context: ConnectionContext) -> bool:
    """Verify the stored Google connection by listing one calendar event."""
    token = context.credentials.get("access_token")
    if not isinstance(token, str) or not token:
        return False
    factory = get_provider(context.provider)
    provider = factory(access_token=token)
    try:
        await provider.list_events(calendar_id="primary", max_results=1)
    except CalendarAuthenticationError:
        return False
    return True


__all__ = ["calendar_connection_tester"]
