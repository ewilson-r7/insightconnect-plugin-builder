"""Unit tests for the Visualization view-model builder (task 5.5; Req 5.1, 5.2, 5.4).

These cover specific examples and edge cases: every component appearing as a
node, actions/triggers carrying both input and output schemas, node ordering,
field ordering, and single-node selection exposing exactly that node's fields.
The universal completeness property is covered separately by the property test
(task 5.6, Property 13).
"""

import pytest

from icplugin_builder.core.spec_model import (
    Component,
    FieldSchema,
    PluginSpec,
    SemVer,
)
from icplugin_builder.core.view_model import (
    ACTION,
    CONNECTION,
    TASK,
    TRIGGER,
    NodeNotFoundError,
    build_view_model,
)


def _full_spec() -> PluginSpec:
    return PluginSpec(
        name="example_plugin",
        title="Example Plugin",
        description="Does example things.",
        version=SemVer(2, 3, 4),
        vendor="rapid7_custom",
        connection={
            "api_key": FieldSchema(type="password", required=True, description="The API key."),
            "url": FieldSchema(type="string", required=False, order=1),
        },
        actions={
            "list_things": Component(
                title="List Things",
                description="Lists things.",
                input={"query": FieldSchema(type="string", required=False, description="Search query.")},
                output={"things": FieldSchema(type="[]string", required=True, description="The things.")},
            )
        },
        triggers={
            "on_thing": Component(
                title="On Thing",
                input={"interval": FieldSchema(type="integer", required=True)},
                output={"thing": FieldSchema(type="object", required=False)},
            )
        },
        tasks={
            "cleanup": Component(title="Cleanup", input={"batch": FieldSchema(type="integer")}, output={}),
        },
    )


class TestBuildViewModel:
    def test_includes_every_component_as_a_node(self):
        # Req 5.1: connection, actions, triggers, tasks all present.
        vm = build_view_model(_full_spec())
        assert vm.has_connection
        assert [node.name for node in vm.actions] == ["list_things"]
        assert [node.name for node in vm.triggers] == ["on_thing"]
        assert [node.name for node in vm.tasks] == ["cleanup"]

    def test_action_and_trigger_carry_input_and_output_schema(self):
        # Req 5.2: input and output schema for each action and trigger.
        vm = build_view_model(_full_spec())
        action = vm.actions[0]
        assert [f.name for f in action.input] == ["query"]
        assert [f.name for f in action.output] == ["things"]
        trigger = vm.triggers[0]
        assert [f.name for f in trigger.input] == ["interval"]
        assert [f.name for f in trigger.output] == ["thing"]

    def test_required_flag_is_carried_through(self):
        vm = build_view_model(_full_spec())
        things = vm.actions[0].output[0]
        assert things.name == "things"
        assert things.required is True
        query = vm.actions[0].input[0]
        assert query.required is False

    def test_connection_node_holds_connection_fields(self):
        vm = build_view_model(_full_spec())
        # Fields ordered by `order` then name: url(order=1) before api_key(unordered).
        assert [f.name for f in vm.connection.input] == ["url", "api_key"]
        assert vm.connection.output == ()

    def test_nodes_iterates_connection_first(self):
        vm = build_view_model(_full_spec())
        nodes = vm.nodes()
        assert nodes[0].kind == CONNECTION
        kinds = {node.kind for node in nodes}
        assert kinds == {CONNECTION, ACTION, TRIGGER, TASK}

    def test_node_ids_are_stable_and_kind_scoped(self):
        vm = build_view_model(_full_spec())
        assert vm.connection.node_id == CONNECTION
        assert vm.actions[0].node_id == "action:list_things"
        assert vm.triggers[0].node_id == "trigger:on_thing"
        assert vm.tasks[0].node_id == "task:cleanup"


class TestSelection:
    def test_select_returns_exactly_that_components_fields(self):
        # Req 5.4: selecting a single node exposes exactly its fields.
        vm = build_view_model(_full_spec())
        selected = vm.select("action:list_things")
        assert selected.name == "list_things"
        assert {f.name for f in selected.fields} == {"query", "things"}

    def test_select_connection(self):
        vm = build_view_model(_full_spec())
        selected = vm.select(CONNECTION)
        assert {f.name for f in selected.fields} == {"api_key", "url"}

    def test_selected_fields_exclude_other_components(self):
        vm = build_view_model(_full_spec())
        selected = vm.select("trigger:on_thing")
        names = {f.name for f in selected.fields}
        assert names == {"interval", "thing"}
        # No leakage from the action or task.
        assert "query" not in names
        assert "batch" not in names

    def test_select_unknown_node_raises(self):
        vm = build_view_model(_full_spec())
        with pytest.raises(NodeNotFoundError):
            vm.select("action:does_not_exist")


class TestEmptyAndSparse:
    def test_empty_spec_is_empty(self):
        vm = build_view_model(PluginSpec(name="p", title="P", vendor="v_custom"))
        assert vm.is_empty
        assert not vm.has_connection
        assert vm.component_nodes() == ()

    def test_connection_only_is_not_empty(self):
        spec = PluginSpec(
            name="p",
            title="P",
            vendor="v_custom",
            connection={"token": FieldSchema(type="password", required=True)},
        )
        vm = build_view_model(spec)
        assert not vm.is_empty
        assert vm.has_connection

    def test_action_with_no_fields_has_empty_schemas(self):
        spec = PluginSpec(
            name="p",
            title="P",
            vendor="v_custom",
            actions={"noop": Component(title="No Op")},
        )
        vm = build_view_model(spec)
        assert not vm.is_empty
        assert vm.actions[0].input == ()
        assert vm.actions[0].output == ()
        assert vm.select("action:noop").fields == ()
