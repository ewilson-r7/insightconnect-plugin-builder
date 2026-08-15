"""Tests for the repair loop's termination semantics.

The loop's value is that it stops for a *stated* reason and cannot describe an
incomplete repair as a success. These tests pin that down: which stopping
condition applies, that `clean` is independent of it, and that a fixer's own
claims never influence the decision.
"""

import asyncio
from pathlib import Path

import pytest

from icplugin_builder.integrations.quality_gate import CodeFinding, QualityReport
from icplugin_builder.orchestrator.repair_loop import (
    RepairLoop,
    RepairStatus,
    RepairOutcome,
)


def finding(path="a.py", line=10, code="unused-import", source="prospector"):
    return CodeFinding(source=source, path=path, code=code, message="msg", line=line)


def report(findings=(), root="/tmp/x"):
    return QualityReport(project_dir=Path(root), findings=tuple(findings))


class ScriptedChecker:
    """Returns a scripted sequence of reports, one per check."""

    def __init__(self, reports):
        self.reports = list(reports)
        self.calls = 0

    async def run(self, project_dir):
        self.calls += 1
        index = min(self.calls - 1, len(self.reports) - 1)
        return self.reports[index]


class RecordingFixer:
    """Records each invocation without changing anything."""

    def __init__(self):
        self.calls = []

    async def __call__(self, root, quality_report):
        self.calls.append((Path(root), tuple(quality_report.findings)))
        return "I fixed everything"  # deliberately untrue; must not be believed


def run(loop, fixer=None, root="/tmp/x"):
    return asyncio.run(loop.run(root, fixer))


class TestCleanTree:
    def test_no_findings_is_clean_without_invoking_the_fixer(self):
        checker = ScriptedChecker([report()])
        fixer = RecordingFixer()
        outcome = run(RepairLoop(checker), fixer)

        assert outcome.status is RepairStatus.CLEAN
        assert outcome.clean
        assert fixer.calls == []
        assert outcome.fix_rounds == 0
        assert "No issues" in outcome.summary()


class TestSuccessfulRepair:
    def test_findings_resolved_after_one_round_reports_repaired(self):
        checker = ScriptedChecker([report([finding()]), report()])
        fixer = RecordingFixer()
        outcome = run(RepairLoop(checker), fixer)

        assert outcome.status is RepairStatus.REPAIRED
        assert outcome.clean
        assert outcome.status.succeeded
        assert len(fixer.calls) == 1
        assert outcome.fix_rounds == 1

    def test_partial_progress_continues_to_a_clean_result(self):
        # Two findings, one fixed per round.
        checker = ScriptedChecker(
            [
                report([finding(path="a.py"), finding(path="b.py")]),
                report([finding(path="b.py")]),
                report(),
            ]
        )
        outcome = run(RepairLoop(checker), RecordingFixer())

        assert outcome.status is RepairStatus.REPAIRED
        assert outcome.clean
        assert outcome.fix_rounds == 2

    def test_the_fixer_receives_the_current_findings(self):
        checker = ScriptedChecker([report([finding(path="a.py")]), report()])
        fixer = RecordingFixer()
        run(RepairLoop(checker), fixer)
        _, findings = fixer.calls[0]
        assert findings[0].path == "a.py"


class TestStall:
    def test_a_round_that_resolves_nothing_stops_the_loop(self):
        # The same finding comes back unchanged: another round will not help.
        checker = ScriptedChecker([report([finding()])])
        fixer = RecordingFixer()
        outcome = run(RepairLoop(checker, max_rounds=10), fixer)

        assert outcome.status is RepairStatus.STALLED
        assert not outcome.clean
        assert not outcome.status.succeeded
        # Tried once, saw no progress, gave up rather than burning the full cap.
        assert len(fixer.calls) == 1

    def test_stall_summary_states_that_nothing_was_resolved(self):
        outcome = run(RepairLoop(ScriptedChecker([report([finding()])])), RecordingFixer())
        summary = outcome.summary()
        assert "resolved nothing" in summary
        assert "still open" in summary

    def test_swapping_one_finding_for_another_is_progress_not_a_stall(self):
        # A resolved key plus a new one means the fixer is doing something, even
        # if the count is unchanged.
        checker = ScriptedChecker(
            [
                report([finding(path="a.py")]),
                report([finding(path="b.py")]),
                report(),
            ]
        )
        outcome = run(RepairLoop(checker, max_rounds=5), RecordingFixer())
        assert outcome.status is RepairStatus.REPAIRED


class TestRoundCap:
    def test_the_cap_is_reported_explicitly_and_is_not_success(self):
        # Findings keep changing, so the loop never stalls; only the cap stops it.
        churn = [report([finding(path=f"file{n}.py")]) for n in range(10)]
        outcome = run(RepairLoop(ScriptedChecker(churn), max_rounds=2), RecordingFixer())

        assert outcome.status is RepairStatus.CAP_REACHED
        assert not outcome.clean
        assert not outcome.status.succeeded
        summary = outcome.summary()
        assert "2-round limit" in summary
        assert "not a clean result" in summary

    def test_the_cap_bounds_the_number_of_fix_attempts(self):
        churn = [report([finding(path=f"file{n}.py")]) for n in range(10)]
        fixer = RecordingFixer()
        run(RepairLoop(ScriptedChecker(churn), max_rounds=2), fixer)
        assert len(fixer.calls) == 2

    def test_a_cap_of_one_still_attempts_a_repair(self):
        checker = ScriptedChecker([report([finding()]), report()])
        outcome = run(RepairLoop(checker, max_rounds=1), RecordingFixer())
        assert outcome.status is RepairStatus.REPAIRED

    def test_max_rounds_below_one_is_rejected(self):
        with pytest.raises(ValueError):
            RepairLoop(ScriptedChecker([report()]), max_rounds=0)


class TestNoFixer:
    def test_findings_without_a_fixer_are_reported_not_silently_accepted(self):
        checker = ScriptedChecker([report([finding()])])
        outcome = run(RepairLoop(checker), None)

        assert outcome.status is RepairStatus.NO_FIXER
        assert not outcome.clean
        assert checker.calls == 1
        assert "no fixer" in outcome.summary()


class TestDeterminism:
    def test_the_fixers_claims_do_not_affect_the_outcome(self):
        # RecordingFixer returns a confident string; the loop must ignore it and
        # judge only by re-running the check.
        checker = ScriptedChecker([report([finding()])])
        outcome = run(RepairLoop(checker, max_rounds=5), RecordingFixer())
        assert not outcome.clean

    def test_line_shifts_within_a_bucket_are_the_same_finding(self):
        # A fix that moves line 10 to 12 must not read as a new problem, or the
        # loop could never recognise convergence.
        checker = ScriptedChecker([report([finding(line=10)]), report([finding(line=12)])])
        outcome = run(RepairLoop(checker, max_rounds=5), RecordingFixer())
        assert outcome.status is RepairStatus.STALLED

    def test_a_genuinely_different_location_is_a_different_finding(self):
        checker = ScriptedChecker([report([finding(line=10)]), report([finding(line=80)]), report()])
        outcome = run(RepairLoop(checker, max_rounds=5), RecordingFixer())
        assert outcome.status is RepairStatus.REPAIRED

    def test_rounds_record_what_was_resolved_and_introduced(self):
        checker = ScriptedChecker(
            [
                report([finding(path="a.py"), finding(path="b.py")]),
                report([finding(path="b.py"), finding(path="c.py")]),
                report(),
            ]
        )
        outcome = run(RepairLoop(checker, max_rounds=5), RecordingFixer())
        second = outcome.rounds[1]
        assert any("a.py" in key for key in second.resolved)
        assert any("c.py" in key for key in second.introduced)
        assert second.made_progress


class TestOutcomeShape:
    def test_remaining_lists_open_findings(self):
        outcome = run(RepairLoop(ScriptedChecker([report([finding(path="a.py")])])), None)
        assert len(outcome.remaining) == 1
        assert "a.py" in outcome.remaining[0]

    def test_clean_is_false_when_no_check_ran(self):
        # Defensive: an outcome with no report must not read as clean.
        assert not RepairOutcome(status=RepairStatus.CLEAN).clean

    def test_every_check_produces_a_round_record(self):
        checker = ScriptedChecker([report([finding()]), report()])
        outcome = run(RepairLoop(checker), RecordingFixer())
        assert len(outcome.rounds) == checker.calls
        assert [record.number for record in outcome.rounds] == [1, 2]
