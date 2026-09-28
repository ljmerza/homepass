"""Absolute expires_at on token create and renew.

A token can be given an end as an epoch second — a check-out time — instead of
a duration. Exactly one of the two is accepted, and the absolute one is taken
as written: it is never re-anchored to a scheduled start the way a duration is.

Real routing and DB; only ha_client is mocked.
"""
import time

from app import database as db
from app.models import NEVER_EXPIRES_SECONDS

BASE_BODY = {"label": "Stay", "entity_ids": ["light.living_room"]}


async def _create(client, admin_session, **fields):
    return await client.post(
        "/admin/tokens", json={**BASE_BODY, **fields}, cookies=admin_session
    )


async def test_absolute_expiry_is_stored_as_written(client, admin_session, mock_ha_client):
    end = int(time.time()) + 7200
    resp = await _create(client, admin_session, expires_at=end)
    assert resp.status_code == 201
    assert resp.json()["expires_at"] == end


async def test_absolute_expiry_is_not_shifted_by_a_start(client, admin_session, mock_ha_client):
    """The whole point of the field: a duration anchors to the start, a
    check-out time does not."""
    now = int(time.time())
    start, end = now + 3600, now + 86400
    resp = await _create(client, admin_session, starts_at=start, expires_at=end)
    assert resp.status_code == 201
    body = resp.json()
    assert body["starts_at"] == start
    assert body["expires_at"] == end


async def test_both_expiry_fields_is_rejected(client, admin_session, mock_ha_client):
    resp = await _create(
        client, admin_session,
        expires_in_seconds=3600, expires_at=int(time.time()) + 3600,
    )
    assert resp.status_code == 422
    assert "exactly one" in resp.json()["detail"]


async def test_neither_expiry_field_is_rejected(client, admin_session, mock_ha_client):
    resp = await _create(client, admin_session)
    assert resp.status_code == 422
    assert "exactly one" in resp.json()["detail"]


async def test_the_exactly_one_rejection_does_not_echo_the_pin(
    client, admin_session, mock_ha_client
):
    """The check lives in the router so its 422 cannot carry the request body,
    PIN included, back in an `input` field."""
    resp = await _create(client, admin_session, pin="918273")
    assert resp.status_code == 422
    assert "918273" not in resp.text


async def test_a_past_absolute_expiry_is_rejected(client, admin_session, mock_ha_client):
    resp = await _create(client, admin_session, expires_at=int(time.time()) - 60)
    assert resp.status_code == 422
    assert "future" in resp.json()["detail"]


async def test_an_expiry_before_the_start_is_rejected(client, admin_session, mock_ha_client):
    now = int(time.time())
    resp = await _create(client, admin_session, starts_at=now + 7200, expires_at=now + 3600)
    assert resp.status_code == 422
    assert "after starts_at" in resp.json()["detail"]
    assert not await db.list_tokens()


async def test_never_expires_is_accepted_as_an_absolute_value(
    client, admin_session, mock_ha_client
):
    resp = await _create(client, admin_session, expires_at=NEVER_EXPIRES_SECONDS)
    assert resp.status_code == 201
    assert resp.json()["expires_at"] == NEVER_EXPIRES_SECONDS


async def test_an_absolute_expiry_beyond_the_sentinel_is_rejected(
    client, admin_session, mock_ha_client
):
    resp = await _create(client, admin_session, expires_at=NEVER_EXPIRES_SECONDS + 1)
    assert resp.status_code == 422


async def test_renew_accepts_an_absolute_expiry(client, admin_session, sample_token, mock_ha_client):
    end = int(time.time()) + 5 * 86400
    resp = await client.patch(
        f"/admin/tokens/{sample_token['id']}/expiry",
        json={"expires_at": end},
        cookies=admin_session,
    )
    assert resp.status_code == 200
    assert resp.json()["expires_at"] == end


async def test_renew_of_a_pending_token_keeps_an_absolute_end_as_written(
    client, admin_session, test_db, mock_ha_client
):
    """The renew modal's custom date is a calendar end, so a still-pending
    token must not have its start added on top of it."""
    now = int(time.time())
    row = await db.create_token(
        label="Later", slug="later", entity_ids=["light.living_room"],
        expires_at=now + 86400 * 3, ip_allowlist=None, starts_at=now + 86400,
    )
    end = now + 86400 * 2
    resp = await client.patch(
        f"/admin/tokens/{row['id']}/expiry", json={"expires_at": end}, cookies=admin_session
    )
    assert resp.status_code == 200
    assert resp.json()["expires_at"] == end


async def test_renew_rejects_both_fields(client, admin_session, sample_token, mock_ha_client):
    resp = await client.patch(
        f"/admin/tokens/{sample_token['id']}/expiry",
        json={"expires_in_seconds": 60, "expires_at": int(time.time()) + 60},
        cookies=admin_session,
    )
    assert resp.status_code == 422
