"""JS-callable bridge exposed to the PyWebView frontend.

The bridge is the ONLY surface where the HTML/JS frontend can invoke Python
work (MIGRATION_PLAN.md section 7). Every method takes JSON-serializable args
and returns JSON-serializable results. Methods are named with a `namespace_`
prefix (e.g. `auth_login`); the frontend's bridge.js re-presents them as the
dotted `auth.login(...)` API from the plan.

Phase 1 scope: auth + config summary. Later phases extend this class with
backup/restore/linelist/merge/unvoid/log methods.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config as _config_mod
from .config import (
    admin_password_configured, load_config, verify_admin_password,
)
from .constants import (
    APP_NAME, APP_VERSION, APPLICATION_LOG_FILE, BACKUP_DIR, LINELIST_DIR,
    SCHEDULE_MARKER_FILE, SCHEDULE_VERSION, _NO_WINDOW, batch_linelists,
    bundled_scripts,
)
from .crypto import get_facility_key
from .logger import get_logger
from .scheduler import install_schedules, schedule_status
from .workflows.backup import append_backup_log, perform_backup
from .workflows.linelist import (
    append_linelist_log, execute_sql_script, perform_linelist_batch,
    write_linelist_csv,
)
from .workflows.merge import append_merge_log, merge_csvs
from .workflows.restore import append_restore_log, classify_dump, run_restore


def _os_open(path: str) -> dict:
    """Open a file or folder in the OS default handler. Returns {ok, ...}."""
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False, creationflags=_NO_WINDOW)
        return {"ok": True, "path": path}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e), "path": path}


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"

ORG_NAME = "Catholic Caritas Foundation of Nigeria"

# Raw [database] profile_label -> semantic profile class used by the frontend to
# pick a banner color token. Mirrors the legacy Tk _DB_PROFILE_COLORS mapping.
_PROFILE_CLASS = {
    "PROD": "prod", "PRODUCTION": "prod",
    "STAGING": "staging", "STAGE": "staging", "UAT": "staging",
    "TEST": "test", "DEV": "test",
    "LOCAL": "local",
}


def profile_class(profile_label: str) -> str:
    """Map a raw profile label to one of: prod | staging | test | local | unlabeled."""
    return _PROFILE_CLASS.get((profile_label or "").strip().upper(), "unlabeled")


class Api:
    """The js_api object handed to webview.create_window(js_api=...).

    Holds the loaded config and a reference to the window (set after creation)
    so later phases can push events via evaluate_js. Loading config here mirrors
    the Tk app's __init__: a load failure is surfaced to the UI rather than
    crashing the process.
    """

    def __init__(self) -> None:
        self._window = None
        self._authenticated = False
        self._log = get_logger()
        self._log_push_on = False
        self._op_seq = 0
        self._restore_cancels: dict = {}  # operation_id -> threading.Event
        self.config = None
        self.config_error: Optional[str] = None
        # Restart survival: seed the ring buffer from APPLICATION_LOG_FILE so the
        # Activity Log shows recent history immediately after launch.
        self._log.load_recent_from_disk()
        try:
            self.config = load_config()
        except Exception as e:  # noqa: BLE001 — surfaced to the UI verbatim
            self.config_error = str(e)
            self._log.emit(f"config load failed: {e}", category="APP", level="error")

    def set_window(self, window) -> None:
        self._window = window
        # Register the single live-tail push subscriber. It forwards every event
        # to the frontend's window.__onLogEvent; the frontend filters client-side.
        self._log.subscribe(self._push_log_event)

    def _push_log_event(self, event: dict) -> None:
        """AppLogger subscriber: deliver one event to the live Activity Log.

        Best-effort and non-fatal: if the window isn't up or the page hasn't
        defined the handler yet, the event is still in the ring (the frontend
        pulls history via log_tail on mount), so nothing is lost.
        """
        if self._window is None or not self._log_push_on:
            return
        try:
            payload = json.dumps(event)
            self._window.evaluate_js(
                f"window.__onLogEvent && window.__onLogEvent({payload})"
            )
        except Exception:
            pass

    def _push_op_event(self, event: dict) -> None:
        """Push a long-running-operation progress/completion event to the
        frontend. Components subscribe by operation_id (section 7)."""
        if self._window is None:
            return
        try:
            self._window.evaluate_js(
                f"window.__onOpEvent && window.__onOpEvent({json.dumps(event)})"
            )
        except Exception:
            pass

    def _next_op_id(self) -> str:
        self._op_seq += 1
        return f"op{self._op_seq}"

    # -- auth ------------------------------------------------------------

    def auth_status(self) -> dict:
        """Tell the login screen what gate (if any) to render.

        Returns whether a config loaded, whether an admin password is set, and
        the app identity for the branded header.
        """
        if self.config is None:
            return {
                "ok": False,
                "config_error": self.config_error,
                "password_required": False,
                "app_name": APP_NAME,
                "app_version": APP_VERSION,
                "org": ORG_NAME,
            }
        return {
            "ok": True,
            "config_error": None,
            "password_required": admin_password_configured(self.config),
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "org": ORG_NAME,
        }

    def auth_login(self, password: str = "") -> dict:
        """Verify the login password against [settings] admin_password.

        If no admin password is configured, login is open (the legacy Tk app
        skipped the gate entirely in that case). Returns {ok, message}.
        """
        if self.config is None:
            return {"ok": False, "message": self.config_error or "No configuration loaded."}
        if not admin_password_configured(self.config):
            self._authenticated = True
            self._log.emit("login: no password gate — entering app", category="UI")
            return {"ok": True, "message": "No password configured."}
        if verify_admin_password(self.config, password):
            self._authenticated = True
            self._log.emit("login: success", category="UI")
            return {"ok": True, "message": "Welcome."}
        # Never log the attempted password (the logger would redact it anyway).
        self._log.emit("login: incorrect password", category="UI", level="warn")
        return {"ok": False, "message": "Incorrect password."}

    # -- config ----------------------------------------------------------

    def config_get_summary(self) -> dict:
        """Summary for the AppShell header + DB profile banner.

        The banner is a safety affordance: profile_class drives a prominent,
        color-coded full-width banner so operators never mistake PROD for LOCAL.
        """
        if self.config is None:
            return {"ok": False, "config_error": self.config_error}
        db = self.config["database"]
        raw_profile = (db.get("profile_label", "") or "").strip()
        return {
            "ok": True,
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "org": ORG_NAME,
            "db_profile": raw_profile.upper(),
            "db_label": raw_profile.upper() or "UNLABELED",
            "profile_class": profile_class(raw_profile),
            "host": db.get("host", "?"),
            "port": db.get("port", "3306"),
            "db_name": db.get("database", "?"),
            "user": db.get("user", "?"),
            "config_path": str(_config_mod.LOADED_CONFIG_PATH or ""),
        }

    # -- activity log ----------------------------------------------------

    def log_subscribe(self, categories=None) -> dict:
        """Enable the live tail. Pushes every subsequent event to the frontend
        via window.__onLogEvent (the frontend filters by category client-side).
        Returns a stream_id for symmetry with the bridge contract (section 7)."""
        self._log_push_on = True
        return {"ok": True, "stream_id": "activity-log"}

    def log_unsubscribe(self) -> dict:
        self._log_push_on = False
        return {"ok": True}

    def log_tail(self, n: int = 1000, categories=None, levels=None) -> list:
        """Recent ring-buffer events for the initial render + restart survival."""
        return self._log.tail(n=n, categories=categories, level=levels)

    def log_search(self, query: str = "", filters: Optional[dict] = None) -> list:
        """Filtered/searched events. filters: {categories, levels, since, until}."""
        f = filters or {}
        return self._log.search(
            query or "",
            categories=f.get("categories"),
            level=f.get("levels"),
            since=f.get("since"),
            until=f.get("until"),
        )

    def log_export(self, filters: Optional[dict] = None, query: str = "") -> dict:
        """Open a Save dialog and write the currently-filtered slice to .txt.

        The slice is computed server-side from the same filters the UI applied,
        so the exported file matches the visible subset exactly.
        """
        if self._window is None:
            return {"ok": False, "message": "No window."}
        import webview  # lazy
        from datetime import datetime
        default = f"nmrs_activity_{datetime.now().strftime('%Y-%m-%d_%H%M')}.txt"
        result = self._window.create_file_dialog(
            webview.SAVE_DIALOG, save_filename=default
        )
        if not result:
            return {"ok": False, "cancelled": True}
        path = result[0] if isinstance(result, (list, tuple)) else result
        f = filters or {}
        res = self._log.export_filtered(
            path,
            categories=f.get("categories"),
            level=f.get("levels"),
            since=f.get("since"),
            until=f.get("until"),
            query=query or None,
        )
        res["path"] = str(path)
        self._log.emit(f"exported {res['lines']} log line(s) -> {path}", category="UI")
        return res

    def log_open_in_editor(self) -> dict:
        """Open APPLICATION_LOG_FILE in the OS default text editor."""
        return _os_open(str(APPLICATION_LOG_FILE))

    def log_disk_info(self) -> dict:
        info = self._log.disk_info()
        info["size_human"] = _human_bytes(info.get("size_bytes", 0))
        info["name"] = APPLICATION_LOG_FILE.name
        return info

    def log_rotate_now(self) -> dict:
        res = self._log.rotate_now()
        self._log.emit("activity log rotated manually", category="UI")
        return res

    # -- backup ----------------------------------------------------------

    def backup_list_facilities(self) -> dict:
        """Per-facility backup status derived from BACKUP_DIR (no DB call).

        Groups *nmrs_backup_*.sql.gz.enc files by facility slug, takes the most
        recent per facility, and classifies by age: <24h encrypted, 24-48h
        stale24, >=48h stale48. Also returns the summary the stat tiles show.
        """
        rows_by_fac: dict = {}
        total_bytes = 0
        if BACKUP_DIR.exists():
            for p in BACKUP_DIR.glob("*nmrs_backup_*.sql.gz.enc"):
                try:
                    st = p.stat()
                except OSError:
                    continue
                total_bytes += st.st_size
                # Facility slug is the prefix before "_nmrs_backup_". Legacy
                # files named "nmrs_backup_*" (no facility prefix) group under
                # "unknown_facility" rather than showing the raw filename.
                fac = (p.name.split("_nmrs_backup_")[0]
                       if "_nmrs_backup_" in p.name else "unknown_facility")
                cur = rows_by_fac.get(fac)
                if cur is None or st.st_mtime > cur["_mtime"]:
                    rows_by_fac[fac] = {"_mtime": st.st_mtime, "size": st.st_size,
                                        "file": p.name}
        now = time.time()
        facilities = []
        for fac, info in rows_by_fac.items():
            age_h = (now - info["_mtime"]) / 3600.0
            status = ("encrypted" if age_h < 24
                      else "stale24" if age_h < 48 else "stale48")
            facilities.append({
                "facility": fac,
                "last_run_epoch": info["_mtime"],
                "last_run_iso": datetime.fromtimestamp(info["_mtime"]).isoformat(timespec="seconds"),
                "size_bytes": info["size"],
                "size_human": _human_bytes(info["size"]),
                "status": status,
                "file": info["file"],
            })
        # If nothing on disk yet, show the configured facility as "Never" (no DB
        # lookup — uses the [settings] facility_name override if present).
        if not facilities and self.config is not None:
            override = self.config.get("settings", "facility_name", fallback="").strip()
            if override:
                facilities.append({
                    "facility": override, "last_run_epoch": None, "last_run_iso": None,
                    "size_bytes": 0, "size_human": "—", "status": "never", "file": None,
                })
        facilities.sort(key=lambda r: r["facility"].lower())
        fresh = sum(1 for r in facilities if r["status"] == "encrypted")
        last = max((r["last_run_epoch"] for r in facilities
                    if r["last_run_epoch"]), default=None)
        return {
            "ok": True,
            "facilities": facilities,
            "total": len(facilities),
            "fresh": fresh,
            "total_bytes": total_bytes,
            "total_human": _human_bytes(total_bytes),
            "last_run_epoch": last,
            "last_run_iso": (datetime.fromtimestamp(last).isoformat(timespec="seconds")
                             if last else None),
            "encryption": "AES-GCM",
            "backup_dir": str(BACKUP_DIR),
        }

    def backup_run_now(self) -> dict:
        """Start a manual backup on a worker thread (force=True, same as the
        legacy 'Backup Now'). Returns immediately with an operation_id; progress
        flows through the AppLogger (category BACKUP) and a completion op event."""
        if self.config is None:
            return {"ok": False, "message": self.config_error or "No configuration."}
        op_id = self._next_op_id()
        threading.Thread(target=self._backup_worker, args=(op_id,),
                         daemon=True).start()
        return {"ok": True, "operation_id": op_id}

    def _backup_worker(self, op_id: str) -> None:
        t0 = time.monotonic()
        self._log.emit("manual backup starting", category="BACKUP", operation_id=op_id)
        try:
            # Identical call to v1.2.0's manual backup: perform_backup(force=True).
            out = perform_backup(self.config, log_func=append_backup_log, force=True)
            append_backup_log(f"[BACKUP] OK {out} (manual)")
            self._push_op_event({
                "operation_id": op_id, "op": "backup", "ok": True,
                "message": f"Wrote {Path(out).name}",
                "path": str(out), "elapsed": round(time.monotonic() - t0, 1),
            })
        except Exception as e:  # noqa: BLE001
            append_backup_log(f"[BACKUP] FAIL {e} (manual)")
            self._push_op_event({
                "operation_id": op_id, "op": "backup", "ok": False,
                "message": str(e), "elapsed": round(time.monotonic() - t0, 1),
            })

    def backup_open_folder(self) -> dict:
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return _os_open(str(BACKUP_DIR))

    def backup_update_schedules(self) -> dict:
        """Re-register OS scheduler jobs (wraps the legacy install_schedules)."""
        if self.config is None:
            return {"ok": False, "message": self.config_error or "No configuration."}
        try:
            binary = (Path(sys.executable).resolve() if getattr(sys, "frozen", False)
                      else Path(sys.argv[0]).resolve())

            def _on(section, key):
                return (self.config.get(section, key, fallback="true").strip().lower()
                        in ("true", "yes", "1"))

            msg = install_schedules(binary, backup=_on("backup", "enabled"),
                                    linelist=_on("linelist", "auto_enabled"))
            SCHEDULE_MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
            SCHEDULE_MARKER_FILE.write_text(
                f"installed_at={datetime.now().isoformat()}\n"
                f"schedule_version={SCHEDULE_VERSION}\n{msg}\n"
            )
            self._log.emit(f"schedules updated: {msg}", category="SCHED")
            return {"ok": True, "message": msg, "status": schedule_status()}
        except Exception as e:  # noqa: BLE001
            self._log.emit(f"schedule update failed: {e}", category="SCHED", level="error")
            return {"ok": False, "message": str(e)}

    def backup_schedule_status(self) -> dict:
        try:
            return {"ok": True, "status": schedule_status()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}

    # -- restore (highest-risk workflow) ---------------------------------

    def restore_pick_file(self) -> dict:
        """Open a file-open dialog accepting the dump formats v1.2.0 accepts."""
        if self._window is None:
            return {"ok": False, "message": "No window."}
        import webview  # lazy
        file_types = (
            "NMRS dumps (*.sql.gz.enc;*.sql.gz;*.sql.zip;*.zip;*.sql)",
            "All files (*.*)",
        )
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False, file_types=file_types)
        if not result:
            return {"ok": False, "cancelled": True}
        path = result[0] if isinstance(result, (list, tuple)) else result
        return {"ok": True, "path": str(path)}

    def restore_preview(self, dump_path: str) -> dict:
        """Classify the selected dump for the Restore tab UI (format/encrypted/size)."""
        try:
            p = Path(dump_path)
            if not p.exists():
                return {"ok": False, "message": "File not found."}
            info = classify_dump(p)
            info.update({
                "ok": True,
                "name": p.name,
                "size_human": _human_bytes(info["size_bytes"]),
                "default_target": (self.config.get("database", "database", fallback="openmrs")
                                   if self.config is not None else "openmrs"),
            })
            return info
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}

    def restore_run(self, dump_path: str, target_db: str, key_hex: str,
                    typed_confirmation: str) -> dict:
        """Start a restore on a worker thread. Returns an operation_id.

        Hard gates enforced HERE (in addition to the frontend disabling RESTORE):
        the dump must exist, a target name is required, and typed_confirmation
        must equal target_db exactly — otherwise nothing is started.
        """
        if self.config is None:
            return {"ok": False, "message": self.config_error or "No configuration."}
        target_db = (target_db or "").strip()
        dump_path = (dump_path or "").strip()
        if not dump_path or not Path(dump_path).exists():
            return {"ok": False, "message": "Dump file not found."}
        if not target_db:
            return {"ok": False, "message": "Target database name is required."}
        if (typed_confirmation or "").strip() != target_db:
            return {"ok": False, "message": "Typed confirmation does not match the target name."}

        op_id = self._next_op_id()
        cancel_event = threading.Event()
        self._restore_cancels[op_id] = cancel_event
        threading.Thread(
            target=self._restore_worker,
            args=(op_id, dump_path, target_db, key_hex, typed_confirmation, cancel_event),
            daemon=True).start()
        return {"ok": True, "operation_id": op_id}

    def _restore_worker(self, op_id, dump_path, target_db, key_hex,
                        typed_confirmation, cancel_event) -> None:
        def status_func(text):
            self._push_op_event({"operation_id": op_id, "op": "restore",
                                 "event": "status", "status": text})

        def progress_func(pct):
            self._push_op_event({"operation_id": op_id, "op": "restore",
                                 "event": "progress", "pct": pct})

        append_restore_log("=" * 60)
        append_restore_log(f"Restore requested: src={dump_path}  target={target_db}")
        try:
            res = run_restore(
                self.config, dump_path, target_db, key_hex, typed_confirmation,
                cancel_event=cancel_event, log_func=append_restore_log,
                status_func=status_func, progress_func=progress_func)
            self._push_op_event({"operation_id": op_id, "op": "restore",
                                 "event": "done", "ok": True,
                                 "message": f"Restored into '{target_db}' in "
                                            f"{res['elapsed']/60:.1f} min"})
        except InterruptedError as e:
            self._log.emit(f"CANCELLED: {e}", category="RESTORE", level="warn")
            self._push_op_event({"operation_id": op_id, "op": "restore",
                                 "event": "cancelled", "message": str(e)})
        except Exception as e:  # noqa: BLE001
            self._log.emit(f"FAILED: {e}", category="RESTORE", level="error")
            self._push_op_event({"operation_id": op_id, "op": "restore",
                                 "event": "error", "ok": False, "message": str(e)})
        finally:
            self._restore_cancels.pop(op_id, None)

    def restore_cancel(self, operation_id: str) -> dict:
        ev = self._restore_cancels.get(operation_id)
        if ev is None:
            return {"ok": False, "message": "No such operation."}
        ev.set()
        self._log.emit("cancel requested — will stop after current chunk",
                       category="RESTORE", level="warn")
        return {"ok": True}

    # -- linelists -------------------------------------------------------

    def linelist_list_bundled(self) -> dict:
        """Curated linelists present on disk (LINELIST_REGISTRY ∩ scripts/),
        plus the weekly-batch count and the output directory."""
        return {
            "ok": True,
            "scripts": [{"name": display, "filename": path.name}
                        for display, path in bundled_scripts()],
            "batch_count": len(batch_linelists()),
            "linelist_dir": str(LINELIST_DIR),
        }

    def linelist_pick_custom(self) -> dict:
        """Open a file-open dialog for a custom .sql script."""
        if self._window is None:
            return {"ok": False, "message": "No window."}
        import webview  # lazy
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("SQL scripts (*.sql)", "All files (*.*)"))
        if not result:
            return {"ok": False, "cancelled": True}
        path = result[0] if isinstance(result, (list, tuple)) else result
        return {"ok": True, "path": str(path), "stem": Path(path).stem}

    def _resolve_linelist_source(self, source: dict):
        """Return (display_name, Path) for the selected source, or raise."""
        if (source or {}).get("type") == "custom":
            p = Path(source.get("path", ""))
            if not p.exists():
                raise FileNotFoundError(f"Custom script not found: {p}")
            return (p.stem, p)
        name = (source or {}).get("name", "")
        for display, path in bundled_scripts():
            if display == name:
                return (display, path)
        raise ValueError(f"Bundled script not found: {name!r}")

    def linelist_run(self, source: dict, output_name: str, encrypt: bool) -> dict:
        """Run a single linelist (bundled or custom) and write the CSV to
        LINELIST_DIR/<output_name>. Same execution + CSV writer as v1.2.0."""
        if self.config is None:
            return {"ok": False, "message": self.config_error or "No configuration."}
        try:
            display, path = self._resolve_linelist_source(source)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}
        output_name = (output_name or "").strip()
        if not output_name:
            return {"ok": False, "message": "Output filename is required."}
        op_id = self._next_op_id()
        threading.Thread(target=self._linelist_worker,
                         args=(op_id, display, path, output_name, bool(encrypt)),
                         daemon=True).start()
        return {"ok": True, "operation_id": op_id}

    def _linelist_worker(self, op_id, display, sql_path, output_name, encrypt):
        t0 = time.monotonic()
        target = LINELIST_DIR / Path(output_name).name
        append_linelist_log(f"Running '{display}' from {sql_path}")
        append_linelist_log(f"Target: {target}  encrypt={encrypt}")
        try:
            sql = sql_path.read_text(encoding="utf-8")
            cols, rows, stmt_count = execute_sql_script(self.config["database"], sql)
            elapsed = time.monotonic() - t0
            append_linelist_log(
                f"Executed {stmt_count} statement(s) in {elapsed:.1f}s; "
                f"final result set: {len(rows)} row(s), {len(cols)} column(s)")
            # Skip the write entirely when there are no rows (legacy behavior).
            if not rows:
                append_linelist_log("Result set is empty — no output file written.")
                self._push_op_event({"operation_id": op_id, "op": "linelist",
                                     "event": "done", "ok": True, "rows": 0,
                                     "message": "0 rows — no file written"})
                return
            LINELIST_DIR.mkdir(parents=True, exist_ok=True)
            key = get_facility_key(self.config) if encrypt else None
            size = write_linelist_csv(cols, rows, target, encrypt, key)
            append_linelist_log(f"Wrote {size:,} bytes -> {target}")
            self._push_op_event({"operation_id": op_id, "op": "linelist",
                                 "event": "done", "ok": True, "rows": len(rows),
                                 "path": str(target),
                                 "message": f"{len(rows)} row(s) -> {target.name}"})
        except Exception as e:  # noqa: BLE001
            append_linelist_log(f"FAILED: {e}")
            self._push_op_event({"operation_id": op_id, "op": "linelist",
                                 "event": "error", "ok": False, "message": str(e)})

    def linelist_run_weekly_batch(self, encrypt: bool) -> dict:
        """Generate the weekly batch (same set as run_headless_linelists)."""
        if self.config is None:
            return {"ok": False, "message": self.config_error or "No configuration."}
        if not batch_linelists():
            return {"ok": False, "message": "No weekly batch linelists are bundled."}
        op_id = self._next_op_id()
        threading.Thread(target=self._linelist_batch_worker,
                         args=(op_id, bool(encrypt)), daemon=True).start()
        return {"ok": True, "operation_id": op_id}

    def _linelist_batch_worker(self, op_id, encrypt):
        try:
            result = perform_linelist_batch(self.config, log_func=append_linelist_log,
                                            encrypt=encrypt)
            written, failed = result["written"], result["failed"]
            skipped = result.get("skipped", [])
            self._push_op_event({
                "operation_id": op_id, "op": "linelist", "event": "done",
                "ok": not (failed and not written),
                "written": len(written), "skipped": len(skipped), "failed": len(failed),
                "message": f"{len(written)} written, {len(skipped)} skipped, "
                           f"{len(failed)} failed",
            })
        except Exception as e:  # noqa: BLE001
            append_linelist_log(f"Batch aborted: {e}")
            self._push_op_event({"operation_id": op_id, "op": "linelist",
                                 "event": "error", "ok": False, "message": str(e)})

    def linelist_open_folder(self) -> dict:
        try:
            LINELIST_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return _os_open(str(LINELIST_DIR))

    # -- merge -----------------------------------------------------------

    @staticmethod
    def _validate_merge_paths(paths) -> dict:
        accepted, rejected = [], []
        for p in paths or []:
            path = Path(p)
            if path.exists() and path.is_file():
                accepted.append({"path": str(path), "name": path.name})
            else:
                rejected.append({"path": str(p), "reason": "not found"})
        return {"ok": True, "accepted": accepted, "rejected": rejected}

    def merge_pick_files(self) -> dict:
        """Open a multi-select file dialog for CSV / encrypted-CSV inputs."""
        if self._window is None:
            return {"ok": False, "message": "No window."}
        import webview  # lazy
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=("CSV / encrypted CSV (*.csv;*.csv.nmrs;*.nmrs)", "All files (*.*)"))
        if not result:
            return {"ok": False, "cancelled": True}
        paths = list(result) if isinstance(result, (list, tuple)) else [result]
        return self._validate_merge_paths(paths)

    def merge_add_files(self, paths) -> dict:
        """Validate paths added by drag-and-drop (where the webview exposes them)."""
        return self._validate_merge_paths(paths)

    def merge_pick_output(self, suggested_name: str = "merged.csv") -> dict:
        """Open a Save dialog for the merged output (legacy let the user choose)."""
        if self._window is None:
            return {"ok": False, "message": "No window."}
        import webview  # lazy
        result = self._window.create_file_dialog(
            webview.SAVE_DIALOG, save_filename=suggested_name or "merged.csv")
        if not result:
            return {"ok": False, "cancelled": True}
        path = result[0] if isinstance(result, (list, tuple)) else result
        return {"ok": True, "path": str(path)}

    def merge_run(self, file_paths, sort_col: str, descending: bool,
                  output_path: str, encrypt: bool) -> dict:
        """Merge the ordered CSV inputs into output_path. Returns an operation_id."""
        if self.config is None:
            return {"ok": False, "message": self.config_error or "No configuration."}
        if not file_paths:
            return {"ok": False, "message": "Add at least one CSV."}
        if not (output_path or "").strip():
            return {"ok": False, "message": "Output path is required."}
        op_id = self._next_op_id()
        threading.Thread(
            target=self._merge_worker,
            args=(op_id, list(file_paths), Path(output_path),
                  bool(encrypt), (sort_col or "").strip(), bool(descending)),
            daemon=True).start()
        return {"ok": True, "operation_id": op_id}

    def _merge_worker(self, op_id, files, target, encrypt, sort_col, sort_desc):
        t0 = time.monotonic()
        append_merge_log(f"Merging {len(files)} file(s) -> {target}  encrypt={encrypt}")
        try:
            res = merge_csvs(files, target, encrypt, sort_col, sort_desc,
                             self.config, log_func=append_merge_log)
            self._push_op_event({
                "operation_id": op_id, "op": "merge", "event": "done", "ok": True,
                "rows": res["n_rows"], "cols": res["n_cols"], "path": str(target),
                "elapsed": round(time.monotonic() - t0, 1),
                "message": f"{res['n_rows']} row(s), {res['n_cols']} column(s) -> {target.name}",
            })
        except Exception as e:  # noqa: BLE001
            append_merge_log(f"FAILED: {e}")
            self._push_op_event({"operation_id": op_id, "op": "merge",
                                 "event": "error", "ok": False, "message": str(e)})
