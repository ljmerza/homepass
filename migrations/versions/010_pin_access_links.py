"""Add PIN-free access links and a per-token remember-PIN setting.

token_access_codes holds the links an admin hands out that skip PIN entry
(/g/<slug>?c=<code>). Each row is one link, so a token can carry several — one
per guest, per device, per printed QR — and each can be revoked or rotated on
its own without disturbing the rest.

The code itself is never stored. code_hash is its SHA-256, not a bcrypt hash
like the PIN's: a PIN is a handful of digits and needs a slow hash to survive an
offline guess, while a code is 192 bits from the OS CSPRNG, for which a fast
unsalted digest is already beyond brute force. The fast digest is also what
lets a redemption check the token's few candidate rows in constant time rather
than paying bcrypt per row. So, like the PIN, a link is shown to the admin once,
when it is minted, and can never be read back — a lost link is rotated.

ON DELETE CASCADE keeps the rows from outliving their token. Rotation of the
token's slug and every PIN write clear them explicitly in app/database.py; this
only covers the delete.

tokens.remember_pin defaults to 1, which is exactly how every existing token
already behaves: one correct PIN entry survives the browser being closed. 0
makes the PIN session a browser-session cookie, so the guest is asked again on
their next visit.

Revision ID: 010
Revises: 008
Create Date: 2026-09-28
"""
from typing import Sequence, Union

from alembic import op

revision: str = "010"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = [r[1] for r in op.get_bind().exec_driver_sql("PRAGMA table_info(tokens)")]
    if "remember_pin" not in cols:
        op.execute("ALTER TABLE tokens ADD COLUMN remember_pin INTEGER NOT NULL DEFAULT 1")

    op.execute("""
        CREATE TABLE IF NOT EXISTS token_access_codes (
            id            TEXT PRIMARY KEY,
            token_id      TEXT NOT NULL REFERENCES tokens(id) ON DELETE CASCADE,
            code_hash     TEXT NOT NULL UNIQUE,
            label         TEXT,
            created_at    INTEGER NOT NULL,
            last_used_at  INTEGER
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_token_access_codes_token_id "
        "ON token_access_codes(token_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_token_access_codes_token_id")
    op.execute("DROP TABLE IF EXISTS token_access_codes")
    cols = [r[1] for r in op.get_bind().exec_driver_sql("PRAGMA table_info(tokens)")]
    if "remember_pin" in cols:
        op.execute("ALTER TABLE tokens DROP COLUMN remember_pin")
