"""Single-use approval grants: the executor's only proof of human approval.

Decision A7 says the model never authorizes. A free-form token string could
come from anywhere (a client, a log, the LLM itself), so ``execute_tool``
accepts an :class:`ApprovalGrant` instead and verifies it with
:func:`consume_grant`. A grant is *live* only when :func:`mint_grant` issued
it, which the approvals service does exclusively after it atomically claimed
an approved, unexpired, payload-bound ``ApprovalRequest``.

Verification (all must hold):

- the value is an ``ApprovalGrant`` minted in this process — any other value,
  including a string or a directly constructed ``ApprovalGrant``, fails;
- it was never presented before — presentation spends it, even when the
  check then fails, so a grant can never be replayed;
- its ``action_key`` equals the key the executor recomputes for the call.

The live set is process-local and weakly referenced: a grant exists only
between minting and the single ``execute_tool`` call it was minted for, so it
never crosses processes. This defends against forged, replayed, and
mismatched grants and against any value from outside the process; in-process
code is trusted (Python cannot hide module state from it).

This module is a leaf (standard library only) so the tools executor can
import it without depending on the approvals service.
"""

import hmac
import weakref
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, eq=False)
class ApprovalGrant:
    """Opaque proof that ``approval_id`` authorizes the call keyed ``action_key``.

    ``eq=False`` keeps identity semantics: two grants with equal fields are
    still different grants, and only the minted instance is live.
    """

    approval_id: UUID
    action_key: str


_live_grants: weakref.WeakSet[ApprovalGrant] = weakref.WeakSet()


def mint_grant(approval_id: UUID, action_key: str) -> ApprovalGrant:
    """Issue a live, single-use grant (approvals service only)."""
    grant = ApprovalGrant(approval_id=approval_id, action_key=action_key)
    _live_grants.add(grant)
    return grant


def consume_grant(grant: object, action_key: str) -> bool:
    """Spend ``grant`` and return whether it authorizes ``action_key``.

    ``True`` only for a live grant minted for exactly this key. The grant is
    removed from the live set before the key comparison, so a second
    presentation always fails.
    """
    if not isinstance(grant, ApprovalGrant) or grant not in _live_grants:
        return False
    _live_grants.discard(grant)
    return hmac.compare_digest(grant.action_key, action_key)
