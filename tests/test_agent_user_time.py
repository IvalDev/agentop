"""Per-request user time context: timezone resolution and the prompt paragraph.

Pure functions with an injected clock (no database, no model, no wall-clock
assertions beyond a bounded window for the default clock).
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.modules.agent.user_time import UserTime, render_time_context, resolve_user_time

#: Friday 2026-10-16 08:07 UTC — 10:07 in Madrid (summer time, UTC+02:00).
FRIDAY = datetime(2026, 10, 16, 8, 7, 30, tzinfo=UTC)


def test_valid_zone_resolves_to_its_local_time() -> None:
    """A known IANA name gives the time in that zone, not a fallback."""
    user_time = resolve_user_time("Europe/Madrid", now=FRIDAY)

    assert user_time.timezone == "Europe/Madrid"
    assert user_time.fallback is False
    assert user_time.now == FRIDAY
    assert user_time.now.tzinfo == ZoneInfo("Europe/Madrid")
    assert user_time.now.isoformat(timespec="seconds") == "2026-10-16T10:07:30+02:00"


def test_offset_follows_the_daylight_saving_change() -> None:
    """After the last Sunday of October Madrid is back on UTC+01:00."""
    user_time = resolve_user_time("Europe/Madrid", now=datetime(2026, 10, 26, 9, 0, tzinfo=UTC))

    assert user_time.now.utcoffset() == timedelta(hours=1)
    assert user_time.now.isoformat(timespec="seconds") == "2026-10-26T10:00:00+01:00"


def test_utc_is_a_valid_zone_and_not_a_fallback() -> None:
    user_time = resolve_user_time("UTC", now=FRIDAY)

    assert (user_time.timezone, user_time.fallback) == ("UTC", False)


def test_surrounding_whitespace_is_ignored() -> None:
    user_time = resolve_user_time("  Europe/Madrid\n", now=FRIDAY)

    assert (user_time.timezone, user_time.fallback) == ("Europe/Madrid", False)


@pytest.mark.parametrize(
    "name",
    [None, "", "   ", "Mars/Olympus_Mons", "../etc/passwd", "/etc/passwd", "Europe/", "zone.tab"],
    ids=["none", "empty", "blank", "unknown", "relative-path", "absolute-path", "dir", "not-tzif"],
)
def test_missing_or_invalid_zone_falls_back_to_utc(name: str | None) -> None:
    """Nothing the client sends can fail the request: unusable names mean UTC."""
    user_time = resolve_user_time(name, now=FRIDAY)

    assert user_time == UserTime(timezone="UTC", now=FRIDAY, fallback=True)
    assert user_time.now.tzinfo == UTC


def test_naive_now_is_treated_as_utc() -> None:
    user_time = resolve_user_time("Europe/Madrid", now=datetime(2026, 10, 16, 8, 7))

    assert user_time.now.isoformat(timespec="minutes") == "2026-10-16T10:07+02:00"


def test_default_clock_is_the_current_aware_time() -> None:
    before = datetime.now(UTC)
    user_time = resolve_user_time("Asia/Tokyo")
    after = datetime.now(UTC)

    assert before <= user_time.now <= after
    assert user_time.now.utcoffset() == timedelta(hours=9)


def test_render_states_date_time_zone_and_offset() -> None:
    """One English paragraph the model can resolve relative dates against."""
    text = render_time_context(resolve_user_time("Europe/Madrid", now=FRIDAY))

    assert text == (
        "Current date and time for the user: Friday, 2026-10-16 10:07 "
        '(Europe/Madrid, UTC+02:00). Resolve relative dates such as "today", '
        '"tomorrow", "this month" or "the 20th" against this date and timezone.'
    )
    assert "unknown" not in text


def test_render_explains_the_utc_fallback() -> None:
    text = render_time_context(resolve_user_time("Not/AZone", now=FRIDAY))

    assert "Friday, 2026-10-16 08:07 (UTC, UTC+00:00)" in text
    assert "The user's timezone is unknown, so this time is in UTC." in text


@pytest.mark.parametrize(
    ("zone", "offset"),
    [("Asia/Kolkata", "UTC+05:30"), ("America/New_York", "UTC-04:00"), ("UTC", "UTC+00:00")],
)
def test_render_formats_fractional_and_negative_offsets(zone: str, offset: str) -> None:
    text = render_time_context(resolve_user_time(zone, now=FRIDAY))

    assert f"({zone}, {offset})" in text


@pytest.mark.parametrize(
    ("day", "weekday"),
    [
        (12, "Monday"),
        (13, "Tuesday"),
        (14, "Wednesday"),
        (15, "Thursday"),
        (16, "Friday"),
        (17, "Saturday"),
        (18, "Sunday"),
    ],
)
def test_render_names_every_weekday_in_english(day: int, weekday: str) -> None:
    """Weekday names come from a fixed English table, not the process locale."""
    now = datetime(2026, 10, day, 12, 0, tzinfo=UTC)

    text = render_time_context(resolve_user_time("UTC", now=now))

    assert f": {weekday}, 2026-10-{day:02d} 12:00 " in text
