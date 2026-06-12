"""Backup workflow: mysqldump (or python fallback) -> gzip -> AES-GCM.

Dump/encrypt logic PRESERVED VERBATIM. append_backup_log now routes through
the unified AppLogger (category BACKUP), which writes both APPLICATION_LOG_FILE
and BACKUP_LOG_FILE.
"""
from __future__ import annotations

import configparser
import gzip
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from ..constants import BACKUP_DIR, BACKUP_LOG_FILE, _NO_WINDOW
from ..crypto import encrypt_bytes, get_facility_key
from ..db import db_connect, _wait_for_mysql
from ..logger import get_logger

_FACILITY_SLUG_MAX = 100
_FACILITY_SAFE_RE = re.compile(r"[^A-Za-z0-9]+")


def _sanitize_facility(name: str) -> str:
    """Make a facility name filename-safe: alphanumerics + underscores, capped."""
    slug = _FACILITY_SAFE_RE.sub("_", (name or "").strip()).strip("_")
    if not slug:
        return "unknown_facility"
    return slug[:_FACILITY_SLUG_MAX]


def _facility_slug(config: configparser.ConfigParser, log_func=print) -> str:
    """Resolve the facility slug used in backup filenames.

    Priority:
      1. [settings] facility_name in config (manual override).
      2. SELECT property_value FROM global_property WHERE property = 'Facility_Name'.
      3. Fall back to "unknown_facility" (logged as a warning).
    """
    override = config.get("settings", "facility_name", fallback="").strip()
    if override:
        return _sanitize_facility(override)

    db = config["database"]
    try:
        conn = db_connect(
            host=db["host"], user=db["user"], password=db["password"],
            database=db["database"], port=int(db.get("port", 3306)),
            connection_timeout=8,
        )
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT property_value FROM global_property "
                "WHERE property = 'Facility_Name' LIMIT 1"
            )
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception as e:
        log_func(f"[BACKUP] facility lookup failed ({e}); using 'unknown_facility'")
        return "unknown_facility"

    if not row or not row[0]:
        log_func("[BACKUP] Facility_Name global_property empty; using 'unknown_facility'")
        return "unknown_facility"
    return _sanitize_facility(str(row[0]))


def _todays_backup_exists(facility: str):
    """If a backup file for today already exists in BACKUP_DIR, return its path,
    else None. Returns: Optional[Path]."""
    if not BACKUP_DIR.exists():
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    # Match any facility's backup for today (covers cases where the facility
    # slug changed between runs but a backup already exists).
    for p in BACKUP_DIR.glob(f"*nmrs_backup_{today}_*.sql.gz.enc"):
        return p
    return None


def perform_backup(config: configparser.ConfigParser, log_func=print,
                   force: bool = False, wait_for_mysql: bool = False) -> Path:
    """Run mysqldump (or python fallback), gzip + encrypt, write to BACKUP_DIR.

    Returns the output path. Raises on failure.

    Behavior:
      - If force=False and a backup file for today already exists in
        BACKUP_DIR, returns that existing path without running a new dump.
      - If wait_for_mysql=True (used by automated @reboot runs), polls MySQL
        for up to 60s before attempting the dump.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    db = config["database"]

    if not force:
        existing = _todays_backup_exists("")
        if existing is not None:
            log_func(f"[BACKUP] skip — today already backed up: {existing.name}")
            return existing

    if wait_for_mysql:
        _wait_for_mysql(db, log_func)

    facility = _facility_slug(config, log_func)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    out_path = BACKUP_DIR / f"{facility}_nmrs_backup_{stamp}.sql.gz.enc"

    log_func(f"[BACKUP] start db={db['database']} host={db['host']} facility={facility} -> {out_path.name}")

    dump_bytes = _dump_database(db, log_func)
    log_func(f"[BACKUP] dump complete ({len(dump_bytes):,} bytes uncompressed)")

    gzipped = gzip.compress(dump_bytes, compresslevel=6)
    log_func(f"[BACKUP] gzipped to {len(gzipped):,} bytes")

    key = get_facility_key(config)
    encrypted = encrypt_bytes(gzipped, key)
    log_func(f"[BACKUP] encrypted to {len(encrypted):,} bytes")

    with open(out_path, "wb") as f:
        f.write(encrypted)
    log_func(f"[BACKUP] wrote {out_path}")

    retention = config.getint("backup", "retention_count", fallback=10)
    _prune_backups(BACKUP_DIR, "*nmrs_backup_*.sql.gz.enc",
                   keep=retention, log_func=log_func, label="BACKUP")
    return out_path


def _prune_backups(folder: Path, pattern: str, keep: int,
                   log_func=print, label: str = "BACKUP") -> None:
    """Keep the `keep` most recent files matching `pattern` in `folder`,
    delete the rest. Stateless — safe against manual deletions."""
    if keep < 1:
        return
    try:
        candidates = sorted(folder.glob(pattern),
                            key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as e:
        log_func(f"[{label}] retention scan failed: {e}")
        return
    for p in candidates[keep:]:
        try:
            p.unlink()
            log_func(f"[{label}] pruned old backup: {p.name}")
        except OSError as e:
            log_func(f"[{label}] could not prune {p.name}: {e}")


def _dump_database(db_cfg, log_func) -> bytes:
    """Return SQL dump as bytes. Prefer mysqldump, fall back to python."""
    mysqldump = shutil.which("mysqldump")
    if mysqldump:
        cmd = [
            mysqldump,
            f"-h{db_cfg['host']}",
            f"-P{db_cfg.get('port', '3306')}",
            f"-u{db_cfg['user']}",
            f"-p{db_cfg['password']}",
            "--single-transaction",
            "--quick",
            "--skip-lock-tables",
            "--routines",
            "--triggers",
            db_cfg["database"],
        ]
        p = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
        if p.returncode != 0:
            raise RuntimeError(f"mysqldump failed: {p.stderr.decode('utf-8', 'replace')[:500]}")
        return p.stdout

    log_func("[BACKUP] mysqldump not on PATH; using python fallback (slower)")
    return _python_dump(db_cfg)


def _python_dump(db_cfg) -> bytes:
    """Naive Python-side dump: schema + data, INSERT statements, no triggers/routines."""
    conn = db_connect(
        host=db_cfg["host"],
        user=db_cfg["user"],
        password=db_cfg["password"],
        database=db_cfg["database"],
        port=int(db_cfg.get("port", 3306)),
    )
    out = []
    try:
        cursor = conn.cursor()
        cursor.execute("SHOW TABLES")
        tables = [r[0] for r in cursor.fetchall()]
        out.append(f"-- NMRS Toolkit python fallback dump @ {datetime.now().isoformat()}\n")
        out.append(f"-- database: {db_cfg['database']}\n")
        out.append("SET FOREIGN_KEY_CHECKS=0;\n")
        for t in tables:
            cursor.execute(f"SHOW CREATE TABLE `{t}`")
            ddl = cursor.fetchone()[1]
            out.append(f"\nDROP TABLE IF EXISTS `{t}`;\n{ddl};\n")
            cursor.execute(f"SELECT * FROM `{t}`")
            cols = [d[0] for d in cursor.description]
            for row in cursor:
                vals = []
                for v in row:
                    if v is None:
                        vals.append("NULL")
                    elif isinstance(v, (int, float)):
                        vals.append(str(v))
                    elif isinstance(v, (bytes, bytearray)):
                        vals.append("0x" + v.hex())
                    else:
                        s = str(v).replace("\\", "\\\\").replace("'", "\\'")
                        vals.append("'" + s + "'")
                col_list = ",".join(f"`{c}`" for c in cols)
                out.append(f"INSERT INTO `{t}` ({col_list}) VALUES ({','.join(vals)});\n")
        out.append("SET FOREIGN_KEY_CHECKS=1;\n")
    finally:
        conn.close()
    return "".join(out).encode("utf-8")


def append_backup_log(line: str) -> None:
    """Persist one backup log line via the unified AppLogger (category BACKUP).

    Replaces the legacy direct-to-file writer. The logger fans the line out to
    APPLICATION_LOG_FILE and BACKUP_LOG_FILE, redacts secrets, and notifies any
    live UI subscribers. Signature unchanged so every existing log_func call
    site (headless + GUI) keeps working untouched.
    """
    get_logger().emit(line, category="BACKUP")
