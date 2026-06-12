"""Headless must import with zero UI dependencies (MIGRATION_PLAN.md 10.9).

Simulates webview being uninstalled and asserts that importing the headless
entry-point module (and its transitive workflow imports) still succeeds, and
that nothing pulled `webview` into sys.modules.
"""
import importlib
import subprocess
import sys
import unittest


class TestHeadlessNoWebview(unittest.TestCase):
    def test_import_headless_without_webview(self):
        # Block `webview` from being importable, then (re)import headless fresh.
        saved = {k: v for k, v in sys.modules.items()
                 if k == "webview" or k.startswith("nmrs_toolkit")}
        for k in list(sys.modules):
            if k == "webview" or k.startswith("nmrs_toolkit"):
                del sys.modules[k]
        sys.modules["webview"] = None  # any import webview -> ImportError
        try:
            importlib.import_module("nmrs_toolkit.headless")
            self.assertIsNone(sys.modules.get("webview"),
                              "headless must not import webview")
        finally:
            del sys.modules["webview"]
            for k in list(sys.modules):
                if k.startswith("nmrs_toolkit"):
                    del sys.modules[k]
            sys.modules.update(saved)

    def test_subprocess_clean_interpreter(self):
        # Strongest form: a fresh interpreter where `import webview` is forced to
        # fail still imports headless cleanly.
        code = (
            "import sys; sys.modules['webview'] = None;"
            "import nmrs_toolkit.headless;"
            "assert sys.modules.get('webview') is None;"
            "print('ok')"
        )
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("ok", out.stdout)


if __name__ == "__main__":
    unittest.main()
