"""One definition of how a plugin's unit tests are run.

Two subsystems need to know whether a plugin's tests pass, and they reached
opposite conclusions about the same plugin: the ``Quality_Gate`` ran them on the
host and reported them passing, while the ``Code_Validator``'s ``test`` stage ran
``docker run --rm <image> python -m pytest -q`` against an image that contains
neither the tests (the generated ``.dockerignore`` excludes ``unit_test/**/*``)
nor ``pytest`` (the runtime image has no test dependencies, correctly). So the
stage failed for every plugin ever built, and the two subsystems contradicted each
other about one tree.

This module is the single definition they now share. It is **mechanics only**: it
runs the tests, parses what came back, and reports it. It produces no findings and
reaches no verdict, because the two callers need different things from the same
run -- the gate turns it into repairable findings, the stage turns it into a
pass/fail -- and folding either judgment in here is what let them diverge.

**``pytest`` is not a dependency of this tool** and is never installed on the
plugin's behalf (SCOPE-12). It has to be present in the plugin's own interpreter,
and its absence is reported with the interpreter that was tried, so the report says
why the run could not happen rather than implying the plugin is broken.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

from ..core.plugin_files import UNIT_TEST_DIR, package_dir
from .build_prep import resolve_test_interpreter

__all__ = [
    "DEFAULT_TEST_TIMEOUT_SECONDS",
    "TestFailure",
    "UnitTestRun",
    "run_unit_tests",
]

#: The abort threshold for a unit test run. Matches the four-stage pipeline's own
#: stage timeout (Req 8.8), because the ``test`` stage is one of those stages.
DEFAULT_TEST_TIMEOUT_SECONDS = 600.0

#: pytest's short summary line, e.g. ``FAILED unit_test/test_api.py::test_bad - ...``.
#: Preferred over scraping the traceback because it is one line per failure and
#: carries the test name.
_PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?\.py)::(\S+?)(?:\s|$)", re.MULTILINE)

#: The ``TOTAL`` row of a coverage term report, e.g. ``TOTAL   4   1   75%``.
_COVERAGE_TOTAL = re.compile(r"^TOTAL\s+.*?(\d+(?:\.\d+)?)%", re.MULTILINE)

#: pytest's wording when a run collected nothing.
_NO_TESTS = ("no tests ran", "no tests collected")


@dataclass(frozen=True)
class TestFailure:
    """One failing test, located.

    Attributes:
        path: the test file, as pytest reported it.
        line: the 1-based line number when one could be parsed, else ``None``.
        name: the test function's name. Carried because it becomes part of a
            finding's identity: two failures in one file must not collapse to one
            key, or fixing one of three would read as resolving nothing and the
            repair loop would call a stall while it was still making progress.
    """

    path: str
    line: Optional[int]
    name: str


@dataclass(frozen=True)
class UnitTestRun:
    """What happened when a plugin's unit tests were run. No verdict attached.

    Attributes:
        interpreter: the interpreter that was used, or that was going to be. Named
            in every outcome, because "the tests failed" and "the tests could not
            be run with this interpreter" are different reports and the operator
            cannot act on the second without knowing which one was tried.
        ran: whether pytest actually executed. ``False`` covers a missing
            ``unit_test/`` directory, an interpreter without pytest, and a run that
            exceeded the timeout.
        no_tests: whether there are no tests to run -- either because
            ``unit_test/`` is absent (with ``ran`` false) or because pytest
            collected nothing from it (with ``ran`` true). The two are distinct
            defects and the pair of flags keeps them so.
        timed_out: whether the run was aborted at ``timeout_seconds``.
        failures: the parsed failing tests.
        returncode: pytest's exit status, or ``None`` when it never ran.
        output: pytest's combined stdout and stderr.
        coverage_percent: the statement coverage measured, or ``None`` when it was
            not measured at all. A figure rather than an implication, because "no
            coverage finding" is true both of a plugin that met the threshold and
            of one whose coverage was never measured.
        package: the plugin package coverage was measured against, when known.
        skipped: notes about what could not be established, so a run that learned
            nothing is never mistaken for a clean one.
        message: a human-readable summary naming the interpreter and the outcome.
    """

    interpreter: Optional[str]
    ran: bool = False
    no_tests: bool = False
    timed_out: bool = False
    failures: Tuple[TestFailure, ...] = ()
    returncode: Optional[int] = None
    output: str = ""
    coverage_percent: Optional[float] = None
    package: Optional[str] = None
    skipped: Tuple[str, ...] = ()
    message: str = ""

    @property
    def passed(self) -> bool:
        """Return ``True`` iff the tests ran, collected something, and all passed.

        Deliberately conservative: a run that did not happen is not a pass. The
        four-stage gate has no third state, so an unrunnable check has to fail
        closed there -- and this property is what it fails closed on.
        """
        return self.ran and not self.no_tests and not self.failures and self.returncode == 0


@dataclass
class _Completed:
    """The outcome of one subprocess: ran, missing, or timed out."""

    returncode: Optional[int] = None
    output: str = ""
    missing: bool = False
    timed_out: bool = False
    detail: str = field(default="")

    @property
    def ok(self) -> bool:
        return not self.missing and not self.timed_out


async def _run(command: Sequence[str], *, cwd: Path, timeout_seconds: float) -> _Completed:
    """Run ``command``, distinguishing a missing tool from a timeout.

    The distinction is the point. The ``Quality_Gate``'s own runner collapses both
    to ``None``, which is tolerable where the outcome is a skip note either way but
    not for the ``test`` stage, where a 600s abort and an absent interpreter need
    different messages (Req 8.8, clause 2.3).
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as error:
        return _Completed(missing=True, detail=str(error))
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        await _terminate(process)
        return _Completed(timed_out=True, detail=f"exceeded the {timeout_seconds:.0f}s limit")
    returncode = process.returncode if process.returncode is not None else -1
    output = stdout_bytes.decode("utf-8", errors="replace") + stderr_bytes.decode("utf-8", errors="replace")
    return _Completed(returncode=returncode, output=output)


async def _terminate(process: "asyncio.subprocess.Process") -> None:
    """Kill ``process`` and reap it after a timeout abort.

    Reaped rather than merely killed: leaving the transport unclosed raises
    ``Event loop is closed`` from the child watcher after the loop has gone, which
    turns a clean timeout report into noise on the way out.
    """
    try:
        process.kill()
    except ProcessLookupError:  # pragma: no cover - already exited
        return
    try:
        await process.wait()
    except Exception:  # pragma: no cover - best-effort reap
        pass


async def run_unit_tests(
    project_dir: Union[str, Path],
    *,
    python_executable: Optional[str] = None,
    timeout_seconds: float = DEFAULT_TEST_TIMEOUT_SECONDS,
    measure_coverage: bool = True,
) -> UnitTestRun:
    """Run the plugin's unit tests under ``python_executable`` and report what happened.

    Args:
        project_dir: the plugin working tree.
        python_executable: the interpreter to run under. When omitted it is
            resolved with
            :func:`~icplugin_builder.integrations.build_prep.resolve_test_interpreter`,
            which requires one that can import **both** the SDK and ``pytest`` --
            a plugin imports the SDK at module scope, so an interpreter with only
            one of the two cannot run its tests at all.
        timeout_seconds: the abort threshold (Req 8.8).
        measure_coverage: whether to ask for coverage. Switched off by callers that
            only need the pass/fail outcome.

    Returns:
        A :class:`UnitTestRun`. No exception is raised for a failing or unrunnable
        run: both are outcomes to report, and which of them blocks anything is the
        caller's decision.
    """
    root = Path(project_dir)
    skipped: List[str] = []

    interpreter = python_executable
    if interpreter is None:
        resolution = resolve_test_interpreter()
        interpreter = resolution.executable
        if not resolution.resolved:
            return UnitTestRun(
                interpreter=None,
                skipped=(f"tests ({resolution.detail})",),
                message=f"the plugin's unit tests could not be run: {resolution.detail}",
            )

    if not (root / UNIT_TEST_DIR).is_dir():
        return UnitTestRun(
            interpreter=interpreter,
            no_tests=True,
            message=f"no {UNIT_TEST_DIR}/ directory; every action needs unit tests",
        )

    package = package_dir(root)
    command = [str(interpreter), "-m", "pytest", UNIT_TEST_DIR, "-q", "--no-header"]

    # Coverage is only requested when the plugin's interpreter actually has
    # pytest-cov. Passing --cov without it makes pytest reject the whole argument
    # vector, so the tests would not run at all and the failure would be reported
    # as though the plugin were broken.
    with_coverage = bool(package) and measure_coverage and await _has_pytest_cov(interpreter, timeout_seconds)
    if with_coverage:
        command.extend([f"--cov={package}", "--cov-report=term-missing"])
    elif package and measure_coverage:
        skipped.append("coverage (pytest-cov not installed for the plugin interpreter)")

    completed = await _run(command, cwd=root, timeout_seconds=timeout_seconds)
    if not completed.ok:
        reason = "timed out" if completed.timed_out else "is not available"
        return UnitTestRun(
            interpreter=interpreter,
            timed_out=completed.timed_out,
            skipped=(f"tests ({interpreter} -m pytest not available)",),
            message=f"{interpreter} -m pytest {reason}: {completed.detail}",
        )

    output = completed.output
    if _no_tests_collected(output):
        return UnitTestRun(
            interpreter=interpreter,
            ran=True,
            no_tests=True,
            returncode=completed.returncode,
            output=output,
            package=package,
            skipped=tuple(skipped),
            message=f"{UNIT_TEST_DIR}/ contains no runnable tests",
        )

    failures = tuple(TestFailure(path=path, line=line, name=name) for path, line, name in _pytest_failures(output))

    percent: Optional[float] = None
    if with_coverage and package:
        percent = _parse_coverage_total(output)
        if percent is None:
            # Coverage was asked for and the total did not come back. Saying
            # nothing would leave the absence of a coverage finding looking like a
            # plugin that met the threshold.
            skipped.append("coverage (the coverage total was not reported)")
    elif not package:
        skipped.append("coverage (no plugin package directory to measure)")

    run = UnitTestRun(
        interpreter=interpreter,
        ran=True,
        failures=failures,
        returncode=completed.returncode,
        output=output,
        coverage_percent=percent,
        package=package,
        skipped=tuple(skipped),
    )
    return _with_message(run)


def _with_message(run: UnitTestRun) -> UnitTestRun:
    """Attach the summary naming the interpreter and the outcome."""
    if run.failures:
        detail = f"{len(run.failures)} unit test(s) failed"
    elif run.returncode != 0:
        detail = f"the unit test run exited {run.returncode} with no parsed failure"
    else:
        detail = "the unit tests passed"
    coverage = f"; coverage {run.coverage_percent:.0f}%" if run.coverage_percent is not None else ""
    return UnitTestRun(
        interpreter=run.interpreter,
        ran=run.ran,
        no_tests=run.no_tests,
        timed_out=run.timed_out,
        failures=run.failures,
        returncode=run.returncode,
        output=run.output,
        coverage_percent=run.coverage_percent,
        package=run.package,
        skipped=run.skipped,
        message=f"{detail} under {run.interpreter}{coverage}",
    )


async def _has_pytest_cov(interpreter: str, timeout_seconds: float) -> bool:
    """Return ``True`` iff ``interpreter`` can import ``pytest_cov``."""
    completed = await _run(
        [str(interpreter), "-c", "import pytest_cov"], cwd=Path.cwd(), timeout_seconds=timeout_seconds
    )
    return completed.ok and completed.returncode == 0


def _no_tests_collected(output: str) -> bool:
    """Return ``True`` iff pytest reported collecting no tests."""
    lowered = output.lower()
    return any(phrase in lowered for phrase in _NO_TESTS)


def _pytest_failures(output: str) -> List[Tuple[str, Optional[int], str]]:
    """Parse ``(path, line, test_name)`` for each failing test."""
    failures: List[Tuple[str, Optional[int], str]] = []
    for match in _PYTEST_FAILED.finditer(output):
        path, name = match.group(1), match.group(2)
        line_match = re.search(rf"^{re.escape(path)}:(\d+):", output, re.MULTILINE)
        line = int(line_match.group(1)) if line_match else None
        failures.append((path, line, name))
    return failures


def _parse_coverage_total(output: str) -> Optional[float]:
    """Return the total coverage percentage from a term report, or ``None``."""
    matches = _COVERAGE_TOTAL.findall(output)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:  # pragma: no cover - regex guarantees a numeric match
        return None
