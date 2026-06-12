"""Database connection helpers.

db_connect() pins use_pure=True for PyInstaller-frozen builds; PRESERVED
VERBATIM (MIGRATION_PLAN.md section 2).
"""
from __future__ import annotations

import time

import mysql.connector as mysql_connector
from mysql.connector import Error  # re-exported for callers

_mysql_connect = mysql_connector.connect


def db_connect(**kwargs):
    """mysql.connector.connect() pinned to the pure-Python client (use_pure)."""
    kwargs.setdefault("use_pure", True)
    return _mysql_connect(**kwargs)


def _wait_for_mysql(db_cfg, log_func=print, max_wait_s: int = 60) -> None:
    """Try to connect to MySQL, retrying every 5s until reachable or timeout.

    Used by automated runs (@reboot) where MySQL may not be up yet. Raises
    RuntimeError if MySQL never becomes reachable within max_wait_s.
    """
    deadline = time.monotonic() + max_wait_s
    last_err = None
    while time.monotonic() < deadline:
        try:
            conn = db_connect(
                host=db_cfg["host"], user=db_cfg["user"], password=db_cfg["password"],
                database=db_cfg["database"], port=int(db_cfg.get("port", 3306)),
                connection_timeout=5,
            )
            conn.close()
            return
        except Exception as e:
            last_err = e
            log_func(f"[BACKUP] waiting for MySQL... ({e})")
            time.sleep(5)
    raise RuntimeError(f"MySQL not reachable after {max_wait_s}s: {last_err}")


def _mysql_db_exists(db_cfg, db_name: str) -> bool:
    conn = db_connect(
        host=db_cfg["host"], user=db_cfg["user"], password=db_cfg["password"],
        port=int(db_cfg.get("port", 3306)), connection_timeout=8,
    )
    try:
        cur = conn.cursor()
        cur.execute("SHOW DATABASES LIKE %s", (db_name,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def _mysql_admin(db_cfg, statements):
    """Run admin statements with no database selected. `statements` is iterable."""
    conn = db_connect(
        host=db_cfg["host"], user=db_cfg["user"], password=db_cfg["password"],
        port=int(db_cfg.get("port", 3306)), connection_timeout=8,
    )
    try:
        cur = conn.cursor()
        for s in statements:
            cur.execute(s)
        conn.commit()
    finally:
        conn.close()

