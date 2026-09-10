"""Protect streaming credentials with Windows current-user DPAPI."""

from __future__ import annotations

import base64
import binascii
import ctypes
from ctypes import wintypes
from typing import Protocol, cast


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _Crypt32(Protocol):
    def CryptProtectData(self, *args: object) -> int: ...

    def CryptUnprotectData(self, *args: object) -> int: ...


class _Kernel32(Protocol):
    def LocalFree(self, memory: object) -> object: ...


def _blob_from_bytes(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(value)
    blob = _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def _dpapi() -> tuple[_Crypt32, _Kernel32]:
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
    except AttributeError as exc:
        raise RuntimeError("Windows DPAPI is unavailable") from exc
    return cast(_Crypt32, crypt32), cast(_Kernel32, kernel32)


def encrypt_secret(value: str) -> str:
    """Encrypt *value* for the current Windows user and return URL-safe base64."""
    if not isinstance(value, str):
        raise TypeError("Secret must be a string")

    plaintext, plaintext_buffer = _blob_from_bytes(value.encode("utf-8"))
    protected = _DataBlob()
    crypt32, kernel32 = _dpapi()
    if not crypt32.CryptProtectData(
        ctypes.byref(plaintext),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(protected),
    ):
        raise OSError(ctypes.get_last_error(), "Unable to encrypt secret")
    del plaintext_buffer
    try:
        encrypted = ctypes.string_at(protected.pbData, protected.cbData)
        return base64.urlsafe_b64encode(encrypted).decode("ascii")
    finally:
        kernel32.LocalFree(protected.pbData)


def decrypt_secret(value: str) -> str:
    """Decrypt URL-safe base64 DPAPI data, rejecting all malformed input."""
    if not isinstance(value, str):
        raise TypeError("Encrypted secret must be a string")
    try:
        encoded = value.encode("ascii")
        encrypted = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ValueError("Unable to decrypt secret") from exc
    if not encrypted:
        raise ValueError("Unable to decrypt secret")

    protected, protected_buffer = _blob_from_bytes(encrypted)
    plaintext = _DataBlob()
    crypt32, kernel32 = _dpapi()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(protected),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(plaintext),
    ):
        raise ValueError("Unable to decrypt secret")
    del protected_buffer
    try:
        raw = ctypes.string_at(plaintext.pbData, plaintext.cbData)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Unable to decrypt secret") from exc
    finally:
        kernel32.LocalFree(plaintext.pbData)
