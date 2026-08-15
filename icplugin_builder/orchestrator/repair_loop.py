"""The repair loop: check, fix, re-check, until it stops making progress.

Running the validators and reporting their results is not the same as producing a
working plugin, and the difference is the whole reason broken plugins shipped.
The four-stage pipeline recorded four failures and stopped. Nothing read them.

This loop closes that gap: it checks the tree, hands the findings to a fixer,
checks again, and repeats until the findings are gone or it can no longer make
progress. It is a small amount of logic, and every decision in it is
deliberately arithmetic.

**The termination decision is deterministic and is never delegated.** A model
asked "are we done?" will eventually say yes. Instead, findings carry stable keys
(see :class:`~icplugin_builder.integrations.quality_gate.CodeFinding.key`) and
each round is compared with the last:

* nothing found -> done;
* the round resolved at least one key -> progress, go again;
* the round resolved nothing -> **stalled**, stop;
* rounds exhausted -> **cap reached**, stop.

The last two are outcomes, not successes. A caller must be able to tell "this is
finished" from "this is as far as I got", because reporting the second as the
first is how a broken plugin gets described as ready. :attr:`RepairOutcome.clean`
is true only when there are genuinely no findings left, and
:meth:`RepairOutcome.summary` states which stopping condition applied.

Line numbers are bucketed inside the key, so a fix that shifts code down a few
lines does not make every later finding look new -- otherwise the loop could
never observe that it had converged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, List, Optional, Protocol, Sequence, Set, Tuple, Union

from ..integrations.quality_gate import QualityReport

__all__ = [
    "DEFAULT_MAX_ROUNDS",
    "RepairStatus",
    "RoundRecord",
    "RepairOutcome",
    "Checker",
    "Fixer",
    "RepairLoop",
]

logger = logging.getLogger(__name__)

#: How many fix attempts to make before stopping. Low on purpose: if two rounds
#: of a capable fixer have not resolved a finding, a third rarely does, and the
#: operator is better served by an honest report than by more spend.
DEFAULT_MAX_ROUNDS = 3


class RepairStatus(str, Enum):
    """How the loop finished."""

    #: Nothing was wrong to begin with.
    CLEAN = "clean"
    #: Findings were present and every one was resolved.
    REPAIRED = "repaired"
    #: A round resolved nothing, so further rounds would not help.
    STALLED = "stalled"
    #: The round cap was reached with findings still open.
    CAP_REACHED = "cap_reached"
    #: Findings were present but no fixer was available to act on them.
    NO_FIXER = "no_fixer"

    @property
    def succeeded(self) -> bool:
        """Return ``True`` only for outcomes that left the tree free of findings."""
        return self in (RepairStatus.CLEAN, RepairStatus.REPAIRED)


@dataclass(frozen=True)
class RoundRecord:
    """What one round observed and achieved.

    Attributes:
        number: the 1-based round number.
        finding_count: how many findings the check produced.
        resolved: keys present in the previous round and gone in this one.
        introduced: keys not seen in any earlier round.
    """

    number: int
    finding_count: int
    resolved: Tuple[str, ...] = ()
    introduced: Tuple[str, ...] = ()

    @property
    def made_progress(self) -> bool:
        """Return ``True`` iff this round resolved at least one finding."""
        return bool(self.resolved)


@dataclass(frozen=True)
class RepairOutcome:
    """The result of running the loop.

    Attributes:
        status: which stopping condition applied.
        rounds: one :class:`RoundRecord` per check performed.
        final_report: the last :class:`QualityReport` produced.
        max_rounds: the cap that was in force.
    """

    status: RepairStatus
    rounds: Tuple[RoundRecord, ...] = ()
    final_report: Optional[QualityReport] = None
    max_rounds: int = DEFAULT_MAX_ROUNDS

    @property
    def clean(self) -> bool:
        """Return ``True`` iff no findings remain.

        This is the only property a caller should use to decide whether the work
        is finished. It is deliberately independent of :attr:`status` so that a
        stalled or capped run cannot read as success.
        """
        return self.final_report is not None and self.final_report.clean

    @property
    def remaining(self) -> Tuple[str, ...]:
        """The findings still open, rendered one per line."""
        if self.final_report is None:
            return ()
        return tuple(str(finding) for finding in self.final_report.findings)

    @property
    def fix_rounds(self) -> int:
        """How many fix attempts were made (checks performed, minus the first)."""
        return max(len(self.rounds) - 1, 0)

    def summary(self) -> str:
        """Return a one-line summary that names the stopping condition.

        A stalled or capped run says so explicitly. Nothing here can describe an
        incomplete repair as a success.
        """
        count = len(self.final_report.findings) if self.final_report is not None else 0
        if self.status is RepairStatus.CLEAN:
            return "No issues found."
        if self.status is RepairStatus.REPAIRED:
            return f"Repaired all findings in {self.fix_rounds} round(s)."
        if self.status is RepairStatus.NO_FIXER:
            return f"{count} finding(s) left unrepaired: no fixer was available."
        if self.status is RepairStatus.STALLED:
            return (
                f"Stopped after {self.fix_rounds} round(s) with {count} finding(s) still open: "
                "the last round resolved nothing, so further rounds were not attempted."
            )
        return (
            f"Reached the {self.max_rounds}-round limit with {count} finding(s) still open. "
            "This is not a clean result."
        )


class Checker(Protocol):
    """Produces a :class:`QualityReport` for a project tree."""

    async def run(self, project_dir: Union[str, Path]) -> QualityReport:  # pragma: no cover - protocol
        ...


#: Called with the project directory and the current report; expected to attempt
#: repairs in place. Its return value is ignored -- whether it helped is decided
#: by re-running the check, not by what it claims.
Fixer = Callable[[Path, QualityReport], Awaitable[object]]


class RepairLoop:
    """Checks a tree, delegates repairs, and re-checks until progress stops."""

    def __init__(
        self,
        checker: Checker,
        *,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
    ) -> None:
        """Configure the loop.

        Args:
            checker: produces the findings each round.
            max_rounds: how many fix attempts to make before stopping. Must be
                at least 1; a value below that would make the loop a plain check.
        """
        if max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        self._checker = checker
        self._max_rounds = max_rounds

    @property
    def max_rounds(self) -> int:
        """The configured round cap."""
        return self._max_rounds

    async def run(
        self,
        project_dir: Union[str, Path],
        fixer: Optional[Fixer] = None,
    ) -> RepairOutcome:
        """Check ``project_dir``, repair, and re-check until progress stops.

        Args:
            project_dir: the plugin working tree to check and repair.
            fixer: attempts repairs given the findings. When ``None``, the tree is
                checked once and reported without modification.

        Returns:
            A :class:`RepairOutcome`. Inspect :attr:`RepairOutcome.clean` to know
            whether anything remains; :attr:`RepairOutcome.status` says why the
            loop stopped.
        """
        root = Path(project_dir)
        records: List[RoundRecord] = []
        seen: Set[str] = set()
        previous: Set[str] = set()
        report = await self._checker.run(root)

        while True:
            current = set(report.keys())
            resolved = previous - current
            introduced = current - seen
            seen |= current
            records.append(
                RoundRecord(
                    number=len(records) + 1,
                    finding_count=len(report.findings),
                    resolved=tuple(sorted(resolved)),
                    introduced=tuple(sorted(introduced)),
                )
            )

            if not report.findings:
                status = RepairStatus.CLEAN if len(records) == 1 else RepairStatus.REPAIRED
                return self._outcome(status, records, report)

            if fixer is None:
                return self._outcome(RepairStatus.NO_FIXER, records, report)

            # A round that resolved nothing will not do better on the next pass.
            if len(records) > 1 and not resolved:
                logger.info(
                    "repair loop stalled at round %d with %d finding(s) open",
                    len(records) - 1,
                    len(report.findings),
                )
                return self._outcome(RepairStatus.STALLED, records, report)

            if len(records) > self._max_rounds:
                logger.info(
                    "repair loop hit its %d-round cap with %d finding(s) open",
                    self._max_rounds,
                    len(report.findings),
                )
                return self._outcome(RepairStatus.CAP_REACHED, records, report)

            await fixer(root, report)
            previous = current
            report = await self._checker.run(root)

    def _outcome(
        self,
        status: RepairStatus,
        records: Sequence[RoundRecord],
        report: QualityReport,
    ) -> RepairOutcome:
        """Assemble the outcome for a terminating condition."""
        return RepairOutcome(
            status=status,
            rounds=tuple(records),
            final_report=report,
            max_rounds=self._max_rounds,
        )
