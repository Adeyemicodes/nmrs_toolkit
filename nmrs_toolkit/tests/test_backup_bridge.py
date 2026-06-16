"""Backup tab bridge: per-facility status derivation from BACKUP_DIR."""
import configparser
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from nmrs_toolkit import bridge


def _api(cfg=None):
    cfg = cfg or configparser.ConfigParser()
    if not cfg.has_section("settings"):
        cfg["settings"] = {}
    with mock.patch.object(bridge, "load_config", return_value=cfg), \
         mock.patch.object(bridge, "get_logger"):
        return bridge.Api()


def _touch(path: Path, age_hours: float, size: int = 1024):
    path.write_bytes(b"x" * size)
    when = time.time() - age_hours * 3600
    os.utime(path, (when, when))


class TestBackupListFacilities(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patcher = mock.patch.object(bridge, "BACKUP_DIR", self.dir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_status_classification_by_age(self):
        _touch(self.dir / "Alpha_HC_nmrs_backup_2026-06-12_0900.sql.gz.enc", 2)    # fresh
        _touch(self.dir / "Beta_HC_nmrs_backup_2026-06-11_0900.sql.gz.enc", 30)    # stale24
        _touch(self.dir / "Gamma_HC_nmrs_backup_2026-06-09_0900.sql.gz.enc", 72)   # stale48
        res = _api().backup_list_facilities()
        by = {r["facility"]: r["status"] for r in res["facilities"]}
        self.assertEqual(by["Alpha_HC"], "encrypted")
        self.assertEqual(by["Beta_HC"], "stale24")
        self.assertEqual(by["Gamma_HC"], "stale48")
        self.assertEqual(res["total"], 3)
        self.assertEqual(res["fresh"], 1)
        self.assertEqual(res["encryption"], "AES-GCM")
        self.assertGreater(res["total_bytes"], 0)

    def test_groups_by_facility_takes_latest(self):
        _touch(self.dir / "Alpha_HC_nmrs_backup_2026-06-10_0900.sql.gz.enc", 48)
        _touch(self.dir / "Alpha_HC_nmrs_backup_2026-06-12_0900.sql.gz.enc", 1)
        res = _api().backup_list_facilities()
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["facilities"][0]["status"], "encrypted")

    def test_never_row_from_config_when_empty(self):
        cfg = configparser.ConfigParser()
        cfg["settings"] = {"facility_name": "Configured Clinic"}
        res = _api(cfg).backup_list_facilities()
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["facilities"][0]["status"], "never")
        self.assertEqual(res["facilities"][0]["facility"], "Configured Clinic")

    def test_empty_when_nothing(self):
        res = _api().backup_list_facilities()
        self.assertEqual(res["facilities"], [])
        self.assertEqual(res["total"], 0)


if __name__ == "__main__":
    unittest.main()
