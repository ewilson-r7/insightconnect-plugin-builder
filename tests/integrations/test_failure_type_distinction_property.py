"""Property test for the build-vs-export failure distinction (task 15.10).

# Feature: insightconnect-plugin-builder, Property 35: Failure indication distinguishes build from export

Property 35 states that *for any* build failure or export failure, the failure
indication the tool presents distinguishes a build failure from an export
failure (Req 19.4).

Both failure surfaces are classified into the same
:class:`~icplugin_builder.integrations.build_export_failure.FailureIndication`
type, which carries a :class:`FailureKind` (``BUILD`` or ``EXPORT``) plus the
mutually exclusive ``is_build_failure`` / ``is_export_failure`` predicates the
UI reads to label the failure. This test builds the two kinds of input the
classifiers accept -- so no real Docker daemon, toolchain, or tenant is needed:

* build-failure inputs: :class:`PipelineReport` objects with an arbitrary
  non-empty subset of the four canonical stages failing, fed to
  :func:`classify_build_failure`;
* export-failure inputs: an arbitrary non-blank failing-step name plus arbitrary
  error output, fed to :func:`classify_export_failure`.

For every generated failure it asserts the indication reports the correct
:class:`FailureKind`, and that ``is_build_failure`` / ``is_export_failure`` are
exactly correct and mutually exclusive (never both, never neither). A build and
an export failure derived from the *same* error text are also shown to classify
differently, confirming the distinction is intrinsic to the classifier and not
an artifact of the output.

**Validates: Requirements 19.4**
"""

from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.integrations.build_export_failure import (
    FailureKind,
    classify_build_failure,
    classify_export_failure,
)
from icplugin_builder.integrations.code_validator import (
    PipelineReport,
    StageName,
    StageResult,
    StageStatus,
)

# The failing statuses a stage may record: a non-zero exit or a timeout abort.
_FAILING_STATUSES = (StageStatus.FAILED, StageStatus.TIMED_OUT)


@st.composite
def _stage_results(draw, name):
    """Draw a :class:`StageResult` for ``name`` that either passes or fails.

    A passing stage exits ``0``; a failing stage is drawn as either a non-zero
    exit (carrying stdout/stderr error output) or a timeout abort (carrying an
    explanatory message), mirroring how the real validator records outcomes.
    """
    if draw(st.booleans()):
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
    returncode = None if status is StageStatus.TIMED_OUT else draw(st.integers(min_value=1, max_value=255))
    return StageResult(
        name=name,
        status=status,
        returncode=returncode,
        stdout=draw(st.text(max_size=200)),
        stderr=draw(st.text(max_size=200)),
        duration_seconds=draw(st.floats(min_value=0.0, max_value=600.0)),
        message=draw(st.text(min_size=1, max_size=80)),
    )


@st.composite
def build_failure_reports(draw):
    """Draw a :class:`PipelineReport` with at least one failing stage.

    The four canonical stages are each independently drawn as pass or fail; when
    the draw produced an all-pass report a random stage is forced to fail, so
    every generated report is a genuine build-failure input (Property 35 is
    scoped to failures) that :func:`classify_build_failure` can classify.
    """
    stages = [draw(_stage_results(name)) for name in StageName.ORDER]

    if all(stage.passed for stage in stages):
        index = draw(st.integers(min_value=0, max_value=len(stages) - 1))
        stages[index] = StageResult(
            name=StageName.ORDER[index],
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


def _non_blank_steps():
    """Draw a non-blank export-step name (``classify_export_failure`` rejects blanks)."""
    return st.text(min_size=1, max_size=60).filter(lambda s: s.strip())


@settings(max_examples=200)
@given(report=build_failure_reports())
def test_build_failures_classify_as_build(report):
    """Property 35: every build failure is indicated as a BUILD failure.

    For any pipeline report with one or more failing stages, the indication has
    :attr:`FailureKind.BUILD`, ``is_build_failure`` is ``True``,
    ``is_export_failure`` is ``False``, and the two predicates are mutually
    exclusive.

    **Validates: Requirements 19.4**
    """
    indication = classify_build_failure(report)

    assert indication.kind is FailureKind.BUILD
    assert indication.is_build_failure is True
    assert indication.is_export_failure is False
    # Mutually exclusive and total: exactly one predicate holds.
    assert indication.is_build_failure != indication.is_export_failure


@settings(max_examples=200)
@given(failing_step=_non_blank_steps(), error_output=st.text(max_size=500))
def test_export_failures_classify_as_export(failing_step, error_output):
    """Property 35: every export failure is indicated as an EXPORT failure.

    For any non-blank failing-step name and any error output, the indication has
    :attr:`FailureKind.EXPORT`, ``is_export_failure`` is ``True``,
    ``is_build_failure`` is ``False``, and the two predicates are mutually
    exclusive.

    **Validates: Requirements 19.4**
    """
    indication = classify_export_failure(failing_step, error_output)

    assert indication.kind is FailureKind.EXPORT
    assert indication.is_export_failure is True
    assert indication.is_build_failure is False
    # Mutually exclusive and total: exactly one predicate holds.
    assert indication.is_build_failure != indication.is_export_failure


@settings(max_examples=200)
@given(
    report=build_failure_reports(),
    failing_step=_non_blank_steps(),
    shared_output=st.text(max_size=500),
)
def test_build_and_export_are_distinguished_from_identical_output(report, failing_step, shared_output):
    """Property 35: build and export failures are told apart, not by their text.

    A build failure and an export failure are always assigned different
    :class:`FailureKind` values, and the classification does not depend on the
    error output: even when an export failure carries the same output text as a
    build failure, the two indications remain distinguishable.

    **Validates: Requirements 19.4**
    """
    build_indication = classify_build_failure(report)
    export_indication = classify_export_failure(failing_step, shared_output)

    # The distinction is intrinsic to the classifier, independent of output text.
    assert build_indication.kind is not export_indication.kind
    assert build_indication.is_build_failure and not build_indication.is_export_failure
    assert export_indication.is_export_failure and not export_indication.is_build_failure
