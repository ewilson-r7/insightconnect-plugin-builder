"""Unit tests for the export-gating decision (task 15.3; Req 7.4, 8.6, 8.7, 22.4).

The gate is a pure conjunction: export is permitted iff the spec is valid and all
four code stages passed (design "Property 17"). These tests build small
``ValidationReport`` / ``PipelineReport`` fixtures by hand -- no Docker daemon or
plugin toolchain is needed -- and assert the permit/block outcome plus the
surfaced remaining errors / failed stages. The exhaustive-conjunction property
test lives in task 15.4.
"""

from pathlib import Path

from icplugin_builder.core.spec_validator import SpecValidationError, ValidationReport
from icplugin_builder.integrations.code_validator import (
    PipelineReport,
    StageName,
    StageResult,
    StageStatus,
)
from icplugin_builder.integrations.export_gate import (
    CODE_NOT_VALIDATED_MESSAGE,
    SPEC_NOT_VALIDATED_MESSAGE,
    ExportGate,
    decide_export,
)


def _stage(name, *, passed=True, message=""):
    """Build a :class:`StageResult` in a passed or failed state."""
    status = StageStatus.PASSED if passed else StageStatus.FAILED
    return StageResult(
        name=name,
        status=status,
        returncode=0 if passed else 1,
        stdout="",
        stderr="",
        duration_seconds=0.0,
        message=message,
    )


def _pipeline(*, all_pass=True, failing=None):
    """Build a full four-stage :class:`PipelineReport`.

    ``failing`` is a set of stage names that should record a fail; the rest pass.
    """
    failing = set(failing or ())
    stages = tuple(_stage(name, passed=name not in failing, message=f"{name} boom") for name in StageName.ORDER)
    docker_available = all_pass or bool(failing)
    return PipelineReport(
        project_dir=Path("/tmp/plugin"),
        stages=stages,
        docker_available=docker_available,
        docker_message="",
    )


def _valid_spec_report():
    return ValidationReport(errors=[], duration_seconds=0.01)


def _invalid_spec_report():
    return ValidationReport(
        errors=[SpecValidationError(path="version", message="version 'x' is invalid")],
        duration_seconds=0.01,
    )


def test_permits_export_when_spec_valid_and_all_stages_pass():
    decision = decide_export(_valid_spec_report(), _pipeline(all_pass=True))

    assert decision.permitted is True
    assert decision.blocked is False
    assert decision.spec_valid is True
    assert decision.code_passed is True
    assert decision.spec_errors == ()
    assert decision.failed_stages == ()
    assert decision.reasons == ()
    assert "permitted" in decision.summary().lower()


def test_blocks_when_spec_invalid_even_if_all_stages_pass():
    decision = decide_export(_invalid_spec_report(), _pipeline(all_pass=True))

    assert decision.permitted is False
    assert decision.spec_valid is False
    assert decision.code_passed is True
    # The remaining spec error is surfaced by path so the operator can fix it.
    assert len(decision.spec_errors) == 1
    assert decision.spec_errors[0].path == "version"
    assert any("version" in reason for reason in decision.reasons)


def test_blocks_when_a_code_stage_fails_even_if_spec_valid():
    decision = decide_export(_valid_spec_report(), _pipeline(failing={StageName.BUILD}))

    assert decision.permitted is False
    assert decision.spec_valid is True
    assert decision.code_passed is False
    failed_names = [stage.name for stage in decision.failed_stages]
    assert failed_names == [StageName.BUILD]
    assert any(StageName.BUILD in reason for reason in decision.reasons)


def test_blocks_and_lists_both_spec_errors_and_failed_stages():
    decision = decide_export(_invalid_spec_report(), _pipeline(failing={StageName.LINT, StageName.TEST}))

    assert decision.permitted is False
    assert decision.spec_valid is False
    assert decision.code_passed is False
    assert len(decision.spec_errors) == 1
    assert [s.name for s in decision.failed_stages] == [StageName.LINT, StageName.TEST]
    summary = decision.summary()
    assert "blocked" in summary.lower()
    assert StageName.LINT in summary and StageName.TEST in summary


def test_blocks_when_spec_report_missing():
    decision = decide_export(None, _pipeline(all_pass=True))

    assert decision.permitted is False
    assert decision.spec_valid is False
    assert decision.code_passed is True
    assert SPEC_NOT_VALIDATED_MESSAGE in decision.reasons


def test_blocks_when_pipeline_report_missing():
    decision = decide_export(_valid_spec_report(), None)

    assert decision.permitted is False
    assert decision.spec_valid is True
    assert decision.code_passed is False
    assert decision.failed_stages == ()
    assert CODE_NOT_VALIDATED_MESSAGE in decision.reasons


def test_blocks_when_pipeline_incomplete_names_missing_stages():
    # Only lint ran (e.g. Docker was absent); the Docker-dependent stages never ran.
    partial = PipelineReport(
        project_dir=Path("/tmp/plugin"),
        stages=(_stage(StageName.LINT, passed=True),),
        docker_available=False,
        docker_message="Docker engine not detected",
    )
    decision = decide_export(_valid_spec_report(), partial)

    assert decision.permitted is False
    assert decision.code_passed is False
    # failed_stages is empty (lint passed) but the decision still explains the gap.
    assert decision.failed_stages == ()
    joined = " ".join(decision.reasons)
    assert StageName.BUILD in joined and StageName.TEST in joined and StageName.VALIDATE in joined


def test_export_gate_facade_matches_pure_function():
    gate = ExportGate()
    spec = _valid_spec_report()
    pipeline = _pipeline(all_pass=True)

    assert gate.decide(spec, pipeline) == decide_export(spec, pipeline)
