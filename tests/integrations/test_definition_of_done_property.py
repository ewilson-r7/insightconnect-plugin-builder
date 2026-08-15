"""
# Feature: insightconnect-plugin-builder, Property 57: An unmet condition is never reported as done

Property 57 states that for *any* combination of ``Definition_Of_Done``
condition results, the plugin is reported complete if and only if every
condition is met; each unmet condition is named; and a condition that could not
be evaluated is reported as unverified rather than met (design "Property 57";
Req 27.1, 27.2, 27.3, 27.5).

This is the property the original tool would have failed. It reported success
from a pipeline that had recorded four stage results, on plugins whose Python did
not parse and whose ``connection.test()`` was a bare ``pass`` -- because no
component was ever asked the conjunction, and a check that had not run was
indistinguishable from one that had passed.

Two tests here, from opposite directions:

* the tagged property drives the whole three-way status space across every
  condition and asserts ``complete`` equals "all met", that each shortfall is
  named, and that dropping a condition altogether cannot read as done;
* a second property drives :func:`evaluate_done` itself with drawn quality-gate
  and pipeline reports, so the *mapping* from tool output to condition status is
  covered too -- in particular that a skipped check becomes ``UNVERIFIED`` and
  never ``MET``.

Both build their report fixtures by hand, so neither needs a plugin toolchain,
Docker, or a working tree.

**Validates: Requirements 27.1, 27.2, 27.3, 27.5**
"""

from pathlib import Path
from typing import Dict, List, Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.integrations.code_validator import (
    PipelineReport,
    StageName,
    StageResult,
    StageStatus,
)
from icplugin_builder.integrations.definition_of_done import (
    CONDITION_CODE_PARSES,
    CONDITION_COVERAGE,
    CONDITION_DESCRIPTIONS,
    CONDITION_FORMATTED,
    CONDITION_LINT_CLEAN,
    CONDITION_ORDER,
    CONDITION_TOOLCHAIN_VALIDATE,
    CONDITION_UNIT_TESTS,
    ConditionResult,
    ConditionStatus,
    DoneReport,
    evaluate_done,
)
from icplugin_builder.integrations.quality_gate import (
    DEFAULT_COVERAGE_THRESHOLD,
    SOURCE_COMPILE,
    SOURCE_COVERAGE,
    SOURCE_FORMAT,
    SOURCE_PROSPECTOR,
    SOURCE_TESTS,
    CodeFinding,
    QualityReport,
)

#: A tree that does not exist, so the structural conditions are unverifiable and
#: the drawn report inputs are the only thing under test.
_ABSENT_TREE = Path("/nonexistent/plugin-tree-for-property-57")


@st.composite
def condition_sets(draw: st.DrawFn):
    """Draw a full set of condition results, one drawn status per condition.

    An all-met draw is offered explicitly rather than left to chance. Drawing
    eleven independent statuses lands on all-met about once in 177,000 examples,
    which would leave the "reported complete" half of the biconditional untested
    -- the half that matters most, since over-reporting done is the failure this
    property exists to catch.
    """
    if draw(st.booleans()):
        statuses = [ConditionStatus.MET] * len(CONDITION_ORDER)
    else:
        statuses = draw(
            st.lists(
                st.sampled_from(list(ConditionStatus)),
                min_size=len(CONDITION_ORDER),
                max_size=len(CONDITION_ORDER),
            )
        )
    return tuple(
        ConditionResult(
            name=name,
            status=status,
            description=CONDITION_DESCRIPTIONS[name],
            detail="" if status is ConditionStatus.MET else f"{name} did not hold",
        )
        for name, status in zip(CONDITION_ORDER, statuses)
    )


# Feature: insightconnect-plugin-builder, Property 57: An unmet condition is never reported as done
@settings(max_examples=300)
@given(conditions=condition_sets(), drop=st.integers(min_value=-1, max_value=len(CONDITION_ORDER) - 1))
def test_a_plugin_is_reported_complete_iff_every_condition_is_met(conditions, drop):
    """``complete`` equals the conjunction, and every shortfall is named."""
    kept = tuple(c for index, c in enumerate(conditions) if index != drop)
    report = DoneReport(project_dir=_ABSENT_TREE, conditions=kept)

    every_condition_met = all(c.status is ConditionStatus.MET for c in conditions)
    nothing_dropped = drop == -1

    # The conjunction, both directions. A dropped condition is not evaluated, so
    # it cannot be met either -- a partial report never reads as done (Req 27.3).
    assert report.complete is (every_condition_met and nothing_dropped)

    # Req 27.5: an unverified condition is reported as unverified, and is never
    # counted among the met ones.
    for condition in report.unverified:
        assert condition.status is ConditionStatus.UNVERIFIED
        assert not condition.met
    assert set(report.unmet).isdisjoint(report.unverified)

    # Req 27.2: every outstanding condition is named in the summary, and nothing
    # calls an incomplete plugin complete (Req 27.3).
    summary = report.summary()
    if report.complete:
        assert "complete" in summary
    else:
        for condition in report.outstanding:
            assert condition.name in summary
        for name in report.missing_conditions:
            assert name in summary
        assert "not complete" in summary
        assert condition_names_absent(summary)

    # Every condition carries its reason, so "unmet" is actionable rather than a verdict.
    for condition in report.outstanding:
        assert condition.detail


def condition_names_absent(summary: str) -> bool:
    """Return ``True`` iff ``summary`` makes no claim of readiness or success."""
    lowered = summary.lower()
    return "is complete" not in lowered and "ready" not in lowered and "successful" not in lowered


def _quality_report(outcomes: Dict[str, str], *, coverage_percent: Optional[float]) -> QualityReport:
    """Build a :class:`QualityReport` whose every check has a drawn outcome.

    ``outcomes`` maps a quality-gate source to ``"clean"``, ``"finding"``, or
    ``"skipped"``.
    """
    findings: List[CodeFinding] = []
    skipped: List[str] = []
    for source, outcome in outcomes.items():
        if outcome == "finding":
            findings.append(CodeFinding(source=source, path="icon_x/thing.py", code="c", message="m", line=1))
        elif outcome == "skipped":
            skipped.append(f"{source} (its tool is not available)")
    return QualityReport(
        project_dir=_ABSENT_TREE,
        findings=tuple(findings),
        checked_files=("icon_x/thing.py",),
        skipped=tuple(skipped),
        coverage_percent=coverage_percent,
    )


def _pipeline(outcome: str) -> Optional[PipelineReport]:
    """Build a pipeline report whose validate stage passed, failed, or never ran."""
    if outcome == "missing":
        return None
    if outcome == "passed":
        status, returncode = StageStatus.PASSED, 0
    elif outcome == "failed":
        status, returncode = StageStatus.FAILED, 1
    else:  # never ran a process at all (Docker absent, CLI missing, killed)
        status, returncode = StageStatus.FAILED, None
    stages = tuple(
        StageResult(
            name=name,
            status=status if name == StageName.VALIDATE else StageStatus.PASSED,
            returncode=returncode if name == StageName.VALIDATE else 0,
            stdout="",
            stderr="",
            duration_seconds=0.0,
            message="docker absent" if returncode is None and name == StageName.VALIDATE else "",
        )
        for name in StageName.ORDER
    )
    return PipelineReport(project_dir=_ABSENT_TREE, stages=stages, docker_available=True)


@settings(max_examples=300)
@given(
    compile_outcome=st.sampled_from(("clean", "finding", "skipped")),
    format_outcome=st.sampled_from(("clean", "finding", "skipped")),
    lint_outcome=st.sampled_from(("clean", "finding", "skipped")),
    tests_outcome=st.sampled_from(("clean", "finding", "skipped")),
    coverage_percent=st.one_of(st.none(), st.floats(min_value=0.0, max_value=100.0)),
    validate_outcome=st.sampled_from(("passed", "failed", "never_ran", "missing")),
    quality_supplied=st.booleans(),
)
def test_evaluate_done_maps_every_tool_outcome_without_ever_inventing_a_pass(
    compile_outcome,
    format_outcome,
    lint_outcome,
    tests_outcome,
    coverage_percent,
    validate_outcome,
    quality_supplied,
):
    """A check that did not run comes back unverified, never met (Req 27.4, 27.5)."""
    outcomes = {
        SOURCE_COMPILE: compile_outcome,
        SOURCE_FORMAT: format_outcome,
        SOURCE_PROSPECTOR: lint_outcome,
        SOURCE_TESTS: tests_outcome,
        SOURCE_COVERAGE: "clean",
    }
    quality = _quality_report(outcomes, coverage_percent=coverage_percent) if quality_supplied else None

    report = evaluate_done(
        _ABSENT_TREE,
        quality_report=quality,
        pipeline_report=_pipeline(validate_outcome),
        coverage_threshold=DEFAULT_COVERAGE_THRESHOLD,
    )

    # Every condition is evaluated on every run: the gate is the whole answer, so
    # it never returns a report that quietly omits one.
    assert report.missing_conditions == ()
    assert len(report.conditions) == len(CONDITION_ORDER)

    expected = {
        CONDITION_CODE_PARSES: compile_outcome,
        CONDITION_FORMATTED: format_outcome,
        CONDITION_LINT_CLEAN: lint_outcome,
        CONDITION_UNIT_TESTS: tests_outcome,
    }
    for name, outcome in expected.items():
        condition = report.condition(name)
        assert condition is not None
        if not quality_supplied:
            assert condition.status is ConditionStatus.UNVERIFIED
        elif outcome == "finding":
            assert condition.status is ConditionStatus.UNMET
        elif outcome == "skipped":
            assert condition.status is ConditionStatus.UNVERIFIED
        else:
            assert condition.status is ConditionStatus.MET

    # Coverage rests on the measured figure, not on the absence of a finding.
    coverage = report.condition(CONDITION_COVERAGE)
    assert coverage is not None
    if not quality_supplied or coverage_percent is None:
        assert coverage.status is ConditionStatus.UNVERIFIED
    elif coverage_percent < DEFAULT_COVERAGE_THRESHOLD:
        assert coverage.status is ConditionStatus.UNMET
    else:
        assert coverage.status is ConditionStatus.MET

    # Only a real non-zero exit is the toolchain rejecting the plugin; a stage
    # that never ran a process is unverified (Req 27.5).
    validate = report.condition(CONDITION_TOOLCHAIN_VALIDATE)
    assert validate is not None
    if validate_outcome == "passed":
        assert validate.status is ConditionStatus.MET
    elif validate_outcome == "failed":
        assert validate.status is ConditionStatus.UNMET
    else:
        assert validate.status is ConditionStatus.UNVERIFIED

    # The tree does not exist, so the structural conditions cannot be met and the
    # plugin is never reported done on this input.
    assert not report.complete
