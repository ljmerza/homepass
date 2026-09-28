"""Ingress detection helpers.

Only trust X-Ingress-Path when SUPERVISOR_TOKEN exists (add-on mode).
This prevents header spoofing in standalone Docker deployments.
"""
import logging
import os
import re
import time
from dataclasses import dataclass

import httpx
from fastapi import Request

logger = logging.getLogger(__name__)

_SUPERVISOR_TOKEN: str | None = os.environ.get("SUPERVISOR_TOKEN")


def get_ingress_path(request: Request) -> str:
    if not _SUPERVISOR_TOKEN:
        return ""
    return request.headers.get("X-Ingress-Path", "")


def is_ingress_request(request: Request) -> bool:
    return bool(get_ingress_path(request))


# ---------------------------------------------------------------------------
# Direct guest-link address (add-on mode)
# ---------------------------------------------------------------------------
# An admin working through the HA sidebar is on HA's own origin, which does not
# serve guest links — those go to the add-on's published Network port. Without a
# Guest URL the dashboard has to build that address itself, and it used to
# hardcode http://homeassistant.local:5880. Both halves of that can be wrong:
# the port is remappable (or can be switched off) under the add-on's Network
# settings, and the host name is whatever the admin called the machine.
#
# Supervisor knows both, so ask it. /addons/self/info carries the port mapping
# and /info the host name; both are on the Supervisor's add-on bypass list, so
# no hassio_api permission is needed for them.

_SUPERVISOR_API = "http://supervisor"
GUEST_CONTAINER_PORT = 5880
_GUEST_PORT_KEY = f"{GUEST_CONTAINER_PORT}/tcp"
DEFAULT_GUEST_HOST = "homeassistant.local"

# The dashboard awaits this on render, so a Supervisor that has stopped
# answering must cost seconds, not the default httpx minute.
_SUPERVISOR_TIMEOUT = 3.0

# A port remap needs an add-on restart to take effect, which reloads this module
# anyway, so the cache only has to stop every dashboard load from asking. A
# failed lookup is cached for much less so a Supervisor that was briefly busy
# does not pin the fallback for five minutes.
GUEST_LINK_CACHE_TTL = 300
GUEST_LINK_FAILURE_TTL = 30

# One DNS label. The value ends up inside a link an admin copies to a guest, so
# anything that is not plainly a host name is refused rather than escaped.
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


@dataclass(frozen=True)
class GuestLinkTarget:
    host: str
    port: int
    # False when the admin has cleared the host port for 5880/tcp in the
    # add-on's Network settings. `port` then holds the container port only so
    # a link can still be drawn — nothing is listening on it from outside, and
    # the dashboard says so instead of handing out a dead link silently.
    published: bool = True

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


_DEFAULT_TARGET = GuestLinkTarget(DEFAULT_GUEST_HOST, GUEST_CONTAINER_PORT)

_guest_link_cache: GuestLinkTarget | None = None
_guest_link_cache_expires: float = 0.0


def _parse_port(payload: object) -> tuple[int, bool]:
    """(port, published) from /addons/self/info. Raises on anything unexpected.

    Supervisor reports a disabled port as the key mapped to null, which is a
    real answer and not a failure — a lookup that fell back to 5880 for it would
    produce exactly the dead link this exists to prevent.
    """
    network = payload["data"]["network"]  # type: ignore[index]
    if not isinstance(network, dict) or _GUEST_PORT_KEY not in network:
        raise ValueError(f"no {_GUEST_PORT_KEY} in Supervisor network info")
    port = network[_GUEST_PORT_KEY]
    if port is None:
        return GUEST_CONTAINER_PORT, False
    # bool is an int subclass; True is not a port.
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"unusable port {port!r}")
    return port, True


def _parse_host(payload: object) -> str:
    """The mDNS name for the Supervisor's host name. Raises if it is not one."""
    hostname = payload["data"]["hostname"]  # type: ignore[index]
    if not isinstance(hostname, str) or not _HOSTNAME_RE.match(hostname):
        raise ValueError(f"unusable hostname {hostname!r}")
    return f"{hostname.lower()}.local"


async def get_guest_link_target() -> GuestLinkTarget:
    """Where a guest reaches this add-on directly, for the no-Guest-URL fallback.

    Never raises. Standalone mode has no Supervisor and gets the defaults (the
    dashboard does not use this there — it links to its own origin). Each half
    is looked up independently, so an unreadable host name does not throw away
    a good port mapping, and each falls back to the last answer that worked
    before falling back to the built-in default.
    """
    global _guest_link_cache, _guest_link_cache_expires
    if not _SUPERVISOR_TOKEN:
        return _DEFAULT_TARGET

    now = time.monotonic()
    if _guest_link_cache is not None and now < _guest_link_cache_expires:
        return _guest_link_cache

    previous = _guest_link_cache or _DEFAULT_TARGET
    port, published, host = previous.port, previous.published, previous.host
    ok = True
    headers = {"Authorization": f"Bearer {_SUPERVISOR_TOKEN}"}
    async with httpx.AsyncClient(
        base_url=_SUPERVISOR_API, timeout=_SUPERVISOR_TIMEOUT, headers=headers,
    ) as client:
        try:
            resp = await client.get("/addons/self/info")
            resp.raise_for_status()
            port, published = _parse_port(resp.json())
        except Exception as exc:
            ok = False
            logger.warning(
                "Could not read the mapped guest port from Supervisor (%s) — using %d",
                exc, port,
            )
        try:
            resp = await client.get("/info")
            resp.raise_for_status()
            host = _parse_host(resp.json())
        except Exception as exc:
            ok = False
            logger.warning(
                "Could not read the host name from Supervisor (%s) — using %s", exc, host,
            )

    _guest_link_cache = GuestLinkTarget(host, port, published)
    _guest_link_cache_expires = now + (GUEST_LINK_CACHE_TTL if ok else GUEST_LINK_FAILURE_TTL)
    return _guest_link_cache
