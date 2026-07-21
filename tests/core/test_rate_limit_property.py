"""Property-based test for per-user request-rate limiting (task 7.3).

Covers design Property 12 with Hypothesis: across arbitrary configured rate
limits and burst sizes, the ``Cost_Controller`` admits at most the configured
number of requests within a rolling one-minute window and rejects every request
beyond that threshold without invoking the ``LLM_Generator``, each rejection
carrying a retry-after value in the interval ``(0, 60]`` seconds (Req 4.5).

An injected, manually advanced clock makes the rolling window deterministic. All
requests in a scenario are issued within a single 60-second window (the total
elapsed time is bounded below 60 seconds) so that no timestamp is evicted, which
lets the test assert the exact admitted count.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.cost_controller import (
    AUTHORIZED,
    CostController,
    RATE_LIMITED,
    RATE_LIMIT_WINDOW_SECONDS,
)
from icplugin_builder.core.limits import TOKEN_BUDGET_MAX


class _FakeClock:
    """A manually advanced monotonic clock for deterministic rate-limit tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@st.composite
def _rate_scenarios(draw):
    """Generate a rate limit, an over-threshold burst size, and an arrival gap.

    The gap between consecutive requests is bounded so the entire burst lands
    inside a single 60-second window (``(burst - 1) * gap < 60``), guaranteeing
    no timestamp is aged out and the admitted count equals the configured rate.
    """
    rate_limit = draw(st.integers(min_value=1, max_value=40))
    excess = draw(st.integers(min_value=1, max_value=30))
    burst = rate_limit + excess
    max_gap = 59.0 / burst
    gap = draw(st.floats(min_value=0.0, max_value=max_gap, allow_nan=False, allow_infinity=False))
    return rate_limit, burst, gap


# Feature: insightconnect-plugin-builder, Property 12: Rate limit rejects beyond threshold with retry-after
@settings(max_examples=200)
@given(scenario=_rate_scenarios())
def test_rate_limit_rejects_beyond_threshold_with_retry_after(scenario):
    """Excess requests are rejected with a retry-after in ``(0, 60]``.

    Within a single one-minute window, exactly the configured number of
    requests are admitted and every request beyond the threshold is rejected as
    rate-limited without invoking the LLM, each reporting a retry-after strictly
    greater than 0 and no more than 60 seconds.

    **Validates: Requirements 4.5**
    """
    rate_limit, burst, gap = scenario
    clock = _FakeClock()
    # A generous budget keeps the token limit from interfering with the rate check.
    cc = CostController(token_budget=TOKEN_BUDGET_MAX, rate_limit=rate_limit, clock=clock)

    admitted = 0
    rejections = []
    for _ in range(burst):
        decision = cc.authorize("s1", "u1")
        if decision.authorized:
            admitted += 1
            assert decision.reason == AUTHORIZED
        else:
            rejections.append(decision)
        clock.advance(gap)

    # Admitted requests never exceed the configured per-minute rate.
    assert admitted == rate_limit
    assert admitted <= rate_limit

    # Every request beyond the threshold is rejected as rate-limited with a
    # retry-after in the open-below, closed-above interval (0, 60].
    assert len(rejections) == burst - rate_limit
    for decision in rejections:
        assert decision.authorized is False
        assert decision.reason == RATE_LIMITED
        assert decision.retry_after_seconds is not None
        assert 0 < decision.retry_after_seconds <= RATE_LIMIT_WINDOW_SECONDS
