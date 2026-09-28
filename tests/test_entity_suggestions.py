"""The create-token picker's Suggest chips and the endpoint behind them.

Suggestions are advisory — they add to the picker's selection and grant
nothing until the admin saves a token — so what is tested here is that they
stay narrow: whole-word matching, the domain allowlist, and device classes.
"""
import pytest

from tests.test_picker_js import _run, node


def _state(entity_id, name=None, **attrs):
    attributes = dict(attrs)
    if name is not None:
        attributes["friendly_name"] = name
    return {"entity_id": entity_id, "state": "off", "attributes": attributes}


STATES = [
    # access
    _state("lock.shed", "Shed"),                                   # every lock
    _state("cover.garage", "Big Door", device_class="garage"),     # by device class
    _state("cover.side_gate", "Side Gate"),                        # by keyword
    _state("button.portal_abrir", "Abrir portal"),                 # Spanish keyword
    _state("input_button.front_door_buzzer", "Buzzer"),            # keyword in the id
    # lights
    _state("light.kitchen", "Kitchen"),                            # every light
    _state("switch.bedside", "Bedside Lamp"),                      # keyword in the name
    _state("switch.salon", "Lámpara salón"),                       # accent folded
    # a cover named as a light is still access, never lights
    _state("cover.garage_door_light", "Garage Door Light", device_class="garage"),
    # neither — each would match a substring-based matcher
    _state("cover.outdoor_blinds", "Outdoor Blinds", device_class="blind"),
    _state("button.navigate_home", "Navigate"),
    _state("switch.daylight_mode", "Daylight Mode"),
    _state("switch.coffee_maker", "Coffee Maker"),
    _state("cover.living_room_shade", "Living Room", device_class="shade"),
    # a keyword on an unsupported domain is still out of bounds
    _state("script.open_garage_door", "Open Garage Door"),
    _state("automation.door_lights", "Door Lights"),
    # a keyword on a domain that is not in that category
    _state("binary_sensor.front_door", "Front Door", device_class="door"),
    _state("sensor.hall_light_level", "Hall Light Level"),
]


async def _suggest(client, admin_session, mock_ha_client, query=""):
    mock_ha_client["get_states"].return_value = STATES
    return await client.get(f"/admin/ha/suggested-entities{query}", cookies=admin_session)


def _by_category(rows):
    out = {}
    for row in rows:
        out.setdefault(row["category"], []).append(row["entity_id"])
    return out


async def test_requires_admin(client, mock_ha_client):
    resp = await client.get("/admin/ha/suggested-entities")
    assert resp.status_code == 401


async def test_suggestions_are_narrow(client, admin_session, mock_ha_client):
    resp = await _suggest(client, admin_session, mock_ha_client)
    assert resp.status_code == 200
    got = _by_category(resp.json())
    assert got["access"] == [
        "lock.shed", "cover.garage", "cover.side_gate", "button.portal_abrir",
        "input_button.front_door_buzzer", "cover.garage_door_light",
    ]
    assert got["lights"] == [
        "light.kitchen", "switch.bedside", "switch.salon",
    ]


async def test_a_name_never_moves_an_entity_across_categories(client, admin_session, mock_ha_client):
    """Access and lights draw on disjoint domains: a light called "Garage Door"
    is still only a light, and a switch is only ever a light."""
    mock_ha_client["get_states"].return_value = [
        _state("light.garage_door", "Garage Door Light"),
        _state("switch.gate_lights", "Gate Lights"),
    ]
    resp = await client.get("/admin/ha/suggested-entities", cookies=admin_session)
    assert _by_category(resp.json()) == {
        "lights": ["light.garage_door", "switch.gate_lights"],
    }


async def test_single_category(client, admin_session, mock_ha_client):
    resp = await _suggest(client, admin_session, mock_ha_client, "?categories=lights")
    assert {r["category"] for r in resp.json()} == {"lights"}


@pytest.mark.parametrize("query", ["?categories=", "?categories=cameras", "?categories=lights,all"])
async def test_unknown_categories_are_refused(client, admin_session, mock_ha_client, query):
    resp = await _suggest(client, admin_session, mock_ha_client, query)
    assert resp.status_code == 422


async def test_suggestions_never_leave_supported_domains(client, admin_session, mock_ha_client):
    from app.models import SUPPORTED_DOMAINS

    resp = await _suggest(client, admin_session, mock_ha_client)
    assert all(r["entity_id"].split(".")[0] in SUPPORTED_DOMAINS for r in resp.json())


async def test_missing_friendly_name_is_tolerated(client, admin_session, mock_ha_client):
    mock_ha_client["get_states"].return_value = [
        {"entity_id": "cover.gate", "state": "closed"},
        {"entity_id": "switch.porch_light", "state": "off", "attributes": {"friendly_name": None}},
    ]
    resp = await client.get("/admin/ha/suggested-entities", cookies=admin_session)
    assert _by_category(resp.json()) == {"access": ["cover.gate"], "lights": ["switch.porch_light"]}


async def test_ha_unreachable_is_a_502(client, admin_session, mock_ha_client):
    mock_ha_client["get_states"].side_effect = RuntimeError("down")
    resp = await client.get("/admin/ha/suggested-entities", cookies=admin_session)
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# Picker integration (the shipped inline script, run in node)
# ---------------------------------------------------------------------------

_needs_node = pytest.mark.skipif(node is None, reason="node is not installed")


@_needs_node
async def test_suggest_adds_to_the_create_selection_only_what_the_picker_lists(
    client, admin_session, mock_ha_client,
):
    out = await _run(client, admin_session, """
    allEntities = [
      { entity_id: 'lock.front', friendly_name: 'Front', domain: 'lock', state: 'locked', labels: [] },
      { entity_id: 'light.hall', friendly_name: 'Hall', domain: 'light', state: 'off', labels: [] },
    ];
    createPicker.selected = new Set(['light.hall']);
    const toasts = [];
    showToast = (msg) => toasts.push(msg);
    globalThis.fetch = async (url) => ({
      ok: true,
      json: async () => [
        { entity_id: 'lock.front', category: 'access' },
        // Not in allEntities: HA reported it but the picker would not offer it.
        { entity_id: 'lock.ghost', category: 'access' },
      ],
      url,
    });
    await suggestInto('create-picker', 'access');
    const afterFirst = [...createPicker.selected].sort();
    await suggestInto('create-picker', 'access');
    console.log(JSON.stringify({ afterFirst, toasts }));
    """)
    # Additive: the light chosen by hand is still there.
    assert out["afterFirst"] == ["light.hall", "lock.front"]
    assert "Added 1 suggested entity" in out["toasts"][0]
    assert out["toasts"][1].startswith("No new")


@_needs_node
async def test_suggest_chips_render_on_the_create_picker_only(client, admin_session, mock_ha_client):
    out = await _run(client, admin_session, """
    allEntities = [];
    renderPicker('create-picker', createPicker);
    renderPicker('edit-picker', editPicker);
    console.log(JSON.stringify({
      create: (document.getElementById('create-picker').innerHTML.match(/data-action="picker-suggest"/g) || []).length,
      edit: document.getElementById('edit-picker').innerHTML.includes('picker-suggest'),
    }));
    """)
    assert out == {"create": 2, "edit": False}
