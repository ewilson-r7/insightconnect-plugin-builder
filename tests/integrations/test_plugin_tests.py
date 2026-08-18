"""Unit tests for the one definition of how a plugin's unit tests are run.

The module under test is **mechanics only**: it runs pytest, parses what came
back, and reports it. It reaches no verdict, because the two callers need
different things from the same run -- the ``Quality_Gate`` turns it into repairable
findings, the ``Code_Validator``'s ``test`` stage turns it into a pass/fail -- and
folding either judgment in is what let them report opposite outcomes for one tree.

pytest is driven for real here rather than mocked, against tiny generated trees.
The parsing is of pytest's own output format, and a fake that emits what we think
pytest prints would test the fake.
"""

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

from icplugin_builder.core.plugin_files import UNIT_TEST_DIR, package_dir
from icplugin_builder.integrations.plugin_tests import UnitTestRun, run_unit_tests

PACKAGE = "icon_runner"

#: This interpreter has pytest by definition -- it is running these tests. It does
#: not necessarily have the SDK, which is exactly why the resolver exists; passing
#: it explicitly keeps these tests about the runner rather than about the host.
INTERPRETER = sys.executable


def _tree(root: Path, *, tests: str = None, package: bool = True) -> Path:
    """Build a minimal plugin tree, optionally with a unit test module."""
    if package:
        (root / PACKAGE).mkdir(parents=True, exist_ok=True)
        (root / PACKAGE / "api.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    if tests is not None:
        (root / UNIT_TEST_DIR).mkdir(parents=True, exist_ok=True)
        (root / UNIT_TEST_DIR / "test_api.py").write_text(tests, encoding="utf-8")
    return root


def _run(root: Path, **kwargs) -> UnitTestRun:
    kwargs.setdefault("python_executable", INTERPRETER)
    kwargs.setdefault("timeout_seconds", 120.0)
    return asyncio.run(run_unit_tests(root, **kwargs))


class TestARunThatHappens:
    def test_a_passing_suite_reports_passed(self, tmp_path):
        root = _tree(tmp_path, tests="def test_ok():\n    assert True\n")
        run = _run(root, measure_coverage=False)
        assert run.ran and run.passed
        assert run.failures == ()
        assert run.returncode == 0
        assert run.interpreter == INTERPRETER
        assert "passed" in run.message

    def test_a_failing_test_is_parsed_with_its_name(self, tmp_path):
        root = _tree(tmp_path, tests="def test_bad():\n    assert 1 == 2\n")
        run = _run(root, measure_coverage=False)
        assert run.ran and not run.passed
        assert [failure.name for failure in run.failures] == ["test_bad"]
        assert run.failures[0].path.endswith("test_api.py")
        assert run.returncode != 0

    def test_two_failures_in_one_file_stay_distinct(self, tmp_path):
        """Their names are what keep the keys apart.

        If two failures in one file collapsed to one identity, fixing one of them
        would read as resolving nothing and the repair loop's finding-key
        arithmetic would call a stall while progress was being made.
        """
        root = _tree(
            tmp_path,
            tests="def test_one():\n    assert False\n\n\ndef test_two():\n    assert False\n",
        )
        run = _run(root, measure_coverage=False)
        assert sorted(failure.name for failure in run.failures) == ["test_one", "test_two"]

    def test_a_collection_error_is_a_non_zero_exit_rather_than_a_pass(self, tmp_path):
        root = _tree(tmp_path, tests="import a_module_that_does_not_exist\n")
        run = _run(root, measure_coverage=False)
        assert run.ran
        assert not run.passed
        assert run.returncode != 0

    def test_no_tests_collected_is_distinguished_from_no_directory(self, tmp_path):
        root = _tree(tmp_path, tests="# a module with no tests in it\n")
        run = _run(root, measure_coverage=False)
        assert run.ran is True
        assert run.no_tests is True
        assert not run.passed


class TestARunThatCannotHappen:
    """Every one of these is an outcome to report, not an exception to raise."""

    def test_a_missing_unit_test_directory_never_reads_as_a_pass(self, tmp_path):
        run = _run(_tree(tmp_path))
        assert run.ran is False
        assert run.no_tests is True
        assert run.passed is False
        assert UNIT_TEST_DIR in run.message

    def test_a_missing_interpreter_is_reported_with_the_interpreter_named(self, tmp_path):
        root = _tree(tmp_path, tests="def test_ok():\n    assert True\n")
        run = _run(root, python_executable="/nonexistent/python")
        assert run.ran is False
        assert run.passed is False
        assert "/nonexistent/python" in run.message
        assert any("not available" in note for note in run.skipped)

    def test_a_run_that_exceeds_the_timeout_is_recorded_as_a_timeout(self, tmp_path):
        root = _tree(tmp_path, tests="import time\n\n\ndef test_slow():\n    time.sleep(30)\n")
        run = _run(root, timeout_seconds=0.5, measure_coverage=False)
        assert run.timed_out is True
        assert run.passed is False
        assert run.interpreter == INTERPRETER

    def test_pytest_absent_is_reported_and_never_installed(self, tmp_path):
        """SCOPE-12: the absence is a finding, not a thing to remedy."""
        fake = tmp_path / "python-without-pytest"
        fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake.chmod(0o755)
        root = _tree(tmp_path / "tree", tests="def test_ok():\n    assert True\n")
        run = _run(root, python_executable=str(fake))
        assert run.passed is False
        assert str(fake) in run.message or any(str(fake) in note for note in run.skipped)


class TestCoverage:
    def test_coverage_is_measured_when_pytest_cov_is_available(self, tmp_path):
        if not _has_pytest_cov():
            pytest.skip("pytest-cov is not installed for this interpreter; nothing to measure")
        root = _tree(
            tmp_path,
            tests=f"import sys\nsys.path.insert(0, '..')\n"
            f"from {PACKAGE}.api import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        )
        run = _run(root)
        assert run.package == PACKAGE
        assert run.coverage_percent is not None
        assert "coverage" in run.message

    def test_pytest_cov_absent_is_recorded_as_a_skip_not_a_zero(self, tmp_path):
        """A missing measurement is not a measurement of nothing."""
        root = _tree(tmp_path, tests="def test_ok():\n    assert True\n")
        run = _run(root, measure_coverage=False)
        assert run.coverage_percent is None

    def test_a_tree_with_no_package_says_so_rather_than_measuring_nothing(self, tmp_path):
        root = _tree(tmp_path, tests="def test_ok():\n    assert True\n", package=False)
        run = _run(root)
        assert run.package is None
        assert any("no plugin package" in note for note in run.skipped)

    def test_the_package_is_found_under_either_prefix(self, tmp_path):
        for prefix in ("icon_", "komand_"):
            root = tmp_path / prefix
            (root / f"{prefix}thing").mkdir(parents=True)
            assert package_dir(root) == f"{prefix}thing"


def _has_pytest_cov() -> bool:
    return shutil.which(INTERPRETER) is not None and _importable("pytest_cov")


def _importable(module: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(module) is not None


class TestTheGateAdapterIsFindingForFindingIdentical:
    """Task 8.3 -- the move is a refactor, so the gate's output must not shift.

    ``QualityGate._check_tests`` is now a thin adapter over :func:`run_unit_tests`.
    What it must preserve exactly is the *shape* of what it produces: the finding
    keys, the paths, the codes, the messages and the skip notes. Those keys are what
    the repair loop's termination arithmetic compares round over round, so a change
    in any of them is a behavioural change dressed as a refactor -- and the nine
    preservation baselines beside this file assert the same thing over whole trees.
    """

    def _gate_output(self, root):
        from icplugin_builder.integrations.quality_gate import QualityGate

        gate = QualityGate(python_executable=INTERPRETER, run_tests=True)
        return asyncio.run(gate._check_tests(Path(root)))

    def test_a_missing_directory_keeps_its_finding_shape(self, tmp_path):
        findings, skipped, percent = self._gate_output(_tree(tmp_path))
        assert [(f.source, f.path, f.code, f.message) for f in findings] == [
            ("tests", UNIT_TEST_DIR, "no-tests", "no unit_test/ directory; every action needs unit tests")
        ]
        assert skipped == []
        assert percent is None

    def test_no_tests_collected_keeps_its_distinct_message(self, tmp_path):
        root = _tree(tmp_path, tests="# nothing to collect\n")
        findings, _, percent = self._gate_output(root)
        assert [(f.code, f.message) for f in findings] == [("no-tests", "unit_test/ contains no runnable tests")]
        assert percent is None

    def test_a_failing_test_keeps_its_key_shape(self, tmp_path):
        root = _tree(tmp_path, tests="def test_bad():\n    assert False\n")
        findings, _, _ = self._gate_output(root)
        failed = [f for f in findings if f.code.startswith("test-failed")]
        assert [(f.source, f.code, f.message) for f in failed] == [
            ("tests", "test-failed[test_bad]", "unit test test_bad failed")
        ]
        assert failed[0].path.endswith("test_api.py")

    def test_two_failures_keep_two_distinct_keys(self, tmp_path):
        root = _tree(tmp_path, tests="def test_a():\n    assert False\n\n\ndef test_b():\n    assert False\n")
        findings, _, _ = self._gate_output(root)
        keys = sorted(f.key for f in findings if f.code.startswith("test-failed"))
        assert len(keys) == len(set(keys)) == 2, keys

    def test_a_missing_interpreter_keeps_its_skip_note(self, tmp_path):
        from icplugin_builder.integrations.quality_gate import QualityGate

        root = _tree(tmp_path, tests="def test_ok():\n    assert True\n")
        gate = QualityGate(python_executable="/nonexistent/python", run_tests=True)
        findings, skipped, percent = asyncio.run(gate._check_tests(Path(root)))
        assert findings == []
        assert skipped == ["tests (/nonexistent/python -m pytest not available)"]
        assert percent is None

    def test_coverage_below_the_threshold_keeps_its_finding_shape(self, tmp_path):
        if not _importable("pytest_cov"):
            pytest.skip("pytest-cov is not installed for this interpreter; the threshold path cannot run")
        from icplugin_builder.integrations.quality_gate import QualityGate

        root = _tree(tmp_path, tests="def test_ok():\n    assert True\n")
        (root / PACKAGE / "wide.py").write_text(
            "\n".join(f"VALUE_{n} = {n}" for n in range(40)) + "\n", encoding="utf-8"
        )
        gate = QualityGate(python_executable=INTERPRETER, run_tests=True, coverage_threshold=99.0)
        findings, _, percent = asyncio.run(gate._check_tests(Path(root)))
        below = [f for f in findings if f.code == "below-threshold"]
        assert below, f"coverage was {percent!r} and no threshold finding was produced"
        assert below[0].source == "coverage"
        assert below[0].path == PACKAGE
        assert "below the 99% minimum" in below[0].message
