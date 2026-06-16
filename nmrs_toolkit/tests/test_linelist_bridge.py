"""Linelists tab bridge: bundled listing, source resolution, run validation."""
import configparser
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nmrs_toolkit import bridge


def _api(with_config=True):
    cfg = configparser.ConfigParser() if with_config else None
    if cfg is not None:
        cfg["database"] = {"host": "h", "user": "u", "password": "p",
                           "database": "d", "port": "3306"}
    with mock.patch.object(bridge, "load_config", return_value=cfg), \
         mock.patch.object(bridge, "get_logger"):
        api = bridge.Api()
    if cfg is None:
        api.config = None
        api.config_error = "no config"
    return api


class TestListBundled(unittest.TestCase):
    def test_lists_registry_scripts_present_on_disk(self):
        res = _api().linelist_list_bundled()
        self.assertTrue(res["ok"])
        names = [s["name"] for s in res["scripts"]]
        # The curated registry order; at least the core weekly ones should exist.
        self.assertIn("Treatment", names)
        self.assertGreaterEqual(res["batch_count"], 1)
        self.assertIn("linelist_dir", res)


class TestResolveSource(unittest.TestCase):
    def test_bundled_by_name(self):
        api = _api()
        display, path = api._resolve_linelist_source({"type": "bundled", "name": "Treatment"})
        self.assertEqual(display, "Treatment")
        self.assertTrue(path.exists())

    def test_bundled_unknown_raises(self):
        with self.assertRaises(ValueError):
            _api()._resolve_linelist_source({"type": "bundled", "name": "Nope"})

    def test_custom_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "MyReport.sql"
            p.write_text("SELECT 1;")
            display, path = _api()._resolve_linelist_source({"type": "custom", "path": str(p)})
            self.assertEqual(display, "MyReport")
            self.assertEqual(path, p)

    def test_custom_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            _api()._resolve_linelist_source({"type": "custom", "path": "/nope/x.sql"})


class TestRunValidation(unittest.TestCase):
    def test_no_config(self):
        res = _api(with_config=False).linelist_run({"type": "bundled", "name": "Treatment"}, "x.csv", False)
        self.assertFalse(res["ok"])

    def test_empty_output_name(self):
        res = _api().linelist_run({"type": "bundled", "name": "Treatment"}, "  ", False)
        self.assertFalse(res["ok"])
        self.assertIn("filename", res["message"].lower())

    def test_unknown_source(self):
        res = _api().linelist_run({"type": "bundled", "name": "Nope"}, "x.csv", False)
        self.assertFalse(res["ok"])

    def test_batch_no_config(self):
        self.assertFalse(_api(with_config=False).linelist_run_weekly_batch(False)["ok"])


if __name__ == "__main__":
    unittest.main()
