"""Public API authentication: the X-API-Key header, and whether the API exists.

The API is a machine interface — Home Assistant automations, Node-RED — so it
authenticates with a static key in a header and nothing else. An admin session
cookie does not unlock it, and neither does ingress: a browser holding a
dashboard session must not be able to drive the API with a request it was
tricked into sending, and a custom header is something a cross-site form or
<img> cannot add.
"""
import hmac

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from app.config import API_TOKEN_MIN_LENGTH, settings
from app.rate_limiter import RateLimiter

API_KEY_HEADER = "X-API-Key"

# Stands in for the admin session id when an API handler reuses an admin one.
# Those handlers take the require_admin result as a parameter and never look at
# it, but a recognisable value makes an accidental use obvious.
API_PRINCIPAL = "__api__"

# auto_error=False so a missing header reaches require_api_key and gets the same
# rate limiting and the same 401 as a wrong one. Declaring it through
# APIKeyHeader is what puts the Authorize button in Swagger UI.
api_key_header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)

# Per client IP, counted before the key is checked, so a caller guessing keys
# spends the same allowance as one using a real one. Generous on purpose: an
# automation polling a token list, or Node-RED fanning out a dozen calls, should
# never meet it. The key's length is what stops guessing; this stops a runaway
# loop from hammering the database.
API_RATE_LIMIT_PER_MINUTE = 120
_api_limiter = RateLimiter()


def api_enabled() -> bool:
    """Whether the API is switched on and has a usable key.

    The length check repeats the one in Settings on purpose: that one runs once
    at startup, and a key that is empty must never be comparable, whatever
    changed the setting afterwards.
    """
    return settings.api_enabled and len(settings.api_token) >= API_TOKEN_MIN_LENGTH


def _client_ip(request: Request) -> str:
    # Same source as the guest routes and the login limiter. It is spoofable
    # without a reverse proxy that overwrites X-Forwarded-For, which only costs
    # the limiter its per-IP accuracy — the key check does not depend on it.
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def keys_match(presented: str | None) -> bool:
    """Constant-time compare of a presented key against the configured one.

    hmac.compare_digest on bytes, not str: the str form raises on non-ASCII
    input, which would turn a malformed header into a 500 instead of a 401.
    """
    if not presented or not api_enabled():
        return False
    return hmac.compare_digest(presented.encode(), settings.api_token.encode())


async def enforce_api_rate_limit(request: Request) -> None:
    """Spend one request of the caller's allowance, or raise 429.

    Every route that compares a key calls this first — the schema route as well
    as /api/v1 — so no path lets a caller try keys faster than the API allows.
    """
    allowed = await _api_limiter.check(f"api:{_client_ip(request)}", API_RATE_LIMIT_PER_MINUTE)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")


async def require_api_key(
    request: Request,
    api_key: str | None = Security(api_key_header),
) -> None:
    """FastAPI dependency for every /api/v1 route.

    A disabled API answers 404, not 403: it is not a resource that exists and
    refuses you, it is not there.
    """
    if not api_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    await enforce_api_rate_limit(request)

    if not keys_match(api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )
