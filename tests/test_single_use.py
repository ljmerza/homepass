"""Use-limited (single-use) links.

max_uses caps how many times a link can be used; NULL is unlimited. A use is
one guest command Home Assistant accepted — nothing else. The properties under
test:

  * opening the link, reading /state, holding the stream and viewing a camera
    never spend a use, so a chat app unfurling the URL into a preview card
    cannot burn the guest's one use (upstream fork c7bb929's bug);
  * the claim is atomic, a refused or failed command costs nothing, and once
    the last use is spent every guest route answers 410 and connected tabs are
    told;
  * the admin sees uses and remaining, and Renew brings a spent link back.

Real routing, real DB; only ha_client is mocked.
"""
import asyncio
import time

import httpx
import pytest_asyncio

from app import database as db
from app.models import MAX_USES_CAP

WHATSAPP_UA = "WhatsApp/2.23.20.0 A"
TURN_ON = {"entity_id": "light.living_room", "service": "light.turn_on"}


async def _make_token(slug="once", max_uses=1, entity_ids=("light.living_room", "camera.hall")):
    return await db.create_token(
        label=f"Token {slug}", slug=slug, entity_ids=list(entity_ids),
        expires_at=int(time.time()) + 86400, ip_allowlist=None, max_uses=max_uses,
    )


@pytest_asyncio.fixture
async def once(test_db):
    return await _make_token()


async def test_an_unlimited_token_is_unaffected(client, sample_token, mock_ha_client):
    for _ in range(3):
        resp = await client.post(f"/g/{sample_token['slug']}/command", json=TURN_ON)
        assert resp.json() == {"ok": True}
    row = await db.get_token_by_id(sample_token["id"])
    assert row["max_uses"] is None and row["use_count"] == 0


async def test_previews_and_reads_never_spend_a_use(client, once, mock_ha_client):
    """Everything a link-preview bot — or a guest just looking — can do."""
    slug = once["slug"]
    for _ in range(3):
        assert (await client.get(f"/g/{slug}", headers={"User-Agent": WHATSAPP_UA})).status_code == 200
        assert (await client.get(f"/g/{slug}/state")).status_code == 200
        assert (await client.get(f"/g/{slug}/camera/camera.hall")).status_code == 200
        assert (await client.get(f"/g/{slug}/manifest.json")).status_code == 200
    row = await db.get_token_by_id(once["id"])
    assert row["use_count"] == 0


async def test_one_command_spends_the_single_use(client, once, mock_ha_client):
    resp = await client.post(f"/g/{once['slug']}/command", json=TURN_ON)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "uses_remaining": 0}
    mock_ha_client["broadcast_token_expired"].assert_awaited_once_with(once["id"], reason="used")

    again = await client.post(f"/g/{once['slug']}/command", json=TURN_ON)
    assert again.status_code == 410
    assert mock_ha_client["call_service"].await_count == 1


async def test_a_spent_link_is_dead_on_every_route(client, once, mock_ha_client):
    await client.post(f"/g/{once['slug']}/command", json=TURN_ON)
    for suffix in ("/state", "/camera/camera.hall", "/stream"):
        resp = await client.get(f"/g/{once['slug']}{suffix}")
        assert resp.status_code == 410, suffix
    page = await client.get(f"/g/{once['slug']}")
    assert page.status_code == 410
    assert "Link Already Used" in page.text


async def test_a_revoked_limited_link_still_reads_as_expired(client, once, mock_ha_client):
    await client.post(f"/g/{once['slug']}/command", json=TURN_ON)
    await db.revoke_token(once["id"])
    page = await client.get(f"/g/{once['slug']}")
    assert "Access Expired" in page.text


async def test_n_uses_count_down(client, test_db, mock_ha_client):
    row = await _make_token("thrice", max_uses=3)
    remaining = []
    for _ in range(3):
        resp = await client.post(f"/g/{row['slug']}/command", json=TURN_ON)
        remaining.append(resp.json()["uses_remaining"])
    assert remaining == [2, 1, 0]
    mock_ha_client["broadcast_token_expired"].assert_awaited_once()


async def test_a_refused_command_costs_nothing(client, once, mock_ha_client):
    bad = [
        {"entity_id": "light.kitchen", "service": "light.turn_on"},        # not on the token
        {"entity_id": "light.living_room", "service": "light.set_colour"},  # not allowed
        {"entity_id": "light.living_room", "service": "light.turn_on",
         "data": {"rgb_color": [300, 0, 0]}},                               # malformed payload
    ]
    for body in bad:
        resp = await client.post(f"/g/{once['slug']}/command", json=body)
        assert resp.status_code in (403, 422)
    row = await db.get_token_by_id(once["id"])
    assert row["use_count"] == 0


async def test_a_failed_home_assistant_call_is_refunded(client, once, mock_ha_client):
    mock_ha_client["call_service"].side_effect = httpx.ConnectError("down")
    resp = await client.post(f"/g/{once['slug']}/command", json=TURN_ON)
    assert resp.status_code == 502
    row = await db.get_token_by_id(once["id"])
    assert row["use_count"] == 0

    mock_ha_client["call_service"].side_effect = None
    resp = await client.post(f"/g/{once['slug']}/command", json=TURN_ON)
    assert resp.json()["uses_remaining"] == 0


async def test_racing_commands_cannot_both_take_the_last_use(client, once, mock_ha_client):
    responses = await asyncio.gather(*[
        client.post(f"/g/{once['slug']}/command", json=TURN_ON) for _ in range(5)
    ])
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 410, 410, 410, 410]
    assert mock_ha_client["call_service"].await_count == 1
    row = await db.get_token_by_id(once["id"])
    assert row["use_count"] == 1


async def test_consume_is_atomic_at_the_database(test_db):
    row = await _make_token("db-once")
    assert await db.consume_token_use(row["id"]) is True
    assert await db.consume_token_use(row["id"]) is False
    await db.refund_token_use(row["id"])
    assert await db.consume_token_use(row["id"]) is True
    unlimited = await _make_token("db-unlimited", max_uses=None)
    assert await db.consume_token_use(unlimited["id"]) is False


# ---------------------------------------------------------------------------
# Admin side
# ---------------------------------------------------------------------------

async def test_admin_create_and_list_show_uses(client, admin_session, mock_ha_client):
    resp = await client.post("/admin/tokens", json={
        "label": "Courier", "entity_ids": ["light.living_room"],
        "expires_in_seconds": 86400, "max_uses": 1,
    }, cookies=admin_session)
    assert resp.status_code == 201
    body = resp.json()
    assert (body["max_uses"], body["use_count"], body["uses_remaining"]) == (1, 0, 1)

    await client.post(f"/g/{body['slug']}/command", json=TURN_ON)
    listed = (await client.get("/admin/tokens", cookies=admin_session)).json()
    assert (listed[0]["use_count"], listed[0]["uses_remaining"]) == (1, 0)


async def test_an_unlimited_token_reports_no_remaining(client, admin_session, sample_token, mock_ha_client):
    body = (await client.get(f"/admin/tokens/{sample_token['id']}", cookies=admin_session)).json()
    assert body["max_uses"] is None and body["uses_remaining"] is None


async def test_max_uses_bounds(client, admin_session, mock_ha_client):
    for bad in (0, -1, MAX_USES_CAP + 1):
        resp = await client.post("/admin/tokens", json={
            "label": "x", "entity_ids": ["light.living_room"],
            "expires_in_seconds": 60, "max_uses": bad,
        }, cookies=admin_session)
        assert resp.status_code == 422, bad


async def test_renew_brings_a_spent_link_back(client, admin_session, once, mock_ha_client):
    await client.post(f"/g/{once['slug']}/command", json=TURN_ON)
    resp = await client.patch(
        f"/admin/tokens/{once['id']}/expiry", json={"expires_in_seconds": 3600},
        cookies=admin_session,
    )
    assert resp.json()["uses_remaining"] == 1
    assert (await client.get(f"/g/{once['slug']}/state")).status_code == 200


async def test_extending_a_link_with_uses_left_keeps_the_count(
    client, admin_session, test_db, mock_ha_client
):
    row = await _make_token("twice", max_uses=2)
    await client.post(f"/g/{row['slug']}/command", json=TURN_ON)
    resp = await client.patch(
        f"/admin/tokens/{row['id']}/expiry", json={"expires_in_seconds": 3600},
        cookies=admin_session,
    )
    assert resp.json()["use_count"] == 1
