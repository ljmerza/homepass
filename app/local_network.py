"""The add-on's home-network ranges, and whether a client address falls inside.

Backs the per-entity require_local_network gate: a guest can view such an
entity from anywhere, but a command for it is only forwarded when the request
comes from one of the local_network_cidrs ranges — the house's own Wi-Fi, say.

What this is and is not. It does not take the client's word for its address:
chain_is_local() checks every forwarding hop and the socket peer, so a request
from outside cannot pass by writing a LAN address into X-Forwarded-For, with or
without a reverse proxy in front. Something inside private address space — a
container on the host, a device on the LAN — can still claim any LAN address,
since HomePass cannot tell a proxy from a client there.
And "on the home network" is not "at the door" — anyone on the Wi-Fi, or on a
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


def _is_private(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_private
    except ValueError:
        return False


def chain_is_local(chain: list[str]) -> bool:
    """True if a request's whole forwarding chain stays inside the house.

    `chain` is every X-Forwarded-For entry in order, then the socket peer: the
    claimed client first, the address HomePass actually saw last. The first
    has to be inside a home-network range, as for contains(); every hop after
    it has to be inside one too, or be private address space — a reverse proxy
    on the LAN, loopback or a Docker bridge.

    The hops are what make this a network check rather than a header check.
    Anyone can send "X-Forwarded-For: 192.168.1.20", and the guest router takes
    the first entry as the client. A request that really came from outside
    still carries its real address somewhere after that entry: as the socket
    peer when nothing sits in front of HomePass (the add-on port forwarded
    straight through the router), or appended by a proxy that adds to the
    header rather than overwriting it — nginx's $proxy_add_x_forwarded_for, the
    usual config. That address is public, so the request is refused. A proxy
    that overwrites the header leaves only the real client, which is public
    too. Only a request that never left private space can pass.

    Deliberately tighter than the IP and country allowlists' own lookups, which
    still take the first entry alone and so rely on a proxy that overwrites the
    header (see _client_ip in the guest router). Tightening those would also
    change who they admit behind a proxy that reaches HomePass from a public
    address, such as Cloudflare in front of a forwarded port. This check has no
    such case to keep working — a guest reached through a public hop is not on
    the home network — and it is what unlocks a home-network-only control and
    the country exemption, so a forged header must not be enough to earn it.
    """
    if not chain or not contains(chain[0]):
        return False
    return all(contains(hop) or _is_private(hop) for hop in chain[1:])
