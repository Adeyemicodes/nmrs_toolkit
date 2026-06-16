"""Config loading, secure-location migration, and manager helpers.

PRESERVED behavior (MIGRATION_PLAN.md section 2): the .nmrs_config.ini format,
the platform-specific secure directory, and the first-launch migration of
older configs into it. LOADED_CONFIG_PATH is a module global updated by
load_config(); reference it as `config.LOADED_CONFIG_PATH` (attribute access)
so cross-module callers see the live value.
"""
from __future__ import annotations

import configparser
import os
import platform
import shutil
import sys
from pathlib import Path

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
        # The config is UTF-8 (the shipped template contains em-dashes, ±, etc.).
        # configparser.read() opens with the platform default encoding, which on
        # Windows is cp1252 — that crashes on any multibyte UTF-8 sequence (e.g.
        # "charmap codec can't decode byte 0x9d"). Read UTF-8 explicitly (BOM
        # tolerated via utf-8-sig); fall back to latin-1, which decodes any byte
        # without error, so a legacy cp1252-encoded file can never crash startup.
        try:
            text = p.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            text = p.read_text(encoding="latin-1")
        cfg.read_string(text, source=str(p))
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
# Admin password gate (single source of truth for Tk UI + PyWebView bridge)
# ---------------------------------------------------------------------------

def get_admin_password(config: configparser.ConfigParser) -> str:
    """Launch-gate password from [settings] admin_password. Empty = no gate."""
    return config.get("settings", "admin_password", fallback="").strip()


def admin_password_configured(config: configparser.ConfigParser) -> bool:
    """True if this installation has an admin password set."""
    return bool(get_admin_password(config))


def verify_admin_password(config: configparser.ConfigParser, candidate: str) -> bool:
    """Canonical login check used by BOTH the Tk UI and the PyWebView bridge.

    Compares the raw candidate against the stripped [settings] admin_password —
    the exact field and comparison the legacy `_check_password` performed. Do
    not reimplement this comparison elsewhere; call here.
    """
    return (candidate or "") == get_admin_password(config)

