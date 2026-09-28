"""The home-network checks cannot be earned with a forged X-Forwarded-For.

The per-entity home-network gate and the country allowlist's home-network
exemption both widen what a request may do, so a client writing a LAN address
into X-Forwarded-For must not earn either. These tests drive the app with a
real socket peer (ASGITransport's `client`), which the shared `client` fixture
pins to loopback, so the forged-header cases are the ones production sees:

  * nothing in front of HomePass — the peer is the attacker's public address;
  * a proxy that appends to the header — the public address follows the forgery;
  * a proxy that adds its own header line beside the forged one.

Public peers are 8.8.8.8 and 1.1.1.1 because every documentation range
(192.0.2/24, 198.51.100/24, 203.0.113/24) counts as private to ipaddress.
"""
import time

import httpx
import pytest
import pytest_asyncio

from app import database as db
from app import geoip
from app import local_network
from app.config import settings
from tests.test_country_allowlist import _write_db

LOCK = "lock.front_door"
HOME = "192.168.1.0/24"
LAN_GUEST = "192.168.1.50"
PUBLIC = "8.8.8.8"
UNLOCK = {"entity_id": LOCK, "service": "lock.unlock"}


@pytest.fixture
def home_network(monkeypatch):
    monkeypatch.setattr(settings, "local_network_cidrs", HOME)


@pytest.fixture
def geo_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "geoip_db_path", _write_db(tmp_path / "dbip.csv.gz"))
    geoip.reset()
    yield
    geoip.reset()


def _client_from(peer: str) -> httpx.AsyncClient:
    from main import app

    transport = httpx.ASGITransport(app=app, client=(peer, 40000))
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest_asyncio.fixture
async def gated_token(test_db):
    return await db.create_token(
        label="Front door",
        slug="chain-gated",
        entity_ids=[LOCK],
        expires_at=int(time.time()) + 3600,
        ip_allowlist=None,
        entity_meta={LOCK: {"require_local_network": True}},
    )


async def _unlock(peer: str, headers=None) -> httpx.Response:
    async with _client_from(peer) as c:
        return await c.post("/g/chain-gated/command", json=UNLOCK, headers=headers or [])


# ---------------------------------------------------------------------------
# The home-network gate
# ---------------------------------------------------------------------------

async def test_a_forged_header_from_a_public_peer_is_refused(
    gated_token, home_network, mock_ha_client
):
    resp = await _unlock(PUBLIC, {"X-Forwarded-For": LAN_GUEST})
    assert resp.status_code == 403
    assert "home network" in resp.json()["detail"]
    mock_ha_client["call_service"].assert_not_called()


async def test_a_forgery_an_appending_proxy_passed_on_is_refused(
    gated_token, home_network, mock_ha_client
):
    # What nginx's $proxy_add_x_forwarded_for sends on: the client's header,
    # then the address nginx actually saw.
    resp = await _unlock("127.0.0.1", {"X-Forwarded-For": f"{LAN_GUEST}, {PUBLIC}"})
    assert resp.status_code == 403
    mock_ha_client["call_service"].assert_not_called()


async def test_a_forgery_beside_the_proxys_own_header_line_is_refused(
    gated_token, home_network, mock_ha_client
):
    headers = [("X-Forwarded-For", LAN_GUEST), ("X-Forwarded-For", PUBLIC)]
    resp = await _unlock("127.0.0.1", headers)
    assert resp.status_code == 403
    mock_ha_client["call_service"].assert_not_called()


async def test_a_lan_device_with_nothing_in_front_is_let_through(
    gated_token, home_network, mock_ha_client
):
    resp = await _unlock(LAN_GUEST)
    assert resp.status_code == 200
    mock_ha_client["call_service"].assert_called_once()


async def test_a_lan_device_behind_a_trusted_lan_proxy_is_let_through(
    gated_token, home_network, mock_ha_client, monkeypatch
):
    monkeypatch.setattr(settings, "trusted_proxies", "172.17.0.0/16")
    resp = await _unlock("172.17.0.1", {"X-Forwarded-For": LAN_GUEST})
    assert resp.status_code == 200
    mock_ha_client["call_service"].assert_called_once()


# ---------------------------------------------------------------------------
# The country allowlist's home-network exemption
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def gb_token(test_db):
    return await db.create_token(
        label="GB only",
        slug="chain-gb",
        entity_ids=["light.living_room"],
        expires_at=int(time.time()) + 3600,
        ip_allowlist=None,
        country_allowlist=["GB"],
    )


async def test_a_forged_lan_address_does_not_skip_the_country_check(
    gb_token, home_network, geo_db, mock_ha_client
):
    async with _client_from(PUBLIC) as c:
        resp = await c.get("/g/chain-gb/state", headers={"X-Forwarded-For": LAN_GUEST})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Country not allowed"
    mock_ha_client["get_states"].assert_not_called()


async def test_an_untrusted_lan_proxy_is_the_client(
    gated_token, home_network, mock_ha_client
):
    # Not in trusted_proxies: its header is ignored and the proxy itself,
    # outside the home range, is the client.
    resp = await _unlock("172.17.0.1", {"X-Forwarded-For": LAN_GUEST})
    assert resp.status_code == 403
    mock_ha_client["call_service"].assert_not_called()


async def test_a_real_lan_device_still_skips_the_country_check(
    gb_token, home_network, geo_db, mock_ha_client
):
    async with _client_from(LAN_GUEST) as c:
        resp = await c.get("/g/chain-gb/state")
    assert resp.status_code == 200
