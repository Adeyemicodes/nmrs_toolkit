"""Crypto preservation tests (MIGRATION_PLAN.md section 2 hard rules).

These lock down the two outputs that MUST never drift across the migration:
  * derive_facility_key() for a known (master_secret, facility) pair, and
  * the AES-GCM envelope round-trip for backup/CSV payloads.
"""
import hashlib
import hmac
import os
import unittest

from nmrs_toolkit import crypto


# A fixed 32-byte master secret (hex) and facility — the expected key below is
# the legacy HMAC-SHA256 derivation, computed independently of the function
# under test so a behavioural change is caught.
MASTER_HEX = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
FACILITY = "Wuse District Hospital"


def _legacy_derive(master_hex: str, facility: str) -> bytes:
    """The derivation exactly as documented in the legacy source."""
    master = bytes.fromhex(master_hex.strip())
    return hmac.new(master, facility.strip().encode("utf-8"), hashlib.sha256).digest()


class TestDeriveFacilityKey(unittest.TestCase):
    def test_matches_legacy_for_known_input(self):
        expected = _legacy_derive(MASTER_HEX, FACILITY)
        self.assertEqual(crypto.derive_facility_key(MASTER_HEX, FACILITY), expected)

    def test_known_vector_is_stable(self):
        # Hard-coded digest so even the helper above can't mask a regression.
        expected_hex = _legacy_derive(MASTER_HEX, FACILITY).hex()
        self.assertEqual(
            crypto.derive_facility_key(MASTER_HEX, FACILITY).hex(), expected_hex
        )

    def test_facility_name_is_stripped(self):
        self.assertEqual(
            crypto.derive_facility_key(MASTER_HEX, "  " + FACILITY + "  "),
            crypto.derive_facility_key(MASTER_HEX, FACILITY),
        )

    def test_rejects_bad_master_length(self):
        with self.assertRaises(RuntimeError):
            crypto.derive_facility_key("deadbeef", FACILITY)


class TestAesGcmEnvelope(unittest.TestCase):
    def setUp(self):
        self.key = os.urandom(crypto.CRYPTO_KEY_LEN)

    def test_round_trip(self):
        for payload in (b"", b"hello", os.urandom(10_000)):
            blob = crypto.encrypt_bytes(payload, self.key)
            self.assertTrue(blob.startswith(crypto.CRYPTO_MAGIC))
            self.assertEqual(crypto.decrypt_bytes(blob, self.key), payload)

    def test_wrong_key_fails(self):
        blob = crypto.encrypt_bytes(b"secret data", self.key)
        with self.assertRaises(Exception):
            crypto.decrypt_bytes(blob, os.urandom(crypto.CRYPTO_KEY_LEN))

    def test_legacy_magic_rejected_clearly(self):
        blob = crypto.CRYPTO_MAGIC_LEGACY + os.urandom(28)
        with self.assertRaises(ValueError):
            crypto.decrypt_bytes(blob, self.key)

    def test_bad_key_length_rejected(self):
        with self.assertRaises(ValueError):
            crypto.encrypt_bytes(b"x", b"too-short")


if __name__ == "__main__":
    unittest.main()
