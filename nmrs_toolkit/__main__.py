"""Entry dispatcher: headless flags vs. GUI.

  python -m nmrs_toolkit --backup              -> headless backup pass
  python -m nmrs_toolkit --generate-linelists  -> headless weekly linelist batch
  python -m nmrs_toolkit                        -> GUI

Headless branches import only `headless` (no UI / webview), so they run on a
display-less, frontend-less machine (MIGRATION_PLAN.md sections 2 & 10.9).
"""
from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]
    if "--backup" in args:
        from .headless import run_headless_backup
        sys.exit(run_headless_backup())
    if "--generate-linelists" in args:
        from .headless import run_headless_linelists
        sys.exit(run_headless_linelists())
    # GUI: import lazily so headless never pulls in webview / Tk. Default to the
    # PyWebView shell; if webview (or its native backend) is unavailable, fall
    # back to the transitional Tk UI so the app stays usable mid-migration.
    try:
        from .app import run_gui
        run_gui()
    except ImportError as e:
        sys.stderr.write(
            f"PyWebView unavailable ({e}); falling back to the legacy Tk UI.\n"
            "Install the new shell with:  python -m pip install pywebview\n"
        )
        from .ui_tk import launch
        launch()


if __name__ == "__main__":
    main()
