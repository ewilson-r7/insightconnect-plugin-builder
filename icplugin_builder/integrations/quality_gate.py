"""Fast, correctable quality checks over a generated plugin's hand-written code.

This is the check half of the repair loop. It answers "what is wrong with this
code, specifically and by location" in a form something can act on, which the
four-stage :class:`~icplugin_builder.integrations.code_validator.CodeValidator`
does not: that pipeline records pass/fail per stage and hands back raw output.
Pass/fail is enough to gate an export. It is not enough to fix anything.

Three checks run here, all fast and none needing Docker, so the loop can iterate
in seconds rather than minutes:

* **compile** -- every hand-written Python file parses. A file that does not parse
  is the single most damaging defect this tool has shipped, and it is invisible to
  a linter run that crashes on the same file.
* **format** -- ``black --check``, matching the formatting ``insight-plugin
  refresh`` itself applies.
* **prospector** -- the linter the plugin steering specifies, read through its
  JSON formatter rather than by scraping human-readable output.

**Generated files are excluded, and that is load-bearing.** ``insight-plugin``
emits ``schema.py`` files that prospector flags (``bad-super-call``, from the
CLI's own templates) and the steering forbids editing them. A loop that counted
those findings could never converge: it would ask for a fix that must not be made,
round after round, until it hit its cap. Excluding them is what makes "no findings"
an achievable state rather than an impossible one.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import List, Optional, Sequence, Tuple, Union

from .build_prep import LINT_TOOLS, PLUGIN_LINE_LENGTH, LintProfile, resolve_lint_profile

__all__ = [
    "GENERATED_FILE_NAMES",
    "GENERATED_DIR_NAMES",
    "SOURCE_COMPILE",
    "SOURCE_FORMAT",
    "SOURCE_PROSPECTOR",
    "SOURCE_TESTS",
    "SOURCE_COVERAGE",
    "DEFAULT_COVERAGE_THRESHOLD",
    "CodeFinding",
    "QualityReport",
    "QualityGate",
    "is_generated",
    "is_lint_excluded",
    "hand_written_python",
    "package_dir",
]

#: Files ``insight-plugin`` generates from the spec. The steering forbids editing
#: them, so findings against them are not actionable and are dropped.
GENERATED_FILE_NAMES = frozenset(
    {
        "schema.py",
        "__init__.py",
        "setup.py",
        "Dockerfile",
        "Makefile",
        "help.md",
        ".CHECKSUM",
    }
)

#: Directories whose entire contents are generated or vendored.
GENERATED_DIR_NAMES = frozenset({"bin", ".builder", "build", "dist", "__pycache__", ".git"})

#: Finding sources, used as the first segment of a finding's stable key.
SOURCE_COMPILE = "compile"
SOURCE_FORMAT = "format"
SOURCE_PROSPECTOR = "prospector"
SOURCE_TESTS = "tests"
SOURCE_COVERAGE = "coverage"

#: The minimum statement coverage a plugin's unit tests must reach. Stated in the
#: project's own definition of done; enforced here so it is a gate rather than an
#: aspiration.
DEFAULT_COVERAGE_THRESHOLD = 80.0

#: Where a plugin's unit tests live.
_UNIT_TEST_DIR = "unit_test"

#: How far apart two line numbers must be to count as different findings. A fix
#: that shifts a line by a little should not read as a brand-new problem.
_LINE_BUCKET = 5


@dataclass(frozen=True)
class CodeFinding:
    """One actionable defect in hand-written plugin code.

    Attributes:
        source: which check produced it (:data:`SOURCE_COMPILE`,
            :data:`SOURCE_FORMAT`, or :data:`SOURCE_PROSPECTOR`).
        path: the file, relative to the project root and POSIX-separated.
        line: the 1-based line number, or ``None`` when the check is file-level.
        code: a short machine-readable identifier (``syntax-error``,
            ``unused-import``, ``would-reformat``).
        message: the human-readable detail.
    """

    source: str
    path: str
    code: str
    message: str
    line: Optional[int] = None

    @property
    def key(self) -> str:
        """A stable identity used to compare findings between repair rounds.

        The line number is bucketed so a fix that shifts surrounding lines does
        not make every later finding look new -- which would stop the loop from
        ever recognising that it has converged. A genuinely different location
        still lands in a different bucket.
        """
        bucket = "-" if self.line is None else str((self.line // _LINE_BUCKET) * _LINE_BUCKET)
        return f"{self.source}:{self.path}:{bucket}:{self.code}"

    def __str__(self) -> str:  # pragma: no cover - convenience only
        where = f"{self.path}:{self.line}" if self.line is not None else self.path
        return f"{where} [{self.source}/{self.code}] {self.message}"


@dataclass(frozen=True)
class QualityReport:
    """The findings from one quality-gate run.

    Attributes:
        project_dir: the tree that was checked.
        findings: every actionable finding, ordered deterministically.
        checked_files: the hand-written Python files that were considered.
        skipped: notes about checks that could not run (a missing linter, say),
            so a clean report is never mistaken for a check that never happened.
        coverage_percent: the statement coverage actually measured, or ``None``
            when coverage could not be measured at all. Recorded as a figure
            rather than left implicit, because "no coverage finding" is true both
            of a plugin that reached the threshold and of one whose coverage was
            never measured, and a caller deciding whether the plugin is finished
            has to tell those apart.
    """

    project_dir: Path
    findings: Tuple[CodeFinding, ...] = ()
    checked_files: Tuple[str, ...] = ()
    skipped: Tuple[str, ...] = ()
    coverage_percent: Optional[float] = None

    @property
    def clean(self) -> bool:
        """Return ``True`` iff no actionable findings were produced."""
        return not self.findings

    def keys(self) -> Tuple[str, ...]:
        """The stable keys of every finding, sorted."""
        return tuple(sorted(finding.key for finding in self.findings))

    def by_source(self, source: str) -> Tuple[CodeFinding, ...]:
        """The findings produced by one check."""
        return tuple(finding for finding in self.findings if finding.source == source)

    def summary(self) -> str:
        """Return a human-readable one-line summary."""
        if self.clean and not self.skipped:
            return f"No findings across {len(self.checked_files)} hand-written file(s)."
        parts = [f"{len(self.findings)} finding(s)"]
        for source in (SOURCE_COMPILE, SOURCE_FORMAT, SOURCE_PROSPECTOR, SOURCE_TESTS, SOURCE_COVERAGE):
            count = len(self.by_source(source))
            if count:
                parts.append(f"{source}: {count}")
        if self.skipped:
            parts.append(f"skipped: {', '.join(self.skipped)}")
        return "; ".join(parts)

    def render(self, limit: int = 40) -> str:
        """Render the findings as a plain list, for handing to a fixer.

        Args:
            limit: cap the number of lines so a pathological run cannot produce an
                unbounded prompt. Truncation is stated rather than silent.
        """
        lines = [str(finding) for finding in self.findings[:limit]]
        if len(self.findings) > limit:
            lines.append(f"... and {len(self.findings) - limit} more")
        return "\n".join(lines)


def is_generated(relative_path: Union[str, PurePosixPath]) -> bool:
    """Return ``True`` iff ``relative_path`` is a generated or vendored file.

    Findings against these are not actionable: the plugin steering forbids
    hand-editing them, and ``insight-plugin refresh`` would overwrite any edit.
    """
    parts = PurePosixPath(str(relative_path)).parts
    if any(part in GENERATED_DIR_NAMES for part in parts):
        return True
    return bool(parts) and parts[-1] in GENERATED_FILE_NAMES


def is_lint_excluded(relative_path: Union[str, PurePosixPath]) -> bool:
    """Return ``True`` iff ``relative_path`` is outside the linter's remit.

    Everything :func:`is_generated` covers, plus the unit tests. The plugins
    repository's own static-analysis job filters ``unit_test/`` out before running
    prospector, so a test that trips ``protected-access`` for mocking a private
    method is not a defect the plugin has to answer for.

    Deliberately *not* the same predicate as :func:`is_generated`: the unit tests
    are still compiled, still formatted, and still run. Only the linter skips them.
    """
    if is_generated(relative_path):
        return True
    parts = PurePosixPath(str(relative_path)).parts
    return _UNIT_TEST_DIR in parts


def hand_written_python(project_dir: Union[str, Path]) -> Tuple[str, ...]:
    """Return the hand-written ``.py`` files under ``project_dir``, sorted.

    Paths are relative and POSIX-separated. Generated files are excluded per
    :func:`is_generated`.
    """
    root = Path(project_dir)
    if not root.is_dir():
        return ()
    found: List[str] = []
    for path in root.rglob("*.py"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if is_generated(relative):
            continue
        found.append(relative)
    return tuple(sorted(found))


class QualityGate:
    """Runs the fast, correctable checks over a plugin's hand-written code."""

    def __init__(
        self,
        *,
        python_executable: str = "python3",
        black_executable: str = "black",
        prospector_executable: str = "prospector",
        timeout_seconds: float = 300.0,
        run_tests: bool = True,
        coverage_threshold: float = DEFAULT_COVERAGE_THRESHOLD,
        line_length: int = PLUGIN_LINE_LENGTH,
        lint_profile: Optional[LintProfile] = None,
    ) -> None:
        """Configure the gate.

        Args:
            python_executable: interpreter used for the compile check and for
                running the plugin's unit tests. It must be the interpreter that
                has the InsightConnect SDK installed, since a plugin imports the
                SDK at module scope; resolve it with
                :func:`~icplugin_builder.integrations.build_prep.resolve_target_python`
                rather than assuming this process's own interpreter.
            black_executable: the ``black`` binary.
            prospector_executable: the ``prospector`` binary.
            timeout_seconds: per-check ceiling.
            run_tests: whether to run the plugin's unit tests and measure
                coverage. Running them executes generated plugin code in a
                subprocess, so it is switchable for callers that would rather
                leave that to the containerized test stage.
            coverage_threshold: minimum statement coverage percentage.
            line_length: the width ``black`` is checked against. Defaults to
                :data:`~icplugin_builder.integrations.build_prep.PLUGIN_LINE_LENGTH`,
                which is what the plugins repository formats to -- not black's own
                default, which is narrower and would fail correctly formatted code.
            lint_profile: the prospector profile to judge findings against.
                ``None`` resolves it per
                :func:`~icplugin_builder.integrations.build_prep.resolve_lint_profile`
                on each run, preferring the plugins repository's own copy.
        """
        self._python = python_executable
        self._black = black_executable
        self._prospector = prospector_executable
        self._timeout = timeout_seconds
        self._run_tests = run_tests
        self._coverage_threshold = coverage_threshold
        self._line_length = line_length
        self._profile = lint_profile

    async def run(self, project_dir: Union[str, Path]) -> QualityReport:
        """Run every check over ``project_dir`` and collect the findings.

        Args:
            project_dir: the plugin working tree.

        Returns:
            A :class:`QualityReport`. A check whose tool is missing is recorded in
            :attr:`QualityReport.skipped` rather than silently passing.
        """
        root = Path(project_dir)
        files = hand_written_python(root)
        findings: List[CodeFinding] = []
        skipped: List[str] = []

        if files:
            compile_findings, compile_skip = await self._check_compile(root, files)
            findings.extend(compile_findings)
            skipped.extend(compile_skip)

            format_findings, format_skip = await self._check_format(root, files)
            findings.extend(format_findings)
            skipped.extend(format_skip)

        prospector_findings, prospector_skip = await self._check_prospector(root)
        findings.extend(prospector_findings)
        skipped.extend(prospector_skip)

        coverage_percent: Optional[float] = None
        if self._run_tests:
            test_findings, test_skip, coverage_percent = await self._check_tests(root)
            findings.extend(test_findings)
            skipped.extend(test_skip)
        else:
            # Switched off by the caller rather than unavailable, but the
            # reporting consequence is the same: nothing was learned about the
            # tests, so a report with no test findings must not read as the tests
            # having passed.
            skipped.append("tests (not run: the caller disabled them)")

        findings.sort(key=lambda f: (f.path, f.line or 0, f.source, f.code))
        return QualityReport(
            project_dir=root,
            findings=tuple(findings),
            checked_files=files,
            skipped=tuple(skipped),
            coverage_percent=coverage_percent,
        )

    async def _check_compile(self, root: Path, files: Sequence[str]) -> Tuple[List[CodeFinding], List[str]]:
        """Compile each hand-written file, reporting syntax errors by location."""
        findings: List[CodeFinding] = []
        code = (
            "import sys, py_compile\n"
            "for target in sys.argv[1:]:\n"
            "    try:\n"
            "        py_compile.compile(target, cfile=None, doraise=True)\n"
            "    except py_compile.PyCompileError as error:\n"
            "        exc = error.exc_value\n"
            "        line = getattr(exc, 'lineno', 0) or 0\n"
            "        print(f'{target}\\t{line}\\t{type(exc).__name__}: {exc}')\n"
        )
        result = await self._run([self._python, "-c", code, *files], cwd=root)
        if result is None:
            return findings, [f"compile ({self._python} not available)"]

        _, stdout, _ = result
        for line in stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            path, raw_line, message = parts
            findings.append(
                CodeFinding(
                    source=SOURCE_COMPILE,
                    path=path,
                    line=int(raw_line) if raw_line.isdigit() and int(raw_line) > 0 else None,
                    code="syntax-error",
                    message=message.strip(),
                )
            )
        return findings, []

    async def _check_format(self, root: Path, files: Sequence[str]) -> Tuple[List[CodeFinding], List[str]]:
        """Report files ``black`` would reformat."""
        findings: List[CodeFinding] = []
        # --line-length is not optional. A generated plugin carries no
        # pyproject.toml, so black would otherwise apply its own 88-column
        # default, while the plugins repository formats at 120. Without this,
        # correctly formatted plugin code reports as needing reformatting, and the
        # repair loop asks the agent to rewrap it to a width the repository's CI
        # will then object to.
        command = [self._black, "--check", "--quiet", f"--line-length={self._line_length}", *files]
        result = await self._run(command, cwd=root)
        if result is None:
            return findings, [f"format ({self._black} not available)"]

        returncode, stdout, stderr = result
        if returncode == 0:
            return findings, []
        # black names each file it would reformat on stderr; when --quiet
        # suppresses that, fall back to a single tree-level finding.
        named = [token for token in (stderr + stdout).split() if token.endswith(".py") and not is_generated(token)]
        if named:
            for path in sorted(set(named)):
                findings.append(
                    CodeFinding(
                        source=SOURCE_FORMAT,
                        path=path,
                        code="would-reformat",
                        message="black would reformat this file; run black to fix",
                    )
                )
        else:
            findings.append(
                CodeFinding(
                    source=SOURCE_FORMAT,
                    path=".",
                    code="would-reformat",
                    message="black would reformat one or more files; run black to fix",
                )
            )
        return findings, []

    async def _check_prospector(self, root: Path) -> Tuple[List[CodeFinding], List[str]]:
        """Run prospector as the plugins repository runs it, and parse its JSON.

        Both the profile and the explicit tool list matter, and for the same
        reason: without them prospector reports defects against code the
        repository merges without comment, and the repair loop cannot fix any of
        them. See :func:`~icplugin_builder.integrations.build_prep.resolve_lint_profile`
        and :data:`~icplugin_builder.integrations.build_prep.LINT_TOOLS`.
        """
        findings: List[CodeFinding] = []
        skipped: List[str] = []

        command = [self._prospector, "--output-format", "json"]
        profile = self._profile if self._profile is not None else resolve_lint_profile()
        if profile.resolved:
            command.extend(["--profile", str(profile.path)])
            if not profile.is_authoritative:
                skipped.append(f"prospector profile ({profile.detail})")
        else:
            skipped.append(f"prospector profile ({profile.detail})")
        for tool in LINT_TOOLS:
            command.extend(["--tool", tool])

        # Exit status is deliberately ignored: prospector's default is to exit 0
        # even when it reports messages, and the findings come from the JSON body
        # either way.
        result = await self._run(command, cwd=root)
        if result is None:
            return findings, [f"prospector ({self._prospector} not available)"]

        _, stdout, _ = result
        payload = _first_json_object(stdout)
        if payload is None:
            return findings, skipped + ["prospector (output was not parseable JSON)"]

        for message in payload.get("messages", []) or []:
            if not isinstance(message, dict):
                continue
            location = message.get("location") or {}
            path = str(location.get("path", "")).strip()
            if not path or is_lint_excluded(path):
                continue
            raw_line = location.get("line")
            findings.append(
                CodeFinding(
                    source=SOURCE_PROSPECTOR,
                    path=path,
                    line=int(raw_line) if isinstance(raw_line, int) and raw_line > 0 else None,
                    code=str(message.get("code") or "unknown"),
                    message=str(message.get("message") or "").strip(),
                )
            )
        return findings, skipped

    async def _check_tests(self, root: Path) -> Tuple[List[CodeFinding], List[str], Optional[float]]:
        """Run the plugin's unit tests and measure coverage of its package.

        This is the check that makes a failing test *repairable*. Without it a
        broken or stubbed test only surfaces in the containerized test stage at
        export time, by which point the loop that could have fixed it has already
        finished.

        Both a failure and thin coverage are reported as findings rather than as a
        pass/fail verdict, because "test_get_thing failed" and "coverage is 41%"
        are things a fixer can act on.

        Returns:
            The findings, the notes about checks that did not run, and the
            coverage percentage actually measured (``None`` when it was not).
        """
        findings: List[CodeFinding] = []
        if not (root / _UNIT_TEST_DIR).is_dir():
            findings.append(
                CodeFinding(
                    source=SOURCE_TESTS,
                    path=_UNIT_TEST_DIR,
                    code="no-tests",
                    message="no unit_test/ directory; every action needs unit tests",
                )
            )
            return findings, [], None

        package = package_dir(root)
        skipped: List[str] = []
        command = [
            self._python,
            "-m",
            "pytest",
            _UNIT_TEST_DIR,
            "-q",
            "--no-header",
        ]
        # Coverage is only requested when the plugin's interpreter actually has
        # pytest-cov. Passing --cov without it makes pytest reject the whole
        # argument vector, so the tests would not run at all and the failure
        # would be reported as though the plugin were broken.
        measure_coverage = bool(package) and await self._has_pytest_cov()
        if measure_coverage:
            command.extend([f"--cov={package}", "--cov-report=term-missing"])
        elif package:
            skipped.append("coverage (pytest-cov not installed for the plugin interpreter)")

        result = await self._run(command, cwd=root)
        if result is None:
            return findings, [f"tests ({self._python} -m pytest not available)"], None

        returncode, stdout, stderr = result
        combined = stdout + stderr

        if _no_tests_collected(combined):
            findings.append(
                CodeFinding(
                    source=SOURCE_TESTS,
                    path=_UNIT_TEST_DIR,
                    code="no-tests",
                    message="unit_test/ contains no runnable tests",
                )
            )
            return findings, skipped, None

        for path, line, name in _pytest_failures(combined):
            findings.append(
                CodeFinding(
                    source=SOURCE_TESTS,
                    path=path,
                    line=line,
                    # The test name is part of the code so two failures in one
                    # file stay distinct keys; collapsing them would make fixing
                    # one of several read as resolving nothing.
                    code=f"test-failed[{name}]",
                    message=f"unit test {name} failed",
                )
            )

        # A non-zero exit with no parsed failure still means something went wrong
        # (a collection error, say); report it rather than treating it as a pass.
        if returncode != 0 and not findings:
            detail = combined.strip().splitlines()[-1] if combined.strip() else "see output"
            findings.append(
                CodeFinding(
                    source=SOURCE_TESTS,
                    path=_UNIT_TEST_DIR,
                    code="test-run-failed",
                    message=f"the unit test run failed: {detail}",
                )
            )

        percent: Optional[float] = None
        if measure_coverage and package:
            percent = _parse_coverage_total(combined)
            if percent is None:
                # Coverage was asked for and the total did not come back. Saying
                # nothing would leave the absence of a coverage finding looking
                # like a plugin that met the threshold.
                skipped.append("coverage (the coverage total was not reported)")
            elif percent < self._coverage_threshold:
                findings.append(
                    CodeFinding(
                        source=SOURCE_COVERAGE,
                        path=package,
                        code="below-threshold",
                        message=(
                            f"statement coverage is {percent:.0f}%, below the "
                            f"{self._coverage_threshold:.0f}% minimum; add tests for the "
                            "uncovered lines"
                        ),
                    )
                )
        elif not package:
            skipped.append("coverage (no plugin package directory to measure)")
        return findings, skipped, percent

    async def _has_pytest_cov(self) -> bool:
        """Return ``True`` iff the plugin interpreter can import ``pytest_cov``."""
        result = await self._run([self._python, "-c", "import pytest_cov"], cwd=Path.cwd())
        return result is not None and result[0] == 0

    async def _run(self, command: Sequence[str], *, cwd: Path) -> Optional[Tuple[int, str, str]]:
        """Run a check command, returning ``None`` when its tool is unavailable."""
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError):
            return None
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:  # pragma: no cover - already exited
                pass
            return None
        returncode = process.returncode if process.returncode is not None else -1
        return (
            returncode,
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
        )


#: pytest's short summary line, e.g. ``FAILED unit_test/test_api.py::test_bad - ...``.
#: Preferred over scraping the traceback because it is one line per failure and
#: carries the test name.
_PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?\.py)::(\S+?)(?:\s|$)", re.MULTILINE)

#: The ``TOTAL`` row of a coverage term report, e.g. ``TOTAL   4   1   75%``.
_COVERAGE_TOTAL = re.compile(r"^TOTAL\s+.*?(\d+(?:\.\d+)?)%", re.MULTILINE)

#: pytest's wording when a run collected nothing.
_NO_TESTS = ("no tests ran", "no tests collected")


def package_dir(root: Path) -> Optional[str]:
    """Return the plugin's package directory name, or ``None``.

    Coverage is measured against the plugin package specifically. Measuring the
    whole tree would fold the tests themselves into the percentage and make the
    figure meaningless.

    Both eras of prefix are recognised: ``insight-plugin create`` emits
    ``icon_<name>`` and older plugins carry ``komand_<name>``.
    """
    for prefix in ("icon_", "komand_"):
        for candidate in sorted(root.glob(f"{prefix}*")):
            if candidate.is_dir():
                return candidate.name
    return None


def _no_tests_collected(output: str) -> bool:
    """Return ``True`` iff pytest reported collecting no tests."""
    lowered = output.lower()
    return any(phrase in lowered for phrase in _NO_TESTS)


def _pytest_failures(output: str) -> List[Tuple[str, Optional[int], str]]:
    """Parse ``(path, line, test_name)`` for each failing test.

    The test name is carried through because it becomes part of the finding's
    identity. Two failures in the same file must not collapse to one key -- if
    they did, fixing one of three would look like resolving nothing and the repair
    loop would call a stall while it was still making progress.
    """
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


def _first_json_object(text: str) -> Optional[dict]:
    """Extract the first top-level JSON object from ``text``.

    Prospector prints JSON to stdout but tools it wraps may emit warnings around
    it, so the object is located rather than assuming the whole stream parses.
    """
    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass
    start = stripped.find("{")
    if start < 0:
        return None
    depth = 0
    for index in range(start, len(stripped)):
        char = stripped[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(stripped[start : index + 1])
                except ValueError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None
