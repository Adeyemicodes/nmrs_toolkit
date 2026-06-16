"""Dashboard bridge tests: compute/export/gating against synthetic cached
records (no DB, no real files)."""
import configparser
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from nmrs_toolkit import bridge
from nmrs_toolkit.dashboard import exports as dash_exports


def _rec(**kw):
    base = dict(outcome="", last_pickup=date(2025, 12, 20), days_refill=30,
                art_start=date(2024, 1, 1), sex="F", age_years=30, age_months=None,
                months_on_art=24, viral_load="", vl_reported=None, vl_sample=None,
                date_returned=None, outcome_date=None, mmd_type="",
                biometric_captured="No", biometric_date=None, valid_capture="",
                facility="Test HC", current_art_status="")
    base.update(kw)
    return base


def _api(admin=False):
    cfg = configparser.ConfigParser()
    cfg["database"] = {"host": "h", "user": "u", "password": "p", "database": "d", "port": "3306"}
    cfg["ui"] = {"dashboard_admin_mode": "true" if admin else "false"}
    with mock.patch.object(bridge, "load_config", return_value=cfg), \
         mock.patch.object(bridge, "get_logger"):
        return bridge.Api()


class TestDashboardBridge(unittest.TestCase):
    def test_status(self):
        s = _api().dashboard_status()
        self.assertTrue(s["ok"])
        self.assertIn("export_dir", s)
        self.assertFalse(s["admin_mode"])
        self.assertTrue(_api(admin=True).dashboard_status()["admin_mode"])

    def test_compute_requires_data(self):
        self.assertFalse(_api().dashboard_compute("2025-01-01", "2026-01-01")["ok"])

    def test_compute_with_records(self):
        api = _api()
        api._dashboard_records = [_rec(), _rec(last_pickup=date(2025, 1, 1))]  # 1 active, 1 IIT
        api._dashboard_sources = ["Treatment_x.csv"]
        res = api.dashboard_compute("2025-01-01", "2026-01-01")
        self.assertTrue(res["ok"])
        self.assertEqual(res["tx_curr"]["total"], 1)
        self.assertEqual(res["currently_iit"]["total"], 1)
        self.assertEqual(res["meta"]["sources"], ["Treatment_x.csv"])

    def test_compute_facility_filter(self):
        api = _api()
        api._dashboard_records = [_rec(facility="A"), _rec(facility="B")]
        api._dashboard_sources = ["x"]
        self.assertEqual(api.dashboard_compute("2025-01-01", "2026-01-01", ["A"])["tx_curr"]["total"], 1)

    def test_compute_bad_date(self):
        api = _api()
        api._dashboard_records = [_rec()]
        self.assertFalse(api.dashboard_compute("nope", "2026-01-01")["ok"])

    def test_export_writes_report_and_linelist(self):
        api = _api()
        # _raw must be present for the line-list sheet.
        api._dashboard_records = [_rec(_raw={"FacilityName": "Test HC", "Sex": "F"}),
                                  _rec(sex="M", _raw={"FacilityName": "Test HC", "Sex": "M"})]
        api._dashboard_sources = ["Treatment_x.csv"]
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(dash_exports, "DASHBOARD_EXPORTS_DIR", Path(d)):
                res = api.dashboard_export("tx_curr", "2025-01-01", "2026-01-01")
                self.assertTrue(res["ok"])
                report = Path(res["report_path"]).read_text()
                linelist = Path(res["linelist_path"]).read_text()
        self.assertTrue(report.startswith("# DASHBOARD EXPORT"))
        self.assertIn("THIS IS NOT A CURRENT LINELIST", report)
        self.assertIn("Sex | Age band", report)         # cross-tab header
        self.assertIn("THIS IS NOT A CURRENT LINELIST", linelist)
        self.assertIn("FacilityName", linelist)         # line-list carries raw columns
        self.assertEqual(res["linelist_rows"], 2)

    def test_export_unknown_indicator(self):
        api = _api()
        api._dashboard_records = [_rec()]
        self.assertFalse(api.dashboard_export("bogus", "2025-01-01", "2026-01-01")["ok"])

    def test_admin_gating(self):
        self.assertFalse(_api(admin=False).dashboard_load_folder("/tmp")["ok"])

    def test_refresh_validates_date(self):
        self.assertFalse(_api().dashboard_refresh_from_db("not-a-date")["ok"])


if __name__ == "__main__":
    unittest.main()
