"""Dashboard CSV exports. Written to DASHBOARD_EXPORTS_DIR (NOT LINELIST_DIR),
each carrying a mandatory banner so it can never be mistaken for a live linelist.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path

from ..constants import DASHBOARD_EXPORTS_DIR


def _flatten(indicator: dict):
    """Flatten an indicator dict into (metric, group, value) rows. Generic over
    the shapes the engine produces (total + by_* breakdowns, by_reason, etc.)."""
    rows = []
    for key, val in indicator.items():
        if isinstance(val, dict):
            for group, count in val.items():
                rows.append((key, str(group), count))
        elif not isinstance(val, (list,)):
            rows.append((key, "", val))
    return rows


def write_export(indicator_slug: str, indicator_name: str,
                 start_date: str, end_date: str, source_names,
                 indicator: dict) -> dict:
    """Write a banner-headed export CSV. Returns {ok, path, rows, header}."""
    DASHBOARD_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    gen = datetime.now()
    sd = start_date.replace("-", "")
    ed = end_date.replace("-", "")
    fname = (f"Dashboard_{indicator_slug}_{sd}_to_{ed}"
             f"_generated_{gen.strftime('%Y%m%d%H%M')}.csv")
    target = DASHBOARD_EXPORTS_DIR / fname

    if isinstance(source_names, (list, tuple)):
        source_str = ", ".join(source_names)
    else:
        source_str = str(source_names)

    banner = [
        f"# DASHBOARD EXPORT - Indicator: {indicator_name}",
        f"# Period: {start_date} to {end_date}  -  Snapshot indicators reflect end={end_date}",
        f"# Generated: {gen.isoformat(timespec='seconds')}  -  Source: {source_str}",
        "# THIS IS NOT A CURRENT LINELIST - Do not use for current program decisions",
    ]

    buf = io.StringIO()
    for line in banner:
        buf.write(line + "\n")
    w = csv.writer(buf)
    w.writerow(["metric", "group", "value"])
    flat = _flatten(indicator)
    for metric, group, value in flat:
        w.writerow([metric, group, value])

    target.write_text(buf.getvalue(), encoding="utf-8")
    return {"ok": True, "path": str(target), "rows": len(flat), "header": banner[0]}
