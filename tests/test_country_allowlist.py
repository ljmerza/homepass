"""Per-token country allowlist: the offline GeoIP table, the guest gate, and the
admin side that validates codes against the installed database.

Same shape as the other guest suites — real routing, real DB; only ha_client
is mocked. The GeoIP database is a small CSV written per test in DB-IP's
layout, so the loader under test is the one production runs. The properties
carrying the weight:

  * the gate covers every guest endpoint the IP allowlist does, the page and
    the PIN and claim forms included;
  * it fails closed — an address with no country, or no database at all, is
    refused;
  * the home network passes without a lookup; and
  * a token without a country allowlist never touches the database.
"""
import gzip
import os
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app import database as db
from app import geoip
from app import guest_pin
from app.config import settings

GB_IP = "203.0.113.7"
US_IP = "198.51.100.7"
DE_IP6 = "2001:db8::1"
UNASSIGNED_IP = "192.0.2.7"

CSV_ROWS = [
    "ip_start,ip_end,country",  # a header, which the loader must skip
    "192.0.2.0,192.0.2.255,ZZ",
    "198.51.100.0,198.51.100.127,US",
    "198.51.100.128,198.51.100.255,US",  # adjacent, same country: folded
    "203.0.113.0,203.0.113.255,GB",
    "2001:db8::,2001:db8:ffff:ffff:ffff:ffff:ffff:ffff,DE",
]

GATED_ENDPOINTS = [
    ("GET", "/state", None),
    ("GET", "/camera/camera.hall", None),
    ("GET", "/camera/camera.hall/stream", None),
    ("POST", "/command", {"entity_id": "light.living_room", "service": "turn_on"}),
]


def _write_db(path, rows=CSV_ROWS, gz=True):
    data = "\n".join(rows) + "\n"
    if gz:
        with gzip.open(path, "wt") as fh:
            fh.write(data)
    else:
        path.write_text(data)
    return str(path)


@pytest.fixture
def geo_db(tmp_path, monkeypatch):
    path = _write_db(tmp_path / "dbip.csv.gz")
    monkeypatch.setattr(settings, "geoip_db_path", path)
    geoip.reset()
    yield path
    geoip.reset()


@pytest.fixture
def no_geo_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "geoip_db_path", str(tmp_path / "missing.csv.gz"))
    geoip.reset()
    yield
    geoip.reset()


async def _make_token(slug: str, countries: list[str] | None = ("GB",), **extra):
    return await db.create_token(
        label=f"Token {slug}",
        slug=slug,
        entity_ids=["light.living_room", "camera.hall"],
        expires_at=int(time.time()) + 3600,
        ip_allowlist=extra.pop("ip_allowlist", None),
        country_allowlist=list(countries) if countries else None,
        **extra,
    )


def _from(ip: str) -> dict:
    return {"X-Forwarded-For": ip}


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

def test_lookup(geo_db):
    table = geoip.load_table(geo_db)
    assert table.lookup(GB_IP) == "GB"
    assert table.lookup(US_IP) == "US"
    assert table.lookup("198.51.100.200") == "US"
    assert table.lookup(DE_IP6) == "DE"
    assert table.lookup("::ffff:203.0.113.7") == "GB"
    assert table.countries == {"GB", "US", "DE"}


@pytest.mark.parametrize("ip", [UNASSIGNED_IP, "10.0.0.1", "8.8.8.8", "2001:db9::1",
                                "unknown", "", "999.1.1.1", "::g"])
def test_addresses_with_no_country(geo_db, ip):
    assert geoip.load_table(geo_db).lookup(ip) is None


def test_adjacent_ranges_of_one_country_fold(geo_db):
    table = geoip.load_table(geo_db)
    assert len(table._v4.starts) == 2  # US (folded) and GB; ZZ is dropped


def test_plain_csv_and_unsorted_input(tmp_path):
    rows = [CSV_ROWS[4], CSV_ROWS[2], CSV_ROWS[5], CSV_ROWS[3]]
    table = geoip.load_table(_write_db(tmp_path / "db.csv", rows, gz=False))
    assert table.lookup(GB_IP) == "GB"
    assert table.lookup(US_IP) == "US"
    assert table.lookup(DE_IP6) == "DE"


async def test_table_is_loaded_once_and_cached(geo_db):
    first = await geoip.get_table()
    assert first is not None
    assert await geoip.get_table() is first


async def test_replaced_file_is_reloaded(geo_db, tmp_path):
    await geoip.get_table()
    _write_db(tmp_path / "dbip.csv.gz", ["203.0.113.0,203.0.113.255,IE"])
    stat = os.stat(geo_db)
    os.utime(geo_db, (stat.st_atime, stat.st_mtime + 10))
    assert await geoip.country_for(GB_IP) == "IE"


async def test_missing_database(no_geo_db):
    assert not geoip.available()
    assert await geoip.get_table() is None
    assert await geoip.country_for(GB_IP) is None
    assert await geoip.known_countries() is None


async def test_unreadable_database_is_not_reparsed_every_time(tmp_path, monkeypatch):
    bad = tmp_path / "bad.csv.gz"
    bad.write_bytes(b"not gzip at all")
    monkeypatch.setattr(settings, "geoip_db_path", str(bad))
    geoip.reset()
    calls = []
    real = geoip.load_table
    monkeypatch.setattr(geoip, "load_table", lambda p: calls.append(p) or real(p))
    assert await geoip.get_table() is None
    assert await geoip.get_table() is None
    assert len(calls) == 1
    geoip.reset()


# ---------------------------------------------------------------------------
# The guest gate
# ---------------------------------------------------------------------------

async def test_allowed_country_gets_in(client, geo_db, mock_ha_client):
    await _make_token("geo")
    page = await client.get("/g/geo", headers=_from(GB_IP))
    assert page.status_code == 200
    assert "cards-container" in page.text
    for method, suffix, body in GATED_ENDPOINTS:
        resp = await client.request(method, f"/g/geo{suffix}", json=body, headers=_from(GB_IP))
        assert resp.status_code == 200, suffix


async def test_other_country_is_refused_everywhere(client, geo_db, mock_ha_client):
    token = await _make_token("geo")
    page = await client.get("/g/geo", headers=_from(US_IP))
    assert page.status_code == 403
    assert "cards-container" not in page.text
    for method, suffix, body in GATED_ENDPOINTS:
        resp = await client.request(method, f"/g/geo{suffix}", json=body, headers=_from(US_IP))
        assert resp.status_code == 403, suffix
        assert resp.json()["detail"] == "Country not allowed"
    mock_ha_client["call_service"].assert_not_called()
    mock_ha_client["camera_snapshot"].assert_not_called()
    # A refused visit is not an access.
    assert (await db.get_token_by_id(token["id"]))["last_accessed"] is None


async def test_stream_gate_matches(geo_db, test_db):
    """httpx cannot drive a live SSE response; its gate is _validate_token."""
    from fastapi import HTTPException
    from app.routers.guest import _validate_token

    await _make_token("geo")
    from starlette.datastructures import Headers

    def req(ip):
        return SimpleNamespace(headers=Headers(_from(ip)), cookies={}, client=SimpleNamespace(host=ip))
    assert await _validate_token("geo", req(GB_IP), allow_pending=True)
    with pytest.raises(HTTPException) as exc:
        await _validate_token("geo", req(US_IP), allow_pending=True)
    assert exc.value.status_code == 403


async def test_ipv6_country(client, geo_db, mock_ha_client):
    await _make_token("geo6", countries=["DE"])
    assert (await client.get("/g/geo6/state", headers=_from(DE_IP6))).status_code == 200
    assert (await client.get("/g/geo6/state", headers=_from(GB_IP))).status_code == 403


@pytest.mark.parametrize("ip", [UNASSIGNED_IP, "10.0.0.5", "unknown"])
async def test_address_with_no_country_is_refused(client, geo_db, mock_ha_client, ip):
    await _make_token("geo")
    assert (await client.get("/g/geo/state", headers=_from(ip))).status_code == 403


async def test_home_network_passes_without_a_country(
    client, geo_db, monkeypatch, mock_ha_client
):
    monkeypatch.setattr(settings, "local_network_cidrs", "10.0.0.0/8")
    await _make_token("geo")
    assert (await client.get("/g/geo/state", headers=_from("10.0.0.5"))).status_code == 200
    assert (await client.get("/g/geo/state", headers=_from(US_IP))).status_code == 403


async def test_no_database_refuses_everyone(client, no_geo_db, mock_ha_client):
    await _make_token("geo")
    assert (await client.get("/g/geo/state", headers=_from(GB_IP))).status_code == 403
    assert (await client.get("/g/geo", headers=_from(GB_IP))).status_code == 403


async def test_token_without_countries_never_loads_the_database(
    client, geo_db, mock_ha_client
):
    await _make_token("plain", countries=None)
    assert (await client.get("/g/plain/state", headers=_from(US_IP))).status_code == 200
    assert geoip._table is None


async def test_ip_allowlist_and_countries_both_apply(client, geo_db, mock_ha_client):
    await _make_token("both", ip_allowlist=["203.0.113.0/28"])
    assert (await client.get("/g/both/state", headers=_from("203.0.113.5"))).status_code == 200
    # In GB, outside the IP allowlist.
    assert (await client.get("/g/both/state", headers=_from("203.0.113.200"))).status_code == 403


async def test_pin_form_is_gated(client, geo_db, mock_ha_client):
    await _make_token("geopin", pin_hash=await guest_pin.hash_pin("4821"))
    resp = await client.post("/g/geopin/pin", data={"pin": "4821"}, headers=_from(US_IP))
    assert resp.status_code == 403
    assert "set-cookie" not in resp.headers


async def test_claim_form_is_gated(client, geo_db, mock_ha_client):
    token = await _make_token("geobind", device_binding=True)
    resp = await client.post("/g/geobind/bind", headers=_from(US_IP))
    assert resp.status_code == 403
    assert (await db.get_token_by_id(token["id"]))["device_secret_hash"] is None


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

def _create_body(**extra) -> dict:
    return {
        "label": "Guest",
        "entity_ids": ["light.living_room"],
        "expires_in_seconds": 3600,
        **extra,
    }


async def test_create_normalises_codes(client, admin_session, geo_db, mock_ha_client):
    resp = await client.post(
        "/admin/tokens",
        json=_create_body(country_allowlist=["gb", " GB ", "us", ""]),
        cookies=admin_session,
    )
    assert resp.status_code == 201
    assert resp.json()["country_allowlist"] == ["GB", "US"]
    listing = await client.get("/admin/tokens", cookies=admin_session)
    assert listing.json()[0]["country_allowlist"] == ["GB", "US"]


async def test_create_rejects_a_code_the_database_does_not_know(
    client, admin_session, geo_db, mock_ha_client
):
    resp = await client.post(
        "/admin/tokens", json=_create_body(country_allowlist=["UK"]), cookies=admin_session
    )
    assert resp.status_code == 422
    assert "UK" in resp.json()["detail"]
    assert await db.list_tokens() == []


async def test_create_rejects_countries_without_a_database(
    client, admin_session, no_geo_db, mock_ha_client
):
    resp = await client.post(
        "/admin/tokens", json=_create_body(country_allowlist=["GB"]), cookies=admin_session
    )
    assert resp.status_code == 422
    assert "GeoIP" in resp.json()["detail"]


async def test_create_without_countries_needs_no_database(
    client, admin_session, no_geo_db, mock_ha_client
):
    for body in (_create_body(), _create_body(country_allowlist=[])):
        resp = await client.post("/admin/tokens", json=body, cookies=admin_session)
        assert resp.status_code == 201
        assert resp.json()["country_allowlist"] is None


async def test_any_country_allowlist(test_db, geo_db):
    assert not await db.any_country_allowlist()
    token = await _make_token("geo")
    assert await db.any_country_allowlist()
    await db.revoke_token(token["id"])
    assert not await db.any_country_allowlist()


async def test_dashboard_field_follows_database(
    client, admin_session, geo_db, monkeypatch, mock_ha_client
):
    resp = await client.get("/admin/dashboard", cookies=admin_session)
    assert 'id="f-countries"' in resp.text
    assert "IP Geolocation by DB-IP" in resp.text
    assert "GeoIP database not installed" not in resp.text

    monkeypatch.setattr(settings, "geoip_db_path", "/nonexistent/db.csv.gz")
    resp = await client.get("/admin/dashboard", cookies=admin_session)
    assert "GeoIP database not installed" in resp.text


def test_image_bakes_in_the_database():
    with open("Dockerfile") as fh:
        dockerfile = fh.read()
    assert "download.db-ip.com/free/dbip-country-lite-" in dockerfile
    assert "COPY --from=builder /build/geoip ./geoip" in dockerfile
    assert settings.geoip_db_path == "/app/geoip/dbip-country-lite.csv.gz"
