"""Restore workflow tests — the typed-name HARD GATE (must not be bypassable)
and backup-key resolution. The destructive DB path is not exercised here; the
gate is asserted to fire before any DB/decrypt work happens."""
import configparser
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from nmrs_toolkit import bridge
from nmrs_toolkit.crypto import CRYPTO_KEY_LEN
from nmrs_toolkit.workflows import restore


def _cfg(backup_key=""):
    c = configparser.ConfigParser()
    c["database"] = {"host": "h", "user": "u", "password": "p",
                     "database": "openmrs", "port": "3306"}
    c["backup"] = {"backup_key": backup_key}
    return c


class TestTypedGate(unittest.TestCase):
    def test_mismatch_aborts_before_any_work(self):
        ev = threading.Event()
        # If the gate did not fire first, this would try to read a missing file
        # / hit the DB. A mismatch must raise InterruptedError immediately.
        with self.assertRaises(InterruptedError):
            restore.run_restore(_cfg(), "/nonexistent/dump.sql", "openmrs",
                                "", "WRONG_NAME", cancel_event=ev)

    def test_empty_confirmation_aborts(self):
        ev = threading.Event()
        with self.assertRaises(InterruptedError):
            restore.run_restore(_cfg(), "/nonexistent/dump.sql", "openmrs",
                                "", "", cancel_event=ev)

    def test_match_passes_gate_then_fails_later_on_missing_file(self):
        # Correct typed name -> gate passes -> pipeline proceeds and fails at the
        # decrypt/read stage (not the gate). Proves the gate isn't the blocker.
        ev = threading.Event()
        with self.assertRaises(Exception) as ctx:
            restore.run_restore(_cfg(), "/nonexistent/dump.sql", "openmrs",
                                "", "openmrs", cancel_event=ev,
                                log_func=lambda *a, **k: None)
        self.assertNotIsInstance(ctx.exception, InterruptedError)


class TestResolveKey(unittest.TestCase):
    def test_valid_hex_from_ui(self):
        hex_key = "ab" * CRYPTO_KEY_LEN
        self.assertEqual(restore.resolve_restore_key(_cfg(), hex_key),
                         bytes.fromhex(hex_key))

    def test_bad_hex_raises(self):
        with self.assertRaises(ValueError):
            restore.resolve_restore_key(_cfg(), "nothex!")

    def test_wrong_length_raises(self):
        with self.assertRaises(ValueError):
            restore.resolve_restore_key(_cfg(), "abcd")

    def test_blank_falls_back_to_config_then_none(self):
        # No backup_key configured -> get_facility_key raises -> returns None.
        self.assertIsNone(restore.resolve_restore_key(_cfg(), ""))

    def test_blank_uses_config_key(self):
        hex_key = "cd" * CRYPTO_KEY_LEN
        self.assertEqual(restore.resolve_restore_key(_cfg(hex_key), ""),
                         bytes.fromhex(hex_key))


class TestClassifyDump(unittest.TestCase):
    def test_plain_sql(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "dump.sql"
            p.write_bytes(b"CREATE TABLE x (id int);\n")
            info = restore.classify_dump(p)
            self.assertEqual(info["format"], "sql")
            self.assertFalse(info["encrypted"])
            self.assertGreater(info["size_bytes"], 0)


class TestBridgeRestoreGate(unittest.TestCase):
    def _api(self):
        with mock.patch.object(bridge, "load_config", return_value=_cfg()), \
             mock.patch.object(bridge, "get_logger"):
            return bridge.Api()

    def test_run_rejects_typed_mismatch(self):
        api = self._api()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "dump.sql"
            p.write_bytes(b"CREATE TABLE x (id int);")
            res = api.restore_run(str(p), "openmrs", "", "WRONG")
        self.assertFalse(res["ok"])
        self.assertIn("match", res["message"].lower())

    def test_run_rejects_missing_file(self):
        res = self._api().restore_run("/nope/x.sql", "openmrs", "", "openmrs")
        self.assertFalse(res["ok"])
        self.assertIn("not found", res["message"].lower())

    def test_cancel_unknown_op(self):
        self.assertFalse(self._api().restore_cancel("nope")["ok"])


if __name__ == "__main__":
    unittest.main()
