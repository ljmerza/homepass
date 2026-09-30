"""Guest API router: PWA shell, state, SSE, and command proxy."""
# Security note: The slug in the URL acts as a bearer token — knowing the
# slug grants access. CSRF is mitigated by the fact that all state-changing
# operations require the slug in the URL path (not a cookie). The admin
# dashboard uses SameSite=strict cookies for CSRF protection.
import asyncio
import ipaddress
from contextlib import AsyncExitStack
import json
import logging
import re
import time
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Path, Request, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates

from app import database as db
from app import device_binding
from app import geoip
from app import guest_pin
from app import ha_client
from app import i18n
from app import local_network
from app import proximity
from app import schedule
from app.build import BUILD_VERSION, STATIC_DIR
from app.client_ip import client_ip as resolve_client_ip
from app.config import settings
from app.context import base_context
from app.models import (
    ALLOWED_SERVICES,
    CommandRequest,
    FORBIDDEN_DATA_KEYS,
    NEVER_EXPIRES_SECONDS,
    validate_light_color,
)
from app.rate_limiter import rate_limiter

router = APIRouter(prefix="/g")
logger = logging.getLogger(__name__)

# L-31: Named constant for SSE keepalive interval
SSE_KEEPALIVE_SECONDS = 25

# Global rate limits for the guest command proxy, as (window_seconds, max_requests)
# pairs per token — a request has to pass every one of them.
# Hardcoded — no comparable self-hosted app exposes per-user rate limits.
#
# A single per-minute cap cannot serve both cases here. The light colour wheel
# streams throttled updates for as long as the guest drags it, so the short
# window has to be generous: at ~4 updates/second, 300/min covers a solid minute
# of dragging plus the taps interleaved with it. A cap that loose is the wrong
# long-run budget though, so the hour window carries the real ceiling — 3000/hour
# is ten minutes at the burst rate, and clamps anything scripted to under
# 1 req/s averaged out.
COMMAND_BURST_RPM = 300
COMMAND_SUSTAINED_RPH = 3000
COMMAND_LIMITS = ((60.0, COMMAND_BURST_RPM), (3600.0, COMMAND_SUSTAINED_RPH))

# Camera stills are cheap and the UI refreshes thumbnails on a timer, so they get
# their own, looser budget under a separate limiter key — a guest watching a camera
# must not burn the command allowance that controls their lights.
CAMERA_SNAPSHOT_RPM = 120

# Each live MJPEG view holds one upstream connection to HA open for its whole
# lifetime, so this is capped per token rather than rate-limited per minute.
# Cameras stream for as long as the guest page is open, so the real consumption
# is (cameras on the token) x (open tabs) — 2 would be exhausted by a single
# 2-camera page and 429 the guest's second device.
MAX_STREAMS_PER_TOKEN = 8
_active_streams: dict[str, int] = {}
_stream_lock = asyncio.Lock()

# Budget for proximity checks that come back refused, per token, on top of the
# ordinary command limits above. Two reasons it exists:
#
# A refusal is otherwise a free oracle — a caller could binary-search the home
# coordinates out of the gate by watching which lat/long pairs come back 403.
# This slows that to a crawl rather than closing it; the position of a house
# whose guest link you already hold is not a secret worth a tighter cap.
#
# And a guest who really is away stops after a handful of taps with "too many
# location checks" instead of retrying into the main command budget forever.
#
# Only refusals are recorded, so a guest who is actually at the property never
# touches this. Someone holding the slug can exhaust it to keep the real guest
# out — but they can already exhaust COMMAND_LIMITS and deny every entity on the
# token, so it is not a new exposure.
PROXIMITY_FAILURE_LIMITS = ((60.0, 5), (3600.0, 30))

# Brute-force budget for PIN entry, as (window_seconds, max_attempts) pairs.
# Two keys, both of which an attempt has to pass, because neither alone works:
#
# Keyed on the token only, an attacker who rotates IPs still hits one shared
# ceiling — but they can also spend that ceiling to lock the real guest out.
# Keyed on the IP only, rotating addresses evades the limit entirely, and one
# NAT'd household shares a budget across unrelated tokens.
#
# So: a tight per-(token, IP) budget catches the ordinary case, and a looser
# per-token budget bounds total guesses no matter how many addresses are used.
# The per-token ceiling is deliberately well above what a guest fumbling their
# PIN needs, and still caps a 4-digit space at ~2400 guesses/day — the same
# DoS-vs-brute-force trade the command limiter already makes per token.
PIN_ATTEMPT_LIMITS_PER_IP = ((60.0, 5), (3600.0, 20))
PIN_ATTEMPT_LIMITS_PER_TOKEN = ((60.0, 15), (3600.0, 100))

# Budget for redeeming PIN-free access links (?c=<code>), same two-key shape as
# the PIN budget above and for the same reasons. It is not what keeps a code
# safe — 192 random bits are beyond guessing at any rate — so it is looser: a
# household opening one link on every phone and tablet it owns, several times
# over, never meets it. What it does bound is the work an attacker holding the
# slug can make each probe cost, and the rows it can make the server read.
# Kept apart from the PIN budget so a guest who fumbled the keypad a few times
# can still open the link they were sent.
ACCESS_CODE_LIMITS_PER_IP = ((60.0, 10), (3600.0, 60))
ACCESS_CODE_LIMITS_PER_TOKEN = ((60.0, 30), (3600.0, 300))

# entity_id arrives in a URL path here (it does not anywhere else in this app) and
# is interpolated into the upstream HA request, so it is matched against an exact
# shape rather than merely checked for membership.
_CAMERA_ENTITY_RE = re.compile(r"^camera\.[a-z0-9_]+$")

# L-8: Whitelist of allowed SSE event types
_ALLOWED_SSE_EVENTS = {
    "state_change", "token_expired", "token_activated", "schedule_changed",
    "window_closed", "reconnected", "device_unbound", "access_changed",
}

# The subset a stream opened outside a token's access — before its start time,
# or between two of its weekly windows — may forward. No member carries device
# data: one says the link is now live, one that it is gone, one that the admin
# changed its timing and the page should ask again, one that this device no
# longer holds it, and one that the PIN or a link without PIN changed and this
# device may have been signed out. state_change and reconnected are deliberately absent — a
# pending guest must not receive real Home Assistant state, and filtering here
# means the frames are never serialised rather than merely ignored by the page.
_PENDING_SSE_EVENTS = {
    "token_expired", "token_activated", "schedule_changed", "device_unbound", "access_changed",
}

# Events after which a stream hangs up. Each one means the page is about to
# reload — into the live page, the countdown or the claim screen — or has
# nothing left to show, and a stream that stayed open would keep relaying on
# the terms it was opened under rather than the current ones.
_TERMINAL_SSE_EVENTS = {
    "token_expired", "token_activated", "schedule_changed", "device_unbound", "access_changed",
}

# How often a long-lived relay — the SSE stream, a live camera view — re-runs
# the gate it passed at connect. The admin actions that take access away push
# a terminal event (or, for cameras, nothing: an MJPEG relay has no channel to
# push on), and this is the backstop behind them: a push dropped on a full
# queue, a tab that ignores it, a script that never reloads, or a change with
# no push of its own, such as a camera taken off the link. Every other guest
# route re-checks per request; without this, these two would keep relaying on
# the terms they were opened under for as long as the socket stayed up. The
# check is one token read, so its cost is per stream, not per event.
STREAM_REVALIDATE_SECONDS = 30

# M-27: Simple TTL cache for HA state list
_states_cache: list[dict] | None = None
_states_cache_ts: float = 0
STATE_CACHE_TTL = 30  # seconds
ACTIVITY_EVENT_TYPE = "homepass_activity"
ACTIVITY_SCHEMA_VERSION = 1
PAGE_LOAD_EVENT_DEBOUNCE_SECONDS = 30
_page_load_activity_ts: dict[str, float] = {}

# HA does not gate the two activity calls the same way. Firing an event is
# POST /api/events/<type>, whose view carries @require_admin in HA core, while
# the logbook entry goes out over POST /api/services/logbook/log, which has no
# such decorator. A standalone deployment whose long-lived token belongs to a
# non-admin HA user therefore runs every guest command and writes every logbook
# entry, and is refused only the event. (Add-on installs authenticate with the
# Supervisor token, so they never see it.)
#
# A refusal like that is permanent — the same token gets the same answer on
# every request, so warning once per guest page load and per command is noise
# and tells nobody how to fix it. A 401/403 latches its channel off after one
# actionable message; a timeout or a 5xx is transient and keeps the per-request
# warning, because the next request may well succeed.
_ACTIVITY_DENIED_STATUSES = frozenset({401, 403})

# A latched channel is re-probed this often, so fixing the token's permissions
# takes effect on its own instead of needing a restart. That costs one refused
# request an hour and stays quiet unless the answer changes.
ACTIVITY_DENIED_RETRY_SECONDS = 3600

# channel -> the one message an admin gets when it latches, given the status code
_ACTIVITY_DENIED_HELP = {
    "event": (
        f"Home Assistant refused the {ACTIVITY_EVENT_TYPE} event with HTTP %d and "
        "will refuse every later one: POST /api/events/ is admin-only, so HA_TOKEN "
        "has to be a long-lived access token belonging to a Home Assistant user "
        "with Administrator enabled. HomePass has stopped firing these events, so HA "
        f"automations that trigger on {ACTIVITY_EVENT_TYPE} will not run. Guest "
        "access, HomePass's own access log and the dashboard's Recent Activity are "
        "unaffected. Give the token's user Administrator and HomePass picks the "
        "events back up within an hour — no restart needed."
    ),
    "logbook": (
        "Home Assistant refused the logbook.log service call with HTTP %d and will "
        "refuse every later one: the user behind HA_TOKEN is not permitted to call "
        "it. HomePass has stopped writing them, so guest activity will not appear in "
        "the Home Assistant logbook. Guest access, HomePass's own access log and the "
        "dashboard's Recent Activity are unaffected. Fix the token's permissions "
        "and HomePass picks the entries back up within an hour — no restart needed."
    ),
}

# channel -> the per-request warning a transient failure still gets
_ACTIVITY_TRANSIENT_WARNING = {
    "event": "Failed to emit HA activity event: %s",
    "logbook": "Failed to write HA logbook activity: %s",
}

# channel -> time.monotonic() of the refusal that latched it
_activity_denied: dict[str, float] = {}


async def _get_cached_states() -> list[dict]:
    global _states_cache, _states_cache_ts
    now = time.monotonic()
    if _states_cache is not None and (now - _states_cache_ts) < STATE_CACHE_TTL:
        return _states_cache
    _states_cache = await ha_client.get_states()
    _states_cache_ts = now
    return _states_cache


templates = Jinja2Templates(directory="templates")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_ip(request: Request) -> str:
    """The guest's address. X-Forwarded-For is only believed from a trusted
    proxy (the trusted_proxies option) — see app/client_ip.py.
    """
    return resolve_client_ip(request)


def _from_home_network(request: Request) -> bool:
    """Whether this request comes from the home network.

    Uses the same client address as every other check: X-Forwarded-For only
    counts when it arrives from a trusted proxy, read right to left, so a
    client cannot earn a home address by writing one into the header.
    """
    return local_network.contains(_client_ip(request))


def _enforce_ip_allowlist(row, request: Request) -> None:
    if not row["ip_allowlist"]:
        return
    client_ip = _client_ip(request)
    allowed_cidrs: list[str] = json.loads(row["ip_allowlist"])
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid client IP")
    if not any(addr in ipaddress.ip_network(cidr, strict=False) for cidr in allowed_cidrs):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP not allowed")


async def _enforce_country_allowlist(row, request: Request) -> None:
    """Refuse a request whose address the GeoIP database does not place in one
    of the token's countries.

    Sits beside the IP allowlist and is enforced everywhere that is: every
    guest route, the page, the PIN form and the claim. Both apply when a token
    has both.

    The home network passes. A LAN address has no country to look up, and a
    guest on the house Wi-Fi opening the link directly is the last person this
    is meant to stop; the local_network_cidrs option is what says which
    addresses those are, so an install that has not set it gets no exemption.

    Fails closed: an address with no country (private space other than the home
    network, unassigned blocks, "unknown") is refused, and so is every address
    when no database is installed — an allowlist that opens when it cannot
    check is not an allowlist.
    """
    raw = row["country_allowlist"]
    if not raw:
        return
    # The whole forwarding chain, not the first entry: a forged
    # "X-Forwarded-For: 192.168.1.20" must not skip the lookup entirely.
    if _from_home_network(request):
        return
    client_ip = _client_ip(request)
    allowed: list[str] = json.loads(raw)
    country = await geoip.country_for(client_ip)
    if country is None or country not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Country not allowed")


async def _pin_gate_ok(row, request: Request) -> bool:
    """True if this token carries no PIN, or this request already proved it.

    A token with no PIN — the default — never reaches the signature check, so
    nothing about those requests changes.

    A session minted by a PIN-free access link names that link, and passes only
    while the link still exists. That one row read is what makes revoking or
    rotating a link sign out the devices it let in, rather than leaving them
    in until their cookie runs out — a revocation that only stops *new* devices
    is not the one an admin thinks they performed. Sessions from a typed PIN
    carry no link and never pay for the read.
    """
    pin_hash = row["pin_hash"]
    if not pin_hash:
        return True
    claims = guest_pin.read_session(
        request.cookies.get(guest_pin.SESSION_COOKIE),
        row["id"],
        pin_hash,
        remember=bool(row["remember_pin"]),
    )
    if claims is None:
        return False
    if claims.access_code_id is None:
        return True
    return await db.access_code_exists(row["id"], claims.access_code_id)


def _device_gate_ok(row, request: Request) -> bool:
    """True if this token is not device-bound, or this request is the bound device.

    A token with binding off — the default — returns before any cookie is read.
    A bound token nobody has claimed yet returns False: until the first device
    claims it through POST /bind, it is closed to every device, which is what
    stops a script holding only the slug from reading state without ever
    committing to being the one device.
    """
    if not row["device_binding"]:
        return True
    return device_binding.verify(
        request.cookies.get(device_binding.COOKIE), row["device_secret_hash"]
    )


def _refuse_device(row) -> None:
    """Refuse a request from a device that does not hold this token's binding.

    403, like the IP allowlist: the link is live, this device just may not use
    it. The body is a dict so the guest page can tell this refusal from every
    other 403 and reload into the server's explanation, instead of showing a
    generic "command failed" to a guest whose problem is which phone they are on.
    Nothing here says which device holds it or when it was claimed.
    """
    if row["device_secret_hash"]:
        message = "This link is already in use on another device"
    else:
        message = "This link has not been set up on this device yet"
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": message, "device": True},
    )


def _set_device_cookie(response: Response, request: Request, slug: str, secret: str) -> None:
    response.set_cookie(
        device_binding.COOKIE,
        secret,
        httponly=True,
        # Lax, not Strict, for the same reason as the PIN cookie: a guest link
        # is always arrived at from somewhere else — a message, an email — and
        # Strict withholds the cookie on exactly that cross-site navigation, so
        # the bound device would arrive looking like a stranger and be refused
        # on its own link. Lax still keeps it off cross-site POSTs. The slug is
        # the credential; this cookie only ever narrows who may use it.
        samesite="lax",
        secure=_is_https(request),
        max_age=device_binding.COOKIE_MAX_AGE_SECONDS,
        # Same scoping as the PIN cookie, for the same reasons — including the
        # ingress prefix, without which the browser would never send it back.
        path=_pin_cookie_path(request, slug),
    )


def _is_https(request: Request) -> bool:
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    return request.url.scheme == "https" or forwarded_proto == "https"


def _pin_cookie_path(request: Request, slug: str) -> str:
    """Scope the PIN cookie to one token's own URL prefix.

    Under HA ingress the app is mounted below /api/hassio_ingress/<token>, so the
    path has to carry that prefix or the browser never sends the cookie back.
    Narrowing to /g/<slug> also keeps a session for one token off the wire on
    another token's requests — cookie paths match on whole segments, so /g/abc
    is not sent for /g/abcdef. The HMAC binding in guest_pin is what actually
    enforces the scoping; this just stops the cookie travelling needlessly.
    """
    return f"{request.state.base_path}/g/{slug}"


def _set_pin_session(
    response: Response, request: Request, slug: str, row, access_code_id: str | None = None
) -> None:
    """Attach a fresh PIN session to `response` — the one cookie both a correct
    PIN and a PIN-free access link earn, so the two cannot drift apart in how
    they scope or protect it."""
    value, max_age = guest_pin.issue_session(
        row["id"],
        row["pin_hash"],
        row["expires_at"],
        remember=bool(row["remember_pin"]),
        access_code_id=access_code_id,
    )
    response.set_cookie(
        guest_pin.SESSION_COOKIE,
        value,
        httponly=True,
        # Lax, not strict: guest links are opened from a text message or an
        # email, and a strict cookie is withheld on that first cross-site
        # navigation — the guest would be re-prompted every single time. Lax
        # still withholds it from cross-site POSTs, so a forged command from
        # another origin fails the gate.
        samesite="lax",
        secure=_is_https(request),
        # None when the token does not remember the PIN: a cookie with no
        # Max-Age is a browser-session cookie, gone when the browser closes.
        max_age=max_age,
        path=_pin_cookie_path(request, slug),
    )


def _pin_page(request: Request, slug: str, error_key: str | None = None, status_code: int = 200):
    """The PIN screen, optionally with an error — named by its catalogue key,
    so it is rendered in the guest's language like the rest of the page."""
    ctx = base_context(request, i18n.GUEST)
    ctx.update({"slug": slug, "contact_message": settings.contact_message})
    if error_key:
        ctx["error_key"] = error_key
    return templates.TemplateResponse(request, "pin_entry.html", ctx, status_code=status_code)


async def _redeem_access_code(request: Request, row, slug: str, code: str):
    """Exchange a PIN-free access link for a PIN session, then drop the code.

    Every outcome that lets the guest in is a redirect to the bare /g/<slug>, so
    the code does not stay in the address bar, the tab's history, or a
    bookmark the guest makes of the app — and a PWA installed from that page
    starts from the bare link, not the code. (The Referer is already withheld
    app-wide by the security-headers middleware.)

    The code is only ever accepted here. /state, /stream, /command and the
    camera routes accept the session cookie and nothing else, so the code is
    never a bearer credential for the API, only a one-step way to earn the same
    cookie a correct PIN earns — and everything behind the gate is unchanged.

    Callers have already refused dead tokens (revoked, expired, used up) and
    applied the IP and country allowlists: a link without a PIN skips the
    keypad, not any other gate. The device lock and the schedule are met on the
    page this redirects to, and on every route after it.
    """
    clean = RedirectResponse(
        url=f"{request.state.base_path}/g/{slug}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    # No PIN to skip, or a device already past it: nothing to redeem, and not
    # worth spending the budget on. The strip still happens.
    if not row["pin_hash"] or await _pin_gate_ok(row, request):
        return clean

    ip_ok = await rate_limiter.check_multi(
        f"code:{row['id']}:{_client_ip(request)}", ACCESS_CODE_LIMITS_PER_IP
    )
    if not ip_ok or not await rate_limiter.check_multi(
        f"code:{row['id']}", ACCESS_CODE_LIMITS_PER_TOKEN
    ):
        return _pin_page(
            request, slug,
            error_key="page.pin.error_rate_limited",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    code_id = guest_pin.match_access_code(code, await db.get_access_code_hashes(row["id"]))
    if code_id is None:
        # Revoked, rotated, retired by a PIN change, or never real — the guest
        # is told only that the link no longer skips the PIN, and gets the
        # keypad, which is what they would need in every one of those cases.
        return _pin_page(
            request, slug,
            error_key="page.pin.error_link_retired",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    await db.touch_access_code(code_id)
    _set_pin_session(clean, request, slug, row, access_code_id=code_id)
    return clean


def _is_pending(row) -> bool:
    """True while a scheduled token's start time is still ahead of us.

    NULL starts_at is the ordinary case and returns False without arithmetic,
    so nothing about an unscheduled token changes.

    This is the start time alone. Whether the link works right now — which a
    weekly window also decides — is _access(); this only tells the two kinds
    of "not now" apart for the message.
    """
    starts_at = row["starts_at"]
    return bool(starts_at) and starts_at > int(time.time())


def _is_used_up(row) -> bool:
    """True once a use-limited token has spent every use.

    Treated as dead, beside revoked and expired, rather than as "not now": no
    amount of waiting brings it back, only an admin's renew does.
    """
    max_uses = row["max_uses"]
    return max_uses is not None and row["use_count"] >= max_uses


def _is_dead(row) -> bool:
    return bool(row["revoked"]) or row["expires_at"] <= int(time.time()) or _is_used_up(row)


async def _access(row) -> schedule.Access:
    """Whether this live token can be used right now, and until when.

    The house's time zone is only looked up for a token that carries weekly
    windows, so every other token decides this with no upstream call at all.
    """
    windows = schedule.windows_from_row(row["access_windows"])
    tz = await schedule.house_zone() if windows is not None else None
    return schedule.evaluate(row["starts_at"], row["expires_at"], windows, tz, int(time.time()))


def _refuse_pending(row, access: schedule.Access) -> None:
    """Refuse a request that arrived outside the token's access.

    That is before its start time, or between two of its weekly windows.
    403 rather than 410: the link is valid, it is simply not in its window,
    and 410 would tell a guest who opened it early that their link is dead.

    starts_at and opens_at ride along in the body because the caller has
    already cleared every gate that protects them — the IP allowlist and,
    where one is set, the PIN — and the page they were served states the same
    time in its banner. They are what let a tab whose own clock ran fast
    resynchronise instead of dropping into a generic error. starts_at is the
    token's own scheduled start (None once it has passed); opens_at is when it
    next works, which a weekly window can push later than that, and which is
    None when nothing opens again before the token expires.

    A windowed token whose house time zone cannot be read is refused with 503
    instead: the schedule cannot be checked, and a gate that opens when it
    cannot verify is not a gate — the same call the proximity gate makes.

    Deliberately not metered. The proximity refusal budget exists because that
    refusal is an oracle over the home's coordinates and costs an upstream zone
    read; this one is a comparison against a row already in hand, and repeating
    it reveals nothing the banner did not. A budget here would also misfire at
    exactly the wrong moment: a tab that retried while waiting would be sitting
    in a 429 at the instant its access opened.
    """
    if access.zone_unknown:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "Can't check this link's schedule right now",
                    "starts_at": None, "opens_at": None},
        )
    pending_start = _is_pending(row)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "This link is not active yet" if pending_start
            else "This link is not active right now",
            "starts_at": row["starts_at"] if pending_start else None,
            "opens_at": access.opens_at,
        },
    )


async def _validate_token(slug: str, request: Request, allow_pending: bool = False):
    """Load and validate a token by slug. Raises HTTP 410 on any issue.

    The PIN gate lives here rather than in each handler so every guest endpoint
    that reads state or performs an action inherits it, including ones added
    later. Gating only the HTML page would leave /state, /stream, /command and
    both camera endpoints reachable with nothing but the slug — the camera pair
    being the worst of it, since those relay live frames.

    The device-binding gate sits here for the same reason. A bound token checks
    the device cookie on every one of those routes, so a forwarded link that is
    refused at the page is refused just the same by a script calling /state or
    /command directly.

    The scheduled-start gate sits here for the same reason and defaults closed:
    a pending token is not an active token, so a route added later is refused
    before its start time unless it opts out. /stream is the one that does —
    it is the channel the activation push travels on, and it forwards nothing
    but lifecycle events while pending. Weekly access windows are the same
    gate: outside every window a token is pending in exactly the sense a
    not-yet-started one is, and is refused by every route the same way.

    Order matters. The PIN is checked first, so a locked token that is also
    scheduled answers "PIN required" and never "starts Tuesday": the pending
    preview names every entity on the link, and that is not something to hand
    to someone who has not proved the PIN. The device check follows the PIN, so
    a second phone holding a PIN-protected link learns nothing — not even that
    the link is claimed — until it has proved the PIN; and it precedes the
    schedule, so the preview is withheld from a device that is not the bound
    one. Revocation, expiry and a spent use limit come before all of them — a
    dead token is dead whatever its schedule said.

    In full: dead → IP allowlist → country allowlist → PIN → device → schedule,
    then, on /command only, home network → proximity per entity. The network
    gates lead because they are about where the request comes from, not who
    sent it, and refusing them says nothing about the link. The PIN precedes
    the device lock rather than following it: a device refusal confirms the
    link is claimed, which is more than a forwarded slug should learn. A PIN-free
    access link only ever stands in for the PIN step — it is redeemed on the
    page and earns the same cookie — so a link opened that way still meets the
    device lock and the schedule here, on every route.
    """
    row = await db.get_token_by_slug(slug)
    if not row:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Access unavailable")

    if _is_dead(row):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Access unavailable")

    _enforce_ip_allowlist(row, request)
    await _enforce_country_allowlist(row, request)

    if not await _pin_gate_ok(row, request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="PIN required")

    if not _device_gate_ok(row, request):
        _refuse_device(row)

    if not allow_pending:
        access = await _access(row)
        if not access.active:
            _refuse_pending(row, access)

    return row


async def _refuse_proximity(token_id: str, status_code: int, detail: str) -> None:
    """Record a refused proximity check and raise, or raise 429 once it is spent."""
    if not await rate_limiter.check_multi(f"prox:{token_id}", PROXIMITY_FAILURE_LIMITS):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many location checks — please wait a minute",
        )
    raise HTTPException(status_code=status_code, detail=detail)


async def _enforce_proximity(row, body: CommandRequest) -> None:
    """Refuse a gated entity's command unless a fresh fix puts the guest at home.

    Only entities the admin marked reach any of this, so an ungated entity on
    the same token neither needs a location nor waits on one — the lookup is a
    membership test and returns immediately when nothing is gated.

    Fails closed at every step: no fix, a stale fix, or a zone.home that cannot
    be read all refuse. A gate that opens when it cannot verify is not a gate,
    and the cost of the strict side is an admin noticing their door button stops
    working while HA is unreachable.

    Soft by nature — see app/proximity.py. The coordinates are self-reported, so
    this is friction for a casual guest, not evidence anyone is at the door.
    """
    gated = await db.get_proximity_entity_ids(row["id"])
    if body.entity_id not in gated:
        return

    token_id = row["id"]
    loc = body.location
    if loc is None:
        await _refuse_proximity(
            token_id,
            status.HTTP_400_BAD_REQUEST,
            "This control needs your location",
        )

    if not proximity.fix_is_fresh(loc.timestamp, time.time()):
        await _refuse_proximity(
            token_id,
            status.HTTP_400_BAD_REQUEST,
            "Your location is out of date — try again",
        )

    zone = await ha_client.get_home_zone()
    if zone is None:
        await _refuse_proximity(
            token_id,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Can't check your location right now",
        )

    if not proximity.is_within_zone(loc.latitude, loc.longitude, zone):
        await _refuse_proximity(
            token_id,
            status.HTTP_403_FORBIDDEN,
            "You need to be at the property to use this",
        )


async def _enforce_local_network(row, body: CommandRequest, request: Request) -> None:
    """Refuse a home-network-only entity's command from outside the home network.

    Per entity, like the proximity gate beside it, rather than one add-on-wide
    rule for a fixed list of domains. The add-on option says what the home
    network is — a fact about the house, set once. Which controls need it is a
    decision about each guest: the cleaner's front-door lock, not the lamp; the
    garage for the neighbour watering plants, but not for the house-sitter who
    may need it opened from the road. A domain list would also miss whatever it
    did not name, alarm_control_panel's disarm among them.

    Viewing is never gated: the entity's state and the page itself work from
    anywhere, and only this command path consults the flag.

    Inert while local_network_cidrs is empty, which is what "empty turns the
    feature off" has to mean for a link flagged before the option was cleared.
    Not metered like the proximity refusal: the caller already knows its own
    address, so a refusal tells it nothing, and the command limits apply.
    """
    if not local_network.is_configured():
        return
    gated = await db.get_local_network_entity_ids(row["id"])
    if body.entity_id not in gated:
        return
    if not _from_home_network(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This control only works when you are on the home network",
        )


def _activity_channel_open(channel: str) -> bool:
    """False while a channel is latched off by a refusal already explained."""
    denied_at = _activity_denied.get(channel)
    if denied_at is None:
        return True
    return (time.monotonic() - denied_at) >= ACTIVITY_DENIED_RETRY_SECONDS


def _note_activity_failure(channel: str, exc: Exception) -> None:
    """Log one activity-reporting failure, latching the permanent ones."""
    status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    if status_code in _ACTIVITY_DENIED_STATUSES:
        # Only the move into refused is logged. A re-probe that comes back
        # refused again tells the admin nothing new, and logging it would turn
        # the hourly retry into an hourly error.
        first_refusal = channel not in _activity_denied
        _activity_denied[channel] = time.monotonic()
        if first_refusal:
            logger.error(_ACTIVITY_DENIED_HELP[channel], status_code)
        return
    logger.warning(_ACTIVITY_TRANSIENT_WARNING[channel], exc)


def _note_activity_success(channel: str) -> None:
    """Unlatch a channel HA has started accepting again."""
    if _activity_denied.pop(channel, None) is not None:
        logger.info("Home Assistant is accepting HomePass %s activity again.", channel)


async def _fire_activity_event(payload: dict) -> None:
    if _activity_channel_open("event"):
        try:
            await ha_client.fire_event(ACTIVITY_EVENT_TYPE, payload)
            _note_activity_success("event")
        except Exception as exc:
            _note_activity_failure("event", exc)
    if _activity_channel_open("logbook"):
        try:
            await ha_client.logbook_log(_logbook_payload(payload))
            _note_activity_success("logbook")
        except Exception as exc:
            _note_activity_failure("logbook", exc)


def _logbook_payload(payload: dict) -> dict:
    token_label = payload["token_label"]
    if payload["activity"] == "command":
        target_entity_id = payload["target_entity_id"]
        data = {
            "name": "HomePass",
            "message": f"{token_label} used {payload['service']} on {target_entity_id}",
            "entity_id": target_entity_id,
        }
        if target_entity_id and "." in target_entity_id:
            data["domain"] = target_entity_id.split(".", 1)[0]
        return data
    return {
        "name": "HomePass",
        "message": f"{token_label} opened guest link",
    }


def _activity_payload(
    row,
    activity: str,
    target_entity_id: str | None = None,
    service: str | None = None,
) -> dict:
    return {
        "schema_version": ACTIVITY_SCHEMA_VERSION,
        "activity": activity,
        "token_label": row["label"],
        "target_entity_id": target_entity_id,
        "service": service,
    }


def _schedule_activity_event(background_tasks: BackgroundTasks, payload: dict) -> None:
    background_tasks.add_task(_fire_activity_event, payload)


def _schedule_page_load_activity(background_tasks: BackgroundTasks, row) -> None:
    now = time.monotonic()
    cutoff = now - PAGE_LOAD_EVENT_DEBOUNCE_SECONDS
    for token_id, last_emitted in list(_page_load_activity_ts.items()):
        if last_emitted < cutoff:
            del _page_load_activity_ts[token_id]
    token_id = row["id"]
    last_emitted = _page_load_activity_ts.get(token_id)
    if last_emitted is not None and (now - last_emitted) < PAGE_LOAD_EVENT_DEBOUNCE_SECONDS:
        return
    _page_load_activity_ts[token_id] = now
    _schedule_activity_event(background_tasks, _activity_payload(row, "page_load"))


# ---------------------------------------------------------------------------
# Service worker
# ---------------------------------------------------------------------------
# Served from under /g/ rather than from /static/ because a worker's default
# scope is the directory it is served from. At /static/sw.js that scope was
# /static/, which holds no pages, so no guest page was ever controlled and the
# fetch handler never ran. Here the default scope is /g/ — every guest page and
# nothing else — so the registration needs no Service-Worker-Allowed header and
# the admin UI stays outside the worker entirely.
#
# Declared above /{slug} on purpose: routes match in declaration order and the
# slug route would otherwise answer this path with the expired page. A token
# cannot collide with it either — generated slugs are hex and a custom one is
# [a-z0-9_-], neither of which can contain a dot.
SW_PATH = STATIC_DIR / "sw.js"


@router.get("/sw.js", include_in_schema=False)
async def guest_service_worker():
    """The shipped static/sw.js, with the CACHE_VERSION the build stamped into
    it. Served from the file rather than templated so the Dockerfile's sed is
    still what decides the cache name. The media type is explicit because a
    worker served as anything but JavaScript fails to register."""
    return FileResponse(SW_PATH, media_type="text/javascript")


# ---------------------------------------------------------------------------
# PWA shell
# ---------------------------------------------------------------------------

@router.get("/{slug}", response_class=HTMLResponse)
async def guest_pwa(background_tasks: BackgroundTasks, request: Request, slug: str = Path(max_length=64)):
    row = await db.get_token_by_slug(slug)
    if not row or _is_dead(row):
        ctx = base_context(request, i18n.GUEST)
        # "used" changes the wording and nothing else. It tells whoever holds
        # the slug that the link was used, which is no more than "expired"
        # tells them, and it saves a guest who tapped once from wondering
        # whether the link was ever valid.
        ctx.update({
            "slug": slug,
            "contact_message": settings.contact_message,
            "reason": "used" if row and not row["revoked"] and _is_used_up(row) else "expired",
        })
        return templates.TemplateResponse(request, "expired.html", ctx, status_code=410)

    try:
        _enforce_ip_allowlist(row, request)
        await _enforce_country_allowlist(row, request)
    except HTTPException as exc:
        ctx = base_context(request, i18n.GUEST)
        ctx.update({"slug": slug, "contact_message": settings.contact_message})
        return templates.TemplateResponse(request, "expired.html", ctx, status_code=exc.status_code)

    # A PIN-free access link. After the IP allowlist, so it skips the keypad
    # and nothing else; before the PIN gate, so even a device that is already
    # unlocked has the code stripped from its address bar. Membership, not a
    # truthy value: a bare `?c=` is still a link that needs cleaning up.
    if "c" in request.query_params:
        return await _redeem_access_code(request, row, slug, request.query_params["c"])

    # Locked tokens get the PIN screen instead of the app. Nothing is touched or
    # logged yet — an unanswered prompt is not an access, the same way a request
    # blocked by the IP allowlist above is not.
    if not await _pin_gate_ok(row, request):
        return _pin_page(request, slug)

    # Device binding. Loading this page never claims anything, because this GET
    # is exactly the request a chat app makes to build its link preview:
    # claiming here spent the one binding on WhatsApp's fetcher seconds after a
    # link was sent (reported against the fork this feature comes from). An
    # unclaimed link gets the claim screen instead, whose button POSTs to /bind
    # — preview fetchers only GET, and no User-Agent list has to be kept. The
    # claim screen names neither the link nor anything on it, so the preview
    # card a chat app builds from it leaks nothing about the home either.
    if row["device_binding"]:
        if not row["device_secret_hash"]:
            ctx = base_context(request, i18n.GUEST)
            ctx.update({"slug": slug, "contact_message": settings.contact_message})
            return templates.TemplateResponse(request, "device_claim.html", ctx)
        if not _device_gate_ok(row, request):
            # Logged, unlike the unanswered claim screen above: a refusal is
            # the one trace that tells an admin a link has reached a second
            # device — or that the guest's own browser lost its cookie — rather
            # than simply never having been opened.
            await db.log_access(
                token_id=row["id"],
                event_type="device_refused",
                ip_address=_client_ip(request),
                user_agent=request.headers.get("User-Agent"),
            )
            return _device_refused_page(request, slug)

    # A visit before the window opens is not an access: nothing is touched,
    # logged, or reported to HA, the same way an unanswered PIN prompt is not.
    # The guest gets the shape of their page and a countdown, and the real
    # page_load lands when the link activates and the tab reloads into it.
    #
    # Opening a link is never a "use" of a use-limited one either — see
    # guest_command. A chat app unfurling the URL into a preview card fetches
    # exactly this page, and must not be able to spend the guest's one use.
    access = await _access(row)
    pending = not access.active
    has_windows = row["access_windows"] is not None
    if not pending:
        await db.touch_token(row["id"])
        await db.log_access(
            token_id=row["id"],
            event_type="page_load",
            ip_address=_client_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )
        _schedule_page_load_activity(background_tasks, row)

    ctx = base_context(request, i18n.GUEST)
    ctx.update({
        "slug": slug,
        "label": row["label"],
        "expires_at": row["expires_at"],
        "contact_message": settings.contact_message,
        "never_expires": NEVER_EXPIRES_SECONDS,
        # Decided here, not in the browser: the template only emits the
        # geolocation block when this is true, so a token with nothing gated
        # renders a page that never mentions the API and can never prompt.
        # A pending page is always false — it can command nothing, so it has no
        # business asking anyone where they are.
        "requires_location": (
            False if pending else bool(await db.get_proximity_entity_ids(row["id"]))
        ),
        "pending": pending,
        # The countdown target: when the link next works. None when nothing
        # opens again before expiry, or the schedule cannot be checked — the
        # banner then says so instead of counting down to nothing.
        "starts_at": access.opens_at if pending else None,
        # Which "not now" this is, for the banner's wording only: "start"
        # before the first start time, "window" between weekly windows,
        # "none" when no window opens again before expiry, "unknown" when
        # the house's time zone cannot be read.
        "pending_reason": (
            None if not pending
            else "unknown" if access.zone_unknown
            else "none" if access.opens_at is None
            else "start" if _is_pending(row)
            else "window"
        ),
        # When the current weekly window ends, so a page left open flips back
        # to the countdown on time. Only for windowed tokens: an ordinary
        # token's end is its expiry, which the page already watches.
        "window_closes_at": access.closes_at if has_windows and not pending else None,
        # The countdown is measured against this rather than the device clock,
        # so a phone whose time is minutes out still unlocks when the server
        # says so instead of reloading early into another refusal.
        "server_now": int(time.time()),
        # The preview is built from these two and nothing else. Entity IDs give
        # the domains, and so the icons, grouping and order; the overrides give
        # the names the admin chose. No Home Assistant state is read here, and
        # none is reachable from the page until it reloads as an active one.
        "preview_entity_ids": await db.get_token_entities(row["id"]) if pending else [],
        "preview_entity_meta": await db.get_token_entity_meta(row["id"]) if pending else {},
    })
    response = templates.TemplateResponse(request, "guest_pwa.html", ctx)
    if row["device_binding"]:
        # Re-issue the cookie the bound device just presented, restarting the
        # browser's 400-day clock, so a long-lived link the guest keeps using
        # never loses its binding to cookie expiry.
        _set_device_cookie(
            response, request, slug, request.cookies[device_binding.COOKIE]
        )
    return response


def _device_refused_page(request: Request, slug: str) -> HTMLResponse:
    ctx = base_context(request, i18n.GUEST)
    ctx.update({"slug": slug, "contact_message": settings.contact_message})
    return templates.TemplateResponse(
        request, "device_refused.html", ctx, status_code=status.HTTP_403_FORBIDDEN
    )


# ---------------------------------------------------------------------------
# Device claim
# ---------------------------------------------------------------------------

@router.post("/{slug}/bind", response_class=HTMLResponse)
async def guest_bind(request: Request, slug: str = Path(max_length=64)):
    """Claim a device-bound link for the browser that asked.

    A plain form POST from the claim screen, answered with a redirect back to
    the page, the same shape as the PIN form. Split out of the page load so
    that claiming takes a deliberate tap: link-preview fetchers and mail
    scanners issue GETs, and none of them submits a form.

    Every gate in front of the page applies here too — a claim is not a way
    around the IP allowlist or the PIN — except the schedule: a guest who opens
    the link before check-in can claim it then, and the countdown they land on
    afterwards is served to their device and no other. The claim itself reveals
    nothing, so there is no reason to make them come back to do it.
    """
    row = await db.get_token_by_slug(slug)
    # _is_dead, not just revoked/expired: a use-limited link that has spent its
    # last use is as dead as an expired one, and must not be claimable.
    if not row or _is_dead(row):
        ctx = base_context(request, i18n.GUEST)
        ctx.update({"slug": slug, "contact_message": settings.contact_message})
        return templates.TemplateResponse(request, "expired.html", ctx, status_code=410)

    try:
        _enforce_ip_allowlist(row, request)
        await _enforce_country_allowlist(row, request)
    except HTTPException as exc:
        ctx = base_context(request, i18n.GUEST)
        ctx.update({"slug": slug, "contact_message": settings.contact_message})
        return templates.TemplateResponse(request, "expired.html", ctx, status_code=exc.status_code)

    back_to_page = RedirectResponse(
        url=f"{request.state.base_path}/g/{slug}",
        status_code=status.HTTP_303_SEE_OTHER,
    )

    # Not unlocked yet, or nothing to claim: the page is where either state is
    # explained, so go back to it rather than restate it here. An admin may
    # have turned binding off while the claim screen sat open.
    if not await _pin_gate_ok(row, request) or not row["device_binding"]:
        return back_to_page

    if row["device_secret_hash"]:
        # A second tap on the same device, or a back-button resubmit, lands on
        # the page it already owns. Anyone else is told the link is taken.
        if _device_gate_ok(row, request):
            return back_to_page
        return _device_refused_page(request, slug)

    secret = device_binding.new_secret()
    if not await db.claim_device_binding(row["id"], device_binding.hash_secret(secret)):
        # Lost the race to a device that claimed between our read and our
        # write, or the admin turned binding off in the same instant. Either
        # way this device holds nothing; the page says which.
        return back_to_page

    await db.log_access(
        token_id=row["id"],
        event_type="device_bound",
        ip_address=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    _set_device_cookie(back_to_page, request, slug, secret)
    return back_to_page


# ---------------------------------------------------------------------------
# PIN entry
# ---------------------------------------------------------------------------

@router.post("/{slug}/pin", response_class=HTMLResponse)
async def guest_pin_submit(
    request: Request,
    slug: str = Path(max_length=64),
    # No max_length here on purpose: a Form() constraint failure returns a 422
    # whose body echoes the rejected `input`, which would put the PIN in a
    # response. Length is checked below, where the answer is a generic error.
    pin: str = Form(default=""),
):
    """Check a submitted PIN and, on success, hand back a session cookie.

    POST rather than a query parameter so the PIN never reaches browser history,
    a Referer header, or the reverse proxy's access log.
    """
    row = await db.get_token_by_slug(slug)
    if not row or _is_dead(row):
        ctx = base_context(request, i18n.GUEST)
        ctx.update({"slug": slug, "contact_message": settings.contact_message})
        return templates.TemplateResponse(request, "expired.html", ctx, status_code=410)

    try:
        _enforce_ip_allowlist(row, request)
        await _enforce_country_allowlist(row, request)
    except HTTPException as exc:
        ctx = base_context(request, i18n.GUEST)
        ctx.update({"slug": slug, "contact_message": settings.contact_message})
        return templates.TemplateResponse(request, "expired.html", ctx, status_code=exc.status_code)

    pin_hash = row["pin_hash"]
    if not pin_hash:
        # Nothing to unlock. Same redirect a correct PIN gets, so a guest sitting
        # on a bookmarked PIN page still lands on the app after an admin clears
        # the PIN. That the token has none is already plain from GET /g/<slug>.
        return RedirectResponse(
            url=f"{request.state.base_path}/g/{slug}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # Per-(token, IP) first: a single address that has already burned its own
    # budget is turned away without also spending the token-wide one.
    ip_ok = await rate_limiter.check_multi(
        f"pin:{row['id']}:{_client_ip(request)}", PIN_ATTEMPT_LIMITS_PER_IP
    )
    if not ip_ok or not await rate_limiter.check_multi(
        f"pin:{row['id']}", PIN_ATTEMPT_LIMITS_PER_TOKEN
    ):
        ctx = base_context(request, i18n.GUEST)
        ctx.update({
            "slug": slug,
            "contact_message": settings.contact_message,
            "error_key": "page.pin.error_rate_limited",
        })
        return templates.TemplateResponse(
            request, "pin_entry.html", ctx,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    if not guest_pin.is_valid_pin(pin) or not await guest_pin.verify_pin(pin, pin_hash):
        ctx = base_context(request, i18n.GUEST)
        ctx.update({
            "slug": slug,
            "contact_message": settings.contact_message,
            "error_key": "page.pin.error_incorrect",
        })
        return templates.TemplateResponse(
            request, "pin_entry.html", ctx,
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    response = RedirectResponse(
        url=f"{request.state.base_path}/g/{slug}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    _set_pin_session(response, request, slug, row)
    return response


# ---------------------------------------------------------------------------
# Dynamic PWA manifest
# ---------------------------------------------------------------------------

@router.get("/{slug}/manifest.json")
async def guest_manifest(request: Request, slug: str = Path(max_length=64)):
    bp = request.state.base_path
    # Same ?v= stamp the templates put on their asset tags. An installed PWA
    # re-reads the manifest and its icons rarely, so an unversioned icon URL is
    # the longest-lived stale asset of the lot.
    v = BUILD_VERSION
    manifest = {  # colors must match static/input.css
        "name": settings.app_name,
        "short_name": settings.app_name[:12],
        # In the language of the browser installing the app, like its pages.
        "description": i18n.translator(i18n.GUEST, i18n.guest_language(request))(
            "page.manifest_description"
        ),
        "start_url": f"{bp}/g/{slug}",
        "scope": f"{bp}/g/{slug}",
        "display": "standalone",
        "background_color": settings.brand_bg,
        "theme_color": settings.brand_primary,
        "orientation": "portrait",
        "icons": [
            {"src": f"{bp}/static/icons/icon-192.png?v={v}", "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": f"{bp}/static/icons/icon-512.png?v={v}", "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
            {"src": f"{bp}/static/icons/icon-maskable-192.png?v={v}", "sizes": "192x192",
             "type": "image/png", "purpose": "maskable"},
            {"src": f"{bp}/static/icons/icon-maskable-512.png?v={v}", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    }
    return JSONResponse(manifest)


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

@router.get("/{slug}/state")
async def guest_state(request: Request, slug: str = Path(max_length=64)):
    row = await _validate_token(slug, request)
    entity_ids = await db.get_token_entities(row["id"])

    allowed = set(entity_ids)
    all_states = await _get_cached_states()
    states = {}
    for s in all_states:
        eid = s.get("entity_id", "")
        if eid in allowed:
            states[eid] = s
    for eid in entity_ids:
        if eid not in states:
            states[eid] = {"entity_id": eid, "state": "unavailable", "attributes": {}}

    # Presentation overrides ride alongside the states rather than being merged
    # into them, so the raw HA attributes the UI reads stay untouched. The
    # per-entity require_proximity flag comes through here too — the guest UI
    # uses it to mark which controls will ask for a location, and it is
    # false everywhere on a token with no gated entity.
    meta = await db.get_token_entity_meta(row["id"])
    return {"entities": entity_ids, "states": states, "entity_meta": meta}


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------

async def _still_admitted(slug: str, request: Request, allow_pending: bool) -> bool:
    """Whether a request that opened a long-lived relay would still be let in."""
    try:
        await _validate_token(slug, request, allow_pending=allow_pending)
    except HTTPException:
        return False
    return True


async def _event_generator(
    token_id: str, slug: str, request: Request, starts_at: int | None = None,
    closes_at: int | None = None,
) -> AsyncIterator[str]:
    """Relay a token's events to one guest tab.

    `starts_at` is set only when the stream was opened outside the token's
    access — before its start time or between weekly windows — and is the
    moment it next opens. Until that moment passes the generator forwards the
    lifecycle subset and nothing else, so no device state leaves the server —
    and it watches the clock itself, emitting token_activated when the boundary
    arrives. That is what unlocks a tab whose own timer never fired because the
    device was asleep or offline across it: the push is waiting on the socket
    when it wakes, and a tab that missed the socket entirely reconnects into a
    stream that is no longer pending.

    `closes_at` is the mirror image, set only on a live stream of a windowed
    token: the moment its current window shuts. The generator emits
    window_closed there and hangs up, so state stops flowing at the boundary
    rather than whenever the tab next asks — every other guest route re-checks
    per request, but this one was checked once, at connect.

    Everything else that can end a device's access is caught by re-running the
    connect-time gate every STREAM_REVALIDATE_SECONDS: a stream that no longer
    passes emits access_changed and hangs up, and the page reloads into
    whatever the server now serves it. A live stream is re-checked as live, so
    a token that turned pending under it stops relaying state too.
    """
    q = await ha_client.subscribe(token_id)
    next_check = time.monotonic() + STREAM_REVALIDATE_SECONDS
    try:
        # M-5: Expose WS health in SSE connected event
        yield f"event: connected\ndata: {{\"ws_healthy\": {str(ha_client.is_ws_healthy()).lower()}}}\n\n"

        while True:
            if await request.is_disconnected():
                break

            timeout = SSE_KEEPALIVE_SECONDS
            if starts_at is not None:
                remaining = starts_at - time.time()
                if remaining <= 0:
                    yield 'event: token_activated\ndata: {"type": "token_activated"}\n\n'
                    break
                timeout = min(timeout, remaining)
            elif closes_at is not None:
                remaining = closes_at - time.time()
                if remaining <= 0:
                    yield 'event: window_closed\ndata: {"type": "window_closed"}\n\n'
                    break
                timeout = min(timeout, remaining)

            if time.monotonic() >= next_check:
                if not await _still_admitted(slug, request, allow_pending=starts_at is not None):
                    yield 'event: access_changed\ndata: {"type": "access_changed"}\n\n'
                    break
                next_check = time.monotonic() + STREAM_REVALIDATE_SECONDS
            timeout = max(0.0, min(timeout, next_check - time.monotonic()))

            try:
                event = await asyncio.wait_for(q.get(), timeout=timeout)
                # L-8: Only forward whitelisted event types
                allowed = _PENDING_SSE_EVENTS if starts_at is not None else _ALLOWED_SSE_EVENTS
                if event["type"] not in allowed:
                    continue
                yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                if event["type"] in _TERMINAL_SSE_EVENTS:
                    break
            except asyncio.TimeoutError:
                # A stream reaching its boundary — a pending one its start, a
                # live one its window's end — lands here, and the check at the
                # top of the next pass is what turns it into the push.
                boundary = starts_at if starts_at is not None else closes_at
                if boundary is None or boundary - time.time() > 0:
                    yield ": keepalive\n\n"

    finally:
        await ha_client.unsubscribe(token_id, q)


@router.get("/{slug}/stream")
async def guest_stream(request: Request, slug: str = Path(max_length=64)):
    # The one guest route a pending token may hold open. It carries no device
    # data before the start time — see _event_generator — and it is how an
    # "Activate Now" reaches a tab that is already sitting on the countdown.
    row = await _validate_token(slug, request, allow_pending=True)
    access = await _access(row)
    if access.active:
        opens_at = None
        closes_at = access.closes_at if row["access_windows"] is not None else None
    else:
        # Pending with nothing to count down to — no window opens again before
        # expiry, or the zone cannot be read — still gets a pending stream, so
        # it can hear an admin's schedule edit. The sentinel is a boundary
        # that never arrives, not a special case in the generator.
        opens_at = access.opens_at or NEVER_EXPIRES_SECONDS
        closes_at = None
    return StreamingResponse(
        _event_generator(row["id"], slug, request, opens_at, closes_at),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Camera proxy
# ---------------------------------------------------------------------------
# Guests never receive an HA URL or the HA token. Every frame is relayed through
# these endpoints after the same token + allowlist checks the command path uses.

async def _validate_camera(slug: str, entity_id: str, request: Request):
    """Shared gate for both camera endpoints. Order matters: token, shape, allowlist."""
    row = await _validate_token(slug, request)

    if not _CAMERA_ENTITY_RE.match(entity_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid camera entity",
        )

    entity_ids = await db.get_token_entities(row["id"])
    if entity_id not in entity_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Entity not in allowlist")

    return row


@router.get("/{slug}/camera/{entity_id}")
async def guest_camera_snapshot(
    request: Request,
    slug: str = Path(max_length=64),
    entity_id: str = Path(max_length=255),
):
    row = await _validate_camera(slug, entity_id, request)

    if not await rate_limiter.check(f"cam:{row['id']}", CAMERA_SNAPSHOT_RPM):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")

    try:
        data, ctype = await ha_client.camera_snapshot(entity_id)
    except Exception:
        logger.warning("Camera snapshot failed for %s", entity_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Camera unavailable")

    return Response(content=data, media_type=ctype, headers={"Cache-Control": "no-store"})


@router.get("/{slug}/camera/{entity_id}/stream")
async def guest_camera_stream(
    request: Request,
    slug: str = Path(max_length=64),
    entity_id: str = Path(max_length=255),
):
    row = await _validate_camera(slug, entity_id, request)
    token_id = row["id"]
    # A live view opened inside a weekly window must not outlast it. Like the
    # SSE stream, this relay is gated once, at open, so it carries its own
    # deadline rather than relying on a re-check that never comes.
    deadline = None
    if row["access_windows"] is not None:
        deadline = (await _access(row)).closes_at

    async with _stream_lock:
        if _active_streams.get(token_id, 0) >= MAX_STREAMS_PER_TOKEN:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many concurrent streams",
            )
        _active_streams[token_id] = _active_streams.get(token_id, 0) + 1

    async def _release() -> None:
        async with _stream_lock:
            remaining = _active_streams.get(token_id, 1) - 1
            if remaining > 0:
                _active_streams[token_id] = remaining
            else:
                _active_streams.pop(token_id, None)

    # The upstream is opened here rather than inside the generator so the real
    # boundary from HA's Content-Type reaches the browser, and so an upstream
    # failure surfaces as 502 instead of a truncated 200.
    stack = AsyncExitStack()
    try:
        ctype, chunks = await stack.enter_async_context(ha_client.camera_stream(entity_id))
    except Exception:
        await stack.aclose()
        await _release()
        logger.warning("Camera stream failed to open for %s", entity_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Camera unavailable")

    async def _relay() -> AsyncIterator[bytes]:
        # Re-run the camera gate now and then, as the SSE stream does: a live
        # view is the one relay that outlasting a revocation would matter most
        # for, and it has no event channel for a push to hang it up on.
        next_check = time.monotonic() + STREAM_REVALIDATE_SECONDS
        try:
            async for chunk in chunks:
                if await request.is_disconnected():
                    break
                if deadline is not None and time.time() >= deadline:
                    break
                if time.monotonic() >= next_check:
                    try:
                        await _validate_camera(slug, entity_id, request)
                    except HTTPException:
                        break
                    next_check = time.monotonic() + STREAM_REVALIDATE_SECONDS
                yield chunk
        except Exception:
            logger.info("Camera stream ended for %s", entity_id)
        finally:
            await stack.aclose()
            await _release()

    return StreamingResponse(
        _relay(),
        media_type=ctype,
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Command proxy
# ---------------------------------------------------------------------------

@router.post("/{slug}/command")
async def guest_command(
    body: CommandRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    slug: str = Path(max_length=64),
):
    row = await _validate_token(slug, request)
    token_id = row["id"]

    allowed = await rate_limiter.check_multi(token_id, COMMAND_LIMITS)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")

    # L-6: Validate service format before processing
    if not re.match(r'^[a-z_]+\.[a-z_]+$', body.service) and not re.match(r'^[a-z_]+$', body.service):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid service format",
        )

    entity_ids = await db.get_token_entities(token_id)
    if body.entity_id not in entity_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Entity not in allowlist")

    entity_domain = body.entity_id.split(".")[0]

    if "." in body.service:
        svc_domain, svc_name = body.service.split(".", 1)
        if svc_domain != entity_domain:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Service domain does not match entity",
            )
    else:
        svc_name = body.service

    allowed_svc = ALLOWED_SERVICES.get(entity_domain)
    if not allowed_svc or svc_name not in allowed_svc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Service '{svc_name}' not allowed for {entity_domain}",
        )

    # Last of the authorization checks, and before the payload ones, so a
    # malformed colour on a gated entity from off-site still answers "you need
    # to be at the property" rather than confirming the payload was fine.
    # The network check goes first of the two: it is a comparison against an
    # address already in hand, and a guest off the network should not be
    # asked for their location only to be refused for something else.
    await _enforce_local_network(row, body, request)
    await _enforce_proximity(row, body)

    # The colour wheel is the one widget that posts a structured value built
    # from raw pointer coordinates, so its payload is validated rather than
    # forwarded on trust.
    if entity_domain == "light" and svc_name == "turn_on":
        color_error = validate_light_color(body.data)
        if color_error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=color_error,
            )

    # Only entity_id and service are ever logged, so secrets a widget has to
    # pass through here — an alarm code, say — stay in transit and nowhere else.
    clean_data = {k: v for k, v in body.data.items() if k not in FORBIDDEN_DATA_KEYS}
    service_data = {**clean_data, "entity_id": body.entity_id}

    # A use of a use-limited link is one command Home Assistant accepted —
    # nothing else. Not opening the page, not /state, not the stream: a chat
    # app that unfurls the URL into a preview card fetches the page (some run
    # its JavaScript too), and if that counted, the guest's single use would
    # be gone before they ever tapped it. No preview fetcher POSTs a command,
    # so a use is something only a person pressing a control can spend — no
    # User-Agent list to keep up to date.
    #
    # Claimed last, after every authorization and payload check, so a refused
    # or malformed command costs nothing; claimed before the upstream call,
    # atomically, so two taps racing for the last use cannot both get it; and
    # refunded if HA then fails, so a 502 does not spend it either.
    limited = row["max_uses"] is not None
    if limited and not await db.consume_token_use(token_id):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Access unavailable")

    try:
        result = await ha_client.call_service(entity_domain, svc_name, service_data)
    except httpx.HTTPStatusError as exc:
        if limited:
            await db.refund_token_use(token_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Service call failed")
    except Exception:
        if limited:
            await db.refund_token_use(token_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Service call failed")

    await db.log_access(
        token_id=token_id,
        event_type="command",
        ip_address=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        entity_id=body.entity_id,
        service=body.service,
    )
    _schedule_activity_event(
        background_tasks,
        _activity_payload(
            row,
            "command",
            target_entity_id=body.entity_id,
            service=f"{entity_domain}.{svc_name}",
        ),
    )

    if not limited:
        return {"ok": True}

    fresh = await db.get_token_by_id(token_id)
    uses_remaining = max(0, fresh["max_uses"] - fresh["use_count"]) if fresh else 0
    if uses_remaining == 0:
        # Every other tab on this link is still showing live controls it can no
        # longer use; tell them, the same way a revoke does.
        await ha_client.broadcast_token_expired(token_id, reason="used")
    return {"ok": True, "uses_remaining": uses_remaining}
