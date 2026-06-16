"""Indicator engine. Each function takes parsed loader records + start/end dates
and returns a JSON-serializable dict. Snapshot indicators evaluate AT end_date;
period-flow indicators filter by their own date columns over [start, end].

Status is recomputed from raw columns via clinical.py — never trusted from the
CSV's CurrentARTStatus column for a historical end_date.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from . import clinical, disagg


def _in_period(d: Optional[date], start: date, end: date) -> bool:
    return d is not None and start <= d <= end


def _status(rec, end_date: date) -> str:
    return clinical.status_category(
        rec.get("outcome"), rec.get("last_pickup"), rec.get("days_refill"),
        rec.get("art_start"), end_date)


def _ever_enrolled_recs(records, end_date: date) -> List[dict]:
    return [r for r in records
            if r.get("art_start") is not None and r.get("art_start") <= end_date]


# -- snapshot indicators (as of end_date) -----------------------------------

def ever_enrolled(records, start_date, end_date) -> dict:
    return disagg.split(_ever_enrolled_recs(records, end_date))


def tx_curr(records, start_date, end_date) -> dict:
    return disagg.split([r for r in records if _status(r, end_date) == clinical.ACTIVE])


def currently_iit(records, start_date, end_date) -> dict:
    iit = [r for r in records if _status(r, end_date) == clinical.IIT]
    out = disagg.split(iit)
    by_duration = {}
    by_subgroup = {}
    for r in iit:
        d = clinical.iit_duration_bucket(r.get("last_pickup"), r.get("days_refill"), end_date)
        by_duration[d] = by_duration.get(d, 0) + 1
        sub = clinical.outcome_subgroup(r.get("outcome")) or "Computed LTFU"
        by_subgroup[sub] = by_subgroup.get(sub, 0) + 1
    out["by_duration"] = by_duration
    out["by_subgroup"] = by_subgroup
    return out


def mmd_distribution(records, start_date, end_date) -> dict:
    curr = [r for r in records if _status(r, end_date) == clinical.ACTIVE]
    dist = {}
    covered = 0
    for r in curr:
        b = clinical.mmd_bucket(r.get("mmd_type"), r.get("days_refill"))
        dist[b] = dist.get(b, 0) + 1
        if clinical.is_mmd_covered(b):
            covered += 1
    total = len(curr)
    return {"total": total, "by_bucket": dist,
            "mmd_coverage_pct": round(100.0 * covered / total, 1) if total else 0.0}


def age_sex_pyramid(records, start_date, end_date) -> dict:
    curr = [r for r in records if _status(r, end_date) == clinical.ACTIVE]
    grid = {b: {"F": 0, "M": 0} for b in disagg.AGE_BANDS}
    for r in curr:
        s = (r.get("sex") or "").strip().upper()
        if s not in ("F", "M"):
            continue
        b = clinical.age_band(r.get("age_years"), r.get("age_months"))
        grid[b][s] += 1
    return {"total": len(curr), "grid": grid}


def biometric_coverage(records, start_date, end_date) -> dict:
    curr = [r for r in records if _status(r, end_date) == clinical.ACTIVE]
    captured = valid = up_to_date = needs_recapture = never = 0
    for r in curr:
        b = clinical.biometric_status(r.get("biometric_captured"), r.get("valid_capture"),
                                      r.get("biometric_date"), end_date)
        if b["captured"]:
            captured += 1
        else:
            never += 1
        if b["valid"]:
            valid += 1
        if b["up_to_date"]:
            up_to_date += 1
        if b["needs_recapture"]:
            needs_recapture += 1
    return {"total": len(curr), "captured": captured, "valid": valid,
            "up_to_date": up_to_date, "needs_recapture": needs_recapture,
            "never_captured": never}


def vl_cascade(records, start_date, end_date) -> dict:
    eligible = [r for r in records
                if _status(r, end_date) == clinical.ACTIVE
                and clinical.is_vl_eligible(r.get("months_on_art"))]
    sampled = [r for r in eligible
               if clinical.is_vl_within_12mo(r.get("vl_sample"), end_date)]
    with_result = [r for r in sampled
                   if clinical.parse_viral_load(r.get("viral_load")) is not None
                   and clinical.is_vl_within_12mo(r.get("vl_reported"), end_date)]
    suppressed = [r for r in with_result if clinical.is_vl_suppressed(r.get("viral_load"))]
    n_elig, n_res = len(eligible), len(with_result)
    return {
        "eligible": n_elig,
        "sampled": len(sampled),
        "with_result": n_res,
        "suppressed": len(suppressed),
        "unsuppressed": n_res - len(suppressed),
        "coverage_pct": round(100.0 * n_res / n_elig, 1) if n_elig else 0.0,
        "suppression_pct": round(100.0 * len(suppressed) / n_res, 1) if n_res else 0.0,
        "eligible_disagg": disagg.split(eligible),
        "suppressed_disagg": disagg.split(suppressed),
        "with_result_disagg": disagg.split(with_result),
    }


# -- period-flow indicators (events within [start, end]) --------------------

def tx_new(records, start_date, end_date) -> dict:
    return disagg.split([r for r in records if _in_period(r.get("art_start"), start_date, end_date)])


def tx_rtt(records, start_date, end_date) -> dict:
    return disagg.split([r for r in records if _in_period(r.get("date_returned"), start_date, end_date)])


def tx_ml(records, start_date, end_date) -> dict:
    """Patients who left Active during [start, end], by reason."""
    reasons = {"Newly IIT": [], "Died": [], "Transferred Out": [], "Refused/Stopped": []}
    for r in records:
        cat = _status(r, end_date)
        od = r.get("outcome_date")
        if cat == clinical.DEAD and _in_period(od, start_date, end_date):
            reasons["Died"].append(r)
        elif cat == clinical.TRANSFERRED_OUT and _in_period(od, start_date, end_date):
            reasons["Transferred Out"].append(r)
        elif cat == clinical.STOPPED and _in_period(od, start_date, end_date):
            reasons["Refused/Stopped"].append(r)
        elif cat == clinical.IIT:
            # Newly IIT: cutoff falls in the period AND was Active at start_date.
            cutoff = clinical.iit_cutoff(r.get("last_pickup"), r.get("days_refill"))
            was_active_at_start = clinical.status_category(
                r.get("outcome"), r.get("last_pickup"), r.get("days_refill"),
                r.get("art_start"), start_date) == clinical.ACTIVE
            if cutoff is not None and _in_period(cutoff, start_date, end_date) and was_active_at_start:
                reasons["Newly IIT"].append(r)
    by_reason = {k: len(v) for k, v in reasons.items()}
    total = sum(by_reason.values())
    return {"total": total, "by_reason": by_reason,
            "disagg": {k: disagg.split(v) for k, v in reasons.items()}}


# -- cohort flow + roll-up ---------------------------------------------------

def cohort_flow(records, start_date, end_date) -> dict:
    ever = _ever_enrolled_recs(records, end_date)
    counts = {clinical.ACTIVE: 0, clinical.IIT: 0, clinical.DEAD: 0,
              clinical.TRANSFERRED_OUT: 0, clinical.STOPPED: 0}
    for r in ever:
        c = _status(r, end_date)
        if c in counts:
            counts[c] += 1
    return {
        "ever_enrolled": len(ever),
        "tx_new": len([r for r in records if _in_period(r.get("art_start"), start_date, end_date)]),
        "tx_curr": counts[clinical.ACTIVE],
        "currently_iit": counts[clinical.IIT],
        "dead": counts[clinical.DEAD],
        "transferred_out": counts[clinical.TRANSFERRED_OUT],
        "stopped": counts[clinical.STOPPED],
        "tx_rtt": len([r for r in records if _in_period(r.get("date_returned"), start_date, end_date)]),
        "tx_ml_total": tx_ml(records, start_date, end_date)["total"],
    }


def compute_all(records, start_date, end_date, *, meta_extra: Optional[dict] = None) -> dict:
    """Run every indicator once. Returns a single JSON-encodable dict."""
    out = {
        "ever_enrolled": ever_enrolled(records, start_date, end_date),
        "tx_new": tx_new(records, start_date, end_date),
        "tx_curr": tx_curr(records, start_date, end_date),
        "currently_iit": currently_iit(records, start_date, end_date),
        "tx_ml": tx_ml(records, start_date, end_date),
        "tx_rtt": tx_rtt(records, start_date, end_date),
        "vl_cascade": vl_cascade(records, start_date, end_date),
        "mmd_distribution": mmd_distribution(records, start_date, end_date),
        "age_sex_pyramid": age_sex_pyramid(records, start_date, end_date),
        "biometric_coverage": biometric_coverage(records, start_date, end_date),
        "cohort_flow": cohort_flow(records, start_date, end_date),
    }
    meta = {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "row_count": len(records),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if meta_extra:
        meta.update(meta_extra)
    out["meta"] = meta
    return out
