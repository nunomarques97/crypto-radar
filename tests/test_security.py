import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_v08.security import (
    PRIVATE_ENV_VARS,
    SecurityViolation,
    assert_no_private_credentials,
    assert_public_get,
)


class TestApiKeyGuard(unittest.TestCase):
    def test_api_key_guard_aborts(self):
        with self.assertRaises(SecurityViolation):
            assert_no_private_credentials(env={"KRAKEN_API_KEY": "whatever"})

    def test_secret_guard_aborts(self):
        with self.assertRaises(SecurityViolation):
            assert_no_private_credentials(env={"KRAKEN_SECRET": "whatever"})

    def test_private_read_secret_guard_aborts(self):
        # The private read adapter's secret name is refused too, alone or with the key.
        for env in ({"KRAKEN_API_SECRET": "whatever"}, {"KRAKEN_API_KEY": "k", "KRAKEN_API_SECRET": "s"}):
            with self.subTest(env=sorted(env)):
                with self.assertRaises(SecurityViolation) as raised:
                    assert_no_private_credentials(env=env)
                self.assertNotIn("whatever", str(raised.exception))

    def test_every_kraken_credential_name_is_guarded_and_stripped_from_children(self):
        from scripts import run_tests, verify_environment

        self.assertEqual(set(PRIVATE_ENV_VARS), {"KRAKEN_API_KEY", "KRAKEN_SECRET", "KRAKEN_API_SECRET"})
        self.assertLessEqual(set(PRIVATE_ENV_VARS), run_tests.SENSITIVE_ENVIRONMENT_NAMES)
        self.assertLessEqual(set(PRIVATE_ENV_VARS), verify_environment.SENSITIVE_ENVIRONMENT_NAMES)

    def test_no_credentials_passes(self):
        assert_no_private_credentials(env={})  # must not raise

    def test_unrelated_or_empty_names_pass(self):
        # Negative case: public names, look-alikes and an empty value are not credentials.
        assert_no_private_credentials(
            env={"KRAKEN_API_SECRET_PATH": "x", "KRAKEN_PAIR": "XBTEUR", "RADAR_STATE_DIR": "x", "KRAKEN_API_SECRET": ""}
        )  # must not raise


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
