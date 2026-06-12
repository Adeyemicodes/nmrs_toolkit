"""Linelist workflow: SQL script execution (DELIMITER-aware), CSV emit, and the
weekly batch. Execution + splitter logic PRESERVED VERBATIM. append_linelist_log
now routes through the unified AppLogger (category LINELIST).
"""
from __future__ import annotations

import csv
import re
import time
from datetime import datetime
from io import StringIO
from pathlib import Path

from ..constants import (
    LINELIST_DIR, LINELIST_LOG_FILE, LINELIST_WEEK_MARKER, batch_linelists,
)
from ..crypto import encrypt_bytes, get_facility_key
from ..db import db_connect, _wait_for_mysql
from ..logger import get_logger

_DELIMITER_RE = re.compile(r"^\s*DELIMITER\s+(\S+)\s*$", re.IGNORECASE)


def _has_delimiter_directive(sql) -> bool:
    for line in sql.splitlines():
        if _DELIMITER_RE.match(line):
            return True
    return False


def _iter_result_sets(cursor, sql):
    """Run a (possibly multi-statement) `sql` and yield each result set as an
    object exposing .with_rows / .description / .fetchall().

    Bridges a mysql-connector-python API change so the same code works whether
    the build machine has 8.x or 9.x installed:
      * 8.x: cursor.execute(sql, multi=True) returns a lazy iterator of
        per-statement result cursors.
      * 9.x: the `multi` argument was removed (passing it raises TypeError).
        A single execute() runs every statement — MULTI_STATEMENTS is on by
        default — and later result sets are reached via nextset().
    """
    try:
        multi_iter = cursor.execute(sql, multi=True)
    except TypeError:
        cursor.execute(sql)
        while True:
            yield cursor
            if not cursor.nextset():
                break
    else:
        yield from multi_iter


def _split_with_delimiters(sql):
    """Split a SQL script into individual statements.

    Honors `DELIMITER xxx` directives and ignores any delimiter that falls
    inside a string literal, a backtick-quoted identifier, or a comment.
    The scan is character-by-character rather than line-by-line because:
      * statements may share a line (e.g. "SET @a=0;SET @b=0;"), so
        splitting only at end-of-line ';' would merge them into one blob
        that execute() then runs as a hidden multi-statement — breaking
        result handling ("Commands out of sync" on the C client), and
      * a routine body keeps its inner ';' only while a custom delimiter
        (e.g. $$) is active.
    Returns a flat list of statements (delimiter stripped, blanks dropped).
    """
    statements = []
    delimiter = ";"
    buf = []
    has_sql = False  # does buf hold anything beyond comments/whitespace?
    i, n = 0, len(sql)
    at_line_start = True  # only here is a DELIMITER directive recognized

    def flush():
        # Skip comment-only / blank chunks — executing one raises 1065
        # "Query was empty" (the file's trailing comments are the usual case).
        nonlocal has_sql
        if has_sql:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
        buf.clear()
        has_sql = False

    while i < n:
        ch = sql[i]

        # DELIMITER directive — at the start of a line, outside any quoting.
        # Consumes the rest of the line and switches the active delimiter.
        if (at_line_start and sql[i:i + 9].upper() == "DELIMITER"
                and (i + 9 >= n or sql[i + 9] in " \t")):
            flush()
            eol = sql.find("\n", i)
            eol = n if eol == -1 else eol
            new_delim = sql[i + 9:eol].strip()
            if new_delim:
                delimiter = new_delim
            i = eol + 1
            at_line_start = True
            continue

        # line comment: "-- " (dash-dash + whitespace/EOL) or "#" — to EOL.
        if ch == "#" or (sql[i:i + 2] == "--"
                         and (i + 2 >= n or sql[i + 2] in " \t\r\n")):
            eol = sql.find("\n", i)
            eol = n if eol == -1 else eol
            buf.append(sql[i:eol])
            i = eol
            at_line_start = False
            continue

        # block comment /* ... */ — kept verbatim. A /*! ... */ conditional
        # comment is executable SQL (MySQL runs it), so it counts as content.
        if sql[i:i + 2] == "/*":
            end = sql.find("*/", i + 2)
            end = n if end == -1 else end + 2
            buf.append(sql[i:end])
            if sql[i:i + 3] == "/*!":
                has_sql = True
            i = end
            at_line_start = False
            continue

        # string literal ('..','"..') or backtick-quoted identifier (`..`).
        if ch in ("'", '"', "`"):
            buf.append(ch)
            i += 1
            while i < n:
                c = sql[i]
                buf.append(c)
                if c == "\\" and ch != "`" and i + 1 < n:  # backslash escape
                    buf.append(sql[i + 1])
                    i += 2
                    continue
                if c == ch:
                    if i + 1 < n and sql[i + 1] == ch:  # doubled => escaped
                        buf.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            at_line_start = False
            has_sql = True  # a string/identifier literal is real SQL
            continue

        # active delimiter -> statement boundary.
        if sql[i:i + len(delimiter)] == delimiter:
            flush()
            i += len(delimiter)
            at_line_start = False
            continue

        buf.append(ch)
        if ch == "\n":
            at_line_start = True
        elif ch not in " \t\r":
            at_line_start = False  # whitespace keeps us "at line start"
            has_sql = True
        i += 1

    flush()
    return statements


def execute_sql_script(db_cfg, sql):
    """Open a fresh DB connection, run `sql` (multi-statement), and return
    (columns, rows, statement_count) for the LAST result set that produced rows.

    `db_cfg` is the [database] config section (or any mapping with host/user/
    password/database/port). See the two execution paths in _iter_result_sets /
    _split_with_delimiters for why DELIMITER scripts are handled separately.
    """
    conn = db_connect(
        host=db_cfg["host"], user=db_cfg["user"], password=db_cfg["password"],
        database=db_cfg["database"], port=int(db_cfg.get("port", 3306)),
        autocommit=True,
    )
    cols, rows, stmt_count = [], [], 0
    try:
        cursor = conn.cursor()
        try:
            if _has_delimiter_directive(sql):
                for stmt in _split_with_delimiters(sql):
                    stmt_count += 1
                    cursor.execute(stmt)
                    if cursor.with_rows:
                        cols = [d[0] for d in cursor.description]
                        rows = cursor.fetchall()
            else:
                for result in _iter_result_sets(cursor, sql):
                    stmt_count += 1
                    if result.with_rows:
                        cols = [d[0] for d in result.description]
                        rows = result.fetchall()
        finally:
            cursor.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return cols, rows, stmt_count


def linelist_rows_to_csv_bytes(cols, rows) -> bytes:
    """Serialize a result set to UTF-8 CSV bytes (header row + data rows)."""
    buf = StringIO()
    w = csv.writer(buf)
    if cols:
        w.writerow(cols)
    for r in rows:
        w.writerow(["" if v is None else str(v) for v in r])
    return buf.getvalue().encode("utf-8")


def write_linelist_csv(cols, rows, target: Path, encrypt: bool, key) -> int:
    """Write a result set to `target` as plain CSV, or AES-GCM encrypted bytes
    when encrypt=True. Returns the uncompressed payload size in bytes."""
    payload = linelist_rows_to_csv_bytes(cols, rows)
    target.parent.mkdir(parents=True, exist_ok=True)
    if encrypt:
        target.write_bytes(encrypt_bytes(payload, key))
    else:
        target.write_bytes(payload)
    return len(payload)


def append_linelist_log(line: str) -> None:
    """Persist one linelist log line via the unified AppLogger (category LINELIST).

    The logger fans out to APPLICATION_LOG_FILE and LINELIST_LOG_FILE. Signature
    unchanged so headless + GUI call sites keep working.
    """
    get_logger().emit(line, category="LINELIST")


def _linelist_output_path(display_name: str, encrypt: bool) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d")
    ext = ".csv.nmrs" if encrypt else ".csv"
    return LINELIST_DIR / f"{display_name}_Linelist_{stamp}{ext}"


def perform_linelist_batch(config: configparser.ConfigParser, log_func=print,
                           encrypt=None, wait_for_mysql: bool = False) -> dict:
    """Generate every weekly-batch linelist (Treatment, PMTCT, EAC, AHD) into
    LINELIST_DIR. Always (re)generates — week-level idempotency is the caller's
    job (see run_headless_linelists). Each script runs independently so one
    failure doesn't abort the rest.

    encrypt: True/False forces the mode; None reads [linelist] encrypt (default
    false). When encrypting, output uses the facility backup key (.csv.nmrs).

    Returns {"written": [Path, ...], "failed": [(display, error), ...]}.
    """
    LINELIST_DIR.mkdir(parents=True, exist_ok=True)
    if encrypt is None:
        encrypt = config.getboolean("linelist", "encrypt", fallback=False)
    db = config["database"]
    if wait_for_mysql:
        _wait_for_mysql(db, log_func)
    key = get_facility_key(config) if encrypt else None

    written, failed, skipped = [], [], []
    items = batch_linelists()
    if not items:
        log_func("[LINELIST] no batch linelists present under scripts/ — nothing to do")
        return {"written": [], "failed": [], "skipped": []}

    log_func(f"[LINELIST] batch start: {len(items)} linelist(s), encrypt={encrypt} -> {LINELIST_DIR}")
    for display, path in items:
        t0 = time.monotonic()
        try:
            sql = path.read_text(encoding="utf-8")
            cols, rows, stmt_count = execute_sql_script(db, sql)
            elapsed = time.monotonic() - t0
            # Don't create an empty/headers-only file for a script that returned
            # zero result rows. Counted as "skipped", not "failed" — the script
            # ran fine, there was just nothing to write.
            if not rows:
                log_func(f"[LINELIST] {display}: 0 row(s) in {elapsed:.1f}s — no file written")
                skipped.append(display)
                continue
            out = _linelist_output_path(display, encrypt)
            size = write_linelist_csv(cols, rows, out, encrypt, key)
            log_func(f"[LINELIST] {display}: {len(rows)} row(s), {size:,} bytes in "
                     f"{elapsed:.1f}s -> {out.name}")
            written.append(out)
        except Exception as e:
            elapsed = time.monotonic() - t0
            log_func(f"[LINELIST] {display} FAILED after {elapsed:.1f}s: {e}")
            failed.append((display, str(e)))
    log_func(f"[LINELIST] batch done: {len(written)} written, "
             f"{len(skipped)} skipped (0 rows), {len(failed)} failed")
    return {"written": written, "failed": failed, "skipped": skipped}


def _current_week_id(dt: datetime = None) -> str:
    """ISO year-week identifier, e.g. '2026-W22'."""
    dt = dt or datetime.now()
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _read_linelist_week_marker() -> str:
    try:
        return LINELIST_WEEK_MARKER.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_linelist_week_marker(week_id: str) -> None:
    try:
        LINELIST_DIR.mkdir(parents=True, exist_ok=True)
        LINELIST_WEEK_MARKER.write_text(week_id, encoding="utf-8")
    except OSError:
        pass

