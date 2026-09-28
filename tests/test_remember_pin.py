"""Per-token remember-PIN: whether one correct entry outlives the browser.

On — the default, and how every token behaved before the setting existed — the
PIN session is a persistent cookie. Off, it is a browser-session cookie with no
Max-Age, so the guest is asked again on their next visit. Either way the
server-side expiry signed into the cookie is unchanged.

Real routing, real DB, real bcrypt; only ha_client is mocked.
"""
import time

import pytest_asyncio

from app import database as db
from app import guest_pin

PIN = "4821"


async def _make_token(slug: str, remember_pin: bool = True, pin: str | None = PIN):
    return await db.create_token(
        label=f"Token {slug}",
        slug=slug,
        entity_ids=["light.living_room"],
        expires_at=int(time.time()) + 3600,
        ip_allowlist=None,
        pin_hash=await guest_pin.hash_pin(pin) if pin else None,
        remember_pin=remember_pin,
    )


def _cookie_header(value: str) -> dict:
    return {"Cookie": f"{guest_pin.SESSION_COOKIE}={value}"}


def _session_value(resp) -> str:
    return resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]


@pytest_asyncio.fixture
async def remembered(test_db):
    return await _make_token("remembered")


@pytest_asyncio.fixture
async def forgetful(test_db):
    return await _make_token("forgetful", remember_pin=False)


# ---------------------------------------------------------------------------
# Defaults and storage
# ---------------------------------------------------------------------------

async def test_existing_behaviour_is_the_default(test_db):
    """A token created without the argument remembers, like every token did."""
    token = await db.create_token(
        label="Legacy", slug="legacy", entity_ids=["light.a"],
        expires_at=int(time.time()) + 3600, ip_allowlist=None,
    )
    assert token["remember_pin"] == 1


async def test_create_token_defaults_to_remember(client, admin_session, mock_ha_client):
    resp = await client.post(
        "/admin/tokens",
        json={"label": "G", "entity_ids": ["light.a"], "expires_in_seconds": 3600, "pin": PIN},
        cookies=admin_session,
    )
    assert resp.status_code == 201
    assert resp.json()["remember_pin"] is True


async def test_create_token_can_turn_it_off(client, admin_session, mock_ha_client):
    resp = await client.post(
        "/admin/tokens",
        json={
            "label": "G", "entity_ids": ["light.a"], "expires_in_seconds": 3600,
            "pin": PIN, "remember_pin": False,
        },
        cookies=admin_session,
    )
    assert resp.status_code == 201
    assert resp.json()["remember_pin"] is False
    row = await db.get_token_by_id(resp.json()["id"])
    assert row["remember_pin"] == 0


async def test_token_list_reports_remember_pin(client, admin_session, remembered, forgetful):
    resp = await client.get("/admin/tokens", cookies=admin_session)
    by_slug = {t["slug"]: t for t in resp.json()}
    assert by_slug["remembered"]["remember_pin"] is True
    assert by_slug["forgetful"]["remember_pin"] is False


# ---------------------------------------------------------------------------
# The cookie
# ---------------------------------------------------------------------------

async def test_remembered_session_is_persistent(client, remembered, mock_ha_client):
    resp = await client.post("/g/remembered/pin", data={"pin": PIN})
    assert resp.status_code == 303
    raw = resp.headers["set-cookie"].lower()
    assert "max-age=" in raw


async def test_forgetful_session_is_a_browser_session_cookie(client, forgetful, mock_ha_client):
    resp = await client.post("/g/forgetful/pin", data={"pin": PIN})
    assert resp.status_code == 303
    raw = resp.headers["set-cookie"].lower()
    # Neither attribute: either would make the browser keep it past closing.
    assert "max-age=" not in raw
    assert "expires=" not in raw
    # Everything else about the cookie is unchanged.
    assert "httponly" in raw
    assert "samesite=lax" in raw
    assert "path=/g/forgetful" in raw


async def test_forgetful_session_still_works_while_the_browser_is_open(
    client, forgetful, mock_ha_client
):
    await client.post("/g/forgetful/pin", data={"pin": PIN})
    assert (await client.get("/g/forgetful/state")).status_code == 200
    page = await client.get("/g/forgetful")
    assert "Enter PIN" not in page.text
    cmd = await client.post(
        "/g/forgetful/command",
        json={"entity_id": "light.living_room", "service": "turn_on"},
    )
    assert cmd.status_code == 200


async def test_forgetful_session_keeps_the_server_side_expiry(client, forgetful, mock_ha_client):
    """A browser that restores session cookies on relaunch gets no more than a
    remembered session would: the signed expiry is still 24 hours at most."""
    resp = await client.post("/g/forgetful/pin", data={"pin": PIN})
    claimed = int(_session_value(resp).split(".")[1])
    assert claimed <= int(time.time()) + guest_pin.SESSION_TTL_SECONDS


# ---------------------------------------------------------------------------
# Changing the setting
# ---------------------------------------------------------------------------

async def test_turning_it_off_signs_out_remembered_sessions(
    client, admin_session, remembered, mock_ha_client
):
    """Otherwise a guest told they would be asked every visit coasts on the
    cookie they already had for up to a day."""
    session = _session_value(await client.post("/g/remembered/pin", data={"pin": PIN}))
    assert (await client.get("/g/remembered/state", headers=_cookie_header(session))).status_code == 200

    resp = await client.patch(
        f"/admin/tokens/{remembered['id']}/remember-pin",
        json={"remember_pin": False},
        cookies=admin_session,
    )
    assert resp.status_code == 200
    assert resp.json() == {"remember_pin": False}

    resp = await client.get("/g/remembered/state", headers=_cookie_header(session))
    assert resp.status_code == 401


async def test_turning_it_on_keeps_current_sessions(
    client, admin_session, forgetful, mock_ha_client
):
    """No reason to sign anyone out: their cookie dies with the browser anyway."""
    session = _session_value(await client.post("/g/forgetful/pin", data={"pin": PIN}))

    await client.patch(
        f"/admin/tokens/{forgetful['id']}/remember-pin",
        json={"remember_pin": True},
        cookies=admin_session,
    )
    resp = await client.get("/g/forgetful/state", headers=_cookie_header(session))
    assert resp.status_code == 200


async def test_session_from_before_the_setting_still_verifies(remembered):
    """The original three-part, suffix-free cookie is what a remembered PIN
    session still is, so no one is signed out by the upgrade."""
    row = await db.get_token_by_id(remembered["id"])
    value, max_age = guest_pin.issue_session(row["id"], row["pin_hash"], row["expires_at"])
    assert len(value.split(".")) == 3
    assert max_age and max_age > 0
    assert guest_pin.verify_session(value, row["id"], row["pin_hash"])


async def test_with_it_off_only_a_forgetful_session_verifies(forgetful):
    row = await db.get_token_by_id(forgetful["id"])
    remembered_value, _ = guest_pin.issue_session(
        row["id"], row["pin_hash"], row["expires_at"], remember=True
    )
    once_value, max_age = guest_pin.issue_session(
        row["id"], row["pin_hash"], row["expires_at"], remember=False
    )
    assert max_age is None
    assert not guest_pin.verify_session(remembered_value, row["id"], row["pin_hash"], remember=False)
    assert guest_pin.verify_session(once_value, row["id"], row["pin_hash"], remember=False)


async def test_setting_it_does_not_touch_the_pin(client, admin_session, remembered, mock_ha_client):
    before = (await db.get_token_by_id(remembered["id"]))["pin_hash"]
    await client.patch(
        f"/admin/tokens/{remembered['id']}/remember-pin",
        json={"remember_pin": False},
        cookies=admin_session,
    )
    assert (await db.get_token_by_id(remembered["id"]))["pin_hash"] == before


async def test_it_can_be_set_before_a_pin_exists(client, admin_session, test_db, mock_ha_client):
    token = await _make_token("no-pin-yet", pin=None)
    resp = await client.patch(
        f"/admin/tokens/{token['id']}/remember-pin",
        json={"remember_pin": False},
        cookies=admin_session,
    )
    assert resp.status_code == 200
    # Kept for when a PIN is set, and the open link is still open meanwhile.
    assert (await db.get_token_by_id(token["id"]))["remember_pin"] == 0
    assert (await client.get("/g/no-pin-yet/state")).status_code == 200


async def test_remember_pin_endpoint_requires_admin(client, remembered, mock_ha_client):
    resp = await client.patch(
        f"/admin/tokens/{remembered['id']}/remember-pin", json={"remember_pin": False}
    )
    assert resp.status_code == 401
    assert (await db.get_token_by_id(remembered["id"]))["remember_pin"] == 1


async def test_remember_pin_endpoint_on_unknown_token_is_404(
    client, admin_session, test_db, mock_ha_client
):
    resp = await client.patch(
        "/admin/tokens/00000000-0000-0000-0000-000000000000/remember-pin",
        json={"remember_pin": False},
        cookies=admin_session,
    )
    assert resp.status_code == 404


async def test_remember_pin_endpoint_rejects_a_missing_value(
    client, admin_session, remembered, mock_ha_client
):
    resp = await client.patch(
        f"/admin/tokens/{remembered['id']}/remember-pin", json={}, cookies=admin_session
    )
    assert resp.status_code == 422
