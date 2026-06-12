"""Restore-support workflow: dump classification, decrypt-to-SQL, pre-restore
safety snapshot. Format handling PRESERVED VERBATIM. append_restore_log now
routes through the unified AppLogger (category RESTORE).
"""
from __future__ import annotations

import gzip
import os
import platform
import secrets
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from ..constants import BACKUP_DIR, PRE_RESTORE_DIR, _NO_WINDOW
from ..crypto import (
    CRYPTO_KEY_LEN, CRYPTO_MAGIC, decrypt_bytes, encrypt_bytes, get_facility_key,
)
from ..db import _mysql_admin, _mysql_db_exists, db_connect
from ..logger import get_logger
from .backup import _dump_database, _facility_slug, _prune_backups

def _classify_dump_file(path: Path) -> str:
    """Identify the dump format by extension + magic bytes.

    Returns one of: 'enc' (.sql.gz.enc), 'gz' (.sql.gz), 'zip' (.sql.zip),
    'sql' (plain), or raises ValueError if magic doesn't match extension.
    """
    name = path.name.lower()
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError as e:
        raise ValueError(f"Cannot read file: {e}")

    if name.endswith(".sql.gz.enc"):
        if not head.startswith(CRYPTO_MAGIC):
            raise ValueError("File has .sql.gz.enc extension but missing NMRS magic header")
        return "enc"
    if name.endswith(".sql.gz"):
        if not head.startswith(b"\x1f\x8b"):
            raise ValueError("File has .sql.gz extension but is not gzip-compressed")
        return "gz"
    if name.endswith(".sql.zip") or name.endswith(".zip"):
        if not head.startswith(b"PK"):
            raise ValueError("File has .zip extension but is not a zip archive")
        return "zip"
    if name.endswith(".sql"):
        return "sql"
    # Fall back to magic-byte sniffing if extension is unusual.
    if head.startswith(CRYPTO_MAGIC):
        return "enc"
    if head.startswith(b"\x1f\x8b"):
        return "gz"
    if head.startswith(b"PK"):
        return "zip"
    return "sql"


def _decrypt_to_sql_file(src: Path, key_bytes, dest: Path, log_func=print) -> int:
    """Decrypt/gunzip/unzip `src` to plain SQL bytes at `dest` (0600). Returns
    the number of bytes written. `key_bytes` is the 32-byte key for .enc
    files (None/empty means the caller hasn't provided one — an error is
    raised if the file actually needs decryption)."""
    kind = _classify_dump_file(src)
    log_func(f"[RESTORE] source format: {kind}")

    if kind == "enc":
        if not key_bytes:
            raise ValueError(
                "Encrypted .sql.gz.enc file selected but no backup key was "
                "provided. Enter a 64-char hex key in the Backup key field, "
                "or set [backup] backup_key in .nmrs_config.ini."
            )
        blob = src.read_bytes()
        decrypted = decrypt_bytes(blob, key_bytes)
        sql_bytes = gzip.decompress(decrypted)
    elif kind == "gz":
        sql_bytes = gzip.decompress(src.read_bytes())
    elif kind == "zip":
        import zipfile
        with zipfile.ZipFile(src) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".sql")]
            if not members:
                raise ValueError("Zip archive contains no .sql file")
            sql_bytes = zf.read(members[0])
    else:
        sql_bytes = src.read_bytes()

    # Write with 0600 on POSIX; on Windows we accept default ACLs.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if platform.system() != "Windows":
        fd = os.open(str(dest), flags, 0o600)
    else:
        fd = os.open(str(dest), flags)
    with os.fdopen(fd, "wb") as f:
        f.write(sql_bytes)
    return len(sql_bytes)


def _sql_sanity_check(sql_path: Path) -> None:
    """Raise if the file isn't a plausible MySQL dump."""
    if sql_path.stat().st_size == 0:
        raise ValueError("Decrypted SQL file is empty")
    with open(sql_path, "rb") as f:
        head = f.read(64 * 1024).decode("utf-8", errors="replace").upper()
    if "CREATE TABLE" not in head and "INSERT INTO" not in head:
        raise ValueError("File does not look like a MySQL dump "
                         "(no CREATE TABLE or INSERT INTO in first 64 KB)")


def _shred_unlink(path: Path) -> None:
    """Best-effort overwrite + delete. Not crypto-grade on COW/journaled FS."""
    try:
        size = path.stat().st_size
        with open(path, "r+b") as f:
            chunk = b"\x00" * min(1 << 16, size or 1)
            written = 0
            while written < size:
                f.write(chunk[: min(len(chunk), size - written)])
                written += len(chunk)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    try:
        path.unlink()
    except OSError:
        pass


def perform_pre_restore_backup(config: configparser.ConfigParser,
                               log_func=print) -> Path:
    """Snapshot the live DB into PRE_RESTORE_DIR before a destructive restore.

    Distinguished from regular backups by the 'PRE-RESTORE' tag in the
    filename and a separate retention policy. Verified by round-trip
    decrypt before returning — raises if the file can't be decrypted.
    """
    PRE_RESTORE_DIR.mkdir(parents=True, exist_ok=True)
    db = config["database"]
    facility = _facility_slug(config, log_func)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_path = PRE_RESTORE_DIR / f"{facility}_PRE-RESTORE_{stamp}.sql.gz.enc"

    log_func(f"[RESTORE] pre-restore backup -> {out_path.name}")
    dump_bytes = _dump_database(db, log_func)
    gzipped = gzip.compress(dump_bytes, compresslevel=6)
    key = get_facility_key(config)
    encrypted = encrypt_bytes(gzipped, key)
    with open(out_path, "wb") as f:
        f.write(encrypted)

    # Round-trip verify so we never drop the live DB without a readable snapshot.
    verify = decrypt_bytes(out_path.read_bytes(), key)
    if gzip.decompress(verify) != dump_bytes:
        out_path.unlink(missing_ok=True)
        raise RuntimeError("pre-restore verification failed; aborting")
    log_func(f"[RESTORE] pre-restore verified OK ({out_path.stat().st_size:,} bytes)")

    retention = config.getint("backup", "pre_restore_retention", fallback=5)
    _prune_backups(PRE_RESTORE_DIR, "*_PRE-RESTORE_*.sql.gz.enc",
                   keep=retention, log_func=log_func, label="RESTORE")
    return out_path


def append_restore_log(line: str) -> None:
    """Persist one restore log line via the unified AppLogger (category RESTORE).

    Unlike the legacy writer (which wrote restore.log only and never reached the
    Activity Log), this fans out to APPLICATION_LOG_FILE and RESTORE_LOG_FILE and
    notifies live subscribers. Signature unchanged.
    """
    get_logger().emit(line, category="RESTORE")


# ---------------------------------------------------------------------------
# Restore orchestration (extracted from the legacy Tk worker, parameterized).
# The Tk callbacks (ui_log/ui_status/ui_progress) and the threading.Event cancel
# become function arguments; the destructive-stage logic is PRESERVED VERBATIM.
# ---------------------------------------------------------------------------

def resolve_restore_key(config, ui_key_hex: str):
    """Pick the key for decrypting the selected .sql.gz.enc. Priority:
    UI Backup-key field (hex) -> [backup] backup_key in config -> None.
    Plain (.sql / .sql.gz / .sql.zip) files don't need this — the decrypt helper
    only complains when the file is .enc and the key is None.
    """
    ui_key_hex = (ui_key_hex or "").strip()
    if ui_key_hex:
        try:
            k = bytes.fromhex(ui_key_hex)
        except ValueError as e:
            raise ValueError(f"Backup key field is not valid hex: {e}")
        if len(k) != CRYPTO_KEY_LEN:
            raise ValueError(
                f"Backup key must be {CRYPTO_KEY_LEN * 2} hex chars "
                f"({CRYPTO_KEY_LEN} bytes); got {len(k)} bytes"
            )
        return k
    try:
        return get_facility_key(config)
    except RuntimeError:
        return None  # only an error later if the file is actually encrypted


def stream_import(sql_path: Path, target_db: str, db_cfg, cancel_event,
                  log_func=print, status_func=None, progress_func=None) -> None:
    """Pipe the temp .sql file into `mysql` with a byte counter. Cancellable via
    cancel_event (kills the mysql process mid-stream)."""
    status = status_func or (lambda *a, **k: None)
    progress = progress_func or (lambda *a, **k: None)
    total = sql_path.stat().st_size
    log_func(f"Importing {total:,} bytes...")

    mysql_bin = shutil.which("mysql") or "mysql"
    cmd = [
        mysql_bin,
        f"-h{db_cfg['host']}",
        f"-P{db_cfg.get('port', '3306')}",
        f"-u{db_cfg['user']}",
        f"-p{db_cfg['password']}",
        "--max_allowed_packet=1G",
        "--init-command=SET autocommit=0; SET unique_checks=0; SET foreign_key_checks=0;",
        "--default-character-set=utf8mb4",
        target_db,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=_NO_WINDOW)
    sent = 0
    t_in = time.monotonic()
    last_ui = 0.0
    try:
        with open(sql_path, "rb") as src:
            while True:
                if cancel_event.is_set():
                    proc.kill()
                    raise InterruptedError("cancelled during import")
                chunk = src.read(64 * 1024)
                if not chunk:
                    break
                try:
                    proc.stdin.write(chunk)
                except BrokenPipeError:
                    break
                sent += len(chunk)
                now = time.monotonic()
                if now - last_ui >= 0.5:
                    last_ui = now
                    pct = (sent / total) * 100 if total else 0
                    elapsed = now - t_in
                    rate = sent / elapsed if elapsed > 0 else 0
                    remaining = (total - sent) / rate if rate > 0 else 0
                    progress(pct)
                    status(
                        f"Imported {sent/1e6:.1f} / {total/1e6:.1f} MB "
                        f"({pct:.1f}%, {rate/1e6:.1f} MB/s, ETA {remaining:.0f}s)"
                    )
        try:
            proc.stdin.close()
        except OSError:
            pass
    finally:
        rc = proc.wait()
    if rc != 0:
        stderr = proc.stderr.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"mysql import failed (rc={rc}):\n{stderr}")
    progress(100)
    log_func(f"Import finished — {sent:,} bytes sent to mysql")


def restore_verify(db_cfg, target_db: str, log_func=print) -> None:
    """Post-import sanity check: table count > 0; if known tables exist, sample
    row counts."""
    conn = db_connect(
        host=db_cfg["host"], user=db_cfg["user"], password=db_cfg["password"],
        database=target_db,
        port=int(db_cfg.get("port", 3306)), connection_timeout=10,
    )
    try:
        cur = conn.cursor()
        cur.execute("SHOW TABLES")
        tables = [r[0] for r in cur.fetchall()]
        log_func(f"Verification: {len(tables)} table(s) present")
        if len(tables) == 0:
            raise RuntimeError("Restored database has no tables")
        for sample in ("patient", "encounter", "obs", "users"):
            if sample in tables:
                cur.execute(f"SELECT COUNT(*) FROM `{sample}`")
                n = cur.fetchone()[0]
                log_func(f"  rows in `{sample}`: {n:,}")
    finally:
        conn.close()


def run_restore(config, src: str, target_db: str, key_hex: str,
                typed_confirmation: str, *, cancel_event,
                log_func=print, status_func=None, progress_func=None) -> dict:
    """Full restore pipeline, extracted from the legacy Tk worker.

    Stages: decrypt/decompress -> sanity -> existence check -> (if exists)
    pre-restore safety backup -> drop/create -> streamed cancellable import ->
    verify. The destructive logic is byte-for-byte the legacy behavior.

    typed_confirmation is a HARD GATE: it must equal target_db exactly or the
    function aborts before touching anything (defense in depth — the frontend
    also disables RESTORE until the typed name matches). Raises InterruptedError
    on cancel / gate failure, or Exception on error. Returns a result dict on
    success. The temp plaintext SQL is always shredded.
    """
    status = status_func or (lambda *a, **k: None)
    progress = progress_func or (lambda *a, **k: None)

    # Hard gate, checked first so a mismatch never reaches any destructive work.
    if (typed_confirmation or "").strip() != target_db:
        raise InterruptedError(
            "typed confirmation does not match the target database name")

    src_path = Path(src)
    temp_sql = BACKUP_DIR / f".tmp_restore_{secrets.token_hex(6)}.sql"
    t_start = time.monotonic()
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)

        status("Preparing dump file...")
        log_func(f"Stage 1: decrypt + decompress -> {temp_sql.name}")
        key_bytes = resolve_restore_key(config, key_hex)
        bytes_written = _decrypt_to_sql_file(src_path, key_bytes, temp_sql, log_func)
        log_func(f"Wrote {bytes_written:,} bytes of plain SQL")

        log_func("Stage 2: SQL sanity check")
        _sql_sanity_check(temp_sql)
        log_func("SQL looks valid (contains CREATE TABLE / INSERT INTO)")

        if cancel_event.is_set():
            raise InterruptedError("cancelled before destructive phase")

        status("Checking target database...")
        db_cfg = config["database"]
        exists = _mysql_db_exists(db_cfg, target_db)
        log_func(f"Target database '{target_db}' "
                 + ("EXISTS" if exists else "does not exist"))

        if exists:
            status("Taking pre-restore safety backup...")
            log_func("Stage 5: pre-restore backup (mysqldump + gzip + encrypt + verify)")
            pre_path = perform_pre_restore_backup(config, log_func)
            log_func(f"Pre-restore backup OK: {pre_path}")

            if cancel_event.is_set():
                raise InterruptedError("cancelled after pre-restore backup")

            status("Dropping and recreating database...")
            log_func(f"Stage 6: DROP DATABASE `{target_db}`; CREATE DATABASE `{target_db}`")
            _mysql_admin(db_cfg, [
                f"DROP DATABASE `{target_db}`",
                f"CREATE DATABASE `{target_db}` "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
            ])
        else:
            status("Creating target database...")
            log_func(f"Stage 6: CREATE DATABASE `{target_db}`")
            _mysql_admin(db_cfg, [
                f"CREATE DATABASE `{target_db}` "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
            ])

        status("Importing SQL — this can take a while...")
        log_func("Stage 7: streaming SQL into mysql with progress")
        stream_import(temp_sql, target_db, db_cfg, cancel_event,
                      log_func, status, progress)

        status("Verifying restored data...")
        log_func("Stage 8: post-import verification")
        restore_verify(db_cfg, target_db, log_func)

        elapsed = time.monotonic() - t_start
        progress(100)
        status(f"Restore complete in {elapsed/60:.1f} min")
        log_func(f"DONE in {elapsed:.1f}s")
        return {"ok": True, "elapsed": elapsed, "target_db": target_db,
                "pre_restore_dir": str(PRE_RESTORE_DIR)}
    finally:
        _shred_unlink(temp_sql)


def classify_dump(path: Path) -> dict:
    """Preview a dump file for the Restore tab: {format, encrypted, size_bytes}."""
    fmt = _classify_dump_file(path)
    return {"format": fmt, "encrypted": fmt == "enc",
            "size_bytes": path.stat().st_size}
