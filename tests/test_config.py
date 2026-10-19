"""Settings configuration tests."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.crypto import CredentialsKeyFormatError, generate_credentials_key


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
    """Production environment accepts a non-empty jwt_secret and a valid key."""
    monkeypatch.setenv("AA_ENVIRONMENT", "production")
    monkeypatch.setenv("AA_JWT_SECRET", "non-empty-secret")
    monkeypatch.setenv("AA_CREDENTIALS_KEY", generate_credentials_key())

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


def test_production_without_credentials_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production refuses to start with no Fernet key for connection credentials."""
    monkeypatch.setenv("AA_ENVIRONMENT", "production")
    monkeypatch.setenv("AA_JWT_SECRET", "non-empty-secret")
    monkeypatch.delenv("AA_CREDENTIALS_KEY", raising=False)

    with pytest.raises(ValidationError, match="credentials_key"):
        Settings()


def test_credentials_key_is_a_secret_never_shown_in_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key round-trips through SecretStr and never appears in repr/str."""
    key = generate_credentials_key()
    monkeypatch.setenv("AA_ENVIRONMENT", "production")
    monkeypatch.setenv("AA_JWT_SECRET", "non-empty-secret")
    monkeypatch.setenv("AA_CREDENTIALS_KEY", key)

    settings = Settings()

    assert settings.credentials_key is not None
    assert settings.credentials_key.get_secret_value() == key
    assert key not in repr(settings)
    assert key not in str(settings)


def test_malformed_credentials_key_fails_eagerly_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-Fernet key explodes at construction with a clear, leak-free error."""
    monkeypatch.setenv("AA_CREDENTIALS_KEY", "definitely-not-a-fernet-key")

    with pytest.raises(CredentialsKeyFormatError, match="urlsafe base64") as excinfo:
        Settings()

    assert "definitely-not-a-fernet-key" not in str(excinfo.value)


def test_dev_environment_keeps_credentials_key_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside production the key may be absent; services must then fail closed."""
    monkeypatch.delenv("AA_ENVIRONMENT", raising=False)
    monkeypatch.delenv("AA_CREDENTIALS_KEY", raising=False)

    settings = Settings()

    assert settings.environment == "dev"
    assert settings.credentials_key is None


def test_google_oauth_credentials_load_from_env_as_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AA_GOOGLE_CLIENT_ID / AA_GOOGLE_CLIENT_SECRET load into SecretStr fields."""
    monkeypatch.setenv("AA_GOOGLE_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setenv("AA_GOOGLE_CLIENT_SECRET", "GOCSPX-test-secret")

    settings = Settings()

    assert settings.google_client_id is not None
    assert settings.google_client_id.get_secret_value() == "client-id.apps.googleusercontent.com"
    assert settings.google_client_secret is not None
    assert settings.google_client_secret.get_secret_value() == "GOCSPX-test-secret"
    assert "GOCSPX-test-secret" not in repr(settings)
    assert "GOCSPX-test-secret" not in str(settings)


def test_google_credentials_default_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Google OAuth stays optional: without env both fields are None."""
    monkeypatch.delenv("AA_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("AA_GOOGLE_CLIENT_SECRET", raising=False)

    settings = Settings()

    assert settings.google_client_id is None
    assert settings.google_client_secret is None
