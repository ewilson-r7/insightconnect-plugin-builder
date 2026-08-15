"""Tests for the quality gate.

The exclusion of generated files is the load-bearing part and is tested hardest:
`insight-plugin` emits `schema.py` files that prospector flags and the steering
forbids editing, so a gate that reported them would ask for a fix that must not
be made and the repair loop could never converge.

The compile check is exercised against real files rather than mocked, because a
file that does not parse is the defect this tool has actually shipped.
"""

import asyncio

from icplugin_builder.integrations.quality_gate import (
    SOURCE_COMPILE,
    SOURCE_FORMAT,
    SOURCE_PROSPECTOR,
    CodeFinding,
    QualityGate,
    QualityReport,
    hand_written_python,
    is_generated,
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
        (tmp_path / "icon_x").mkdir()
        (tmp_path / "icon_x" / "action.py").write_text("x = 1\n", encoding="utf-8")
        gate = QualityGate(
            python_executable="definitely-not-python-xyz",
            black_executable="definitely-not-black-xyz",
            prospector_executable="definitely-not-prospector-xyz",
        )
        report = asyncio.run(gate.run(tmp_path))
        assert len(report.skipped) == 3
        assert report.clean  # no findings, but the skips make that unambiguous
