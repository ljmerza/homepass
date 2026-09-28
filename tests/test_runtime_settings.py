"""Deployment settings editable from the dashboard (app/settings_store.py).

Precedence under test: a saved override beats the add-on option, reverting puts
the option back, and only the allow-listed keys can be touched at all. The
overrides mutate the process-wide `settings` object, so every test here starts
and ends with the configured options restored.
"""
import time

import pytest

from app import database as db
from app import settings_store
from app.config import settings


@pytest.fixture(autouse=True)
def _restore_settings():
    settings_store.reset_state()
    yield
    settings_store.reset_state()


async def _patch(client, admin_session, body):
    return await client.patch("/admin/settings", json=body, cookies=admin_session)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def test_settings_require_admin(client):
    assert (await client.get("/admin/settings")).status_code == 401
    assert (await client.patch("/admin/settings", json={"app_name": "X"})).status_code == 401
    assert (await client.delete("/admin/settings/app_name")).status_code == 401
    assert settings.app_name == settings_store._option_values["app_name"]


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

async def test_read_lists_exactly_the_editable_keys(client, admin_session):
    resp = await client.get("/admin/settings", cookies=admin_session)
    assert resp.status_code == 200
    data = resp.json()["settings"]
    assert set(data) == {
        "app_name", "contact_message", "brand_bg", "brand_primary",
        "guest_url", "access_log_retention_days",
    }
    assert data["app_name"] == {
        "value": settings.app_name, "option": settings.app_name, "overridden": False,
    }


async def test_read_never_exposes_credentials(client, admin_session):
    body = (await client.get("/admin/settings", cookies=admin_session)).text
    assert settings.admin_password not in body
    assert settings.ha_token not in body


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

async def test_save_applies_live_and_persists(client, admin_session):
    resp = await _patch(client, admin_session, {"app_name": "  Lake House  "})
    assert resp.status_code == 200
    entry = resp.json()["settings"]["app_name"]
    assert entry["value"] == "Lake House"
    assert entry["overridden"] is True
    assert entry["option"] == settings_store._option_values["app_name"]
    assert settings.app_name == "Lake House"
    assert (await db.get_app_settings()) == {"app_name": "Lake House"}


async def test_save_is_partial(client, admin_session):
    before = settings.contact_message
    await _patch(client, admin_session, {"app_name": "Lake House"})
    assert settings.contact_message == before
    assert set(await db.get_app_settings()) == {"app_name"}


async def test_saving_the_option_value_clears_the_override(client, admin_session):
    """Otherwise the row would pin today's option and a later Supervisor edit
    would silently do nothing."""
    option = settings_store._option_values["app_name"]
    await _patch(client, admin_session, {"app_name": "Lake House"})
    resp = await _patch(client, admin_session, {"app_name": option})
    assert resp.json()["settings"]["app_name"]["overridden"] is False
    assert await db.get_app_settings() == {}


async def test_empty_save_is_refused(client, admin_session):
    assert (await _patch(client, admin_session, {})).status_code == 400


@pytest.mark.parametrize("key", [
    "admin_username", "admin_password", "ha_base_url", "ha_token",
    "db_path", "supervisor_token",
])
async def test_credentials_and_connection_settings_are_not_editable(client, admin_session, key):
    before = getattr(settings, key)
    resp = await _patch(client, admin_session, {key: "http://attacker.example"})
    assert resp.status_code == 422
    assert getattr(settings, key) == before
    assert await db.get_app_settings() == {}


async def test_one_bad_field_rejects_the_whole_save(client, admin_session):
    before = settings.app_name
    resp = await _patch(client, admin_session, {"app_name": "Fine", "brand_bg": "red"})
    assert resp.status_code == 422
    assert settings.app_name == before
    assert await db.get_app_settings() == {}


@pytest.mark.parametrize("body", [
    {"app_name": "   "},
    {"app_name": "x" * 65},
    {"app_name": None},
    {"contact_message": ""},
    {"contact_message": "x" * 501},
    {"brand_bg": "#FFF"},
    {"brand_primary": "D9523C"},
    {"brand_primary": "#D9523C;}</style><script>"},
    {"guest_url": "guest.example.com"},
    {"guest_url": "javascript:alert(1)"},
    {"guest_url": "https://guest.example.com/?x=1"},
    {"guest_url": "https://guest.example.com/#frag"},
    {"guest_url": "https://user:pass@guest.example.com"},
    {"guest_url": "https://guest.example.com/';alert(1);'"},
    {"guest_url": "https://guest.example.com:99999"},
    {"access_log_retention_days": 0},
    {"access_log_retention_days": 3651},
    {"access_log_retention_days": True},
    {"access_log_retention_days": "30"},
])
async def test_invalid_values_are_refused(client, admin_session, body):
    resp = await _patch(client, admin_session, body)
    assert resp.status_code == 422
    assert await db.get_app_settings() == {}


async def test_guest_url_is_normalised(client, admin_session):
    resp = await _patch(client, admin_session, {"guest_url": " https://guest.example.com/ "})
    assert resp.status_code == 200
    assert settings.guest_url == "https://guest.example.com"


async def test_colours_are_stored_in_one_spelling(client, admin_session):
    await _patch(client, admin_session, {"brand_primary": "#00aa11"})
    assert settings.brand_primary == "#00AA11"


# ---------------------------------------------------------------------------
# Revert
# ---------------------------------------------------------------------------

async def test_revert_restores_the_option(client, admin_session):
    option = settings_store._option_values["app_name"]
    await _patch(client, admin_session, {"app_name": "Lake House"})
    resp = await client.delete("/admin/settings/app_name", cookies=admin_session)
    assert resp.status_code == 200
    assert resp.json()["settings"]["app_name"] == {
        "value": option, "option": option, "overridden": False,
    }
    assert settings.app_name == option
    assert await db.get_app_settings() == {}


@pytest.mark.parametrize("key", ["admin_password", "ha_token", "nonsense"])
async def test_revert_refuses_non_editable_keys(client, admin_session, key):
    resp = await client.delete(f"/admin/settings/{key}", cookies=admin_session)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# The overrides actually reach what they configure
# ---------------------------------------------------------------------------

async def test_app_name_reaches_guest_pages_and_manifest(client, admin_session, sample_token):
    await _patch(client, admin_session, {"app_name": "Lake House"})
    page = await client.get("/g/test-token")
    assert "Lake House" in page.text
    manifest = (await client.get("/g/test-token/manifest.json")).json()
    assert manifest["name"] == "Lake House"


async def test_contact_message_reaches_the_expired_page(client, admin_session):
    await _patch(client, admin_session, {"contact_message": "Text Sam on 555-0100."})
    resp = await client.get("/g/no-such-link")
    assert resp.status_code == 410
    assert "Text Sam on 555-0100." in resp.text


async def test_brand_colours_change_the_palette_without_a_restart(client, admin_session, sample_token):
    await _patch(client, admin_session, {"brand_primary": "#112233"})
    page = await client.get("/g/test-token")
    assert "--color-primary: 17 34 51;" in page.text
    manifest = (await client.get("/g/test-token/manifest.json")).json()
    assert manifest["theme_color"] == "#112233"

    await client.delete("/admin/settings/brand_primary", cookies=admin_session)
    page = await client.get("/g/test-token")
    assert "--color-primary: 17 34 51;" not in page.text


async def test_guest_url_reaches_the_dashboard(client, admin_session):
    await _patch(client, admin_session, {"guest_url": "https://guest.example.com"})
    resp = await client.get("/admin/dashboard", cookies=admin_session)
    assert 'const GUEST_URL = "https://guest.example.com";' in resp.text


async def test_retention_is_what_cleanup_uses(client, admin_session):
    await _patch(client, admin_session, {"access_log_retention_days": 7})
    assert settings.access_log_retention_days == 7
    token = await db.create_token(
        label="t", slug="retention", entity_ids=["light.x"],
        expires_at=int(time.time()) + 3600, ip_allowlist=None,
    )
    await db.log_access(token_id=token["id"], event_type="page_load")
    conn = await db.get_db()
    await conn.execute("UPDATE access_log SET timestamp = ?", (int(time.time()) - 8 * 86400,))
    await conn.commit()
    await db.cleanup_old_data(settings.access_log_retention_days)
    assert await db.list_access_logs() == []


# ---------------------------------------------------------------------------
# Startup load
# ---------------------------------------------------------------------------

async def test_load_applies_stored_overrides(test_db):
    await db.set_app_settings({"app_name": "Lake House", "access_log_retention_days": 14})
    await settings_store.load()
    assert settings.app_name == "Lake House"
    assert settings.access_log_retention_days == 14
    snap = settings_store.snapshot()
    assert snap["app_name"]["overridden"] is True
    assert snap["contact_message"]["overridden"] is False


async def test_load_skips_unknown_and_invalid_rows(test_db):
    """A row planted in the table outside the API is still validated before it
    is rendered into a page."""
    await db.set_app_settings({
        "ha_token": "stolen",
        "brand_bg": "#000;}</style><script>alert(1)</script>",
        "app_name": "Lake House",
    })
    await settings_store.load()
    assert settings.ha_token != "stolen"
    assert settings.brand_bg == settings_store._option_values["brand_bg"]
    assert settings.app_name == "Lake House"
    assert set(k for k, v in settings_store.snapshot().items() if v["overridden"]) == {"app_name"}


async def test_load_with_an_empty_table_is_the_old_behaviour(test_db):
    await settings_store.load()
    for name, entry in settings_store.snapshot().items():
        assert entry["overridden"] is False
        assert entry["value"] == settings_store._option_values[name]


# ---------------------------------------------------------------------------
# Dashboard dialog (the shipped inline script, run in node)
# ---------------------------------------------------------------------------

from tests.test_picker_js import _run, node  # noqa: E402

_needs_node = pytest.mark.skipif(node is None, reason="node is not installed")

_SETTINGS_SEED = """
settingsState = {
  app_name: { value: 'Home Access', option: 'Home Access', overridden: false },
  contact_message: { value: 'Ask Sam', option: 'Please request a new link.', overridden: true },
  guest_url: { value: '', option: '', overridden: false },
  brand_primary: { value: '#D9523C', option: '#D9523C', overridden: false },
  brand_bg: { value: '#F2F0E9', option: '#F2F0E9', overridden: false },
  access_log_retention_days: { value: 90, option: 90, overridden: false },
};
const put = (key, v) => { document.getElementById('set-' + key).value = v; };
const fill = () => {
  put('app_name', 'Home Access'); put('contact_message', 'Ask Sam'); put('guest_url', '');
  put('brand_primary', '#d9523c'); put('brand_bg', '#f2f0e9');
  put('access_log_retention_days', '90');
};
"""


@_needs_node
async def test_dialog_sends_only_what_changed(client, admin_session, mock_ha_client):
    """An untouched field must not become an override just because the dialog
    was saved with it on screen — including a colour the picker lower-cased."""
    out = await _run(client, admin_session, _SETTINGS_SEED + """
    fill();
    const untouched = collectSettings();
    put('app_name', '  Lake House '); put('access_log_retention_days', '30');
    const edited = collectSettings();
    put('access_log_retention_days', '3.5');
    let error = null;
    try { collectSettings(); } catch (e) { error = e.message; }
    console.log(JSON.stringify({ untouched, edited, error }));
    """)
    assert out["untouched"] == {}
    assert out["edited"] == {"app_name": "Lake House", "access_log_retention_days": 30}
    assert "whole number" in out["error"]


@_needs_node
async def test_dialog_offers_revert_only_on_overridden_fields(client, admin_session, mock_ha_client):
    out = await _run(client, admin_session, _SETTINGS_SEED + """
    renderSettings();
    const html = document.getElementById('settings-fields').innerHTML;
    console.log(JSON.stringify({
      reverts: (html.match(/data-action="revert-setting"/g) || []).length,
      key: html.includes('data-key="contact_message"'),
      option: html.includes('Please request a new link.'),
    }));
    """)
    assert out == {"reverts": 1, "key": True, "option": True}
