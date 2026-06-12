"""Headless entry points used by the OS scheduler.

`run_headless_backup` (--backup) and `run_headless_linelists`
(--generate-linelists) PRESERVED VERBATIM. This module imports NO UI / webview
code, so it runs on a machine with no display and no frontend bundle.
"""
from __future__ import annotations

import sys
from datetime import datetime

from .config import load_config
from .workflows.backup import append_backup_log, perform_backup
from .workflows.linelist import (
    _current_week_id,
    _read_linelist_week_marker,
    _write_linelist_week_marker,
    append_linelist_log,
    perform_linelist_batch,
)

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

