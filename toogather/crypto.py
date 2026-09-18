"""
Encrypting small secrets before they are stored.

Two things end up in the database that must not be readable by someone who
gets a copy of it: a project's AI provider API key, and a connector's access
token. Both are encrypted here first.

How the key is derived: TooGather already has exactly one long-lived secret,
SECRET_KEY, and asking an operator to manage a second one is how installs end
up with `ENCRYPTION_KEY=change-me`. So the encryption key is derived from
SECRET_KEY with HKDF and a fixed label. The label means the derived key is
unrelated to the session-signing use of SECRET_KEY: neither one can be used to
attack the other.

What this protects against, stated honestly:

  * A database dump, a stolen backup, or a read-only SQL user. These see
    ciphertext only, because SECRET_KEY lives in the environment or in
    TOOGATHER_DATA_DIR, not in PostgreSQL.

  * It does NOT protect against someone who can already read the server's
    environment or run code on it. They have SECRET_KEY, and therefore the
    keys. Nothing short of an external KMS would change that, and a KMS is
    not something a small self-hosted install should have to run.

If SECRET_KEY changes, old ciphertext can no longer be read. That is why
decrypt_secret returns None instead of raising: the app treats an unreadable
key as "not set" and asks for it again, which is recoverable, rather than
crashing every page that touches it.
"""

from __future__ import annotations

import base64
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger("toogather.crypto")

# Changing this label makes every stored secret unreadable, so it is versioned
# rather than edited. A future v2 would try v2 first and fall back to v1.
_HKDF_INFO = b"toogather-stored-secret-v1"


@lru_cache(maxsize=4)
def _cipher(secret_key: str) -> Fernet:
    """
    The Fernet cipher for this SECRET_KEY.

    Cached because HKDF runs on every encrypt and decrypt otherwise, and the
    key never changes while the process is alive. maxsize is small on purpose:
    there is normally exactly one key.
    """
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,          # SECRET_KEY is already high-entropy; HKDF allows this
        info=_HKDF_INFO,
    ).derive(secret_key.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_secret(secret_key: str, plaintext: str) -> str:
    """
    Encrypt a secret for storage. An empty secret stays empty.

    Returning "" for "" keeps the "is a key set?" check in the rest of the app
    to a simple truth test on the stored column.
    """
    if not plaintext:
        return ""
    return _cipher(secret_key).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(secret_key: str, token: str) -> str | None:
    """
    Read a stored secret back, or None if it cannot be read.

    None means one of two things, and the caller should treat both the same
    way - as "there is no usable key here":
      * nothing was stored, or
      * SECRET_KEY has changed since it was stored.
    """
    if not token:
        return None
    try:
        return _cipher(secret_key).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError):
        log.warning(
            "A stored secret could not be decrypted. This normally means SECRET_KEY "
            "changed since it was saved. Enter the secret again to replace it."
        )
        return None
