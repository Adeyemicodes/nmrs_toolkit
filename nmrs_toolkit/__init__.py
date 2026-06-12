"""NMRS Toolkit — linelist runner, encrypted CSV merge, scheduled DB backup.

v2 package layout (MIGRATION_PLAN.md). Headless entry points live in
`headless` and import no UI; the GUI lives in `ui_tk` (transitional Tkinter)
and, from Phase 1 on, `app` (PyWebView).
"""
from .constants import APP_NAME, APP_VERSION

__all__ = ["APP_NAME", "APP_VERSION"]
