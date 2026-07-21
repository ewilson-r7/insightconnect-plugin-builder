"""Unit tests for build/export failure classification and retention (task 15.9).

These cover specific examples and edge cases for Req 19.1, 19.2, 19.4, and 19.5:

* build failures name the *first* failing validation stage and surface its
  complete error output (Req 19.1), truncated per Req 19.5;
* export failures are distinguished from build failures (Req 19.4);
* a failed export's ``.plg`` is retained with a guaranteed >=24-hour window
  (Req 19.2).

The universal build-vs-export distinction property is covered separately by the
property test (task 15.10, Property 35).
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from icplugin_builder.core.truncation import MAX_DISPLAY_CHARS
from icplugin_builder.integrations.build_export_failure import (
    MINIMUM_EXPORT_RETENTION,
    FailureKind,
    RetainedArtifact,
    classify_build_failure,
    classify_export_failure,
    retain_failed_export_artifact,
)
from icplugin_builder.integrations.code_validator import (
    PipelineReport,
    StageName,
    StageResult,
    StageStatus,
)


def _stage(name, status, *, stdout="", stderr="", message="", returncode=None):
    return StageResult(
        name=name,
        status=status,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.1,
        message=message,
    )


def _report(*stages):
    return PipelineReport(project_dir=Path("/tmp/plugin"), stages=tuple(stages), docker_available=True)


class FakeStore:
    """A minimal :class:`ArtifactStore` capturing what was saved."""

    def __init__(self, base=Path("/tmp/plugin/.builder/artifacts")):
        self.base = Path(base)
        self.saved = {}

    def save_artifact(self, filename: str, content: bytes) -> Path:
        self.saved[filename] = content
        return self.base / filename


class TestClassifyBuildFailure:
    def test_names_first_failing_stage(self):
        report = _report(
            _stage(StageName.LINT, StageStatus.PASSED),
            _stage(StageName.BUILD, StageStatus.FAILED, stderr="docker build boom", returncode=1),
            _stage(StageName.TEST, StageStatus.FAILED, stderr="tests boom", returncode=1),
        )
        indication = classify_build_failure(report)
        assert indication.kind is FailureKind.BUILD
        assert indication.is_build_failure is True
        assert indication.is_export_failure is False
        assert indication.failing_step == StageName.BUILD

    def test_surfaces_complete_error_output(self):
        report = _report(_stage(StageName.LINT, StageStatus.FAILED, stdout="lint out", stderr="lint err", returncode=2))
        indication = classify_build_failure(report)
        # Both captured streams are surfaced in stdout-then-stderr order.
        assert indication.full_output == "lint out\nlint err"
        assert indication.displayed_output == "lint out\nlint err"
        assert indication.truncated is False

    def test_falls_back_to_message_when_streams_empty(self):
        # A timeout abort captures no stdout/stderr; the message carries context.
        report = _report(
            _stage(StageName.LINT, StageStatus.PASSED),
            _stage(
                StageName.TEST,
                StageStatus.TIMED_OUT,
                message="test stage exceeded the 600s limit and was aborted",
            ),
        )
        indication = classify_build_failure(report)
        assert indication.failing_step == StageName.TEST
        assert indication.full_output == "test stage exceeded the 600s limit and was aborted"

    def test_error_output_truncated_at_limit(self):
        big = "e" * (MAX_DISPLAY_CHARS + 250)
        report = _report(_stage(StageName.BUILD, StageStatus.FAILED, stderr=big, returncode=1))
        indication = classify_build_failure(report)
        assert indication.truncated is True
        assert len(indication.displayed_output) == MAX_DISPLAY_CHARS
        assert indication.full_output == big
        assert indication.output.omitted_char_count == 250

    def test_raises_when_no_failing_stage(self):
        report = _report(
            _stage(StageName.LINT, StageStatus.PASSED),
            _stage(StageName.BUILD, StageStatus.PASSED),
        )
        with pytest.raises(ValueError):
            classify_build_failure(report)


class TestClassifyExportFailure:
    def test_distinguished_from_build(self):
        indication = classify_export_failure("tenant upload", "HTTP 409 version conflict")
        assert indication.kind is FailureKind.EXPORT
        assert indication.is_export_failure is True
        assert indication.is_build_failure is False
        assert indication.failing_step == "tenant upload"
        assert indication.full_output == "HTTP 409 version conflict"

    def test_output_truncated(self):
        big = "x" * (MAX_DISPLAY_CHARS + 1)
        indication = classify_export_failure("tenant upload", big)
        assert indication.truncated is True
        assert len(indication.displayed_output) == MAX_DISPLAY_CHARS
        assert indication.full_output == big

    def test_rejects_blank_step(self):
        with pytest.raises(ValueError):
            classify_export_failure("   ", "boom")


class TestRetainFailedExportArtifact:
    def test_stores_artifact_and_returns_path(self):
        store = FakeStore()
        retained = retain_failed_export_artifact(store, "my_plugin-1.2.0.plg", b"PLGBYTES")
        assert store.saved["my_plugin-1.2.0.plg"] == b"PLGBYTES"
        assert retained.path == store.base / "my_plugin-1.2.0.plg"
        assert retained.available_for_retry is True
        assert isinstance(retained, RetainedArtifact)

    def test_retention_window_is_at_least_24_hours(self):
        store = FakeStore()
        start = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        retained = retain_failed_export_artifact(store, "p-1.0.0.plg", b"z", retained_utc=start)
        assert retained.retain_until - retained.retained_at == MINIMUM_EXPORT_RETENTION
        assert retained.retain_until - retained.retained_at >= timedelta(hours=24)

    def test_guaranteed_retained_within_window(self):
        store = FakeStore()
        start = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        retained = retain_failed_export_artifact(store, "p-1.0.0.plg", b"z", retained_utc=start)
        # Any instant within the 24h window is still guaranteed retained.
        assert retained.is_guaranteed_retained_at(start) is True
        assert retained.is_guaranteed_retained_at(start + timedelta(hours=23, minutes=59)) is True
        # At/after the 24h boundary the minimum has been met.
        assert retained.is_guaranteed_retained_at(start + timedelta(hours=24)) is False
        assert retained.is_guaranteed_retained_at(start + timedelta(hours=48)) is False

    def test_naive_timestamp_treated_as_utc(self):
        store = FakeStore()
        start_naive = datetime(2024, 6, 1, 0, 0, 0)
        retained = retain_failed_export_artifact(store, "p-2.0.0.plg", b"z", retained_utc=start_naive)
        assert retained.retained_at == start_naive.replace(tzinfo=timezone.utc)

    def test_rejects_blank_filename(self):
        store = FakeStore()
        with pytest.raises(ValueError):
            retain_failed_export_artifact(store, "  ", b"z")
