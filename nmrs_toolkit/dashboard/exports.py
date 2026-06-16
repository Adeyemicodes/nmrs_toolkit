"""Dashboard exports — TWO CSVs per indicator, both banner-headed and written to
DASHBOARD_EXPORTS_DIR (NEVER LINELIST_DIR):

  * <stem>_report.csv   — the aggregated Sex×Age cross-tab(s) ("pivot") report.
  * <stem>_linelist.csv — the affected clients' rows behind that report.

Both carry the "NOT a current linelist" banner. The line-list file contains
patient-level data by design (the actionable client list); it stays in the local
exports folder, and logs never include patient identifiers.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from ..constants import DASHBOARD_EXPORTS_DIR


def _banner(indicator_name, start_date, end_date, source_str):
    return [
        f"# DASHBOARD EXPORT - Indicator: {indicator_name}",
        f"# Period: {start_date} to {end_date}  -  Snapshot indicators reflect end={end_date}",
        f"# Generated: {datetime.now().isoformat(timespec='seconds')}  -  Source: {source_str}",
        "# THIS IS NOT A CURRENT LINELIST - Do not use for current program decisions",
    ]


def write_export(indicator_slug, indicator_name, start_date, end_date,
                 source_names, report_tables, affected) -> dict:
    """Write the report + line-list CSVs. report_tables = list of
    (title, {header, rows, total}); affected = list of loader records (use _raw).
    Returns {ok, report_path, linelist_path, report_rows, linelist_rows}."""
    DASHBOARD_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    gen = datetime.now().strftime("%Y%m%d%H%M")
    sd, ed = start_date.replace("-", ""), end_date.replace("-", "")
    stem = f"Dashboard_{indicator_slug}_{sd}_to_{ed}_generated_{gen}"
    source_str = (", ".join(source_names) if isinstance(source_names, (list, tuple))
                  else str(source_names))
    banner = _banner(indicator_name, start_date, end_date, source_str)

    # --- report (cross-tab pivots) ---
    rbuf = io.StringIO()
    for line in banner:
        rbuf.write(line + "\n")
    rw = csv.writer(rbuf)
    report_rows = 0
    for title, table in report_tables:
        rbuf.write(f"\n# {title}\n")
        rw.writerow(table["header"])
        for row in table["rows"]:
            rw.writerow(row)
            report_rows += 1
        rw.writerow(table["total"])
    report_path = DASHBOARD_EXPORTS_DIR / f"{stem}_report.csv"
    report_path.write_text(rbuf.getvalue(), encoding="utf-8")

    # --- line list (affected clients) ---
    lbuf = io.StringIO()
    for line in banner:
        lbuf.write(line + "\n")
    lw = csv.writer(lbuf)
    cols = list(affected[0]["_raw"].keys()) if affected and affected[0].get("_raw") else []
    if cols:
        lw.writerow(cols)
        for r in affected:
            raw = r.get("_raw", {})
            lw.writerow([raw.get(c, "") for c in cols])
    linelist_path = DASHBOARD_EXPORTS_DIR / f"{stem}_linelist.csv"
    linelist_path.write_text(lbuf.getvalue(), encoding="utf-8")

    return {"ok": True, "report_path": str(report_path),
            "linelist_path": str(linelist_path),
            "report_rows": report_rows, "linelist_rows": len(affected)}
