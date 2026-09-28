"""PIN-free access links: /g/<slug>?c=<code> in place of typing the PIN.

The property under test throughout is that a code is a one-step way to earn the
PIN session cookie and nothing more. It is honoured on the page only, never on
/state, /stream, /command or the camera routes; it skips the keypad but not the
IP allowlist, revocation or expiry; and taking it away — revoke, rotate, a PIN
change, a slug rotation — also signs out every device it let in.

Upstream forks got several of these wrong: codes stored and returned in the
clear, accepted as a query parameter on every API route, looked up without a
rate limit, and left alive by a PIN change. Each has a test here.

Real routing, real DB, real bcrypt, real rate limiter; only ha_client is mocked.
"""
import hashlib
import time

import pytest
import pytest_asyncio

from app import database as db
from app import guest_pin
from app.routers.admin import MAX_ACCESS_CODES_PER_TOKEN
from app.routers.guest import ACCESS_CODE_LIMITS_PER_IP, ACCESS_CODE_LIMITS_PER_TOKEN

PIN = "4821"

GATED_ENDPOINTS = [
    ("GET", "/state", None),
    ("GET", "/stream", None),
    ("GET", "/camera/camera.hall", None),
    ("GET", "/camera/camera.hall/stream", None),
    ("POST", "/command", {"entity_id": "light.living_room", "service": "turn_on"}),
]


async def _make_token(
    slug: str,
    pin: str | None = PIN,
    remember_pin: bool = True,
    ip_allowlist: list[str] | None = None,
    starts_at: int | None = None,
):
    return await db.create_token(
        label=f"Token {slug}",
        slug=slug,
        entity_ids=["light.living_room", "camera.hall"],
        expires_at=int(time.time()) + 3600,
        ip_allowlist=ip_allowlist,
        pin_hash=await guest_pin.hash_pin(pin) if pin else None,
        remember_pin=remember_pin,
        starts_at=starts_at,
    )


async def _mint(client, admin_session, token_id: str, label: str | None = None) -> dict:
    resp = await client.post(
        f"/admin/tokens/{token_id}/access-codes",
        json={"label": label} if label is not None else {},
        cookies=admin_session,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _session_value(resp) -> str:
    raw = resp.headers["set-cookie"]
    assert raw.startswith(f"{guest_pin.SESSION_COOKIE}=")
    return raw.split(";")[0].split("=", 1)[1]


def _cookie_header(value: str) -> dict:
    return {"Cookie": f"{guest_pin.SESSION_COOKIE}={value}"}


async def _redeem(client, slug: str, code: str, **kwargs):
    return await client.get(f"/g/{slug}", params={"c": code}, **kwargs)


@pytest_asyncio.fixture
async def pin_token(test_db):
    return await _make_token("pin-token")


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------

async def test_mint_returns_the_code_once(client, admin_session, pin_token, mock_ha_client):
    entry = await _mint(client, admin_session, pin_token["id"], label="Front door QR")
    assert entry["label"] == "Front door QR"
    assert entry["last_used_at"] is None
    # 192 bits, URL-safe: at least the 128 the slug is held to.
    assert len(entry["code"]) == 32
    assert guest_pin.ACCESS_CODE_BYTES * 8 >= 128

    listed = (await client.get(
        f"/admin/tokens/{pin_token['id']}/access-codes", cookies=admin_session
    )).json()
    assert listed == [{k: entry[k] for k in ("id", "label", "created_at", "last_used_at")}]
    # Nothing usable in the list — no code, no hash.
    assert entry["code"] not in str(listed)
    assert "code_hash" not in str(listed)


async def test_code_is_stored_hashed_not_in_the_clear(
    client, admin_session, pin_token, mock_ha_client
):
    entry = await _mint(client, admin_session, pin_token["id"])
    conn = await db.get_db()
    async with conn.execute("SELECT * FROM token_access_codes") as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert entry["code"] not in " ".join(str(v) for v in tuple(rows[0]) if v is not None)
    assert rows[0]["code_hash"] == hashlib.sha256(entry["code"].encode()).hexdigest()


async def test_the_token_response_never_carries_a_code(
    client, admin_session, pin_token, mock_ha_client
):
    entry = await _mint(client, admin_session, pin_token["id"])
    for resp in (
        await client.get("/admin/tokens", cookies=admin_session),
        await client.get(f"/admin/tokens/{pin_token['id']}", cookies=admin_session),
    ):
        assert entry["code"] not in resp.text


async def test_each_mint_is_a_distinct_link(client, admin_session, pin_token, mock_ha_client):
    a = await _mint(client, admin_session, pin_token["id"])
    b = await _mint(client, admin_session, pin_token["id"])
    assert a["id"] != b["id"] and a["code"] != b["code"]


async def test_label_is_trimmed_and_blank_means_none(
    client, admin_session, pin_token, mock_ha_client
):
    assert (await _mint(client, admin_session, pin_token["id"], "  Sam  "))["label"] == "Sam"
    assert (await _mint(client, admin_session, pin_token["id"], "   "))["label"] is None


async def test_overlong_label_is_refused(client, admin_session, pin_token, mock_ha_client):
    resp = await client.post(
        f"/admin/tokens/{pin_token['id']}/access-codes",
        json={"label": "x" * 65},
        cookies=admin_session,
    )
    assert resp.status_code == 422


async def test_mint_needs_a_pin(client, admin_session, test_db, mock_ha_client):
    token = await _make_token("open-token", pin=None)
    resp = await client.post(
        f"/admin/tokens/{token['id']}/access-codes", json={}, cookies=admin_session
    )
    assert resp.status_code == 400


async def test_mint_refused_on_a_revoked_token(client, admin_session, pin_token, mock_ha_client):
    await db.revoke_token(pin_token["id"])
    resp = await client.post(
        f"/admin/tokens/{pin_token['id']}/access-codes", json={}, cookies=admin_session
    )
    assert resp.status_code == 400


async def test_mint_is_capped_per_token(client, admin_session, pin_token, mock_ha_client):
    for _ in range(MAX_ACCESS_CODES_PER_TOKEN):
        await _mint(client, admin_session, pin_token["id"])
    resp = await client.post(
        f"/admin/tokens/{pin_token['id']}/access-codes", json={}, cookies=admin_session
    )
    assert resp.status_code == 409


async def test_access_code_endpoints_require_admin(client, admin_session, pin_token, mock_ha_client):
    entry = await _mint(client, admin_session, pin_token["id"])
    base = f"/admin/tokens/{pin_token['id']}/access-codes"
    assert (await client.get(base)).status_code == 401
    assert (await client.post(base, json={})).status_code == 401
    assert (await client.post(f"{base}/{entry['id']}/rotate")).status_code == 401
    assert (await client.delete(f"{base}/{entry['id']}")).status_code == 401
    # The guest session a code earns is not admin auth either.
    session = _session_value(await _redeem(client, "pin-token", entry["code"]))
    assert (await client.get(base, headers=_cookie_header(session))).status_code == 401


async def test_unknown_token_or_link_is_404(client, admin_session, pin_token, mock_ha_client):
    ghost = "00000000-0000-0000-0000-000000000000"
    assert (await client.get(
        f"/admin/tokens/{ghost}/access-codes", cookies=admin_session
    )).status_code == 404
    assert (await client.post(
        f"/admin/tokens/{ghost}/access-codes", json={}, cookies=admin_session
    )).status_code == 404
    base = f"/admin/tokens/{pin_token['id']}/access-codes"
    assert (await client.post(f"{base}/{'0' * 32}/rotate", cookies=admin_session)).status_code == 404
    assert (await client.delete(f"{base}/{'0' * 32}", cookies=admin_session)).status_code == 404


async def test_a_link_cannot_be_managed_through_another_token(
    client, admin_session, pin_token, mock_ha_client
):
    other = await _make_token("other-token")
    entry = await _mint(client, admin_session, pin_token["id"])
    base = f"/admin/tokens/{other['id']}/access-codes/{entry['id']}"
    assert (await client.delete(base, cookies=admin_session)).status_code == 404
    assert (await client.post(f"{base}/rotate", cookies=admin_session)).status_code == 404
    assert await db.access_code_exists(pin_token["id"], entry["id"])


# ---------------------------------------------------------------------------
# Redeeming
# ---------------------------------------------------------------------------

async def test_valid_code_unlocks_and_strips_itself(
    client, admin_session, pin_token, mock_ha_client
):
    entry = await _mint(client, admin_session, pin_token["id"])
    resp = await _redeem(client, "pin-token", entry["code"])
    assert resp.status_code == 303
    assert resp.headers["location"] == "/g/pin-token"
    assert entry["code"] not in resp.headers["location"]
    raw = resp.headers["set-cookie"].lower()
    assert "httponly" in raw and "samesite=lax" in raw and "path=/g/pin-token" in raw

    # The client jar now holds the same cookie a correct PIN earns.
    page = await client.get("/g/pin-token")
    assert page.status_code == 200
    assert "Enter PIN" not in page.text
    assert (await client.get("/g/pin-token/state")).status_code == 200
    assert (await client.get("/g/pin-token/camera/camera.hall")).status_code == 200
    cmd = await client.post(
        "/g/pin-token/command",
        json={"entity_id": "light.living_room", "service": "turn_on"},
    )
    assert cmd.status_code == 200


async def test_redemption_records_last_used(client, admin_session, pin_token, mock_ha_client):
    entry = await _mint(client, admin_session, pin_token["id"])
    await _redeem(client, "pin-token", entry["code"])
    listed = (await client.get(
        f"/admin/tokens/{pin_token['id']}/access-codes", cookies=admin_session
    )).json()
    assert listed[0]["last_used_at"] is not None


async def test_one_link_opens_many_devices(client, admin_session, pin_token, mock_ha_client):
    entry = await _mint(client, admin_session, pin_token["id"])
    first = _session_value(await _redeem(client, "pin-token", entry["code"]))
    client.cookies.clear()
    second = _session_value(await _redeem(client, "pin-token", entry["code"]))
    for session in (first, second):
        resp = await client.get("/g/pin-token/state", headers=_cookie_header(session))
        assert resp.status_code == 200


@pytest.mark.parametrize("method,suffix,body", GATED_ENDPOINTS)
async def test_code_is_not_accepted_on_the_api(
    client, admin_session, pin_token, mock_ha_client, method, suffix, body
):
    """The fork honoured ?c= on every guest route, which made the code a
    permanent bearer credential for the API. Here it is honoured on the page
    and nowhere else."""
    entry = await _mint(client, admin_session, pin_token["id"])
    resp = await client.request(
        method, f"/g/pin-token{suffix}", params={"c": entry["code"]}, json=body
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "PIN required"}
    assert "set-cookie" not in resp.headers
    mock_ha_client["call_service"].assert_not_called()
    mock_ha_client["camera_snapshot"].assert_not_called()


@pytest.mark.parametrize("bad", ["", "nope", "A" * 32, "A" * 31, "A" * 33, "../../etc"])
async def test_wrong_code_gets_the_keypad(client, admin_session, pin_token, mock_ha_client, bad):
    await _mint(client, admin_session, pin_token["id"])
    resp = await _redeem(client, "pin-token", bad)
    assert resp.status_code == 401
    assert "Enter PIN" in resp.text
    assert "no longer skips the PIN" in resp.text
    assert "set-cookie" not in resp.headers
    assert (await client.get("/g/pin-token/state")).status_code == 401


async def test_the_pin_still_works_alongside_links(client, admin_session, pin_token, mock_ha_client):
    await _mint(client, admin_session, pin_token["id"])
    resp = await client.post("/g/pin-token/pin", data={"pin": PIN})
    assert resp.status_code == 303
    assert (await client.get("/g/pin-token/state")).status_code == 200


async def test_code_for_one_token_does_not_open_another(
    client, admin_session, pin_token, mock_ha_client
):
    await _make_token("token-b")
    entry = await _mint(client, admin_session, pin_token["id"])
    resp = await _redeem(client, "token-b", entry["code"])
    assert resp.status_code == 401
    assert "set-cookie" not in resp.headers


async def test_code_session_does_not_unlock_another_token(
    client, admin_session, pin_token, mock_ha_client
):
    await _make_token("token-b")
    entry = await _mint(client, admin_session, pin_token["id"])
    session = _session_value(await _redeem(client, "pin-token", entry["code"]))
    resp = await client.get("/g/token-b/state", headers=_cookie_header(session))
    assert resp.status_code == 401


async def test_code_on_a_token_without_a_pin_is_just_stripped(client, test_db, mock_ha_client):
    await _make_token("open-token", pin=None)
    resp = await _redeem(client, "open-token", "whatever")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/g/open-token"
    assert "set-cookie" not in resp.headers


async def test_an_unlocked_device_is_just_stripped(client, admin_session, pin_token, mock_ha_client):
    """Already past the gate: redirect, no new cookie, and no budget spent."""
    await client.post("/g/pin-token/pin", data={"pin": PIN})
    for _ in range(ACCESS_CODE_LIMITS_PER_IP[0][1] + 2):
        resp = await _redeem(client, "pin-token", "stale-link-from-history")
        assert resp.status_code == 303
        assert "set-cookie" not in resp.headers


async def test_code_does_not_skip_the_ip_allowlist(client, admin_session, test_db, mock_ha_client):
    token = await _make_token("fenced", ip_allowlist=["10.0.0.0/8"])
    entry = await _mint(client, admin_session, token["id"])
    resp = await _redeem(client, "fenced", entry["code"], headers={"X-Forwarded-For": "192.0.2.1"})
    assert resp.status_code == 403
    assert "set-cookie" not in resp.headers

    resp = await _redeem(client, "fenced", entry["code"], headers={"X-Forwarded-For": "10.1.2.3"})
    assert resp.status_code == 303


async def test_code_does_not_revive_a_revoked_or_expired_token(
    client, admin_session, pin_token, mock_ha_client
):
    entry = await _mint(client, admin_session, pin_token["id"])
    await db.revoke_token(pin_token["id"])
    resp = await _redeem(client, "pin-token", entry["code"])
    assert resp.status_code == 410
    assert "set-cookie" not in resp.headers

    await db.unrevoke_token(pin_token["id"])
    await db.update_token_expiry(pin_token["id"], int(time.time()) - 1)
    resp = await _redeem(client, "pin-token", entry["code"])
    assert resp.status_code == 410


async def test_code_on_a_scheduled_token_lands_on_the_countdown(
    client, admin_session, test_db, mock_ha_client
):
    token = await _make_token("later", starts_at=int(time.time()) + 3600)
    entry = await _mint(client, admin_session, token["id"])
    resp = await _redeem(client, "later", entry["code"])
    assert resp.status_code == 303
    page = await client.get("/g/later")
    assert page.status_code == 200
    assert "Enter PIN" not in page.text
    # Past the PIN, still not active: the data routes answer "not yet".
    assert (await client.get("/g/later/state")).status_code == 403


async def test_code_honours_remember_pin_off(client, admin_session, test_db, mock_ha_client):
    token = await _make_token("forgetful", remember_pin=False)
    entry = await _mint(client, admin_session, token["id"])
    resp = await _redeem(client, "forgetful", entry["code"])
    raw = resp.headers["set-cookie"].lower()
    assert "max-age=" not in raw and "expires=" not in raw
    assert (await client.get("/g/forgetful/state")).status_code == 200


async def test_code_session_is_secure_over_https(client, admin_session, pin_token, mock_ha_client):
    entry = await _mint(client, admin_session, pin_token["id"])
    resp = await _redeem(client, "pin-token", entry["code"], headers={"x-forwarded-proto": "https"})
    assert "secure" in resp.headers["set-cookie"].lower()


async def test_code_never_reaches_the_access_log_or_activity(
    client, admin_session, pin_token, mock_ha_client
):
    entry = await _mint(client, admin_session, pin_token["id"])
    await _redeem(client, "pin-token", entry["code"])
    await client.get("/g/pin-token")

    conn = await db.get_db()
    async with conn.execute("SELECT * FROM access_log") as cur:
        rows = await cur.fetchall()
    assert rows
    for row in rows:
        assert entry["code"] not in " ".join(str(v) for v in tuple(row) if v is not None)
    for call in mock_ha_client["fire_event"].call_args_list + mock_ha_client["logbook_log"].call_args_list:
        assert entry["code"] not in str(call)


# ---------------------------------------------------------------------------
# Taking a link away signs out what it let in
# ---------------------------------------------------------------------------

async def test_revoking_a_link_signs_out_its_devices(
    client, admin_session, pin_token, mock_ha_client
):
    keep = await _mint(client, admin_session, pin_token["id"], "keep")
    drop = await _mint(client, admin_session, pin_token["id"], "drop")
    # One device per session: an unlocked jar would make the next redemption
    # a plain strip with no cookie of its own.
    kept = _session_value(await _redeem(client, "pin-token", keep["code"]))
    client.cookies.clear()
    dropped = _session_value(await _redeem(client, "pin-token", drop["code"]))
    client.cookies.clear()
    typed = _session_value(await client.post("/g/pin-token/pin", data={"pin": PIN}))
    client.cookies.clear()

    resp = await client.delete(
        f"/admin/tokens/{pin_token['id']}/access-codes/{drop['id']}", cookies=admin_session
    )
    assert resp.status_code == 200

    assert (await _redeem(client, "pin-token", drop["code"])).status_code == 401
    assert (await client.get("/g/pin-token/state", headers=_cookie_header(dropped))).status_code == 401
    # Only that link's devices: the other link and a typed PIN are untouched.
    assert (await client.get("/g/pin-token/state", headers=_cookie_header(kept))).status_code == 200
    assert (await client.get("/g/pin-token/state", headers=_cookie_header(typed))).status_code == 200


async def test_revoking_works_on_a_revoked_token(client, admin_session, pin_token, mock_ha_client):
    """Taking a credential away is never refused."""
    entry = await _mint(client, admin_session, pin_token["id"])
    await db.revoke_token(pin_token["id"])
    resp = await client.delete(
        f"/admin/tokens/{pin_token['id']}/access-codes/{entry['id']}", cookies=admin_session
    )
    assert resp.status_code == 200


async def test_rotating_a_link_replaces_it(client, admin_session, pin_token, mock_ha_client):
    old = await _mint(client, admin_session, pin_token["id"], "Sam")
    old_session = _session_value(await _redeem(client, "pin-token", old["code"]))
    client.cookies.clear()

    resp = await client.post(
        f"/admin/tokens/{pin_token['id']}/access-codes/{old['id']}/rotate", cookies=admin_session
    )
    assert resp.status_code == 200
    new = resp.json()
    assert new["label"] == "Sam"
    assert new["id"] != old["id"] and new["code"] != old["code"]

    assert (await _redeem(client, "pin-token", old["code"])).status_code == 401
    assert (await client.get("/g/pin-token/state", headers=_cookie_header(old_session))).status_code == 401
    assert (await _redeem(client, "pin-token", new["code"])).status_code == 303

    listed = (await client.get(
        f"/admin/tokens/{pin_token['id']}/access-codes", cookies=admin_session
    )).json()
    assert [e["id"] for e in listed] == [new["id"]]


async def test_changing_the_pin_retires_every_link(client, admin_session, pin_token, mock_ha_client):
    entry = await _mint(client, admin_session, pin_token["id"])
    session = _session_value(await _redeem(client, "pin-token", entry["code"]))
    client.cookies.clear()

    resp = await client.patch(
        f"/admin/tokens/{pin_token['id']}/pin", json={"pin": "9999"}, cookies=admin_session
    )
    assert resp.status_code == 200

    assert await db.list_access_codes(pin_token["id"]) == []
    assert (await _redeem(client, "pin-token", entry["code"])).status_code == 401
    assert (await client.get("/g/pin-token/state", headers=_cookie_header(session))).status_code == 401


async def test_clearing_then_resetting_the_pin_does_not_revive_links(
    client, admin_session, pin_token, mock_ha_client
):
    entry = await _mint(client, admin_session, pin_token["id"])
    await client.patch(f"/admin/tokens/{pin_token['id']}/pin", json={"pin": None}, cookies=admin_session)
    assert await db.list_access_codes(pin_token["id"]) == []
    await client.patch(f"/admin/tokens/{pin_token['id']}/pin", json={"pin": PIN}, cookies=admin_session)
    assert (await _redeem(client, "pin-token", entry["code"])).status_code == 401


async def test_rotating_the_slug_retires_every_link(client, admin_session, pin_token, mock_ha_client):
    entry = await _mint(client, admin_session, pin_token["id"])
    resp = await client.post(f"/admin/tokens/{pin_token['id']}/rotate-slug", cookies=admin_session)
    new_slug = resp.json()["slug"]
    assert await db.list_access_codes(pin_token["id"]) == []
    assert (await _redeem(client, new_slug, entry["code"])).status_code == 401


async def test_deleting_the_token_deletes_its_links(client, admin_session, pin_token, mock_ha_client):
    await _mint(client, admin_session, pin_token["id"])
    await client.delete(f"/admin/tokens/{pin_token['id']}", cookies=admin_session)
    conn = await db.get_db()
    async with conn.execute("SELECT COUNT(*) FROM token_access_codes") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_forged_link_id_in_a_session_is_rejected(
    client, admin_session, pin_token, mock_ha_client
):
    """The link id rides in the clear; swapping it breaks the signature."""
    a = await _mint(client, admin_session, pin_token["id"])
    b = await _mint(client, admin_session, pin_token["id"])
    session = _session_value(await _redeem(client, "pin-token", a["code"]))
    version, expires_at, sig, _code_id = session.split(".")

    swapped = f"{version}.{expires_at}.{sig}.{b['id']}"
    assert (await client.get("/g/pin-token/state", headers=_cookie_header(swapped))).status_code == 401
    # Dropping the id to pass as a typed-PIN session fails too.
    stripped = f"{version}.{expires_at}.{sig}"
    assert (await client.get("/g/pin-token/state", headers=_cookie_header(stripped))).status_code == 401


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

async def test_redemptions_are_rate_limited_per_ip(client, admin_session, pin_token, mock_ha_client):
    entry = await _mint(client, admin_session, pin_token["id"])
    burst = ACCESS_CODE_LIMITS_PER_IP[0][1]
    for _ in range(burst):
        assert (await _redeem(client, "pin-token", "A" * 32)).status_code == 401

    resp = await _redeem(client, "pin-token", "A" * 32)
    assert resp.status_code == 429
    assert "Too many attempts" in resp.text
    # A real code is refused too while the budget is spent.
    resp = await _redeem(client, "pin-token", entry["code"])
    assert resp.status_code == 429
    assert "set-cookie" not in resp.headers


async def test_rotating_ips_still_hits_the_per_token_ceiling(
    client, admin_session, pin_token, mock_ha_client
):
    per_ip = ACCESS_CODE_LIMITS_PER_IP[0][1]
    per_token = ACCESS_CODE_LIMITS_PER_TOKEN[0][1]
    attempts = 0
    octet = 0
    while attempts < per_token:
        octet += 1
        for _ in range(per_ip):
            if attempts >= per_token:
                break
            resp = await _redeem(
                client, "pin-token", "bad", headers={"X-Forwarded-For": f"10.0.0.{octet}"}
            )
            assert resp.status_code == 401
            attempts += 1

    resp = await _redeem(client, "pin-token", "bad", headers={"X-Forwarded-For": "10.0.0.250"})
    assert resp.status_code == 429


async def test_code_budget_is_separate_from_the_pin_budget(
    client, admin_session, pin_token, mock_ha_client
):
    """A guest who fumbled the keypad can still open the link they were sent."""
    from app.routers.guest import PIN_ATTEMPT_LIMITS_PER_IP

    entry = await _mint(client, admin_session, pin_token["id"])
    for _ in range(PIN_ATTEMPT_LIMITS_PER_IP[0][1] + 1):
        await client.post("/g/pin-token/pin", data={"pin": "0000"})
    assert (await _redeem(client, "pin-token", entry["code"])).status_code == 303


# ---------------------------------------------------------------------------
# The matcher
# ---------------------------------------------------------------------------

def test_match_access_code_checks_every_candidate():
    code = guest_pin.generate_access_code()
    other = guest_pin.generate_access_code()
    candidates = [
        ("id-other", guest_pin.hash_access_code(other)),
        ("id-mine", guest_pin.hash_access_code(code)),
    ]
    assert guest_pin.match_access_code(code, candidates) == "id-mine"
    assert guest_pin.match_access_code(other, candidates) == "id-other"
    assert guest_pin.match_access_code(guest_pin.generate_access_code(), candidates) is None
    assert guest_pin.match_access_code(code, []) is None


@pytest.mark.parametrize("bad", ["", "short", "A" * 31, "A" * 33, "!" * 32, "A" * 31 + "="])
def test_match_access_code_refuses_malformed_input(bad):
    # Even a candidate whose hash is the malformed input's own digest.
    assert guest_pin.match_access_code(bad, [("x", guest_pin.hash_access_code(bad))]) is None
