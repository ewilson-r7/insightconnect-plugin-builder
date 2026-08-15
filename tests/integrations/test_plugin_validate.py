"""Tests for running the toolchain's validators minus the ones a fork cannot satisfy.

The behaviour under test is not "a check was skipped". It is that
`insight-plugin validate` invoked plainly *crashes* on any plugin outside a
plugins-repo clone -- and because the crash is an unhandled exception mid-loop, it
skips every validator after it and never prints the failures it had already
collected. The tool reported a stack trace where eleven real defects were waiting.

So the exclusion is not a relaxation of the bar; it is what makes the bar
measurable at all. These tests hold the exclusion list honest: every entry carries
a reason, and the list stays as small as the evidence supports.
"""

import json

from icplugin_builder.integrations.plugin_validate import (
    EXCLUDED_VALIDATORS,
    VALIDATE_DRIVER,
    excluded_validator_names,
    validate_command,
)


class TestExclusionList:
    def test_every_exclusion_carries_a_reason(self):
        # An exclusion is a claim that a check *cannot* be satisfied outside the
        # plugins repository. Unexplained entries are how a list like this decays
        # into "whatever was complaining".
        assert EXCLUDED_VALIDATORS
        for name, reason in EXCLUDED_VALIDATORS.items():
            assert name.strip(), name
            assert len(reason) > 60, f"{name} needs a real justification, got {reason!r}"

    def test_the_repo_dependent_validator_is_excluded(self):
        # The only validator in the installed set with a hard dependency on the
        # plugins repository: it opens a git repo and splits the path on /plugins/.
        assert "Version Increment Validator" in EXCLUDED_VALIDATORS

    def test_the_reason_names_the_dependency(self):
        reason = EXCLUDED_VALIDATORS["Version Increment Validator"]
        assert "git" in reason
        assert "/plugins/" in reason

    def test_the_list_stays_small(self):
        # A growing list means the bar is quietly dropping. Anything added here
        # should have to argue for itself in review.
        assert len(EXCLUDED_VALIDATORS) == 1

    def test_version_arithmetic_is_covered_elsewhere(self):
        # Excluding the version validator is only defensible because this tool
        # does the same arithmetic itself, against the registry's export history.
        from icplugin_builder.core.version_bump import bump_for_export

        assert callable(bump_for_export)

    def test_names_are_sorted_for_a_deterministic_command(self):
        assert excluded_validator_names(["b", "a"]) == ("a", "b")

    def test_an_explicit_set_overrides_the_default(self):
        assert excluded_validator_names(["Only This"]) == ("Only This",)


class TestValidateCommand:
    def test_it_runs_under_the_given_interpreter(self):
        # icon_validator ships with the plugin toolchain, not with this tool, so
        # running the driver under this process's interpreter would fail on import.
        command = validate_command("/plugins/acme", python_executable="/opt/py/bin/python")
        assert command[0] == "/opt/py/bin/python"
        assert command[1] == "-c"

    def test_it_passes_the_project_directory(self):
        command = validate_command("/tmp/acme")
        assert "/tmp/acme" in command

    def test_it_passes_the_exclusions_as_json(self):
        command = validate_command("/tmp/acme")
        assert json.loads(command[-1]) == ["Version Increment Validator"]

    def test_the_driver_filters_by_validator_name(self):
        assert "v.name not in excluded" in VALIDATE_DRIVER

    def test_the_driver_rebuilds_the_validator_list(self):
        # icon_validator.validate does `validators += JENKINS_VALIDATORS` on the
        # module-level VALIDATORS, mutating it in place, so its contents depend on
        # whether anything called validate earlier in the process.
        assert "list(VALIDATORS) + list(JENKINS_VALIDATORS)" in VALIDATE_DRIVER

    def test_the_driver_delegates_the_verdict_to_the_toolchain(self):
        # The pass/fail decision is icon_validator's, not ours: we choose which
        # validators run, and it decides whether they passed.
        assert "status = validate(" in VALIDATE_DRIVER
        assert "raise SystemExit(status)" in VALIDATE_DRIVER

    def test_the_driver_reports_a_missing_toolchain_distinctly(self):
        # Exit 3, not 1: "the validators are not installed here" is a different
        # thing from "the plugin failed validation".
        assert "SystemExit(3)" in VALIDATE_DRIVER
        assert "icon_validator is not importable" in VALIDATE_DRIVER

    def test_the_driver_compiles(self):
        # It is shipped as source and executed by another interpreter, so a syntax
        # error here would only ever surface as a confusing stage failure.
        compile(VALIDATE_DRIVER, "<validate-driver>", "exec")
