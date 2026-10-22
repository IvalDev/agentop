"""Request/response schemas of the connector API (L1 contract)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PairingCodeResponse(BaseModel):
    """A newly minted pairing code and its ten-minute expiry."""

    code: str
    expires_at: datetime


class PairRequest(BaseModel):
    """Body of ``POST /api/v1/connectors/pair``; the code is the secret."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(max_length=32)
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def _require_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name must not be blank")
        return name


class PairResponse(BaseModel):
    """The paired connector plus its raw token, shown exactly once."""

    connector_id: UUID
    token: str
    name: str


class ConnectorResponse(BaseModel):
    """Public view of one connector; never a token, hash, or folder path."""

    id: UUID
    name: str
    connected: bool
    last_seen_at: datetime | None
    created_at: datetime


class ConnectorListResponse(BaseModel):
    """The caller's own active connectors."""

    connectors: list[ConnectorResponse]
