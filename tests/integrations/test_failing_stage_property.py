"""Property test for failing-stage identification in the report (task 15.2).

# Feature: insightconnect-plugin-builder, Property 18: Failing stage is identified in the report

Property 18 states that *for any* pipeline report in which one or more stages
failed, the reported failure identifies each failing stage and its associated
error output (Req 8.5).

The validation pipeline (:class:`~icplugin_builder.integrations.code_validator.CodeValidator`)
aggregates one :class:`StageResult` per stage into a :class:`PipelineReport`.
The report's :attr:`PipelineReport.failed_stages` is the surface a caller uses
to identify which stages failed; each failing :class:`StageResult` carries the
error output that stage emitted (``stdout``/``stderr``) plus an explanatory
``message``. This test builds ``StageResult`` objects directly -- so no real
Docker daemon or plugin toolchain is required -- with an arbitrary subset of the
four canonical stages failing (via non-zero exit or timeout), then asserts:

* every failing stage appears in ``failed_stages`` (identification), and its
  error output is preserved byte-for-byte on the reported result;
* every passing stage is excluded from ``failed_stages``;
* the failure classifier
  (:func:`~icplugin_builder.integrations.build_export_failure.classify_build_failure`)
  names the first failing stage and surfaces its complete error output.

**Validates: Requirements 8.5**
"""

from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.integrations.build_export_failure import FailureKind, classify_build_failure
from icplugin_builder.integrations.code_validator import (
    PipelineReport,
    StageName,
    StageResult,
    StageStatus,
)

# The failing statuses a stage may record: a non-zero exit or a timeout abort.
_FAILING_STATUSES = (StageStatus.FAILED, StageStatus.TIMED_OUT)


@st.composite
def stage_results(draw, name):
    """Draw a :class:`StageResult` for ``name`` that either passes or fails.

    A passing stage exits ``0`` with empty explanatory message. A failing stage
    is drawn as either a non-zero exit (carrying stdout/stderr error output) or a
    timeout abort (carrying an explanatory message), mirroring how the real
    validator records outcomes.
    """
    passed = draw(st.booleans())
    if passed:
        return StageResult(
            name=name,
            status=StageStatus.PASSED,
            returncode=0,
            stdout=draw(st.text(max_size=40)),
            stderr="",
            duration_seconds=draw(st.floats(min_value=0.0, max_value=600.0)),
            message="",
        )

    status = draw(st.sampled_from(_FAILING_STATUSES))
    stdout = draw(st.text(max_size=200))
    stderr = draw(st.text(max_size=200))
    message = draw(st.text(min_size=1, max_size=80))
    returncode = None if status is StageStatus.TIMED_OUT else draw(st.integers(min_value=1, max_value=255))
    return StageResult(
        name=name,
        status=status,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=draw(st.floats(min_value=0.0, max_value=600.0)),
        message=message,
    )


@st.composite
def pipeline_reports(draw):
    """Draw a four-stage :class:`PipelineReport` with an arbitrary failing subset.

    The four canonical stages (lint/build/test/validate) are each independently
    drawn as pass or fail, then we ensure at least one stage fails (Property 18
    is scoped to reports with one or more failures) by forcing a random stage to
    fail when the draw produced an all-pass report.
    """
    stages = [draw(stage_results(name)) for name in StageName.ORDER]

    if all(stage.passed for stage in stages):
        index = draw(st.integers(min_value=0, max_value=len(stages) - 1))
        name = StageName.ORDER[index]
        stages[index] = StageResult(
            name=name,
            status=draw(st.sampled_from(_FAILING_STATUSES)),
            returncode=draw(st.integers(min_value=1, max_value=255)),
            stdout=draw(st.text(min_size=1, max_size=200)),
            stderr=draw(st.text(max_size=200)),
            duration_seconds=1.0,
            message=draw(st.text(min_size=1, max_size=80)),
        )

    return PipelineReport(
        project_dir=Path("/tmp/plugin"),
        stages=tuple(stages),
        docker_available=True,
        docker_message="",
    )


def _error_text(stage):
    """The complete error output a caller can read off a failing stage result."""
    streams = [text for text in (stage.stdout, stage.stderr) if text]
    combined = "\n".join(streams)
    return combined if combined else (stage.message or "")


@settings(max_examples=200)
@given(report=pipeline_reports())
def test_failing_stages_are_identified_with_error_output(report):
    """Property 18: every failing stage is reported with its error output.

    For any report with one or more failures, ``failed_stages`` contains exactly
    the non-passing stages (identification), each failing stage's error output is
    preserved on the reported result, and passing stages never appear.

    **Validates: Requirements 8.5**
    """
    failed = report.failed_stages
    expected_failing = tuple(stage for stage in report.stages if not stage.passed)
    passing = tuple(stage for stage in report.stages if stage.passed)

    # The property's precondition: at least one stage failed.
    assert len(failed) >= 1

    # Identification: failed_stages is exactly the non-passing stages, in order.
    assert failed == expected_failing
    reported_names = [stage.name for stage in failed]
    for stage in expected_failing:
        assert stage.name in reported_names
        # The reported result is the same object carrying the error output, so
        # the failing stage's output is preserved and retrievable.
        assert report.stage(stage.name) is stage
        assert _error_text(report.stage(stage.name)) == _error_text(stage)

    # Passing stages are never reported as failures.
    for stage in passing:
        assert stage not in failed
        assert stage.passed

    # A report with any failure never reports overall success (feeds Req 8.7).
    assert report.passed is False


@settings(max_examples=200)
@given(report=pipeline_reports())
def test_classifier_names_failing_stage_and_surfaces_its_output(report):
    """Property 18 (classifier): the build-failure indication names a failing stage.

    ``classify_build_failure`` selects the first non-passing stage, labels the
    result a build failure, names that stage as the failing step, and surfaces
    its complete error output (the full text is always retained).

    **Validates: Requirements 8.5**
    """
    first_failing = report.failed_stages[0]
    indication = classify_build_failure(report)

    assert indication.kind is FailureKind.BUILD
    assert indication.failing_step == first_failing.name
    # The complete error output of the failing stage is retained in full.
    assert indication.full_output == _error_text(first_failing)
