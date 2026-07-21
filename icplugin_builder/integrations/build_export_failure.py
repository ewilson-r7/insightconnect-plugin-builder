"""Build- and export-failure classification and failed-export retention (Req 19).

When a plugin's build or export fails, the tool must present a clear, prompt
failure indication (design Property 35; Req 19):

* **Distinguish build from export** (Req 19.4). Every failure carries a
  :class:`FailureKind` (``BUILD`` or ``EXPORT``) so the UI can label it
  unambiguously.
* **Name the failing step and show its complete error output** (Req 19.1). A
  build failure names the failing validation stage (lint/build/test/validate)
  and surfaces the exact output that stage emitted; an export failure names the
  failing export step and surfaces its error output.
* **Bound the displayed output while keeping the full text** (Req 19.5). The
  error output is passed through :func:`truncate_error_output`, so when it
  exceeds 10,000 characters only the first 10,000 are displayed while the
  complete output stays accessible.
* **Retain a failed export's artifact for retry** (Req 19.2). When an export
  fails, the already-built ``.plg`` is retained for at least 24 hours so the
  user can retry the export or download the artifact.

Classification is a pure, in-memory transformation of an already-captured build
report or export error, so it completes far inside the 5-second display budget
of Req 19.1 (the truncation bound also keeps rendering cheap regardless of how
large the raw output is). Retention delegates the actual write to an artifact
store (the plugin's :class:`~icplugin_builder.persistence.project_folder.ProjectFolder`)
and records the guaranteed retain-until instant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, List, Protocol, Union, runtime_checkable

from icplugin_builder.core.truncation import MAX_DISPLAY_CHARS, TruncatedOutput, truncate_error_output

if TYPE_CHECKING:  # pragma: no cover - typing only
    from icplugin_builder.integrations.code_validator import PipelineReport, StageResult

__all__ = [
    "FailureKind",
    "FailureIndication",
    "RetainedArtifact",
    "ArtifactStore",
    "MINIMUM_EXPORT_RETENTION",
    "classify_build_failure",
    "classify_export_failure",
    "retain_failed_export_artifact",
]

#: The minimum time a failed export's ``.plg`` is retained for retry (Req 19.2).
MINIMUM_EXPORT_RETENTION = timedelta(hours=24)

#: Accepted timestamp inputs: a datetime, an ISO-8601 string, or ``None`` (=now).
TimestampInput = Union[datetime, str, None]


class FailureKind(Enum):
    """Whether a failure came from the build or the export phase (Req 19.4)."""

    #: A pre-export validation stage (lint/build/test/validate) failed.
    BUILD = "build"
    #: An export step (local write or tenant upload) failed.
    EXPORT = "export"


@dataclass(frozen=True)
class FailureIndication:
    """A user-facing indication of a build or export failure (Req 19.1, 19.4, 19.5).

    Attributes:
        kind: :class:`FailureKind` -- distinguishes a build failure from an
            export failure (Req 19.4).
        failing_step: The name of the step that failed (a validation stage name
            for build failures, an export-step name for export failures) (Req 19.1).
        output: The failing step's error output as a :class:`TruncatedOutput`;
            ``output.displayed`` is bounded to 10,000 characters while
            ``output.full`` retains the complete text (Req 19.1, 19.5).
    """

    kind: FailureKind
    failing_step: str
    output: TruncatedOutput

    @property
    def is_build_failure(self) -> bool:
        """Return ``True`` iff this indicates a build failure (Req 19.4)."""
        return self.kind is FailureKind.BUILD

    @property
    def is_export_failure(self) -> bool:
        """Return ``True`` iff this indicates an export failure (Req 19.4)."""
        return self.kind is FailureKind.EXPORT

    @property
    def displayed_output(self) -> str:
        """The bounded error output shown to the user (Req 19.1, 19.5)."""
        return self.output.displayed

    @property
    def full_output(self) -> str:
        """The complete error output, always accessible (Req 19.5)."""
        return self.output.full

    @property
    def truncated(self) -> bool:
        """Return ``True`` iff the displayed output is a truncated prefix."""
        return self.output.truncated


@runtime_checkable
class ArtifactStore(Protocol):
    """Minimal interface for storing a retained artifact.

    Satisfied by :class:`~icplugin_builder.persistence.project_folder.ProjectFolder`,
    whose :meth:`save_artifact` writes under ``.builder/artifacts/`` and returns
    the stored path.
    """

    def save_artifact(self, filename: str, content: bytes) -> Path:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True)
class RetainedArtifact:
    """A failed export's retained ``.plg`` and its guaranteed retention window (Req 19.2).

    Attributes:
        filename: The retained artifact's filename (``<name>-<version>.plg``).
        path: The on-disk path the artifact was written to.
        retained_utc: ISO-8601 UTC instant the artifact was retained.
        retain_until_utc: ISO-8601 UTC instant until which retention is
            guaranteed (``retained_utc`` + :data:`MINIMUM_EXPORT_RETENTION`).
        available_for_retry: Whether the artifact is offered for retry/download.
    """

    filename: str
    path: Path
    retained_utc: str
    retain_until_utc: str
    available_for_retry: bool = True

    @property
    def retained_at(self) -> datetime:
        """The retention start instant as an aware UTC datetime."""
        return _parse_iso(self.retained_utc)

    @property
    def retain_until(self) -> datetime:
        """The guaranteed retention deadline as an aware UTC datetime."""
        return _parse_iso(self.retain_until_utc)

    def is_guaranteed_retained_at(self, moment: TimestampInput = None) -> bool:
        """Return ``True`` iff retention is still guaranteed at ``moment``.

        Retention is guaranteed for every instant strictly before
        :attr:`retain_until` (i.e. for the full >=24-hour window after
        :attr:`retained_at`). ``moment`` defaults to the current UTC time.
        """
        return _coerce_datetime(moment) < self.retain_until


def classify_build_failure(report: "PipelineReport", *, limit: int = MAX_DISPLAY_CHARS) -> FailureIndication:
    """Classify a failed validation ``report`` as a build failure (Req 19.1, 19.4, 19.5).

    Selects the first stage that did not pass (stages are in pipeline order:
    lint -> build -> test -> validate), names it as the failing step, and
    surfaces its complete error output truncated per Req 19.5.

    Args:
        report: A completed :class:`PipelineReport` with at least one non-passing
            stage. Only its ``failed_stages`` are read.
        limit: The display truncation limit; defaults to 10,000 (Req 19.5).

    Returns:
        A :class:`FailureIndication` of kind :attr:`FailureKind.BUILD`.

    Raises:
        ValueError: If ``report`` has no failing stage (nothing to classify).
    """
    failed = list(report.failed_stages)
    if not failed:
        raise ValueError("cannot classify a build failure from a report with no failing stage")
    stage = failed[0]
    return FailureIndication(
        kind=FailureKind.BUILD,
        failing_step=stage.name,
        output=truncate_error_output(_stage_error_text(stage), limit),
    )


def classify_export_failure(
    failing_step: str,
    error_output: str,
    *,
    limit: int = MAX_DISPLAY_CHARS,
) -> FailureIndication:
    """Classify an export failure, naming its step and output (Req 19.1, 19.4, 19.5).

    Args:
        failing_step: The name of the export step that failed (e.g.
            ``"tenant upload"`` or ``"local export"``).
        error_output: The complete error output emitted by that step.
        limit: The display truncation limit; defaults to 10,000 (Req 19.5).

    Returns:
        A :class:`FailureIndication` of kind :attr:`FailureKind.EXPORT`.

    Raises:
        ValueError: If ``failing_step`` is empty or blank.
    """
    if not failing_step or not failing_step.strip():
        raise ValueError("failing_step must be a non-empty step name")
    return FailureIndication(
        kind=FailureKind.EXPORT,
        failing_step=failing_step,
        output=truncate_error_output(error_output, limit),
    )


def retain_failed_export_artifact(
    store: ArtifactStore,
    filename: str,
    content: bytes,
    *,
    retained_utc: TimestampInput = None,
) -> RetainedArtifact:
    """Retain a failed export's built ``.plg`` for at least 24 hours (Req 19.2).

    Writes the already-built artifact to ``store`` (the plugin's project folder)
    and records the instant until which retention is guaranteed so the user can
    retry the export or download the artifact.

    Args:
        store: The artifact store to persist into (any object exposing
            ``save_artifact(filename, content) -> Path``).
        filename: The artifact filename (``<plugin_name>-<version>.plg``).
        content: The built ``.plg`` bytes.
        retained_utc: The retention start instant; defaults to the current UTC
            time.

    Returns:
        A :class:`RetainedArtifact` describing the stored artifact and its
        guaranteed >=24-hour retention window.

    Raises:
        ValueError: If ``filename`` is empty or blank.
    """
    if not filename or not filename.strip():
        raise ValueError("filename must be a non-empty artifact name")
    retained = _coerce_datetime(retained_utc)
    path = store.save_artifact(filename, content)
    retain_until = retained + MINIMUM_EXPORT_RETENTION
    return RetainedArtifact(
        filename=filename,
        path=Path(path),
        retained_utc=retained.isoformat(),
        retain_until_utc=retain_until.isoformat(),
        available_for_retry=True,
    )


def _stage_error_text(stage: "StageResult") -> str:
    """Return the complete error output emitted by a failing ``stage`` (Req 19.1).

    Combines the stage's captured ``stdout`` and ``stderr`` (in that order,
    skipping empty streams). When no process output was captured -- e.g. a
    timeout abort or a missing executable -- the stage's explanatory ``message``
    is surfaced instead so the failing step is never presented without context.
    """
    streams: List[str] = [text for text in (stage.stdout, stage.stderr) if text]
    combined = "\n".join(streams)
    if not combined:
        return stage.message or ""
    return combined


def _coerce_datetime(value: TimestampInput) -> datetime:
    """Coerce a timestamp input to an aware UTC datetime (``None`` -> now)."""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return _parse_iso(value)


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 string into an aware UTC datetime.

    A naive value is assumed to already be in UTC.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
