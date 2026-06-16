"""Regression guard: every native file-open dialog must pass pywebview's
file_types validator. A multi-dot pattern (*.sql.gz.enc) in a non-first filter
entry, or a non-word char in the description, makes parse_file_type raise — the
dialog never opens and the Browse button silently does nothing."""
import configparser
import unittest
from unittest import mock

from nmrs_toolkit import bridge

try:
    from webview.util import parse_file_type
    _HAVE_WEBVIEW = True
except Exception:  # pragma: no cover
    _HAVE_WEBVIEW = False


class _FakeWindow:
    def __init__(self):
        self.file_types = None

    def create_file_dialog(self, dialog_type, allow_multiple=False, file_types=()):
        self.file_types = file_types
        return None  # simulate "cancelled" so the method returns cleanly


def _api():
    cfg = configparser.ConfigParser()
    cfg["database"] = {"host": "h", "user": "u", "password": "p", "database": "d", "port": "3306"}
    cfg["backup"] = {"backup_key": "ab" * 32}
    with mock.patch.object(bridge, "load_config", return_value=cfg), \
         mock.patch.object(bridge, "get_logger"):
        api = bridge.Api()
    return api


@unittest.skipUnless(_HAVE_WEBVIEW, "pywebview not installed")
class TestFileDialogFilters(unittest.TestCase):
    def _filters_for(self, method):
        api = _api()
        win = _FakeWindow()
        api._window = win
        getattr(api, method)()
        self.assertIsNotNone(win.file_types, f"{method} did not call create_file_dialog")
        return win.file_types

    def test_every_picker_uses_valid_filters(self):
        for method in ("restore_pick_file", "decrypt_pick_file",
                       "linelist_pick_custom", "merge_pick_files"):
            for ft in self._filters_for(method):
                # raises ValueError if the filter is invalid
                parse_file_type(ft)

    def test_dialog_error_returns_message_not_exception(self):
        # A backend failure must come back as {ok: False, message}, never raise
        # (which would reject the JS promise -> dead Browse button).
        api = _api()

        class Boom:
            def create_file_dialog(self, *a, **k):
                raise RuntimeError("backend exploded")
        api._window = Boom()
        res = api.restore_pick_file()
        self.assertFalse(res["ok"])
        self.assertIn("backend exploded", res["message"])


if __name__ == "__main__":
    unittest.main()
