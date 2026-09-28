"""Recurring weekly access windows.

A token may carry weekly windows — "Tue/Thu 09:00-13:00" — evaluated in the
house's time zone, inside its starts_at/expires_at. The properties under test:

  * the evaluation itself: same-day, overnight (weekday is the day the window
    opens), 24:00 ends, merged neighbours, the next opening, nothing opening
    before expiry, DST, and a corrupted column failing closed;
  * the gate is server-side on every guest route the PIN gate covers, and a
    guest outside the window never reaches Home Assistant;
  * the stream and a live camera view stop at the window's end;
  * the zone comes from the add-on option when set, otherwise from HA, and a
    windowed link refuses with 503 when neither can be read.

Real routing, real DB; only ha_client is mocked (UTC by default).
"""
import html
import json
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from app import database as db
from app import guest_pin
from app import schedule
from app.config import settings
from app.models import AccessWindow, NEVER_EXPIRES_SECONDS
from app.routers.guest import _event_generator

UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")
ENTITIES = ["light.living_room", "camera.hall"]

GATED_ENDPOINTS = [
    ("GET", "/state", None),
    ("GET", "/camera/camera.hall", None),
    ("GET", "/camera/camera.hall/stream", None),
    ("POST", "/command", {"entity_id": "light.living_room", "service": "light.turn_on"}),
]
HA_CALLS = ("get_states", "call_service", "camera_snapshot", "fire_event", "logbook_log")


def _epoch(tz, *args) -> int:
    return int(datetime(*args, tzinfo=tz).timestamp())


def _window_at(offset_minutes: int, length_minutes: int) -> dict:
    """A UTC window opening `offset_minutes` from now, on the day it opens."""
    start = time.time() + offset_minutes * 60
    st, en = time.gmtime(start), time.gmtime(start + length_minutes * 60)
    return {
        "weekdays": [st.tm_wday],
        "start": f"{st.tm_hour:02d}:{st.tm_min:02d}",
        "end": f"{en.tm_hour:02d}:{en.tm_min:02d}",
    }


async def _make_token(slug, windows, starts_in=None, expires_in=86400 * 14, pin=None):
    now = int(time.time())
    return await db.create_token(
        label=f"Token {slug}", slug=slug, entity_ids=ENTITIES,
        expires_at=now + expires_in, ip_allowlist=None,
        pin_hash=await guest_pin.hash_pin(pin) if pin else None,
        starts_at=now + starts_in if starts_in else None,
        access_windows=windows,
    )


async def _call(client, slug, method, suffix, body):
    if method == "GET":
        return await client.get(f"/g/{slug}{suffix}")
    return await client.post(f"/g/{slug}{suffix}", json=body)


@pytest_asyncio.fixture
async def inside_token(test_db):
    return await _make_token("inside", [_window_at(-60, 120)])


@pytest_asyncio.fixture
async def outside_token(test_db):
    return await _make_token("outside", [_window_at(120, 60)])


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

TUE_THU_MORNINGS = [{"weekdays": [1, 3], "start": "09:00", "end": "13:00"}]
FAR = _epoch(UTC, 2030, 1, 1)


def test_no_windows_is_the_old_behaviour():
    now = _epoch(UTC, 2026, 9, 29, 3, 0)
    assert schedule.evaluate(None, FAR, None, None, now) == schedule.Access(True, closes_at=FAR)
    later = now + 3600
    assert schedule.evaluate(later, FAR, None, None, now) == schedule.Access(False, opens_at=later)


def test_inside_a_same_day_window():
    now = _epoch(UTC, 2026, 9, 29, 10, 0)  # Tuesday
    access = schedule.evaluate(None, FAR, TUE_THU_MORNINGS, UTC, now)
    assert access.active
    assert access.closes_at == _epoch(UTC, 2026, 9, 29, 13, 0)


def test_the_end_is_exclusive():
    now = _epoch(UTC, 2026, 9, 29, 13, 0)
    access = schedule.evaluate(None, FAR, TUE_THU_MORNINGS, UTC, now)
    assert not access.active
    assert access.opens_at == _epoch(UTC, 2026, 10, 1, 9, 0)  # Thursday


def test_outside_reports_the_next_opening_across_the_week():
    now = _epoch(UTC, 2026, 10, 1, 14, 0)  # Thursday afternoon
    access = schedule.evaluate(None, FAR, TUE_THU_MORNINGS, UTC, now)
    assert not access.active
    assert access.opens_at == _epoch(UTC, 2026, 10, 6, 9, 0)  # next Tuesday


def test_overnight_window_belongs_to_the_day_it_opens():
    fri_night = [{"weekdays": [4], "start": "22:00", "end": "02:00"}]
    sat_1am = _epoch(UTC, 2026, 10, 3, 1, 0)
    access = schedule.evaluate(None, FAR, fri_night, UTC, sat_1am)
    assert access.active
    assert access.closes_at == _epoch(UTC, 2026, 10, 3, 2, 0)
    # 01:00 on the Friday itself is the tail of a Thursday window, which
    # does not exist — so it is closed until Friday 22:00.
    fri_1am = _epoch(UTC, 2026, 10, 2, 1, 0)
    access = schedule.evaluate(None, FAR, fri_night, UTC, fri_1am)
    assert not access.active
    assert access.opens_at == _epoch(UTC, 2026, 10, 2, 22, 0)


def test_a_24_00_end_covers_the_whole_day():
    saturday = [{"weekdays": [5], "start": "00:00", "end": "24:00"}]
    late = _epoch(UTC, 2026, 10, 3, 23, 59, 30)
    access = schedule.evaluate(None, FAR, saturday, UTC, late)
    assert access.active
    assert access.closes_at == _epoch(UTC, 2026, 10, 4, 0, 0)


def test_touching_windows_merge_into_one_stretch():
    windows = [
        {"weekdays": [0], "start": "09:00", "end": "12:00"},
        {"weekdays": [0], "start": "12:00", "end": "15:00"},
    ]
    now = _epoch(UTC, 2026, 9, 28, 10, 0)  # Monday
    access = schedule.evaluate(None, FAR, windows, UTC, now)
    assert access.closes_at == _epoch(UTC, 2026, 9, 28, 15, 0)


def test_multiple_windows_any_one_opens_the_link():
    windows = TUE_THU_MORNINGS + [{"weekdays": [5], "start": "18:00", "end": "20:00"}]
    sat = _epoch(UTC, 2026, 10, 3, 19, 0)
    assert schedule.evaluate(None, FAR, windows, UTC, sat).active


def test_windows_apply_inside_the_start_and_expiry():
    now = _epoch(UTC, 2026, 9, 29, 10, 0)  # Tuesday, inside a window
    # Not started yet: waits for the start even though a window is open now.
    start = _epoch(UTC, 2026, 10, 1, 10, 0)  # Thursday, also inside one
    access = schedule.evaluate(start, FAR, TUE_THU_MORNINGS, UTC, now)
    assert access == schedule.Access(False, opens_at=start)
    # A start outside every window opens at the next window after it.
    start = _epoch(UTC, 2026, 9, 30, 10, 0)  # Wednesday
    access = schedule.evaluate(start, FAR, TUE_THU_MORNINGS, UTC, now)
    assert access.opens_at == _epoch(UTC, 2026, 10, 1, 9, 0)
    # An expiry inside the window closes it early.
    expires = _epoch(UTC, 2026, 9, 29, 11, 0)
    access = schedule.evaluate(None, expires, TUE_THU_MORNINGS, UTC, now)
    assert access.closes_at == expires


def test_no_opening_before_expiry_is_reported_as_none():
    now = _epoch(UTC, 2026, 9, 29, 14, 0)  # Tuesday after the window
    expires = _epoch(UTC, 2026, 9, 30, 0, 0)  # before Thursday's window
    access = schedule.evaluate(None, expires, TUE_THU_MORNINGS, UTC, now)
    assert access == schedule.Access(False, opens_at=None)


def test_windows_are_wall_clock_in_the_house_zone():
    """09:00 in New York is 13:00 UTC in summer and 14:00 in winter."""
    daily = [{"weekdays": list(range(7)), "start": "09:00", "end": "10:00"}]
    summer = _epoch(UTC, 2026, 7, 1, 13, 30)
    winter = _epoch(UTC, 2026, 12, 1, 13, 30)
    assert schedule.evaluate(None, FAR, daily, NY, summer).active
    assert not schedule.evaluate(None, FAR, daily, NY, winter).active
    assert schedule.evaluate(None, FAR, daily, NY, winter + 3600).active


def test_an_overnight_window_across_a_dst_change_keeps_its_wall_clock_end():
    """US clocks go back at 02:00 on 2026-11-01. Sat 22:00 to Sun 06:00 is nine
    real hours that night, and still ends at 06:00 on the wall."""
    windows = [{"weekdays": [5], "start": "22:00", "end": "06:00"}]
    now = _epoch(NY, 2026, 10, 31, 23, 0)
    access = schedule.evaluate(None, FAR, windows, NY, now)
    assert access.active
    assert access.closes_at == _epoch(NY, 2026, 11, 1, 6, 0)
    assert access.closes_at - _epoch(NY, 2026, 10, 31, 22, 0) == 9 * 3600


def test_a_corrupted_column_fails_closed():
    assert schedule.windows_from_row(None) is None
    assert schedule.windows_from_row("not json") == []
    assert schedule.windows_from_row('{"weekdays": [1]}') == []
    now = _epoch(UTC, 2026, 9, 29, 10, 0)
    assert not schedule.evaluate(None, FAR, [], UTC, now).active


def test_a_window_needs_the_zone():
    now = _epoch(UTC, 2026, 9, 29, 10, 0)
    access = schedule.evaluate(None, FAR, TUE_THU_MORNINGS, None, now)
    assert not access.active and access.zone_unknown


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("window", [
    {"weekdays": [], "start": "09:00", "end": "10:00"},
    {"weekdays": [7], "start": "09:00", "end": "10:00"},
    {"weekdays": [True], "start": "09:00", "end": "10:00"},
    {"weekdays": ["1"], "start": "09:00", "end": "10:00"},
    {"weekdays": [1], "start": "9:00", "end": "10:00"},
    {"weekdays": [1], "start": "24:00", "end": "10:00"},
    {"weekdays": [1], "start": "09:00", "end": "24:01"},
    {"weekdays": [1], "start": "09:00", "end": "09:00"},
])
def test_malformed_windows_are_rejected(window):
    with pytest.raises(ValueError):
        AccessWindow(**window)


def test_weekdays_are_deduplicated_and_sorted():
    assert AccessWindow(weekdays=[3, 1, 3], start="22:00", end="02:00").weekdays == [1, 3]


async def test_admin_create_stores_and_returns_windows(client, admin_session, mock_ha_client):
    windows = [{"weekdays": [4], "start": "22:00", "end": "02:00"}]
    resp = await client.post("/admin/tokens", json={
        "label": "Cleaner", "entity_ids": ["light.living_room"],
        "expires_in_seconds": 86400 * 30, "access_windows": windows,
    }, cookies=admin_session)
    assert resp.status_code == 201
    assert resp.json()["access_windows"] == windows
    listed = (await client.get("/admin/tokens", cookies=admin_session)).json()
    assert listed[0]["access_windows"] == windows


async def test_admin_create_folds_an_empty_list_to_no_windows(client, admin_session, mock_ha_client):
    """An empty stored list is the fail-closed reading; an admin sending []
    means "any time" and must not land in it."""
    resp = await client.post("/admin/tokens", json={
        "label": "Open", "entity_ids": ["light.living_room"],
        "expires_in_seconds": 3600, "access_windows": [],
    }, cookies=admin_session)
    assert resp.json()["access_windows"] is None
    row = await db.get_token_by_id(resp.json()["id"])
    assert row["access_windows"] is None


async def test_a_bad_window_is_a_422_without_the_pin(client, admin_session, mock_ha_client):
    resp = await client.post("/admin/tokens", json={
        "label": "Bad", "entity_ids": ["light.living_room"], "expires_in_seconds": 3600,
        "pin": "918273", "access_windows": [{"weekdays": [1], "start": "09:00", "end": "09:00"}],
    }, cookies=admin_session)
    assert resp.status_code == 422
    assert "918273" not in resp.text


async def test_too_many_windows_are_rejected(client, admin_session, mock_ha_client):
    window = {"weekdays": [1], "start": "09:00", "end": "10:00"}
    resp = await client.post("/admin/tokens", json={
        "label": "Many", "entity_ids": ["light.living_room"], "expires_in_seconds": 3600,
        "access_windows": [window] * 15,
    }, cookies=admin_session)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,suffix,body", GATED_ENDPOINTS)
async def test_every_guest_route_works_inside_the_window(
    client, inside_token, mock_ha_client, method, suffix, body
):
    resp = await _call(client, inside_token["slug"], method, suffix, body)
    assert resp.status_code == 200, f"{method} {suffix}"


@pytest.mark.parametrize("method,suffix,body", GATED_ENDPOINTS)
async def test_every_guest_route_is_refused_outside_the_window(
    client, outside_token, mock_ha_client, method, suffix, body
):
    resp = await _call(client, outside_token["slug"], method, suffix, body)
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error"] == "This link is not active right now"
    assert detail["starts_at"] is None
    assert detail["opens_at"] > time.time()


async def test_outside_the_window_never_reaches_home_assistant(
    client, outside_token, mock_ha_client
):
    await client.get(f"/g/{outside_token['slug']}")
    for method, suffix, body in GATED_ENDPOINTS:
        await _call(client, outside_token["slug"], method, suffix, body)
    for name in HA_CALLS:
        assert not mock_ha_client[name].called, f"reached ha_client.{name}"
    assert not await db.list_access_logs(limit=50)


async def test_the_pin_is_asked_before_the_window_is_revealed(client, test_db, mock_ha_client):
    row = await _make_token("locked", [_window_at(120, 60)], pin="5173")
    resp = await client.get(f"/g/{row['slug']}/state")
    assert resp.status_code == 401
    page = await client.get(f"/g/{row['slug']}")
    assert "Not active right now" not in page.text


async def test_the_ip_allowlist_is_checked_before_the_window(client, test_db, mock_ha_client):
    now = int(time.time())
    row = await db.create_token(
        label="Fenced", slug="fenced", entity_ids=ENTITIES, expires_at=now + 86400,
        ip_allowlist=["10.0.0.0/8"], access_windows=[_window_at(120, 60)],
    )
    resp = await client.get(f"/g/{row['slug']}/state")
    assert resp.json()["detail"] == "IP not allowed"


async def test_the_page_outside_the_window_is_the_pending_preview(
    client, outside_token, mock_ha_client
):
    resp = await client.get(f"/g/{outside_token['slug']}")
    assert resp.status_code == 200
    assert "Not active right now" in resp.text
    assert 'id="pending-banner"' in resp.text
    assert 'id="conn-badge"' not in resp.text


async def test_the_page_inside_the_window_carries_its_end(client, inside_token, mock_ha_client):
    resp = await client.get(f"/g/{inside_token['slug']}")
    assert resp.status_code == 200
    assert "Not active right now" not in resp.text
    assert "const WINDOW_CLOSES_AT = null" not in resp.text


async def test_an_unwindowed_page_has_no_window_end(client, sample_token, mock_ha_client):
    resp = await client.get(f"/g/{sample_token['slug']}")
    assert "const WINDOW_CLOSES_AT = null" in resp.text


async def test_no_further_opening_before_expiry_says_so(client, test_db, mock_ha_client):
    row = await _make_token("done", [_window_at(180, 60)], expires_in=3600)
    resp = await client.get(f"/g/{row['slug']}")
    assert "No more access times" in resp.text
    state = await client.get(f"/g/{row['slug']}/state")
    assert state.status_code == 403
    assert state.json()["detail"]["opens_at"] is None


# ---------------------------------------------------------------------------
# The house's time zone
# ---------------------------------------------------------------------------

async def test_an_unreadable_zone_fails_closed_with_503(client, inside_token, mock_ha_client):
    mock_ha_client["get_time_zone"].return_value = None
    for method, suffix, body in GATED_ENDPOINTS:
        resp = await _call(client, inside_token["slug"], method, suffix, body)
        assert resp.status_code == 503, f"{method} {suffix}"
    mock_ha_client["call_service"].assert_not_awaited()
    page = await client.get(f"/g/{inside_token['slug']}")
    # Unescaped: the banner is rendered through the i18n catalogue, and Jinja
    # writes the apostrophe as &#39; — the same text on screen.
    assert "check this link's schedule" in html.unescape(page.text)


async def test_an_unwindowed_token_never_asks_for_the_zone(client, sample_token, mock_ha_client):
    mock_ha_client["get_time_zone"].return_value = None
    resp = await client.get(f"/g/{sample_token['slug']}/state")
    assert resp.status_code == 200
    mock_ha_client["get_time_zone"].assert_not_awaited()


async def test_the_timezone_setting_overrides_home_assistant(
    client, admin_session, test_db, mock_ha_client, monkeypatch
):
    # A zone well away from UTC, so a window that is open in UTC is shut there.
    monkeypatch.setattr(settings, "timezone", "Pacific/Kiritimati")  # UTC+14
    row = await _make_token("zoned", [_window_at(-60, 120)])
    resp = await client.get(f"/g/{row['slug']}/state")
    assert resp.status_code == 403
    mock_ha_client["get_time_zone"].assert_not_awaited()
    tz = await client.get("/admin/timezone", cookies=admin_session)
    assert tz.json() == {"timezone": "Pacific/Kiritimati", "source": "setting"}


async def test_the_timezone_endpoint_reports_home_assistant(client, admin_session, mock_ha_client):
    mock_ha_client["get_time_zone"].return_value = "Europe/Madrid"
    resp = await client.get("/admin/timezone", cookies=admin_session)
    assert resp.json() == {"timezone": "Europe/Madrid", "source": "home_assistant"}
    mock_ha_client["get_time_zone"].return_value = None
    resp = await client.get("/admin/timezone", cookies=admin_session)
    assert resp.json() == {"timezone": None, "source": None}


def test_an_unknown_timezone_setting_is_rejected_at_startup():
    from app.config import Settings
    with pytest.raises(ValueError):
        Settings(timezone="Mars/Olympus_Mons")


async def test_ha_time_zone_is_cached_and_survives_a_failure(monkeypatch):
    from app import ha_client

    calls = []

    class _Resp:
        def __init__(self, ok):
            self.ok = ok

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError("down")

        def json(self):
            return {"time_zone": "Europe/Madrid"}

    class _Client:
        def __init__(self):
            self.ok = True

        async def get(self, path):
            calls.append(path)
            return _Resp(self.ok)

    fake = _Client()
    monkeypatch.setattr(ha_client, "_client", fake)
    monkeypatch.setattr(ha_client, "_time_zone", None)
    monkeypatch.setattr(ha_client, "_time_zone_next_read", 0.0)

    assert await ha_client.get_time_zone() == "Europe/Madrid"
    assert await ha_client.get_time_zone() == "Europe/Madrid"
    assert calls == ["/api/config"]

    # Past the TTL, with HA down: the last good zone is still served.
    monkeypatch.setattr(ha_client, "_time_zone_next_read", 0.0)
    fake.ok = False
    assert await ha_client.get_time_zone() == "Europe/Madrid"
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Long-lived connections stop at the window's end
# ---------------------------------------------------------------------------

def _fake_request():
    async def is_disconnected():
        return False
    return SimpleNamespace(is_disconnected=is_disconnected)


async def test_a_live_stream_closes_at_the_window_end(test_db, inside_token):
    gen = _event_generator(
        inside_token["id"], inside_token["slug"], _fake_request(), None, time.time() + 0.2
    )
    first = await gen.__anext__()
    assert first.startswith("event: connected")
    frame = await gen.__anext__()
    assert frame.startswith("event: window_closed")
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()


async def test_the_stream_route_passes_the_window_end(client, inside_token, mock_ha_client, monkeypatch):
    from app.routers import guest

    seen = {}

    async def fake_gen(token_id, slug, request, starts_at=None, closes_at=None):
        seen.update(starts_at=starts_at, closes_at=closes_at)
        yield "event: connected\ndata: {}\n\n"

    monkeypatch.setattr(guest, "_event_generator", fake_gen)
    await client.get(f"/g/{inside_token['slug']}/stream")
    assert seen["starts_at"] is None
    assert time.time() < seen["closes_at"] <= time.time() + 3600


async def test_a_pending_stream_with_nothing_to_wait_for_stays_pending(
    client, test_db, mock_ha_client, monkeypatch
):
    from app.routers import guest

    row = await _make_token("never", [_window_at(180, 60)], expires_in=3600)
    seen = {}

    async def fake_gen(token_id, slug, request, starts_at=None, closes_at=None):
        seen.update(starts_at=starts_at, closes_at=closes_at)
        yield "event: connected\ndata: {}\n\n"

    monkeypatch.setattr(guest, "_event_generator", fake_gen)
    await client.get(f"/g/{row['slug']}/stream")
    assert seen == {"starts_at": NEVER_EXPIRES_SECONDS, "closes_at": None}


async def test_a_pending_stream_forwards_a_schedule_change(test_db, outside_token):
    from app import ha_client

    gen = _event_generator(
        outside_token["id"], outside_token["slug"], _fake_request(), time.time() + 3600
    )
    await gen.__anext__()
    await ha_client.broadcast_schedule_changed(outside_token["id"])
    frame = await gen.__anext__()
    assert frame.startswith("event: schedule_changed")
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()


async def test_a_camera_view_stops_at_the_window_end(client, test_db, mock_ha_client, monkeypatch):
    """The relay is gated once, at open, so it carries the window's end with
    it. Here that end has already passed by the first frame."""
    from app.routers import guest

    row = await _make_token("cam", [_window_at(-60, 120)])

    async def just_closing(r):
        return schedule.Access(active=True, closes_at=int(time.time()) - 1)

    monkeypatch.setattr(guest, "_access", just_closing)
    resp = await client.get(f"/g/{row['slug']}/camera/camera.hall/stream")
    assert resp.status_code == 200
    assert resp.content == b""


async def test_an_unwindowed_camera_view_is_not_cut_short(client, sample_token, mock_ha_client):
    await db.update_token_entities(sample_token["id"], ["camera.hall"])
    resp = await client.get(f"/g/{sample_token['slug']}/camera/camera.hall/stream")
    assert resp.status_code == 200
    assert b"fake-frame" in resp.content
