"""Legacy Tkinter UI — TRANSITIONAL.

Kept intact during the PyWebView migration so the app stays shippable at every
phase (MIGRATION_PLAN.md section 9). The class body is copied verbatim from the
v1.2.1 single-file build; only the import surface changed (it now pulls from the
split package) and the per-workflow log helpers emit through the unified
AppLogger. This module is removed once the HTML frontend reaches feature parity.
"""
from __future__ import annotations

import configparser
import csv
import gzip
import os
import platform
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from mysql.connector import Error

from . import config as _config_mod
from .config import (
    facilities_file_path, get_admin_password, get_master_secret, load_config,
    load_facility_names, verify_admin_password,
)
from .constants import (
    APP_NAME, APP_VERSION, BACKUP_DIR, BACKUP_LOG_FILE, LINELIST_DIR,
    LINELIST_LOG_FILE, LINELIST_REGISTRY, PRE_RESTORE_DIR, RESTORE_LOG_FILE,
    SCHEDULE_MARKER_FILE, SCHEDULE_VERSION, batch_linelists, bundled_scripts,
    resource_path,
)
from .crypto import (
    CRYPTO_KEY_LEN, CRYPTO_MAGIC, CRYPTO_MAGIC_LEGACY, CRYPTO_NONCE_LEN,
    decrypt_bytes, derive_facility_key, encrypt_bytes, get_facility_key,
    is_encrypted_file,
)
from .db import _mysql_admin, _mysql_db_exists, db_connect
from .logger import get_logger
from .scheduler import install_schedules, schedule_status
from .workflows.backup import append_backup_log, perform_backup
from .workflows.linelist import (
    _linelist_output_path, append_linelist_log, execute_sql_script,
    linelist_rows_to_csv_bytes, perform_linelist_batch, write_linelist_csv,
)
from .workflows.restore import (
    _classify_dump_file, _decrypt_to_sql_file, _shred_unlink, _sql_sanity_check,
    append_restore_log, perform_pre_restore_backup,
)


def launch() -> None:
    """Open the Tkinter GUI (used by __main__ when no headless flag is given)."""
    root = tk.Tk()
    NMRSToolkitApp(root)
    root.mainloop()


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
        # Sized for a comfortable first launch on a 1366×768 (and up) facility
        # laptop; min size keeps every tab + the activity log usable at the
        # smallest the user can drag the window to. Resizable in both axes —
        # the previous resizable(False, False) was why the log got clipped
        # whenever the chosen geometry didn't fit the host screen.
        self.root.geometry("1150x820")
        self.root.minsize(950, 700)

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
        return get_admin_password(self.config)

    def _enter_app(self):
        self.authenticated = True
        self.show_main_screen()
        self._maybe_install_schedule_on_first_launch()

    def _check_password(self):
        if verify_admin_password(self.config, self.password_entry.get()):
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

        # Pack BOTTOM-UP so the activity log is guaranteed to be visible at any
        # window size: pack reserves space in declaration order, so packing the
        # log first with side="bottom" anchors it to the bottom edge with its
        # full requested height, and the notebook then fills everything above
        # with expand=True. (A previous attempt to use ttk.PanedWindow here
        # hid the log entirely because weight= only governs how extra space is
        # distributed on resize — it does NOT set the initial sash position,
        # which Tk decides from pane reqheights. The notebook's reqheight
        # dwarfed the log's, so the sash defaulted to the bottom edge.)
        log_frame = tk.LabelFrame(self.root, text="Activity Log",
                                  font=("Arial", 10, "bold"), padx=10, pady=6)
        log_frame.pack(side="bottom", fill="x", padx=24, pady=(0, 12))

        log_bar = tk.Frame(log_frame)
        log_bar.pack(side="top", fill="x", pady=(0, 4))
        tk.Button(log_bar, text="Clear", command=self._clear_log,
                  bg="#9e9e9e", fg="white", font=("Arial", 8, "bold"),
                  padx=8, pady=1, cursor="hand2").pack(side="right")

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=8, font=("Courier", 9),
            bg="#1e1e1e", fg="#dcdcdc", insertbackground="white",
        )
        self.log_text.pack(fill="both", expand=True)

        nb_frame = tk.Frame(self.root, padx=24, pady=10)
        nb_frame.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(nb_frame)
        self.notebook.pack(fill="both", expand=True)

        self._safe_add_tab("Linelists", self._build_linelist_tab)
        self._safe_add_tab("Merge Reports", self._build_merge_tab)
        self._safe_add_tab("Backup", self._build_backup_tab)
        self._safe_add_tab("Restore", self._build_restore_tab)
        if self.config.getboolean("ui", "unvoid_tab_enabled", fallback=True):
            self._safe_add_tab("Unvoid Patient", self._build_unvoid_tab)
        if self.config.getboolean("ui", "reverse_tab_enabled", fallback=False):
            self._safe_add_tab("Reverse Unvoid", self._build_reverse_tab)
        if self.config.getboolean("ui", "decrypt_tab_enabled", fallback=False):
            self._safe_add_tab("Decrypt", self._build_decrypt_tab)

        self.log(f"{APP_NAME} v{APP_VERSION} ready.")
        db = self.config["database"]
        self.log(f"Config loaded from: {_config_mod.LOADED_CONFIG_PATH}")
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
                f"Using credentials from:\n  {_config_mod.LOADED_CONFIG_PATH}\n"
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

    def _clear_log(self):
        """Wipe the activity log pane. Bound to the small 'Clear' button in
        the log header — useful between runs to keep the pane focused on the
        current task. The on-disk backup/linelist logs are untouched."""
        try:
            self.log_text.config(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.config(state="disabled")
        except tk.TclError:
            pass

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
        # Persist GUI linelist runs (legacy code logged these to the in-memory
        # widget only). Routes to APPLICATION_LOG_FILE + linelist.log.
        get_logger().emit(msg, category="LINELIST")

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
        # Persist GUI merge runs (legacy code logged these to the in-memory
        # widget only). MERGE has no per-workflow file, so this lands in
        # APPLICATION_LOG_FILE — first time merge activity is durably recorded.
        get_logger().emit(msg, category="MERGE")

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
            get_logger().emit(f"re-installed: {msg}", category="SCHED")
            self.backup_schedule_label.config(text=f"Schedule:  {schedule_status()}")
            messagebox.showinfo("Schedules Installed", msg)
        except Exception as e:
            get_logger().emit(f"schedule install failed: {e}", category="SCHED", level="error")
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

    # ====================================================================
    # Unvoid Patient  (Stage 1: schema + single-client unvoid)
    # --------------------------------------------------------------------
    # Reverses an erroneous patient void. Safety model:
    #   * Gate on the `patient` row's void_reason — only reasons in the
    #     configured accepted set may be unvoided (default: the ART/DATIM
    #     bulk-void reason plus "Duplicate Client").
    #   * Anchor on that row's date_voided; unvoid every timestamp-bearing
    #     table within ±window_seconds of it (default 120s). Only the most
    #     recent void cluster is ever touched.
    #   * Sensitive identity tables (person_name/address/attribute) often
    #     lack a reliable date_voided, so we unvoid only their single most
    #     recent voided row to avoid resurrecting duplicates.
    #   * Every mutated row is logged to nmrs_unvoid_op_row with its prior
    #     void state BEFORE the update, so the Reverse tab (Stage 2) can
    #     re-void exactly those rows.
    # ====================================================================

    # Timestamp-windowed tables: (table, pk_column, key_column). key_column
    # holds the patient/person id; for patients person_id == patient_id.
    _UNVOID_WINDOW_TABLES = [
        ("patient_identifier", "patient_identifier_id", "patient_id"),
        ("patient_program",    "patient_program_id",    "patient_id"),
        ("person",             "person_id",             "person_id"),
        ("visit",              "visit_id",              "patient_id"),
        ("encounter",          "encounter_id",          "patient_id"),
        ("obs",                "obs_id",                "person_id"),
    ]
    # Sensitive identity tables: unvoid most-recent voided row only.
    _UNVOID_IDENTITY_TABLES = [
        ("person_name",      "person_name_id",      "person_id"),
        ("person_address",   "person_address_id",   "person_id"),
        ("person_attribute", "person_attribute_id", "person_id"),
    ]

    def _unvoid_accepted_reasons(self):
        raw = self.config.get(
            "settings", "unvoid_accepted_reasons",
            fallback="Bulk void via ART/DATIM mapping, Duplicate Client",
        )
        return [r.strip() for r in raw.split(",") if r.strip()]

    def _unvoid_window_seconds(self):
        try:
            return int(self.config.get("settings", "unvoid_window_seconds",
                                       fallback="120"))
        except (ValueError, TypeError):
            return 120

    # -- schema ----------------------------------------------------------

    def _ensure_unvoid_schema(self, cursor):
        """Create the reversible-audit tables if absent. CREATE TABLE is DDL
        and auto-commits in MySQL, so call this BEFORE opening the data
        transaction (nothing pending must be lost to the implicit commit)."""
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS nmrs_unvoid_op (
                op_id              INT AUTO_INCREMENT PRIMARY KEY,
                op_time            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                op_type            VARCHAR(10)  NOT NULL,
                identifier         VARCHAR(50)  NOT NULL,
                patient_id         INT          NOT NULL,
                patient_name       VARCHAR(255),
                anchor_date_voided DATETIME,
                window_seconds     INT          NOT NULL,
                accepted_reason    VARCHAR(255),
                executed_by        VARCHAR(100),
                status             VARCHAR(20)  NOT NULL,
                rows_affected      INT          NOT NULL DEFAULT 0,
                reversed_op_id     INT          NULL,
                remarks            TEXT,
                INDEX idx_unvoid_op_patient (patient_id),
                INDEX idx_unvoid_op_identifier (identifier),
                INDEX idx_unvoid_op_time (op_time)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS nmrs_unvoid_op_row (
                row_id            INT AUTO_INCREMENT PRIMARY KEY,
                op_id             INT          NOT NULL,
                table_name        VARCHAR(64)  NOT NULL,
                pk_column         VARCHAR(64)  NOT NULL,
                pk_value          INT          NOT NULL,
                prev_voided       TINYINT,
                prev_date_voided  DATETIME,
                prev_voided_by    INT,
                prev_void_reason  VARCHAR(255),
                INDEX idx_unvoid_row_op (op_id),
                INDEX idx_unvoid_row_table_pk (table_name, pk_value)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )

    # -- UI --------------------------------------------------------------

    def _build_unvoid_tab(self, parent):
        self._unvoid_batch = []    # validated patient dicts ready to unvoid
        self._unvoid_skipped = []  # (identifier, reason) pairs

        content = tk.Frame(parent, padx=20, pady=15)
        content.pack(fill="both", expand=True)

        # Step 1: identifier(s)
        search_frame = tk.LabelFrame(
            content, text="Step 1: Enter Patient Identifier(s)",
            font=("Arial", 11, "bold"), padx=15, pady=15,
        )
        search_frame.pack(fill="x", pady=(0, 12))
        tk.Label(search_frame,
                 text="ART Identifier(s) — single, or comma-separated for batch "
                      "(e.g., IMO01104166, IMO01104167):",
                 font=("Arial", 10)).pack(anchor="w", pady=(0, 5))

        entry_row = tk.Frame(search_frame)
        entry_row.pack(fill="x")
        self._unvoid_id_entry = tk.Entry(entry_row, font=("Arial", 12), width=50,
                                         bd=2, relief="solid")
        self._unvoid_id_entry.pack(side="left", padx=(0, 10))
        self._unvoid_id_entry.bind("<Return>", lambda e: self._unvoid_validate())
        tk.Button(entry_row, text="VALIDATE", command=self._unvoid_validate,
                  bg="#2196F3", fg="white", font=("Arial", 11, "bold"),
                  padx=20, pady=8, cursor="hand2").pack(side="left")

        # Step 2: details
        details_frame = tk.LabelFrame(
            content, text="Step 2: Verify (one block per identifier)",
            font=("Arial", 11, "bold"), padx=15, pady=15,
        )
        details_frame.pack(fill="both", expand=True, pady=(0, 12))
        self._unvoid_details = scrolledtext.ScrolledText(
            details_frame, height=16, font=("Courier", 10),
            bg="#f5f5f5", relief="solid", bd=1,
        )
        self._unvoid_details.pack(fill="both", expand=True)
        self._unvoid_details.config(state="disabled")

        # Step 3: action
        action_frame = tk.LabelFrame(
            content, text="Step 3: Unvoid Patient Records",
            font=("Arial", 11, "bold"), padx=15, pady=15,
        )
        action_frame.pack(fill="x")
        tk.Label(action_frame,
                 text="WARNING: unvoids records within the time window of the "
                      "most recent void only.",
                 font=("Arial", 9), fg="#d32f2f").pack(pady=(0, 10))
        self._unvoid_button = tk.Button(
            action_frame, text="UNVOID PATIENT RECORDS",
            command=self._unvoid_confirm, bg="#cccccc", fg="#666666",
            font=("Arial", 12, "bold"), padx=30, pady=12, cursor="hand2",
            state="disabled", disabledforeground="#666666",
        )
        self._unvoid_button.pack()

    def _unvoid_set_button(self, enabled):
        if enabled:
            self._unvoid_button.config(state="normal", bg="#f44336", fg="white")
        else:
            self._unvoid_button.config(state="disabled", bg="#cccccc", fg="#666666")

    def _unvoid_show_details(self, text):
        self._unvoid_details.config(state="normal")
        self._unvoid_details.delete("1.0", tk.END)
        self._unvoid_details.insert("1.0", text.strip())
        self._unvoid_details.config(state="disabled")

    # -- search / validate ----------------------------------------------

    def _unvoid_tokenize(self, raw):
        """Split on any common separator (comma, newline, tab, semicolon,
        pipe, whitespace), de-duplicate, preserve order."""
        seen = set()
        out = []
        for tok in re.split(r"[,\n\t;|\s]+", raw):
            t = tok.strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def _unvoid_lookup(self, cursor, identifier, accepted, window):
        """Return (patient_dict, None) if unvoidable, else (None, skip_reason)."""
        cursor.execute(
            """
            SELECT pi.patient_id, pi.identifier,
                   CONCAT(pn.given_name, ' ', IFNULL(pn.family_name, '')) AS patient_name,
                   p.gender, p.birthdate,
                   pat.date_voided AS patient_date_voided,
                   pat.void_reason AS patient_void_reason
            FROM patient_identifier pi
            JOIN person p   ON pi.patient_id = p.person_id
            JOIN patient pat ON pi.patient_id = pat.patient_id
            LEFT JOIN person_name pn ON p.person_id = pn.person_id
            WHERE pi.identifier = %s AND pi.voided = 1
            ORDER BY pn.preferred DESC, pn.date_created DESC
            LIMIT 1
            """,
            (identifier,),
        )
        result = cursor.fetchone()
        if not result:
            cursor.execute(
                "SELECT patient_id FROM patient_identifier "
                "WHERE identifier = %s AND voided = 0 LIMIT 1",
                (identifier,),
            )
            if cursor.fetchone():
                return None, "already active (not voided)"
            return None, "not found in database"

        reason = result.get("patient_void_reason")
        if reason not in accepted:
            return None, f"void reason '{reason or 'NULL'}' not in accepted set"
        if not result.get("patient_date_voided"):
            return None, "no date_voided timestamp on patient row"

        anchor = result["patient_date_voided"]
        result["time_start"] = anchor - timedelta(seconds=window)
        result["time_end"] = anchor + timedelta(seconds=window)
        result["window_seconds"] = window
        result["accepted_reason"] = reason
        return result, None

    def _unvoid_validate(self):
        raw = self._unvoid_id_entry.get().strip()
        self._unvoid_batch = []
        self._unvoid_skipped = []
        self._unvoid_set_button(False)

        identifiers = self._unvoid_tokenize(raw)
        if not identifiers:
            messagebox.showwarning("Input Required",
                                   "Please enter one or more ART identifiers.")
            return

        self.log(f"[UNVOID] Validating {len(identifiers)} identifier(s)...")
        conn = self.get_connection()
        if not conn:
            return

        accepted = self._unvoid_accepted_reasons()
        window = self._unvoid_window_seconds()
        cursor = conn.cursor(dictionary=True, buffered=True)
        try:
            for ident in identifiers:
                patient, skip = self._unvoid_lookup(cursor, ident, accepted, window)
                if patient:
                    self._unvoid_batch.append(patient)
                else:
                    self._unvoid_skipped.append((ident, skip))
        except Error as e:
            self.log(f"[UNVOID] Validation failed: {e}")
            messagebox.showerror("Database Error", f"Query failed:\n\n{e}")
            return
        finally:
            cursor.close()

        # Build the preview.
        blocks = []
        for p in self._unvoid_batch:
            blocks.append(
                f"[READY] {p['identifier']}  (patient_id={p['patient_id']})\n"
                f"        Name:    {p['patient_name']}\n"
                f"        Reason:  {p['accepted_reason']}\n"
                f"        Voided:  {p['patient_date_voided']}\n"
                f"        Window:  {p['time_start']} .. {p['time_end']} "
                f"(±{p['window_seconds']}s)"
            )
        for ident, reason in self._unvoid_skipped:
            blocks.append(f"[SKIP]  {ident}  —  {reason}")

        summary = (f"{len(self._unvoid_batch)} ready, "
                   f"{len(self._unvoid_skipped)} skipped "
                   f"of {len(identifiers)} identifier(s).\n"
                   + "-" * 64 + "\n\n")
        self._unvoid_show_details(summary + "\n\n".join(blocks))
        self.log(f"[UNVOID] Validated: {len(self._unvoid_batch)} ready, "
                 f"{len(self._unvoid_skipped)} skipped.")

        if self._unvoid_batch:
            n = len(self._unvoid_batch)
            self._unvoid_button.config(
                text=f"UNVOID {n} PATIENT{'S' if n != 1 else ''}")
            self._unvoid_set_button(True)

    # -- execute ---------------------------------------------------------

    def _unvoid_confirm(self):
        batch = self._unvoid_batch
        if not batch:
            return
        names = "\n".join(f"  • {p['identifier']}  ({p['patient_name']})"
                          for p in batch[:15])
        more = f"\n  … and {len(batch) - 15} more" if len(batch) > 15 else ""
        if not messagebox.askyesno(
            "Confirm Unvoid",
            f"Unvoid {len(batch)} patient(s)?\n\n"
            f"{names}{more}\n\n"
            f"Each patient is unvoided within ±{self._unvoid_window_seconds()}s "
            f"of their most recent void, as its own logged operation.\n"
            f"Failures are skipped, not rolled into others.\n"
            f"This can be reversed by an administrator.\n\n"
            f"Proceed?",
            icon="warning",
        ):
            return
        self._unvoid_execute()

    def _unvoid_capture_and_clear(self, cursor, op_id, table, pk_col, where_sql, params):
        """Capture the prior void state of every row matching `where_sql` into
        nmrs_unvoid_op_row, then unvoid exactly those rows (by captured PK).
        Returns the number of rows unvoided. table/pk_col are internal
        constants (never user input), so f-string interpolation is safe."""
        cursor.execute(
            f"SELECT {pk_col} AS pk, voided, date_voided, voided_by, void_reason "
            f"FROM {table} WHERE {where_sql}",
            params,
        )
        rows = cursor.fetchall()
        if not rows:
            return 0
        for r in rows:
            cursor.execute(
                "INSERT INTO nmrs_unvoid_op_row "
                "(op_id, table_name, pk_column, pk_value, prev_voided, "
                " prev_date_voided, prev_voided_by, prev_void_reason) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (op_id, table, pk_col, r["pk"], r["voided"],
                 r["date_voided"], r["voided_by"], r["void_reason"]),
            )
        pks = [r["pk"] for r in rows]
        placeholders = ",".join(["%s"] * len(pks))
        cursor.execute(
            f"UPDATE {table} SET voided = 0, voided_by = NULL, "
            f"date_voided = NULL, void_reason = NULL "
            f"WHERE {pk_col} IN ({placeholders})",
            pks,
        )
        return cursor.rowcount

    def _unvoid_one(self, conn, p, accepted, admin_name):
        """Unvoid a single patient as its own committed transaction.
        Returns (op_id, rows_unvoided). Raises on failure (caller rolls back)."""
        patient_id = p["patient_id"]
        time_start, time_end = p["time_start"], p["time_end"]
        cursor = conn.cursor(dictionary=True, buffered=True)
        try:
            cursor.execute(
                "INSERT INTO nmrs_unvoid_op "
                "(op_type, identifier, patient_id, patient_name, "
                " anchor_date_voided, window_seconds, accepted_reason, "
                " executed_by, status) "
                "VALUES ('UNVOID', %s, %s, %s, %s, %s, %s, %s, 'IN_PROGRESS')",
                (p["identifier"], patient_id, p["patient_name"],
                 p["patient_date_voided"], p["window_seconds"],
                 p["accepted_reason"], admin_name),
            )
            op_id = cursor.lastrowid
            total = 0

            # 1. patient table — re-check void_reason for safety.
            ph = ",".join(["%s"] * len(accepted))
            total += self._unvoid_capture_and_clear(
                cursor, op_id, "patient", "patient_id",
                f"patient_id = %s AND voided = 1 AND void_reason IN ({ph}) "
                f"AND date_voided BETWEEN %s AND %s",
                (patient_id, *accepted, time_start, time_end),
            )

            # 2. timestamp-windowed tables.
            for table, pk_col, key_col in self._UNVOID_WINDOW_TABLES:
                total += self._unvoid_capture_and_clear(
                    cursor, op_id, table, pk_col,
                    f"{key_col} = %s AND voided = 1 "
                    f"AND date_voided BETWEEN %s AND %s",
                    (patient_id, time_start, time_end),
                )

            # 3. identity tables — most recent voided row only.
            for table, pk_col, key_col in self._UNVOID_IDENTITY_TABLES:
                cursor.execute(
                    f"SELECT {pk_col} AS pk FROM {table} "
                    f"WHERE {key_col} = %s AND voided = 1 "
                    f"ORDER BY COALESCE(date_voided, date_created) DESC LIMIT 1",
                    (patient_id,),
                )
                row = cursor.fetchone()
                if row:
                    total += self._unvoid_capture_and_clear(
                        cursor, op_id, table, pk_col,
                        f"{pk_col} = %s", (row["pk"],),
                    )

            cursor.execute(
                "UPDATE nmrs_unvoid_op SET status = 'SUCCESS', rows_affected = %s, "
                "remarks = %s WHERE op_id = %s",
                (total,
                 f"Unvoided within ±{p['window_seconds']}s of "
                 f"{p['patient_date_voided']}; reason '{p['accepted_reason']}'.",
                 op_id),
            )
            conn.commit()
            return op_id, total
        except Error:
            conn.rollback()
            raise
        finally:
            cursor.close()

    def _unvoid_execute(self):
        batch = self._unvoid_batch
        if not batch:
            return
        conn = self.get_connection()
        if not conn:
            return

        accepted = self._unvoid_accepted_reasons()
        admin_name = self.config.get("settings", "admin_name", fallback="Administrator")

        # DDL first — CREATE TABLE auto-commits, so do it before any data txn.
        ddl_cursor = conn.cursor()
        try:
            self._ensure_unvoid_schema(ddl_cursor)
        finally:
            ddl_cursor.close()

        # UNVOID is an audited, high-stakes workflow; persist it (legacy code
        # logged to the in-memory widget only). Lands in APPLICATION_LOG_FILE.
        log = get_logger()
        self.log("-" * 70)
        self.log(f"[UNVOID] START batch of {len(batch)} patient(s).")
        log.emit(f"START batch of {len(batch)} patient(s)", category="UNVOID")

        succeeded, failed, grand_total = [], [], 0
        for p in batch:
            try:
                op_id, total = self._unvoid_one(conn, p, accepted, admin_name)
                grand_total += total
                succeeded.append((p, op_id, total))
                self.log(f"[UNVOID]   {p['identifier']}: op_id={op_id}, "
                         f"{total} row(s) unvoided.")
                log.emit(f"{p['identifier']}: op_id={op_id}, {total} row(s) unvoided",
                         category="UNVOID")
            except Error as e:
                failed.append((p, str(e)))
                self.log(f"[UNVOID]   {p['identifier']}: FAILED — {e}")
                log.emit(f"{p['identifier']}: FAILED — {e}",
                         category="UNVOID", level="error")

        self.log(f"[UNVOID] DONE — {len(succeeded)} succeeded, {len(failed)} failed, "
                 f"{grand_total} total row(s).")
        log.emit(f"DONE — {len(succeeded)} succeeded, {len(failed)} failed, "
                 f"{grand_total} total row(s)", category="UNVOID")
        self.log("-" * 70)

        # Build result summary into the details pane and a dialog.
        lines = [f"{len(succeeded)} succeeded, {len(failed)} failed "
                 f"(of {len(batch)} attempted). {grand_total} row(s) unvoided.",
                 "-" * 64, ""]
        for p, op_id, total in succeeded:
            lines.append(f"[OK]    {p['identifier']}  op_id={op_id}  "
                         f"{total} row(s)")
        for p, err in failed:
            lines.append(f"[FAIL]  {p['identifier']}  —  {err}")
        if self._unvoid_skipped:
            lines.append("")
            for ident, reason in self._unvoid_skipped:
                lines.append(f"[SKIP]  {ident}  —  {reason}")
        self._unvoid_show_details("\n".join(lines))

        messagebox.showinfo(
            "Unvoid Complete",
            f"Succeeded: {len(succeeded)}\nFailed: {len(failed)}\n"
            f"Skipped at validation: {len(self._unvoid_skipped)}\n\n"
            f"Total records unvoided: {grand_total}\n\n"
            f"These operations can be reversed by an administrator.",
        )
        self._unvoid_batch = []
        self._unvoid_skipped = []
        self._unvoid_set_button(False)
        self._unvoid_button.config(text="UNVOID PATIENT RECORDS")
        self._unvoid_id_entry.delete(0, tk.END)

    # ====================================================================
    # Reverse Unvoid  (Stage 2: admin-only, feature-flagged)
    # --------------------------------------------------------------------
    # Re-voids EXACTLY the rows a prior UNVOID operation touched, restoring
    # each row's captured prior void state. A row that has been changed
    # since (no longer voided=0) is skipped, never clobbered. The reverse
    # is itself logged as a REVERSE op so it, too, is auditable.
    # ====================================================================

    def _build_reverse_tab(self, parent):
        content = tk.Frame(parent, padx=20, pady=15)
        content.pack(fill="both", expand=True)

        tk.Label(content,
                 text="Reverse a prior unvoid — re-voids only the exact rows it "
                      "changed, restoring their original void state.",
                 font=("Arial", 10), fg="#d32f2f").pack(anchor="w", pady=(0, 8))

        bar = tk.Frame(content)
        bar.pack(fill="x", pady=(0, 8))
        tk.Button(bar, text="REFRESH", command=self._reverse_refresh,
                  bg="#2196F3", fg="white", font=("Arial", 10, "bold"),
                  padx=14, pady=4, cursor="hand2").pack(side="left")
        tk.Button(bar, text="REVERSE SELECTED", command=self._reverse_selected,
                  bg="#f44336", fg="white", font=("Arial", 10, "bold"),
                  padx=14, pady=4, cursor="hand2").pack(side="left", padx=(8, 0))

        cols = [("op_id", "Op", 60), ("op_time", "When", 150),
                ("identifier", "Identifier", 120), ("patient_name", "Name", 200),
                ("rows_affected", "Rows", 60),
                ("anchor_date_voided", "Voided At", 150)]
        tree_frame = tk.Frame(content)
        tree_frame.pack(fill="both", expand=True)
        self._reverse_tree = ttk.Treeview(
            tree_frame, columns=[c[0] for c in cols], show="headings", height=14)
        for key, label, width in cols:
            self._reverse_tree.heading(key, text=label)
            self._reverse_tree.column(key, width=width, anchor="w")
        yscroll = ttk.Scrollbar(tree_frame, orient="vertical",
                                command=self._reverse_tree.yview)
        self._reverse_tree.configure(yscrollcommand=yscroll.set)
        self._reverse_tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")

        self._reverse_refresh()

    def _reverse_refresh(self):
        for item in self._reverse_tree.get_children():
            self._reverse_tree.delete(item)
        conn = self.get_connection()
        if not conn:
            return
        cursor = conn.cursor(dictionary=True, buffered=True)
        try:
            self._ensure_unvoid_schema(cursor)
            cursor.execute(
                "SELECT op_id, op_time, identifier, patient_name, rows_affected, "
                "       anchor_date_voided "
                "FROM nmrs_unvoid_op "
                "WHERE op_type = 'UNVOID' AND status = 'SUCCESS' "
                "      AND reversed_op_id IS NULL "
                "ORDER BY op_time DESC"
            )
            rows = cursor.fetchall()
        except Error as e:
            self.log(f"[REVERSE] Refresh failed: {e}")
            messagebox.showerror("Database Error", f"Query failed:\n\n{e}")
            return
        finally:
            cursor.close()

        for r in rows:
            self._reverse_tree.insert(
                "", "end", iid=str(r["op_id"]),
                values=(r["op_id"], r["op_time"], r["identifier"],
                        r["patient_name"], r["rows_affected"],
                        r["anchor_date_voided"]))
        self.log(f"[REVERSE] {len(rows)} reversible operation(s).")

    def _reverse_selected(self):
        sel = self._reverse_tree.selection()
        if not sel:
            messagebox.showinfo("Nothing Selected",
                                "Select an operation to reverse.")
            return
        try:
            op_id = int(sel[0])
        except ValueError:
            return

        vals = self._reverse_tree.item(sel[0], "values")
        if not messagebox.askyesno(
            "Confirm Reverse",
            f"Reverse unvoid operation {op_id}?\n\n"
            f"Identifier: {vals[2]}\n"
            f"Name:       {vals[3]}\n"
            f"Rows:       {vals[4]}\n\n"
            f"This re-voids only the rows that operation unvoided, restoring "
            f"their original void state. Rows changed since are skipped.\n\n"
            f"Proceed?",
            icon="warning",
        ):
            return

        conn = self.get_connection()
        if not conn:
            return
        admin_name = self.config.get("settings", "admin_name", fallback="Administrator")
        try:
            rev_op_id, restored, skipped = self._reverse_one(conn, op_id, admin_name)
        except Error as e:
            self.log(f"[REVERSE] op {op_id} FAILED — rolled back: {e}")
            messagebox.showerror("Reverse Failed",
                                 f"Operation failed and was rolled back:\n\n{e}")
            return

        self.log(f"[REVERSE] op {op_id} reversed by op {rev_op_id}: "
                 f"{restored} restored, {skipped} skipped.")
        messagebox.showinfo(
            "Reverse Complete",
            f"Reversed operation {op_id}.\n\n"
            f"Restored (re-voided): {restored}\n"
            f"Skipped (changed since): {skipped}\n\n"
            f"Reverse operation id: {rev_op_id}",
        )
        self._reverse_refresh()

    def _reverse_one(self, conn, orig_op_id, admin_name):
        """Re-void exactly the rows logged for orig_op_id. Returns
        (reverse_op_id, restored, skipped). Raises on failure (rolls back)."""
        cursor = conn.cursor(dictionary=True, buffered=True)
        try:
            cursor.execute(
                "SELECT identifier, patient_id, patient_name, anchor_date_voided, "
                "       window_seconds, accepted_reason, reversed_op_id "
                "FROM nmrs_unvoid_op WHERE op_id = %s AND op_type = 'UNVOID'",
                (orig_op_id,),
            )
            op = cursor.fetchone()
            if not op:
                raise Error(f"Unvoid operation {orig_op_id} not found.")
            if op["reversed_op_id"] is not None:
                raise Error(f"Operation {orig_op_id} has already been reversed "
                            f"(by op {op['reversed_op_id']}).")

            cursor.execute(
                "SELECT table_name, pk_column, pk_value, prev_voided, "
                "       prev_date_voided, prev_voided_by, prev_void_reason "
                "FROM nmrs_unvoid_op_row WHERE op_id = %s",
                (orig_op_id,),
            )
            detail = cursor.fetchall()

            cursor.execute(
                "INSERT INTO nmrs_unvoid_op "
                "(op_type, identifier, patient_id, patient_name, "
                " anchor_date_voided, window_seconds, accepted_reason, "
                " executed_by, status, reversed_op_id) "
                "VALUES ('REVERSE', %s, %s, %s, %s, %s, %s, %s, 'IN_PROGRESS', %s)",
                (op["identifier"], op["patient_id"], op["patient_name"],
                 op["anchor_date_voided"], op["window_seconds"],
                 op["accepted_reason"], admin_name, orig_op_id),
            )
            rev_op_id = cursor.lastrowid

            restored = skipped = 0
            for d in detail:
                table, pk_col, pk = d["table_name"], d["pk_column"], d["pk_value"]
                # Capture current state for the reverse op's own audit trail.
                cursor.execute(
                    f"SELECT voided, date_voided, voided_by, void_reason "
                    f"FROM {table} WHERE {pk_col} = %s",
                    (pk,),
                )
                cur = cursor.fetchone()
                if cur is None:
                    skipped += 1
                    continue
                cursor.execute(
                    "INSERT INTO nmrs_unvoid_op_row "
                    "(op_id, table_name, pk_column, pk_value, prev_voided, "
                    " prev_date_voided, prev_voided_by, prev_void_reason) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (rev_op_id, table, pk_col, pk, cur["voided"],
                     cur["date_voided"], cur["voided_by"], cur["void_reason"]),
                )
                # Restore prior void state, but only if still unvoided (voided=0)
                # — never clobber a row that has been changed since.
                cursor.execute(
                    f"UPDATE {table} SET voided = %s, date_voided = %s, "
                    f"voided_by = %s, void_reason = %s "
                    f"WHERE {pk_col} = %s AND voided = 0",
                    (d["prev_voided"], d["prev_date_voided"],
                     d["prev_voided_by"], d["prev_void_reason"], pk),
                )
                if cursor.rowcount:
                    restored += 1
                else:
                    skipped += 1

            cursor.execute(
                "UPDATE nmrs_unvoid_op SET status = 'SUCCESS', rows_affected = %s, "
                "remarks = %s WHERE op_id = %s",
                (restored,
                 f"Reversed op {orig_op_id}: {restored} re-voided, {skipped} skipped.",
                 rev_op_id),
            )
            cursor.execute(
                "UPDATE nmrs_unvoid_op SET reversed_op_id = %s WHERE op_id = %s",
                (rev_op_id, orig_op_id),
            )
            conn.commit()
            return rev_op_id, restored, skipped
        except Error:
            conn.rollback()
            raise
        finally:
            cursor.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
