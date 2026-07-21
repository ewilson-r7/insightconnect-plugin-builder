"""Unit tests for session token accounting (task 3.7; Req 3.5, 3.6, 3.7).

These cover specific examples and edge cases: adding successful invocations,
excluding failed ones, the empty/zero baseline, and input validation. The
universal "total equals sum of successful invocation token counts only"
property is covered separately by the property test (task 3.8, Property 9).
"""

import pytest

from icplugin_builder.core.token_accounting import (
    SessionTokenAccount,
    TokenInvocation,
    sum_successful_tokens,
)


class TestSessionTokenAccount:
    def test_starts_at_zero(self):
        # Req 3.6: an unused session totals a non-negative integer (zero).
        account = SessionTokenAccount()
        assert account.total == 0

    def test_record_success_accumulates(self):
        # Req 3.5: each successful invocation adds its tokens to the total.
        account = SessionTokenAccount()
        assert account.record_success(100) == 100
        assert account.record_success(50) == 150
        assert account.total == 150

    def test_record_failure_excluded(self):
        # Req 3.7: a failed invocation is excluded from the total.
        account = SessionTokenAccount()
        account.record_success(100)
        assert account.record_failure(999) == 100
        assert account.total == 100

    def test_record_failure_defaults_to_zero_tokens(self):
        account = SessionTokenAccount()
        assert account.record_failure() == 0
        assert account.total == 0

    def test_interleaved_successes_and_failures(self):
        # Req 3.5/3.7: only successes count, regardless of interleaving.
        account = SessionTokenAccount()
        account.record(10, succeeded=True)
        account.record(500, succeeded=False)
        account.record(20, succeeded=True)
        account.record(500, succeeded=False)
        account.record(30, succeeded=True)
        assert account.total == 60

    def test_zero_token_success_keeps_total(self):
        account = SessionTokenAccount()
        account.record_success(0)
        assert account.total == 0

    def test_total_is_int(self):
        account = SessionTokenAccount()
        account.record_success(7)
        assert isinstance(account.total, int)

    def test_negative_tokens_rejected(self):
        account = SessionTokenAccount()
        with pytest.raises(ValueError):
            account.record_success(-1)
        # Total is unchanged after a rejected record.
        assert account.total == 0

    @pytest.mark.parametrize("bad", [1.5, "10", None, True])
    def test_non_int_tokens_rejected(self, bad):
        account = SessionTokenAccount()
        with pytest.raises(TypeError):
            account.record_success(bad)


class TestSumSuccessfulTokens:
    def test_empty_sums_to_zero(self):
        assert sum_successful_tokens([]) == 0

    def test_sums_only_successful(self):
        # Req 3.7: failed invocations contribute nothing.
        invocations = [
            TokenInvocation(tokens=100, succeeded=True),
            TokenInvocation(tokens=999, succeeded=False),
            TokenInvocation(tokens=25, succeeded=True),
        ]
        assert sum_successful_tokens(invocations) == 125

    def test_all_failed_sums_to_zero(self):
        invocations = [
            TokenInvocation(tokens=10, succeeded=False),
            TokenInvocation(tokens=20, succeeded=False),
        ]
        assert sum_successful_tokens(invocations) == 0

    def test_invocation_rejects_negative_tokens(self):
        with pytest.raises(ValueError):
            TokenInvocation(tokens=-5, succeeded=True)

    def test_invocation_rejects_bool_tokens(self):
        with pytest.raises(TypeError):
            TokenInvocation(tokens=True, succeeded=True)
