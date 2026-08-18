"""Bug-condition exploration tests for export preview fidelity (spec task 1.6).

**These tests are expected to FAIL on unfixed code.** The failure is the point:
each one encodes the behavior `bugfix.md` requires and, until the fix lands,
reports the counterexample that proves the defect exists. Do not "repair" them by
weakening an assertion -- they become the fix's acceptance check at task 5.4.

Scope of this module: Bug 3, the **lead** bug -- the one that reports a correct
plugin as broken. Nothing here touches code generation, which the run verified as
correct (`bugfix.md` 3.1), and nothing here edits a production file.

Task 1.6 -- ``isBugCondition_3``, the preview judges a stale draft::

    RETURN implementationDelegated(X)
       AND diskSpec(projectFolder(X)) <> draftSpec(X)

and the property required of the fixed tool::

    FOR ALL X WHERE isBugCondition_3(X) DO
      plan <- prepareExport'(X)
      ASSERT plan.spec_preview = versionedVendorSuffixed(diskSpec(projectFolder(X)))
      ASSERT completenessFindings(plan) = checkCompleteness(diskSpec(projectFolder(X)))
      ASSERT isCompleteOnDisk(X) IMPLIES completenessFindings(plan) = EMPTY
    END FOR

The mechanism, confirmed by reading ``orchestrator.py`` rather than assumed:
``prepare_export`` derives everything from ``session.draft`` --
``atomic_apply(session.draft, _suffix_vendor)`` at the top, and then
``spec_preview``, :func:`check_completeness` and ``_evaluate_done``'s ``spec=``
all from that one draft. ``_file_tree`` already reads the tree, which is why the
run's file list was right while its spec was wrong. Nothing re-reads
``plugin.spec.yaml`` after ``_delegate_implementation`` returns.

**Reconstructing "a session that delegated implementation."** The original
session is gone, and reconstructing it must not involve an LLM call or
``kiro-cli``. What is reconstructed is the *state*: a session whose draft is the
pre-implementation draft while the tree carries the agent's finished spec. It is
built by running the real turn path -- :meth:`Orchestrator.apply_turn` with real
:class:`DraftOperation` edits -- against a copy of the concrete JumpCloud tree,
with a stand-in agent that replays the run's **recorded** output: the
``plugin.spec.yaml`` the real agent actually wrote, read from that tree. So the
orchestrator code under test is the real one, the tree is the real one, and the
only substitution is the agent process, whose output is not synthesised but
replayed.

Two things make the reconstruction faithful rather than convenient:

* The eleven absent top-level fields are **structurally forced**, not chosen. The
  planner's whole vocabulary is :class:`AddComponent`, :class:`ModifyComponent`,
  :class:`RemoveComponent`, :class:`SetConnection` and :class:`UpdateMetadata`;
  none of them can set ``extension``, ``products``, ``support``, ``status``,
  ``cloud_ready``, ``supported_versions``, ``key_features``, ``requirements``,
  ``version_history``, ``resources`` or ``hub_tags``. The twelfth required field,
  ``sdk``, *is* present because ``_with_resolved_sdk`` stamps it on a structural
  turn. Any pre-implementation draft in this codebase therefore reports exactly
  those eleven, which is where `bugfix.md` 1.7's number 11 comes from.
* The two values that are *not* derivable from the tree -- the interpreter's
  ``credential_token`` and the absence of output examples -- are taken from the
  run as `bugfix.md` 1.7 records them, and are named as recorded observations
  where they are set.

**A copy, not the concrete tree.** The turn path writes: materializing a project
folder calls ``ProjectFolder.adopt``, which rewrites ``plugin.spec.yaml`` from the
draft. Run against ``~/.icplugin-builder/projects/jumpcloud/`` that would put the
stale spec over the run's evidence. The copy is byte-identical, and
:meth:`TestTheReconstructionIsFaithful.test_the_copy_carries_the_concrete_trees_spec`
asserts it, so every figure below is a figure about the concrete tree.

**The control needs no reconstruction.** ``iterate_custom`` loads the spec from
disk in ``_start_iterate``, so opening the same tree in that mode is the contrast
that makes the point: same plugin, same code, same stages, differing only in
which spec was read.

**Environment.** ``insight-plugin`` and ``prospector`` live in
``~/Library/Python/3.9/bin`` and ``docker`` in
``/Applications/Docker.app/Contents/Resources/bin``; neither is on a non-login
shell ``PATH``, so :func:`_tool_path` prepends both. The preview and completeness
claims need no toolchain and run anywhere. Only the definition-of-done contrast
does, and it **skips** rather than failing when the toolchain is absent, so a
missing tool is never reported as a finding (parent Req 26.4, 27.5).

Task 1.7 appends its own section to this module, below task 1.6's classes: the
write-back, split into the spec it ships and the source files it reverts.

_Requirements: 1.7, 1.8_
"""

import asyncio
import copy
import inspect
import json
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

# Task 1.10 measures what reaches the operator, so it drives the real websocket
# route and reads the real export payload. Both live in the API layer; the
# behaviour under test is the orchestrator's and the route's own.
from icplugin_builder.api.app import _serialize_export_plan, create_app
from icplugin_builder.core.cost_controller import CostController
from icplugin_builder.core.draft import ComponentKind, Draft
from icplugin_builder.core.generation import ArtifactKind, GenerationRequest
from icplugin_builder.core.spec_completeness import (
    REQUIRED_TOP_LEVEL,
    CompletenessReport,
    check_completeness,
)
from icplugin_builder.core.spec_model import Component, FieldSchema, PluginSpec
from icplugin_builder.core.vendor import apply_custom_vendor_suffix
from icplugin_builder.core.yaml_codec import dump_plugin_spec, load_plugin_spec
from icplugin_builder.integrations.build_prep import resolve_target_python
from icplugin_builder.integrations.code_validator import CodeValidator
from icplugin_builder.integrations.definition_of_done import (
    CONDITION_SPEC_COMPLETE,
    ConditionStatus,
)
from icplugin_builder.integrations.llm_generator import LLMGenerator
from icplugin_builder.integrations.plugin_agent import AgentRunResult, PluginAgent, PluginAgentError
from icplugin_builder.integrations.quality_gate import QualityGate
from icplugin_builder.integrations.reference_material import (
    ReferenceAcquirer,
    ReferenceSet,
    store_reference_set,
)
from icplugin_builder.orchestrator import interpreter as interpreter_module
from icplugin_builder.orchestrator.interpreter import Interpreter, InterpreterError
from icplugin_builder.orchestrator.operations import AddComponent, SetConnection, UpdateMetadata
from icplugin_builder.orchestrator.orchestrator import Orchestrator, TurnPlan
from icplugin_builder.orchestrator.session import ExportOutcome, ExportPlan, TurnStatus
from icplugin_builder.persistence.audit_log import AuditLog
from icplugin_builder.persistence.project_folder import (
    ENTRY_MODE_CREATE_NEW,
    ENTRY_MODE_ITERATE_CUSTOM,
)
from icplugin_builder.persistence.registry import PluginRegistry

#: The tree the reproduction run produced. Every concrete-tree figure below is
#: about this plugin and no other.
JUMPCLOUD_TREE = Path("~/.icplugin-builder/projects/jumpcloud").expanduser()

#: Directories holding the real toolchain on the reproduction host, per
#: `bugfix.md` "Reproduction Environment". Prepended rather than replaced.
TOOLCHAIN_PATH_ENTRIES: Tuple[str, ...] = (
    str(Path("~/Library/Python/3.9/bin").expanduser()),
    "/Applications/Docker.app/Contents/Resources/bin",
)

#: The credential type the run's interpreter put on ``api_key``. Not derivable
#: from the tree -- the agent replaced it with ``credential_secret_key`` -- so it
#: is taken from `bugfix.md` 1.7, which records it as observed.
RECORDED_DRAFT_CREDENTIAL_TYPE = "credential_token"

#: The concrete tree's four action outputs. Held as a constant because Hypothesis
#: strategies are built at import time;
#: :meth:`TestTheReconstructionIsFaithful.test_the_recorded_output_paths_match_the_tree`
#: asserts they are the tree's own.
CONCRETE_OUTPUT_PATHS: Tuple[Tuple[str, str], ...] = (
    ("create_user", "user_id"),
    ("create_user", "success"),
    ("add_user_to_group", "success"),
    ("suspend_user", "success"),
)

#: What `bugfix.md` 1.7 and 1.8 record for this plugin, so a re-measurement that
#: differs is visible rather than quietly absorbed into a new expectation.
RECORDED_STALE_FINDING_COUNT = 16
RECORDED_DISK_TOP_LEVEL_KEYS = 23
RECORDED_CONTROL_FINDING_COUNT = 0
RECORDED_CONTROL_OUTSTANDING = 2
RECORDED_STALE_OUTSTANDING = 3


def _tool_path() -> str:
    """Return ``PATH`` with the toolchain directories prepended."""
    existing = os.environ.get("PATH", "")
    return os.pathsep.join([*TOOLCHAIN_PATH_ENTRIES, existing]) if existing else os.pathsep.join(TOOLCHAIN_PATH_ENTRIES)


@pytest.fixture(scope="module", autouse=True)
def toolchain_on_path():
    """Put the real toolchain on ``PATH`` for this module.

    Module-scoped rather than ``monkeypatch``-based on purpose: the reconstruction
    fixtures below are themselves module-scoped, so they are built before any
    function-scoped fixture could have altered the environment.
    """
    previous = os.environ.get("PATH", "")
    os.environ["PATH"] = _tool_path()
    try:
        yield
    finally:
        os.environ["PATH"] = previous


def _require_tree() -> None:
    """Skip when the concrete tree is absent; it cannot be substituted.

    A synthesised tree would not carry the run's evidence, and the figures in
    `bugfix.md` 1.7 and 1.8 are figures about this one plugin.
    """
    if not JUMPCLOUD_TREE.is_dir():
        pytest.skip(
            f"the JumpCloud tree is not present at {JUMPCLOUD_TREE}; these assertions are about that "
            "concrete tree and a synthesised one would not carry the same evidence"
        )


def _copy_tree(destination_root: Path) -> Path:
    """Copy the concrete tree under ``destination_root`` and return the copy.

    The turn path writes to the tree it works in (``ProjectFolder.adopt`` rewrites
    ``plugin.spec.yaml`` from the draft), so the reconstruction never runs against
    the run's own evidence.
    """
    _require_tree()
    projects = destination_root / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    copy = projects / JUMPCLOUD_TREE.name
    shutil.copytree(JUMPCLOUD_TREE, copy, symlinks=True)
    return copy


def _without_examples(fields: Mapping[str, FieldSchema]) -> Dict[str, FieldSchema]:
    """Return ``fields`` with every ``example`` cleared.

    The 12 ``example:`` entries on disk are the agent's work (`bugfix.md` 1.7), so
    the pre-implementation draft carries none. Only the *output* examples affect
    completeness; the inputs are cleared for the same reason rather than because
    the count depends on them.
    """
    stripped: Dict[str, FieldSchema] = {}
    for name, schema in fields.items():
        without = copy.deepcopy(schema)
        without.example = None
        stripped[name] = without
    return stripped


def _pre_implementation_component(component: Component) -> Component:
    """The interpreter's version of one action: shape without examples."""
    return Component(
        title=component.title,
        description=component.description,
        input=_without_examples(component.input),
        output=_without_examples(component.output),
    )


def _pre_implementation_turn(disk_spec: PluginSpec) -> TurnPlan:
    """The turn the run's interpretation layer produced, in real operations.

    Every edit goes through the planner's actual vocabulary. That is what makes
    the eleven absent required fields a structural fact about this codebase rather
    than a choice made by this test: no :class:`DraftOperation` can set any of
    them.
    """
    operations = [
        UpdateMetadata(
            name=disk_spec.name,
            title=disk_spec.title,
            description=disk_spec.description,
            version=disk_spec.version,
            vendor=disk_spec.vendor,
        ),
        SetConnection(
            connection={
                "api_key": FieldSchema(
                    # Recorded, not derived: see RECORDED_DRAFT_CREDENTIAL_TYPE.
                    type=RECORDED_DRAFT_CREDENTIAL_TYPE,
                    required=True,
                    title="API Key",
                    description="JumpCloud API key for authentication",
                )
            }
        ),
    ]
    operations.extend(
        AddComponent(kind=ComponentKind.ACTION, name=name, component=_pre_implementation_component(component))
        for name, component in disk_spec.actions.items()
    )
    return TurnPlan(
        operations=operations,
        reasoning=[
            GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, parameters={"action": name}) for name in disk_spec.actions
        ],
    )


class RecordedAgent:
    """Stands in for the delegated agent, replaying its recorded output.

    The real agent's output is the tree, and the part of it this bug is about is
    ``plugin.spec.yaml``. That file is replayed verbatim from the concrete tree,
    so nothing about the agent's work is synthesised. No LLM call, no
    ``kiro-cli``: the substitution is the process, not the result.

    Args:
        recorded_spec_text: the spec to replay into the tree.
        block_seconds: how long ``implement`` holds the turn open. Added for task
            1.10, whose progress claim (`bugfix.md` 1.14) is about what is emitted
            *while* a delegated run is in flight, so the run has to have a duration
            this module chose. Defaults to zero, so no caller above changes.
    """

    def __init__(self, recorded_spec_text: str, *, block_seconds: float = 0.0) -> None:
        self._recorded_spec_text = recorded_spec_text
        self._block_seconds = block_seconds
        self.calls = 0
        #: When ``implement`` was entered and left, on :func:`time.monotonic`'s
        #: clock, so a silence can be attributed to the delegated phase rather
        #: than to the turn as a whole.
        self.entered_at: Optional[float] = None
        self.left_at: Optional[float] = None

    async def implement(self, root, instruction, *, session_id: str, user_id: str) -> AgentRunResult:
        """Write the recorded spec into ``root``, as the delegated run did."""
        self.entered_at = time.monotonic()
        self.calls += 1
        (Path(root) / "plugin.spec.yaml").write_text(self._recorded_spec_text, encoding="utf-8")
        if self._block_seconds > 0:
            # ``await`` rather than ``sleep``: a real delegated run is waiting on a
            # subprocess, so it leaves the event loop free. Blocking it here would
            # measure the stand-in instead of the orchestrator's emissions.
            await asyncio.sleep(self._block_seconds)
        self.left_at = time.monotonic()
        return AgentRunResult(
            succeeded=True,
            summary="implemented the API client, connection, three actions and their unit tests",
            transcript="",
            changed_files=("plugin.spec.yaml",),
        )


class Divergence:
    """One reconstructed diverged session and its ``iterate_custom`` control.

    A plain class rather than a dataclass on purpose: the property test below
    receives this through a fixture, and Hypothesis's pretty-printer expands a
    dataclass field by field, which prints two whole export plans (~40kB) on every
    failing example and says nothing the assertion messages do not.

    Attributes:
        tree: the copied working tree both sessions point at.
        disk_text: the concrete tree's ``plugin.spec.yaml``, verbatim.
        disk_spec: that spec, parsed -- ``diskSpec(projectFolder(X))``.
        stale_draft_spec: the pre-implementation draft's spec, as the turn
            committed it -- ``draftSpec(X)``.
        agent_calls: how many times the delegated agent ran, so
            ``implementationDelegated(X)`` is measured rather than assumed.
        stale_plan: the preview computed in the diverged session.
        control_plan: the preview computed in a fresh ``iterate_custom`` session
            over the same tree.
        graded: whether the real four-stage validator and quality gate ran, which
            is what the definition-of-done contrast needs.
        orchestrator: the orchestrator both sessions live in. Added for task 1.7,
            which has to *confirm* an export from the diverged session rather than
            only read its preview, and so needs the live object rather than the
            plan alone. Read-only for every task 1.6 assertion.

    **Confirming an export from a shared reconstruction writes to its tree.**
    ``confirm_export`` -> ``_build_dir`` saves over the project folder, so any
    caller that exports must hold its own :func:`_copy_tree` copy. Task 1.7's
    fixtures do; the module-scoped :func:`divergence` must never be exported from,
    or the task 1.6 assertions above would be reading a tree the export rewrote.
    """

    def __init__(
        self,
        *,
        tree: Path,
        disk_text: str,
        disk_spec: PluginSpec,
        stale_draft_spec: PluginSpec,
        agent_calls: int,
        stale_plan: ExportPlan,
        control_plan: ExportPlan,
        graded: bool,
        orchestrator: Optional[Orchestrator] = None,
    ) -> None:
        self.tree = tree
        self.disk_text = disk_text
        self.disk_spec = disk_spec
        self.stale_draft_spec = stale_draft_spec
        self.agent_calls = agent_calls
        self.stale_plan = stale_plan
        self.control_plan = control_plan
        self.graded = graded
        self.orchestrator = orchestrator

    def __repr__(self) -> str:
        return f"Divergence(tree={self.tree}, graded={self.graded}, agent_calls={self.agent_calls})"


def _orchestrator(
    tree: Path,
    recorded_spec_text: str,
    *,
    graded: bool,
    block_seconds: float = 0.0,
    registry: Optional[PluginRegistry] = None,
    audit_log: Optional[AuditLog] = None,
) -> Tuple[Orchestrator, RecordedAgent]:
    """Wire an orchestrator over ``tree``, mirroring ``api/app.py``'s own wiring.

    ``graded`` selects whether the real four-stage :class:`CodeValidator` and
    :class:`QualityGate` are attached. They are needed only for the
    definition-of-done contrast: ``spec_preview`` and the completeness findings
    are computed from the draft and from the tree, and no stage or check
    contributes to either. Leaving them out where they contribute nothing keeps
    the preview claims runnable on a host with no Docker -- it does not stub any
    behavior the claims are about.

    ``block_seconds``, ``registry`` and ``audit_log`` are task 1.10's additions,
    all defaulting to the previous behaviour so no caller above changes:
    ``block_seconds`` holds a delegated run open long enough to time the frames
    emitted during it (`bugfix.md` 1.14), and the registry is what gives a plugin a
    *prior* exported version, without which no version bump can happen and 1.16's
    display can only ever be measured in one of its two cases.
    """
    agent = RecordedAgent(recorded_spec_text, block_seconds=block_seconds)
    target_python = resolve_target_python().executable or "python3"
    return (
        Orchestrator(
            plugin_agent=agent,
            projects_root=tree.parent,
            registry=registry,
            audit_log=audit_log,
            # Mirrors api/app.py: one gate, the resolved target interpreter.
            quality_gate=QualityGate(python_executable=target_python) if graded else None,
            code_validator=(
                CodeValidator(
                    lint_command=("flake8", "."),
                    docker_executable="docker",
                    insight_plugin_executable="insight-plugin",
                    validate_python_executable=target_python,
                )
                if graded
                else None
            ),
        ),
        agent,
    )


async def _reconstruct(tree: Path, *, graded: bool) -> Divergence:
    """Reconstruct the diverged session and its control over ``tree``."""
    disk_text = (JUMPCLOUD_TREE / "plugin.spec.yaml").read_text(encoding="utf-8")
    disk_spec = load_plugin_spec(disk_text)
    orchestrator, agent = _orchestrator(tree, disk_text, graded=graded)

    # The diverged session: one turn that creates the plugin and delegates its
    # implementation, exactly as the run's single message did.
    orchestrator.start_session(ENTRY_MODE_CREATE_NEW, session_id="diverged", user_id="tester")
    result = await orchestrator.apply_turn("diverged", _pre_implementation_turn(disk_spec))
    assert (
        result.status is TurnStatus.APPLIED
    ), f"the reconstruction's turn did not apply: {result.status} {result.message}"
    stale_draft_spec = copy.deepcopy(orchestrator.session("diverged").draft.spec)
    stale_plan = await orchestrator.prepare_export("diverged")

    # The control: the same tree, reopened in the mode that loads from disk.
    orchestrator.start_session(
        ENTRY_MODE_ITERATE_CUSTOM,
        session_id="control",
        user_id="tester",
        plugin_name=tree.name,
    )
    control_plan = await orchestrator.prepare_export("control")

    return Divergence(
        tree=tree,
        disk_text=disk_text,
        disk_spec=disk_spec,
        stale_draft_spec=stale_draft_spec,
        agent_calls=agent.calls,
        stale_plan=stale_plan,
        control_plan=control_plan,
        graded=graded,
        orchestrator=orchestrator,
    )


@pytest.fixture(scope="module")
def divergence(tmp_path_factory) -> Divergence:
    """The reconstruction, without the toolchain: the preview and spec claims.

    Module-scoped so the turn and both previews are computed once.
    """
    _require_tree()
    tree = _copy_tree(tmp_path_factory.mktemp("preview_fidelity"))
    return asyncio.run(_reconstruct(tree, graded=False))


@pytest.fixture(scope="module")
def mutable_tree(tmp_path_factory) -> Path:
    """A copy of the tree the property test below is free to rewrite.

    Its own copy rather than :func:`divergence`'s, so no example can leave state
    behind that another test would read.
    """
    _require_tree()
    return _copy_tree(tmp_path_factory.mktemp("preview_fidelity_mutations"))


@pytest.fixture(scope="module")
def graded_divergence(tmp_path_factory) -> Divergence:
    """The same reconstruction with the real four stages and quality gate.

    Skips when the toolchain is incomplete: a condition that reads ``unverified``
    because Docker was absent is an environmental artifact, not a finding.
    """
    _require_tree()
    for tool in ("docker", "insight-plugin", "black"):
        if shutil.which(tool) is None:
            pytest.skip(
                f"{tool} is not on PATH, so the definition-of-done contrast would measure the host rather "
                "than the plugin"
            )
    tree = _copy_tree(tmp_path_factory.mktemp("preview_fidelity_graded"))
    return asyncio.run(_reconstruct(tree, graded=True))


def _suffixed(spec: PluginSpec) -> PluginSpec:
    """``versionedVendorSuffixed(spec)`` for a plugin with no prior export.

    The vendor suffix is applied unconditionally (Req 13.3). The version bump is
    not: with no prior exported version and no registry entry, ``bump_for_export``
    reports no change, so the exported version is the spec's own. Asserted by
    :meth:`TestTheReconstructionIsFaithful.test_no_version_bump_is_in_play`, so
    this stays an observation rather than an assumption.
    """
    suffixed = copy.deepcopy(spec)
    suffixed.vendor = apply_custom_vendor_suffix(suffixed.vendor)
    return suffixed


def _completeness(plan: ExportPlan) -> CompletenessReport:
    """The preview's completeness report, or a failure if it carries none."""
    assert plan.completeness is not None, "the export preview carried no completeness report at all"
    return plan.completeness


def _top_level_keys(spec: PluginSpec) -> Tuple[str, ...]:
    """The spec's top-level keys as the serialized document would carry them."""
    return tuple(spec.to_mapping().keys())


def _finding_counts(report: CompletenessReport) -> Dict[str, int]:
    """Findings grouped by code, for a failure message that names the shape."""
    counts: Dict[str, int] = {}
    for finding in report.findings:
        counts[finding.code] = counts.get(finding.code, 0) + 1
    return counts


def _outstanding(plan: ExportPlan) -> Tuple[str, ...]:
    """The names of the plan's outstanding definition-of-done conditions."""
    assert plan.done_report is not None, "the export preview carried no definition-of-done report"
    return tuple(condition.name for condition in plan.done_report.outstanding)


def _condition_status(plan: ExportPlan, name: str) -> Optional[ConditionStatus]:
    """One condition's status from the plan's report, or ``None`` if absent."""
    assert plan.done_report is not None, "the export preview carried no definition-of-done report"
    condition = plan.done_report.condition(name)
    return None if condition is None else condition.status


class TestTheReconstructionIsFaithful:
    """The reconstruction's premises, measured rather than asserted in prose.

    These are the *witnesses*. They say that what was reconstructed really is a
    session satisfying ``isBugCondition_3``, and that the tree it ran against
    really is the concrete one. They are expected to pass both before and after
    the fix, because none of them is about ``prepare_export``: the fix changes
    which spec the preview reads, not what the planner can express or what the
    agent wrote.
    """

    def test_the_copy_carries_the_concrete_trees_spec(self, divergence: Divergence):
        """The tree under test is byte-identical to the run's own."""
        assert (divergence.tree / "plugin.spec.yaml").read_text(encoding="utf-8") == divergence.disk_text, (
            "the reconstruction's tree no longer carries the concrete tree's spec, so its figures are not "
            "figures about the JumpCloud plugin"
        )

    def test_implementation_was_delegated(self, divergence: Divergence):
        """``implementationDelegated(X)`` -- the first conjunct, measured."""
        assert divergence.agent_calls == 1, (
            f"the delegated agent ran {divergence.agent_calls} time(s), so this session is not one that "
            "delegated implementation"
        )

    def test_the_agents_spec_is_the_one_on_disk(self, divergence: Divergence):
        """The agent's output reached the tree, which is where it always went."""
        on_disk = (divergence.tree / "plugin.spec.yaml").read_text(encoding="utf-8")
        assert load_plugin_spec(on_disk).to_mapping() == divergence.disk_spec.to_mapping()

    def test_the_pre_implementation_draft_differs_from_the_spec_on_disk(self, divergence: Divergence):
        """``diskSpec(projectFolder(X)) <> draftSpec(X)`` -- the second conjunct.

        Stated over the *pre-implementation* draft rather than over the session's
        draft at export time, deliberately. The divergence is what the turn
        creates; whether it survives to export time is precisely what the fix
        changes (task 4.1 syncs the draft from the tree), so a witness phrased
        over the session's later state would have to fail once the fix lands.
        """
        assert divergence.stale_draft_spec.to_mapping() != divergence.disk_spec.to_mapping(), (
            "the pre-implementation draft and the agent's spec are identical, so this reconstruction is not "
            "an instance of isBugCondition_3"
        )

    def test_no_draft_operation_can_set_the_eleven_absent_fields(self, divergence: Divergence):
        """Why the number is 11 rather than a figure this test chose.

        The planner's vocabulary reaches ``name``, ``title``, ``description``,
        ``version``, ``vendor``, ``connection`` and the components. It reaches
        none of the eleven remaining required top-level fields, so no draft the
        orchestrator can be asked to build carries them. ``sdk`` is the twelfth
        and is present because ``_with_resolved_sdk`` stamps it.
        """
        draft_keys = set(_top_level_keys(divergence.stale_draft_spec))
        absent = tuple(key for key in REQUIRED_TOP_LEVEL if key not in draft_keys)
        assert len(absent) == 11, (
            f"expected the 11 required top-level fields no DraftOperation can set to be absent from the "
            f"draft; {len(absent)} were: {absent}. Draft keys: {sorted(draft_keys)}"
        )
        assert "sdk" in draft_keys, (
            "the draft carries no sdk block, so _with_resolved_sdk did not resolve one and the absent-field "
            f"count would be 12 rather than 11: {sorted(draft_keys)}"
        )

    def test_the_recorded_output_paths_match_the_tree(self, divergence: Divergence):
        """:data:`CONCRETE_OUTPUT_PATHS` is the tree's own set, not a stale copy."""
        on_disk = tuple(
            sorted(
                (action, field)
                for action, component in divergence.disk_spec.actions.items()
                for field in component.output
            )
        )
        assert on_disk == tuple(
            sorted(CONCRETE_OUTPUT_PATHS)
        ), f"the concrete tree's action outputs are not the ones this module generates mutations over: {on_disk}"

    def test_no_version_bump_is_in_play(self, divergence: Divergence):
        """``versionedVendorSuffixed`` is the vendor suffix alone for this plugin.

        With no prior exported version and no registry, ``bump_for_export``
        reports no change, so :func:`_suffixed` is the whole of the expected
        transformation and the comparison below is not weakened by a version.
        """
        for label, plan in (("diverged", divergence.stale_plan), ("control", divergence.control_plan)):
            assert not plan.version_bump.changed, (
                f"the {label} preview bumped the version ({plan.version_display!r}), so the expected "
                "preview is not the vendor-suffixed disk spec alone"
            )


class TestThePreviewJudgesAStaleDraft:
    """`bugfix.md` 1.7 / 2.12 -- the preview must describe what would be packaged.

    Expected to FAIL on unfixed code. ``prepare_export`` derives ``spec_preview``,
    the completeness findings and ``_evaluate_done``'s ``spec=`` from
    ``session.draft``, which no step refreshes from the tree after
    ``_delegate_implementation`` returns.
    """

    def test_the_preview_is_the_spec_on_disk(self, divergence: Divergence):
        """``ASSERT plan.spec_preview = versionedVendorSuffixed(diskSpec(...))``."""
        preview = divergence.stale_plan.spec_preview
        expected = _suffixed(divergence.disk_spec)
        assert preview.to_mapping() == expected.to_mapping(), (
            "the previewed spec is not the spec that would be packaged. The preview carries "
            f"{len(_top_level_keys(preview))} top-level keys {_top_level_keys(preview)} while the file on "
            f"disk carries {len(_top_level_keys(expected))} {_top_level_keys(expected)}"
        )

    def test_the_previews_completeness_findings_are_the_disk_specs(self, divergence: Divergence):
        """``ASSERT completenessFindings(plan) = checkCompleteness(diskSpec(...))``."""
        previewed = _completeness(divergence.stale_plan)
        on_disk = check_completeness(_suffixed(divergence.disk_spec))
        assert previewed.keys() == on_disk.keys(), (
            f"the preview reports {len(previewed.findings)} completeness finding(s) "
            f"{_finding_counts(previewed)} against a file that produces {len(on_disk.findings)}. "
            f"Reported: {previewed.keys()}"
        )

    def test_a_spec_complete_on_disk_yields_no_findings(self, divergence: Divergence):
        """``ASSERT isCompleteOnDisk(X) IMPLIES completenessFindings(plan) = EMPTY``."""
        on_disk = check_completeness(_suffixed(divergence.disk_spec))
        assert (
            on_disk.is_complete
        ), f"the spec on disk is not complete, so this tree cannot demonstrate the implication: {on_disk.keys()}"
        previewed = _completeness(divergence.stale_plan)
        assert not previewed.findings, (
            f"the spec on disk carries every field the toolchain needs -- "
            f"{len(_top_level_keys(divergence.disk_spec))} top-level keys and "
            f"{divergence.disk_text.count('example:')} example entries -- and the preview still reports "
            f"{len(previewed.findings)} completeness error(s): {previewed.keys()}"
        )

    def test_the_reported_findings_are_false_against_the_file_on_disk(self, divergence: Divergence):
        """Each finding named individually, since 1.7's claim is that all are false."""
        previewed = _completeness(divergence.stale_plan)
        disk_mapping = divergence.disk_spec.to_mapping()
        false_findings = []
        for finding in previewed.findings:
            root = finding.path.split(".")[0]
            if finding.code in {"missing_field", "empty_field"} and root in disk_mapping:
                false_findings.append(f"{finding.key} (the file has {root!r})")
            elif finding.code == "invalid_credential_type":
                declared = disk_mapping.get("connection", {}).get("api_key", {}).get("type")
                false_findings.append(f"{finding.key} (the file declares {declared!r})")
            elif finding.code == "output_missing_example":
                false_findings.append(f"{finding.key} (the file carries an example there)")
        assert not false_findings, (
            f"{len(false_findings)} of the preview's {len(previewed.findings)} findings are false against "
            f"the file on disk: {false_findings}"
        )

    def test_the_recorded_counterexample_still_reproduces(self, divergence: Divergence):
        """The measurement `bugfix.md` 1.7 recorded, re-taken.

        Fails on unfixed code by *reproducing* rather than by contradiction, so
        the recorded figure is checked instead of trusted. When the fix lands this
        assertion inverts, which is why it names the fixed expectation too.
        """
        previewed = _completeness(divergence.stale_plan)
        counts = _finding_counts(previewed)
        reproduced = (
            len(previewed.findings) == RECORDED_STALE_FINDING_COUNT
            and counts.get("missing_field") == 11
            and counts.get("invalid_credential_type") == 1
        )
        assert not reproduced, (
            f"the counterexample `bugfix.md` 1.7 records reproduces exactly: "
            f"{RECORDED_STALE_FINDING_COUNT} findings, {counts}, against a file on disk that produces 0. "
            "This assertion is written to fail while the bug is present; when task 4.4 lands, the preview "
            "reports the disk spec's 0 findings and it passes."
        )


class TestTheIterateCustomControl:
    """`bugfix.md` 1.8 -- the same plugin read from disk, which is the contrast.

    ``_start_iterate`` loads the spec with ``load_plugin_spec(folder.spec_path...)``,
    so this session's draft *is* the tree. Expected to pass on unfixed code: it is
    the control, and its passing is what shows the defect is in which spec was
    read rather than in the plugin.
    """

    def test_the_control_reports_no_completeness_findings(self, divergence: Divergence):
        """0 findings -- the first of 1.8's four measurements."""
        previewed = _completeness(divergence.control_plan)
        assert len(previewed.findings) == RECORDED_CONTROL_FINDING_COUNT, (
            f"the control reports {len(previewed.findings)} completeness finding(s) "
            f"{previewed.keys()}, not {RECORDED_CONTROL_FINDING_COUNT}"
        )

    def test_the_control_previews_all_twenty_three_top_level_keys(self, divergence: Divergence):
        """23 top-level keys -- the second."""
        keys = _top_level_keys(divergence.control_plan.spec_preview)
        assert len(keys) == RECORDED_DISK_TOP_LEVEL_KEYS, (
            f"the control's preview carries {len(keys)} top-level keys, " f"not {RECORDED_DISK_TOP_LEVEL_KEYS}: {keys}"
        )
        assert keys == _top_level_keys(_suffixed(divergence.disk_spec))

    def test_the_control_meets_spec_complete(self, graded_divergence: Divergence):
        """``spec_complete`` met -- the third.

        Needs the graded reconstruction only because the condition is reported on
        a :class:`DoneReport` computed beside the other eleven.
        """
        status = _condition_status(graded_divergence.control_plan, CONDITION_SPEC_COMPLETE)
        assert status is ConditionStatus.MET, (
            f"the control reports {CONDITION_SPEC_COMPLETE}={status}, though the spec on disk carries every "
            "field the toolchain needs"
        )

    def test_the_control_has_two_outstanding_conditions(self, graded_divergence: Divergence):
        """2 outstanding -- the fourth, and the one figure here that is F-specific.

        ``formatted`` is Bug 2 (`bugfix.md` 1.5) and ``api_client`` is 1.9, so this
        count is a record of the tool as it behaves at ``e7726b7``, not an
        invariant: tasks 5 and 7 close both, and when they land this expectation
        drops with them. It is asserted rather than merely printed because 1.8's
        claim is that the control differs from the diverged session by exactly one
        condition, and that claim needs both numbers.
        """
        outstanding = _outstanding(graded_divergence.control_plan)
        assert len(outstanding) == RECORDED_CONTROL_OUTSTANDING, (
            f"the control reports {len(outstanding)} outstanding condition(s) {outstanding}, not the "
            f"{RECORDED_CONTROL_OUTSTANDING} `bugfix.md` 1.8 records. If tasks 5 (api_client) or 7 "
            "(formatted) have landed, this recorded figure is what changed"
        )
        assert (
            CONDITION_SPEC_COMPLETE not in outstanding
        ), f"{CONDITION_SPEC_COMPLETE} is outstanding for a spec that is complete on disk: {outstanding}"

    def test_the_two_sessions_report_the_same_conditions(self, graded_divergence: Divergence):
        """1.8's actual claim -- same plugin, same code, same stages.

        Expected to FAIL on unfixed code: the diverged session reports one more
        outstanding condition than the control, and the extra one is
        ``spec_complete``.
        """
        diverged = _outstanding(graded_divergence.stale_plan)
        control = _outstanding(graded_divergence.control_plan)
        assert diverged == control, (
            f"the same plugin reports {len(diverged)} outstanding condition(s) {diverged} in the session "
            f"that built it and {len(control)} {control} when reopened from disk. The difference is "
            f"{tuple(sorted(set(diverged) - set(control)))}, and nothing about the plugin differs between "
            "the two -- only which spec was read"
        )

    def test_the_diverged_session_reports_spec_complete_unmet(self, graded_divergence: Divergence):
        """The recorded 3-versus-2, re-taken from the diverged side.

        Fails on unfixed code by reproducing `bugfix.md` 1.8's figure. Inverts
        when task 4.4 lands.
        """
        status = _condition_status(graded_divergence.stale_plan, CONDITION_SPEC_COMPLETE)
        outstanding = _outstanding(graded_divergence.stale_plan)
        assert status is ConditionStatus.MET, (
            f"the diverged session reports {CONDITION_SPEC_COMPLETE}={status} and "
            f"{len(outstanding)} outstanding condition(s) {outstanding}, against a plugin whose spec on disk "
            f"is complete. `bugfix.md` 1.8 records {RECORDED_STALE_OUTSTANDING} here and "
            f"{RECORDED_CONTROL_OUTSTANDING} for the control"
        )


#: How a generated example mutates the concrete tree's spec on disk. Scoped to
#: that tree's own content -- required-field removals, the credential type, and
#: its four output examples -- rather than generating whole plugins, per task 1's
#: parent text. The generalization over trees arrives with Property 63 (task 4.8).
_DISK_MUTATIONS = st.fixed_dictionaries(
    {
        "dropped": st.lists(st.sampled_from(REQUIRED_TOP_LEVEL), unique=True, max_size=4).map(tuple),
        "credential_type": st.sampled_from(
            ("credential_secret_key", "credential_token", "credential_username_password", "credential_asymmetric_key")
        ),
        "stripped_examples": st.lists(st.sampled_from(CONCRETE_OUTPUT_PATHS), unique=True, max_size=4).map(tuple),
    }
)


def _mutate_on_disk(tree: Path, base: PluginSpec, mutation: Mapping[str, Any]) -> PluginSpec:
    """Write a mutated spec into ``tree`` and return what a reader would parse.

    Each example writes the whole file, so the tree's state is a function of the
    example alone and no example depends on its predecessors.
    """
    mapping: Dict[str, Any] = copy.deepcopy(base.to_mapping())
    for key in mutation["dropped"]:
        mapping.pop(key, None)
    connection = mapping.get("connection")
    if isinstance(connection, dict) and isinstance(connection.get("api_key"), dict):
        connection["api_key"]["type"] = mutation["credential_type"]
    for action, field in mutation["stripped_examples"]:
        outputs = mapping.get("actions", {}).get(action, {}).get("output", {})
        if isinstance(outputs.get(field), dict):
            outputs[field].pop("example", None)

    text = dump_plugin_spec(PluginSpec.from_mapping(mapping))
    (tree / "plugin.spec.yaml").write_text(text, encoding="utf-8")
    return load_plugin_spec(text)


class TestThePreviewIsIndependentOfTheTree:
    """The content of Bug 3 stated as a universal: the tree does not reach the preview.

    One mismatch shows the preview disagreed with the tree once. Quantifying over
    edits to the tree's spec shows something stronger and more diagnostic -- that
    the preview does not vary with the tree **at all**, because it is computed
    from the draft. Generation is cheap here (a spec write plus a preview, no
    Docker and no toolchain), and the claim is genuinely universal over the file's
    content, which is why this one clause is property-based while the rest are
    scoped to the concrete tree's recorded figures.
    """

    # Feature: export-gate-and-preview-fidelity, Property 1: Bug Condition
    @settings(max_examples=100, deadline=None)
    @given(mutation=_DISK_MUTATIONS)
    def test_the_previews_findings_track_the_spec_on_disk(self, divergence: Divergence, mutable_tree: Path, mutation):
        """**Validates: Requirements 1.7, 1.8**

        Expected to FAIL on unfixed code: whatever the file says, the preview
        reports the stale draft's findings.
        """
        try:
            on_disk = _mutate_on_disk(mutable_tree, divergence.disk_spec, mutation)
            plan = asyncio.run(_prepared(mutable_tree, divergence.stale_draft_spec))
            expected = check_completeness(_suffixed(on_disk))
            previewed = _completeness(plan)
            assert previewed.keys() == expected.keys(), (
                f"the file on disk produces {len(expected.findings)} finding(s) {expected.keys()} and the "
                f"preview reports {len(previewed.findings)} {previewed.keys()}; the preview did not change "
                f"with the tree. Mutation: {mutation}"
            )
        finally:
            (mutable_tree / "plugin.spec.yaml").write_text(divergence.disk_text, encoding="utf-8")


async def _prepared(tree: Path, stale_draft_spec: PluginSpec) -> ExportPlan:
    """Compute one preview for a session whose draft is ``stale_draft_spec``.

    The session is rebuilt per example rather than reused so that no example can
    observe another's state. It is assembled through ``iterate_custom`` -- the one
    entry mode that can adopt an existing tree -- and its draft spec is then set
    to the pre-implementation draft, which is the state a delegated turn leaves
    behind. ``prepare_export`` reads no entry mode, so the two are the same input
    as far as the code under test is concerned.
    """
    orchestrator, _ = _orchestrator(tree, "", graded=False)
    session = orchestrator.start_session(
        ENTRY_MODE_ITERATE_CUSTOM,
        session_id="mutated",
        user_id="tester",
        plugin_name=tree.name,
    )
    session.draft = Draft(spec=copy.deepcopy(stale_draft_spec), code_files=dict(session.draft.code_files))
    session.baseline_spec = copy.deepcopy(stale_draft_spec)
    session.last_exported_spec = None
    return await orchestrator.prepare_export("mutated")


# ---------------------------------------------------------------------------
# Task 1.7 -- the write-back: Bug 3 reaches the artifact, not only the report.
#
# Task 1.6 above shows the preview *reports* a stale spec. This section shows it
# *ships* one, which is what changes the bug's severity: a mis-report costs the
# operator trust, a mis-packaged artifact costs the tenant a wrong plugin. So the
# subject here is the bytes inside the produced ``.plg``, not an ``ExportPlan``.
#
# The mechanism, read out of the code rather than inferred:
# ``confirm_export`` commits ``plan.spec_preview`` to the session draft and calls
# ``_build_dir``, which for a session with a project folder does
# ``project_folder.save(export_spec, generated_files=_as_generated(session.draft.code_files))``
# -- ``ProjectFolder.save`` then ``_write_spec(spec)`` (the stale preview) and
# ``_write_generated(files)`` (every entry of the draft's code-file map, each
# ``target.write_text``/``write_bytes`` unconditional). Only then does
# ``BuildEngine.package`` read the tree. The tree is therefore rewritten from the
# draft *before* anything is packaged, and the archive is a faithful copy of a
# tree the export just made stale.
#
# ``force`` is required because the gate blocks: Bugs 1 and 2 fail the ``test``
# and ``lint`` stages for every plugin, and in the ungraded reconstruction used
# here no pipeline runs at all, which ``decide_export`` also treats as not passed
# (``CODE_NOT_VALIDATED_MESSAGE``). Either way the override exercised is the same
# one -- ``getattr(plan, "_force", False)`` in ``confirm_export``, set through
# ``object.__setattr__`` exactly as ``api/app.py`` sets it, since ``ExportPlan``
# is a frozen dataclass. `bugfix.md` records that path's crash as already fixed in
# the working tree, so a forced export is expected to complete (3.12).
#
# Every fixture below takes its **own** copy of the tree, because unlike task
# 1.6's reads an export writes deliberately.
#
# _Requirements: 1.7_
# ---------------------------------------------------------------------------

#: The archive member the primary claim is about.
SPEC_MEMBER = "plugin.spec.yaml"

#: What `bugfix.md` 1.7 records about the file on disk, held so a re-measurement
#: that differs is visible rather than absorbed into a new expectation.
RECORDED_DISK_CREDENTIAL_TYPE = "credential_secret_key"
RECORDED_DISK_EXAMPLE_ENTRIES = 12

#: The stale draft's top-level key count, from task 1.6's measurement.
RECORDED_STALE_TOP_LEVEL_KEYS = 9

#: Appended to a real hand-written module to stand in for a change the agent
#: makes after the draft was read. Its *content* is immaterial -- the claim is
#: about which side of the divergence reaches the tree and the archive, and a
#: sentinel is simply what makes that write observable. Nothing about the agent's
#: own output is synthesised: the recorded ``plugin.spec.yaml`` is still replayed
#: verbatim by :class:`RecordedAgent`, and the tree's source files are the real
#: ones throughout.
POST_OPEN_SENTINEL = "\n# a change made after the draft was read\n"


def _archive_members(artifact_path: Path) -> Tuple[str, ...]:
    """Every member name in the produced ``.plg`` (a gzipped tarball, Req 9.1)."""
    with tarfile.open(artifact_path, mode="r:gz") as archive:
        return tuple(archive.getnames())


def _member_text(artifact_path: Path, member: str) -> str:
    """Read one member out of the ``.plg`` as text.

    This is the whole point of the task: the assertion is about what the artifact
    carries, so the artifact is opened rather than the tree it was built from.
    """
    with tarfile.open(artifact_path, mode="r:gz") as archive:
        try:
            extracted = archive.extractfile(member)
        except KeyError:  # pragma: no cover - reported by the assertion below
            extracted = None
        assert extracted is not None, f"the .plg at {artifact_path} carries no {member!r} member"
        with extracted:
            return extracted.read().decode("utf-8")


def _credential_type(spec: PluginSpec) -> Any:
    """The connection's ``api_key`` type as the serialized document carries it."""
    connection = spec.to_mapping().get("connection")
    if not isinstance(connection, dict):
        return None
    api_key = connection.get("api_key")
    return api_key.get("type") if isinstance(api_key, dict) else None


def _output_examples(spec: PluginSpec) -> Dict[str, Any]:
    """``"<action>.<output>"`` -> ``example`` for every action output carrying one."""
    examples: Dict[str, Any] = {}
    actions = spec.to_mapping().get("actions")
    if not isinstance(actions, dict):
        return examples
    for action, definition in actions.items():
        outputs = definition.get("output") if isinstance(definition, dict) else None
        if not isinstance(outputs, dict):
            continue
        for field, schema in outputs.items():
            if isinstance(schema, dict) and "example" in schema:
                examples[f"{action}.{field}"] = schema["example"]
    return examples


def _api_module(tree: Path) -> Optional[Path]:
    """The tree's hand-written API client, or ``None`` if it is not where expected."""
    matches = sorted(tree.glob("icon_*/util/api.py"))
    return matches[0] if matches else None


class ForcedExport:
    """One forced export from the diverged session, and what it actually shipped.

    A plain class for the same reason :class:`Divergence` is one: nothing here
    should be expanded field by field into a failure message.

    Attributes:
        divergence: the reconstruction the export was run from.
        outcome: the :class:`ExportOutcome` ``confirm_export`` returned.
        artifact_path: the produced ``.plg``.
        members: its member names.
        shipped_text: the ``plugin.spec.yaml`` read back **out of the archive**.
        shipped_spec: that text, parsed.
        tree_text_after: the tree's ``plugin.spec.yaml`` after the export, which
            is what the export-time write-back left behind.
        draft_code_files: how many entries the draft's code-file map held at
            export time -- the second half of the write-back, measured rather
            than assumed.
    """

    def __init__(
        self,
        *,
        divergence: Divergence,
        outcome: ExportOutcome,
        artifact_path: Path,
        members: Tuple[str, ...],
        shipped_text: str,
        shipped_spec: PluginSpec,
        tree_text_after: str,
        draft_code_files: int,
    ) -> None:
        self.divergence = divergence
        self.outcome = outcome
        self.artifact_path = artifact_path
        self.members = members
        self.shipped_text = shipped_text
        self.shipped_spec = shipped_spec
        self.tree_text_after = tree_text_after
        self.draft_code_files = draft_code_files

    def __repr__(self) -> str:
        return f"ForcedExport(artifact={self.artifact_path.name}, members={len(self.members)})"


async def _force_export(tree: Path, output_dir: Path) -> ForcedExport:
    """Reconstruct the diverged session over ``tree`` and force an export from it.

    The export is confirmed on **the session that delegated implementation**, which
    is the whole condition: a different session (the ``iterate_custom`` control,
    say) would read the tree at open time and ship the right spec for the wrong
    reason.
    """
    divergence = await _reconstruct(tree, graded=False)
    orchestrator = divergence.orchestrator
    assert orchestrator is not None, "the reconstruction did not carry its orchestrator"
    session = orchestrator.session("diverged")
    draft_code_files = len(session.draft.code_files)

    plan = divergence.stale_plan
    # As api/app.py does it: ExportPlan is frozen, so a plain assignment raises.
    object.__setattr__(plan, "_force", True)
    outcome = await orchestrator.confirm_export(
        "diverged",
        plan,
        confirmed=True,
        target="local",
        output_dir=output_dir,
    )
    assert (
        outcome.artifact_path is not None
    ), f"the forced export produced no artifact: {outcome.status} {outcome.message}"

    artifact_path = Path(outcome.artifact_path)
    shipped_text = _member_text(artifact_path, SPEC_MEMBER)
    return ForcedExport(
        divergence=divergence,
        outcome=outcome,
        artifact_path=artifact_path,
        members=_archive_members(artifact_path),
        shipped_text=shipped_text,
        shipped_spec=load_plugin_spec(shipped_text),
        tree_text_after=(tree / SPEC_MEMBER).read_text(encoding="utf-8"),
        draft_code_files=draft_code_files,
    )


@pytest.fixture(scope="module")
def forced_export(tmp_path_factory) -> ForcedExport:
    """One forced export, on its own copy of the tree because it writes.

    Module-scoped so the turn, the preview, the write-back and the packaging
    happen once for every assertion below.
    """
    _require_tree()
    root = tmp_path_factory.mktemp("preview_fidelity_export")
    return asyncio.run(_force_export(_copy_tree(root), root / "artifacts"))


class TestTheForcedExportShipsTheStaleDraft:
    """`bugfix.md` 1.7, second half -- the write-back reaches the artifact.

    Expected to FAIL on unfixed code. Task 4.5 is the fix: with the draft a view
    of the tree (4.1) the spec write becomes the version bump and vendor suffix
    only, which is correct and stays; the code-file map write goes away for a
    session that has a project folder.
    """

    def test_the_gate_blocked_the_export_so_force_was_required(self, forced_export: ForcedExport):
        """The premise: nothing here would have exported without ``force``.

        Expected to pass on unfixed code, and to keep passing -- the ungraded
        reconstruction runs no pipeline, and ``decide_export`` treats a missing
        pipeline report as not passed. On the reproduction host the graded run
        blocks for the stronger reason Bugs 1 and 2 give it: ``lint`` and ``test``
        fail for every plugin.
        """
        plan = forced_export.divergence.stale_plan
        assert not plan.permitted, (
            "the gate permitted this export, so `force` was not the only route to an artifact and the "
            f"premise of 1.7 does not hold here: {plan.decision.summary()}"
        )

    def test_the_forced_export_succeeded(self, forced_export: ForcedExport):
        """Preservation 3.12 -- a forced export still completes and is recorded.

        Passes on unfixed code (the frozen-dataclass crash `bugfix.md` records is
        already fixed in the working tree) and must keep passing: this test would
        otherwise be unable to tell "the fix worked" from "the export broke".
        """
        assert forced_export.outcome.succeeded, (
            f"the forced export did not succeed ({forced_export.outcome.status}): " f"{forced_export.outcome.message}"
        )
        assert forced_export.artifact_path.is_file(), f"no artifact at {forced_export.artifact_path}"

    def test_the_archive_carries_a_spec_at_all(self, forced_export: ForcedExport):
        """A witness for the reads below, so a packaging change cannot read as Bug 3."""
        assert SPEC_MEMBER in forced_export.members, (
            f"the .plg carries no {SPEC_MEMBER}; its {len(forced_export.members)} members are "
            f"{forced_export.members}"
        )

    def test_the_plg_carries_the_spec_the_agent_wrote(self, forced_export: ForcedExport):
        """The claim itself: the packaged spec is the one on disk, vendor-suffixed.

        Expected to FAIL on unfixed code with the stale draft's spec, because
        ``_build_dir`` wrote ``plan.spec_preview`` over the tree before
        ``BuildEngine.package`` read it.
        """
        shipped = forced_export.shipped_spec
        expected = _suffixed(forced_export.divergence.disk_spec)
        assert shipped.to_mapping() == expected.to_mapping(), (
            f"the .plg ships a spec with {len(_top_level_keys(shipped))} top-level keys "
            f"{_top_level_keys(shipped)} where the agent wrote {len(_top_level_keys(expected))} "
            f"{_top_level_keys(expected)}. The exported artifact carries the draft the preview judged, not "
            "the plugin the agent built"
        )

    def test_the_shipped_spec_declares_the_credential_type_on_disk(self, forced_export: ForcedExport):
        """The one field 1.7 names by value, followed all the way to the artifact.

        Expected to FAIL on unfixed code: the interpreter's ``credential_token``
        ships in place of the agent's ``credential_secret_key``, so the wrong
        connection type reaches the tenant rather than merely the report.
        """
        shipped = _credential_type(forced_export.shipped_spec)
        on_disk = _credential_type(forced_export.divergence.disk_spec)
        assert on_disk == RECORDED_DISK_CREDENTIAL_TYPE, (
            f"the file on disk declares {on_disk!r}, not the {RECORDED_DISK_CREDENTIAL_TYPE!r} "
            "`bugfix.md` 1.7 records, so this tree is not the one that figure was taken from"
        )
        assert shipped == on_disk, (
            f"the .plg declares connection.api_key.type={shipped!r} where the agent wrote {on_disk!r}. "
            f"{RECORDED_DRAFT_CREDENTIAL_TYPE!r} is the interpreter's value from the pre-implementation "
            "draft, and it was packaged"
        )

    def test_the_shipped_spec_carries_the_agents_output_examples(self, forced_export: ForcedExport):
        """The examples ``insight-plugin validate`` needs, in the artifact.

        Expected to FAIL on unfixed code: the pre-implementation draft has none,
        so the shipped spec has none, and the plugin that ships is one the
        toolchain would reject for the reason the preview named.
        """
        shipped = _output_examples(forced_export.shipped_spec)
        on_disk = _output_examples(forced_export.divergence.disk_spec)
        assert len(on_disk) == len(CONCRETE_OUTPUT_PATHS), (
            f"the file on disk carries {len(on_disk)} output example(s) {sorted(on_disk)}, not one per "
            f"output; this tree is not the one 1.7's figures came from"
        )
        assert shipped == on_disk, (
            f"the .plg carries {len(shipped)} output example(s) {sorted(shipped)} where the agent wrote "
            f"{len(on_disk)} {sorted(on_disk)}. The file on disk carries "
            f"{forced_export.divergence.disk_text.count('example:')} `example:` entries in total "
            f"(`bugfix.md` 1.7 records {RECORDED_DISK_EXAMPLE_ENTRIES}) and the artifact carries "
            f"{forced_export.shipped_text.count('example:')}"
        )

    def test_the_shipped_spec_is_complete(self, forced_export: ForcedExport):
        """What the artifact would be judged as, on its own terms.

        The preview's 16 findings are false against the file on disk (task 1.6).
        They are **true** against the file in the ``.plg``, which is the sharpest
        statement of why this is worse than a reporting defect: the export makes
        the complaint accurate by shipping the thing complained about.
        """
        shipped = check_completeness(forced_export.shipped_spec)
        assert shipped.is_complete, (
            f"the packaged spec is incomplete: {len(shipped.findings)} finding(s) "
            f"{_finding_counts(shipped)}. Those are the findings the preview reported and task 1.6 shows "
            f"are false against the file on disk -- the export made them true. Reported: {shipped.keys()}"
        )

    def test_the_export_left_the_agents_spec_on_the_tree(self, forced_export: ForcedExport):
        """Where the artifact's content comes from: the tree, rewritten first.

        Expected to FAIL on unfixed code. Asserted separately from the archive
        because it localizes the defect to ``_build_dir``'s ``save`` rather than to
        packaging -- ``BuildEngine`` is read-only with respect to the tree (Req
        9.5), so a wrong archive means a wrong tree.
        """
        after = load_plugin_spec(forced_export.tree_text_after)
        expected = _suffixed(forced_export.divergence.disk_spec)
        assert after.to_mapping() == expected.to_mapping(), (
            f"after the export the working tree carries {len(_top_level_keys(after))} top-level keys "
            f"{_top_level_keys(after)} where the agent left {len(_top_level_keys(expected))}. The export "
            "wrote the draft over the agent's spec before packaging, so the plugin's own working tree no "
            "longer holds what was built"
        )

    def test_the_recorded_counterexample_still_reproduces(self, forced_export: ForcedExport):
        """The write-back identified positively, not just as a mismatch.

        Written to fail while the bug is present, in the same shape as task 1.6's
        equivalent: it asserts the shipped spec is **not** the stale draft, and
        fails by naming the exact identity when it is. When task 4.5 lands, the
        artifact carries the agent's spec and this passes.
        """
        shipped = forced_export.shipped_spec.to_mapping()
        stale = _suffixed(forced_export.divergence.stale_draft_spec).to_mapping()
        assert shipped != stale, (
            "the spec inside the .plg is byte-for-byte the vendor-suffixed pre-implementation draft: "
            f"{len(_top_level_keys(forced_export.shipped_spec))} top-level keys (`bugfix.md`/task 1.6 "
            f"record {RECORDED_STALE_TOP_LEVEL_KEYS} for the draft, {RECORDED_DISK_TOP_LEVEL_KEYS} for the "
            "file on disk). A forced export ships the spec the preview complained about, which is the "
            "counterexample task 1.7 exists to record"
        )


class RevertedExport:
    """A forced export from a session whose draft holds a stale code-file map.

    The second half of the write-back. ``_build_dir`` passes
    ``generated_files=_as_generated(session.draft.code_files)`` and
    ``ProjectFolder._write_generated`` writes every entry unconditionally, so any
    file the tree gained after the draft was read is overwritten from memory.

    The map is empty for a ``create_new`` session -- nothing populates it, which
    :meth:`TestTheDraftsCodeFileMapIsWrittenOverTheTree.test_the_diverged_sessions_draft_carries_no_code_files`
    measures -- so this half needs the entry modes that do populate it.
    ``_start_iterate`` and ``_start_enhance`` both set
    ``code_files=_read_dir_tree(folder.path)``: a snapshot taken at open time and
    never refreshed, which is exactly the state a delegated turn leaves behind.

    Attributes:
        tree: the copied tree this export ran in.
        member: the hand-written module the post-open change was made to.
        text_before: its content when the draft was read.
        text_after_change: its content after the post-open change.
        snapshot_text: what the draft's code-file map holds for it.
        draft_code_files: the map's size, so "the map is populated" is measured.
        outcome: the :class:`ExportOutcome`.
        artifact_path: the produced ``.plg``.
        tree_text_after: the module's content in the tree after the export.
        shipped_text: the module's content read out of the archive.
        tree_spec_after: the tree's ``plugin.spec.yaml`` after the export.
        shipped_spec_text: the archive's ``plugin.spec.yaml``.
        export_spec: the spec ``confirm_export`` was asked to commit.
    """

    def __init__(
        self,
        *,
        tree: Path,
        member: str,
        text_before: str,
        text_after_change: str,
        snapshot_text: Any,
        draft_code_files: int,
        outcome: ExportOutcome,
        artifact_path: Path,
        tree_text_after: str,
        shipped_text: str,
        tree_spec_after: str,
        shipped_spec_text: str,
        export_spec: PluginSpec,
    ) -> None:
        self.tree = tree
        self.member = member
        self.text_before = text_before
        self.text_after_change = text_after_change
        self.snapshot_text = snapshot_text
        self.draft_code_files = draft_code_files
        self.outcome = outcome
        self.artifact_path = artifact_path
        self.tree_text_after = tree_text_after
        self.shipped_text = shipped_text
        self.tree_spec_after = tree_spec_after
        self.shipped_spec_text = shipped_spec_text
        self.export_spec = export_spec

    def __repr__(self) -> str:
        return f"RevertedExport(member={self.member}, draft_code_files={self.draft_code_files})"


async def _export_after_a_post_open_change(tree: Path, output_dir: Path) -> RevertedExport:
    """Open ``tree`` in ``iterate_custom``, change it, then force an export.

    The order is the run's order with the draft's two halves swapped in: the draft
    is read from the tree, the tree then moves on, and the export decides which of
    the two wins. Everything but the post-open change is the real path --
    ``_start_iterate``, ``prepare_export``, ``confirm_export``,
    ``BuildEngine.package``.
    """
    orchestrator, _ = _orchestrator(tree, "", graded=False)
    session = orchestrator.start_session(
        ENTRY_MODE_ITERATE_CUSTOM,
        session_id="reopened",
        user_id="tester",
        plugin_name=tree.name,
    )

    api_module = _api_module(tree)
    assert api_module is not None, f"no icon_*/util/api.py under {tree}; the tree is not the expected shape"
    member = api_module.relative_to(tree).as_posix()
    text_before = api_module.read_text(encoding="utf-8")
    snapshot_text = session.draft.code_files.get(member)
    draft_code_files = len(session.draft.code_files)

    text_after_change = text_before + POST_OPEN_SENTINEL
    api_module.write_text(text_after_change, encoding="utf-8")

    plan = await orchestrator.prepare_export("reopened")
    object.__setattr__(plan, "_force", True)
    outcome = await orchestrator.confirm_export(
        "reopened",
        plan,
        confirmed=True,
        target="local",
        output_dir=output_dir,
    )
    assert (
        outcome.artifact_path is not None
    ), f"the forced export produced no artifact: {outcome.status} {outcome.message}"

    artifact_path = Path(outcome.artifact_path)
    return RevertedExport(
        tree=tree,
        member=member,
        text_before=text_before,
        text_after_change=text_after_change,
        snapshot_text=snapshot_text,
        draft_code_files=draft_code_files,
        outcome=outcome,
        artifact_path=artifact_path,
        tree_text_after=api_module.read_text(encoding="utf-8"),
        shipped_text=_member_text(artifact_path, member),
        tree_spec_after=(tree / SPEC_MEMBER).read_text(encoding="utf-8"),
        shipped_spec_text=_member_text(artifact_path, SPEC_MEMBER),
        export_spec=plan.spec_preview,
    )


@pytest.fixture(scope="module")
def reverted_export(tmp_path_factory) -> RevertedExport:
    """The code-file half of the write-back, on its own copy of the tree."""
    _require_tree()
    root = tmp_path_factory.mktemp("preview_fidelity_codefiles")
    return asyncio.run(_export_after_a_post_open_change(_copy_tree(root), root / "artifacts"))


class TestTheDraftsCodeFileMapIsWrittenOverTheTree:
    """The write-back's second half, which task 4.5 splits from the first.

    4.5 keeps the spec write (it is the version bump and the vendor suffix, which
    are required) and drops the code-file map write when a project folder exists.
    These two halves are measured separately here so that split has something to
    verify against.
    """

    def test_the_diverged_sessions_draft_carries_no_code_files(self, forced_export: ForcedExport):
        """Why the diverged session's artifact is wrong only in its spec.

        A ``create_new`` draft's code-file map starts empty and nothing populates
        it: no :class:`DraftOperation` writes code, ``_dispatch_reasoning``'s
        artifacts go to ``session.generated``, and the delegated agent writes to
        the tree rather than back into the draft. An empty mapping is falsy, so
        ``ProjectFolder.save`` skips ``_write_generated`` entirely. Measured rather
        than argued, and expected to pass before and after the fix.
        """
        assert forced_export.draft_code_files == 0, (
            f"the diverged session's draft carried {forced_export.draft_code_files} code file(s) at export "
            "time; the source-file half of the write-back was in play for the artifact above too, and its "
            "assertions are about more than the spec"
        )

    def test_a_reopened_sessions_draft_carries_the_whole_tree(self, reverted_export: RevertedExport):
        """The premise of this half: the map is populated, and it is a snapshot.

        ``_start_iterate`` sets ``code_files=_read_dir_tree(folder.path)``, so the
        map holds the tree as it was at open time -- including the file changed
        afterwards, at its pre-change content. Expected to pass before and after
        the fix; the fix changes what the export does with the map, not what the
        entry mode reads.
        """
        assert reverted_export.draft_code_files > 0, (
            "the reopened session's draft carried no code files, so this reconstruction cannot show what "
            "the map does to the tree"
        )
        assert reverted_export.snapshot_text == reverted_export.text_before, (
            f"the draft's map does not hold the pre-change content of {reverted_export.member}, so the "
            "divergence this test needs is not the one set up"
        )

    def test_the_tree_keeps_a_change_made_after_the_draft_was_read(self, reverted_export: RevertedExport):
        """Expected to FAIL on unfixed code: the map reverts the tree's own file.

        This is the source-file counterpart of the spec write-back. Anything the
        agent wrote after the draft was read -- which in a reopened session is
        everything it wrote -- is overwritten from memory at export time.
        """
        assert reverted_export.tree_text_after == reverted_export.text_after_change, (
            f"the export reverted {reverted_export.member} in the working tree to the content the draft "
            f"held when the session opened ({len(reverted_export.text_before)} chars) rather than leaving "
            f"the tree's own ({len(reverted_export.text_after_change)} chars). "
            "ProjectFolder._write_generated writes every entry of the draft's code-file map unconditionally"
        )

    def test_the_plg_carries_a_change_made_after_the_draft_was_read(self, reverted_export: RevertedExport):
        """Expected to FAIL on unfixed code: and so the reverted file is packaged."""
        assert reverted_export.shipped_text == reverted_export.text_after_change, (
            f"the .plg carries {reverted_export.member} at the draft's older content "
            f"({len(reverted_export.shipped_text)} chars, sentinel present: "
            f"{POST_OPEN_SENTINEL.strip()!r} -> {POST_OPEN_SENTINEL.strip() in reverted_export.shipped_text}). "
            "The artifact ships source the tree no longer has"
        )

    def test_the_code_file_map_does_not_undo_the_export_spec_write(self, reverted_export: RevertedExport):
        """A consequence not recorded in `bugfix.md`, surfaced by this measurement.

        ``plugin.spec.yaml`` is itself an entry in the map ``_read_dir_tree``
        produces, and ``ProjectFolder.save`` writes the spec **first** and the map
        **second**. So for a reopened session the map's copy of the spec lands on
        top of the export spec, and the vendor suffix Req 13.3 requires before any
        export is undone in both the tree and the artifact.

        Expected to FAIL on unfixed code. Recorded here rather than filed
        separately because task 4.5's fix -- passing no ``generated_files`` when a
        project folder exists -- closes it as a side effect, and a finding closed
        by a fix should be visible to that fix's verification.
        """
        expected_vendor = reverted_export.export_spec.vendor
        shipped_vendor = load_plugin_spec(reverted_export.shipped_spec_text).vendor
        tree_vendor = load_plugin_spec(reverted_export.tree_spec_after).vendor
        assert (shipped_vendor, tree_vendor) == (expected_vendor, expected_vendor), (
            f"the export committed vendor {expected_vendor!r}, and after the code-file map was written the "
            f"artifact carries {shipped_vendor!r} and the tree {tree_vendor!r}. The map's stale "
            f"{SPEC_MEMBER} overwrote the spec save, so the _custom vendor suffix never reached the "
            "packaged plugin"
        )


# ---------------------------------------------------------------------------
# Task 1.10, orchestrator half -- progress (1.12, 1.14), token accounting (1.13),
# interpreter truncation (1.15) and ``version_display`` (1.16)
# ---------------------------------------------------------------------------
#
# Task 1.10 lists seven counterexamples across two layers. The two that live in
# the integrations layer -- what a blocked export reports (1.11) and which
# credential types the toolchain defines (1.17) -- are in
# ``tests/integrations/test_export_gate_bug_conditions.py``. The five here are the
# orchestrator-layer group:
#
#   * **1.12 / 2.17** -- a turn ending in a clarification request has already
#     emitted ``"Generating logic for N action(s)..."``.
#   * **1.13 / 2.18** -- ``token_total`` stays 0 across two interpreter calls and
#     then jumps after the agent run, so interpreter usage appears uncounted.
#   * **1.14 / 2.19** -- no frame for 13 minutes during a delegated run.
#   * **1.15 / 2.20** -- an attachment over 60,000 characters is truncated
#     silently at ``orchestrator/interpreter.py:245``.
#   * **1.16 / 2.21** -- ``version_display`` is empty in the returned preview.
#
# **No LLM call and no ``kiro-cli`` anywhere in this section.** Two substitutions
# stand in for the two processes, and neither synthesises a result the claims turn
# on:
#
#   * :func:`_fake_cli` writes a Python script that reads its prompt from stdin,
#     records it, and prints a fixed response. The *process boundary is real* --
#     :class:`Interpreter` and :class:`PluginAgent` launch it with
#     ``create_subprocess_exec`` exactly as they launch the CLI -- which is what
#     makes the truncation measurement a measurement: the prompt asserted against
#     is the bytes the child actually received, not a string this module built.
#   * :class:`RecordedAgent`, already used by task 1.6, gains an optional
#     ``block_seconds`` so a delegated run can be held open for a known interval.
#     The emission structure around it is the real one.
#
# **1.14 is measured as emission structure, not as a 13-minute wait.** What the
# reproduction run observed (last frame at 9.31s, next at 780.34s) is a gap
# proportional to how long the agent took. So a stand-in agent is held open for
# :data:`DELEGATED_BLOCK_SECONDS` and the frames are timestamped as they arrive at
# a real websocket client; the claim asserted is that the longest silence is
# shorter than the delegated phase, which is scale-free.
#
# **1.16 is a diagnosis, not a defect report.** Task 11.5 says diagnose before
# editing and the design says an empty display may be Requirement 12.7 behaving
# correctly. Both cases are therefore measured -- a plugin with no prior export,
# and the same plugin after one -- and the classes below are named for what they
# found. A nil finding is the outcome for the first case.
#
# Every fixture here works on its own :func:`_copy_tree` copy, because the turns
# and exports below write.
#
# _Requirements: 1.12, 1.13, 1.14, 1.15, 1.16_

#: How long the stand-in agent holds a delegated run open. Long enough that a
#: silence spanning it is unambiguous, short enough not to dominate the suite.
DELEGATED_BLOCK_SECONDS = 3.0

#: The reporting interval this test's 3-second block assumes the fix will not
#: exceed. 2.19 and task 11.2 require "periodic progress carrying the current
#: step" without naming a period, so the figure is stated here rather than left
#: implicit; task 12.4 is where the real interval gets pinned.
ASSUMED_PROGRESS_INTERVAL_SECONDS = 1.0

#: How far inside the delegated phase a frame has to land to count as progress
#: reported *during* it. Exists because the frame the route sends just before
#: entering the phase is received by the client a few milliseconds later, which
#: without a margin reads as progress it is not.
PROGRESS_MARGIN_SECONDS = 0.5

#: The gap `bugfix.md` 1.14 recorded, in seconds: last frame at 9.31s, next at
#: 780.34s. Held as evidence of scale, not asserted -- reproducing 13 minutes of
#: silence would mean waiting 13 minutes.
RECORDED_SILENCE_SECONDS = 780.34 - 9.31

#: The progress message 1.12 names, with the run's own action count.
RECORDED_GENERATION_STATUS = "Generating logic for 3 action(s)..."

#: The vendor the run's interpreter identified, which is what makes a turn that
#: would implement code end in a request for documentation (Req 28.12).
RECORDED_VENDOR_API = "JumpCloud"

#: The interpreter's attachment cap and the line `bugfix.md` 1.15 cites.
INTERPRETER_ATTACHMENT_CAP = 60_000
RECORDED_TRUNCATION_LINE = 245

#: The marker the interpreter appends in place of what it dropped. The user never
#: sees it -- it goes into the prompt, not into any report.
TRUNCATION_MARKER = "... (truncated, file too large to include fully)"

#: The reference document 1.15 is about, and the API path it names as the
#: consequence -- "a 206KB OpenAPI spec has its ``/systemusers`` paths at roughly
#: byte 65,000, outside the interpreter's view".
RECORDED_REFERENCE_DOCUMENT = "jumpcloud_v1_swagger.yaml"
RECORDED_REFERENCE_PATH = "systemusers"

#: `bugfix.md` 1.13's figures: two interpreter calls counted as nothing, then a
#: jump to 53,836 of the 100,000-token budget after the agent run.
RECORDED_INTERPRETER_CALLS = 2
RECORDED_POST_AGENT_TOKEN_TOTAL = 53_836

#: What a session's version display must read once a bump has happened, for a
#: plugin whose spec on disk carries 1.0.0 and which has been exported once.
RECORDED_FIRST_VERSION = "1.0.0"
RECORDED_BUMPED_DISPLAY = "1.0.0 -> 1.0.1"

#: A response the fake CLI can return that parses as a plan carrying code work.
_FAKE_PLAN_RESPONSE = json.dumps(
    {
        "operations": [
            {
                "op": "add_component",
                "kind": "action",
                "name": "create_user",
                "component": {"title": "Create User", "description": "Create a user.", "input": {}, "output": {}},
            }
        ],
        "reasoning": [{"kind": "action_logic", "parameters": {"action": "create_user"}}],
        "clarification": None,
        "vendor_api": RECORDED_VENDOR_API,
        "proceed_without_reference": False,
    }
)


def _fake_cli(directory: Path, *, response: str = _FAKE_PLAN_RESPONSE, exit_code: int = 0, label: str = "cli") -> tuple:
    """Write a stand-in CLI and return ``(command_prefix, prompt_record)``.

    The script is deliberately minimal: read stdin, record it, print a fixed
    response, exit with a fixed code. Everything about *how* it is launched -- the
    argument vector, the prompt on stdin, the environment, the captured streams --
    stays the production code's own, which is the part these measurements depend
    on.

    The prompt is recorded to a file rather than through an environment variable
    because :class:`PluginAgent` gives its child a default-deny environment
    (Req 29) and an env-carried path would not survive it.

    Args:
        directory: where to write the script and the prompt record.
        response: what the script prints on stdout after a ``"> "`` noise line.
        exit_code: the exit status, so a failed invocation can be measured too.
        label: distinguishes several stand-ins in one directory.

    Returns:
        ``([sys.executable, script_path], prompt_record_path)``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / f"fake_{label}.py"
    record = directory / f"prompt_{label}.txt"
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"record = Path(r{str(record)!r})\n"
        f"response = r{response!r}\n"
        "prompt = sys.stdin.read()\n"
        "record.write_text(prompt, encoding='utf-8')\n"
        "sys.stdout.write('> stood in for the CLI\\n')\n"
        "sys.stdout.write(response + '\\n')\n"
        f"sys.exit({int(exit_code)})\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)], record


class StubInterpreter:
    """Returns a prepared :class:`TurnPlan`, standing in for the interpretation layer.

    The websocket route calls ``interpreter.interpret(...)`` and then decides what
    progress to announce from the plan it got back. That decision is the code under
    test in 1.12, so what is substituted here is only the source of the plan -- no
    LLM call, and no dependence on a model returning a particular shape.
    """

    def __init__(self, plan: TurnPlan) -> None:
        self._plan = plan
        self.calls = 0

    async def interpret(self, text: str, spec, *, attachments=None) -> TurnPlan:
        """Return the prepared plan, recording that the route asked for one."""
        self.calls += 1
        return self._plan


class Frames:
    """The frames one websocket message produced, with the time each arrived.

    Attributes:
        entries: ``(elapsed_seconds, frame)`` in arrival order, timed from the
            moment the ``submit_message`` frame was sent.
        started_at: that moment, on :func:`time.monotonic`'s clock, so a frame's
            arrival can be compared with when the agent was running.
    """

    def __init__(self, entries, *, started_at: float = 0.0) -> None:
        self.entries = tuple(entries)
        self.started_at = started_at

    @property
    def types(self) -> Tuple[str, ...]:
        """The ``type`` of each frame, in order."""
        return tuple(str(frame.get("type")) for _, frame in self.entries)

    def statuses(self) -> Tuple[str, ...]:
        """The message of every ``status`` frame, in order."""
        return tuple(str(frame.get("message", "")) for _, frame in self.entries if frame.get("type") == "status")

    def turn(self) -> Optional[Dict[str, Any]]:
        """The ``turn`` frame's result, or ``None`` if the turn never reported."""
        for _, frame in self.entries:
            if frame.get("type") == "turn":
                result = frame.get("result")
                return result if isinstance(result, dict) else None
        return None

    def widest_gap(self) -> Tuple[float, str, str]:
        """The longest silence, and the frames it sits between.

        Measured from the send rather than from the first frame, so a delay before
        anything at all is emitted counts as a silence like any other.
        """
        widest = (0.0, "submit_message", "(nothing)")
        previous_at = 0.0
        previous_label = "submit_message"
        for elapsed, frame in self.entries:
            label = str(frame.get("type"))
            if frame.get("type") == "status":
                label = f"status:{frame.get('message')!r}"
            if elapsed - previous_at > widest[0]:
                widest = (elapsed - previous_at, previous_label, label)
            previous_at = elapsed
            previous_label = label
        return widest

    def render(self) -> str:
        """One line per frame, for a failure message that is evidence."""
        lines = []
        for elapsed, frame in self.entries:
            detail = frame.get("message") or frame.get("result", {}).get("status") if isinstance(frame, dict) else ""
            lines.append(f"  {elapsed:7.2f}s  {frame.get('type')}  {detail!r}")
        return "\n".join(lines)


def _drive_one_message(orchestrator: Orchestrator, session_id: str, plan: TurnPlan, *, text: str) -> Frames:
    """Send one user message over the real websocket route and time every frame.

    The route is the production one, wired through :func:`create_app`, and the
    client is a real websocket client. Only the interpretation layer is
    substituted (:class:`StubInterpreter`), because the route's own decision about
    what to announce is what is being measured.

    The loop stops at the ``visualization`` frame, which the route sends last for
    a completed turn, or at an ``error`` frame, and is bounded so a route that
    stops sending cannot hang the suite.
    """
    app = create_app(orchestrator=orchestrator, interpreter=StubInterpreter(plan))
    entries = []
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{session_id}") as websocket:
            opening = websocket.receive_json()
            assert opening.get("type") == "state", f"the channel opened with {opening.get('type')!r}, not state"
            started = time.monotonic()
            websocket.send_json({"type": "submit_message", "text": text})
            for _ in range(24):
                frame = websocket.receive_json()
                entries.append((time.monotonic() - started, frame))
                if frame.get("type") in ("visualization", "error"):
                    break
    return Frames(entries, started_at=started)


class Clarifying:
    """A turn that would implement code and ends in a request for documentation.

    ``_reference_clarification`` asks for documentation when the turn would
    implement code, the interpretation layer named a vendor, nothing has been
    supplied, and the user has not said to proceed anyway (Req 28.12). That is the
    run's own situation: the first message described a JumpCloud plugin and asked
    for the implementation, and the tool had to ask for the API documentation
    before it could write anything.

    Attributes:
        frames: what the websocket emitted, timed.
        plan: the plan the route was handed.
    """

    def __init__(self, *, frames: Frames, plan: TurnPlan) -> None:
        self.frames = frames
        self.plan = plan

    def __repr__(self) -> str:
        return f"Clarifying(frames={len(self.frames.entries)}, reasoning={len(self.plan.reasoning)})"


@pytest.fixture(scope="module")
def clarifying_turn(tmp_path_factory) -> Clarifying:
    """One websocket message whose turn ends in a clarification request."""
    _require_tree()
    tree = _copy_tree(tmp_path_factory.mktemp("progress_clarification"))
    disk_spec = load_plugin_spec((JUMPCLOUD_TREE / "plugin.spec.yaml").read_text(encoding="utf-8"))
    orchestrator, _ = _orchestrator(tree, "", graded=False)
    orchestrator.start_session(ENTRY_MODE_CREATE_NEW, session_id="clarify", user_id="tester")

    base = _pre_implementation_turn(disk_spec)
    plan = TurnPlan(
        operations=base.operations,
        reasoning=base.reasoning,
        vendor_api=RECORDED_VENDOR_API,
    )
    frames = _drive_one_message(orchestrator, "clarify", plan, text="Build a JumpCloud user provisioning plugin.")
    return Clarifying(frames=frames, plan=plan)


class TestProgressAnnouncesWorkTheTurnNeverDid:
    """`bugfix.md` 1.12 / 2.17. **Expected to FAIL on unfixed code.**

    ``api/app.py``'s websocket handler emits the generation status from
    ``plan.reasoning`` *before* calling ``submit_message``, so it announces work
    on the strength of the plan alone. The orchestrator then declines to do any of
    it: a turn that would implement code against a vendor API with no
    documentation supplied ends in a clarification request and leaves the draft
    untouched (Req 28.12, 1.5).
    """

    def test_the_turn_ended_in_a_clarification(self, clarifying_turn: Clarifying):
        """The premise, measured: the turn really did decline to do the work.

        Expected to pass before and after -- 2.17 changes what is announced, not
        whether documentation is asked for.
        """
        result = clarifying_turn.frames.turn()
        assert result is not None, f"no turn frame arrived:\n{clarifying_turn.frames.render()}"
        assert result["status"] == TurnStatus.CLARIFICATION.value, (
            f"the turn reported {result['status']!r}, so this is not a turn that ended in a clarification "
            f"and 1.12's condition does not hold here: {result['message'][:200]!r}"
        )
        assert clarifying_turn.plan.reasoning, "the plan carried no code work, so nothing would be announced"

    def test_no_progress_message_announced_generation(self, clarifying_turn: Clarifying):
        """2.17: the system must not have announced work it did not perform."""
        announced = tuple(status for status in clarifying_turn.frames.statuses() if "Generating logic" in status)
        assert not announced, (
            f"the turn ended in a clarification and the operator was told {list(announced)} first. Frames:\n"
            f"{clarifying_turn.frames.render()}"
        )

    def test_the_recorded_message_still_reproduces(self, clarifying_turn: Clarifying):
        """`bugfix.md` 1.12's exact string, re-taken.

        Fails on unfixed code by *reproducing* rather than by contradiction, so the
        recorded message is checked instead of trusted. Inverts when task 11.2
        moves the frame to where the work is dispatched.
        """
        statuses = clarifying_turn.frames.statuses()
        assert RECORDED_GENERATION_STATUS not in statuses, (
            f"{RECORDED_GENERATION_STATUS!r} reproduces exactly, ahead of a turn that generated nothing. "
            f"Statuses emitted: {list(statuses)}. This assertion is written to fail while the bug is "
            "present; when task 11.2 lands it passes"
        )

    def test_the_draft_was_not_touched_either(self, clarifying_turn: Clarifying):
        """Why the announcement is wrong rather than merely early (Req 1.5).

        Expected to pass before and after: the clarification path leaves the draft
        alone today and must keep doing so. Recorded here so the reader can see
        that nothing at all happened after the announcement.
        """
        result = clarifying_turn.frames.turn()
        assert result is not None
        assert not result["generated"], (
            f"the clarifying turn produced {len(result['generated'])} artifact(s), so it did do some of the "
            "work it announced"
        )
        assert result["refreshed"] is False


class Delegated:
    """One websocket message whose turn delegates implementation to the agent.

    Attributes:
        frames: what the websocket emitted, timed from the send.
        agent: the stand-in, which records when it was entered and left.
    """

    def __init__(self, *, frames: Frames, agent: "RecordedAgent") -> None:
        self.frames = frames
        self.agent = agent

    @property
    def delegated_seconds(self) -> float:
        """How long the delegated phase actually ran, measured not assumed."""
        if self.agent.entered_at is None or self.agent.left_at is None:
            return 0.0
        return self.agent.left_at - self.agent.entered_at

    def __repr__(self) -> str:
        return f"Delegated(frames={len(self.frames.entries)}, delegated={self.delegated_seconds:.2f}s)"


@pytest.fixture(scope="module")
def delegated_turn(tmp_path_factory) -> Delegated:
    """One websocket message whose delegated run is held open for a known interval."""
    _require_tree()
    tree = _copy_tree(tmp_path_factory.mktemp("progress_delegated"))
    disk_text = (JUMPCLOUD_TREE / "plugin.spec.yaml").read_text(encoding="utf-8")
    disk_spec = load_plugin_spec(disk_text)
    orchestrator, agent = _orchestrator(tree, disk_text, graded=False, block_seconds=DELEGATED_BLOCK_SECONDS)
    orchestrator.start_session(ENTRY_MODE_CREATE_NEW, session_id="delegated", user_id="tester")
    frames = _drive_one_message(
        orchestrator,
        "delegated",
        _pre_implementation_turn(disk_spec),
        text="Implement the three actions.",
    )
    return Delegated(frames=frames, agent=agent)


class TestADelegatedRunEmitsNothingWhileItRuns:
    """`bugfix.md` 1.14 / 2.19 -- a long run must be distinguishable from a hang.

    Expected to FAIL on unfixed code. The websocket handler emits its last frame
    before ``submit_message`` and its next after the whole turn returns; the
    orchestrator has no channel to report on -- ``submit_message`` takes
    ``(session_id, text, plan)`` and nothing else -- so the delegated run,
    the repair rounds, the checks and the done evaluation all happen inside one
    silence.

    The scale here is :data:`DELEGATED_BLOCK_SECONDS` rather than the run's 13
    minutes; the claim asserted is a ratio, so it holds at either scale.
    """

    def test_the_run_was_delegated_and_held_open(self, delegated_turn: Delegated):
        """The premise: an agent really did run, and for the interval intended."""
        assert delegated_turn.agent.calls == 1, (
            f"the delegated agent ran {delegated_turn.agent.calls} time(s); frames:\n"
            f"{delegated_turn.frames.render()}"
        )
        assert delegated_turn.delegated_seconds >= DELEGATED_BLOCK_SECONDS * 0.9, (
            f"the delegated phase lasted {delegated_turn.delegated_seconds:.2f}s, not the "
            f"{DELEGATED_BLOCK_SECONDS}s intended, so the silence measured below is not the delegated run's"
        )

    def test_the_longest_silence_is_shorter_than_the_delegated_run(self, delegated_turn: Delegated):
        """2.19: periodic progress, so the operator can tell running from hung."""
        gap, before, after = delegated_turn.frames.widest_gap()
        assert gap < delegated_turn.delegated_seconds, (
            f"the longest silence was {gap:.2f}s -- between {before} and {after} -- while the delegated "
            f"phase ran for {delegated_turn.delegated_seconds:.2f}s. Nothing was emitted for the whole of "
            f"it. At the run's own scale that silence was {RECORDED_SILENCE_SECONDS:.0f}s "
            f"({RECORDED_SILENCE_SECONDS / 60:.0f} minutes). Frames:\n{delegated_turn.frames.render()}"
        )

    def test_some_frame_arrives_while_the_agent_is_running(self, delegated_turn: Delegated):
        """The same claim stated as presence rather than as a gap.

        Asserted separately because it is the operator's actual question -- "is
        anything happening?" -- and because it holds for any reporting interval
        below the phase's duration, which is what makes it a fair check on the fix
        as well as on the defect. :data:`ASSUMED_PROGRESS_INTERVAL_SECONDS` records
        the interval this 3-second block assumes will not be exceeded.
        """
        agent = delegated_turn.agent
        assert agent.entered_at is not None and agent.left_at is not None
        # Both clocks are `time.monotonic` in one process, so they compare directly.
        # The margin excludes the frame the route sends immediately *before*
        # entering the phase: the client receives it a few milliseconds later, which
        # can land after the agent was entered and would otherwise read as progress
        # reported during the run. A reporting interval of
        # ASSUMED_PROGRESS_INTERVAL_SECONDS puts real frames well inside it.
        during = tuple(
            f"{frame.get('type')}:{frame.get('message', '')!r}"
            for elapsed, frame in delegated_turn.frames.entries
            if agent.entered_at + PROGRESS_MARGIN_SECONDS
            <= (delegated_turn.frames.started_at + elapsed)
            <= agent.left_at - PROGRESS_MARGIN_SECONDS
        )
        assert during, (
            "not one frame arrived while the agent was running. The operator's last word was "
            f"{list(delegated_turn.frames.statuses())[-1:]!r}, "
            f"{delegated_turn.delegated_seconds:.2f}s before the turn reported. A reporting interval of "
            f"{ASSUMED_PROGRESS_INTERVAL_SECONDS}s would have produced roughly "
            f"{int(delegated_turn.delegated_seconds / ASSUMED_PROGRESS_INTERVAL_SECONDS)} frames. Frames:\n"
            f"{delegated_turn.frames.render()}"
        )

    def test_the_orchestrator_can_report_progress_at_all(self):
        """The structural half: there is no channel to report on.

        ``submit_message`` takes ``(session_id, text, plan)``. A ticker in the
        route can only re-emit what the route already knows, which is the plan --
        so "repair round 2 of 3" cannot come from there. Task 11.2's
        ``ProgressReporter`` is the seam this asserts. Expected to FAIL now.
        """
        parameters = tuple(inspect.signature(Orchestrator.submit_message).parameters)
        assert any(name in parameters for name in ("progress", "reporter", "progress_reporter")), (
            f"Orchestrator.submit_message takes {parameters}, so no caller can be told which phase is "
            "running. The phases a delegated turn passes through -- applying operations, scaffolding, "
            "refreshing, implementing, repair round n of m, checking, evaluating done -- are all visible "
            "here and reportable nowhere"
        )


class TestInterpreterUsageIsNotCounted:
    """`bugfix.md` 1.13 / 2.18 and design Property 72. **Expected to FAIL now.**

    :class:`Interpreter` holds no :class:`CostController` and
    :meth:`Interpreter.interpret` takes no ``session_id``, so a paid interpretation
    is recorded nowhere. :class:`LLMGenerator` and :class:`PluginAgent` both call
    ``record_usage`` around the same subprocess boundary, which is why the total
    sits at zero and then jumps.

    The invocations below are real subprocesses through the production call path
    (:func:`_fake_cli`); only the binary is a stand-in.
    """

    @staticmethod
    def _measure(tmp_path: Path) -> Dict[str, Any]:
        """Two successful interpretations, one failure, then one agent run."""
        cost = CostController()
        command, _ = _fake_cli(tmp_path / "ok", label="ok")
        failing, _ = _fake_cli(tmp_path / "bad", exit_code=1, label="bad")
        interpreter = Interpreter(executable=command)
        totals = {"start": cost.session_total("s1")}

        for index in range(RECORDED_INTERPRETER_CALLS):
            asyncio.run(interpreter.interpret(f"message {index}", None))
            totals[f"after_interpret_{index + 1}"] = cost.session_total("s1")

        with pytest.raises(InterpreterError):
            asyncio.run(Interpreter(executable=failing).interpret("this one fails", None))
        totals["after_failed_interpret"] = cost.session_total("s1")

        project = tmp_path / "tree"
        project.mkdir(parents=True, exist_ok=True)
        agent_command, _ = _fake_cli(tmp_path / "agent", label="agent")
        run = asyncio.run(
            PluginAgent(cost, executable=agent_command).implement(
                project, "implement the actions", session_id="s1", user_id="u1"
            )
        )
        totals["after_agent"] = cost.session_total("s1")
        totals["agent_tokens"] = run.tokens
        return totals

    def test_a_successful_interpretation_reaches_the_session_total(self, tmp_path):
        """2.18: every paid call is counted where it happens."""
        totals = self._measure(tmp_path)
        assert totals["after_interpret_1"] > totals["start"], (
            "a successful interpretation added nothing to the session total "
            f"({totals['start']} -> {totals['after_interpret_1']}). The call ran a subprocess and returned a "
            "parsed plan, so it was paid for"
        )

    def test_the_total_is_the_sum_of_the_successful_invocations(self, tmp_path):
        """Property 72, over the sequence 1.13 describes."""
        totals = self._measure(tmp_path)
        assert totals["after_agent"] > totals["agent_tokens"], (
            f"after {RECORDED_INTERPRETER_CALLS} successful interpretations and one agent run the session "
            f"total is {totals['after_agent']}, which is exactly the agent's own "
            f"{totals['agent_tokens']} tokens. The interpretations contributed nothing, so the displayed "
            "cost is not the sum of the paid calls"
        )

    def test_the_recorded_shape_still_reproduces(self, tmp_path):
        """1.13's shape: zero across the interpreter calls, then a jump.

        Fails on unfixed code by reproducing. The run's own figure was
        :data:`RECORDED_POST_AGENT_TOKEN_TOTAL` of a 100,000-token budget; the
        stand-in's figure is smaller because the instruction and transcript are,
        so the shape is what is asserted and the magnitude is recorded.
        """
        totals = self._measure(tmp_path)
        interpreter_calls_counted_nothing = all(
            totals[f"after_interpret_{index + 1}"] == 0 for index in range(RECORDED_INTERPRETER_CALLS)
        )
        assert not (interpreter_calls_counted_nothing and totals["after_agent"] > 0), (
            f"the shape `bugfix.md` 1.13 records reproduces exactly: {RECORDED_INTERPRETER_CALLS} "
            f"interpreter calls leave the total at 0 and it then jumps to {totals['after_agent']} after the "
            f"agent run (the run itself jumped to {RECORDED_POST_AGENT_TOKEN_TOTAL:,}, 54% of the budget). "
            "This assertion is written to fail while the bug is present; when task 11.3 lands it passes"
        )

    def test_the_interpreter_can_record_usage_at_all(self):
        """The structural half: nothing is threaded through to record against.

        Task 11.3 gives ``interpret`` the ``session_id`` and the
        :class:`CostController`, "exactly as :class:`LLMGenerator` does". Expected
        to FAIL now.
        """
        parameters = tuple(inspect.signature(Interpreter.interpret).parameters)
        assert "session_id" in parameters, (
            f"Interpreter.interpret takes {parameters}: no session, so no account to charge. "
            f"LLMGenerator.generate takes {tuple(inspect.signature(LLMGenerator.generate).parameters)}"
        )


class TestPreservationAFailedInvocationStaysExcluded:
    """3.7's arithmetic half -- a failed call must not be charged (Req 3.7).

    Expected to pass before and after, and recorded because it is the one clause
    of Property 72 the current code satisfies -- and satisfies vacuously. A total
    that never moves is consistent with "failed calls are excluded" and with
    "nothing is counted at all", which is precisely why the exclusion cannot be
    read as evidence that the accounting works.
    """

    def test_a_failed_interpretation_is_excluded(self, tmp_path):
        cost = CostController()
        failing, _ = _fake_cli(tmp_path, exit_code=1, label="bad")
        before = cost.session_total("s2")
        with pytest.raises(InterpreterError):
            asyncio.run(Interpreter(executable=failing).interpret("fails", None))
        assert cost.session_total("s2") == before, "a failed interpretation was charged to the session"

    def test_a_failed_agent_run_is_excluded(self, tmp_path):
        cost = CostController()
        failing, _ = _fake_cli(tmp_path, exit_code=1, label="agent_bad")
        project = tmp_path / "tree"
        project.mkdir(parents=True, exist_ok=True)
        with pytest.raises(PluginAgentError):
            asyncio.run(
                PluginAgent(cost, executable=failing).implement(project, "implement", session_id="s2", user_id="u1")
            )
        assert cost.session_total("s2") == 0, (
            "a failed agent run was charged to the session, which would contradict Req 3.7 as well as " "Property 72"
        )


class Truncation:
    """One interpretation of a message carrying an over-long attachment.

    Attributes:
        attachment: the attachment as the UI would send it.
        prompt: the prompt the child process actually received.
        plan: the plan the interpreter returned.
        stored: the path the same document occupies in the tree, for the agent.
        stored_text: what that file holds.
    """

    def __init__(self, *, attachment, prompt: str, plan: TurnPlan, stored: Tuple[str, ...], stored_text: str) -> None:
        self.attachment = attachment
        self.prompt = prompt
        self.plan = plan
        self.stored = stored
        self.stored_text = stored_text

    @property
    def content(self) -> str:
        """The attachment's full text."""
        return str(self.attachment["content"])

    def __repr__(self) -> str:
        return f"Truncation(attachment={len(self.content)} chars, prompt={len(self.prompt)} chars)"


def _reference_document_text() -> Tuple[str, str]:
    """The over-long document to measure with, and where it came from.

    Prefers the run's own 206KB JumpCloud v1 Swagger out of the tree's
    ``.builder/reference/``, because 1.15's claim is about that document. Falls
    back to a synthesised document of the same order when the tree is present but
    the reference material is not, so the disclosure claim is still measurable.
    """
    document = JUMPCLOUD_TREE / ".builder" / "reference" / RECORDED_REFERENCE_DOCUMENT
    if document.is_file():
        return document.read_text(encoding="utf-8", errors="replace"), str(document)
    # Shaped like the real one: a few references to the endpoint inside the window
    # and most of them past it, which is what the measurement below partitions.
    head = f"paths:\n  /{RECORDED_REFERENCE_PATH}:\n    get: {{}}\n" + "# filler\n" * 8_000
    tail = f"  /{RECORDED_REFERENCE_PATH}/{{id}}:\n    put: {{}}\n" * 20
    return head + tail, "(synthesised)"


@pytest.fixture(scope="module")
def truncation(tmp_path_factory) -> Truncation:
    """One interpretation with an over-long attachment, plus the agent's copy."""
    _require_tree()
    root = tmp_path_factory.mktemp("interpreter_truncation")
    tree = _copy_tree(root)
    text, _ = _reference_document_text()
    attachment = {"name": RECORDED_REFERENCE_DOCUMENT, "content": text}

    command, record = _fake_cli(root / "cli", label="truncation")
    plan = asyncio.run(Interpreter(executable=command).interpret("build it", None, attachments=[attachment]))

    # The other half of 1.15: what the delegated agent gets. Stored through the
    # production path, so "the agent receives the full file" is measured rather
    # than asserted from the requirement's wording.
    acquirer = ReferenceAcquirer()
    document = acquirer.from_attachment(RECORDED_REFERENCE_DOCUMENT, text.encode("utf-8"), media_type="text/yaml")
    stored = store_reference_set(tree, ReferenceSet(documents=(document,)))
    stored_text = (tree / stored[0]).read_text(encoding="utf-8") if stored else ""
    return Truncation(
        attachment=attachment,
        prompt=record.read_text(encoding="utf-8"),
        plan=plan,
        stored=stored,
        stored_text=stored_text,
    )


class TestTheAttachmentIsTruncatedSilently:
    """`bugfix.md` 1.15 / 2.20 -- the user is not told. **Expected to FAIL now.**

    ``interpret`` drops everything past :data:`INTERPRETER_ATTACHMENT_CAP` and
    appends a marker *into the prompt*, which only the model reads. Nothing on the
    return path carries the file's name, its size, or the size included:
    :class:`TurnPlan` has no field for it and the turn payload has no key for it.
    """

    def test_the_truncation_happens_where_bugfix_says_it_does(self, truncation: Truncation):
        """The cited line, read rather than trusted.

        Expected to pass before and after: task 11.4 leaves the 60,000-character
        cap alone and adds the disclosure, so this stays the mechanism.
        """
        source = Path(interpreter_module.__file__).read_text(encoding="utf-8").splitlines()
        line = source[RECORDED_TRUNCATION_LINE - 1].strip()
        assert line == f"max_attachment_chars = {INTERPRETER_ATTACHMENT_CAP:_}", (
            f"{interpreter_module.__file__}:{RECORDED_TRUNCATION_LINE} is {line!r}, not the "
            f"{INTERPRETER_ATTACHMENT_CAP:,}-character cap `bugfix.md` 1.15 cites; re-read the mechanism "
            "before task 11.4 discloses it"
        )

    def test_the_prompt_was_truncated_at_the_cap(self, truncation: Truncation):
        """The measurement itself, taken from the bytes the child received.

        Expected to pass before and after -- the cap is unchanged by 2.20.
        """
        assert len(truncation.content) > INTERPRETER_ATTACHMENT_CAP, (
            f"the document is only {len(truncation.content)} characters, so it would not be truncated and "
            "this fixture measures nothing"
        )
        assert truncation.content not in truncation.prompt, (
            f"the whole {len(truncation.content)}-character document reached the prompt; 1.15's premise "
            "does not hold and the cap must have changed"
        )
        assert truncation.content[:INTERPRETER_ATTACHMENT_CAP] in truncation.prompt
        assert TRUNCATION_MARKER in truncation.prompt, (
            "the prompt carries no truncation marker at all, so the interpreter dropped the tail without "
            "even telling the model"
        )

    def test_the_user_is_told_which_file_was_truncated_and_at_what_size(self, truncation: Truncation):
        """2.20's first clause: name, full size, included size."""
        content = truncation.content
        notices = tuple(
            str(value)
            for name, value in vars(truncation.plan).items()
            if "truncat" in name.lower() or "notice" in name.lower()
        )
        assert notices, (
            f"{len(content) - INTERPRETER_ATTACHMENT_CAP:,} of {len(content):,} characters of "
            f"{truncation.attachment['name']!r} were dropped and the returned plan carries no notice of any "
            f"kind. TurnPlan's attributes are {sorted(vars(truncation.plan))}, and the marker the "
            f"interpreter does write goes into the prompt where only the model sees it"
        )
        joined = " ".join(notices)
        assert truncation.attachment["name"] in joined, f"no notice names the file: {notices}"
        assert (
            str(len(content)) in joined or f"{len(content):,}" in joined
        ), f"no notice states the full size ({len(content):,} characters): {notices}"

    def test_the_notice_says_the_agent_receives_the_whole_file(self, truncation: Truncation):
        """2.20's second clause, which is what makes the notice actionable.

        Without it, an operator told "your spec was truncated" would reasonably
        conclude the plugin was built from a truncated spec. It was not -- which
        the witness below measures -- so the statement is the difference between a
        useful disclosure and a false alarm.
        """
        notices = tuple(
            str(value)
            for name, value in vars(truncation.plan).items()
            if "truncat" in name.lower() or "notice" in name.lower()
        )
        assert notices and any("agent" in notice.lower() for notice in notices), (
            "no notice states that the delegated agent receives the whole file, so nothing distinguishes "
            "'the interpreter saw less of your spec' from 'your plugin was built from less of your spec'"
        )


class TestTheAgentStillReceivesTheWholeFile:
    """Why 1.15 is a disclosure defect and not a correctness one.

    Expected to pass before and after. `bugfix.md` says "the agent still receives
    the full file, so implementation was unaffected" -- measured here rather than
    taken on trust, because it is the whole reason 2.20 asks for a notice instead
    of a bigger cap.
    """

    def test_the_stored_reference_document_is_byte_identical(self, truncation: Truncation):
        assert truncation.stored, "nothing was stored under .builder/reference/, so the agent got nothing"
        assert truncation.stored_text == truncation.content, (
            f"the stored document is {len(truncation.stored_text)} characters where the attachment is "
            f"{len(truncation.content)}; the agent does not receive the whole file after all, which would "
            "make 1.15 a correctness defect rather than a disclosure one"
        )

    def test_the_interpreters_view_is_a_fraction_of_what_the_agent_reads(self, truncation: Truncation):
        """The size of the gap, recorded rather than described.

        `bugfix.md` 1.15 adds that "a 206KB OpenAPI spec has its ``/systemusers``
        paths at roughly byte 65,000, outside the interpreter's view". That
        specific figure is **re-measured** here rather than repeated: on this
        document the first occurrence falls *inside* the window and most of the
        others fall outside it. The claim that matters -- the interpreter sees a
        fraction of the document -- holds either way, and the corrected figures
        ride on the assertion message so the document can be fixed.
        """
        content = truncation.content
        visible = content[:INTERPRETER_ATTACHMENT_CAP]
        needle = f"/{RECORDED_REFERENCE_PATH}"
        first = content.find(needle)
        inside = visible.count(needle)
        total = content.count(needle)
        record = (
            f"{truncation.attachment['name']} is {len(content):,} characters; the interpreter sees the "
            f"first {INTERPRETER_ATTACHMENT_CAP:,} ({INTERPRETER_ATTACHMENT_CAP / len(content):.0%}) and "
            f"drops {len(content) - INTERPRETER_ATTACHMENT_CAP:,}. First {needle!r} at character "
            f"{first:,} -- `bugfix.md` 1.15 says 'roughly byte 65,000', so the section in fact *begins* "
            f"inside the window. {inside} of the {total} {needle!r} references are inside it and "
            f"{total - inside} are outside"
        )
        assert len(visible) < len(content), f"nothing was dropped: {record}"
        assert first >= 0, f"{needle!r} does not appear in {truncation.attachment['name']}"
        assert inside < total, (
            f"every one of the {total} {needle!r} references falls inside the interpreter's window, so this "
            f"document does not demonstrate the consequence 1.15 describes. {record}"
        )
        assert total - inside > inside, (
            "most of the document's references to the endpoint the plugin was built against are inside the "
            f"window, so 'outside the interpreter's view' overstates it. {record}"
        )


class VersionDisplay:
    """The version display before and after a plugin's first export.

    Attributes:
        first_plan: the preview computed with no prior export.
        second_plan: the preview computed after one export was recorded.
        exported_version: the version the first export went out at.
        prior_versions: what the registry held when the second preview ran.
    """

    def __init__(self, *, first_plan: ExportPlan, second_plan: ExportPlan, exported_version, prior_versions) -> None:
        self.first_plan = first_plan
        self.second_plan = second_plan
        self.exported_version = exported_version
        self.prior_versions = tuple(prior_versions)

    def __repr__(self) -> str:
        return (
            f"VersionDisplay(first={self.first_plan.version_display!r}, "
            f"second={self.second_plan.version_display!r}, priors={self.prior_versions})"
        )


async def _export_then_preview_again(tree: Path, output_dir: Path, registry_path: Path) -> VersionDisplay:
    """Preview, export, and preview again over ``tree``, with a real registry.

    ``iterate_custom`` rather than the reconstruction, deliberately: 1.16 is about
    the preview's version display and not about which spec the preview reads, and
    the entry mode that loads from disk keeps the two questions apart.
    """
    registry = PluginRegistry(str(registry_path))
    orchestrator, _ = _orchestrator(
        tree, "", graded=False, registry=registry, audit_log=AuditLog(output_dir / "audit.log")
    )
    orchestrator.start_session(
        ENTRY_MODE_ITERATE_CUSTOM,
        session_id="versioned",
        user_id="tester",
        plugin_name=tree.name,
    )

    first_plan = await orchestrator.prepare_export("versioned")
    object.__setattr__(first_plan, "_force", True)
    outcome = await orchestrator.confirm_export(
        "versioned",
        first_plan,
        confirmed=True,
        target="local",
        output_dir=output_dir,
    )
    assert outcome.succeeded, f"the first export did not succeed: {outcome.status} {outcome.message}"

    second_plan = await orchestrator.prepare_export("versioned")
    return VersionDisplay(
        first_plan=first_plan,
        second_plan=second_plan,
        exported_version=outcome.version,
        prior_versions=[record.version for record in registry.exports(tree.name)],
    )


@pytest.fixture(scope="module")
def version_display(tmp_path_factory) -> VersionDisplay:
    """Both version-display cases, on their own copy of the tree because they export."""
    _require_tree()
    root = tmp_path_factory.mktemp("version_display")
    return asyncio.run(_export_then_preview_again(_copy_tree(root), root / "artifacts", root / "registry.db"))


class TestAnEmptyVersionDisplayWithNoPriorExportIsRequirement127:
    """`bugfix.md` 1.16 -- and the diagnosis task 11.5 asks for, not a defect report.

    **Verdict: not a defect.** With no prior export ``bump_for_export`` returns
    ``BUMP_NONE`` (Req 12.7 -- "no prior export -> the plugin is exported at its
    current version unchanged"), so ``bump.changed`` is false and
    ``prepare_export`` leaves the display empty. There is no previous version, so
    ``"<previous> -> <new>"`` has nothing to say. Req 12.6, which 2.21 cites, is
    explicitly about the display "after a version bump".

    These assertions therefore *pass* on unfixed code and are written to keep
    passing. What they add is the evidence for the verdict, so task 11.5 begins
    from a measurement instead of from 1.16's one-line report.
    """

    def test_no_bump_happens_without_a_prior_export(self, version_display: VersionDisplay):
        """The cause, measured."""
        bump = version_display.first_plan.version_bump
        assert not bump.changed, (
            f"the first preview bumped {bump.previous} -> {bump.new} with no prior export, which Req 12.7 "
            "says should not happen; if that is now the behaviour, 1.16 needs re-diagnosing"
        )
        assert str(bump.previous) == RECORDED_FIRST_VERSION

    def test_the_display_is_empty_because_there_is_nothing_to_display(self, version_display: VersionDisplay):
        """1.16's observation, reproduced -- and correct at this point in the plugin's life."""
        assert version_display.first_plan.version_display == "", (
            "the display is populated for a preview with no bump, so the propagation is not what 1.16 "
            f"describes: {version_display.first_plan.version_display!r}"
        )

    def test_the_preview_still_reports_the_version_it_would_export(self, version_display: VersionDisplay):
        """The part 1.16 does not say, and the reason the verdict is 'not a defect'.

        The operator is not left without a version: ``spec_preview`` carries it,
        and the UI renders the whole spec beside the display line
        (``SpecPreview.tsx``). What is missing is a dedicated line, which is a
        presentation choice rather than a lost value -- and 12.6 does not speak to
        it, which is why task 11.5 can add one without contradicting anything.
        """
        payload = _serialize_export_plan(version_display.first_plan)
        assert payload["version_display"] == ""
        assert payload["spec_preview"]["version"] == RECORDED_FIRST_VERSION, (
            "the preview payload carries no version anywhere, which would make 1.16 a real gap rather than "
            f"a presentation one: {sorted(payload['spec_preview'])}"
        )


class TestTheVersionDisplayIsPopulatedWhenABumpHappens:
    """The second half of task 11.5's diagnosis: does the propagation work at all?

    If the display were empty *here* the defect would be in the propagation, and
    the fix would belong there. Expected to pass on unfixed code -- which is what
    makes the verdict above a verdict rather than a guess.
    """

    def test_the_registry_recorded_the_first_export(self, version_display: VersionDisplay):
        """The premise: there is now a prior version to bump from."""
        assert version_display.prior_versions == (RECORDED_FIRST_VERSION,), (
            f"the registry holds {version_display.prior_versions} after one export, so the second preview "
            "is not the after-a-bump case"
        )
        assert version_display.exported_version == RECORDED_FIRST_VERSION

    def test_the_second_preview_bumps_and_says_so(self, version_display: VersionDisplay):
        """2.21 in the case Req 12.6 actually speaks to."""
        bump = version_display.second_plan.version_bump
        assert bump.changed, (
            f"the second preview did not bump ({bump.previous} -> {bump.new}) though the registry holds "
            f"{version_display.prior_versions}; Req 12.4 requires a patch increment here"
        )
        assert version_display.second_plan.version_display == RECORDED_BUMPED_DISPLAY, (
            f"the display reads {version_display.second_plan.version_display!r}, not "
            f"{RECORDED_BUMPED_DISPLAY!r}. If this is empty while the bump happened, 1.16 is a propagation "
            "defect after all and task 11.5's second branch applies"
        )

    def test_the_display_reaches_the_payload(self, version_display: VersionDisplay):
        """And survives serialization, which is where the UI reads it."""
        payload = _serialize_export_plan(version_display.second_plan)
        assert payload["version_display"] == RECORDED_BUMPED_DISPLAY
        assert payload["spec_preview"]["version"] == str(version_display.second_plan.version_bump.new)


def test_the_reporting_and_accounting_measurements_inputs_are_recorded():
    """Guard: state the constants, the seams, and the two stand-ins every claim above used.

    Read-only, and written to survive the fix: each check names what would make a
    figure above stale rather than asserting a behaviour the fix changes.
    """
    assert INTERPRETER_ATTACHMENT_CAP == 60_000, (
        f"the cap this section measured is {INTERPRETER_ATTACHMENT_CAP}, not the 60,000 `bugfix.md` 1.15 "
        "names; retake the truncation measurement"
    )
    source = Path(interpreter_module.__file__).read_text(encoding="utf-8")
    assert source.count("max_attachment_chars") == 3, (
        "the attachment cap is no longer assigned once, compared once and sliced once, so the single "
        f"truncation site 1.15 cites has moved: {interpreter_module.__file__}"
    )
    assert TRUNCATION_MARKER in source, "the truncation marker text has changed"

    # The two seams tasks 11.2 and 11.3 add, recorded here so what they change is
    # stated once rather than inferred from four failures. Written to hold before
    # and after: today's parameters are the three, and the fix adds to them.
    submit_parameters = tuple(inspect.signature(Orchestrator.submit_message).parameters)
    assert submit_parameters[:4] == ("self", "session_id", "text", "plan"), (
        f"Orchestrator.submit_message now takes {submit_parameters}; the progress measurements above were "
        "taken against the three-parameter form and should be re-read"
    )
    assert (
        "session_id" in inspect.signature(PluginAgent.implement).parameters
    ), "PluginAgent.implement no longer takes a session_id, so the comparison 11.3 draws with it is stale"

    # The stand-ins: one substitutes a binary, the other a process, and neither
    # substitutes an outcome any assertion above depends on.
    assert RecordedAgent.implement.__doc__, "RecordedAgent lost its docstring"
    assert "block_seconds" in inspect.signature(RecordedAgent.__init__).parameters, (
        "RecordedAgent no longer accepts block_seconds, so the delegated-run timing above cannot be what it " "claims"
    )
    assert DELEGATED_BLOCK_SECONDS > ASSUMED_PROGRESS_INTERVAL_SECONDS, (
        f"the delegated block ({DELEGATED_BLOCK_SECONDS}s) is not longer than the reporting interval this "
        f"section assumes ({ASSUMED_PROGRESS_INTERVAL_SECONDS}s), so the silence claim is untestable"
    )
