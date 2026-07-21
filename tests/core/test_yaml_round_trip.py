"""Property test for the Plugin_Spec YAML round trip (task 1.3).

# Feature: insightconnect-plugin-builder, Property 5: Plugin_Spec YAML round trip

Serializing any valid ``PluginSpec`` to ``plugin.spec.yaml`` text and loading it
back yields an equivalent ``PluginSpec`` (same components, fields, types, and
metadata).
"""

from hypothesis import given, settings

from icplugin_builder.core.spec_model import PluginSpec
from icplugin_builder.core.yaml_codec import dump_plugin_spec, load_plugin_spec
from tests import strategies as strat


# Property 5: Plugin_Spec YAML round trip
# Validates: Requirements 2.2, 21.5
@settings(max_examples=200)
@given(strat.plugin_specs())
def test_plugin_spec_yaml_round_trip(spec: PluginSpec):
    """dump_plugin_spec then load_plugin_spec is the identity on PluginSpec values."""
    reloaded = load_plugin_spec(dump_plugin_spec(spec))
    assert reloaded == spec
