"""Property test for the export-gating conjunction (task 15.4).

# Feature: insightconnect-plugin-builder, Property 17: Export gating equals validation conjunction

Property 17 states that export is permitted *if and only if* the plugin spec is
valid **and** all four code stages (lint/build/test/validate) passed; every
other combination blocks export (design "Property 17"; Req 7.4, 8.6, 8.7, 9.1,
9.4, 22.4).

The gate under test (:func:`~icplugin_builder.integrations.export_gate.decide_export`
and the :class:`~icplugin_builder.integrations.export_gate.ExportGate` facade)
is a pure function of two reports: the ``Spec_Validator``
:class:`ValidationReport` and the ``Code_Validator`` :class:`PipelineReport`.
This test drives it across the full input space by drawing a spec-validity
outcome (valid / invalid / not-run) and, independently, a pass/fail outcome for
each of the four stages (or a not-run pipeline), building the matching report
fixtures by hand -- so no Docker daemon or plugin toolchain is needed -- and
asserting that ``permitted`` equals the conjunction ``spec_valid and
all-four-stages-passed`` for every draw.

**Validates: Requirements 7.4, 8.6, 8.7, 9.1, 9.4, 22.4**
"""

from pathlib import Path
from typing import List

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.spec_validator import SpecValidationError, ValidationReport
from icplugin_builder.integrations.code_validator import (
    PipelineReport,
    StageName,
    StageResult,
    StageStatus,
)
from icplugin_builder.integrations.export_gate import ExportGate, decide_export


def _stage(name: str, *, passed: bool) -> StageResult:
    """Build a passed or failed :class:`StageResult` for ``name``."""
    return StageResult(
        name=name,
        status=StageStatus.PASSED if passed else StageStatus.FAILED,
        returncode=0 if passed else 1,
        stdout="",
        stderr="",
        duration_seconds=0.0,
        message="" if passed else f"{name} boom",
    )


@st.composite
def spec_reports(draw: st.DrawFn):
    """Draw ``(report, spec_valid)`` covering valid, invalid, and not-run specs.

    A ``None`` report models a spec that was never validated (treated as
    invalid by the gate); an invalid report carries one outstanding error.
    """
    choice = draw(st.sampled_from(("valid", "invalid", "missing")))
    if choice == "missing":
        return None, False
    if choice == "valid":
        return ValidationReport(errors=[], duration_seconds=0.0), True
    error = SpecValidationError(path="version", message="version 'x' is invalid")
    return ValidationReport(errors=[error], duration_seconds=0.0), False


@st.composite
def pipeline_reports(draw: st.DrawFn):
    """Draw ``(report, all_passed)`` over the four stage pass/fail outcomes.

    A ``None`` report models a pipeline that was never run (treated as
    not-passed). Otherwise each of the four canonical stages is independently
    drawn pass/fail, so both the all-pass case and every failing subset occur.
    """
    if draw(st.booleans()):
        # Missing pipeline report (never run) roughly 1-in-2 of the time so the
        # not-run branch is well covered alongside concrete stage combinations.
        if draw(st.integers(min_value=0, max_value=4)) == 0:
            return None, False

    outcomes: List[bool] = draw(st.lists(st.booleans(), min_size=len(StageName.ORDER), max_size=len(StageName.ORDER)))
    stages = tuple(_stage(name, passed=passed) for name, passed in zip(StageName.ORDER, outcomes))
    report = PipelineReport(
        project_dir=Path("/tmp/plugin"),
        stages=stages,
        docker_available=True,
        docker_message="",
    )
    return report, all(outcomes)


@settings(max_examples=200)
@given(spec=spec_reports(), pipeline=pipeline_reports())
def test_export_permitted_iff_spec_valid_and_all_stages_pass(spec, pipeline):
    """Property 17: export permitted iff spec valid AND all four stages passed.

    For any combination of a spec-validity outcome and the four stage outcomes,
    ``decide_export`` permits export exactly when the spec is valid and every
    code stage passed, and blocks it (with ``blocked`` the strict inverse) in
    every other case.

    **Validates: Requirements 7.4, 8.6, 8.7, 9.1, 9.4, 22.4**
    """
    spec_report, spec_valid = spec
    pipeline_report, code_passed = pipeline

    decision = decide_export(spec_report, pipeline_report)

    expected = spec_valid and code_passed
    assert decision.permitted is expected
    assert decision.blocked is (not expected)
    # The decision's own booleans agree with the drawn ground truth.
    assert decision.spec_valid is spec_valid
    assert decision.code_passed is code_passed
    # The facade is a pure pass-through and yields the identical decision.
    assert ExportGate().decide(spec_report, pipeline_report) == decision
    # A block always explains why; a permit carries no outstanding reasons.
    if expected:
        assert decision.reasons == ()
    else:
        assert len(decision.reasons) >= 1
