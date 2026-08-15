"""Tests for pre-build readiness: SDK version resolution and toolchain checks.

The SDK version must come from the SDK's own changelog rather than being
hardcoded, because that file is updated with every release. The parsing tests
therefore focus on not picking up a version-shaped string from somewhere else in
the README, and on distinguishing "latest released" from "whatever is installed
here".
"""

import asyncio

from icplugin_builder.integrations import build_prep as bp
from icplugin_builder.integrations.build_prep import (
    REQUIRED_TOOLS,
    SDK_SOURCE_CHANGELOG,
    SDK_SOURCE_INSTALLED,
    BuildPrepReport,
    SdkVersion,
    ToolStatus,
    check_tooling,
    parse_sdk_changelog_version,
    resolve_sdk_version,
)

README = """\
# InsightConnect Python Plugin Runtime

Install with `pip install insightconnect-plugin-runtime==1.0.0`.

## Changelog

* 6.6.0 - Disable OpenSSL post-quantum hybrid key exchange
* 6.5.1 - Updated core dependencies to latest versions
* 6.5.0 - Implement stateful triggers

## Contributing
"""


class TestParseChangelog:
    def test_takes_the_top_entry_because_the_changelog_is_newest_first(self):
        assert parse_sdk_changelog_version(README) == "6.6.0"

    def test_ignores_a_version_outside_the_changelog_section(self):
        # The install example above the changelog mentions 1.0.0; picking that up
        # would silently build every plugin against an ancient SDK.
        assert parse_sdk_changelog_version(README) != "1.0.0"

    def test_stops_at_the_next_heading(self):
        text = "## Changelog\n\n## Contributing\n\n* 9.9.9 - not a release\n"
        assert parse_sdk_changelog_version(text) is None

    def test_returns_none_without_a_changelog_section(self):
        assert parse_sdk_changelog_version("# Title\n\nNo changelog here.\n") is None

    def test_accepts_a_leading_v_and_hyphen_bullets(self):
        assert parse_sdk_changelog_version("## Changelog\n- v7.0.1 - something\n") == "7.0.1"

    def test_tolerates_a_different_heading_level(self):
        assert parse_sdk_changelog_version("# Changelog\n* 2.3.4 - x\n") == "2.3.4"

    def test_skips_blank_and_prose_lines_before_the_first_bullet(self):
        text = "## Changelog\n\nSome preamble prose.\n\n* 5.1.2 - real entry\n"
        assert parse_sdk_changelog_version(text) == "5.1.2"


class TestResolveSdkVersion:
    def test_prefers_the_changelog_and_labels_it(self, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text(README, encoding="utf-8")
        resolved = resolve_sdk_version(readme)
        assert resolved.version == "6.6.0"
        assert resolved.source == SDK_SOURCE_CHANGELOG
        assert resolved.is_latest_known
        assert str(readme) in resolved.detail

    def test_falls_back_to_the_installed_distribution_and_says_so(self, tmp_path, monkeypatch):
        # The SDK runtime is deliberately not a dependency of this tool -- it
        # builds plugins rather than running them -- so the fallback is exercised
        # with the lookup stubbed. It must be labelled as possibly lagging.
        monkeypatch.setattr(bp, "_installed_sdk_version", lambda: "6.4.4")
        resolved = resolve_sdk_version(tmp_path / "absent" / "README.md")
        assert resolved.version == "6.4.4"
        assert resolved.source == SDK_SOURCE_INSTALLED
        assert not resolved.is_latest_known
        assert "may lag" in resolved.detail

    def test_unresolvable_when_neither_source_is_available(self, tmp_path):
        # No SDK checkout and the runtime not installed: report unresolved with a
        # reason rather than inventing a version.
        resolved = resolve_sdk_version(tmp_path / "absent" / "README.md")
        assert not resolved.resolved
        assert resolved.version is None
        assert "not found" in resolved.detail

    def test_reports_a_readme_without_a_changelog(self, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text("# Title\n\nNothing here.\n", encoding="utf-8")
        resolved = resolve_sdk_version(readme)
        assert "no version bullet" in resolved.detail

    def test_the_real_sdk_checkout_resolves_when_present(self):
        # Not a hardcoded expectation: assert only that if the conventional
        # checkout exists, the changelog path is what gets used.
        from pathlib import Path

        from icplugin_builder.integrations.build_prep import DEFAULT_SDK_README

        if not Path(DEFAULT_SDK_README).expanduser().is_file():
            return
        resolved = resolve_sdk_version()
        assert resolved.source == SDK_SOURCE_CHANGELOG
        assert resolved.version and resolved.version.count(".") == 2

    def test_expands_a_user_relative_path(self):
        # Should not raise on a ~ path that does not exist.
        assert isinstance(resolve_sdk_version("~/definitely/not/here/README.md"), SdkVersion)


class TestCheckTooling:
    def test_reports_a_status_for_every_required_tool(self):
        statuses = asyncio.run(check_tooling())
        assert set(statuses) == set(REQUIRED_TOOLS)
        assert all(isinstance(status, ToolStatus) for status in statuses.values())

    def test_a_missing_tool_is_reported_absent_without_raising(self):
        statuses = asyncio.run(check_tooling(["definitely-not-a-real-binary-xyz"]))
        status = statuses["definitely-not-a-real-binary-xyz"]
        assert not status.present
        assert status.path is None

    def test_a_present_tool_carries_its_path(self):
        statuses = asyncio.run(check_tooling(["python3"]))
        assert statuses["python3"].present
        assert statuses["python3"].path


class TestBuildPrepReport:
    def test_missing_tools_are_listed_in_declaration_order(self):
        tools = {
            name: ToolStatus(name=name, path=None if name in ("prospector", "docker") else f"/usr/bin/{name}")
            for name in REQUIRED_TOOLS
        }
        report = BuildPrepReport(sdk=SdkVersion(version="6.6.0", source=SDK_SOURCE_CHANGELOG), tools=tools)
        assert report.missing_tools == ("prospector", "docker")
        assert not report.ready

    def test_ready_requires_both_an_sdk_version_and_every_tool(self):
        all_present = {name: ToolStatus(name=name, path=f"/usr/bin/{name}") for name in REQUIRED_TOOLS}
        assert BuildPrepReport(sdk=SdkVersion(version="6.6.0"), tools=all_present).ready
        assert not BuildPrepReport(sdk=SdkVersion(version=None), tools=all_present).ready

    def test_summary_names_what_is_wrong(self):
        report = BuildPrepReport(
            sdk=SdkVersion(version=None),
            tools={name: ToolStatus(name=name, path=None) for name in REQUIRED_TOOLS},
        )
        summary = report.summary()
        assert "unresolved" in summary
        assert "missing" in summary
