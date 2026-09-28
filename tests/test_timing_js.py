"""The dashboard's token timing form, run in node against the shipped script.

Same harness as test_picker_js: the admin page is rendered through the real
route and its inline script is executed with a stub DOM, so these assert on
the code the browser gets. The form logic under test is pure on purpose —
buildTimingBody turns raw form values into API fields, timingPresetFromToken
turns a token back into form values for Edit Schedule and Duplicate.

Skipped when node is not installed.
"""
import json

import pytest

from tests.test_picker_js import _run, node

pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")

NOW_MS = 1_790_000_000_000  # a fixed "now" so the probes need no clock


async def _probe(client, admin_session, body: str):
    return await _run(client, admin_session, f"const NOW_MS = {NOW_MS};\n" + body)


async def test_the_form_is_rendered_into_both_modals(client, admin_session, mock_ha_client):
    out = await _probe(client, admin_session, """
    console.log(JSON.stringify({
      create: document.getElementById('f-timing').innerHTML.includes('f-mode-single_use'),
      schedule: document.getElementById('s-timing').innerHTML.includes('s-mode-period'),
    }));
    """)
    assert out == {"create": True, "schedule": True}


async def test_never_mode(client, admin_session, mock_ha_client):
    out = await _probe(client, admin_session, """
    console.log(JSON.stringify(buildTimingBody({ mode: 'never' }, NOW_MS)));
    """)
    assert out["body"]["max_uses"] is None
    assert out["body"]["access_windows"] is None
    assert out["body"]["expires_at"] == 4102444800


async def test_single_use_mode(client, admin_session, mock_ha_client):
    out = await _probe(client, admin_session, """
    const end = toLocalInput(NOW_MS / 1000 + 86400);
    console.log(JSON.stringify({
      open: buildTimingBody({ mode: 'single_use', uses: '1', singleEnd: '' }, NOW_MS),
      dated: buildTimingBody({ mode: 'single_use', uses: '3', singleEnd: end }, NOW_MS),
      zero: buildTimingBody({ mode: 'single_use', uses: '0', singleEnd: '' }, NOW_MS),
      frac: buildTimingBody({ mode: 'single_use', uses: '1.5', singleEnd: '' }, NOW_MS),
      past: buildTimingBody({ mode: 'single_use', uses: '1', singleEnd: toLocalInput(NOW_MS / 1000 - 60) }, NOW_MS),
    }));
    """)
    assert out["open"]["body"] == {
        "starts_at": None, "expires_at": 4102444800, "access_windows": None, "max_uses": 1,
    }
    assert out["dated"]["body"]["max_uses"] == 3
    # datetime-local has minute resolution, so the round trip truncates seconds.
    assert abs(out["dated"]["body"]["expires_at"] - (NOW_MS // 1000 + 86400)) < 60
    assert "error" in out["zero"] and "error" in out["frac"] and "error" in out["past"]


async def test_period_mode_sends_an_absolute_end(client, admin_session, mock_ha_client):
    out = await _probe(client, admin_session, """
    const s = NOW_MS / 1000;
    const f = (start, end, extra = {}) => buildTimingBody(
      { mode: 'period', start, end, advanced: false, windows: [], ...extra }, NOW_MS);
    console.log(JSON.stringify({
      ok: f(toLocalInput(s + 3600), toLocalInput(s + 7200)),
      pastStart: f(toLocalInput(s - 3600), toLocalInput(s + 7200)),
      noEnd: f('', ''),
      backwards: f(toLocalInput(s + 7200), toLocalInput(s + 3600)),
    }));
    """)
    ok = out["ok"]["body"]
    assert ok["starts_at"] is not None and ok["expires_at"] - ok["starts_at"] == 3600
    assert "expires_in_seconds" not in ok
    # A past start folds to "now", as the server does, rather than erroring.
    assert out["pastStart"]["body"]["starts_at"] is None
    assert "error" in out["noEnd"] and "error" in out["backwards"]


async def test_weekly_windows(client, admin_session, mock_ha_client):
    out = await _probe(client, admin_session, """
    const end = toLocalInput(NOW_MS / 1000 + 86400 * 30);
    const f = windows => buildTimingBody(
      { mode: 'period', start: '', end, advanced: true, windows }, NOW_MS);
    console.log(JSON.stringify({
      overnight: f([{ weekdays: [4, 5], start: '22:00', end: '02:00' }]),
      midnight: f([{ weekdays: [6], start: '00:00', end: '00:00' }]),
      noDays: f([{ weekdays: [], start: '09:00', end: '10:00' }]),
      same: f([{ weekdays: [1], start: '09:00', end: '09:00' }]),
      none: f([]),
      off: buildTimingBody({ mode: 'period', start: '', end, advanced: false,
        windows: [{ weekdays: [1], start: '09:00', end: '10:00' }] }, NOW_MS),
    }));
    """)
    assert out["overnight"]["body"]["access_windows"] == [
        {"weekdays": [4, 5], "start": "22:00", "end": "02:00"}
    ]
    # 00:00 as an end is the close of the day: 00:00-00:00 is a whole day.
    assert out["midnight"]["body"]["access_windows"][0]["end"] == "24:00"
    assert "error" in out["noDays"] and "error" in out["same"] and "error" in out["none"]
    # Windows typed and then switched off are not sent.
    assert out["off"]["body"]["access_windows"] is None


TOKEN = {
    "label": "Stay", "created_at": NOW_MS // 1000 - 86400,
    "starts_at": NOW_MS // 1000 + 3600, "expires_at": NOW_MS // 1000 + 3600 + 3 * 86400,
    "access_windows": [{"weekdays": [1], "start": "09:00", "end": "24:00"}],
    "max_uses": None, "use_count": 0,
}


async def test_edit_preset_reflects_the_token(client, admin_session, mock_ha_client):
    out = await _probe(client, admin_session, f"""
    const t = {json.dumps(TOKEN)};
    const p = timingPresetFromToken(t, NOW_MS, false);
    console.log(JSON.stringify({{ ...p,
      startOk: p.start === toLocalInput(t.starts_at), endOk: p.end === toLocalInput(t.expires_at) }}));
    """)
    assert out["mode"] == "period" and out["advanced"] is True
    assert out["startOk"] and out["endOk"]
    # A time input cannot hold 24:00, so it is shown as 00:00.
    assert out["windows"][0]["end"] == "00:00"


async def test_duplicate_preset_keeps_the_shape_not_the_dates(client, admin_session, mock_ha_client):
    out = await _probe(client, admin_session, f"""
    const t = {json.dumps(TOKEN)};
    const p = timingPresetFromToken(t, NOW_MS, true);
    const single = timingPresetFromToken({{ ...t, max_uses: 2, access_windows: null,
      expires_at: 4102444800 }}, NOW_MS, true);
    console.log(JSON.stringify({{ p, single,
      lengthOk: p.end === toLocalInput(NOW_MS / 1000 + 3 * 86400) }}));
    """)
    assert out["p"]["start"] == ""
    assert out["lengthOk"]
    assert out["p"]["windows"] == [{"weekdays": [1], "start": "09:00", "end": "00:00"}]
    assert out["single"]["mode"] == "single_use"
    assert out["single"]["uses"] == 2
    assert out["single"]["singleEnd"] == ""


async def test_status_and_summary(client, admin_session, mock_ha_client):
    out = await _probe(client, admin_session, """
    const far = Date.now() / 1000 + 86400;
    console.log(JSON.stringify({
      used: tokenStatus({ revoked: false, uses_remaining: 0, expires_at: far }),
      left: tokenStatus({ revoked: false, uses_remaining: 1, expires_at: far }),
      revokedWins: tokenStatus({ revoked: true, uses_remaining: 0, expires_at: far }),
      summary: windowsSummary([
        { weekdays: [1, 3], start: '09:00', end: '13:00' },
        { weekdays: [4], start: '22:00', end: '02:00' },
      ]),
    }));
    """)
    assert out["used"] == "used"
    assert out["left"] == "active"
    assert out["revokedWins"] == "revoked"
    assert out["summary"] == "Tue, Thu 09:00–13:00 · Fri 22:00–02:00"
