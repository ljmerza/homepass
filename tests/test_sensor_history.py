"""Sensor history on the guest page: GET /g/{slug}/history/{entity_id}.

A link reveals the recorder's past for a sensor only when its admin ticked
show_history for that sensor on that link. Everything else about the route —
which ranges it takes, how much it sends, how often HA is asked — is bounded
here because the slug is the only credential the caller needs.
"""
import json
import re
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

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


# ── Comparing sensors ─────────────────────────────────────────────────────
# A sensor's chart can have the link's other shared sensors of the same unit
# laid over it. All of it is the guest page's own doing, and each line is still
# one GET of the route above, so what is pinned here is the page script: which
# sensors are offered, which are drawn, what is remembered, and that an outage
# on one line is still a break in it once the lines share a chart.

UPLOT = Path(__file__).resolve().parent.parent / "static" / "uPlot.iife.min.js"

# Three heat pumps share a unit. The humidity sensor has another, one pump's
# history is not shared, "mode" is text with no unit, and the light is no sensor.
COMPARE_SETUP = """
const sensor = (name, unit, state = '21') => (
  { state, attributes: { friendly_name: name, ...(unit ? { unit_of_measurement: unit } : {}) } }
);
states = {
  'sensor.pump_1': sensor('Pump 1', '°C'),
  'sensor.humidity': sensor('Humidity', '%'),
  'sensor.pump_2': sensor('Pump <2>', '°C'),
  'sensor.pump_3': sensor('Pump 3', '°C'),
  'sensor.private': sensor('Private', '°C'),
  'sensor.mode': sensor('Mode', '', 'eco'),
  'light.hall': { state: 'on', attributes: { unit_of_measurement: '°C' } },
};
entityOrder = Object.keys(states);
entityMeta = {};
for (const id of entityOrder) {
  if (id !== 'sensor.private') entityMeta[id] = { options: { show_history: true } };
}
const stored = {};
window.localStorage = { getItem: k => stored[k] ?? null, setItem: (k, v) => { stored[k] = v; } };
const drawn = eid => historySeries(eid).filter(r => r.on).map(r => r.eid);
const compare = (from, eid) => { historyEid = from; toggleHistoryCompare(eid); };
"""

PUMPS = ["sensor.pump_1", "sensor.pump_2", "sensor.pump_3"]


async def _compare(client, probe: str):
    return await _run(client, COMPARE_SETUP + textwrap.dedent(probe))


@needs_node
async def test_only_shared_sensors_of_the_same_unit_can_be_compared(client, sample_token, mock_ha_client):
    out = await _compare(client, """
    console.log(JSON.stringify({
      pump: historyPeers('sensor.pump_1'),
      other_pump: historyPeers('sensor.pump_3'),
      humidity: historyPeers('sensor.humidity'),
      text: historyPeers('sensor.mode'),
    }));
    """)
    # Page order, whichever of them the sheet was opened for.
    assert out["pump"] == out["other_pump"] == PUMPS
    # One axis: another unit is never offered, and no unit is no match at all.
    assert out["humidity"] == ["sensor.humidity"]
    assert out["text"] == ["sensor.mode"]


@needs_node
async def test_a_chart_is_one_line_until_the_guest_compares(client, sample_token, mock_ha_client):
    out = await _compare(client, """
    const before = drawn('sensor.pump_1');
    compare('sensor.pump_1', 'sensor.pump_2');
    const after = { from_1: drawn('sensor.pump_1'), from_2: drawn('sensor.pump_2'), from_3: drawn('sensor.pump_3') };
    const kept = JSON.parse(stored['hp-compare:test-token']);
    compare('sensor.pump_2', 'sensor.pump_3');
    const all = drawn('sensor.pump_3');
    compare('sensor.pump_1', 'sensor.pump_2');
    console.log(JSON.stringify({
      before, after, kept, all,
      off: { from_1: drawn('sensor.pump_1'), from_2: drawn('sensor.pump_2') },
    }));
    """)
    assert out["before"] == ["sensor.pump_1"]
    # Both ends are remembered, so the pair opens together from either of them,
    # and a pump nobody compared still opens alone.
    assert out["after"] == {"from_1": PUMPS[:2], "from_2": PUMPS[:2], "from_3": ["sensor.pump_3"]}
    assert out["kept"] == {"sensor.pump_1": "°C", "sensor.pump_2": "°C"}
    assert out["all"] == PUMPS
    # Switching one off forgets that one only.
    assert out["off"] == {"from_1": ["sensor.pump_1", "sensor.pump_3"], "from_2": ["sensor.pump_2"]}


@needs_node
@pytest.mark.parametrize("kept, expected", [
    pytest.param('{"sensor.pump_1": "°C", "sensor.pump_3": "°C"}', ["sensor.pump_1", "sensor.pump_3"], id="remembered"),
    pytest.param('{"sensor.pump_1": "°C", "sensor.private": "°C"}', ["sensor.pump_1"], id="no-longer-shared"),
    pytest.param('{"sensor.pump_1": "°C", "sensor.humidity": "°C"}', ["sensor.pump_1"], id="unit-has-changed"),
    pytest.param('{"sensor.pump_1": 1, "sensor.pump_3": null}', ["sensor.pump_1"], id="not-units"),
    pytest.param("[1, 2]", ["sensor.pump_1"], id="not-an-object"),
    pytest.param("null", ["sensor.pump_1"], id="null"),
    pytest.param("{oops", ["sensor.pump_1"], id="not-json"),
])
async def test_the_choice_is_read_back_from_the_links_own_storage(client, sample_token, mock_ha_client, kept, expected):
    out = await _compare(client, f"""
    stored['hp-compare:test-token'] = {json.dumps(kept)};
    stored['hp-compare:another-link'] = '{{"sensor.pump_2": "°C"}}';
    loadHistoryCompare();
    console.log(JSON.stringify(drawn('sensor.pump_1')));
    """)
    assert out == expected


@needs_node
async def test_storage_that_throws_does_not_stop_comparing(client, sample_token, mock_ha_client):
    out = await _compare(client, """
    window.localStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
    loadHistoryCompare();
    compare('sensor.pump_1', 'sensor.pump_2');
    console.log(JSON.stringify(drawn('sensor.pump_1')));
    """)
    assert out == PUMPS[:2]


@needs_node
async def test_a_compared_sensor_that_goes_unavailable_keeps_its_line(client, sample_token, mock_ha_client):
    """HA drops an unavailable entity's attributes, the unit with them, and
    that is the day its line is worth seeing next to the others."""
    out = await _compare(client, """
    compare('sensor.pump_1', 'sensor.pump_2');
    const gone = { state: 'unavailable', attributes: {} };
    states['sensor.pump_2'] = gone;
    states['sensor.pump_3'] = gone;
    console.log(JSON.stringify({
      rows: historySeries('sensor.pump_1'),
      from_the_dead_one: drawn('sensor.pump_2'),
      never_compared: historyPeers('sensor.pump_3'),
    }));
    """)
    assert [(r["eid"], r["slot"], r["on"]) for r in out["rows"]] == [
        ("sensor.pump_1", 0, True), ("sensor.pump_2", 1, True),
    ]
    assert out["from_the_dead_one"] == PUMPS[:2]
    assert out["never_compared"] == ["sensor.pump_3"]


@needs_node
async def test_a_sensor_keeps_its_colour_whatever_is_drawn(client, sample_token, mock_ha_client):
    out = await _compare(client, """
    const slots = eid => Object.fromEntries(historySeries(eid).map(r => [r.eid, r.slot]));
    const alone = slots('sensor.pump_3');
    compare('sensor.pump_3', 'sensor.pump_1');
    const dark = HISTORY_SERIES_COLORS.map((c, i) => historySeriesColor(i));
    document.documentElement.classList.contains = () => false;
    console.log(JSON.stringify({
      alone, compared: slots('sensor.pump_1'), dark,
      light: HISTORY_SERIES_COLORS.map((c, i) => historySeriesColor(i)),
    }));
    """)
    assert out["alone"] == out["compared"] == dict(zip(PUMPS, range(3)))
    # A step of each hue per theme, and no two lines alike in either.
    assert len(set(out["light"])) == len(set(out["dark"])) == len(out["light"]) >= 6
    assert out["light"] != out["dark"]


@needs_node
async def test_two_sensors_sharing_a_colour_are_never_drawn_together(client, sample_token, mock_ha_client):
    """More comparable sensors than colours: the one whose colour is on the
    chart waits, so a guest is never left guessing which line is which."""
    out = await _compare(client, """
    states = {};
    for (let i = 0; i < HISTORY_SERIES_COLORS.length + 2; i++) states[`sensor.s${i}`] = sensor(`S${i}`, 'W');
    entityOrder = Object.keys(states);
    entityMeta = Object.fromEntries(entityOrder.map(id => [id, { options: { show_history: true } }]));
    const n = HISTORY_SERIES_COLORS.length;
    const blocked = () => historySeries('sensor.s0').filter(r => r.blocked).map(r => r.eid);
    const first = blocked();
    compare('sensor.s0', `sensor.s${n}`);
    const refused = drawn('sensor.s0');
    compare('sensor.s0', 'sensor.s1');
    console.log(JSON.stringify({ n, first, refused, then: blocked(), drawn: drawn('sensor.s0'),
      html: historyLegendHtml('sensor.s0', historySeries('sensor.s0')) }));
    """)
    n = out["n"]
    assert out["first"] == [f"sensor.s{n}"]
    assert out["refused"] == ["sensor.s0"]
    assert out["then"] == [f"sensor.s{n}", f"sensor.s{n + 1}"]
    assert out["drawn"] == ["sensor.s0", "sensor.s1"]
    assert out["html"].count(" disabled") == 2


@needs_node
async def test_the_legend_is_the_switch_for_each_other_sensor(client, sample_token, mock_ha_client):
    out = await _compare(client, """
    compare('sensor.pump_1', 'sensor.pump_2');
    console.log(JSON.stringify({
      pump: historyLegendHtml('sensor.pump_1', historySeries('sensor.pump_1')),
      humidity: historyLegendHtml('sensor.humidity', historySeries('sensor.humidity')),
    }));
    """)
    # Nothing to compare with: no legend, and the sheet is as it always was.
    assert out["humidity"] == ""
    html = out["pump"]
    buttons = re.findall(r'<button[^>]*data-eid="([^"]+)"[^>]*aria-pressed="(\w+)"', html)
    assert buttons == [("sensor.pump_2", "true"), ("sensor.pump_3", "false")]
    # The sheet's own sensor is a row of the legend, and not one to switch off.
    assert html.startswith("<div") and html.count("data-history-value=") == 3
    assert "Pump &lt;2&gt;" in html and "<2>" not in html
    assert "disabled" not in html
    # A drawn line's dot is filled with its colour; one that is off is a ring.
    dots = re.findall(r'style="border-color:(#\w+);(?:background-color:(#\w+))?"', html)
    assert [bool(fill) for _, fill in dots] == [True, True, False]
    assert all(fill == ring for ring, fill in dots if fill)
    assert len({ring for ring, _ in dots}) == 3


@needs_node
async def test_an_outage_still_breaks_its_line_among_others(client, sample_token, mock_ha_client):
    """Joined onto shared times, a line is undefined wherever only another
    sensor reported, and uPlot draws straight across undefined. The gap has to
    be widened over those, or pump 1's outage would be a line through it."""
    out = await _compare(client, f"""
    // The real library, for its join. Handed no window, it leaves the DOM
    // the harness does not have alone.
    globalThis.uPlot = new Function('window', {json.dumps(UPLOT.read_text())} + '; return uPlot;')();
    const series = [
      {{ eid: 'sensor.pump_1', points: [[10, 20], [20, null], [50, 22]] }},
      {{ eid: 'sensor.pump_2', points: [[15, 30], [30, 31], [40, 32], [60, 33]] }},
    ];
    states['sensor.pump_1'].state = '23';
    states['sensor.pump_2'].state = 'unavailable';
    const show = data => data.map(col => Array.from(col, v => v === undefined ? 'none' : v));
    console.log(JSON.stringify({{
      both: show(historyData(series, 100)),
      one: show(historyData(series.slice(0, 1), 100)),
    }}));
    """)
    times, pump_1, pump_2 = out["both"]
    assert times == [10, 15, 20, 30, 40, 50, 60, 100]
    # The outage at 20 runs from the reading before it to the one after, across
    # pump 2's times, which is where a chart of pump 1 alone breaks its line.
    assert pump_1 == [20, None, None, None, None, 22, "none", 23]
    # Each live reading is carried to the right edge; an unavailable one is a gap.
    assert pump_2 == ["none", 30, "none", 31, 32, "none", 33, None]
    # A chart of one line is its own points, as before.
    assert out["one"] == [[10, 20, 50, 100], [20, None, 22, 23]]


@needs_node
async def test_only_a_new_line_costs_a_request(client, sample_token, mock_ha_client):
    out = await _compare(client, """
    const asked = [];
    let ok = true;
    globalThis.fetch = async url => {
      asked.push(url);
      return { ok, status: ok ? 200 : 429, json: async () => ({ points: [[1, 2]] }) };
    };
    await fetchHistory('sensor.pump_1', 24);
    const again = await fetchHistory('sensor.pump_1', 24);
    await fetchHistory('sensor.pump_1', 6);
    await fetchHistory('sensor.pump_2', 24);
    ok = false;
    const failed = await fetchHistory('sensor.pump_3', 24).catch(e => e.message);
    ok = true;
    await fetchHistory('sensor.pump_3', 24);
    console.log(JSON.stringify({ asked, again, failed }));
    """)
    assert out["again"] == [[1, 2]]
    assert out["failed"] == "429"
    assert out["asked"] == [
        "/g/test-token/history/sensor.pump_1?hours=24",
        "/g/test-token/history/sensor.pump_1?hours=6",
        "/g/test-token/history/sensor.pump_2?hours=24",
        # A refusal is not kept, so the next look asks again.
        "/g/test-token/history/sensor.pump_3?hours=24",
        "/g/test-token/history/sensor.pump_3?hours=24",
    ]


@needs_node
async def test_a_peer_that_cannot_be_fetched_is_left_off_the_chart(client, sample_token, mock_ha_client):
    """One sensor unshared since the page loaded, or one request over the
    link's budget, must not cost the guest the lines that did arrive. The
    sheet's own sensor failing is still the whole chart failing."""
    out = await _compare(client, """
    loadUplot = () => Promise.resolve();
    let calls = [];
    drawHistory = (eid, series, hours, legend) => { calls.push({ eid, hours, legend, series: series.map(s => s.eid) }); };
    let refuse = 'sensor.pump_2';
    globalThis.fetch = async url => ({ ok: !url.includes(refuse), status: 403, json: async () => ({ points: [[1, 2]] }) });
    compare('sensor.pump_1', 'sensor.pump_2');
    compare('sensor.pump_1', 'sensor.pump_3');
    historyHours = 6;
    await loadHistory(false);
    const peer_refused = calls.at(-1);
    calls = [];
    refuse = 'sensor.pump_1';
    historyFetched.clear();
    await loadHistory(false);
    console.log(JSON.stringify({
      peer_refused, own_refused: calls, status: document.getElementById('history-status').textContent,
    }));
    """)
    assert out["peer_refused"] == {
        "eid": "sensor.pump_1", "hours": 6, "legend": True,
        "series": ["sensor.pump_1", "sensor.pump_3"],
    }
    assert out["own_refused"] == []
    assert out["status"] == "Couldn't load the history. Try again in a moment."


async def test_the_sheet_has_a_legend_to_compare_from(client, sample_token, mock_ha_client):
    page = (await client.get("/g/test-token")).text
    legend = re.search(r'<div id="history-legend" class="([^"]*)">(.*?)</div>\s*</div>', page, re.S)
    # Hidden until a sensor with something to compare with is opened.
    assert legend and "hidden" in legend.group(1).split()
    assert ">Compare</p>" in legend.group(2)
    assert 'id="history-legend-rows"' in legend.group(2) and 'aria-label="Compare"' in legend.group(2)
