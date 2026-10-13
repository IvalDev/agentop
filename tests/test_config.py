"""Settings configuration tests."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_default_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings read AA_-prefixed environment variables and keep defaults."""
    monkeypatch.setenv("AA_DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/admin")
    monkeypatch.setenv("AA_LOG_LEVEL", "DEBUG")

    settings = Settings()

    assert settings.database_url == "postgresql+asyncpg://user:pass@localhost:5432/admin"
    assert settings.log_level == "DEBUG"
    assert settings.environment == "dev"
    assert settings.database_pool_size == 5
    assert settings.database_max_overflow == 10
    assert settings.access_token_expire_minutes == 30


def test_production_with_empty_jwt_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production environment refuses to start with an empty jwt_secret."""
    monkeypatch.setenv("AA_ENVIRONMENT", "production")
    monkeypatch.setenv("AA_JWT_SECRET", "")

    with pytest.raises(ValidationError, match="jwt_secret"):
        Settings()


def test_production_with_jwt_secret_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production environment accepts a non-empty jwt_secret."""
    monkeypatch.setenv("AA_ENVIRONMENT", "production")
    monkeypatch.setenv("AA_JWT_SECRET", "non-empty-secret")

    settings = Settings()

    assert settings.environment == "production"
    assert settings.jwt_secret == "non-empty-secret"


def test_dev_environment_allows_empty_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-production environments keep the empty jwt_secret default."""
    monkeypatch.delenv("AA_ENVIRONMENT", raising=False)
    monkeypatch.delenv("AA_JWT_SECRET", raising=False)

    settings = Settings()

    assert settings.environment == "dev"
    assert settings.jwt_secret == ""
