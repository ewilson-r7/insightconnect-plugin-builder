"""Tests for the spec-completeness checks.

These are grounded in specs this tool actually shipped: they went out with no
``sdk`` block, no ``version_history``, no ``supported_versions``, no
``resources``, no ``example`` on any output, and a credential type that does not
exist. Each check below corresponds to one of those, or to a rule in the plugin
steering that the toolchain enforces.
"""

import pytest

from icplugin_builder.core.spec_completeness import (
    REQUIRED_TOP_LEVEL,
    VALID_CREDENTIAL_TYPES,
    Severity,
    check_completeness,
    with_sdk_version,
)
from icplugin_builder.core.spec_model import FieldSchema, PluginSpec, SemVer

from tests.toolchain import toolchain_credential_types


def complete_mapping(**overrides):
    """A spec mapping that passes every completeness check."""
    mapping = {
        "plugin_spec_version": "v2",
        "extension": "plugin",
        "products": ["insightconnect"],
        "name": "acme_widget",
        "title": "Acme Widget",
        "description": "Manage Acme widgets",
        "version": "1.0.0",
        "vendor": "rapid7",
        "support": "rapid7",
        "status": [],
        "cloud_ready": True,
        "sdk": {"type": "slim", "version": "6.6.0", "user": "nobody"},
        "supported_versions": ["Acme API 2026-01-01"],
        "key_features": ["Manage widgets"],
        "requirements": ["An Acme API key"],
        "version_history": ["1.0.0 - Initial plugin release"],
        "resources": {
            "source_url": "https://github.com/rapid7/insightconnect-plugins",
            "license_url": "https://github.com/rapid7/insightconnect-plugins/blob/master/LICENSE",
        },
        "hub_tags": {"use_cases": ["threat_detection_and_response"], "keywords": ["acme"], "features": []},
        "connection": {
            "api_key": {"title": "API Key", "type": "credential_secret_key", "required": True},
        },
        "actions": {
            "get_widget": {
                "title": "Get Widget",
                "description": "Retrieve a widget",
                "input": {"widget_id": {"title": "Widget ID", "type": "string", "required": True}},
                "output": {"widget": {"title": "Widget", "type": "object", "example": {"id": "1"}}},
            }
        },
    }
    mapping.update(overrides)
    return mapping


class TestCompleteSpec:
    def test_a_complete_spec_has_no_findings(self):
        assert check_completeness(complete_mapping()).findings == []

    def test_complete_spec_reports_complete(self):
        report = check_completeness(complete_mapping())
        assert report.is_complete
        assert "complete" in report.summary()


class TestMissingTopLevelFields:
    def test_every_required_field_is_reported_when_absent(self):
        minimal = {
            "plugin_spec_version": "v2",
            "name": "acme_widget",
            "title": "Acme Widget",
            "description": "Manage Acme widgets",
            "version": "1.0.0",
            "vendor": "rapid7",
        }
        report = check_completeness(minimal)
        reported = {finding.path for finding in report.findings}
        # This minimal shape is exactly what the structural schema accepts, and
        # it is what shipped: every one of these was missing.
        assert set(REQUIRED_TOP_LEVEL) <= reported
        assert not report.is_complete

    def test_an_empty_required_field_is_reported(self):
        report = check_completeness(complete_mapping(version_history=[]))
        assert any(f.path == "version_history" and f.code == "empty_field" for f in report.findings)

    def test_empty_status_is_accepted_because_that_is_conventional(self):
        report = check_completeness(complete_mapping(status=[]))
        assert not any(f.path == "status" for f in report.findings)

    def test_a_false_boolean_is_not_treated_as_empty(self):
        # cloud_ready: false is a real setting. A plain falsiness test would
        # report it as missing content, which would be wrong.
        report = check_completeness(complete_mapping(cloud_ready=False))
        assert not any(f.path == "cloud_ready" for f in report.findings)


class TestNestedRequirements:
    def test_missing_sdk_version_is_reported(self):
        report = check_completeness(complete_mapping(sdk={"type": "slim", "user": "nobody"}))
        assert any(f.path == "sdk.version" for f in report.findings)

    def test_missing_resource_urls_are_reported(self):
        report = check_completeness(complete_mapping(resources={"vendor_url": "https://acme.example"}))
        paths = {f.path for f in report.findings}
        assert {"resources.source_url", "resources.license_url"} <= paths


class TestOutputExamples:
    def test_output_without_an_example_is_reported(self):
        mapping = complete_mapping()
        del mapping["actions"]["get_widget"]["output"]["widget"]["example"]
        report = check_completeness(mapping)
        assert any(
            f.code == "output_missing_example" and f.path == "actions.get_widget.output.widget" for f in report.findings
        )

    def test_triggers_and_tasks_are_checked_too(self):
        mapping = complete_mapping(
            triggers={"new_alert": {"output": {"alert": {"title": "Alert", "type": "object"}}}},
            tasks={"sweep": {"output": {"count": {"title": "Count", "type": "integer"}}}},
        )
        paths = {f.path for f in check_completeness(mapping).findings}
        assert "triggers.new_alert.output.alert" in paths
        assert "tasks.sweep.output.count" in paths

    def test_inputs_are_not_required_to_have_examples(self):
        # Only outputs are checked; requiring examples on inputs would be wrong.
        paths = {f.path for f in check_completeness(complete_mapping()).findings}
        assert not any(path.endswith("input.widget_id") for path in paths)


class TestCredentialTypes:
    def test_a_nonexistent_credential_type_is_reported(self):
        """A type the platform does not define cannot bind its credential at runtime.

        The example used to be ``credential_token``, on the belief that the platform
        did not define it -- a belief inherited from the hand-written prompts this
        tool deleted. The toolchain has defined it all along, so the example is now a
        type that genuinely does not exist. The check itself is unchanged: what moved
        is which types it accepts.
        """
        mapping = complete_mapping(
            connection={"api_key": {"title": "API Key", "type": "credential_teapot", "required": True}}
        )
        report = check_completeness(mapping)
        finding = next(f for f in report.findings if f.code == "invalid_credential_type")
        assert finding.path == "connection.api_key.type"
        assert "credential_secret_key" in finding.message

    def test_valid_credential_types_pass(self):
        for valid in VALID_CREDENTIAL_TYPES:
            mapping = complete_mapping(connection={"cred": {"title": "C", "type": valid, "required": True}})
            assert not any(f.code == "invalid_credential_type" for f in check_completeness(mapping).findings)

    def test_non_credential_types_are_not_flagged(self):
        mapping = complete_mapping(connection={"base_url": {"title": "URL", "type": "string", "required": True}})
        assert not any(f.code == "invalid_credential_type" for f in check_completeness(mapping).findings)


class TestEncodingConventions:
    def test_em_dash_anywhere_is_reported(self):
        # The EncodingValidator rejects them outright.
        report = check_completeness(complete_mapping(description="Manage widgets \u2014 quickly"))
        assert any(f.code == "em_dash" and f.path == "description" for f in report.findings)

    def test_em_dash_inside_a_list_is_found(self):
        report = check_completeness(complete_mapping(requirements=["An API key \u2014 with write scope"]))
        assert any(f.code == "em_dash" for f in report.findings)

    def test_nested_double_quote_in_a_description_is_reported(self):
        # This breaks the generated schema.py with a syntax error.
        mapping = complete_mapping()
        mapping["actions"]["get_widget"]["description"] = 'Supports categories (e.g., "News")'
        report = check_completeness(mapping)
        assert any(f.code == "nested_quotes" for f in report.findings)

    def test_quotes_outside_descriptions_are_not_flagged(self):
        report = check_completeness(complete_mapping(title='Acme "Widget"'))
        assert not any(f.code == "nested_quotes" for f in report.findings)


class TestReportShape:
    def test_findings_are_deterministically_ordered(self):
        mapping = {"plugin_spec_version": "v2", "name": "x", "title": "X", "description": "d", "version": "1.0.0"}
        first = check_completeness(mapping).findings
        second = check_completeness(mapping).findings
        assert first == second
        assert [f.path for f in first] == sorted(f.path for f in first)

    def test_finding_keys_are_stable_for_round_over_round_comparison(self):
        # The repair loop compares these keys to tell a persisting problem from
        # a new one, so they must not vary between runs.
        mapping = complete_mapping(sdk={"type": "slim", "user": "nobody"})
        assert check_completeness(mapping).keys() == check_completeness(mapping).keys()
        assert "missing_field:sdk.version" in check_completeness(mapping).keys()

    def test_errors_and_warnings_are_separable(self):
        report = check_completeness(complete_mapping(sdk={"type": "slim", "user": "nobody"}))
        assert all(f.severity is Severity.ERROR for f in report.errors)
        assert set(report.errors) | set(report.warnings) == set(report.findings)

    def test_accepts_a_typed_plugin_spec(self):
        spec = PluginSpec(
            name="acme_widget",
            title="Acme Widget",
            description="Manage Acme widgets",
            version=SemVer(1, 0, 0),
            vendor="rapid7",
            connection={"api_key": FieldSchema(type="credential_secret_key", required=True, title="API Key")},
        )
        report = check_completeness(spec)
        assert any(f.path == "sdk" for f in report.findings)


class TestWithSdkVersion:
    def _spec(self, **extra):
        return PluginSpec(
            name="acme_widget",
            title="Acme Widget",
            description="Manage Acme widgets",
            version=SemVer(1, 0, 0),
            vendor="rapid7",
            extra=extra,
        )

    def test_fills_in_an_absent_sdk_block(self):
        result = with_sdk_version(self._spec(), "6.6.0")
        assert result.extra["sdk"] == {"type": "slim", "version": "6.6.0", "user": "nobody"}

    def test_does_not_overwrite_a_deliberate_pin(self):
        pinned = self._spec(sdk={"type": "full", "version": "6.1.0", "user": "root"})
        result = with_sdk_version(pinned, "6.6.0")
        assert result.extra["sdk"]["version"] == "6.1.0"
        assert result.extra["sdk"]["type"] == "full"

    def test_completes_a_partial_sdk_block(self):
        partial = self._spec(sdk={"type": "full"})
        result = with_sdk_version(partial, "6.6.0")
        assert result.extra["sdk"] == {"type": "full", "version": "6.6.0", "user": "nobody"}

    def test_does_not_mutate_the_input(self):
        original = self._spec()
        with_sdk_version(original, "6.6.0")
        assert "sdk" not in original.extra

    def test_result_satisfies_the_sdk_completeness_checks(self):
        result = with_sdk_version(self._spec(), "6.6.0")
        paths = {f.path for f in check_completeness(result).findings}
        assert not any(path.startswith("sdk.") for path in paths)


class TestCredentialTypesComeFromTheToolchain:
    """The valid set is the installed toolchain's, not this repository's taste.

    ``VALID_CREDENTIAL_TYPES`` listed three types and offered ``credential_token``
    as its example of one "the platform does not define". The toolchain has defined
    it all along, so a spec the toolchain would accept was reported as a defect --
    one of the sixteen findings a real run raised against a plugin whose every
    endpoint had been verified by hand.
    """

    def test_the_valid_set_matches_the_installed_schema(self):
        """Property 74: the tuple is cross-checked, so it cannot drift again in silence.

        Skipped only when no interpreter available to the test has the toolchain --
        including the one this tool *resolves* for it, which is the case that makes a
        naive import check useless here.
        """
        types = toolchain_credential_types()
        if types is None:
            pytest.skip(
                "no interpreter available to this test has insight_plugin installed, so the toolchain's "
                "own credential schema cannot be read; a hardcoded expectation here would be the very "
                "thing this cross-check exists to catch"
            )
        toolchain = tuple(sorted(types))
        missing = tuple(name for name in toolchain if name not in VALID_CREDENTIAL_TYPES)
        invented = tuple(name for name in VALID_CREDENTIAL_TYPES if name not in toolchain)
        assert not missing and not invented, (
            f"the toolchain defines {toolchain} and this repository accepts "
            f"{tuple(sorted(VALID_CREDENTIAL_TYPES))}. Defined but rejected: {missing}. Accepted but not "
            f"defined: {invented}"
        )

    def test_credential_token_reports_no_finding(self):
        report = check_completeness(complete_mapping(connection={"api_key": {"type": "credential_token"}}))
        assert not [finding for finding in report.findings if finding.code == "invalid_credential_type"]

    def test_an_invented_credential_type_is_still_reported(self):
        """The check still bites: widening the set is not the same as removing it."""
        report = check_completeness(complete_mapping(connection={"api_key": {"type": "credential_teapot"}}))
        invalid = [finding for finding in report.findings if finding.code == "invalid_credential_type"]
        assert invalid, report.findings
        assert "credential_teapot" in invalid[0].message

    def test_every_accepted_type_reports_no_finding(self):
        for name in VALID_CREDENTIAL_TYPES:
            report = check_completeness(complete_mapping(connection={"api_key": {"type": name}}))
            assert not [
                finding for finding in report.findings if finding.code == "invalid_credential_type"
            ], f"{name} is in the accepted set yet reports a finding"
