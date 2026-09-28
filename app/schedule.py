"""Weekly access windows: when a token's link works inside its start/expiry.

A token may carry a list of windows, each {"weekdays": [0-6], "start": "HH:MM",
"end": "HH:MM"} — 0 is Monday, matching datetime.weekday(). The windows narrow a
token and never widen it: a guest needs now inside starts_at/expires_at *and*
inside at least one window. A token with no windows is not affected by anything
in this module.

Overnight windows are supported: an end earlier than the start crosses
midnight, and the weekday names the day the window *opens* on. "Fri 22:00-02:00"
therefore runs from Friday night into Saturday morning, and is not open at
01:00 on a Friday. "24:00" is accepted as an end so a window can cover a whole
day without a one-minute gap at midnight.

Times are wall-clock times in the house's time zone — see house_zone() for
where that comes from. Evaluation turns each window into concrete epoch
intervals for the days around the moment in question and compares integers,
so the rest of the app never handles a datetime. Around a DST change the
wall-clock arithmetic is zoneinfo's: a start inside the skipped hour lands just
after the jump, and an ambiguous hour resolves to its first occurrence. That is
best effort at two moments a year, and errs towards a window that opens late
rather than one that opens early.
"""
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import ha_client
from app.config import settings

logger = logging.getLogger(__name__)

# How far past the reference day intervals are generated. Every window names
# at least one weekday, so any 7-day span holds one of its openings; one day
# of slack each side covers an overnight window begun the day before and a
# reference moment late on its own day.
HORIZON_DAYS = 8

END_OF_DAY = "24:00"


@dataclass(frozen=True)
class Access:
    """Whether a live, unexpired token can be used right now, and until when.

    opens_at is the next moment it can be, when it cannot be now — None if
    nothing on the schedule opens again before the token expires, or if the
    schedule could not be checked (zone_unknown). closes_at is the moment the
    current access ends when it can be used now.
    """
    active: bool
    opens_at: int | None = None
    closes_at: int | None = None
    zone_unknown: bool = False


def windows_from_row(raw: str | None) -> list[dict[str, Any]] | None:
    """Parse the stored JSON. None means "no weekly pattern".

    A value that does not parse is returned as an empty list — a schedule
    that never opens — rather than as None. A corrupted column must not be
    read as "no restriction": that would turn a link limited to Tuesday
    mornings into one that works around the clock.
    """
    if raw is None:
        return None
    try:
        windows = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("Unreadable access_windows value; treating the token as closed")
        return []
    if not isinstance(windows, list):
        return []
    return [w for w in windows if _well_formed(w)]


def _well_formed(w: Any) -> bool:
    try:
        return (
            isinstance(w, dict)
            and bool(w["weekdays"])
            and all(isinstance(d, int) and 0 <= d <= 6 for d in w["weekdays"])
            and _minutes(w["start"]) < 24 * 60
            and _minutes(w["end"]) <= 24 * 60
            and w["start"] != w["end"]
        )
    except (KeyError, TypeError, ValueError):
        return False


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _local_epoch(day: date, hhmm: str, tz: ZoneInfo) -> int:
    minutes = _minutes(hhmm)
    # 24:00 is midnight at the start of the next day, spelt so an admin can
    # write "until the end of Sunday" without a one-minute hole.
    day = day + timedelta(days=minutes // (24 * 60))
    minutes %= 24 * 60
    return int(datetime(day.year, day.month, day.day, minutes // 60, minutes % 60,
                        tzinfo=tz).timestamp())


def window_intervals(
    windows: list[dict[str, Any]], tz: ZoneInfo, around: int
) -> list[tuple[int, int]]:
    """Concrete [start, end) epoch intervals near `around`, sorted and merged.

    Merged so two touching or overlapping windows — Mon 09-12 and Mon 12-15,
    or an overnight Sunday window running into a Monday one — read as a single
    stretch of access. Otherwise a guest's page would be told the window closes
    at noon and then reload straight back into an open one.
    """
    today = datetime.fromtimestamp(around, tz).date()
    raw: list[tuple[int, int]] = []
    for offset in range(-1, HORIZON_DAYS + 1):
        day = today + timedelta(days=offset)
        weekday = day.weekday()
        for w in windows:
            if weekday not in w["weekdays"]:
                continue
            start = _local_epoch(day, w["start"], tz)
            overnight = _minutes(w["end"]) <= _minutes(w["start"])
            end = _local_epoch(day + timedelta(days=1) if overnight else day, w["end"], tz)
            if end > start:
                raw.append((start, end))

    raw.sort()
    merged: list[tuple[int, int]] = []
    for start, end in raw:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def evaluate(
    starts_at: int | None,
    expires_at: int,
    windows: list[dict[str, Any]] | None,
    tz: ZoneInfo | None,
    now: int,
) -> Access:
    """Decide whether a token that is neither revoked nor expired works now.

    The caller has already settled revocation, expiry and use limits; this is
    only the time-of-day question. `tz` may be None only when `windows` is
    None — a token without a weekly pattern never needs the house's zone.

    A merged interval that runs to the edge of the generated horizon — a
    window on every day that ends at 24:00 — reports that edge as its close.
    The guest page reloads there and lands straight back in an open window;
    one reload a week is the cost of never scanning an unbounded range.
    """
    lower = starts_at if starts_at and starts_at > now else now

    if windows is None:
        if lower > now:
            return Access(active=False, opens_at=lower)
        return Access(active=True, closes_at=expires_at)

    if tz is None:
        return Access(active=False, zone_unknown=True)

    opens_at = None
    for start, end in window_intervals(windows, tz, lower):
        if end <= lower:
            continue
        if start <= lower:
            if lower > now:
                # The scheduled start lands inside a window: it opens then.
                opens_at = lower
                break
            return Access(active=True, closes_at=min(end, expires_at))
        opens_at = start
        break

    if opens_at is not None and opens_at >= expires_at:
        opens_at = None
    return Access(active=False, opens_at=opens_at)


def zone_or_none(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown time zone %r", name)
        return None


async def house_zone_name() -> tuple[str | None, str | None]:
    """The zone windows are evaluated in, and where it came from.

    The add-on's `timezone` option wins when set. Otherwise it is Home
    Assistant's own configured time zone, read from /api/config: that is the
    zone the admin already set the house to, and a guest window of "09:00"
    means 09:00 on the clocks in that house, not in the container. Returns
    (None, None) when neither is available.
    """
    if settings.timezone:
        return settings.timezone, "setting"
    name = await ha_client.get_time_zone()
    if name and zone_or_none(name):
        return name, "home_assistant"
    return None, None


async def house_zone() -> ZoneInfo | None:
    name, _source = await house_zone_name()
    return zone_or_none(name)
