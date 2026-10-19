"""The requesting user's current date, time, and timezone for one agent run.

The browser sends an IANA timezone name with each chat request
(``ChatRequest.timezone``). :func:`resolve_user_time` turns it into a
:class:`UserTime` with the system tz database (stdlib :mod:`zoneinfo`, no
``tzdata`` dependency). Nothing the client sends can fail a request: a
missing, blank, unknown, or malformed name (path-like keys included) falls
back to UTC with ``fallback=True``, which the run records in its audit
trail (feature agent-engine-routing D5/D6).

:func:`render_time_context` writes the short English paragraph appended to
the system message so the model resolves "today" or "the 20th" against the
user's real date instead of its training data. It is server-built text and
never contains client input other than a name the tz database accepted.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Name recorded when the client's timezone is missing or unusable.
FALLBACK_TIMEZONE = "UTC"
#: English weekday names by ``datetime.weekday()``, independent of the process locale.
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True)
class UserTime:
    """The user's wall-clock time at the start of a run.

    ``timezone`` is the resolved IANA name (``"UTC"`` on fallback), ``now``
    a timezone-aware datetime in that zone, and ``fallback`` tells whether
    the client's timezone was missing or unusable.
    """

    timezone: str
    now: datetime
    fallback: bool


def _zone(name: str | None) -> ZoneInfo | None:
    """The tz database zone for ``name``, or ``None`` when it is unusable."""
    key = (name or "").strip()
    if not key:
        return None
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # Unknown keys, non-normalized or absolute paths ("../etc/passwd"),
        # and files that are not TZif data all mean "no usable timezone".
        return None


def resolve_user_time(name: str | None, *, now: datetime | None = None) -> UserTime:
    """Resolve the client's timezone ``name`` and the current time in it.

    ``now`` is the clock seam for tests (default: the current UTC time); a
    naive ``now`` is treated as UTC. Surrounding whitespace in ``name`` is
    ignored; ``None``, blank, unknown, or invalid names yield UTC with
    ``fallback=True``.
    """
    moment = datetime.now(UTC) if now is None else now
    if moment.utcoffset() is None:
        moment = moment.replace(tzinfo=UTC)
    zone = _zone(name)
    if zone is None:
        return UserTime(timezone=FALLBACK_TIMEZONE, now=moment.astimezone(UTC), fallback=True)
    return UserTime(timezone=zone.key, now=moment.astimezone(zone), fallback=False)


def _utc_offset(moment: datetime) -> str:
    """``UTC+02:00``-style offset of an aware datetime (whole minutes)."""
    offset = moment.utcoffset()
    total_minutes = 0 if offset is None else int(offset.total_seconds()) // 60
    sign = "-" if total_minutes < 0 else "+"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def render_time_context(user_time: UserTime) -> str:
    """The paragraph telling the model the user's date, time, and timezone."""
    now = user_time.now
    text = (
        f"Current date and time for the user: {_WEEKDAYS[now.weekday()]}, "
        f"{now:%Y-%m-%d %H:%M} ({user_time.timezone}, {_utc_offset(now)}). "
        'Resolve relative dates such as "today", "tomorrow", "this month" or "the 20th" '
        "against this date and timezone."
    )
    if user_time.fallback:
        text += " The user's timezone is unknown, so this time is in UTC."
    return text
