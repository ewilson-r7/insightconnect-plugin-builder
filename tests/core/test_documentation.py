"""Unit tests for the Documentation_Generator (task 5.3; Req 6.1-6.5).

These cover specific examples and edge cases: the four distinct sections, field
tables carrying name/type/required-optional, the title/description/version/vendor
metadata, empty-category placeholders, and the missing-metadata abort that leaves
an existing ``help.md`` unchanged. The universal completeness property is covered
separately by the property test (task 5.4, Property 14).
"""

import pytest

from icplugin_builder.core.documentation import (
    MissingMetadataError,
    generate_help,
    write_help,
)
from icplugin_builder.core.spec_model import (
    Component,
    FieldSchema,
    PluginSpec,
    SemVer,
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
            "cleanup": Component(title="Cleanup", input={}, output={}),
        },
    )


class TestGenerateHelp:
    def test_includes_all_four_distinct_sections(self):
        # Req 6.1: distinct connection/actions/triggers/tasks sections.
        doc = generate_help(_full_spec())
        assert "## Connection" in doc
        assert "## Actions" in doc
        assert "## Triggers" in doc
        assert "## Tasks" in doc

    def test_includes_title_description_version_vendor(self):
        # Req 6.3: title, description, version, vendor.
        doc = generate_help(_full_spec())
        assert "# Example Plugin" in doc
        assert "Does example things." in doc
        assert "2.3.4" in doc
        assert "rapid7_custom" in doc

    def test_action_fields_include_name_type_required(self):
        # Req 6.2: input+output fields with name, data type, required/optional.
        doc = generate_help(_full_spec())
        assert "query" in doc
        assert "string" in doc
        assert "Optional" in doc
        assert "things" in doc
        assert "[]string" in doc
        assert "Required" in doc

    def test_trigger_fields_rendered(self):
        # Req 6.2 applies to triggers too.
        doc = generate_help(_full_spec())
        assert "interval" in doc
        assert "integer" in doc

    def test_empty_categories_render_heading_and_placeholder(self):
        # Req 6.4: empty category -> heading + placeholder, not omitted.
        spec = PluginSpec(
            name="empty",
            title="Empty",
            description="No components.",
            version=SemVer(1, 0, 0),
            vendor="acme_custom",
        )
        doc = generate_help(spec)
        assert "## Connection" in doc
        assert "## Actions" in doc
        assert "## Triggers" in doc
        assert "## Tasks" in doc
        assert "does not define a connection" in doc
        assert "does not define any actions" in doc
        assert "does not define any triggers" in doc
        assert "does not define any tasks" in doc

    def test_component_with_no_fields_renders_input_output_placeholders(self):
        doc = generate_help(_full_spec())
        # The cleanup task has no input/output fields.
        assert "does not define any input fields" in doc
        assert "does not define any output fields" in doc

    def test_pipe_in_description_is_escaped(self):
        spec = PluginSpec(
            name="p",
            title="P",
            description="A | B",
            version=SemVer(1, 0, 0),
            vendor="v_custom",
            actions={
                "a": Component(
                    input={"f": FieldSchema(type="string", description="pipe | here")},
                    output={},
                )
            },
        )
        doc = generate_help(spec)
        assert "pipe \\| here" in doc


class TestMissingMetadata:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("title", ""),
            ("description", "   "),
            ("vendor", ""),
        ],
    )
    def test_missing_string_metadata_aborts(self, field, value):
        # Req 6.5: absent required metadata aborts with an error naming the field.
        spec = _full_spec()
        setattr(spec, field, value)
        with pytest.raises(MissingMetadataError) as exc:
            generate_help(spec)
        assert field in exc.value.missing_fields
        assert field in str(exc.value)

    def test_multiple_missing_fields_all_reported(self):
        spec = _full_spec()
        spec.title = ""
        spec.vendor = "  "
        with pytest.raises(MissingMetadataError) as exc:
            generate_help(spec)
        assert "title" in exc.value.missing_fields
        assert "vendor" in exc.value.missing_fields

    def test_write_help_leaves_existing_file_unchanged_on_abort(self, tmp_path):
        # Req 6.5: aborting generation leaves any existing help.md unchanged.
        help_path = tmp_path / "help.md"
        original = "# Existing help\n\nDo not overwrite.\n"
        help_path.write_text(original, encoding="utf-8")

        spec = _full_spec()
        spec.title = ""  # force abort
        with pytest.raises(MissingMetadataError):
            write_help(spec, str(help_path))

        assert help_path.read_text(encoding="utf-8") == original

    def test_write_help_writes_content_on_success(self, tmp_path):
        help_path = tmp_path / "help.md"
        write_help(_full_spec(), str(help_path))
        content = help_path.read_text(encoding="utf-8")
        assert content.startswith("# Example Plugin")
        assert "## Connection" in content
