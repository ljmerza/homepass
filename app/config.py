from pydantic import Field, model_validator
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
    # Off unless asked for. The API is a second way in to everything the admin
    # dashboard can do, so it does not exist — no routes, no docs — until an
    # admin turns it on and sets a key.
    api_enabled: bool = False
    api_token: str = ""

    @model_validator(mode="after")
    def _require_credentials_in_standalone(self):
        if not self.supervisor_token:
            if len(self.admin_password) < 8:
                raise ValueError("admin_password must be at least 8 characters in standalone mode")
            if not self.admin_username:
                raise ValueError("admin_username is required in standalone mode")
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
