"""Shared template context builder.

Extracted from main.py so routers can import it without a circular dependency.
"""
from fastapi import Request

from app import i18n
from app.build import BUILD_VERSION
from app.config import settings
from app.theme import brand_theme


def base_context(request: Request, audience: str | None = None) -> dict:
    """Common template context: theme, CSP nonce, URL base path.

    ``build_version`` is here rather than in each route because every
    template emits static asset URLs and a page that missed it would keep
    serving the previous build's assets while looking fine.

    ``audience`` (i18n.GUEST or i18n.ADMIN) adds the request's language and
    its strings — see app/i18n.py. A page rendered without one is English.
    """
    # Read per request, not at import: the brand colours can be overridden
    # from the dashboard while the app runs.
    brand_css, brand_bg_dark = brand_theme(settings.brand_bg, settings.brand_primary)
    ctx = {
        "request": request,
        "app_name": settings.app_name,
        "brand_bg": settings.brand_bg,
        "brand_bg_dark": brand_bg_dark,
        "brand_primary": settings.brand_primary,
        "brand_css": brand_css,
        "csp_nonce": request.state.csp_nonce,
        "base_path": request.state.base_path,
        "build_version": BUILD_VERSION,
    }
    if audience is not None:
        ctx.update(i18n.template_context(request, audience))
    return ctx
