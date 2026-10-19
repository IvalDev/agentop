"""Connection tester of the google_mail module: one real read proves access.

Receives the decrypted secret answers plus the non-secret ones (in memory for
this call only, per the :class:`ConnectionContext
<app.modules.onboarding.testers.ConnectionContext>` contract) and answers:

- ``False`` — no usable ``access_token`` (missing, empty, or not a string), or
  the provider rejected the tokens (401/403). The token check runs **before**
  any provider lookup, so a not-yet-registered provider can never turn a
  credential problem into a 502;
- ``True`` — exactly one ``get_profile`` call (``users.getProfile``) succeeded:
  enough to prove the connection end to end, cheap enough for a settings page;
- raises — unknown provider, provider errors, network failures: the check
  itself could not run, and the onboarding engine owns that mapping (502),
  not this function.

No refresh: a dead access token surfaces as ``False`` and the health seam
owns reconnection. The tester never logs or persists the context.
"""

from app.modules.google_mail.provider import GmailAuthenticationError, get_provider
from app.modules.onboarding.testers import ConnectionContext


async def google_mail_connection_tester(context: ConnectionContext) -> bool:
    """Verify the stored Google connection by reading the Gmail profile."""
    token = context.credentials.get("access_token")
    if not isinstance(token, str) or not token:
        return False
    factory = get_provider(context.provider)
    provider = factory(access_token=token)
    try:
        await provider.get_profile()
    except GmailAuthenticationError:
        return False
    return True


__all__ = ["google_mail_connection_tester"]
