import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.security import SecurityViolation, assert_no_private_credentials, assert_public_get


class TestApiKeyGuard(unittest.TestCase):
    def test_api_key_guard_aborts(self):
        with self.assertRaises(SecurityViolation):
            assert_no_private_credentials(env={"KRAKEN_API_KEY": "whatever"})

    def test_secret_guard_aborts(self):
        with self.assertRaises(SecurityViolation):
            assert_no_private_credentials(env={"KRAKEN_SECRET": "whatever"})

    def test_no_credentials_passes(self):
        assert_no_private_credentials(env={})  # must not raise


class TestRequestGuards(unittest.TestCase):
    def test_private_endpoint_guard_aborts(self):
        with self.assertRaises(SecurityViolation):
            assert_public_get("GET", "https://api.kraken.com/0/private/Balance")

    def test_non_get_guard_aborts(self):
        with self.assertRaises(SecurityViolation):
            assert_public_get("POST", "https://api.kraken.com/0/public/Ticker")

    def test_public_get_passes(self):
        assert_public_get("GET", "https://api.kraken.com/0/public/Ticker")  # must not raise


if __name__ == "__main__":
    unittest.main()
