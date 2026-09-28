"""Interface language: negotiation, the catalogues, and pages rendered in them.

Three layers, tested separately:

- app/i18n.py picks a language from Accept-Language (and, for the dashboard,
  the pinned cookie), and merges each catalogue over English.
- The catalogues in app/locales must agree with English: the same keys, the
  same {placeholders}, the same markup in _html strings, and every plural form
  the language's CLDR rules can ask for. A missing key would only fall back to
  English, so these are what keep that fallback from being the norm.
- The pages themselves: rendered through the real routes with a browser's
  Accept-Language, and — for the parts drawn by script — run in node against
  the shipped static/util.js.
"""
import html
import json
import re
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from app import database as db
from app import guest_pin
from app import i18n
from tests.test_picker_js import DOM_STUB, REPO_SCRIPTS, _dashboard_script, node

ROOT = Path(__file__).resolve().parent.parent
LOCALES = ROOT / "app" / "locales"

# Every template and script that draws text, by audience.
GUEST_TEMPLATES = ["guest_pwa.html", "expired.html", "pin_entry.html", "device_claim.html", "device_refused.html"]
ADMIN_TEMPLATES = ["admin_dashboard.html"]

# The CLDR plural categories each guest language uses for whole numbers — what
# Intl.PluralRules(lang).select(n) can return for n = 0, 1, 2, ... Taken from
# CLDR; English and Spanish cover the dashboard too.
PLURAL_CATEGORIES = {
    "bg": {"one", "other"}, "cs": {"one", "few", "other"}, "da": {"one", "other"},
    "de": {"one", "other"}, "el": {"one", "other"}, "en": {"one", "other"},
    "es": {"one", "other"}, "et": {"one", "other"}, "fi": {"one", "other"},
    "fr": {"one", "other"}, "ga": {"one", "two", "few", "many", "other"},
    "hr": {"one", "few", "other"}, "hu": {"one", "other"}, "it": {"one", "other"},
    "lt": {"one", "few", "other"}, "lv": {"zero", "one", "other"},
    "mt": {"one", "two", "few", "many", "other"}, "nl": {"one", "other"},
    "pl": {"one", "few", "many", "other"}, "pt": {"one", "other"},
    "ro": {"one", "few", "other"}, "sk": {"one", "few", "other"},
    "sl": {"one", "two", "few", "other"}, "sv": {"one", "other"},
}


def _load(audience: str, lang: str) -> dict:
    with open(LOCALES / audience / f"{lang}.json", encoding="utf-8") as fh:
        return json.load(fh)


def _plural_bases(table: dict) -> set[str]:
    return {k.rsplit(".", 1)[0] for k in table if k.endswith(".other")}


def _split(table: dict) -> tuple[dict, dict]:
    """(plain keys, {plural base: {category: text}})."""
    bases = _plural_bases(table)
    plain, plural = {}, {}
    for key, text in table.items():
        base, _, category = key.rpartition(".")
        if base in bases and category in i18n.PLURAL_CATEGORIES:
            plural.setdefault(base, {})[category] = text
        else:
            plain[key] = text
    return plain, plural


def _placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", text))


def _tags(text: str) -> list[str]:
    return sorted(re.findall(r"</?[a-z]+", text))


# ---------------------------------------------------------------------------
# Negotiation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("header, expected", [
    (None, "en"),
    ("", "en"),
    ("es-ES,es;q=0.9,en;q=0.8", "es"),
    ("de-AT", "de"),
    ("pt-BR,pt;q=0.9", "pt"),
    # Weights win over order, and a region is matched on its language.
    ("en;q=0.5, fr-CA;q=0.9", "fr"),
    # Not an EU language: the first one that is.
    ("ja,ko;q=0.9,nl;q=0.8", "nl"),
    ("ja", "en"),
    # q=0 is "not this one", a malformed q is ignored, * matches nothing.
    ("fr;q=0, it;q=0.1", "it"),
    ("fr;q=abc, sv", "sv"),
    ("*, pl;q=0.5", "pl"),
    ("EL", "el"),
    ("ga-IE", "ga"),
])
def test_guest_negotiation(header, expected):
    assert i18n.negotiate(header, i18n.GUEST_LANGUAGES) == expected


def test_admin_negotiation_is_english_or_spanish():
    assert i18n.negotiate("de-DE,es;q=0.5", i18n.ADMIN_LANGUAGES) == "es"
    assert i18n.negotiate("de-DE", i18n.ADMIN_LANGUAGES) == "en"


def test_equal_weights_keep_the_browsers_order():
    assert i18n.parse_accept_language("da, sv, nb") == ["da", "sv", "nb"]


def test_a_hostile_header_is_bounded():
    # The guest side reads whatever a caller sends; only a prefix is parsed.
    header = ",".join(["xx"] * 10_000) + ",es"
    assert len(i18n.parse_accept_language(header)) <= 16
    assert i18n.negotiate(header, i18n.GUEST_LANGUAGES) == "en"


def test_eu_languages_are_all_there():
    assert len(i18n.GUEST_LANGUAGES) == 24
    assert set(PLURAL_CATEGORIES) == set(i18n.GUEST_LANGUAGES)


# ---------------------------------------------------------------------------
# Catalogues
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("audience, languages", [
    ("guest", i18n.GUEST_LANGUAGES),
    ("admin", i18n.ADMIN_LANGUAGES),
])
def test_every_catalogue_matches_english(audience, languages):
    en_plain, en_plural = _split(_load(audience, "en"))
    assert sorted(p.name for p in (LOCALES / audience).glob("*.json")) == sorted(
        f"{lang}.json" for lang in languages
    )
    for lang in languages:
        plain, plural = _split(_load(audience, lang))
        assert set(plain) == set(en_plain), (
            lang, sorted(set(en_plain) ^ set(plain))
        )
        assert set(plural) == set(en_plural), lang
        for key, text in plain.items():
            assert text.strip(), (lang, key)
            assert _placeholders(text) == _placeholders(en_plain[key]), (lang, key)
            if key.endswith("_html"):
                assert _tags(text) == _tags(en_plain[key]), (lang, key)
            else:
                # Markup only where the key says so; "<1m" is text, not a tag.
                assert not re.search(r"<[a-z/!]", text, re.I), (lang, key)
        for base, forms in plural.items():
            assert "other" in forms, (lang, base)
            assert set(forms) >= PLURAL_CATEGORIES[lang], (lang, base, sorted(forms))
            for category, text in forms.items():
                assert _placeholders(text) == _placeholders(en_plural[base]["other"]), (lang, base, category)


def test_english_is_the_fallback_for_a_missing_key(tmp_path, monkeypatch):
    (tmp_path / "guest").mkdir()
    (tmp_path / "guest" / "en.json").write_text('{"a": "A", "b": "B", "n.one": "x", "n.other": "y"}')
    (tmp_path / "guest" / "de.json").write_text(
        '{"a": "Ä", "stale": "gone", "n.one": "1", "n.few": "f", "n.other": "o"}'
    )
    monkeypatch.setattr(i18n, "LOCALES_DIR", tmp_path)
    i18n.strings.cache_clear()
    try:
        table = i18n.strings("guest", "de")
        # A missing key falls back, a stale one is dropped, and a plural form
        # English does not use is kept.
        assert table == {"a": "Ä", "b": "B", "n.one": "1", "n.few": "f", "n.other": "o"}
    finally:
        i18n.strings.cache_clear()
        i18n.script_strings.cache_clear()


def test_html_strings_escape_their_values_and_plain_strings_stay_text():
    t = i18n.translator(i18n.ADMIN, "en")
    rendered = t("page.entity.display_name_hint_html")
    assert str(rendered).count("<span") == 1 and hasattr(rendered, "__html__")
    # A plain string is returned as text; Jinja escapes it like any value.
    assert not hasattr(t("rel.just_now"), "__html__")
    guest_t = i18n.translator(i18n.GUEST, "en")
    assert guest_t("card.toggle", name="<b>x</b>") == "Toggle <b>x</b>"
    assert guest_t("no.such.key") == "no.such.key"


def test_html_values_are_escaped(tmp_path, monkeypatch):
    (tmp_path / "guest").mkdir()
    (tmp_path / "guest" / "en.json").write_text('{"x_html": "<b>{who}</b>"}')
    monkeypatch.setattr(i18n, "LOCALES_DIR", tmp_path)
    i18n.strings.cache_clear()
    try:
        out = i18n.translator("guest", "en")("x_html", who="<script>")
        assert str(out) == "<b>&lt;script&gt;</b>"
    finally:
        i18n.strings.cache_clear()


def test_script_catalogue_leaves_out_server_only_strings():
    for audience, lang in (("guest", "de"), ("admin", "es")):
        script = i18n.script_strings(audience, lang)
        assert script and not any(k.startswith("page.") for k in script)
        assert set(script) | {k for k in i18n.strings(audience, lang) if k.startswith("page.")} == set(
            i18n.strings(audience, lang)
        )


# The strings a template or script asks for, found by reading them. Keys built
# at runtime (a domain, an HA state word, a weekday) are checked by family.
_JINJA_KEY = re.compile(r"(?<![\w.])t\(\s*'([a-z0-9_.]+)'")
_SCRIPT_KEY = re.compile(r"\b(?:tr|trHtml|hasTr)\(\s*['\"`]([a-z0-9_.]+)['\"`]")
_SCRIPT_PLURAL = re.compile(r"\btrn\(\s*['\"`]([a-z0-9_.]+)['\"`]")
_DYNAMIC_FAMILIES = {
    "guest": ["domain.", "climate.", "cover.", "hvac.", "state.", "alarm.state.", "server."],
    "admin": ["domain.", "weekday.", "extend.label_"],
}


@pytest.mark.parametrize("audience, templates", [
    ("guest", GUEST_TEMPLATES),
    ("admin", ADMIN_TEMPLATES),
])
def test_every_key_the_pages_ask_for_exists(audience, templates):
    en = _load(audience, "en")
    sources = [(ROOT / "templates" / name).read_text() for name in templates]
    sources.append((ROOT / "static" / "domains.js").read_text())
    if audience == "guest":
        sources.append((ROOT / "app" / "routers" / "guest.py").read_text())
    jinja, script, plural = set(), set(), set()
    for src in sources:
        jinja |= set(_JINJA_KEY.findall(src))
        script |= set(_SCRIPT_KEY.findall(src))
        plural |= set(_SCRIPT_PLURAL.findall(src))
    if audience == "guest":
        jinja |= set(re.findall(r'"(page\.[a-z_.]+)"', sources[-1]))
    script = {k for k in script if not k.endswith(".")}  # tr(`domain.${d}`) etc.
    assert jinja and script
    assert not (jinja | script) - set(en), sorted((jinja | script) - set(en))
    assert all(f"{base}.other" in en for base in plural), plural
    # A script is never sent page.* keys, so it must never ask for one.
    assert not {k for k in script | plural if k.startswith("page.")}
    for family in _DYNAMIC_FAMILIES[audience]:
        assert any(k.startswith(family) for k in en), family


def test_every_domain_has_a_name():
    domains = re.search(r"const DOMAIN_ORDER = \[(.*?)\];", (ROOT / "static" / "domains.js").read_text(), re.S)
    for domain in re.findall(r"'(\w+)'", domains.group(1)):
        for audience in ("guest", "admin"):
            assert f"domain.{domain}" in _load(audience, "en"), (audience, domain)


def test_translated_server_refusals_are_ones_the_server_sends():
    page = (ROOT / "templates" / "guest_pwa.html").read_text()
    table = re.search(r"const SERVER_ERROR_KEYS = \{(.*?)\};", page, re.S).group(1)
    pairs = re.findall(r"""(['"])(.+?)\1: '([a-z_.]+)'""", table)
    assert len(pairs) >= 6
    router = (ROOT / "app" / "routers" / "guest.py").read_text()
    en = _load("guest", "en")
    for _, message, key in pairs:
        assert f'"{message}"' in router, message
        assert en[key] == message, key


# ---------------------------------------------------------------------------
# Guest pages
# ---------------------------------------------------------------------------

def _embedded_catalogue(page_html: str) -> dict:
    match = re.search(r"const I18N = (\{.*?\});\n", page_html)
    assert match, "no catalogue in the page"
    return json.loads(match.group(1))


async def test_guest_page_follows_the_browser(client, sample_token, mock_ha_client):
    resp = await client.get("/g/test-token", headers={"Accept-Language": "es-ES,es;q=0.9"})
    assert resp.status_code == 200
    assert '<html lang="es"' in resp.text
    assert "Cargando dispositivos..." in resp.text
    catalogue = _embedded_catalogue(resp.text)
    assert catalogue["lang"] == "es"
    assert catalogue["strings"]["conn.live"] == "En directo"
    assert not any(k.startswith("page.") for k in catalogue["strings"])


async def test_guest_page_is_english_by_default(client, sample_token, mock_ha_client):
    resp = await client.get("/g/test-token")
    assert '<html lang="en"' in resp.text
    assert "Loading devices..." in resp.text
    assert _embedded_catalogue(resp.text)["lang"] == "en"


async def test_unsupported_language_falls_back_to_english(client, sample_token, mock_ha_client):
    resp = await client.get("/g/test-token", headers={"Accept-Language": "ja-JP"})
    assert '<html lang="en"' in resp.text
    assert "Loading devices..." in resp.text


@pytest.mark.parametrize("lang, title", [
    ("fr", "Accès expiré"), ("pl", "Dostęp wygasł"), ("el", "Η πρόσβαση έληξε"), ("ga", "Rochtain imithe in éag"),
])
async def test_expired_page_is_translated(client, test_db, mock_ha_client, lang, title):
    await db.create_token(
        label="Gone", slug="gone-link", entity_ids=["light.a"],
        expires_at=int(time.time()) - 60, ip_allowlist=None,
    )
    resp = await client.get("/g/gone-link", headers={"Accept-Language": lang})
    assert resp.status_code == 410
    assert f'<html lang="{lang}"' in resp.text
    assert title in html.unescape(resp.text)


async def test_pin_page_and_its_error_are_translated(client, test_db, mock_ha_client):
    await db.create_token(
        label="Locked", slug="locked-link", entity_ids=["light.a"],
        expires_at=int(time.time()) + 3600, ip_allowlist=None,
        pin_hash=await guest_pin.hash_pin("4821"),
    )
    headers = {"Accept-Language": "de-DE,de;q=0.9"}
    page = await client.get("/g/locked-link", headers=headers)
    assert "PIN eingeben" in page.text
    wrong = await client.post("/g/locked-link/pin", data={"pin": "0000"}, headers=headers)
    assert wrong.status_code == 401
    assert "Falsche PIN" in wrong.text


async def test_device_claim_screen_is_translated(client, test_db, mock_ha_client):
    await db.create_token(
        label="Bound", slug="bound-link", entity_ids=["light.a"],
        expires_at=int(time.time()) + 3600, ip_allowlist=None, device_binding=True,
    )
    resp = await client.get("/g/bound-link", headers={"Accept-Language": "it"})
    assert "Usare questo dispositivo?" in resp.text
    # The chat-app hint carries markup, rendered as markup rather than text.
    assert '<span class="font-medium">Apri nel browser</span>' in resp.text


async def test_pending_banner_is_translated(client, test_db, mock_ha_client):
    now = int(time.time())
    await db.create_token(
        label="Later", slug="later-link", entity_ids=["light.a"],
        expires_at=now + 86400, ip_allowlist=None, starts_at=now + 3600,
    )
    resp = await client.get("/g/later-link", headers={"Accept-Language": "nl"})
    assert "Nog niet actief" in resp.text
    assert "Opent over" in resp.text


async def test_manifest_description_follows_the_browser(client, sample_token, mock_ha_client):
    resp = await client.get("/g/test-token/manifest.json", headers={"Accept-Language": "sv"})
    assert resp.json()["description"] == "Tillfällig styrning av hemmet"
    assert (await client.get("/g/test-token/manifest.json")).json()["description"] == "Temporary home controls"


async def test_api_refusals_stay_in_english(client, sample_token, mock_ha_client):
    # The page translates what it shows; the API answers the same to everyone.
    resp = await client.get("/g/no-such-link/state", headers={"Accept-Language": "es"})
    assert resp.status_code == 410
    assert resp.json()["detail"] == "Access unavailable"


# ---------------------------------------------------------------------------
# Admin dashboard
# ---------------------------------------------------------------------------

async def test_dashboard_follows_the_browser(client, admin_session, mock_ha_client):
    resp = await client.get(
        "/admin/dashboard", cookies=admin_session, headers={"Accept-Language": "es-MX,es;q=0.9"}
    )
    assert '<html lang="es"' in resp.text
    assert "Crear token" in resp.text
    assert "Acceso de administración" in resp.text
    catalogue = _embedded_catalogue(resp.text)
    assert catalogue["lang"] == "es" and catalogue["strings"]["rel.just_now"] == "ahora mismo"
    # The picker offers "Automatic" while nothing is pinned.
    assert re.search(r'<option value="auto" selected>', resp.text)


async def test_dashboard_is_english_for_any_other_language(client, admin_session, mock_ha_client):
    resp = await client.get("/admin/dashboard", cookies=admin_session, headers={"Accept-Language": "de"})
    assert '<html lang="en"' in resp.text
    assert "Create Token" in resp.text


async def test_pinned_language_beats_the_browser(client, admin_session, mock_ha_client):
    cookies = {**admin_session, i18n.ADMIN_LANG_COOKIE: "es"}
    resp = await client.get("/admin/dashboard", cookies=cookies, headers={"Accept-Language": "en-GB"})
    assert '<html lang="es"' in resp.text
    assert re.search(r'<option value="es" lang="es" selected>Español</option>', resp.text)


async def test_an_unknown_pin_is_ignored(client, admin_session, mock_ha_client):
    cookies = {**admin_session, i18n.ADMIN_LANG_COOKIE: "<script>"}
    resp = await client.get("/admin/dashboard", cookies=cookies, headers={"Accept-Language": "es"})
    assert '<html lang="es"' in resp.text
    assert "<script>\"" not in resp.text


async def test_the_pin_does_not_reach_guest_pages(client, sample_token, mock_ha_client):
    # An admin opening a guest link from the same browser sees what a guest sees.
    resp = await client.get(
        "/g/test-token", cookies={i18n.ADMIN_LANG_COOKIE: "es"}, headers={"Accept-Language": "fr"}
    )
    assert '<html lang="fr"' in resp.text


# ---------------------------------------------------------------------------
# Script-drawn text, run in node
# ---------------------------------------------------------------------------

def _node(script: str) -> dict:
    proc = subprocess.run(
        [node, "--input-type=module"], input=script, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _util_with(lang: str, audience: str = "guest") -> str:
    catalogue = {"lang": lang, "strings": i18n.script_strings(audience, lang)}
    parts = [DOM_STUB, f"const I18N = {json.dumps(catalogue)};"]
    for rel in REPO_SCRIPTS:
        parts.append((ROOT / rel).read_text())
    return "\n".join(parts)


@pytest.mark.skipif(node is None, reason="node is not installed")
def test_plurals_pick_the_languages_own_form():
    out = _node(_util_with("pl") + textwrap.dedent("""
    console.log(JSON.stringify({
      one: trn('command.uses_left', 1), few: trn('command.uses_left', 3),
      many: trn('command.uses_left', 5), domain: domainLabel('light'),
      unknownDomain: domainLabel('vacuum'),
    }));
    """))
    assert out["one"] == "Na tym linku pozostało 1 użycie"
    assert out["few"] == "Na tym linku pozostały 3 użycia"
    assert out["many"] == "Na tym linku pozostało 5 użyć"
    assert out["domain"] == "Światła"
    assert out["unknownDomain"] == "Vacuum"


@pytest.mark.skipif(node is None, reason="node is not installed")
def test_tr_fills_placeholders_and_trhtml_escapes_them():
    out = _node(_util_with("en") + textwrap.dedent("""
    console.log(JSON.stringify({
      plain: tr('card.toggle', { name: '<b>' }),
      missing: tr('no.such.key'),
      html: trHtml('install.ios_html', {}, { icon: '<i>icon</i>' }),
    }));
    """))
    assert out["plain"] == "Toggle <b>"
    assert out["missing"] == "no.such.key"
    assert out["html"].startswith("Tap <i>icon</i> then <strong>")


async def _dashboard_in(client, admin_session, lang: str, probe: str) -> dict:
    resp = await client.get("/admin/dashboard", cookies=admin_session, headers={"Accept-Language": lang})
    parts = [DOM_STUB] + [(ROOT / rel).read_text() for rel in REPO_SCRIPTS]
    parts += [_dashboard_script(resp.text), textwrap.dedent(probe)]
    return _node("\n".join(parts))


@pytest.mark.skipif(node is None, reason="node is not installed")
async def test_relative_times_are_whole_phrases(client, admin_session, mock_ha_client):
    probe = """
    const now = Date.now() / 1000;
    console.log(JSON.stringify({
      ago: relativeTime(now - 2 * 3600 - 5), soon: relativeTime(now + 9 * 86400 + 5),
      justNow: relativeTime(now - 5), never: relativeTime(NEVER_EXPIRES),
      bound: tr('card.device_bound', { when: relativeTime(now - 8 * 3600 - 5) }),
      entities: [trn('card.entities', 1), trn('card.entities', 4)],
      week: windowsSummary([{ weekdays: [1, 3], start: '09:00', end: '13:00' }]),
    }));
    """
    es = await _dashboard_in(client, admin_session, "es", probe)
    assert es["ago"] == "hace 2 h"
    assert es["soon"] == "en 9 d"
    assert es["justNow"] == "ahora mismo"
    assert es["never"] == "Nunca"
    assert es["bound"] == "Vinculado hace 8 h"
    assert es["entities"] == ["1 entidad", "4 entidades"]
    assert es["week"] == "mar, jue 09:00–13:00"
    en = await _dashboard_in(client, admin_session, "en", probe)
    assert en["ago"] == "2h ago" and en["soon"] == "in 9d" and en["bound"] == "Device bound 8h ago"
    assert en["entities"] == ["1 entity", "4 entities"]


@pytest.mark.skipif(node is None, reason="node is not installed")
async def test_language_cookie_is_scoped_to_the_dashboard(client, admin_session, mock_ha_client):
    out = await _dashboard_in(client, admin_session, "en", """
    console.log(JSON.stringify({
      pin: adminLanguageCookie('es', true), auto: adminLanguageCookie('auto', false),
    }));
    """)
    assert out["pin"].startswith(f"{i18n.ADMIN_LANG_COOKIE}=es;")
    assert "Path=/admin" in out["pin"] and "SameSite=Lax" in out["pin"] and "Secure" in out["pin"]
    assert "Max-Age=0" in out["auto"] and "Secure" not in out["auto"]


# Enough extra DOM for the guest page's own top level, which listens on window
# and opens a stream the moment it runs.
GUEST_STUB = """
globalThis.window.addEventListener = () => {};
globalThis.EventSource = class { addEventListener() {} close() {} };
globalThis.performance = { now: () => 0 };
"""

# One card per kind of control, in the states that draw the most text.
GUEST_PROBE = """
const probeStates = {
  'light.a': { entity_id: 'light.a', state: 'on', attributes: { friendly_name: 'Lamp' } },
  'lock.b': { entity_id: 'lock.b', state: 'locked', attributes: { friendly_name: 'Door', supported_features: 1 } },
  'climate.c': { entity_id: 'climate.c', state: 'heat', attributes: {
    friendly_name: 'Heat', hvac_modes: ['off', 'heat', 'heat_cool'], current_temperature: 20, temperature: 21,
    current_humidity: 40, hvac_action: 'heating' } },
  'alarm_control_panel.d': { entity_id: 'alarm_control_panel.d', state: 'armed_away',
    attributes: { friendly_name: 'Alarm', supported_features: 3 } },
  'cover.e': { entity_id: 'cover.e', state: 'opening', attributes: { friendly_name: 'Blind' } },
  'timer.f': { entity_id: 'timer.f', state: 'paused', attributes: { friendly_name: 'Oven', remaining: '0:05:00' } },
  'sensor.g': { entity_id: 'sensor.g', state: 'unavailable', attributes: { friendly_name: 'Temp' } },
};
let cards = '';
for (const [eid, s] of Object.entries(probeStates)) cards += buildCard(eid, s);
console.log(JSON.stringify({
  cards,
  climate: formatState('climate', 'heat', probeStates['climate.c']),
  cover: formatState('cover', 'opening', probeStates['cover.e']),
  sensor: formatState('sensor', 'unavailable', probeStates['sensor.g']),
  uses: trn('command.uses_left', 2),
  refusal: serverErrorText('You need to be at the property to use this'),
  unknownRefusal: serverErrorText('Something new'),
}));
"""


@pytest.mark.skipif(node is None, reason="node is not installed")
@pytest.mark.parametrize("lang", ["en", "sl", "mt"])
async def test_guest_cards_render_in_the_guests_language(client, sample_token, mock_ha_client, lang):
    resp = await client.get("/g/test-token", headers={"Accept-Language": lang})
    script = re.findall(r'<script nonce="[^"]*">(.*?)</script>', resp.text, re.S)[-1]
    parts = [DOM_STUB, GUEST_STUB] + [(ROOT / rel).read_text() for rel in REPO_SCRIPTS]
    out = _node("\n".join(parts + [script, GUEST_PROBE]))
    strings = i18n.strings("guest", lang)
    # Every string a card draws came out of the catalogue: no raw keys, no
    # "undefined" from a key the catalogue lacks.
    assert "undefined" not in out["cards"]
    assert not re.search(r"\b(?:state|lock|climate|alarm|cover|timer|light)\.[a-z_]+\b(?![\w-])", html.unescape(
        re.sub(r'(?:id|data-[a-z-]+|for)="[^"]*"', "", out["cards"])
    ))
    assert html.unescape(out["cards"]).count(strings["lock.tap_unlock"]) == 1
    assert strings["alarm.state.armed_away"] in out["cards"] or html.escape(strings["alarm.state.armed_away"]) in out["cards"]
    assert out["climate"] == html.escape(f"{strings['climate.heating']} · 20°", quote=False)
    assert out["cover"] == html.escape(strings["cover.opening"], quote=True)
    assert out["sensor"] == html.escape(strings["state.unavailable"], quote=True)
    assert out["refusal"] == strings["server.not_at_property"]
    assert out["unknownRefusal"] == "Something new"
    if lang == "sl":
        assert out["uses"] == strings["command.uses_left.two"].replace("{n}", "2")
