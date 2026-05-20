#!/usr/bin/env python3
"""
derive_key.py — Manager utility for the NMRS Toolkit.

Generates per-facility backup keys deterministically from a single master
secret. Run on the manager's machine only — facility installs neither hold
nor need the master_secret.

Generation rule:
    backup_key = HMAC-SHA256(master_secret_bytes, facility_name_bytes)

This means:
  * One master secret => keys for any number of facilities, on demand.
  * Same facility name + same master => same key (deterministic).
  * Different facilities get different keys, so a leaked facility binary
    only exposes that one facility's data.

Usage:
    # Show the key for one facility (master read from prompt):
    python3 derive_key.py "Imo State University Teaching Hospital"

    # Pass master via env var (avoids it appearing in shell history):
    NMRS_MASTER=$MASTER python3 derive_key.py "Facility Name"

    # Generate a fresh random master_secret (run once, keep it safe):
    python3 derive_key.py --generate-master

    # Output the config snippet you can paste into a facility's
    # .nmrs_config.ini:
    python3 derive_key.py "Facility Name" --as-config

Security notes:
  * The master_secret never leaves your machine — it is NOT in any facility
    config or binary. Treat it like a root password.
  * Lose the master, and you cannot regenerate any facility's key (existing
    backups are unrecoverable). Back the master up offline.
  * The facility name must match exactly, byte for byte, between key
    generation and the facility's installed binary (lookup uses
    Facility_Name from openmrs.global_property).
"""

import argparse
import getpass
import hashlib
import hmac
import os
import secrets
import sys

KEY_LEN = 32


def derive_facility_key(master_secret_hex: str, facility_name: str) -> bytes:
    master = bytes.fromhex(master_secret_hex.strip())
    if len(master) != KEY_LEN:
        raise ValueError(f"master_secret must be {KEY_LEN} bytes "
                         f"({KEY_LEN * 2} hex chars); got {len(master)} bytes")
    name = (facility_name or "").strip()
    if not name:
        raise ValueError("facility_name is empty")
    return hmac.new(master, name.encode("utf-8"), hashlib.sha256).digest()


def main():
    ap = argparse.ArgumentParser(
        description="Derive per-facility backup keys for NMRS Toolkit.",
    )
    ap.add_argument("facility", nargs="?",
                    help="Facility name (as stored in openmrs.global_property "
                         "where property = 'Facility_Name')")
    ap.add_argument("--generate-master", action="store_true",
                    help="Generate a fresh 32-byte master_secret and exit")
    ap.add_argument("--master",
                    help="Master secret hex (else read from NMRS_MASTER env var "
                         "or prompted)")
    ap.add_argument("--as-config", action="store_true",
                    help="Print a ready-to-paste .nmrs_config.ini snippet")
    ap.add_argument("--save", action="store_true",
                    help="Append the facility name to the names file used by "
                         "the Decrypt-tab dropdown (deduplicated)")
    ap.add_argument("--facilities-file", default="facilities.txt",
                    help="Path to the facility-name list for --save "
                         "(default: facilities.txt in the current directory)")
    args = ap.parse_args()

    if args.generate_master:
        master_hex = secrets.token_hex(KEY_LEN)
        print(master_hex)
        sys.stderr.write(
            "\nGenerated a fresh master_secret (32 bytes / 64 hex chars).\n"
            "Store this offline. Paste into your manager .nmrs_config.ini:\n\n"
            "[manager]\n"
            f"master_secret = {master_hex}\n\n"
            "Anyone with this value can decrypt every facility's backups.\n"
        )
        return

    if not args.facility:
        sys.exit("Provide a facility name, or use --generate-master.")

    master = args.master or os.environ.get("NMRS_MASTER", "")
    if not master:
        master = getpass.getpass("Master secret (64 hex chars): ")
    try:
        key = derive_facility_key(master, args.facility)
    except ValueError as e:
        sys.exit(str(e))

    hex_key = key.hex()
    if args.as_config:
        sys.stderr.write(f"# Facility: {args.facility}\n")
        print("[backup]")
        print(f"backup_key = {hex_key}")
    else:
        print(hex_key)

    if args.save:
        _append_facility(args.facilities_file, args.facility)


def _append_facility(path_str: str, facility: str) -> None:
    """Append `facility` to the names file unless it's already present."""
    import os.path
    from pathlib import Path
    path = Path(path_str)
    existing = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                existing.append(s)
    if facility.strip() in existing:
        sys.stderr.write(f"(already in {path}: {facility})\n")
        return
    header_needed = not path.exists()
    with open(path, "a", encoding="utf-8") as f:
        if header_needed:
            f.write("# NMRS Toolkit facility names — one EXACT name per line.\n")
            f.write("# Must match global_property Facility_Name at each site.\n")
        f.write(facility.strip() + "\n")
    sys.stderr.write(f"Saved to {path}: {facility}\n")


if __name__ == "__main__":
    main()
