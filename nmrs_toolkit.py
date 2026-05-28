#!/usr/bin/env python3
"""NMRS Toolkit — linelist runner, encrypted CSV merge, and scheduled DB backup.

Single-binary GUI that ships with bundled SQL linelists, supports arbitrary .sql
input, encrypts/decrypts CSVs with a per-facility 32-byte key from .nmrs_config.ini, merges
multiple reports into one, and runs a daily encrypted mysqldump via the OS
scheduler (cron on Linux, schtasks on Windows).

Headless entry points (used by the OS scheduler):
  --backup              run one idempotent encrypted backup pass and exit.
  --generate-linelists  generate the weekly linelist batch (Treatment, PMTCT,
                        EAC, AHD) once per ISO week, gated to Thursday-or-later.
"""

import configparser
import csv
import gzip
import hashlib
import hmac
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from io import StringIO
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import mysql.connector as mysql_connector
from mysql.connector import Error

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# Every MySQL connection routes through db_connect() so we can pin the
# pure-Python protocol implementation. mysql-connector-python prefers its C
# extension (_mysql_connector) when installed, but PyInstaller freezes the
# extension WITHOUT the native authentication-plugin libraries it loads at
# runtime — so the frozen build fails with "Authentication plugin
# 'mysql_native_password' cannot be loaded: The specified module could not be
# found." The pure-Python client implements those auth plugins itself (using the
# bundled `cryptography` package for caching_sha2_password), so it works inside
# a single-file binary. We capture the original connect() up front so this
# wrapper isn't caught by the call-site rename.
_mysql_connect = mysql_connector.connect


def db_connect(**kwargs):
    """mysql.connector.connect() pinned to the pure-Python client (use_pure)."""
    kwargs.setdefault("use_pure", True)
    return _mysql_connect(**kwargs)


# A windowed (no-console) Windows build still pops a visible console window for
# every child process it spawns — mysqldump, the mysql restore client, schtasks.
# To a non-technical user that flash looks like an error, and closing it can
# interrupt a running backup. CREATE_NO_WINDOW runs them silently. The attribute
# exists only on Windows, so guard on the platform first; 0 is a no-op elsewhere.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0


APP_NAME = "NMRS Toolkit"
APP_VERSION = "1.1.1"

# Filesystem layout for backups.
BACKUP_DIR = Path(r"C:\NMRS_DB") if platform.system() == "Windows" else Path.home() / "NMRS_DB"
BACKUP_LOG_FILE = BACKUP_DIR / "backup.log"

# Filesystem layout for generated linelists (weekly batch + manual "Generate All").
LINELIST_DIR = (Path(r"C:\NMRS_Linelists") if platform.system() == "Windows"
                else Path.home() / "NMRS_Linelists")
LINELIST_LOG_FILE = LINELIST_DIR / "linelist.log"
# Records the ISO year-week of the most recent successful weekly batch so the
# @reboot catch-up trigger doesn't regenerate a set already produced this week.
LINELIST_WEEK_MARKER = LINELIST_DIR / ".last_weekly_run"

# Bump whenever the OS-scheduler definition changes (times, triggers, new jobs)
# so existing installs re-register on the next launch instead of being skipped
# by the "already installed" marker.
SCHEDULE_VERSION = 2

# Curated linelist registry — the single source of truth for what appears in the
# Linelists dropdown and what the weekly batch generates. Tuple fields:
#   (display_name, filename under scripts/, include_in_weekly_batch)
# Files present in scripts/ but absent here (e.g. FAST_RADET, Last-10) are still
# shipped in the binary but intentionally hidden from the UI. OTZ is offered for
# manual runs but excluded from the unattended weekly batch (in_batch=False).
LINELIST_REGISTRY = [
    ("Treatment", "TreatmentLinelistv3_2.sql", True),
    ("PMTCT", "PMTCT_ANC.sql", True),
    ("EAC", "EAC Script V2.5 07-08-2025_modified.sql", True),
    ("OTZ", "OTZLinelist.sql", False),
    ("AHD", "AHD_SCRIPT_24TH_OCTOBER_2025.sql", True),
]


# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------
# Current format (v2):
#   magic       : 8 bytes,  b"NMRS2\x00\x00\x00"
#   nonce       : 12 bytes
#   ciphertext  : AES-GCM with 16-byte tag appended
# Key: 32 random bytes ("backup_key" in config, derived from manager's
# master_secret via HMAC-SHA256). No password / KDF — the key is the secret.
#
# Legacy format (v1, NMRS1*) used PBKDF2+passphrase. Files in that format
# CANNOT be decrypted by this version; they are recognised only so we can
# show a clearer error.

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


# ---------------------------------------------------------------------------
# Resource path (for bundled scripts/ inside the PyInstaller binary)
# ---------------------------------------------------------------------------

def resource_path(rel: str) -> Path:
    """Return absolute path to a bundled resource, both in dev and PyInstaller."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / rel
    return Path(__file__).resolve().parent / rel


def bundled_scripts() -> list:
    """Return [(display_name, absolute_path), ...] for the curated linelists in
    LINELIST_REGISTRY that are actually present under scripts/. Order follows the
    registry. Files in scripts/ that aren't registered are intentionally omitted."""
    scripts_dir = resource_path("scripts")
    out = []
    for display, filename, _in_batch in LINELIST_REGISTRY:
        p = scripts_dir / filename
        if p.exists():
            out.append((display, p))
    return out


def batch_linelists() -> list:
    """Return [(display_name, absolute_path), ...] for the registry entries flagged
    for the weekly batch and present under scripts/ (Treatment, PMTCT, EAC, AHD —
    OTZ is excluded by its in_batch=False flag)."""
    scripts_dir = resource_path("scripts")
    out = []
    for display, filename, in_batch in LINELIST_REGISTRY:
        if not in_batch:
            continue
        p = scripts_dir / filename
        if p.exists():
            out.append((display, p))
    return out


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

CONFIG_FILENAME = ".nmrs_config.ini"
LEGACY_CONFIG_FILENAME = "nmrs_config.ini"


def secure_config_dir() -> Path:
    """Return the platform-specific hidden directory where the config lives
    after first launch. Per-platform conventions:

      Linux   : $XDG_CONFIG_HOME/nmrs_toolkit/   (default ~/.config/nmrs_toolkit/)
      macOS   : ~/Library/Application Support/NMRS_Toolkit/
      Windows : %APPDATA%\\NMRS_Toolkit\\
    """
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "NMRS_Toolkit"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "NMRS_Toolkit"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "nmrs_toolkit"
    return Path.home() / ".config" / "nmrs_toolkit"


def secure_config_path() -> Path:
    return secure_config_dir() / CONFIG_FILENAME


def config_paths() -> list:
    """Return candidate config paths in priority order (first existing wins).

    The platform-specific secure location is preferred; if a config is found
    anywhere else (next to the binary, in cwd, or in the legacy location),
    load_config() moves it into the secure path on first launch. The
    PyInstaller _MEIPASS directory is NOT searched — that contains only the
    example template, never a live config.
    """
    paths = [secure_config_path()]
    if getattr(sys, "frozen", False):
        paths.append(Path(sys.executable).resolve().parent / CONFIG_FILENAME)
        paths.append(Path(sys.executable).resolve().parent / LEGACY_CONFIG_FILENAME)
    paths.append(Path.cwd() / CONFIG_FILENAME)
    paths.append(Path.cwd() / LEGACY_CONFIG_FILENAME)
    # Back-compat: older installs may have used ~/.nmrs_toolkit/config.ini.
    paths.append(Path.home() / ".nmrs_toolkit" / "config.ini")
    # Source-tree fallback (development convenience only).
    paths.append(Path(__file__).resolve().parent / CONFIG_FILENAME)
    paths.append(Path(__file__).resolve().parent / LEGACY_CONFIG_FILENAME)
    return paths


# Set by load_config() so the GUI can show which file was used.
LOADED_CONFIG_PATH: Path = None  # type: ignore[assignment]


def _secure_config_file(path: Path) -> None:
    """Tighten permissions on the config file (POSIX only; no-op on Windows)."""
    if platform.system() == "Windows":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _migrate_to_secure(found: Path) -> Path:
    """If `found` is not already in the secure location, move it there.
    Returns the path where the config now lives (may be `found` if move fails)."""
    secure = secure_config_path()
    try:
        same = found.resolve() == secure.resolve()
    except OSError:
        same = False
    if same:
        return found
    try:
        secure.parent.mkdir(parents=True, exist_ok=True)
        # On POSIX, set the dir perms to 700 so only the owner can list it.
        if platform.system() != "Windows":
            try:
                os.chmod(secure.parent, 0o700)
            except OSError:
                pass
        if secure.exists():
            # Hidden file already in secure location — leave the second copy in
            # place (don't risk overwriting), just read the secure one.
            return secure
        shutil.move(str(found), str(secure))
        return secure
    except OSError:
        return found  # couldn't move — read in place


def load_config() -> configparser.ConfigParser:
    global LOADED_CONFIG_PATH
    cfg = configparser.ConfigParser()
    for p in config_paths():
        if not p.exists():
            continue
        # Legacy filename: rename "nmrs_config.ini" -> ".nmrs_config.ini" first,
        # then attempt the secure-location migration.
        if p.name == LEGACY_CONFIG_FILENAME:
            hidden = p.with_name(CONFIG_FILENAME)
            if not hidden.exists():
                try:
                    p.rename(hidden)
                    p = hidden
                except OSError:
                    pass
            else:
                p = hidden
        # Move into the platform-secure location if we're not already there.
        p = _migrate_to_secure(p)
        cfg.read(p)
        LOADED_CONFIG_PATH = p
        _secure_config_file(p)
        return cfg
    secure = secure_config_path()
    raise FileNotFoundError(
        f"{CONFIG_FILENAME} not found.\n"
        f"Expected location:\n"
        f"  {secure}\n"
        f"On first launch, you can drop a .nmrs_config.ini next to the binary "
        f"and the app will move it into the secure location automatically. "
        f"A template is bundled with the binary (.nmrs_config.example.ini) — "
        f"copy and customize it before relaunching."
    )


# ---------------------------------------------------------------------------
# Manager helpers (master secret + facility-name list for the Decrypt dropdown)
# ---------------------------------------------------------------------------

def get_master_secret(config: configparser.ConfigParser) -> str:
    """Return the manager's master_secret hex (empty string if not set)."""
    return config.get("manager", "master_secret", fallback="").strip()


def facilities_file_path(config: configparser.ConfigParser) -> Path:
    """Resolve the facility-name list path. Defaults to facilities.txt next to
    the loaded config; overridable via [manager] facilities_file (relative
    paths resolve against the config's directory)."""
    custom = config.get("manager", "facilities_file", fallback="").strip()
    base = LOADED_CONFIG_PATH.parent if LOADED_CONFIG_PATH else Path.home()
    if custom:
        p = Path(custom)
        return p if p.is_absolute() else base / custom
    return base / "facilities.txt"


def load_facility_names(config: configparser.ConfigParser) -> list:
    """Read the newline-delimited facility-name list. Blank lines and lines
    starting with '#' are ignored. Returns [] if the file is missing."""
    p = facilities_file_path(config)
    try:
        if not p.exists():
            return []
        names = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            names.append(line)
        return names
    except OSError:
        return []


# ---------------------------------------------------------------------------
# OS scheduler install (called once on first launch)
# ---------------------------------------------------------------------------

SCHEDULE_MARKER_FILE = Path.home() / ".nmrs_toolkit" / "schedule_installed"


def install_schedules(binary_path: Path, backup: bool = True,
                       linelist: bool = True) -> str:
    """Register the automated-job triggers with the host OS scheduler:

      Backup   : 00:00 Mon-Fri  + on system startup  -> --backup
      Linelist : 00:00 Thursday + on system startup  -> --generate-linelists

    Each job is idempotent (backup: at most once per day; linelist: once per
    ISO week, gated to Thursday-or-later), so the extra on-startup trigger that
    covers machines powered off at 00:00 never produces a duplicate run.

    `backup`/`linelist` select which job sets to install (driven by config).
    Returns a human-readable description of what was installed.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    LINELIST_DIR.mkdir(parents=True, exist_ok=True)
    system = platform.system()
    binary_path = Path(binary_path).resolve()

    if system == "Windows":
        # Remove every NMRS task first so re-install is clean (covers renamed
        # times and the legacy 14:00 backup task).
        for tn in ("NMRSBackup", "NMRSBackup_Boot", "NMRSLinelist", "NMRSLinelist_Boot"):
            subprocess.run(["schtasks", "/delete", "/f", "/tn", tn],
                           capture_output=True, creationflags=_NO_WINDOW)
        summary = []
        if backup:
            subprocess.run([
                "schtasks", "/create", "/f", "/sc", "weekly",
                "/d", "MON,TUE,WED,THU,FRI", "/st", "00:00",
                "/tn", "NMRSBackup", "/tr", f'"{binary_path}" --backup',
            ], check=True, capture_output=True, creationflags=_NO_WINDOW)
            subprocess.run([
                "schtasks", "/create", "/f", "/sc", "onstart",
                "/tn", "NMRSBackup_Boot", "/tr", f'"{binary_path}" --backup',
            ], check=True, capture_output=True, creationflags=_NO_WINDOW)
            summary.append("Backup 'NMRSBackup' (00:00 Mon-Fri) + 'NMRSBackup_Boot' (on startup)")
        if linelist:
            subprocess.run([
                "schtasks", "/create", "/f", "/sc", "weekly",
                "/d", "THU", "/st", "00:00",
                "/tn", "NMRSLinelist", "/tr", f'"{binary_path}" --generate-linelists',
            ], check=True, capture_output=True, creationflags=_NO_WINDOW)
            subprocess.run([
                "schtasks", "/create", "/f", "/sc", "onstart",
                "/tn", "NMRSLinelist_Boot", "/tr", f'"{binary_path}" --generate-linelists',
            ], check=True, capture_output=True, creationflags=_NO_WINDOW)
            summary.append("Linelist 'NMRSLinelist' (00:00 Thu) + 'NMRSLinelist_Boot' (on startup)")
        return ("Windows scheduled tasks installed -> " + "; ".join(summary)) if summary \
            else "Nothing installed (both job sets disabled in config)"

    # Linux: rebuild the # NMRS_TOOLKIT cron block in one pass (drops every stale
    # NMRS line — including the legacy 0 14 backup — then re-adds the fresh set).
    cron_lines = []
    if backup:
        cron_lines += [
            f"@reboot {binary_path} --backup >> {BACKUP_LOG_FILE} 2>&1 # NMRS_TOOLKIT",
            f"0 0 * * 1-5 {binary_path} --backup >> {BACKUP_LOG_FILE} 2>&1 # NMRS_TOOLKIT",
        ]
    if linelist:
        cron_lines += [
            f"@reboot {binary_path} --generate-linelists >> {LINELIST_LOG_FILE} 2>&1 # NMRS_TOOLKIT",
            f"0 0 * * 4 {binary_path} --generate-linelists >> {LINELIST_LOG_FILE} 2>&1 # NMRS_TOOLKIT",
        ]
    existing = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True
    )
    lines = []
    if existing.returncode == 0:
        for line in existing.stdout.splitlines():
            if "# NMRS_TOOLKIT" in line:
                continue  # drop stale entries, we'll re-add the fresh set
            lines.append(line)
    lines.extend(cron_lines)
    new_crontab = "\n".join(lines) + "\n"
    p = subprocess.run(
        ["crontab", "-"], input=new_crontab, text=True, capture_output=True
    )
    if p.returncode != 0:
        raise RuntimeError(f"crontab install failed: {p.stderr.strip() or p.stdout.strip()}")
    return ("Linux cron entries installed:\n  " + "\n  ".join(cron_lines)) if cron_lines \
        else "Nothing installed (both job sets disabled in config)"


def schedule_status() -> str:
    """Return a short description of which automated schedules are installed."""
    system = platform.system()
    if system == "Windows":
        def _has(tn):
            return subprocess.run(
                ["schtasks", "/query", "/tn", tn],
                capture_output=True, text=True, creationflags=_NO_WINDOW,
            ).returncode == 0
        backup_ok = _has("NMRSBackup") and _has("NMRSBackup_Boot")
        linelist_ok = _has("NMRSLinelist") and _has("NMRSLinelist_Boot")
        if backup_ok and linelist_ok:
            return "Installed (backup 00:00 Mon-Fri + linelists 00:00 Thu, both + on startup)"
        if not backup_ok and not linelist_ok:
            return "Not installed"
        present = ("backup" if backup_ok else "") + (" linelists" if linelist_ok else "")
        return f"Partially installed ({present.strip()}) — re-run Update Schedules"
    p = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if p.returncode != 0 or "# NMRS_TOOLKIT" not in p.stdout:
        return "Not installed"
    nmrs_lines = [ln for ln in p.stdout.splitlines() if "# NMRS_TOOLKIT" in ln]
    has_backup = any("--backup" in ln for ln in nmrs_lines)
    has_linelist = any("--generate-linelists" in ln for ln in nmrs_lines)
    if has_backup and has_linelist:
        return "Installed (cron: backup 00:00 Mon-Fri + linelists 00:00 Thu, both + on startup)"
    if has_backup or has_linelist:
        present = ("backup" if has_backup else "") + (" linelists" if has_linelist else "")
        return f"Partially installed ({present.strip()}) — re-run Update Schedules"
    return f"Installed (legacy entries: {len(nmrs_lines)}) — re-run Update Schedules"


# ---------------------------------------------------------------------------
# Headless backup pass (used by --backup and by the Backup Now button)
# ---------------------------------------------------------------------------

_FACILITY_SLUG_MAX = 100
_FACILITY_SAFE_RE = re.compile(r"[^A-Za-z0-9]+")


def _sanitize_facility(name: str) -> str:
    """Make a facility name filename-safe: alphanumerics + underscores, capped."""
    slug = _FACILITY_SAFE_RE.sub("_", (name or "").strip()).strip("_")
    if not slug:
        return "unknown_facility"
    return slug[:_FACILITY_SLUG_MAX]


def _facility_slug(config: configparser.ConfigParser, log_func=print) -> str:
    """Resolve the facility slug used in backup filenames.

    Priority:
      1. [settings] facility_name in config (manual override).
      2. SELECT property_value FROM global_property WHERE property = 'Facility_Name'.
      3. Fall back to "unknown_facility" (logged as a warning).
    """
    override = config.get("settings", "facility_name", fallback="").strip()
    if override:
        return _sanitize_facility(override)

    db = config["database"]
    try:
        conn = db_connect(
            host=db["host"], user=db["user"], password=db["password"],
            database=db["database"], port=int(db.get("port", 3306)),
            connection_timeout=8,
        )
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT property_value FROM global_property "
                "WHERE property = 'Facility_Name' LIMIT 1"
            )
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception as e:
        log_func(f"[BACKUP] facility lookup failed ({e}); using 'unknown_facility'")
        return "unknown_facility"

    if not row or not row[0]:
        log_func("[BACKUP] Facility_Name global_property empty; using 'unknown_facility'")
        return "unknown_facility"
    return _sanitize_facility(str(row[0]))


def _wait_for_mysql(db_cfg, log_func=print, max_wait_s: int = 60) -> None:
    """Try to connect to MySQL, retrying every 5s until reachable or timeout.

    Used by automated runs (@reboot) where MySQL may not be up yet. Raises
    RuntimeError if MySQL never becomes reachable within max_wait_s.
    """
    deadline = time.monotonic() + max_wait_s
    last_err = None
    while time.monotonic() < deadline:
        try:
            conn = db_connect(
                host=db_cfg["host"], user=db_cfg["user"], password=db_cfg["password"],
                database=db_cfg["database"], port=int(db_cfg.get("port", 3306)),
                connection_timeout=5,
            )
            conn.close()
            return
        except Exception as e:
            last_err = e
            log_func(f"[BACKUP] waiting for MySQL... ({e})")
            time.sleep(5)
    raise RuntimeError(f"MySQL not reachable after {max_wait_s}s: {last_err}")


def _todays_backup_exists(facility: str):
    """If a backup file for today already exists in BACKUP_DIR, return its path,
    else None. Returns: Optional[Path]."""
    if not BACKUP_DIR.exists():
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    # Match any facility's backup for today (covers cases where the facility
    # slug changed between runs but a backup already exists).
    for p in BACKUP_DIR.glob(f"*nmrs_backup_{today}_*.sql.gz.enc"):
        return p
    return None


def perform_backup(config: configparser.ConfigParser, log_func=print,
                   force: bool = False, wait_for_mysql: bool = False) -> Path:
    """Run mysqldump (or python fallback), gzip + encrypt, write to BACKUP_DIR.

    Returns the output path. Raises on failure.

    Behavior:
      - If force=False and a backup file for today already exists in
        BACKUP_DIR, returns that existing path without running a new dump.
      - If wait_for_mysql=True (used by automated @reboot runs), polls MySQL
        for up to 60s before attempting the dump.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    db = config["database"]

    if not force:
        existing = _todays_backup_exists("")
        if existing is not None:
            log_func(f"[BACKUP] skip — today already backed up: {existing.name}")
            return existing

    if wait_for_mysql:
        _wait_for_mysql(db, log_func)

    facility = _facility_slug(config, log_func)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    out_path = BACKUP_DIR / f"{facility}_nmrs_backup_{stamp}.sql.gz.enc"

    log_func(f"[BACKUP] start db={db['database']} host={db['host']} facility={facility} -> {out_path.name}")

    dump_bytes = _dump_database(db, log_func)
    log_func(f"[BACKUP] dump complete ({len(dump_bytes):,} bytes uncompressed)")

    gzipped = gzip.compress(dump_bytes, compresslevel=6)
    log_func(f"[BACKUP] gzipped to {len(gzipped):,} bytes")

    key = get_facility_key(config)
    encrypted = encrypt_bytes(gzipped, key)
    log_func(f"[BACKUP] encrypted to {len(encrypted):,} bytes")

    with open(out_path, "wb") as f:
        f.write(encrypted)
    log_func(f"[BACKUP] wrote {out_path}")

    retention = config.getint("backup", "retention_count", fallback=10)
    _prune_backups(BACKUP_DIR, "*nmrs_backup_*.sql.gz.enc",
                   keep=retention, log_func=log_func, label="BACKUP")
    return out_path


PRE_RESTORE_DIR = BACKUP_DIR / "pre_restore"
RESTORE_LOG_FILE = BACKUP_DIR / "restore.log"


def append_restore_log(line: str) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with open(RESTORE_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {line}\n")


def _prune_backups(folder: Path, pattern: str, keep: int,
                   log_func=print, label: str = "BACKUP") -> None:
    """Keep the `keep` most recent files matching `pattern` in `folder`,
    delete the rest. Stateless — safe against manual deletions."""
    if keep < 1:
        return
    try:
        candidates = sorted(folder.glob(pattern),
                            key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as e:
        log_func(f"[{label}] retention scan failed: {e}")
        return
    for p in candidates[keep:]:
        try:
            p.unlink()
            log_func(f"[{label}] pruned old backup: {p.name}")
        except OSError as e:
            log_func(f"[{label}] could not prune {p.name}: {e}")


# ---------------------------------------------------------------------------
# Restore helpers (consumed by the Restore tab + worker thread)
# ---------------------------------------------------------------------------

def _classify_dump_file(path: Path) -> str:
    """Identify the dump format by extension + magic bytes.

    Returns one of: 'enc' (.sql.gz.enc), 'gz' (.sql.gz), 'zip' (.sql.zip),
    'sql' (plain), or raises ValueError if magic doesn't match extension.
    """
    name = path.name.lower()
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError as e:
        raise ValueError(f"Cannot read file: {e}")

    if name.endswith(".sql.gz.enc"):
        if not head.startswith(CRYPTO_MAGIC):
            raise ValueError("File has .sql.gz.enc extension but missing NMRS magic header")
        return "enc"
    if name.endswith(".sql.gz"):
        if not head.startswith(b"\x1f\x8b"):
            raise ValueError("File has .sql.gz extension but is not gzip-compressed")
        return "gz"
    if name.endswith(".sql.zip") or name.endswith(".zip"):
        if not head.startswith(b"PK"):
            raise ValueError("File has .zip extension but is not a zip archive")
        return "zip"
    if name.endswith(".sql"):
        return "sql"
    # Fall back to magic-byte sniffing if extension is unusual.
    if head.startswith(CRYPTO_MAGIC):
        return "enc"
    if head.startswith(b"\x1f\x8b"):
        return "gz"
    if head.startswith(b"PK"):
        return "zip"
    return "sql"


def _decrypt_to_sql_file(src: Path, key_bytes, dest: Path, log_func=print) -> int:
    """Decrypt/gunzip/unzip `src` to plain SQL bytes at `dest` (0600). Returns
    the number of bytes written. `key_bytes` is the 32-byte key for .enc
    files (None/empty means the caller hasn't provided one — an error is
    raised if the file actually needs decryption)."""
    kind = _classify_dump_file(src)
    log_func(f"[RESTORE] source format: {kind}")

    if kind == "enc":
        if not key_bytes:
            raise ValueError(
                "Encrypted .sql.gz.enc file selected but no backup key was "
                "provided. Enter a 64-char hex key in the Backup key field, "
                "or set [backup] backup_key in .nmrs_config.ini."
            )
        blob = src.read_bytes()
        decrypted = decrypt_bytes(blob, key_bytes)
        sql_bytes = gzip.decompress(decrypted)
    elif kind == "gz":
        sql_bytes = gzip.decompress(src.read_bytes())
    elif kind == "zip":
        import zipfile
        with zipfile.ZipFile(src) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".sql")]
            if not members:
                raise ValueError("Zip archive contains no .sql file")
            sql_bytes = zf.read(members[0])
    else:
        sql_bytes = src.read_bytes()

    # Write with 0600 on POSIX; on Windows we accept default ACLs.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if platform.system() != "Windows":
        fd = os.open(str(dest), flags, 0o600)
    else:
        fd = os.open(str(dest), flags)
    with os.fdopen(fd, "wb") as f:
        f.write(sql_bytes)
    return len(sql_bytes)


def _sql_sanity_check(sql_path: Path) -> None:
    """Raise if the file isn't a plausible MySQL dump."""
    if sql_path.stat().st_size == 0:
        raise ValueError("Decrypted SQL file is empty")
    with open(sql_path, "rb") as f:
        head = f.read(64 * 1024).decode("utf-8", errors="replace").upper()
    if "CREATE TABLE" not in head and "INSERT INTO" not in head:
        raise ValueError("File does not look like a MySQL dump "
                         "(no CREATE TABLE or INSERT INTO in first 64 KB)")


def _shred_unlink(path: Path) -> None:
    """Best-effort overwrite + delete. Not crypto-grade on COW/journaled FS."""
    try:
        size = path.stat().st_size
        with open(path, "r+b") as f:
            chunk = b"\x00" * min(1 << 16, size or 1)
            written = 0
            while written < size:
                f.write(chunk[: min(len(chunk), size - written)])
                written += len(chunk)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    try:
        path.unlink()
    except OSError:
        pass


def _mysql_db_exists(db_cfg, db_name: str) -> bool:
    conn = db_connect(
        host=db_cfg["host"], user=db_cfg["user"], password=db_cfg["password"],
        port=int(db_cfg.get("port", 3306)), connection_timeout=8,
    )
    try:
        cur = conn.cursor()
        cur.execute("SHOW DATABASES LIKE %s", (db_name,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def _mysql_admin(db_cfg, statements):
    """Run admin statements with no database selected. `statements` is iterable."""
    conn = db_connect(
        host=db_cfg["host"], user=db_cfg["user"], password=db_cfg["password"],
        port=int(db_cfg.get("port", 3306)), connection_timeout=8,
    )
    try:
        cur = conn.cursor()
        for s in statements:
            cur.execute(s)
        conn.commit()
    finally:
        conn.close()


def perform_pre_restore_backup(config: configparser.ConfigParser,
                               log_func=print) -> Path:
    """Snapshot the live DB into PRE_RESTORE_DIR before a destructive restore.

    Distinguished from regular backups by the 'PRE-RESTORE' tag in the
    filename and a separate retention policy. Verified by round-trip
    decrypt before returning — raises if the file can't be decrypted.
    """
    PRE_RESTORE_DIR.mkdir(parents=True, exist_ok=True)
    db = config["database"]
    facility = _facility_slug(config, log_func)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_path = PRE_RESTORE_DIR / f"{facility}_PRE-RESTORE_{stamp}.sql.gz.enc"

    log_func(f"[RESTORE] pre-restore backup -> {out_path.name}")
    dump_bytes = _dump_database(db, log_func)
    gzipped = gzip.compress(dump_bytes, compresslevel=6)
    key = get_facility_key(config)
    encrypted = encrypt_bytes(gzipped, key)
    with open(out_path, "wb") as f:
        f.write(encrypted)

    # Round-trip verify so we never drop the live DB without a readable snapshot.
    verify = decrypt_bytes(out_path.read_bytes(), key)
    if gzip.decompress(verify) != dump_bytes:
        out_path.unlink(missing_ok=True)
        raise RuntimeError("pre-restore verification failed; aborting")
    log_func(f"[RESTORE] pre-restore verified OK ({out_path.stat().st_size:,} bytes)")

    retention = config.getint("backup", "pre_restore_retention", fallback=5)
    _prune_backups(PRE_RESTORE_DIR, "*_PRE-RESTORE_*.sql.gz.enc",
                   keep=retention, log_func=log_func, label="RESTORE")
    return out_path


def _dump_database(db_cfg, log_func) -> bytes:
    """Return SQL dump as bytes. Prefer mysqldump, fall back to python."""
    mysqldump = shutil.which("mysqldump")
    if mysqldump:
        cmd = [
            mysqldump,
            f"-h{db_cfg['host']}",
            f"-P{db_cfg.get('port', '3306')}",
            f"-u{db_cfg['user']}",
            f"-p{db_cfg['password']}",
            "--single-transaction",
            "--quick",
            "--skip-lock-tables",
            "--routines",
            "--triggers",
            db_cfg["database"],
        ]
        p = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
        if p.returncode != 0:
            raise RuntimeError(f"mysqldump failed: {p.stderr.decode('utf-8', 'replace')[:500]}")
        return p.stdout

    log_func("[BACKUP] mysqldump not on PATH; using python fallback (slower)")
    return _python_dump(db_cfg)


def _python_dump(db_cfg) -> bytes:
    """Naive Python-side dump: schema + data, INSERT statements, no triggers/routines."""
    conn = db_connect(
        host=db_cfg["host"],
        user=db_cfg["user"],
        password=db_cfg["password"],
        database=db_cfg["database"],
        port=int(db_cfg.get("port", 3306)),
    )
    out = []
    try:
        cursor = conn.cursor()
        cursor.execute("SHOW TABLES")
        tables = [r[0] for r in cursor.fetchall()]
        out.append(f"-- NMRS Toolkit python fallback dump @ {datetime.now().isoformat()}\n")
        out.append(f"-- database: {db_cfg['database']}\n")
        out.append("SET FOREIGN_KEY_CHECKS=0;\n")
        for t in tables:
            cursor.execute(f"SHOW CREATE TABLE `{t}`")
            ddl = cursor.fetchone()[1]
            out.append(f"\nDROP TABLE IF EXISTS `{t}`;\n{ddl};\n")
            cursor.execute(f"SELECT * FROM `{t}`")
            cols = [d[0] for d in cursor.description]
            for row in cursor:
                vals = []
                for v in row:
                    if v is None:
                        vals.append("NULL")
                    elif isinstance(v, (int, float)):
                        vals.append(str(v))
                    elif isinstance(v, (bytes, bytearray)):
                        vals.append("0x" + v.hex())
                    else:
                        s = str(v).replace("\\", "\\\\").replace("'", "\\'")
                        vals.append("'" + s + "'")
                col_list = ",".join(f"`{c}`" for c in cols)
                out.append(f"INSERT INTO `{t}` ({col_list}) VALUES ({','.join(vals)});\n")
        out.append("SET FOREIGN_KEY_CHECKS=1;\n")
    finally:
        conn.close()
    return "".join(out).encode("utf-8")


def append_backup_log(line: str) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with open(BACKUP_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {line}\n")


def run_headless_backup() -> int:
    """Entry point for `--backup` invocation. Returns shell exit code."""
    try:
        cfg = load_config()
    except Exception as e:
        sys.stderr.write(f"config load failed: {e}\n")
        return 2
    try:
        out = perform_backup(cfg, log_func=append_backup_log,
                             force=False, wait_for_mysql=True)
        append_backup_log(f"[BACKUP] OK {out}")
        return 0
    except Exception as e:
        append_backup_log(f"[BACKUP] FAIL {e}")
        sys.stderr.write(f"backup failed: {e}\n")
        return 1


# ---------------------------------------------------------------------------
# SQL script execution (shared by the GUI Linelist tab and headless batch runs)
# ---------------------------------------------------------------------------

_DELIMITER_RE = re.compile(r"^\s*DELIMITER\s+(\S+)\s*$", re.IGNORECASE)


def _has_delimiter_directive(sql) -> bool:
    for line in sql.splitlines():
        if _DELIMITER_RE.match(line):
            return True
    return False


def _iter_result_sets(cursor, sql):
    """Run a (possibly multi-statement) `sql` and yield each result set as an
    object exposing .with_rows / .description / .fetchall().

    Bridges a mysql-connector-python API change so the same code works whether
    the build machine has 8.x or 9.x installed:
      * 8.x: cursor.execute(sql, multi=True) returns a lazy iterator of
        per-statement result cursors.
      * 9.x: the `multi` argument was removed (passing it raises TypeError).
        A single execute() runs every statement — MULTI_STATEMENTS is on by
        default — and later result sets are reached via nextset().
    """
    try:
        multi_iter = cursor.execute(sql, multi=True)
    except TypeError:
        cursor.execute(sql)
        while True:
            yield cursor
            if not cursor.nextset():
                break
    else:
        yield from multi_iter


def _split_with_delimiters(sql):
    """Split a SQL script into individual statements.

    Honors `DELIMITER xxx` directives and ignores any delimiter that falls
    inside a string literal, a backtick-quoted identifier, or a comment.
    The scan is character-by-character rather than line-by-line because:
      * statements may share a line (e.g. "SET @a=0;SET @b=0;"), so
        splitting only at end-of-line ';' would merge them into one blob
        that execute() then runs as a hidden multi-statement — breaking
        result handling ("Commands out of sync" on the C client), and
      * a routine body keeps its inner ';' only while a custom delimiter
        (e.g. $$) is active.
    Returns a flat list of statements (delimiter stripped, blanks dropped).
    """
    statements = []
    delimiter = ";"
    buf = []
    has_sql = False  # does buf hold anything beyond comments/whitespace?
    i, n = 0, len(sql)
    at_line_start = True  # only here is a DELIMITER directive recognized

    def flush():
        # Skip comment-only / blank chunks — executing one raises 1065
        # "Query was empty" (the file's trailing comments are the usual case).
        nonlocal has_sql
        if has_sql:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
        buf.clear()
        has_sql = False

    while i < n:
        ch = sql[i]

        # DELIMITER directive — at the start of a line, outside any quoting.
        # Consumes the rest of the line and switches the active delimiter.
        if (at_line_start and sql[i:i + 9].upper() == "DELIMITER"
                and (i + 9 >= n or sql[i + 9] in " \t")):
            flush()
            eol = sql.find("\n", i)
            eol = n if eol == -1 else eol
            new_delim = sql[i + 9:eol].strip()
            if new_delim:
                delimiter = new_delim
            i = eol + 1
            at_line_start = True
            continue

        # line comment: "-- " (dash-dash + whitespace/EOL) or "#" — to EOL.
        if ch == "#" or (sql[i:i + 2] == "--"
                         and (i + 2 >= n or sql[i + 2] in " \t\r\n")):
            eol = sql.find("\n", i)
            eol = n if eol == -1 else eol
            buf.append(sql[i:eol])
            i = eol
            at_line_start = False
            continue

        # block comment /* ... */ — kept verbatim. A /*! ... */ conditional
        # comment is executable SQL (MySQL runs it), so it counts as content.
        if sql[i:i + 2] == "/*":
            end = sql.find("*/", i + 2)
            end = n if end == -1 else end + 2
            buf.append(sql[i:end])
            if sql[i:i + 3] == "/*!":
                has_sql = True
            i = end
            at_line_start = False
            continue

        # string literal ('..','"..') or backtick-quoted identifier (`..`).
        if ch in ("'", '"', "`"):
            buf.append(ch)
            i += 1
            while i < n:
                c = sql[i]
                buf.append(c)
                if c == "\\" and ch != "`" and i + 1 < n:  # backslash escape
                    buf.append(sql[i + 1])
                    i += 2
                    continue
                if c == ch:
                    if i + 1 < n and sql[i + 1] == ch:  # doubled => escaped
                        buf.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            at_line_start = False
            has_sql = True  # a string/identifier literal is real SQL
            continue

        # active delimiter -> statement boundary.
        if sql[i:i + len(delimiter)] == delimiter:
            flush()
            i += len(delimiter)
            at_line_start = False
            continue

        buf.append(ch)
        if ch == "\n":
            at_line_start = True
        elif ch not in " \t\r":
            at_line_start = False  # whitespace keeps us "at line start"
            has_sql = True
        i += 1

    flush()
    return statements


def execute_sql_script(db_cfg, sql):
    """Open a fresh DB connection, run `sql` (multi-statement), and return
    (columns, rows, statement_count) for the LAST result set that produced rows.

    `db_cfg` is the [database] config section (or any mapping with host/user/
    password/database/port). See the two execution paths in _iter_result_sets /
    _split_with_delimiters for why DELIMITER scripts are handled separately.
    """
    conn = db_connect(
        host=db_cfg["host"], user=db_cfg["user"], password=db_cfg["password"],
        database=db_cfg["database"], port=int(db_cfg.get("port", 3306)),
        autocommit=True,
    )
    cols, rows, stmt_count = [], [], 0
    try:
        cursor = conn.cursor()
        try:
            if _has_delimiter_directive(sql):
                for stmt in _split_with_delimiters(sql):
                    stmt_count += 1
                    cursor.execute(stmt)
                    if cursor.with_rows:
                        cols = [d[0] for d in cursor.description]
                        rows = cursor.fetchall()
            else:
                for result in _iter_result_sets(cursor, sql):
                    stmt_count += 1
                    if result.with_rows:
                        cols = [d[0] for d in result.description]
                        rows = result.fetchall()
        finally:
            cursor.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return cols, rows, stmt_count


def linelist_rows_to_csv_bytes(cols, rows) -> bytes:
    """Serialize a result set to UTF-8 CSV bytes (header row + data rows)."""
    buf = StringIO()
    w = csv.writer(buf)
    if cols:
        w.writerow(cols)
    for r in rows:
        w.writerow(["" if v is None else str(v) for v in r])
    return buf.getvalue().encode("utf-8")


def write_linelist_csv(cols, rows, target: Path, encrypt: bool, key) -> int:
    """Write a result set to `target` as plain CSV, or AES-GCM encrypted bytes
    when encrypt=True. Returns the uncompressed payload size in bytes."""
    payload = linelist_rows_to_csv_bytes(cols, rows)
    target.parent.mkdir(parents=True, exist_ok=True)
    if encrypt:
        target.write_bytes(encrypt_bytes(payload, key))
    else:
        target.write_bytes(payload)
    return len(payload)


# ---------------------------------------------------------------------------
# Headless weekly linelist batch (used by --generate-linelists and the
# "Generate All Weekly Linelists" button)
# ---------------------------------------------------------------------------

def append_linelist_log(line: str) -> None:
    LINELIST_DIR.mkdir(parents=True, exist_ok=True)
    with open(LINELIST_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {line}\n")


def _linelist_output_path(display_name: str, encrypt: bool) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d")
    ext = ".csv.nmrs" if encrypt else ".csv"
    return LINELIST_DIR / f"{display_name}_Linelist_{stamp}{ext}"


def perform_linelist_batch(config: configparser.ConfigParser, log_func=print,
                           encrypt=None, wait_for_mysql: bool = False) -> dict:
    """Generate every weekly-batch linelist (Treatment, PMTCT, EAC, AHD) into
    LINELIST_DIR. Always (re)generates — week-level idempotency is the caller's
    job (see run_headless_linelists). Each script runs independently so one
    failure doesn't abort the rest.

    encrypt: True/False forces the mode; None reads [linelist] encrypt (default
    false). When encrypting, output uses the facility backup key (.csv.nmrs).

    Returns {"written": [Path, ...], "failed": [(display, error), ...]}.
    """
    LINELIST_DIR.mkdir(parents=True, exist_ok=True)
    if encrypt is None:
        encrypt = config.getboolean("linelist", "encrypt", fallback=False)
    db = config["database"]
    if wait_for_mysql:
        _wait_for_mysql(db, log_func)
    key = get_facility_key(config) if encrypt else None

    written, failed, skipped = [], [], []
    items = batch_linelists()
    if not items:
        log_func("[LINELIST] no batch linelists present under scripts/ — nothing to do")
        return {"written": [], "failed": [], "skipped": []}

    log_func(f"[LINELIST] batch start: {len(items)} linelist(s), encrypt={encrypt} -> {LINELIST_DIR}")
    for display, path in items:
        t0 = time.monotonic()
        try:
            sql = path.read_text(encoding="utf-8")
            cols, rows, stmt_count = execute_sql_script(db, sql)
            elapsed = time.monotonic() - t0
            # Don't create an empty/headers-only file for a script that returned
            # zero result rows. Counted as "skipped", not "failed" — the script
            # ran fine, there was just nothing to write.
            if not rows:
                log_func(f"[LINELIST] {display}: 0 row(s) in {elapsed:.1f}s — no file written")
                skipped.append(display)
                continue
            out = _linelist_output_path(display, encrypt)
            size = write_linelist_csv(cols, rows, out, encrypt, key)
            log_func(f"[LINELIST] {display}: {len(rows)} row(s), {size:,} bytes in "
                     f"{elapsed:.1f}s -> {out.name}")
            written.append(out)
        except Exception as e:
            elapsed = time.monotonic() - t0
            log_func(f"[LINELIST] {display} FAILED after {elapsed:.1f}s: {e}")
            failed.append((display, str(e)))
    log_func(f"[LINELIST] batch done: {len(written)} written, "
             f"{len(skipped)} skipped (0 rows), {len(failed)} failed")
    return {"written": written, "failed": failed, "skipped": skipped}


def _current_week_id(dt: datetime = None) -> str:
    """ISO year-week identifier, e.g. '2026-W22'."""
    dt = dt or datetime.now()
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _read_linelist_week_marker() -> str:
    try:
        return LINELIST_WEEK_MARKER.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_linelist_week_marker(week_id: str) -> None:
    try:
        LINELIST_DIR.mkdir(parents=True, exist_ok=True)
        LINELIST_WEEK_MARKER.write_text(week_id, encoding="utf-8")
    except OSError:
        pass


def run_headless_linelists() -> int:
    """Entry point for `--generate-linelists` invocation. Returns shell exit code.

    Cadence: the deliverable is weekly, due Thursday 00:00. Two triggers call
    this — a Thursday-00:00 schedule and an @reboot catch-up:
      * If this ISO week's set is already generated, do nothing (idempotent).
      * Before Thursday, the @reboot trigger waits — the set isn't due yet.
      * From Thursday onward (incl. a machine that was off on Thursday and
        first boots Friday/Saturday), generate the set and stamp the week.
    """
    try:
        cfg = load_config()
    except Exception as e:
        sys.stderr.write(f"config load failed: {e}\n")
        return 2

    now = datetime.now()
    week_id = _current_week_id(now)
    iso_weekday = now.isocalendar()[2]  # Mon=1 .. Sun=7; Thursday=4

    if _read_linelist_week_marker() == week_id:
        append_linelist_log(f"[LINELIST] {week_id} already generated; skipping")
        return 0
    if iso_weekday < 4:
        append_linelist_log(f"[LINELIST] before Thursday ({week_id}, weekday={iso_weekday}); "
                            f"not yet due — skipping")
        return 0

    try:
        result = perform_linelist_batch(cfg, log_func=append_linelist_log,
                                        encrypt=None, wait_for_mysql=True)
    except Exception as e:
        append_linelist_log(f"[LINELIST] batch aborted: {e}")
        sys.stderr.write(f"linelist batch failed: {e}\n")
        return 1

    # Stamp the week only if at least one linelist was produced, so a totally
    # failed run (e.g. DB never came up) retries on the next trigger.
    if result["written"]:
        _write_linelist_week_marker(week_id)
    return 0 if not result["failed"] else 1


# ---------------------------------------------------------------------------
# GUI application
# ---------------------------------------------------------------------------

class Tooltip:
    """Lightweight hover tooltip for any tk widget."""

    def __init__(self, widget, text: str, delay_ms: int = 400):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id = None
        self._tip = None
        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress>", self._hide)

    def _schedule(self, _evt=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self):
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self._tip, text=self.text, justify="left",
            bg="#263238", fg="#eceff1", font=("Arial", 9),
            padx=8, pady=4, wraplength=320,
        ).pack()

    def _hide(self, _evt=None):
        self._cancel()
        if self._tip:
            self._tip.destroy()
            self._tip = None


class NMRSToolkitApp:
    """Top-level Tk application."""

    # DB banner palette (matches the v6.x app for visual consistency).
    _DB_PROFILE_COLORS = {
        "PROD":       ("#c62828", "white"),
        "PRODUCTION": ("#c62828", "white"),
        "STAGING":    ("#ef6c00", "white"),
        "STAGE":      ("#ef6c00", "white"),
        "UAT":        ("#ef6c00", "white"),
        "TEST":       ("#2e7d32", "white"),
        "DEV":        ("#2e7d32", "white"),
        "LOCAL":      ("#455a64", "white"),
    }

    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("980x900")
        self.root.resizable(False, False)

        self.config = None
        self.connection = None
        self.authenticated = False

        try:
            self.config = load_config()
        except Exception as e:
            messagebox.showerror("Config Error", str(e))
            self.root.after(50, self.root.destroy)
            return

        # Login gate is optional: shown only if an admin_password is configured.
        if self._admin_password():
            self.show_login_screen()
        else:
            self._enter_app()

    # -- login -----------------------------------------------------------

    def show_login_screen(self):
        for w in self.root.winfo_children():
            w.destroy()

        frame = tk.Frame(self.root, bg="#f0f0f0")
        frame.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(frame, text=APP_NAME, font=("Arial", 18, "bold"),
                 bg="#f0f0f0", fg="#1a237e").pack(pady=(0, 8))
        tk.Label(frame, text=f"v{APP_VERSION}", font=("Arial", 10),
                 bg="#f0f0f0", fg="#666").pack(pady=(0, 24))
        tk.Label(frame, text="Administrator Password:",
                 font=("Arial", 11), bg="#f0f0f0").pack(pady=(0, 4))

        self.password_entry = tk.Entry(frame, width=30, font=("Arial", 12),
                                       show="*", bd=2, relief="solid")
        self.password_entry.pack(pady=(0, 16))
        self.password_entry.focus()
        self.password_entry.bind("<Return>", lambda e: self._check_password())

        tk.Button(frame, text="LOGIN", command=self._check_password,
                  bg="#1976d2", fg="white", font=("Arial", 11, "bold"),
                  padx=24, pady=6, cursor="hand2").pack()

    def _admin_password(self) -> str:
        """Launch-gate password from [settings] admin_password. Empty = no gate."""
        return self.config.get("settings", "admin_password", fallback="").strip()

    def _enter_app(self):
        self.authenticated = True
        self.show_main_screen()
        self._maybe_install_schedule_on_first_launch()

    def _check_password(self):
        if self.password_entry.get() == self._admin_password():
            self._enter_app()
        else:
            messagebox.showerror("Access Denied", "Incorrect password.")
            self.password_entry.delete(0, tk.END)

    # -- main UI ---------------------------------------------------------

    def show_main_screen(self):
        for w in self.root.winfo_children():
            w.destroy()

        # Title header
        header = tk.Frame(self.root, bg="#1a237e", height=70)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text=APP_NAME, font=("Arial", 18, "bold"),
                 bg="#1a237e", fg="white").pack(pady=(12, 0))
        tk.Label(header, text=f"v{APP_VERSION}  ·  Catholic Caritas Foundation of Nigeria",
                 font=("Arial", 10), bg="#1a237e", fg="white").pack()

        # DB banner
        self._build_db_banner(self.root)

        # Notebook
        nb_frame = tk.Frame(self.root, padx=24, pady=10)
        nb_frame.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(nb_frame)
        self.notebook.pack(fill="both", expand=True)

        self._safe_add_tab("Linelists", self._build_linelist_tab)
        self._safe_add_tab("Merge Reports", self._build_merge_tab)
        self._safe_add_tab("Backup", self._build_backup_tab)
        self._safe_add_tab("Restore", self._build_restore_tab)
        if self.config.getboolean("ui", "decrypt_tab_enabled", fallback=False):
            self._safe_add_tab("Decrypt", self._build_decrypt_tab)

        # Shared activity log
        log_frame = tk.LabelFrame(self.root, text="Activity Log",
                                  font=("Arial", 10, "bold"), padx=10, pady=8)
        log_frame.pack(fill="both", expand=False, padx=24, pady=(0, 12))
        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=8, font=("Courier", 9),
            bg="#1e1e1e", fg="#dcdcdc", insertbackground="white",
        )
        self.log_text.pack(fill="both", expand=True)

        self.log(f"{APP_NAME} v{APP_VERSION} ready.")
        db = self.config["database"]
        self.log(f"Config loaded from: {LOADED_CONFIG_PATH}")
        self.log(f"DB: {db['database']} @ {db['host']}:{db.get('port', '3306')} "
                 f"as {db.get('user', '?')}  "
                 f"(profile: {db.get('profile_label', '').strip() or 'UNLABELED'})")
        self.log("-" * 70)

    def _safe_add_tab(self, label, builder):
        """Add a notebook tab; if its builder raises, replace its content with a
        visible error message instead of silently aborting the whole main screen.
        """
        frame = tk.Frame(self.notebook)
        self.notebook.add(frame, text=f"  {label}  ")
        try:
            builder(frame)
        except Exception as e:
            tb = traceback.format_exc()
            for child in frame.winfo_children():
                child.destroy()
            tk.Label(
                frame,
                text=f"Tab '{label}' failed to render:\n\n{e}",
                fg="#b71c1c", font=("Arial", 10, "bold"),
                justify="left", anchor="nw", wraplength=860,
            ).pack(fill="x", padx=20, pady=10)
            detail = scrolledtext.ScrolledText(
                frame, height=20, font=("Courier", 9),
                bg="#fff3e0", fg="#3e2723",
            )
            detail.pack(fill="both", expand=True, padx=20, pady=(0, 12))
            detail.insert("1.0", tb)
            detail.config(state="disabled")
            # Best-effort log; log_text may not exist yet at this point.
            if hasattr(self, "log_text"):
                self.log(f"[UI] tab '{label}' failed: {e}")
            else:
                sys.stderr.write(f"tab '{label}' failed: {e}\n{tb}\n")

    def _build_db_banner(self, parent):
        db = self.config["database"]
        profile = (db.get("profile_label", "") or "").strip().upper()
        bg, fg = self._DB_PROFILE_COLORS.get(profile, ("#eceff1", "#37474f"))

        banner = tk.Frame(parent, bg=bg, height=30)
        banner.pack(fill="x")
        banner.pack_propagate(False)

        chip = profile if profile else "UNLABELED"
        tk.Label(banner, text=f"  {chip}  ", bg=bg, fg=fg,
                 font=("Arial", 10, "bold")).pack(side="left", padx=(12, 8), pady=4)
        tk.Label(
            banner,
            text=(f"DB: {db.get('database', '?')}  @ "
                  f"{db.get('host', '?')}:{db.get('port', '3306')}  "
                  f"user: {db.get('user', '?')}"),
            bg=bg, fg=fg, font=("Arial", 10),
        ).pack(side="left", pady=4)

    # -- connection ------------------------------------------------------

    def get_connection(self):
        try:
            if self.connection and self.connection.is_connected():
                return self.connection
            db = self.config["database"]
            self.connection = db_connect(
                host=db["host"], user=db["user"], password=db["password"],
                database=db["database"], port=int(db.get("port", 3306)),
            )
            return self.connection
        except Error as e:
            db = self.config["database"]
            messagebox.showerror(
                "Database Error",
                f"Connection failed:\n\n{e}\n\n"
                f"Using credentials from:\n  {LOADED_CONFIG_PATH}\n"
                f"  user={db.get('user', '?')}  "
                f"host={db.get('host', '?')}:{db.get('port', '3306')}  "
                f"database={db.get('database', '?')}",
            )
            return None

    # -- logging ---------------------------------------------------------

    def log(self, message: str):
        try:
            self.log_text.config(state="normal")
            self.log_text.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {message}\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        except tk.TclError:
            print(message)

    # -- tabs (stubs; filled in by per-feature tasks) --------------------

    def _build_linelist_tab(self, parent):
        # Filled by tasks/linelist section below.
        self._linelist_setup(parent)

    def _build_merge_tab(self, parent):
        # Filled by tasks/merge section below.
        self._merge_setup(parent)

    def _build_decrypt_tab(self, parent):
        self._decrypt_setup(parent)

    def _build_backup_tab(self, parent):
        # Filled by tasks/backup section below.
        self._backup_setup(parent)

    def _build_restore_tab(self, parent):
        self._restore_setup(parent)

    # -- first-launch schedule install -----------------------------------

    def _maybe_install_schedule_on_first_launch(self):
        def _on(section, key):
            return (self.config.get(section, key, fallback="true").strip().lower()
                    in ("true", "yes", "1"))
        want_backup = _on("backup", "enabled")
        want_linelist = _on("linelist", "auto_enabled")
        if not want_backup and not want_linelist:
            self.log("[SCHED] backup.enabled and linelist.auto_enabled both off; "
                     "skipping schedule install")
            return
        # Re-install when the schedule definition changed (version bump) even if a
        # marker from an older build exists — otherwise upgrades keep the old times.
        if SCHEDULE_MARKER_FILE.exists():
            try:
                marker = SCHEDULE_MARKER_FILE.read_text(encoding="utf-8")
            except OSError:
                marker = ""
            if f"schedule_version={SCHEDULE_VERSION}" in marker:
                self.log(f"[SCHED] schedules already current ({schedule_status()})")
                return
            self.log("[SCHED] schedule definition changed since last install; re-registering")
        try:
            binary = Path(sys.executable if getattr(sys, "frozen", False)
                          else __file__).resolve()
            msg = install_schedules(binary, backup=want_backup, linelist=want_linelist)
            SCHEDULE_MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
            SCHEDULE_MARKER_FILE.write_text(
                f"installed_at={datetime.now().isoformat()}\n"
                f"schedule_version={SCHEDULE_VERSION}\n{msg}\n"
            )
            self.log(f"[SCHED] scheduled: {msg}")
        except Exception as e:
            self.log(f"[SCHED] schedule install failed: {e}")

    # ====================================================================
    # Linelist Runner
    # ====================================================================

    def _linelist_setup(self, parent):
        content = tk.Frame(parent, padx=20, pady=15)
        content.pack(fill="both", expand=True)

        top = tk.LabelFrame(content, text="Step 1: Pick a script",
                            font=("Arial", 11, "bold"), padx=15, pady=10)
        top.pack(fill="x")

        tk.Label(top, text="Bundled:", font=("Arial", 10)).grid(row=0, column=0, sticky="e", padx=(0, 4))
        self.linelist_bundled_var = tk.StringVar()
        self._bundled = bundled_scripts()
        bundled_names = ["(custom .sql file)"] + [n for n, _ in self._bundled]
        self.linelist_bundled = ttk.Combobox(
            top, textvariable=self.linelist_bundled_var,
            values=bundled_names, state="readonly", width=36, font=("Arial", 10),
        )
        self.linelist_bundled.current(1 if self._bundled else 0)
        self.linelist_bundled.grid(row=0, column=1, columnspan=3, sticky="w", padx=(0, 12))
        self.linelist_bundled.bind("<<ComboboxSelected>>", lambda e: self._linelist_on_source_change())

        tk.Label(top, text="Custom file:", font=("Arial", 10)).grid(row=1, column=0, sticky="e", padx=(0, 4), pady=(6, 0))
        self.linelist_custom_path = tk.Entry(top, font=("Arial", 10), width=50, state="readonly")
        self.linelist_custom_path.grid(row=1, column=1, columnspan=2, sticky="we", padx=(0, 8), pady=(6, 0))
        tk.Button(top, text="Browse...", command=self._linelist_browse,
                  bg="#607d8b", fg="white", font=("Arial", 9, "bold"),
                  padx=8, pady=2, cursor="hand2").grid(row=1, column=3, sticky="w", pady=(6, 0))

        out = tk.LabelFrame(content, text="Step 2: Output",
                            font=("Arial", 11, "bold"), padx=15, pady=10)
        out.pack(fill="x", pady=(10, 0))

        # IMPORTANT: encrypt_var must exist before _linelist_refresh_default_name()
        # is called, because that helper reads its value to decide on extension.
        self.linelist_encrypt_var = tk.BooleanVar(value=False)

        tk.Label(out, text="Filename:", font=("Arial", 10)).grid(row=0, column=0, sticky="e", padx=(0, 4))
        self.linelist_outname = tk.Entry(out, font=("Arial", 10), width=46)
        self.linelist_outname.grid(row=0, column=1, sticky="we", padx=(0, 12))

        tk.Checkbutton(out, text="Encrypt output (.csv.nmrs)",
                       variable=self.linelist_encrypt_var, font=("Arial", 10),
                       command=self._linelist_refresh_default_name
                       ).grid(row=0, column=2, sticky="w")

        self._linelist_refresh_default_name()

        # Run + log
        run_frame = tk.Frame(content)
        run_frame.pack(fill="x", pady=(12, 4))
        self.linelist_run_button = tk.Button(
            run_frame, text="RUN LINELIST",
            command=self.run_linelist,
            bg="#1976d2", fg="white", font=("Arial", 11, "bold"),
            padx=20, pady=6, cursor="hand2",
        )
        self.linelist_run_button.pack(side="left")
        # One-click weekly batch: Treatment, PMTCT, EAC, AHD (OTZ excluded), saved
        # to LINELIST_DIR with auto names. Mirrors the unattended Thursday run.
        self.linelist_batch_button = tk.Button(
            run_frame, text="GENERATE ALL WEEKLY (4)",
            command=self.run_linelist_batch,
            bg="#2e7d32", fg="white", font=("Arial", 11, "bold"),
            padx=14, pady=6, cursor="hand2",
        )
        self.linelist_batch_button.pack(side="left", padx=(10, 0))
        Tooltip(
            self.linelist_batch_button,
            "Generates the four weekly linelists (Treatment, PMTCT, EAC, AHD) in one "
            f"pass, saved to {LINELIST_DIR}. OTZ is excluded from the batch. Honors the "
            "'Encrypt output' checkbox above. These also generate automatically at "
            "00:00 every Thursday (or on the next startup if the machine was off).",
        )
        tk.Button(run_frame, text="Open Folder", command=self.open_linelist_folder,
                  bg="#9e9e9e", fg="white", font=("Arial", 9, "bold"),
                  padx=10, pady=4, cursor="hand2").pack(side="left", padx=(10, 0))
        # Indeterminate progressbar that animates while a script is running.
        # We can't show real progress because the SQL is opaque from our side,
        # but the bouncing bar makes it obvious the app didn't freeze.
        self.linelist_progress = ttk.Progressbar(run_frame, mode="indeterminate", length=180)
        self.linelist_progress.pack(side="left", padx=(12, 8))
        self.linelist_status = tk.Label(
            run_frame, text="Idle.", font=("Arial", 9, "bold"), fg="#555",
        )
        self.linelist_status.pack(side="left", padx=(0, 0))

        log_frame = tk.LabelFrame(content, text="Run log",
                                  font=("Arial", 10, "bold"), padx=8, pady=6)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.linelist_log = scrolledtext.ScrolledText(
            log_frame, height=12, font=("Courier", 9),
            bg="#fafafa", fg="#212121", wrap="none",
        )
        self.linelist_log.pack(fill="both", expand=True)

    def _linelist_on_source_change(self):
        # If the user picked a bundled script from the dropdown, wipe the
        # stale custom-file path so the UI no longer suggests the old browsed
        # file is still in play. Index 0 is "(custom .sql file)" — leave the
        # path alone there so the user can keep working with what they browsed.
        # (Note: this fires only for real <<ComboboxSelected>> events; the
        # programmatic .current(0) call inside _linelist_browse does not
        # trigger it, so a fresh browse keeps its newly-set path.)
        if self.linelist_bundled.current() != 0:
            self.linelist_custom_path.config(state="normal")
            self.linelist_custom_path.delete(0, "end")
            self.linelist_custom_path.config(state="readonly")
        self._linelist_refresh_default_name()

    def _linelist_browse(self):
        path = filedialog.askopenfilename(
            title="Pick a .sql script",
            filetypes=[("SQL", "*.sql"), ("All files", "*.*")],
        )
        if not path:
            return
        self.linelist_custom_path.config(state="normal")
        self.linelist_custom_path.delete(0, "end")
        self.linelist_custom_path.insert(0, path)
        self.linelist_custom_path.config(state="readonly")
        self.linelist_bundled.current(0)  # switch to "(custom .sql file)"
        self._linelist_refresh_default_name()

    def _linelist_active_script(self):
        """Return (display_name, Path) for whichever source is currently selected."""
        if self.linelist_bundled.current() == 0:
            p = self.linelist_custom_path.get().strip()
            if not p:
                return None
            return (Path(p).stem, Path(p))
        idx = self.linelist_bundled.current() - 1
        if idx < 0 or idx >= len(self._bundled):
            return None
        return self._bundled[idx]

    def _linelist_refresh_default_name(self):
        active = self._linelist_active_script()
        stamp = datetime.now().strftime("%Y-%m-%d")
        stem = active[0] if active else "linelist"
        ext = ".csv.nmrs" if self.linelist_encrypt_var.get() else ".csv"
        new_default = f"{stem}_{stamp}{ext}"
        self.linelist_outname.delete(0, "end")
        self.linelist_outname.insert(0, new_default)

    def _ll_log(self, msg: str):
        self.linelist_log.insert("end", msg + "\n")
        self.linelist_log.see("end")
        self.log(f"[LINELIST] {msg}")

    def run_linelist(self):
        """UI-side handler: validate inputs, then hand off to a worker thread.

        The SQL is run on a background thread so the Tk event loop stays responsive
        (otherwise the indeterminate progressbar can't animate and the window
        appears frozen during multi-minute reports).
        """
        active = self._linelist_active_script()
        if not active:
            messagebox.showwarning("Pick a script", "Select a bundled script or browse for a .sql file.")
            return
        name, path = active
        try:
            sql = path.read_text(encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Read Failed", str(e))
            return

        out_name = self.linelist_outname.get().strip()
        if not out_name:
            messagebox.showwarning("Filename Required", "Set an output filename.")
            return

        target = filedialog.asksaveasfilename(
            initialfile=out_name,
            defaultextension=".csv.nmrs" if self.linelist_encrypt_var.get() else ".csv",
            filetypes=[("CSV (encrypted)", "*.csv.nmrs"), ("CSV", "*.csv"), ("All files", "*.*")],
        )
        if not target:
            return
        target = Path(target)
        encrypt = self.linelist_encrypt_var.get()

        # Lock UI, start progress.
        self.linelist_log.delete("1.0", "end")
        self._ll_log(f"Running '{name}' from {path}")
        self._ll_log(f"Target: {target}  encrypt={encrypt}")
        self.linelist_run_button.config(state="disabled", bg="#9e9e9e")
        self.linelist_status.config(text="Running...", fg="#1976d2")
        self.linelist_progress.start(12)

        threading.Thread(
            target=self._linelist_worker,
            args=(name, sql, target, encrypt),
            daemon=True,
        ).start()

    def _linelist_worker(self, name, sql, target, encrypt):
        """Background thread: opens its own connection, runs the script,
        writes the output, and posts the result back to the UI thread.
        """
        t0 = time.monotonic()
        try:
            cols, rows, stmt_count = self._execute_script(sql)
            elapsed = time.monotonic() - t0
            self._post_linelist_log(
                f"Executed {stmt_count} statement(s) in {elapsed:.1f}s; "
                f"final result set: {len(rows)} row(s), {len(cols)} column(s)"
            )
        except Exception as e:
            elapsed = time.monotonic() - t0
            tb = traceback.format_exc()
            self._post_linelist_log(f"SQL error after {elapsed:.1f}s: {e}")
            self.root.after(0, self._linelist_finish, False, 0, target, str(e), tb)
            return

        # Skip the file write entirely when the script produced no result rows.
        # Two cases both land here:
        #   1) DDL-only scripts (CREATE FUNCTION/ALTER/DROP/...) that never run a
        #      SELECT — cols and rows are both empty.
        #   2) A SELECT that legitimately matched zero records — cols populated,
        #      rows empty.
        # In both cases an empty/headers-only file is misleading clutter, so we
        # just log and report "no file written" instead of touching the disk.
        if not rows:
            self._post_linelist_log("Result set is empty — no output file written.")
            self.root.after(0, self._linelist_finish, True, 0, target, None, None)
            return

        # Build CSV in memory and write to disk.
        try:
            size = write_linelist_csv(
                cols, rows, target, encrypt, get_facility_key(self.config) if encrypt else None)
            self._post_linelist_log(f"Wrote {size:,} bytes -> {target}")
        except Exception as e:
            self.root.after(0, self._linelist_finish, False, 0, target, f"Write failed: {e}", None)
            return

        self.root.after(0, self._linelist_finish, True, len(rows), target, None, None)

    def _execute_script(self, sql):
        """Run `sql` against the configured DB; return (columns, rows, stmt_count)
        for the last result set that produced rows. Thin wrapper over the shared
        module-level execute_sql_script()."""
        return execute_sql_script(self.config["database"], sql)

    def _post_linelist_log(self, msg):
        """Marshal a log line from a worker thread back onto the UI thread."""
        self.root.after(0, self._ll_log, msg)

    def _linelist_finish(self, success, n_rows, target, error, tb):
        """UI-thread completion callback. Stops progress, re-enables RUN, notifies."""
        self.linelist_progress.stop()
        self.linelist_run_button.config(state="normal", bg="#1976d2")
        if success:
            if n_rows == 0:
                # Worker skipped the write — see the "Result set is empty" branch
                # in _linelist_worker. Use amber, not green, to signal the run
                # succeeded but produced nothing useful.
                self.linelist_status.config(text="Done — 0 row(s); no file written.",
                                            fg="#ef6c00")
                messagebox.showinfo(
                    "No Output",
                    "The script ran successfully but produced no result rows.\n"
                    "No output file was written.\n\n"
                    "(DDL-only scripts — CREATE FUNCTION, ALTER, DROP, etc. — "
                    "and queries that match no records both end up here.)",
                )
                return
            self.linelist_status.config(
                text=f"Done — {n_rows} row(s) -> {Path(target).name}", fg="#2e7d32",
            )
            messagebox.showinfo(
                "Done",
                f"Wrote {n_rows} row(s) to:\n{target}\n\n"
                + ("(encrypted with facility key)" if self.linelist_encrypt_var.get() else "(plain CSV)"),
            )
        else:
            self.linelist_status.config(text=f"Failed.", fg="#b71c1c")
            if tb:
                self._ll_log("--- traceback ---\n" + tb)
            messagebox.showerror("Run Failed", error)

    # -- weekly batch ("Generate All") -----------------------------------

    def run_linelist_batch(self):
        """Generate the four weekly linelists (Treatment, PMTCT, EAC, AHD) in one
        pass into LINELIST_DIR. Encryption follows the same 'Encrypt output'
        checkbox as a single run. Work happens on a worker thread."""
        items = batch_linelists()
        if not items:
            messagebox.showwarning(
                "Nothing to generate",
                "No weekly batch linelists are bundled (Treatment, PMTCT, EAC, AHD).")
            return
        encrypt = self.linelist_encrypt_var.get()
        names = ", ".join(d for d, _ in items)
        if not messagebox.askyesno(
                "Generate All Weekly Linelists",
                f"Generate {len(items)} linelist(s) — {names} — into:\n{LINELIST_DIR}\n\n"
                + ("Output will be ENCRYPTED (.csv.nmrs)." if encrypt
                   else "Output will be plain CSV (.csv).")):
            return

        self.linelist_log.delete("1.0", "end")
        self._ll_log(f"Weekly batch: {len(items)} linelist(s) -> {LINELIST_DIR}  encrypt={encrypt}")
        self.linelist_run_button.config(state="disabled", bg="#9e9e9e")
        self.linelist_batch_button.config(state="disabled", bg="#9e9e9e")
        self.linelist_status.config(text="Generating batch...", fg="#1976d2")
        self.linelist_progress.start(12)
        threading.Thread(target=self._linelist_batch_worker, args=(encrypt,), daemon=True).start()

    def _linelist_batch_worker(self, encrypt):
        t0 = time.monotonic()
        try:
            result = perform_linelist_batch(
                self.config, log_func=self._post_linelist_log, encrypt=encrypt)
        except Exception as e:
            tb = traceback.format_exc()
            self._post_linelist_log(f"Batch aborted: {e}")
            self.root.after(0, self._linelist_batch_finish, {"written": [], "failed": [("batch", str(e))]}, tb)
            return
        elapsed = time.monotonic() - t0
        self._post_linelist_log(f"Batch finished in {elapsed:.1f}s")
        self.root.after(0, self._linelist_batch_finish, result, None)

    def _linelist_batch_finish(self, result, tb):
        self.linelist_progress.stop()
        self.linelist_run_button.config(state="normal", bg="#1976d2")
        self.linelist_batch_button.config(state="normal", bg="#2e7d32")
        written = result["written"]
        failed = result["failed"]
        skipped = result.get("skipped", [])  # back-compat for the early-abort dict
        skipped_note = (f"\n\nSkipped (0 rows, no file written): {len(skipped)}\n"
                        + "\n".join(f"• {name}" for name in skipped)) if skipped else ""
        if failed and not written:
            self.linelist_status.config(text="Batch failed.", fg="#b71c1c")
            if tb:
                self._ll_log("--- traceback ---\n" + tb)
            messagebox.showerror(
                "Batch Failed",
                "No linelists were generated.\n\n"
                + "\n".join(f"• {name}: {err}" for name, err in failed)
                + skipped_note)
            return
        if failed:
            self.linelist_status.config(
                text=f"Batch: {len(written)} ok, {len(skipped)} skipped, "
                     f"{len(failed)} failed.", fg="#ef6c00")
            messagebox.showwarning(
                "Batch Completed With Errors",
                f"Wrote {len(written)} linelist(s) to:\n{LINELIST_DIR}\n\n"
                f"Failed ({len(failed)}):\n"
                + "\n".join(f"• {name}: {err}" for name, err in failed)
                + skipped_note)
        else:
            tail = f" ({len(skipped)} skipped — 0 rows)" if skipped else ""
            self.linelist_status.config(
                text=f"Batch done — {len(written)} file(s){tail}.", fg="#2e7d32")
            messagebox.showinfo(
                "Batch Complete",
                f"Generated {len(written)} linelist(s) into:\n{LINELIST_DIR}"
                + skipped_note)

    def open_linelist_folder(self):
        LINELIST_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if platform.system() == "Windows":
                os.startfile(str(LINELIST_DIR))  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(LINELIST_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(LINELIST_DIR)])
        except Exception as e:
            messagebox.showerror("Open Failed", str(e))

    # ====================================================================
    # Merge Reports
    # ====================================================================

    def _merge_setup(self, parent):
        content = tk.Frame(parent, padx=20, pady=15)
        content.pack(fill="both", expand=True)

        files_frame = tk.LabelFrame(content, text="Step 1: Input files (in merge order)",
                                    font=("Arial", 11, "bold"), padx=15, pady=10)
        files_frame.pack(fill="both", expand=True)

        list_holder = tk.Frame(files_frame)
        list_holder.pack(fill="both", expand=True)

        self.merge_listbox = tk.Listbox(list_holder, font=("Courier", 9), height=10,
                                        selectmode="extended")
        yscroll = ttk.Scrollbar(list_holder, orient="vertical", command=self.merge_listbox.yview)
        self.merge_listbox.configure(yscrollcommand=yscroll.set)
        self.merge_listbox.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")

        btn_row = tk.Frame(files_frame)
        btn_row.pack(fill="x", pady=(8, 0))
        tk.Button(btn_row, text="Add files...", command=self._merge_add,
                  bg="#607d8b", fg="white", font=("Arial", 9, "bold"),
                  padx=10, pady=2).pack(side="left")
        tk.Button(btn_row, text="Remove", command=self._merge_remove,
                  bg="#9e9e9e", fg="white", font=("Arial", 9, "bold"),
                  padx=10, pady=2).pack(side="left", padx=(8, 0))
        tk.Button(btn_row, text="Move Up", command=lambda: self._merge_move(-1),
                  font=("Arial", 9, "bold"), padx=10, pady=2).pack(side="left", padx=(8, 0))
        tk.Button(btn_row, text="Move Down", command=lambda: self._merge_move(+1),
                  font=("Arial", 9, "bold"), padx=10, pady=2).pack(side="left", padx=(8, 0))

        opts = tk.LabelFrame(content, text="Step 2: Sort & output",
                             font=("Arial", 11, "bold"), padx=15, pady=10)
        opts.pack(fill="x", pady=(10, 0))

        tk.Label(opts, text="Sort by column (blank = preserve order):",
                 font=("Arial", 10)).grid(row=0, column=0, sticky="e", padx=(0, 4))
        self.merge_sort_col = tk.Entry(opts, font=("Arial", 10), width=24)
        self.merge_sort_col.grid(row=0, column=1, sticky="w", padx=(0, 12))

        self.merge_sort_desc_var = tk.BooleanVar(value=False)
        tk.Checkbutton(opts, text="Descending", variable=self.merge_sort_desc_var,
                       font=("Arial", 10)).grid(row=0, column=2, sticky="w")

        tk.Label(opts, text="Output filename:", font=("Arial", 10)
                 ).grid(row=1, column=0, sticky="e", padx=(0, 4), pady=(6, 0))
        self.merge_outname = tk.Entry(opts, font=("Arial", 10), width=46)
        self.merge_outname.grid(row=1, column=1, sticky="we", padx=(0, 12), pady=(6, 0))
        self.merge_outname.insert(0, f"merged_{datetime.now().strftime('%Y-%m-%d')}.csv")

        self.merge_encrypt_var = tk.BooleanVar(value=False)
        tk.Checkbutton(opts, text="Encrypt output (.csv.nmrs)",
                       variable=self.merge_encrypt_var, font=("Arial", 10),
                       command=self._merge_refresh_default_name
                       ).grid(row=1, column=2, sticky="w", pady=(6, 0))

        run_frame = tk.Frame(content)
        run_frame.pack(fill="x", pady=(12, 4))
        self.merge_run_button = tk.Button(
            run_frame, text="MERGE", command=self.run_merge,
            bg="#1976d2", fg="white", font=("Arial", 11, "bold"),
            padx=20, pady=6, cursor="hand2",
        )
        self.merge_run_button.pack(side="left")
        self.merge_progress = ttk.Progressbar(run_frame, mode="indeterminate", length=180)
        self.merge_progress.pack(side="left", padx=(12, 8))
        self.merge_status = tk.Label(
            run_frame, text="Idle.", font=("Arial", 9, "bold"), fg="#555",
        )
        self.merge_status.pack(side="left")

        log_frame = tk.LabelFrame(content, text="Merge log",
                                  font=("Arial", 10, "bold"), padx=8, pady=6)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.merge_log = scrolledtext.ScrolledText(
            log_frame, height=8, font=("Courier", 9),
            bg="#fafafa", fg="#212121", wrap="none",
        )
        self.merge_log.pack(fill="both", expand=True)

    def _merge_refresh_default_name(self):
        cur = self.merge_outname.get().strip()
        # Only auto-adjust extension if the user hasn't customised the stem.
        stamp = datetime.now().strftime("%Y-%m-%d")
        default_stem = f"merged_{stamp}"
        if not cur or cur.startswith(default_stem):
            self.merge_outname.delete(0, "end")
            ext = ".csv.nmrs" if self.merge_encrypt_var.get() else ".csv"
            self.merge_outname.insert(0, default_stem + ext)

    def _merge_add(self):
        paths = filedialog.askopenfilenames(
            title="Pick CSV files (plain or .csv.nmrs)",
            filetypes=[("CSV / encrypted", "*.csv *.nmrs"), ("All files", "*.*")],
        )
        for p in paths:
            self.merge_listbox.insert("end", p)

    def _merge_remove(self):
        for idx in reversed(self.merge_listbox.curselection()):
            self.merge_listbox.delete(idx)

    def _merge_move(self, direction: int):
        sel = list(self.merge_listbox.curselection())
        if not sel:
            return
        if direction < 0 and sel[0] == 0:
            return
        if direction > 0 and sel[-1] == self.merge_listbox.size() - 1:
            return
        order = sel if direction < 0 else list(reversed(sel))
        for idx in order:
            text = self.merge_listbox.get(idx)
            self.merge_listbox.delete(idx)
            self.merge_listbox.insert(idx + direction, text)
            self.merge_listbox.selection_set(idx + direction)

    def _mg_log(self, msg: str):
        self.merge_log.insert("end", msg + "\n")
        self.merge_log.see("end")
        self.log(f"[MERGE] {msg}")

    def run_merge(self):
        """UI-side handler: validate, then hand off to a worker thread.

        Large CSV inputs (or encrypted ones requiring AES-GCM decryption per
        file) can block the UI for several seconds. The worker keeps the
        progressbar animating and the window responsive.
        """
        files = [self.merge_listbox.get(i) for i in range(self.merge_listbox.size())]
        if not files:
            messagebox.showwarning("No Files", "Add at least one CSV.")
            return
        out_name = self.merge_outname.get().strip()
        if not out_name:
            messagebox.showwarning("Filename Required", "Set an output filename.")
            return

        target = filedialog.asksaveasfilename(
            initialfile=out_name,
            defaultextension=".csv.nmrs" if self.merge_encrypt_var.get() else ".csv",
            filetypes=[("CSV (encrypted)", "*.csv.nmrs"), ("CSV", "*.csv"), ("All files", "*.*")],
        )
        if not target:
            return
        target = Path(target)
        encrypt = self.merge_encrypt_var.get()
        sort_col = self.merge_sort_col.get().strip()
        sort_desc = self.merge_sort_desc_var.get()

        self.merge_log.delete("1.0", "end")
        self._mg_log(f"Merging {len(files)} file(s) -> {target}  encrypt={encrypt}")
        self.merge_run_button.config(state="disabled", bg="#9e9e9e")
        self.merge_status.config(text="Running...", fg="#1976d2")
        self.merge_progress.start(12)

        threading.Thread(
            target=self._merge_worker,
            args=(files, target, encrypt, sort_col, sort_desc),
            daemon=True,
        ).start()

    def _merge_worker(self, files, target, encrypt, sort_col, sort_desc):
        """Background thread: read/decrypt every input, merge, sort, write."""
        t0 = time.monotonic()
        all_headers = []
        all_rows = []

        try:
            for path_str in files:
                path = Path(path_str)
                self._post_merge_log(f"Reading {path.name}")
                raw = path.read_bytes()
                if is_encrypted_file(path) or path.suffix.lower() == ".nmrs":
                    raw = decrypt_bytes(raw, get_facility_key(self.config))
                    self._post_merge_log(f"  decrypted ({len(raw):,} bytes)")
                text = raw.decode("utf-8", errors="replace")
                reader = csv.DictReader(text.splitlines())
                file_headers = reader.fieldnames or []
                for h in file_headers:
                    if h not in all_headers:
                        all_headers.append(h)
                n_before = len(all_rows)
                for row in reader:
                    all_rows.append(row)
                self._post_merge_log(
                    f"  +{len(all_rows) - n_before} row(s); columns: {file_headers}"
                )
        except Exception as e:
            self.root.after(0, self._merge_finish, False, 0, target, str(e), traceback.format_exc())
            return

        if sort_col:
            if sort_col not in all_headers:
                self._post_merge_log(
                    f"Sort column '{sort_col}' not in merged headers; skipping sort"
                )
            else:
                all_rows.sort(
                    key=lambda r: (r.get(sort_col) or ""),
                    reverse=sort_desc,
                )
                self._post_merge_log(
                    f"Sorted by '{sort_col}' ({'desc' if sort_desc else 'asc'})"
                )

        try:
            buf = StringIO()
            w = csv.DictWriter(buf, fieldnames=all_headers)
            w.writeheader()
            for r in all_rows:
                w.writerow({h: r.get(h, "") for h in all_headers})
            payload = buf.getvalue().encode("utf-8")
            if encrypt:
                target.write_bytes(encrypt_bytes(payload, get_facility_key(self.config)))
            else:
                target.write_bytes(payload)
            elapsed = time.monotonic() - t0
            self._post_merge_log(
                f"Wrote {len(all_rows)} row(s), {len(all_headers)} column(s) "
                f"in {elapsed:.1f}s -> {target}"
            )
        except Exception as e:
            self.root.after(0, self._merge_finish, False, len(all_rows), target,
                            f"Write failed: {e}", traceback.format_exc())
            return

        self.root.after(0, self._merge_finish, True, len(all_rows), target, None, None)

    def _post_merge_log(self, msg):
        self.root.after(0, self._mg_log, msg)

    def _merge_finish(self, success, n_rows, target, error, tb):
        self.merge_progress.stop()
        self.merge_run_button.config(state="normal", bg="#1976d2")
        if success:
            self.merge_status.config(
                text=f"Done — {n_rows} row(s) -> {Path(target).name}", fg="#2e7d32",
            )
            messagebox.showinfo(
                "Done",
                f"Merged -> {target}\n\nRows: {n_rows}\n"
                + ("(encrypted with facility key)" if self.merge_encrypt_var.get() else "(plain CSV)"),
            )
        else:
            self.merge_status.config(text="Failed.", fg="#b71c1c")
            if tb:
                self._mg_log("--- traceback ---\n" + tb)
            messagebox.showerror("Merge Failed", error)

    # ====================================================================
    # Decrypt tab — open / preview / export .csv.nmrs files
    # ====================================================================

    DECRYPT_PREVIEW_ROWS = 200  # cap for the in-tab preview Treeview

    def _decrypt_setup(self, parent):
        content = tk.Frame(parent, padx=20, pady=15)
        content.pack(fill="both", expand=True)

        pick_frame = tk.LabelFrame(
            content, text="Step 1: Pick an encrypted file (.csv.nmrs or .sql.gz.enc)",
            font=("Arial", 11, "bold"), padx=15, pady=10,
        )
        pick_frame.pack(fill="x")

        self.decrypt_path = tk.Entry(pick_frame, font=("Arial", 10), width=70, state="readonly")
        self.decrypt_path.grid(row=0, column=0, sticky="we", padx=(0, 8))
        tk.Button(
            pick_frame, text="Browse...", command=self._decrypt_browse,
            bg="#607d8b", fg="white", font=("Arial", 9, "bold"),
            padx=10, pady=2, cursor="hand2",
        ).grid(row=0, column=1, sticky="w")
        pick_frame.columnconfigure(0, weight=1)

        # Optional key override — for a manager decrypting another facility's
        # file. Blank falls back to this machine's [backup] backup_key.
        tk.Label(pick_frame, text="Backup key (hex):", font=("Arial", 9, "bold")
                 ).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.decrypt_key_var = tk.StringVar(value="")
        key_entry = tk.Entry(pick_frame, textvariable=self.decrypt_key_var,
                             font=("Courier", 10), show="*")
        key_entry.grid(row=2, column=0, sticky="we", padx=(0, 8))
        self.decrypt_key_show = tk.BooleanVar(value=False)
        tk.Checkbutton(
            pick_frame, text="show", variable=self.decrypt_key_show,
            font=("Arial", 8),
            command=lambda: key_entry.config(
                show="" if self.decrypt_key_show.get() else "*"),
        ).grid(row=2, column=1, sticky="w")
        tk.Label(pick_frame,
                 text="Leave blank to use this machine's configured key. "
                      "Paste another facility's key to decrypt their file.",
                 font=("Arial", 8), fg="#888"
                 ).grid(row=3, column=0, columnspan=2, sticky="w")

        # Manager-only facility picker: visible only when a master_secret is
        # configured AND the facility-name list has entries. Selecting a
        # facility derives its key from the master under the hood.
        self._decrypt_facility_placeholder = "— select facility —"
        facility_names = load_facility_names(self.config)
        if get_master_secret(self.config) and facility_names:
            tk.Label(pick_frame, text="Facility (derive key):",
                     font=("Arial", 9, "bold")
                     ).grid(row=4, column=0, sticky="w", pady=(8, 0))
            self.decrypt_facility_var = tk.StringVar(
                value=self._decrypt_facility_placeholder)
            combo = ttk.Combobox(
                pick_frame, textvariable=self.decrypt_facility_var,
                state="readonly", font=("Arial", 10),
                values=[self._decrypt_facility_placeholder] + facility_names,
            )
            combo.grid(row=5, column=0, sticky="we", padx=(0, 8))
            tk.Label(pick_frame,
                     text="Manager mode: derives the selected facility's key "
                          "from your master secret. The hex field above overrides this.",
                     font=("Arial", 8), fg="#888"
                     ).grid(row=6, column=0, columnspan=2, sticky="w")

        action_frame = tk.Frame(content)
        action_frame.pack(fill="x", pady=(10, 4))
        tk.Button(
            action_frame, text="PREVIEW", command=self._decrypt_preview,
            bg="#1976d2", fg="white", font=("Arial", 10, "bold"),
            padx=14, pady=4, cursor="hand2",
        ).pack(side="left")
        self.decrypt_save_button = tk.Button(
            action_frame, text="SAVE AS PLAIN FILE...", command=self._decrypt_save_plain,
            bg="#2e7d32", fg="white", font=("Arial", 10, "bold"),
            padx=14, pady=4, cursor="hand2",
        )
        self.decrypt_save_button.pack(side="left", padx=(8, 0))
        self.decrypt_hint = tk.Label(
            action_frame,
            text="Pick a file: .csv.nmrs saves as plain CSV; "
                 ".sql.gz.enc saves as plain .sql.",
            font=("Arial", 9), fg="#555",
        )
        self.decrypt_hint.pack(side="left", padx=(12, 0))

        # Preview area: Treeview with horizontal+vertical scroll.
        self.decrypt_preview_frame = tk.LabelFrame(
            content, text="Preview",
            font=("Arial", 10, "bold"), padx=8, pady=6,
        )
        preview_frame = self.decrypt_preview_frame
        preview_frame.pack(fill="both", expand=True, pady=(10, 0))

        tree_holder = tk.Frame(preview_frame)
        tree_holder.pack(fill="both", expand=True)

        self.decrypt_tree = ttk.Treeview(tree_holder, show="headings", height=16)
        yscroll = ttk.Scrollbar(tree_holder, orient="vertical", command=self.decrypt_tree.yview)
        xscroll = ttk.Scrollbar(tree_holder, orient="horizontal", command=self.decrypt_tree.xview)
        self.decrypt_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.decrypt_tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        tree_holder.rowconfigure(0, weight=1)
        tree_holder.columnconfigure(0, weight=1)

        self.decrypt_status = tk.Label(
            content, text="", font=("Arial", 9), fg="#555", anchor="w",
        )
        self.decrypt_status.pack(fill="x", pady=(4, 0))

    def _decrypt_browse(self):
        path = filedialog.askopenfilename(
            title="Pick an encrypted file",
            filetypes=[
                ("Encrypted files", "*.csv.nmrs *.nmrs *.sql.gz.enc"),
                ("Encrypted CSV", "*.csv.nmrs *.nmrs"),
                ("Encrypted DB backup", "*.sql.gz.enc"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self.decrypt_path.config(state="normal")
        self.decrypt_path.delete(0, "end")
        self.decrypt_path.insert(0, path)
        self.decrypt_path.config(state="readonly")
        self.decrypt_status.config(text="")
        # Clear the preview when a new file is selected.
        self.decrypt_tree["columns"] = ()
        for iid in self.decrypt_tree.get_children():
            self.decrypt_tree.delete(iid)
        # Adapt the Save button + hint + preview title to the selected file type.
        if self._decrypt_kind() == "sql":
            self.decrypt_save_button.config(text="SAVE AS PLAIN SQL...")
            self.decrypt_hint.config(
                text="DB backup selected. Preview shows the first 8 KB; "
                     "Save writes the full decrypted .sql. To restore into a "
                     "database, use the Restore tab instead.")
            self.decrypt_preview_frame.config(text="Preview (first 8 KB)")
        else:
            self.decrypt_save_button.config(text="SAVE AS PLAIN CSV...")
            self.decrypt_hint.config(
                text=f"Linelist selected. Preview shows first "
                     f"{self.DECRYPT_PREVIEW_ROWS} rows; Save writes the full "
                     f"plain CSV.")
            self.decrypt_preview_frame.config(
                text=f"Preview (first {self.DECRYPT_PREVIEW_ROWS} rows)")

    def _decrypt_read_bytes(self):
        """Read & decrypt the selected file. Returns raw plaintext bytes, or
        None on failure (with a user-facing error already shown).
        """
        path_str = self.decrypt_path.get().strip()
        if not path_str:
            messagebox.showwarning("No File", "Pick an encrypted file first.")
            return None
        path = Path(path_str)
        if not path.exists():
            messagebox.showerror("Not Found", f"File no longer exists:\n{path}")
            return None
        try:
            blob = path.read_bytes()
        except OSError as e:
            messagebox.showerror("Read Failed", str(e))
            return None
        if not is_encrypted_file(path):
            messagebox.showerror(
                "Not Encrypted",
                "This file doesn't have the NMRS encryption header — it's either "
                "a plain file (open it directly) or wasn't produced by this toolkit.",
            )
            return None
        try:
            key = self._resolve_decrypt_key()
        except (ValueError, RuntimeError) as e:
            messagebox.showerror("No Key", str(e))
            return None
        try:
            return decrypt_bytes(blob, key)
        except Exception as e:
            messagebox.showerror(
                "Decrypt Failed",
                f"Could not decrypt:\n\n{e}\n\n"
                f"The file may be corrupt, or it was encrypted with a different key.",
            )
            return None

    def _resolve_decrypt_key(self) -> bytes:
        """Key for the Decrypt tab, in precedence order:
          1. pasted hex field (manual override)
          2. selected facility in the dropdown (derived from master_secret)
          3. configured [backup] backup_key
        Raises ValueError/RuntimeError on invalid/absent key."""
        hex_field = (self.decrypt_key_var.get() or "").strip()
        if hex_field:
            try:
                k = bytes.fromhex(hex_field)
            except ValueError as e:
                raise ValueError(f"Backup key field is not valid hex: {e}")
            if len(k) != CRYPTO_KEY_LEN:
                raise ValueError(
                    f"Backup key must be {CRYPTO_KEY_LEN * 2} hex chars "
                    f"({CRYPTO_KEY_LEN} bytes); got {len(k)} bytes"
                )
            return k
        # Facility dropdown (manager mode): derive from master_secret.
        facility = getattr(self, "decrypt_facility_var", None)
        if facility is not None:
            chosen = facility.get().strip()
            if chosen and chosen != self._decrypt_facility_placeholder:
                master = get_master_secret(self.config)
                if not master:
                    raise RuntimeError(
                        "Facility selected but no master_secret is configured."
                    )
                return derive_facility_key(master, chosen)
        return get_facility_key(self.config)

    def _decrypt_kind(self) -> str:
        """Classify the currently-selected file as 'csv' or 'sql'."""
        name = self.decrypt_path.get().strip().lower()
        if name.endswith(".sql.gz.enc"):
            return "sql"
        return "csv"

    def _decrypt_read(self):
        """Back-compat wrapper for CSV consumers: returns plaintext str or None."""
        raw = self._decrypt_read_bytes()
        if raw is None:
            return None
        return raw.decode("utf-8", errors="replace")

    def _decrypt_preview(self):
        if self._decrypt_kind() == "sql":
            # SQL dumps don't render as a table. Decrypt + gunzip + show
            # the first few hundred lines as plain text in the status area.
            raw = self._decrypt_read_bytes()
            if raw is None:
                return
            try:
                sql_bytes = gzip.decompress(raw)
            except Exception as e:
                messagebox.showerror("Gunzip Failed",
                                     f"Decrypted OK but gunzip failed:\n{e}")
                return
            head = sql_bytes[:8192].decode("utf-8", errors="replace")
            self.decrypt_tree["columns"] = ()
            for iid in self.decrypt_tree.get_children():
                self.decrypt_tree.delete(iid)
            self.decrypt_status.config(
                text=(f"SQL dump preview ({len(sql_bytes):,} bytes uncompressed). "
                      "Use SAVE AS PLAIN to extract the full .sql file."),
                fg="#1976d2",
            )
            messagebox.showinfo(
                "SQL Preview",
                f"First 8 KB of decrypted dump:\n\n{head[:2000]}"
                + ("\n\n[truncated]" if len(head) > 2000 else ""),
            )
            self.log(f"[DECRYPT] previewed SQL dump from {self.decrypt_path.get()}")
            return
        text = self._decrypt_read()
        if text is None:
            return
        reader = csv.reader(text.splitlines())
        try:
            headers = next(reader)
        except StopIteration:
            messagebox.showinfo("Empty", "The decrypted file contains no rows.")
            return

        # Wipe + rebuild columns to match this file's headers.
        for iid in self.decrypt_tree.get_children():
            self.decrypt_tree.delete(iid)
        self.decrypt_tree["columns"] = headers
        for h in headers:
            self.decrypt_tree.heading(h, text=h)
            self.decrypt_tree.column(h, width=110, anchor="w", stretch=False)

        shown = 0
        total = 0
        for row in reader:
            total += 1
            if shown < self.DECRYPT_PREVIEW_ROWS:
                # Pad/truncate row to header length so the Treeview lines up.
                vals = list(row[: len(headers)])
                while len(vals) < len(headers):
                    vals.append("")
                self.decrypt_tree.insert("", "end", values=vals)
                shown += 1
        self.decrypt_status.config(
            text=f"Decrypted OK. Showing {shown} of {total} row(s); {len(headers)} column(s).",
            fg="#2e7d32",
        )
        self.log(f"[DECRYPT] previewed {total} row(s) from {self.decrypt_path.get()}")

    def _decrypt_save_plain(self):
        kind = self._decrypt_kind()
        if kind == "sql":
            raw = self._decrypt_read_bytes()
            if raw is None:
                return
            try:
                sql_bytes = gzip.decompress(raw)
            except Exception as e:
                messagebox.showerror("Gunzip Failed",
                                     f"Decrypted OK but gunzip failed:\n{e}")
                return
            src = Path(self.decrypt_path.get())
            stem = src.name
            if stem.endswith(".sql.gz.enc"):
                default = stem[: -len(".gz.enc")]  # -> something.sql
            else:
                default = stem + ".sql"
            target = filedialog.asksaveasfilename(
                initialfile=default,
                defaultextension=".sql",
                filetypes=[("SQL dump", "*.sql"), ("All files", "*.*")],
            )
            if not target:
                return
            try:
                Path(target).write_bytes(sql_bytes)
            except OSError as e:
                messagebox.showerror("Write Failed", str(e))
                return
            self.decrypt_status.config(
                text=f"Saved plain SQL -> {target} ({len(sql_bytes):,} bytes)",
                fg="#2e7d32",
            )
            self.log(f"[DECRYPT] wrote plaintext SQL -> {target}")
            messagebox.showinfo("Saved", f"Wrote plain SQL to:\n{target}")
            return

        # CSV path (default)
        text = self._decrypt_read()
        if text is None:
            return
        src = Path(self.decrypt_path.get())
        # Default name: strip the trailing .nmrs (and .csv.nmrs becomes .csv).
        stem = src.name
        if stem.endswith(".csv.nmrs"):
            default = stem[:-len(".nmrs")]  # -> something.csv
        elif stem.endswith(".nmrs"):
            default = stem[:-len(".nmrs")] + ".csv"
        else:
            default = stem + ".csv"

        target = filedialog.asksaveasfilename(
            initialfile=default,
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
        )
        if not target:
            return
        try:
            Path(target).write_text(text, encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Write Failed", str(e))
            return
        self.decrypt_status.config(
            text=f"Saved plain CSV -> {target}", fg="#2e7d32",
        )
        self.log(f"[DECRYPT] wrote plaintext -> {target}")
        messagebox.showinfo("Saved", f"Wrote plain CSV to:\n{target}")

    # ====================================================================
    # Backup tab
    # ====================================================================

    def _backup_setup(self, parent):
        content = tk.Frame(parent, padx=20, pady=15)
        content.pack(fill="both", expand=True)

        info = tk.LabelFrame(content, text="Status",
                             font=("Arial", 11, "bold"), padx=15, pady=10)
        info.pack(fill="x")

        tk.Label(info, text=f"Backup directory:  {BACKUP_DIR}",
                 font=("Arial", 10), anchor="w").pack(fill="x")
        self.backup_schedule_label = tk.Label(info, text="Schedule:  (checking...)",
                                              font=("Arial", 10), anchor="w")
        self.backup_schedule_label.pack(fill="x", pady=(2, 0))
        self.backup_schedule_label.config(text=f"Schedule:  {schedule_status()}")
        tk.Label(info, text="Backups run at 00:00 Mon-Fri and on system startup "
                            "via the OS scheduler. Runs are idempotent — only one "
                            "backup per day. Files are gzip-compressed and "
                            "AES-GCM encrypted.  Weekly linelists are generated "
                            "separately at 00:00 Thursday (see the Linelists tab).",
                 font=("Arial", 9), fg="#555", wraplength=860, justify="left",
                 anchor="w").pack(fill="x", pady=(6, 0))

        btn_row = tk.Frame(content)
        btn_row.pack(fill="x", pady=(10, 4))
        self.backup_run_button = tk.Button(
            btn_row, text="BACKUP NOW", command=self.backup_now,
            bg="#1976d2", fg="white", font=("Arial", 11, "bold"),
            padx=20, pady=6, cursor="hand2",
        )
        self.backup_run_button.pack(side="left")
        schedule_btn = tk.Button(
            btn_row, text="Update Schedules", command=self.reinstall_schedule,
            bg="#607d8b", fg="white", font=("Arial", 10, "bold"),
            padx=14, pady=4, cursor="hand2",
        )
        schedule_btn.pack(side="left", padx=(12, 0))
        Tooltip(
            schedule_btn,
            "Re-registers both OS-scheduler jobs: the daily backup (00:00 Mon-Fri "
            "+ on startup) and the weekly linelist batch (00:00 Thu + on startup). "
            "Use this if the binary path changed, an entry was deleted, or the "
            "schedule times were updated. This does NOT import or restore a "
            "database — use the Restore tab for that.",
        )
        tk.Button(btn_row, text="Open Folder", command=self.open_backup_folder,
                  bg="#9e9e9e", fg="white", font=("Arial", 10, "bold"),
                  padx=14, pady=4, cursor="hand2").pack(side="left", padx=(8, 0))

        # Progress + status for the long-running mysqldump pass.
        self.backup_progress = ttk.Progressbar(btn_row, mode="indeterminate", length=180)
        self.backup_progress.pack(side="left", padx=(20, 8))
        self.backup_status = tk.Label(
            btn_row, text="Idle.", font=("Arial", 9, "bold"), fg="#555",
        )
        self.backup_status.pack(side="left")

        log_frame = tk.LabelFrame(content, text="Recent backup log entries",
                                  font=("Arial", 10, "bold"), padx=8, pady=6)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.backup_log_text = scrolledtext.ScrolledText(
            log_frame, height=14, font=("Courier", 9),
            bg="#fafafa", fg="#212121", wrap="none",
        )
        self.backup_log_text.pack(fill="both", expand=True)
        self._reload_backup_log()

    def _reload_backup_log(self):
        self.backup_log_text.config(state="normal")
        self.backup_log_text.delete("1.0", "end")
        if BACKUP_LOG_FILE.exists():
            try:
                tail = BACKUP_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
                self.backup_log_text.insert("end", "\n".join(tail) + "\n")
            except OSError as e:
                self.backup_log_text.insert("end", f"(could not read log: {e})\n")
        else:
            self.backup_log_text.insert("end", "(no backup.log yet — runs will populate this)\n")
        self.backup_log_text.see("end")

    def backup_now(self):
        """UI-side handler. mysqldump + gzip + encrypt can take many minutes
        on a real OpenMRS schema; do it on a worker so the UI stays responsive.
        """
        if not messagebox.askyesno("Backup Now",
                                   f"Run an encrypted backup now to:\n{BACKUP_DIR}?"):
            return
        self.log("[BACKUP] manual run starting")
        self.backup_run_button.config(state="disabled", bg="#9e9e9e")
        self.backup_status.config(text="Running mysqldump...", fg="#1976d2")
        self.backup_progress.start(12)
        threading.Thread(target=self._backup_worker, daemon=True).start()

    def _backup_worker(self):
        t0 = time.monotonic()

        def step_log(msg):
            # Both surfaces: the global Activity Log (UI thread) and the on-disk log.
            self.root.after(0, self.log, msg)
            try:
                append_backup_log(msg)
            except Exception:
                pass

        try:
            out = perform_backup(self.config, log_func=step_log, force=True)
            append_backup_log(f"[BACKUP] OK {out} (manual)")
            elapsed = time.monotonic() - t0
            self.root.after(0, self._backup_finish, True, out, None, elapsed)
        except Exception as e:
            append_backup_log(f"[BACKUP] FAIL {e} (manual)")
            elapsed = time.monotonic() - t0
            self.root.after(0, self._backup_finish, False, None, str(e), elapsed)

    def _backup_finish(self, success, out_path, error, elapsed):
        self.backup_progress.stop()
        self.backup_run_button.config(state="normal", bg="#1976d2")
        self._reload_backup_log()
        if success:
            self.backup_status.config(
                text=f"Done in {elapsed:.1f}s — {Path(out_path).name}", fg="#2e7d32",
            )
            messagebox.showinfo("Backup Complete", f"Wrote:\n{out_path}\n\n({elapsed:.1f}s)")
        else:
            self.backup_status.config(text="Failed.", fg="#b71c1c")
            messagebox.showerror("Backup Failed", error)

    def reinstall_schedule(self):
        def _on(section, key):
            return (self.config.get(section, key, fallback="true").strip().lower()
                    in ("true", "yes", "1"))
        try:
            binary = Path(sys.executable if getattr(sys, "frozen", False)
                          else __file__).resolve()
            msg = install_schedules(binary, backup=_on("backup", "enabled"),
                                    linelist=_on("linelist", "auto_enabled"))
            SCHEDULE_MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
            SCHEDULE_MARKER_FILE.write_text(
                f"installed_at={datetime.now().isoformat()}\n"
                f"schedule_version={SCHEDULE_VERSION}\n{msg}\n"
            )
            self.log(f"[SCHED] re-installed: {msg}")
            self.backup_schedule_label.config(text=f"Schedule:  {schedule_status()}")
            messagebox.showinfo("Schedules Installed", msg)
        except Exception as e:
            messagebox.showerror("Schedule Install Failed", str(e))

    def open_backup_folder(self):
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if platform.system() == "Windows":
                os.startfile(str(BACKUP_DIR))  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(BACKUP_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(BACKUP_DIR)])
        except Exception as e:
            messagebox.showerror("Open Failed", str(e))

    # ------------------------------------------------------------------
    # Restore tab (skeleton — full logic in task #6)
    # ------------------------------------------------------------------

    def _restore_setup(self, parent):
        content = tk.Frame(parent, padx=20, pady=15)
        content.pack(fill="both", expand=True)

        intro = tk.LabelFrame(content, text="Restore an encrypted or plain MySQL dump",
                              font=("Arial", 11, "bold"), padx=15, pady=10)
        intro.pack(fill="x")
        tk.Label(
            intro,
            text=(
                "Accepts .sql.gz.enc (NMRS encrypted), .sql.gz, .sql.zip, and plain .sql "
                "files. If the target database already exists, the app takes a pre-restore "
                "safety backup before dropping and recreating it. Pre-restore snapshots "
                "live in a separate folder and are kept across the usual retention sweeps."
            ),
            font=("Arial", 9), fg="#555", wraplength=860, justify="left", anchor="w",
        ).pack(fill="x")

        # File picker row
        file_row = tk.Frame(content)
        file_row.pack(fill="x", pady=(12, 4))
        tk.Label(file_row, text="Dump file:", font=("Arial", 10, "bold"),
                 width=14, anchor="w").pack(side="left")
        self.restore_file_var = tk.StringVar(value="")
        tk.Entry(file_row, textvariable=self.restore_file_var,
                 font=("Arial", 10), state="readonly").pack(
            side="left", fill="x", expand=True, padx=(0, 8))
        tk.Button(file_row, text="Browse...", command=self._restore_pick_file,
                  bg="#607d8b", fg="white", font=("Arial", 9, "bold"),
                  padx=10, pady=2).pack(side="left")

        # Target DB row
        db_row = tk.Frame(content)
        db_row.pack(fill="x", pady=(4, 4))
        tk.Label(db_row, text="Target database:", font=("Arial", 10, "bold"),
                 width=14, anchor="w").pack(side="left")
        self.restore_db_var = tk.StringVar(
            value=self.config.get("database", "database", fallback="openmrs"))
        tk.Entry(db_row, textvariable=self.restore_db_var,
                 font=("Arial", 10)).pack(side="left", fill="x", expand=True)

        # Key row (only meaningful for .sql.gz.enc files — wired up in task #6)
        key_row = tk.Frame(content)
        key_row.pack(fill="x", pady=(4, 8))
        tk.Label(key_row, text="Backup key (hex):", font=("Arial", 10, "bold"),
                 width=14, anchor="w").pack(side="left")
        self.restore_key_var = tk.StringVar(value="")
        tk.Entry(key_row, textvariable=self.restore_key_var,
                 font=("Courier", 10), show="*").pack(
            side="left", fill="x", expand=True)
        tk.Label(key_row, text="(leave blank for plain/unencrypted files)",
                 font=("Arial", 8), fg="#888").pack(side="left", padx=(8, 0))

        # Action row
        btn_row = tk.Frame(content)
        btn_row.pack(fill="x", pady=(6, 4))
        self.restore_run_button = tk.Button(
            btn_row, text="RESTORE", command=self.restore_now,
            bg="#c62828", fg="white", font=("Arial", 11, "bold"),
            padx=20, pady=6, cursor="hand2",
        )
        self.restore_run_button.pack(side="left")
        Tooltip(
            self.restore_run_button,
            "Destructive operation. If the target database exists, it will be "
            "backed up, dropped, recreated, and replaced with the contents of "
            "the selected dump file.",
        )

        self.restore_cancel_button = tk.Button(
            btn_row, text="CANCEL", command=self._restore_request_cancel,
            bg="#9e9e9e", fg="white", font=("Arial", 10, "bold"),
            padx=14, pady=4, state="disabled", cursor="hand2",
        )
        self.restore_cancel_button.pack(side="left", padx=(8, 0))

        self.restore_progress = ttk.Progressbar(btn_row, mode="determinate",
                                                length=240, maximum=100)
        self.restore_progress.pack(side="left", padx=(20, 8))
        self.restore_status = tk.Label(
            btn_row, text="Idle.", font=("Arial", 9, "bold"), fg="#555",
        )
        self.restore_status.pack(side="left")

        self._restore_cancel_event = threading.Event()

        # Restore log area
        log_frame = tk.LabelFrame(content, text="Restore activity",
                                  font=("Arial", 10, "bold"), padx=8, pady=6)
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.restore_log_text = scrolledtext.ScrolledText(
            log_frame, height=14, font=("Courier", 9),
            bg="#fafafa", fg="#212121", wrap="none",
        )
        self.restore_log_text.pack(fill="both", expand=True)
        self._restore_log("Idle. Pick a dump file and target database, then click RESTORE.")

    def _restore_pick_file(self):
        path = filedialog.askopenfilename(
            title="Choose a dump file to restore",
            filetypes=[
                ("All supported", "*.sql *.sql.gz *.sql.gz.enc *.sql.zip"),
                ("NMRS encrypted", "*.sql.gz.enc"),
                ("Gzip-compressed SQL", "*.sql.gz"),
                ("Zipped SQL", "*.sql.zip"),
                ("Plain SQL", "*.sql"),
                ("All files", "*.*"),
            ],
            initialdir=str(BACKUP_DIR) if BACKUP_DIR.exists() else str(Path.home()),
        )
        if path:
            self.restore_file_var.set(path)

    # -- restore: logging + UI updaters ---------------------------------

    def _restore_log(self, msg: str, level: str = "info"):
        """Append a line to the on-screen restore log AND the on-disk log."""
        try:
            self.restore_log_text.config(state="normal")
            stamp = datetime.now().strftime("%H:%M:%S")
            self.restore_log_text.insert("end", f"[{stamp}] {msg}\n")
            self.restore_log_text.see("end")
            self.restore_log_text.config(state="disabled")
        except tk.TclError:
            pass
        try:
            append_restore_log(msg)
        except Exception:
            pass

    def _restore_set_status(self, text: str, color: str = "#555"):
        try:
            self.restore_status.config(text=text, fg=color)
        except tk.TclError:
            pass

    def _restore_set_progress(self, pct: float):
        try:
            self.restore_progress["value"] = max(0.0, min(100.0, pct))
        except tk.TclError:
            pass

    def _restore_request_cancel(self):
        self._restore_cancel_event.set()
        self._restore_log("Cancel requested — will stop after current chunk.",
                          level="warn")

    def _restore_lock_ui(self, locked: bool):
        try:
            self.restore_run_button.config(
                state="disabled" if locked else "normal",
                bg="#9e9e9e" if locked else "#c62828",
            )
            self.restore_cancel_button.config(
                state="normal" if locked else "disabled",
            )
        except tk.TclError:
            pass

    # -- restore: validation + entry point ------------------------------

    def restore_now(self):
        src = self.restore_file_var.get().strip()
        target_db = self.restore_db_var.get().strip()
        if not src:
            messagebox.showwarning("No File", "Pick a dump file first.")
            return
        if not Path(src).exists():
            messagebox.showerror("Not Found", f"File not found:\n{src}")
            return
        if not target_db:
            messagebox.showwarning("No Database", "Enter the target database name.")
            return

        # First confirmation — coarse warning before we touch anything.
        ok = messagebox.askyesno(
            "Confirm Restore",
            f"Restore '{Path(src).name}' into database '{target_db}'?\n\n"
            "If the database exists, a safety backup will be taken first, "
            "then the database will be dropped and recreated.\n\n"
            "This operation may take many minutes for large databases.",
        )
        if not ok:
            return

        self._restore_cancel_event.clear()
        self._restore_lock_ui(True)
        self._restore_set_progress(0)
        self._restore_set_status("Starting...", "#1976d2")
        self._restore_log("=" * 60)
        self._restore_log(f"Restore requested: src={src}  target={target_db}")
        threading.Thread(target=self._restore_worker,
                         args=(src, target_db, self.restore_key_var.get()),
                         daemon=True).start()

    # -- restore: helpers -----------------------------------------------

    def _resolve_restore_key(self, ui_key_hex: str):
        """Pick the key for decrypting the selected .sql.gz.enc. Priority:
        UI Backup-key field (hex) -> [backup] backup_key in config -> None.
        Plain (.sql / .sql.gz / .sql.zip) files don't need this — the
        decrypt helper only complains when the file is .enc and the key
        is None.
        """
        ui_key_hex = (ui_key_hex or "").strip()
        if ui_key_hex:
            try:
                k = bytes.fromhex(ui_key_hex)
            except ValueError as e:
                raise ValueError(f"Backup key field is not valid hex: {e}")
            if len(k) != CRYPTO_KEY_LEN:
                raise ValueError(
                    f"Backup key must be {CRYPTO_KEY_LEN * 2} hex chars "
                    f"({CRYPTO_KEY_LEN} bytes); got {len(k)} bytes"
                )
            return k
        try:
            return get_facility_key(self.config)
        except RuntimeError:
            return None  # only an error later if the file is actually encrypted

    # -- restore: worker (runs off the UI thread) -----------------------

    def _restore_worker(self, src: str, target_db: str, key: str):
        def ui_log(msg, level="info"):
            self.root.after(0, self._restore_log, msg, level)

        def ui_status(text, color="#555"):
            self.root.after(0, self._restore_set_status, text, color)

        def ui_progress(pct):
            self.root.after(0, self._restore_set_progress, pct)

        src_path = Path(src)
        temp_sql = BACKUP_DIR / f".tmp_restore_{secrets.token_hex(6)}.sql"
        t_start = time.monotonic()
        cancelled = False

        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)

            # Stage 1: decrypt / decompress to temp SQL
            ui_status("Preparing dump file...", "#1976d2")
            ui_log(f"Stage 1: decrypt + decompress -> {temp_sql.name}")
            key_bytes = self._resolve_restore_key(key)
            bytes_written = _decrypt_to_sql_file(src_path, key_bytes, temp_sql, ui_log)
            ui_log(f"Wrote {bytes_written:,} bytes of plain SQL")

            # Stage 2: sanity check
            ui_log("Stage 2: SQL sanity check")
            _sql_sanity_check(temp_sql)
            ui_log("SQL looks valid (contains CREATE TABLE / INSERT INTO)")

            if self._restore_cancel_event.is_set():
                raise InterruptedError("cancelled before destructive phase")

            # Stage 3: check target DB existence
            ui_status("Checking target database...", "#1976d2")
            db_cfg = self.config["database"]
            exists = _mysql_db_exists(db_cfg, target_db)
            ui_log(f"Target database '{target_db}' "
                   + ("EXISTS" if exists else "does not exist"))

            if exists:
                # Stage 4: typed confirmation
                ok = self._restore_typed_confirm(target_db)
                if not ok:
                    raise InterruptedError("user declined typed confirmation")

                # Stage 5: pre-restore backup
                ui_status("Taking pre-restore safety backup...", "#1976d2")
                ui_log("Stage 5: pre-restore backup (mysqldump + gzip + encrypt + verify)")
                pre_path = perform_pre_restore_backup(self.config, ui_log)
                ui_log(f"Pre-restore backup OK: {pre_path}")

                if self._restore_cancel_event.is_set():
                    raise InterruptedError("cancelled after pre-restore backup")

                # Stage 6: drop + create
                ui_status("Dropping and recreating database...", "#c62828")
                ui_log(f"Stage 6: DROP DATABASE `{target_db}`; CREATE DATABASE `{target_db}`")
                _mysql_admin(db_cfg, [
                    f"DROP DATABASE `{target_db}`",
                    f"CREATE DATABASE `{target_db}` "
                    f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
                ])
            else:
                ui_status("Creating target database...", "#1976d2")
                ui_log(f"Stage 6: CREATE DATABASE `{target_db}`")
                _mysql_admin(db_cfg, [
                    f"CREATE DATABASE `{target_db}` "
                    f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
                ])

            # Stage 7: stream import
            ui_status("Importing SQL — this can take a while...", "#1976d2")
            ui_log("Stage 7: streaming SQL into mysql with progress")
            self._restore_stream_import(temp_sql, target_db, db_cfg,
                                        ui_log, ui_status, ui_progress)

            # Stage 8: post-import verification
            ui_status("Verifying restored data...", "#1976d2")
            ui_log("Stage 8: post-import verification")
            self._restore_verify(db_cfg, target_db, ui_log)

            elapsed = time.monotonic() - t_start
            ui_progress(100)
            ui_status(f"Restore complete in {elapsed/60:.1f} min", "#2e7d32")
            ui_log(f"DONE in {elapsed:.1f}s")
            self.root.after(0, messagebox.showinfo, "Restore Complete",
                            f"Restored '{src_path.name}' into '{target_db}' "
                            f"in {elapsed/60:.1f} min.")
        except InterruptedError as e:
            cancelled = True
            ui_status("Cancelled.", "#ef6c00")
            ui_log(f"CANCELLED: {e}", level="warn")
            self.root.after(0, messagebox.showwarning, "Restore Cancelled",
                            f"Restore was cancelled: {e}\n\n"
                            f"If a pre-restore backup was taken, it's in:\n{PRE_RESTORE_DIR}")
        except Exception as e:
            ui_status("Failed.", "#b71c1c")
            ui_log(f"FAILED: {e}", level="error")
            self.root.after(0, messagebox.showerror, "Restore Failed", str(e))
        finally:
            _shred_unlink(temp_sql)
            self.root.after(0, self._restore_lock_ui, False)
            if not cancelled:
                self._restore_cancel_event.clear()

    def _restore_typed_confirm(self, db_name: str) -> bool:
        """Modal: user must type the exact DB name to proceed. Returns True on match."""
        result = {"ok": False}
        dlg = tk.Toplevel(self.root)
        dlg.title("Confirm destructive restore")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.geometry("+%d+%d" % (self.root.winfo_rootx() + 80,
                                  self.root.winfo_rooty() + 80))
        tk.Label(dlg, text="DESTRUCTIVE OPERATION",
                 font=("Arial", 12, "bold"), fg="#c62828").pack(padx=20, pady=(16, 4))
        tk.Label(dlg, text=f"This will DROP and recreate the database '{db_name}'.\n"
                            f"A safety backup will be taken first, but the live data\n"
                            f"will be replaced with the contents of the selected dump.",
                 justify="left").pack(padx=20, pady=(0, 8))
        tk.Label(dlg, text=f"Type '{db_name}' to confirm:",
                 font=("Arial", 10, "bold")).pack(padx=20, pady=(8, 2))
        entry = tk.Entry(dlg, font=("Courier", 11), width=30)
        entry.pack(padx=20, pady=(0, 12))
        entry.focus()

        def submit():
            if entry.get().strip() == db_name:
                result["ok"] = True
                dlg.destroy()
            else:
                messagebox.showerror("Mismatch",
                                     f"You typed '{entry.get()}'. Must match exactly: '{db_name}'.",
                                     parent=dlg)

        btn_row = tk.Frame(dlg)
        btn_row.pack(pady=(0, 14))
        tk.Button(btn_row, text="Proceed (DESTRUCTIVE)", command=submit,
                  bg="#c62828", fg="white", font=("Arial", 10, "bold"),
                  padx=16, pady=4).pack(side="left", padx=6)
        tk.Button(btn_row, text="Cancel", command=dlg.destroy,
                  bg="#9e9e9e", fg="white", font=("Arial", 10, "bold"),
                  padx=16, pady=4).pack(side="left", padx=6)
        entry.bind("<Return>", lambda e: submit())
        self.root.wait_window(dlg)
        return result["ok"]

    def _restore_stream_import(self, sql_path: Path, target_db: str, db_cfg,
                                ui_log, ui_status, ui_progress):
        """Pipe the temp .sql file into `mysql` with a byte counter."""
        total = sql_path.stat().st_size
        ui_log(f"Importing {total:,} bytes...")

        mysql_bin = shutil.which("mysql") or "mysql"
        cmd = [
            mysql_bin,
            f"-h{db_cfg['host']}",
            f"-P{db_cfg.get('port', '3306')}",
            f"-u{db_cfg['user']}",
            f"-p{db_cfg['password']}",
            "--max_allowed_packet=1G",
            "--init-command=SET autocommit=0; SET unique_checks=0; SET foreign_key_checks=0;",
            "--default-character-set=utf8mb4",
            target_db,
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=_NO_WINDOW)
        sent = 0
        t_in = time.monotonic()
        last_ui = 0.0
        try:
            with open(sql_path, "rb") as src:
                while True:
                    if self._restore_cancel_event.is_set():
                        proc.kill()
                        raise InterruptedError("cancelled during import")
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break
                    try:
                        proc.stdin.write(chunk)
                    except BrokenPipeError:
                        break
                    sent += len(chunk)
                    now = time.monotonic()
                    if now - last_ui >= 0.5:
                        last_ui = now
                        pct = (sent / total) * 100 if total else 0
                        elapsed = now - t_in
                        rate = sent / elapsed if elapsed > 0 else 0
                        remaining = (total - sent) / rate if rate > 0 else 0
                        ui_progress(pct)
                        ui_status(
                            f"Imported {sent/1e6:.1f} / {total/1e6:.1f} MB "
                            f"({pct:.1f}%, {rate/1e6:.1f} MB/s, "
                            f"ETA {remaining:.0f}s)",
                            "#1976d2",
                        )
            try:
                proc.stdin.close()
            except OSError:
                pass
        finally:
            rc = proc.wait()
        if rc != 0:
            stderr = proc.stderr.read().decode("utf-8", errors="replace")[:2000]
            raise RuntimeError(f"mysql import failed (rc={rc}):\n{stderr}")
        ui_progress(100)
        ui_log(f"Import finished — {sent:,} bytes sent to mysql")

    def _restore_verify(self, db_cfg, target_db: str, ui_log):
        """Post-import sanity check: table count > 0; if known tables exist, sample row counts."""
        conn = db_connect(
            host=db_cfg["host"], user=db_cfg["user"], password=db_cfg["password"],
            database=target_db,
            port=int(db_cfg.get("port", 3306)), connection_timeout=10,
        )
        try:
            cur = conn.cursor()
            cur.execute("SHOW TABLES")
            tables = [r[0] for r in cur.fetchall()]
            ui_log(f"Verification: {len(tables)} table(s) present")
            if len(tables) == 0:
                raise RuntimeError("Restored database has no tables")
            for sample in ("patient", "encounter", "obs", "users"):
                if sample in tables:
                    cur.execute(f"SELECT COUNT(*) FROM `{sample}`")
                    n = cur.fetchone()[0]
                    ui_log(f"  rows in `{sample}`: {n:,}")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    if "--backup" in args:
        sys.exit(run_headless_backup())
    if "--generate-linelists" in args:
        sys.exit(run_headless_linelists())
    root = tk.Tk()
    NMRSToolkitApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
