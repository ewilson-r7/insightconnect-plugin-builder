"""Preservation baselines for the orchestrator layer (spec task 2.1, axes 7-9).

**These tests must PASS on unfixed code.** They record F so the post-fix checks at
tasks 5.5, 7.5 and 9.6 compare against recorded fact rather than against an
expectation written after the change. Nothing here fixes anything and no
production file is edited by this task.

Three of task 2.1's nine axes are orchestrator-layer and live here; the other six
are in ``tests/integrations/test_export_gate_preservation.py``:

7. **Forced export** -- a blocked gate, ``force`` set, the export still succeeds
   and is still recorded (`bugfix.md` 3.12).
8. **Repair loop** -- finding-key arithmetic, the stall condition, the round
   limit, and honest labelling (3.8; parent Req 26.6-26.11).
9. **Delegation isolation** -- the prompt on stdin, a default-deny environment,
   and enumerated tools (3.9; parent Req 29). Asserted here because change 9
   touches the interpreter's call path, which is the same
   ``create_subprocess_exec`` seam.

**Verdicts, not messages**, per the design's preservation section: statuses,
counts, keys and booleans. Where a message is genuinely part of the guarantee --
"honest labelling" -- what is recorded is a *property* of the message
(``status.succeeded``, whether the cap figure appears) rather than its text, so a
rewording is not a regression and a claim of success is.

**Axis 9 uses a real subprocess with a stand-in binary.** Task 1.10 built the
technique in ``tests/orchestrator/test_preview_fidelity_bug_conditions.py`` and it
is reused rather than reinvented: :func:`_recording_cli` writes a script that
records the argv it was given, the environment it was handed and the bytes it read
on stdin. Everything about *how* it is launched stays the production code's own --
:meth:`PluginAgent._invoke`'s own ``create_subprocess_exec`` call -- which is what
makes the isolation claims measurements instead of restatements of the source.
No Kiro CLI is invoked and no model is called.

_Requirements: 3.8, 3.9, 3.11, 3.12_
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from icplugin_builder.core.cost_controller import CostController
from icplugin_builder.core.spec_model import PluginSpec, SemVer
from icplugin_builder.integrations.agent_config import AGENT_NAME, DEFAULT_TOOLS
from icplugin_builder.integrations.env_guard import BASE_NAMES, BASE_PREFIXES, KIRO_ALLOW_PREFIXES
from icplugin_builder.integrations.plugin_agent import PluginAgent
from icplugin_builder.integrations.quality_gate import CodeFinding, QualityReport
from icplugin_builder.orchestrator import Orchestrator
from icplugin_builder.orchestrator.repair_loop import (
    DEFAULT_MAX_ROUNDS,
    RepairLoop,
    RepairOutcome,
    RepairStatus,
)
from icplugin_builder.orchestrator.session import ExportStatus
from icplugin_builder.persistence.audit_log import AuditEvent, AuditLog
from icplugin_builder.persistence.project_folder import ENTRY_MODE_ITERATE_CUSTOM, ProjectFolder
from icplugin_builder.persistence.registry import PluginRegistry

from tests.preservation_baseline import pin

#: The plugin the axis-7 session exports. Net-new and minimal: axis 7 is about the
#: ``force`` route through a blocked gate, not about any particular plugin.
PLUGIN_NAME = "my_plugin"
VENDOR = "acme"

#: Environment variables planted before the axis-9 run. The first three are the
#: shapes `env_guard`'s module docstring names as the real exposure -- this process
#: decrypts tenant API keys and git credentials -- and must not reach the child.
#: The fourth is admitted by :data:`KIRO_ALLOW_PREFIXES`, so it is the control that
#: keeps the assertion from passing because *nothing* was inherited.
SECRETS_THAT_MUST_NOT_TRAVEL: Tuple[str, ...] = (
    "GITHUB_TOKEN",
    "ANTHROPIC_API_KEY",
    "IC_TENANT_API_KEY",
)
ALLOWED_MARKER = "KIRO_PRESERVATION_MARKER"

#: The task handed to the stand-in agent. Long enough that finding it in the
#: process list would be unambiguous if it were ever put on argv.
DELEGATED_INSTRUCTION = (
    "Implement suspend_user against the supplied reference material, " "using the central _make_request in util/api.py."
)


def _spec() -> PluginSpec:
    """The minimal spec the axis-7 session is opened on."""
    return PluginSpec(
        name=PLUGIN_NAME,
        title="My Plugin",
        description="A plugin used to observe the forced-export path.",
        version=SemVer(1, 0, 0),
        vendor=VENDOR,
    )


# ---------------------------------------------------------------------------
# Axis 7 -- a forced export past a blocked gate
# ---------------------------------------------------------------------------


class ForcedExportObservation:
    """What F does when a blocked export is forced.

    Attributes:
        permitted: whether the gate permitted the export before ``force``.
        block_reason_count: how many reasons the decision carried.
        outcome: the :class:`ExportOutcome` ``confirm_export`` returned.
        artifact: the produced ``.plg``, if any.
        export_records: the registry rows the export wrote.
        audit_events: the audit event names the export wrote.
    """

    def __init__(
        self,
        *,
        permitted: bool,
        block_reason_count: int,
        outcome: Any,
        artifact: Optional[Path],
        export_records: Sequence[Any],
        audit_events: Sequence[str],
    ) -> None:
        self.permitted = permitted
        self.block_reason_count = block_reason_count
        self.outcome = outcome
        self.artifact = artifact
        self.export_records = list(export_records)
        self.audit_events = list(audit_events)


@pytest.fixture(scope="module")
def forced_export(tmp_path_factory) -> ForcedExportObservation:
    """Force one export past a blocked gate, through the orchestrator's own path.

    The gate is blocked the way ``test_blocked_gate_refuses_build`` blocks it: no
    ``Code_Validator`` is wired, so no pipeline report exists and ``decide_export``
    treats a missing report as not passed. That is deliberate -- it needs no Docker
    and no toolchain, so this axis is measurable on any host, and the *route* under
    observation is ``force``, not the particular reason the gate said no.
    """
    work = tmp_path_factory.mktemp("axis7")
    projects = work / "projects"
    projects.mkdir()
    folder = ProjectFolder.create(projects, PLUGIN_NAME, _spec())
    folder.save(_spec(), generated_files={"README.md": "hello\n"})

    registry = PluginRegistry(str(work / "registry.db"))
    audit = AuditLog(work / "audit.log")
    orchestrator = Orchestrator(projects_root=projects, registry=registry, audit_log=audit)
    orchestrator.start_session(
        ENTRY_MODE_ITERATE_CUSTOM,
        session_id="forced",
        user_id="operator",
        plugin_name=PLUGIN_NAME,
    )

    plan = asyncio.run(orchestrator.prepare_export("forced"))
    permitted = plan.permitted
    block_reason_count = len(plan.decision.reasons)
    # As `api/app.py` does it: ExportPlan is a frozen dataclass, so a plain
    # attribute assignment raises FrozenInstanceError. This is the fix already in
    # the working tree, and 3.12 requires it keep working.
    object.__setattr__(plan, "_force", True)
    outcome = asyncio.run(
        orchestrator.confirm_export(
            "forced",
            plan,
            confirmed=True,
            target="local",
            output_dir=work / "out",
        )
    )
    return ForcedExportObservation(
        permitted=permitted,
        block_reason_count=block_reason_count,
        outcome=outcome,
        artifact=None if outcome.artifact_path is None else Path(outcome.artifact_path),
        export_records=registry.exports(PLUGIN_NAME),
        audit_events=[record.event for record in audit.records()],
    )


class TestAxisSevenAForcedExportStillSucceedsAndIsRecorded:
    """Axis 7 (`bugfix.md` 3.12) -- the escape hatch keeps working, and keeps a trail.

    Every fix in this bugfix reduces the number of plugins that *need* ``force``.
    None of them may break it: while Bugs 1 and 2 are open it is the only route to
    an export, and after they close it is still the route for a plugin whose gate
    legitimately fails.
    """

    def test_the_premise_holds_the_gate_blocked_the_export(self, forced_export: ForcedExportObservation):
        """Without a block there is no forcing, and the axis measures nothing."""
        assert not forced_export.permitted, (
            "the gate permitted this export, so `force` was not exercised at all and this observation says "
            "nothing about the forced path"
        )

    def test_the_forced_export_succeeded(self, forced_export: ForcedExportObservation):
        assert (
            forced_export.outcome.status is ExportStatus.SUCCEEDED
        ), f"the forced export did not succeed ({forced_export.outcome.status}): {forced_export.outcome.message}"
        assert (
            forced_export.artifact is not None and forced_export.artifact.is_file()
        ), f"the forced export reported success without producing an artifact: {forced_export.artifact}"

    def test_the_recorded_verdicts_are_unchanged(self, forced_export: ForcedExportObservation):
        observed = {
            "gate_permitted_before_force": forced_export.permitted,
            "outcome_status": forced_export.outcome.status.value,
            "artifact_produced": forced_export.artifact is not None,
            "registry_export_count": len(forced_export.export_records),
            "registry_export_targets": [record.target for record in forced_export.export_records],
            "registry_export_results": [record.result for record in forced_export.export_records],
            "audit_events": sorted(set(forced_export.audit_events)),
            "audit_has_build": AuditEvent.BUILD in forced_export.audit_events,
            "audit_has_export": AuditEvent.EXPORT in forced_export.audit_events,
        }
        pin(
            "axis_7_forced_export",
            observed,
            description=(
                "A blocked gate with `force` set on the plan, exported locally. F completes the export and "
                "writes one successful registry export row. It records a *build* audit event and no *export* "
                "one -- see test_no_export_audit_entry_is_written_on_the_local_path, which records that as a "
                "gap between bugfix.md 3.11's wording and what F does."
            ),
            requirements=("3.11", "3.12"),
            measured={
                "block_reason_count": forced_export.block_reason_count,
                "note": (
                    "the block reason count is recorded, not asserted: change 9 adds failed_stages detail "
                    "to the blocked payload (2.16), and the gate here is blocked for the weaker reason that "
                    "no pipeline ran at all"
                ),
            },
        )

    def test_the_export_is_traceable_in_the_registry(self, forced_export: ForcedExportObservation):
        """3.11's registry half: a successful forced export still leaves a row."""
        assert forced_export.export_records, "a successful export wrote no registry row"
        assert [record.result for record in forced_export.export_records] == [
            "success"
        ], f"the registry rows do not read as successful: {forced_export.export_records}"

    def test_no_export_audit_entry_is_written_on_the_local_path(self, forced_export: ForcedExportObservation):
        """A finding about F, recorded because 3.11 says otherwise.

        `bugfix.md` 3.11 states the system "SHALL CONTINUE TO record registry and
        audit entries for builds and exports (Requirements 11, 18)". Measured, F
        records the **build** and not the export: ``confirm_export`` calls
        ``_audit_build``, and ``_finish_local_export`` -- whose own docstring cites
        Req 18.6 and says "registry/audit log" -- calls ``_record_export``, which
        writes to the registry only. :meth:`AuditLog.record_export` exists and is
        never called on this path.

        Recorded as an observation rather than repaired: task 2.1 records F and
        edits no production file, and this is outside the three bugs. It is
        reported so it is not mistaken for something this bugfix broke, and so the
        gap between 3.11's wording and F's behaviour is visible to whoever decides
        what to do about it.
        """
        assert (
            AuditEvent.BUILD in forced_export.audit_events
        ), f"not even the build was audited; the log holds {sorted(set(forced_export.audit_events))}"
        assert AuditEvent.EXPORT not in forced_export.audit_events, (
            "an export audit entry was written, which F did not do when this baseline was recorded. If the "
            "gap in 3.11's wording has been closed deliberately, re-record this axis; if not, something "
            f"else changed. The log holds {sorted(set(forced_export.audit_events))}"
        )


# ---------------------------------------------------------------------------
# Axis 8 -- the repair loop's arithmetic
# ---------------------------------------------------------------------------


class ScriptedChecker:
    """Returns a prepared :class:`QualityReport` per round.

    The loop's termination decision is *arithmetic over finding keys* and is never
    delegated, so scripting the reports is the whole test: it fixes the key sets
    each round observes and leaves the decision entirely to the code under
    measurement. Nothing here mocks the decision itself.
    """

    def __init__(self, reports: Sequence[QualityReport]) -> None:
        self._reports = list(reports)
        self.calls = 0

    async def run(self, project_dir) -> QualityReport:
        """Return the next scripted report, repeating the last one when exhausted."""
        index = min(self.calls, len(self._reports) - 1)
        self.calls += 1
        return self._reports[index]


def _report(root: Path, *codes: str) -> QualityReport:
    """A report carrying one prospector finding per code, at distinct lines.

    Distinct lines matter: :attr:`CodeFinding.key` buckets the line number in
    fives so a fix that shifts code does not make every later finding look new,
    and two findings sharing a bucket would share a key.
    """
    findings = tuple(
        CodeFinding(
            source="prospector",
            path="icon_example/util/api.py",
            line=10 + (index * 10),
            code=code,
            message=f"scripted finding {code}",
        )
        for index, code in enumerate(codes)
    )
    return QualityReport(project_dir=root, findings=findings, checked_files=("icon_example/util/api.py",))


async def _noop_fixer(root: Path, report: QualityReport) -> None:
    """A fixer that changes nothing. Whether it helped is decided by re-checking."""
    return None


def _outcome_rows(outcome: RepairOutcome) -> Dict[str, Any]:
    """The loop's decision as compared verdicts.

    ``summary_claims_success`` is the honest-labelling clause reduced to a
    boolean, so a rewording of the summary is not a regression while a stalled run
    describing itself as finished is.
    """
    return {
        "status": outcome.status.value,
        "status_succeeded": outcome.status.succeeded,
        "clean": outcome.clean,
        "fix_rounds": outcome.fix_rounds,
        "round_count": len(outcome.rounds),
        "max_rounds": outcome.max_rounds,
        "remaining_count": len(outcome.remaining),
        "rounds": [
            {
                "number": record.number,
                "finding_count": record.finding_count,
                "resolved": list(record.resolved),
                "introduced": list(record.introduced),
                "made_progress": record.made_progress,
            }
            for record in outcome.rounds
        ],
        "summary_claims_success": outcome.status.succeeded,
        "summary_names_the_cap": str(outcome.max_rounds) in outcome.summary(),
    }


def _run_loop(
    root: Path,
    reports: Sequence[QualityReport],
    *,
    with_fixer: bool = True,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> RepairOutcome:
    """Drive the loop over ``reports`` and return its outcome."""
    loop = RepairLoop(ScriptedChecker(reports), max_rounds=max_rounds)
    return asyncio.run(loop.run(root, _noop_fixer if with_fixer else None))


class TestAxisEightTheRepairLoopsArithmeticIsUnchanged:
    """Axis 8 (`bugfix.md` 3.8; parent Req 26.6-26.11) -- key arithmetic and honest stops.

    The loop is what makes validation corrective rather than decorative, and its
    honesty is the property that matters: a stalled or capped run must not read as
    a success. Change 5 changes which findings the ``Quality_Gate`` produces, which
    changes what the loop consumes, so its decision procedure is pinned here
    before that lands.
    """

    def test_a_clean_tree_stops_at_one_check(self, tmp_path):
        outcome = _run_loop(tmp_path, [QualityReport(project_dir=tmp_path)])
        assert outcome.status is RepairStatus.CLEAN
        assert outcome.clean and outcome.fix_rounds == 0
        pin(
            "axis_8_repair_clean",
            _outcome_rows(outcome),
            description=(
                "A tree with nothing wrong. F checks once, reports CLEAN, makes no fix attempt, and clean is " "true."
            ),
            requirements=("3.8",),
        )

    def test_a_resolved_finding_counts_as_progress(self, tmp_path):
        outcome = _run_loop(tmp_path, [_report(tmp_path, "undefined-variable"), QualityReport(project_dir=tmp_path)])
        assert outcome.status is RepairStatus.REPAIRED
        assert outcome.clean
        pin(
            "axis_8_repair_repaired",
            _outcome_rows(outcome),
            description=(
                "One finding, resolved by the first fix round. F reports REPAIRED with one fix round, and "
                "the second round records the resolved key. Progress is measured by key arithmetic, never "
                "asked of the fixer."
            ),
            requirements=("3.8",),
        )

    def test_a_round_that_resolves_nothing_stalls(self, tmp_path):
        """The stall condition: identical keys twice, so a third round is refused."""
        repeated = _report(tmp_path, "undefined-variable")
        outcome = _run_loop(tmp_path, [repeated, repeated, repeated])
        assert outcome.status is RepairStatus.STALLED
        assert not outcome.clean and not outcome.status.succeeded
        pin(
            "axis_8_repair_stalled",
            _outcome_rows(outcome),
            description=(
                "The same finding key twice. F stops after the round that resolved nothing, reports STALLED, "
                "and clean is false. A stalled run must not read as a success (Req 26.6-26.11)."
            ),
            requirements=("3.8",),
        )

    def test_progress_every_round_still_stops_at_the_cap(self, tmp_path):
        """The round limit, reached while progress is still being made.

        Each round resolves a key and introduces a new one, so the stall condition
        never fires and only the cap can stop the loop. That is the case in which
        an honest label matters most: there is no shortage of progress to point
        at, and the run is still not finished.
        """
        reports = [
            (
                _report(tmp_path, f"finding-{index}", "carried-over")
                if index % 2 == 0
                else _report(tmp_path, f"finding-{index}")
            )
            for index in range(8)
        ]
        outcome = _run_loop(tmp_path, reports, max_rounds=2)
        assert outcome.status is RepairStatus.CAP_REACHED
        assert not outcome.clean and not outcome.status.succeeded
        pin(
            "axis_8_repair_cap_reached",
            _outcome_rows(outcome),
            description=(
                "A loop making progress every round against a two-round cap. F stops at the cap, reports "
                "CAP_REACHED, names the cap figure in its summary, and clean is false. Hitting the cap is "
                "reported as hitting the cap."
            ),
            requirements=("3.8",),
        )

    def test_no_fixer_reports_the_findings_without_touching_the_tree(self, tmp_path):
        outcome = _run_loop(tmp_path, [_report(tmp_path, "undefined-variable")], with_fixer=False)
        assert outcome.status is RepairStatus.NO_FIXER
        pin(
            "axis_8_repair_no_fixer",
            _outcome_rows(outcome),
            description=(
                "Findings present and no fixer available. F checks once, reports NO_FIXER, and leaves the "
                "tree alone. Not a success, and not silent either."
            ),
            requirements=("3.8",),
        )

    def test_a_line_shift_inside_the_bucket_is_the_same_finding(self, tmp_path):
        """Position-shift-stable identity, which is what lets the loop converge.

        Without bucketing, a fix that moves code down a line would make every
        later finding look new, the resolved set would never be empty, and the
        loop could not observe that it had stopped making progress.
        """
        first = QualityReport(
            project_dir=tmp_path,
            findings=(CodeFinding(source="prospector", path="a.py", line=11, code="c", message="m"),),
        )
        shifted = QualityReport(
            project_dir=tmp_path,
            findings=(CodeFinding(source="prospector", path="a.py", line=13, code="c", message="m"),),
        )
        outcome = _run_loop(tmp_path, [first, shifted, shifted])
        pin(
            "axis_8_repair_key_stability",
            {
                "first_keys": list(first.keys()),
                "shifted_keys": list(shifted.keys()),
                "keys_are_equal": first.keys() == shifted.keys(),
                **_outcome_rows(outcome),
            },
            description=(
                "The same defect reported two lines lower. F assigns both the same bucketed key, so the "
                "round resolves nothing and the loop stalls rather than chasing a moving target forever."
            ),
            requirements=("3.8",),
        )
        assert first.keys() == shifted.keys()
        assert outcome.status is RepairStatus.STALLED


# ---------------------------------------------------------------------------
# Axis 9 -- delegation isolation
# ---------------------------------------------------------------------------


def _recording_cli(directory: Path) -> Tuple[List[str], Path]:
    """Write a stand-in CLI that records its argv, environment and stdin.

    Returns ``(command_prefix, record_path)``. The record is written to a **file**
    rather than passed back through an environment variable, because the child is
    handed a default-deny environment (Req 29) and an env-carried path would not
    survive it -- which is itself part of what is being measured. Task 1.10 hit the
    same constraint and solved it the same way.
    """
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "fake_kiro_cli.py"
    record = directory / "invocation.json"
    script.write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"record = Path(r{str(record)!r})\n"
        "prompt = sys.stdin.read()\n"
        "record.write_text(\n"
        "    json.dumps({'argv': sys.argv, 'env': dict(os.environ), 'stdin': prompt}, sort_keys=True),\n"
        "    encoding='utf-8',\n"
        ")\n"
        "sys.stdout.write('Implemented the task.\\n')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)], record


def _self_injected_names(command: Sequence[str], record: Path) -> Tuple[str, ...]:
    """Names the child adds to its own environment, measured rather than assumed.

    A launched interpreter does not necessarily see exactly the mapping it was
    given: on macOS a CoreFoundation-linked CPython finds
    ``__CF_USER_TEXT_ENCODING`` in its own :data:`os.environ` even when the parent
    passed an environment without it. Recording that as a leak would be wrong --
    nothing in this tool put it there -- and hardcoding a platform exception would
    quietly widen over time. So the same script is run once with an **empty**
    environment and whatever it reports is the platform's own contribution.
    """

    async def probe() -> Tuple[str, ...]:
        process = await asyncio.subprocess.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={},
        )
        await process.communicate(b"")
        payload = json.loads(record.read_text(encoding="utf-8"))
        return tuple(sorted(payload["env"]))

    return asyncio.run(probe())


class DelegationObservation:
    """What the child process actually received."""

    def __init__(
        self,
        *,
        argv: Sequence[str],
        env: Dict[str, str],
        stdin: str,
        script: str,
        self_injected: Sequence[str] = (),
    ) -> None:
        self.argv = list(argv)
        self.env = dict(env)
        self.stdin = stdin
        self.script = script
        self.self_injected = tuple(self_injected)

    @property
    def tail(self) -> List[str]:
        """The argv the agent appended, with the interpreter and script removed."""
        return self.argv[1:] if self.argv and self.argv[0] == self.script else list(self.argv)

    @property
    def unexplained(self) -> Tuple[str, ...]:
        """Variables the child saw that neither the allowlist nor the platform explains.

        Computed against :mod:`env_guard`'s own lists rather than a copy of them: a
        second copy of an allowlist is how the two drift into disagreeing about
        what is safe.
        """
        prefixes = (*BASE_PREFIXES, *KIRO_ALLOW_PREFIXES)
        return tuple(
            sorted(
                name
                for name in self.env
                if name not in BASE_NAMES and not name.startswith(prefixes) and name not in self.self_injected
            )
        )

    @property
    def admitted_by_policy(self) -> bool:
        """Is every variable the child saw admitted, or contributed by the platform?"""
        return not self.unexplained


@pytest.fixture(scope="module")
def delegated_run(tmp_path_factory) -> DelegationObservation:
    """Run :meth:`PluginAgent.implement` against the recording stand-in."""
    work = tmp_path_factory.mktemp("axis9")
    project = work / "tree"
    project.mkdir()
    command, record = _recording_cli(work / "cli")

    planted = {name: f"value-of-{name}" for name in SECRETS_THAT_MUST_NOT_TRAVEL}
    planted[ALLOWED_MARKER] = "kiro-may-see-this"
    # Set directly rather than through `monkeypatch`, which is function-scoped and
    # cannot reach a module-scoped fixture; restored in the `finally` below.
    saved = {name: os.environ.get(name) for name in planted}
    os.environ.update(planted)
    try:
        agent = PluginAgent(CostController(), executable=command)
        asyncio.run(
            agent.implement(
                project,
                DELEGATED_INSTRUCTION,
                session_id="delegated",
                user_id="operator",
            )
        )
    finally:
        for name, previous in saved.items():
            if previous is None:
                os.environ.pop(name, None)
            else:  # pragma: no cover - only when the operator already had one set
                os.environ[name] = previous

    payload = json.loads(record.read_text(encoding="utf-8"))
    observation = DelegationObservation(
        argv=payload["argv"],
        env=payload["env"],
        stdin=payload["stdin"],
        script=command[1],
        # Measured after the real run so the control cannot overwrite the record
        # the run produced.
        self_injected=_self_injected_names(command, record),
    )
    return observation


class TestAxisNineDelegationIsolationIsUnchanged:
    """Axis 9 (`bugfix.md` 3.9; parent Req 29) -- stdin, default-deny, named tools.

    Nothing in this bugfix is *supposed* to touch delegation. It is asserted
    because change 9 edits the interpreter's call path -- adding cost accounting
    (2.18) and truncation notices (2.20) -- and that path is the same
    ``create_subprocess_exec`` seam with the same three guarantees. A regression
    here would be silent: a prompt moved to argv still works, and still puts the
    plugin's task in the process list.
    """

    def test_the_prompt_arrived_on_stdin_and_not_on_argv(self, delegated_run: DelegationObservation):
        assert delegated_run.stdin == DELEGATED_INSTRUCTION, (
            "the child did not receive the instruction on stdin; it read " f"{delegated_run.stdin[:120]!r}"
        )
        joined = " ".join(delegated_run.argv)
        assert (
            DELEGATED_INSTRUCTION not in joined
        ), f"the instruction reached the process list through argv: {joined[:300]}"

    def test_the_child_environment_is_default_deny(self, delegated_run: DelegationObservation):
        leaked = sorted(name for name in SECRETS_THAT_MUST_NOT_TRAVEL if name in delegated_run.env)
        assert not leaked, f"the delegated CLI was handed {leaked}, none of which it needs"
        assert delegated_run.env.get(ALLOWED_MARKER) == "kiro-may-see-this", (
            "the allowlisted control variable did not reach the child either, so this measurement cannot "
            "tell a default-deny environment from an empty one"
        )
        assert delegated_run.admitted_by_policy, (
            "the child saw variables that neither the env_guard allowlist nor the platform's own injection "
            f"explains: {delegated_run.unexplained}. The platform contributed "
            f"{delegated_run.self_injected}"
        )

    def test_the_trusted_tools_are_enumerated_not_blanket(self, delegated_run: DelegationObservation):
        trust = [part for part in delegated_run.argv if part.startswith("--trust-tools=")]
        assert len(trust) == 1, f"expected exactly one --trust-tools argument, got {trust}"
        assert "--trust-all-tools" not in delegated_run.argv, (
            "the delegated agent was launched with blanket tool trust, which Req 29 requires be a stated "
            "exception rather than the default"
        )
        granted = trust[0].split("=", 1)[1].split(",")
        assert tuple(granted) == tuple(
            DEFAULT_TOOLS
        ), f"the trusted set {granted} does not match the agent config's own {list(DEFAULT_TOOLS)}"

    def test_the_recorded_verdicts_are_unchanged(self, delegated_run: DelegationObservation):
        observed = {
            "argv_tail": delegated_run.tail,
            "agent_name": AGENT_NAME,
            "trusted_tools": list(DEFAULT_TOOLS),
            "trust_all_tools_present": "--trust-all-tools" in delegated_run.argv,
            "prompt_on_stdin": delegated_run.stdin == DELEGATED_INSTRUCTION,
            "prompt_in_argv": DELEGATED_INSTRUCTION in " ".join(delegated_run.argv),
            "secrets_reaching_the_child": sorted(
                name for name in SECRETS_THAT_MUST_NOT_TRAVEL if name in delegated_run.env
            ),
            "allowlisted_marker_reached_the_child": delegated_run.env.get(ALLOWED_MARKER) is not None,
            "every_variable_admitted_by_policy": delegated_run.admitted_by_policy,
            "unexplained_variables": list(delegated_run.unexplained),
        }
        pin(
            "axis_9_delegation_isolation",
            observed,
            description=(
                "One PluginAgent.implement run against a stand-in binary that records what it was given. F "
                "delivers the prompt on stdin and never on argv, hands the child only variables the "
                "env_guard allowlist admits, and names the trusted tools explicitly rather than trusting "
                "everything. Unchanged by this bugfix; asserted because change 9 edits the same seam."
            ),
            requirements=("3.9",),
            measured={
                "child_variable_count": len(delegated_run.env),
                "platform_injected_names": list(delegated_run.self_injected),
                "note": (
                    "the number of variables the child saw is a property of the operator's shell, not of "
                    "the tool, so it is recorded and not asserted; what is asserted is that every one of "
                    "them is admitted by BASE_NAMES, BASE_PREFIXES or KIRO_ALLOW_PREFIXES, or is one the "
                    "platform injects into any child regardless of the environment it was handed "
                    "(measured on this host as the control run with env={})"
                ),
            },
        )
