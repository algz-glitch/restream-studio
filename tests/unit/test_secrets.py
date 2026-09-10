import base64
import ctypes
import os
from typing import cast

import pytest

import restream_studio.security.secrets as secrets_module
from restream_studio.security.secrets import decrypt_secret, encrypt_secret


def test_unicode_secret_round_trip_is_encrypted() -> None:
    secret = "\u76f4\u64ad-\U0001f510-credential"

    encrypted = encrypt_secret(secret)

    assert encrypted != secret
    assert secret not in encrypted
    assert decrypt_secret(encrypted) == secret


@pytest.mark.parametrize("secret", ["", "prefix\x00suffix"])
def test_empty_and_embedded_nul_secrets_round_trip(secret: str) -> None:
    assert decrypt_secret(encrypt_secret(secret)) == secret


@pytest.mark.parametrize("value", [None, 1, b"bytes"])
def test_public_apis_reject_non_strings(value: object) -> None:
    with pytest.raises(TypeError):
        encrypt_secret(value)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        decrypt_secret(value)  # type: ignore[arg-type]


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


@pytest.mark.parametrize("ciphertext", ["+/8=", "AB=="])
def test_noncanonical_base64_is_rejected(ciphertext: str) -> None:
    with pytest.raises(ValueError, match="Unable to decrypt secret"):
        decrypt_secret(ciphertext)


class _FakeFunction:
    def __init__(self, result: int) -> None:
        self.result = result
        self.argtypes: object = None
        self.restype: object = None
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *args: object) -> int:
        self.calls.append(args)
        return self.result


class _FakeCrypt32:
    def __init__(self, protect_result: int = 1, unprotect_result: int = 1) -> None:
        self.CryptProtectData = _FakeFunction(protect_result)
        self.CryptUnprotectData = _FakeFunction(unprotect_result)


class _FakeKernel32:
    def __init__(self) -> None:
        self.LocalFree = _FakeFunction(0)


def test_winapi_failure_preserves_immediate_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    crypt32 = _FakeCrypt32(protect_result=0)
    kernel32 = _FakeKernel32()
    requested: list[tuple[str, bool]] = []

    def fake_windll(name: str, *, use_last_error: bool) -> object:
        requested.append((name, use_last_error))
        return crypt32 if name == "crypt32" else kernel32

    errors = iter([1234, 9999])
    monkeypatch.setattr(ctypes, "WinDLL", fake_windll)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: next(errors))

    with pytest.raises(OSError) as caught:
        encrypt_secret("failure-case")

    assert caught.value.errno == 1234
    assert requested == [("crypt32", True), ("kernel32", True)]
    assert crypt32.CryptProtectData.argtypes is not None
    assert crypt32.CryptProtectData.restype is not None
    assert kernel32.LocalFree.argtypes is not None
    assert kernel32.LocalFree.restype is not None


@pytest.mark.parametrize("plaintext", [b"decoded", b"\xff"])
def test_decrypt_frees_successful_winapi_allocation_once(
    monkeypatch: pytest.MonkeyPatch, plaintext: bytes
) -> None:
    crypt32 = _FakeCrypt32()
    kernel32 = _FakeKernel32()
    allocation = ctypes.create_string_buffer(plaintext)

    def unprotect(*args: object) -> int:
        output = ctypes.cast(
            cast(ctypes.c_void_p, args[-1]), ctypes.POINTER(secrets_module._DataBlob)
        ).contents
        output.cbData = len(plaintext)
        output.pbData = ctypes.cast(allocation, ctypes.POINTER(ctypes.c_byte))
        return 1

    class _CallableUnprotect(_FakeFunction):
        def __call__(self, *args: object) -> int:
            return unprotect(*args)

    crypt32.CryptUnprotectData = _CallableUnprotect(1)
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, *, use_last_error: crypt32 if name == "crypt32" else kernel32,
    )
    ciphertext = base64.urlsafe_b64encode(b"input-blob").decode("ascii")

    if plaintext == b"decoded":
        assert decrypt_secret(ciphertext) == "decoded"
    else:
        with pytest.raises(ValueError, match="Unable to decrypt secret"):
            decrypt_secret(ciphertext)

    assert len(kernel32.LocalFree.calls) == 1
