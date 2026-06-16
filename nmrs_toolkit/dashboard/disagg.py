"""Disaggregation helpers (sex / age band / sex×age). One place so every
indicator splits consistently. Operates on parsed loader records."""
from __future__ import annotations

from typing import Iterable

from . import clinical

SEXES = ("F", "M")
AGE_BANDS = ("Pediatric (0-9)", "Adolescent (10-19)", "Adult (20+)", "Unknown")


def _sex(rec) -> str:
    s = (rec.get("sex") or "").strip().upper()
    return s if s in SEXES else "Unknown"


def _band(rec) -> str:
    return clinical.age_band(rec.get("age_years"), rec.get("age_months"))


def split(records: Iterable[dict]) -> dict:
    """Return {'total', 'by_sex', 'by_age_band', 'by_sex_age'} for a set of
    records (already filtered to an indicator's numerator)."""
    by_sex = {}
    by_age = {}
    by_sex_age = {}
    total = 0
    for rec in records:
        total += 1
        s = _sex(rec)
        b = _band(rec)
        by_sex[s] = by_sex.get(s, 0) + 1
        by_age[b] = by_age.get(b, 0) + 1
        key = f"{s} | {b}"
        by_sex_age[key] = by_sex_age.get(key, 0) + 1
    return {
        "total": total,
        "by_sex": by_sex,
        "by_age_band": by_age,
        "by_sex_age": by_sex_age,
    }
