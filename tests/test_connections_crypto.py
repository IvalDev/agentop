"""Credential crypto tests: Fernet round trip, wrong key, key-format failures.

``app.core.crypto`` is pure functions over explicit keys: nothing here reads
settings or the database. Every failure is asserted to carry no key material
and no plaintext in its message (feature acceptance criterion 3: credentials
never reach logs, errors, or anything user-visible).
"""

import base64

import pytest

from app.core.crypto import (
    CredentialsKeyFormatError,
    DecryptionError,
    decrypt_credentials,
    encrypt_credentials,
    generate_credentials_key,
    validate_credentials_key,
)

PLAINTEXT = "calDAV-password-ñ-🔐"


def test_generated_key_passes_validation() -> None:
    """A freshly generated key is a string and validates without raising."""
    key = generate_credentials_key()

    assert isinstance(key, str)
    validate_credentials_key(key)


def test_encrypt_decrypt_round_trips() -> None:
    """Ciphertext is opaque and decrypts back to the exact plaintext."""
    key = generate_credentials_key()

    token = encrypt_credentials(key, PLAINTEXT)

    assert isinstance(token, str)
    assert token != PLAINTEXT
    assert PLAINTEXT not in token
    assert key not in token
    assert decrypt_credentials(key, token) == PLAINTEXT


def test_round_trip_keeps_an_empty_plaintext() -> None:
    """The empty string is valid input and survives the round trip."""
    key = generate_credentials_key()

    token = encrypt_credentials(key, "")

    assert decrypt_credentials(key, token) == ""


def test_wrong_key_raises_decryption_error_without_leaking() -> None:
    """A token from another key fails closed with no key or plaintext inside."""
    key = generate_credentials_key()
    other_key = generate_credentials_key()
    token = encrypt_credentials(key, PLAINTEXT)

    with pytest.raises(DecryptionError) as excinfo:
        decrypt_credentials(other_key, token)

    rendered = repr(excinfo.value)
    assert key not in rendered
    assert other_key not in rendered
    assert PLAINTEXT not in rendered
    assert PLAINTEXT not in str(excinfo.value)
    cause = excinfo.value.__cause__
    if cause is not None:
        assert PLAINTEXT not in repr(cause)
        assert key not in repr(cause)


def test_garbage_token_raises_decryption_error() -> None:
    """A non-Fernet token is a DecryptionError, never a raw library error."""
    key = generate_credentials_key()

    with pytest.raises(DecryptionError):
        decrypt_credentials(key, "definitely-not-a-fernet-token")


def test_validate_rejects_malformed_keys_without_echoing_them() -> None:
    """Empty, non-base64, and wrong-length keys each fail with a clear error."""
    too_short = base64.urlsafe_b64encode(b"only-sixteen-btes").decode()

    for bad_key in ("", "not-a-fernet-key", too_short):
        with pytest.raises(CredentialsKeyFormatError, match="urlsafe base64"):
            validate_credentials_key(bad_key)

    with pytest.raises(CredentialsKeyFormatError) as excinfo:
        validate_credentials_key("not-a-fernet-key")
    assert "not-a-fernet-key" not in str(excinfo.value)


def test_key_format_error_is_deliberately_not_a_value_error() -> None:
    """Pydantic folds ``ValueError`` from validators into a ``ValidationError``.

    That rendering echoes the raw input dict — for ``Settings`` that dict holds
    the plaintext key — so the format error must stay outside the ValueError
    family and propagate untouched through ``Settings()``.
    """
    assert not issubclass(CredentialsKeyFormatError, ValueError)
    assert not issubclass(CredentialsKeyFormatError, AssertionError)


def test_encrypt_rejects_a_malformed_key_before_touching_the_plaintext() -> None:
    """A bad key fails closed at encryption time with the same clear error."""
    with pytest.raises(CredentialsKeyFormatError, match="urlsafe base64"):
        encrypt_credentials("not-a-fernet-key", PLAINTEXT)
