"""Session token accounting (design Property 9; Req 3.5, 3.6, 3.7).

The tool tracks how many LLM tokens a session has consumed so the operator can
see the running cost and so the ``Cost_Controller`` can enforce a budget. This
module is the pure-logic core of that accounting; it performs no I/O and knows
nothing about how tokens are measured, only how they are summed.

The single rule is:

* A *successful* ``LLM_Generator`` invocation contributes its token count to the
  cumulative session total (Req 3.5).
* A *failed* invocation contributes nothing; it is excluded from the total
  (Req 3.7).

The cumulative total is therefore always a non-negative integer equal to the sum
of the token counts of the successful invocations only, and it is what the UI
displays when a generation step completes (Req 3.6).

Two shapes are offered:

* :func:`sum_successful_tokens` -- a stateless reduction over a sequence of
  recorded invocations, convenient for recomputation and testing.
* :class:`SessionTokenAccount` -- a small stateful accumulator that a session
  updates one invocation at a time as generation proceeds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

__all__ = [
    "TokenInvocation",
    "SessionTokenAccount",
    "sum_successful_tokens",
]


def _validate_token_count(tokens: int) -> int:
    """Return ``tokens`` after checking it is a non-negative integer.

    ``bool`` is rejected explicitly because it is a subclass of ``int`` in
    Python and a boolean token count is almost certainly a caller mistake.

    Raises:
        TypeError: If ``tokens`` is not an ``int`` (or is a ``bool``).
        ValueError: If ``tokens`` is negative.
    """
    if isinstance(tokens, bool) or not isinstance(tokens, int):
        raise TypeError(f"token count must be a non-negative int, got {tokens!r}")
    if tokens < 0:
        raise ValueError(f"token count must be non-negative, got {tokens}")
    return tokens


@dataclass(frozen=True)
class TokenInvocation:
    """A single recorded ``LLM_Generator`` invocation and its outcome.

    Attributes:
        tokens: The non-negative token count consumed by the invocation.
        succeeded: Whether the invocation completed successfully. Only
            successful invocations contribute to a cumulative total.
    """

    tokens: int
    succeeded: bool

    def __post_init__(self) -> None:
        _validate_token_count(self.tokens)


def sum_successful_tokens(invocations: Iterable[TokenInvocation]) -> int:
    """Sum the token counts of the successful invocations only.

    Args:
        invocations: The recorded invocations for a session, in any order and
            with any interleaving of successes and failures.

    Returns:
        A non-negative integer equal to the sum of ``tokens`` across the
        invocations whose ``succeeded`` flag is ``True``. Failed invocations are
        excluded (Req 3.7). An empty sequence sums to ``0``.
    """
    return sum(inv.tokens for inv in invocations if inv.succeeded)


@dataclass
class SessionTokenAccount:
    """A stateful cumulative token total for a single session.

    The account starts at zero and only ever increases, one invocation at a
    time, as successful ``LLM_Generator`` calls are recorded. Failed calls are
    recorded too (for completeness) but leave the total unchanged.

    The invariant ``total == sum of tokens of all successfully recorded
    invocations`` holds after every operation, and ``total`` is always a
    non-negative integer (Req 3.5, 3.6, 3.7).
    """

    _total: int = field(default=0, init=False)

    @property
    def total(self) -> int:
        """The cumulative session token total as a non-negative integer."""
        return self._total

    def record_success(self, tokens: int) -> int:
        """Record a successful invocation and add its tokens to the total.

        Args:
            tokens: The non-negative token count consumed by the invocation.

        Returns:
            The new cumulative total.

        Raises:
            TypeError: If ``tokens`` is not a non-negative ``int``.
            ValueError: If ``tokens`` is negative.
        """
        self._total += _validate_token_count(tokens)
        return self._total

    def record_failure(self, tokens: int = 0) -> int:
        """Record a failed invocation without changing the total (Req 3.7).

        The token count is validated for consistency with
        :meth:`record_success` but is deliberately excluded from the total.

        Args:
            tokens: The token count the failed invocation would have consumed;
                defaults to ``0``. Excluded from the cumulative total.

        Returns:
            The unchanged cumulative total.

        Raises:
            TypeError: If ``tokens`` is not a non-negative ``int``.
            ValueError: If ``tokens`` is negative.
        """
        _validate_token_count(tokens)
        return self._total

    def record(self, tokens: int, succeeded: bool) -> int:
        """Record an invocation, adding its tokens only when it succeeded.

        Args:
            tokens: The non-negative token count consumed by the invocation.
            succeeded: Whether the invocation completed successfully.

        Returns:
            The cumulative total after recording.
        """
        if succeeded:
            return self.record_success(tokens)
        return self.record_failure(tokens)
