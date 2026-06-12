# NMRS Toolkit v2.0 — Migration to PyWebView + Unified Logging

**Purpose:** This document describes the architectural changes required to
modernize the NMRS Toolkit UI without compromising its security model,
operational guarantees, or forensic logging.

**Audience:** Project implementers, organizational management, and the
engineer (or AI assistant) executing the migration.

**Out of scope:** Adding new product features. This migration is strictly a
UI modernization plus a logging architecture refactor. Functional behavior
is preserved one-to-one except where explicitly noted.

---

## 1. Why PyWebView, not a web app

The toolkit's security narrative — per-facility encrypted local databases,
manager-held master secret that never leaves the manager's machine,
offline-first operation, encrypted local backups — depends on the
application running locally on operator hardware. A traditional web
application (browser + central server) recreates the Canvas-style attack
surface that the briefing deck warns against. A "local web server + browser"
hybrid loses OS scheduler integration and the single-binary deployment
story.

PyWebView gives us a modern HTML/CSS/JS frontend rendered inside a
desktop window, with full access to the existing Python backend. The app
remains local, offline-first, and single-binary. Only the rendering layer
changes.

---

## 2. What is preserved (do not modify)

The following parts of the existing codebase carry hard-won correctness and
are explicitly out of scope for rewriting:

- All cryptography: `AESGCM` encryption/decryption, `derive_facility_key`
  (HMAC-SHA256 from manager master secret), `derive_csv_key`,
  CSV `.nmrs` envelope format.
- The `db_connect()` wrapper that pins `use_pure=True` on
  `mysql-connector-python`. This is required for PyInstaller-frozen builds.
- The `_NO_WINDOW = subprocess.CREATE_NO_WINDOW` guard for Windows child
  processes (mysqldump, mysql, schtasks). Without it, a console window
  flashes on every subprocess and non-technical users interpret the flash
  as an error.
- The `secure_config_dir()` / `secure_config_path()` logic and the
  `.nmrs_config.ini` file format. The migration of older configs into the
  platform-specific hidden directory must continue to work.
- The headless entry points: `--backup` (`run_headless_backup`) and
  `--generate-linelists` (`run_headless_linelists`). These are invoked by
  the OS scheduler (cron/schtasks) and their command-line contract must
  not change.
- The OS scheduler integration code: `install_schedule_*`,
  `uninstall_schedule_*`, the `SCHEDULE_VERSION` marker.
- The idempotent backup gating: same-day already-backed-up detection,
  pre-restore safety backup, retention pruning.
- `LINELIST_REGISTRY` — the curated list and its `in_batch` flags.
- The directory layout: `C:\NMRS_DB` / `~/NMRS_DB` for backups,
  `C:\NMRS_Linelists` / `~/NMRS_Linelists` for linelists.
- `BACKUP_LOG_FILE`, `LINELIST_LOG_FILE`, and (to be added) the new
  `RESTORE_LOG_FILE` and `APPLICATION_LOG_FILE` paths. The on-disk log
  files are the canonical forensic record.
- The Unvoid Patient workflow including the `nmrs_unvoid_op` audit table.
- Tooltip semantics and helper text content (the existing copy is good).

---

## 3. What is replaced

- The entire Tkinter UI: `NMRSToolkitApp.__init__`, every `_build_*_tab`
  method, every Tk widget. Replaced by an HTML/CSS/JS frontend rendered
  in PyWebView.
- The login screen.
- The DB profile banner — preserved as a *concept* and as a *safety
  affordance*, but reimplemented in HTML with stronger visual emphasis.
- The Activity Log widget — replaced by a persistent, searchable,
  filterable, exportable log viewer.
- The per-tab log widgets — replaced by the three-tier display pattern
  (summary chips → recent activity card → full raw log).

---

## 4. The unified logging architecture

This is the substantive refactor beyond UI. A new `AppLogger` service
becomes the canonical log emission point for every workflow.

### Today (problem)

- `self.log()` writes only to an in-memory Tk widget. It does not persist.
- `_ll_log()` (linelist) forwards to Activity Log with `[LINELIST]` tag,
  does not persist independently.
- `step_log()` (backup) writes to Activity Log AND to `BACKUP_LOG_FILE`.
- `_restore_log()` writes to the restore widget AND to disk, but does
  NOT forward to Activity Log.
- Result: Activity Log is incomplete (no restore events) and ephemeral
  (lost on restart). On-disk per-workflow files are the actual canonical
  record, but split across files with no unified view.

### After (solution)

A single `AppLogger` class owns all logging. Every workflow uses it.

```python
class AppLogger:
    def emit(
        self,
        message: str,
        *,
        category: str,        # "APP" | "BACKUP" | "RESTORE" | "LINELIST" | "MERGE" | "UNVOID" | "SCHED" | "UI"
        level: str = "info",  # "info" | "warn" | "error" | "debug"
        facility: str = None,
        operation_id: str = None,
    ) -> None:
        # 1. Append to in-memory ring buffer (for live UI subscribers)
        # 2. Append to APPLICATION_LOG_FILE (rotating, 10 MB × 3 generations)
        # 3. Append to category-specific on-disk file (BACKUP_LOG_FILE etc.)
        # 4. Notify any subscribed UI panels (frontend tabs subscribe by category)
```

Properties this gives us:

- **One source of truth.** `APPLICATION_LOG_FILE` contains every event,
  cross-cutting. Open it in any text editor for full forensic context.
- **Per-category on-disk files preserved.** `backup.log`, `restore.log`,
  `linelist.log` continue to exist for workflow-specific debugging.
- **Crash survival.** Every emit flushes to disk before returning.
- **Restart survival.** The UI loads recent entries from the persisted
  file on launch.
- **Structured data.** Each line includes ISO timestamp, level,
  category, optional facility name, and optional operation_id, so the
  frontend can filter/group reliably.

### Required forensic UX (non-negotiable per stakeholder decision)

The new Activity Log UI must support:

- Plain monospace rendering of every line, byte-identical to the on-disk
  format. Selectable text, no syntax-tinted obstruction.
- Copy-to-clipboard: per-line, multi-line selection, and "copy all
  visible."
- Search (substring, case-insensitive) with live highlight.
- Filter by category and level (multi-select chips).
- Filter by date range.
- "Export filtered slice to .txt" — writes the currently-visible
  subset to a file the user picks.
- "Open application.log in system text editor."
- Auto-scroll toggle (so a user can pause to read without the live tail
  pulling away).
- Visible disk-size indicator and a manual "rotate now" action.

These affordances are what allow the per-tab logs to be presented as
summary views without losing troubleshooting capacity. The unified
Activity Log is the forensic surface; per-tab views are convenience
surfaces.

---

## 5. New file structure

```
nmrs-toolkit/
├── pyproject.toml                # Build config (replaces inline PyInstaller spec)
├── README.md
├── nmrs_toolkit/                 # Python package (was a single file)
│   ├── __init__.py
│   ├── __main__.py               # Entry: GUI vs --backup vs --generate-linelists
│   ├── app.py                    # PyWebView window + JS bridge API
│   ├── config.py                 # Config loading (preserved logic)
│   ├── crypto.py                 # AESGCM, HMAC key derivation (preserved)
│   ├── db.py                     # db_connect, connection helpers (preserved)
│   ├── logger.py                 # NEW: AppLogger service
│   ├── workflows/
│   │   ├── __init__.py
│   │   ├── backup.py             # perform_backup, append_backup_log (preserved)
│   │   ├── restore.py            # restore_dump (preserved, logging refactored)
│   │   ├── linelist.py           # SQL execution, batch (preserved)
│   │   ├── merge.py              # CSV merge (preserved)
│   │   └── unvoid.py             # Unvoid + reverse (preserved)
│   ├── scheduler.py              # cron/schtasks install (preserved)
│   ├── headless.py               # --backup / --generate-linelists (preserved)
│   ├── bridge.py                 # JS-callable methods exposed to PyWebView
│   └── frontend/
│       ├── index.html
│       ├── tokens.css            # Brand tokens (navy, teal, scale)
│       ├── components.css        # Component library
│       ├── app.js                # SPA shell, routing, state
│       └── tabs/
│           ├── login.js
│           ├── linelists.js
│           ├── merge.js
│           ├── backup.js
│           ├── restore.js
│           ├── unvoid.js
│           ├── reverse-unvoid.js
│           ├── decrypt.js
│           └── activity-log.js
└── scripts/                      # Bundled SQL linelists (unchanged)
    ├── TreatmentLinelistv3_2.sql
    ├── PMTCT_ANC.sql
    └── ...
```

The PyInstaller `--add-data` declaration changes to include `frontend/`.

---

## 6. Design system

### Color tokens (from the briefing deck)

```css
:root {
  /* Brand */
  --color-primary:        #0B1F3A;   /* deep navy: trust, security */
  --color-primary-2:      #1A2F4F;   /* navy elevated */
  --color-accent:         #1FB6A6;   /* medical teal: safe, under control */
  --color-accent-2:       #168F84;   /* teal pressed/active */

  /* Status (used semantically, never decoratively) */
  --color-success:        #1B8B5A;
  --color-success-bg:     #E6F4EE;
  --color-warning:        #B07015;
  --color-warning-bg:     #FBEFD9;
  --color-danger:         #B23B3B;
  --color-danger-bg:      #F8E3E3;
  --color-info:           #1E5FAA;
  --color-info-bg:        #E6EEF8;

  /* DB profile (kept from existing code, modernized) */
  --color-prof-prod:      #C62828;
  --color-prof-staging:   #EF6C00;
  --color-prof-test:      #2E7D32;
  --color-prof-local:     #455A64;

  /* Surfaces */
  --color-bg:             #F7FAFC;
  --color-surface:        #FFFFFF;
  --color-surface-2:      #F1F4F7;
  --color-border:         #DBE2EA;
  --color-border-strong:  #B7C2CF;

  /* Text */
  --color-text:           #0F1A2C;
  --color-text-muted:     #5A6878;
  --color-text-faint:     #8A95A4;

  /* Scale */
  --radius-sm: 4px;
  --radius-md: 8px;
  --radius-lg: 12px;
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 24px;
  --space-6: 32px;

  /* Type */
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas,
               "Liberation Mono", monospace;
}
```

### Component library (minimum)

- **AppShell**: header bar (navy, app name + version + org), DB profile
  banner (full-width, color-coded by profile, prominent), tab nav,
  content slot, persistent Activity Log drawer (collapsible).
- **StatTile**: small label, large number, optional sub-label, optional
  status color.
- **StatusChip**: text + icon, semantic color (success/warning/danger/info).
- **ActionButton**: primary (teal), secondary (outline), danger (red),
  disabled state, loading state with spinner.
- **DataTable**: rows, sortable headers, status column with chips,
  per-row actions.
- **ActivityCard**: vertical timeline of recent events, semantic
  left-border color, icon, two-line layout (event + metadata).
- **LogViewer**: monospace, copyable, searchable, filterable, exportable
  (used for both the unified Activity Log and per-tab "Show full log").
- **FormField**: label, input, helper text, error state.
- **Stepper**: numbered steps for workflows (preserves existing Step 1 / 2
  / 3 pattern).
- **ConfirmDialog**: especially for Restore — typed-name confirmation,
  list of consequences, explicit danger framing.
- **Toast**: non-blocking status notifications.

### Typography scale

- Display (page titles): 24px / 600
- Heading (section titles): 18px / 600
- Body strong: 14px / 500
- Body: 14px / 400
- Caption (metadata, helper): 12px / 400
- Mono (log entries): 12px / 400

---

## 7. Bridge API (Python ↔ JS)

PyWebView exposes Python methods to the frontend via a `js_api` object.
The bridge is the only surface where JS can invoke Python work. Every
bridge method:

- Takes JSON-serializable args, returns JSON-serializable results.
- Is non-blocking from the UI's perspective — long-running work runs on
  a worker thread and emits progress events via the logger.
- Logs entry and exit through `AppLogger`.

Minimum methods:

```
auth.login(password) -> {ok, message}
config.get_summary() -> {db_profile, db_label, host, db_name, user, ...}

linelists.list_bundled() -> [{name, filename}, ...]
linelists.run(script_ref, output_path, encrypt) -> operation_id
linelists.run_weekly_batch() -> operation_id
linelists.open_folder() -> {ok}

merge.add_files([paths]) -> {accepted: [...], rejected: [...]}
merge.run(file_paths, sort_col, descending, output_path, encrypt) -> operation_id

backup.list_facilities() -> [{facility, last_run, size, status}, ...]
backup.run_now() -> operation_id
backup.update_schedules() -> {ok, details}
backup.open_folder() -> {ok}

restore.preview(dump_path) -> {format, encrypted, estimated_size}
restore.run(dump_path, target_db, key_hex, typed_confirmation) -> operation_id
restore.cancel(operation_id) -> {ok}

unvoid.validate(identifiers) -> [{identifier, found, current_state, ...}]
unvoid.commit(operation_payload) -> operation_id

log.subscribe(categories=[]) -> stream_id
log.search(query, filters) -> [entries...]
log.export(filters, output_path) -> {ok, written_bytes}
log.open_in_editor() -> {ok}
```

Progress and completion are pushed to the frontend via PyWebView's
`evaluate_js` channel as the operation runs. The frontend subscribes by
`operation_id`.

---

## 8. Build & distribution

PyInstaller, single-file binary, unchanged target platforms (Linux first,
Windows roadmap). The spec adds the `frontend/` directory via `--add-data`
and bundles `pywebview` plus its platform GUI backend (gtk/qt on Linux,
edgechromium on Windows).

Output: a single executable. Double-click to launch. Offline-capable. No
behavior change from the current distribution model.

---

## 9. Migration sequence (phases)

Phase 0 — Project scaffold and the AppLogger
- Split `nmrs_toolkit.py` into the package layout above.
- Implement `AppLogger`; retrofit `perform_backup`,
  `run_headless_backup`, `run_headless_linelists`, restore, merge,
  unvoid to emit through it.
- Verify: headless `--backup` and `--generate-linelists` still work
  identically. `BACKUP_LOG_FILE` and `LINELIST_LOG_FILE` still receive
  their per-workflow lines. `APPLICATION_LOG_FILE` now receives all.

Phase 1 — PyWebView shell with the login screen
- Window opens, login renders, password check works against existing
  config-stored `admin_password`.
- DB profile banner renders post-login.

Phase 2 — Activity Log (unified, persistent, forensic-grade)
- Build it first because every other tab uses its drawer.
- All forensic UX features in section 4.

Phase 3 — Backup tab
- Per-facility table, stat tiles, recent activity, "Show full log."
- `backup.run_now()` wired to existing `perform_backup`.

Phase 4 — Restore tab
- Strong danger framing, typed-name confirmation, progress bar,
  cancel support.

Phase 5 — Linelists tab
- Script picker, output settings, run + batch.
- Three-tier log: status pill + recent runs + full `linelist.log`.

Phase 6 — Merge tab
- File list with reordering, sort options, run.

Phase 7 — Unvoid + Reverse Unvoid + Decrypt tabs
- Same component library; smaller surfaces.

Phase 8 — Polish, PyInstaller spec, smoke test
- Single-binary build verified on Ubuntu; smoke-tested on a clean
  workstation.

Each phase ends with the app being shippable. If we run out of time
mid-migration, the version we have is still usable — earlier tabs are
modernized, later tabs fall back to a "Coming in 2.1" placeholder that
links to the still-working v1.2.0 binary.

---

## 10. Acceptance criteria

The migration is complete when:

1. All five visible tabs (Linelists, Merge, Backup, Restore, Unvoid)
   plus the three config-gated tabs (Reverse Unvoid, Decrypt, plus any
   future Manager Tools) render under the new design system.
2. The headless entry points produce byte-identical output to v1.2.0.
3. `BACKUP_LOG_FILE`, `LINELIST_LOG_FILE`, and the new
   `APPLICATION_LOG_FILE` and `RESTORE_LOG_FILE` all receive entries
   correctly under every workflow.
4. The Activity Log UI provides every forensic affordance listed in
   section 4.
5. The DB profile banner is more visually prominent than v1.2.0 (this
   is a safety affordance, not a decoration).
6. The Restore confirmation flow requires typed-name confirmation.
7. PyInstaller produces a single-file Linux binary that launches
   offline and runs every tab without internet.
8. No external network calls are made by the running application.
9. Headless invocations require zero UI dependencies (PyWebView and
   the frontend bundle are skipped when invoked with `--backup` or
   `--generate-linelists`).

---

## 11. Estimated effort

A focused engineer (human or AI) working in Claude Code: roughly
40–80 hours. Most of the time is in Phase 2 (Activity Log forensic UX)
and Phase 4 (Restore — confirmation flow + progress + cancel).
Phases 0, 1, 3, 5, 6, 7 are mechanical once the design system and
AppLogger are stable.
