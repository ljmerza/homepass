"""Per-entity "only from the home network" gate: the range arithmetic, the
command path, and the admin side that sets the flag.

Same shape as test_proximity.py — real routing, real DB; only ha_client is
mocked. The properties carrying the weight:

  * the gate is per entity, so an ungated entity on the same token is never
    refused for where the guest is;
  * only commands are gated — the page and the state feed work from anywhere;
  * it uses the same client address every other guest check uses, and fails
    closed on one it cannot read; and
  * an empty local_network_cidrs option turns it off, flags and all.
"""
import time

import pytest
import pytest_asyncio
from pydantic import ValidationError

from app import database as db
from app import local_network
from app.config import Settings, settings

LOCK = "lock.front_door"
LAMP = "light.living_room"
HOME = "192.168.1.0/24, fd00::/8"
INSIDE = "192.168.1.50"
OUTSIDE = "203.0.113.9"


@pytest.fixture
def home_network(monkeypatch):
    monkeypatch.setattr(settings, "local_network_cidrs", HOME)


async def _make_token(slug: str, gated: bool = True, proximity: bool = False):
    meta = {LOCK: {"require_local_network": gated, "require_proximity": proximity}}
    return await db.create_token(
        label=f"Token {slug}",
        slug=slug,
        entity_ids=[LOCK, LAMP],
        expires_at=int(time.time()) + 3600,
        ip_allowlist=None,
        entity_meta=meta,
    )


@pytest_asyncio.fixture
async def gated_token(test_db):
    return await _make_token("netgated")


async def _command(client, entity_id: str = LOCK, ip: str | None = OUTSIDE, **extra):
    service = "lock.unlock" if entity_id == LOCK else "light.turn_on"
    headers = {"X-Forwarded-For": ip} if ip else {}
    return await client.post(
        "/g/netgated/command",
        json={"entity_id": entity_id, "service": service, **extra},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Range arithmetic
# ---------------------------------------------------------------------------

def test_empty_option_is_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "local_network_cidrs", "")
    assert not local_network.is_configured()
    assert not local_network.contains(INSIDE)


def test_contains(home_network):
    assert local_network.is_configured()
    assert local_network.contains(INSIDE)
    assert local_network.contains("fd12:3456::1")
    assert not local_network.contains(OUTSIDE)
    assert not local_network.contains("2001:db8::1")


def test_ipv4_mapped_address_matches_its_ipv4_range(home_network):
    assert local_network.contains("::ffff:192.168.1.50")


@pytest.mark.parametrize("bad", ["unknown", "", "not-an-ip", "192.168.1"])
def test_unreadable_address_is_outside(home_network, bad):
    assert not local_network.contains(bad)


def test_invalid_entry_assigned_later_admits_nobody(monkeypatch):
    """Settings refuses a bad entry at startup; one slipped in afterwards is
    skipped, which is the strict side."""
    monkeypatch.setattr(settings, "local_network_cidrs", "garbage, 10.0.0.0/8")
    assert local_network.contains("10.1.2.3")
    assert not local_network.contains(INSIDE)


def test_settings_reject_an_invalid_cidr():
    with pytest.raises(ValidationError, match="local_network_cidrs"):
        Settings(
            ha_base_url="http://ha:8123", ha_token="t",
            admin_username="a", admin_password="password123",
            local_network_cidrs="192.168.1.0/24, 999.1.1.0/24",
        )


def test_settings_accept_valid_cidrs():
    s = Settings(
        ha_base_url="http://ha:8123", ha_token="t",
        admin_username="a", admin_password="password123",
        local_network_cidrs=" 192.168.1.0/24 ,fd00::/8, ",
    )
    assert s.local_network_cidrs


# ---------------------------------------------------------------------------
# The command path
# ---------------------------------------------------------------------------

async def test_gated_command_from_outside_is_refused(
    client, gated_token, home_network, mock_ha_client
):
    resp = await _command(client)
    assert resp.status_code == 403
    assert "home network" in resp.json()["detail"]
    mock_ha_client["call_service"].assert_not_called()


async def test_gated_command_from_inside_goes_through(
    client, gated_token, home_network, mock_ha_client
):
    resp = await _command(client, ip=INSIDE)
    assert resp.status_code == 200
    mock_ha_client["call_service"].assert_called_once()


async def test_ipv6_home_range_is_honoured(client, gated_token, home_network, mock_ha_client):
    assert (await _command(client, ip="fd00::abcd")).status_code == 200


async def test_ungated_entity_is_never_refused(
    client, gated_token, home_network, mock_ha_client
):
    resp = await _command(client, entity_id=LAMP)
    assert resp.status_code == 200


async def test_viewing_works_from_anywhere(client, gated_token, home_network, mock_ha_client):
    headers = {"X-Forwarded-For": OUTSIDE}
    assert (await client.get("/g/netgated", headers=headers)).status_code == 200
    state = await client.get("/g/netgated/state", headers=headers)
    assert state.status_code == 200
    assert state.json()["entity_meta"][LOCK]["require_local_network"] is True


async def test_gate_is_off_while_the_option_is_empty(
    client, gated_token, monkeypatch, mock_ha_client
):
    monkeypatch.setattr(settings, "local_network_cidrs", "")
    assert (await _command(client)).status_code == 200


async def test_peer_address_is_used_without_a_forwarded_header(
    client, gated_token, monkeypatch, mock_ha_client
):
    """No X-Forwarded-For: the socket peer (127.0.0.1 under the test transport)
    is the client address, the same fallback the IP allowlist uses."""
    monkeypatch.setattr(settings, "local_network_cidrs", "127.0.0.0/8")
    assert (await _command(client, ip=None)).status_code == 200
    monkeypatch.setattr(settings, "local_network_cidrs", "10.0.0.0/8")
    assert (await _command(client, ip=None)).status_code == 403


async def test_network_check_comes_before_proximity(
    client, test_db, home_network, mock_ha_client
):
    """Off the network, the guest is refused without being asked where they are,
    and the proximity refusal budget is not spent."""
    await _make_token("netgated", proximity=True)
    resp = await _command(client)
    assert resp.status_code == 403
    assert "home network" in resp.json()["detail"]
    mock_ha_client["get_home_zone"].assert_not_called()


async def test_refusal_comes_after_the_allowlist_checks(
    client, gated_token, home_network, mock_ha_client
):
    """An entity not on the token is still "not in allowlist", not a network refusal."""
    resp = await client.post(
        "/g/netgated/command",
        json={"entity_id": "lock.back_door", "service": "lock.unlock"},
        headers={"X-Forwarded-For": OUTSIDE},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Entity not in allowlist"


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

async def test_create_persists_the_flag(client, admin_session, mock_ha_client):
    resp = await client.post(
        "/admin/tokens",
        json={
            "label": "Cleaner",
            "entity_ids": [LOCK, LAMP],
            "expires_in_seconds": 3600,
            "entity_meta": {LOCK: {"require_local_network": True}},
        },
        cookies=admin_session,
    )
    assert resp.status_code == 201
    token_id = resp.json()["id"]
    assert await db.get_local_network_entity_ids(token_id) == {LOCK}
    meta = await db.get_token_entity_meta(token_id)
    assert meta[LAMP]["require_local_network"] is False


async def test_flag_arriving_inside_options_is_ignored(client, admin_session, mock_ha_client):
    """The flag is an access control in its own column, never a presentation option."""
    resp = await client.post(
        "/admin/tokens",
        json={
            "label": "Cleaner",
            "entity_ids": [LOCK],
            "expires_in_seconds": 3600,
            "entity_meta": {LOCK: {"options": {"require_local_network": True}}},
        },
        cookies=admin_session,
    )
    assert await db.get_local_network_entity_ids(resp.json()["id"]) == set()


async def test_patch_entity_meta_sets_and_clears(
    client, admin_session, gated_token, mock_ha_client
):
    url = f"/admin/tokens/{gated_token['id']}/entity-meta"
    resp = await client.patch(
        url, json={"entity_id": LAMP, "require_local_network": True}, cookies=admin_session
    )
    assert resp.status_code == 200
    assert resp.json()["require_local_network"] is True
    assert await db.get_local_network_entity_ids(gated_token["id"]) == {LOCK, LAMP}

    await client.patch(url, json={"entity_id": LOCK}, cookies=admin_session)
    assert await db.get_local_network_entity_ids(gated_token["id"]) == {LAMP}


async def test_editing_entities_preserves_the_flag(
    client, admin_session, gated_token, mock_ha_client
):
    resp = await client.patch(
        f"/admin/tokens/{gated_token['id']}/entities",
        json={"entity_ids": [LOCK, LAMP, "switch.porch"]},
        cookies=admin_session,
    )
    assert resp.status_code == 200
    assert await db.get_local_network_entity_ids(gated_token["id"]) == {LOCK}


async def test_dashboard_hides_the_toggle_without_a_home_network(
    client, admin_session, monkeypatch, mock_ha_client
):
    monkeypatch.setattr(settings, "local_network_cidrs", "")
    resp = await client.get("/admin/dashboard", cookies=admin_session)
    assert "const LOCAL_NETWORKS = [];" in resp.text


async def test_dashboard_lists_the_home_network(
    client, admin_session, home_network, mock_ha_client
):
    resp = await client.get("/admin/dashboard", cookies=admin_session)
    assert '"192.168.1.0/24"' in resp.text
    assert "entity-require-local-network" in resp.text


def test_addon_option_is_wired_through():
    with open("config.yaml") as fh:
        config = fh.read()
    assert 'local_network_cidrs: ""' in config
    assert 'local_network_cidrs: "str?"' in config
    with open("run.sh") as fh:
        assert "'local_network_cidrs': 'LOCAL_NETWORK_CIDRS'" in fh.read()
    with open("translations/en.yaml") as fh:
        assert "local_network_cidrs:" in fh.read()


# ---------------------------------------------------------------------------
# The picker's default for access-type domains
# ---------------------------------------------------------------------------

async def _picker_defaults(client, admin_session):
    from tests.test_picker_js import _run

    return await _run(client, admin_session, """
    allEntities = [
      { entity_id: 'lock.front_door', friendly_name: 'Front', domain: 'lock', state: 'locked' },
      { entity_id: 'light.lamp', friendly_name: 'Lamp', domain: 'light', state: 'off' },
      { entity_id: 'cover.garage', friendly_name: 'Garage', domain: 'cover', state: 'closed' },
    ];
    pickerAdd('create-picker', 'lock.front_door');
    pickerAdd('create-picker', 'light.lamp');
    pickerAddAll('create-picker');
    // An admin who already unticked it keeps their choice on a re-add.
    createPicker.meta['lock.front_door'].require_local_network = false;
    pickerAdd('create-picker', 'lock.front_door');
    console.log(JSON.stringify(createPicker.meta));
    """)


async def test_picker_ticks_access_domains_when_configured(
    client, admin_session, home_network, mock_ha_client
):
    from tests.test_picker_js import node
    if node is None:
        pytest.skip("node is not installed")
    meta = await _picker_defaults(client, admin_session)
    assert meta["lock.front_door"]["require_local_network"] is False
    assert meta["cover.garage"]["require_local_network"] is True
    assert "light.lamp" not in meta


async def test_picker_ticks_nothing_without_a_home_network(
    client, admin_session, monkeypatch, mock_ha_client
):
    from tests.test_picker_js import node
    if node is None:
        pytest.skip("node is not installed")
    monkeypatch.setattr(settings, "local_network_cidrs", "")
    # The probe mutates createPicker.meta['lock.front_door'], which only exists
    # if something ticked it; with nothing configured that line would throw.
    from tests.test_picker_js import _run
    meta = await _run(client, admin_session, """
    allEntities = [
      { entity_id: 'lock.front_door', friendly_name: 'Front', domain: 'lock', state: 'locked' },
    ];
    pickerAdd('create-picker', 'lock.front_door');
    console.log(JSON.stringify(createPicker.meta));
    """)
    assert meta == {}
