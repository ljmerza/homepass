"""Single-device binding: the claim, the gate on every guest endpoint, and the
admin side that turns it on, resets it and reports it.

Same shape as test_pin_protection.py — real routing, real DB, real cookies;
only ha_client is mocked. The properties carrying the weight:

  * merely fetching the link never claims it — a chat app's preview fetcher
    (the bug that burned the fork this comes from) gets the claim screen and
    leaves the link unclaimed;
  * once claimed, every guest endpoint the PIN gate covers refuses any other
    device, not just the HTML page; and
  * a token with binding off behaves exactly as before.
"""
import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app import database as db
from app import device_binding
from app import guest_pin
from app import ha_client

PIN = "4821"
WHATSAPP_UA = "WhatsApp/2.23.20.0 A"

# Every guest endpoint that reads state or performs an action — the same list
# the PIN gate is held to.
GATED_ENDPOINTS = [
    ("GET", "/state", None),
    ("GET", "/stream", None),
    ("GET", "/camera/camera.hall", None),
    ("GET", "/camera/camera.hall/stream", None),
    ("POST", "/command", {"entity_id": "light.living_room", "service": "turn_on"}),
]

# The ones that answer promptly over httpx's ASGI transport once the gate
# passes. /stream never returns there, so its open side is checked at
# _validate_token instead.
OPEN_ENDPOINTS = [e for e in GATED_ENDPOINTS if e[1] != "/stream"]


async def _make_token(
    slug: str,
    binding: bool = True,
    pin: str | None = None,
    starts_at: int | None = None,
    ip_allowlist: list[str] | None = None,
):
    return await db.create_token(
        label=f"Secret Label {slug}",
        slug=slug,
        entity_ids=["light.living_room", "camera.hall"],
        expires_at=int(time.time()) + 3600,
        ip_allowlist=ip_allowlist,
        pin_hash=await guest_pin.hash_pin(pin) if pin else None,
        starts_at=starts_at,
        device_binding=binding,
    )


def _device_cookie(resp) -> str | None:
    """The device cookie a response set, read from the header, or None."""
    for raw in resp.headers.get_list("set-cookie"):
        if raw.startswith(f"{device_binding.COOKIE}="):
            return raw
    return None


async def _claim(client, slug: str) -> str:
    """Claim a link from `client` and return the raw Set-Cookie header."""
    resp = await client.post(f"/g/{slug}/bind")
    assert resp.status_code == 303
    raw = _device_cookie(resp)
    assert raw, "claim set no device cookie"
    return raw


@pytest_asyncio.fixture
async def other_device(test_db, mock_ha_client):
    """A second browser: same app, its own cookie jar."""
    from main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture
async def bound_token(test_db):
    return await _make_token("bound")


@pytest_asyncio.fixture
async def open_token(test_db):
    return await _make_token("open", binding=False)


async def _log_types(token_id: str) -> list[str]:
    conn = await db.get_db()
    async with conn.execute(
        "SELECT event_type FROM access_log WHERE token_id = ? ORDER BY id", (token_id,)
    ) as cur:
        return [r["event_type"] for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Regression: binding off is the default and changes nothing
# ---------------------------------------------------------------------------

async def test_binding_is_off_by_default(test_db):
    row = await db.create_token(
        label="Plain", slug="plain", entity_ids=["light.a"],
        expires_at=int(time.time()) + 3600, ip_allowlist=None,
    )
    assert row["device_binding"] == 0
    assert row["device_secret_hash"] is None


async def test_unbound_token_is_unaffected(client, open_token, mock_ha_client):
    page = await client.get("/g/open")
    assert page.status_code == 200
    assert "cards-container" in page.text
    assert _device_cookie(page) is None

    for method, suffix, body in OPEN_ENDPOINTS:
        resp = await client.request(method, f"/g/open{suffix}", json=body)
        assert resp.status_code == 200, suffix
    mock_ha_client["call_service"].assert_called_once()


async def test_bind_on_unbound_token_claims_nothing(client, open_token, mock_ha_client):
    resp = await client.post("/g/open/bind")
    assert resp.status_code == 303
    assert _device_cookie(resp) is None
    assert (await db.get_token_by_id(open_token["id"]))["device_secret_hash"] is None


# ---------------------------------------------------------------------------
# Unclaimed: fetching never claims, and nothing is reachable yet
# ---------------------------------------------------------------------------

async def test_page_load_never_claims_the_binding(client, bound_token, mock_ha_client):
    """The fork's production bug: WhatsApp's preview fetcher GETs the link seconds
    after it is sent. That GET must leave the link unclaimed."""
    resp = await client.get("/g/bound", headers={"User-Agent": WHATSAPP_UA})
    assert resp.status_code == 200
    assert "Use this device" in resp.text
    assert _device_cookie(resp) is None

    row = await db.get_token_by_id(bound_token["id"])
    assert row["device_secret_hash"] is None
    assert row["device_bound_at"] is None
    # An unanswered claim screen is not an access.
    assert row["last_accessed"] is None
    assert await _log_types(bound_token["id"]) == []


async def test_claim_screen_names_nothing_about_the_home(client, bound_token, mock_ha_client):
    """It is the page a chat app builds its preview card from."""
    resp = await client.get("/g/bound")
    assert "Secret Label" not in resp.text
    assert "light.living_room" not in resp.text
    assert "camera.hall" not in resp.text
    assert "noindex" in resp.text


@pytest.mark.parametrize("method,suffix,body", GATED_ENDPOINTS)
async def test_unclaimed_token_refuses_every_endpoint(
    client, bound_token, mock_ha_client, method, suffix, body
):
    """Unclaimed is closed, not open: a script holding the slug cannot read state
    without committing to being the one device."""
    resp = await client.request(method, f"/g/bound{suffix}", json=body)
    assert resp.status_code == 403
    assert resp.json()["detail"]["device"] is True
    mock_ha_client["call_service"].assert_not_called()
    mock_ha_client["camera_snapshot"].assert_not_called()


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------

async def test_claim_sets_a_scoped_httponly_lax_cookie(client, bound_token, mock_ha_client):
    raw = (await _claim(client, "bound")).lower()
    assert "httponly" in raw
    assert "samesite=lax" in raw
    assert "path=/g/bound" in raw
    assert "max-age=" in raw


async def test_claim_cookie_is_secure_over_https(client, bound_token, mock_ha_client):
    resp = await client.post("/g/bound/bind", headers={"x-forwarded-proto": "https"})
    assert "secure" in _device_cookie(resp).lower()


async def test_claim_stores_only_a_hash(client, bound_token, mock_ha_client):
    raw = await _claim(client, "bound")
    secret = raw.split(";")[0].split("=", 1)[1]
    row = await db.get_token_by_id(bound_token["id"])
    assert row["device_secret_hash"] == device_binding.hash_secret(secret)
    assert secret not in row["device_secret_hash"]
    assert row["device_bound_at"] is not None


async def test_claim_is_logged(client, bound_token, mock_ha_client):
    await _claim(client, "bound")
    assert await _log_types(bound_token["id"]) == ["device_bound"]


async def test_claimed_device_reaches_everything(client, bound_token, mock_ha_client):
    await _claim(client, "bound")

    page = await client.get("/g/bound")
    assert page.status_code == 200
    assert "cards-container" in page.text

    for method, suffix, body in OPEN_ENDPOINTS:
        resp = await client.request(method, f"/g/bound{suffix}", json=body)
        assert resp.status_code == 200, suffix
    mock_ha_client["call_service"].assert_called_once()


async def test_page_reissues_the_cookie(client, bound_token, mock_ha_client):
    """Restarts the browser's 400-day clock on every visit."""
    first = await _claim(client, "bound")
    page = await client.get("/g/bound")
    again = _device_cookie(page)
    assert again is not None
    assert again.split(";")[0] == first.split(";")[0]


async def test_second_tap_on_the_same_device_is_harmless(client, bound_token, mock_ha_client):
    await _claim(client, "bound")
    before = (await db.get_token_by_id(bound_token["id"]))["device_secret_hash"]
    resp = await client.post("/g/bound/bind")
    assert resp.status_code == 303
    assert (await db.get_token_by_id(bound_token["id"]))["device_secret_hash"] == before


async def test_claim_race_has_one_winner(test_db, bound_token):
    first = await db.claim_device_binding(bound_token["id"], "a" * 64)
    second = await db.claim_device_binding(bound_token["id"], "b" * 64)
    assert first is True
    assert second is False
    assert (await db.get_token_by_id(bound_token["id"]))["device_secret_hash"] == "a" * 64


async def test_claim_on_unknown_slug_is_expired(client, test_db, mock_ha_client):
    resp = await client.post("/g/nope/bind")
    assert resp.status_code == 410


async def test_claim_on_revoked_token_is_expired(client, bound_token, mock_ha_client):
    await db.revoke_token(bound_token["id"])
    resp = await client.post("/g/bound/bind")
    assert resp.status_code == 410
    assert (await db.get_token_by_id(bound_token["id"]))["device_secret_hash"] is None


async def test_claim_respects_the_ip_allowlist(client, test_db, mock_ha_client):
    token = await _make_token("ipbound", ip_allowlist=["10.0.0.0/8"])
    resp = await client.post("/g/ipbound/bind", headers={"X-Forwarded-For": "8.8.8.8"})
    assert resp.status_code == 403
    assert (await db.get_token_by_id(token["id"]))["device_secret_hash"] is None


# ---------------------------------------------------------------------------
# The other device
# ---------------------------------------------------------------------------

async def test_other_device_gets_the_refusal_page(
    client, other_device, bound_token, mock_ha_client
):
    await _claim(client, "bound")
    resp = await other_device.get("/g/bound")
    assert resp.status_code == 403
    assert "another device" in resp.text
    # Not the expired page: the link is fine, only the device is wrong.
    assert "Access Expired" not in resp.text
    assert "cards-container" not in resp.text


async def test_refusal_is_logged(client, other_device, bound_token, mock_ha_client):
    await _claim(client, "bound")
    await other_device.get("/g/bound", headers={"User-Agent": "Stranger/1.0"})
    assert await _log_types(bound_token["id"]) == ["device_bound", "device_refused"]


@pytest.mark.parametrize("method,suffix,body", GATED_ENDPOINTS)
async def test_other_device_is_refused_on_every_endpoint(
    client, other_device, bound_token, mock_ha_client, method, suffix, body
):
    await _claim(client, "bound")
    resp = await other_device.request(method, f"/g/bound{suffix}", json=body)
    assert resp.status_code == 403
    assert resp.json()["detail"]["device"] is True
    mock_ha_client["call_service"].assert_not_called()
    mock_ha_client["camera_snapshot"].assert_not_called()


async def test_other_device_cannot_claim_over_the_first(
    client, other_device, bound_token, mock_ha_client
):
    await _claim(client, "bound")
    before = (await db.get_token_by_id(bound_token["id"]))["device_secret_hash"]
    resp = await other_device.post("/g/bound/bind")
    assert resp.status_code == 403
    assert _device_cookie(resp) is None
    assert (await db.get_token_by_id(bound_token["id"]))["device_secret_hash"] == before


async def test_forged_cookie_is_refused(client, bound_token, mock_ha_client):
    await _claim(client, "bound")
    resp = await client.get(
        "/g/bound/state",
        headers={"Cookie": f"{device_binding.COOKIE}={device_binding.new_secret()}"},
    )
    assert resp.status_code == 403


async def test_cookie_for_one_token_does_not_open_another(client, test_db, mock_ha_client):
    await _make_token("first")
    await _make_token("second")
    raw = await _claim(client, "first")
    await _claim(client, "second")
    # Replay the first token's secret against the second, bypassing the path.
    secret = raw.split(";")[0].split("=", 1)[1]
    client.cookies.clear()
    resp = await client.get(
        "/g/second/state", headers={"Cookie": f"{device_binding.COOKIE}={secret}"}
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Interplay with the PIN, the schedule and the stream
# ---------------------------------------------------------------------------

async def test_pin_comes_before_the_claim(client, test_db, mock_ha_client):
    token = await _make_token("pinbound", pin=PIN)

    page = await client.get("/g/pinbound")
    assert "Enter PIN" in page.text

    # A claim without the PIN goes back to the PIN screen and claims nothing.
    resp = await client.post("/g/pinbound/bind")
    assert resp.status_code == 303
    assert (await db.get_token_by_id(token["id"]))["device_secret_hash"] is None

    assert (await client.post("/g/pinbound/pin", data={"pin": PIN})).status_code == 303
    assert "Use this device" in (await client.get("/g/pinbound")).text
    await _claim(client, "pinbound")
    assert (await client.get("/g/pinbound/state")).status_code == 200


async def test_pin_is_asked_before_revealing_a_claim(
    client, other_device, test_db, mock_ha_client
):
    """A second phone without the PIN learns nothing, not even that the link is taken."""
    await _make_token("pinbound", pin=PIN)
    await client.post("/g/pinbound/pin", data={"pin": PIN})
    await _claim(client, "pinbound")

    resp = await other_device.get("/g/pinbound/state")
    assert resp.status_code == 401
    assert "Enter PIN" in (await other_device.get("/g/pinbound")).text


async def test_pending_link_can_be_claimed_but_preview_needs_the_device(
    client, other_device, test_db, mock_ha_client
):
    await _make_token("early", starts_at=int(time.time()) + 3600)

    # Unclaimed: the claim screen, never the preview that names every entity.
    page = await client.get("/g/early")
    assert "Use this device" in page.text
    assert "light.living_room" not in page.text

    await _claim(client, "early")
    page = await client.get("/g/early")
    assert page.status_code == 200
    assert "light.living_room" in page.text  # the pending preview

    refused = await other_device.get("/g/early")
    assert refused.status_code == 403


async def test_stream_gate_matches_the_other_endpoints(test_db, bound_token):
    """httpx cannot drive a live SSE response, so the stream's gate — which is
    _validate_token, the same one every other route uses — is checked there."""
    from fastapi import HTTPException
    from app.routers.guest import _validate_token

    stranger = SimpleNamespace(headers={}, cookies={}, client=SimpleNamespace(host="1.2.3.4"))
    with pytest.raises(HTTPException) as exc:
        await _validate_token("bound", stranger, allow_pending=True)
    assert exc.value.status_code == 403

    secret = device_binding.new_secret()
    assert await db.claim_device_binding(bound_token["id"], device_binding.hash_secret(secret))
    owner = SimpleNamespace(
        headers={}, cookies={device_binding.COOKIE: secret},
        client=SimpleNamespace(host="1.2.3.4"),
    )
    assert await _validate_token("bound", owner, allow_pending=True)


def _fake_request():
    async def _is_disconnected():
        return False
    return SimpleNamespace(is_disconnected=_is_disconnected)


async def test_stream_forwards_device_unbound_and_hangs_up(test_db, bound_token):
    from app.routers.guest import _event_generator

    gen = _event_generator(bound_token["id"], "bound", _fake_request())
    assert "event: connected" in await asyncio.wait_for(gen.__anext__(), timeout=2)

    task = asyncio.create_task(asyncio.wait_for(gen.__anext__(), timeout=2))
    await asyncio.sleep(0.05)
    await ha_client.broadcast_device_unbound(bound_token["id"])
    assert "event: device_unbound" in await task
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(gen.__anext__(), timeout=2)


async def test_pending_stream_forwards_device_unbound(test_db, bound_token):
    from app.routers.guest import _event_generator

    gen = _event_generator(
        bound_token["id"], "bound", _fake_request(), int(time.time()) + 3600
    )
    await asyncio.wait_for(gen.__anext__(), timeout=2)
    task = asyncio.create_task(asyncio.wait_for(gen.__anext__(), timeout=2))
    await asyncio.sleep(0.05)
    await ha_client.broadcast_device_unbound(bound_token["id"])
    assert "event: device_unbound" in await task
    await gen.aclose()


async def test_ingress_scopes_the_cookie_under_the_prefix(
    client, bound_token, mock_ha_client, monkeypatch
):
    monkeypatch.setattr("app.ingress._SUPERVISOR_TOKEN", "sv")
    resp = await client.post(
        "/g/bound/bind", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"}
    )
    assert resp.headers["location"] == "/api/hassio_ingress/abc/g/bound"
    assert "path=/api/hassio_ingress/abc/g/bound" in _device_cookie(resp).lower()


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


async def test_create_with_binding(client, admin_session, mock_ha_client):
    resp = await client.post(
        "/admin/tokens", json=_create_body(device_binding=True), cookies=admin_session
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["device_binding"] is True
    assert data["device_bound_at"] is None


async def test_create_defaults_to_no_binding(client, admin_session, mock_ha_client):
    resp = await client.post("/admin/tokens", json=_create_body(), cookies=admin_session)
    assert resp.json()["device_binding"] is False


async def test_admin_sees_claim_time_but_never_the_hash(
    client, admin_session, bound_token, mock_ha_client
):
    await _claim(client, "bound")
    row = await db.get_token_by_id(bound_token["id"])
    listing = await client.get("/admin/tokens", cookies=admin_session)
    (token,) = [t for t in listing.json() if t["id"] == bound_token["id"]]
    assert token["device_bound_at"] == row["device_bound_at"]
    assert row["device_secret_hash"] not in listing.text
    detail = await client.get(f"/admin/tokens/{bound_token['id']}", cookies=admin_session)
    assert row["device_secret_hash"] not in detail.text


async def test_unbind_lets_the_next_device_claim(
    client, other_device, admin_session, bound_token, mock_ha_client
):
    await _claim(client, "bound")
    resp = await client.post(f"/admin/tokens/{bound_token['id']}/unbind", cookies=admin_session)
    assert resp.status_code == 200
    assert resp.json()["device_bound_at"] is None
    assert resp.json()["device_binding"] is True
    mock_ha_client["broadcast_device_unbound"].assert_awaited_once_with(bound_token["id"])

    # The old device's cookie no longer matches anything.
    assert (await client.get("/g/bound/state")).status_code == 403
    # And a new device can take it.
    await _claim(other_device, "bound")
    assert (await other_device.get("/g/bound/state")).status_code == 200


async def test_unbind_requires_binding(client, admin_session, open_token, mock_ha_client):
    resp = await client.post(f"/admin/tokens/{open_token['id']}/unbind", cookies=admin_session)
    assert resp.status_code == 400


async def test_unbind_requires_admin(client, bound_token, mock_ha_client):
    resp = await client.post(f"/admin/tokens/{bound_token['id']}/unbind")
    assert resp.status_code == 401


async def test_unbind_unknown_token_is_404(client, admin_session, test_db, mock_ha_client):
    resp = await client.post("/admin/tokens/nope/unbind", cookies=admin_session)
    assert resp.status_code == 404


async def test_toggle_binding_on_and_off(
    client, admin_session, open_token, mock_ha_client
):
    url = f"/admin/tokens/{open_token['id']}/device-binding"
    resp = await client.patch(url, json={"enabled": True}, cookies=admin_session)
    assert resp.status_code == 200
    assert resp.json()["device_binding"] is True
    # A link already in use is closed until someone claims it.
    assert (await client.get("/g/open/state")).status_code == 403
    await _claim(client, "open")
    assert (await client.get("/g/open/state")).status_code == 200

    resp = await client.patch(url, json={"enabled": False}, cookies=admin_session)
    assert resp.json()["device_binding"] is False
    row = await db.get_token_by_id(open_token["id"])
    assert row["device_secret_hash"] is None and row["device_bound_at"] is None
    assert mock_ha_client["broadcast_device_unbound"].await_count == 2

    client.cookies.clear()
    assert (await client.get("/g/open/state")).status_code == 200


async def test_re_enabling_starts_unclaimed(client, admin_session, bound_token, mock_ha_client):
    await _claim(client, "bound")
    await client.patch(
        f"/admin/tokens/{bound_token['id']}/device-binding",
        json={"enabled": True}, cookies=admin_session,
    )
    assert (await db.get_token_by_id(bound_token["id"]))["device_secret_hash"] is None


async def test_toggle_requires_admin(client, bound_token, mock_ha_client):
    resp = await client.patch(
        f"/admin/tokens/{bound_token['id']}/device-binding", json={"enabled": False}
    )
    assert resp.status_code == 401


async def test_rotation_releases_the_claim(client, admin_session, bound_token, mock_ha_client):
    await _claim(client, "bound")
    resp = await client.post(
        f"/admin/tokens/{bound_token['id']}/rotate-slug", cookies=admin_session
    )
    new_slug = resp.json()["slug"]
    assert resp.json()["device_binding"] is True
    assert resp.json()["device_bound_at"] is None
    assert "Use this device" in (await client.get(f"/g/{new_slug}")).text


async def test_renewing_keeps_the_claim(client, admin_session, bound_token, mock_ha_client):
    await _claim(client, "bound")
    await client.patch(
        f"/admin/tokens/{bound_token['id']}/expiry",
        json={"expires_in_seconds": 86400}, cookies=admin_session,
    )
    assert (await client.get("/g/bound/state")).status_code == 200


async def test_dashboard_offers_the_controls(client, admin_session, mock_ha_client):
    resp = await client.get("/admin/dashboard", cookies=admin_session)
    assert 'id="f-device-binding"' in resp.text
    assert "confirm-unbind" in resp.text
    assert "device-binding" in resp.text
