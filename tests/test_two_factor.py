import unittest
from datetime import datetime, timedelta, timezone
try:
    import pyotp
except ImportError:
    raise unittest.SkipTest("pyotp is declared in gateway/requirements.txt but is unavailable in this isolated audit environment")
from gateway.two_factor import (
    generate_secret, provisioning_uri, verify_code,
    generate_session_token, session_expiry, is_session_valid,
)


class TestTotpLogic(unittest.TestCase):
    def test_generated_secret_is_valid_base32_and_usable(self):
        secret = generate_secret()
        self.assertTrue(len(secret) >= 16)
        # If pyotp can construct a TOTP from it and generate a code, it's a valid secret.
        code = pyotp.TOTP(secret).now()
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())

    def test_correct_code_at_fixed_time_verifies(self):
        secret = generate_secret()
        fixed_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        code = pyotp.TOTP(secret).at(fixed_time)
        self.assertTrue(verify_code(secret, code, for_time=fixed_time))

    def test_wrong_code_is_rejected(self):
        secret = generate_secret()
        fixed_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        real_code = pyotp.TOTP(secret).at(fixed_time)
        wrong_code = '000000' if real_code != '000000' else '111111'
        self.assertFalse(verify_code(secret, wrong_code, for_time=fixed_time))

    def test_code_from_wrong_secret_is_rejected(self):
        secret_a = generate_secret()
        secret_b = generate_secret()
        fixed_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        code_for_a = pyotp.TOTP(secret_a).at(fixed_time)
        self.assertFalse(verify_code(secret_b, code_for_a, for_time=fixed_time))

    def test_code_far_outside_window_is_rejected(self):
        secret = generate_secret()
        base_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        code_at_base = pyotp.TOTP(secret).at(base_time)
        far_future = base_time + timedelta(hours=1)
        self.assertFalse(verify_code(secret, code_at_base, for_time=far_future))

    def test_non_numeric_code_rejected_without_touching_pyotp(self):
        secret = generate_secret()
        self.assertFalse(verify_code(secret, 'not-a-code'))
        self.assertFalse(verify_code(secret, ''))
        self.assertFalse(verify_code(secret, None))

    def test_provisioning_uri_contains_issuer_and_account(self):
        secret = generate_secret()
        uri = provisioning_uri(secret, 'admin@example.com', issuer='Vidigen')
        self.assertIn('Vidigen', uri)
        self.assertIn('admin%40example.com', uri)  # URL-encoded @ in the otpauth:// URI

    def test_session_token_is_unique_and_reasonably_long(self):
        t1, t2 = generate_session_token(), generate_session_token()
        self.assertNotEqual(t1, t2)
        self.assertGreaterEqual(len(t1), 32)

    def test_session_expiry_is_in_the_future(self):
        expiry = session_expiry(hours=4)
        self.assertGreater(expiry, datetime.now(timezone.utc))

    def test_is_session_valid_before_and_after_expiry(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expires_at = now + timedelta(hours=1)
        self.assertTrue(is_session_valid(expires_at, now=now))
        past_now = now + timedelta(hours=2)
        self.assertFalse(is_session_valid(expires_at, now=past_now))


if __name__ == '__main__':
    unittest.main()
