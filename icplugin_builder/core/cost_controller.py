"""LLM cost control: per-session token budgets and per-user rate limits.

The ``Cost_Controller`` is the single gate every ``LLM_Generator`` invocation
passes through before it is dispatched (design "Cost_Controller"). It answers a
single question -- *may this session/user invoke the LLM right now?* -- and
enforces two independent limits when doing so:

* a **per-session token budget** (1..10,000,000 tokens, Req 4.1): once a
  session's cumulative token usage reaches its configured budget, every
  subsequent invocation is blocked, the already-completed output is retained,
  no partial result of the blocked invocation is persisted, and a
  budget-reached message is returned (Req 4.2, 4.3); and
* a **per-user request rate** (1..1,000 requests per minute, Req 4.4): requests
  beyond the configured maximum within a rolling one-minute window are rejected
  without invoking the LLM, each carrying a retry-after value in the interval
  ``(0, 60]`` seconds indicating when further requests will be accepted
  (Req 4.5).

This module owns :meth:`Cost_Controller.authorize` (task 7.1) and
:meth:`Cost_Controller.record_usage` together with the 100,000-token default
budget (task 7.4). Per-session token totals are tracked via
:class:`SessionTokenAccount` and reachable through
:meth:`Cost_Controller._session_account`; ``record_usage`` feeds that account so
that a successful invocation adds its tokens to the cumulative session total
while a failed invocation is excluded (Req 3.5, 3.7), and the total it maintains
is exactly what :meth:`authorize` checks against the budget.

When no token budget is configured for a session, the controller applies a
default maximum budget of 100,000 tokens (Req 4.6): construct a controller
without an explicit ``token_budget`` (or pass ``None``) to take that default.

The numeric ranges are validated through :mod:`icplugin_builder.core.limits`,
the single source of truth for the accepted budget and rate ranges.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Optional

from .limits import validate_rate_limit, validate_token_budget
from .token_accounting import SessionTokenAccount

__all__ = [
    "AUTHORIZED",
    "BUDGET_REACHED",
    "DEFAULT_TOKEN_BUDGET",
    "RATE_LIMITED",
    "RATE_LIMIT_WINDOW_SECONDS",
    "Decision",
    "CostController",
]

# The rolling window over which the per-user request rate is measured (Req 4.4).
RATE_LIMIT_WINDOW_SECONDS = 60.0

# The default per-session token budget applied when none is configured (Req 4.6).
DEFAULT_TOKEN_BUDGET = 100_000

# Reason codes carried by a :class:`Decision`.
AUTHORIZED = "authorized"
BUDGET_REACHED = "budget_reached"
RATE_LIMITED = "rate_limited"


@dataclass(frozen=True)
class Decision:
    """The outcome of a :meth:`Cost_Controller.authorize` call.

    Attributes:
        authorized: ``True`` iff the caller may invoke the ``LLM_Generator``.
        reason: One of :data:`AUTHORIZED`, :data:`BUDGET_REACHED`, or
            :data:`RATE_LIMITED`, identifying why the request was allowed or
            blocked.
        message: A user-facing explanation. ``None`` when authorized; the
            budget-reached message (Req 4.3) or the rate-limit message (Req 4.5)
            when blocked.
        retry_after_seconds: For a :data:`RATE_LIMITED` decision, the number of
            seconds after which further requests will be accepted -- always in
            the interval ``(0, 60]`` (Req 4.5). ``None`` for every other
            decision.
    """

    authorized: bool
    reason: str
    message: Optional[str] = None
    retry_after_seconds: Optional[float] = None


class CostController:
    """Enforces per-session token budgets and per-user request rates.

    A single controller instance serves every session and user of a running
    Plugin_Builder. Token budgets are tracked per ``session_id``; request rates
    are tracked per ``user_id``. Both limits are configured once at construction
    and validated against their inclusive ranges (Req 4.1, 4.4).

    The controller is not thread-safe; it is intended to be driven from the
    single-operator orchestration loop.
    """

    def __init__(
        self,
        token_budget: Optional[int] = None,
        rate_limit: int = 60,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a controller with a validated token budget and request rate.

        Args:
            token_budget: The per-session token budget; must be an integer in
                the inclusive range ``1..10,000,000`` (Req 4.1). When ``None``
                (or omitted) no budget is configured and the controller applies
                the default maximum of :data:`DEFAULT_TOKEN_BUDGET` (100,000)
                tokens per session (Req 4.6).
            rate_limit: The per-user maximum requests per minute; must be an
                integer in the inclusive range ``1..1,000`` (Req 4.4).
            clock: A zero-argument callable returning a monotonically
                non-decreasing time in seconds, used to age out the rate-limit
                window. Injectable for deterministic testing; defaults to
                :func:`time.monotonic`.

        Raises:
            LimitOutOfRangeError: If an explicit ``token_budget`` or the
                ``rate_limit`` falls outside its inclusive range.
        """
        if token_budget is None:
            token_budget = DEFAULT_TOKEN_BUDGET
        self._token_budget = validate_token_budget(token_budget)
        self._rate_limit = validate_rate_limit(rate_limit)
        self._clock = clock
        # Per-session cumulative token accounts (fed by record_usage in 7.4).
        self._accounts: Dict[str, SessionTokenAccount] = {}
        # Per-user timestamps of authorized requests within the rolling window.
        self._request_times: Dict[str, Deque[float]] = {}

    @property
    def token_budget(self) -> int:
        """The configured per-session token budget."""
        return self._token_budget

    @property
    def rate_limit(self) -> int:
        """The configured per-user maximum requests per minute."""
        return self._rate_limit

    def _session_account(self, session_id: str) -> SessionTokenAccount:
        """Return the token account for ``session_id``, creating it on demand.

        This is the seam ``record_usage`` (task 7.4) uses to add successful
        invocation tokens to a session's cumulative total; :meth:`authorize`
        reads the same account to decide whether the budget has been reached.
        """
        account = self._accounts.get(session_id)
        if account is None:
            account = SessionTokenAccount()
            self._accounts[session_id] = account
        return account

    def session_total(self, session_id: str) -> int:
        """Return the cumulative token total recorded for ``session_id``.

        A session with no recorded usage totals ``0``.
        """
        account = self._accounts.get(session_id)
        return account.total if account is not None else 0

    def record_usage(self, session_id: str, tokens: int, succeeded: bool = True) -> int:
        """Record an ``LLM_Generator`` invocation's token usage for a session.

        A *successful* invocation adds its token count to the session's
        cumulative total (Req 3.5); a *failed* invocation is excluded and leaves
        the total unchanged (Req 3.7). The maintained total is exactly what
        :meth:`authorize` checks against the budget, so recording usage after a
        completed invocation is what eventually drives a session to its budget.

        Args:
            session_id: The session whose cumulative token total is updated.
            tokens: The non-negative token count consumed by the invocation.
            succeeded: Whether the invocation completed successfully. When
                ``False`` the tokens are excluded from the total (Req 3.7).

        Returns:
            The cumulative session token total after recording, a non-negative
            integer (Req 3.6).

        Raises:
            TypeError: If ``tokens`` is not a non-negative ``int``.
            ValueError: If ``tokens`` is negative.
        """
        return self._session_account(session_id).record(tokens, succeeded)

    def authorize(self, session_id: str, user_id: str) -> Decision:
        """Decide whether ``user_id`` may invoke the LLM for ``session_id``.

        The two limits are checked in order of severity. The per-session token
        budget is terminal: once a session's cumulative usage reaches the
        configured budget, every further request is blocked (Req 4.2) and the
        budget-reached message is returned (Req 4.3) without consuming a
        rate-limit slot. Otherwise the per-user request rate is enforced over a
        rolling one-minute window; a burst beyond the configured maximum is
        rejected without invoking the LLM, each rejection carrying a retry-after
        in ``(0, 60]`` seconds (Req 4.5).

        A request is only counted against the rate window when it is authorized;
        blocked and rejected requests consume no slot, so exactly ``rate_limit``
        requests are admitted per user per minute.

        Args:
            session_id: The session whose token budget governs this request.
            user_id: The user whose request rate governs this request.

        Returns:
            A :class:`Decision`. When ``authorized`` is ``True`` the caller may
            invoke the ``LLM_Generator``; when ``False`` the ``reason`` and
            ``message`` explain why, and ``retry_after_seconds`` is populated for
            a rate-limit rejection.
        """
        # Budget check first: reaching the budget terminally blocks the session
        # and must not be masked by (or consume) a rate-limit slot (Req 4.2).
        if self.session_total(session_id) >= self._token_budget:
            return Decision(
                authorized=False,
                reason=BUDGET_REACHED,
                message=(
                    f"Session token budget of {self._token_budget} tokens reached. "
                    "No further LLM requests will be processed for this session."
                ),
            )

        now = self._clock()
        window = self._request_times.get(user_id)
        if window is None:
            window = deque()
            self._request_times[user_id] = window
        self._evict_expired(window, now)

        if len(window) >= self._rate_limit:
            retry_after = self._retry_after(window, now)
            return Decision(
                authorized=False,
                reason=RATE_LIMITED,
                message=(
                    f"Request rate limit of {self._rate_limit} requests per minute exceeded. "
                    f"Retry after {math.ceil(retry_after)} seconds."
                ),
                retry_after_seconds=retry_after,
            )

        # Authorized: record the request against the rolling window.
        window.append(now)
        return Decision(authorized=True, reason=AUTHORIZED)

    @staticmethod
    def _evict_expired(window: Deque[float], now: float) -> None:
        """Drop request timestamps that fell outside the one-minute window.

        A timestamp ``t`` is retained iff ``now - t < 60`` so that only requests
        within the rolling window count toward the rate limit.
        """
        cutoff = now - RATE_LIMIT_WINDOW_SECONDS
        while window and window[0] <= cutoff:
            window.popleft()

    @staticmethod
    def _retry_after(window: Deque[float], now: float) -> float:
        """Return seconds until the window frees a slot, always in ``(0, 60]``.

        The oldest in-window request expires ``60`` seconds after it was made;
        that is the earliest moment a new request will be accepted. Because the
        oldest retained timestamp ``t`` satisfies ``now - 60 < t <= now``, the
        result ``t + 60 - now`` is strictly greater than ``0`` and at most
        ``60`` (Req 4.5).
        """
        oldest = window[0]
        return oldest + RATE_LIMIT_WINDOW_SECONDS - now
