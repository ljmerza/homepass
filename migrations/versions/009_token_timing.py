"""Add weekly access windows and use limits to tokens.

access_windows is a JSON list of {"weekdays": [0-6], "start": "HH:MM",
"end": "HH:MM"} — "every Tuesday and Thursday 09:00-13:00", say — evaluated in
the house's time zone. NULL means "no weekly pattern", the state every existing
token starts in, and such a token behaves exactly as it did before. The windows
narrow a token; they never widen it: access still needs now to fall inside
starts_at/expires_at, and then also inside one of the windows.

A JSON blob in one column, like entity_templates.entity_ids, because nothing
queries across it — the guest gate reads it whole off the row it has already
loaded, and the admin writes it whole.

max_uses/use_count make a link single- (or N-) use. NULL max_uses is
unlimited, again the state of every existing token. What counts as a use is
decided in the guest router, not here — see guest_command — but it is
deliberately something a link-preview bot can never do by fetching the URL.

use_count is NOT NULL DEFAULT 0 so the atomic "use_count < max_uses" claim in
the database layer never has to reason about a NULL.

Revision ID: 009
Revises: 008
Create Date: 2026-09-28
"""
from typing import Sequence, Union

from alembic import op

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = {
    "access_windows": "TEXT",
    "max_uses": "INTEGER",
    "use_count": "INTEGER NOT NULL DEFAULT 0",
}


def upgrade() -> None:
    cols = [r[1] for r in op.get_bind().exec_driver_sql("PRAGMA table_info(tokens)")]
    for name, ddl in _COLUMNS.items():
        if name not in cols:
            op.execute(f"ALTER TABLE tokens ADD COLUMN {name} {ddl}")


def downgrade() -> None:
    cols = [r[1] for r in op.get_bind().exec_driver_sql("PRAGMA table_info(tokens)")]
    for name in reversed(list(_COLUMNS)):
        if name in cols:
            op.execute(f"ALTER TABLE tokens DROP COLUMN {name}")
