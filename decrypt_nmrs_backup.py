#!/usr/bin/env python3
"""
decrypt_nmrs_backup.py — Standalone decryptor for NMRS Toolkit backup files.

Decrypts and decompresses a .sql.gz.enc file produced by the NMRS Toolkit
without needing the full app installed. Hand this single file to anyone who
needs to inspect or restore a backup; they only need Python 3 and the
`cryptography` package.

File format (NMRS Toolkit v2 encrypted payload):
    magic (8B)  : "NMRS2\\x00\\x00\\x00"
    nonce       : 12 bytes (random per file)
    ciphertext  : AES-GCM (with 16-byte tag appended)
  Inner payload (after decrypt): a gzip-compressed mysqldump SQL stream.

Install:
    pip install cryptography

Usage:
    python3 decrypt_nmrs_backup.py <input.sql.gz.enc> --key <hex>
    python3 decrypt_nmrs_backup.py <input.sql.gz.enc> --key-stdin
    python3 decrypt_nmrs_backup.py <input.sql.gz.enc> --master <hex> --facility "Name"

Examples:
    # Decrypt with the facility's backup_key directly (64 hex chars):
    python3 decrypt_nmrs_backup.py backup.sql.gz.enc --key aabbcc...

    # Manager workflow — derive the facility key on the fly:
    python3 decrypt_nmrs_backup.py imo_state_uth_nmrs_backup_*.sql.gz.enc \\
        --master $MASTER_SECRET --facility "Imo State University Teaching Hospital"

Output: a plain .sql file you can import with `mysql -u<user> -p <db> < dump.sql`.
"""

import argparse
import getpass
import gzip
import hashlib
import hmac
import sys
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    sys.stderr.write(
        "Missing dependency: cryptography\n"
        "Install with:  pip install cryptography\n"
    )
    sys.exit(2)

MAGIC = b"NMRS2\x00\x00\x00"
MAGIC_LEGACY = b"NMRS1\x00\x00\x00"
NONCE_LEN = 12
KEY_LEN = 32


def derive_facility_key(master_secret_hex: str, facility_name: str) -> bytes:
    master = bytes.fromhex(master_secret_hex.strip())
    if len(master) != KEY_LEN:
        raise ValueError(f"master_secret must be {KEY_LEN} bytes ({KEY_LEN * 2} hex chars)")
    return hmac.new(master, facility_name.strip().encode("utf-8"),
                    hashlib.sha256).digest()


def decrypt(blob: bytes, key: bytes) -> bytes:
    if blob.startswith(MAGIC_LEGACY):
        raise SystemExit(
            "This file uses the legacy v1 format (passphrase-based, pre-2026 toolkit). "
            "It cannot be decrypted with the current scheme."
        )
    if not blob.startswith(MAGIC):
        raise ValueError("Not an NMRS-encrypted file (bad magic header).")
    if len(key) != KEY_LEN:
        raise ValueError(f"key must be {KEY_LEN} bytes")
    pos = len(MAGIC)
    nonce = blob[pos:pos + NONCE_LEN]
    pos += NONCE_LEN
    ct = blob[pos:]
    return AESGCM(key).decrypt(nonce, ct, associated_data=None)


def main():
    ap = argparse.ArgumentParser(
        description="Decrypt an NMRS Toolkit backup file (.sql.gz.enc) to plain SQL.",
    )
    ap.add_argument("input", help="Path to a .sql.gz.enc file")
    ap.add_argument("--output", "-o",
                    help="Output path for the plain .sql file "
                         "(default: input filename without .gz.enc)")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--key",
                     help="Backup key as 64 hex chars (32 bytes)")
    grp.add_argument("--key-stdin", action="store_true",
                     help="Read the hex key from stdin")
    grp.add_argument("--master",
                     help="Manager master_secret (hex) — combined with "
                          "--facility to derive the per-facility key")
    ap.add_argument("--facility",
                    help="Facility name, paired with --master to derive the key")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"File not found: {src}")

    if args.output:
        dest = Path(args.output)
    elif src.name.endswith(".sql.gz.enc"):
        dest = src.with_name(src.name[:-len(".gz.enc")])  # -> *.sql
    else:
        dest = src.with_suffix(".sql")

    # Resolve the key.
    if args.master:
        if not args.facility:
            sys.exit("--master requires --facility")
        key = derive_facility_key(args.master, args.facility)
        print(f"Derived key for facility: {args.facility}", file=sys.stderr)
    else:
        if args.key:
            hex_key = args.key
        elif args.key_stdin:
            hex_key = sys.stdin.readline().rstrip("\n")
        else:
            hex_key = getpass.getpass("Backup key (64 hex chars): ")
        try:
            key = bytes.fromhex(hex_key.strip())
        except ValueError as e:
            sys.exit(f"Invalid hex key: {e}")
        if len(key) != KEY_LEN:
            sys.exit(f"Key must be {KEY_LEN} bytes ({KEY_LEN * 2} hex chars)")

    print(f"Reading:    {src}", file=sys.stderr)
    blob = src.read_bytes()
    print(f"Decrypting  ({len(blob):,} bytes)...", file=sys.stderr)
    try:
        gzipped = decrypt(blob, key)
    except Exception as e:
        sys.exit(f"Decrypt failed: {e}\n(Wrong key or corrupt file?)")
    print(f"Gunzipping  ({len(gzipped):,} bytes)...", file=sys.stderr)
    sql_bytes = gzip.decompress(gzipped)
    print(f"Writing:    {dest}  ({len(sql_bytes):,} bytes)", file=sys.stderr)
    dest.write_bytes(sql_bytes)
    print(f"Done. Import with:  mysql -u<user> -p <db> < {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
