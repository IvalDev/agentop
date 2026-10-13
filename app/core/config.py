"""Typed application settings loaded from environment variables."""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings.

    Every field is read from an environment variable prefixed with ``AA_``
    (for example ``AA_DATABASE_URL``). Defaults keep the app importable
    without any environment configuration during scaffolding.
    """

    model_config = SettingsConfigDict(env_prefix="AA_")

    environment: str = "dev"
    database_url: str = ""
    database_pool_size: int = 5
    database_max_overflow: int = 10
    redis_url: str = ""
    minio_endpoint: str = ""
    minio_access_key: str = ""
    minio_secret_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    log_level: str = "INFO"

    @model_validator(mode="after")
    def require_jwt_secret_in_production(self) -> "Settings":
        """Refuse to run in production when no JWT signing secret is configured."""
        if self.environment == "production" and not self.jwt_secret:
            raise ValueError("jwt_secret must be non-empty when environment is 'production'")
        return self
