"""Pydantic request/response models."""
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NEVER_EXPIRES_SECONDS = 4102444800  # 2099-12-31T00:00:00Z

# Services guests are permitted to call, keyed by entity domain.
# Script/scene/automation domains are intentionally excluded —
# they execute arbitrary automations and bypass entity scoping.
ALLOWED_SERVICES: dict[str, set[str]] = {
    "light":         {"turn_on", "turn_off", "toggle"},
    "switch":        {"turn_on", "turn_off", "toggle"},
    "input_boolean": {"turn_on", "turn_off", "toggle"},
    "climate":       {"set_temperature", "set_hvac_mode", "turn_on", "turn_off"},
    "lock":          {"lock", "unlock", "open"},
    "media_player":  {"media_play", "media_pause", "media_stop", "volume_set",
                      "media_play_pause", "turn_on", "turn_off"},
    "cover":         {"open_cover", "close_cover", "stop_cover"},
    "fan":           {"turn_on", "turn_off", "toggle", "set_percentage"},
    "group":         {"turn_on", "turn_off", "toggle"},
    "button":        {"press"},
    "time":          {"set_value"},
    "datetime":      {"set_value"},
    # alarm_trigger is excluded for the same reason as script/scene: it lets a
    # guest link set off the siren remotely, which no arm/disarm widget needs.
    "alarm_control_panel": {"alarm_arm_home", "alarm_arm_away",
                            "alarm_arm_night", "alarm_disarm"},
    # Helper domains (Settings -> Devices & Services -> Helpers). Each lists
    # exactly the services its guest widget calls, nothing more.
    "input_number":   {"set_value"},
    "input_text":     {"set_value"},
    "input_select":   {"select_option"},
    "input_datetime": {"set_datetime"},
    "input_button":   {"press"},
    "counter":        {"increment", "decrement", "reset"},
    "timer":          {"start", "pause", "cancel"},
}

# camera is read-only on purpose: it is deliberately absent from ALLOWED_SERVICES,
# so every camera.* service call is rejected by the domain check in the command
# handler. Guests get pixels, never control.
# schedule is read-only for a different reason: its only services rewrite the
# weekly schedule wholesale, which is not a safe one-tap guest action.
READ_ONLY_DOMAINS: set[str] = {"sensor", "binary_sensor", "camera", "schedule"}
SUPPORTED_DOMAINS: set[str] = set(ALLOWED_SERVICES) | READ_ONLY_DOMAINS

# Keys that could bypass the entity allowlist if forwarded to HA
FORBIDDEN_DATA_KEYS = {"entity_id", "device_id", "area_id", "floor_id", "label_id"}


class AdminLoginRequest(BaseModel):
    username: str
    password: str
    remember: bool = False


# Per-token presentation overrides. A display name is free text shown to the
# guest, so it is length-capped here and escaped at render time.
DISPLAY_NAME_MAX = 64

# Per-entity display toggles. Allow-listed so an arbitrary JSON blob from the
# admin API can't accumulate keys nothing renders. Presentation only — nothing in
# the command path reads these.
#
# show_brightness and show_color are opt-IN: with no option set a light gets
# on/off only, and a token has to enable the slider or the colour wheel per
# entity. Neither gates the command path: putting a light on a token is what
# grants light.turn_on, so these only decide what the guest UI draws.
ENTITY_OPTION_KEYS: set[str] = {"show_brightness", "show_color"}

# Colour keys HA's light.turn_on understands. Only rgb_color and
# color_temp_kelvin are accepted from a guest: they are what the colour wheel
# and the warm-cool slider send, and every other format would need its own range
# check before it could be forwarded safely. The mired `color_temp` stays in the
# rejected set on purpose — HA removed it in favour of the kelvin key.
LIGHT_COLOR_KEYS: set[str] = {
    "rgb_color", "rgbw_color", "rgbww_color", "hs_color", "xy_color",
    "color_temp", "color_temp_kelvin", "color_name", "profile", "white",
}
GUEST_COLOR_KEY = "rgb_color"
GUEST_TEMP_KEY = "color_temp_kelvin"
GUEST_COLOR_KEYS: set[str] = {GUEST_COLOR_KEY, GUEST_TEMP_KEY}

# Absolute bounds for a guest colour temperature, in kelvin. Deliberately wider
# than HA's own 2000-6535 fallbacks: every bulb publishes its own
# min_color_temp_kelvin/max_color_temp_kelvin and some sit outside that pair.
# The slider clamps to the bulb's range; this only rejects values no lamp could
# mean, because narrowing it to the real entity would cost a state lookup on
# every command.
KELVIN_MIN = 1000
KELVIN_MAX = 20000


def validate_light_color(data: dict[str, Any]) -> str | None:
    """Check a guest light.turn_on payload's colour. Returns an error, or None.

    The wheel and the temperature slider post whatever the guest's pointer
    produced, so the values are shape- and range-checked here rather than handed
    to HA as-is.
    """
    for key in data:
        if key in LIGHT_COLOR_KEYS and key not in GUEST_COLOR_KEYS:
            return f"Colour format '{key}' is not accepted"
    # HA's colour formats are mutually exclusive and it picks one silently when
    # given both. Neither guest control ever sends a pair, so an ambiguous
    # payload is rejected rather than resolved here.
    if GUEST_COLOR_KEY in data and GUEST_TEMP_KEY in data:
        return f"Send either {GUEST_COLOR_KEY} or {GUEST_TEMP_KEY}, not both"
    if GUEST_TEMP_KEY in data:
        kelvin = data[GUEST_TEMP_KEY]
        # bool again: True would otherwise be an int inside the range check.
        if (isinstance(kelvin, bool) or not isinstance(kelvin, int)
                or not KELVIN_MIN <= kelvin <= KELVIN_MAX):
            return f"{GUEST_TEMP_KEY} must be an integer from {KELVIN_MIN} to {KELVIN_MAX}"
    if GUEST_COLOR_KEY not in data:
        return None
    # Membership, not .get() — an explicit null is a malformed colour, not an
    # absent one, and must not be forwarded as a null key.
    rgb = data[GUEST_COLOR_KEY]
    if not isinstance(rgb, (list, tuple)) or len(rgb) != 3:
        return "rgb_color must be three values"
    for channel in rgb:
        # bool is an int subclass, so True would otherwise pass the range check.
        if isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel <= 255:
            return "rgb_color values must be integers from 0 to 255"
    return None


class TokenCreateRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=200)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,64}$")
    entity_ids: list[str] = Field(..., min_length=1)
    expires_in_seconds: int = Field(..., gt=0)
    # Epoch seconds the link starts working, or None for "right away". Capped
    # below the never-expires sentinel because a start beyond the end of every
    # expiry the app can express is not a schedule, it is a typo. A value in
    # the past is normalised to None by the router rather than rejected — the
    # admin asked for access now, and that is what it means.
    starts_at: int | None = Field(default=None, gt=0, lt=NEVER_EXPIRES_SECONDS)
    ip_allowlist: list[str] | None = None
    entity_meta: dict[str, dict[str, Any]] | None = None
    # Deliberately unconstrained here and validated in the router instead: a
    # Field(pattern=...) rejection becomes a 422 whose body echoes the offending
    # `input` back, which for this one field would put the PIN in a response.
    pin: str | None = None


class TokenPinRequest(BaseModel):
    """Set, replace, or clear a token's PIN. Null or blank clears it.

    Unconstrained for the same reason as TokenCreateRequest.pin.
    """
    pin: str | None = None


class TokenUpdateEntitiesRequest(BaseModel):
    entity_ids: list[str] = Field(..., min_length=1)
    entity_meta: dict[str, dict[str, Any]] | None = None


class EntityMetaRequest(BaseModel):
    """Set one entity's presentation overrides and its proximity gate.

    A blank display_name clears the override and falls back to the HA
    friendly_name. Unknown option keys are dropped, not rejected.

    require_proximity is a sibling of `options`, not a member of it: it is an
    access control the command path enforces, and `options` is the blob nothing
    in that path reads.
    """
    entity_id: str = Field(..., min_length=1, max_length=255)
    display_name: str | None = Field(default=None, max_length=DISPLAY_NAME_MAX)
    options: dict[str, Any] | None = None
    require_proximity: bool = False


class TokenUpdateExpiryRequest(BaseModel):
    expires_in_seconds: int = Field(..., gt=0)


# Template names come from the admin and are rendered back into the picker, so
# they are capped here and escaped at render, same as DISPLAY_NAME_MAX. 64 is
# deliberately short: this is a chip label, not a description.
TEMPLATE_NAME_MAX = 64


class EntityTemplateCreateRequest(BaseModel):
    """Save the picker's current selection under a name.

    Entity IDs only. A template answers "which entities"; the per-entity
    presentation and proximity overrides stay on the token that uses it.
    """
    name: str = Field(..., min_length=1, max_length=TEMPLATE_NAME_MAX)
    entity_ids: list[str] = Field(..., min_length=1)


class EntityTemplateResponse(BaseModel):
    id: str
    name: str
    entity_ids: list[str]
    created_at: int


class GuestLocation(BaseModel):
    """Where the guest's browser says it is, for a proximity-gated command.

    Coordinates only — there is no "I am at home" flag to send, because the
    server does the comparison against zone.home itself and a client-asserted
    answer would be worth nothing. `timestamp` is GeolocationPosition.timestamp
    (milliseconds since the epoch) and is required: a fix with no time on it
    cannot be checked for staleness, and one that cannot be checked is refused.
    """
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    timestamp: int


class CommandRequest(BaseModel):
    entity_id: str
    service: str  # e.g. "light.turn_on"
    data: dict[str, Any] = Field(default_factory=dict)
    # Only sent for entities the token marks as proximity-gated. Absent on every
    # other command, which is why it defaults to None rather than being required.
    location: GuestLocation | None = None


class TokenResponse(BaseModel):
    id: str
    slug: str
    label: str
    created_at: int
    starts_at: int | None
    expires_at: int
    revoked: bool
    last_accessed: int | None
    ip_allowlist: list[str] | None
    entity_count: int
    entity_ids: list[str] | None = None


# ---------------------------------------------------------------------------
# Runtime settings (see app/settings_store.py)
# ---------------------------------------------------------------------------
# Caps for the values an admin can override from the dashboard. They are shown
# to guests (app name, contact message) or interpolated into links and a
# <style> block (guest URL, colours), so each is bounded and shape-checked here
# rather than trusted because it came from an admin.
APP_NAME_MAX = 64
CONTACT_MESSAGE_MAX = 500
GUEST_URL_MAX = 300
RETENTION_DAYS_MAX = 3650
HEX_COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"

# A guest URL is a base that "/g/<slug>" is appended to, then copied into a
# message to a guest and dropped into a JavaScript string on the dashboard. So
# it has to be exactly scheme://host[:port][/path] — no query or fragment the
# slug would land inside, no user:pass@ that would ride along to the guest, and
# none of the characters that could end a string or an attribute.
_GUEST_URL_FORBIDDEN_CHARS = set(" \t\r\n\"'<>`\\{}|^")


def normalise_guest_url(value: str) -> str:
    """A cleaned guest base URL, or "" for none. Raises ValueError on a bad one."""
    value = value.strip().rstrip("/")
    if not value:
        return ""
    if any(c in _GUEST_URL_FORBIDDEN_CHARS for c in value):
        raise ValueError("Guest URL contains characters a link cannot carry")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Guest URL must start with http:// or https:// and name a host")
    if parts.query or parts.fragment or "?" in value or "#" in value:
        raise ValueError("Guest URL cannot have a query string or fragment")
    if "@" in parts.netloc:
        raise ValueError("Guest URL cannot contain a username or password")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError("Guest URL has an invalid port") from exc
    return value


class SettingsUpdateRequest(BaseModel):
    """Overrides for the deployment settings an admin may change live.

    The field list IS the allowlist: app/settings_store.py derives the editable
    keys from it, and extra="forbid" turns an attempt to set anything else —
    admin_password, ha_token, db_path — into a 422 instead of a silent no-op.

    Every field is optional so a save can carry only what changed. An explicit
    null is refused: reverting to the add-on option is its own DELETE, and a
    null that quietly meant "revert" would make a malformed save look like one.
    """
    model_config = ConfigDict(extra="forbid")

    app_name: str | None = Field(default=None, max_length=APP_NAME_MAX)
    contact_message: str | None = Field(default=None, max_length=CONTACT_MESSAGE_MAX)
    brand_bg: str | None = Field(default=None, pattern=HEX_COLOR_PATTERN)
    brand_primary: str | None = Field(default=None, pattern=HEX_COLOR_PATTERN)
    guest_url: str | None = Field(default=None, max_length=GUEST_URL_MAX)
    # strict: JSON true would otherwise be accepted as 1 day.
    access_log_retention_days: int | None = Field(
        default=None, ge=1, le=RETENTION_DAYS_MAX, strict=True,
    )

    @field_validator("app_name", "contact_message")
    @classmethod
    def _required_text(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("brand_bg", "brand_primary")
    @classmethod
    def _upper_hex(cls, value: str | None) -> str | None:
        # One spelling per colour, so #d9523c and the #D9523C default compare
        # equal and the default-palette shortcut in app/theme.py still applies.
        return value.upper() if value is not None else value

    @field_validator("guest_url")
    @classmethod
    def _guest_url(cls, value: str | None) -> str | None:
        return normalise_guest_url(value) if value is not None else value

    @model_validator(mode="after")
    def _no_explicit_null(self):
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null — revert it instead")
        return self
