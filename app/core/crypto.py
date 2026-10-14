"""Fernet encryption for provider credentials stored at rest.

Design rules that drive this module (feature criterion 3: credentials never
reach API responses, logs, audit payloads, or the model):

- pure functions over an explicit ``key`` argument — no settings import, no
  module-level key, no hidden state: callers pass the value of
  ``Settings.credentials_key`` (or a test key) and unit tests need no config;
- string in, string out: plaintext never touches the database as bytes and
  callers never handle key material beyond the one explicit argument;
- every failure carries a fixed message and never echoes the key or the
  plaintext;
- no logging here: this module's only job is to encrypt and decrypt.

A Fernet key is urlsafe base64 encoding of 32 random bytes. Generate one with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import base64
import binascii

from cryptography.fernet import Fernet, InvalidToken

#: How to mint a fresh key; part of every format-error message (never the key).
_KEY_HELP = (
    'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
)
_FORMAT_MESSAGE = (
    "credentials key must be urlsafe base64 encoding exactly 32 bytes; "
    f"generate one with: {_KEY_HELP}"
)


class CredentialsKeyFormatError(Exception):
    """The configured Fernet key is not urlsafe base64 of exactly 32 bytes.

    Deliberately not a ``ValueError``: pydantic folds a ``ValueError`` raised
    inside a ``model_validator`` into a ``ValidationError`` whose rendering
    echoes the raw input dict — for ``Settings`` that dict holds the key
    itself. A dedicated type propagates through ``Settings()`` untouched, so
    a bad key fails loudly at startup without ever being printed.
    """


class DecryptionError(Exception):
    """A stored token could not be decrypted (wrong key or corrupted data).

    The message is fixed; the library error is only chained as ``__cause__``
    (``InvalidToken``/``TypeError`` carry no key or plaintext), so neither the
    key nor the credentials can leak through the exception text.
    """


def generate_credentials_key() -> str:
    """Return a fresh Fernet key as an ASCII string (value for ``AA_CREDENTIALS_KEY``)."""
    return Fernet.generate_key().decode("ascii")


def validate_credentials_key(key: str) -> None:
    """Raise :class:`CredentialsKeyFormatError` unless ``key`` is a Fernet key.

    Checks urlsafe base64, the exact 32-byte length, and the canonical
    round-trip — a key that fails here fails at startup, never on first
    decrypt.
    """
    if not isinstance(key, str):
        raise CredentialsKeyFormatError(_FORMAT_MESSAGE)
    try:
        raw = base64.urlsafe_b64decode(key.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise CredentialsKeyFormatError(_FORMAT_MESSAGE) from None
    if len(raw) != 32 or base64.urlsafe_b64encode(raw) != key.encode("ascii"):
        raise CredentialsKeyFormatError(_FORMAT_MESSAGE)


def encrypt_credentials(key: str, plaintext: str) -> str:
    """Encrypt ``plaintext`` under ``key`` and return an opaque Fernet token.

    Raises:
        CredentialsKeyFormatError: ``key`` is not a usable Fernet key.
    """
    validate_credentials_key(key)
    return Fernet(key.encode("ascii")).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_credentials(key: str, token: str) -> str:
    """Decrypt ``token`` back to the original string.

    Raises:
        CredentialsKeyFormatError: ``key`` is not a usable Fernet key.
        DecryptionError: the token is not valid for this key (or corrupted);
            the message carries no key material and no plaintext.
    """
    validate_credentials_key(key)
    try:
        plaintext = Fernet(key.encode("ascii")).decrypt(token.encode("ascii"))
    except (InvalidToken, TypeError, ValueError) as exc:
        raise DecryptionError("stored credentials could not be decrypted") from exc
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DecryptionError("stored credentials could be corrupted") from exc
