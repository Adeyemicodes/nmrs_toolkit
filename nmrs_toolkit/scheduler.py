"""OS scheduler integration (cron / schtasks). PRESERVED VERBATIM."""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from .constants import (
    BACKUP_DIR, BACKUP_LOG_FILE, LINELIST_DIR, LINELIST_LOG_FILE, _NO_WINDOW,
)

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

