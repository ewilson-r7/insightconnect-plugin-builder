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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import List, Optional, Sequence, Tuple, Union

__all__ = [
    "GENERATED_FILE_NAMES",
    "GENERATED_DIR_NAMES",
    "SOURCE_COMPILE",
    "SOURCE_FORMAT",
    "SOURCE_PROSPECTOR",
    "CodeFinding",
    "QualityReport",
    "QualityGate",
    "is_generated",
    "hand_written_python",
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
    """

    project_dir: Path
    findings: Tuple[CodeFinding, ...] = ()
    checked_files: Tuple[str, ...] = ()
    skipped: Tuple[str, ...] = ()

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
        for source in (SOURCE_COMPILE, SOURCE_FORMAT, SOURCE_PROSPECTOR):
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
    ) -> None:
        """Configure the gate.

        Args:
            python_executable: interpreter used for the compile check.
            black_executable: the ``black`` binary.
            prospector_executable: the ``prospector`` binary.
            timeout_seconds: per-check ceiling.
        """
        self._python = python_executable
        self._black = black_executable
        self._prospector = prospector_executable
        self._timeout = timeout_seconds

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

        findings.sort(key=lambda f: (f.path, f.line or 0, f.source, f.code))
        return QualityReport(
            project_dir=root,
            findings=tuple(findings),
            checked_files=files,
            skipped=tuple(skipped),
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
        result = await self._run([self._black, "--check", "--quiet", *files], cwd=root)
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
        """Run prospector and parse its JSON output into findings."""
        findings: List[CodeFinding] = []
        # Exit status is deliberately ignored: prospector's default is to exit 0
        # even when it reports messages, and the findings come from the JSON body
        # either way.
        result = await self._run([self._prospector, "--output-format", "json"], cwd=root)
        if result is None:
            return findings, [f"prospector ({self._prospector} not available)"]

        _, stdout, _ = result
        payload = _first_json_object(stdout)
        if payload is None:
            return findings, ["prospector (output was not parseable JSON)"]

        for message in payload.get("messages", []) or []:
            if not isinstance(message, dict):
                continue
            location = message.get("location") or {}
            path = str(location.get("path", "")).strip()
            if not path or is_generated(path):
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
        return findings, []

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
