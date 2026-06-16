"""Phase 1 bridge tests: the auth gate (wired to the shared comparison) and the
config summary that drives the DB profile banner. The bridge logic is tested
directly in Python — no webview required."""
import configparser
import unittest
from unittest import mock

from nmrs_toolkit import bridge


def _cfg(admin_password="", profile="LOCAL"):
    c = configparser.ConfigParser()
    c["settings"] = {"admin_password": admin_password}
    c["database"] = {
        "host": "db.example", "port": "3307", "database": "openmrs1",
        "user": "svc", "profile_label": profile,
    }
    return c


def _api(cfg):
    """Build an Api without running load_config()/get_logger side effects."""
    with mock.patch.object(bridge, "load_config", return_value=cfg), \
         mock.patch.object(bridge, "get_logger"):
        api = bridge.Api()
    # LOADED_CONFIG_PATH attribute access happens in config_get_summary.
    return api


class TestProfileClass(unittest.TestCase):
    def test_mapping(self):
        cases = {
            "PROD": "prod", "production": "prod",
            "STAGING": "staging", "uat": "staging",
            "TEST": "test", "Dev": "test",
            "LOCAL": "local", "": "unlabeled", "weird": "unlabeled",
        }
        for raw, expected in cases.items():
            self.assertEqual(bridge.profile_class(raw), expected, raw)


class TestAuth(unittest.TestCase):
    def test_login_correct_password(self):
        api = _api(_cfg(admin_password="s3cret"))
        self.assertTrue(api.auth_login("s3cret")["ok"])

    def test_login_incorrect_password(self):
        api = _api(_cfg(admin_password="s3cret"))
        res = api.auth_login("wrong")
        self.assertFalse(res["ok"])
        self.assertIn("Incorrect", res["message"])

    def test_login_open_when_no_password(self):
        api = _api(_cfg(admin_password=""))
        self.assertTrue(api.auth_login("")["ok"])

    def test_status_reports_password_required(self):
        self.assertTrue(_api(_cfg("pw")).auth_status()["password_required"])
        self.assertFalse(_api(_cfg("")).auth_status()["password_required"])

    def test_uses_same_comparison_as_tk(self):
        # The bridge must defer to config.verify_admin_password (the single
        # source of truth shared with the Tk UI), not a private reimplementation.
        cfg = _cfg(admin_password="pw")
        api = _api(cfg)
        with mock.patch.object(bridge, "verify_admin_password",
                               return_value=True) as vp:
            api.auth_login("anything")
        vp.assert_called_once_with(cfg, "anything")


class TestConfigSummary(unittest.TestCase):
    def test_summary_fields_and_banner_class(self):
        api = _api(_cfg(profile="PROD"))
        s = api.config_get_summary()
        self.assertTrue(s["ok"])
        self.assertEqual(s["db_profile"], "PROD")
        self.assertEqual(s["profile_class"], "prod")
        self.assertEqual(s["db_name"], "openmrs1")
        self.assertEqual(s["port"], "3307")

    def test_unlabeled_profile(self):
        s = _api(_cfg(profile="")).config_get_summary()
        self.assertEqual(s["db_label"], "UNLABELED")
        self.assertEqual(s["profile_class"], "unlabeled")


class TestLogBridge(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from nmrs_toolkit.logger import AppLogger
        self.api = _api(_cfg())
        # _api mocks get_logger; swap in a real, isolated AppLogger on a tmp dir.
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.api._log = AppLogger(base / "application.log",
                                  {"BACKUP": base / "backup.log",
                                   "RESTORE": base / "restore.log"})
        self.marker = "ZZmarker_" + str(id(self))
        self.api._log.emit(f"{self.marker} alpha encrypted", category="BACKUP")
        self.api._log.emit(f"{self.marker} beta plain", category="RESTORE")

    def tearDown(self):
        self.tmp.cleanup()

    def test_tail_returns_events(self):
        seqs = [e["seq"] for e in self.api.log_tail(5000)]
        self.assertEqual(len(seqs), len(set(seqs)), "seq ids must be unique")

    def test_search_filters_by_query_and_category(self):
        hits = self.api.log_search(self.marker)
        mine = [e for e in hits if self.marker in e["line"]]
        self.assertEqual(len(mine), 2)
        backup_only = self.api.log_search(self.marker, {"categories": ["BACKUP"]})
        cats = {e["category"] for e in backup_only if self.marker in e["line"]}
        self.assertEqual(cats, {"BACKUP"})

    def test_search_substring(self):
        hits = self.api.log_search("encrypted", {"categories": ["BACKUP"]})
        self.assertTrue(any(self.marker in e["line"] for e in hits))

    def test_disk_info_shape(self):
        info = self.api.log_disk_info()
        self.assertIn("size_human", info)
        self.assertIn("name", info)

    def test_subscribe_toggles_push(self):
        self.assertEqual(self.api.log_subscribe()["ok"], True)
        self.assertTrue(self.api._log_push_on)
        self.api.log_unsubscribe()
        self.assertFalse(self.api._log_push_on)


if __name__ == "__main__":
    unittest.main()
