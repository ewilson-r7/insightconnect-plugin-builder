"""Property-based test for token-budget blocking (task 7.2).

Covers design Property 11 with Hypothesis: for any session and any sequence of
invocation token costs, once the session's cumulative usage reaches the
configured budget, every subsequent ``Cost_Controller.authorize`` call is
blocked with the budget-reached reason, no partial output of a blocked
invocation is persisted, and the already-completed total is retained
(Req 4.2).

Usage is fed through the per-session token-account seam
(``CostController._session_account(...).record_success(...)``) exactly as the
forthcoming ``record_usage`` wiring (task 7.4) will, so the property exercises
the real budget gate rather than a stand-in.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.cost_controller import (
    BUDGET_REACHED,
    CostController,
)

# A rate limit high enough that the bounded number of authorize() calls in each
# example never trips the per-minute rate window, isolating the budget gate.
_RATE_LIMIT = 1000


def budgets_and_usage() -> st.SearchStrategy:
    """Generate a token budget and a bounded sequence of usage increments.

    Budgets are drawn from the low end of the valid ``1..10,000,000`` range and
    increments are non-negative, so the cumulative usage realistically reaches
    (and passes) the budget within the short sequence -- the region the property
    is about. The number of increments is kept well under ``_RATE_LIMIT`` so the
    rate window never interferes with the budget observations.
    """
    return st.tuples(
        st.integers(min_value=1, max_value=10_000),
        st.lists(st.integers(min_value=0, max_value=5_000), max_size=20),
    )


# Feature: insightconnect-plugin-builder, Property 11: Token budget blocks once reached
@settings(max_examples=200)
@given(data=budgets_and_usage())
def test_token_budget_blocks_once_reached(data: tuple) -> None:
    """Once cumulative usage reaches the budget, authorization stays blocked.

    Walks the usage sequence one increment at a time. Before the budget is
    reached, authorization succeeds and the recorded total tracks the completed
    usage. From the first moment cumulative usage reaches the budget onward,
    every ``authorize`` call is blocked with the budget-reached reason, carries a
    user-facing message and no retry-after, persists no partial tokens (the
    session total is unchanged by the blocked call), and retains the completed
    total.

    **Validates: Requirements 4.2**
    """
    budget, increments = data
    controller = CostController(token_budget=budget, rate_limit=_RATE_LIMIT)
    account = controller._session_account("s1")

    cumulative = 0
    reached = False
    for increment in increments:
        cumulative += increment
        account.record_success(increment)
        reached = reached or cumulative >= budget

        total_before = controller.session_total("s1")
        assert total_before == cumulative

        decision = controller.authorize("s1", "u1")

        if reached:
            # Subsequent authorizations are blocked with the budget-reached
            # reason and a message, and no retry-after is offered.
            assert decision.authorized is False
            assert decision.reason == BUDGET_REACHED
            assert decision.message is not None
            assert decision.retry_after_seconds is None
        else:
            assert decision.authorized is True

        # No partial output of the blocked (or authorized) invocation is
        # persisted, and the completed total is retained.
        assert controller.session_total("s1") == cumulative

    # After the budget has been reached, a fresh burst of authorizations all
    # remain blocked -- the block is terminal for the session.
    if reached:
        for _ in range(5):
            later = controller.authorize("s1", "u1")
            assert later.authorized is False
            assert later.reason == BUDGET_REACHED
        assert controller.session_total("s1") == cumulative
