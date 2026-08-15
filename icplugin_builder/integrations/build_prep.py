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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple, Union

__all__ = [
    "DEFAULT_SDK_README",
    "SDK_DISTRIBUTION",
    "REQUIRED_TOOLS",
    "SDK_SOURCE_CHANGELOG",
    "SDK_SOURCE_INSTALLED",
    "SdkVersion",
    "ToolStatus",
    "BuildPrepReport",
    "parse_sdk_changelog_version",
    "resolve_sdk_version",
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
