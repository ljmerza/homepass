"""Admin API router."""
import asyncio
import ipaddress
import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app import database as db
from app.auth import INGRESS_SENTINEL, SESSION_COOKIE, require_admin, verify_password
from app.config import settings
from app import geoip
from app import guest_pin
from app import ha_client
from app import schedule
from app.models import (
    ACCESS_CODE_LABEL_MAX,
    AccessCodeCreateRequest,
    AdminLoginRequest,
    DISPLAY_NAME_MAX,
    ENTITY_OPTION_KEYS,
    EntityMetaRequest,
    EntityTemplateCreateRequest,
    NEVER_EXPIRES_SECONDS,
    SUPPORTED_DOMAINS,
    TEMPLATE_NAME_MAX,
    TokenCreateRequest,
    TokenDeviceBindingRequest,
    TokenPinRequest,
    TokenRememberPinRequest,
    TokenScheduleRequest,
    TokenUpdateEntitiesRequest,
    TokenUpdateExpiryRequest,
)
from app.rate_limiter import RateLimiter

router = APIRouter(prefix="/admin")

# Admin session lifetime — 24 hours, hardcoded like Uptime Kuma / Dockge.
ADMIN_SESSION_TTL = 86400

# "Remember me" session lifetime — 7 days. Applied to both the DB row's
# expires_at and the cookie max_age so neither can outlive the other.
ADMIN_SESSION_TTL_REMEMBER = 604800

# CSRF: Admin routes are protected by SameSite=strict cookie. The slug-based
# guest auth acts as a bearer token — no additional CSRF token needed.

# M-24: Rate limiting on admin login (5 failed attempts/min/IP)
_login_limiter = RateLimiter()

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@router.post("/login")
async def login(body: AdminLoginRequest, request: Request, response: Response) -> dict:
    if not settings.admin_password:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Login disabled — use HA sidebar")

    # Rate limit login attempts by IP
    client_ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    allowed = await _login_limiter.check(f"login:{client_ip}", 5)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts")

    if body.username != settings.admin_username or not await verify_password(body.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    is_https = (
        request.url.scheme == "https"
        or forwarded_proto == "https"
    )
    ttl = ADMIN_SESSION_TTL_REMEMBER if body.remember else ADMIN_SESSION_TTL
    session_id = await db.create_admin_session(ttl_seconds=ttl)
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        samesite="strict",
        secure=is_https,
        max_age=ttl,
    )
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response, session_id: str = Depends(require_admin)) -> dict:
    if session_id == INGRESS_SENTINEL:
        return {"ok": True}
    await db.delete_admin_session(session_id)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

# 128 bits of urandom, hex. The slug is the guest's whole credential when no
# PIN is set, so it is generated here and nowhere else — creation and rotation
# must not be able to drift apart in how unguessable a link is.
def _generate_slug() -> str:
    return secrets.token_hex(16)


def _normalise_starts_at(starts_at: int | None) -> int | None:
    """A scheduled start, or None when there is nothing to wait for.

    A start already in the past is "active now", which is what None means, so
    it is folded rather than stored. That keeps every pending test in the app a
    single `starts_at > now` comparison with no second case for a stale value.
    """
    if starts_at is None or starts_at <= int(time.time()):
        return None
    return starts_at


def _expires_at_from(expires_in_seconds: int, starts_at: int | None) -> int:
    """Turn a requested validity into an absolute expiry.

    NEVER_EXPIRES_SECONDS is an absolute sentinel (2099), not a duration, and
    every "no expiration" test in the app compares against it exactly — so it
    is returned untouched rather than added to anything.

    Anything else is a duration, and it is measured from the moment the guest
    can first use the link, not from the moment the admin pressed Create. A
    3-day token minted a week before check-in has to be three days of access;
    anchoring it to creation would expire it four days before the guest
    arrived, which is the whole reason a scheduled start needed one.
    """
    if expires_in_seconds == NEVER_EXPIRES_SECONDS:
        return NEVER_EXPIRES_SECONDS
    now = int(time.time())
    anchor = starts_at if starts_at and starts_at > now else now
    return anchor + expires_in_seconds


def _resolve_expiry(
    expires_in_seconds: int | None,
    expires_at: int | None,
    starts_at: int | None,
) -> int:
    """The absolute expiry a create or renew asked for, from either field.

    Exactly one of the two must be present. Checked here rather than in the
    request model because a model-level rejection echoes the whole body back in
    the 422, and on a create the whole body carries the PIN.

    An absolute expires_at is a calendar fact — a check-out time — so it is
    taken as written and never re-anchored to the start the way a duration is.
    It does have to be in the future, and after the start if there is one: a
    link whose last moment precedes its first is not a schedule, it is a typo,
    and storing it would mint a token that is born dead.
    """
    if (expires_in_seconds is None) == (expires_at is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Provide exactly one of expires_in_seconds or expires_at",
        )
    if expires_in_seconds is not None:
        return _expires_at_from(expires_in_seconds, starts_at)
    if expires_at == NEVER_EXPIRES_SECONDS:
        return NEVER_EXPIRES_SECONDS
    if expires_at <= int(time.time()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="expires_at must be in the future",
        )
    if starts_at and expires_at <= starts_at:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="expires_at must be after starts_at",
        )
    return expires_at


def _row_to_response(row: Any, entity_ids: list[str] | None = None,
                     entity_meta: dict[str, dict[str, Any]] | None = None) -> dict:
    ip_raw = row["ip_allowlist"]
    ip_list = json.loads(ip_raw) if ip_raw else None
    country_raw = row["country_allowlist"]
    if entity_ids is not None:
        count = len(entity_ids)
    elif "entity_count" in row.keys():
        count = row["entity_count"]
    else:
        count = 0
    return {
        "id": row["id"],
        "slug": row["slug"],
        "label": row["label"],
        "created_at": row["created_at"],
        # None on every token that starts immediately, which is all of them
        # unless an admin scheduled one. The dashboard reads it to decide
        # whether a card is Scheduled rather than Active.
        "starts_at": row["starts_at"],
        "expires_at": row["expires_at"],
        "revoked": bool(row["revoked"]),
        "last_accessed": row["last_accessed"],
        "ip_allowlist": ip_list,
        "country_allowlist": json.loads(country_raw) if country_raw else None,
        "entity_count": count,
        "entity_ids": entity_ids,
        "entity_meta": entity_meta,
        # Whether, never what — the PIN is stored as a bcrypt hash and there is
        # no path that returns it or the hash to the dashboard.
        "has_pin": bool(row["pin_hash"]),
        # None on every token without a weekly pattern. Parsed with the same
        # function the guest gate uses, so the dashboard shows the windows the
        # gate is actually enforcing.
        "access_windows": schedule.windows_from_row(row["access_windows"]),
        # max_uses None is unlimited, and uses_remaining is None with it.
        "max_uses": row["max_uses"],
        "use_count": row["use_count"],
        "uses_remaining": (
            max(0, row["max_uses"] - row["use_count"]) if row["max_uses"] is not None else None
        ),
        "remember_pin": bool(row["remember_pin"]),
        # Whether the link is locked to one device, and when that device
        # claimed it (None while unclaimed). The claim's secret hash never
        # leaves the server, for the same reason the PIN hash does not.
        "device_binding": bool(row["device_binding"]),
        "device_bound_at": row["device_bound_at"],
    }


def _activity_row_to_response(row: Any) -> dict:
    return {
        "timestamp": row["timestamp"],
        "activity": row["event_type"],
        "token_label": row["token_label"],
        "target_entity_id": row["entity_id"],
        "service": row["service"],
        "ip_address": row["ip_address"],
    }


@router.get("/tokens")
async def list_tokens(_: str = Depends(require_admin)) -> list[dict]:
    rows = await db.list_tokens()
    return [_row_to_response(r) for r in rows]


@router.get("/activity")
async def list_activity(
    limit: int = Query(default=50, ge=1, le=200),
    _: str = Depends(require_admin),
) -> list[dict]:
    rows = await db.list_access_logs(limit=limit)
    return [_activity_row_to_response(r) for r in rows]


@router.post("/tokens", status_code=status.HTTP_201_CREATED)
async def create_token(
    body: TokenCreateRequest,
    request: Request,
    _: str = Depends(require_admin),
) -> dict:
    # Validate IP CIDR list if provided
    if body.ip_allowlist:
        for cidr in body.ip_allowlist:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Invalid CIDR: {cidr}",
                )

    countries = await _clean_country_allowlist(body.country_allowlist)

    slug = body.slug or _generate_slug()
    starts_at = _normalise_starts_at(body.starts_at)
    expires_at = _resolve_expiry(body.expires_in_seconds, body.expires_at, starts_at)

    # Ensure slug uniqueness
    existing = await db.get_token_by_slug(slug)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Slug '{slug}' already exists",
        )

    row = await db.create_token(
        label=body.label,
        slug=slug,
        entity_ids=body.entity_ids,
        expires_at=expires_at,
        ip_allowlist=body.ip_allowlist,
        entity_meta=_clean_entity_meta(body.entity_meta),
        pin_hash=await _hash_pin_or_none(body.pin),
        starts_at=starts_at,
        access_windows=_windows_or_none(body.access_windows),
        max_uses=body.max_uses,
        remember_pin=body.remember_pin,
        device_binding=body.device_binding,
        country_allowlist=countries,
    )
    entity_ids = await db.get_token_entities(row["id"])
    return _row_to_response(row, entity_ids)


def _windows_or_none(windows: list[Any] | None) -> list[dict[str, Any]] | None:
    """The validated windows as plain dicts, or None for "any time".

    An empty list is folded to None here, so the stored column has one
    spelling for "no weekly pattern". The guest gate reads a stored empty list
    as a schedule that never opens — the fail-closed answer for a corrupted
    value — and an admin clearing the windows must not land in that state.
    """
    if not windows:
        return None
    return [w.model_dump() for w in windows]


async def _clean_country_allowlist(codes: list[str] | None) -> list[str] | None:
    """Upper-case, de-duplicate and check a country allowlist. Empty means none.

    Checked against the countries the installed GeoIP database actually has
    addresses for, not a static ISO list: "UK" is a plausible typo for "GB",
    and a code the database never returns would refuse every guest, silently,
    on a link that looked fine. No database at all is refused for the same
    reason — the link could never be opened from outside the home network.
    """
    if not codes:
        return None
    cleaned = list(dict.fromkeys(c.strip().upper() for c in codes if c and c.strip()))
    if not cleaned:
        return None
    known = await geoip.known_countries()
    if known is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Country allowlists need the GeoIP database, which is not installed",
        )
    unknown = [c for c in cleaned if c not in known]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown country code: {', '.join(unknown)} (use ISO codes such as GB, US)",
        )
    return cleaned


async def _hash_pin_or_none(value: Any) -> str | None:
    """Validate a submitted PIN and hash it. Blank or None means 'no PIN'.

    The rejection message describes the policy without echoing the value — the
    PIN must not turn up in a response body, and an admin API error is a
    response body like any other.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or not guest_pin.is_valid_pin(value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"PIN must be {guest_pin.PIN_MIN_LENGTH}-{guest_pin.PIN_MAX_LENGTH} digits"
            ),
        )
    return await guest_pin.hash_pin(value)


def _clean_name(value: Any) -> str | None:
    """Trim and cap a display name. Blank means 'no override'."""
    if not isinstance(value, str):
        return None
    return value.strip()[:DISPLAY_NAME_MAX] or None


def _clean_options(value: Any) -> dict[str, Any] | None:
    """Keep only allow-listed option keys, coerced to bool."""
    if not isinstance(value, dict):
        return None
    cleaned = {k: bool(v) for k, v in value.items() if k in ENTITY_OPTION_KEYS and v}
    return cleaned or None


def _clean_entity_meta(
    meta: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]] | None:
    """Normalise the per-entity override blob the dashboard posts.

    require_proximity is read from the top level of each entry, never from
    `options` — it is stored in its own column because the command path enforces
    it, and letting it arrive inside the presentation blob would blur exactly
    the line that column exists to keep. require_local_network is handled the
    same way for the same reason.
    """
    if not meta:
        return None
    cleaned = {}
    for eid, m in meta.items():
        if not isinstance(m, dict):
            continue
        name = _clean_name(m.get("display_name"))
        opts = _clean_options(m.get("options"))
        gated = bool(m.get("require_proximity"))
        local_only = bool(m.get("require_local_network"))
        if name or opts or gated or local_only:
            cleaned[eid] = {
                "display_name": name,
                "options": opts,
                "require_proximity": gated,
                "require_local_network": local_only,
            }
    return cleaned or None


@router.patch("/tokens/{token_id}/entity-meta")
async def set_entity_meta(
    token_id: str,
    body: EntityMetaRequest,
    _: str = Depends(require_admin),
) -> dict:
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    name = _clean_name(body.display_name)
    opts = _clean_options(body.options)

    updated = await db.set_entity_meta(
        token_id, body.entity_id, name, opts, body.require_proximity,
        body.require_local_network,
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Entity not on this token",
        )

    await ha_client.invalidate_entity_cache(token_id)
    return {
        "entity_id": body.entity_id,
        "display_name": name,
        "options": opts or {},
        "require_proximity": body.require_proximity,
        "require_local_network": body.require_local_network,
    }


@router.get("/tokens/{token_id}")
async def get_token(token_id: str, _: str = Depends(require_admin)) -> dict:
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    entity_ids = await db.get_token_entities(token_id)
    meta = await db.get_token_entity_meta(token_id)
    return _row_to_response(row, entity_ids, meta)


@router.patch("/tokens/{token_id}/entities")
async def update_token_entities(
    token_id: str,
    body: TokenUpdateEntitiesRequest,
    _: str = Depends(require_admin),
) -> dict:
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if row["revoked"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot edit entities on a revoked token",
        )
    await db.update_token_entities(
        token_id, body.entity_ids, _clean_entity_meta(body.entity_meta)
    )
    await ha_client.invalidate_entity_cache(token_id)
    entity_ids = await db.get_token_entities(token_id)
    meta = await db.get_token_entity_meta(token_id)
    row = await db.get_token_by_id(token_id)
    return _row_to_response(row, entity_ids, meta)


@router.patch("/tokens/{token_id}/expiry")
async def update_token_expiry(
    token_id: str,
    body: TokenUpdateExpiryRequest,
    _: str = Depends(require_admin),
) -> dict:
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    # Same anchor as creation: extending a token that has not started yet buys
    # the guest that much access, measured from their check-in. Anchoring to
    # now instead would hand a still-pending token an expiry it might already
    # have passed by the time the link began working.
    new_expires = _resolve_expiry(
        body.expires_in_seconds, body.expires_at, _normalise_starts_at(row["starts_at"])
    )
    await db.update_token_expiry(token_id, new_expires)
    # Un-revoke if the token was revoked (admin is explicitly renewing it)
    if row["revoked"]:
        await db.unrevoke_token(token_id)
    # Same reasoning for a use-limited link that has spent every use: Renew is
    # the dashboard's action for a dead card, and a spent link is one. Only a
    # spent one — extending a link with uses left must not hand back the ones
    # already made.
    if row["max_uses"] is not None and row["use_count"] >= row["max_uses"]:
        await db.reset_token_uses(token_id)
    row = await db.get_token_by_id(token_id)
    return _row_to_response(row)


@router.patch("/tokens/{token_id}/schedule")
async def update_token_schedule(
    token_id: str,
    body: TokenScheduleRequest,
    _: str = Depends(require_admin),
) -> dict:
    """Replace a token's timing: start, end, weekly windows and use limit.

    The same rules as creation apply — a start in the past means "now", the
    end must be in the future and after the start — so an edit cannot build a
    schedule that creation would refuse.

    A revoked token is refused rather than quietly rescheduled, as Activate
    Now refuses one: revoking is the stronger statement, and Renew exists for
    undoing it.

    Every connected guest tab is told to re-check. One on the countdown may
    have been given an earlier start, one that is live may have just lost its
    window, and neither would otherwise learn it until its own boundary.
    """
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if row["revoked"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot reschedule a revoked token",
        )
    starts_at = _normalise_starts_at(body.starts_at)
    expires_at = _resolve_expiry(None, body.expires_at, starts_at)
    await db.update_token_schedule(
        token_id,
        starts_at=starts_at,
        expires_at=expires_at,
        access_windows=_windows_or_none(body.access_windows),
        max_uses=body.max_uses,
        reset_uses=body.reset_uses,
    )
    await ha_client.broadcast_schedule_changed(token_id)
    row = await db.get_token_by_id(token_id)
    return _row_to_response(row)


@router.get("/timezone")
async def get_timezone(_: str = Depends(require_admin)) -> dict:
    """The zone weekly windows are evaluated in, for the dashboard to label.

    `source` is "setting" when the add-on option pins it, "home_assistant"
    when it is HA's configured zone, and None when neither can be read — in
    which case windowed links refuse access until one can, and the dashboard
    says so.
    """
    name, source = await schedule.house_zone_name()
    return {"timezone": name, "source": source}


@router.patch("/tokens/{token_id}/pin")
async def update_token_pin(
    token_id: str,
    body: TokenPinRequest,
    _: str = Depends(require_admin),
) -> dict:
    """Set, replace, or clear the token's PIN.

    There is no read side. Changing or clearing the PIN also invalidates every
    guest PIN session for the token, because those cookies are signed with a key
    derived from the hash this writes — and it retires every PIN-free access
    link on the token, in the same write (see db.set_token_pin).
    """
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    pin_hash = await _hash_pin_or_none(body.pin)
    await db.set_token_pin(token_id, pin_hash)
    return {"has_pin": pin_hash is not None}


@router.patch("/tokens/{token_id}/remember-pin")
async def update_token_remember_pin(
    token_id: str,
    body: TokenRememberPinRequest,
    _: str = Depends(require_admin),
) -> dict:
    """Choose whether a correct PIN is remembered on the guest's device.

    Allowed on a token with no PIN: the setting is kept and applies once one is
    set. Turning it off signs out guests holding a remembered session, so the
    change means what the admin expects straight away rather than a day later.
    """
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await db.set_token_remember_pin(token_id, body.remember_pin)
    return {"remember_pin": body.remember_pin}


# ---------------------------------------------------------------------------
# PIN-free access links
# ---------------------------------------------------------------------------
# A link of the form /g/<slug>?c=<code> that lets a guest past the PIN without
# typing it. The code is returned exactly once — in the response that minted or
# rotated it — and stored only as a hash, so the list endpoint reports labels
# and timestamps, never a usable link.

# Per token. Every "Copy link without PIN" mints a fresh link, and a cap keeps
# an admin who clicks it habitually from accumulating an unbounded set of live
# credentials they have forgotten about. Twenty is far past one per device.
MAX_ACCESS_CODES_PER_TOKEN = 20


def _clean_access_code_label(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip()[:ACCESS_CODE_LABEL_MAX] or None


async def _token_for_access_codes(token_id: str) -> Any:
    """The token, refusing one that has nothing for an access link to bypass."""
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not row["pin_hash"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This token has no PIN — its normal link already opens without one",
        )
    if row["revoked"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot create access links on a revoked token",
        )
    return row


@router.get("/tokens/{token_id}/access-codes")
async def list_access_codes(token_id: str, _: str = Depends(require_admin)) -> list[dict]:
    if not await db.get_token_by_id(token_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return await db.list_access_codes(token_id)


@router.post("/tokens/{token_id}/access-codes", status_code=status.HTTP_201_CREATED)
async def create_access_code(
    token_id: str,
    body: AccessCodeCreateRequest,
    _: str = Depends(require_admin),
) -> dict:
    """Mint a PIN-free link. The response is the only place the code appears."""
    await _token_for_access_codes(token_id)
    if await db.count_access_codes(token_id) >= MAX_ACCESS_CODES_PER_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This token already has {MAX_ACCESS_CODES_PER_TOKEN} links without PIN — "
                "revoke one first"
            ),
        )
    code = guest_pin.generate_access_code()
    entry = await db.create_access_code(
        token_id, guest_pin.hash_access_code(code), _clean_access_code_label(body.label)
    )
    return {**entry, "code": code}


@router.post("/tokens/{token_id}/access-codes/{code_id}/rotate")
async def rotate_access_code(
    token_id: str, code_id: str, _: str = Depends(require_admin)
) -> dict:
    """Replace one link with a new one under the same label.

    The old link stops working, and so does every device it had already let in
    — rotation is for a link that reached the wrong person, and a rotation that
    left them signed in would not have dealt with that.
    """
    await _token_for_access_codes(token_id)
    code = guest_pin.generate_access_code()
    entry = await db.rotate_access_code(token_id, code_id, guest_pin.hash_access_code(code))
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return {**entry, "code": code}


@router.delete("/tokens/{token_id}/access-codes/{code_id}")
async def delete_access_code(
    token_id: str, code_id: str, _: str = Depends(require_admin)
) -> dict:
    """Revoke one link, signing out the devices it let in.

    Unlike minting, this works whatever state the token is in: taking a
    credential away must never be refused.
    """
    if not await db.get_token_by_id(token_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not await db.delete_access_code(token_id, code_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return {"ok": True}


@router.patch("/tokens/{token_id}/device-binding")
async def update_device_binding(
    token_id: str,
    body: TokenDeviceBindingRequest,
    _: str = Depends(require_admin),
) -> dict:
    """Turn single-device binding on or off for an existing link.

    Both directions clear any claim, and both hang up the link's open streams.
    Turning it on for a link a guest is already using sends their tab back to
    the page, which now asks them to claim it — they are the likeliest first
    device, and the claim is theirs for one tap. Turning it off is the same
    push the other way: their page reloads without the lock.
    """
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await db.set_device_binding(token_id, body.enabled)
    await ha_client.broadcast_device_unbound(token_id)
    row = await db.get_token_by_id(token_id)
    return _row_to_response(row)


@router.post("/tokens/{token_id}/unbind")
async def unbind_device(token_id: str, _: str = Depends(require_admin)) -> dict:
    """Release a device-bound link's claim so the next device to claim it gets it.

    The recovery path for a guest who changed phones, cleared their cookies, or
    claimed the link inside a chat app's built-in browser. Binding stays on; only
    the claim goes. POST, like revoke and rotate — it changes state.

    The device that held the link is hung up at once rather than on its next
    request, and it can claim again like any other device — Unbind is "let the
    right phone in", not "ban this one". If the wrong person has the link,
    Rotate Link is the tool: it releases the claim and retires the URL together.
    """
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not row["device_binding"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This token is not locked to a device",
        )
    await db.clear_device_binding(token_id)
    await ha_client.broadcast_device_unbound(token_id)
    row = await db.get_token_by_id(token_id)
    return _row_to_response(row)


@router.post("/tokens/{token_id}/revoke")
async def revoke_token(token_id: str, _: str = Depends(require_admin)) -> dict:
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await db.revoke_token(token_id)
    # Notify connected SSE clients
    if not row["revoked"]:
        await ha_client.broadcast_token_expired(token_id)
    return {"ok": True}


@router.post("/tokens/{token_id}/activate")
async def activate_token(token_id: str, _: str = Depends(require_admin)) -> dict:
    """Drop a scheduled token's remaining delay so its link works right now.

    POST, like revoke and rotate — it changes state and is not idempotent.

    The expiry is left exactly where it is. It was anchored to the start the
    admin picked, and that end is a calendar fact — a check-out time, say — not
    a duration owed from the moment this was pressed. Starting early lengthens
    the window; it does not slide it.

    A revoked token is refused rather than quietly brought back: revoking is the
    stronger statement of the two, and Renew already exists for undoing it.
    """
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if row["revoked"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot activate a revoked token",
        )
    if not row["starts_at"] or row["starts_at"] <= int(time.time()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This token is not scheduled — it is already active",
        )

    await db.activate_token_now(token_id)
    # A guest tab sitting on the countdown has no reason to look again until
    # the start time it was told about, so push. Its stream is the one guest
    # connection a pending token is allowed to hold, and this is what it holds
    # it for.
    await ha_client.broadcast_token_activated(token_id)
    row = await db.get_token_by_id(token_id)
    return _row_to_response(row)


@router.post("/tokens/{token_id}/rotate-slug")
async def rotate_token_slug(token_id: str, _: str = Depends(require_admin)) -> dict:
    """Mint a fresh slug for an existing token, retiring the old link.

    POST, like revoke — this changes state and is not idempotent, and revoke was
    deliberately moved off DELETE for the same reason.

    The response carries the new link only. The old slug is not echoed back and
    not logged: it is a credential that has just been retired, and writing it
    into a response body or a log line would outlive the rotation that was
    supposed to end it.

    Everything except the slug survives — entities and their overrides, expiry,
    the PIN, and the access log, which is keyed on token id. Two things do not:
    a guest holding a PIN session for the old link has to enter the PIN again,
    because that cookie is scoped Path=/g/<old-slug> and the browser will never
    send it to the new one; the token's PIN-free access links are deleted,
    since each one embeds the old slug; and a device claim is released, for the
    same cookie-path reason. That is the intended outcome — rotation exists to
    hand the same access to a different person.
    """
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    # 128 bits makes a collision unreachable, but the slug column is UNIQUE and
    # an insert that lost the lottery would be a 500, so retry rather than trust.
    for _attempt in range(5):
        new_slug = _generate_slug()
        if not await db.get_token_by_slug(new_slug):
            break
    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not generate a unique slug",
        )

    await db.rotate_token_slug(token_id, new_slug)
    # An SSE stream opened on the old slug is validated once, at connect, and
    # then runs until the token expires — so hang it up explicitly. Every other
    # guest endpoint re-reads the slug per request and is already dead.
    await ha_client.broadcast_token_expired(token_id)
    row = await db.get_token_by_id(token_id)
    return _row_to_response(row)


@router.delete("/tokens/{token_id}")
async def delete_token(token_id: str, _: str = Depends(require_admin)) -> dict:
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await ha_client.broadcast_token_expired(token_id)
    await db.delete_token(token_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Entity templates
# ---------------------------------------------------------------------------
# A named, reusable entity selection for the picker. There is no update side:
# a template is small enough that re-saving it under the same name after a
# delete is simpler than an edit flow nobody asked for.

@router.get("/templates")
async def list_entity_templates(_: str = Depends(require_admin)) -> list[dict]:
    return await db.list_entity_templates()


@router.post("/templates", status_code=status.HTTP_201_CREATED)
async def create_entity_template(
    body: EntityTemplateCreateRequest,
    _: str = Depends(require_admin),
) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Template name is required",
        )
    # Trimming can only shorten, so the Field cap already holds — but the check
    # is here too because the cap is the thing a caller must not be able to slip.
    if len(name) > TEMPLATE_NAME_MAX:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Template name must be at most {TEMPLATE_NAME_MAX} characters",
        )
    if await db.get_entity_template_by_name(name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Template '{name}' already exists",
        )
    return await db.create_entity_template(name, body.entity_ids)


@router.delete("/templates/{template_id}")
async def delete_entity_template(template_id: str, _: str = Depends(require_admin)) -> dict:
    if not await db.get_entity_template(template_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await db.delete_entity_template(template_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# HA entity list proxy
# ---------------------------------------------------------------------------

@router.get("/ha/entities")
async def ha_entities(
    include_labels: bool = False,
    _: str = Depends(require_admin),
) -> Any:
    """List the entities guests may be given, optionally with HA labels.

    Without include_labels the response is the bare entity list it has always
    been, and no registry read happens. With it, the response becomes an
    envelope carrying the same list (each entity gaining `labels`) plus the
    label catalogue, so the picker gets both halves of a label filter in one
    round trip. Labels come from HA's registries over the WebSocket API; when
    those cannot be read the envelope still arrives, with labels_available
    false and every label list empty, and the picker hides its filter.
    """
    if include_labels:
        # The registry read is independent of /api/states, so overlap them —
        # an unreachable or slow registry must not add to how long the picker
        # waits for its entities.
        states, registry = await asyncio.gather(
            ha_client.get_states(),
            ha_client.get_label_registry(),
            return_exceptions=True,
        )
        if isinstance(states, BaseException):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Home Assistant unreachable")
        if isinstance(registry, BaseException):
            registry = None
    else:
        registry = None
        try:
            states = await ha_client.get_states()
        except Exception:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Home Assistant unreachable")

    # Only return entities whose domain guests can either control or view.
    entity_labels = (registry or {}).get("entity_labels", {})
    entities = [
        {
            "entity_id": s["entity_id"],
            "friendly_name": s.get("attributes", {}).get("friendly_name", s["entity_id"]),
            "domain": domain,
            "state": s["state"],
        }
        for s in states
        if (domain := s["entity_id"].split(".")[0]) in SUPPORTED_DOMAINS
    ]
    if not include_labels:
        return entities

    for entity in entities:
        entity["labels"] = entity_labels.get(entity["entity_id"], [])
    return {
        "entities": entities,
        "labels": (registry or {}).get("labels", []),
        "labels_available": registry is not None,
    }
