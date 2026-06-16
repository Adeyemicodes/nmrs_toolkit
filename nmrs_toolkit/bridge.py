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

import csv
import gzip
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
    admin_password_configured, get_master_secret, load_config,
    load_facility_names, verify_admin_password,
)
from .constants import (
    APP_NAME, APP_VERSION, APPLICATION_LOG_FILE, BACKUP_DIR, LINELIST_DIR,
    SCHEDULE_MARKER_FILE, SCHEDULE_VERSION, _NO_WINDOW, batch_linelists,
    bundled_scripts,
)
from .crypto import (
    CRYPTO_KEY_LEN, decrypt_bytes, derive_facility_key, get_facility_key,
    is_encrypted_file,
)
from .db import db_connect
from .logger import get_logger
from .scheduler import install_schedules, schedule_status
from .workflows.backup import append_backup_log, perform_backup
from .workflows.linelist import (
    append_linelist_log, execute_sql_script, perform_linelist_batch,
    write_linelist_csv,
)
from .workflows.merge import append_merge_log, merge_csvs
from .workflows.restore import append_restore_log, classify_dump, run_restore
from .workflows.unvoid import (
    append_unvoid_log, ensure_unvoid_schema, get_accepted_reasons,
    get_window_seconds, lookup_patient, reverse_one, tokenize_identifiers,
    unvoid_one,
)
from .constants import DASHBOARD_EXPORTS_DIR
from .dashboard import exports as dash_exports
from .dashboard import indicators as dash_indicators
from .dashboard import loader as dash_loader

DECRYPT_PREVIEW_ROWS = 200
_FACILITY_PLACEHOLDER = "— select facility —"

# Dashboard export slug -> (display name, indicator function).
_DASHBOARD_INDICATORS = {
    "ever_enrolled": ("Ever Enrolled", dash_indicators.ever_enrolled),
    "tx_new": ("TX_NEW", dash_indicators.tx_new),
    "tx_curr": ("TX_CURR", dash_indicators.tx_curr),
    "currently_iit": ("Currently IIT", dash_indicators.currently_iit),
    "tx_ml": ("TX_ML", dash_indicators.tx_ml),
    "tx_rtt": ("TX_RTT", dash_indicators.tx_rtt),
    "vl_cascade": ("VL Cascade", dash_indicators.vl_cascade),
    "mmd_distribution": ("MMD Distribution", dash_indicators.mmd_distribution),
    "age_sex_pyramid": ("Age/Sex Pyramid", dash_indicators.age_sex_pyramid),
    "biometric_coverage": ("Biometric Coverage", dash_indicators.biometric_coverage),
    "cohort_flow": ("Cohort Flow", dash_indicators.cohort_flow),
}


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
        self._unvoid_batch: list = []     # validated patient dicts (server-side)
        self._unvoid_skipped: list = []
        self._dashboard_records: list = []   # parsed loader records (cached)
        self._dashboard_sources: list = []   # source CSV name(s)
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
            "ui_flags": self._ui_flags(),
        }

    def _ui_flags(self) -> dict:
        """Config-gated tab visibility (preserves the legacy defaults)."""
        if self.config is None:
            return {"unvoid": False, "reverse": False, "decrypt": False}
        g = self.config.getboolean
        return {
            "unvoid": g("ui", "unvoid_tab_enabled", fallback=True),
            "reverse": g("ui", "reverse_tab_enabled", fallback=False),
            "decrypt": g("ui", "decrypt_tab_enabled", fallback=False),
        }

    def _connect(self):
        db = self.config["database"]
        return db_connect(host=db["host"], user=db["user"], password=db["password"],
                          database=db["database"], port=int(db.get("port", 3306)))

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

    # -- native file dialogs --------------------------------------------

    def _open_file(self, file_types, multiple=False) -> dict:
        """Shared OPEN dialog. Returns {ok, path} (or {ok, paths} when multiple),
        {cancelled} on dismiss, or {ok: False, message} on error — so a dialog
        failure never silently rejects the JS promise (a dead Browse button)."""
        if self._window is None:
            return {"ok": False, "message": "No window."}
        try:
            import webview  # lazy
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=multiple, file_types=file_types)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"File dialog failed: {e}"}
        if not result:
            return {"ok": False, "cancelled": True}
        paths = list(result) if isinstance(result, (list, tuple)) else [result]
        if multiple:
            return {"ok": True, "paths": [str(p) for p in paths]}
        return {"ok": True, "path": str(paths[0])}

    def _open_folder(self) -> dict:
        """Shared FOLDER picker. Returns {ok, path} / {cancelled} / {ok:False}."""
        if self._window is None:
            return {"ok": False, "message": "No window."}
        try:
            import webview  # lazy
            result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"Folder dialog failed: {e}"}
        if not result:
            return {"ok": False, "cancelled": True}
        path = result[0] if isinstance(result, (list, tuple)) else result
        return {"ok": True, "path": str(path)}

    # -- restore (highest-risk workflow) ---------------------------------

    def restore_pick_file(self) -> dict:
        """Open a file-open dialog accepting the dump formats v1.2.0 accepts.

        file_types use single-extension suffix globs (*.enc matches .sql.gz.enc):
        pywebview's validator rejects multi-dot patterns (*.sql.gz.enc) in any
        but the first filter entry, and rejects non-word chars in descriptions.
        """
        return self._open_file(
            ("Database dumps (*.enc;*.gz;*.zip;*.sql)", "All files (*.*)"))

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
        res = self._open_file(("SQL scripts (*.sql)", "All files (*.*)"))
        if res.get("ok"):
            res["stem"] = Path(res["path"]).stem
        return res

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
        res = self._open_file(
            ("CSV or encrypted CSV (*.csv;*.nmrs)", "All files (*.*)"), multiple=True)
        if not res.get("ok"):
            return res
        return self._validate_merge_paths(res["paths"])

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

    # -- unvoid patient (high-stakes; audited + reversible) --------------

    def unvoid_validate(self, identifiers: str) -> dict:
        """Look up each ART identifier and classify as ready / skipped. Stores
        the validated batch server-side so commit uses the exact validated data
        (no patient state round-trips through JS)."""
        if self.config is None:
            return {"ok": False, "message": self.config_error or "No configuration."}
        idents = tokenize_identifiers(identifiers)
        if not idents:
            return {"ok": False, "message": "Enter one or more ART identifiers."}
        accepted = get_accepted_reasons(self.config)
        window = get_window_seconds(self.config)
        self._unvoid_batch = []
        self._unvoid_skipped = []
        append_unvoid_log(f"Validating {len(idents)} identifier(s)...")
        try:
            conn = self._connect()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"DB connection failed: {e}"}
        cursor = conn.cursor(dictionary=True, buffered=True)
        try:
            for ident in idents:
                patient, skip = lookup_patient(cursor, ident, accepted, window)
                if patient:
                    self._unvoid_batch.append(patient)
                else:
                    self._unvoid_skipped.append((ident, skip))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"Query failed: {e}"}
        finally:
            cursor.close()
            conn.close()
        ready = [{
            "identifier": p["identifier"],
            "patient_id": p["patient_id"],
            "patient_name": p["patient_name"],
            "accepted_reason": p["accepted_reason"],
            "date_voided": str(p["patient_date_voided"]),
            "window_start": str(p["time_start"]),
            "window_end": str(p["time_end"]),
            "window_seconds": p["window_seconds"],
        } for p in self._unvoid_batch]
        append_unvoid_log(f"Validated: {len(ready)} ready, "
                          f"{len(self._unvoid_skipped)} skipped.")
        return {
            "ok": True,
            "ready": ready,
            "skipped": [{"identifier": i, "reason": r} for i, r in self._unvoid_skipped],
            "total": len(idents),
        }

    def unvoid_commit(self) -> dict:
        """Execute the validated batch (each patient as its own committed txn)."""
        if self.config is None:
            return {"ok": False, "message": self.config_error or "No configuration."}
        if not self._unvoid_batch:
            return {"ok": False, "message": "Nothing validated to unvoid."}
        op_id = self._next_op_id()
        batch = self._unvoid_batch
        self._unvoid_batch = []  # consume so it can't be double-committed
        threading.Thread(target=self._unvoid_worker, args=(op_id, batch),
                         daemon=True).start()
        return {"ok": True, "operation_id": op_id, "count": len(batch)}

    def _unvoid_worker(self, op_id, batch):
        accepted = get_accepted_reasons(self.config)
        admin_name = self.config.get("settings", "admin_name", fallback="Administrator")
        try:
            conn = self._connect()
        except Exception as e:  # noqa: BLE001
            self._push_op_event({"operation_id": op_id, "op": "unvoid",
                                 "event": "error", "ok": False,
                                 "message": f"DB connection failed: {e}"})
            return
        try:
            ddl = conn.cursor()
            try:
                ensure_unvoid_schema(ddl)
            finally:
                ddl.close()
            append_unvoid_log(f"START batch of {len(batch)} patient(s)")
            succeeded, failed, grand_total = 0, 0, 0
            for p in batch:
                try:
                    uid, total = unvoid_one(conn, p, accepted, admin_name)
                    grand_total += total
                    succeeded += 1
                    append_unvoid_log(f"{p['identifier']}: op_id={uid}, {total} row(s) unvoided")
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    append_unvoid_log(f"{p['identifier']}: FAILED — {e}")
            append_unvoid_log(f"DONE — {succeeded} succeeded, {failed} failed, "
                              f"{grand_total} total row(s)")
            self._push_op_event({
                "operation_id": op_id, "op": "unvoid", "event": "done",
                "ok": failed == 0, "succeeded": succeeded, "failed": failed,
                "rows": grand_total,
                "message": f"{succeeded} succeeded, {failed} failed, {grand_total} row(s) unvoided",
            })
        finally:
            conn.close()

    # -- reverse unvoid (config-gated) ----------------------------------

    def reverse_list(self) -> dict:
        """Reversible UNVOID operations (SUCCESS, not yet reversed)."""
        if self.config is None:
            return {"ok": False, "message": self.config_error or "No configuration."}
        try:
            conn = self._connect()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"DB connection failed: {e}"}
        cursor = conn.cursor(dictionary=True, buffered=True)
        try:
            ensure_unvoid_schema(cursor)
            cursor.execute(
                "SELECT op_id, op_time, identifier, patient_name, rows_affected, "
                "       anchor_date_voided "
                "FROM nmrs_unvoid_op "
                "WHERE op_type = 'UNVOID' AND status = 'SUCCESS' "
                "      AND reversed_op_id IS NULL "
                "ORDER BY op_time DESC")
            rows = cursor.fetchall()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"Query failed: {e}"}
        finally:
            cursor.close()
            conn.close()
        return {"ok": True, "operations": [{
            "op_id": r["op_id"], "op_time": str(r["op_time"]),
            "identifier": r["identifier"], "patient_name": r["patient_name"],
            "rows_affected": r["rows_affected"],
            "anchor_date_voided": str(r["anchor_date_voided"]),
        } for r in rows]}

    def reverse_run(self, op_id: int) -> dict:
        if self.config is None:
            return {"ok": False, "message": self.config_error or "No configuration."}
        try:
            orig = int(op_id)
        except (ValueError, TypeError):
            return {"ok": False, "message": "Invalid operation id."}
        operation_id = self._next_op_id()
        threading.Thread(target=self._reverse_worker, args=(operation_id, orig),
                         daemon=True).start()
        return {"ok": True, "operation_id": operation_id}

    def _reverse_worker(self, operation_id, orig_op_id):
        admin_name = self.config.get("settings", "admin_name", fallback="Administrator")
        try:
            conn = self._connect()
        except Exception as e:  # noqa: BLE001
            self._push_op_event({"operation_id": operation_id, "op": "reverse",
                                 "event": "error", "ok": False,
                                 "message": f"DB connection failed: {e}"})
            return
        try:
            rev_op_id, restored, skipped = reverse_one(conn, orig_op_id, admin_name)
            append_unvoid_log(f"op {orig_op_id} reversed by op {rev_op_id}: "
                              f"{restored} restored, {skipped} skipped")
            self._push_op_event({
                "operation_id": operation_id, "op": "reverse", "event": "done",
                "ok": True, "reverse_op_id": rev_op_id, "restored": restored,
                "skipped": skipped,
                "message": f"op {orig_op_id} reversed: {restored} re-voided, {skipped} skipped",
            })
        except Exception as e:  # noqa: BLE001
            append_unvoid_log(f"op {orig_op_id} FAILED — rolled back: {e}")
            self._push_op_event({"operation_id": operation_id, "op": "reverse",
                                 "event": "error", "ok": False, "message": str(e)})
        finally:
            conn.close()

    # -- decrypt (config-gated) -----------------------------------------

    def decrypt_list_facilities(self) -> dict:
        if self.config is None:
            return {"ok": False, "message": self.config_error or "No configuration."}
        return {"ok": True, "facilities": load_facility_names(self.config),
                "has_master": bool(get_master_secret(self.config)),
                "placeholder": _FACILITY_PLACEHOLDER}

    def decrypt_pick_file(self) -> dict:
        res = self._open_file(("Encrypted files (*.nmrs;*.enc)", "All files (*.*)"))
        if res.get("ok"):
            res["name"] = Path(res["path"]).name
        return res

    def _resolve_decrypt_key(self, key_hex, facility):
        """hex field -> facility-derived (master_secret) -> config backup_key."""
        hex_field = (key_hex or "").strip()
        if hex_field:
            try:
                k = bytes.fromhex(hex_field)
            except ValueError as e:
                raise ValueError(f"Backup key field is not valid hex: {e}")
            if len(k) != CRYPTO_KEY_LEN:
                raise ValueError(f"Backup key must be {CRYPTO_KEY_LEN * 2} hex chars "
                                 f"({CRYPTO_KEY_LEN} bytes); got {len(k)} bytes")
            return k
        chosen = (facility or "").strip()
        if chosen and chosen != _FACILITY_PLACEHOLDER:
            master = get_master_secret(self.config)
            if not master:
                raise RuntimeError("Facility selected but no master_secret is configured.")
            return derive_facility_key(master, chosen)
        return get_facility_key(self.config)

    def _decrypt_bytes_from(self, path: str, key_hex, facility):
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {p}")
        if not is_encrypted_file(p):
            raise ValueError("This file doesn't have the NMRS encryption header.")
        return decrypt_bytes(p.read_bytes(), self._resolve_decrypt_key(key_hex, facility))

    def decrypt_preview(self, path: str, key_hex: str = "", facility: str = "") -> dict:
        try:
            raw = self._decrypt_bytes_from(path, key_hex, facility)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}
        if str(path).lower().endswith(".sql.gz.enc"):
            try:
                sql_bytes = gzip.decompress(raw)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "message": f"Decrypted OK but gunzip failed: {e}"}
            head = sql_bytes[:8192].decode("utf-8", errors="replace")
            self._log.emit(f"previewed SQL dump from {path}", category="UI")
            return {"ok": True, "kind": "sql", "size": len(sql_bytes), "head": head}
        text = raw.decode("utf-8", errors="replace")
        reader = csv.reader(text.splitlines())
        try:
            headers = next(reader)
        except StopIteration:
            return {"ok": True, "kind": "csv", "headers": [], "rows": [], "total": 0}
        rows, total = [], 0
        for row in reader:
            total += 1
            if len(rows) < DECRYPT_PREVIEW_ROWS:
                vals = list(row[:len(headers)])
                vals += [""] * (len(headers) - len(vals))
                rows.append(vals)
        self._log.emit(f"previewed {total} row(s) from {path}", category="UI")
        return {"ok": True, "kind": "csv", "headers": headers, "rows": rows,
                "total": total, "shown": len(rows)}

    def decrypt_save(self, path: str, key_hex: str = "", facility: str = "") -> dict:
        if self._window is None:
            return {"ok": False, "message": "No window."}
        try:
            raw = self._decrypt_bytes_from(path, key_hex, facility)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}
        import webview  # lazy
        name = Path(path).name
        if str(path).lower().endswith(".sql.gz.enc"):
            try:
                out_bytes = gzip.decompress(raw)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "message": f"gunzip failed: {e}"}
            default = name[:-len(".gz.enc")] if name.endswith(".sql.gz.enc") else name + ".sql"
        else:
            out_bytes = raw
            if name.endswith(".csv.nmrs"):
                default = name[:-len(".nmrs")]
            elif name.endswith(".nmrs"):
                default = name[:-len(".nmrs")] + ".csv"
            else:
                default = name + ".csv"
        result = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename=default)
        if not result:
            return {"ok": False, "cancelled": True}
        target = result[0] if isinstance(result, (list, tuple)) else result
        Path(target).write_bytes(out_bytes)
        self._log.emit(f"wrote plaintext -> {target}", category="UI")
        return {"ok": True, "path": str(target), "bytes": len(out_bytes)}

    # -- analytics dashboard --------------------------------------------

    def _dashboard_admin_mode(self) -> bool:
        return (self.config is not None
                and self.config.getboolean("ui", "dashboard_admin_mode", fallback=False))

    def _parse_iso(self, s):
        from datetime import date, datetime
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()

    def dashboard_status(self) -> dict:
        """Initial state for the dashboard tab."""
        latest = dash_loader.latest_linelist()
        gen = None
        if latest is not None:
            from datetime import datetime
            try:
                gen = datetime.fromtimestamp(latest.stat().st_mtime).isoformat(timespec="seconds")
            except OSError:
                gen = None
        return {
            "ok": True,
            "latest_linelist": latest.name if latest else None,
            "generated_at": gen,
            "admin_mode": self._dashboard_admin_mode(),
            "export_dir": str(DASHBOARD_EXPORTS_DIR),
        }

    def dashboard_load_latest(self) -> dict:
        """Load the most recent Treatment linelist into the cache."""
        if self.config is None:
            return {"ok": False, "message": self.config_error or "No configuration."}
        latest = dash_loader.latest_linelist()
        if latest is None:
            self._log.emit("no Treatment linelist found to load", category="DASHBOARD", level="warn")
            return {"ok": False, "message": "No Treatment linelist found in NMRS_Linelists."}
        try:
            key = self._dashboard_key()
            frame = dash_loader.load_linelist(latest, decrypt_key=key)
        except Exception as e:  # noqa: BLE001
            self._log.emit(f"load failed: {e}", category="DASHBOARD", level="error")
            return {"ok": False, "message": str(e)}
        self._dashboard_records = frame.records
        self._dashboard_sources = [frame.source_name]
        self._log.emit(f"loaded {frame.row_count} row(s) from {frame.source_name}",
                       category="DASHBOARD")
        gen = frame.generated_at.isoformat(timespec="seconds") if frame.generated_at else None
        return {"ok": True, "source": frame.source_name, "rows": frame.row_count,
                "facility": frame.facility_name, "generated_at": gen}

    def dashboard_pick_folder(self) -> dict:
        return self._open_folder()

    def dashboard_load_folder(self, folder: str) -> dict:
        """Admin mode: load every Treatment_*.csv* in `folder`."""
        if not self._dashboard_admin_mode():
            return {"ok": False, "message": "Multi-facility mode is disabled (dashboard_admin_mode)."}
        try:
            frames = dash_loader.load_linelist_folder(folder, decrypt_key=self._dashboard_key())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}
        if not frames:
            return {"ok": False, "message": "No Treatment_*.csv files in that folder."}
        self._dashboard_records = dash_loader.concat(frames)
        self._dashboard_sources = [f.source_name for f in frames]
        facilities = sorted({f.facility_name for f in frames})
        self._log.emit(f"loaded {len(frames)} file(s), {len(self._dashboard_records)} rows "
                       f"across {len(facilities)} facility(ies)", category="DASHBOARD")
        return {"ok": True, "sources": self._dashboard_sources, "facilities": facilities,
                "total_rows": len(self._dashboard_records)}

    def _dashboard_key(self):
        """Facility key for decrypting .csv.nmrs linelists, or None if unset."""
        try:
            from .crypto import get_facility_key
            return get_facility_key(self.config)
        except Exception:  # noqa: BLE001
            return None

    def _filtered_records(self, facility_filter):
        if not facility_filter:
            return self._dashboard_records
        wanted = set(facility_filter)
        return [r for r in self._dashboard_records
                if (r.get("_source_facility") or r.get("facility")) in wanted]

    def dashboard_compute(self, start_date: str, end_date: str,
                          facility_filter: list = None) -> dict:
        """Run compute_all against the cached frame for [start, end]."""
        if not self._dashboard_records:
            return {"ok": False, "message": "No linelist loaded."}
        try:
            sd, ed = self._parse_iso(start_date), self._parse_iso(end_date)
        except ValueError:
            return {"ok": False, "message": "Dates must be YYYY-MM-DD."}
        records = self._filtered_records(facility_filter)
        result = dash_indicators.compute_all(
            records, sd, ed,
            meta_extra={"sources": self._dashboard_sources,
                        "facility_filter": facility_filter or []})
        result["ok"] = True
        self._log.emit(f"computed indicators {start_date}..{end_date} "
                       f"({len(records)} rows)", category="DASHBOARD")
        return result

    def dashboard_refresh_from_db(self, end_date: str) -> dict:
        """Regenerate the Treatment linelist at @endDate=end_date, then reload.
        Returns an operation_id; completion is pushed via _push_op_event."""
        if self.config is None:
            return {"ok": False, "message": self.config_error or "No configuration."}
        try:
            self._parse_iso(end_date)
        except ValueError:
            return {"ok": False, "message": "end_date must be YYYY-MM-DD."}
        op_id = self._next_op_id()
        threading.Thread(target=self._dashboard_refresh_worker,
                         args=(op_id, end_date), daemon=True).start()
        return {"ok": True, "operation_id": op_id}

    def _dashboard_refresh_worker(self, op_id, end_date):
        import time as _t
        from .constants import LINELIST_DIR, bundled_scripts
        t0 = _t.monotonic()
        self._log.emit(f"refresh from DB starting (@endDate={end_date})", category="DASHBOARD")
        try:
            treatment = next((p for name, p in bundled_scripts() if name == "Treatment"), None)
            if treatment is None:
                raise RuntimeError("Treatment script not bundled.")
            sql = treatment.read_text(encoding="utf-8")
            # Pre-set @endDate; sql:31 (SET @endDate = IFNULL(@endDate, NOW())) honors it.
            sql = f"SET @endDate = '{end_date} 23:59:59';\n" + sql
            cols, rows, _ = execute_sql_script(self.config["database"], sql)
            stamp = datetime.now().strftime("%Y%m%d%H%M")
            # "asof" name so a snapshot linelist is never mistaken for the current one.
            out = LINELIST_DIR / f"Treatment_asof_{end_date}_{stamp}.csv"
            write_linelist_csv(cols, rows, out, False, None)
            frame = dash_loader.load_linelist(out)
            self._dashboard_records = frame.records
            self._dashboard_sources = [frame.source_name]
            self._log.emit(f"refresh complete: {frame.row_count} row(s) -> {out.name}",
                           category="DASHBOARD")
            self._push_op_event({
                "operation_id": op_id, "op": "dashboard", "event": "done", "ok": True,
                "source": frame.source_name, "rows": frame.row_count,
                "facility": frame.facility_name, "end_date": end_date,
                "elapsed": round(_t.monotonic() - t0, 1),
                "message": f"Regenerated at {end_date}: {frame.row_count} rows",
            })
        except Exception as e:  # noqa: BLE001
            self._log.emit(f"refresh failed: {e}", category="DASHBOARD", level="error")
            self._push_op_event({"operation_id": op_id, "op": "dashboard",
                                 "event": "error", "ok": False, "message": str(e)})

    def dashboard_export(self, indicator_slug: str, start_date: str, end_date: str,
                         facility_filter: list = None) -> dict:
        """Compute one indicator and write a banner-headed export CSV."""
        if not self._dashboard_records:
            return {"ok": False, "message": "No linelist loaded."}
        entry = _DASHBOARD_INDICATORS.get(indicator_slug)
        if entry is None:
            return {"ok": False, "message": f"Unknown indicator: {indicator_slug}"}
        name, fn = entry
        try:
            sd, ed = self._parse_iso(start_date), self._parse_iso(end_date)
        except ValueError:
            return {"ok": False, "message": "Dates must be YYYY-MM-DD."}
        records = self._filtered_records(facility_filter)
        indicator = fn(records, sd, ed)
        res = dash_exports.write_export(indicator_slug, name, start_date, end_date,
                                        self._dashboard_sources, indicator)
        self._log.emit(f"exported {indicator_slug} {start_date}..{end_date} "
                       f"-> {Path(res['path']).name}", category="DASHBOARD")
        return res

    def dashboard_open_exports_folder(self) -> dict:
        try:
            DASHBOARD_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return _os_open(str(DASHBOARD_EXPORTS_DIR))
