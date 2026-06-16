"""Cryptography: AES-GCM envelope and HMAC-SHA256 facility-key derivation.

PRESERVED VERBATIM from the legacy build (MIGRATION_PLAN.md section 2). The
envelope formats (NMRS2 magic + 12-byte nonce + AES-GCM) and the
derive_facility_key output for a given (master_secret, facility) pair MUST
remain byte-identical.
"""
from __future__ import annotations

import configparser
import hashlib
import hmac
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CRYPTO_MAGIC = b"NMRS2\x00\x00\x00"
CRYPTO_MAGIC_LEGACY = b"NMRS1\x00\x00\x00"
CRYPTO_NONCE_LEN = 12
CRYPTO_KEY_LEN = 32


def _validate_key(key: bytes) -> None:
    if not isinstance(key, (bytes, bytearray)) or len(key) != CRYPTO_KEY_LEN:
        raise ValueError(
            f"backup key must be {CRYPTO_KEY_LEN} bytes "
            f"(got {len(key) if hasattr(key, '__len__') else type(key).__name__})"
        )


def encrypt_bytes(plaintext: bytes, key: bytes) -> bytes:
    _validate_key(key)
    nonce = os.urandom(CRYPTO_NONCE_LEN)
    ct = AESGCM(bytes(key)).encrypt(nonce, plaintext, associated_data=None)
    return CRYPTO_MAGIC + nonce + ct


def decrypt_bytes(blob: bytes, key: bytes) -> bytes:
    if blob.startswith(CRYPTO_MAGIC_LEGACY):
        raise ValueError(
            "This file is in the legacy v1 (passphrase) format. Re-encrypt it "
            "with the current toolkit to read it."
        )
    if not blob.startswith(CRYPTO_MAGIC):
        raise ValueError("File does not look like an NMRS-encrypted payload (bad magic)")
    _validate_key(key)
    pos = len(CRYPTO_MAGIC)
    nonce = blob[pos:pos + CRYPTO_NONCE_LEN]
    pos += CRYPTO_NONCE_LEN
    ct = blob[pos:]
    return AESGCM(bytes(key)).decrypt(nonce, ct, associated_data=None)


def is_encrypted_file(path: Path) -> bool:
    """Cheap header-only check — recognises both current and legacy magic."""
    try:
        with open(path, "rb") as f:
            head = f.read(len(CRYPTO_MAGIC))
        return head == CRYPTO_MAGIC or head == CRYPTO_MAGIC_LEGACY
    except OSError:
        return False


def get_facility_key(config: configparser.ConfigParser) -> bytes:
    """Read [backup] backup_key from config and validate. Raises with a
    helpful message if missing or malformed."""
    hex_key = config.get("backup", "backup_key", fallback="").strip()
    if not hex_key:
        raise RuntimeError(
            "No backup_key configured. Add a 64-character hex value to "
            "[backup] backup_key in .nmrs_config.ini. The manager generates "
            "this with derive_key.py for each facility."
        )
    try:
        key = bytes.fromhex(hex_key)
    except ValueError as e:
        raise RuntimeError(f"backup_key in config is not valid hex: {e}")
    if len(key) != CRYPTO_KEY_LEN:
        raise RuntimeError(
            f"backup_key must be {CRYPTO_KEY_LEN} bytes "
            f"({CRYPTO_KEY_LEN * 2} hex chars); got {len(key)} bytes"
        )
    return key


def derive_facility_key(master_secret_hex: str, facility_name: str) -> bytes:
    """Deterministically derive a per-facility 32-byte key from the manager's
    master_secret and the facility name. Used by the manager-side derive_key.py
    utility and the (future) in-app manager tools."""
    if not master_secret_hex:
        raise RuntimeError("master_secret is empty")
    try:
        master = bytes.fromhex(master_secret_hex.strip())
    except ValueError as e:
        raise RuntimeError(f"master_secret is not valid hex: {e}")
    if len(master) != CRYPTO_KEY_LEN:
        raise RuntimeError(
            f"master_secret must be {CRYPTO_KEY_LEN} bytes "
            f"({CRYPTO_KEY_LEN * 2} hex chars); got {len(master)} bytes"
        )
    facility = (facility_name or "").strip()
    if not facility:
        raise RuntimeError("facility_name is empty")
    return hmac.new(master, facility.encode("utf-8"), hashlib.sha256).digest()

