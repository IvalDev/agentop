"""Exceptions raised by the tool engine.

``ToolRegistrationError`` and ``ToolNotFoundError`` are raised while building
and reading the registry. ``ToolPermissionError`` and ``ToolExecutionError``
are part of the public surface reserved for the approval/execution layers
(T7 approvals, T9 orchestrator); ``execute_tool`` itself reports those
conditions as ``ToolExecutionResult(status="error")`` values instead of
raising, so API handlers never need to translate tool errors into HTTP
responses by hand.
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
