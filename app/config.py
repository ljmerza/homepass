import ipaddress
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The public API key is the whole credential for every admin action the API
# exposes, and it is compared as a string, not hashed — so it has to be long
# enough that guessing it is not a plan. 32 characters is the floor for a key a
# person typed; `openssl rand -hex 32` gives 64.
API_TOKEN_MIN_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    admin_username: str = ""
    admin_password: str = ""
    ha_base_url: str = Field(min_length=1, pattern=r"^https?://")
    ha_token: str = Field(min_length=1)
    db_path: str = Field(default="/data/db.sqlite", min_length=1)
    app_name: str = "Home Access"
    contact_message: str = "Please request a new link from the person who shared this one."
    access_log_retention_days: int = Field(default=90, ge=1)
    brand_bg: str = "#F2F0E9"
    brand_primary: str = "#D9523C"
    supervisor_token: str = ""
    guest_url: str = ""
    # IANA zone name ("Europe/Madrid") that weekly access windows are evaluated
    # in. Blank — the default — uses Home Assistant's own configured time zone,
    # which is what the house's clocks already show; this only exists for the
    # rare install whose HA zone is not the one its guests live by. See
    # app/schedule.py.
    timezone: str = ""
    # Comma-separated CIDRs that count as "the home network", e.g.
    # "192.168.1.0/24, fd00::/8". Entities marked require_local_network only
    # accept commands from these ranges; empty turns that gate off. A plain
    # string rather than list[str] because pydantic-settings would demand JSON
    # for a list, and the add-on option is typed by a person.
    local_network_cidrs: str = ""
    # The offline IP-to-country database behind per-token country allowlists.
    # The image bakes one in at build time (see the Dockerfile); a missing file
    # is not an error, it only means no link can be given a country allowlist.
    geoip_db_path: str = "/app/geoip/dbip-country-lite.csv.gz"
    # Off unless asked for. The API is a second way in to everything the admin
    # dashboard can do, so it does not exist — no routes, no docs — until an
    # admin turns it on and sets a key.
    api_enabled: bool = False
    api_token: str = ""

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        # Rejected at startup rather than at the first guest request: a typo
        # here would otherwise surface as every windowed link refusing access.
        value = value.strip()
        if value:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError(f"timezone {value!r} is not a known IANA time zone")
        return value

    @model_validator(mode="after")
    def _require_credentials_in_standalone(self):
        if not self.supervisor_token:
            if len(self.admin_password) < 8:
                raise ValueError("admin_password must be at least 8 characters in standalone mode")
            if not self.admin_username:
                raise ValueError("admin_username is required in standalone mode")
        return self

    @model_validator(mode="after")
    def _validate_local_network_cidrs(self):
        # Refuse to start on a typo rather than drop the bad entry: a silently
        # skipped range is a door button that stops working from the one
        # network it was meant to work from, with nothing in the log to say why.
        for cidr in (c.strip() for c in self.local_network_cidrs.split(",")):
            if not cidr:
                continue
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                raise ValueError(f"local_network_cidrs: invalid CIDR {cidr!r}")
        return self

    @model_validator(mode="after")
    def _require_api_token_when_enabled(self):
        # Refuse to start rather than serve an API with a short or empty key:
        # an enabled API with no key would compare "" against "" and let
        # everyone in.
        if self.api_enabled and len(self.api_token) < API_TOKEN_MIN_LENGTH:
            raise ValueError(
                f"api_token must be at least {API_TOKEN_MIN_LENGTH} characters when api_enabled is true"
            )
        return self


settings = Settings()
