from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Sentinel dev secret. Safe for local dev and tests, but the production startup
# guard (see `_validate_production_secret`) refuses to boot if this is still in
# use when APP_ENV=production. Must match web/src/lib/auth-constants.ts fallback.
DEV_JWT_SECRET = "dev-change-me-in-production-min-32-chars!!"

# HS256 keys should carry at least 256 bits (32 bytes) of entropy.
MIN_SECRET_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = Field(default="onboarding-automation", validation_alias="APP_NAME")
    app_env: Literal["development", "staging", "production"] = Field(
        default="development",
        validation_alias="APP_ENV",
    )
    debug: bool = Field(default=False, validation_alias="DEBUG")
    api_v1_prefix: str = Field(default="/api/v1", validation_alias="API_V1_PREFIX")

    host: str = Field(default="0.0.0.0", validation_alias="HOST")
    port: int = Field(default=8000, validation_alias="PORT")

    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    log_json: bool = Field(default=False, validation_alias="LOG_JSON")

    # Database. SQLite (async) by default for zero-setup local dev; point this at
    # `postgresql+asyncpg://...` in staging/production. The same ORM code runs on both.
    database_url: str = Field(
        default="sqlite+aiosqlite:///./onboarding.db",
        validation_alias="DATABASE_URL",
    )
    # Echo raw SQL to logs. Off by default even in DEBUG mode — SQL echo is noisy
    # and independent of app-level debugging. Set DB_ECHO=true to enable.
    db_echo: bool = Field(default=False, validation_alias="DB_ECHO")

    jwt_secret_key: str = Field(
        default=DEV_JWT_SECRET,
        validation_alias="JWT_SECRET_KEY",
    )
    jwt_algorithm: str = Field(default="HS256", validation_alias="JWT_ALGORITHM")
    access_token_expire_minutes: int = Field(
        default=60 * 24,
        validation_alias="ACCESS_TOKEN_EXPIRE_MINUTES",
    )

    # AI chat provider. "mock" streams a canned reply with no external calls;
    # "openai"/"gemini" activate real models when the matching API key is set.
    chat_provider: Literal["mock", "openai", "gemini"] = Field(
        default="mock",
        validation_alias="CHAT_PROVIDER",
    )
    # AI assessment provider (resume scoring + interview judgment). Same options
    # as chat; "mock" is deterministic and needs no key.
    assessment_provider: Literal["mock", "openai", "gemini"] = Field(
        default="mock",
        validation_alias="ASSESSMENT_PROVIDER",
    )
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", validation_alias="OPENAI_MODEL")
    gemini_api_key: str | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    gemini_model: str = Field(
        default="gemini-1.5-flash",
        validation_alias="GEMINI_MODEL",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @model_validator(mode="after")
    def _validate_production_secret(self) -> "Settings":
        """Fail fast rather than boot production with a weak or default secret.

        Without this guard, a deployment that forgets to set JWT_SECRET_KEY would
        sign tokens with a publicly known string, letting anyone forge an admin
        token. We only enforce in production so dev and tests stay zero-config.
        """
        if self.app_env == "production":
            if self.jwt_secret_key == DEV_JWT_SECRET:
                raise ValueError(
                    "JWT_SECRET_KEY must be set to a unique value in production "
                    "(the built-in dev default is not allowed)."
                )
            if len(self.jwt_secret_key) < MIN_SECRET_LENGTH:
                raise ValueError(
                    f"JWT_SECRET_KEY must be at least {MIN_SECRET_LENGTH} characters "
                    "in production."
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
