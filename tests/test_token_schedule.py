"""PATCH /admin/tokens/{id}/schedule — edit a token's timing after creation.

A full replacement of start, end, weekly windows and use limit, under the same
rules creation applies, and a push to every connected guest tab so none of
them keeps acting on the old schedule.
"""
import time

import pytest_asyncio

from app import database as db
from app.models import NEVER_EXPIRES_SECONDS

WINDOWS = [{"weekdays": [1, 3], "start": "09:00", "end": "13:00"}]


@pytest_asyncio.fixture
async def token(test_db):
    return await db.create_token(
        label="Stay", slug="stay", entity_ids=["light.living_room"],
        expires_at=int(time.time()) + 3600, ip_allowlist=None,
    )


async def _patch(client, admin_session, token_id, **body):
    return await client.patch(
        f"/admin/tokens/{token_id}/schedule", json=body, cookies=admin_session
    )


async def test_schedule_replaces_every_timing_field(client, admin_session, token, mock_ha_client):
    now = int(time.time())
    resp = await _patch(
        client, admin_session, token["id"],
        starts_at=now + 3600, expires_at=now + 86400 * 7,
        access_windows=WINDOWS, max_uses=3,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["starts_at"] == now + 3600
    assert body["expires_at"] == now + 86400 * 7
    assert body["access_windows"] == WINDOWS
    assert (body["max_uses"], body["uses_remaining"]) == (3, 3)
    mock_ha_client["broadcast_schedule_changed"].assert_awaited_once_with(token["id"])


async def test_omitted_fields_are_cleared(client, admin_session, test_db, mock_ha_client):
    now = int(time.time())
    row = await db.create_token(
        label="Full", slug="full", entity_ids=["light.living_room"],
        expires_at=now + 86400, ip_allowlist=None, starts_at=now + 60,
        access_windows=WINDOWS, max_uses=1,
    )
    resp = await _patch(client, admin_session, row["id"], expires_at=NEVER_EXPIRES_SECONDS)
    body = resp.json()
    assert body["starts_at"] is None
    assert body["access_windows"] is None
    assert body["max_uses"] is None
    assert body["expires_at"] == NEVER_EXPIRES_SECONDS


async def test_a_past_start_is_folded_to_now(client, admin_session, token, mock_ha_client):
    resp = await _patch(
        client, admin_session, token["id"],
        starts_at=int(time.time()) - 60, expires_at=int(time.time()) + 3600,
    )
    assert resp.json()["starts_at"] is None


async def test_the_end_must_be_future_and_after_the_start(client, admin_session, token, mock_ha_client):
    now = int(time.time())
    resp = await _patch(client, admin_session, token["id"], expires_at=now - 1)
    assert resp.status_code == 422
    resp = await _patch(client, admin_session, token["id"], starts_at=now + 7200, expires_at=now + 3600)
    assert resp.status_code == 422
    row = await db.get_token_by_id(token["id"])
    assert row["expires_at"] == token["expires_at"]
    mock_ha_client["broadcast_schedule_changed"].assert_not_awaited()


async def test_the_end_is_required(client, admin_session, token, mock_ha_client):
    resp = await _patch(client, admin_session, token["id"])
    assert resp.status_code == 422


async def test_use_count_carries_over_unless_reset(client, admin_session, test_db, mock_ha_client):
    row = await db.create_token(
        label="Once", slug="once", entity_ids=["light.living_room"],
        expires_at=int(time.time()) + 3600, ip_allowlist=None, max_uses=1,
    )
    await db.consume_token_use(row["id"])
    end = int(time.time()) + 3600
    resp = await _patch(client, admin_session, row["id"], expires_at=end, max_uses=2)
    assert (resp.json()["use_count"], resp.json()["uses_remaining"]) == (1, 1)
    resp = await _patch(client, admin_session, row["id"], expires_at=end, max_uses=2, reset_uses=True)
    assert (resp.json()["use_count"], resp.json()["uses_remaining"]) == (0, 2)


async def test_a_new_schedule_takes_effect_on_the_guest_side(client, admin_session, token, mock_ha_client):
    now = int(time.time())
    await _patch(client, admin_session, token["id"], starts_at=now + 3600, expires_at=now + 7200)
    resp = await client.get(f"/g/{token['slug']}/state")
    assert resp.status_code == 403
    await _patch(client, admin_session, token["id"], expires_at=now + 7200)
    resp = await client.get(f"/g/{token['slug']}/state")
    assert resp.status_code == 200


async def test_a_revoked_token_is_refused(client, admin_session, token, mock_ha_client):
    await db.revoke_token(token["id"])
    resp = await _patch(client, admin_session, token["id"], expires_at=int(time.time()) + 60)
    assert resp.status_code == 400


async def test_schedule_is_admin_only_and_404s(client, admin_session, token, mock_ha_client):
    resp = await client.patch(
        f"/admin/tokens/{token['id']}/schedule", json={"expires_at": int(time.time()) + 60}
    )
    assert resp.status_code == 401
    resp = await _patch(client, admin_session, "nope", expires_at=int(time.time()) + 60)
    assert resp.status_code == 404
