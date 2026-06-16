"""Clinically-correct status calculations that mirror the Treatment linelist SQL.

PURE functions only — no I/O, no Tk, no webview. The LTFU math is the single
source of truth shared with scripts/TreatmentLinelistv3_2.sql; keep this file in
sync with that SQL (and with test_clinical.py) on any change.

Concept identifiers (from TreatmentLinelistv3_2.sql):
  162240 = pharmacy pickup obs (LastPickupDate)
  159368 = days of ARV refill obs (DaysOfARVRefil)
  165470 = patient outcome obs (PatientOutcome / form 13)

CurrentARTStatus in the SQL (TreatmentLinelistv3_2.sql:557-566) is:
    IFNULL(<patient-outcome concept name>,
           IF(DATEDIFF(LastPickup + (DaysOfARVRefil + 28) DAY, @endDate) >= 0,
              'Active', 'LTFU'))
i.e. the recorded outcome wins; otherwise it is the pickup-based Active/LTFU.
`current_art_status()` reproduces that string exactly (so it can be validated
against the real CurrentARTStatus column). `status_category()` layers the
program's grouping on top (see OUTCOME_CATEGORY), grounded in the actual outcome
strings seen in facility exports.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

# Mirrors the "+28" grace in the SQL DATE_ADD(..., (days_refill + 28) DAY).
LTFU_GRACE_DAYS = 28

LINELIST_DATE_FMT = "%d-%b-%Y"  # e.g. "19-Dec-2025"

# Categories returned by status_category().
ACTIVE = "Active"
IIT = "IIT"
DEAD = "Dead"
TRANSFERRED_OUT = "Transferred Out"
STOPPED = "Stopped"
NOT_YET_STARTED = "Not Yet Started"
UNKNOWN = "Unknown"

# Real PatientOutcome concept-name strings -> clinical category. Derived from
# actual facility exports (see review). Matching is case-insensitive/trimmed.
# Per program decision: any non-active outcome that isn't Dead/TO/Stopped
# (Lost to followup, Duplicate record, Could not verify client) groups under
# IIT — "simply not active" — with the data-quality ones available as an IIT
# sub-breakdown via outcome_subgroup().
OUTCOME_CATEGORY = {
    "causes of death": DEAD,
    "transferred out": TRANSFERRED_OUT,
    "discontinued care": STOPPED,
    "lost to followup": IIT,
    "duplicate record": IIT,
    "could not verify client": IIT,
}
# IIT sub-labels for the data-quality / explicitly-recorded outcomes.
OUTCOME_SUBGROUP = {
    "lost to followup": "Lost to follow-up (recorded)",
    "duplicate record": "Duplicate record",
    "could not verify client": "Could not verify",
}


def parse_ll_date(value) -> Optional[date]:
    """Parse a linelist date ('%d-%b-%Y'). Returns None on blank/invalid."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, LINELIST_DATE_FMT).date()
    except ValueError:
        # Tolerate ISO too, just in case an export differs.
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _to_int(value) -> Optional[int]:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def iit_cutoff(last_pickup_date: Optional[date],
               days_of_arv_refill: Optional[int]) -> Optional[date]:
    """The date a patient becomes LTFU: LastPickup + DaysOfARVRefil + 28 days.
    None if there is no pickup date."""
    if last_pickup_date is None:
        return None
    from datetime import timedelta
    days = (days_of_arv_refill or 0) + LTFU_GRACE_DAYS
    return last_pickup_date + timedelta(days=days)


def active_or_ltfu(last_pickup_date: Optional[date],
                   days_of_arv_refill: Optional[int],
                   end_date: date) -> str:
    """Pickup-based status, verbatim with the SQL:
        IF(DATEDIFF(cutoff, end_date) >= 0, 'Active', 'LTFU')
    where cutoff = LastPickup + (DaysOfARVRefil + 28) days.
    A null pickup -> 'LTFU' (the SQL's IF(NULL>=0,...) falls through to LTFU)."""
    cutoff = iit_cutoff(last_pickup_date, days_of_arv_refill)
    if cutoff is None:
        return "LTFU"
    return "Active" if (cutoff - end_date).days >= 0 else "LTFU"


def current_art_status(patient_outcome: Optional[str],
                       last_pickup_date: Optional[date],
                       days_of_arv_refill: Optional[int],
                       end_date: date) -> str:
    """Reproduce the SQL CurrentARTStatus string exactly: the recorded outcome
    concept name if present, otherwise 'Active'/'LTFU'. Used to validate against
    the real CurrentARTStatus column."""
    if patient_outcome and str(patient_outcome).strip():
        return str(patient_outcome).strip()
    return active_or_ltfu(last_pickup_date, days_of_arv_refill, end_date)


def status_category(patient_outcome: Optional[str],
                    last_pickup_date: Optional[date],
                    days_of_arv_refill: Optional[int],
                    art_start_date: Optional[date],
                    end_date: date) -> str:
    """Clinical grouping used by the indicators. Returns one of:
    Active / IIT / Dead / Transferred Out / Stopped / Not Yet Started.

    Precedence: recorded outcome (mapped via OUTCOME_CATEGORY) wins; else, if
    ART hasn't started by end_date -> Not Yet Started; else pickup-based, with
    'LTFU' grouped as IIT."""
    outcome = (patient_outcome or "").strip().lower()
    if outcome in OUTCOME_CATEGORY:
        return OUTCOME_CATEGORY[outcome]
    if outcome:  # an unrecognized recorded outcome -> not active -> IIT
        return IIT
    if art_start_date is None or art_start_date > end_date:
        return NOT_YET_STARTED
    return IIT if active_or_ltfu(last_pickup_date, days_of_arv_refill, end_date) == "LTFU" else ACTIVE


def outcome_subgroup(patient_outcome: Optional[str]) -> Optional[str]:
    """For IIT patients, an optional sub-label distinguishing data-quality /
    explicitly-recorded outcomes from computed LTFU. None = computed LTFU."""
    return OUTCOME_SUBGROUP.get((patient_outcome or "").strip().lower())


def iit_duration_bucket(last_pickup_date: Optional[date],
                        days_of_arv_refill: Optional[int],
                        end_date: date) -> str:
    """For an IIT patient, time since the LTFU cutoff, bucketed:
    '<3 months' / '3-<6 months' / '>=6 months' / 'Unknown'.
    Months use a 30-day approximation (consistent with the calendar-day grace);
    boundaries at 90 and 180 days. 'Unknown' when there is no pickup date
    (e.g. an outcome-only IIT with no cutoff)."""
    cutoff = iit_cutoff(last_pickup_date, days_of_arv_refill)
    if cutoff is None:
        return "Unknown"
    days_since = (end_date - cutoff).days
    if days_since < 0:
        return "Unknown"  # not actually past cutoff at end_date
    if days_since < 90:
        return "<3 months"
    if days_since < 180:
        return "3-<6 months"
    return ">=6 months"


def age_band(age_years: Optional[int], age_months: Optional[int] = None) -> str:
    """PEPFAR-style bands: 'Pediatric (0-9)' / 'Adolescent (10-19)' /
    'Adult (20+)' / 'Unknown'. Uses CurrentAgeYears; for under-5s (years null)
    falls back to CurrentAgeMonths/12."""
    yrs = _to_int(age_years)
    if yrs is None:
        months = _to_int(age_months)
        if months is None:
            return "Unknown"
        yrs = months // 12
    if yrs < 0:
        return "Unknown"
    if yrs <= 9:
        return "Pediatric (0-9)"
    if yrs <= 19:
        return "Adolescent (10-19)"
    return "Adult (20+)"


def is_vl_eligible(months_on_art: Optional[int]) -> bool:
    """PEPFAR VL eligibility: >= 6 months on ART."""
    m = _to_int(months_on_art)
    return m is not None and m >= 6


def is_vl_within_12mo(vl_date: Optional[date], end_date: date) -> bool:
    """True if vl_date is within 365 days before end_date (and not in the
    future relative to end_date)."""
    if vl_date is None:
        return False
    delta = (end_date - vl_date).days
    return 0 <= delta <= 365


def parse_viral_load(value) -> Optional[float]:
    """Parse a CurrentViralLoad(c/ml) value. Handles plain numerics ('19.0'),
    undetectable markers ('TND', 'LDL', 'undetectable', '<20') -> 0.0, and
    blanks -> None. Documented formats: facility exports use plain floats;
    undetectable markers are tolerated defensively."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    low = s.lower()
    if low.startswith("<") or "tnd" in low or "ldl" in low or "undetect" in low or "target not" in low:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


def is_vl_suppressed(viral_load) -> bool:
    """True if viral load < 1000 c/mL. Undetectable markers -> suppressed."""
    vl = parse_viral_load(viral_load)
    return vl is not None and vl < 1000.0


def mmd_bucket(mmd_type: Optional[str],
               days_of_arv_refill: Optional[int] = None) -> str:
    """Multi-month dispensing bucket: '<3 months' / '3-5 months' /
    '>=6 months' / 'Unknown'. Prefers the MMDType column (facility exports
    pre-bucket it as 'MMD less than 3' / 'MMD 3 to 5' / 'MMD greater than or
    equal to 6'); otherwise derives from DaysOfARVRefil (<90 / 90-179 / >=180)."""
    t = (mmd_type or "").strip().lower()
    if "less than 3" in t:
        return "<3 months"
    if "3 to 5" in t or "3-5" in t:
        return "3-5 months"
    if "greater than or equal to 6" in t or ">=6" in t or "6 or more" in t:
        return ">=6 months"
    days = _to_int(days_of_arv_refill)
    if days is None:
        return "Unknown"
    if days < 90:
        return "<3 months"
    if days < 180:
        return "3-5 months"
    return ">=6 months"


def is_mmd_covered(bucket: str) -> bool:
    """MMD coverage = on >= 3 months of dispensing."""
    return bucket in ("3-5 months", ">=6 months")


def biometric_status(biometric_captured: Optional[str],
                     recapture_date: Optional[date] = None,
                     recapture_count: Optional[int] = None) -> dict:
    """Biometric capture state. Recapture cascades from baseline.

    baseline   = a baseline biometric has been captured (BiometricCaptured=Yes).
    recaptured = a recapture has occurred (a RecaptureDate or RecaptureCount>0),
                 AND a baseline exists (the normal cascade).
    suspicious = a recapture is recorded with NO baseline — a data anomaly.
    no_capture = no baseline at all (the capture gap to action).
    (A fingerprint 'match'-at-pickup metric is a planned future addition.)"""
    baseline = (biometric_captured or "").strip().lower() == "yes"
    has_recap = bool(recapture_date) or (_to_int(recapture_count) or 0) > 0
    return {
        "baseline": baseline,
        "recaptured": baseline and has_recap,
        "suspicious": has_recap and not baseline,
        "no_capture": not baseline,
    }
