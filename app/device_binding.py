"""Single-device binding: the secret a claiming browser holds, and its check.

A token with device_binding on belongs to the first browser that claims it.
Claiming mints a random secret, hands it to that browser in a cookie, and
stores only its SHA-256. Every guest request after that has to present the
secret, so a forwarded link opens a refusal page on the second phone instead of
the controls.

What this is and is not. It is "one cookie jar", not "one person" and not "one
piece of hardware": a guest who clears their cookies, switches browsers, or
opens the link inside a chat app's built-in browser presents no cookie and is
refused like anyone else, and the admin's Unbind is the way back. It narrows
access and never widens it — the slug is still the credential, and a request
has to pass every other gate before this one is consulted.

Deliberately separate from app/guest_pin.py. A PIN session is a signed claim
that expires on its own; a binding is a claim on the token row that lasts until
an admin clears it, and nothing about one may unlock the other.
"""
import hashlib
import hmac
import secrets

COOKIE = "homepass_device"

# Browsers cap a persistent cookie at 400 days whatever max_age asks for, so
# this is the longest a binding can survive in the browser untouched. The guest
# page re-issues the cookie on every successful load, which keeps a long-lived
# link's binding alive for as long as the guest keeps using it. The cookie is
# not clamped to the token's expiry the way the PIN session is: it carries no
# claim of its own, the token row is re-read on every request, and a clamp
# would lock the bound guest out of a link the admin later extended.
COOKIE_MAX_AGE_SECONDS = 400 * 24 * 60 * 60


def new_secret() -> str:
    """256 bits of urandom. The cookie value, never stored server-side."""
    return secrets.token_urlsafe(32)


def hash_secret(secret: str) -> str:
    """What the token row stores. A plain SHA-256 rather than bcrypt: the input
    is 256 random bits, not something a person chose, so there is nothing for a
    slow hash to protect against — and this runs on every guest request.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


def verify(cookie: str | None, stored_hash: str | None) -> bool:
    """True only if `cookie` is the secret behind `stored_hash`.

    An unclaimed token (no stored hash) verifies nothing: a bound token nobody
    has claimed yet is closed to everyone until someone claims it.
    """
    if not cookie or not stored_hash:
        return False
    return hmac.compare_digest(hash_secret(cookie), stored_hash)
