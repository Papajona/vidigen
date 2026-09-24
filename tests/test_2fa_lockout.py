import unittest
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from gateway import persistence


def _run(coro):
    return asyncio.run(coro)


class TestTwoFactorLockout(unittest.TestCase):
    """Covers gateway/persistence.py's account-level TOTP lockout, additive to the
    gateway's IP-based RateLimitMiddleware. sb_request is mocked throughout — this is
    pure logic-and-sequencing coverage, not a live-Supabase integration test."""

    def test_unenrolled_account_reads_as_unlocked_with_zero_attempts(self):
        with patch.object(persistence, 'sb_request', new=AsyncMock(return_value=[])):
            state = _run(persistence.get_2fa_lock_state('u1'))
        self.assertEqual(state, {'locked_until': None, 'failed_attempts': 0})

    def test_is_2fa_locked_false_when_no_locked_until(self):
        rows = [{'failed_attempts': 2, 'locked_until': None}]
        with patch.object(persistence, 'sb_request', new=AsyncMock(return_value=rows)):
            locked, until = _run(persistence.is_2fa_locked('u1'))
        self.assertFalse(locked)
        self.assertIsNone(until)

    def test_is_2fa_locked_true_when_locked_until_in_future(self):
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        rows = [{'failed_attempts': 5, 'locked_until': future}]
        with patch.object(persistence, 'sb_request', new=AsyncMock(return_value=rows)):
            locked, until = _run(persistence.is_2fa_locked('u1'))
        self.assertTrue(locked)
        self.assertEqual(until, future)

    def test_is_2fa_locked_false_when_locked_until_in_past(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        rows = [{'failed_attempts': 5, 'locked_until': past}]
        with patch.object(persistence, 'sb_request', new=AsyncMock(return_value=rows)):
            locked, until = _run(persistence.is_2fa_locked('u1'))
        self.assertFalse(locked)

    def test_record_failure_increments_without_locking_below_threshold(self):
        rows = [{'failed_attempts': 1, 'locked_until': None}]
        mock = AsyncMock(return_value=rows)
        with patch.object(persistence, 'sb_request', new=mock):
            _run(persistence.record_2fa_failure('u1'))
        patch_call = mock.call_args_list[-1]
        self.assertEqual(patch_call.args[0], 'PATCH')
        self.assertEqual(patch_call.args[2], {'failed_attempts': 2})

    def test_record_failure_sets_locked_until_at_threshold(self):
        rows = [{'failed_attempts': persistence.TWO_FA_MAX_ATTEMPTS - 1, 'locked_until': None}]
        mock = AsyncMock(return_value=rows)
        with patch.object(persistence, 'sb_request', new=mock):
            _run(persistence.record_2fa_failure('u1'))
        patch_call = mock.call_args_list[-1]
        self.assertEqual(patch_call.args[2]['failed_attempts'], persistence.TWO_FA_MAX_ATTEMPTS)
        self.assertIn('locked_until', patch_call.args[2])

    def test_reset_clears_attempts_and_lock(self):
        mock = AsyncMock(return_value=[])
        with patch.object(persistence, 'sb_request', new=mock):
            _run(persistence.reset_2fa_failures('u1'))
        mock.assert_called_once_with('PATCH', 'admin_2fa', {'failed_attempts': 0, 'locked_until': None}, params={'uid': 'eq.u1'})


if __name__ == '__main__':
    unittest.main()
