"""Unified application logging service (AppLogger).

The canonical log-emission point for every workflow. A single emit() call:

  1. appends a structured line to an in-memory ring buffer (live UI tail),
  2. appends to APPLICATION_LOG_FILE (the cross-cutting forensic record,
     rotated at 10 MB, 3 generations),
  3. appends to the category-specific on-disk file (backup.log, restore.log,
     linelist.log) when one exists for that category,
  4. notifies any subscribed UI panels with a structured event.

Secrets (backup_key, master_secret, admin_password, password) are redacted
before any line is written or dispatched. See MIGRATION_PLAN.md section 4.
"""
from __future__ import annotations

import os
import re
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from .constants import (
    APPLICATION_LOG_FILE, BACKUP_LOG_FILE, LINELIST_LOG_FILE, RESTORE_LOG_FILE,
)

# Categories the logger understands. APP/UI/MERGE/UNVOID/SCHED have no dedicated
# on-disk file (they live in APPLICATION_LOG_FILE only); BACKUP/RESTORE/LINELIST
# also fan out to their long-standing per-workflow files.
VALID_CATEGORIES = (
    "APP", "BACKUP", "RESTORE", "LINELIST", "MERGE", "UNVOID", "SCHED", "UI",
)
VALID_LEVELS = ("info", "warn", "error", "debug")

# Redaction: match a known secret key followed by =/: and a value, keep the key,
# replace the value. Case-insensitive. admin_password is matched before the
# generic `password` alternative, but either redacts the value.
_SECRET_RE = re.compile(
    r"(?i)\b(backup_key|master_secret|admin_password|password)(\s*[=:]\s*)(\S+)"
)


def redact(text: str) -> str:
    """Return `text` with any recognised secret value replaced by <redacted>."""
    return _SECRET_RE.sub(r"\1\2<redacted>", text)


class AppLogger:
    """Cross-cutting log service. All workflow code emits through this.

    Thread-safe: emit() may be called from worker threads (backups, restores,
    linelists run off the UI thread) and the UI thread concurrently.
    """

    def __init__(
        self,
        application_log_path: Path,
        category_log_paths: dict,
        *,
        ring_size: int = 5000,
        max_bytes: int = 10 * 1024 * 1024,
        backup_generations: int = 3,
    ) -> None:
        self._app_path = Path(application_log_path)
        self._cat_paths = {k.upper(): Path(v) for k, v in category_log_paths.items()}
        self._ring: deque = deque(maxlen=ring_size)
        self._lock = threading.RLock()
        self._subscribers: List[tuple] = []  # (callback, categories_or_None)
        self._max_bytes = max_bytes
        self._backup_generations = backup_generations
        self._last_rotated: Optional[str] = None
        self._seq = 0  # monotonic event id; lets the UI dedupe push vs. tail

    # -- emission --------------------------------------------------------

    def emit(
        self,
        message: str,
        *,
        category: str = "APP",
        level: str = "info",
        facility: Optional[str] = None,
        operation_id: Optional[str] = None,
    ) -> None:
        category = (category or "APP").upper()
        if category not in VALID_CATEGORIES:
            category = "APP"
        level = (level or "info").lower()
        if level not in VALID_LEVELS:
            level = "info"
        ts = datetime.now().isoformat(timespec="seconds")
        line = redact(self._format(ts, level, category, facility, operation_id, message))
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "ts": ts,
                "level": level,
                "category": category,
                "facility": facility,
                "operation_id": operation_id,
                "message": redact(str(message)),
                "line": line,
            }
            self._ring.append(event)
            self._append(self._app_path, line)
            self._maybe_rotate()
            cat_path = self._cat_paths.get(category)
            if cat_path is not None:
                self._append(cat_path, line)
            subscribers = list(self._subscribers)
        # Dispatch outside the lock so a slow/blocking subscriber can't stall
        # a worker thread that's mid-emit.
        for callback, categories in subscribers:
            if categories and category not in categories:
                continue
            try:
                callback(event)
            except Exception:
                pass  # a broken subscriber must never break logging

    @staticmethod
    def _format(ts, level, category, facility, operation_id, message) -> str:
        meta = ""
        if facility:
            meta += f"[{facility}] "
        if operation_id:
            meta += f"({operation_id}) "
        return f"{ts}  {level.upper():<5} {category:<8} {meta}{message}"

    @staticmethod
    def _append(path: Path, line: str) -> None:
        """Append one line and flush so it survives a process crash."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
        except OSError:
            pass  # never let a logging failure propagate into a workflow

    # -- rotation --------------------------------------------------------

    def _maybe_rotate(self) -> None:
        try:
            if not self._app_path.exists():
                return
            if self._app_path.stat().st_size < self._max_bytes:
                return
        except OSError:
            return
        self._rotate()

    def _rotate(self) -> None:
        """application.log -> .1 -> .2 -> .3 (drop the oldest)."""
        try:
            oldest = self._app_path.with_suffix(self._app_path.suffix + f".{self._backup_generations}")
            if oldest.exists():
                oldest.unlink()
            for gen in range(self._backup_generations - 1, 0, -1):
                src = self._app_path.with_suffix(self._app_path.suffix + f".{gen}")
                if src.exists():
                    src.rename(self._app_path.with_suffix(self._app_path.suffix + f".{gen + 1}"))
            if self._app_path.exists():
                self._app_path.rename(self._app_path.with_suffix(self._app_path.suffix + ".1"))
            self._last_rotated = datetime.now().isoformat(timespec="seconds")
        except OSError:
            pass

    def rotate_now(self) -> dict:
        """Force a rotation regardless of size. Returns {ok, rotated_at}."""
        with self._lock:
            self._rotate()
        return {"ok": True, "rotated_at": self._last_rotated}

    # -- subscribers -----------------------------------------------------

    def subscribe(self, callback: Callable, categories: Optional[Iterable[str]] = None) -> None:
        cats = {c.upper() for c in categories} if categories else None
        with self._lock:
            self._subscribers.append((callback, cats))

    def unsubscribe(self, callback: Callable) -> None:
        # Compare with == not `is`: a bound method (obj.method) yields a fresh
        # object on each access but compares equal by (instance, function), so
        # callers can unsubscribe the same bound method they subscribed.
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s[0] != callback]

    # -- queries ---------------------------------------------------------

    def tail(self, n: int = 200, categories=None, level=None) -> list:
        """Return the last `n` matching events from the in-memory ring."""
        cats = {c.upper() for c in categories} if categories else None
        lvls = {l.lower() for l in level} if level else None
        with self._lock:
            events = list(self._ring)
        out = [e for e in events
               if (not cats or e["category"] in cats)
               and (not lvls or e["level"] in lvls)]
        return out[-n:]

    def search(self, query: str, *, categories=None, level=None,
               since=None, until=None) -> list:
        """Case-insensitive substring search over the ring buffer."""
        q = (query or "").lower()
        cats = {c.upper() for c in categories} if categories else None
        lvls = {l.lower() for l in level} if level else None
        with self._lock:
            events = list(self._ring)
        out = []
        for e in events:
            if cats and e["category"] not in cats:
                continue
            if lvls and e["level"] not in lvls:
                continue
            if since and e["ts"] < since:
                continue
            if until and e["ts"] > until:
                continue
            if q and q not in e["line"].lower():
                continue
            out.append(e)
        return out

    def export_filtered(self, output_path, *, categories=None, level=None,
                        since=None, until=None, query=None) -> dict:
        """Write the filtered slice (matching the same predicate as search) to
        `output_path` as plain UTF-8 text. Returns {ok, written_bytes, lines}."""
        events = self.search(query or "", categories=categories, level=level,
                             since=since, until=until)
        text = "\n".join(e["line"] for e in events)
        if text:
            text += "\n"
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        return {"ok": True, "written_bytes": len(text.encode("utf-8")), "lines": len(events)}

    # -- restart survival ------------------------------------------------

    def load_recent_from_disk(self, n: int = 500) -> int:
        """Seed the ring buffer from the tail of APPLICATION_LOG_FILE so the UI
        shows recent history after a restart. Returns the number of lines loaded.
        Lines are stored as raw `line` events with best-effort field parsing."""
        try:
            if not self._app_path.exists():
                return 0
            raw = self._app_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return 0
        tail = raw[-n:]
        loaded = 0
        with self._lock:
            for line in tail:
                self._seq += 1
                event = self._parse_line(line)
                event["seq"] = self._seq
                self._ring.append(event)
                loaded += 1
        return loaded

    @staticmethod
    def _parse_line(line: str) -> dict:
        """Best-effort parse of a persisted line back into an event dict."""
        ts, level, category = "", "info", "APP"
        parts = line.split(None, 3)
        if len(parts) >= 3 and "T" in parts[0]:
            ts = parts[0]
            if parts[1].lower() in VALID_LEVELS:
                level = parts[1].lower()
            if parts[2].upper() in VALID_CATEGORIES:
                category = parts[2].upper()
        return {
            "ts": ts, "level": level, "category": category,
            "facility": None, "operation_id": None,
            "message": line, "line": line,
        }

    # -- disk-size reporting ---------------------------------------------

    def disk_info(self) -> dict:
        """Return {path, size_bytes, last_rotated} for the application log."""
        try:
            size = self._app_path.stat().st_size if self._app_path.exists() else 0
        except OSError:
            size = 0
        return {
            "path": str(self._app_path),
            "size_bytes": size,
            "last_rotated": self._last_rotated,
        }


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_LOGGER: Optional[AppLogger] = None
_LOGGER_LOCK = threading.Lock()


def get_logger() -> AppLogger:
    """Return the process-wide AppLogger, creating it on first use."""
    global _LOGGER
    if _LOGGER is None:
        with _LOGGER_LOCK:
            if _LOGGER is None:
                _LOGGER = AppLogger(
                    APPLICATION_LOG_FILE,
                    {
                        "BACKUP": BACKUP_LOG_FILE,
                        "RESTORE": RESTORE_LOG_FILE,
                        "LINELIST": LINELIST_LOG_FILE,
                    },
                )
    return _LOGGER
