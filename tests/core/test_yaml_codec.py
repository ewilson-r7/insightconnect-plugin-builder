"""Unit tests for the ruamel-backed YAML codec (task 1.2).

Covers both the raw round-trip document path (comments/ordering survive) and
the typed PluginSpec codec.
"""

from icplugin_builder.core import spec_model
from icplugin_builder.core.yaml_codec import (
    dump_document,
    dump_plugin_spec,
    load_document,
    load_plugin_spec,
)

SAMPLE_SPEC = """\
plugin_spec_version: v2
name: example
title: Example
description: An example plugin
version: 1.2.3
vendor: rapid7
# keep this comment
connection:
  url:
    title: URL
    description: Base URL
    type: string
    required: true
actions:
  run:
    title: Run
    input:
      x:
        title: X
        type: integer
        required: true
    output:
      y:
        title: Y
        type: string
        required: false
"""


def test_raw_document_round_trip_preserves_comments_and_order():
    document = load_document(SAMPLE_SPEC)
    rendered = dump_document(document)
    # The raw round trip is byte-stable: comments and key ordering survive.
    assert rendered == SAMPLE_SPEC
    assert "# keep this comment" in rendered


def test_raw_document_edit_preserves_surrounding_comments():
    document = load_document(SAMPLE_SPEC)
    document["version"] = "1.2.4"
    rendered = dump_document(document)
    assert "version: 1.2.4" in rendered
    assert "# keep this comment" in rendered


def test_load_plugin_spec_builds_typed_tree():
    spec = load_plugin_spec(SAMPLE_SPEC)
    assert spec.name == "example"
    assert spec.version == spec_model.SemVer(1, 2, 3)
    assert spec.actions["run"].input["x"].type == "integer"


def test_typed_codec_round_trip_equivalence():
    spec = load_plugin_spec(SAMPLE_SPEC)
    reloaded = load_plugin_spec(dump_plugin_spec(spec))
    assert reloaded == spec


def test_dump_plugin_spec_is_parseable_yaml():
    spec = load_plugin_spec(SAMPLE_SPEC)
    text = dump_plugin_spec(spec)
    # Dumped output must itself load as a mapping with the core fields.
    document = load_document(text)
    assert document["name"] == "example"
    assert document["version"] == "1.2.3"
