"""Where the separately built guest-access features meet.

Each feature has its own suite; this one holds the seams between them, which
none of those suites could see:

  * a PIN-free access link skips the keypad and nothing else — it still lands
    on the device claim or refusal, and outside a weekly window it still lands
    on the countdown, with every data endpoint refusing;
  * the device claim (POST /bind) sits behind the PIN and is refused on a link
    whose use limit is spent, like every other route;
  * the gates run in one coherent order: IP allowlist, country, PIN, device,
    schedule — so each refusal reveals only what the earlier gates allow;
  * the public API reads and writes every token field the dashboard can,
    through the dashboard's own handlers.

Real routing, real DB, real bcrypt, real rate limiter; only ha_client is mocked.
"""
import gzip
import time
from unittest.mock import patch

import pytest

from app import api_auth
from app import database as db
from app import device_binding
from app import geoip
from app import guest_pin
from app.config import settings

from tests.test_access_windows import _window_at
from tests.test_picker_js import _run, node

PIN = "4821"
KEY = "k" * 40
API = {"X-API-Key": KEY}

GB_IP = "203.0.113.7"
US_IP = "198.51.100.7"


@pytest.fixture(autouse=True)
def _reset_api_limiter():
    api_auth._api_limiter._windows.clear()


@pytest.fixture
def api_on():
    with patch.object(settings, "api_enabled", True), patch.object(settings, "api_token", KEY):
        yield


@pytest.fixture
def geo_db(tmp_path, monkeypatch):
    path = tmp_path / "dbip.csv.gz"
    with gzip.open(path, "wt") as fh:
        fh.write("198.51.100.0,198.51.100.255,US\n203.0.113.0,203.0.113.255,GB\n")
    monkeypatch.setattr(settings, "geoip_db_path", str(path))
    geoip.reset()
    yield
    geoip.reset()


async def _make_token(slug: str, **extra):
    pin = extra.pop("pin", None)
    return await db.create_token(
        label=f"Token {slug}",
        slug=slug,
        entity_ids=["light.living_room", "camera.hall"],
        expires_at=extra.pop("expires_at", int(time.time()) + 86400),
        ip_allowlist=extra.pop("ip_allowlist", None),
        pin_hash=await guest_pin.hash_pin(pin) if pin else None,
        **extra,
    )


async def _mint_code(client, admin_session, token_id: str) -> str:
    resp = await client.post(
        f"/admin/tokens/{token_id}/access-codes", json={}, cookies=admin_session
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["code"]


def _device_cookie_set(resp) -> bool:
    return any(
        raw.startswith(f"{device_binding.COOKIE}=")
        for raw in resp.headers.get_list("set-cookie")
    )


# ---------------------------------------------------------------------------
# Links without PIN meet the device lock and the schedule
# ---------------------------------------------------------------------------

async def test_access_link_on_a_device_locked_token_still_needs_the_claim(
    client, admin_session, mock_ha_client
):
    token = await _make_token("code-bound", pin=PIN, device_binding=True)
    code = await _mint_code(client, admin_session, token["id"])

    redeemed = await client.get(f"/g/code-bound?c={code}")
    assert redeemed.status_code == 303

    # Past the PIN, but not past the lock: the page is the claim screen, and
    # the data endpoints refuse until this device has claimed the link.
    page = await client.get("/g/code-bound")
    assert "Use this device?" in page.text
    state = await client.get("/g/code-bound/state")
    assert state.status_code == 403
    assert state.json()["detail"]["device"] is True

    claim = await client.post("/g/code-bound/bind")
    assert claim.status_code == 303 and _device_cookie_set(claim)
    assert (await client.get("/g/code-bound/state")).status_code == 200


async def test_access_link_on_a_device_locked_token_claimed_elsewhere_is_refused(
    client, admin_session, mock_ha_client
):
    token = await _make_token("code-taken", pin=PIN, device_binding=True)
    await db.claim_device_binding(token["id"], device_binding.hash_secret("someone-else"))
    code = await _mint_code(client, admin_session, token["id"])

    assert (await client.get(f"/g/code-taken?c={code}")).status_code == 303
    assert (await client.get("/g/code-taken")).status_code == 403
    state = await client.get("/g/code-taken/state")
    assert state.status_code == 403 and state.json()["detail"]["device"] is True
    cmd = await client.post(
        "/g/code-taken/command", json={"entity_id": "light.living_room", "service": "turn_on"}
    )
    assert cmd.status_code == 403
    mock_ha_client["call_service"].assert_not_called()


async def test_access_link_outside_a_weekly_window_lands_on_the_countdown(
    client, admin_session, mock_ha_client
):
    token = await _make_token("code-window", pin=PIN, access_windows=[_window_at(120, 60)])
    code = await _mint_code(client, admin_session, token["id"])

    assert (await client.get(f"/g/code-window?c={code}")).status_code == 303
    page = await client.get("/g/code-window")
    assert page.status_code == 200
    # The pending preview, not the live app — and nothing logged as a visit.
    assert "Not active right now" in page.text
    assert await db.list_access_logs(limit=10) == []

    state = await client.get("/g/code-window/state")
    assert state.status_code == 403
    assert state.json()["detail"]["opens_at"] is not None
    cmd = await client.post(
        "/g/code-window/command", json={"entity_id": "light.living_room", "service": "turn_on"}
    )
    assert cmd.status_code == 403
    mock_ha_client["call_service"].assert_not_called()


async def test_access_link_on_a_used_up_token_is_gone(client, admin_session, mock_ha_client):
    token = await _make_token("code-used", pin=PIN, max_uses=1)
    code = await _mint_code(client, admin_session, token["id"])
    assert await db.consume_token_use(token["id"])

    resp = await client.get(f"/g/code-used?c={code}")
    assert resp.status_code == 410
    assert guest_pin.SESSION_COOKIE not in resp.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# The device claim sits behind the PIN and the use limit
# ---------------------------------------------------------------------------

async def test_claiming_a_pin_protected_link_needs_the_pin(client, mock_ha_client):
    token = await _make_token("claim-pin", pin=PIN, device_binding=True)

    resp = await client.post("/g/claim-pin/bind")
    assert resp.status_code == 303
    assert not _device_cookie_set(resp)
    row = await db.get_token_by_id(token["id"])
    assert row["device_secret_hash"] is None


async def test_claiming_a_used_up_link_is_refused(client, mock_ha_client):
    token = await _make_token("claim-used", device_binding=True, max_uses=1)
    assert await db.consume_token_use(token["id"])

    resp = await client.post("/g/claim-used/bind")
    assert resp.status_code == 410
    row = await db.get_token_by_id(token["id"])
    assert row["device_secret_hash"] is None


# ---------------------------------------------------------------------------
# Gate order: IP allowlist → country → PIN → device → schedule
# ---------------------------------------------------------------------------

async def test_country_refusal_comes_before_the_pin(client, geo_db, mock_ha_client):
    await _make_token("order-country", pin=PIN, country_allowlist=["GB"])
    resp = await client.get("/g/order-country/state", headers={"X-Forwarded-For": US_IP})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Country not allowed"
    ok = await client.get("/g/order-country/state", headers={"X-Forwarded-For": GB_IP})
    assert ok.status_code == 401


async def test_a_second_device_learns_nothing_before_the_pin(client, mock_ha_client):
    token = await _make_token("order-device", pin=PIN, device_binding=True)
    await db.claim_device_binding(token["id"], device_binding.hash_secret("someone-else"))
    resp = await client.get("/g/order-device/state")
    assert resp.status_code == 401


async def test_the_bound_device_is_checked_before_the_schedule(client, mock_ha_client):
    token = await _make_token(
        "order-window", device_binding=True, access_windows=[_window_at(120, 60)]
    )
    await db.claim_device_binding(token["id"], device_binding.hash_secret("someone-else"))
    resp = await client.get("/g/order-window/state")
    # The device refusal, not the countdown's opens_at: a stranger's phone is
    # not told when the link next works.
    assert resp.status_code == 403
    assert resp.json()["detail"].get("device") is True
    assert "opens_at" not in resp.json()["detail"]


# ---------------------------------------------------------------------------
# The public API carries every token field
# ---------------------------------------------------------------------------

async def test_api_create_accepts_and_returns_the_new_fields(client, api_on, mock_ha_client):
    windows = [{"weekdays": [0, 2], "start": "09:00", "end": "17:00"}]
    resp = await client.post("/api/v1/tokens", headers=API, json={
        "label": "Cleaner",
        "entity_ids": ["light.living_room", "lock.front_door"],
        "expires_in_seconds": 86400,
        "access_windows": windows,
        "max_uses": 3,
        "remember_pin": False,
        "device_binding": True,
        "pin": PIN,
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["access_windows"] == windows
    assert body["max_uses"] == 3 and body["use_count"] == 0 and body["uses_remaining"] == 3
    assert body["remember_pin"] is False
    assert body["device_binding"] is True and body["device_bound_at"] is None
    assert body["country_allowlist"] is None
    assert PIN not in resp.text
    assert set(body["entity_meta"]["lock.front_door"]) >= {
        "require_proximity", "require_local_network"
    }


async def test_api_patch_sets_remember_pin_and_device_binding(client, api_on, mock_ha_client):
    token = await _make_token("api-patch", pin=PIN)
    resp = await client.patch(
        f"/api/v1/tokens/{token['id']}", headers=API,
        json={"remember_pin": False, "device_binding": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["remember_pin"] is False
    assert resp.json()["device_binding"] is True
    mock_ha_client["broadcast_device_unbound"].assert_awaited_once_with(token["id"])


async def test_api_patch_repeating_device_binding_keeps_the_claim(client, api_on, mock_ha_client):
    token = await _make_token("api-keep", device_binding=True)
    await db.claim_device_binding(token["id"], device_binding.hash_secret("guest-phone"))
    resp = await client.patch(
        f"/api/v1/tokens/{token['id']}", headers=API,
        json={"label": "Renamed", "device_binding": True},
    )
    assert resp.status_code == 200
    assert resp.json()["device_bound_at"] is not None
    mock_ha_client["broadcast_device_unbound"].assert_not_called()


async def test_api_schedule_replaces_the_timing(client, api_on, mock_ha_client):
    token = await _make_token("api-schedule")
    expires_at = int(time.time()) + 7 * 86400
    windows = [{"weekdays": [4], "start": "22:00", "end": "02:00"}]
    resp = await client.put(
        f"/api/v1/tokens/{token['id']}/schedule", headers=API,
        json={"expires_at": expires_at, "access_windows": windows, "max_uses": 2},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["expires_at"] == expires_at
    assert body["access_windows"] == windows
    assert body["max_uses"] == 2
    # The same shape GET returns, like every other API write.
    assert body["entity_ids"] == await db.get_token_entities(token["id"])
    assert body["entity_meta"] is not None
    mock_ha_client["broadcast_schedule_changed"].assert_awaited_once_with(token["id"])


async def test_api_unbind_releases_the_claim(client, api_on, mock_ha_client):
    token = await _make_token("api-unbind", device_binding=True)
    await db.claim_device_binding(token["id"], device_binding.hash_secret("old-phone"))
    resp = await client.post(f"/api/v1/tokens/{token['id']}/unbind", headers=API)
    assert resp.status_code == 200
    assert resp.json()["device_binding"] is True
    assert resp.json()["device_bound_at"] is None


async def test_api_access_codes_round_trip(client, api_on, mock_ha_client):
    token = await _make_token("api-codes", pin=PIN)
    base = f"/api/v1/tokens/{token['id']}/access-codes"

    minted = await client.post(base, headers=API, json={"label": "Fridge QR"})
    assert minted.status_code == 201, minted.text
    code = minted.json()["code"]
    assert minted.json()["label"] == "Fridge QR"

    listed = await client.get(base, headers=API)
    assert [c["id"] for c in listed.json()] == [minted.json()["id"]]
    assert code not in listed.text

    # The minted code really skips the PIN.
    assert (await client.get(f"/g/api-codes?c={code}")).status_code == 303
    assert (await client.get("/g/api-codes/state")).status_code == 200

    rotated = await client.post(f"{base}/{minted.json()['id']}/rotate", headers=API)
    assert rotated.status_code == 200 and rotated.json()["code"] != code
    # Rotation signs out the device the old link let in.
    assert (await client.get("/g/api-codes/state")).status_code == 401

    gone = await client.delete(f"{base}/{rotated.json()['id']}", headers=API)
    assert gone.status_code == 204
    assert (await client.get(base, headers=API)).json() == []


async def test_api_access_codes_need_a_pin(client, api_on, mock_ha_client):
    token = await _make_token("api-codes-nopin")
    resp = await client.post(
        f"/api/v1/tokens/{token['id']}/access-codes", headers=API, json={}
    )
    assert resp.status_code == 400


async def test_api_duplicate_carries_the_access_policy(
    client, api_on, geo_db, mock_ha_client
):
    windows = [{"weekdays": [1], "start": "08:00", "end": "12:00"}]
    token = await _make_token(
        "api-dup", pin=PIN, access_windows=windows, max_uses=2, remember_pin=False,
        device_binding=True, country_allowlist=["GB"],
    )
    await db.claim_device_binding(token["id"], device_binding.hash_secret("guest-phone"))
    assert await db.consume_token_use(token["id"])

    resp = await client.post(f"/api/v1/tokens/{token['id']}/duplicate", headers=API)
    assert resp.status_code == 201, resp.text
    copy = resp.json()
    assert copy["access_windows"] == windows
    assert copy["max_uses"] == 2 and copy["use_count"] == 0
    assert copy["remember_pin"] is False
    assert copy["device_binding"] is True and copy["device_bound_at"] is None
    assert copy["country_allowlist"] == ["GB"]
    assert copy["has_pin"] is False


async def test_api_duplicate_refuses_an_unreadable_schedule(client, api_on, mock_ha_client):
    token = await _make_token("api-dup-bad")
    conn = await db.get_db()
    await conn.execute(
        "UPDATE tokens SET access_windows = ? WHERE id = ?", ("not json", token["id"])
    )
    await conn.commit()
    resp = await client.post(f"/api/v1/tokens/{token['id']}/duplicate", headers=API)
    assert resp.status_code == 409


async def test_api_renew_gives_a_spent_link_its_uses_back(client, api_on, mock_ha_client):
    token = await _make_token("api-renew", max_uses=1)
    assert await db.consume_token_use(token["id"])
    resp = await client.post(
        f"/api/v1/tokens/{token['id']}/renew", headers=API,
        json={"expires_in_seconds": 3600},
    )
    assert resp.status_code == 200
    assert resp.json()["use_count"] == 0 and resp.json()["uses_remaining"] == 1


async def test_api_create_rejects_an_unknown_country(client, api_on, geo_db, mock_ha_client):
    resp = await client.post("/api/v1/tokens", headers=API, json={
        "label": "X", "entity_ids": ["light.living_room"], "expires_in_seconds": 60,
        "country_allowlist": ["UK"],
    })
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Entity suggestions follow the home-network defaults
# ---------------------------------------------------------------------------

@pytest.mark.skipif(node is None, reason="node is not installed")
async def test_suggested_locks_are_home_network_only_like_hand_picked_ones(
    client, admin_session, mock_ha_client, monkeypatch
):
    monkeypatch.setattr(settings, "local_network_cidrs", "192.168.1.0/24")
    out = await _run(client, admin_session, """
    allEntities = [
      { entity_id: 'lock.front', friendly_name: 'Front', domain: 'lock', state: 'locked', labels: [] },
      { entity_id: 'light.hall', friendly_name: 'Hall', domain: 'light', state: 'off', labels: [] },
    ];
    showToast = () => {};
    globalThis.fetch = async () => ({ ok: true, json: async () => [
      { entity_id: 'lock.front', category: 'access' },
      { entity_id: 'light.hall', category: 'lights' },
    ] });
    await suggestInto('create-picker', 'access');
    console.log(JSON.stringify({ meta: createPicker.meta }));
    """)
    assert out["meta"]["lock.front"]["require_local_network"] is True
    assert "light.hall" not in out["meta"]
