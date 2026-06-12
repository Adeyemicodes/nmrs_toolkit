"""CSV merge workflow, extracted from the legacy Tk worker (PRESERVED VERBATIM).

Reads one or more CSV inputs (decrypting .nmrs / NMRS-encrypted files with the
facility key), unions their columns in first-seen order, optionally sorts by a
column, and writes a single CSV (optionally AES-GCM encrypted). The merge logic
is byte-for-byte the v1.2.0 behavior; the Tk progress callback became a
`log_func` argument.
"""
from __future__ import annotations

import csv
from io import StringIO
from pathlib import Path

from ..crypto import decrypt_bytes, encrypt_bytes, get_facility_key, is_encrypted_file
from ..logger import get_logger


def append_merge_log(line: str) -> None:
    """Persist one merge log line via the unified AppLogger (category MERGE).

    In v1.2.0 merge activity was in-memory only; it now lands in
    APPLICATION_LOG_FILE (MERGE has no dedicated per-workflow file)."""
    get_logger().emit(line, category="MERGE")


def merge_csvs(files, target: Path, encrypt: bool, sort_col: str,
               sort_desc: bool, config, log_func=print) -> dict:
    """Merge `files` into `target`. Returns {"n_rows", "n_cols"}; raises on error.

    Column union preserves first-seen order across inputs; rows missing a column
    are written blank. Sort (when `sort_col` is a merged header) is a stable sort
    on the string value, descending if `sort_desc`.
    """
    all_headers: list = []
    all_rows: list = []

    for path_str in files:
        path = Path(path_str)
        log_func(f"Reading {path.name}")
        raw = path.read_bytes()
        if is_encrypted_file(path) or path.suffix.lower() == ".nmrs":
            raw = decrypt_bytes(raw, get_facility_key(config))
            log_func(f"  decrypted ({len(raw):,} bytes)")
        text = raw.decode("utf-8", errors="replace")
        reader = csv.DictReader(text.splitlines())
        file_headers = reader.fieldnames or []
        for h in file_headers:
            if h not in all_headers:
                all_headers.append(h)
        n_before = len(all_rows)
        for row in reader:
            all_rows.append(row)
        log_func(f"  +{len(all_rows) - n_before} row(s); columns: {file_headers}")

    if sort_col:
        if sort_col not in all_headers:
            log_func(f"Sort column '{sort_col}' not in merged headers; skipping sort")
        else:
            all_rows.sort(key=lambda r: (r.get(sort_col) or ""), reverse=sort_desc)
            log_func(f"Sorted by '{sort_col}' ({'desc' if sort_desc else 'asc'})")

    buf = StringIO()
    w = csv.DictWriter(buf, fieldnames=all_headers)
    w.writeheader()
    for r in all_rows:
        w.writerow({h: r.get(h, "") for h in all_headers})
    payload = buf.getvalue().encode("utf-8")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if encrypt:
        target.write_bytes(encrypt_bytes(payload, get_facility_key(config)))
    else:
        target.write_bytes(payload)
    log_func(f"Wrote {len(all_rows)} row(s), {len(all_headers)} column(s) -> {target}")
    return {"n_rows": len(all_rows), "n_cols": len(all_headers)}
