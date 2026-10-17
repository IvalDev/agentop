"""Typed application settings loaded from environment variables."""

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.crypto import validate_credentials_key

#: ``environment`` values that enable development-only features (the ``/ui``
#: console). Matched exactly, like the demo seed: ``PRODUCTION`` is not ``dev``.
DEVELOPMENT_ENVIRONMENTS = frozenset({"dev", "development", "local", "test"})

#: Build output of the React web app (``npm --prefix web run build``).
DEFAULT_WEB_DIST_DIR = Path(__file__).resolve().parents[2] / "web" / "dist"


class Settings(BaseSettings):
    """Application settings.

    Every field is read from an environment variable prefixed with ``AA_``
    (for example ``AA_DATABASE_URL``). Defaults keep the app importable
    without any environment configuration during scaffolding.

    Chat model (T9): ``llm_provider`` is ``ollama`` (default, local inference
    at ``ollama_base_url``), ``fake`` (offline tests), or ``openai_compatible``
    — an opt-in door for development against an external OpenAI-compatible
    API. The external provider sends prompts and document content off the
    machine, so it refuses to start unless ``allow_external_llm`` is true and
    ``llm_api_key`` / ``llm_base_url`` are set. Example (xAI Grok)::

        AA_LLM_PROVIDER=openai_compatible
        AA_ALLOW_EXTERNAL_LLM=true
        AA_LLM_BASE_URL=https://api.x.ai/v1
        AA_LLM_MODEL=grok-4
        AA_LLM_API_KEY=<key>

    Embeddings keep their own ``embedding_*`` settings in every case.
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
    # Persistent sessions (T2): how long an unused refresh cookie stays valid,
    # and whether it carries ``Secure`` (True for HTTPS deployments).
    refresh_token_expire_days: int = 30
    refresh_cookie_secure: bool = False
    max_upload_bytes: int = 20 * 1024 * 1024
    storage_base_dir: str = "./var/storage"
    # Retrieval (T5): adapter selection, model identity, and vector size.
    embedding_provider: str = "ollama"  # "ollama" | "fake"
    embedding_model: str = "nomic-embed-text"
    embedding_dim: int = 768
    embedding_timeout_seconds: float = 30.0
    # Agent (T9): chat model adapter, external-inference guard, and loop bound.
    llm_provider: str = "ollama"  # "ollama" | "fake" | "openai_compatible"
    llm_model: str = "qwen2.5:7b-instruct"
    llm_timeout_seconds: float = 120.0
    llm_base_url: str = ""  # openai_compatible only; Ollama uses ollama_base_url
    llm_api_key: SecretStr | None = None  # openai_compatible only
    allow_external_llm: bool = False
    agent_max_steps: int = Field(default=6, ge=1, le=50)  # model calls per chat run
    # Approvals (T7): how long a request stays decidable/executable after creation.
    approval_ttl_seconds: int = 24 * 60 * 60
    # Audit (T8): cap on the compact JSON of each stored payload/result.
    audit_max_payload_bytes: int = 16 * 1024
    log_level: str = "INFO"
    # Web app: served at ``/`` only when this directory holds an ``index.html``.
    web_dist_dir: str = str(DEFAULT_WEB_DIST_DIR)
    bootstrap_email: str = ""
    bootstrap_password: str = ""
    # Connections (T3): Fernet key encrypting per-user connection credentials
    # at rest (``AA_CREDENTIALS_KEY``). Required in production (validated
    # below), optional elsewhere — without it the connections service fails
    # closed and refuses to store credentials. Key lives outside the database.
    credentials_key: SecretStr | None = None
    # Google OAuth (calendar-mvp D2): the app's OAuth client credentials,
    # read only when the authorization-code + PKCE flow starts (the module is
    # optional until used, so both stay None outside a configured deployment).
    google_client_id: SecretStr | None = None
    google_client_secret: SecretStr | None = None

    @property
    def is_development(self) -> bool:
        """Whether ``environment`` is one of :data:`DEVELOPMENT_ENVIRONMENTS`."""
        return self.environment in DEVELOPMENT_ENVIRONMENTS

    @model_validator(mode="after")
    def require_jwt_secret_in_production(self) -> "Settings":
        """Refuse to run in production when no JWT signing secret is configured."""
        if self.environment == "production" and not self.jwt_secret:
            raise ValueError("jwt_secret must be non-empty when environment is 'production'")
        return self

    @model_validator(mode="after")
    def require_credentials_key_in_production(self) -> "Settings":
        """Fail fast when production lacks a credentials key; validate its shape.

        Production without a key raises the usual ``ValidationError`` (nothing
        secret exists in that case). A key that *is* present is format-checked
        here so a bad value explodes at startup, not on first write — the
        format check raises :class:`CredentialsKeyFormatError`, deliberately
        not a ``ValueError``: pydantic wraps validator ``ValueError``s into a
        ``ValidationError`` whose rendering echoes the raw input, i.e. the key
        itself. See that exception's docstring for the full reasoning.
        """
        if not self.credentials_key or not self.credentials_key.get_secret_value():
            if self.environment == "production":
                raise ValueError(
                    "credentials_key must be non-empty when environment is 'production'"
                )
            return self
        validate_credentials_key(self.credentials_key.get_secret_value())
        return self
