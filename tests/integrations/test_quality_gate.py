"""Tests for the quality gate.

The exclusion of generated files is the load-bearing part and is tested hardest:
`insight-plugin` emits `schema.py` files that prospector flags and the steering
forbids editing, so a gate that reported them would ask for a fix that must not
be made and the repair loop could never converge.

The compile check is exercised against real files rather than mocked, because a
file that does not parse is the defect this tool has actually shipped.
"""

import asyncio

from icplugin_builder.integrations.build_prep import (
    LINT_PROFILE_SOURCE_FALLBACK,
    LINT_TOOLS,
    PLUGIN_LINE_LENGTH,
    LintProfile,
)
from icplugin_builder.core.plugin_files import (
    hand_written_python,
    is_generated,
    is_lint_excluded,
)
from icplugin_builder.integrations.quality_gate import (
    SOURCE_COMPILE,
    SOURCE_COVERAGE,
    SOURCE_FORMAT,
    SOURCE_PROSPECTOR,
    SOURCE_TESTS,
    CodeFinding,
    QualityGate,
    QualityReport,
)


class TestGeneratedFileExclusion:
    def test_generated_files_are_excluded_by_name(self):
        for path in (
            "icon_x/schema.py",
            "icon_x/actions/a/schema.py",
            "icon_x/__init__.py",
            "setup.py",
            "Dockerfile",
            "Makefile",
            "help.md",
            ".CHECKSUM",
        ):
            assert is_generated(path), path

    def test_generated_directories_are_excluded_wholesale(self):
        for path in ("bin/icon_x", ".builder/project.json", "build/lib/x.py", "__pycache__/x.pyc"):
            assert is_generated(path), path

    def test_hand_written_code_is_not_excluded(self):
        for path in (
            "icon_x/actions/get_thing/action.py",
            "icon_x/connection/connection.py",
            "icon_x/util/api.py",
            "icon_x/util/constants.py",
            "unit_test/test_get_thing.py",
        ):
            assert not is_generated(path), path

    def test_schema_py_is_excluded_even_though_prospector_flags_it(self):
        # insight-plugin's own template emits `super(self.__class__, self)`, which
        # prospector reports as bad-super-call. It cannot be fixed, so counting it
        # would make convergence impossible.
        assert is_generated("icon_x/actions/a/schema.py")


class TestHandWrittenDiscovery:
    def test_lists_only_hand_written_python_sorted(self, tmp_path):
        (tmp_path / "icon_x" / "actions" / "a").mkdir(parents=True)
        (tmp_path / "icon_x" / "actions" / "a" / "action.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "icon_x" / "actions" / "a" / "schema.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "icon_x" / "__init__.py").write_text("", encoding="utf-8")
        (tmp_path / "setup.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "unit_test").mkdir()
        (tmp_path / "unit_test" / "test_a.py").write_text("x = 1\n", encoding="utf-8")

        assert hand_written_python(tmp_path) == (
            "icon_x/actions/a/action.py",
            "unit_test/test_a.py",
        )

    def test_missing_directory_yields_nothing(self, tmp_path):
        assert hand_written_python(tmp_path / "absent") == ()


class TestCompileCheck:
    def _plugin(self, tmp_path, body):
        package = tmp_path / "icon_x" / "actions" / "a"
        package.mkdir(parents=True)
        (package / "action.py").write_text(body, encoding="utf-8")
        return tmp_path

    def test_reports_a_syntax_error_with_its_line(self, tmp_path):
        # Mirrors the real defect: correct code followed by an over-indented block.
        root = self._plugin(
            tmp_path,
            "def run():\n    x = 1\n        y = 2\n    return x\n",
        )
        report = asyncio.run(QualityGate().run(root))
        compile_findings = report.by_source(SOURCE_COMPILE)
        assert len(compile_findings) == 1
        assert compile_findings[0].path == "icon_x/actions/a/action.py"
        assert compile_findings[0].line == 3
        assert compile_findings[0].code == "syntax-error"

    def test_a_parsing_file_produces_no_compile_finding(self, tmp_path):
        root = self._plugin(tmp_path, "def run():\n    return 1\n")
        report = asyncio.run(QualityGate().run(root))
        assert report.by_source(SOURCE_COMPILE) == ()

    def test_a_broken_generated_file_is_not_reported(self, tmp_path):
        package = tmp_path / "icon_x"
        package.mkdir(parents=True)
        (package / "schema.py").write_text("def broken(\n", encoding="utf-8")
        report = asyncio.run(QualityGate().run(tmp_path))
        assert report.by_source(SOURCE_COMPILE) == ()


class TestTestsAndCoverage:
    """Unit test failures and thin coverage are findings, not a pass/fail verdict.

    This is what makes them repairable. Previously a stubbed or failing test only
    surfaced in the containerized test stage at export time, after the loop that
    could have fixed it had already finished.
    """

    def _plugin(self, tmp_path, *, test_body, module_body="def add(a, b):\n    return a + b\n"):
        (tmp_path / "icon_x").mkdir()
        (tmp_path / "icon_x" / "api.py").write_text(module_body, encoding="utf-8")
        (tmp_path / "unit_test").mkdir()
        (tmp_path / "unit_test" / "test_api.py").write_text(test_body, encoding="utf-8")
        return tmp_path

    def _gate(self, **kwargs):
        # This interpreter suffices for the failure cases: the fixtures import
        # nothing from the SDK. It has no pytest-cov, so coverage is skipped --
        # which the gate must handle without breaking the test run.
        import sys

        kwargs.setdefault("python_executable", sys.executable)
        kwargs.setdefault("coverage_threshold", 80.0)
        return QualityGate(**kwargs)

    def _coverage_gate(self):
        """A gate on an interpreter that has pytest-cov, or ``None``."""
        from icplugin_builder.integrations.build_prep import resolve_target_python

        target = resolve_target_python()
        if not target.resolved:
            return None
        return QualityGate(python_executable=target.executable, coverage_threshold=80.0)

    def test_a_missing_unit_test_directory_is_a_finding(self, tmp_path):
        (tmp_path / "icon_x").mkdir()
        (tmp_path / "icon_x" / "api.py").write_text("x = 1\n", encoding="utf-8")
        report = asyncio.run(self._gate().run(tmp_path))
        assert any(f.code == "no-tests" for f in report.by_source(SOURCE_TESTS))

    def test_a_failing_test_is_reported_with_its_file(self, tmp_path):
        root = self._plugin(
            tmp_path,
            test_body="from icon_x.api import add\n\ndef test_bad():\n    assert add(1, 2) == 4\n",
        )
        report = asyncio.run(self._gate().run(root))
        failures = report.by_source(SOURCE_TESTS)
        assert len(failures) == 1
        assert failures[0].path == "unit_test/test_api.py"
        assert "test_bad" in failures[0].code

    def test_two_failures_in_one_file_stay_distinct_findings(self, tmp_path):
        # If they collapsed to one key, fixing one of two would look like
        # resolving nothing and the repair loop would call a premature stall.
        root = self._plugin(
            tmp_path,
            test_body=(
                "from icon_x.api import add\n\n"
                "def test_one():\n    assert add(1, 2) == 4\n\n"
                "def test_two():\n    assert add(2, 2) == 5\n"
            ),
        )
        report = asyncio.run(self._gate().run(root))
        keys = {f.key for f in report.by_source(SOURCE_TESTS)}
        assert len(keys) == 2

    def test_passing_tests_with_good_coverage_produce_nothing(self, tmp_path):
        root = self._plugin(
            tmp_path,
            test_body="from icon_x.api import add\n\ndef test_ok():\n    assert add(1, 2) == 3\n",
        )
        report = asyncio.run(self._gate().run(root))
        assert report.by_source(SOURCE_TESTS) == ()
        assert report.by_source(SOURCE_COVERAGE) == ()

    def test_coverage_below_the_threshold_is_a_finding(self, tmp_path):
        gate = self._coverage_gate()
        if gate is None:  # pragma: no cover - depends on the local toolchain
            return
        root = self._plugin(
            tmp_path,
            module_body=(
                "def add(a, b):\n    return a + b\n\n"
                "def untested_one(x):\n    return x * 2\n\n"
                "def untested_two(x):\n    return x * 3\n\n"
                "def untested_three(x):\n    return x * 4\n"
            ),
            test_body="from icon_x.api import add\n\ndef test_ok():\n    assert add(1, 2) == 3\n",
        )
        report = asyncio.run(gate.run(root))
        coverage = report.by_source(SOURCE_COVERAGE)
        assert len(coverage) == 1
        assert coverage[0].code == "below-threshold"
        assert "80%" in coverage[0].message

    def test_absent_pytest_cov_skips_coverage_without_breaking_the_test_run(self, tmp_path):
        # Passing --cov to a pytest without pytest-cov makes it reject the whole
        # argument vector, so the tests would not run and the failure would look
        # like a broken plugin. Coverage must be dropped and reported as skipped.
        root = self._plugin(
            tmp_path,
            test_body="from icon_x.api import add\n\ndef test_bad():\n    assert add(1, 2) == 4\n",
        )
        report = asyncio.run(self._gate().run(root))
        # The real failure is reported...
        assert any("test_bad" in f.code for f in report.by_source(SOURCE_TESTS))
        # ...and nothing is invented about coverage.
        assert report.by_source(SOURCE_COVERAGE) == ()
        assert any("coverage" in note for note in report.skipped)

    def test_tests_can_be_switched_off(self, tmp_path):
        (tmp_path / "icon_x").mkdir()
        (tmp_path / "icon_x" / "api.py").write_text("x = 1\n", encoding="utf-8")
        import sys

        gate = QualityGate(python_executable=sys.executable, run_tests=False)
        report = asyncio.run(gate.run(tmp_path))
        assert report.by_source(SOURCE_TESTS) == ()
        # Switched off is still "nothing was learned", so it is disclosed as a
        # skip. Otherwise a caller deciding whether the plugin is finished would
        # read the absence of test findings as the tests having passed.
        assert any(note.startswith("tests (") for note in report.skipped)
        assert report.coverage_percent is None


class TestFindingKeys:
    def test_line_numbers_are_bucketed(self):
        a = CodeFinding(source="prospector", path="a.py", code="c", message="m", line=10)
        b = CodeFinding(source="prospector", path="a.py", code="c", message="m", line=12)
        assert a.key == b.key

    def test_distant_lines_differ(self):
        a = CodeFinding(source="prospector", path="a.py", code="c", message="m", line=10)
        b = CodeFinding(source="prospector", path="a.py", code="c", message="m", line=80)
        assert a.key != b.key

    def test_the_message_does_not_affect_the_key(self):
        # Two runs may word the same problem differently; the key must be stable.
        a = CodeFinding(source="prospector", path="a.py", code="c", message="one", line=10)
        b = CodeFinding(source="prospector", path="a.py", code="c", message="two", line=10)
        assert a.key == b.key

    def test_source_and_code_are_part_of_the_identity(self):
        a = CodeFinding(source="prospector", path="a.py", code="c1", message="m", line=10)
        b = CodeFinding(source="prospector", path="a.py", code="c2", message="m", line=10)
        c = CodeFinding(source="compile", path="a.py", code="c1", message="m", line=10)
        assert len({a.key, b.key, c.key}) == 3

    def test_a_file_level_finding_has_a_stable_key(self):
        a = CodeFinding(source=SOURCE_FORMAT, path="a.py", code="would-reformat", message="m")
        assert a.key.endswith(":-:would-reformat")


class TestReport:
    def test_clean_report_says_so(self, tmp_path):
        (tmp_path / "icon_x").mkdir()
        (tmp_path / "icon_x" / "action.py").write_text("x = 1\n", encoding="utf-8")
        report = QualityReport(project_dir=tmp_path, findings=(), checked_files=("icon_x/action.py",))
        assert report.clean
        assert "No findings" in report.summary()

    def test_summary_counts_per_source(self, tmp_path):
        findings = (
            CodeFinding(source=SOURCE_COMPILE, path="a.py", code="syntax-error", message="m", line=1),
            CodeFinding(source=SOURCE_PROSPECTOR, path="b.py", code="unused-import", message="m", line=1),
            CodeFinding(source=SOURCE_PROSPECTOR, path="c.py", code="unused-import", message="m", line=1),
        )
        summary = QualityReport(project_dir=tmp_path, findings=findings).summary()
        assert "3 finding(s)" in summary
        assert "compile: 1" in summary
        assert "prospector: 2" in summary

    def test_skipped_checks_are_surfaced_so_clean_is_not_misread(self, tmp_path):
        report = QualityReport(project_dir=tmp_path, findings=(), skipped=("prospector (not available)",))
        assert "skipped" in report.summary()

    def test_render_truncates_and_says_so(self, tmp_path):
        findings = tuple(
            CodeFinding(source=SOURCE_PROSPECTOR, path=f"f{n}.py", code="c", message="m", line=1) for n in range(50)
        )
        rendered = QualityReport(project_dir=tmp_path, findings=findings).render(limit=10)
        assert rendered.count("\n") == 10
        assert "and 40 more" in rendered

    def test_keys_are_sorted_and_stable(self, tmp_path):
        findings = (
            CodeFinding(source=SOURCE_PROSPECTOR, path="z.py", code="c", message="m", line=1),
            CodeFinding(source=SOURCE_PROSPECTOR, path="a.py", code="c", message="m", line=1),
        )
        report = QualityReport(project_dir=tmp_path, findings=findings)
        assert report.keys() == tuple(sorted(report.keys()))


class TestMissingTools:
    def test_a_missing_tool_is_recorded_as_skipped_not_passed(self, tmp_path):
        # A check whose tool is absent must be reported as skipped, so a report
        # with no findings from it cannot be read as that check having passed.
        (tmp_path / "icon_x").mkdir()
        (tmp_path / "icon_x" / "action.py").write_text("x = 1\n", encoding="utf-8")
        gate = QualityGate(
            python_executable="definitely-not-python-xyz",
            black_executable="definitely-not-black-xyz",
            prospector_executable="definitely-not-prospector-xyz",
            run_tests=False,
        )
        report = asyncio.run(gate.run(tmp_path))
        # compile, format, prospector -- their tools are absent -- plus the tests,
        # which this gate was told not to run.
        assert len(report.skipped) == 4
        assert report.clean
        assert "skipped" in report.summary()


class TestLintRemit:
    """The linter's remit is narrower than the compiler's, and deliberately so."""

    def test_generated_files_are_outside_the_lint_remit(self):
        assert is_lint_excluded("icon_x/actions/a/schema.py")
        assert is_lint_excluded("setup.py")

    def test_unit_tests_are_outside_the_lint_remit(self):
        # The plugins repository's static-analysis job filters unit_test/ out
        # before running prospector, so a test that trips protected-access for
        # mocking a private method is not a defect the plugin answers for.
        assert is_lint_excluded("unit_test/test_action.py")
        assert is_lint_excluded("unit_test/helpers/fixtures.py")

    def test_plugin_code_is_inside_it(self):
        assert not is_lint_excluded("icon_x/actions/a/action.py")
        assert not is_lint_excluded("icon_x/util/api.py")
        assert not is_lint_excluded("icon_x/connection/connection.py")

    def test_unit_tests_are_still_compiled_and_formatted(self):
        # Excluded from the *linter* only. A unit test that does not parse is
        # still a broken plugin, so it must stay in the compile/format set.
        assert is_lint_excluded("unit_test/test_action.py")
        assert not is_generated("unit_test/test_action.py")


class TestToolInvocation:
    """The gate must run the tools the way the plugins repository runs them.

    Every assertion here is a defect this tool actually had. Bare `black` applies
    its own 88-column default to a plugin formatted at 120; bare `prospector`
    loads no profile and enables pycodestyle, so it reported `bad-super-call`,
    `dangerous-default-value` and `E501` against code the repository merges
    without comment -- none of which the agent may fix, because the steering
    forbids editing the templates they come from.
    """

    def _capture(self, tmp_path, **kwargs):
        """Run the gate with its subprocess calls recorded instead of executed."""
        (tmp_path / "icon_x").mkdir()
        (tmp_path / "icon_x" / "api.py").write_text("x = 1\n", encoding="utf-8")

        commands = []
        gate = QualityGate(run_tests=False, **kwargs)

        async def fake_run(command, *, cwd):
            commands.append(list(command))
            if "--output-format" in command:
                return (0, '{"messages": []}', "")
            return (0, "", "")

        gate._run = fake_run  # noqa: SLF001 - asserting on the argv is the point
        asyncio.run(gate.run(tmp_path))
        return commands

    def _command_for(self, commands, executable_fragment):
        for command in commands:
            if executable_fragment in command[0]:
                return command
        raise AssertionError(f"no command found for {executable_fragment}: {commands}")

    def test_black_is_checked_at_the_plugin_line_length(self, tmp_path):
        command = self._command_for(self._capture(tmp_path), "black")
        assert f"--line-length={PLUGIN_LINE_LENGTH}" in command
        assert PLUGIN_LINE_LENGTH == 120  # what the repository's CI formats to

    def test_prospector_runs_with_a_profile(self, tmp_path):
        command = self._command_for(self._capture(tmp_path), "prospector")
        assert "--profile" in command
        assert command[command.index("--profile") + 1].endswith(".yaml")

    def test_prospector_runs_exactly_the_tools_the_repository_names(self, tmp_path):
        command = self._command_for(self._capture(tmp_path), "prospector")
        named = [command[i + 1] for i, token in enumerate(command) if token == "--tool"]
        assert named == list(LINT_TOOLS)
        # pycodestyle is absent on purpose: it is the source of E501, and the
        # repository neither runs it nor treats long lines as defects.
        assert "pycodestyle" not in named

    def test_an_explicit_profile_is_used_as_given(self, tmp_path):
        profile = LintProfile(path="/tmp/custom-profile.yaml", source="repository", detail="")
        command = self._command_for(self._capture(tmp_path, lint_profile=profile), "prospector")
        assert command[command.index("--profile") + 1] == "/tmp/custom-profile.yaml"

    def test_a_vendored_profile_is_disclosed_as_possibly_stale(self, tmp_path):
        # The repository's copy is authoritative. Falling back to ours is not a
        # failure, but it must not pass silently: the two can disagree about what
        # "clean" means, and the operator should know which one judged the plugin.
        profile = LintProfile(
            path="/tmp/vendored.yaml",
            source=LINT_PROFILE_SOURCE_FALLBACK,
            detail="no plugins checkout; using the vendored copy",
        )
        gate = QualityGate(run_tests=False, lint_profile=profile)

        async def fake_run(command, *, cwd):
            return (0, '{"messages": []}', "")

        gate._run = fake_run  # noqa: SLF001
        report = asyncio.run(gate.run(tmp_path))
        assert any("prospector profile" in note for note in report.skipped)

    def test_an_unresolvable_profile_is_disclosed_too(self, tmp_path):
        profile = LintProfile(path=None, detail="no lint profile found anywhere")
        gate = QualityGate(run_tests=False, lint_profile=profile)

        async def fake_run(command, *, cwd):
            assert "--profile" not in command
            return (0, '{"messages": []}', "")

        gate._run = fake_run  # noqa: SLF001
        report = asyncio.run(gate.run(tmp_path))
        assert any("prospector profile" in note for note in report.skipped)


class TestFindingsTheRepositoryWouldNotRaise:
    """End to end against real black and prospector, on the shapes that misfired."""

    def _plugin(self, tmp_path):
        package = tmp_path / "icon_x"
        package.mkdir()
        # A line between 89 and 120 characters: a defect at black's default width,
        # fine at the width the plugins repository formats to.
        long_but_allowed = "MESSAGE = " + '"' + ("a" * 95) + '"' + "\n"
        assert 88 < len(long_but_allowed.strip()) <= PLUGIN_LINE_LENGTH
        (package / "api.py").write_text(long_but_allowed, encoding="utf-8")
        return tmp_path

    def test_a_line_within_the_plugin_width_is_not_a_formatting_defect(self, tmp_path):
        root = self._plugin(tmp_path)
        report = asyncio.run(QualityGate(run_tests=False).run(root))
        assert report.by_source(SOURCE_FORMAT) == (), report.render()

    def test_the_scaffolders_own_template_shapes_are_not_reported(self, tmp_path):
        # `super(self.__class__, self)` and `params={}` come from the scaffolder's
        # templates. The repository's profile disables both, and the steering
        # forbids editing the files they appear in -- so reporting them would ask
        # for a change that must not be made, round after round.
        package = tmp_path / "icon_x" / "actions" / "thing"
        package.mkdir(parents=True)
        (package / "action.py").write_text(
            "import insightconnect_plugin_runtime\n\n\n"
            "class Thing(insightconnect_plugin_runtime.Action):\n"
            "    def __init__(self):\n"
            "        super(self.__class__, self).__init__(name='thing')\n\n"
            "    def run(self, params={}):\n"
            "        return {}\n",
            encoding="utf-8",
        )
        report = asyncio.run(QualityGate(run_tests=False).run(tmp_path))
        codes = {finding.code for finding in report.by_source(SOURCE_PROSPECTOR)}
        assert "bad-super-call" not in codes, report.render()
        assert "dangerous-default-value" not in codes, report.render()
        assert "E501" not in codes, report.render()

    def test_a_real_defect_is_still_reported(self, tmp_path):
        # The alignment must not have turned the linter off. This is the actual
        # bug in the throwaway abuseipdb plugin: requests used, never imported.
        package = tmp_path / "icon_x"
        package.mkdir()
        (package / "api.py").write_text(
            "def fetch(url):\n    return requests.get(url)\n",
            encoding="utf-8",
        )
        report = asyncio.run(QualityGate(run_tests=False).run(tmp_path))
        codes = {finding.code for finding in report.by_source(SOURCE_PROSPECTOR)}
        assert "undefined-variable" in codes, report.render()

    def test_a_lint_defect_in_a_unit_test_is_not_reported(self, tmp_path):
        (tmp_path / "icon_x").mkdir()
        (tmp_path / "icon_x" / "api.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "unit_test").mkdir()
        (tmp_path / "unit_test" / "test_api.py").write_text(
            "def test_thing():\n    return undefined_name_in_a_test\n",
            encoding="utf-8",
        )
        report = asyncio.run(QualityGate(run_tests=False).run(tmp_path))
        lint_paths = {finding.path for finding in report.by_source(SOURCE_PROSPECTOR)}
        assert not any(path.startswith("unit_test/") for path in lint_paths), report.render()
        # ...but the file was still compiled, so a test that cannot parse is caught.
        assert "unit_test/test_api.py" in report.checked_files
