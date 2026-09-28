"""Long-lived relays stop when the device holding them loses access.

Every guest route re-checks the token per request except two, which are
checked once, at connect: the SSE stream and a live camera view. The
properties carrying the weight:

  * the admin writes that sign guests out past the PIN — a PIN change,
    remember-PIN turned off, a link without PIN revoked or rotated — push
    access_changed, which hangs the stream up, the way revoke and unbind
    already push their own events;
  * behind those pushes, both relays re-run their gate every
    STREAM_REVALIDATE_SECONDS, so a device signed out by any means stops
    receiving state or frames even if no push reaches it; and
  * a device that is still let in is not disturbed by the re-check.
"""
import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from starlette.datastructures import Headers

from app import database as db
from app import guest_pin
from app import ha_client
from app.routers import guest
from app.routers.guest import _event_generator

PIN = "4821"


@pytest_asyncio.fixture
async def pin_token(test_db):
    return await db.create_token(
        label="Locked",
        slug="relock",
        entity_ids=["light.living_room", "camera.hall"],
        expires_at=int(time.time()) + 3600,
        ip_allowlist=None,
        pin_hash=await guest_pin.hash_pin(PIN),
    )


async def _code_session(token) -> tuple[str, str]:
    """A PIN session minted by a link without PIN: (code_id, cookie value)."""
    entry = await db.create_access_code(
        token["id"], guest_pin.hash_access_code(guest_pin.generate_access_code()), None
    )
    value, _ = guest_pin.issue_session(
        token["id"], token["pin_hash"], token["expires_at"], access_code_id=entry["id"]
    )
    return entry["id"], value


def _request(cookie: str):
    async def is_disconnected():
        return False
    return SimpleNamespace(
        headers=Headers({}),
        cookies={guest_pin.SESSION_COOKIE: cookie},
        client=SimpleNamespace(host="127.0.0.1"),
        is_disconnected=is_disconnected,
    )


async def _next(gen):
    return await asyncio.wait_for(gen.__anext__(), timeout=2)


# ---------------------------------------------------------------------------
# The periodic re-check
# ---------------------------------------------------------------------------

async def test_a_stream_hangs_up_once_its_access_link_is_revoked(pin_token, monkeypatch):
    monkeypatch.setattr(guest, "STREAM_REVALIDATE_SECONDS", 0.05)
    code_id, cookie = await _code_session(pin_token)
    gen = _event_generator(pin_token["id"], pin_token["slug"], _request(cookie))
    assert (await _next(gen)).startswith("event: connected")

    # Still let in: the re-check runs and the stream carries on.
    assert (await _next(gen)).startswith(": keepalive")

    await db.delete_access_code(pin_token["id"], code_id)
    frame = await _next(gen)
    while frame.startswith(": keepalive"):
        frame = await _next(gen)
    assert frame.startswith("event: access_changed")
    with pytest.raises(StopAsyncIteration):
        await _next(gen)


async def test_a_stream_hangs_up_once_the_pin_changes(pin_token, monkeypatch):
    monkeypatch.setattr(guest, "STREAM_REVALIDATE_SECONDS", 0.05)
    value, _ = guest_pin.issue_session(pin_token["id"], pin_token["pin_hash"], pin_token["expires_at"])
    gen = _event_generator(pin_token["id"], pin_token["slug"], _request(value))
    await _next(gen)

    await db.set_token_pin(pin_token["id"], await guest_pin.hash_pin("9999"))
    frame = await _next(gen)
    while frame.startswith(": keepalive"):
        frame = await _next(gen)
    assert frame.startswith("event: access_changed")


async def test_a_live_stream_is_rechecked_as_live(test_db, monkeypatch):
    """A token pushed back into pending under an open stream stops relaying
    state, even if the schedule_changed push never arrived."""
    monkeypatch.setattr(guest, "STREAM_REVALIDATE_SECONDS", 0.05)
    row = await db.create_token(
        label="Plain", slug="plain", entity_ids=["light.living_room"],
        expires_at=int(time.time()) + 3600, ip_allowlist=None,
    )
    gen = _event_generator(row["id"], row["slug"], _request(""))
    await _next(gen)
    await db.update_token_schedule(
        row["id"], starts_at=int(time.time()) + 600, expires_at=row["expires_at"],
        access_windows=None, max_uses=None,
    )
    frame = await _next(gen)
    while frame.startswith(": keepalive"):
        frame = await _next(gen)
    assert frame.startswith("event: access_changed")


# ---------------------------------------------------------------------------
# The push
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pending", [False, True])
async def test_access_changed_is_forwarded_and_ends_the_stream(pin_token, pending):
    _, cookie = await _code_session(pin_token)
    starts_at = time.time() + 3600 if pending else None
    gen = _event_generator(pin_token["id"], pin_token["slug"], _request(cookie), starts_at)
    await _next(gen)
    await ha_client.broadcast_access_changed(pin_token["id"])
    assert (await _next(gen)).startswith("event: access_changed")
    with pytest.raises(StopAsyncIteration):
        await _next(gen)


async def test_revoking_an_access_link_pushes(client, admin_session, pin_token, mock_ha_client):
    code_id, _ = await _code_session(pin_token)
    resp = await client.delete(
        f"/admin/tokens/{pin_token['id']}/access-codes/{code_id}", cookies=admin_session
    )
    assert resp.status_code == 200
    mock_ha_client["broadcast_access_changed"].assert_awaited_once_with(pin_token["id"])


async def test_rotating_an_access_link_pushes(client, admin_session, pin_token, mock_ha_client):
    code_id, _ = await _code_session(pin_token)
    resp = await client.post(
        f"/admin/tokens/{pin_token['id']}/access-codes/{code_id}/rotate", cookies=admin_session
    )
    assert resp.status_code == 200
    mock_ha_client["broadcast_access_changed"].assert_awaited_once_with(pin_token["id"])


async def test_changing_the_pin_pushes(client, admin_session, pin_token, mock_ha_client):
    resp = await client.patch(
        f"/admin/tokens/{pin_token['id']}/pin", json={"pin": "7777"}, cookies=admin_session
    )
    assert resp.status_code == 200
    mock_ha_client["broadcast_access_changed"].assert_awaited_once_with(pin_token["id"])


@pytest.mark.parametrize("remember, pushed", [(False, True), (True, False)])
async def test_only_turning_remember_pin_off_pushes(
    client, admin_session, pin_token, mock_ha_client, remember, pushed
):
    resp = await client.patch(
        f"/admin/tokens/{pin_token['id']}/remember-pin",
        json={"remember_pin": remember}, cookies=admin_session,
    )
    assert resp.status_code == 200
    assert mock_ha_client["broadcast_access_changed"].await_count == (1 if pushed else 0)


async def test_the_api_pin_change_pushes(client, pin_token, mock_ha_client, monkeypatch):
    from app.config import settings

    key = "k" * 40
    monkeypatch.setattr(settings, "api_enabled", True)
    monkeypatch.setattr(settings, "api_token", key)
    resp = await client.patch(
        f"/api/v1/tokens/{pin_token['id']}", json={"pin": None}, headers={"X-API-Key": key}
    )
    assert resp.status_code == 200
    mock_ha_client["broadcast_access_changed"].assert_awaited_once_with(pin_token["id"])


def test_the_guest_page_reloads_on_access_changed():
    with open("templates/guest_pwa.html") as fh:
        page = fh.read()
    assert "pendingEventSource.addEventListener('access_changed'" in page
    assert "eventSource.addEventListener('access_changed'" in page


# ---------------------------------------------------------------------------
# The camera relay
# ---------------------------------------------------------------------------

async def test_a_live_camera_view_stops_once_the_device_is_signed_out(
    client, pin_token, mock_ha_client, monkeypatch
):
    monkeypatch.setattr(guest, "STREAM_REVALIDATE_SECONDS", 0)
    code_id, cookie = await _code_session(pin_token)

    @asynccontextmanager
    async def camera_stream(entity_id):
        async def chunks():
            yield b"frame-1"
            # The admin revokes the link while the view is open.
            await db.delete_access_code(pin_token["id"], code_id)
            yield b"frame-2"
            yield b"frame-3"
        yield "multipart/x-mixed-replace; boundary=frame", chunks()

    monkeypatch.setattr(ha_client, "camera_stream", camera_stream)
    client.cookies.set(guest_pin.SESSION_COOKIE, cookie)
    resp = await client.get("/g/relock/camera/camera.hall/stream")
    assert resp.status_code == 200
    assert resp.content == b"frame-1"


async def test_a_live_camera_view_carries_on_while_the_device_is_let_in(
    client, pin_token, mock_ha_client, monkeypatch
):
    monkeypatch.setattr(guest, "STREAM_REVALIDATE_SECONDS", 0)
    _, cookie = await _code_session(pin_token)
    client.cookies.set(guest_pin.SESSION_COOKIE, cookie)
    resp = await client.get("/g/relock/camera/camera.hall/stream")
    assert resp.status_code == 200
    assert resp.content.endswith(b"fake-frame")
