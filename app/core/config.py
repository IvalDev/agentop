"""Typed application settings loaded from environment variables."""

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: ``environment`` values that enable development-only features (the ``/ui``
#: console). Matched exactly, like the demo seed: ``PRODUCTION`` is not ``dev``.
DEVELOPMENT_ENVIRONMENTS = frozenset({"dev", "development", "local", "test"})


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
    bootstrap_email: str = ""
    bootstrap_password: str = ""

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
