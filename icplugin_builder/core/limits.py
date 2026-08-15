"""Configurable numeric-limit validation for LLM cost controls.

The ``Cost_Controller`` is configured with two operator-supplied numeric limits
(design "Cost_Controller"; Requirements 4.1, 4.4):

* a per-session **token budget**, accepting integer values from 1 to
  10,000,000 tokens inclusive (Req 4.1); and
* a per-user **request rate**, accepting integer values from 1 to 1,000
  requests per minute inclusive (Req 4.4).

A third operator-supplied limit bounds the ``Repair_Loop`` rather than the
``Cost_Controller``, but belongs here for the same reasons -- it is an integer
count an operator configures, and each round it permits is a paid agent run
(Req 26.8):

* a maximum number of **repair rounds**, accepting integer values from 1 to 10
  inclusive.

This module is the single source of truth for those inclusive ranges. Each limit
exposes a pure predicate (``is_valid_*``) that returns ``True`` **iff** the value
falls within the limit's inclusive range, and a validating helper
(``validate_*``) that returns the value unchanged when valid or raises
:class:`LimitOutOfRangeError` otherwise. A value is accepted only when it is a
genuine integer within range; booleans and non-integer types are rejected
because a limit is an integer count, not a truth value or a fraction.
"""

from __future__ import annotations

__all__ = [
    "TOKEN_BUDGET_MIN",
    "TOKEN_BUDGET_MAX",
    "RATE_LIMIT_MIN",
    "RATE_LIMIT_MAX",
    "REPAIR_ROUNDS_MIN",
    "REPAIR_ROUNDS_MAX",
    "LimitOutOfRangeError",
    "is_valid_token_budget",
    "is_valid_rate_limit",
    "is_valid_repair_rounds",
    "validate_token_budget",
    "validate_rate_limit",
    "validate_repair_rounds",
]

# Inclusive bounds for the per-session token budget (Req 4.1).
TOKEN_BUDGET_MIN = 1
TOKEN_BUDGET_MAX = 10_000_000

# Inclusive bounds for the per-user request rate, in requests per minute (Req 4.4).
RATE_LIMIT_MIN = 1
RATE_LIMIT_MAX = 1_000

# Inclusive bounds for the maximum number of repair rounds (Req 26.8). One is the
# floor because a loop permitted zero fix attempts is just a check. The ceiling is
# there because every round is a paid agent run, and a mistyped 100 would spend
# heavily for little gain -- the loop's stall detector almost always stops it
# first, so a large value buys rounds that never happen while risking ones that do.
REPAIR_ROUNDS_MIN = 1
REPAIR_ROUNDS_MAX = 10


class LimitOutOfRangeError(ValueError):
    """Raised when a configured numeric limit falls outside its inclusive range."""


def _is_int_in_range(value: object, low: int, high: int) -> bool:
    """Return ``True`` iff ``value`` is a non-boolean int within ``[low, high]``.

    Booleans are excluded even though ``bool`` is a subclass of ``int``: a limit
    is an integer count, so ``True``/``False`` are not valid configured values.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return low <= value <= high


def is_valid_token_budget(value: object) -> bool:
    """Return ``True`` iff ``value`` is a valid per-session token budget.

    The budget is valid exactly when it is an integer in the inclusive range
    ``1..10,000,000`` (Req 4.1).
    """
    return _is_int_in_range(value, TOKEN_BUDGET_MIN, TOKEN_BUDGET_MAX)


def is_valid_rate_limit(value: object) -> bool:
    """Return ``True`` iff ``value`` is a valid per-user request rate.

    The rate is valid exactly when it is an integer in the inclusive range
    ``1..1,000`` requests per minute (Req 4.4).
    """
    return _is_int_in_range(value, RATE_LIMIT_MIN, RATE_LIMIT_MAX)


def is_valid_repair_rounds(value: object) -> bool:
    """Return ``True`` iff ``value`` is a valid maximum repair-round count.

    Valid exactly when it is an integer in the inclusive range ``1..10``
    (Req 26.8).
    """
    return _is_int_in_range(value, REPAIR_ROUNDS_MIN, REPAIR_ROUNDS_MAX)


def validate_token_budget(value: object) -> int:
    """Return ``value`` unchanged when it is a valid token budget.

    Args:
        value: The configured token budget to validate.

    Returns:
        The validated token budget as an ``int``.

    Raises:
        LimitOutOfRangeError: If ``value`` is not an integer in the inclusive
            range ``1..10,000,000`` (Req 4.1).
    """
    if not is_valid_token_budget(value):
        raise LimitOutOfRangeError(
            f"token budget must be an integer from {TOKEN_BUDGET_MIN} to "
            f"{TOKEN_BUDGET_MAX} inclusive, got {value!r}"
        )
    return value


def validate_rate_limit(value: object) -> int:
    """Return ``value`` unchanged when it is a valid request rate.

    Args:
        value: The configured requests-per-minute rate to validate.

    Returns:
        The validated request rate as an ``int``.

    Raises:
        LimitOutOfRangeError: If ``value`` is not an integer in the inclusive
            range ``1..1,000`` (Req 4.4).
    """
    if not is_valid_rate_limit(value):
        raise LimitOutOfRangeError(
            f"rate limit must be an integer from {RATE_LIMIT_MIN} to " f"{RATE_LIMIT_MAX} inclusive, got {value!r}"
        )
    return value


def validate_repair_rounds(value: object) -> int:
    """Return ``value`` unchanged when it is a valid maximum repair-round count.

    Args:
        value: The configured maximum number of repair rounds to validate.

    Returns:
        The validated round count as an ``int``.

    Raises:
        LimitOutOfRangeError: If ``value`` is not an integer in the inclusive
            range ``1..10`` (Req 26.8).
    """
    if not is_valid_repair_rounds(value):
        raise LimitOutOfRangeError(
            f"maximum repair rounds must be an integer from {REPAIR_ROUNDS_MIN} to "
            f"{REPAIR_ROUNDS_MAX} inclusive, got {value!r}"
        )
    return value
