"""Unit tests for the Spec_Validator (task 5.1).

These cover specific examples and edge cases for Req 7.1, 7.2, 7.3, 7.5, 7.6.
The universal error-completeness property across generated inputs is covered
separately by the property test (task 5.2 / design Property 16).
"""

from icplugin_builder.core.spec_model import Component, FieldSchema, PluginSpec, SemVer
from icplugin_builder.core.spec_validator import (
    SpecValidator,
    ValidationReport,
    validate_spec,
)


def _valid_mapping(**overrides):
    """A minimal, schema-valid plugin.spec.yaml mapping."""
    base = {
        "plugin_spec_version": "v2",
        "name": "example_plugin",
        "title": "Example Plugin",
        "description": "An example plugin.",
        "version": "1.0.0",
        "vendor": "rapid7_custom",
        "connection": {"url": {"type": "string", "required": True}},
        "actions": {
            "run": {
                "title": "Run",
                "input": {"host": {"type": "string", "required": True}},
                "output": {"result": {"type": "string"}},
            }
        },
    }
    base.update(overrides)
    return base


class TestSuccess:
    def test_valid_mapping_reports_success(self):
        report = validate_spec(_valid_mapping())
        assert report.is_valid is True
        assert report.errors == []
        assert "valid" in report.summary().lower()

    def test_valid_typed_plugin_spec(self):
        spec = PluginSpec(
            name="example_plugin",
            title="Example",
            description="desc",
            version=SemVer(2, 3, 4),
            vendor="acme_custom",
            connection={"url": FieldSchema(type="string", required=True)},
            actions={"run": Component(title="Run", input={"host": FieldSchema(type="string")})},
        )
        report = validate_spec(spec)
        assert report.is_valid is True

    def test_completes_within_time_budget(self):
        # Req 7.1: validation completes well within the 5s budget.
        report = validate_spec(_valid_mapping())
        assert report.duration_seconds < 5.0


class TestSemanticVersion:
    def test_invalid_version_reported_with_expected_format(self):
        report = validate_spec(_valid_mapping(version="1.0"))
        assert report.is_valid is False
        version_errors = [e for e in report.errors if e.path == "version"]
        assert len(version_errors) == 1
        assert "MAJOR.MINOR.PATCH" in version_errors[0].message

    def test_non_semver_string_rejected(self):
        report = validate_spec(_valid_mapping(version="not-a-version"))
        assert any(e.path == "version" for e in report.errors)

    def test_leading_zero_rejected(self):
        report = validate_spec(_valid_mapping(version="01.0.0"))
        assert any(e.path == "version" for e in report.errors)

    def test_non_string_version_reports_type_error_not_semver_duplicate(self):
        report = validate_spec(_valid_mapping(version=100))
        version_errors = [e for e in report.errors if e.path == "version"]
        # Only the schema type error, not an additional semver-format message.
        assert len(version_errors) == 1
        assert "MAJOR.MINOR.PATCH" not in version_errors[0].message


class TestSchemaViolations:
    def test_missing_required_top_level_fields_each_reported(self):
        mapping = _valid_mapping()
        del mapping["title"]
        del mapping["vendor"]
        report = validate_spec(mapping)
        assert report.is_valid is False
        messages = " ".join(e.message for e in report.errors)
        assert "title" in messages
        assert "vendor" in messages

    def test_every_violation_reported(self):
        # Two independent violations should both appear (Req 7.2).
        mapping = _valid_mapping(name="Bad Name", vendor="")
        report = validate_spec(mapping)
        assert len(report.errors) >= 2

    def test_field_path_points_to_offending_field(self):
        mapping = _valid_mapping()
        # Remove the required "type" from an action input field.
        del mapping["actions"]["run"]["input"]["host"]["type"]
        report = validate_spec(mapping)
        assert report.is_valid is False
        assert any("actions.run.input.host" in e.path for e in report.errors)

    def test_invalid_name_pattern_reported(self):
        report = validate_spec(_valid_mapping(name="Not-Snake-Case"))
        assert any(e.path == "name" for e in report.errors)

    def test_action_not_object_reported(self):
        mapping = _valid_mapping(actions={"run": "should-be-an-object"})
        report = validate_spec(mapping)
        assert any(e.path.startswith("actions.run") for e in report.errors)

    def test_empty_document_reports_all_missing_fields(self):
        report = validate_spec({})
        assert report.is_valid is False
        # All six required top-level keys should be flagged.
        assert len(report.errors) >= 6


class TestReport:
    def test_summary_mentions_error_count(self):
        report = validate_spec(_valid_mapping(version="bad"))
        assert "1" in report.summary()

    def test_errors_are_sorted_deterministically(self):
        mapping = _valid_mapping()
        del mapping["title"]
        del mapping["description"]
        report = validate_spec(mapping)
        paths = [e.path for e in report.errors]
        assert paths == sorted(paths)

    def test_validator_instance_reusable(self):
        validator = SpecValidator()
        first = validator.validate(_valid_mapping())
        second = validator.validate(_valid_mapping(version="bad"))
        assert isinstance(first, ValidationReport)
        assert first.is_valid is True
        assert second.is_valid is False
