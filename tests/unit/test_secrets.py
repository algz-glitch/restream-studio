import base64
import os

import pytest

from restream_studio.security.secrets import decrypt_secret, encrypt_secret


def test_unicode_secret_round_trip_is_encrypted() -> None:
    secret = "\u76f4\u64ad-\U0001f510-credential"

    encrypted = encrypt_secret(secret)

    assert encrypted != secret
    assert secret not in encrypted
    assert decrypt_secret(encrypted) == secret


@pytest.mark.parametrize(
    "ciphertext",
    [
        "%%%not-base64%%%",
        base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
        base64.urlsafe_b64encode(b"not-a-dpapi-blob").decode("ascii"),
    ],
)
def test_invalid_ciphertext_raises_stable_typed_error(ciphertext: str) -> None:
    with pytest.raises(ValueError, match="Unable to decrypt secret"):
        decrypt_secret(ciphertext)
