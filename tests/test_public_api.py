"""Public REST API (/api/v1).

The properties under test:

  * the API does not exist until it is enabled with a long-enough key — every
    route answers 404 until then;
  * the X-API-Key header is the only credential: an admin session or ingress
    does not unlock /api/v1, and a missing, wrong or non-ASCII key is a 401;
  * the key check is rate limited per IP, and guesses spend the allowance;
  * every action goes through the admin router's own logic, so the validation
    (CIDRs, PINs, slugs) and the side effects (hanging up guest streams,
    invalidating the entity cache) are the dashboard's;
  * a PIN never comes back in a response, including a 422.

Real routing, real DB, real bcrypt, real rate limiter; only ha_client is mocked.
"""
import json
import time
from unittest.mock import patch

import pydantic
import pytest

import app.ingress
from app import api_auth
from app import database as db
from app.config import Settings, settings
from app.models import NEVER_EXPIRES_SECONDS

KEY = "k" * 40
HEADERS = {"X-API-Key": KEY}
INGRESS_PREFIX = "/api/hassio_ingress/abc123"
PIN = "4821"


@pytest.fixture(autouse=True)
def _reset_api_limiter():
    """Module-level limiter, same cross-test pollution as the others in conftest."""
    api_auth._api_limiter._windows.clear()


@pytest.fixture
def api_on():
    with patch.object(settings, "api_enabled", True), patch.object(settings, "api_token", KEY):
        yield


def _body(**overrides):
    body = {
        "label": "Cleaner",
        "entity_ids": ["light.living_room", "lock.front_door"],
        "expires_in_seconds": 3600,
    }
    body.update(overrides)
    return body


async def _create(client, **overrides) -> dict:
    resp = await client.post("/api/v1/tokens", json=_body(**overrides), headers=HEADERS)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Enablement and settings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("GET", "/api/v1/tokens"),
    ("POST", "/api/v1/tokens"),
    ("GET", "/api/v1/tokens/whatever"),
])
async def test_disabled_api_is_not_there(client, mock_ha_client, test_db, admin_session, method, path):
    """Off by default. Even the correct key and an admin session find nothing."""
    with patch.object(settings, "api_token", KEY):
        resp = await client.request(method, path, headers=HEADERS, json=_body(), cookies=admin_session)
    assert resp.status_code == 404


async def test_disabled_api_hides_routes_behind_malformed_json(client, mock_ha_client, test_db):
    """A bad JSON body fails before dependencies run; it still must not reveal
    that the route exists."""
    resp = await client.post(
        "/api/v1/tokens", content=b"{not json", headers={**HEADERS, "Content-Type": "application/json"}
    )
    assert resp.status_code == 404


async def test_enabled_flag_with_short_key_stays_disabled(client, mock_ha_client, test_db):
    """The startup validator is not the only guard: an empty key is never comparable."""
    with patch.object(settings, "api_enabled", True), patch.object(settings, "api_token", ""):
        resp = await client.get("/api/v1/tokens", headers={"X-API-Key": ""})
    assert resp.status_code == 404


def _settings(**kw):
    return Settings(ha_base_url="http://ha:8123", ha_token="t", **kw)


def test_settings_reject_enabled_api_with_short_token():
    with pytest.raises(pydantic.ValidationError, match="api_token must be at least 32"):
        _settings(api_enabled=True, api_token="x" * 31)


def test_settings_accept_enabled_api_with_long_token_and_disabled_without_one():
    assert _settings(api_enabled=True, api_token="x" * 32).api_enabled
    assert not _settings().api_enabled


def test_addon_option_is_mapped_and_declared():
    """An option the add-on schema declares but run.sh does not export would be
    silently ignored — the API would never turn on in add-on mode."""
    config = open("config.yaml").read()
    run_sh = open("run.sh").read()
    translations = open("translations/en.yaml").read()
    for key, env in (("api_enabled", "API_ENABLED"), ("api_token", "API_TOKEN")):
        assert f"'{key}': '{env}'" in run_sh
        assert config.count(f"{key}:") == 2  # options + schema
        assert f"{key}:" in translations
    # Masked in the add-on UI like the admin password.
    assert 'api_token: "password?"' in config


# ---------------------------------------------------------------------------
# Authentication and rate limiting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [
    {},
    {"X-API-Key": "wrong"},
    {"X-API-Key": KEY[:-1]},
    {"X-API-Key": KEY + "k"},
])
async def test_missing_or_wrong_key_is_401(client, mock_ha_client, test_db, api_on, headers):
    resp = await client.get("/api/v1/tokens", headers=headers)
    assert resp.status_code == 401


async def test_non_ascii_key_is_401_not_500(client, mock_ha_client, test_db, api_on):
    """compare_digest raises on non-ASCII str; the compare is done on bytes.
    Starlette decodes header bytes as latin-1, so raw UTF-8 arrives as non-ASCII."""
    resp = await client.get("/api/v1/tokens", headers={"X-API-Key": "é".encode() * 20})
    assert resp.status_code == 401


async def test_correct_key_is_accepted(client, mock_ha_client, test_db, api_on, sample_token):
    resp = await client.get("/api/v1/tokens", headers=HEADERS)
    assert resp.status_code == 200
    assert [t["slug"] for t in resp.json()] == ["test-token"]


async def test_admin_session_does_not_unlock_the_api(client, mock_ha_client, test_db, api_on, admin_session):
    resp = await client.get("/api/v1/tokens", cookies=admin_session)
    assert resp.status_code == 401


async def test_ingress_does_not_unlock_the_api(client, mock_ha_client, test_db, api_on):
    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", "fake-supervisor-token"):
        resp = await client.get("/api/v1/tokens", headers={"X-Ingress-Path": INGRESS_PREFIX})
    assert resp.status_code == 401


async def test_key_guesses_spend_the_rate_limit(client, mock_ha_client, test_db, api_on):
    """Counted before the key is checked: after the allowance is gone even the
    right key is refused, so guessing buys nothing a legitimate caller would not
    also pay for."""
    with patch.object(api_auth, "API_RATE_LIMIT_PER_MINUTE", 3):
        for _ in range(3):
            resp = await client.get("/api/v1/tokens", headers={"X-API-Key": "wrong"})
            assert resp.status_code == 401
        resp = await client.get("/api/v1/tokens", headers=HEADERS)
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

async def test_create_with_duration(client, mock_ha_client, test_db, api_on):
    before = int(time.time())
    token = await _create(client)
    assert before + 3600 <= token["expires_at"] <= int(time.time()) + 3600
    assert token["entity_ids"] == ["light.living_room", "lock.front_door"]
    assert token["has_pin"] is False
    assert token["starts_at"] is None
    assert len(token["slug"]) == 32  # the admin path's 128-bit slug


async def test_create_with_absolute_expiry(client, mock_ha_client, test_db, api_on):
    expires_at = int(time.time()) + 7200
    token = await _create(client, expires_in_seconds=None, expires_at=expires_at)
    assert token["expires_at"] == expires_at
    row = await db.get_token_by_id(token["id"])
    assert row["expires_at"] == expires_at


async def test_create_with_never_expires_sentinel(client, mock_ha_client, test_db, api_on):
    token = await _create(client, expires_in_seconds=None, expires_at=NEVER_EXPIRES_SECONDS)
    assert token["expires_at"] == NEVER_EXPIRES_SECONDS


async def test_create_duration_is_anchored_to_scheduled_start(client, mock_ha_client, test_db, api_on):
    """Same anchoring as the dashboard: the validity starts when the link does."""
    starts_at = int(time.time()) + 86400
    token = await _create(client, starts_at=starts_at, expires_in_seconds=3600)
    assert token["starts_at"] == starts_at
    assert token["expires_at"] == starts_at + 3600


@pytest.mark.parametrize("overrides,detail", [
    ({"expires_at": int(time.time()) + 3600}, "not both"),
    ({"expires_in_seconds": None}, "Send expires_at or expires_in_seconds"),
    ({"expires_in_seconds": None, "expires_at": int(time.time()) - 10}, "in the future"),
    ({"expires_in_seconds": None, "expires_at": int(time.time()) + 3600,
      "starts_at": int(time.time()) + 7200}, "after starts_at"),
])
async def test_create_rejects_bad_expiry(client, mock_ha_client, test_db, api_on, overrides, detail):
    resp = await client.post("/api/v1/tokens", json=_body(**overrides), headers=HEADERS)
    assert resp.status_code == 422
    assert detail in resp.json()["detail"]
    assert await db.list_tokens() == []


async def test_create_reuses_admin_validation(client, mock_ha_client, test_db, api_on, sample_token):
    resp = await client.post("/api/v1/tokens", json=_body(ip_allowlist=["not-a-cidr"]), headers=HEADERS)
    assert resp.status_code == 422
    assert "Invalid CIDR" in resp.json()["detail"]

    resp = await client.post("/api/v1/tokens", json=_body(slug="test-token"), headers=HEADERS)
    assert resp.status_code == 409


async def test_create_with_pin_never_returns_it(client, mock_ha_client, test_db, api_on):
    resp = await client.post("/api/v1/tokens", json=_body(pin=PIN), headers=HEADERS)
    assert resp.status_code == 201
    assert resp.json()["has_pin"] is True
    assert PIN not in resp.text


@pytest.mark.parametrize("overrides", [
    {"pin": "12ab"},                               # rejected by the PIN policy
    {"pin": "98765432", "label": None},            # a missing field: default 422 echoes the body
    {"pin": "98765432", "expires_at": "tomorrow"},  # a field-level type error
])
async def test_rejected_create_does_not_echo_the_pin(client, mock_ha_client, test_db, api_on, overrides):
    body = {k: v for k, v in _body(**overrides).items() if v is not None}
    resp = await client.post("/api/v1/tokens", json=body, headers=HEADERS)
    assert resp.status_code == 422
    assert overrides["pin"] not in resp.text


async def test_admin_validation_errors_keep_their_default_shape(client, mock_ha_client, test_db, admin_session):
    """The input-stripping handler is scoped to /api/; the dashboard's 422s are
    exactly what they were."""
    resp = await client.post("/admin/tokens", json={"label": "x"}, cookies=admin_session)
    assert resp.status_code == 422
    assert all("input" in err for err in resp.json()["detail"])


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

async def test_get_returns_entities_and_meta(client, mock_ha_client, test_db, api_on):
    token = await _create(client, entity_meta={
        "lock.front_door": {"display_name": "Front", "require_proximity": True},
    })
    resp = await client.get(f"/api/v1/tokens/{token['id']}", headers=HEADERS)
    assert resp.status_code == 200
    meta = resp.json()["entity_meta"]
    assert meta["lock.front_door"]["display_name"] == "Front"
    assert meta["lock.front_door"]["require_proximity"] is True


async def test_list_omits_entity_lists(client, mock_ha_client, test_db, api_on):
    await _create(client)
    [listed] = (await client.get("/api/v1/tokens", headers=HEADERS)).json()
    assert listed["entity_count"] == 2
    assert listed["entity_ids"] is None


@pytest.mark.parametrize("method,suffix", [
    ("GET", ""), ("PATCH", ""), ("DELETE", ""),
    ("POST", "/revoke"), ("POST", "/renew"), ("POST", "/activate"),
    ("POST", "/rotate-slug"), ("POST", "/duplicate"),
])
async def test_unknown_token_is_404(client, mock_ha_client, test_db, api_on, method, suffix):
    body = {"expires_in_seconds": 60} if method != "DELETE" and method != "GET" else None
    resp = await client.request(method, f"/api/v1/tokens/nope{suffix}", json=body, headers=HEADERS)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

async def test_patch_label_entities_expiry(client, mock_ha_client, test_db, api_on):
    token = await _create(client)
    new_expiry = int(time.time()) + 9000
    resp = await client.patch(
        f"/api/v1/tokens/{token['id']}",
        json={"label": "Plumber", "entity_ids": ["switch.heater"], "expires_at": new_expiry},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["label"] == "Plumber"
    assert body["entity_ids"] == ["switch.heater"]
    assert body["expires_at"] == new_expiry
    # A guest page caches its entity list; changing it has to drop that cache.
    mock_ha_client["invalidate_entity_cache"].assert_awaited_with(token["id"])


async def test_patch_meta_alone_keeps_the_entity_list(client, mock_ha_client, test_db, api_on):
    token = await _create(client)
    resp = await client.patch(
        f"/api/v1/tokens/{token['id']}",
        json={"entity_meta": {"lock.front_door": {"require_proximity": True}}},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["entity_ids"] == ["light.living_room", "lock.front_door"]
    assert await db.get_proximity_entity_ids(token["id"]) == {"lock.front_door"}


async def test_patch_pin_absent_set_and_clear(client, mock_ha_client, test_db, api_on):
    token = await _create(client)
    url = f"/api/v1/tokens/{token['id']}"

    resp = await client.patch(url, json={"pin": PIN}, headers=HEADERS)
    assert resp.json()["has_pin"] is True
    assert PIN not in resp.text

    # Absent leaves it alone.
    resp = await client.patch(url, json={"label": "Other"}, headers=HEADERS)
    assert resp.json()["has_pin"] is True

    # Explicit null clears it.
    resp = await client.patch(url, json={"pin": None}, headers=HEADERS)
    assert resp.json()["has_pin"] is False


async def test_patch_validates_everything_before_writing(client, mock_ha_client, test_db, api_on):
    token = await _create(client)
    resp = await client.patch(
        f"/api/v1/tokens/{token['id']}",
        json={"label": "Changed", "entity_ids": ["switch.heater"], "pin": "nope"},
        headers=HEADERS,
    )
    assert resp.status_code == 422
    assert "nope" not in resp.text
    row = await db.get_token_by_id(token["id"])
    assert row["label"] == "Cleaner"
    assert await db.get_token_entities(token["id"]) == ["light.living_room", "lock.front_door"]


async def test_patch_refuses_entity_edit_on_revoked_token(client, mock_ha_client, test_db, api_on):
    token = await _create(client)
    await db.revoke_token(token["id"])
    resp = await client.patch(
        f"/api/v1/tokens/{token['id']}", json={"entity_ids": ["switch.heater"]}, headers=HEADERS
    )
    assert resp.status_code == 400


async def test_patch_expiry_does_not_unrevoke(client, mock_ha_client, test_db, api_on):
    """Only /renew brings a revoked token back."""
    token = await _create(client)
    await db.revoke_token(token["id"])
    resp = await client.patch(
        f"/api/v1/tokens/{token['id']}", json={"expires_in_seconds": 7200}, headers=HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["revoked"] is True


# ---------------------------------------------------------------------------
# Lifecycle actions
# ---------------------------------------------------------------------------

async def test_revoke_hangs_up_guests_and_kills_the_link(client, mock_ha_client, test_db, api_on):
    token = await _create(client)
    resp = await client.post(f"/api/v1/tokens/{token['id']}/revoke", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["revoked"] is True
    mock_ha_client["broadcast_token_expired"].assert_awaited_with(token["id"])
    assert (await client.get(f"/g/{token['slug']}/state")).status_code in (401, 403, 410)


@pytest.mark.parametrize("use_absolute", [False, True])
async def test_renew_unrevokes_with_either_expiry_form(client, mock_ha_client, test_db, api_on, use_absolute):
    token = await _create(client)
    await db.revoke_token(token["id"])
    target = int(time.time()) + 50000
    body = {"expires_at": target} if use_absolute else {"expires_in_seconds": 50000}
    resp = await client.post(f"/api/v1/tokens/{token['id']}/renew", json=body, headers=HEADERS)
    assert resp.status_code == 200
    renewed = resp.json()
    assert renewed["revoked"] is False
    assert abs(renewed["expires_at"] - target) <= 2


async def test_renew_requires_an_expiry(client, mock_ha_client, test_db, api_on):
    token = await _create(client)
    resp = await client.post(f"/api/v1/tokens/{token['id']}/renew", json={}, headers=HEADERS)
    assert resp.status_code == 422


async def test_activate_starts_a_scheduled_token(client, mock_ha_client, test_db, api_on):
    token = await _create(client, starts_at=int(time.time()) + 86400)
    resp = await client.post(f"/api/v1/tokens/{token['id']}/activate", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["starts_at"] is None
    assert resp.json()["expires_at"] == token["expires_at"]
    mock_ha_client["broadcast_token_activated"].assert_awaited_with(token["id"])


async def test_activate_refuses_an_unscheduled_token(client, mock_ha_client, test_db, api_on):
    token = await _create(client)
    resp = await client.post(f"/api/v1/tokens/{token['id']}/activate", headers=HEADERS)
    assert resp.status_code == 400


async def test_rotate_slug(client, mock_ha_client, test_db, api_on):
    token = await _create(client)
    resp = await client.post(f"/api/v1/tokens/{token['id']}/rotate-slug", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["slug"] != token["slug"]
    assert await db.get_token_by_slug(token["slug"]) is None
    mock_ha_client["broadcast_token_expired"].assert_awaited_with(token["id"])


async def test_delete(client, mock_ha_client, test_db, api_on):
    token = await _create(client)
    resp = await client.delete(f"/api/v1/tokens/{token['id']}", headers=HEADERS)
    assert resp.status_code == 204
    assert await db.get_token_by_id(token["id"]) is None
    mock_ha_client["broadcast_token_expired"].assert_awaited_with(token["id"])


# ---------------------------------------------------------------------------
# Duplicate
# ---------------------------------------------------------------------------

async def test_duplicate_copies_access_not_identity(client, mock_ha_client, test_db, api_on):
    source = await _create(
        client,
        pin=PIN,
        starts_at=int(time.time()) + 86400,
        ip_allowlist=["10.0.0.0/8"],
        entity_meta={"lock.front_door": {"display_name": "Front", "require_proximity": True}},
    )
    resp = await client.post(f"/api/v1/tokens/{source['id']}/duplicate", headers=HEADERS)
    assert resp.status_code == 201
    copy = resp.json()

    assert copy["id"] != source["id"]
    assert copy["slug"] != source["slug"]
    assert copy["label"] == "Cleaner copy"
    assert copy["entity_ids"] == source["entity_ids"]
    assert copy["ip_allowlist"] == ["10.0.0.0/8"]
    # The proximity gate is an access control; a copy must not be looser.
    assert await db.get_proximity_entity_ids(copy["id"]) == {"lock.front_door"}
    assert copy["entity_meta"]["lock.front_door"]["display_name"] == "Front"
    # One guest's stay: not carried over.
    assert copy["has_pin"] is False
    assert copy["starts_at"] is None
    assert abs(copy["expires_at"] - (int(time.time()) + 86400)) <= 2


async def test_duplicate_of_never_expiring_token_never_expires(client, mock_ha_client, test_db, api_on):
    source = await _create(client, expires_in_seconds=None, expires_at=NEVER_EXPIRES_SECONDS)
    resp = await client.post(f"/api/v1/tokens/{source['id']}/duplicate", json={}, headers=HEADERS)
    assert resp.json()["expires_at"] == NEVER_EXPIRES_SECONDS


async def test_duplicate_overrides(client, mock_ha_client, test_db, api_on):
    source = await _create(client)
    expires_at = int(time.time()) + 4000
    resp = await client.post(
        f"/api/v1/tokens/{source['id']}/duplicate",
        json={"label": "Next guest", "slug": "next-guest", "pin": PIN, "expires_at": expires_at},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    copy = resp.json()
    assert (copy["label"], copy["slug"], copy["has_pin"], copy["expires_at"]) == (
        "Next guest", "next-guest", True, expires_at,
    )
    assert PIN not in resp.text


def test_schema_body_models_do_not_constrain_the_pin():
    """A Field constraint on `pin` would put the value in a 422's `input`."""
    from app.models import ApiTokenCreateRequest, ApiTokenDuplicateRequest, ApiTokenUpdateRequest
    for model in (ApiTokenCreateRequest, ApiTokenUpdateRequest, ApiTokenDuplicateRequest):
        assert model.model_fields["pin"].metadata == []
    json.dumps(ApiTokenCreateRequest.model_json_schema())  # schema builds
