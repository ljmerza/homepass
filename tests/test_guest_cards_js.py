"""The guest cards' Mushroom-style look: state colours, dimming, names.

The cards are drawn by the inline script in templates/guest_pwa.html with the
state palette from static/domains.js, so — as in test_picker_js.py — the page
is rendered through the real guest route and its script run in node against a
stub DOM. The assertions are on the shipped code, not on a copy of it.

What is pinned here is behaviour a guest can see and nothing errors on when it
breaks: an icon shape that stops following its entity, a switched-off light
that fades its own name again, a class Tailwind never emits.

Skipped when node is not installed.
"""
import json
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.test_picker_js import DOM_STUB, REPO_SCRIPTS, node

ROOT = Path(__file__).resolve().parent.parent

# The guest page listens on window and opens a stream as soon as it runs.
GUEST_STUB = """
globalThis.window.addEventListener = () => {};
globalThis.EventSource = class { addEventListener() {} close() {} };
globalThis.performance = { now: () => 0 };
"""

NEUTRAL = "bg-soot/[0.07] dark:bg-white/10"

needs_node = pytest.mark.skipif(node is None, reason="node is not installed")


def _guest_script(page_html: str) -> str:
    blocks = re.findall(r'<script nonce="[^"]*">(.*?)</script>', page_html, re.S)
    assert blocks, "no inline script on the guest page"
    return blocks[-1]


async def _run(client, probe: str):
    resp = await client.get("/g/test-token")
    assert resp.status_code == 200
    parts = [DOM_STUB, GUEST_STUB] + [(ROOT / rel).read_text() for rel in REPO_SCRIPTS]
    parts += [_guest_script(resp.text), textwrap.dedent(probe)]
    proc = subprocess.run(
        [node, "--input-type=module"], input="\n".join(parts),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _st(state, **attributes):
    return {"state": state, "attributes": attributes}


# (domain, state, expected shape background class)
COLOUR_CASES = [
    # Idle goes neutral; doing something takes a colour.
    ("light", _st("on"), "bg-amber-500/15"),
    ("light", _st("off"), NEUTRAL),
    ("switch", _st("on"), "bg-teal-600/15"),
    ("input_boolean", _st("off"), NEUTRAL),
    ("group", _st("on"), "bg-teal-600/15"),
    ("fan", _st("on"), "bg-teal-600/15"),
    ("fan", _st("off"), NEUTRAL),
    # A lock is coloured either way: secured, open, or on its way.
    ("lock", _st("locked"), "bg-emerald-500/15"),
    ("lock", _st("unlocked"), "bg-red-500/15"),
    ("lock", _st("open"), "bg-red-500/15"),
    ("lock", _st("jammed"), "bg-red-500/15"),
    ("lock", _st("unlocking"), "bg-amber-500/15"),
    # The action leads, the mode stands in for an idling unit — an idle
    # heater is not "cooling", which is what the fork this is ported from
    # showed for it.
    ("climate", _st("heat", hvac_action="heating"), "bg-red-500/15"),
    ("climate", _st("heat", hvac_action="idle"), "bg-red-500/15"),
    ("climate", _st("cool", hvac_action="cooling"), "bg-blue-500/15"),
    ("climate", _st("heat_cool"), "bg-amber-500/15"),
    ("climate", _st("off", hvac_action="off"), NEUTRAL),
    ("alarm_control_panel", _st("armed_away"), "bg-emerald-500/15"),
    ("alarm_control_panel", _st("armed_night"), "bg-emerald-500/15"),
    ("alarm_control_panel", _st("disarmed"), NEUTRAL),
    ("alarm_control_panel", _st("arming"), "bg-amber-500/15"),
    ("alarm_control_panel", _st("triggered"), "bg-red-500/15"),
    ("cover", _st("closed"), NEUTRAL),
    ("cover", _st("open"), "bg-sky-500/15"),
    ("cover", _st("opening"), "bg-sky-500/15"),
    ("media_player", _st("playing"), "bg-purple-500/15"),
    ("media_player", _st("idle"), NEUTRAL),
    ("timer", _st("active"), "bg-pink-500/15"),
    ("timer", _st("paused"), "bg-amber-500/15"),
    ("timer", _st("idle"), NEUTRAL),
    ("schedule", _st("on"), "bg-emerald-500/15"),
    ("binary_sensor", _st("on"), "bg-amber-500/15"),
    ("binary_sensor", _st("off"), NEUTRAL),
    # A never-pressed button reports 'unknown'; that is its resting state.
    ("button", _st("unknown"), "bg-orange-500/15"),
    ("input_button", _st("2026-01-01T10:00:00+00:00"), "bg-orange-500/15"),
    ("button", _st("unavailable"), NEUTRAL),
    # Value domains keep one colour while they have a value at all.
    ("sensor", _st("21.5", unit_of_measurement="°C"), "bg-cyan-500/15"),
    ("sensor", _st("unavailable"), NEUTRAL),
    ("sensor", _st("unknown"), NEUTRAL),
    ("counter", _st("3"), "bg-yellow-500/15"),
    ("input_number", _st("40"), "bg-violet-500/15"),
    ("input_select", _st("Eco"), "bg-fuchsia-500/15"),
    ("input_datetime", _st("2026-01-01 10:00:00"), "bg-emerald-500/15"),
    ("camera", _st("streaming"), "bg-indigo-500/15"),
    ("camera", _st("unavailable"), NEUTRAL),
    # Anything unknown to the palette: on is the one state worth a colour.
    ("vacuum", _st("on"), "bg-amber-500/15"),
    ("vacuum", _st("docked"), NEUTRAL),
]


@needs_node
async def test_the_shape_colour_follows_what_the_entity_is_doing(client, sample_token, mock_ha_client):
    cases = [{"domain": d, "state": s} for d, s, _ in COLOUR_CASES]
    out = await _run(client, f"""
    const cases = {json.dumps(cases)};
    console.log(JSON.stringify(cases.map(c => entityColor(c.domain, c.state).bg)));
    """)
    for (domain, state, expected), got in zip(COLOUR_CASES, out):
        assert got == expected, (domain, state, got)


@needs_node
async def test_a_coloured_bulb_shows_its_own_colour(client, sample_token, mock_ha_client):
    out = await _run(client, """
    const rgb = s => lightRgb({ state: 'on', attributes: s });
    console.log(JSON.stringify({
      red: rgb({ rgb_color: [255, 0, 0], color_mode: 'hs' }),
      clamped: rgb({ rgb_color: [300, -5, 10.4] }),
      nearWhite: rgb({ rgb_color: [255, 240, 230] }),
      whiteMode: rgb({ rgb_color: [255, 120, 0], color_mode: 'color_temp' }),
      junk: rgb({ rgb_color: ['x', 1, 2] }),
      injected: rgb({ rgb_color: ['255); background: url(x)', 0, 0] }),
      short: rgb({ rgb_color: [1, 2] }),
      none: rgb({}),
      offColour: entityColor('light', { state: 'off', attributes: { rgb_color: [255, 0, 0] } }).rgb ?? null,
      onColour: entityColor('light', { state: 'on', attributes: { rgb_color: [0, 0, 255] } }).rgb,
    }));
    """)
    assert out["red"] == [255, 0, 0]
    assert out["clamped"] == [255, 0, 10]
    # Near-white would vanish on a light card, so it keeps the plain amber.
    assert out["nearWhite"] is None
    assert out["whiteMode"] is None
    # Only finite numbers make it into a style attribute.
    assert out["junk"] is None
    assert out["injected"] is None
    assert out["short"] is None
    assert out["none"] is None
    assert out["offColour"] is None
    assert out["onColour"] == [0, 0, 255]


@needs_node
async def test_switching_off_no_longer_fades_the_card(client, sample_token, mock_ha_client):
    """Only the shape goes quiet for "off"; an unreachable entity still dims."""
    out = await _run(client, """
    const card = (eid, state) => buildCard(eid, { entity_id: eid, state, attributes: { friendly_name: 'X' } });
    console.log(JSON.stringify({
      lightOff: card('light.a', 'off'),
      switchOff: card('switch.b', 'off'),
      fanOff: card('fan.c', 'off'),
      lightGone: card('light.d', 'unavailable'),
      sensorUnknown: card('sensor.e', 'unknown'),
      buttonNeverPressed: card('button.f', 'unknown'),
    }));
    """)
    for key in ("lightOff", "switchOff", "fanOff", "buttonNeverPressed"):
        assert "opacity-60" not in out[key], key
    assert "opacity-60" in out["lightGone"]
    assert "opacity-60" in out["sensorUnknown"]
    assert NEUTRAL in out["lightOff"]


@needs_node
async def test_the_card_is_borderless_with_a_decorative_shape(client, sample_token, mock_ha_client):
    out = await _run(client, """
    entityMeta = {};
    const light = buildCard('light.a', { entity_id: 'light.a', state: 'on',
      attributes: { friendly_name: 'Lamp', rgb_color: [0, 128, 255] } });
    const lock = buildCard('lock.b', { entity_id: 'lock.b', state: 'locked', attributes: { friendly_name: 'Door' } });
    console.log(JSON.stringify({ light, lock }));
    """)
    light, lock = out["light"], out["lock"]
    outer = re.match(r'<div [^>]*class="([^"]*)"', light).group(1)
    assert "border" not in outer.split()
    assert "shadow-card" in outer
    assert 'id="shape-light-a"' in light and "entity-shape" in light
    # The shape carries no role or focus of its own; its icon is hidden from
    # assistive tech, and the state stays spelled out in words next to it.
    shape = re.search(r'<div class="entity-shape[^>]*>', light).group(0)
    assert "tabindex" not in shape and "role=" not in shape
    assert re.search(r'<span aria-hidden="true" class="material-symbols-outlined[^"]*" id="icon-light-a"', light)
    assert 'id="state-light-a"' in light
    # The bulb's own colour, as a style built from clamped integers.
    assert 'style="background-color: rgb(0 128 255 / 0.2)"' in light
    assert 'style="color: rgb(0 128 255)"' in light
    # The whole tile stays a keyboard-reachable toggle.
    assert 'role="button" tabindex="0"' in light and 'role="switch"' in light
    assert "bg-emerald-500/15" in lock and "style=" not in re.search(r'<div class="entity-shape[^>]*>', lock).group(0)
    # Strict CSP: markup carries data-action hooks, never inline handlers.
    assert not re.search(r"\son[a-z]+=", light + lock)


@needs_node
async def test_live_updates_repaint_the_shape_and_leave_the_card_lit(client, sample_token, mock_ha_client):
    out = await _run(client, """
    entityMeta = {};
    const toggles = [];
    const card = document.getElementById('card-lock-b');
    card.classList = { add() {}, remove() {}, contains() { return false; },
      toggle(c, on) { toggles.push([c, on]); } };
    card.parentElement = null;
    updateCard('lock.b', { entity_id: 'lock.b', state: 'locked', attributes: {} });
    const locked = document.getElementById('shape-lock-b').className;
    updateCard('lock.b', { entity_id: 'lock.b', state: 'unlocked', attributes: {} });
    const unlocked = document.getElementById('shape-lock-b').className;
    const iconUnlocked = document.getElementById('icon-lock-b').className;

    const lampCard = document.getElementById('card-light-a');
    const lampToggles = [];
    lampCard.classList = { add() {}, remove() {}, contains() { return false; },
      toggle(c, on) { lampToggles.push([c, on]); } };
    updateCard('light.a', { entity_id: 'light.a', state: 'on', attributes: { rgb_color: [255, 0, 0] } });
    const lampOnBg = document.getElementById('shape-light-a').style.backgroundColor;
    updateCard('light.a', { entity_id: 'light.a', state: 'off', attributes: { rgb_color: [255, 0, 0] } });
    const lampOff = document.getElementById('shape-light-a').className;
    const lampOffBg = document.getElementById('shape-light-a').style.backgroundColor;
    console.log(JSON.stringify({ locked, unlocked, iconUnlocked, toggles, lampOnBg, lampOff, lampOffBg, lampToggles }));
    """)
    assert out["locked"] == "entity-shape bg-emerald-500/15"
    assert out["unlocked"] == "entity-shape bg-red-500/15"
    assert out["iconUnlocked"] == "material-symbols-outlined text-red-500 dark:text-red-400"
    assert out["lampOnBg"] == "rgb(255 0 0 / 0.2)"
    assert out["lampOff"] == f"entity-shape {NEUTRAL}"
    # The device colour is cleared with the state that earned it.
    assert out["lampOffBg"] == ""
    # The only opacity decision is the unreachable one, and "off" is not it.
    assert all(c != "opacity-60" or on is False for c, on in out["toggles"] + out["lampToggles"])


@needs_node
async def test_an_entity_ha_lost_still_gets_a_legible_name(client, sample_token, mock_ha_client):
    """An unavailable entity arrives with no attributes, friendly_name included."""
    out = await _run(client, """
    entityMeta = { 'light.renamed': { display_name: 'Guest lamp' } };
    console.log(JSON.stringify({
      bare: displayName('light.hall_lamp', { state: 'unavailable', attributes: {} }),
      friendly: displayName('light.hall_lamp', { attributes: { friendly_name: 'Hall' } }),
      override: displayName('light.renamed', { attributes: { friendly_name: 'Hall' } }),
      odd: displayName('light.__', { attributes: {} }),
    }));
    """)
    assert out["bare"] == "Hall Lamp"
    assert out["friendly"] == "Hall"
    assert out["override"] == "Guest lamp"
    assert out["odd"] == "light.__"


@needs_node
async def test_group_headings_are_quiet_real_headings(client, sample_token, mock_ha_client):
    out = await _run(client, """
    entityMeta = {};
    entityOrder = ['light.a', 'lock.b'];
    states = {
      'light.a': { entity_id: 'light.a', state: 'off', attributes: { friendly_name: 'Lamp' } },
      'lock.b': { entity_id: 'lock.b', state: 'locked', attributes: { friendly_name: 'Door' } },
    };
    renderAll();
    console.log(JSON.stringify(document.getElementById('cards-container').innerHTML));
    """)
    headings = re.findall(r"<h2 [^>]*>([^<]*)</h2>", out)
    assert headings == ["Lights", "Locks"]
    # No competing domain icon or shouty mono caps in the heading any more.
    assert "uppercase" not in re.search(r"<h2 [^>]*>", out).group(0)


def test_every_state_colour_class_is_one_tailwind_can_see():
    """Tailwind only emits classes it finds in `content`. A colour assembled at
    runtime, or a scan that stops covering static/, renders an untinted shape —
    and nothing errors, so only a test notices."""
    config = (ROOT / "tailwind.config.js").read_text()
    content = re.search(r"content:\s*\[(.*?)\]", config, re.S).group(1)
    assert "./static/**/*.js" in content
    domains = (ROOT / "static" / "domains.js").read_text()
    palette = re.search(r"const STATE_COLORS = \{(.*?)\n\};", domains, re.S).group(1)
    entries = re.findall(r"(\w+): \{ bg: '([^']+)', text: '([^']+)' \}", palette)
    assert len(entries) >= 10
    for _, bg, text in entries:
        # Whole literals, one opacity for every shape, a dark variant where the
        # light one would be too dim on the dark surface.
        assert re.fullmatch(r"bg-[a-z]+-\d{3}/15", bg), bg
        assert re.fullmatch(r"text-[a-z]+-\d{3}( dark:text-[a-z]+-\d{3})?", text), text
    used = set(re.findall(r"STATE_COLORS\.(\w+)", domains))
    for table in ("VALUE_DOMAIN_COLORS", "CLIMATE_ACTION_COLORS", "CLIMATE_MODE_COLORS"):
        body = re.search(rf"const {table} = \{{(.*?)\}};", domains, re.S).group(1)
        used |= set(re.findall(r"'(\w+)'", body))
    # Every colour name a lookup reaches for exists in the palette.
    assert used and used <= {name for name, _, _ in entries}, used - {name for name, _, _ in entries}
