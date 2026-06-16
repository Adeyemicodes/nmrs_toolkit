"""Phase 7 tests: identifier tokenization, config gating, decrypt key
resolution + preview round-trip. (The real unvoid/reverse DB mutation is
verified by the Phase 7 integration script against a throwaway database.)"""
import configparser
import gzip
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nmrs_toolkit import bridge
from nmrs_toolkit.crypto import CRYPTO_KEY_LEN, derive_facility_key, encrypt_bytes
from nmrs_toolkit.workflows import unvoid

KEY_HEX = "ab" * 32
MASTER = "cd" * 32


def _cfg(**overrides):
    c = configparser.ConfigParser()
    c["settings"] = {}
    c["database"] = {"host": "h", "user": "u", "password": "p", "database": "d", "port": "3306"}
    c["backup"] = {"backup_key": KEY_HEX}
    c["manager"] = {"master_secret": overrides.get("master", "")}
    if "ui" in overrides:
        c["ui"] = overrides["ui"]
    if "settings" in overrides:
        c["settings"] = overrides["settings"]
    return c


def _api(cfg):
    with mock.patch.object(bridge, "load_config", return_value=cfg), \
         mock.patch.object(bridge, "get_logger"):
        return bridge.Api()


class TestTokenize(unittest.TestCase):
    def test_separators_dedup_order(self):
        self.assertEqual(
            unvoid.tokenize_identifiers("A, B\nC\tA;D| B  E"),
            ["A", "B", "C", "D", "E"])

    def test_empty(self):
        self.assertEqual(unvoid.tokenize_identifiers("  ,\n; "), [])


class TestUnvoidConfig(unittest.TestCase):
    def test_accepted_reasons_default(self):
        self.assertIn("Duplicate Client", unvoid.get_accepted_reasons(_cfg()))

    def test_window_seconds_default_and_override(self):
        self.assertEqual(unvoid.get_window_seconds(_cfg()), 120)
        c = _cfg(settings={"unvoid_window_seconds": "300"})
        self.assertEqual(unvoid.get_window_seconds(c), 300)

    def test_window_seconds_bad_value(self):
        self.assertEqual(unvoid.get_window_seconds(_cfg(settings={"unvoid_window_seconds": "x"})), 120)


class TestUiGating(unittest.TestCase):
    def test_defaults(self):
        flags = _api(_cfg())._ui_flags()
        self.assertTrue(flags["unvoid"])
        self.assertFalse(flags["reverse"])
        self.assertFalse(flags["decrypt"])

    def test_enable_via_config(self):
        c = _cfg(ui={"reverse_tab_enabled": "true", "decrypt_tab_enabled": "true",
                     "unvoid_tab_enabled": "false"})
        flags = _api(c)._ui_flags()
        self.assertFalse(flags["unvoid"])
        self.assertTrue(flags["reverse"])
        self.assertTrue(flags["decrypt"])

    def test_summary_includes_flags(self):
        self.assertIn("ui_flags", _api(_cfg()).config_get_summary())


class TestUnvoidBridgeValidation(unittest.TestCase):
    def test_validate_empty(self):
        self.assertFalse(_api(_cfg()).unvoid_validate("  ,; ")["ok"])

    def test_commit_without_batch(self):
        self.assertFalse(_api(_cfg()).unvoid_commit()["ok"])

    def test_reverse_run_bad_id(self):
        self.assertFalse(_api(_cfg()).reverse_run("notanint")["ok"])


class TestDecryptKey(unittest.TestCase):
    def test_hex_field(self):
        self.assertEqual(_api(_cfg())._resolve_decrypt_key(KEY_HEX, ""),
                         bytes.fromhex(KEY_HEX))

    def test_bad_hex(self):
        with self.assertRaises(ValueError):
            _api(_cfg())._resolve_decrypt_key("xyz", "")

    def test_facility_derivation(self):
        api = _api(_cfg(master=MASTER))
        self.assertEqual(api._resolve_decrypt_key("", "Wuse HC"),
                         derive_facility_key(MASTER, "Wuse HC"))

    def test_facility_without_master_raises(self):
        with self.assertRaises(RuntimeError):
            _api(_cfg())._resolve_decrypt_key("", "Wuse HC")

    def test_config_fallback(self):
        self.assertEqual(_api(_cfg())._resolve_decrypt_key("", ""), bytes.fromhex(KEY_HEX))


class TestDecryptPreview(unittest.TestCase):
    def test_csv_preview_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.csv.nmrs"
            p.write_bytes(encrypt_bytes(b"name,age\nAda,30\nBola,25\n", bytes.fromhex(KEY_HEX)))
            res = _api(_cfg()).decrypt_preview(str(p), KEY_HEX, "")
        self.assertTrue(res["ok"])
        self.assertEqual(res["kind"], "csv")
        self.assertEqual(res["headers"], ["name", "age"])
        self.assertEqual(res["total"], 2)

    def test_sql_preview(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "b.sql.gz.enc"
            p.write_bytes(encrypt_bytes(gzip.compress(b"CREATE TABLE t (id int);\n"),
                                        bytes.fromhex(KEY_HEX)))
            res = _api(_cfg()).decrypt_preview(str(p), KEY_HEX, "")
        self.assertTrue(res["ok"])
        self.assertEqual(res["kind"], "sql")
        self.assertIn("CREATE TABLE", res["head"])

    def test_not_encrypted_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "plain.csv"
            p.write_text("a,b\n1,2\n")
            res = _api(_cfg()).decrypt_preview(str(p), KEY_HEX, "")
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
