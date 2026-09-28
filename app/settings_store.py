"""Deployment settings an admin can change from the dashboard, without a restart.

Precedence, highest first:

1. An override saved from the dashboard (a row in `app_settings`).
2. The add-on option — or, standalone, the environment variable.
3. The built-in default in app/config.py.

An override is applied onto the live `settings` object, so every reader keeps
using `settings.app_name` and neither knows nor cares where the value came from.
Reverting deletes the row and puts the option's value back.

Nothing is copied into the table on upgrade. Seeding the current options as
overrides would freeze them: a later edit in the Supervisor's configuration form
would do nothing, with no hint why. So an empty table is exactly the behaviour
before this existed, and a row exists only because an admin saved one. For the
same reason, saving a value equal to the option clears the override instead of
pinning it — "overridden" always means "differs from the add-on option".

What is editable is the field list of SettingsUpdateRequest and nothing else.
Deliberately absent, and staying add-on options or environment variables:

- admin_username / admin_password. A typo would lock the only admin out of the
  one place that could fix it, and the password is hashed once at import.
- ha_base_url / ha_token / supervisor_token. The HA client and WebSocket are
  built from them at startup, and they are the keys to the whole house: a
  dashboard session must not be able to repoint or read them.
- db_path. The database cannot tell the app where the database is.
- The guest rate limits. They are constants on purpose, not options at all —
  see app/routers/guest.py — and a panel is not the place to loosen them.
"""
import logging
from typing import Any

from pydantic import ValidationError

from app import database as db
from app.config import settings
from app.models import SettingsUpdateRequest

logger = logging.getLogger(__name__)

EDITABLE: tuple[str, ...] = tuple(SettingsUpdateRequest.model_fields)

# What the process was configured with, captured before any override touches
# the live object. It is both what "revert" restores and what the dashboard
# shows as the add-on option beside an overridden value.
_option_values: dict[str, Any] = {name: getattr(settings, name) for name in EDITABLE}
_overridden: set[str] = set()


def _validated(key: str, value: Any) -> Any:
    """Run a stored value through the same checks a save gets.

    Load-time values come from the database, not a request, but they are about
    to be rendered into guest pages and a <style> block all the same, so a row
    that would not pass today's validation is not applied.
    """
    try:
        return getattr(SettingsUpdateRequest.model_validate({key: value}), key)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc


def _restore_options() -> None:
    for name, value in _option_values.items():
        setattr(settings, name, value)
    _overridden.clear()


async def load() -> None:
    """Apply stored overrides onto the live settings. Called once at startup.

    A row that is unknown or no longer valid is skipped with a warning rather
    than failing startup: the option it overrode is still there to fall back on.
    It stays in the table so a downgrade-then-upgrade does not lose it.
    """
    _restore_options()
    for key, value in (await db.get_app_settings()).items():
        if key not in EDITABLE:
            logger.warning("Ignoring stored setting %r — it is not editable", key)
            continue
        try:
            value = _validated(key, value)
        except ValueError:
            logger.warning("Ignoring stored setting %r — its value is no longer valid", key)
            continue
        setattr(settings, key, value)
        _overridden.add(key)
    if _overridden:
        logger.info("Applied dashboard overrides for: %s", ", ".join(sorted(_overridden)))


async def apply(changes: dict[str, Any]) -> None:
    """Persist and apply already-validated changes.

    A value equal to the add-on option is a revert, not an override — see the
    module docstring. The database is written before the live object, so a
    failed write leaves the running app as it was.
    """
    to_store = {k: v for k, v in changes.items() if v != _option_values[k]}
    to_clear = [k for k in changes if k not in to_store]
    if to_store:
        await db.set_app_settings(to_store)
    for key in to_clear:
        await db.delete_app_setting(key)
    for key, value in changes.items():
        setattr(settings, key, value)
    _overridden.update(to_store)
    _overridden.difference_update(to_clear)


async def revert(key: str) -> None:
    """Drop one override so the add-on option applies again."""
    if key not in EDITABLE:
        raise KeyError(key)
    await db.delete_app_setting(key)
    setattr(settings, key, _option_values[key])
    _overridden.discard(key)


def snapshot() -> dict[str, dict[str, Any]]:
    """Every editable setting: the live value, the option under it, and whether
    the dashboard is overriding it."""
    return {
        name: {
            "value": getattr(settings, name),
            "option": _option_values[name],
            "overridden": name in _overridden,
        }
        for name in EDITABLE
    }


def reset_state() -> None:
    """For tests: put the configured options back without touching the database."""
    _restore_options()
