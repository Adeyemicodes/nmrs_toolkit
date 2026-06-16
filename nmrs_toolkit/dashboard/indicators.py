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


def _bio(r):
    return clinical.biometric_status(r.get("biometric_captured"),
                                     r.get("recapture_date"), r.get("recapture_count"))


def biometric_coverage(records, start_date, end_date) -> dict:
    """Of TX_CURR at end_date: baseline coverage, recapture (cascades from
    baseline), the no-capture gap, and the suspicious recapture-without-baseline
    anomaly count."""
    curr = [r for r in records if _status(r, end_date) == clinical.ACTIVE]
    baseline = recaptured = no_capture = suspicious = 0
    bl_recs, gap_recs = [], []
    for r in curr:
        b = _bio(r)
        if b["baseline"]:
            baseline += 1
            bl_recs.append(r)
        if b["recaptured"]:
            recaptured += 1
        if b["no_capture"]:
            no_capture += 1
            gap_recs.append(r)
        if b["suspicious"]:
            suspicious += 1
    return {"total": len(curr), "baseline": baseline, "recaptured": recaptured,
            "no_capture": no_capture, "suspicious": suspicious,
            "baseline_disagg": disagg.split(bl_recs),
            "no_capture_disagg": disagg.split(gap_recs)}


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


# ---------------------------------------------------------------------------
# Export support: affected-client record lists + Sex×Age cross-tabs ("pivots").
# The dashboard export writes two CSVs per indicator — the line list of affected
# clients, and the aggregated cross-tab report behind it.
# ---------------------------------------------------------------------------

_SEX_ORDER = ["F", "M", "Unknown"]
_BAND_ORDER = ["Pediatric (0-9)", "Adolescent (10-19)", "Adult (20+)", "Unknown"]
_ROW_ORDER = [f"{s} | {b}" for s in _SEX_ORDER for b in _BAND_ORDER]


def _rsex(r):
    s = (r.get("sex") or "").strip().upper()
    return s if s in ("F", "M") else "Unknown"


def _row_key(r):
    return f"{_rsex(r)} | {clinical.age_band(r.get('age_years'), r.get('age_months'))}"


def crosstab(records, col_fn, col_order) -> dict:
    """Sex×Age (rows) × col_fn (columns) cross-tab with row/column totals.
    Returns {header, rows, total} (lists of cells), ready to write to CSV."""
    cells = {}
    for r in records:
        rk = _row_key(r)
        ck = col_fn(r)
        cells.setdefault(rk, {})
        cells[rk][ck] = cells[rk].get(ck, 0) + 1
    cols = list(col_order)
    header = ["Sex | Age band"] + cols + ["Total"]
    body = []
    col_tot = {c: 0 for c in cols}
    grand = 0
    for rk in _ROW_ORDER:
        if rk not in cells:
            continue
        row = [rk]
        rt = 0
        for c in cols:
            n = cells[rk].get(c, 0)
            row.append(n)
            rt += n
            col_tot[c] += n
        row.append(rt)
        grand += rt
        body.append(row)
    total = ["Total"] + [col_tot[c] for c in cols] + [grand]
    return {"header": header, "rows": body, "total": total}


def _tx_ml_records(records, start_date, end_date):
    """[(record, reason), ...] for patients who left Active during the period."""
    out = []
    for r in records:
        cat = _status(r, end_date)
        od = r.get("outcome_date")
        if cat == clinical.DEAD and _in_period(od, start_date, end_date):
            out.append((r, "Died"))
        elif cat == clinical.TRANSFERRED_OUT and _in_period(od, start_date, end_date):
            out.append((r, "Transferred Out"))
        elif cat == clinical.STOPPED and _in_period(od, start_date, end_date):
            out.append((r, "Refused/Stopped"))
        elif cat == clinical.IIT:
            cutoff = clinical.iit_cutoff(r.get("last_pickup"), r.get("days_refill"))
            was_active = clinical.status_category(
                r.get("outcome"), r.get("last_pickup"), r.get("days_refill"),
                r.get("art_start"), start_date) == clinical.ACTIVE
            if cutoff is not None and _in_period(cutoff, start_date, end_date) and was_active:
                out.append((r, "Newly IIT"))
    return out


def affected_records(slug, records, start_date, end_date) -> list:
    """The line-list (numerator) records behind an indicator."""
    active = [r for r in records if _status(r, end_date) == clinical.ACTIVE]
    if slug in ("ever_enrolled", "cohort_flow"):
        return _ever_enrolled_recs(records, end_date)
    if slug == "tx_new":
        return [r for r in records if _in_period(r.get("art_start"), start_date, end_date)]
    if slug in ("tx_curr", "mmd_distribution", "age_sex_pyramid"):
        return active
    if slug == "currently_iit":
        return [r for r in records if _status(r, end_date) == clinical.IIT]
    if slug == "tx_ml":
        return [r for r, _reason in _tx_ml_records(records, start_date, end_date)]
    if slug == "tx_rtt":
        return [r for r in records if _in_period(r.get("date_returned"), start_date, end_date)]
    if slug == "vl_cascade":
        return [r for r in active if clinical.is_vl_eligible(r.get("months_on_art"))]
    if slug == "biometric_coverage":  # actionable list = the no-baseline capture gap
        return [r for r in active if _bio(r)["no_capture"]]
    return []


def report_for(slug, records, start_date, end_date) -> list:
    """List of (title, crosstab-table) for the report CSV. The column dimension
    adapts per indicator (the multi-dimensional 'pivot' the report needs)."""
    recs = affected_records(slug, records, start_date, end_date)
    count_col = (lambda r: "Count", ["Count"])

    if slug == "currently_iit":
        sub = (lambda r: clinical.outcome_subgroup(r.get("outcome")) or "Computed LTFU",
               ["Computed LTFU", "Lost to follow-up (recorded)", "Duplicate record", "Could not verify"])
        dur = (lambda r: clinical.iit_duration_bucket(r.get("last_pickup"), r.get("days_refill"), end_date),
               ["<3 months", "3-<6 months", ">=6 months", "Unknown"])
        return [("Currently IIT — Sex×Age × sub-group", crosstab(recs, sub[0], sub[1])),
                ("Currently IIT — Sex×Age × duration", crosstab(recs, dur[0], dur[1]))]
    if slug == "tx_ml":
        pairs = _tx_ml_records(records, start_date, end_date)
        reason = {id(r): rs for r, rs in pairs}
        col = (lambda r: reason.get(id(r), "Other"),
               ["Newly IIT", "Died", "Transferred Out", "Refused/Stopped"])
        return [("TX_ML — Sex×Age × exit reason", crosstab(recs, col[0], col[1]))]
    if slug == "mmd_distribution":
        col = (lambda r: clinical.mmd_bucket(r.get("mmd_type"), r.get("days_refill")),
               ["<3 months", "3-5 months", ">=6 months", "Unknown"])
        return [("MMD — Sex×Age × bucket", crosstab(recs, col[0], col[1]))]
    if slug == "biometric_coverage":
        active = [r for r in records if _status(r, end_date) == clinical.ACTIVE]

        def _bstatus(r):
            b = _bio(r)
            if b["suspicious"]:
                return "Recapture w/o baseline"
            if b["recaptured"]:
                return "Recaptured"
            if b["baseline"]:
                return "Baseline only"
            return "No capture"
        col = (_bstatus, ["Baseline only", "Recaptured", "No capture", "Recapture w/o baseline"])
        return [("Biometric — Sex×Age × status", crosstab(active, col[0], col[1]))]
    if slug == "vl_cascade":
        elig = affected_records("vl_cascade", records, start_date, end_date)
        with_result = [r for r in elig
                       if clinical.parse_viral_load(r.get("viral_load")) is not None
                       and clinical.is_vl_within_12mo(r.get("vl_reported"), end_date)
                       and clinical.is_vl_within_12mo(r.get("vl_sample"), end_date)]
        col = (lambda r: "Suppressed" if clinical.is_vl_suppressed(r.get("viral_load")) else "Unsuppressed",
               ["Suppressed", "Unsuppressed"])
        return [("VL eligible — Sex×Age", crosstab(elig, count_col[0], count_col[1])),
                ("VL with-result — Sex×Age × suppression", crosstab(with_result, col[0], col[1]))]
    # KPIs / default: a plain Sex×Age distribution.
    return [(f"{slug} — Sex×Age", crosstab(recs, count_col[0], count_col[1]))]
