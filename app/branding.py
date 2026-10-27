"""Per-link branding: an eyebrow line, a title, a logo and, optionally, colours.

Every part is optional. A link with none of them looks exactly as it did before.

What is stored, in the ``tokens.branding`` JSON column (``{}`` is unbranded)::

    {"eyebrow": "Harvest Host", "title": "Be-Nith the Locust Trees",
     "logo_height": 40, "title_size": 16, "eyebrow_size": 12,
     "colors": {"brand_primary": "#D9523C", "brand_bg": "#F2F0E9", ...},
     "logo": {"v": "3f2a9c1b7d04", "width": 256, "height": 256}}

``colors`` is None (absent) while the link uses the global colours. Otherwise it
holds all nine colour settings of app/theme.py, with "" meaning Auto for the
seven optional ones. ``logo`` only says that a logo exists and which one: the
image itself is in the ``token_logos`` table, so listing links never loads it.

Logos are PNG or WebP only. Both can be fully checked here without an image
library, and neither can carry script, which is why SVG is not accepted.
"""
import json
import re
import struct
import unicodedata
import zlib

from app import theme
from app.config import settings
from app.theme import Brand, brand_theme

EYEBROW_MAX = 40
TITLE_MAX = 80

LOGO_HEIGHT_MIN = 24
LOGO_HEIGHT_MAX = 64
LOGO_HEIGHT_DEFAULT = 40
# Text sizes in px for the pinned header. The defaults are the size the header
# has always used (16 for the label, 12 for the line under it).
TITLE_SIZE_MIN, TITLE_SIZE_MAX, TITLE_SIZE_DEFAULT = 14, 28, 16
EYEBROW_SIZE_MIN, EYEBROW_SIZE_MAX, EYEBROW_SIZE_DEFAULT = 10, 18, 12
# A wide logo is held to this share of the page so it cannot squeeze the
# eyebrow beside it off the row.
LOGO_MAX_WIDTH_PERCENT = 50

LOGO_MAX_BYTES = 512 * 1024
# Room for the multipart framing around the file in an upload.
LOGO_UPLOAD_SLACK = 16 * 1024
LOGO_MIN_SIDE = 32
LOGO_MAX_SIDE = 1024
# Sent with the logo by the admin and guest routes. The bytes were checked as
# PNG or WebP at upload; this keeps a browser from reading them as anything
# else, and from running them if it ever did.
LOGO_RESPONSE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Cache-Control": "private, max-age=31536000, immutable",
}

# One list of the color settings lives in app/theme.py.
COLOR_KEYS = theme.COLOR_KEYS
REQUIRED_COLOR_KEYS = theme.REQUIRED_KEYS

_HEX = re.compile(r"#[0-9A-Fa-f]{6}")


class LogoError(ValueError):
    """An uploaded logo that cannot be accepted. The message is for the admin."""


# ── text and colour checks (shared by the request models) ─────────────────

class WrongType(TypeError, ValueError):
    """A value of the wrong type. It is a TypeError for what it is, and also a
    ValueError so a pydantic validator turns it into a 422 and the callers that
    catch ValueError keep working."""


def clean_line(value):
    """One line of plain text. Line breaks, tabs and other control or format
    characters (which include the invisible ones that reorder text) become
    spaces, and runs of spaces collapse. Case is left exactly as typed."""
    if value is None:
        return value
    if not isinstance(value, str):
        raise WrongType("must be text")
    text = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in value)
    return " ".join(text.split())


def clean_colors(value):
    """The link's own colours as a full nine-key dict, or None for "use the
    global colours". Primary and background are required; the rest may be ""
    (Auto)."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise WrongType("colors must be an object")
    unknown = set(value) - set(COLOR_KEYS)
    if unknown:
        raise ValueError("unknown colour: " + ", ".join(sorted(map(str, unknown))))
    out = {}
    for key in COLOR_KEYS:
        raw = value.get(key, "")
        if raw is None:
            raw = ""
        if not isinstance(raw, str):
            raise WrongType(f"{key} must be text")
        raw = raw.strip()
        if raw == "":
            if key in REQUIRED_COLOR_KEYS:
                raise ValueError(f"{key} is required when a link has its own colours")
            out[key] = ""
        elif _HEX.fullmatch(raw):
            out[key] = raw.upper()
        else:
            raise ValueError(f"{key} must be a colour like #1A2B3C")
    return out


# ── the stored value ──────────────────────────────────────────────────────

def _size(value, low, high, default) -> int:
    """A stored text size: a whole number held to its range, else the default."""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(low, min(high, value))


def from_row(raw) -> dict:
    """The stored branding with every key present.

    The column is written only through the validated request models, so an
    unreadable value means a hand-edited database. It reads as "unbranded"
    rather than failing a guest page: branding is presentation, and nothing
    about access depends on it.
    """
    try:
        value = json.loads(raw or "{}")
    except (ValueError, TypeError):
        value = {}
    if not isinstance(value, dict):
        value = {}

    def text(key, limit):
        try:
            cleaned = clean_line(value.get(key, "")) or ""
        except ValueError:
            return ""
        return cleaned[:limit]

    # An older, taller setting is held to the new limit, not thrown away.
    height = _size(value.get("logo_height"), LOGO_HEIGHT_MIN, LOGO_HEIGHT_MAX, LOGO_HEIGHT_DEFAULT)
    title_size = _size(value.get("title_size"), TITLE_SIZE_MIN, TITLE_SIZE_MAX, TITLE_SIZE_DEFAULT)
    eyebrow_size = _size(value.get("eyebrow_size"), EYEBROW_SIZE_MIN, EYEBROW_SIZE_MAX, EYEBROW_SIZE_DEFAULT)
    try:
        colors = clean_colors(value.get("colors"))
    except ValueError:
        colors = None
    logo = value.get("logo")
    if not (
        isinstance(logo, dict)
        and isinstance(logo.get("v"), str) and re.fullmatch(r"[0-9a-f]{12}", logo["v"])
        and isinstance(logo.get("width"), int) and logo["width"] > 0
        and isinstance(logo.get("height"), int) and logo["height"] > 0
    ):
        logo = None
    else:
        logo = {"v": logo["v"], "width": logo["width"], "height": logo["height"]}
    return {
        "eyebrow": text("eyebrow", EYEBROW_MAX),
        "title": text("title", TITLE_MAX),
        "logo_height": height,
        "title_size": title_size,
        "eyebrow_size": eyebrow_size,
        "colors": colors,
        "logo": logo,
    }


def stored(branding: dict) -> dict:
    """The dict to write: only what differs from unbranded, so ``{}`` is none."""
    out = {}
    if branding.get("eyebrow"):
        out["eyebrow"] = branding["eyebrow"]
    if branding.get("title"):
        out["title"] = branding["title"]
    if branding.get("logo_height", LOGO_HEIGHT_DEFAULT) != LOGO_HEIGHT_DEFAULT:
        out["logo_height"] = branding["logo_height"]
    if branding.get("title_size", TITLE_SIZE_DEFAULT) != TITLE_SIZE_DEFAULT:
        out["title_size"] = branding["title_size"]
    if branding.get("eyebrow_size", EYEBROW_SIZE_DEFAULT) != EYEBROW_SIZE_DEFAULT:
        out["eyebrow_size"] = branding["eyebrow_size"]
    if branding.get("colors"):
        out["colors"] = branding["colors"]
    if branding.get("logo"):
        out["logo"] = branding["logo"]
    return out


def to_json(branding: dict) -> str:
    return json.dumps(stored(branding), separators=(",", ":"), ensure_ascii=False)


def public(raw) -> dict:
    """The branding as the dashboard and the API show it."""
    b = from_row(raw)
    logo = b["logo"]
    return {
        "eyebrow": b["eyebrow"],
        "title": b["title"],
        "logo_height": b["logo_height"],
        "title_size": b["title_size"],
        "eyebrow_size": b["eyebrow_size"],
        "colors": b["colors"],
        "logo": None if logo is None else {
            "version": logo["v"], "width": logo["width"], "height": logo["height"],
        },
    }


# ── what a guest page needs ───────────────────────────────────────────────

def brand_from_colors(colors: dict) -> Brand:
    return theme.brand_from_colors(colors)


# Which of the link's own colours an Auto one follows. The two groups are kept
# apart on purpose: changing the Primary leaves the surfaces and text on the
# global setting, and changing the Background leaves Secondary and Accent there.
_AUTO_FOLLOWS_PRIMARY = ("brand_secondary", "brand_accent")
_AUTO_FOLLOWS_BG = ("brand_surface", "brand_text", "brand_dark_bg", "brand_dark_surface", "brand_dark_text")


def global_colors() -> dict:
    """The global colour settings as they are right now."""
    return {key: (getattr(settings, key, "") or "") for key in COLOR_KEYS}


def effective_colors(colors: dict, global_values: dict | None = None) -> dict:
    """The link's colours with each Auto settled.

    An Auto colour follows the global setting for it, so a link that only
    changes the Background keeps the Secondary and Accent chosen globally, and
    one that only changes the Primary keeps the global Card, Text and dark
    colours. An Auto colour is worked out from the link's own colours instead
    once the one its group depends on differs from the global one: Primary for
    Secondary and Accent, Background for the rest. An optional colour the global
    settings also leave on Auto stays blank, and the palette works it out as
    usual.
    """
    g = global_values if global_values is not None else global_colors()

    def differs(key: str) -> bool:
        return colors[key].strip().upper() != (g.get(key) or "").strip().upper()

    out = dict(colors)
    for keys, anchor in ((_AUTO_FOLLOWS_PRIMARY, "brand_primary"), (_AUTO_FOLLOWS_BG, "brand_bg")):
        if differs(anchor):
            continue
        for key in keys:
            if not out[key]:
                out[key] = (g.get(key) or "").upper()
    return out


def link_brand(raw) -> Brand | None:
    """The link's own palette, or None while it uses the global colours."""
    colors = from_row(raw)["colors"]
    return None if colors is None else brand_from_colors(effective_colors(colors))


def page_context(row, base_path: str, slug: str) -> dict:
    """Template context for one link's pages: its palette and its header.

    Merged over ``base_context`` by the routes that render a link's own page.
    ``branding`` is None when the link has no logo, eyebrow or title, and the
    templates draw nothing then. The palette keys are only set when the link
    has colours of its own, so the global ones stay in effect otherwise.
    """
    b = from_row(row["branding"])
    ctx: dict = {"branding": None}
    if b["colors"] is not None:
        css, dark = brand_theme(brand_from_colors(effective_colors(b["colors"])))
        ctx.update(
            brand_css=css,
            brand_bg_dark=dark,
            brand_bg=b["colors"]["brand_bg"],
            brand_primary=b["colors"]["brand_primary"],
        )
    if b["eyebrow"] or b["title"] or b["logo"]:
        logo = b["logo"]
        ctx["branding"] = {
            "eyebrow": b["eyebrow"],
            "title": b["title"],
            "logo_url": f"{base_path}/g/{slug}/logo?v={logo['v']}" if logo else None,
            # The width the chosen height gives this image, and its shape, so the
            # page reserves the space before the image arrives and a wide logo
            # that hits the width cap shrinks without distortion.
            "logo_width": round(logo["width"] * b["logo_height"] / logo["height"]) if logo else None,
            "logo_ratio": f"{logo['width']} / {logo['height']}" if logo else None,
            "logo_max_width": LOGO_MAX_WIDTH_PERCENT,
            "title_size": b["title_size"],
            "eyebrow_size": b["eyebrow_size"],
        }
    return ctx


# ── logo files ────────────────────────────────────────────────────────────

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_size(data: bytes) -> tuple[int, int]:
    """Walk every chunk of a PNG, checking lengths and CRCs, and return its size.

    Stricter than a browser needs to be on purpose: a file that does not end
    where its last chunk says, or that carries animation, is refused instead
    of guessed at.
    """
    if not data.startswith(_PNG_SIGNATURE):
        raise LogoError("That file is not a PNG or WebP image.")
    pos = len(_PNG_SIGNATURE)
    size = None
    seen_data = False
    while True:
        if pos + 12 > len(data):
            raise LogoError("That PNG is damaged or cut short.")
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        end = pos + 8 + length + 4
        if length > 0x7FFFFFFF or end > len(data):
            raise LogoError("That PNG is damaged or cut short.")
        body = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", data[end - 4:end])
        if zlib.crc32(kind + body) & 0xFFFFFFFF != crc:
            raise LogoError("That PNG is damaged.")
        if size is None:
            if kind != b"IHDR" or length != 13:
                raise LogoError("That PNG is damaged.")
            size = struct.unpack(">II", body[:8])
        elif kind == b"acTL":
            raise LogoError("Animated images are not supported. Use a still PNG.")
        elif kind == b"IDAT":
            seen_data = True
        elif kind == b"IEND":
            if end != len(data):
                raise LogoError("That PNG has extra data after its end.")
            if not seen_data:
                raise LogoError("That PNG has no image data.")
            return size
        pos = end


def _webp_size(data: bytes) -> tuple[int, int]:
    """Check a WebP's container and return its size. Animated files are refused."""
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise LogoError("That file is not a PNG or WebP image.")
    if struct.unpack("<I", data[4:8])[0] + 8 != len(data):
        raise LogoError("That WebP is damaged or cut short.")
    pos = 12
    while pos < len(data):
        if pos + 8 > len(data):
            raise LogoError("That WebP is damaged.")
        kind = data[pos:pos + 4]
        (length,) = struct.unpack("<I", data[pos + 4:pos + 8])
        if kind in (b"ANIM", b"ANMF"):
            raise LogoError("Animated images are not supported. Use a still WebP.")
        pos += 8 + length + (length & 1)
    if pos != len(data):
        raise LogoError("That WebP is damaged.")

    kind = data[12:16]
    if kind == b"VP8X":
        if data[20] & 0x02:
            raise LogoError("Animated images are not supported. Use a still WebP.")
        return (
            1 + int.from_bytes(data[24:27], "little"),
            1 + int.from_bytes(data[27:30], "little"),
        )
    if kind == b"VP8L":
        if data[20] != 0x2F:
            raise LogoError("That WebP is damaged.")
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            raise LogoError("That WebP is damaged.")
        return (
            int.from_bytes(data[26:28], "little") & 0x3FFF,
            int.from_bytes(data[28:30], "little") & 0x3FFF,
        )
    raise LogoError("That WebP is damaged.")


def inspect_logo(data: bytes) -> tuple[str, int, int]:
    """Check an uploaded logo from its bytes, never its name or declared type.

    Returns ``(content_type, width, height)`` or raises LogoError with a message
    the admin can act on.
    """
    if not data:
        raise LogoError("That file is empty.")
    if len(data) > LOGO_MAX_BYTES:
        raise LogoError(f"The logo must be {LOGO_MAX_BYTES // 1024} KB or smaller.")
    if data.startswith(_PNG_SIGNATURE):
        content_type, (width, height) = "image/png", _png_size(data)
    elif data[:4] == b"RIFF":
        content_type, (width, height) = "image/webp", _webp_size(data)
    else:
        raise LogoError("That file is not a PNG or WebP image.")
    if not (
        LOGO_MIN_SIDE <= width <= LOGO_MAX_SIDE
        and LOGO_MIN_SIDE <= height <= LOGO_MAX_SIDE
    ):
        raise LogoError(
            f"The logo must be {LOGO_MIN_SIDE} to {LOGO_MAX_SIDE} px on each side; "
            f"this one is {width} by {height}."
        )
    return content_type, width, height
