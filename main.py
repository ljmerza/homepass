"""HomePass — FastAPI entry point."""
import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import database as db
from app import geoip
from app import ha_client
from app import local_network
from app import i18n
from app import settings_store
from app.api_auth import api_enabled
from app.config import settings
from app.context import base_context
from app.ingress import get_guest_link_target, get_ingress_path
from app.models import NEVER_EXPIRES_SECONDS
from app.rate_limiter import rate_limiter
from app.routers import admin, guest, public_api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

CLEANUP_INTERVAL_SECONDS = 300
HA_STARTUP_ATTEMPTS = 60
HA_STARTUP_RETRY_SECONDS = 5


@asynccontextmanager
async def lifespan(app: FastAPI):
    # L-3: Wrap DB creation in try/except
    try:
        os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)
        db.run_migrations()
        await db.get_db()
        logger.info("Database ready at %s", settings.db_path)
    except Exception as exc:
        logger.critical("Failed to initialize database at %s: %s", settings.db_path, exc)
        raise RuntimeError(f"Database initialization failed: {exc}") from exc

    # Before anything renders or reads a setting. A failure here is not fatal:
    # the add-on options underneath every override are still a working config.
    try:
        await settings_store.load()
    except Exception:
        logger.exception("Could not apply dashboard setting overrides — using the add-on options")

    ha_client.init_client()  # sync — no await

    # HA may still be booting (e.g. after a host reboot) — retry before giving up.
    for attempt in range(1, HA_STARTUP_ATTEMPTS + 1):
        try:
            await ha_client.validate_connectivity()
            break
        except Exception as exc:
            if attempt == HA_STARTUP_ATTEMPTS:
                logger.error("Cannot reach Home Assistant: %s", exc)
                raise RuntimeError("Home Assistant unreachable at startup") from exc
            logger.warning(
                "Home Assistant not reachable (attempt %d/%d): %s — retrying in %ds",
                attempt, HA_STARTUP_ATTEMPTS, exc, HA_STARTUP_RETRY_SECONDS,
            )
            await asyncio.sleep(HA_STARTUP_RETRY_SECONDS)

    await ha_client.start_ws_listener()

    # Parse the GeoIP table in the background when a live link will need it,
    # so the first country-gated guest request does not wait on a cold load.
    # Installs with no country allowlist never load it at all.
    geoip_warm = None
    if geoip.available() and await db.any_country_allowlist():
        geoip_warm = asyncio.create_task(geoip.get_table())

    async def _cleanup_loop():
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            try:
                await rate_limiter.cleanup()
                await db.cleanup_old_data(settings.access_log_retention_days)
            except Exception:
                logger.exception("Cleanup loop iteration failed")

    # M-2: Add done_callback to detect silent cleanup task death
    cleanup_task = asyncio.create_task(_cleanup_loop())
    cleanup_task.add_done_callback(lambda t: logger.error("Cleanup task terminated: %s", t.exception()) if not t.cancelled() and t.exception() else None)

    yield

    # M-7: Shutdown with timeout
    cleanup_task.cancel()
    if geoip_warm is not None:
        geoip_warm.cancel()
    try:
        await asyncio.wait_for(ha_client.stop_ws_listener(), timeout=5)
    except asyncio.TimeoutError:
        logger.warning("WS listener stop timed out, forcing cancel")
        if ha_client._ws_task:
            ha_client._ws_task.cancel()
    await ha_client.close_client()
    try:
        await db.close_db()
    except Exception:
        logger.exception("Error closing database")


app = FastAPI(
    title="HomePass",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
_templates = Jinja2Templates(directory="templates")


def get_guest_url_path(request: Request) -> str:
    """Return the public prefix for the configured guest host, or no prefix."""
    if not settings.guest_url:
        return ""
    guest_url = urlsplit(settings.guest_url)
    # A direct-port visit on another host must keep its root-based URLs.
    if request.url.netloc.lower() != guest_url.netloc.lower():
        return ""
    return guest_url.path.rstrip("/")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce
    ingress_path = get_ingress_path(request)
    # request.state.ingress_path is the URL base used by templates and guest
    # routes for assets, API calls, redirects, cookie paths, and the manifest.
    # Use the Ingress path if present, or the matching guest host's public path.
    request.state.ingress_path = ingress_path or get_guest_url_path(request)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    # All routes use strict nonce-based CSP (M-17: admin inline handlers
    # migrated to event delegation).
    script_src = f"'self' 'nonce-{nonce}'"
    if ingress_path:
        # Ingress loads inside HA iframe — allow framing from same origin
        frame_ancestors = "frame-ancestors 'self'"
    else:
        response.headers["X-Frame-Options"] = "DENY"
        frame_ancestors = "frame-ancestors 'none'"
    csp = (
        f"default-src 'self'; "
        f"script-src {script_src}; "
        f"style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        f"font-src https://fonts.gstatic.com; "
        f"img-src 'self' data:; "
        # Fonts hosts: the service worker fetch()es them, and a worker's
        # fetch() is governed by connect-src, not style-src/font-src.
        f"connect-src 'self' https://fonts.googleapis.com https://fonts.gstatic.com; "
        f"{frame_ancestors}"
    )
    response.headers["Content-Security-Policy"] = csp
    # Prevent browser from caching HTML responses (avoids stale JS after deploys)
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type:
        response.headers["Cache-Control"] = "no-store"
    return response


app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(admin.router)
app.include_router(guest.router)
# Always mounted; every route in both answers 404 while the API is disabled.
# Registering them conditionally at import would tie the decision to whatever
# the settings said when the module loaded, and leave nothing to test.
app.include_router(public_api.router)
app.include_router(public_api.docs_router)
app.add_exception_handler(RequestValidationError, public_api.api_validation_error_handler)


@app.get("/")
async def root(request: Request):
    return RedirectResponse(url=f"{request.state.ingress_path}/admin/dashboard")


@app.get("/admin/dashboard", include_in_schema=False)
async def admin_dashboard_page(request: Request):
    ctx = base_context(request, i18n.ADMIN)
    is_ingress = bool(get_ingress_path(request))
    # Only the sidebar needs the Supervisor lookup: there the admin is on HA's
    # origin and guest links have to point at the add-on's published port.
    # Direct-port admins link to their own origin, and a Guest URL beats both.
    target = await get_guest_link_target() if is_ingress and not settings.guest_url else None
    ctx.update({
        "never_expires": NEVER_EXPIRES_SECONDS,
        "is_ingress": is_ingress,
        "guest_url": settings.guest_url,
        # The "home network only" toggle is offered only when there is a home
        # network to check against; the ranges are shown beside it so the admin
        # can see what the flag will actually compare with.
        "local_networks": [str(n) for n in local_network.networks()],
        "geoip_available": geoip.available(),
        "api_enabled": api_enabled(),
        "direct_guest_base": target.base_url if target else "",
        "guest_port_unpublished": bool(target and not target.published),
    })
    return _templates.TemplateResponse(request, "admin_dashboard.html", ctx)


# M-6: Health check with WS and DB status
@app.get("/health")
async def health():
    ws_ok = ha_client.is_ws_healthy()
    try:
        await db.get_db()
        db_ok = True
    except Exception:
        db_ok = False
    if ws_ok and db_ok:
        return {"status": "ok", "ws": "connected", "db": "accessible"}
    return JSONResponse(
        status_code=503,
        content={"status": "degraded", "ws": "connected" if ws_ok else "disconnected", "db": "accessible" if db_ok else "unavailable"},
    )
