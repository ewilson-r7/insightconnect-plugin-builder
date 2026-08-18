"""The four-stage pre-export code-validation pipeline (the Code_Validator).

Req 8 (design "Code_Validator": ``run_pipeline(project) -> PipelineReport``)
requires that, before a plugin may be exported, its generated code passes four
independent stages, each recording a pass/fail result:

1. **lint** (Req 8.1) -- static lint checks against the generated plugin code.
   Lint is a pure Python-package check, so it runs even when Docker is absent,
   preserving partial offline validation feedback (design "Docker-optional").
2. **build** (Req 8.2) -- build the plugin container image from its Dockerfile.
3. **test** (Req 8.3) -- run the plugin's unit tests.
4. **validate** (Req 8.4) -- run the ``insight-plugin validate`` operation.

Additional guarantees implemented here:

* The **build** and **test** stages abort at 600 seconds, recording a *timeout*
  fail and surfacing a timeout message (Req 8.8). The lint and validate stages
  are not subject to that abort (per the requirement's wording).
* On **any** failing stage the pipeline still reports every stage's outcome so
  the caller can identify the failing stage and its error output (Req 8.5, feeds
  task 15.2). The validator only *reads* and *runs* the project -- it never
  writes to the working tree -- so a failing run leaves the generated code
  unchanged (Req 8.6). Export gating (Req 8.6/8.7) is a separate decision (task
  15.3) that consumes :attr:`PipelineReport.passed`.
* Docker availability is probed up front (``docker version``). When Docker is
  **absent or not running**, the Docker-dependent stages (build, test, validate)
  record a fail carrying a clear, actionable message rather than partially
  building (design "Docker-optional"; Req 8.6), while lint still runs.

Every external command runs through :func:`asyncio.create_subprocess_exec` so
the caller's event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence, Tuple, Union

from ..core.plugin_files import hand_written_python, is_lint_excluded
from .build_prep import LINT_TOOLS, PLUGIN_LINE_LENGTH, LintProfile, resolve_lint_profile
from .plugin_validate import validate_command
from .quality_gate import first_json_object

__all__ = [
    "StageStatus",
    "StageResult",
    "PipelineReport",
    "DockerProbe",
    "CodeValidator",
    "StageName",
    "DEFAULT_STAGE_TIMEOUT_SECONDS",
    "DEFAULT_DOCKER_PROBE_TIMEOUT_SECONDS",
    "DOCKER_UNAVAILABLE_MESSAGE",
]

#: The build/test stage abort threshold (Req 8.8).
DEFAULT_STAGE_TIMEOUT_SECONDS = 600.0

#: How long the up-front ``docker version`` probe is allowed to run.
DEFAULT_DOCKER_PROBE_TIMEOUT_SECONDS = 15.0

#: The actionable error surfaced on Docker-dependent stages when the engine is
#: absent or not running (design "Docker-optional"; Req 8.6).
DOCKER_UNAVAILABLE_MESSAGE = (
    "Docker engine not detected; start Docker Desktop or the Docker daemon to build and validate the plugin."
)

#: Accepted path inputs.
PathInput = Union[str, Path]


class StageName:
    """The four canonical stage names, in pipeline order."""

    LINT = "lint"
    BUILD = "build"
    TEST = "test"
    VALIDATE = "validate"

    ORDER: Tuple[str, ...] = (LINT, BUILD, TEST, VALIDATE)


class StageStatus(Enum):
    """The outcome of a single pipeline stage."""

    #: The stage's command exited ``0``.
    PASSED = "passed"
    #: The stage's command exited non-zero, its executable was missing, or a
    #: Docker-dependent stage could not run because Docker was unavailable.
    FAILED = "failed"
    #: The stage exceeded its abort threshold and was terminated (Req 8.8).
    TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class StageResult:
    """The captured outcome of one validation stage.

    Attributes:
        name: The stage name (one of :attr:`StageName.ORDER`).
        status: :class:`StageStatus` -- pass, fail, or timeout.
        returncode: The process exit code, or ``None`` when no process ran
            (missing executable, Docker-unavailable, or timeout-killed).
        stdout: Captured standard output (decoded, empty when no process ran).
        stderr: Captured standard error (decoded, empty when no process ran).
        duration_seconds: Wall-clock time the stage ran for.
        message: A human-readable note explaining a non-pass outcome (timeout,
            missing executable, or the actionable Docker error). Empty on pass.
        lint_profile: for the ``lint`` stage, the prospector profile the verdict
            was reached under -- its path and whether it came from the plugins
            repository or this package's vendored copy. ``None`` for every other
            stage. Carried as structured data rather than only in the message so
            the export payload can state which bar produced a finding (clause
            2.8): two operators with different checkouts can still be held to
            different bars, and what changes is that the report says so.
        line_length: for the ``lint`` stage, the column width applied. ``None``
            elsewhere.
    """

    name: str
    status: StageStatus
    returncode: Optional[int]
    stdout: str
    stderr: str
    duration_seconds: float
    message: str = ""
    lint_profile: Optional[LintProfile] = None
    line_length: Optional[int] = None

    @property
    def passed(self) -> bool:
        """Return ``True`` iff this stage recorded a pass result."""
        return self.status is StageStatus.PASSED

    @property
    def timed_out(self) -> bool:
        """Return ``True`` iff this stage was aborted by the timeout (Req 8.8)."""
        return self.status is StageStatus.TIMED_OUT


@dataclass(frozen=True)
class PipelineReport:
    """The aggregate outcome of a four-stage pipeline run.

    Attributes:
        project_dir: The plugin working tree the pipeline ran against.
        stages: One :class:`StageResult` per stage, in pipeline order.
        docker_available: Whether the up-front Docker probe succeeded.
        docker_message: The actionable Docker error when unavailable, else empty.
    """

    project_dir: Path
    stages: Tuple[StageResult, ...]
    docker_available: bool
    docker_message: str = ""

    @property
    def passed(self) -> bool:
        """Return ``True`` iff every stage ran and passed (feeds Req 8.7 gating)."""
        return len(self.stages) == len(StageName.ORDER) and all(stage.passed for stage in self.stages)

    @property
    def failed_stages(self) -> Tuple[StageResult, ...]:
        """The stages that did not pass, in pipeline order (feeds Req 8.5)."""
        return tuple(stage for stage in self.stages if not stage.passed)

    def stage(self, name: str) -> Optional[StageResult]:
        """Return the :class:`StageResult` for ``name`` if present, else ``None``."""
        for stage in self.stages:
            if stage.name == name:
                return stage
        return None


@dataclass(frozen=True)
class DockerProbe:
    """The result of probing for a working Docker engine.

    Attributes:
        available: Whether ``docker version`` succeeded.
        message: The actionable error when unavailable, else empty.
        detail: Raw probe output/diagnostics (for logging), else empty.
    """

    available: bool
    message: str = ""
    detail: str = ""


@dataclass(frozen=True)
class _StageSpec:
    """Internal description of how to run one stage."""

    name: str
    command: Tuple[str, ...]
    requires_docker: bool
    timeout_seconds: Optional[float]


class CodeValidator:
    """Runs the four-stage pre-export validation pipeline (design "Code_Validator").

    The validator is read-only with respect to the plugin working tree: it only
    shells out to lint/build/test/validate commands and records their outcomes,
    so a failing run leaves the generated code unchanged (Req 8.6). Stage
    commands are injectable to keep the pipeline testable without a real Docker
    daemon or toolchain.
    """

    def __init__(
        self,
        *,
        prospector_executable: str = "prospector",
        lint_profile: Optional[LintProfile] = None,
        docker_executable: str = "docker",
        insight_plugin_executable: str = "insight-plugin",
        test_command: Optional[Sequence[str]] = None,
        validate_python_executable: str = "python3",
        stage_timeout_seconds: float = DEFAULT_STAGE_TIMEOUT_SECONDS,
        docker_probe_timeout_seconds: float = DEFAULT_DOCKER_PROBE_TIMEOUT_SECONDS,
    ) -> None:
        """Configure the pipeline.

        Args:
            prospector_executable: The linter the ``lint`` stage runs (offline, no
                Docker). It is prospector rather than ``flake8`` because the
                ``Quality_Gate`` judges the same code with prospector under the
                plugins repository's own profile, and two subsystems applying
                different rules to one plugin is the defect clause 2.6 closes.
            lint_profile: The prospector profile to judge against; resolved from a
                local plugins checkout (or this package's vendored copy) when
                omitted. Pass one explicitly when a verdict must not vary with the
                operator's home directory.
            docker_executable: The ``docker`` binary name/path (build/test/probe).
            insight_plugin_executable: The ``insight-plugin`` binary name/path.
                Retained for callers that override it; the validate stage drives
                ``icon_validator`` directly so it can skip the checks a standalone
                plugin cannot satisfy.
            test_command: Overrides the default in-container unit-test command;
                ``{image}`` is substituted with the built image tag when present.
            validate_python_executable: The interpreter that has the plugin
                toolchain (and therefore ``icon_validator``) installed. Resolve it
                with
                :func:`~icplugin_builder.integrations.build_prep.resolve_target_python`;
                this tool's own interpreter does not have it.
            stage_timeout_seconds: The build/test abort threshold (Req 8.8).
            docker_probe_timeout_seconds: The ``docker version`` probe timeout.
        """
        self._prospector = prospector_executable
        self._configured_profile = lint_profile
        self._resolved_profile: Optional[LintProfile] = None
        self._docker_executable = docker_executable
        self._insight_plugin_executable = insight_plugin_executable
        self._test_command = tuple(test_command) if test_command is not None else None
        self._validate_python = validate_python_executable
        self._stage_timeout_seconds = stage_timeout_seconds
        self._docker_probe_timeout_seconds = docker_probe_timeout_seconds

    async def run_pipeline(self, project: object, *, image_tag: Optional[str] = None) -> PipelineReport:
        """Run lint, build, test, and validate against ``project`` (Req 8.1-8.4, 8.8).

        Probes Docker first; when it is unavailable the Docker-dependent stages
        (build, test, validate) record an actionable fail and only lint runs
        (design "Docker-optional"). The build and test stages abort at the
        configured timeout with a timeout fail (Req 8.8). The working tree is
        never modified, so a failing run leaves the code unchanged (Req 8.6).

        Args:
            project: A plugin working tree -- either a path, or any object with a
                ``root`` attribute (e.g. an
                :class:`~icplugin_builder.integrations.insight_plugin_cli.ProjectTree`).
            image_tag: The Docker image tag for the build/test stages; a stable
                tag is derived from the project directory name when omitted.

        Returns:
            A :class:`PipelineReport` with one :class:`StageResult` per stage.
        """
        root = _resolve_root(project)
        tag = image_tag if image_tag else _derive_image_tag(root)
        probe = await self.probe_docker()

        results = []
        for spec in self._stage_specs(tag, root):
            if spec.requires_docker and not probe.available:
                results.append(
                    StageResult(
                        name=spec.name,
                        status=StageStatus.FAILED,
                        returncode=None,
                        stdout="",
                        stderr="",
                        duration_seconds=0.0,
                        message=probe.message,
                    )
                )
                continue
            results.append(await self._run_stage(spec, root))

        return PipelineReport(
            project_dir=root,
            stages=tuple(results),
            docker_available=probe.available,
            docker_message="" if probe.available else probe.message,
        )

    async def probe_docker(self) -> DockerProbe:
        """Probe for a working Docker engine via ``docker version`` (design "Docker-optional").

        Returns:
            A :class:`DockerProbe`; ``available`` is ``True`` only when the probe
            command exits ``0``. A missing binary, non-zero exit, or probe
            timeout all yield an unavailable result carrying
            :data:`DOCKER_UNAVAILABLE_MESSAGE`.
        """
        command = (self._docker_executable, "version")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as error:
            return DockerProbe(available=False, message=DOCKER_UNAVAILABLE_MESSAGE, detail=str(error))

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=self._docker_probe_timeout_seconds
            )
        except asyncio.TimeoutError:
            await _terminate(process)
            return DockerProbe(available=False, message=DOCKER_UNAVAILABLE_MESSAGE, detail="docker version timed out")

        detail = _decode(stdout_bytes) + _decode(stderr_bytes)
        if process.returncode == 0:
            return DockerProbe(available=True, detail=detail)
        return DockerProbe(available=False, message=DOCKER_UNAVAILABLE_MESSAGE, detail=detail)

    def lint_profile(self) -> LintProfile:
        """The prospector profile this validator judges against, resolved once.

        Memoized so the command the ``lint`` stage runs and the bar its result
        reports cannot disagree: resolution reads the filesystem, and a checkout
        appearing or vanishing between the two would otherwise produce a verdict
        attributed to a profile that did not produce it.
        """
        if self._configured_profile is not None:
            return self._configured_profile
        if self._resolved_profile is None:
            self._resolved_profile = resolve_lint_profile()
        return self._resolved_profile

    def _lint_command(self) -> Tuple[str, ...]:
        """The prospector invocation, matching the ``Quality_Gate``'s exactly.

        The profile and the explicit tool list both matter, and for the same
        reason: left to its defaults prospector also runs ``pycodestyle``, whose
        ``E501`` fires at 79 columns on code the plugins repository formats to 120
        and merges without comment. Naming the width as well means the two
        subsystems and the repository's own CI agree about what clean is.
        """
        command = [self._prospector, "--output-format", "json", "--max-line-length", str(PLUGIN_LINE_LENGTH)]
        profile = self.lint_profile()
        if profile.resolved:
            command.extend(["--profile", str(profile.path)])
        for tool in LINT_TOOLS:
            command.extend(["--tool", tool])
        return tuple(command)

    def _stage_specs(self, image_tag: str, root: Path) -> Tuple[_StageSpec, ...]:
        """Build the ordered stage specs for one run against ``image_tag``."""
        if self._test_command is not None:
            test_command = tuple(part.replace("{image}", image_tag) for part in self._test_command)
        else:
            test_command = (self._docker_executable, "run", "--rm", image_tag, "python", "-m", "pytest", "-q")

        return (
            _StageSpec(
                name=StageName.LINT,
                command=self._lint_command(),
                requires_docker=False,
                timeout_seconds=None,
            ),
            _StageSpec(
                name=StageName.BUILD,
                command=(self._docker_executable, "build", "-t", image_tag, "."),
                requires_docker=True,
                timeout_seconds=self._stage_timeout_seconds,
            ),
            _StageSpec(
                name=StageName.TEST,
                command=test_command,
                requires_docker=True,
                timeout_seconds=self._stage_timeout_seconds,
            ),
            # Drives icon_validator directly rather than shelling `insight-plugin
            # validate`, which always runs every validator and therefore crashes on
            # the one that needs a plugins-repo git clone -- aborting the run and
            # suppressing the failures it had already collected. Docker is still
            # required: DockerValidator builds and runs an image.
            _StageSpec(
                name=StageName.VALIDATE,
                command=validate_command(root, python_executable=self._validate_python),
                requires_docker=True,
                timeout_seconds=None,
            ),
        )

    async def _run_stage(self, spec: _StageSpec, cwd: Path) -> StageResult:
        """Run one stage's command in ``cwd`` and capture its pass/fail outcome.

        A missing executable is a fail with an actionable message; exceeding a
        stage's ``timeout_seconds`` aborts it with a :attr:`StageStatus.TIMED_OUT`
        result (Req 8.8).
        """
        loop = asyncio.get_event_loop()
        start = loop.time()
        try:
            process = await asyncio.create_subprocess_exec(
                *spec.command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as error:
            return StageResult(
                name=spec.name,
                status=StageStatus.FAILED,
                returncode=None,
                stdout="",
                stderr="",
                duration_seconds=loop.time() - start,
                message=f"failed to start {spec.command[0]!r} for the {spec.name} stage: {error}",
            )

        try:
            if spec.timeout_seconds is not None:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=spec.timeout_seconds)
            else:
                stdout_bytes, stderr_bytes = await process.communicate()
        except asyncio.TimeoutError:
            await _terminate(process)
            return StageResult(
                name=spec.name,
                status=StageStatus.TIMED_OUT,
                returncode=process.returncode,
                stdout="",
                stderr="",
                duration_seconds=loop.time() - start,
                message=(f"{spec.name} stage exceeded the {spec.timeout_seconds:.0f}s limit and was aborted"),
            )

        duration = loop.time() - start
        returncode = process.returncode if process.returncode is not None else -1
        stdout = _decode(stdout_bytes)
        stderr = _decode(stderr_bytes)
        if spec.name == StageName.LINT:
            return self._judge_lint(cwd, returncode=returncode, stdout=stdout, stderr=stderr, duration=duration)
        if returncode == 0:
            return StageResult(
                name=spec.name,
                status=StageStatus.PASSED,
                returncode=0,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration,
            )
        return StageResult(
            name=spec.name,
            status=StageStatus.FAILED,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            message=f"{spec.name} stage failed with exit code {returncode}",
        )

    def _judge_lint(
        self,
        root: Path,
        *,
        returncode: int,
        stdout: str,
        stderr: str,
        duration: float,
    ) -> StageResult:
        """Decide the ``lint`` stage from its findings rather than its exit code.

        The exit code cannot carry the verdict: prospector exits ``0`` even when it
        reports messages, so trusting it would pass every tree. Reading the
        findings is also what lets the stage ignore what the plugin's author is
        forbidden to change -- the fourteen messages that blocked an export in the
        originating run were all in generated ``__init__.py`` and ``schema.py``
        files, real and correctly located and unfixable by their audience.

        Findings outside the linter's remit are counted and named but do not fail
        the stage, so the report says what was ignored rather than hiding it.

        Unparseable output is a **fail**: the four-stage gate has no third state,
        and a stage that could not read its linter has established nothing.
        """
        profile = self.lint_profile()
        judged = tuple(path for path in hand_written_python(root) if not is_lint_excluded(path))
        bar = (
            f"{len(judged)} hand-written file(s) judged at {PLUGIN_LINE_LENGTH} columns by "
            f"{self._prospector} under the {profile.source or 'unresolved'} profile {profile.path}"
        )

        payload = first_json_object(stdout)
        if payload is None:
            return StageResult(
                name=StageName.LINT,
                status=StageStatus.FAILED,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration,
                message=f"{self._prospector} produced no parseable JSON, so the lint stage established nothing ({bar})",
                lint_profile=profile,
                line_length=PLUGIN_LINE_LENGTH,
            )

        actionable = []
        ignored = 0
        for message in payload.get("messages", []) or []:
            if not isinstance(message, dict):
                continue
            location = message.get("location") or {}
            path = str(location.get("path", "")).strip()
            if not path or is_lint_excluded(path):
                ignored += 1
                continue
            line = location.get("line")
            code = str(message.get("code") or "unknown")
            detail = str(message.get("message") or "").strip()
            actionable.append(f"{path}:{line if isinstance(line, int) else '?'}: {code}: {detail}")

        ignored_note = f"; {ignored} finding(s) in generated or excluded files ignored" if ignored else ""
        if actionable:
            return StageResult(
                name=StageName.LINT,
                status=StageStatus.FAILED,
                returncode=returncode,
                stdout="\n".join(actionable),
                stderr=stderr,
                duration_seconds=duration,
                message=f"{len(actionable)} lint finding(s) in hand-written code ({bar}{ignored_note})",
                lint_profile=profile,
                line_length=PLUGIN_LINE_LENGTH,
            )
        return StageResult(
            name=StageName.LINT,
            status=StageStatus.PASSED,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            message=f"no lint finding in hand-written code ({bar}{ignored_note})",
            lint_profile=profile,
            line_length=PLUGIN_LINE_LENGTH,
        )


def _resolve_root(project: object) -> Path:
    """Resolve ``project`` to a working-tree path.

    Accepts a path-like value (``str`` / :class:`os.PathLike`) directly, or any
    other object exposing a ``root`` attribute (e.g. a ``ProjectTree``), so the
    validator composes with the CLI wrapper output. Path-likes are checked first
    because :class:`pathlib.Path` itself exposes an unrelated ``root`` anchor.
    """
    if isinstance(project, (str, os.PathLike)):
        return Path(project)
    root = getattr(project, "root", project)
    return Path(root)


def _derive_image_tag(root: Path) -> str:
    """Derive a stable, Docker-legal image tag from the project directory name."""
    name = "".join(char if (char.isalnum() or char in "._-") else "-" for char in root.name.lower())
    name = name.strip("._-") or "plugin"
    return f"icplugin-validate/{name}:latest"


def _decode(raw: bytes) -> str:
    """Decode captured process output as UTF-8, replacing undecodable bytes."""
    return raw.decode("utf-8", errors="replace")


async def _terminate(process: "asyncio.subprocess.Process") -> None:
    """Kill ``process`` and reap it after a wait/timeout abort."""
    try:
        process.kill()
    except ProcessLookupError:  # pragma: no cover - already exited
        return
    try:
        await process.wait()
    except Exception:  # pragma: no cover - best-effort reap
        pass
