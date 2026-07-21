"""Smoke checks that the shared Hypothesis strategies produce sane values.

These are not property tests for a design property; they guard the generators
themselves (task 1.4) so the classifier/preservation tests that consume them
can rely on well-formed specs and correctly labeled mutations.
"""

from hypothesis import given
from hypothesis import strategies as st

from icplugin_builder.core.spec_model import Component, FieldSchema, PluginSpec
from icplugin_builder.core.yaml_codec import dump_plugin_spec, load_plugin_spec
from tests import strategies as strat


@given(strat.field_schemas())
def test_field_schema_type_is_supported(fs: FieldSchema):
    assert fs.type in strat.ALL_FIELD_TYPES


@given(strat.plugin_specs())
def test_generated_spec_is_plugin_spec_and_round_trips(spec: PluginSpec):
    assert isinstance(spec, PluginSpec)
    # A generated spec must survive the YAML codec unchanged.
    assert load_plugin_spec(dump_plugin_spec(spec)) == spec


@given(strat.mutatable_plugin_specs())
def test_mutatable_specs_have_targets(spec: PluginSpec):
    assert len(spec.actions) >= 1
    assert len(spec.connection) >= 1
    assert all(isinstance(a, Component) for a in spec.actions.values())


@given(strat.mutatable_plugin_specs().flatmap(strat.labeled_mutations))
def test_labeled_mutation_is_well_formed(mutation: strat.LabeledMutation):
    assert mutation.label in strat.MUTATION_LABELS
    assert mutation.breaking == (mutation.label in strat.BREAKING_LABELS)
    assert isinstance(mutation.spec, PluginSpec)


@given(st.data())
def test_mutation_deep_copies_base(data):
    base = data.draw(strat.mutatable_plugin_specs())
    before = dump_plugin_spec(base)
    data.draw(strat.labeled_mutations(base))
    # Applying a mutation must not touch the base spec.
    assert dump_plugin_spec(base) == before
