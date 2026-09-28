from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
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
    # IANA zone name ("Europe/Madrid") that weekly access windows are evaluated
    # in. Blank — the default — uses Home Assistant's own configured time zone,
    # which is what the house's clocks already show; this only exists for the
    # rare install whose HA zone is not the one its guests live by. See
    # app/schedule.py.
    timezone: str = ""

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


settings = Settings()
