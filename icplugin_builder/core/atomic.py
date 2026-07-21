"""Generic commit-on-success atomic apply wrapper (design Property 2; Req 1.7,
9.5, 11.6, 14.6, 19.3).

Many operations in the builder transform some working state -- a
:class:`~icplugin_builder.core.draft.Draft`, a ``Plugin_Spec``, an on-disk
source tree, a registry snapshot, a credential blob -- by running a *step* that
either succeeds and produces a new state or fails partway through. The
overarching error-handling rule for all of them is the same: **fail closed,
preserve state.** A step is only allowed to change the committed state when it
fully succeeds; on *any* failure the state must be byte-for-byte identical to
what it was immediately before the step began (design Property 2).

This module is the single, generic seam that gives every such operation that
guarantee, so the rule lives in one exhaustively tested place rather than being
re-implemented (and re-broken) at each call site:

* **Isolation** -- the step never runs against the live state. It is handed an
  independent deep copy, so even a step that mutates its argument in place and
  *then* raises cannot reach back into the pre-step state.
* **Commit-on-success** -- the new state replaces the old one only after the
  step returns normally. If the step raises, the committed state is left
  exactly as it was.

The :class:`Draft` operations are already non-mutating (they return a new
draft), which composes cleanly here: a step can call ``draft.add_component(...)``
and return the result, and this wrapper decides whether that result is
committed. But the isolation guarantee means the wrapper is equally safe for
steps that mutate their working copy directly (e.g. editing a copied file tree).

Two shapes are offered, mirroring the split in
:mod:`icplugin_builder.core.token_accounting`:

* :func:`atomic_apply` / :func:`try_atomic_apply` -- stateless helpers that take
  a state and a step and return the committed (or preserved) state.
* :class:`AtomicState` -- a small stateful holder that owns a current state and
  commits successful steps into it one at a time.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable, Generic, Optional, TypeVar

__all__ = [
    "AtomicResult",
    "atomic_apply",
    "try_atomic_apply",
    "AtomicState",
]

S = TypeVar("S")

#: A step transforms a working copy of the state into the next state. It may
#: build and return a fresh value (the non-mutating style used by ``Draft``) or
#: mutate the working copy it is given and return it; either way the value it
#: returns is what gets committed on success.
Step = Callable[[S], S]

#: How a state is isolated before a step runs. Defaults to :func:`copy.deepcopy`
#: so the step cannot reach the pre-step state through shared references.
Copier = Callable[[S], S]


@dataclass(frozen=True)
class AtomicResult(Generic[S]):
    """The outcome of a non-raising atomic apply.

    Attributes:
        state: The resulting state. On success this is the step's committed new
            state; on failure it is the pre-step state, unchanged.
        committed: ``True`` iff the step succeeded and its result was committed.
        error: The exception raised by the step when ``committed`` is ``False``;
            ``None`` on success.
    """

    state: S
    committed: bool
    error: Optional[Exception] = None


def atomic_apply(state: S, step: Step, *, copier: Copier = copy.deepcopy) -> S:
    """Run ``step`` and return its new state, or re-raise leaving ``state`` intact.

    The step is run against an independent copy of ``state`` (via ``copier``), so
    the object passed in as ``state`` is never mutated regardless of what the
    step does. On success the step's returned value is the committed new state.
    On failure the step's exception propagates unchanged and the caller's
    ``state`` object is guaranteed identical to its pre-call value (design
    Property 2; Req 1.7, 9.5, 11.6, 14.6, 19.3).

    Args:
        state: The current state to transform. Left unchanged on failure.
        step: The transformation to run; receives an isolated working copy and
            returns the next state.
        copier: How to isolate ``state`` before running the step. Defaults to
            :func:`copy.deepcopy`.

    Returns:
        The committed new state produced by a successful step.

    Raises:
        Exception: Whatever the step raises. ``state`` is untouched in this case.
    """
    working = copier(state)
    return step(working)


def try_atomic_apply(state: S, step: Step, *, copier: Copier = copy.deepcopy) -> AtomicResult[S]:
    """Run ``step`` and report the outcome without raising on step failure.

    Behaves like :func:`atomic_apply` but captures a step failure instead of
    propagating it: on success the result carries the committed new state; on
    failure it carries the unchanged pre-step ``state`` and the captured
    exception. Either way the ``state`` object passed in is never mutated
    (design Property 2).

    Only :class:`Exception` is captured; :class:`BaseException` subclasses such
    as :class:`KeyboardInterrupt` and :class:`SystemExit` propagate so the
    process can still be interrupted or shut down.

    Args:
        state: The current state to transform. Preserved as-is on failure.
        step: The transformation to run; receives an isolated working copy.
        copier: How to isolate ``state`` before running the step.

    Returns:
        An :class:`AtomicResult` describing the committed or preserved state.
    """
    working = copier(state)
    try:
        new_state = step(working)
    except Exception as error:  # noqa: BLE001 -- deliberate: preserve state, report the failure.
        return AtomicResult(state=state, committed=False, error=error)
    return AtomicResult(state=new_state, committed=True, error=None)


class AtomicState(Generic[S]):
    """A holder for a current state that commits successful steps into itself.

    The holder owns a single current :attr:`state`. Each :meth:`apply` /
    :meth:`try_apply` runs a step against an isolated copy of that state and
    replaces the held state with the step's result *only* when the step
    succeeds. A failing step leaves :attr:`state` exactly as it was, so the
    invariant "the held state only ever advances through fully-successful steps"
    holds after every call (design Property 2; Req 1.7, 9.5, 11.6, 14.6, 19.3).
    """

    def __init__(self, initial: S, *, copier: Copier = copy.deepcopy) -> None:
        """Create a holder around ``initial``.

        Args:
            initial: The starting state.
            copier: How to isolate the held state before each step runs.
                Defaults to :func:`copy.deepcopy`.
        """
        self._state = initial
        self._copier = copier

    @property
    def state(self) -> S:
        """The current committed state."""
        return self._state

    def apply(self, step: Step) -> S:
        """Run ``step`` and commit its result, or re-raise leaving state intact.

        Args:
            step: The transformation to run against an isolated copy of the
                current state.

        Returns:
            The new committed state.

        Raises:
            Exception: Whatever the step raises; :attr:`state` is unchanged.
        """
        new_state = atomic_apply(self._state, step, copier=self._copier)
        self._state = new_state
        return new_state

    def try_apply(self, step: Step) -> AtomicResult[S]:
        """Run ``step``, committing on success and preserving state on failure.

        Args:
            step: The transformation to run against an isolated copy of the
                current state.

        Returns:
            An :class:`AtomicResult`. On success its state is committed into
            this holder; on failure the holder's :attr:`state` is left unchanged
            and the result carries the captured exception.
        """
        result = try_atomic_apply(self._state, step, copier=self._copier)
        if result.committed:
            self._state = result.state
        return result
