"""Property-based test for the Documentation_Generator (task 5.4).

Covers design Property 14 with Hypothesis: across arbitrary ``PluginSpec`` trees
carrying the required metadata, the generated ``help.md`` always contains a
distinct section for connection/actions/triggers/tasks, renders every action and
trigger input+output field with its name, data type, and required-or-optional
status, surfaces the plugin's title/description/version/vendor, and renders a
placeholder for any empty component category rather than omitting the section.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.documentation import generate_help
from icplugin_builder.core.spec_model import FieldSchema, PluginSpec

from tests.strategies import plugin_specs, semvers


def _nonempty_text() -> st.SearchStrategy[str]:
    """Generate round-trip-safe, non-empty text for required metadata fields.

    Restricted to printable ASCII and guaranteed non-empty after stripping so it
    satisfies the Documentation_Generator's required-metadata precondition.
    """
    return (
        st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=1, max_size=40)
        .map(str.strip)
        .filter(lambda value: value != "")
    )


@st.composite
def _empty_specs(draw: st.DrawFn) -> PluginSpec:
    """Generate a spec with no connection/actions/triggers/tasks at all.

    Mixed into the main generator so the empty-category branch of Property 14
    (Req 6.4) is exercised even though the general generator only sometimes
    empties every category at once.
    """
    return PluginSpec(
        name="empty_plugin",
        title=draw(_nonempty_text()),
        description=draw(_nonempty_text()),
        version=draw(semvers()),
        vendor="acme_custom",
    )


@st.composite
def specs_with_required_metadata(draw: st.DrawFn) -> PluginSpec:
    """Generate a structurally valid spec that carries all required metadata.

    ``version`` is always a :class:`SemVer` and ``vendor`` from the shared
    strategy is a non-empty snake_case name; here we additionally force a
    non-empty ``title`` and ``description`` so ``generate_help`` never aborts on
    missing metadata (that abort path is covered by the unit tests). Fully-empty
    specs are mixed in to guarantee the empty-category placeholder branch fires.
    """
    if draw(st.booleans()):
        return draw(_empty_specs())
    spec = draw(plugin_specs())
    spec.title = draw(_nonempty_text())
    spec.description = draw(_nonempty_text())
    if not spec.vendor.strip():
        spec.vendor = "acme"
    return spec


def _field_row_prefix(name: str, schema: FieldSchema) -> str:
    """Return the leading ``|name|type|Required-or-Optional|`` of a field's row.

    Field names are snake_case and types come from a fixed vocabulary, so
    neither needs the table-cell escaping the generator applies; the prefix is
    therefore an exact substring of the rendered row when the field is present.
    """
    required = "Required" if schema.required else "Optional"
    return f"|{name}|{schema.type}|{required}|"


def _assert_component_fields_rendered(doc: str, components: dict) -> None:
    """Assert every input and output field of each component is rendered (Req 6.2)."""
    for component in components.values():
        for name, schema in component.input.items():
            assert _field_row_prefix(name, schema) in doc
        for name, schema in component.output.items():
            assert _field_row_prefix(name, schema) in doc


# Feature: insightconnect-plugin-builder, Property 14: help.md completeness
@settings(max_examples=200)
@given(spec=specs_with_required_metadata())
def test_help_md_is_complete(spec: PluginSpec):
    """The generated help.md carries all required sections, fields, and metadata.

    **Validates: Requirements 6.1, 6.2, 6.3, 6.4**
    """
    doc = generate_help(spec)

    # Req 6.1: a distinct section for connection, actions, triggers, and tasks.
    assert "## Connection" in doc
    assert "## Actions" in doc
    assert "## Triggers" in doc
    assert "## Tasks" in doc

    # Req 6.3: the plugin's title, description, version, and vendor.
    assert f"# {spec.title.strip()}" in doc
    assert spec.description.strip() in doc
    assert str(spec.version) in doc
    assert spec.vendor.strip() in doc

    # Req 6.2: every action and trigger input+output field with name/type/status.
    _assert_component_fields_rendered(doc, spec.actions)
    _assert_component_fields_rendered(doc, spec.triggers)

    # Req 6.4: an empty category renders its heading followed by a placeholder
    # rather than being omitted.
    if not spec.connection:
        assert "does not define a connection" in doc
    if not spec.actions:
        assert "does not define any actions" in doc
    if not spec.triggers:
        assert "does not define any triggers" in doc
    if not spec.tasks:
        assert "does not define any tasks" in doc
