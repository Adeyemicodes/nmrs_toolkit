"""Indicator-engine tests: end_date-driven snapshots, period scoping, and
cohort-flow internal consistency."""
import unittest
from datetime import date

from nmrs_toolkit.dashboard import indicators as I
from nmrs_toolkit.dashboard import clinical as C


def rec(**kw):
    """Build a parsed loader-style record with sensible defaults."""
    base = dict(outcome="", last_pickup=None, days_refill=30, art_start=None,
                sex="F", age_years=30, age_months=None, months_on_art=24,
                viral_load="", vl_reported=None, vl_sample=None,
                date_returned=None, outcome_date=None, mmd_type="",
                biometric_captured="No", biometric_date=None, valid_capture="")
    base.update(kw)
    return base


class TestEndDateDriven(unittest.TestCase):
    def test_tx_curr_uses_end_date_not_today(self):
        # Patient started ART on 2025-09-01, recent pickup -> Active "now".
        # With end_date before their ART start, they're Not Yet Started.
        r = rec(art_start=date(2025, 9, 1), last_pickup=date(2025, 12, 20), days_refill=30)
        now_curr = I.tx_curr([r], date(2025, 1, 1), date(2026, 1, 1))["total"]
        past_curr = I.tx_curr([r], date(2024, 1, 1), date(2025, 6, 1))["total"]
        self.assertEqual(now_curr, 1)
        self.assertEqual(past_curr, 0)

    def test_ever_enrolled_excludes_post_period_starts(self):
        r = rec(art_start=date(2026, 3, 1))  # after end
        self.assertEqual(I.ever_enrolled([r], date(2025, 1, 1), date(2026, 1, 1))["total"], 0)
        self.assertEqual(I.ever_enrolled([r], date(2025, 1, 1), date(2026, 6, 1))["total"], 1)


class TestPeriodScoping(unittest.TestCase):
    def test_tx_new_window(self):
        rows = [rec(art_start=date(2025, 11, 15)), rec(art_start=date(2025, 6, 1))]
        self.assertEqual(I.tx_new(rows, date(2025, 10, 1), date(2025, 12, 31))["total"], 1)

    def test_tx_rtt_window(self):
        rows = [rec(date_returned=date(2025, 11, 1)), rec(date_returned=date(2024, 1, 1))]
        self.assertEqual(I.tx_rtt(rows, date(2025, 10, 1), date(2025, 12, 31))["total"], 1)

    def test_tx_ml_reasons(self):
        end = date(2026, 1, 1)
        start = date(2025, 10, 1)
        rows = [
            rec(outcome="Causes of Death", outcome_date=date(2025, 11, 1), art_start=date(2024, 1, 1)),
            rec(outcome="Transferred out", outcome_date=date(2025, 12, 1), art_start=date(2024, 1, 1)),
            rec(outcome="Discontinued Care", outcome_date=date(2025, 10, 15), art_start=date(2024, 1, 1)),
            # death outside the period -> not counted
            rec(outcome="Causes of Death", outcome_date=date(2025, 1, 1), art_start=date(2024, 1, 1)),
        ]
        ml = I.tx_ml(rows, start, end)
        self.assertEqual(ml["by_reason"]["Died"], 1)
        self.assertEqual(ml["by_reason"]["Transferred Out"], 1)
        self.assertEqual(ml["by_reason"]["Refused/Stopped"], 1)
        self.assertEqual(ml["total"], 3)


class TestCohortFlowConsistency(unittest.TestCase):
    def test_internal_consistency(self):
        end = date(2026, 1, 1)
        rows = [
            rec(art_start=date(2024, 1, 1), last_pickup=date(2025, 12, 20), days_refill=30),  # Active
            rec(art_start=date(2024, 1, 1), last_pickup=date(2025, 1, 1), days_refill=30),    # IIT
            rec(art_start=date(2024, 1, 1), outcome="Causes of Death", outcome_date=date(2025, 6, 1)),  # Dead
            rec(art_start=date(2024, 1, 1), outcome="Transferred out", outcome_date=date(2025, 6, 1)),  # TO
            rec(art_start=date(2024, 1, 1), outcome="Discontinued Care", outcome_date=date(2025, 6, 1)),  # Stopped
            rec(art_start=date(2024, 1, 1), outcome="Duplicate record"),  # IIT (data quality)
            rec(art_start=date(2026, 6, 1)),  # Not Yet Started -> excluded from ever_enrolled
        ]
        cf = I.cohort_flow(rows, date(2025, 1, 1), end)
        self.assertEqual(cf["ever_enrolled"], 6)  # 7 rows minus the future-start
        total = (cf["tx_curr"] + cf["currently_iit"] + cf["dead"]
                 + cf["transferred_out"] + cf["stopped"])
        self.assertEqual(total, cf["ever_enrolled"])
        self.assertEqual(cf["tx_curr"], 1)
        self.assertEqual(cf["currently_iit"], 2)  # one computed LTFU + one duplicate


class TestComputeAll(unittest.TestCase):
    def test_compute_all_shape(self):
        rows = [rec(art_start=date(2024, 1, 1), last_pickup=date(2025, 12, 20))]
        out = I.compute_all(rows, date(2025, 1, 1), date(2026, 1, 1))
        for key in ("ever_enrolled", "tx_new", "tx_curr", "currently_iit", "tx_ml",
                    "tx_rtt", "vl_cascade", "mmd_distribution", "age_sex_pyramid",
                    "biometric_coverage", "cohort_flow", "meta"):
            self.assertIn(key, out)
        self.assertEqual(out["meta"]["end_date"], "2026-01-01")


if __name__ == "__main__":
    unittest.main()
