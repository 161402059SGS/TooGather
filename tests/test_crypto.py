"""
Tests for encrypting stored secrets.

These pin down the two behaviours the rest of the app relies on: a secret
survives a round trip, and a secret encrypted under a different SECRET_KEY
comes back as None rather than raising. The second one is what lets a server
whose SECRET_KEY was replaced still open every page and simply ask for the
key again.
"""

from toogather.crypto import decrypt_secret, encrypt_secret

KEY = "a-long-enough-secret-key-for-tests-0123456789"
OTHER_KEY = "a-completely-different-secret-key-9876543210"


def test_round_trip_returns_the_original():
    token = encrypt_secret(KEY, "sk-proj-abcdef123456")
    assert decrypt_secret(KEY, token) == "sk-proj-abcdef123456"


def test_ciphertext_does_not_contain_the_secret():
    token = encrypt_secret(KEY, "sk-proj-abcdef123456")
    assert "sk-proj" not in token
    assert "abcdef123456" not in token


def test_the_same_secret_encrypts_differently_each_time():
    """Fernet includes a random IV, so equal keys must not have equal ciphertext."""
    assert encrypt_secret(KEY, "same") != encrypt_secret(KEY, "same")


def test_an_empty_secret_stays_empty():
    # The rest of the app tests the stored column for truthiness to answer
    # "is a key set?", so "" must not become a valid-looking token.
    assert encrypt_secret(KEY, "") == ""
    assert decrypt_secret(KEY, "") is None


def test_a_different_key_cannot_read_it():
    token = encrypt_secret(KEY, "sk-proj-abcdef123456")
    assert decrypt_secret(OTHER_KEY, token) is None


def test_rubbish_is_refused_rather_than_raising():
    assert decrypt_secret(KEY, "not-a-token") is None
    assert decrypt_secret(KEY, "!!! not even base64 !!!") is None
