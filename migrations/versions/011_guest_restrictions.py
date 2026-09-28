"""Add single-device binding, a country allowlist and a home-network gate.

Three independent restrictions, all off by default, so every existing token and
every existing entity row keeps behaving exactly as it did:

tokens.device_binding — 0 or 1. With 1, the first browser to claim the link
owns it and every other browser is refused. It is the switch alone; the claim
is the next two columns.

tokens.device_secret_hash / tokens.device_bound_at — the claim. The hash is a
SHA-256 of a random secret that lives only in the claiming browser's cookie, so
a database read never yields something a second device could present. NULL
means unclaimed, and writing NULL back is the whole of "Unbind": there is no
sessions table to clean up, the same way migration 005 needed none for PINs.

tokens.country_allowlist — NULL, or a JSON array of ISO 3166-1 alpha-2 codes.
Codes, not the address ranges they resolve to: the ranges come from the offline
GeoIP database at request time (see app/geoip.py), so a database refreshed by a
later image applies to links created before it.

token_entities.require_local_network — 0 or 1, per entity, beside
require_proximity from migration 006 and for the same reason: it is an access
control the command path reads, so it is a column and not a key in the
presentation-only `options` blob. Inert until the add-on's local_network_cidrs
option is set.

Revision ID: 011
Revises: 010
Create Date: 2026-09-28
"""
from typing import Sequence, Union

from alembic import op

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TOKEN_COLUMNS = {
    "device_binding": "INTEGER NOT NULL DEFAULT 0",
    "device_secret_hash": "TEXT",
    "device_bound_at": "INTEGER",
    "country_allowlist": "TEXT",
}


def _columns(table: str) -> list[str]:
    return [r[1] for r in op.get_bind().exec_driver_sql(f"PRAGMA table_info({table})")]


def upgrade() -> None:
    cols = _columns("tokens")
    for name, ddl in _TOKEN_COLUMNS.items():
        if name not in cols:
            op.execute(f"ALTER TABLE tokens ADD COLUMN {name} {ddl}")

    if "require_local_network" not in _columns("token_entities"):
        op.execute(
            "ALTER TABLE token_entities "
            "ADD COLUMN require_local_network INTEGER NOT NULL DEFAULT 0"
        )


def downgrade() -> None:
    if "require_local_network" in _columns("token_entities"):
        op.execute("ALTER TABLE token_entities DROP COLUMN require_local_network")

    cols = _columns("tokens")
    for name in reversed(list(_TOKEN_COLUMNS)):
        if name in cols:
            op.execute(f"ALTER TABLE tokens DROP COLUMN {name}")
