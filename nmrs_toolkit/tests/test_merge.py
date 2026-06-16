"""CSV merge workflow tests: column-union order, missing-fill, sort, encrypted
input/output round-trip, and bridge validation."""
import configparser
import csv
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from nmrs_toolkit import bridge
from nmrs_toolkit.crypto import decrypt_bytes, encrypt_bytes, get_facility_key
from nmrs_toolkit.workflows import merge

KEY_HEX = "ab" * 32


def _cfg():
    c = configparser.ConfigParser()
    c["backup"] = {"backup_key": KEY_HEX}
    return c


def _expected(headers, rows):
    """Reference output using the same csv.DictWriter semantics as the merger."""
    buf = StringIO()
    w = csv.DictWriter(buf, fieldnames=headers)
    w.writeheader()
    for r in rows:
        w.writerow({h: r.get(h, "") for h in headers})
    return buf.getvalue().encode("utf-8")


class TestMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        (self.d / "a.csv").write_text("name,age\nAda,30\nBola,25\n")
        (self.d / "b.csv").write_text("age,city\n40,Lagos\n")
        self.out = self.d / "out.csv"

    def tearDown(self):
        self.tmp.cleanup()

    def test_union_order_and_fill_no_sort(self):
        res = merge.merge_csvs([self.d / "a.csv", self.d / "b.csv"], self.out,
                               False, "", False, _cfg(), log_func=lambda *a: None)
        self.assertEqual(res, {"n_rows": 3, "n_cols": 3})
        expected = _expected(
            ["name", "age", "city"],
            [{"name": "Ada", "age": "30"}, {"name": "Bola", "age": "25"},
             {"age": "40", "city": "Lagos"}])
        self.assertEqual(self.out.read_bytes(), expected)

    def test_sort_ascending(self):
        merge.merge_csvs([self.d / "a.csv", self.d / "b.csv"], self.out,
                         False, "age", False, _cfg(), log_func=lambda *a: None)
        rows = list(csv.DictReader(self.out.read_text().splitlines()))
        self.assertEqual([r["age"] for r in rows], ["25", "30", "40"])

    def test_sort_descending(self):
        merge.merge_csvs([self.d / "a.csv", self.d / "b.csv"], self.out,
                         False, "age", True, _cfg(), log_func=lambda *a: None)
        rows = list(csv.DictReader(self.out.read_text().splitlines()))
        self.assertEqual([r["age"] for r in rows], ["40", "30", "25"])

    def test_unknown_sort_column_preserves_order(self):
        merge.merge_csvs([self.d / "a.csv"], self.out, False, "nope", False,
                         _cfg(), log_func=lambda *a: None)
        rows = list(csv.DictReader(self.out.read_text().splitlines()))
        self.assertEqual([r["name"] for r in rows], ["Ada", "Bola"])

    def test_encrypted_input_and_output_roundtrip(self):
        cfg = _cfg()
        key = get_facility_key(cfg)
        enc_in = self.d / "c.csv.nmrs"
        enc_in.write_bytes(encrypt_bytes(b"name,age\nChidi,50\n", key))
        enc_out = self.d / "out.csv.nmrs"
        merge.merge_csvs([self.d / "a.csv", enc_in], enc_out, True, "", False,
                         cfg, log_func=lambda *a: None)
        plain = decrypt_bytes(enc_out.read_bytes(), key)
        expected = _expected(
            ["name", "age"],
            [{"name": "Ada", "age": "30"}, {"name": "Bola", "age": "25"},
             {"name": "Chidi", "age": "50"}])
        self.assertEqual(plain, expected)


class TestMergeBridge(unittest.TestCase):
    def _api(self, with_config=True):
        cfg = _cfg() if with_config else None
        with mock.patch.object(bridge, "load_config", return_value=cfg), \
             mock.patch.object(bridge, "get_logger"):
            api = bridge.Api()
        if cfg is None:
            api.config = None
        return api

    def test_validate_paths(self):
        with tempfile.TemporaryDirectory() as d:
            good = Path(d) / "x.csv"; good.write_text("a\n1\n")
            res = self._api().merge_add_files([str(good), "/nope/y.csv"])
        self.assertEqual(len(res["accepted"]), 1)
        self.assertEqual(len(res["rejected"]), 1)

    def test_run_requires_files(self):
        self.assertFalse(self._api().merge_run([], "", False, "/tmp/o.csv", False)["ok"])

    def test_run_requires_output(self):
        self.assertFalse(self._api().merge_run(["/x.csv"], "", False, "", False)["ok"])

    def test_run_requires_config(self):
        self.assertFalse(self._api(with_config=False).merge_run(
            ["/x.csv"], "", False, "/tmp/o.csv", False)["ok"])


if __name__ == "__main__":
    unittest.main()
