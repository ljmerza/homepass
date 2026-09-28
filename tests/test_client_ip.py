"""Client address resolution: X-Forwarded-For only from trusted proxies.

The properties carrying the weight:

  * a peer that is not a trusted proxy cannot choose its address by sending
    the header — the allowlists, the home-network gate and every rate limiter
    see the socket peer;
  * behind a trusted proxy the header is read right to left, so entries a
    client wrote in front of the one its proxy appended are never picked,
    including when the proxy adds its own header line;
  * the add-on trusts the hassio network by default, standalone does not, and
    setting trusted_proxies replaces that default (loopback stays trusted).
"""
import logging
import time

import httpx
import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app import client_ip as client_ip_mod
from app import database as db
from app.client_ip import client_ip
from app.config import Settings, settings

GUEST = "203.0.113.5"
SPOOF = "192.168.1.50"


@pytest.fixture(autouse=True)
def _standalone(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "")
    monkeypatch.setattr(settings, "supervisor_token", "")
    monkeypatch.setattr(client_ip_mod, "_warned_untrusted", False)


def _request(peer: str | None, *forwarded: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"x-forwarded-for", f.encode()) for f in forwarded],
        "client": (peer, 1234) if peer is not None else None,
    }
    return Request(scope)


def test_no_header_is_the_peer():
    assert client_ip(_request("198.51.100.7")) == "198.51.100.7"


def test_untrusted_peer_cannot_claim_an_address(caplog):
    with caplog.at_level(logging.WARNING, logger="app.client_ip"):
        assert client_ip(_request("198.51.100.7", SPOOF)) == "198.51.100.7"
        assert client_ip(_request("198.51.100.7", SPOOF)) == "198.51.100.7"
    warnings = [r for r in caplog.records if "not a trusted proxy" in r.getMessage()]
    assert len(warnings) == 1, "the untrusted-proxy warning is logged once, not per request"


def test_loopback_proxy_is_trusted():
    assert client_ip(_request("127.0.0.1", GUEST)) == GUEST


def test_appending_proxy_ignores_the_clients_own_entries():
    # nginx $proxy_add_x_forwarded_for: the client's header, then its real peer.
    assert client_ip(_request("127.0.0.1", f"{SPOOF}, {GUEST}")) == GUEST


def test_separate_header_lines_are_read_in_order():
    # headers.get() would return only the first line — the client's.
    assert client_ip(_request("127.0.0.1", SPOOF, GUEST)) == GUEST


def test_trusted_hops_are_skipped(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "10.0.0.0/8")
    assert client_ip(_request("10.0.0.2", f"{SPOOF}, {GUEST}, 10.0.0.9")) == GUEST


def test_a_chain_of_only_trusted_proxies_resolves_to_the_leftmost(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "10.0.0.0/8")
    assert client_ip(_request("10.0.0.2", "10.0.0.5, 10.0.0.9")) == "10.0.0.5"


def test_addon_mode_trusts_the_hassio_network_by_default(monkeypatch):
    assert client_ip(_request("172.30.33.4", GUEST)) == "172.30.33.4"
    monkeypatch.setattr(settings, "supervisor_token", "sv-token")
    assert client_ip(_request("172.30.33.4", GUEST)) == GUEST


def test_setting_trusted_proxies_replaces_the_addon_default(monkeypatch):
    monkeypatch.setattr(settings, "supervisor_token", "sv-token")
    monkeypatch.setattr(settings, "trusted_proxies", "192.168.1.10/32")
    assert client_ip(_request("172.30.33.4", GUEST)) == "172.30.33.4"
    assert client_ip(_request("192.168.1.10", GUEST)) == GUEST
    assert client_ip(_request("127.0.0.1", GUEST)) == GUEST, "loopback stays trusted"


@pytest.mark.parametrize(
    "hop, expected",
    [
        ("203.0.113.5:4711", "203.0.113.5"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("2001:db8::1", "2001:db8::1"),
        ("::ffff:203.0.113.5", "203.0.113.5"),
    ],
)
def test_ports_brackets_and_mapped_addresses(hop, expected):
    assert client_ip(_request("127.0.0.1", hop)) == expected


def test_ipv4_mapped_loopback_peer_is_trusted():
    assert client_ip(_request("::ffff:127.0.0.1", GUEST)) == GUEST


def test_unparseable_hop_is_returned_so_gates_fail_closed():
    assert client_ip(_request("127.0.0.1", "not-an-ip")) == "not-an-ip"


def test_no_peer_ignores_the_header():
    assert client_ip(_request(None, GUEST)) == "unknown"


def test_invalid_trusted_proxies_refuses_to_start():
    with pytest.raises(ValidationError, match="trusted_proxies"):
        Settings(
            ha_base_url="http://ha:8123",
            ha_token="t",
            admin_username="admin",
            admin_password="password123",
            trusted_proxies="10.0.0.0/8, 300.1.1.0/24",
        )


# -- End to end: the IP allowlist, from a peer that is not a proxy ----------

@pytest.fixture
async def remote_client(test_db, mock_ha_client):
    """Like the `client` fixture, but connecting from a non-loopback address."""
    from main import app

    transport = httpx.ASGITransport(app=app, client=("198.51.100.7", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_ip_allowlist_cannot_be_bypassed_with_the_header(remote_client):
    await db.create_token(
        label="Fenced",
        slug="fenced-ip",
        entity_ids=["light.living_room"],
        expires_at=int(time.time()) + 3600,
        ip_allowlist=["192.168.1.0/24"],
    )
    resp = await remote_client.get("/g/fenced-ip/state", headers={"X-Forwarded-For": SPOOF})
    assert resp.status_code == 403
