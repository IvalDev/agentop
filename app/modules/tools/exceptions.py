"""Exceptions raised by the tool engine.

``ToolRegistrationError`` and ``ToolNotFoundError`` are raised while building
and reading the registry. ``ToolPermissionError`` and ``ToolExecutionError``
are part of the public surface reserved for the approval/execution layers
(T7 approvals, T9 orchestrator); ``execute_tool`` itself reports those
conditions as ``ToolExecutionResult(status="error")`` values instead of
raising, so API handlers never need to translate tool errors into HTTP
responses by hand.

Tools raise :class:`ToolPrecheckError` from their ``precheck`` to refuse a
call (the executor turns it into a ``precheck_failed`` result). A handler
whose refusal is only known once it runs — a remote side that owns the final
check — raises :class:`ToolRefusalError`, the one handler exception the
executor reports with its reason instead of masking it.
:class:`ToolApprovalRoleError` is raised by
:func:`~app.modules.tools.executor.resolve_approval_role` when a tool's
``approval_role`` resolver fails, so no caller ever falls back to a guessed
approver.
"""


class ToolError(Exception):
    """Base class for every tool-engine error."""


class ToolRegistrationError(ToolError):
    """A ToolSpec violated a registration invariant (duplicate, bad name, policy)."""


class ToolNotFoundError(ToolError):
    """No tool with the requested name is registered."""


class ToolPermissionError(ToolError):
    """The caller's role is below the tool's required role."""


class ToolExecutionError(ToolError):
    """A tool handler failed while executing."""


class ToolApprovalRoleError(ToolError):
    """A tool's ``approval_role`` resolver raised or returned no valid role."""


class ToolPrecheckError(ToolError):
    """A tool's precheck refused the call against current tenant state.

    ``reason`` must be short and safe to show: the executor returns it to the
    caller as ``precheck_failed: <reason>``, the agent shows it to the model,
    and the approvals service stores it as the request's execution error. It
    must never carry internals, secrets, or another tenant's data.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ToolRefusalError(ToolPrecheckError):
    """A handler refused the call with a user-safe reason after its precheck passed.

    The executor masks every other handler exception — a bare
    :class:`ToolPrecheckError` included — as ``tool_failed``; this one is
    reported exactly like a precheck refusal (``precheck_failed: <reason>``).
    The same rules for ``reason`` apply.
    """
