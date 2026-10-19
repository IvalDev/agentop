"""Exceptions raised by the onboarding service.

Every error maps to exactly one HTTP answer in the router
(:data:`~app.modules.onboarding.router._HTTP_STATUS`), and none of the
messages carry secret answers — validation messages name field *keys* and
constraints, never submitted values. All errors share
:class:`OnboardingError` for handlers that want one catch clause;
``UnknownModuleError`` keeps living in the catalog exceptions (the router
catches both bases, like the connections router).
"""


class OnboardingError(Exception):
    """Base class for every onboarding-service error."""


class ModuleInactiveError(OnboardingError):
    """The module exists in the catalog but is not active for this tenant (router: 409).

    A conflict, not a permission denial: onboarding configures the caller's
    own connection, but only an admin-enabled module can be configured —
    rephrased as ``409`` so the client learns to refresh the catalog state.
    """


class WrongStepError(OnboardingError):
    """The submitted step key is not the flow's current step (router: 409)."""


class OnboardingCompletedError(OnboardingError):
    """A step was submitted to an already-completed flow (router: 409)."""


class OnboardingAnswersError(OnboardingError):
    """Answers failed strict validation against the step's fields (router: 422).

    The message names the offending field keys and the rule they broke; it
    never echoes the submitted values (a value may be a secret).
    """


class RedirectStepError(OnboardingError):
    """A ``redirect`` step was submitted; it completes at the OAuth callback (router: 501).

    Foundations placeholder: the engine never invents a fake OAuth flow, so a
    redirect step is declarative-only until a real provider callback exists.
    """


class TesterNotRegisteredError(OnboardingError):
    """No connection tester is registered for the module (router: 501).

    Fail closed: a ``test_connection`` step without a tester can never
    complete — an unverified connection is not a connection.
    """


class ConnectionTestFailedError(OnboardingError):
    """The registered tester answered that the connection does not work (router: 409)."""


class ConnectionTestError(OnboardingError):
    """The registered tester raised while checking the connection (router: 502)."""


class ScopeOptionsUnavailableError(OnboardingError):
    """Dynamic ``scope_select`` options could not be loaded (router: 409).

    Raised on submit when the module's registered options provider failed
    (no stored connection, dead tokens, provider outage): no options, no
    choice — fail closed with the same fixed actionable message the
    rendered step carries. The client's 409 → reload loop then shows the
    step's error state. The message never carries provider internals.
    """


class OnboardingEncryptionError(OnboardingError):
    """Secret answers cannot be encrypted or decrypted (router: 500).

    Raised *before* any write when no usable credentials key is configured
    (fail closed: no key, no secret at rest), and when a stored token can no
    longer be decrypted (key rotated or corrupted data).
    """
