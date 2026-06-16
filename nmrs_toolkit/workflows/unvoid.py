"""Unvoid + Reverse-Unvoid workflow (PRESERVED VERBATIM from the legacy Tk app).

Reverses an erroneous patient void. Safety model:
  * Gate on the patient row's void_reason — only reasons in the configured
    accepted set may be unvoided.
  * Anchor on that row's date_voided; unvoid every timestamp-bearing table
    within ±window_seconds of it. Only the most recent void cluster is touched.
  * Identity tables (person_name/address/attribute) often lack a reliable
    date_voided, so only their single most recent voided row is unvoided.
  * Every mutated row is logged to nmrs_unvoid_op_row with its prior void state
    BEFORE the update, so Reverse can re-void exactly those rows.

The Tk methods became module functions; the SQL and transaction handling are
byte-for-byte the v1.2.0 behavior.
"""
from __future__ import annotations

import re
from datetime import timedelta

from mysql.connector import Error

from ..logger import get_logger

# Timestamp-windowed tables: (table, pk_column, key_column). key_column holds
# the patient/person id; for patients person_id == patient_id.
UNVOID_WINDOW_TABLES = [
    ("patient_identifier", "patient_identifier_id", "patient_id"),
    ("patient_program",    "patient_program_id",    "patient_id"),
    ("person",             "person_id",             "person_id"),
    ("visit",              "visit_id",              "patient_id"),
    ("encounter",          "encounter_id",          "patient_id"),
    ("obs",                "obs_id",                "person_id"),
]
# Sensitive identity tables: unvoid most-recent voided row only.
UNVOID_IDENTITY_TABLES = [
    ("person_name",      "person_name_id",      "person_id"),
    ("person_address",   "person_address_id",   "person_id"),
    ("person_attribute", "person_attribute_id", "person_id"),
]


def append_unvoid_log(line: str) -> None:
    """Persist one unvoid/reverse log line via the AppLogger (category UNVOID)."""
    get_logger().emit(line, category="UNVOID")


def get_accepted_reasons(config) -> list:
    raw = config.get(
        "settings", "unvoid_accepted_reasons",
        fallback="Bulk void via ART/DATIM mapping, Duplicate Client",
    )
    return [r.strip() for r in raw.split(",") if r.strip()]


def get_window_seconds(config) -> int:
    try:
        return int(config.get("settings", "unvoid_window_seconds", fallback="120"))
    except (ValueError, TypeError):
        return 120


def ensure_unvoid_schema(cursor) -> None:
    """Create the reversible-audit tables if absent. CREATE TABLE is DDL and
    auto-commits in MySQL, so call this BEFORE opening the data transaction."""
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS nmrs_unvoid_op (
            op_id              INT AUTO_INCREMENT PRIMARY KEY,
            op_time            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            op_type            VARCHAR(10)  NOT NULL,
            identifier         VARCHAR(50)  NOT NULL,
            patient_id         INT          NOT NULL,
            patient_name       VARCHAR(255),
            anchor_date_voided DATETIME,
            window_seconds     INT          NOT NULL,
            accepted_reason    VARCHAR(255),
            executed_by        VARCHAR(100),
            status             VARCHAR(20)  NOT NULL,
            rows_affected      INT          NOT NULL DEFAULT 0,
            reversed_op_id     INT          NULL,
            remarks            TEXT,
            INDEX idx_unvoid_op_patient (patient_id),
            INDEX idx_unvoid_op_identifier (identifier),
            INDEX idx_unvoid_op_time (op_time)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS nmrs_unvoid_op_row (
            row_id            INT AUTO_INCREMENT PRIMARY KEY,
            op_id             INT          NOT NULL,
            table_name        VARCHAR(64)  NOT NULL,
            pk_column         VARCHAR(64)  NOT NULL,
            pk_value          INT          NOT NULL,
            prev_voided       TINYINT,
            prev_date_voided  DATETIME,
            prev_voided_by    INT,
            prev_void_reason  VARCHAR(255),
            INDEX idx_unvoid_row_op (op_id),
            INDEX idx_unvoid_row_table_pk (table_name, pk_value)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def tokenize_identifiers(raw: str) -> list:
    """Split on any common separator, de-duplicate, preserve order."""
    seen = set()
    out = []
    for tok in re.split(r"[,\n\t;|\s]+", raw or ""):
        t = tok.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def lookup_patient(cursor, identifier, accepted, window):
    """Return (patient_dict, None) if unvoidable, else (None, skip_reason).
    cursor must be a dictionary cursor."""
    cursor.execute(
        """
        SELECT pi.patient_id, pi.identifier,
               CONCAT(pn.given_name, ' ', IFNULL(pn.family_name, '')) AS patient_name,
               p.gender, p.birthdate,
               pat.date_voided AS patient_date_voided,
               pat.void_reason AS patient_void_reason
        FROM patient_identifier pi
        JOIN person p   ON pi.patient_id = p.person_id
        JOIN patient pat ON pi.patient_id = pat.patient_id
        LEFT JOIN person_name pn ON p.person_id = pn.person_id
        WHERE pi.identifier = %s AND pi.voided = 1
        ORDER BY pn.preferred DESC, pn.date_created DESC
        LIMIT 1
        """,
        (identifier,),
    )
    result = cursor.fetchone()
    if not result:
        cursor.execute(
            "SELECT patient_id FROM patient_identifier "
            "WHERE identifier = %s AND voided = 0 LIMIT 1",
            (identifier,),
        )
        if cursor.fetchone():
            return None, "already active (not voided)"
        return None, "not found in database"

    reason = result.get("patient_void_reason")
    if reason not in accepted:
        return None, f"void reason '{reason or 'NULL'}' not in accepted set"
    if not result.get("patient_date_voided"):
        return None, "no date_voided timestamp on patient row"

    anchor = result["patient_date_voided"]
    result["time_start"] = anchor - timedelta(seconds=window)
    result["time_end"] = anchor + timedelta(seconds=window)
    result["window_seconds"] = window
    result["accepted_reason"] = reason
    return result, None


def capture_and_clear(cursor, op_id, table, pk_col, where_sql, params):
    """Capture the prior void state of every row matching `where_sql` into
    nmrs_unvoid_op_row, then unvoid exactly those rows (by captured PK).
    Returns the number of rows unvoided. table/pk_col are internal constants
    (never user input), so f-string interpolation is safe."""
    cursor.execute(
        f"SELECT {pk_col} AS pk, voided, date_voided, voided_by, void_reason "
        f"FROM {table} WHERE {where_sql}",
        params,
    )
    rows = cursor.fetchall()
    if not rows:
        return 0
    for r in rows:
        cursor.execute(
            "INSERT INTO nmrs_unvoid_op_row "
            "(op_id, table_name, pk_column, pk_value, prev_voided, "
            " prev_date_voided, prev_voided_by, prev_void_reason) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (op_id, table, pk_col, r["pk"], r["voided"],
             r["date_voided"], r["voided_by"], r["void_reason"]),
        )
    pks = [r["pk"] for r in rows]
    placeholders = ",".join(["%s"] * len(pks))
    cursor.execute(
        f"UPDATE {table} SET voided = 0, voided_by = NULL, "
        f"date_voided = NULL, void_reason = NULL "
        f"WHERE {pk_col} IN ({placeholders})",
        pks,
    )
    return cursor.rowcount


def unvoid_one(conn, p, accepted, admin_name):
    """Unvoid a single patient as its own committed transaction.
    Returns (op_id, rows_unvoided). Raises on failure (rolls back)."""
    patient_id = p["patient_id"]
    time_start, time_end = p["time_start"], p["time_end"]
    cursor = conn.cursor(dictionary=True, buffered=True)
    try:
        cursor.execute(
            "INSERT INTO nmrs_unvoid_op "
            "(op_type, identifier, patient_id, patient_name, "
            " anchor_date_voided, window_seconds, accepted_reason, "
            " executed_by, status) "
            "VALUES ('UNVOID', %s, %s, %s, %s, %s, %s, %s, 'IN_PROGRESS')",
            (p["identifier"], patient_id, p["patient_name"],
             p["patient_date_voided"], p["window_seconds"],
             p["accepted_reason"], admin_name),
        )
        op_id = cursor.lastrowid
        total = 0

        # 1. patient table — re-check void_reason for safety.
        ph = ",".join(["%s"] * len(accepted))
        total += capture_and_clear(
            cursor, op_id, "patient", "patient_id",
            f"patient_id = %s AND voided = 1 AND void_reason IN ({ph}) "
            f"AND date_voided BETWEEN %s AND %s",
            (patient_id, *accepted, time_start, time_end),
        )

        # 2. timestamp-windowed tables.
        for table, pk_col, key_col in UNVOID_WINDOW_TABLES:
            total += capture_and_clear(
                cursor, op_id, table, pk_col,
                f"{key_col} = %s AND voided = 1 "
                f"AND date_voided BETWEEN %s AND %s",
                (patient_id, time_start, time_end),
            )

        # 3. identity tables — most recent voided row only.
        for table, pk_col, key_col in UNVOID_IDENTITY_TABLES:
            cursor.execute(
                f"SELECT {pk_col} AS pk FROM {table} "
                f"WHERE {key_col} = %s AND voided = 1 "
                f"ORDER BY COALESCE(date_voided, date_created) DESC LIMIT 1",
                (patient_id,),
            )
            row = cursor.fetchone()
            if row:
                total += capture_and_clear(
                    cursor, op_id, table, pk_col,
                    f"{pk_col} = %s", (row["pk"],),
                )

        cursor.execute(
            "UPDATE nmrs_unvoid_op SET status = 'SUCCESS', rows_affected = %s, "
            "remarks = %s WHERE op_id = %s",
            (total,
             f"Unvoided within ±{p['window_seconds']}s of "
             f"{p['patient_date_voided']}; reason '{p['accepted_reason']}'.",
             op_id),
        )
        conn.commit()
        return op_id, total
    except Error:
        conn.rollback()
        raise
    finally:
        cursor.close()


def reverse_one(conn, orig_op_id, admin_name):
    """Re-void exactly the rows logged for orig_op_id. Returns
    (reverse_op_id, restored, skipped). Raises on failure (rolls back)."""
    cursor = conn.cursor(dictionary=True, buffered=True)
    try:
        cursor.execute(
            "SELECT identifier, patient_id, patient_name, anchor_date_voided, "
            "       window_seconds, accepted_reason, reversed_op_id "
            "FROM nmrs_unvoid_op WHERE op_id = %s AND op_type = 'UNVOID'",
            (orig_op_id,),
        )
        op = cursor.fetchone()
        if not op:
            raise Error(f"Unvoid operation {orig_op_id} not found.")
        if op["reversed_op_id"] is not None:
            raise Error(f"Operation {orig_op_id} has already been reversed "
                        f"(by op {op['reversed_op_id']}).")

        cursor.execute(
            "SELECT table_name, pk_column, pk_value, prev_voided, "
            "       prev_date_voided, prev_voided_by, prev_void_reason "
            "FROM nmrs_unvoid_op_row WHERE op_id = %s",
            (orig_op_id,),
        )
        detail = cursor.fetchall()

        cursor.execute(
            "INSERT INTO nmrs_unvoid_op "
            "(op_type, identifier, patient_id, patient_name, "
            " anchor_date_voided, window_seconds, accepted_reason, "
            " executed_by, status, reversed_op_id) "
            "VALUES ('REVERSE', %s, %s, %s, %s, %s, %s, %s, 'IN_PROGRESS', %s)",
            (op["identifier"], op["patient_id"], op["patient_name"],
             op["anchor_date_voided"], op["window_seconds"],
             op["accepted_reason"], admin_name, orig_op_id),
        )
        rev_op_id = cursor.lastrowid

        restored = skipped = 0
        for d in detail:
            table, pk_col, pk = d["table_name"], d["pk_column"], d["pk_value"]
            cursor.execute(
                f"SELECT voided, date_voided, voided_by, void_reason "
                f"FROM {table} WHERE {pk_col} = %s",
                (pk,),
            )
            cur = cursor.fetchone()
            if cur is None:
                skipped += 1
                continue
            cursor.execute(
                "INSERT INTO nmrs_unvoid_op_row "
                "(op_id, table_name, pk_column, pk_value, prev_voided, "
                " prev_date_voided, prev_voided_by, prev_void_reason) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (rev_op_id, table, pk_col, pk, cur["voided"],
                 cur["date_voided"], cur["voided_by"], cur["void_reason"]),
            )
            cursor.execute(
                f"UPDATE {table} SET voided = %s, date_voided = %s, "
                f"voided_by = %s, void_reason = %s "
                f"WHERE {pk_col} = %s AND voided = 0",
                (d["prev_voided"], d["prev_date_voided"],
                 d["prev_voided_by"], d["prev_void_reason"], pk),
            )
            if cursor.rowcount:
                restored += 1
            else:
                skipped += 1

        cursor.execute(
            "UPDATE nmrs_unvoid_op SET status = 'SUCCESS', rows_affected = %s, "
            "remarks = %s WHERE op_id = %s",
            (restored,
             f"Reversed op {orig_op_id}: {restored} re-voided, {skipped} skipped.",
             rev_op_id),
        )
        cursor.execute(
            "UPDATE nmrs_unvoid_op SET reversed_op_id = %s WHERE op_id = %s",
            (rev_op_id, orig_op_id),
        )
        conn.commit()
        return rev_op_id, restored, skipped
    except Error:
        conn.rollback()
        raise
    finally:
        cursor.close()
