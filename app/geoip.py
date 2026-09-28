"""Offline IP-to-country lookup behind the per-token country allowlist.

The data is DB-IP's "IP to Country Lite" database, downloaded once when the
image is built (see the Dockerfile) and read from disk at runtime, so a guest
request never waits on, or leaks its address to, a third-party service. It is
licensed CC BY 4.0, which permits shipping it inside the image provided DB-IP
is credited — the README and the dashboard carry that attribution. MaxMind's
GeoLite2 was the other candidate and was passed over because downloading it
needs an account and a licence key, which a public image build cannot hold.

Why not the approach the fork this feature comes from took (fetch per-country
CIDR lists from ipdeny.com when a link is created, and store them in the IP
allowlist column): it makes creating a link depend on a network call, freezes a
snapshot of the ranges into each token so a refreshed database never reaches
old links, and turns every guest request into a linear scan over thousands of
CIDRs. Here tokens store country codes, and the address is resolved against
whatever database the running image carries.

File format: CSV rows of `first_ip,last_ip,country_code`, IPv4 and IPv6 mixed,
optionally gzipped — DB-IP's layout. A standalone install can point
GEOIP_DB_PATH at any file in that shape. Rows that do not parse (a header, a
comment) are skipped, and so is DB-IP's "ZZ" for unassigned space.

What it is and is not. It says which country an address is registered to, not
where the person is: VPNs, corporate egress and mobile roaming all move a guest
across a border without their moving at all. It is a coarse filter against a
link being used from the other side of the world, not a location check.

Memory: the table is held as flat arrays rather than Python ints. IPv6 ranges
are stored at /64 granularity (the top 64 bits of each bound), which fits them
in 64-bit slots; published allocations are far coarser than /64, so a boundary
inside one is not expected, and if one occurs only addresses inside that single
/64 can be attributed to the neighbouring range.
"""
import array
import asyncio
import bisect
import csv
import gzip
import logging
import os
import socket

from app.config import settings

logger = logging.getLogger(__name__)

# DB-IP's code for space that is allocated to no country.
UNASSIGNED = "ZZ"

_V6_SHIFT = 64


def _encode_cc(cc: str) -> int:
    return (ord(cc[0]) << 8) | ord(cc[1])


def _decode_cc(value: int) -> str:
    return chr(value >> 8) + chr(value & 0xFF)


class _Ranges:
    """Sorted, non-overlapping [start, end] ranges, each with a country."""

    __slots__ = ("starts", "ends", "codes")

    def __init__(self, typecode: str) -> None:
        self.starts = array.array(typecode)
        self.ends = array.array(typecode)
        self.codes = array.array("H")

    def append(self, start: int, end: int, code: int) -> None:
        # Adjacent ranges of one country fold into one — DB-IP splits a country
        # into many consecutive rows, and a single entry per run is all a
        # lookup needs.
        if self.codes and self.codes[-1] == code and start <= self.ends[-1] + 1:
            if end > self.ends[-1]:
                self.ends[-1] = end
            return
        self.starts.append(start)
        self.ends.append(end)
        self.codes.append(code)

    def lookup(self, value: int) -> int | None:
        i = bisect.bisect_right(self.starts, value) - 1
        if i >= 0 and value <= self.ends[i]:
            return self.codes[i]
        return None


class CountryTable:
    """An in-memory IP-to-country table. Build one with load_table()."""

    def __init__(self, v4: _Ranges, v6: _Ranges, countries: frozenset[str]) -> None:
        self._v4 = v4
        self._v6 = v6
        self.countries = countries

    def lookup(self, ip: str) -> str | None:
        """The ISO 3166-1 alpha-2 code for `ip`, or None if it has none.

        None covers every address the table cannot place: private and reserved
        space, unassigned blocks, and a string that is not an address at all.
        """
        try:
            if ":" in ip:
                packed = socket.inet_pton(socket.AF_INET6, ip)
                if packed[:12] == b"\x00" * 10 + b"\xff\xff":
                    # ::ffff:a.b.c.d is an IPv4 client on a dual-stack socket.
                    code = self._v4.lookup(int.from_bytes(packed[12:], "big"))
                else:
                    code = self._v6.lookup(int.from_bytes(packed, "big") >> _V6_SHIFT)
            else:
                code = self._v4.lookup(int.from_bytes(socket.inet_pton(socket.AF_INET, ip), "big"))
        except (OSError, ValueError):
            return None
        return _decode_cc(code) if code is not None else None


def _parse_rows(fh):
    """Yield (is_v6, start, end, code) for every usable row."""
    for row in csv.reader(fh):
        if len(row) < 3:
            continue
        first, last, cc = row[0].strip(), row[1].strip(), row[2].strip().upper()
        if len(cc) != 2 or not cc.isascii() or not cc.isalpha() or cc == UNASSIGNED:
            continue
        try:
            if ":" in first:
                start = int.from_bytes(socket.inet_pton(socket.AF_INET6, first), "big") >> _V6_SHIFT
                end = int.from_bytes(socket.inet_pton(socket.AF_INET6, last), "big") >> _V6_SHIFT
                is_v6 = True
            else:
                start = int.from_bytes(socket.inet_pton(socket.AF_INET, first), "big")
                end = int.from_bytes(socket.inet_pton(socket.AF_INET, last), "big")
                is_v6 = False
        except OSError:
            continue
        if end < start:
            continue
        yield is_v6, start, end, cc


def load_table(path: str) -> CountryTable:
    """Read a DB-IP-shaped CSV (optionally .gz) into a CountryTable.

    Blocking and CPU-bound — call it off the event loop. Rows stream straight
    into the arrays, because the published file is sorted by start address. A
    file that is not gets a second, sorting pass rather than being trusted:
    bisect over an unsorted array answers wrongly, and quietly.
    """
    try:
        return _load(path, presorted=True)
    except _Unsorted:
        return _load(path, presorted=False)


class _Unsorted(Exception):
    pass


def _load(path: str, presorted: bool) -> CountryTable:
    opener = gzip.open if path.endswith(".gz") else open
    ranges = {False: _Ranges("I"), True: _Ranges("Q")}
    countries: set[str] = set()
    with opener(path, "rt", encoding="utf-8", newline="") as fh:
        rows = _parse_rows(fh)
        if not presorted:
            rows = iter(sorted(rows))
        last = {False: -1, True: -1}
        for is_v6, start, end, cc in rows:
            if start < last[is_v6]:
                raise _Unsorted
            last[is_v6] = start
            ranges[is_v6].append(start, end, _encode_cc(cc))
            countries.add(cc)
    return CountryTable(ranges[False], ranges[True], frozenset(countries))


# ---------------------------------------------------------------------------
# The process-wide table
# ---------------------------------------------------------------------------
# Loaded lazily, on the first request that needs it, so an install where no
# link has a country allowlist never pays the memory or the second or so the
# load takes. A load that fails is remembered against the file's mtime, so a
# broken file is not re-parsed on every guest request but a replaced one is
# picked up.

_table: CountryTable | None = None
_table_key: tuple[str, float] | None = None
_failed_key: tuple[str, float] | None = None
_missing_logged = False
_load_lock = asyncio.Lock()


def _file_key() -> tuple[str, float] | None:
    path = settings.geoip_db_path
    try:
        return path, os.path.getmtime(path)
    except OSError:
        return None


def available() -> bool:
    """Whether a database file is installed. Cheap: no load, no parse."""
    return _file_key() is not None


async def get_table() -> CountryTable | None:
    """The loaded table, loading it on first use. None if there is no usable file."""
    global _table, _table_key, _failed_key, _missing_logged
    key = _file_key()
    if key is None:
        # Only reached when something needs a country — a link with an
        # allowlist, or an admin setting one — so this is worth saying once.
        if not _missing_logged:
            _missing_logged = True
            logger.warning(
                "No GeoIP database at %s: links with a country allowlist will "
                "refuse every visitor from outside the home network until one "
                "is installed.", settings.geoip_db_path,
            )
        return None
    if _table is not None and _table_key == key:
        return _table
    if _failed_key == key:
        return None
    async with _load_lock:
        if _table is not None and _table_key == key:
            return _table
        loop = asyncio.get_running_loop()
        try:
            table = await loop.run_in_executor(None, load_table, key[0])
        except Exception:
            logger.exception("Could not load the GeoIP database at %s", key[0])
            _failed_key = key
            return None
        _table, _table_key, _failed_key = table, key, None
        logger.info(
            "GeoIP database loaded from %s (%d countries)", key[0], len(table.countries)
        )
        return _table


async def country_for(ip: str) -> str | None:
    table = await get_table()
    return table.lookup(ip) if table else None


async def known_countries() -> frozenset[str] | None:
    """Every country the installed database has addresses for, or None if none is."""
    table = await get_table()
    return table.countries if table else None


def reset() -> None:
    """Forget the loaded table. For tests that swap the database file."""
    global _table, _table_key, _failed_key, _missing_logged
    _table = _table_key = _failed_key = None
    _missing_logged = False
