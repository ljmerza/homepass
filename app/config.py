import ipaddress

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Comma-separated CIDRs that count as "the home network", e.g.
    # "192.168.1.0/24, fd00::/8". Entities marked require_local_network only
    # accept commands from these ranges; empty turns that gate off. A plain
    # string rather than list[str] because pydantic-settings would demand JSON
    # for a list, and the add-on option is typed by a person.
    local_network_cidrs: str = ""

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


settings = Settings()
