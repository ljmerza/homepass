"""Guest PIN hashing and the signed session a correct entry hands back.

Deliberately separate from app/auth.py: that module owns the admin password and
the admin session cookie, and nothing a guest does may touch either. The two
share only bcrypt, used the same way — hash once, verify off the event loop.

Storage is write-only. A PIN is bcrypt-hashed on the way in and never decrypted,
so the admin UI can report whether a PIN is set but can never show it; a
forgotten PIN is replaced, not recovered.
"""
import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import time
from typing import NamedTuple

import bcrypt

# Numeric, 4-8 digits. The guest enters this on a phone, so a digits-only policy
# buys a keypad (inputmode="numeric") instead of a full keyboard — and the PIN is
# a second factor behind a 128-bit random slug the attacker must already hold,
# not a standalone credential. 4 digits is the floor most people will actually
# use; 8 is there for an admin who wants a link that never expires to cost more
# than 10^4 guesses. Everything outside the range is refused server-side so the
# guest keypad can never produce a PIN the admin's keyboard accepted.
PIN_MIN_LENGTH = 4
PIN_MAX_LENGTH = 8
_PIN_RE = re.compile(rf"^\d{{{PIN_MIN_LENGTH},{PIN_MAX_LENGTH}}}$")

SESSION_COOKIE = "homepass_pin_session"

# How long one correct entry lasts before the guest is asked again. Matches the
# admin session lifetime; always clamped down to the token's own expiry.
SESSION_TTL_SECONDS = 86400

_SESSION_VERSION = "v1"
_SESSION_KEY_INFO = b"homepass-guest-pin-session-v1"


def is_valid_pin(pin: str) -> bool:
    return bool(_PIN_RE.match(pin))


async def hash_pin(pin: str) -> str:
    """bcrypt hash of a PIN. CPU-bound, so it runs off the event loop."""
    loop = asyncio.get_running_loop()
    hashed = await loop.run_in_executor(None, bcrypt.hashpw, pin.encode(), bcrypt.gensalt())
    return hashed.decode()


async def verify_pin(pin: str, pin_hash: str) -> bool:
    """Check a submitted PIN. bcrypt.checkpw does the comparison in constant
    time — there is deliberately no `==` anywhere in this module's hot path.
    """
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, bcrypt.checkpw, pin.encode(), pin_hash.encode()
        )
    except ValueError:
        # Hash unreadable (hand-edited row, truncated column). Refuse rather
        # than 500 — a broken hash must fail closed, not open.
        return False


def _session_key(pin_hash: str) -> bytes:
    """Per-token HMAC key derived from that token's own bcrypt hash.

    Three of the session requirements fall out of this one choice and need no
    extra bookkeeping: the key is distinct per token (a cookie minted for token A
    cannot verify against token B), and it changes the instant the PIN is changed
    or cleared (outstanding sessions stop verifying). The hash carries 128 bits
    of bcrypt salt, never leaves the server, and is never returned by the admin
    API, so it is suitable key material; the info string keeps this use
    domain-separated from the hash's own purpose.
    """
    return hmac.new(pin_hash.encode(), _SESSION_KEY_INFO, hashlib.sha256).digest()


def _signature(
    token_id: str,
    expires_at: int,
    pin_hash: str,
    remember: bool = True,
    access_code_id: str | None = None,
) -> str:
    """HMAC over everything a session vouches for.

    The two optional claims are appended only when they differ from the
    original shape, so a session minted before either existed — remembered, and
    from a typed PIN — still carries the same message and keeps verifying.

    `|once` marks a session issued while the token had remember-PIN off. The
    verifier rebuilds the message from the token's *current* setting, so turning
    remember-PIN off retires every remembered session at once: a guest who was
    told they would be asked each visit must not coast on a cookie from before.

    `|code:<id>` binds a session to the access link that minted it, which is
    what lets revoking that one link sign out the devices it let in. The id is
    hex, so it cannot smuggle in a separator of its own.
    """
    msg = f"{_SESSION_VERSION}|{token_id}|{expires_at}"
    if not remember:
        msg += "|once"
    if access_code_id is not None:
        msg += f"|code:{access_code_id}"
    digest = hmac.new(_session_key(pin_hash), msg.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def issue_session(
    token_id: str,
    pin_hash: str,
    token_expires_at: int,
    remember: bool = True,
    access_code_id: str | None = None,
) -> tuple[str, int | None]:
    """Mint a session cookie value for a token. Returns (value, max_age).

    The expiry claim is clamped to the token's own expiry, so the cookie cannot
    outlive the link even on a token that never expires. The guest endpoints
    re-read the token on every request anyway, so a link revoked or shortened
    after the fact still fails immediately — this clamp is the cheap half.

    max_age is None when the token does not remember the PIN, which the caller
    passes straight to set_cookie: no Max-Age makes it a browser-session cookie,
    dropped when the browser closes. The signed expiry still applies on top, so
    a browser that restores session cookies on relaunch gets no more than the
    same 24 hours a remembered one would.

    A session from an access link is the only one with four parts; the link id
    rides in the clear because the signature, not secrecy, is what protects it.
    """
    now = int(time.time())
    expires_at = min(now + SESSION_TTL_SECONDS, token_expires_at)
    sig = _signature(token_id, expires_at, pin_hash, remember, access_code_id)
    value = f"{_SESSION_VERSION}.{expires_at}.{sig}"
    if access_code_id is not None:
        value += f".{access_code_id}"
    return value, (max(expires_at - now, 0) if remember else None)


class SessionClaims(NamedTuple):
    """What a verified session says about itself.

    access_code_id is None for a session a typed PIN produced. When set, the
    caller still has to confirm that link exists — the signature proves the
    link once did, not that it has not been revoked since.
    """
    access_code_id: str | None


def read_session(
    cookie: str | None, token_id: str, pin_hash: str, remember: bool = True
) -> SessionClaims | None:
    """The claims of a live session this token's current PIN signed, else None.

    `remember` is the token's current setting. With it on, a session minted
    while it was off is accepted too — that cookie dies with the browser anyway,
    and turning the setting back on is no reason to sign anyone out. With it
    off, only a session minted under off verifies.
    """
    if not cookie:
        return None
    parts = cookie.split(".")
    if len(parts) not in (3, 4) or parts[0] != _SESSION_VERSION:
        return None
    try:
        expires_at = int(parts[1])
    except ValueError:
        return None
    if expires_at <= int(time.time()):
        return None
    access_code_id = parts[3] if len(parts) == 4 else None
    if access_code_id is not None and not _ACCESS_CODE_ID_RE.match(access_code_id):
        return None

    # Every candidate is computed and compared, with no early exit, so which
    # variant matched is not visible in the timing either.
    candidates = [False] + ([True] if remember else [])
    matched = False
    for flag in candidates:
        expected = _signature(token_id, expires_at, pin_hash, flag, access_code_id)
        matched |= hmac.compare_digest(parts[2], expected)
    return SessionClaims(access_code_id) if matched else None


def verify_session(
    cookie: str | None, token_id: str, pin_hash: str, remember: bool = True
) -> bool:
    """True only if `cookie` is a live session this token's current PIN signed.

    Says nothing about whether an access link behind the session still exists —
    the guest router's gate checks that. This is the signature half only.
    """
    return read_session(cookie, token_id, pin_hash, remember) is not None


# ---------------------------------------------------------------------------
# Access links (PIN-free)
# ---------------------------------------------------------------------------
# An access code is a second, independent secret on a PIN-protected token: a
# link carrying it (/g/<slug>?c=<code>) is exchanged once for the same session
# cookie a correct PIN earns, and the guest never sees the keypad.
#
# 24 bytes is 192 bits — comfortably past the 128-bit floor the slug itself is
# held to — and token_urlsafe renders it as 32 URL-safe characters, short enough
# to keep the QR code scannable.
ACCESS_CODE_BYTES = 24
_ACCESS_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")
# The id a code-bound session names. uuid4().hex, so it matches exactly this.
_ACCESS_CODE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def generate_access_code() -> str:
    return secrets.token_urlsafe(ACCESS_CODE_BYTES)


def hash_access_code(code: str) -> str:
    """SHA-256 of a code, hex. See migration 010 for why this is not bcrypt."""
    return hashlib.sha256(code.encode()).hexdigest()


def match_access_code(code: str, candidates: list[tuple[str, str]]) -> str | None:
    """The id of the candidate `code` belongs to, or None.

    `candidates` is (id, code_hash) for one token's links — and only that
    token's, so a code minted for another token cannot open this one even
    though the column is globally unique.

    Deliberately not a `WHERE code_hash = ?` lookup: every candidate is compared
    with compare_digest and the loop never exits early, so neither the database
    index nor the comparison leaks how close a guess came. A shape check runs
    first and costs the same for every malformed input.
    """
    if not _ACCESS_CODE_RE.match(code):
        return None
    digest = hash_access_code(code)
    found = None
    for code_id, code_hash in candidates:
        if hmac.compare_digest(digest, code_hash) and found is None:
            found = code_id
    return found
