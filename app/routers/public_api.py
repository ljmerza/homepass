"""Public REST API (/api/v1) and its Swagger UI (/api/docs).

Everything here is off unless the add-on options api_enabled and api_token are
set; until then every route answers 404, the docs included.

The API is a second front door to the admin token actions, not a second
implementation of them. Handlers call the admin router's own functions — the
creation path with its CIDR, slug, PIN and entity-meta validation, the revoke
that hangs up live guest streams, the rotation that retries on a slug collision
— so a rule tightened for the dashboard is tightened here in the same edit.
What is genuinely new is only what the dashboard has no need for: an absolute
expiry, and one PATCH that changes several things at once.

Every token field the dashboard can set is reachable here too — the weekly
windows and use limit (on create, and through /schedule), remember-PIN and the
device lock (on create and PATCH), the country allowlist (on create, like the
IP allowlist), links without PIN, and Unbind — each through the same admin
handler the dashboard calls, so the guest-side effects are the dashboard's.
"""
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Security, status
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import database as db
from app import ha_client
from app import schedule
from app.api_auth import (
    API_PRINCIPAL,
    api_enabled,
    api_key_header,
    enforce_api_rate_limit,
    keys_match,
    require_api_key,
)
from app.auth import require_admin
from app.context import base_context
from app.models import (
    NEVER_EXPIRES_SECONDS,
    AccessCodeCreateRequest,
    ApiAccessCode,
    ApiAccessCodeCreated,
    ApiTokenCreateRequest,
    ApiTokenDuplicateRequest,
    ApiTokenRenewRequest,
    ApiTokenResponse,
    ApiTokenUpdateRequest,
    TokenDeviceBindingRequest,
    TokenRememberPinRequest,
    TokenScheduleRequest,
)
from app.routers import admin

API_PREFIX = "/api/v1"

router = APIRouter(
    prefix=API_PREFIX,
    tags=["tokens"],
    dependencies=[Depends(require_api_key)],
)

# The docs and the schema are for the owner, not the internet. Port 5880 is
# often the port guest links are served on, reverse-proxied to the outside, and
# a public Swagger page would hand anyone a map of the admin surface. So the
# page wants an admin session (or ingress), and the schema accepts either that
# or the API key, which is how a client generator would fetch it.
docs_router = APIRouter(prefix="/api", include_in_schema=False)

templates = Jinja2Templates(directory="templates")

# Default validity of a duplicate when the request names none — the same 24
# hours the dashboard's Duplicate pre-selects.
DUPLICATE_DEFAULT_SECONDS = 86400

LABEL_MAX = 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _resolve_expiry(body: Any, starts_at: int | None) -> int | None:
    """Absolute expiry from whichever of expires_at / expires_in_seconds was sent.

    None when neither was — the caller decides whether that is an error. A
    duration goes through the dashboard's own anchoring (from the scheduled
    start, not from now) so the two cannot disagree about what "3 days" means.
    An absolute time is taken as given, and so has to land after the moment the
    link would start working: an expiry before the start is a link that never
    works, which is a mistake to report rather than a token to store.
    """
    has_at = body.expires_at is not None
    has_in = body.expires_in_seconds is not None
    if has_at and has_in:
        raise _unprocessable("Send either expires_at or expires_in_seconds, not both")
    if has_in:
        return admin._expires_at_from(body.expires_in_seconds, starts_at)
    if not has_at:
        return None
    if body.expires_at == NEVER_EXPIRES_SECONDS:
        return NEVER_EXPIRES_SECONDS
    now = int(time.time())
    if starts_at and starts_at > now:
        if body.expires_at <= starts_at:
            raise _unprocessable("expires_at must be after starts_at")
    elif body.expires_at <= now:
        raise _unprocessable("expires_at must be in the future")
    return body.expires_at


async def _row_or_404(token_id: str):
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return row


async def _full_token(token_id: str) -> dict:
    """The token with its entity list and overrides, as GET returns it.

    Every write answers with this rather than the admin handler's own response,
    which varies (some return {"ok": true}, some omit the entities): an
    automation should get back the same shape it would read.
    """
    return await admin.get_token(token_id, _=API_PRINCIPAL)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

@router.get("/tokens", response_model=list[ApiTokenResponse])
async def list_tokens() -> list[dict]:
    """List every token, newest first. Entity lists are omitted; fetch one
    token for its `entity_ids` and `entity_meta`."""
    return await admin.list_tokens(_=API_PRINCIPAL)


@router.post("/tokens", response_model=ApiTokenResponse, status_code=status.HTTP_201_CREATED)
async def create_token(body: ApiTokenCreateRequest) -> dict:
    """Create a token. Send exactly one of `expires_at` (epoch seconds) or
    `expires_in_seconds`. A duration is measured from `starts_at` when the
    token is scheduled, from now otherwise."""
    starts_at = admin._normalise_starts_at(body.starts_at)
    expires_at = _resolve_expiry(body, starts_at)
    if expires_at is None:
        raise _unprocessable("Send expires_at or expires_in_seconds")
    created = await admin._create_token(body, expires_at=expires_at)
    return await _full_token(created["id"])


@router.get("/tokens/{token_id}", response_model=ApiTokenResponse)
async def get_token(token_id: str) -> dict:
    """One token, with its entity list and per-entity overrides."""
    return await _full_token(token_id)


@router.patch("/tokens/{token_id}", response_model=ApiTokenResponse)
async def update_token(token_id: str, body: ApiTokenUpdateRequest) -> dict:
    """Change any of label, entities, expiry, PIN, remember-PIN and the
    device lock in one call.

    Everything is validated before anything is written, so a bad PIN does not
    leave the entity list changed. The expiry change leaves a revocation in
    place — use /renew to bring a revoked token back.
    """
    row = await _row_or_404(token_id)
    sent = body.model_fields_set

    expires_at = _resolve_expiry(body, row["starts_at"])
    pin_hash = await admin._hash_pin_or_none(body.pin) if "pin" in sent else None
    entities_change = body.entity_ids is not None or body.entity_meta is not None
    if entities_change and row["revoked"]:
        # Same refusal as the dashboard's entity editor.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot edit entities on a revoked token",
        )

    if body.label is not None:
        await db.update_token_label(token_id, body.label)
    if entities_change:
        # entity_meta on its own restyles the entities already on the token;
        # it never adds one, because the list it is applied to is the stored one.
        entity_ids = body.entity_ids or await db.get_token_entities(token_id)
        await db.update_token_entities(
            token_id, entity_ids, admin._clean_entity_meta(body.entity_meta)
        )
        await ha_client.invalidate_entity_cache(token_id)
    if expires_at is not None:
        await db.update_token_expiry(token_id, expires_at)
    if "pin" in sent:
        # Also ends every guest PIN session for the token, and retires its
        # links without PIN — see set_token_pin — and, like the dashboard's
        # PIN change, hangs up the streams those sessions hold open.
        await db.set_token_pin(token_id, pin_hash)
        await ha_client.broadcast_access_changed(token_id)
    if body.remember_pin is not None:
        await admin.update_token_remember_pin(
            token_id, TokenRememberPinRequest(remember_pin=body.remember_pin), _=API_PRINCIPAL
        )
    if body.device_binding is not None and body.device_binding != bool(row["device_binding"]):
        # The dashboard's toggle: clears any claim and hangs up open streams.
        await admin.update_device_binding(
            token_id, TokenDeviceBindingRequest(enabled=body.device_binding), _=API_PRINCIPAL
        )
    return await _full_token(token_id)


@router.put("/tokens/{token_id}/schedule", response_model=ApiTokenResponse)
async def replace_schedule(token_id: str, body: TokenScheduleRequest) -> dict:
    """Replace the token's timing: `starts_at`, an absolute `expires_at`,
    `access_windows` and `max_uses`, plus `reset_uses`.

    A full replacement — an omitted field takes its default, so leaving out
    `access_windows` clears them. Open guest tabs are told to re-check.
    """
    return await admin.update_token_schedule(token_id, body, _=API_PRINCIPAL)


@router.post("/tokens/{token_id}/unbind", response_model=ApiTokenResponse)
async def unbind_device(token_id: str) -> dict:
    """Release a device-locked token's claim so the next device can take it."""
    await admin.unbind_device(token_id, _=API_PRINCIPAL)
    return await _full_token(token_id)


@router.get("/tokens/{token_id}/access-codes", response_model=list[ApiAccessCode])
async def list_access_codes(token_id: str) -> list[dict]:
    """The token's links without PIN: labels and timestamps, never the codes."""
    return await admin.list_access_codes(token_id, _=API_PRINCIPAL)


@router.post(
    "/tokens/{token_id}/access-codes",
    response_model=ApiAccessCodeCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_access_code(
    token_id: str, body: AccessCodeCreateRequest | None = None
) -> dict:
    """Mint a link that skips the PIN. The response is the only place the code
    appears. The token must have a PIN and not be revoked."""
    return await admin.create_access_code(
        token_id, body or AccessCodeCreateRequest(), _=API_PRINCIPAL
    )


@router.post(
    "/tokens/{token_id}/access-codes/{code_id}/rotate",
    response_model=ApiAccessCodeCreated,
)
async def rotate_access_code(token_id: str, code_id: str) -> dict:
    """Replace one link with a new code under the same label, signing out the
    devices the old one let in."""
    return await admin.rotate_access_code(token_id, code_id, _=API_PRINCIPAL)


@router.delete(
    "/tokens/{token_id}/access-codes/{code_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_access_code(token_id: str, code_id: str) -> Response:
    """Revoke one link, signing out the devices it let in."""
    await admin.delete_access_code(token_id, code_id, _=API_PRINCIPAL)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_token(token_id: str) -> Response:
    """Delete a token and its access log for good. To stop a link but keep the
    record, revoke it instead."""
    await admin.delete_token(token_id, _=API_PRINCIPAL)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/tokens/{token_id}/revoke", response_model=ApiTokenResponse)
async def revoke_token(token_id: str) -> dict:
    """Stop the link working now. Open guest tabs are told immediately."""
    await admin.revoke_token(token_id, _=API_PRINCIPAL)
    return await _full_token(token_id)


@router.post("/tokens/{token_id}/renew", response_model=ApiTokenResponse)
async def renew_token(token_id: str, body: ApiTokenRenewRequest) -> dict:
    """Set a new expiry and clear a revocation — the dashboard's Renew."""
    row = await _row_or_404(token_id)
    expires_at = _resolve_expiry(body, row["starts_at"])
    if expires_at is None:
        raise _unprocessable("Send expires_at or expires_in_seconds")
    await admin._renew_token(row, expires_at)
    return await _full_token(token_id)


@router.post("/tokens/{token_id}/activate", response_model=ApiTokenResponse)
async def activate_token(token_id: str) -> dict:
    """Start a scheduled token now. The expiry does not move."""
    await admin.activate_token(token_id, _=API_PRINCIPAL)
    return await _full_token(token_id)


@router.post("/tokens/{token_id}/rotate-slug", response_model=ApiTokenResponse)
async def rotate_token_slug(token_id: str) -> dict:
    """Issue a new link for the token and retire the old one."""
    await admin.rotate_token_slug(token_id, _=API_PRINCIPAL)
    return await _full_token(token_id)


@router.post(
    "/tokens/{token_id}/duplicate",
    response_model=ApiTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
async def duplicate_token(token_id: str, body: ApiTokenDuplicateRequest | None = None) -> dict:
    """Create a new token from an existing one's entities, allowlists, weekly
    windows, use limit, remember-PIN and device-lock settings.

    The slug, PIN and scheduled start are not copied; pass new ones in the
    body if the copy needs them. The use count starts at zero, and a device
    claim or links without PIN never carry over.
    """
    body = body or ApiTokenDuplicateRequest()
    row = await _row_or_404(token_id)
    windows = schedule.windows_from_row(row["access_windows"])
    if row["access_windows"] is not None and not windows:
        # The guest gate reads an unreadable schedule as "never open", but the
        # create path folds an empty list to "any time" — copying it as-is
        # would turn a closed link into an unrestricted one.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The source token's weekly windows are unreadable; set them with /schedule first",
        )

    starts_at = admin._normalise_starts_at(body.starts_at)
    expires_at = _resolve_expiry(body, starts_at)
    if expires_at is None:
        expires_at = (
            NEVER_EXPIRES_SECONDS
            if row["expires_at"] == NEVER_EXPIRES_SECONDS
            else admin._expires_at_from(DUPLICATE_DEFAULT_SECONDS, starts_at)
        )

    # The per-entity overrides are copied, which the dashboard's Duplicate does
    # not do. They include the proximity requirement, an access control: a copy
    # that silently dropped it would be a looser link than the one it was made
    # from, and an API caller has no picker in front of them to notice.
    create = ApiTokenCreateRequest(
        label=body.label or f"{row['label']} copy"[:LABEL_MAX],
        slug=body.slug,
        entity_ids=await db.get_token_entities(token_id),
        entity_meta=await db.get_token_entity_meta(token_id),
        ip_allowlist=json.loads(row["ip_allowlist"]) if row["ip_allowlist"] else None,
        starts_at=starts_at,
        pin=body.pin,
        # Access policy, carried for the same reason as the overrides above: a
        # copy must not be a looser link than its source. Each is re-validated
        # by the create path — the countries against the installed database,
        # the windows by the model — so a stored value that no longer passes
        # fails the copy rather than slipping through.
        access_windows=windows,
        max_uses=row["max_uses"],
        remember_pin=bool(row["remember_pin"]),
        device_binding=bool(row["device_binding"]),
        country_allowlist=(
            json.loads(row["country_allowlist"]) if row["country_allowlist"] else None
        ),
    )
    created = await admin._create_token(create, expires_at=expires_at)
    return await _full_token(created["id"])


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------

async def api_validation_error_handler(request: Request, exc: RequestValidationError):
    """422s for /api/ without the `input` echo; everything else unchanged.

    FastAPI's default body echoes each failing value back, and for a missing
    field that value is the whole request body — PIN included. The dashboard's
    own fetches live with that today; an automation is more likely to write
    its responses to a log, so the API does not send it.
    """
    if not request.url.path.startswith("/api/"):
        return await request_validation_exception_handler(request, exc)
    # A malformed JSON body fails before any dependency runs, so without this a
    # disabled API would answer 422 and give away that the route exists.
    if not api_enabled():
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": "Not Found"})
    errors = [{k: v for k, v in err.items() if k != "input"} for err in exc.errors()]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": jsonable_encoder(errors)},
    )


# ---------------------------------------------------------------------------
# OpenAPI schema and Swagger UI
# ---------------------------------------------------------------------------

_schema_cache: dict | None = None


def _openapi_schema() -> dict:
    """The schema for /api/v1 and nothing else.

    Built from this router's routes rather than the app's, so it cannot pick up
    an admin or guest route by omission — a new admin endpoint would otherwise
    appear here the day someone forgot include_in_schema=False on it.
    """
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = get_openapi(
            title="HomePass API",
            version="1",
            description=(
                "Manage HomePass guest tokens from Home Assistant automations, "
                "Node-RED or any HTTP client. Authenticate with the `X-API-Key` "
                "header set to the add-on's API Token."
            ),
            routes=router.routes,
        )
    return _schema_cache


async def _is_admin(request: Request) -> bool:
    try:
        await require_admin(request)
    except HTTPException:
        return False
    return True


@docs_router.get("/openapi.json")
async def api_openapi(
    request: Request,
    api_key: str | None = Security(api_key_header),
) -> JSONResponse:
    if not api_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await enforce_api_rate_limit(request)
    if not (keys_match(api_key) or await _is_admin(request)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    schema = dict(_openapi_schema())
    # Under ingress the browser reaches this app at /api/hassio_ingress/<t>/…,
    # and Swagger UI resolves operation paths against `servers`. Naming the
    # prefix here is what makes Try it out land on the add-on instead of on
    # Home Assistant's own /api. Setting ASGI root_path instead would do the
    # same for the schema and break every other route's path matching.
    prefix = request.state.ingress_path
    if prefix:
        schema["servers"] = [{"url": prefix}]
    return JSONResponse(schema)


@docs_router.get("/docs")
async def api_docs(request: Request):
    """Swagger UI, served under the same CSP as every other page.

    FastAPI's built-in docs page loads Swagger from a CDN and boots it with an
    inline script, which would need both a CDN allowance and a script
    exception in the policy. This page loads a pinned Swagger UI from /static
    (fetched and checksummed at image build time) and boots it from a static
    file, so the policy needs no change at all — for this page or any other.
    """
    if not api_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    base = request.state.ingress_path
    if not await _is_admin(request):
        return RedirectResponse(url=f"{base}/admin/dashboard")
    ctx = base_context(request)
    ctx["openapi_url"] = f"{base}/api/openapi.json"
    return templates.TemplateResponse(request, "api_docs.html", ctx)
