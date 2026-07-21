"""Unit tests for the Cost_Controller authorize() gate (task 7.1; Req 4.2-4.5).

These cover specific examples and edge cases for the two limits enforced by
:meth:`CostController.authorize`:

* per-session token budget blocking, the budget-reached message, retention of
  the completed total, and absence of any partial-result persistence
  (Req 4.2, 4.3); and
* per-user request-rate rejection with a retry-after in ``(0, 60]`` and the
  rate-limit message (Req 4.4, 4.5).

The universal "budget blocks once reached" and "rate limit rejects beyond
threshold" properties are covered separately by the property tests (tasks 7.2
and 7.3, Properties 11 and 12). Wiring of ``record_usage`` and the 100,000-token
default budget is task 7.4; here session usage is seeded through the controller's
per-session token account seam.
"""

import pytest

from icplugin_builder.core.cost_controller import (
    AUTHORIZED,
    BUDGET_REACHED,
    DEFAULT_TOKEN_BUDGET,
    CostController,
    Decision,
    RATE_LIMITED,
    RATE_LIMIT_WINDOW_SECONDS,
)
from icplugin_builder.core.limits import LimitOutOfRangeError


class _FakeClock:
    """A manually advanced monotonic clock for deterministic rate-limit tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class TestConstructionValidation:
    def test_rejects_out_of_range_token_budget(self):
        # Req 4.1: budget must be 1..10,000,000.
        with pytest.raises(LimitOutOfRangeError):
            CostController(token_budget=0, rate_limit=10)
        with pytest.raises(LimitOutOfRangeError):
            CostController(token_budget=10_000_001, rate_limit=10)

    def test_rejects_out_of_range_rate_limit(self):
        # Req 4.4: rate must be 1..1,000.
        with pytest.raises(LimitOutOfRangeError):
            CostController(token_budget=1000, rate_limit=0)
        with pytest.raises(LimitOutOfRangeError):
            CostController(token_budget=1000, rate_limit=1001)

    def test_accepts_inclusive_boundaries(self):
        low = CostController(token_budget=1, rate_limit=1)
        high = CostController(token_budget=10_000_000, rate_limit=1_000)
        assert (low.token_budget, low.rate_limit) == (1, 1)
        assert (high.token_budget, high.rate_limit) == (10_000_000, 1_000)


class TestTokenBudget:
    def test_authorizes_when_under_budget(self):
        cc = CostController(token_budget=1000, rate_limit=100)
        decision = cc.authorize("s1", "u1")
        assert decision == Decision(authorized=True, reason=AUTHORIZED)

    def test_blocks_when_usage_reaches_budget(self):
        # Req 4.2: reaching the budget blocks subsequent invocations.
        cc = CostController(token_budget=1000, rate_limit=100)
        cc._session_account("s1").record_success(1000)

        decision = cc.authorize("s1", "u1")

        assert decision.authorized is False
        assert decision.reason == BUDGET_REACHED
        assert decision.retry_after_seconds is None

    def test_blocks_when_usage_exceeds_budget(self):
        cc = CostController(token_budget=1000, rate_limit=100)
        cc._session_account("s1").record_success(1500)
        assert cc.authorize("s1", "u1").authorized is False

    def test_budget_reached_message_returned(self):
        # Req 4.3: return a budget-reached message to the user.
        cc = CostController(token_budget=500, rate_limit=100)
        cc._session_account("s1").record_success(500)

        decision = cc.authorize("s1", "u1")

        assert decision.message is not None
        assert "budget" in decision.message.lower()
        assert "500" in decision.message

    def test_completed_total_retained_after_block(self):
        # Req 4.2: already-completed output (the recorded total) is retained;
        # a blocked authorization persists no partial result, so the total is
        # unchanged by the blocked call.
        cc = CostController(token_budget=1000, rate_limit=100)
        cc._session_account("s1").record_success(1000)

        cc.authorize("s1", "u1")

        assert cc.session_total("s1") == 1000

    def test_budget_is_per_session(self):
        # One session reaching its budget does not block a different session.
        cc = CostController(token_budget=1000, rate_limit=100)
        cc._session_account("s1").record_success(1000)

        assert cc.authorize("s1", "u1").authorized is False
        assert cc.authorize("s2", "u1").authorized is True

    def test_unused_session_totals_zero(self):
        cc = CostController(token_budget=1000, rate_limit=100)
        assert cc.session_total("never-seen") == 0


class TestRateLimit:
    def test_admits_exactly_rate_limit_requests_per_window(self):
        # Req 4.4: up to `rate_limit` requests per minute are admitted.
        clock = _FakeClock()
        cc = CostController(token_budget=10_000, rate_limit=3, clock=clock)

        assert [cc.authorize("s1", "u1").authorized for _ in range(3)] == [True, True, True]

    def test_rejects_request_beyond_threshold(self):
        # Req 4.5: excess request rejected without invoking the LLM.
        clock = _FakeClock()
        cc = CostController(token_budget=10_000, rate_limit=3, clock=clock)
        for _ in range(3):
            cc.authorize("s1", "u1")

        decision = cc.authorize("s1", "u1")

        assert decision.authorized is False
        assert decision.reason == RATE_LIMITED

    def test_retry_after_within_open_interval_up_to_60(self):
        # Req 4.5: retry-after is > 0 and <= 60 seconds.
        clock = _FakeClock()
        cc = CostController(token_budget=10_000, rate_limit=2, clock=clock)
        cc.authorize("s1", "u1")  # t=0
        clock.advance(10)
        cc.authorize("s1", "u1")  # t=10

        decision = cc.authorize("s1", "u1")  # still t=10, window full

        # Oldest request was at t=0; it frees at t=60, i.e. 50s from now.
        assert decision.retry_after_seconds == pytest.approx(50.0)
        assert 0 < decision.retry_after_seconds <= RATE_LIMIT_WINDOW_SECONDS

    def test_retry_after_is_60_when_all_requests_are_now(self):
        clock = _FakeClock()
        cc = CostController(token_budget=10_000, rate_limit=1, clock=clock)
        cc.authorize("s1", "u1")

        decision = cc.authorize("s1", "u1")

        assert decision.retry_after_seconds == pytest.approx(RATE_LIMIT_WINDOW_SECONDS)

    def test_rate_limit_message_reports_seconds(self):
        # Req 4.5: message indicates the rate limit and when requests resume.
        clock = _FakeClock()
        cc = CostController(token_budget=10_000, rate_limit=1, clock=clock)
        cc.authorize("s1", "u1")

        decision = cc.authorize("s1", "u1")

        assert decision.message is not None
        assert "rate limit" in decision.message.lower()
        assert "60" in decision.message

    def test_window_frees_slot_after_60_seconds(self):
        clock = _FakeClock()
        cc = CostController(token_budget=10_000, rate_limit=1, clock=clock)
        cc.authorize("s1", "u1")  # t=0
        assert cc.authorize("s1", "u1").authorized is False

        clock.advance(60)  # first request now expired (now - t == 60)
        assert cc.authorize("s1", "u1").authorized is True

    def test_rejected_request_does_not_consume_a_slot(self):
        # Only authorized requests count toward the window, so rejections do
        # not push the freeing time further out.
        clock = _FakeClock()
        cc = CostController(token_budget=10_000, rate_limit=1, clock=clock)
        cc.authorize("s1", "u1")  # t=0 admitted
        clock.advance(30)
        cc.authorize("s1", "u1")  # rejected, must not record t=30

        clock.advance(30)  # now t=60: original request has expired
        assert cc.authorize("s1", "u1").authorized is True

    def test_rate_limit_is_per_user(self):
        clock = _FakeClock()
        cc = CostController(token_budget=10_000, rate_limit=1, clock=clock)
        cc.authorize("s1", "u1")

        assert cc.authorize("s1", "u1").authorized is False
        assert cc.authorize("s1", "u2").authorized is True


class TestRecordUsage:
    def test_successful_invocation_adds_to_session_total(self):
        # Req 3.5: a successful invocation's tokens are added to the total.
        cc = CostController(token_budget=1000, rate_limit=100)

        assert cc.record_usage("s1", 300) == 300
        assert cc.record_usage("s1", 200) == 500
        assert cc.session_total("s1") == 500

    def test_failed_invocation_excluded_from_total(self):
        # Req 3.7: a failed invocation contributes nothing.
        cc = CostController(token_budget=1000, rate_limit=100)
        cc.record_usage("s1", 300, succeeded=True)

        assert cc.record_usage("s1", 999, succeeded=False) == 300
        assert cc.session_total("s1") == 300

    def test_record_usage_is_per_session(self):
        cc = CostController(token_budget=1000, rate_limit=100)
        cc.record_usage("s1", 400)
        cc.record_usage("s2", 100)

        assert cc.session_total("s1") == 400
        assert cc.session_total("s2") == 100

    def test_recorded_usage_drives_session_to_budget_block(self):
        # record_usage feeds the same total authorize() checks (Req 4.2).
        cc = CostController(token_budget=500, rate_limit=100)
        cc.record_usage("s1", 500)

        decision = cc.authorize("s1", "u1")

        assert decision.authorized is False
        assert decision.reason == BUDGET_REACHED

    def test_rejects_negative_token_count(self):
        cc = CostController(token_budget=1000, rate_limit=100)
        with pytest.raises(ValueError):
            cc.record_usage("s1", -1)


class TestDefaultTokenBudget:
    def test_default_applied_when_budget_unconfigured(self):
        # Req 4.6: with no configured budget, the 100,000-token default applies.
        cc = CostController()
        assert cc.token_budget == DEFAULT_TOKEN_BUDGET == 100_000

    def test_default_applied_when_budget_is_none(self):
        cc = CostController(token_budget=None, rate_limit=100)
        assert cc.token_budget == 100_000

    def test_default_budget_blocks_at_100_000_tokens(self):
        # Req 4.6: an unconfigured session is blocked once cumulative usage
        # reaches the 100,000-token default, with the budget-reached reason and
        # a message naming the default budget.
        cc = CostController(rate_limit=100)
        cc.record_usage("s1", 100_000)

        decision = cc.authorize("s1", "u1")

        assert decision.authorized is False
        assert decision.reason == BUDGET_REACHED
        assert decision.message is not None
        assert "100000" in decision.message

    def test_default_budget_authorizes_below_limit(self):
        cc = CostController(rate_limit=100)
        cc.record_usage("s1", 99_999)

        assert cc.authorize("s1", "u1").authorized is True


class TestLimitPrecedence:
    def test_budget_block_takes_precedence_over_rate_limit(self):
        # A budget-reached session is blocked terminally and must not consume a
        # rate-limit slot.
        clock = _FakeClock()
        cc = CostController(token_budget=1000, rate_limit=5, clock=clock)
        cc._session_account("s1").record_success(1000)

        decision = cc.authorize("s1", "u1")

        assert decision.reason == BUDGET_REACHED
        # No slot consumed: a fresh session for the same user is still admitted.
        assert cc.authorize("s2", "u1").authorized is True
