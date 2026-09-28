"""Interface language: which one a request gets, and the strings for it.

Two audiences, two rules:

The guest side — the guest PWA and every page a guest link can land on
(expired, PIN entry, the device claim screen and its refusal) — speaks all 24
official languages of the European Union and follows the guest's browser, with
no way to pick another. A guest link belongs to nobody who would keep a
preference on it, and the one fact the page has about its reader is what their
browser says they read.

The admin dashboard speaks English and Spanish. It follows the browser too,
but an admin can pin one from the Settings dialog: the dashboard is often
opened from a shared machine or through Home Assistant's own frame, and a
browser's languages are not always the one its user wants to manage the house
in. The pin is a cookie scoped to the dashboard, set by the page itself, so it
lives with the browser that chose it and needs no server-side state.

Strings live in app/locales/<audience>/<lang>.json, one flat object of dotted
keys each. English is the reference: every other catalogue is merged over it,
so a key a translation is missing still renders — in English — rather than as
a blank or a bare key, and tests/test_i18n.py keeps every catalogue's keys in
step with English's so that fallback is a safety net and not the norm.

Placeholders are ``{name}``, replaced with str.replace rather than str.format:
a stray brace in a translation must not be able to raise at render time.

Keys ending in ``_html`` carry markup and are the only ones that do. Their text
is trusted — it comes from these files, not from a request — but every value
substituted into them is escaped, so a token label or an entity name dropped
into one cannot inject anything. Every other key is plain text and is escaped
by Jinja like any other value.

No gettext, no .po files, no dependency: the catalogues are small, loaded once
at import, and the same JSON is handed to the browser for the parts of both
pages that are drawn by script.
"""
import json
import logging
from functools import lru_cache
from pathlib import Path

from fastapi import Request
from markupsafe import Markup, escape

logger = logging.getLogger(__name__)

LOCALES_DIR = Path(__file__).resolve().parent / "locales"

DEFAULT_LANGUAGE = "en"

GUEST = "guest"
ADMIN = "admin"

# The 24 official languages of the EU, by ISO 639-1 code.
GUEST_LANGUAGES = (
    "bg", "cs", "da", "de", "el", "en", "es", "et", "fi", "fr", "ga", "hr",
    "hu", "it", "lt", "lv", "mt", "nl", "pl", "pt", "ro", "sk", "sl", "sv",
)
ADMIN_LANGUAGES = ("en", "es")

SUPPORTED = {GUEST: GUEST_LANGUAGES, ADMIN: ADMIN_LANGUAGES}

# What the admin's language picker lists, each in its own language: someone
# who cannot read the current one still has to be able to find theirs.
LANGUAGE_NAMES = {"en": "English", "es": "Español"}

# The admin's pinned language. Written by the dashboard's own script (see the
# language picker in templates/admin_dashboard.html) and only ever read here,
# where anything but a supported code is ignored — so a hand-edited cookie can
# at worst fall back to the browser's language.
ADMIN_LANG_COOKIE = "homepass_admin_lang"

# Accept-Language is attacker-controlled on the guest side, so only a bounded
# prefix of it is looked at. Real browsers send a handful of ranges in well
# under a hundred characters.
_MAX_HEADER_LENGTH = 512
_MAX_RANGES = 16


def parse_accept_language(header: str | None) -> list[str]:
    """Language ranges from an Accept-Language header, most preferred first.

    Lower-cased; ranges with q=0 (explicitly unwanted), a malformed q, or the
    ``*`` wildcard are dropped. Equal weights keep the order they were sent in,
    which is the order a browser lists its user's languages.
    """
    if not header:
        return []
    weighted: list[tuple[float, int, str]] = []
    for index, part in enumerate(header[:_MAX_HEADER_LENGTH].split(",")[:_MAX_RANGES]):
        tag, _, params = part.partition(";")
        tag = tag.strip().lower()
        if not tag or tag == "*":
            continue
        q = 1.0
        for param in params.split(";"):
            name, _, value = param.partition("=")
            if name.strip().lower() == "q":
                try:
                    q = float(value.strip())
                except ValueError:
                    q = 0.0
        if not 0.0 < q <= 1.0:
            continue
        weighted.append((-q, index, tag))
    weighted.sort()
    return [tag for _, _, tag in weighted]


def negotiate(header: str | None, supported: tuple[str, ...]) -> str:
    """The first language the header asks for that `supported` has.

    Matched on the primary subtag, so ``pt-BR`` gets Portuguese and ``de-AT``
    German — a regional variant is always closer to what the reader wants than
    the English fallback is.
    """
    for tag in parse_accept_language(header):
        primary = tag.split("-", 1)[0]
        if primary in supported:
            return primary
    return DEFAULT_LANGUAGE


def guest_language(request: Request) -> str:
    return negotiate(request.headers.get("accept-language"), GUEST_LANGUAGES)


def admin_language_pin(request: Request) -> str | None:
    """The admin's pinned language, or None to follow the browser."""
    pinned = request.cookies.get(ADMIN_LANG_COOKIE)
    return pinned if pinned in ADMIN_LANGUAGES else None


def admin_language(request: Request) -> str:
    return admin_language_pin(request) or negotiate(
        request.headers.get("accept-language"), ADMIN_LANGUAGES
    )


def language_for(request: Request, audience: str) -> str:
    return admin_language(request) if audience == ADMIN else guest_language(request)


def _load(audience: str, lang: str) -> dict[str, str]:
    with open(LOCALES_DIR / audience / f"{lang}.json", encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=None)
def strings(audience: str, lang: str) -> dict[str, str]:
    """Every string for one audience in one language, English filling any gap.

    Cached per (audience, language): the files do not change while the app
    runs, and the merged table is handed whole to each page's script.
    """
    reference = _load(audience, DEFAULT_LANGUAGE)
    if lang == DEFAULT_LANGUAGE:
        return reference
    table = _load(audience, lang)
    missing = reference.keys() - table.keys()
    if missing:
        # Not fatal: the English text renders instead. The key-parity test is
        # what keeps this from ever shipping.
        logger.warning(
            "%s catalogue %r is missing %d key(s); English is used for them",
            audience, lang, len(missing),
        )
    merged = dict(reference)
    for key, value in table.items():
        # A key English does not have is stale; a plural category English
        # does not use (Polish "few") is not.
        if key in reference or _is_plural_variant(key, reference):
            merged[key] = value
    return merged


# CLDR plural categories. English only ever needs one/other, but Polish needs
# few and many, Slovenian two, Irish all five — so a plural key is a family
# (``key.one``, ``key.other``, ...) and a translation carries whichever members
# its language uses. The browser picks one with Intl.PluralRules; see trn() in
# static/util.js.
PLURAL_CATEGORIES = ("zero", "one", "two", "few", "many", "other")


def _is_plural_variant(key: str, reference: dict[str, str]) -> bool:
    base, _, category = key.rpartition(".")
    return category in PLURAL_CATEGORIES and f"{base}.other" in reference


# Keys under "page." are rendered by the server and nowhere else — the pages a
# guest link can land on besides the app (expired, PIN entry, the device claim
# screen and its refusal), and the parts of the app and the dashboard that the
# template draws rather than the script. They are left out of the catalogue a
# page's script receives. Partly weight: the guest page is fetched by every
# visitor. Mostly so a page's source says what the page says. With every string
# inlined, a live link's source would contain "Enter PIN" and a pending
# banner's wording whether or not either was on screen, and "the page does not
# show X" could no longer be checked by looking for X. tests/test_i18n.py keeps
# a script from asking for a key it will never be sent.
SCRIPT_EXCLUDED_PREFIX = "page."


@lru_cache(maxsize=None)
def script_strings(audience: str, lang: str) -> dict[str, str]:
    """The part of strings() a page's script can use."""
    return {
        k: v for k, v in strings(audience, lang).items()
        if not k.startswith(SCRIPT_EXCLUDED_PREFIX)
    }


def _substitute(text: str, params: dict, html: bool) -> str:
    for name, value in params.items():
        text = text.replace("{" + name + "}", str(escape(value)) if html else str(value))
    return text


def translator(audience: str, lang: str):
    """A ``t(key, **params)`` for templates, bound to one language.

    A key missing everywhere comes back as the key itself: visibly wrong on the
    page, which is what makes a typo get noticed.
    """
    table = strings(audience, lang)

    def t(key: str, **params):
        text = table.get(key, key)
        if key.endswith("_html"):
            return Markup(_substitute(text, params, html=True))
        return _substitute(text, params, html=False)

    return t


def template_context(request: Request, audience: str) -> dict:
    """What a page needs to render in the request's language.

    ``t`` is for the template itself; ``i18n`` is the same catalogue for the
    page's script, emitted with tojson and read by tr() in static/util.js.
    """
    lang = language_for(request, audience)
    ctx = {
        "lang": lang,
        "t": translator(audience, lang),
        "i18n": {"lang": lang, "strings": script_strings(audience, lang)},
    }
    if audience == ADMIN:
        ctx["admin_lang_pin"] = admin_language_pin(request)
        ctx["admin_languages"] = [(code, LANGUAGE_NAMES[code]) for code in ADMIN_LANGUAGES]
        ctx["admin_lang_cookie"] = ADMIN_LANG_COOKIE
    return ctx
