"""Unit tests for the generic commit-on-success atomic apply wrapper (task 4.5).

These cover specific examples and edge cases for :mod:`icplugin_builder.core.atomic`:
the stateless helpers (:func:`atomic_apply`, :func:`try_atomic_apply`) and the
stateful :class:`AtomicState` holder. The focus is the atomicity guarantee --
that a failing step leaves the pre-step state byte-identical -- including the
adversarial case of a step that mutates its working copy and *then* raises. The
universal failure-atomicity property is covered separately by the property test
(task 4.6).
"""

import copy

import pytest

from icplugin_builder.core.atomic import (
    AtomicResult,
    AtomicState,
    atomic_apply,
    try_atomic_apply,
)
from icplugin_builder.core.draft import ComponentKind, Draft
from icplugin_builder.core.spec_model import Component, FieldSchema, PluginSpec


class _Boom(Exception):
    """Distinct exception type so tests assert the exact failure propagated."""


def _draft() -> Draft:
    spec = PluginSpec(
        name="demo",
        title="Demo",
        vendor="rapid7",
        actions={"alpha": Component(title="Alpha", input={"a": FieldSchema(type="string")})},
    )
    return Draft(spec=spec, code_files={"icon_demo/actions/alpha/action.py": b"# alpha\n"})


class TestAtomicApplySuccess:
    def test_returns_committed_new_state(self):
        result = atomic_apply(2, lambda n: n + 40)
        assert result == 42

    def test_step_runs_on_isolated_copy_not_the_original(self):
        original = {"items": [1, 2, 3]}

        def step(working):
            working["items"].append(4)
            return working

        new_state = atomic_apply(original, step)
        assert new_state == {"items": [1, 2, 3, 4]}
        # The caller's object is untouched even on success.
        assert original == {"items": [1, 2, 3]}

    def test_commits_non_mutating_draft_operation(self):
        draft = _draft()
        new_draft = atomic_apply(
            draft,
            lambda d: d.add_component(ComponentKind.ACTION, "beta", Component(title="Beta")),
        )
        assert new_draft.has_component(ComponentKind.ACTION, "beta")
        # Original draft object is unchanged.
        assert not draft.has_component(ComponentKind.ACTION, "beta")


class TestAtomicApplyFailure:
    def test_reraises_step_exception(self):
        def step(_):
            raise _Boom("step failed")

        with pytest.raises(_Boom):
            atomic_apply({"k": "v"}, step)

    def test_state_unchanged_when_step_mutates_then_raises(self):
        original = {"items": [1, 2, 3]}
        before = copy.deepcopy(original)

        def step(working):
            working["items"].append(999)  # partial mutation of the working copy
            raise _Boom("after partial mutation")

        with pytest.raises(_Boom):
            atomic_apply(original, step)
        # Pre-step state is byte-identical despite the partial mutation.
        assert original == before

    def test_draft_unchanged_when_step_fails(self):
        draft = _draft()
        before_spec = copy.deepcopy(draft.spec)
        before_code = dict(draft.code_files)

        def step(working):
            working.code_files["icon_demo/actions/alpha/action.py"] = b"# clobbered\n"
            raise _Boom("mid-edit failure")

        with pytest.raises(_Boom):
            atomic_apply(draft, step)
        assert draft.spec == before_spec
        assert draft.code_files == before_code


class TestTryAtomicApply:
    def test_success_reports_committed_state(self):
        result = try_atomic_apply(2, lambda n: n + 40)
        assert isinstance(result, AtomicResult)
        assert result.committed is True
        assert result.state == 42
        assert result.error is None

    def test_failure_preserves_state_and_captures_error(self):
        original = {"items": [1, 2, 3]}

        def step(working):
            working["items"].append(999)
            raise _Boom("nope")

        result = try_atomic_apply(original, step)
        assert result.committed is False
        assert result.state is original
        assert original == {"items": [1, 2, 3]}
        assert isinstance(result.error, _Boom)

    def test_keyboard_interrupt_propagates(self):
        def step(_):
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            try_atomic_apply({"k": "v"}, step)


class TestAtomicState:
    def test_apply_commits_on_success(self):
        cell = AtomicState(0)
        assert cell.apply(lambda n: n + 5) == 5
        assert cell.apply(lambda n: n + 7) == 12
        assert cell.state == 12

    def test_apply_leaves_state_unchanged_on_failure(self):
        cell = AtomicState({"items": [1, 2, 3]})

        def step(working):
            working["items"].append(999)
            raise _Boom("fail")

        with pytest.raises(_Boom):
            cell.apply(step)
        assert cell.state == {"items": [1, 2, 3]}

    def test_try_apply_commits_on_success(self):
        cell = AtomicState(0)
        result = cell.try_apply(lambda n: n + 3)
        assert result.committed is True
        assert result.state == 3
        assert cell.state == 3

    def test_try_apply_preserves_state_on_failure(self):
        cell = AtomicState(10)

        def step(_):
            raise _Boom("fail")

        result = cell.try_apply(step)
        assert result.committed is False
        assert isinstance(result.error, _Boom)
        assert cell.state == 10

    def test_failed_step_does_not_block_later_success(self):
        cell = AtomicState({"actions": ["alpha"]})

        def failing(working):
            working["actions"].append("bad")
            raise _Boom("fail")

        cell.try_apply(failing)
        assert cell.state == {"actions": ["alpha"]}

        def good(working):
            working["actions"].append("beta")
            return working

        result = cell.try_apply(good)
        assert result.committed is True
        assert cell.state == {"actions": ["alpha", "beta"]}

    def test_holds_draft_and_commits_targeted_operation(self):
        cell = AtomicState(_draft())
        cell.apply(lambda d: d.add_component(ComponentKind.ACTION, "beta", Component(title="Beta")))
        assert cell.state.has_component(ComponentKind.ACTION, "beta")
        assert cell.state.has_component(ComponentKind.ACTION, "alpha")
