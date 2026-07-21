"""Property-based test for the generic atomic apply wrapper (task 4.6).

Covers design Property 2 (failure atomicity -- no partial mutation) with
Hypothesis: across arbitrary states, including :class:`Draft` states, a step
that mutates its working copy and *then* raises must leave the pre-step state
byte-for-byte identical, and a step that succeeds must commit its new state
without ever touching the caller's original object.
"""

from __future__ import annotations

import pickle
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.atomic import AtomicState, atomic_apply, try_atomic_apply
from icplugin_builder.core.draft import Draft
from icplugin_builder.core.spec_model import Component

from tests.strategies import plugin_specs


class _Boom(Exception):
    """Distinct exception type so the test asserts the exact failure propagated."""


#: The path/content key a mutating step injects into a working copy. Using a
#: fixed sentinel guarantees the mutation is always observable (the key never
#: pre-exists in a generated state).
_INJECTED = "__injected_by_step__"


def _draft_states() -> st.SearchStrategy[Draft]:
    """Generate arbitrary :class:`Draft` states with spec and code files."""
    code_files = st.dictionaries(
        st.text(alphabet="abcde/._", min_size=1, max_size=8),
        st.binary(max_size=8),
        max_size=4,
    )
    return st.builds(Draft, spec=plugin_specs(), code_files=code_files)


def _container_states() -> st.SearchStrategy[Any]:
    """Generate arbitrary nested mutable ``dict``/``list`` container states."""
    leaves = st.one_of(st.integers(), st.text(max_size=5), st.booleans(), st.none())
    return st.recursive(
        leaves,
        lambda children: st.one_of(
            st.lists(children, max_size=4),
            st.dictionaries(st.text(max_size=4), children, max_size=4),
        ),
        max_leaves=8,
    ).filter(lambda value: isinstance(value, (dict, list)))


def atomic_states() -> st.SearchStrategy[Any]:
    """Generate the mutable states the wrapper must protect (Drafts + containers)."""
    return st.one_of(_draft_states(), _container_states())


def _mutate_in_place(working: Any) -> None:
    """Partially mutate ``working`` in place so the injection is always observable.

    Every branch adds the :data:`_INJECTED` sentinel, so a state that is mutated
    this way is guaranteed to differ from its pre-step snapshot.
    """
    if isinstance(working, Draft):
        working.code_files[_INJECTED] = b"partial"
        working.spec.actions[_INJECTED] = Component(title="partial")
    elif isinstance(working, dict):
        working[_INJECTED] = "partial"
    elif isinstance(working, list):
        working.append(_INJECTED)
    else:  # pragma: no cover - the state strategy only yields Drafts/containers.
        raise TypeError(f"unexpected state type: {type(working)!r}")


# Feature: insightconnect-plugin-builder, Property 2: Failure atomicity (no partial mutation)
@settings(max_examples=200)
@given(state=atomic_states())
def test_failing_step_preserves_state_and_success_commits(state: Any):
    """A failing step leaves state byte-identical; a succeeding step commits.

    **Validates: Requirements 1.7, 9.5, 11.6, 14.6, 19.3**
    """
    before = pickle.dumps(state)

    def failing(working: Any) -> Any:
        _mutate_in_place(working)  # partial mutation of the isolated working copy
        raise _Boom("failed after partial mutation")

    def succeeding(working: Any) -> Any:
        _mutate_in_place(working)
        return working

    # --- Failure atomicity: the pre-step state is byte-for-byte unchanged. ---

    # atomic_apply re-raises the step's exception and never touches the original.
    with pytest.raises(_Boom):
        atomic_apply(state, failing)
    assert pickle.dumps(state) == before

    # try_atomic_apply reports the failure, preserves the original object, and
    # leaves it byte-identical.
    result = try_atomic_apply(state, failing)
    assert result.committed is False
    assert result.state is state
    assert isinstance(result.error, _Boom)
    assert pickle.dumps(state) == before

    # AtomicState.apply re-raises and leaves the held state untouched.
    cell = AtomicState(state)
    with pytest.raises(_Boom):
        cell.apply(failing)
    assert cell.state is state
    assert pickle.dumps(cell.state) == before

    # AtomicState.try_apply reports the failure and leaves the held state as-is.
    failed = cell.try_apply(failing)
    assert failed.committed is False
    assert isinstance(failed.error, _Boom)
    assert pickle.dumps(cell.state) == before

    # --- Commit-on-success: the new state is committed, the original is not. ---

    committed_state = atomic_apply(state, succeeding)
    assert pickle.dumps(state) == before  # original still untouched on success
    assert pickle.dumps(committed_state) != before  # new state carries the change

    ok = try_atomic_apply(state, succeeding)
    assert ok.committed is True
    assert ok.error is None
    assert pickle.dumps(state) == before
    assert pickle.dumps(ok.state) != before

    holder = AtomicState(state)
    new_state = holder.apply(succeeding)
    assert pickle.dumps(holder.state) == pickle.dumps(new_state)
    assert pickle.dumps(holder.state) != before
