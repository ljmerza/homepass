"""The client address behind every per-IP decision, and who may vouch for it.

The IP allowlist, the country allowlist, the home-network gate and every rate
limiter key on this one address, so it has to be one a guest cannot choose.
X-Forwarded-For is only a claim: anyone can send the header, and a proxy that
appends to it (nginx's $proxy_add_x_forwarded_for, most tunnels) leaves the
client's own entries in front of the one it adds. So the header is believed
only when the socket peer is a proxy we trust, and then read right to left:
each trusted hop is skipped, and the first address that is not a trusted proxy
is the client. Nothing a client writes into the header can land to the right
of the entry its own proxy appended, so nothing it writes gets picked.

Who is trusted:
  * loopback, always — a proxy on the same host, and the test transport;
  * in add-on mode with trusted_proxies empty, Home Assistant's internal
    "hassio" network, which is where the ingress proxy and reverse-proxy
    add-ons (NGINX Proxy Manager, Cloudflared) connect from. Any add-on on
    that network could claim any address, which is the same trust the host
    already gives them;
  * otherwise exactly the trusted_proxies ranges (plus loopback).

A proxy that is not trusted is not an error, it is just a peer: its own
address becomes the client. Allowlists then fail closed and every guest behind
it shares one rate-limit bucket, which is visible and safe — and logged once so
the admin can find the option.
"""
import functools
import ipaddress
import logging

from fastapi import Request

from app.config import settings

logger = logging.getLogger(__name__)

Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Network = ipaddress.IPv4Network | ipaddress.IPv6Network

LOOPBACK = ("127.0.0.0/8", "::1/128")
# Supervisor's Docker network: the Supervisor itself (and so ingress) is
# 172.30.32.2, add-ons are handed addresses in 172.30.33.0/24.
HASSIO_NETWORK = "172.30.32.0/23"

_warned_untrusted = False


@functools.lru_cache(maxsize=8)
def _parse(raw: str, addon_mode: bool) -> tuple[Network, ...]:
    """Loopback plus the configured (or add-on default) proxy ranges.

    Settings refuses an invalid entry at startup; skipping one here only
    matters for a value assigned afterwards, and skipping errs strict — a range
    that is not there vouches for nobody.
    """
    entries = [c.strip() for c in raw.split(",") if c.strip()]
    if not entries and addon_mode:
        entries = [HASSIO_NETWORK]
    networks = []
    for cidr in (*LOOPBACK, *entries):
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def trusted_networks() -> tuple[Network, ...]:
    return _parse(settings.trusted_proxies, bool(settings.supervisor_token))


def _parse_addr(value: str) -> Address | None:
    """An address from a header entry or socket peer, tolerating a port.

    Accepts "1.2.3.4", "1.2.3.4:5678", "2001:db8::1" and "[2001:db8::1]:443";
    IPv4-mapped IPv6 ("::ffff:1.2.3.4") comes back as the IPv4 address so it
    matches IPv4 ranges. None for anything else.
    """
    value = value.strip()
    if value.startswith("["):
        value = value[1:].split("]", 1)[0]
    elif value.count(":") == 1:
        value = value.split(":", 1)[0]
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return addr.ipv4_mapped
    return addr


def _is_trusted(addr: Address) -> bool:
    return any(addr in net for net in trusted_networks() if addr.version == net.version)


def client_ip(request: Request) -> str:
    """The guest's address, as far as a trusted proxy chain can vouch for it.

    "unknown" when there is no socket peer at all: without one there is no
    proxy to trust, so the header is ignored and every gate fails closed on an
    address that does not parse.
    """
    global _warned_untrusted
    peer_raw = request.client.host if request.client else ""
    peer = _parse_addr(peer_raw) if peer_raw else None
    if peer is None:
        return peer_raw or "unknown"

    # Every header line, in order: a proxy may add its own line rather than
    # extend the client's, and headers.get() would return only the first —
    # the one the client wrote.
    hops = [
        hop.strip()
        for line in request.headers.getlist("X-Forwarded-For")
        for hop in line.split(",")
        if hop.strip()
    ]
    if not hops:
        return str(peer)
    if not _is_trusted(peer):
        if not _warned_untrusted:
            _warned_untrusted = True
            logger.warning(
                "Ignoring X-Forwarded-For from %s, which is not a trusted proxy. "
                "If HomePass sits behind a reverse proxy at that address, add it "
                "to the Trusted Proxies option; until then every guest behind it "
                "is seen as %s.",
                peer, peer,
            )
        return str(peer)

    for hop in reversed(hops):
        addr = _parse_addr(hop)
        if addr is None:
            # Unparseable, and to the right of it only trusted proxies: return
            # it as-is so every allowlist fails closed on it.
            return hop
        if not _is_trusted(addr):
            return str(addr)
    # The whole chain is trusted proxies — the leftmost is as far back as the
    # header can tell.
    return str(_parse_addr(hops[0]))
