"""AppLogger tests: secret redaction, dual-file fan-out, rotation, subscribers.

The redaction test is a hard requirement (MIGRATION_PLAN.md): a line carrying
`backup_key=<hex>` must never reach disk un-redacted.
"""
import tempfile
import unittest
from pathlib import Path

from nmrs_toolkit.logger import AppLogger, redact


class LoggerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.app_log = base / "application.log"
        self.backup_log = base / "backup.log"
        self.restore_log = base / "restore.log"
        self.linelist_log = base / "linelist.log"
        self.logger = AppLogger(
            self.app_log,
            {
                "BACKUP": self.backup_log,
                "RESTORE": self.restore_log,
                "LINELIST": self.linelist_log,
            },
        )

    def tearDown(self):
        self.tmp.cleanup()


class TestRedaction(LoggerTestBase):
    def test_backup_key_redacted_on_disk(self):
        self.logger.emit("loaded backup_key=deadbeefcafebabe ok", category="BACKUP")
        for path in (self.app_log, self.backup_log):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("deadbeefcafebabe", text)
            self.assertIn("backup_key=<redacted>", text)

    def test_all_secret_keys_redacted(self):
        for key, val in (
            ("master_secret", "00ff00ff00ff"),
            ("admin_password", "hunter2"),
            ("password", "s3cr3t"),
        ):
            self.logger.emit(f"{key}={val}", category="APP")
        text = self.app_log.read_text(encoding="utf-8")
        for _, val in (("", "00ff00ff00ff"), ("", "hunter2"), ("", "s3cr3t")):
            self.assertNotIn(val, text)

    def test_redact_helper_keeps_key(self):
        self.assertEqual(redact("password: topsecret"), "password: <redacted>")


class TestFanOut(LoggerTestBase):
    def test_category_line_hits_both_files(self):
        self.logger.emit("backup line", category="BACKUP")
        self.assertIn("backup line", self.app_log.read_text(encoding="utf-8"))
        self.assertIn("backup line", self.backup_log.read_text(encoding="utf-8"))

    def test_app_category_only_application_log(self):
        self.logger.emit("ui line", category="UI")
        self.assertIn("ui line", self.app_log.read_text(encoding="utf-8"))
        self.assertFalse(self.backup_log.exists())

    def test_structured_fields_present(self):
        self.logger.emit("dump done", category="BACKUP", level="warn",
                         facility="Wuse", operation_id="op42")
        line = self.app_log.read_text(encoding="utf-8").strip()
        self.assertIn("WARN", line)
        self.assertIn("BACKUP", line)
        self.assertIn("[Wuse]", line)
        self.assertIn("(op42)", line)


class TestSubscribers(LoggerTestBase):
    def test_subscriber_receives_filtered_events(self):
        received = []
        self.logger.subscribe(received.append, categories=["BACKUP"])
        self.logger.emit("a", category="BACKUP")
        self.logger.emit("b", category="LINELIST")  # filtered out
        self.assertEqual([e["message"] for e in received], ["a"])

    def test_unsubscribe(self):
        received = []
        self.logger.subscribe(received.append)
        self.logger.unsubscribe(received.append)
        self.logger.emit("x", category="APP")
        self.assertEqual(received, [])

    def test_broken_subscriber_does_not_break_emit(self):
        def boom(_event):
            raise RuntimeError("subscriber failure")
        self.logger.subscribe(boom)
        self.logger.emit("still logged", category="APP")  # must not raise
        self.assertIn("still logged", self.app_log.read_text(encoding="utf-8"))


class TestQueriesAndRotation(LoggerTestBase):
    def test_tail_and_search(self):
        self.logger.emit("alpha encrypted ok", category="BACKUP")
        self.logger.emit("beta plain", category="LINELIST")
        self.assertEqual(len(self.logger.tail(10)), 2)
        hits = self.logger.search("encrypted")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["category"], "BACKUP")

    def test_export_filtered(self):
        self.logger.emit("keep me", category="BACKUP")
        self.logger.emit("drop me", category="LINELIST")
        out = Path(self.tmp.name) / "export.txt"
        res = self.logger.export_filtered(out, categories=["BACKUP"])
        self.assertTrue(res["ok"])
        text = out.read_text(encoding="utf-8")
        self.assertIn("keep me", text)
        self.assertNotIn("drop me", text)

    def test_rotation_keeps_generations(self):
        small = AppLogger(self.app_log, {}, max_bytes=200, backup_generations=3)
        for i in range(200):
            small.emit(f"line number {i} with some padding text", category="APP")
        self.assertTrue(self.app_log.with_suffix(".log.1").exists())

    def test_load_recent_from_disk(self):
        self.logger.emit("persisted line", category="BACKUP")
        fresh = AppLogger(self.app_log, {"BACKUP": self.backup_log})
        loaded = fresh.load_recent_from_disk()
        self.assertGreaterEqual(loaded, 1)
        self.assertTrue(any("persisted line" in e["line"] for e in fresh.tail(10)))


if __name__ == "__main__":
    unittest.main()
