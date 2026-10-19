"""The fictional demo company and its team: one user per role.

Shared by the seed (which creates them) and the console chat client (which
logs in as them). Every name, email, and password here is development-only
fixture data and obviously fictional.
"""

from dataclasses import dataclass

from app.modules.identity.models import UserRole

DEMO_TENANT_SLUG = "demo"
DEMO_TENANT_NAME = "Suministros Ficticios Demo, S.L."
DEMO_EMAIL_DOMAIN = "demo.test"
#: Overrides every demo user's password when set (flag ``--password`` wins).
PASSWORD_ENV_VAR = "AA_DEMO_PASSWORD"


@dataclass(frozen=True)
class DemoUser:
    """One member of the demo team, identified by role."""

    role: UserRole
    full_name: str

    @property
    def email(self) -> str:
        return f"{self.role.value}@{DEMO_EMAIL_DOMAIN}"

    @property
    def default_password(self) -> str:
        """Development-only password used when no override is configured."""
        return f"demo-{self.role.value}-pass"


DEMO_USERS: tuple[DemoUser, ...] = (
    DemoUser(UserRole.OWNER, "Olga Dueña Demo"),
    DemoUser(UserRole.ADMIN, "Adrián Gestor Demo"),
    DemoUser(UserRole.OPERATOR, "Óscar Operario Demo"),
    DemoUser(UserRole.VIEWER, "Vera Consulta Demo"),
)


def demo_user(role: str) -> DemoUser:
    """Return the demo team member with ``role`` (``ValueError`` when unknown)."""
    wanted = UserRole(role)
    return next(user for user in DEMO_USERS if user.role == wanted)
