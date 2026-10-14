"""Onboarding models: one user's progress through one module's declared steps.

Tenant + titularity isolation (same column-based rule as identity/connections)
---------------------------------------------------------------------------
Tenants share single tables, every row carries ``tenant_id``, and every query
MUST filter by it. On top of that, onboarding belongs to exactly one *user*
(``user_id``): the router never accepts a user id from the client — the
principal is the scope — so one user can neither read nor advance another
user's flow, inside their tenant or anywhere else.

``UNIQUE (tenant_id, user_id, module_key)`` gives one onboarding state per
user per module, so a resume always finds the same row.

Secrets versus answers
----------------------
``data`` holds the NON-secret answers only (the merged form/scope answers the
user may review). Fields marked ``secret`` in the manifest (and ``password``
widgets, which the engine treats as secret regardless) never enter ``data``:
their values are accumulated in ``secrets_encrypted`` — a Fernet token of the
JSON secret dict, encrypted with the same ``Settings.credentials_key`` that
protects connection credentials. The token is cleared the moment the
completion step hands the secrets to the connections service, and again on
restart. No API contract in this module has a field for either ``data``
secrets or the token, so a secret cannot leak through serialization.

``status`` stores the :class:`OnboardingStatus` name inside an ``Enum``
column — same pattern as ``User.role`` and ``UserConnection.status``.
"""

import enum
from typing import Any

from sqlalchemy import JSON, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin


class OnboardingStatus(enum.StrEnum):
    """Lifecycle of one user's onboarding for one module.

    ``not_started`` is a *virtual* state — the engine never writes a row
    before the first successful submit. ``in_progress`` parks the user at
    ``current_step``; ``completed`` means the connection was written.
    """

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class OnboardingState(TimestampMixin, Base):
    """One user's onboarding progress for one catalog module."""

    __tablename__ = "onboarding_states"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "module_key",
            name="uq_onboarding_states_tenant_user_module",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    #: The flow's owner: everyone configures their own connections (D5).
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    #: Key of a module declared in the catalog (``ModuleManifest.key``).
    module_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[OnboardingStatus] = mapped_column(
        Enum(OnboardingStatus, name="onboarding_status"),
        default=OnboardingStatus.NOT_STARTED,
    )
    #: Key of the manifest step the user is parked on (``None`` when done).
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: NON-secret answers only — never a field the manifest marks ``secret``.
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: Fernet token of the accumulated secret answers — never returned by any
    #: API, cleared on completion and restart.
    secrets_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
