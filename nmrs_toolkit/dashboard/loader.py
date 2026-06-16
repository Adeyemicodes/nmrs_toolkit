"""Load Treatment linelist CSVs into a normalized, typed in-memory form.

stdlib csv only (no pandas) — the linelist is ~1-2k rows, so a list of dicts is
ample and keeps the binary lean. Row-level parse failures are logged through the
AppLogger (category DASHBOARD, level warn) with the row INDEX and reason ONLY —
never patient values — and the row is skipped, never silently dropped.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from ..constants import LINELIST_DIR
from ..crypto import decrypt_bytes, is_encrypted_file
from ..logger import get_logger
from . import clinical

# Map normalized CSV column -> record field. Only the columns the indicators
# need are parsed; the raw row is kept for exports.
_DATE_COLS = {
    "ARTStartDate": "art_start",
    "LastPickupDate": "last_pickup",
    "ViralLoadReportedDate": "vl_reported",
    "ViralLoadSampleCollectionDate": "vl_sample",  # NB: not "LastSampleTakenDate"
    "DateReturnedToCare": "date_returned",
    "PatientOutcomeDate": "outcome_date",
    "BiometricCaptureDate": "biometric_date",
}
_STR_COLS = {
    "FacilityName": "facility",
    "Sex": "sex",
    "PatientOutcome": "outcome",
    "CurrentARTStatus": "current_art_status",
    "MMDType": "mmd_type",
    "BiometricCaptured": "biometric_captured",
    "ValidCapture": "valid_capture",
    "CurrentViralLoad(c/ml)": "viral_load",
}
_INT_COLS = {
    "DaysOfARVRefil": "days_refill",
    "MonthsOnART": "months_on_art",
    "CurrentAgeYears": "age_years",
    "CurrentAgeMonths": "age_months",
}


class LinelistFrame:
    """Thin wrapper around the parsed rows + metadata."""

    def __init__(self, records: List[dict], *, facility_name: str,
                 generated_at: Optional[datetime], source_name: str):
        self.records = records
        self.facility_name = facility_name
        self.generated_at = generated_at
        self.source_name = source_name

    @property
    def row_count(self) -> int:
        return len(self.records)


def _norm_header(name: str) -> str:
    return (name or "").replace("﻿", "").strip()


def _read_text(path: Path, decrypt_key: Optional[bytes]) -> str:
    raw = path.read_bytes()
    if is_encrypted_file(path) or path.suffix.lower() == ".nmrs":
        if not decrypt_key:
            raise ValueError(f"{path.name} is encrypted but no decrypt key was provided.")
        raw = decrypt_bytes(raw, decrypt_key)
    # utf-8-sig strips a BOM if present.
    return raw.decode("utf-8-sig", errors="replace")


def load_linelist(path, *, decrypt_key: Optional[bytes] = None) -> LinelistFrame:
    """Load a single Treatment_*.csv (or .csv.nmrs) into a LinelistFrame."""
    path = Path(path)
    log = get_logger()
    text = _read_text(path, decrypt_key)
    reader = csv.DictReader(io.StringIO(text))
    reader.fieldnames = [_norm_header(h) for h in (reader.fieldnames or [])]

    records: List[dict] = []
    facility_name = ""
    skipped = 0
    for i, raw in enumerate(reader, start=2):  # row 1 is the header
        try:
            rec = {"_raw": raw}
            for col, field in _STR_COLS.items():
                rec[field] = (raw.get(col) or "").strip()
            for col, field in _DATE_COLS.items():
                rec[field] = clinical.parse_ll_date(raw.get(col))
            for col, field in _INT_COLS.items():
                rec[field] = _to_int(raw.get(col))
            if rec["facility"] and not facility_name:
                facility_name = rec["facility"]
            records.append(rec)
        except Exception as e:  # noqa: BLE001 — never include patient values
            skipped += 1
            log.emit(f"skipped row {i} ({type(e).__name__})",
                     category="DASHBOARD", level="warn")

    if skipped:
        log.emit(f"loaded {len(records)} row(s) from {path.name}, {skipped} skipped",
                 category="DASHBOARD", level="warn")
    return LinelistFrame(
        records, facility_name=facility_name or path.stem,
        generated_at=_mtime(path), source_name=path.name)


def load_linelist_folder(folder, *, pattern: str = "Treatment_*.csv*",
                         decrypt_key: Optional[bytes] = None) -> List[LinelistFrame]:
    """Multi-facility load: one LinelistFrame per matching file. Each record is
    tagged with `_source_facility` so a concatenation can be filtered by site."""
    folder = Path(folder)
    frames: List[LinelistFrame] = []
    for p in sorted(folder.glob(pattern)):
        try:
            frame = load_linelist(p, decrypt_key=decrypt_key)
        except Exception as e:  # noqa: BLE001
            get_logger().emit(f"failed to load {p.name} ({type(e).__name__})",
                              category="DASHBOARD", level="warn")
            continue
        for rec in frame.records:
            rec["_source_facility"] = frame.facility_name
        frames.append(frame)
    return frames


def latest_linelist(folder=LINELIST_DIR, *, pattern: str = "Treatment_*.csv*") -> Optional[Path]:
    """Most recent CURRENT Treatment_*.csv* by mtime, or None. Excludes the
    dashboard's historical-snapshot files (Treatment_asof_*) so a snapshot
    generated by Refresh-from-DB can never be picked up as the current linelist."""
    folder = Path(folder)
    if not folder.exists():
        return None
    candidates = [p for p in folder.glob(pattern) if "_asof_" not in p.name]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def concat(frames: List[LinelistFrame]) -> List[dict]:
    """Flatten multiple frames' records into one list (multi-facility)."""
    out: List[dict] = []
    for f in frames:
        out.extend(f.records)
    return out


def _to_int(value) -> Optional[int]:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _mtime(path: Path) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None
