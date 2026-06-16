"""Clinical calculation tests — the LTFU/status logic that must stay in sync
with scripts/TreatmentLinelistv3_2.sql (CurrentARTStatus, sql:557-566)."""
import glob
import os
import unittest
from datetime import date, timedelta

from nmrs_toolkit.dashboard import clinical as C

END = date(2026, 1, 1)


class TestPatientStatus(unittest.TestCase):
    def test_status_categories_fixture(self):
        # (outcome, last_pickup, days_refill, art_start) -> expected category
        cases = [
            # Active: cutoff (pickup + refill + 28) >= end_date
            (None, date(2025, 12, 1), 30, date(2024, 1, 1), C.ACTIVE),
            # LTFU/IIT: cutoff well before end_date
            (None, date(2025, 6, 1), 30, date(2024, 1, 1), C.IIT),
            # recorded outcomes win
            ("Causes of Death", date(2025, 12, 1), 30, date(2024, 1, 1), C.DEAD),
            ("Transferred out", date(2025, 12, 1), 30, date(2024, 1, 1), C.TRANSFERRED_OUT),
            ("Discontinued Care", date(2025, 12, 1), 30, date(2024, 1, 1), C.STOPPED),
            # data-quality / explicit -> IIT (program decision)
            ("Lost to followup", date(2025, 6, 1), 30, date(2024, 1, 1), C.IIT),
            ("Duplicate record", date(2025, 12, 1), 30, date(2024, 1, 1), C.IIT),
            ("Could not verify client", None, None, date(2024, 1, 1), C.IIT),
            # not yet started: art_start after end_date
            (None, date(2025, 12, 1), 30, date(2026, 6, 1), C.NOT_YET_STARTED),
            (None, None, None, None, C.NOT_YET_STARTED),
        ]
        for outcome, lp, dr, art, expected in cases:
            self.assertEqual(
                C.status_category(outcome, lp, dr, art, END), expected,
                msg=f"{outcome=} {lp=} {dr=} {art=}")

    def test_active_ltfu_boundary(self):
        # cutoff exactly == end_date -> Active (DATEDIFF >= 0)
        lp = END - timedelta(days=58)   # +30 refill +28 grace = cutoff == END
        self.assertEqual(C.active_or_ltfu(lp, 30, END), "Active")
        self.assertEqual(C.active_or_ltfu(lp - timedelta(days=1), 30, END), "LTFU")

    def test_current_art_status_mirrors_outcome_then_pickup(self):
        self.assertEqual(C.current_art_status("Transferred out", None, None, END), "Transferred out")
        self.assertEqual(C.current_art_status(None, date(2025, 12, 20), 30, END), "Active")
        self.assertEqual(C.current_art_status(None, None, None, END), "LTFU")


class TestIitDuration(unittest.TestCase):
    def _bucket_for_days_since(self, days_since):
        # choose last_pickup so cutoff = END - days_since (refill 0, grace 28)
        lp = END - timedelta(days=days_since + C.LTFU_GRACE_DAYS)
        return C.iit_duration_bucket(lp, 0, END)

    def test_boundaries(self):
        self.assertEqual(self._bucket_for_days_since(89), "<3 months")
        self.assertEqual(self._bucket_for_days_since(90), "3-<6 months")
        self.assertEqual(self._bucket_for_days_since(179), "3-<6 months")
        self.assertEqual(self._bucket_for_days_since(180), ">=6 months")

    def test_no_pickup_unknown(self):
        self.assertEqual(C.iit_duration_bucket(None, 30, END), "Unknown")


class TestAgeBands(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(C.age_band(0), "Pediatric (0-9)")
        self.assertEqual(C.age_band(9), "Pediatric (0-9)")
        self.assertEqual(C.age_band(10), "Adolescent (10-19)")
        self.assertEqual(C.age_band(19), "Adolescent (10-19)")
        self.assertEqual(C.age_band(20), "Adult (20+)")
        self.assertEqual(C.age_band(65), "Adult (20+)")
        self.assertEqual(C.age_band(None), "Unknown")

    def test_under5_months_fallback(self):
        self.assertEqual(C.age_band(None, 30), "Pediatric (0-9)")  # 2y


class TestVl(unittest.TestCase):
    def test_eligibility(self):
        self.assertFalse(C.is_vl_eligible(5))
        self.assertTrue(C.is_vl_eligible(6))
        self.assertTrue(C.is_vl_eligible(12))
        self.assertFalse(C.is_vl_eligible(None))

    def test_within_12mo_window(self):
        self.assertTrue(C.is_vl_within_12mo(END - timedelta(days=365), END))
        self.assertFalse(C.is_vl_within_12mo(END - timedelta(days=366), END))
        self.assertFalse(C.is_vl_within_12mo(END + timedelta(days=1), END))  # future

    def test_suppression_thresholds(self):
        self.assertTrue(C.is_vl_suppressed("999"))
        self.assertFalse(C.is_vl_suppressed("1000"))
        self.assertFalse(C.is_vl_suppressed("1001"))
        self.assertTrue(C.is_vl_suppressed("19.0"))
        self.assertTrue(C.is_vl_suppressed("TND"))
        self.assertTrue(C.is_vl_suppressed("<20"))
        self.assertFalse(C.is_vl_suppressed(""))   # no result


class TestMmd(unittest.TestCase):
    def test_real_labels(self):
        self.assertEqual(C.mmd_bucket("MMD less than 3"), "<3 months")
        self.assertEqual(C.mmd_bucket("MMD 3 to 5"), "3-5 months")
        self.assertEqual(C.mmd_bucket("MMD greater than or equal to 6"), ">=6 months")

    def test_day_fallback(self):
        self.assertEqual(C.mmd_bucket("", 30), "<3 months")
        self.assertEqual(C.mmd_bucket("", 120), "3-5 months")
        self.assertEqual(C.mmd_bucket("", 180), ">=6 months")
        self.assertEqual(C.mmd_bucket("", None), "Unknown")


class TestBiometric(unittest.TestCase):
    def test_baseline_recapture_cascade(self):
        # baseline only (no recapture)
        b = C.biometric_status("Yes")
        self.assertTrue(b["baseline"])
        self.assertFalse(b["recaptured"])
        self.assertFalse(b["suspicious"])
        # recapture cascades from baseline (has a RecaptureDate)
        rc = C.biometric_status("Yes", recapture_date=date(2025, 6, 1))
        self.assertTrue(rc["baseline"])
        self.assertTrue(rc["recaptured"])
        self.assertFalse(rc["suspicious"])
        # recapture via RecaptureCount > 0
        self.assertTrue(C.biometric_status("Yes", recapture_count=2)["recaptured"])
        # suspicious: recapture without a baseline
        s = C.biometric_status("No", recapture_date=date(2025, 6, 1))
        self.assertFalse(s["baseline"])
        self.assertFalse(s["recaptured"])
        self.assertTrue(s["suspicious"])
        # no capture at all
        n = C.biometric_status("No")
        self.assertTrue(n["no_capture"])
        self.assertFalse(n["recaptured"])


class TestRealCsvEquivalence(unittest.TestCase):
    """Dev-only: validate current_art_status against a real Treatment CSV's
    CurrentARTStatus column. Skipped when no real linelist is present (so the
    repo test suite never depends on patient data)."""

    def test_matches_real_currentartstatus(self):
        from nmrs_toolkit.dashboard import loader
        # Use latest_linelist(): it excludes Treatment_asof_* snapshots (which are
        # generated at a chosen @endDate, so their CurrentARTStatus wouldn't match
        # a generation-date recompute).
        path = loader.latest_linelist()
        if path is None:
            self.skipTest("no real (non-snapshot) Treatment linelist present")
        frame = loader.load_linelist(path)
        # End date = the CSV generation date (its @endDate = NOW at generation).
        gen = frame.generated_at.date() if frame.generated_at else date.today()
        mism = 0
        for r in frame.records:
            real = (r["current_art_status"] or "").strip()
            if not real:
                continue
            mine = C.current_art_status(r["outcome"], r["last_pickup"], r["days_refill"], gen)
            if mine != real:
                mism += 1
        self.assertEqual(mism, 0, f"{mism} mismatches vs real CurrentARTStatus in {path}")


if __name__ == "__main__":
    unittest.main()
