"""
Security helpers: passwords, API tokens, and CSRF tokens.

Kept small and separate on purpose so that security-sensitive code is easy to
find and review in one place.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# argon2 is the current recommended password hashing algorithm. The library's
# defaults are sensible; do not lower them to make logins "faster".
_hasher = PasswordHasher()

# Prefix makes tokens recognisable, e.g. by secret scanners, if one leaks.
API_TOKEN_PREFIX = "tg_"

MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Return True if the password matches. Never raises on a bad password."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def password_problem(password: str) -> str | None:
    """Return a readable problem with the password, or None if it is acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    return None


def new_api_token() -> tuple[str, str]:
    """
    Create an API token.

    Returns (plain_token, token_hash). Show plain_token to the user exactly
    once; store only token_hash. Tokens are long and random, so a fast SHA-256
    hash is appropriate here (unlike passwords, they cannot be guessed).
    """
    plain = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
    return plain, hash_api_token(plain)


def hash_api_token(plain_token: str) -> str:
    return hashlib.sha256(plain_token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(expected: str | None, received: str | None) -> bool:
    """Constant-time comparison so response timing reveals nothing."""
    if not expected or not received:
        return False
    return hmac.compare_digest(expected, received)
