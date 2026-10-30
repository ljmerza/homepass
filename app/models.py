"""Pydantic request/response models."""
import re
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app import branding, theme
from app.region import valid_region

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

# "Suggest" in the create-token picker: a starting selection for the two things
# guests are most often given, a way in and the lights. Suggestions only ever
# add to the picker's selection, which the admin still reviews and saves — they
# grant nothing on their own, and they never reach outside SUPPORTED_DOMAINS.
#
# Deliberately narrow. Keywords are matched against whole words of the entity
# ID and friendly name, never substrings: "door" must not pull in
# cover.outdoor_blinds, "gate" must not match "navigate", and "light" must not
# match sensor-ish names like "daylight". Spanish terms are included because
# the fork this was ported from was built for Spanish-speaking households.
SUGGESTION_CATEGORIES: tuple[str, ...] = ("access", "lights")
ACCESS_KEYWORDS: frozenset[str] = frozenset({
    "door", "doors", "gate", "gates", "garage", "entrance", "deadbolt",
    "puerta", "portal", "garaje", "verja", "cancela", "cerradura",
})
LIGHT_KEYWORDS: frozenset[str] = frozenset({
    "light", "lights", "lamp", "lamps", "luz", "luces", "lampara",
})
# A lock is access by definition, so every lock is suggested. A cover is only
# access when HA says it is a door, gate or garage door (blinds and shades are
# covers too) or its name says so; a button only when its name does.
ACCESS_COVER_DEVICE_CLASSES: frozenset[str] = frozenset({"door", "garage", "gate"})
ACCESS_KEYWORD_DOMAINS: frozenset[str] = frozenset({"cover", "button", "input_button"})
# Every light is a light. A switch is only when its name says so — most smart
# plugs that drive a lamp are called something like "Bedside Lamp".
LIGHT_KEYWORD_DOMAINS: frozenset[str] = frozenset({"switch"})


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
#
# show_history is opt-in the same way, for sensors, and is the one option a
# guest route does read: /g/{slug}/history serves nothing for an entity without
# it, so a link only reveals the recorder's past for the sensors an admin picked.
ENTITY_OPTION_KEYS: set[str] = {"show_brightness", "show_color", "show_history"}

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


# Weekly access windows. See app/schedule.py for how they are evaluated.
# Fourteen is two per weekday — room for "mornings and evenings" without the
# list becoming the unbounded blob the admin API should never store.
MAX_ACCESS_WINDOWS = 14
_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
# An end may also be 24:00, so a window can run to the end of its day.
_HHMM_END = r"^(([01]\d|2[0-3]):[0-5]\d|24:00)$"

# Upper bound on max_uses. A use limit is for "open the gate once" or "a
# handful of times", and a cap keeps a stray extra digit from quietly turning
# a limited link into an unlimited one.
MAX_USES_CAP = 1000


class AccessWindow(BaseModel):
    """One weekly window, e.g. Tue/Thu 09:00-13:00, in the house's time zone.

    An end before the start crosses midnight, and the weekdays are the days the
    window opens on: Fri 22:00-02:00 is Friday night into Saturday. A start
    equal to the end is rejected rather than guessed at — it could mean an
    empty window or a 24-hour one, and those are opposite answers; a full day
    is written 00:00-24:00.

    Weekdays are strict ints (0 is Monday, as in datetime.weekday()) so a
    bool or a numeric string is refused rather than coerced into a day.
    """
    weekdays: list[Annotated[int, Field(strict=True, ge=0, le=6)]] = Field(
        ..., min_length=1, max_length=7
    )
    start: str = Field(..., pattern=_HHMM)
    end: str = Field(..., pattern=_HHMM_END)

    @model_validator(mode="after")
    def _check(self):
        if self.start == self.end:
            raise ValueError("start and end must differ; write a full day as 00:00-24:00")
        self.weekdays = sorted(set(self.weekdays))
        return self


# Text an admin writes for guests: the global welcome text and a link's own
# info text, with a few formatting tags allowed (app/richtext.py). Capped like
# every other admin text that reaches a guest page; the cap counts the text as
# typed, tags included.
INFO_TEXT_MAX = 1000


def clean_info_text(value: Any) -> Any:
    """Trim the text and give it one kind of line ending.

    Run as a mode="before" validator, so a field's max_length counts the text
    as it will be stored: a CRLF pasted from Windows must not use up two
    characters of the limit per line. Whitespace-only becomes "", which these
    fields read as "none". Tags are kept exactly as typed, never stripped
    here: the text is sanitised on every render (app/richtext.py), so what is
    stored is never trusted. Anything that is not a string is passed through for
    the field's own type check to refuse.
    """
    if isinstance(value, str):
        value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return value


# Per-link section headings on the guest page, keyed by group key (see
# groupKey() in static/domains.js and migration 015). Thirty is more than the
# number of distinct sections the guest page can draw, so a client that sends
# more is sending something other than headings.
GROUP_HEADINGS_MAX = 30
_GROUP_KEY = re.compile(r"^[a-z_]+$")


def clean_group_headings(value: Any) -> Any:
    """Check and normalise a section-headings object. Returns the stored shape.

    {"<group_key>": {"name": str, "locked": bool}}. The name is trimmed and
    capped at DISPLAY_NAME_MAX, "" (or null) meaning the default heading;
    locked (the guest cannot collapse the section) is coerced to bool. Any
    other field in an entry is ignored. An entry that changes nothing is dropped, so
    the result only ever holds headings the admin actually changed, and {}
    means every heading as default.

    The keys are checked for shape only, never against the token's entities:
    a PATCH may change the entities in the same request, and a heading for a
    section the link no longer has is simply never drawn.

    Run as a field validator, so a refusal is a 422 whose `input` is this
    field alone and never the rest of the body (which can carry a PIN).
    None passes through: on an update it means "keep what is stored".
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("group_headings must be an object")
    if len(value) > GROUP_HEADINGS_MAX:
        raise ValueError(f"group_headings can have at most {GROUP_HEADINGS_MAX} entries")
    cleaned: dict[str, dict[str, Any]] = {}
    for key, entry in value.items():
        if not isinstance(key, str) or not _GROUP_KEY.fullmatch(key):
            raise ValueError("group_headings keys must be lower-case letters and underscores")
        if not isinstance(entry, dict):
            raise ValueError(f"group_headings.{key} must be an object")
        name = entry.get("name")
        if name is None:
            name = ""
        if not isinstance(name, str):
            raise ValueError(f"group_headings.{key}.name must be a string")
        name = name.strip()
        if len(name) > DISPLAY_NAME_MAX:
            raise ValueError(
                f"group_headings.{key}.name can be at most {DISPLAY_NAME_MAX} characters"
            )
        locked = bool(entry.get("locked"))
        if name or locked:
            cleaned[key] = {"name": name, "locked": locked}
    return cleaned


class BrandingInput(BaseModel):
    """A link's branding as sent when creating one, or copied from another.

    Every part is optional and blank shows nothing. ``colors`` is None while
    the link uses the global colours; otherwise all nine colour settings, with
    "" for Auto on the seven optional ones. The logo is a file, uploaded
    separately (POST /admin/tokens/{id}/logo).
    """
    model_config = ConfigDict(extra="forbid")

    eyebrow: str = Field(default="", max_length=branding.EYEBROW_MAX)
    title: str = Field(default="", max_length=branding.TITLE_MAX)
    logo_height: int = Field(
        default=branding.LOGO_HEIGHT_DEFAULT,
        ge=branding.LOGO_HEIGHT_MIN, le=branding.LOGO_HEIGHT_MAX, strict=True,
    )
    title_size: int = Field(
        default=branding.TITLE_SIZE_DEFAULT,
        ge=branding.TITLE_SIZE_MIN, le=branding.TITLE_SIZE_MAX, strict=True,
    )
    eyebrow_size: int = Field(
        default=branding.EYEBROW_SIZE_DEFAULT,
        ge=branding.EYEBROW_SIZE_MIN, le=branding.EYEBROW_SIZE_MAX, strict=True,
    )
    colors: dict[str, str] | None = None

    _clean_text = field_validator("eyebrow", "title", mode="before")(branding.clean_line)
    _clean_colors = field_validator("colors", mode="before")(branding.clean_colors)


class TokenBrandingRequest(BaseModel):
    """Change a link's branding. A field left out keeps its stored value.

    ``eyebrow`` and ``title`` may be "" to clear them. ``colors`` is the one
    field where null means something: it puts the link back on the global
    colours.
    """
    model_config = ConfigDict(extra="forbid")

    eyebrow: str | None = Field(default=None, max_length=branding.EYEBROW_MAX)
    title: str | None = Field(default=None, max_length=branding.TITLE_MAX)
    logo_height: int | None = Field(
        default=None,
        ge=branding.LOGO_HEIGHT_MIN, le=branding.LOGO_HEIGHT_MAX, strict=True,
    )
    title_size: int | None = Field(
        default=None,
        ge=branding.TITLE_SIZE_MIN, le=branding.TITLE_SIZE_MAX, strict=True,
    )
    eyebrow_size: int | None = Field(
        default=None,
        ge=branding.EYEBROW_SIZE_MIN, le=branding.EYEBROW_SIZE_MAX, strict=True,
    )
    colors: dict[str, str] | None = None

    _clean_text = field_validator("eyebrow", "title", mode="before")(branding.clean_line)
    _clean_colors = field_validator("colors", mode="before")(branding.clean_colors)


class TokenCreateRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=200)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,64}$")
    entity_ids: list[str] = Field(..., min_length=1)
    # Exactly one of these two. expires_in_seconds is a duration measured from
    # the moment the link starts working; expires_at is an absolute epoch
    # second — a check-out time — and is taken as written, whatever the start.
    # NEVER_EXPIRES_SECONDS means "no expiry" in either field.
    #
    # "Exactly one" is enforced in the router rather than by a model_validator
    # here: a model-level rejection is a 422 whose `input` is the whole request
    # body, and the whole body includes the PIN. See `pin` below.
    expires_in_seconds: int | None = Field(default=None, gt=0)
    expires_at: int | None = Field(default=None, gt=0, le=NEVER_EXPIRES_SECONDS)
    # Epoch seconds the link starts working, or None for "right away". Capped
    # below the never-expires sentinel because a start beyond the end of every
    # expiry the app can express is not a schedule, it is a typo. A value in
    # the past is normalised to None by the router rather than rejected — the
    # admin asked for access now, and that is what it means.
    starts_at: int | None = Field(default=None, gt=0, lt=NEVER_EXPIRES_SECONDS)
    ip_allowlist: list[str] | None = None
    entity_meta: dict[str, dict[str, Any]] | None = None
    # Weekly windows inside starts_at/expires_at. None or empty is "any time".
    access_windows: list[AccessWindow] | None = Field(
        default=None, max_length=MAX_ACCESS_WINDOWS
    )
    # None is unlimited. See guest_command for what a use is.
    max_uses: int | None = Field(default=None, ge=1, le=MAX_USES_CAP)
    # Keep a "N uses left" counter on the guest page. Only means anything with
    # max_uses set. Off by default, which is how every link behaved before.
    show_remaining_uses: bool = False
    # Deliberately unconstrained here and validated in the router instead: a
    # Field(pattern=...) rejection becomes a 422 whose body echoes the offending
    # `input` back, which for this one field would put the PIN in a response.
    pin: str | None = None
    # On by default, which is how every token behaved before the setting
    # existed. Stored even when no PIN is set, so a PIN added later inherits
    # the choice made here.
    remember_pin: bool = True
    # Lock the link to the first browser that claims it. Off by default.
    device_binding: bool = False
    # ISO 3166-1 alpha-2 codes. Validated and upper-cased in the router, which
    # checks them against the GeoIP database actually installed — a code that
    # database has no addresses for would lock every guest out.
    country_allowlist: list[str] | None = None
    # Plain text shown to the guest on this link's page; "" is none. With
    # info_text_in_footer it sits in a box below the controls instead of at
    # the top with the welcome text.
    info_text: str = Field(default="", max_length=INFO_TEXT_MAX)
    info_text_in_footer: bool = False
    # Rename the guest page's section headings, or stop the guest collapsing
    # a section. None and {} both mean every heading as default. See
    # clean_group_headings.
    group_headings: dict[str, Any] | None = None
    # Eyebrow, title, logo size and colours for the link's pages. None is
    # unbranded. The logo image is uploaded after the link exists, or copied
    # from another link with copy_logo_from (what Duplicate does).
    branding: BrandingInput | None = None
    copy_logo_from: str | None = Field(default=None, max_length=64)

    _clean_info_text = field_validator("info_text", mode="before")(clean_info_text)
    _clean_group_headings = field_validator("group_headings", mode="before")(
        clean_group_headings
    )


class TokenInfoRequest(BaseModel):
    """Change a link's info text and where it is shown, from the dashboard.

    None keeps the stored value, the same convention as
    TokenScheduleRequest.show_remaining_uses, so a client that only knows
    about one of the two fields cannot reset the other by leaving it out. ""
    is a value: it clears the text.
    """
    info_text: str | None = Field(default=None, max_length=INFO_TEXT_MAX)
    info_text_in_footer: bool | None = None

    _clean_info_text = field_validator("info_text", mode="before")(clean_info_text)


class TokenRememberPinRequest(BaseModel):
    """Whether one correct PIN entry survives the guest closing the browser.

    Its own request rather than a field on TokenPinRequest: that one treats a
    null PIN as "clear it", so an admin toggling this alone would otherwise
    have to resend a PIN the server cannot show them.
    """
    remember_pin: bool


# Access-link labels are admin free text rendered back into the dashboard, so
# they are capped here and escaped at render, same as TEMPLATE_NAME_MAX.
ACCESS_CODE_LABEL_MAX = 64


class AccessCodeCreateRequest(BaseModel):
    """Mint a PIN-free access link. The label is optional and only for the admin
    — "Front door QR", "Sam's phone" — it never reaches the guest."""
    label: str | None = Field(default=None, max_length=ACCESS_CODE_LABEL_MAX)


class TokenDeviceBindingRequest(BaseModel):
    """Turn single-device binding on or off. Either direction clears the claim."""
    enabled: bool


class TokenPinRequest(BaseModel):
    """Set, replace, or clear a token's PIN. Null or blank clears it.

    Unconstrained for the same reason as TokenCreateRequest.pin.
    """
    pin: str | None = None


class TokenUpdateEntitiesRequest(BaseModel):
    entity_ids: list[str] = Field(..., min_length=1)
    entity_meta: dict[str, dict[str, Any]] | None = None
    # Saved with the entities because the dashboard edits both in one form.
    # None keeps the stored headings; {} resets them all.
    group_headings: dict[str, Any] | None = None

    _clean_group_headings = field_validator("group_headings", mode="before")(
        clean_group_headings
    )


class EntityMetaRequest(BaseModel):
    """Set one entity's presentation overrides and its proximity gate.

    A blank display_name clears the override and falls back to the HA
    friendly_name. Unknown option keys are dropped, not rejected.

    require_proximity is a sibling of `options`, not a member of it: it is an
    access control the command path enforces, and `options` is the blob nothing
    in that path reads. require_local_network sits beside it for the same
    reason.
    """
    entity_id: str = Field(..., min_length=1, max_length=255)
    display_name: str | None = Field(default=None, max_length=DISPLAY_NAME_MAX)
    options: dict[str, Any] | None = None
    require_proximity: bool = False
    require_local_network: bool = False


class TokenUpdateExpiryRequest(BaseModel):
    """Exactly one of the two, same as on TokenCreateRequest."""
    expires_in_seconds: int | None = Field(default=None, gt=0)
    expires_at: int | None = Field(default=None, gt=0, le=NEVER_EXPIRES_SECONDS)


class TokenScheduleRequest(BaseModel):
    """Replace a token's timing after creation: start, end, windows, use limit.

    A full replacement, not a patch — every field is the new value, and an
    omitted one takes its default. The dashboard always has the whole schedule
    in hand when it saves, and a partial update would leave "clear the windows"
    and "leave the windows alone" needing two different spellings.

    expires_at is absolute here and nowhere relative: editing a schedule is
    choosing dates, and a duration would need an anchor the admin cannot see.
    reset_uses zeroes the use counter, for handing a spent single-use link out
    again; without it the count carries over, so raising max_uses from 1 to 2
    on a used link grants exactly one more use.
    """
    starts_at: int | None = Field(default=None, gt=0, lt=NEVER_EXPIRES_SECONDS)
    expires_at: int = Field(..., gt=0, le=NEVER_EXPIRES_SECONDS)
    access_windows: list[AccessWindow] | None = Field(
        default=None, max_length=MAX_ACCESS_WINDOWS
    )
    max_uses: int | None = Field(default=None, ge=1, le=MAX_USES_CAP)
    reset_uses: bool = False
    # None keeps the stored choice; True/False replaces it.
    show_remaining_uses: bool | None = None


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
# Public API (/api/v1)
# ---------------------------------------------------------------------------
# The API reuses the dashboard's create path, so its request bodies are the
# dashboard's with one addition: an automation usually knows when a stay ends
# (a check-out time off a calendar) rather than how long it lasts, so every
# body that sets an expiry takes either an absolute `expires_at` or the
# dashboard's `expires_in_seconds`.
#
# "Exactly one of the two" is enforced in the router, not with a model
# validator. A model-level ValueError becomes a 422 whose `input` is the whole
# body — and the body can carry a PIN, which must not come back in a response.

class ApiTokenCreateRequest(TokenCreateRequest):
    expires_in_seconds: int | None = Field(default=None, gt=0)
    # Epoch seconds. NEVER_EXPIRES_SECONDS is accepted and means "never", same
    # as it does for expires_in_seconds. Checked against the clock and the start
    # time in the router.
    expires_at: int | None = Field(default=None, gt=0, le=NEVER_EXPIRES_SECONDS)


class ApiTokenUpdateRequest(BaseModel):
    """Change any subset of a token. Omitted fields are left alone.

    `pin` distinguishes "absent" from "null": leaving it out keeps the PIN,
    sending null or "" clears it. Unconstrained for the same reason as
    TokenCreateRequest.pin.

    Changing the expiry here does not un-revoke a revoked token — that is what
    /renew is for, the same split the dashboard has between editing a token and
    renewing one.
    """
    label: str | None = Field(default=None, min_length=1, max_length=200)
    entity_ids: list[str] | None = Field(default=None, min_length=1)
    entity_meta: dict[str, dict[str, Any]] | None = None
    expires_in_seconds: int | None = Field(default=None, gt=0)
    expires_at: int | None = Field(default=None, gt=0, le=NEVER_EXPIRES_SECONDS)
    pin: str | None = None
    # Same effect as the dashboard's remember-PIN checkbox.
    remember_pin: bool | None = None
    # Applied only when it differs from the stored value. The dashboard's
    # toggle clears the claim in either direction, and a PATCH that repeated
    # the current value — the common "send the whole object back" client —
    # must not quietly lock the guest's own phone out of their link.
    device_binding: bool | None = None
    # Same effect as the "Show remaining uses to the guest" checkbox.
    show_remaining_uses: bool | None = None
    # Same as the dashboard's info dialog: None keeps, "" clears.
    info_text: str | None = Field(default=None, max_length=INFO_TEXT_MAX)
    info_text_in_footer: bool | None = None
    # Same as the dashboard's Section headings: None keeps, {} resets.
    group_headings: dict[str, Any] | None = None
    # Same as the dashboard's Branding dialog; see TokenBrandingRequest.
    branding: TokenBrandingRequest | None = None

    _clean_info_text = field_validator("info_text", mode="before")(clean_info_text)
    _clean_group_headings = field_validator("group_headings", mode="before")(
        clean_group_headings
    )


class ApiTokenRenewRequest(BaseModel):
    """New expiry for a token, clearing a revocation. Same effect as the
    dashboard's Renew."""
    expires_in_seconds: int | None = Field(default=None, gt=0)
    expires_at: int | None = Field(default=None, gt=0, le=NEVER_EXPIRES_SECONDS)


class ApiTokenDuplicateRequest(BaseModel):
    """Overrides for a copy of an existing token. Every field is optional.

    What is copied and what is not follows the dashboard's Duplicate: the
    entities, the IP and country allowlists, the weekly windows, the use limit
    (with a fresh count), remember-PIN, the device-lock setting, the info
    text and where it is shown, and the section headings carry over;
    the slug, the PIN, the scheduled start, a device claim and links without
    PIN do not, because they belong to one guest's stay. With no expiry given,
    a never-expiring source gives a never-expiring copy and anything else gets
    24 hours.
    """
    label: str | None = Field(default=None, min_length=1, max_length=200)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,64}$")
    starts_at: int | None = Field(default=None, gt=0, lt=NEVER_EXPIRES_SECONDS)
    expires_in_seconds: int | None = Field(default=None, gt=0)
    expires_at: int | None = Field(default=None, gt=0, le=NEVER_EXPIRES_SECONDS)
    pin: str | None = None


class ApiEntityMeta(BaseModel):
    display_name: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    require_proximity: bool = False
    require_local_network: bool = False


class ApiGroupHeading(BaseModel):
    """One changed section heading. An empty name is the default heading;
    locked means the guest cannot collapse the section."""
    name: str = ""
    locked: bool = False


class ApiAccessCode(BaseModel):
    """A PIN-free access link as listed. The code itself is never readable."""
    id: str
    label: str | None
    created_at: int
    last_used_at: int | None


class ApiAccessCodeCreated(ApiAccessCode):
    """A freshly minted or rotated link. `code` appears here and nowhere else;
    the guest opens /g/<slug>?c=<code>."""
    code: str


class ApiBrandingLogo(BaseModel):
    """The uploaded logo. `version` changes when the image does; the guest
    page serves it at /g/<slug>/logo?v=<version>."""
    version: str
    width: int
    height: int


class ApiBranding(BaseModel):
    """A link's branding. `colors` is null while it uses the global colours."""
    eyebrow: str = ""
    title: str = ""
    logo_height: int = branding.LOGO_HEIGHT_DEFAULT
    title_size: int = branding.TITLE_SIZE_DEFAULT
    eyebrow_size: int = branding.EYEBROW_SIZE_DEFAULT
    colors: dict[str, str] | None = None
    logo: ApiBrandingLogo | None = None


class ApiTokenResponse(BaseModel):
    """A token as the API returns it. Same fields as the dashboard sees.

    `entity_ids` and `entity_meta` are null in list responses, which carry only
    `entity_count`; fetch one token to get them.
    """
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
    entity_meta: dict[str, ApiEntityMeta] | None = None
    has_pin: bool
    remember_pin: bool
    access_windows: list[AccessWindow] | None
    max_uses: int | None
    use_count: int
    uses_remaining: int | None
    show_remaining_uses: bool
    info_text: str
    info_text_in_footer: bool
    # Only the headings the admin changed, keyed by group key; {} is all
    # defaults.
    group_headings: dict[str, ApiGroupHeading]
    branding: ApiBranding = Field(default_factory=ApiBranding)
    country_allowlist: list[str] | None
    device_binding: bool
    device_bound_at: int | None


# ---------------------------------------------------------------------------
# Runtime settings (see app/settings_store.py)
# ---------------------------------------------------------------------------
# Caps for the values an admin can override from the dashboard. They are shown
# to guests (app name, contact message) or interpolated into links and a
# <style> block (guest URL, colours), so each is bounded and shape-checked here
# rather than trusted because it came from an admin.
APP_NAME_MAX = 64
CONTACT_MESSAGE_MAX = 500
WELCOME_TEXT_MAX = INFO_TEXT_MAX
GUEST_URL_MAX = 300
RETENTION_DAYS_MAX = 3650
HEX_COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"
# The optional colours also accept blank, which means "work it out".
OPTIONAL_HEX_COLOR_PATTERN = r"^(#[0-9A-Fa-f]{6})?$"

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


# The colour settings, required ones first. Shared by the settings save and the
# dashboard's live preview so the two cannot drift apart.
THEME_FIELDS = theme.COLOR_KEYS


class ThemePreviewRequest(BaseModel):
    """Colours to preview in the dashboard before they are saved.

    A field left out keeps the colour now in effect; a blank optional one means
    "work it out", the same as saving it blank. Nothing here is stored.
    """
    model_config = ConfigDict(extra="forbid")

    brand_primary: str | None = Field(default=None, pattern=HEX_COLOR_PATTERN)
    brand_bg: str | None = Field(default=None, pattern=HEX_COLOR_PATTERN)
    brand_secondary: str | None = Field(default=None, pattern=OPTIONAL_HEX_COLOR_PATTERN)
    brand_accent: str | None = Field(default=None, pattern=OPTIONAL_HEX_COLOR_PATTERN)
    brand_surface: str | None = Field(default=None, pattern=OPTIONAL_HEX_COLOR_PATTERN)
    brand_text: str | None = Field(default=None, pattern=OPTIONAL_HEX_COLOR_PATTERN)
    brand_dark_bg: str | None = Field(default=None, pattern=OPTIONAL_HEX_COLOR_PATTERN)
    brand_dark_surface: str | None = Field(default=None, pattern=OPTIONAL_HEX_COLOR_PATTERN)
    brand_dark_text: str | None = Field(default=None, pattern=OPTIONAL_HEX_COLOR_PATTERN)


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
    welcome_text: str | None = Field(default=None, max_length=WELCOME_TEXT_MAX)
    phone_region: str | None = None
    brand_bg: str | None = Field(default=None, pattern=HEX_COLOR_PATTERN)
    brand_primary: str | None = Field(default=None, pattern=HEX_COLOR_PATTERN)
    brand_secondary: str | None = Field(default=None, pattern=OPTIONAL_HEX_COLOR_PATTERN)
    brand_accent: str | None = Field(default=None, pattern=OPTIONAL_HEX_COLOR_PATTERN)
    brand_surface: str | None = Field(default=None, pattern=OPTIONAL_HEX_COLOR_PATTERN)
    brand_text: str | None = Field(default=None, pattern=OPTIONAL_HEX_COLOR_PATTERN)
    brand_dark_bg: str | None = Field(default=None, pattern=OPTIONAL_HEX_COLOR_PATTERN)
    brand_dark_surface: str | None = Field(default=None, pattern=OPTIONAL_HEX_COLOR_PATTERN)
    brand_dark_text: str | None = Field(default=None, pattern=OPTIONAL_HEX_COLOR_PATTERN)
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

    # Empty is allowed and means "show nothing", unlike the contact message.
    # See clean_info_text for why this runs before the length check.
    _clean_welcome_text = field_validator("welcome_text", mode="before")(clean_info_text)

    # Blank is "work it out" (see app/region.py). Anything else must be a region
    # libphonenumber can read numbers in, checked by the same rule the resolver
    # uses, so a saved value can never be one it would silently skip.
    @field_validator("phone_region")
    @classmethod
    def _phone_region(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip().upper()
        if value and valid_region(value) is None:
            raise ValueError("must be a two-letter country code, such as CA")
        return value

    @field_validator(
        "brand_bg", "brand_primary", "brand_secondary", "brand_accent",
        "brand_surface", "brand_text", "brand_dark_bg", "brand_dark_surface",
        "brand_dark_text",
    )
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
