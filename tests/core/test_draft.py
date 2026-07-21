"""Unit tests for the in-session Draft targeted component operations (task 4.1).

These cover specific examples and edge cases for :class:`Draft`: add/modify/
remove of a named component, code-file ownership, non-mutation of the receiver,
and the error seams (not-found and already-exists). The universal
component-preservation property is covered separately by the property test
(task 4.2).
"""

import copy

import pytest

from icplugin_builder.core.draft import (
    ComponentExistsError,
    ComponentKind,
    ComponentNotFoundError,
    Draft,
)
from icplugin_builder.core.spec_model import Component, FieldSchema, PluginSpec


def _spec_with_two_actions() -> PluginSpec:
    return PluginSpec(
        name="demo",
        title="Demo",
        vendor="rapid7",
        actions={
            "alpha": Component(title="Alpha", input={"a": FieldSchema(type="string")}),
            "beta": Component(title="Beta", output={"b": FieldSchema(type="integer")}),
        },
        triggers={"watch": Component(title="Watch")},
        tasks={"sweep": Component(title="Sweep")},
    )


def _draft_with_code() -> Draft:
    code = {
        "icon_demo/actions/alpha/action.py": b"# alpha logic\n",
        "icon_demo/actions/beta/action.py": b"# beta logic\n",
        "icon_demo/triggers/watch/trigger.py": b"# watch logic\n",
        "icon_demo/connection/connection.py": b"# connection\n",
    }
    return Draft(spec=_spec_with_two_actions(), code_files=dict(code))


class TestAdd:
    def test_add_action_adds_component_and_code(self):
        draft = _draft_with_code()
        new = draft.add_component(
            ComponentKind.ACTION,
            "gamma",
            Component(title="Gamma"),
            code_files={"icon_demo/actions/gamma/action.py": b"# gamma\n"},
        )
        assert new.has_component(ComponentKind.ACTION, "gamma")
        assert new.spec.actions["gamma"].title == "Gamma"
        assert new.code_files["icon_demo/actions/gamma/action.py"] == b"# gamma\n"

    def test_add_leaves_receiver_unchanged(self):
        draft = _draft_with_code()
        before_spec = copy.deepcopy(draft.spec)
        before_code = dict(draft.code_files)
        draft.add_component(ComponentKind.ACTION, "gamma", Component(title="Gamma"))
        assert draft.spec == before_spec
        assert draft.code_files == before_code

    def test_add_existing_name_raises(self):
        draft = _draft_with_code()
        with pytest.raises(ComponentExistsError):
            draft.add_component(ComponentKind.ACTION, "alpha", Component())

    def test_add_trigger_and_task_are_independent_namespaces(self):
        draft = _draft_with_code()
        # A trigger named "alpha" does not collide with the action "alpha".
        new = draft.add_component(ComponentKind.TRIGGER, "alpha", Component(title="Alpha trigger"))
        assert new.has_component(ComponentKind.TRIGGER, "alpha")
        assert new.has_component(ComponentKind.ACTION, "alpha")


class TestModify:
    def test_modify_replaces_component(self):
        draft = _draft_with_code()
        new = draft.modify_component(ComponentKind.ACTION, "alpha", Component(title="Alpha v2"))
        assert new.spec.actions["alpha"].title == "Alpha v2"
        # Other action untouched.
        assert new.spec.actions["beta"] == draft.spec.actions["beta"]

    def test_modify_without_code_preserves_owned_code(self):
        draft = _draft_with_code()
        new = draft.modify_component(ComponentKind.ACTION, "alpha", Component(title="Alpha v2"))
        assert new.code_files == draft.code_files

    def test_modify_with_code_replaces_owned_files_only(self):
        draft = _draft_with_code()
        new = draft.modify_component(
            ComponentKind.ACTION,
            "alpha",
            Component(title="Alpha v2"),
            code_files={"icon_demo/actions/alpha/action.py": b"# rewritten\n"},
        )
        beta_path = "icon_demo/actions/beta/action.py"
        assert new.code_files["icon_demo/actions/alpha/action.py"] == b"# rewritten\n"
        # beta and connection code are byte-identical.
        assert new.code_files[beta_path] == draft.code_files[beta_path]
        assert new.code_files["icon_demo/connection/connection.py"] == b"# connection\n"

    def test_modify_missing_name_raises(self):
        draft = _draft_with_code()
        with pytest.raises(ComponentNotFoundError):
            draft.modify_component(ComponentKind.ACTION, "nope", Component())

    def test_modify_leaves_receiver_unchanged(self):
        draft = _draft_with_code()
        before_spec = copy.deepcopy(draft.spec)
        draft.modify_component(ComponentKind.ACTION, "alpha", Component(title="changed"))
        assert draft.spec == before_spec


class TestRemove:
    def test_remove_drops_component_and_owned_code(self):
        draft = _draft_with_code()
        new = draft.remove_component(ComponentKind.ACTION, "alpha")
        assert not new.has_component(ComponentKind.ACTION, "alpha")
        assert "icon_demo/actions/alpha/action.py" not in new.code_files
        # beta preserved.
        assert new.has_component(ComponentKind.ACTION, "beta")
        assert "icon_demo/actions/beta/action.py" in new.code_files
        assert "icon_demo/connection/connection.py" in new.code_files

    def test_remove_missing_name_raises(self):
        draft = _draft_with_code()
        with pytest.raises(ComponentNotFoundError):
            draft.remove_component(ComponentKind.TASK, "nope")

    def test_remove_leaves_receiver_unchanged(self):
        draft = _draft_with_code()
        before_code = dict(draft.code_files)
        before_spec = copy.deepcopy(draft.spec)
        draft.remove_component(ComponentKind.ACTION, "alpha")
        assert draft.code_files == before_code
        assert draft.spec == before_spec


class TestOwnership:
    def test_owned_code_files_matches_directory_and_name(self):
        draft = _draft_with_code()
        owned = draft.owned_code_files(ComponentKind.ACTION, "alpha")
        assert set(owned) == {"icon_demo/actions/alpha/action.py"}

    def test_ownership_uses_full_segment_match(self):
        # A directory whose name merely starts with the target is not owned.
        draft = Draft(
            spec=PluginSpec(actions={"a": Component()}),
            code_files={
                "pkg/actions/a/action.py": b"x",
                "pkg/actions/alpha/action.py": b"y",
            },
        )
        owned = draft.owned_code_files(ComponentKind.ACTION, "a")
        assert set(owned) == {"pkg/actions/a/action.py"}

    def test_komand_prefix_paths_are_owned(self):
        draft = Draft(
            spec=PluginSpec(actions={"run": Component()}),
            code_files={"komand_legacy/actions/run/action.py": b"x"},
        )
        new = draft.remove_component(ComponentKind.ACTION, "run")
        assert new.code_files == {}


class TestNameValidation:
    def test_empty_name_rejected(self):
        draft = _draft_with_code()
        with pytest.raises(ValueError):
            draft.add_component(ComponentKind.ACTION, "", Component())


class TestNotFoundHandling:
    """Not-found handling for named-component operations (task 4.3; Req 15.4).

    A modify/remove targeting a name absent from the draft is rejected with a
    clear user-facing not-found message, and the draft is provably unchanged.
    """

    @pytest.mark.parametrize(
        "kind, existing_names",
        [
            (ComponentKind.ACTION, {"alpha", "beta"}),
            (ComponentKind.TRIGGER, {"watch"}),
            (ComponentKind.TASK, {"sweep"}),
        ],
    )
    def test_modify_missing_name_rejected(self, kind, existing_names):
        draft = _draft_with_code()
        with pytest.raises(ComponentNotFoundError) as excinfo:
            draft.modify_component(kind, "ghost", Component(title="X"))
        error = excinfo.value
        assert error.operation == "modify"
        assert error.kind is kind
        assert error.name == "ghost"
        assert set(error.available) == existing_names

    @pytest.mark.parametrize(
        "kind",
        [ComponentKind.ACTION, ComponentKind.TRIGGER, ComponentKind.TASK],
    )
    def test_remove_missing_name_rejected(self, kind):
        draft = _draft_with_code()
        with pytest.raises(ComponentNotFoundError) as excinfo:
            draft.remove_component(kind, "ghost")
        assert excinfo.value.operation == "remove"
        assert excinfo.value.kind is kind
        assert excinfo.value.name == "ghost"

    def test_message_names_the_missing_component_and_operation(self):
        draft = _draft_with_code()
        with pytest.raises(ComponentNotFoundError) as excinfo:
            draft.modify_component(ComponentKind.ACTION, "ghost", Component())
        message = str(excinfo.value)
        assert "ghost" in message
        assert "action" in message
        assert "modify" in message
        # Existing action names help the user correct the request.
        assert "alpha" in message and "beta" in message

    def test_message_when_no_components_of_kind_exist(self):
        draft = Draft(spec=PluginSpec(name="demo"))
        with pytest.raises(ComponentNotFoundError) as excinfo:
            draft.remove_component(ComponentKind.ACTION, "ghost")
        error = excinfo.value
        assert error.available == []
        assert "no actions" in str(error)

    def test_modify_missing_leaves_draft_provably_unchanged(self):
        draft = _draft_with_code()
        before_spec = copy.deepcopy(draft.spec)
        before_code = dict(draft.code_files)
        with pytest.raises(ComponentNotFoundError):
            draft.modify_component(
                ComponentKind.ACTION,
                "ghost",
                Component(title="X"),
                code_files={"icon_demo/actions/ghost/action.py": b"# ghost\n"},
            )
        assert draft.spec == before_spec
        assert draft.code_files == before_code

    def test_remove_missing_leaves_draft_provably_unchanged(self):
        draft = _draft_with_code()
        before_spec = copy.deepcopy(draft.spec)
        before_code = dict(draft.code_files)
        with pytest.raises(ComponentNotFoundError):
            draft.remove_component(ComponentKind.TRIGGER, "ghost")
        assert draft.spec == before_spec
        assert draft.code_files == before_code

    def test_not_found_error_is_a_draft_error(self):
        # The orchestrator/atomic-apply seam can catch the base DraftError.
        from icplugin_builder.core.draft import DraftError

        draft = _draft_with_code()
        with pytest.raises(DraftError):
            draft.remove_component(ComponentKind.TASK, "ghost")
