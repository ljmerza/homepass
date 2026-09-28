"""Add admin-edited overrides for the deployment settings that are safe to change live.

Every option used to come from the add-on configuration or the container
environment, so renaming the app or fixing a Guest URL meant a trip to the
Supervisor (or a container edit) and a restart. A standalone install has no
configuration form at all.

A row here is an override, not a copy: the add-on option or environment variable
stays the default, and deleting the row puts it back in charge. Nothing is
seeded on upgrade on purpose — writing the current options into this table
would freeze them, and a later change in the Supervisor form would silently do
nothing. An empty table is exactly the old behaviour.

Which keys may appear is decided in app/settings_store.py, not here. Credentials
and connection settings are never among them: HA_TOKEN, the admin login, and
DB_PATH — the database cannot tell the app where the database is.

Values are JSON so an integer survives the round trip as one.

Revision ID: 012
Revises: 011
Create Date: 2026-09-28
"""
from typing import Sequence, Union

from alembic import op

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  INTEGER NOT NULL
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app_settings")
