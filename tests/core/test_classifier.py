"""Unit tests for the breaking-change classifier (task 2.3).

These cover specific examples and edge cases for Req 12.2 / design Property 23.
The universal iff-property across generated inputs is covered separately by the
property test (task 2.4).
"""

from icplugin_builder.core.classifier import (
    classify_change,
    is_breaking_change,
)
from icplugin_builder.core.spec_model import Component, FieldSchema, PluginSpec


def _spec(**kwargs) -> PluginSpec:
    base = dict(name="example", title="Example", description="d", vendor="rapid7")
    base.update(kwargs)
    return PluginSpec(**base)


def _action(inputs=None, outputs=None) -> Component:
    return Component(
        title="Run",
        input=dict(inputs or {}),
        output=dict(outputs or {}),
    )


class TestNonBreaking:
    def test_identical_specs_not_breaking(self):
        spec = _spec(actions={"run": _action({"x": FieldSchema(type="string", required=True)})})
        other = _spec(actions={"run": _action({"x": FieldSchema(type="string", required=True)})})
        assert is_breaking_change(spec, other) is False

    def test_adding_a_new_optional_field_not_breaking(self):
        old = _spec(actions={"run": _action({"x": FieldSchema(type="string")})})
        new = _spec(
            actions={"run": _action({"x": FieldSchema(type="string"), "y": FieldSchema(type="string", required=False)})}
        )
        assert is_breaking_change(old, new) is False

    def test_adding_a_whole_new_action_not_breaking(self):
        old = _spec(actions={"run": _action({"x": FieldSchema(type="string")})})
        new = _spec(
            actions={
                "run": _action({"x": FieldSchema(type="string")}),
                "brand_new": _action({"z": FieldSchema(type="integer", required=True)}),
            }
        )
        assert is_breaking_change(old, new) is False

    def test_required_to_optional_not_breaking(self):
        old = _spec(actions={"run": _action({"x": FieldSchema(type="string", required=True)})})
        new = _spec(actions={"run": _action({"x": FieldSchema(type="string", required=False)})})
        assert is_breaking_change(old, new) is False

    def test_adding_new_connection_field_not_breaking(self):
        old = _spec(connection={"url": FieldSchema(type="string", required=True)})
        new = _spec(
            connection={
                "url": FieldSchema(type="string", required=True),
                "token": FieldSchema(type="password", required=False),
            }
        )
        assert is_breaking_change(old, new) is False

    def test_trigger_and_task_changes_are_out_of_scope(self):
        # Req 12.2 is defined only over existing actions and the connection.
        old = _spec(triggers={"poll": _action({"x": FieldSchema(type="string", required=True)})})
        new = _spec(triggers={})  # trigger removed entirely
        assert is_breaking_change(old, new) is False


class TestBreaking:
    def test_removing_an_action_field_is_breaking(self):
        old = _spec(actions={"run": _action({"x": FieldSchema(type="string")})})
        new = _spec(actions={"run": _action({})})
        result = classify_change(old, new)
        assert result.is_breaking is True
        assert any("field 'x' was removed" in r for r in result.reasons)

    def test_changing_a_field_type_is_breaking(self):
        old = _spec(actions={"run": _action({"x": FieldSchema(type="string")})})
        new = _spec(actions={"run": _action({"x": FieldSchema(type="integer")})})
        result = classify_change(old, new)
        assert result.is_breaking is True
        assert any("type changed" in r for r in result.reasons)

    def test_optional_to_required_is_breaking(self):
        old = _spec(actions={"run": _action({"x": FieldSchema(type="string", required=False)})})
        new = _spec(actions={"run": _action({"x": FieldSchema(type="string", required=True)})})
        result = classify_change(old, new)
        assert result.is_breaking is True
        assert any("optional to required" in r for r in result.reasons)

    def test_removing_an_action_is_breaking(self):
        old = _spec(actions={"run": _action({"x": FieldSchema(type="string")})})
        new = _spec(actions={})
        result = classify_change(old, new)
        assert result.is_breaking is True
        assert any("action 'run' was removed" in r for r in result.reasons)

    def test_output_field_removal_is_breaking(self):
        old = _spec(actions={"run": _action(outputs={"y": FieldSchema(type="string")})})
        new = _spec(actions={"run": _action(outputs={})})
        assert is_breaking_change(old, new) is True

    def test_connection_field_type_change_is_breaking(self):
        old = _spec(connection={"port": FieldSchema(type="string", required=True)})
        new = _spec(connection={"port": FieldSchema(type="integer", required=True)})
        assert is_breaking_change(old, new) is True

    def test_connection_field_removal_is_breaking(self):
        old = _spec(connection={"url": FieldSchema(type="string", required=True)})
        new = _spec(connection={})
        assert is_breaking_change(old, new) is True

    def test_multiple_breaking_reasons_are_all_reported(self):
        old = _spec(
            actions={
                "run": _action({"x": FieldSchema(type="string"), "y": FieldSchema(type="string")}),
                "gone": _action({"z": FieldSchema(type="string")}),
            }
        )
        new = _spec(actions={"run": _action({"x": FieldSchema(type="integer")})})
        result = classify_change(old, new)
        assert result.is_breaking is True
        # y removed, x type changed, action 'gone' removed -> 3 reasons
        assert len(result.reasons) == 3
