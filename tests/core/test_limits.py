"""Unit tests for configurable numeric-limit validation (task 3.9; Req 4.1, 4.4).

These cover the inclusive boundaries and just-outside-range values for both
limits, plus type rejection (booleans and non-integers). The universal
"accepted iff within inclusive range" property is covered separately by the
property test (task 3.10, Property 10).
"""

import pytest

from icplugin_builder.core.limits import (
    LimitOutOfRangeError,
    RATE_LIMIT_MAX,
    RATE_LIMIT_MIN,
    REPAIR_ROUNDS_MAX,
    REPAIR_ROUNDS_MIN,
    TOKEN_BUDGET_MAX,
    TOKEN_BUDGET_MIN,
    is_valid_rate_limit,
    is_valid_repair_rounds,
    is_valid_token_budget,
    validate_rate_limit,
    validate_repair_rounds,
    validate_token_budget,
)


class TestTokenBudgetLimit:
    def test_ranges_match_requirement(self):
        # Req 4.1: token budget accepts 1..10,000,000 inclusive.
        assert TOKEN_BUDGET_MIN == 1
        assert TOKEN_BUDGET_MAX == 10_000_000

    @pytest.mark.parametrize("value", [1, 10_000_000, 2, 100_000, 9_999_999])
    def test_accepts_values_within_inclusive_range(self, value):
        assert is_valid_token_budget(value) is True
        assert validate_token_budget(value) == value

    def test_accepts_both_inclusive_boundaries(self):
        # Inclusive lower and upper boundaries are accepted.
        assert validate_token_budget(TOKEN_BUDGET_MIN) == TOKEN_BUDGET_MIN
        assert validate_token_budget(TOKEN_BUDGET_MAX) == TOKEN_BUDGET_MAX

    @pytest.mark.parametrize("value", [0, -1, TOKEN_BUDGET_MAX + 1, 20_000_000])
    def test_rejects_values_just_outside_range(self, value):
        assert is_valid_token_budget(value) is False
        with pytest.raises(LimitOutOfRangeError):
            validate_token_budget(value)

    @pytest.mark.parametrize("value", [True, False, 1.0, "1", None, 5_000.0])
    def test_rejects_non_integer_and_boolean(self, value):
        assert is_valid_token_budget(value) is False
        with pytest.raises(LimitOutOfRangeError):
            validate_token_budget(value)


class TestRateLimit:
    def test_ranges_match_requirement(self):
        # Req 4.4: rate accepts 1..1,000 requests/minute inclusive.
        assert RATE_LIMIT_MIN == 1
        assert RATE_LIMIT_MAX == 1_000

    @pytest.mark.parametrize("value", [1, 1_000, 2, 60, 999])
    def test_accepts_values_within_inclusive_range(self, value):
        assert is_valid_rate_limit(value) is True
        assert validate_rate_limit(value) == value

    def test_accepts_both_inclusive_boundaries(self):
        assert validate_rate_limit(RATE_LIMIT_MIN) == RATE_LIMIT_MIN
        assert validate_rate_limit(RATE_LIMIT_MAX) == RATE_LIMIT_MAX

    @pytest.mark.parametrize("value", [0, -1, RATE_LIMIT_MAX + 1, 5_000])
    def test_rejects_values_just_outside_range(self, value):
        assert is_valid_rate_limit(value) is False
        with pytest.raises(LimitOutOfRangeError):
            validate_rate_limit(value)

    @pytest.mark.parametrize("value", [True, False, 1.0, "60", None])
    def test_rejects_non_integer_and_boolean(self, value):
        assert is_valid_rate_limit(value) is False
        with pytest.raises(LimitOutOfRangeError):
            validate_rate_limit(value)


class TestRepairRounds:
    """The repair-round cap is an operator-configured integer count (Req 26.8)."""

    def test_accepts_exactly_the_documented_range(self):
        for value in range(REPAIR_ROUNDS_MIN, REPAIR_ROUNDS_MAX + 1):
            assert is_valid_repair_rounds(value), value
            assert validate_repair_rounds(value) == value

    def test_rejects_zero_because_a_loop_with_no_attempts_is_just_a_check(self):
        assert not is_valid_repair_rounds(0)
        with pytest.raises(LimitOutOfRangeError):
            validate_repair_rounds(0)

    def test_rejects_a_value_above_the_ceiling(self):
        # Every round is a paid agent run, so an accidental large value is a
        # spending mistake rather than a harmless one.
        assert not is_valid_repair_rounds(REPAIR_ROUNDS_MAX + 1)
        with pytest.raises(LimitOutOfRangeError):
            validate_repair_rounds(100)

    def test_rejects_non_integers_and_booleans(self):
        for value in (2.5, "3", None, True, False):
            assert not is_valid_repair_rounds(value), value

    def test_the_message_names_the_range(self):
        with pytest.raises(LimitOutOfRangeError) as exc:
            validate_repair_rounds(0)
        assert str(REPAIR_ROUNDS_MIN) in str(exc.value)
        assert str(REPAIR_ROUNDS_MAX) in str(exc.value)
