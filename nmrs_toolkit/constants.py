"""Shared constants: filesystem layout, app identity, bundled-script lookup.

Path/identity constants and the curated LINELIST_REGISTRY are copied verbatim
from the legacy single-file build. `resource_path` is adjusted for the package
layout (the package lives one directory below the repo root that holds
scripts/), and the new APPLICATION_LOG_FILE path is added for the unified
AppLogger.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

# A windowed (no-console) Windows build still pops a visible console window for
# every child process it spawns — mysqldump, the mysql restore client, schtasks.
# To a non-technical user that flash looks like an error, and closing it can
# interrupt a running backup. CREATE_NO_WINDOW runs them silently. The attribute
# exists only on Windows, so guard on the platform first; 0 is a no-op elsewhere.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0


APP_NAME = "NMRS Toolkit"
APP_VERSION = "1.2.1"

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


# Hidden per-user app-state directory (already used for the schedule marker).
SCHEDULE_MARKER_FILE = Path.home() / ".nmrs_toolkit" / "schedule_installed"

# Restore artefacts live alongside the backups.
PRE_RESTORE_DIR = BACKUP_DIR / "pre_restore"
RESTORE_LOG_FILE = BACKUP_DIR / "restore.log"

# NEW (v2): the unified cross-cutting forensic log. Lives in the hidden
# per-user app dir because it spans every workflow, not just backups.
APPLICATION_LOG_FILE = Path.home() / ".nmrs_toolkit" / "application.log"


def resource_path(rel: str) -> Path:
    """Return absolute path to a bundled resource, both in dev and PyInstaller.

    In a frozen build, resources live under sys._MEIPASS. In dev, the package
    sits one level below the repo root that contains scripts/, so we resolve
    against this file's parent's parent (legacy code used a single .parent
    because it lived at the repo root).
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / rel
    return Path(__file__).resolve().parent.parent / rel


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

