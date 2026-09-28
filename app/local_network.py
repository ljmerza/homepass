"""The add-on's home-network ranges, and whether a client address falls inside.

Backs the per-entity require_local_network gate: a guest can view such an
entity from anywhere, but a command for it is only forwarded when the request
comes from one of the local_network_cidrs ranges — the house's own Wi-Fi, say.

What this is and is not. It trusts the client address the guest router
resolves (app/client_ip.py), which only takes X-Forwarded-For from a trusted
proxy — so it is as good as the trusted_proxies setting. And "on the home network" is not "at the door" — anyone on the Wi-Fi, or on a
VPN into it, passes. It stops a link being used to open a lock from across
town, which is the case it exists for.

Deliberately free of routing: the guest router owns the address and the
entity lookup, this holds only the range arithmetic so it is testable alone.
"""
import functools
import ipaddress

from app.config import settings

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@functools.lru_cache(maxsize=8)
def _parse(raw: str) -> tuple[Network, ...]:
    """The comma-separated option as networks, parsed once per distinct value.

    Settings refuses an invalid entry at startup, so skipping one here only
    matters for a value assigned after the fact. Skipping errs strict: a range
    that is not there admits nobody.
    """
    networks = []
    for cidr in (c.strip() for c in raw.split(",")):
        if not cidr:
            continue
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def networks() -> tuple[Network, ...]:
    return _parse(settings.local_network_cidrs)


def is_configured() -> bool:
    """False while the option is empty — the gate is then off entirely."""
    return bool(networks())


def contains(client_ip: str) -> bool:
    """True if `client_ip` is inside a configured home-network range.

    An address that does not parse — "unknown", when the server could not see
    a peer at all — is outside every range, so the gate fails closed on it.
    """
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    # An IPv4 client reached over a dual-stack socket arrives as ::ffff:a.b.c.d;
    # compare it as the IPv4 address it is, or a 192.168.1.0/24 range would
    # never match it.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in networks())
