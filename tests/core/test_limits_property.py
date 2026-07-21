"""Property-based test for configurable numeric-limit ranges (task 3.10).

Covers design Property 10 with Hypothesis: across arbitrary integers spanning
and exceeding each limit's bounds, a value is accepted exactly when it falls
within the limit's inclusive range, and the validating helper agrees with the
predicate (returning the value when valid, raising otherwise).
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.limits import (
    LimitOutOfRangeError,
    RATE_LIMIT_MAX,
    RATE_LIMIT_MIN,
    TOKEN_BUDGET_MAX,
    TOKEN_BUDGET_MIN,
    is_valid_rate_limit,
    is_valid_token_budget,
    validate_rate_limit,
    validate_token_budget,
)


def _integers_around(low: int, high: int) -> st.SearchStrategy[int]:
    """Generate integers spanning and exceeding the inclusive range ``[low, high]``.

    Deliberately biases toward the interesting territory: values below ``low``,
    values above ``high``, the exact boundaries, and values comfortably inside
    the range, so acceptance is exercised on both sides of each edge.
    """
    span = high - low
    return st.one_of(
        st.integers(min_value=low, max_value=high),
        st.integers(min_value=low - span - 1, max_value=high + span + 1),
        st.just(low - 1),
        st.just(low),
        st.just(high),
        st.just(high + 1),
        st.integers(),
    )


# Feature: insightconnect-plugin-builder, Property 10: Configurable numeric limits accept exactly their range
@settings(max_examples=200)
@given(value=_integers_around(TOKEN_BUDGET_MIN, TOKEN_BUDGET_MAX))
def test_token_budget_accepted_iff_within_inclusive_range(value: int):
    """A token budget is accepted iff it is within ``1..10,000,000`` inclusive.

    **Validates: Requirements 4.1, 4.4**
    """
    within_range = TOKEN_BUDGET_MIN <= value <= TOKEN_BUDGET_MAX

    assert is_valid_token_budget(value) is within_range

    if within_range:
        assert validate_token_budget(value) == value
    else:
        try:
            validate_token_budget(value)
        except LimitOutOfRangeError:
            pass
        else:
            raise AssertionError(f"expected out-of-range value {value!r} to be rejected")


# Feature: insightconnect-plugin-builder, Property 10: Configurable numeric limits accept exactly their range
@settings(max_examples=200)
@given(value=_integers_around(RATE_LIMIT_MIN, RATE_LIMIT_MAX))
def test_rate_limit_accepted_iff_within_inclusive_range(value: int):
    """A request rate is accepted iff it is within ``1..1,000`` inclusive.

    **Validates: Requirements 4.1, 4.4**
    """
    within_range = RATE_LIMIT_MIN <= value <= RATE_LIMIT_MAX

    assert is_valid_rate_limit(value) is within_range

    if within_range:
        assert validate_rate_limit(value) == value
    else:
        try:
            validate_rate_limit(value)
        except LimitOutOfRangeError:
            pass
        else:
            raise AssertionError(f"expected out-of-range value {value!r} to be rejected")
