"""PyWebView desktop shell.

Opens a native window rendering the bundled HTML/CSS/JS frontend and wires it
to Python through the `Api` bridge. `webview` is imported lazily inside
run_gui() so that importing this module (or the package) never pulls in the GUI
toolkit — headless entry points must run on a display-less machine
(MIGRATION_PLAN.md sections 2 & 10.9).
"""
from __future__ import annotations

import sys
from pathlib import Path

from .constants import APP_NAME, APP_VERSION

# Initial window geometry mirrors the legacy Tk app (comfortable on a 1366×768
# facility laptop; min size keeps the shell usable when dragged smaller).
_WINDOW_WIDTH = 1150
_WINDOW_HEIGHT = 820
_MIN_SIZE = (950, 700)


def frontend_dir() -> Path:
    """Absolute path to the bundled frontend/ directory (dev and frozen)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        # Phase 8 ships frontend/ under the package path inside the bundle.
        return Path(base) / "nmrs_toolkit" / "frontend"
    return Path(__file__).resolve().parent / "frontend"


def index_url() -> str:
    return str(frontend_dir() / "index.html")


def run_gui() -> None:
    """Launch the PyWebView window. Raises ImportError if webview is absent."""
    import webview  # lazy: never imported by headless paths

    from .bridge import Api

    api = Api()
    window = webview.create_window(
        title=f"{APP_NAME} v{APP_VERSION}",
        url=index_url(),
        js_api=api,
        width=_WINDOW_WIDTH,
        height=_WINDOW_HEIGHT,
        min_size=_MIN_SIZE,
        background_color="#0B1F3A",  # navy flash before first paint, not white
    )
    api.set_window(window)
    # http_server=True serves the bundled frontend over a 127.0.0.1 loopback
    # origin. This is required so the ES modules load (WebKit refuses to fetch
    # module scripts over file:// — null origin / CORS). It is NOT a network
    # service: it binds loopback only, serves the local bundle to our own
    # embedded webview, and the CSP blocks all egress (connect-src 'none').
    webview.start(http_server=True)
