"""Property-based test for session token accounting (task 3.8).

Covers design Property 9 with Hypothesis: across arbitrary sequences of
successful and failed ``LLM_Generator`` invocations, the cumulative session
total always equals the sum of the token counts of the successful invocations
only, and is always a non-negative integer (Req 3.5, 3.6, 3.7).
"""

from typing import List

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.token_accounting import (
    SessionTokenAccount,
    TokenInvocation,
    sum_successful_tokens,
)


def invocations() -> st.SearchStrategy[List[TokenInvocation]]:
    """Generate lists mixing successful and failed invocations.

    Token counts are constrained to non-negative integers (the input space of a
    valid invocation), and each invocation independently succeeds or fails so
    every interleaving of outcomes is reachable. Empty lists are included to
    exercise the zero baseline.
    """
    invocation = st.builds(
        TokenInvocation,
        tokens=st.integers(min_value=0, max_value=10_000_000),
        succeeded=st.booleans(),
    )
    return st.lists(invocation, max_size=50)


# Feature: insightconnect-plugin-builder, Property 9: Token accounting equals sum of successful invocations
@settings(max_examples=200)
@given(recorded=invocations())
def test_token_accounting_equals_sum_of_successful(recorded: List[TokenInvocation]):
    """Cumulative total equals the sum of successful tokens only.

    Records each invocation one at a time into a stateful account and checks the
    running total against an independent reference sum over the successful
    invocations. Also asserts the total is always a non-negative integer.

    **Validates: Requirements 3.5, 3.6, 3.7**
    """
    account = SessionTokenAccount()
    for inv in recorded:
        account.record(inv.tokens, succeeded=inv.succeeded)

    expected = sum_successful_tokens(recorded)

    assert account.total == expected
    assert isinstance(account.total, int) and not isinstance(account.total, bool)
    assert account.total >= 0
