"""Exceptions raised by the approvals service.

Each maps to exactly one HTTP status in :mod:`app.modules.approvals.router`;
the service never raises ``HTTPException`` itself.
"""


class ApprovalError(Exception):
    """Base class for every approvals-service error."""


class ApprovalNotFoundError(ApprovalError):
    """No request with that id exists in the caller's tenant (cross-tenant included)."""


class ApprovalForbiddenError(ApprovalError):
    """The caller may not act on the request.

    Either their role does not satisfy the request's stored required role, or
    the tool is requester-only and the caller is not its requester.
    """


class ApprovalStateError(ApprovalError):
    """The requested transition is invalid for the request's current status."""


class ApprovalExpiredError(ApprovalError):
    """The request's TTL elapsed; it can no longer be decided or executed."""


class ApprovalBindingError(ApprovalError):
    """The stored payload no longer matches the action key it was approved under."""


class ApprovalNotRequiredError(ApprovalError):
    """The tool runs without approval, so an approval request makes no sense."""
