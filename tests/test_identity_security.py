"""Unit tests for password hashing and JWT helpers."""

import pytest

from app.modules.identity.security import (
    TokenError,
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)

SECRET = "test-secret-0123456789-abcdefghijklmnop"


def test_hash_password_never_stores_plaintext() -> None:
    """Hashed output differs from the password and verifies back."""
    hashed = hash_password("s3cret-pass")
    assert hashed != "s3cret-pass"
    assert verify_password("s3cret-pass", hashed) is True


def test_verify_password_rejects_wrong_password() -> None:
    """A different password does not verify against the hash."""
    hashed = hash_password("s3cret-pass")
    assert verify_password("wrong-pass", hashed) is False


def test_create_and_decode_token_roundtrip() -> None:
    """Issued tokens decode into matching claims with admin-agent iss/aud."""
    token = create_access_token(
        sub="42", tenant_id=7, role="admin", expires_minutes=5, secret=SECRET
    )
    claims = decode_token(token, secret=SECRET)
    assert claims.sub == "42"
    assert claims.tenant_id == 7
    assert claims.role == "admin"
    assert claims.iss == "admin-agent"
    assert claims.aud == "admin-agent"
    assert claims.exp > claims.iat


def test_decode_token_rejects_wrong_secret() -> None:
    """A token signed with another secret fails validation."""
    token = create_access_token(
        sub="1", tenant_id=1, role="viewer", expires_minutes=5, secret=SECRET
    )
    with pytest.raises(TokenError):
        decode_token(token, secret="other-secret-0123456789-abcdefgh")


def test_decode_token_rejects_expired_token() -> None:
    """An expired token raises TokenError instead of returning claims."""
    token = create_access_token(
        sub="1", tenant_id=1, role="viewer", expires_minutes=-1, secret=SECRET
    )
    with pytest.raises(TokenError):
        decode_token(token, secret=SECRET)


def test_decode_token_rejects_garbage_input() -> None:
    """Non-token strings raise TokenError."""
    with pytest.raises(TokenError):
        decode_token("not-a-token", secret=SECRET)
