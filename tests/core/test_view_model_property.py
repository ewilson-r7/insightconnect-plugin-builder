"""Property-based test for the Visualization view-model builder (task 5.6).

Covers design Property 13 with Hypothesis: across arbitrary parseable specs the
built view-model includes every connection/action/trigger/task, carries the
input and output schema of every action and trigger, and, on single selection,
returns exactly that node's fields (and no other node's).
"""

from hypothesis import given, settings

from icplugin_builder.core.spec_model import PluginSpec
from icplugin_builder.core.view_model import (
    ACTION,
    CONNECTION,
    TASK,
    TRIGGER,
    build_view_model,
)
from tests.strategies import plugin_specs


# Feature: insightconnect-plugin-builder, Property 13: Visualization view-model completeness
@settings(max_examples=200)
@given(spec=plugin_specs())
def test_view_model_is_complete(spec: PluginSpec):
    """The view-model includes every component and schema, and selects exactly one node.

    **Validates: Requirements 5.1, 5.2, 5.4**
    """
    vm = build_view_model(spec)

    # Req 5.1: every connection field, action, trigger, and task is represented.
    assert {f.name for f in vm.connection.input} == set(spec.connection)
    assert vm.connection.output == ()
    assert [node.name for node in vm.actions] == list(spec.actions)
    assert [node.name for node in vm.triggers] == list(spec.triggers)
    assert [node.name for node in vm.tasks] == list(spec.tasks)

    # Req 5.2: the input and output schema of every action and trigger is present.
    for kind, nodes, components in (
        (ACTION, vm.actions, spec.actions),
        (TRIGGER, vm.triggers, spec.triggers),
        (TASK, vm.tasks, spec.tasks),
    ):
        for node in nodes:
            component = components[node.name]
            assert node.kind == kind
            assert node.node_id == f"{kind}:{node.name}"
            assert {f.name for f in node.input} == set(component.input)
            assert {f.name for f in node.output} == set(component.output)

    # Req 5.4: selecting any single node returns exactly that node and its fields.
    for node in vm.nodes():
        selected = vm.select(node.node_id)
        assert selected is node
        assert selected.fields == node.input + node.output
        assert {f.name for f in selected.fields} == {f.name for f in node.input} | {f.name for f in node.output}

    # The connection is always addressable, and the node index covers every node exactly once.
    assert vm.select(CONNECTION) is vm.connection
    node_ids = [node.node_id for node in vm.nodes()]
    assert len(node_ids) == len(set(node_ids))
