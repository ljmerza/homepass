"""Sensor history on the guest page: GET /g/{slug}/history/{entity_id}.

A link reveals the recorder's past for a sensor only when its admin ticked
show_history for that sensor on that link. Everything else about the route —
which ranges it takes, how much it sends, how often HA is asked — is bounded
here because the slug is the only credential the caller needs.
"""
import re
import time
from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio

from app import database as db
from app.routers import guest
from tests.test_guest_cards_js import _run, needs_node

SLUG = "hist-token"
SHARED = "sensor.heat_pump_1"
NOT_SHARED = "sensor.heat_pump_2"


@pytest_asyncio.fixture
async def hist_token(test_db):
    now = int(time.time())
    return await db.create_token(
        label="Block committee",
        slug=SLUG,
        entity_ids=[SHARED, NOT_SHARED, "light.hall"],
        expires_at=now + 3600,
        ip_allowlist=None,
        entity_meta={
            SHARED: {"options": {"show_history": True}},
            # Ticking it on a light stores it, but the route still only serves sensors.
            "light.hall": {"options": {"show_history": True}},
        },
    )


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _rows(*pairs):
    """HA's minimal response: a full first state, then {state, last_changed}."""
    first_ts, first_state = pairs[0]
    rows = [{
        "entity_id": SHARED, "state": first_state, "attributes": {},
        "last_changed": _iso(first_ts), "last_updated": _iso(first_ts),
    }]
    rows += [{"state": s, "last_changed": _iso(ts)} for ts, s in pairs[1:]]
    return rows


async def test_a_shared_sensor_returns_its_points(client, hist_token, mock_ha_client):
    now = time.time()
    mock_ha_client["get_history"].return_value = _rows(
        (now - 3000, "21.5"), (now - 2000, "unavailable"), (now - 1000, "22.25"),
    )
    r = await client.get(f"/g/{SLUG}/history/{SHARED}?hours=6")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["entity_id"] == SHARED and body["hours"] == 6
    # An outage is a gap, not a zero.
    assert [v for _, v in body["points"]] == [21.5, None, 22.25]
    assert body["points"][0][0] == pytest.approx(now - 3000, abs=0.01)

    entity_id, start, end = mock_ha_client["get_history"].await_args.args
    assert entity_id == SHARED
    assert (end - start).total_seconds() == 6 * 3600
    assert end.tzinfo is not None


async def test_the_default_range_is_a_day(client, hist_token, mock_ha_client):
    r = await client.get(f"/g/{SLUG}/history/{SHARED}")
    assert r.status_code == 200
    assert r.json()["hours"] == 24
    _, start, end = mock_ha_client["get_history"].await_args.args
    assert (end - start).total_seconds() == 24 * 3600


@pytest.mark.parametrize("entity_id, hours, status", [
    pytest.param(NOT_SHARED, 24, 403, id="on-the-link-but-not-shared"),
    pytest.param("sensor.someone_elses", 24, 403, id="not-on-the-link"),
    pytest.param("light.hall", 24, 422, id="not-a-sensor-even-if-ticked"),
    pytest.param("sensor.Heat-Pump", 24, 422, id="not-an-entity-id-shape"),
    pytest.param(SHARED, 12, 422, id="a-range-not-offered"),
    pytest.param(SHARED, 24 * 30, 422, id="a-month-is-not-offered"),
])
async def test_refusals_never_reach_ha(client, hist_token, mock_ha_client, entity_id, hours, status):
    r = await client.get(f"/g/{SLUG}/history/{entity_id}?hours={hours}")
    assert r.status_code == status
    mock_ha_client["get_history"].assert_not_awaited()


async def test_an_unknown_slug_is_refused(client, hist_token, mock_ha_client):
    r = await client.get(f"/g/not-a-link/history/{SHARED}")
    assert r.status_code >= 400
    mock_ha_client["get_history"].assert_not_awaited()


async def test_an_expired_link_is_refused(client, test_db, mock_ha_client):
    await db.create_token(
        label="Old", slug="old-link", entity_ids=[SHARED], expires_at=int(time.time()) - 60,
        ip_allowlist=None, entity_meta={SHARED: {"options": {"show_history": True}}},
    )
    r = await client.get(f"/g/old-link/history/{SHARED}")
    assert r.status_code >= 400
    mock_ha_client["get_history"].assert_not_awaited()


async def test_a_pending_link_is_refused(client, test_db, mock_ha_client):
    now = int(time.time())
    await db.create_token(
        label="Later", slug="later-link", entity_ids=[SHARED], expires_at=now + 7200,
        ip_allowlist=None, entity_meta={SHARED: {"options": {"show_history": True}}},
        starts_at=now + 3600,
    )
    r = await client.get(f"/g/later-link/history/{SHARED}")
    assert r.status_code >= 400
    mock_ha_client["get_history"].assert_not_awaited()


async def test_ha_failing_is_a_502(client, hist_token, mock_ha_client):
    mock_ha_client["get_history"].side_effect = httpx.ConnectError("down")
    r = await client.get(f"/g/{SLUG}/history/{SHARED}")
    assert r.status_code == 502


async def test_repeat_views_are_served_from_the_cache(client, hist_token, mock_ha_client):
    """Three committee members opening the same chart ask HA once."""
    for _ in range(3):
        assert (await client.get(f"/g/{SLUG}/history/{SHARED}")).status_code == 200
    assert mock_ha_client["get_history"].await_count == 1
    # Another range is another question.
    assert (await client.get(f"/g/{SLUG}/history/{SHARED}?hours=168")).status_code == 200
    assert mock_ha_client["get_history"].await_count == 2


async def test_the_cache_expires(client, hist_token, mock_ha_client, monkeypatch):
    monkeypatch.setattr(guest, "HISTORY_CACHE_SECONDS", 0)
    await client.get(f"/g/{SLUG}/history/{SHARED}")
    await client.get(f"/g/{SLUG}/history/{SHARED}")
    assert mock_ha_client["get_history"].await_count == 2


async def test_a_failure_is_not_cached(client, hist_token, mock_ha_client):
    mock_ha_client["get_history"].side_effect = httpx.ConnectError("down")
    assert (await client.get(f"/g/{SLUG}/history/{SHARED}")).status_code == 502
    mock_ha_client["get_history"].side_effect = None
    assert (await client.get(f"/g/{SLUG}/history/{SHARED}")).status_code == 200


async def test_history_is_rate_limited_per_link(client, hist_token, mock_ha_client, monkeypatch):
    monkeypatch.setattr(guest, "HISTORY_RPM", 2)
    for _ in range(2):
        assert (await client.get(f"/g/{SLUG}/history/{SHARED}")).status_code == 200
    # The limit sits in front of the cache, so a cached answer still counts.
    assert (await client.get(f"/g/{SLUG}/history/{SHARED}")).status_code == 429


async def test_the_route_spends_no_command_budget(client, hist_token, mock_ha_client):
    """Watching a chart must not use up the allowance that works the lights."""
    from app.rate_limiter import rate_limiter
    await client.get(f"/g/{SLUG}/history/{SHARED}")
    assert f"hist:{hist_token['id']}" in rate_limiter._windows
    assert hist_token["id"] not in rate_limiter._windows


async def test_show_history_round_trips_through_the_admin_api(
    client, admin_session, hist_token, mock_ha_client,
):
    r = await client.patch(
        f"/admin/tokens/{hist_token['id']}/entity-meta",
        json={"entity_id": NOT_SHARED, "options": {"show_history": True}},
        cookies=admin_session,
    )
    assert r.status_code == 200
    assert r.json()["options"] == {"show_history": True}
    assert (await client.get(f"/g/{SLUG}/history/{NOT_SHARED}")).status_code == 200


async def test_unticking_show_history_closes_the_route(
    client, admin_session, hist_token, mock_ha_client,
):
    r = await client.patch(
        f"/admin/tokens/{hist_token['id']}/entity-meta",
        json={"entity_id": SHARED, "options": {}},
        cookies=admin_session,
    )
    assert r.status_code == 200
    assert (await client.get(f"/g/{SLUG}/history/{SHARED}")).status_code == 403


async def test_the_guest_state_carries_the_option(client, hist_token, mock_ha_client):
    body = (await client.get(f"/g/{SLUG}/state")).json()
    assert body["entity_meta"][SHARED]["options"] == {"show_history": True}
    assert body["entity_meta"][NOT_SHARED]["options"] == {}


# ── Downsampling ──────────────────────────────────────────────────────────

def test_a_short_series_passes_through():
    rows = _rows((100.0, "1"), (200.0, "2"), (300.0, "3"))
    assert guest._downsample_history(rows, 0, 400) == [[100.0, 1.0], [200.0, 2.0], [300.0, 3.0]]


def test_a_long_series_is_averaged_down():
    start, end = 0.0, 100_000.0
    n = guest.HISTORY_MAX_POINTS * 10
    rows = _rows(*[(start + i * (end - start) / n, str(i % 10)) for i in range(n)])
    out = guest._downsample_history(rows, start, end)
    assert len(out) <= guest.HISTORY_MAX_POINTS
    # Each slice holds one 0..9 cycle, so each mean is 4.5.
    assert {v for _, v in out} == {4.5}
    assert [t for t, _ in out] == sorted(t for t, _ in out)


def test_a_slice_of_only_gaps_stays_a_gap_and_mixed_ones_average_the_numbers():
    start, end = 0.0, float(guest.HISTORY_MAX_POINTS)
    pairs = []
    for i in range(guest.HISTORY_MAX_POINTS):
        pairs += [(i + 0.1, "unavailable"), (i + 0.5, "unknown")] if i == 7 else [(i + 0.1, "10"), (i + 0.5, "20")]
    pairs.append((3.9, "oops"))
    out = dict(guest._downsample_history(_rows(*pairs), start, end))
    assert out[7.1] is None
    assert out[3.1] == 15.0


def test_a_quiet_stretch_leaves_no_points_behind():
    """Empty slices are skipped, not filled — a sensor that held one value
    for hours is a flat line, not a broken one."""
    n = guest.HISTORY_MAX_POINTS + 1
    pairs = [(i * 0.001, "5") for i in range(n)]
    out = guest._downsample_history(_rows(*pairs), 0.0, 1000.0)
    assert len(out) == 1 and out[0][1] == 5.0


@pytest.mark.parametrize("state", ["nan", "inf", "-inf", "on", "", None])
def test_states_that_are_not_finite_numbers_are_gaps(state):
    assert guest._history_value(state) is None


def test_rows_without_a_readable_time_are_dropped():
    rows = [{"state": "1", "last_changed": "not a time"}, {"state": "2"},
            {"state": "3", "last_updated": _iso(50)}]
    assert guest._downsample_history(rows, 0, 100) == [[50.0, 3.0]]


# ── The guest card ────────────────────────────────────────────────────────

@needs_node
async def test_a_shared_sensor_card_opens_its_history(client, sample_token, mock_ha_client):
    out = await _run(client, """
    const sensor = (eid) => ({ entity_id: eid, state: '21.4', attributes: { friendly_name: 'Pump', unit_of_measurement: '°C' } });
    entityMeta = {
      'sensor.shared': { options: { show_history: true } },
      'light.lamp': { options: { show_history: true } },
    };
    console.log(JSON.stringify({
      shared: buildCard('sensor.shared', sensor('sensor.shared')),
      plain: buildCard('sensor.plain', sensor('sensor.plain')),
      light: buildCard('light.lamp', { entity_id: 'light.lamp', state: 'on', attributes: {} }),
      ranges: [6, 24, 168].map(historyRangeLabel),
    }));
    """)
    assert 'data-action="open-history"' in out["shared"]
    assert 'role="button"' in out["shared"] and "show_chart" in out["shared"]
    assert "Show history for Pump" in out["shared"]
    # Without the option the sensor stays a plain read-out, and the option
    # does nothing on a domain the route does not serve.
    assert "open-history" not in out["plain"] and "show_chart" not in out["plain"]
    assert "open-history" not in out["light"]
    assert [label[0] for label in out["ranges"]] == ["6", "2", "7"]


async def test_the_page_never_loads_uplot_up_front(client, sample_token, mock_ha_client):
    """The chart library is fetched on first open, so links without a chart
    never download it — and so it is no tag the service worker must precache."""
    page = (await client.get("/g/test-token")).text
    assert not re.search(r'(?:src|href)="[^"]*uPlot', page)
    assert "/static/uPlot.iife.min.js?v=${BUILD_VERSION}" in page
    assert 'id="history-backdrop"' in page
