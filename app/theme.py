"""Derive a full color palette from BRAND_BG and BRAND_PRIMARY env vars.

Converts hex colors into the RGB-triplet CSS custom properties that Tailwind
reads (e.g. ``--color-bg-light: 242 240 233``). Dark-mode colors are
auto-derived by darkening the background and adjusting supporting colors.
"""
from functools import lru_cache


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def _clamp(v: int) -> int:
    return max(0, min(255, v))


def _mix(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    """Linearly interpolate between c1 and c2.  t=0 → c1, t=1 → c2."""
    return (
        _clamp(round(c1[0] + (c2[0] - c1[0]) * t)),
        _clamp(round(c1[1] + (c2[1] - c1[1]) * t)),
        _clamp(round(c1[2] + (c2[2] - c1[2]) * t)),
    )


def _darken(c: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """Darken a color by mixing toward black."""
    return _mix(c, (0, 0, 0), factor)


def _lighten(c: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """Lighten a color by mixing toward white."""
    return _mix(c, (255, 255, 255), factor)


def _trip(c: tuple[int, int, int]) -> str:
    """Format as Tailwind RGB triplet string."""
    return f"{c[0]} {c[1]} {c[2]}"


def build_palette(brand_bg: str, brand_primary: str) -> dict[str, str]:
    """Return a dict of CSS custom property name → value.

    Keys match the variables in ``static/input.css``.
    """
    bg = _hex_to_rgb(brand_bg)
    primary = _hex_to_rgb(brand_primary)

    # Light mode
    bg_light = bg
    surface_light = _lighten(bg, 0.55)
    border_light = _darken(bg, 0.08)
    soot = _darken(bg, 0.82)
    muted = _darken(bg, 0.52)

    # Dark mode — derived from bg
    bg_dark = _darken(bg, 0.87)
    surface_dark = _darken(bg, 0.80)
    border_dark = _darken(bg, 0.70)
    muted_dark = _lighten(bg_dark, 0.45)

    # Accent — warm-shifted from primary
    accent = _lighten(primary, 0.40)

    # Primary hover — slightly darker
    primary_hover = _darken(primary, 0.10)

    # Ink — cool complement (shift toward blue)
    ink = (
        _clamp(255 - primary[0]),
        _clamp(round((primary[1] + 180) / 2)),
        _clamp(round((primary[2] + 220) / 2)),
    )

    return {
        # RGB triplets (Tailwind alpha-modifier compatible)
        "--color-primary": _trip(primary),
        "--color-primary-hover": _trip(primary_hover),
        "--color-surface-light": _trip(surface_light),
        "--color-surface-dark": _trip(surface_dark),
        "--color-bg-light": _trip(bg_light),
        "--color-bg-dark": _trip(bg_dark),
        "--color-border-light": _trip(border_light),
        "--color-border-dark": _trip(border_dark),
        "--color-accent": _trip(accent),
        "--color-ink": _trip(ink),
        "--color-muted": _trip(muted),
        "--color-soot": _trip(soot),
        # Hex equivalents (used in <style> blocks and scrollbar)
        "--hex-primary": _rgb_to_hex(*primary),
        "--hex-bg-light": brand_bg,
        "--hex-bg-dark": _rgb_to_hex(*bg_dark),
        "--hex-accent": _rgb_to_hex(*accent),
    }


def dark_bg(brand_bg: str) -> str:
    """Return the auto-derived dark-mode background hex color."""
    return _rgb_to_hex(*_darken(_hex_to_rgb(brand_bg), 0.87))


def palette_css(brand_bg: str, brand_primary: str) -> str:
    """Return a ``<style>`` block that overrides the default CSS variables.

    Returns empty string if both colors match the defaults (no override needed).
    """
    if brand_bg == "#F2F0E9" and brand_primary == "#D9523C":
        return ""

    palette = build_palette(brand_bg, brand_primary)
    lines = [":root {"]
    for prop, value in palette.items():
        lines.append(f"  {prop}: {value};")
    lines.append("}")

    # Dark-mode muted override for contrast
    bg_dark = _hex_to_rgb(palette["--hex-bg-dark"])
    muted_dark = _lighten(bg_dark, 0.45)
    lines.append(f".dark {{ --color-muted: {_trip(muted_dark)}; }}")

    return "\n".join(lines)


@lru_cache(maxsize=16)
def brand_theme(brand_bg: str, brand_primary: str) -> tuple[str, str]:
    """(palette <style> body, dark background hex) for the given brand colours.

    Keyed on the colours rather than computed once at import: they can be
    overridden from the dashboard (app/settings_store.py) while the process
    runs, and a palette baked in at startup would keep serving the old one. The
    cache makes the per-request lookup free for the colours actually in use.
    """
    return palette_css(brand_bg, brand_primary), dark_bg(brand_bg)
