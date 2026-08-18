"""Pre-build readiness: resolve the current SDK version and verify the toolchain.

The ``plugin-build-prep`` workflow runs before any plugin is created or changed:
confirm the toolchain is present, and resolve the SDK version to build against
from the SDK's own changelog rather than hardcoding it. This module is the
programmatic half of that step, so the tool can stamp a correct ``sdk.version``
into a spec instead of leaving the field absent.

Absent is what it was. Every plugin this tool produced before this step existed
shipped a spec with no ``sdk`` block at all, which `insight-plugin validate`
rejects.

**Nothing is hardcoded.** The SDK version is read from the top of the
``## Changelog`` section of the SDK repository's ``README.md`` -- the same place
the workflow says to read it -- because that file is updated with each release.
When the SDK checkout is not available, the *installed*
``insightconnect-plugin-runtime`` distribution version is used as a fallback and
labelled as such, so a caller can tell "latest available" from "what happens to
be installed here".
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

__all__ = [
    "DEFAULT_SDK_README",
    "SDK_DISTRIBUTION",
    "REQUIRED_TOOLS",
    "SDK_SOURCE_CHANGELOG",
    "SDK_SOURCE_INSTALLED",
    "SdkVersion",
    "TargetPython",
    "ToolStatus",
    "BuildPrepReport",
    "TARGET_PYTHON_SERIES",
    "PYTHON_SOURCE_PYENV",
    "PYTHON_SOURCE_FALLBACK",
    "DEFAULT_LINT_PROFILE",
    "FALLBACK_LINT_PROFILE",
    "LINT_PROFILE_SOURCE_REPOSITORY",
    "LINT_PROFILE_SOURCE_FALLBACK",
    "LINT_TOOLS",
    "PLUGIN_LINE_LENGTH",
    "LintProfile",
    "SDK_IMPORT_MODULE",
    "TEST_RUNNER_MODULE",
    "InterpreterRejection",
    "TestInterpreter",
    "resolve_test_interpreter",
    "parse_sdk_changelog_version",
    "parse_pyenv_versions",
    "resolve_sdk_version",
    "resolve_target_python",
    "resolve_lint_profile",
    "check_tooling",
    "prepare_build",
]

#: Where the SDK checkout is conventionally cloned. Overridable by the caller;
#: this is only the default lookup location.
DEFAULT_SDK_README = "~/Documents/GitHub/komand-plugin-sdk-python/README.md"

#: The installed distribution that provides the SDK runtime.
SDK_DISTRIBUTION = "insightconnect-plugin-runtime"

#: How a resolved SDK version was obtained.
SDK_SOURCE_CHANGELOG = "changelog"
SDK_SOURCE_INSTALLED = "installed"

#: The Python series the SDK targets. Resolved to a concrete patch version at
#: runtime rather than hardcoded, per the build-prep workflow.
TARGET_PYTHON_SERIES = "3.13"

#: How a target interpreter was resolved.
PYTHON_SOURCE_PYENV = "pyenv"
PYTHON_SOURCE_FALLBACK = "path"

#: The two modules a plugin's unit tests need importable in one interpreter. A
#: plugin imports the SDK at module scope, so an interpreter carrying only
#: ``pytest`` cannot even collect its tests; and one carrying only the SDK cannot
#: run them. Neither alone is usable, which is why this is a conjunction.
SDK_IMPORT_MODULE = "insightconnect_plugin_runtime"
TEST_RUNNER_MODULE = "pytest"

#: Where the plugins repository is conventionally cloned. Its ``prospector.yaml``
#: is the authoritative lint profile: it is what the repository's own CI runs a
#: plugin against, so it decides which findings a plugin must actually answer for.
DEFAULT_LINT_PROFILE = "~/Documents/GitHub/insightconnect-plugins/prospector.yaml"

#: A copy of that profile, used only when the checkout above is absent. Marked as
#: vendored in its own header; the repository's copy wins whenever it is present.
FALLBACK_LINT_PROFILE = Path(__file__).parent / "data" / "prospector-fallback.yaml"

#: How a lint profile was obtained.
LINT_PROFILE_SOURCE_REPOSITORY = "repository"
LINT_PROFILE_SOURCE_FALLBACK = "fallback"

#: The analysers the plugins repository's CI names explicitly when it runs
#: prospector. Naming them matters: prospector's default set also enables
#: ``pycodestyle``, whose ``E501`` fires on lines the repository deliberately
#: allows (its profile disables pylint's ``line-too-long`` and it never runs
#: pycodestyle at all). Left to its defaults, this tool reports style violations
#: against code the repository would merge without comment.
LINT_TOOLS: Tuple[str, ...] = ("bandit", "mccabe", "pylint", "pyflakes")

#: The line length a plugin's code is formatted to. The plugins repository's
#: formatting CI runs ``black --check --line-length 120``; black's own default is
#: 88, and a generated plugin carries no ``pyproject.toml`` to say otherwise, so
#: omitting this makes correctly formatted plugin code report as unformatted.
PLUGIN_LINE_LENGTH = 120

#: The tools a build needs. ``docker`` is required for `insight-plugin validate`
#: (its DockerValidator stage) and for the build/test stages, not for editing a
#: spec, so its absence is reported rather than treated as fatal here.
REQUIRED_TOOLS: Tuple[str, ...] = ("insight-plugin", "prospector", "black", "docker")

#: Matches a changelog bullet's leading version, e.g. ``* 6.6.0 - Disable ...``.
_CHANGELOG_VERSION = re.compile(r"^\s*[-*]\s*v?(\d+\.\d+\.\d+)\b")

#: Matches the changelog heading, at any heading level.
_CHANGELOG_HEADING = re.compile(r"^\s*#+\s*changelog\b", re.IGNORECASE)


@dataclass(frozen=True)
class SdkVersion:
    """A resolved SDK version and where it came from.

    Attributes:
        version: the ``MAJOR.MINOR.PATCH`` version string, or ``None`` when it
            could not be resolved at all.
        source: :data:`SDK_SOURCE_CHANGELOG` when read from the SDK repository's
            changelog (the latest released version), or
            :data:`SDK_SOURCE_INSTALLED` when taken from the installed
            distribution (which may lag behind).
        detail: a human-readable note about how it was resolved, suitable for
            surfacing to the operator.
    """

    version: Optional[str]
    source: Optional[str] = None
    detail: str = ""

    @property
    def resolved(self) -> bool:
        """Return ``True`` iff a version was determined."""
        return self.version is not None

    @property
    def is_latest_known(self) -> bool:
        """Return ``True`` iff the figure came from the SDK changelog."""
        return self.source == SDK_SOURCE_CHANGELOG


@dataclass(frozen=True)
class TargetPython:
    """The interpreter a generated plugin's tests should run under.

    Attributes:
        executable: path to the interpreter, or ``None`` when none was found.
        version: the resolved version when it came from pyenv, else ``None``.
        source: :data:`PYTHON_SOURCE_PYENV` or :data:`PYTHON_SOURCE_FALLBACK`.
        detail: how it was resolved, including why a fallback was used.
    """

    executable: Optional[str]
    version: Optional[str] = None
    source: Optional[str] = None
    detail: str = ""

    @property
    def resolved(self) -> bool:
        """Return ``True`` iff an interpreter was found."""
        return self.executable is not None

    @property
    def is_target_series(self) -> bool:
        """Return ``True`` iff this is a pyenv interpreter in the target series."""
        return self.source == PYTHON_SOURCE_PYENV


@dataclass(frozen=True)
class InterpreterRejection:
    """One candidate interpreter that cannot run a plugin's tests, and why.

    Attributes:
        executable: the candidate that was probed.
        missing: the modules it could not import, from
            :data:`SDK_IMPORT_MODULE` and :data:`TEST_RUNNER_MODULE`. Naming
            *which* is the point: "no interpreter can run the tests" sends an
            operator looking in the wrong place, while "this one has the SDK and no
            pytest, that one the reverse" names the fix.
    """

    executable: str
    missing: Tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.executable} (cannot import {', '.join(self.missing)})"


@dataclass(frozen=True)
class TestInterpreter:
    """The interpreter a plugin's unit tests can actually be run under.

    Attributes:
        executable: the interpreter that can import both modules, or ``None`` when
            no candidate could.
        source: where it came from -- :data:`PYTHON_SOURCE_PYENV`,
            :data:`PYTHON_SOURCE_FALLBACK`, or ``"explicit"`` for a caller-supplied
            candidate list.
        detail: how it was resolved, and for a failure, every candidate tried with
            the imports it lacked.
        rejections: the candidates that were probed and refused, in order.
    """

    executable: Optional[str]
    source: Optional[str] = None
    detail: str = ""
    rejections: Tuple[InterpreterRejection, ...] = ()

    @property
    def resolved(self) -> bool:
        """Return ``True`` iff an interpreter that can run the tests was found."""
        return self.executable is not None


def _missing_imports(
    interpreter: str, modules: Sequence[str] = (SDK_IMPORT_MODULE, TEST_RUNNER_MODULE)
) -> Tuple[str, ...]:
    """Return the modules ``interpreter`` cannot import, in the order probed.

    Every module is probed even after the first failure, because the report is
    supposed to say which of the two is missing rather than only that something is.
    """
    missing = []
    for module in modules:
        if _capture([interpreter, "-c", f"import {module}"]) is None:
            missing.append(module)
    return tuple(missing)


def resolve_test_interpreter(
    candidates: Optional[Sequence[str]] = None,
    series: str = TARGET_PYTHON_SERIES,
) -> TestInterpreter:
    """Resolve an interpreter that can run a generated plugin's unit tests.

    Requires a single interpreter that can import **both** the InsightConnect SDK
    and ``pytest``. That conjunction is the whole reason this exists rather than
    :func:`resolve_target_python` being enough: on the host that motivated it the
    two were split -- the SDK lived in
    ``/Library/Developer/CommandLineTools/usr/bin/python3`` (3.9) with no pytest,
    and the project's own virtualenv had pytest and no SDK. Neither can run a
    plugin's tests, and a resolver that returns the first plausible interpreter
    without probing reports that as the plugin's failure instead of the host's.

    Candidates are tried in order of how likely they are to be the plugin's own
    environment: the pyenv interpreter in the target series first, then this
    process's own, then ``python3`` on ``PATH``.

    Args:
        candidates: an explicit candidate list, which overrides the default order.
            Used by callers that already know where to look, and by tests.
        series: the target version series for the pyenv candidate.

    Returns:
        A :class:`TestInterpreter`. When nothing qualifies, ``resolved`` is
        ``False`` and ``detail`` names every candidate with the imports it lacked,
        so the report can say why the tests could not run rather than that they
        failed. **``pytest`` is never installed to make a candidate qualify**
        (SCOPE-12): its absence is reported, not remedied.
    """
    if candidates is not None:
        ordered = [str(candidate) for candidate in candidates]
        source = "explicit"
    else:
        target = resolve_target_python(series)
        ordered = [
            candidate
            for candidate in (
                target.executable if target.is_target_series else None,
                sys.executable,
                shutil.which("python3"),
            )
            if candidate
        ]
        # Preserve order while dropping the duplicates the three sources produce
        # when they happen to agree.
        ordered = list(dict.fromkeys(ordered))
        source = PYTHON_SOURCE_PYENV if target.is_target_series else PYTHON_SOURCE_FALLBACK

    if not ordered:
        return TestInterpreter(executable=None, detail="no Python interpreter could be found to run the tests with")

    rejections: List[InterpreterRejection] = []
    for candidate in ordered:
        missing = _missing_imports(candidate)
        if not missing:
            return TestInterpreter(
                executable=candidate,
                source=source,
                detail=f"{candidate} can import both {SDK_IMPORT_MODULE} and {TEST_RUNNER_MODULE}",
                # Carried on success too: knowing which earlier candidates lost, and
                # to which missing import, is what tells an operator whether the
                # interpreter they expected to be used was passed over and why.
                rejections=tuple(rejections),
            )
        rejections.append(InterpreterRejection(executable=candidate, missing=missing))

    return TestInterpreter(
        executable=None,
        source=source,
        detail=(
            f"no interpreter can import both {SDK_IMPORT_MODULE} and {TEST_RUNNER_MODULE}; "
            f"rejected: {'; '.join(str(rejection) for rejection in rejections)}"
        ),
        rejections=tuple(rejections),
    )


@dataclass(frozen=True)
class LintProfile:
    """The prospector profile a plugin's code should be judged against.

    Attributes:
        path: the profile file to pass to ``prospector --profile``, or ``None``
            when none could be found at all.
        source: :data:`LINT_PROFILE_SOURCE_REPOSITORY` when it came from a local
            plugins checkout (authoritative), or
            :data:`LINT_PROFILE_SOURCE_FALLBACK` when it came from this package's
            vendored copy, which may be stale.
        detail: how it was resolved, for surfacing to the operator.
    """

    path: Optional[str]
    source: Optional[str] = None
    detail: str = ""

    @property
    def resolved(self) -> bool:
        """Return ``True`` iff a profile was found."""
        return self.path is not None

    @property
    def is_authoritative(self) -> bool:
        """Return ``True`` iff the profile came from the plugins repository itself."""
        return self.source == LINT_PROFILE_SOURCE_REPOSITORY


@dataclass(frozen=True)
class ToolStatus:
    """Whether one required tool is present, and its reported version.

    Attributes:
        name: the executable name.
        path: the resolved absolute path, or ``None`` when not on ``PATH``.
        version: the tool's self-reported version line, when it could be read.
    """

    name: str
    path: Optional[str]
    version: str = ""

    @property
    def present(self) -> bool:
        """Return ``True`` iff the executable was found on ``PATH``."""
        return self.path is not None


@dataclass(frozen=True)
class BuildPrepReport:
    """The outcome of the pre-build readiness check.

    Attributes:
        sdk: the resolved SDK version and its provenance.
        tools: one :class:`ToolStatus` per entry in :data:`REQUIRED_TOOLS`.
    """

    sdk: SdkVersion
    tools: Dict[str, ToolStatus] = field(default_factory=dict)

    @property
    def missing_tools(self) -> Tuple[str, ...]:
        """The required tools that are not on ``PATH``, in declaration order."""
        return tuple(name for name in REQUIRED_TOOLS if name in self.tools and not self.tools[name].present)

    @property
    def ready(self) -> bool:
        """Return ``True`` iff an SDK version resolved and every tool is present."""
        return self.sdk.resolved and not self.missing_tools

    def summary(self) -> str:
        """Return a one-line human-readable readiness summary."""
        parts = []
        if self.sdk.resolved:
            parts.append(f"SDK {self.sdk.version} ({self.sdk.source})")
        else:
            parts.append("SDK version unresolved")
        missing = self.missing_tools
        parts.append("all tools present" if not missing else f"missing: {', '.join(missing)}")
        return "; ".join(parts)


def parse_sdk_changelog_version(readme_text: str) -> Optional[str]:
    """Return the newest version listed under the README's ``## Changelog``.

    The changelog is maintained newest-first, so the first version bullet after
    the heading is the latest release. Only that section is considered -- a
    version-shaped string elsewhere in the README (an install example, say) must
    not be mistaken for a release.

    Args:
        readme_text: the full text of the SDK repository's ``README.md``.

    Returns:
        The newest ``MAJOR.MINOR.PATCH`` string, or ``None`` when the changelog
        section is absent or contains no recognizable version bullet.
    """
    in_changelog = False
    for line in readme_text.splitlines():
        if _CHANGELOG_HEADING.match(line):
            in_changelog = True
            continue
        if not in_changelog:
            continue
        # A new heading ends the changelog section without a match.
        if line.lstrip().startswith("#"):
            return None
        match = _CHANGELOG_VERSION.match(line)
        if match:
            return match.group(1)
    return None


def _installed_sdk_version() -> Optional[str]:
    """Return the installed SDK distribution version, or ``None``."""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - stdlib on supported versions
        return None
    try:
        return version(SDK_DISTRIBUTION)
    except PackageNotFoundError:
        return None


def resolve_sdk_version(sdk_readme: Optional[Union[str, Path]] = None) -> SdkVersion:
    """Resolve the SDK version to build against.

    Prefers the SDK repository's changelog (the latest released version) and
    falls back to the installed distribution, recording which was used so the
    difference is visible rather than silently assumed.

    Args:
        sdk_readme: path to the SDK repository's ``README.md``. Defaults to
            :data:`DEFAULT_SDK_README`.

    Returns:
        An :class:`SdkVersion`; ``resolved`` is ``False`` when neither source
        yielded a version.
    """
    candidate = Path(sdk_readme if sdk_readme is not None else DEFAULT_SDK_README).expanduser()
    if candidate.is_file():
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            text = ""
            detail = f"could not read {candidate}: {error}"
        else:
            detail = ""
        if text:
            parsed = parse_sdk_changelog_version(text)
            if parsed:
                return SdkVersion(
                    version=parsed,
                    source=SDK_SOURCE_CHANGELOG,
                    detail=f"read from the changelog in {candidate}",
                )
            detail = f"no version bullet found under '## Changelog' in {candidate}"
    else:
        detail = f"SDK README not found at {candidate}"

    installed = _installed_sdk_version()
    if installed:
        return SdkVersion(
            version=installed,
            source=SDK_SOURCE_INSTALLED,
            detail=(f"{detail}; using the installed {SDK_DISTRIBUTION} version, which may lag the latest release"),
        )
    return SdkVersion(version=None, detail=detail)


def resolve_lint_profile(profile: Optional[Union[str, Path]] = None) -> LintProfile:
    """Resolve the prospector profile to judge a plugin's code against.

    Prefers the ``prospector.yaml`` in a local ``insightconnect-plugins``
    checkout, because that file *is* the bar: it is what the repository's CI
    applies to a plugin, so a finding it disables is a finding the plugin will
    never have to answer for. Falls back to this package's vendored copy and says
    so, rather than silently running prospector bare -- bare prospector reports
    ``bad-super-call`` and ``dangerous-default-value`` against the scaffolder's own
    templates, which the steering forbids editing, so those findings can never be
    resolved.

    Discovery rather than a vendored constant, for the same reason
    :func:`resolve_sdk_version` reads the SDK's changelog: a second copy of
    someone else's rules drifts from the original, and then the two disagree about
    what "clean" means.

    Args:
        profile: an explicit profile path. Defaults to
            :data:`DEFAULT_LINT_PROFILE`.

    Returns:
        A :class:`LintProfile`; ``resolved`` is ``False`` only when even the
        vendored copy is missing.
    """
    candidate = Path(profile if profile is not None else DEFAULT_LINT_PROFILE).expanduser()
    if candidate.is_file():
        return LintProfile(
            path=str(candidate),
            source=LINT_PROFILE_SOURCE_REPOSITORY,
            detail=f"using the plugins repository's own profile at {candidate}",
        )

    if FALLBACK_LINT_PROFILE.is_file():
        return LintProfile(
            path=str(FALLBACK_LINT_PROFILE),
            source=LINT_PROFILE_SOURCE_FALLBACK,
            detail=(
                f"no plugins checkout at {candidate}; using the vendored copy at "
                f"{FALLBACK_LINT_PROFILE}, which may lag the repository"
            ),
        )

    return LintProfile(
        path=None,
        detail=(
            f"no lint profile found: neither {candidate} nor the vendored copy at " f"{FALLBACK_LINT_PROFILE} exists"
        ),
    )


async def check_tooling(tools: Sequence[str] = REQUIRED_TOOLS) -> Dict[str, ToolStatus]:
    """Report whether each required tool is on ``PATH``, with its version.

    Args:
        tools: executables to check; defaults to :data:`REQUIRED_TOOLS`.

    Returns:
        A mapping of tool name to :class:`ToolStatus`.
    """
    statuses: Dict[str, ToolStatus] = {}
    for name in tools:
        path = shutil.which(name)
        if path is None:
            statuses[name] = ToolStatus(name=name, path=None)
            continue
        statuses[name] = ToolStatus(name=name, path=path, version=await _tool_version(path))
    return statuses


async def _tool_version(executable: str) -> str:
    """Return an executable's reported version line, or an empty string."""
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError):  # pragma: no cover - platform dependent
        return ""
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15.0)
    except asyncio.TimeoutError:  # pragma: no cover - slow tool
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return ""
    text = (stdout or b"").decode("utf-8", errors="replace") or (stderr or b"").decode("utf-8", errors="replace")
    return text.strip().splitlines()[0] if text.strip() else ""


def parse_pyenv_versions(output: str, series: str = TARGET_PYTHON_SERIES) -> Optional[str]:
    """Return the newest installed ``pyenv`` version in ``series``.

    Args:
        output: the raw output of ``pyenv versions --bare``.
        series: the version prefix to match, e.g. ``"3.13"``.

    Returns:
        The highest matching version string, or ``None`` when none is installed.
        Comparison is numeric per segment, so ``3.13.10`` sorts above ``3.13.9``.
    """
    candidates = []
    for line in output.splitlines():
        version = line.strip().lstrip("*").strip()
        # `pyenv versions --bare` can include virtualenv names such as
        # "3.13.3/envs/foo"; only plain interpreter versions are usable here.
        if "/" in version or not version.startswith(f"{series}."):
            continue
        parts = version.split(".")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            continue
        candidates.append((tuple(int(part) for part in parts), version))
    if not candidates:
        return None
    return max(candidates)[1]


def resolve_target_python(series: str = TARGET_PYTHON_SERIES) -> TargetPython:
    """Resolve the interpreter a generated plugin's tests should run under.

    The plugin workflow pins tooling to the SDK's target Python series and says
    to resolve the installed patch version with ``pyenv versions`` rather than
    hardcoding it. That matters here for a practical reason as well: the SDK
    runtime a plugin imports is installed in that interpreter, not in this tool's
    own environment, so running a plugin's tests with ``sys.executable`` would
    fail on the import rather than on anything about the plugin.

    Args:
        series: the target version series; defaults to
            :data:`TARGET_PYTHON_SERIES`.

    Returns:
        A :class:`TargetPython`. When no pyenv interpreter in the series is
        available it falls back to ``python3`` on ``PATH`` and says so, so a
        caller can report that tests ran under an unverified interpreter rather
        than silently trusting the result.
    """
    pyenv = shutil.which("pyenv")
    if pyenv is not None:
        result = _capture([pyenv, "versions", "--bare"])
        if result is not None:
            version = parse_pyenv_versions(result, series)
            if version:
                candidate = Path.home() / ".pyenv" / "versions" / version / "bin" / "python"
                if candidate.is_file():
                    return TargetPython(
                        executable=str(candidate),
                        version=version,
                        source=PYTHON_SOURCE_PYENV,
                        detail=f"resolved {version} via pyenv",
                    )

    fallback = shutil.which("python3")
    if fallback:
        return TargetPython(
            executable=fallback,
            version=None,
            source=PYTHON_SOURCE_FALLBACK,
            detail=(
                f"no pyenv {series}.x interpreter found; falling back to {fallback}, "
                "which may not have the InsightConnect SDK installed"
            ),
        )
    return TargetPython(executable=None, version=None, source=None, detail="no usable Python interpreter found")


def _capture(command: Sequence[str]) -> Optional[str]:
    """Run ``command`` and return its stdout, or ``None`` on any failure.

    Synchronous on purpose. This is called from application startup, which is a
    sync context; an async variant would force an ``asyncio.run`` there and break
    if a loop were already running. :func:`resolve_sdk_version` is sync for the
    same reason.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            list(command),
            capture_output=True,
            timeout=15.0,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):  # pragma: no cover - platform dependent
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace")


async def prepare_build(sdk_readme: Optional[Union[str, Path]] = None) -> BuildPrepReport:
    """Run the pre-build readiness check.

    Args:
        sdk_readme: path to the SDK repository's ``README.md``; defaults to
            :data:`DEFAULT_SDK_README`.

    Returns:
        A :class:`BuildPrepReport` carrying the resolved SDK version and the
        status of each required tool.
    """
    return BuildPrepReport(sdk=resolve_sdk_version(sdk_readme), tools=await check_tooling())
