"""Unit/integration tests for the Orchestrator sequencing (task 20.3).

These exercise the orchestrator's ordering guarantees with **mocked** external
collaborators (the ``insight-plugin`` CLI, the Kiro CLI/LLM, the Docker code
pipeline, and the tenant API) while using the real pure-logic and persistence
components (draft, vendor suffix, version bump, spec validator, build engine,
registry, audit log, project folder). Coroutines are driven with ``asyncio.run``
so no async test plugin is required, matching the repo's conventions.

Covered:

* entry-mode routing and provenance (Req 24);
* the input gate and clarification handling (Req 1.1, 1.5, 1.6, 22.5);
* atomic turn application, not-found rejection, and structural-refresh
  triggering (Req 1.7, 15.4, 22.3);
* the deterministic/LLM reasoning boundary (Req 3.2, 3.3);
* export preview: vendor suffix, version bump, first-version diff, gating, and
  the registry-read-failure abort (Req 12, 13, 16, 7, 8);
* confirm/decline and build/export recording, including failed-tenant retention
  (Req 9, 10, 16.5, 16.6, 18, 19.2).
"""

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from ruamel.yaml import YAMLError

from icplugin_builder.core.cost_controller import CostController
from icplugin_builder.core.draft import ComponentKind, Draft
from icplugin_builder.core.generation import ArtifactKind, GenerationRequest
from icplugin_builder.core.spec_completeness import check_completeness
from icplugin_builder.core.spec_model import Component, PluginSpec, SemVer
from icplugin_builder.core.yaml_codec import dump_plugin_spec, load_plugin_spec
from icplugin_builder.integrations.code_validator import (
    PipelineReport,
    StageName,
    StageResult,
    StageStatus,
)
from icplugin_builder.integrations.export_manager import ExportManager, TenantCredentials, UploadResponse
from icplugin_builder.integrations.reference_material import FetchedBytes, ReferenceAcquirer
from icplugin_builder.integrations.insight_plugin_cli import (
    InsightPluginCliError,
    ProjectTree,
    snapshot_tree,
)
from icplugin_builder.integrations.quality_gate import CodeFinding, QualityReport
from icplugin_builder.integrations.refresh_coordinator import RefreshCoordinator
from icplugin_builder.orchestrator.repair_loop import RepairLoop, RepairStatus
from icplugin_builder.orchestrator import (
    AddComponent,
    EntryModeError,
    ModifyComponent,
    Orchestrator,
    RegistryAccessError,
    TurnPlan,
    TurnStatus,
)
from icplugin_builder.orchestrator.orchestrator import _draft_from_folder
from icplugin_builder.orchestrator.session import ExportStatus
from icplugin_builder.persistence.audit_log import AuditLog
from icplugin_builder.persistence.project_folder import (
    ENTRY_MODE_CREATE_NEW,
    ENTRY_MODE_ENHANCE_PRODUCTION,
    ENTRY_MODE_ITERATE_CUSTOM,
    ProjectFolder,
    ProvenanceRecord,
)
from icplugin_builder.persistence.registry import PluginRegistry, RegistryError

# --- fakes -----------------------------------------------------------------


class FakeCli:
    """Records ``insight-plugin refresh`` calls; returns a fixed tree."""

    def __init__(self):
        self.refresh_calls = []

    async def refresh(self, project_dir):
        self.refresh_calls.append(project_dir)
        return ProjectTree(root=project_dir, files={"help.md": "# help\n"})


class FakeCodeValidator:
    """A code validator whose pipeline always passes (or fails on request)."""

    def __init__(self, *, passing=True):
        self.passing = passing
        self.calls = []

    async def run_pipeline(self, project, *, image_tag=None):
        self.calls.append(project)
        status = StageStatus.PASSED if self.passing else StageStatus.FAILED
        stages = tuple(
            StageResult(
                name=name,
                status=status,
                returncode=0 if self.passing else 1,
                stdout="",
                stderr="" if self.passing else "boom",
                duration_seconds=0.0,
                message="" if self.passing else f"{name} failed",
            )
            for name in StageName.ORDER
        )
        return PipelineReport(project_dir=project, stages=stages, docker_available=True)


class FakeLLM:
    """A stand-in LLM_Generator returning canned content and token counts."""

    def __init__(self, content="def run(self, params={}):\n    return {}\n", tokens=42):
        self.content = content
        self.tokens = tokens
        self.calls = []

    async def generate(self, kind, scoped_context, *, session_id, user_id):
        self.calls.append((kind, dict(scoped_context), session_id, user_id))
        return SimpleNamespace(kind=kind, content=self.content, tokens=self.tokens, session_total=self.tokens)


class FakeAgent:
    """A stand-in PluginAgent recording the implementation tasks it was given."""

    def __init__(self, summary="Implemented 1 action; insight-plugin validate passed.", tokens=31, credits=0.2):
        self.summary = summary
        self.tokens = tokens
        self.credits = credits
        self.calls = []

    async def implement(self, project_dir, instruction, *, session_id, user_id):
        self.calls.append((str(project_dir), instruction, session_id, user_id))
        return SimpleNamespace(
            succeeded=True,
            summary=self.summary,
            transcript=self.summary,
            changed_files=("icon_x/util/api.py",),
            credits=self.credits,
            tokens=self.tokens,
            session_total=self.tokens,
            returncode=0,
            stderr="",
        )


class FakeUploader:
    """A tenant uploader returning a fixed HTTP status."""

    def __init__(self, status_code=200):
        self.status_code = status_code
        self.calls = 0

    def upload(self, *, region_base_url, api_key, artifact_path, timeout):
        self.calls += 1
        return UploadResponse(status_code=self.status_code, body="ok")


class RaisingRegistry:
    """A registry whose reads fail, to exercise the Req 12.8 abort path."""

    def exports(self, plugin_name):
        raise RegistryError("registry unreadable")


# --- helpers ---------------------------------------------------------------


def make_spec(name="my_plugin", vendor="acme", version=SemVer(1, 0, 0), **overrides):
    base = dict(
        name=name,
        title="My Plugin",
        description="A test plugin.",
        version=version,
        vendor=vendor,
    )
    base.update(overrides)
    return PluginSpec(**base)


def make_action(title="Do a thing"):
    return Component(title=title, description="Does a thing", input={}, output={})


def create_project(projects_root, name="my_plugin", vendor="acme"):
    spec = make_spec(name=name, vendor=vendor)
    folder = ProjectFolder.create(projects_root, name, spec)
    folder.save(spec, generated_files={"README.md": "hello\n"})
    return folder


# --- entry modes (Req 24) --------------------------------------------------


class TestEntryModes:
    def test_net_new_starts_empty_with_provenance(self):
        orch = Orchestrator()
        state = orch.start_session(ENTRY_MODE_CREATE_NEW, session_id="s1", user_id="u1")
        assert state.entry_mode == ENTRY_MODE_CREATE_NEW
        assert state.provenance.entry_mode == ENTRY_MODE_CREATE_NEW
        assert state.draft.spec.actions == {}
        assert state.project_folder is None

    def test_iterate_loads_project_folder(self, tmp_path):
        create_project(tmp_path, name="loaded_plugin")
        orch = Orchestrator(projects_root=tmp_path)
        state = orch.start_session(
            ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="loaded_plugin"
        )
        assert state.entry_mode == ENTRY_MODE_ITERATE_CUSTOM
        assert state.provenance.entry_mode == ENTRY_MODE_ITERATE_CUSTOM
        assert state.draft.spec.name == "loaded_plugin"
        assert "README.md" in state.draft.code_files
        assert state.last_exported_spec is not None

    def test_iterate_missing_plugin_is_entry_mode_error(self, tmp_path):
        orch = Orchestrator(projects_root=tmp_path)
        with pytest.raises(EntryModeError):
            orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="nope")

    def test_enhance_imports_via_source_provider(self, tmp_path):
        folder = create_project(tmp_path, name="prod_plugin", vendor="rapid7_custom")
        provenance = ProvenanceRecord(
            entry_mode=ENTRY_MODE_ENHANCE_PRODUCTION,
            created_utc="2024-01-01T00:00:00+00:00",
            source_repo="rapid7/insightconnect-plugins",
            original_plugin_name="prod_plugin",
            original_version="1.0.0",
        )
        import_result = SimpleNamespace(
            project_folder=folder,
            provenance=provenance,
            private_source_notice="restricted",
        )
        source_provider = SimpleNamespace(import_plugin=lambda source, name: import_result)

        orch = Orchestrator(projects_root=tmp_path, source_provider=source_provider)
        state = orch.start_session(
            ENTRY_MODE_ENHANCE_PRODUCTION,
            session_id="s1",
            user_id="u1",
            source="rapid7",
            production_plugin="prod_plugin",
        )
        assert state.entry_mode == ENTRY_MODE_ENHANCE_PRODUCTION
        assert state.provenance.entry_mode == ENTRY_MODE_ENHANCE_PRODUCTION
        assert state.private_source_notice == "restricted"

    def test_unknown_entry_mode_rejected(self):
        orch = Orchestrator()
        with pytest.raises(EntryModeError):
            orch.start_session("bogus", session_id="s1", user_id="u1")


# --- reading a draft from a tree (bugfix task 3.1, clause 2.11) -------------


class TestDraftFromFolder:
    """One helper reads a ``Draft`` from a tree.

    The helper exists so that the entry modes and the mid-session refresh that
    clause 2.11 adds all read the tree the same way. Read and parse failures
    propagate from it unchanged: the caller decides whether an unreadable spec
    means "refuse to open" (entry modes) or "keep the draft you have" (the
    refresh in task 4.2).
    """

    def test_reads_the_spec_and_the_tree_from_disk(self, tmp_path):
        folder = create_project(tmp_path, name="on_disk")
        draft = _draft_from_folder(folder)
        assert draft.spec.name == "on_disk"
        assert draft.code_files["README.md"] == "hello\n"

    def test_reads_what_is_on_disk_rather_than_what_was_authored(self, tmp_path):
        folder = create_project(tmp_path, name="on_disk")
        rewritten = make_spec(name="on_disk", description="edited on disk by someone else")
        folder.spec_path.write_text(dump_plugin_spec(rewritten), encoding="utf-8")

        draft = _draft_from_folder(folder)
        assert draft.spec.description == "edited on disk by someone else"

    def test_an_unreadable_spec_raises_rather_than_returning_a_partial_draft(self, tmp_path):
        folder = create_project(tmp_path, name="on_disk")
        folder.spec_path.unlink()
        with pytest.raises(OSError):
            _draft_from_folder(folder)

    def test_an_unparseable_spec_raises(self, tmp_path):
        folder = create_project(tmp_path, name="on_disk")
        folder.spec_path.write_text("name: [unclosed\n", encoding="utf-8")
        with pytest.raises(YAMLError):
            _draft_from_folder(folder)

    def test_a_caller_can_preserve_its_draft_when_the_read_fails(self, tmp_path):
        """The fallback task 4.2 depends on: catching the raise leaves the draft."""
        folder = create_project(tmp_path, name="on_disk")
        held = Draft(spec=make_spec(name="in_session"))
        folder.spec_path.write_text("name: [unclosed\n", encoding="utf-8")

        try:
            held = _draft_from_folder(folder)
        except (OSError, ValueError, YAMLError):
            pass
        assert held.spec.name == "in_session"


# --- input gate & clarification (Req 1, 22.5) ------------------------------


class TestInputGate:
    def _orch(self):
        orch = Orchestrator()
        orch.start_session(ENTRY_MODE_CREATE_NEW, session_id="s1", user_id="u1")
        return orch

    def test_blank_input_rejected_leaves_draft_unchanged(self):
        orch = self._orch()
        before = orch.session("s1").spec
        result = asyncio.run(orch.submit_message("s1", "   "))
        assert result.status is TurnStatus.REJECTED_INPUT
        assert orch.session("s1").spec is before

    def test_over_long_input_rejected(self):
        orch = self._orch()
        result = asyncio.run(orch.submit_message("s1", "x" * 10_001))
        assert result.status is TurnStatus.REJECTED_INPUT

    def test_no_plan_requests_clarification(self):
        orch = self._orch()
        result = asyncio.run(orch.submit_message("s1", "do something vague", plan=None))
        assert result.status is TurnStatus.CLARIFICATION
        assert result.needs_clarification

    def test_ambiguous_plan_requests_clarification_unchanged(self):
        orch = self._orch()
        before = orch.session("s1").spec
        plan = TurnPlan(clarification="Which action did you mean?")
        result = asyncio.run(orch.submit_message("s1", "change the thing", plan=plan))
        assert result.status is TurnStatus.CLARIFICATION
        assert "Which action" in result.message
        assert orch.session("s1").spec is before


# --- turn application (Req 1.7, 3, 15, 22.3) -------------------------------


class TestApplyTurn:
    def test_add_component_applied(self):
        orch = Orchestrator()
        orch.start_session(ENTRY_MODE_CREATE_NEW, session_id="s1", user_id="u1")
        plan = TurnPlan(operations=[AddComponent(ComponentKind.ACTION, "list_things", make_action())])
        result = asyncio.run(orch.apply_turn("s1", plan))
        assert result.status is TurnStatus.APPLIED
        assert "list_things" in orch.session("s1").spec.actions

    def test_modify_missing_component_rejected_unchanged(self):
        orch = Orchestrator()
        orch.start_session(ENTRY_MODE_CREATE_NEW, session_id="s1", user_id="u1")
        before = orch.session("s1").spec
        plan = TurnPlan(operations=[ModifyComponent(ComponentKind.ACTION, "ghost", make_action())])
        result = asyncio.run(orch.apply_turn("s1", plan))
        assert result.status is TurnStatus.NOT_FOUND
        assert "ghost" in result.message
        assert orch.session("s1").spec is before

    def test_structural_change_triggers_refresh(self, tmp_path):
        create_project(tmp_path, name="loaded_plugin")
        cli = FakeCli()
        orch = Orchestrator(projects_root=tmp_path, refresh_coordinator=RefreshCoordinator(cli=cli))
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="loaded_plugin")
        plan = TurnPlan(operations=[AddComponent(ComponentKind.ACTION, "new_action", make_action())])
        result = asyncio.run(orch.apply_turn("s1", plan))
        assert result.status is TurnStatus.APPLIED
        assert result.refreshed is True
        assert cli.refresh_calls  # refresh ran

    def test_metadata_only_change_does_not_refresh(self, tmp_path):
        create_project(tmp_path, name="loaded_plugin")
        cli = FakeCli()
        orch = Orchestrator(projects_root=tmp_path, refresh_coordinator=RefreshCoordinator(cli=cli))
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="loaded_plugin")
        from icplugin_builder.orchestrator import UpdateMetadata

        plan = TurnPlan(operations=[UpdateMetadata(title="Renamed")])
        result = asyncio.run(orch.apply_turn("s1", plan))
        assert result.status is TurnStatus.APPLIED
        assert result.refreshed is False
        assert cli.refresh_calls == []


def _finding(path="icon_x/actions/a/action.py", line=10, code="unused-import"):
    return CodeFinding(source="prospector", path=path, code=code, message="msg", line=line)


def _report(findings=()):
    return QualityReport(project_dir=Path("/tmp/x"), findings=tuple(findings))


class ScriptedChecker:
    """Returns a scripted sequence of quality reports, one per check."""

    def __init__(self, reports):
        self.reports = list(reports)
        self.calls = 0

    async def run(self, project_dir):
        self.calls += 1
        return self.reports[min(self.calls - 1, len(self.reports) - 1)]


class FakeScaffolder:
    """A stand-in InsightPluginCli.create that produces a plugin tree."""

    def __init__(self, *, prefix="icon", fail=False):
        self.prefix = prefix
        self.fail = fail
        self.calls = []

    async def create(self, spec, projects_root):
        self.calls.append((spec.name, str(projects_root)))
        if self.fail:
            raise InsightPluginCliError("insight-plugin create failed")
        root = Path(projects_root) / spec.name
        package = root / f"{self.prefix}_{spec.name}"
        package.mkdir(parents=True, exist_ok=True)
        (package / "schema.py").write_text("# generated\n", encoding="utf-8")
        (root / "Dockerfile").write_text("FROM python:3.13\n", encoding="utf-8")
        return snapshot_tree(root)


class TestRepairAfterImplementation:
    """Implementation is followed by check-and-repair, not just by a report.

    Running the validators and reporting the results is what the tool did before;
    it is why plugins with unparseable files shipped. The orchestrator now runs
    the repair loop after the agent implements, and surfaces its stopping
    condition on the turn.
    """

    def _orch(self, tmp_path, *, reports, agent=None):
        create_project(tmp_path, name="my_plugin", vendor="acme")
        return Orchestrator(
            cost_controller=CostController(),
            llm_generator=FakeLLM(),
            plugin_agent=agent if agent is not None else FakeAgent(),
            repair_loop=RepairLoop(ScriptedChecker(reports), max_rounds=3),
            projects_root=tmp_path,
        )

    def _code_turn(self):
        return TurnPlan(reasoning=[GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, parameters={"action": "scan"})])

    def test_a_clean_check_needs_no_repair(self, tmp_path):
        orch = self._orch(tmp_path, reports=[_report()])
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        result = asyncio.run(orch.apply_turn("s1", self._code_turn()))

        assert result.status is TurnStatus.APPLIED
        outcome = orch.session("s1").repair_outcome
        assert outcome.status is RepairStatus.CLEAN
        assert outcome.clean

    def test_findings_are_repaired_and_the_turn_says_so(self, tmp_path):
        orch = self._orch(tmp_path, reports=[_report([_finding()]), _report()])
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        result = asyncio.run(orch.apply_turn("s1", self._code_turn()))

        outcome = orch.session("s1").repair_outcome
        assert outcome.status is RepairStatus.REPAIRED
        assert outcome.clean
        assert "Repaired" in result.message

    def test_the_agent_is_given_the_findings_to_fix(self, tmp_path):
        agent = FakeAgent()
        orch = self._orch(tmp_path, reports=[_report([_finding(path="icon_x/util/api.py")]), _report()], agent=agent)
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        asyncio.run(orch.apply_turn("s1", self._code_turn()))

        # Two agent runs: the implementation, then the repair carrying the finding.
        assert len(agent.calls) == 2
        repair_instruction = agent.calls[1][1]
        assert "icon_x/util/api.py" in repair_instruction
        # And the repair must not be to edit a generated file.
        assert "Do not edit generated files" in repair_instruction

    def test_an_unrepaired_result_is_reported_as_such_on_the_turn(self, tmp_path):
        # The same finding keeps coming back: the loop stalls and the turn must
        # not read as a success.
        orch = self._orch(tmp_path, reports=[_report([_finding()])])
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        result = asyncio.run(orch.apply_turn("s1", self._code_turn()))

        outcome = orch.session("s1").repair_outcome
        assert not outcome.clean
        assert outcome.status is RepairStatus.STALLED
        assert "still open" in result.message

    def test_credits_accumulate_across_implementation_and_repair_runs(self, tmp_path):
        # Credits are the only figure the CLI measures, so the session total has
        # to include every delegated run -- the implementation and each repair
        # round -- not just the first.
        agent = FakeAgent()
        orch = self._orch(tmp_path, reports=[_report([_finding()]), _report()], agent=agent)
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        asyncio.run(orch.apply_turn("s1", self._code_turn()))

        state = orch.session("s1")
        assert len(agent.calls) == 2  # implement + one repair
        assert state.credits_spent == pytest.approx(0.4)  # 0.2 from each run
        assert state.credits_reported

    def test_a_run_reporting_no_credits_leaves_spend_unknown(self, tmp_path):
        # 0.0 with credits_reported False means "unknown", not "free".
        agent = FakeAgent(credits=None)
        orch = self._orch(tmp_path, reports=[_report()], agent=agent)
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        asyncio.run(orch.apply_turn("s1", self._code_turn()))

        state = orch.session("s1")
        assert state.credits_spent == 0.0
        assert not state.credits_reported

    def test_no_repair_loop_configured_is_a_no_op(self, tmp_path):
        create_project(tmp_path, name="my_plugin", vendor="acme")
        orch = Orchestrator(
            cost_controller=CostController(),
            plugin_agent=FakeAgent(),
            projects_root=tmp_path,
        )
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        result = asyncio.run(orch.apply_turn("s1", self._code_turn()))
        assert result.status is TurnStatus.APPLIED
        assert orch.session("s1").repair_outcome is None


class TestReferenceMaterial:
    """User-supplied reference files reach the agent through the project tree.

    Deleting the OpenAPI parser in the Phase 1 cleanup left attached specs unable
    to influence implementation. Staging the file for the agent to read is the
    replacement, and is better than the parser was: nothing is lost to a
    summariser and there is no second representation to drift.
    """

    def _orch(self, tmp_path, agent):
        create_project(tmp_path, name="my_plugin", vendor="acme")
        return Orchestrator(
            cost_controller=CostController(),
            plugin_agent=agent,
            projects_root=tmp_path,
        )

    def _turn(self):
        return TurnPlan(reasoning=[GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, parameters={"action": "scan"})])

    def test_an_attachment_is_staged_and_named_in_the_instruction(self, tmp_path):
        agent = FakeAgent()
        orch = self._orch(tmp_path, agent)
        state = orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        state.attachments.append({"name": "vendor-openapi.yaml", "content": "openapi: 3.0.0\n"})
        asyncio.run(orch.apply_turn("s1", self._turn()))

        staged = state.project_folder.path / ".builder" / "reference" / "vendor-openapi.yaml"
        assert staged.is_file()
        assert staged.read_text(encoding="utf-8") == "openapi: 3.0.0\n"  # verbatim, not summarised
        assert ".builder/reference/vendor-openapi.yaml" in agent.calls[0][1]

    def test_reference_material_stays_out_of_the_packaged_artifact(self, tmp_path):
        agent = FakeAgent()
        orch = self._orch(tmp_path, agent)
        state = orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        state.attachments.append({"name": "secretish-notes.md", "content": "internal\n"})
        asyncio.run(orch.apply_turn("s1", self._turn()))

        packaged = orch._file_tree(state, state.spec)
        assert not [path for path in packaged if "reference" in path or ".builder" in path]

    def test_a_path_traversal_in_the_name_is_flattened(self, tmp_path):
        # The name comes from a client-supplied attachment, so it must not be
        # able to place a file outside the reference directory.
        agent = FakeAgent()
        orch = self._orch(tmp_path, agent)
        state = orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        state.attachments.append({"name": "../../escaped.yaml", "content": "x\n"})
        asyncio.run(orch.apply_turn("s1", self._turn()))

        assert (state.project_folder.path / ".builder" / "reference" / "escaped.yaml").is_file()
        assert not (tmp_path / "escaped.yaml").exists()

    def test_no_attachments_adds_nothing_to_the_instruction(self, tmp_path):
        agent = FakeAgent()
        orch = self._orch(tmp_path, agent)
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        asyncio.run(orch.apply_turn("s1", self._turn()))
        assert "Reference material" not in agent.calls[0][1]


class TestNetNewScaffolding:
    """A net-new draft's working tree is scaffolded, not just created empty.

    `insight-plugin create` and `insight-plugin refresh` are not interchangeable
    here: from an identical spec, create emits the current `icon_` package prefix
    while refreshing a directory that holds only a spec emits the legacy
    `komand_` one. Every plugin this tool produced before scaffolding was wired
    carried the legacy prefix.
    """

    def _orch(self, tmp_path, scaffolder):
        return Orchestrator(
            cost_controller=CostController(),
            scaffolder=scaffolder,
            refresh_coordinator=RefreshCoordinator(cli=FakeCli()),
            projects_root=tmp_path,
        )

    def _structural_turn(self):
        return TurnPlan(operations=[AddComponent(ComponentKind.ACTION, "scan", Component(title="Scan"))])

    def test_scaffolds_the_tree_and_records_the_observed_prefix(self, tmp_path):
        scaffolder = FakeScaffolder(prefix="icon")
        orch = self._orch(tmp_path, scaffolder)
        orch.start_session(
            ENTRY_MODE_CREATE_NEW,
            session_id="s1",
            user_id="u1",
            initial_spec=make_spec(name="fresh_plugin"),
        )
        result = asyncio.run(orch.apply_turn("s1", self._structural_turn()))

        assert result.status is TurnStatus.APPLIED
        assert scaffolder.calls == [("fresh_plugin", str(tmp_path))]
        folder = orch.session("s1").project_folder
        assert folder is not None
        # The package directory the scaffold actually produced is present...
        assert (folder.path / "icon_fresh_plugin" / "schema.py").is_file()
        # ...and the recorded metadata matches it rather than a default.
        assert folder.metadata().package_prefix == "icon"

    def test_records_a_legacy_prefix_when_that_is_what_was_produced(self, tmp_path):
        # Metadata must describe what is on disk. Recording an assumed "icon"
        # while the tree is komand_ is the mismatch this guards against.
        scaffolder = FakeScaffolder(prefix="komand")
        orch = self._orch(tmp_path, scaffolder)
        orch.start_session(
            ENTRY_MODE_CREATE_NEW, session_id="s1", user_id="u1", initial_spec=make_spec(name="fresh_plugin")
        )
        asyncio.run(orch.apply_turn("s1", self._structural_turn()))
        assert orch.session("s1").project_folder.metadata().package_prefix == "komand"

    def test_falls_back_to_a_bare_folder_when_scaffolding_fails(self, tmp_path):
        # The draft edit already succeeded, so a scaffolding failure degrades to
        # a bare folder that refresh can populate rather than failing the turn.
        scaffolder = FakeScaffolder(fail=True)
        orch = self._orch(tmp_path, scaffolder)
        orch.start_session(
            ENTRY_MODE_CREATE_NEW, session_id="s1", user_id="u1", initial_spec=make_spec(name="fresh_plugin")
        )
        result = asyncio.run(orch.apply_turn("s1", self._structural_turn()))

        assert result.status is TurnStatus.APPLIED
        folder = orch.session("s1").project_folder
        assert folder is not None
        assert (folder.path / "plugin.spec.yaml").is_file()

    def test_stamps_the_resolved_sdk_version_onto_the_persisted_spec(self, tmp_path):
        # `insight-plugin validate` requires an sdk block, and every plugin this
        # tool produced before this step shipped without one. The stamp has to
        # reach the draft, not just the copy handed to the scaffolder -- stamping
        # only the latter is undone when the draft is saved.
        readme = tmp_path / "SDK_README.md"
        readme.write_text("## Changelog\n\n* 7.1.2 - newest\n* 7.1.1 - older\n", encoding="utf-8")
        orch = Orchestrator(
            cost_controller=CostController(),
            scaffolder=FakeScaffolder(),
            refresh_coordinator=RefreshCoordinator(cli=FakeCli()),
            projects_root=tmp_path / "projects",
            sdk_readme=readme,
        )
        orch.start_session(
            ENTRY_MODE_CREATE_NEW, session_id="s1", user_id="u1", initial_spec=make_spec(name="fresh_plugin")
        )
        asyncio.run(orch.apply_turn("s1", self._structural_turn()))

        # On the in-memory draft...
        assert orch.session("s1").spec.extra["sdk"]["version"] == "7.1.2"
        # ...and in the spec actually written to disk.
        folder = orch.session("s1").project_folder
        written = load_plugin_spec((folder.path / "plugin.spec.yaml").read_text(encoding="utf-8"))
        assert written.extra["sdk"]["version"] == "7.1.2"

    def test_does_not_overwrite_an_sdk_version_already_pinned(self, tmp_path):
        readme = tmp_path / "SDK_README.md"
        readme.write_text("## Changelog\n\n* 7.1.2 - newest\n", encoding="utf-8")
        pinned = make_spec(name="fresh_plugin")
        pinned.extra["sdk"] = {"type": "full", "version": "6.0.0", "user": "root"}
        orch = Orchestrator(
            cost_controller=CostController(),
            scaffolder=FakeScaffolder(),
            refresh_coordinator=RefreshCoordinator(cli=FakeCli()),
            projects_root=tmp_path / "projects",
            sdk_readme=readme,
        )
        orch.start_session(ENTRY_MODE_CREATE_NEW, session_id="s1", user_id="u1", initial_spec=pinned)
        asyncio.run(orch.apply_turn("s1", self._structural_turn()))
        assert orch.session("s1").spec.extra["sdk"]["version"] == "6.0.0"

    def test_an_unresolvable_sdk_version_does_not_fail_the_turn(self, tmp_path):
        # Better to apply the edit and let the completeness check report the
        # missing field than to block on an absent SDK checkout.
        orch = Orchestrator(
            cost_controller=CostController(),
            scaffolder=FakeScaffolder(),
            refresh_coordinator=RefreshCoordinator(cli=FakeCli()),
            projects_root=tmp_path / "projects",
            sdk_readme=tmp_path / "absent.md",
        )
        orch.start_session(
            ENTRY_MODE_CREATE_NEW, session_id="s1", user_id="u1", initial_spec=make_spec(name="fresh_plugin")
        )
        result = asyncio.run(orch.apply_turn("s1", self._structural_turn()))
        assert result.status is TurnStatus.APPLIED

    def test_works_without_a_scaffolder_at_all(self, tmp_path):
        orch = self._orch(tmp_path, None)
        orch.start_session(
            ENTRY_MODE_CREATE_NEW, session_id="s1", user_id="u1", initial_spec=make_spec(name="fresh_plugin")
        )
        result = asyncio.run(orch.apply_turn("s1", self._structural_turn()))
        assert result.status is TurnStatus.APPLIED
        assert orch.session("s1").project_folder is not None


class TestReasoningBoundary:
    def test_template_match_renders_without_llm(self):
        llm = FakeLLM()
        orch = Orchestrator(cost_controller=CostController(), llm_generator=llm)
        orch.start_session(ENTRY_MODE_CREATE_NEW, session_id="s1", user_id="u1")
        request = GenerationRequest(
            kind=ArtifactKind.FIELD_DESCRIPTION,
            pattern="api_key",
            parameters={"service_name": "Acme"},
        )
        result = asyncio.run(orch.apply_turn("s1", TurnPlan(reasoning=[request])))
        assert result.status is TurnStatus.APPLIED
        assert len(result.generated) == 1
        assert result.generated[0].from_llm is False
        assert "Acme" in result.generated[0].content
        assert llm.calls == []  # zero LLM calls for a template match (Req 3.3)

    def test_unmatched_prose_reasoning_dispatches_to_llm(self):
        llm = FakeLLM(content="Some help prose.", tokens=17)
        orch = Orchestrator(cost_controller=CostController(), llm_generator=llm)
        orch.start_session(ENTRY_MODE_CREATE_NEW, session_id="s1", user_id="u1")
        request = GenerationRequest(kind=ArtifactKind.HELP_TEXT, pattern=None, parameters={"topic": "x"})
        result = asyncio.run(orch.apply_turn("s1", TurnPlan(reasoning=[request])))
        assert result.generated[0].from_llm is True
        assert result.generated[0].content == "Some help prose."
        assert result.generated[0].tokens == 17
        assert len(llm.calls) == 1

    def test_code_reasoning_is_delegated_to_the_agent_not_the_llm(self, tmp_path):
        # Code kinds are implemented by the delegated agent working in the
        # project tree, never requested as a text completion from the LLM.
        create_project(tmp_path, name="my_plugin", vendor="acme")
        llm = FakeLLM()
        agent = FakeAgent()
        orch = Orchestrator(
            cost_controller=CostController(),
            llm_generator=llm,
            plugin_agent=agent,
            projects_root=tmp_path,
        )
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        request = GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, pattern=None, parameters={"action": "list_things"})
        result = asyncio.run(orch.apply_turn("s1", TurnPlan(reasoning=[request])))

        assert result.status is TurnStatus.APPLIED
        assert llm.calls == []  # the LLM is not asked to write code
        assert len(agent.calls) == 1
        project_dir, instruction, session_id, user_id = agent.calls[0]
        assert project_dir.endswith("my_plugin")  # runs in the plugin's own tree
        assert "list_things" in instruction  # the requested action is named
        assert (session_id, user_id) == ("s1", "u1")
        # The turn carries the agent's own account of what it did...
        assert agent.summary in result.message
        # ...but does not stop there. This agent claims validation passed; the
        # turn appends what was actually checked, so the claim cannot stand as
        # the last word on whether the plugin is finished (Req 27.3, 27.4).
        assert "not complete" in result.message

    def test_code_reasoning_without_an_agent_is_a_no_op(self, tmp_path):
        # No agent wired (e.g. Kiro CLI unavailable): the structural edit still
        # applies rather than the turn failing outright.
        create_project(tmp_path, name="my_plugin", vendor="acme")
        llm = FakeLLM()
        orch = Orchestrator(cost_controller=CostController(), llm_generator=llm, projects_root=tmp_path)
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        request = GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, pattern=None, parameters={"action": "x"})
        result = asyncio.run(orch.apply_turn("s1", TurnPlan(reasoning=[request])))
        assert result.status is TurnStatus.APPLIED
        assert llm.calls == []


# --- export preview (Req 12, 13, 16, 7, 8) ---------------------------------


class TestPrepareExport:
    def _export_orch(self, tmp_path, *, passing=True, registry=None):
        create_project(tmp_path, name="my_plugin", vendor="acme")
        registry = registry if registry is not None else PluginRegistry(str(tmp_path / "registry.db"))
        orch = Orchestrator(
            projects_root=tmp_path,
            code_validator=FakeCodeValidator(passing=passing),
            registry=registry,
            audit_log=AuditLog(tmp_path / "audit.log"),
        )
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        return orch, registry

    def test_preview_applies_vendor_suffix_and_first_version_diff(self, tmp_path):
        orch, _ = self._export_orch(tmp_path)
        plan = asyncio.run(orch.prepare_export("s1"))
        assert plan.spec_preview.vendor.endswith("_custom")
        assert plan.permitted is True
        assert plan.diff.first_version is True  # no prior export -> all additions (Req 16.4)
        assert plan.version_display == ""  # unchanged with no prior export (Req 12.7)
        # session draft is not mutated by preview (Req 16.6 support).
        assert orch.session("s1").spec.vendor == "acme"

    def test_prior_export_triggers_patch_bump(self, tmp_path):
        orch, registry = self._export_orch(tmp_path)
        registry.record_creation("my_plugin", "acme_custom", "1.0.0")
        registry.record_export("my_plugin", "1.0.0", target="local")
        # A non-breaking addition to the loaded draft.
        asyncio.run(
            orch.apply_turn("s1", TurnPlan(operations=[AddComponent(ComponentKind.ACTION, "extra", make_action())]))
        )
        plan = asyncio.run(orch.prepare_export("s1"))
        assert str(plan.spec_preview.version) == "1.0.1"
        assert plan.version_display == "1.0.0 -> 1.0.1"

    def test_invalid_spec_blocks_export(self, tmp_path):
        orch, _ = self._export_orch(tmp_path)
        # Break the spec name so schema validation fails.
        from icplugin_builder.orchestrator import UpdateMetadata

        asyncio.run(orch.apply_turn("s1", TurnPlan(operations=[UpdateMetadata(name="Invalid Name!")])))
        plan = asyncio.run(orch.prepare_export("s1"))
        assert plan.permitted is False

    def test_registry_read_failure_aborts(self, tmp_path):
        orch, _ = self._export_orch(tmp_path, registry=RaisingRegistry())
        with pytest.raises(RegistryAccessError):
            asyncio.run(orch.prepare_export("s1"))


class TestPreviewFidelity:
    """The preview describes the spec that would be packaged (clauses 2.11, 2.12).

    The defect these cover: the preview evaluated the in-session draft, so once
    implementation was delegated to an agent that writes ``plugin.spec.yaml`` to
    the tree, the preview reported completeness findings about a spec that no
    longer existed -- and a forced export shipped that stale spec over the
    agent's. Disk wins here because disk is what gets packaged.
    """

    def _orch(self, tmp_path):
        create_project(tmp_path, name="my_plugin", vendor="acme")
        orch = Orchestrator(
            projects_root=tmp_path,
            registry=PluginRegistry(str(tmp_path / "registry.db")),
            audit_log=AuditLog(tmp_path / "audit.log"),
        )
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        return orch, ProjectFolder.open(tmp_path, "my_plugin")

    def test_the_preview_is_the_spec_on_disk_not_the_draft(self, tmp_path):
        orch, folder = self._orch(tmp_path)
        # Stand in for the delegated agent: rewrite the spec on the tree only.
        on_disk = make_spec(name="my_plugin", vendor="acme", description="written to the tree by the agent")
        folder.spec_path.write_text(dump_plugin_spec(on_disk), encoding="utf-8")

        plan = asyncio.run(orch.prepare_export("s1"))
        assert plan.spec_preview.description == "written to the tree by the agent"

    def test_the_completeness_findings_are_the_disk_specs(self, tmp_path):
        orch, folder = self._orch(tmp_path)
        complete = load_plugin_spec(
            dump_plugin_spec(make_spec(name="my_plugin", vendor="acme"))
            + "sdk:\n  type: full\n  version: 6.6.0\n  user: nobody\n"
        )
        folder.spec_path.write_text(dump_plugin_spec(complete), encoding="utf-8")

        plan = asyncio.run(orch.prepare_export("s1"))
        expected = check_completeness(plan.spec_preview)
        assert tuple(finding.key for finding in plan.completeness.findings) == tuple(
            finding.key for finding in expected.findings
        )

    def test_an_unparseable_spec_on_disk_leaves_the_draft_alone(self, tmp_path):
        orch, folder = self._orch(tmp_path)
        folder.spec_path.write_text("name: [unclosed\n", encoding="utf-8")

        plan = asyncio.run(orch.prepare_export("s1"))
        # Clause 2.11's fail-safe: the session keeps the draft it has rather than
        # being discarded, and the preview reports on it.
        assert plan.spec_preview.name == "my_plugin"
        assert orch.session("s1").spec.name == "my_plugin"

    def test_a_non_structural_in_session_edit_reaches_the_tree(self, tmp_path):
        """Property 64's hole: a metadata-only edit used to reach no file at all.

        Nothing wrote it, because the save was gated on structural change, so a
        later re-read of the tree would have silently discarded it. Clause 2.11
        persists the spec on any change, which is what makes "disk wins" safe.
        """
        orch, folder = self._orch(tmp_path)
        from icplugin_builder.orchestrator import UpdateMetadata

        asyncio.run(orch.apply_turn("s1", TurnPlan(operations=[UpdateMetadata(title="Renamed In Session")])))

        assert load_plugin_spec(folder.spec_path.read_text(encoding="utf-8")).title == "Renamed In Session"
        plan = asyncio.run(orch.prepare_export("s1"))
        assert plan.spec_preview.title == "Renamed In Session"

    def test_the_export_does_not_write_the_drafts_code_files_over_the_tree(self, tmp_path):
        """Clause 2.11: with the draft a view of the tree, writing it back can only lose work."""
        orch, folder = self._orch(tmp_path)
        (folder.path / "README.md").write_text("changed on the tree after the draft was read\n", encoding="utf-8")

        plan = asyncio.run(orch.prepare_export("s1"))
        asyncio.run(orch.confirm_export("s1", plan, confirmed=False))
        assert (folder.path / "README.md").read_text(encoding="utf-8").startswith("changed on the tree")


class TestReferenceMaterialReachesTheAgent:
    """Vendor documentation is obtained here and read by the agent as files (Req 28).

    The agent cannot look a vendor's API up: no fetch tool is enabled, and giving
    it one would put fetched pages inside the reasoning of a process that can run
    shell commands. So this tool retrieves, writes files, and names them in the
    instruction -- which is also why a PDF has to be extracted rather than written
    verbatim, since verbatim it is bytes the agent cannot read.
    """

    def _orch(self, tmp_path, *, fetcher=None):
        create_project(tmp_path, name="my_plugin", vendor="acme")
        agent = FakeAgent()
        acquirer = ReferenceAcquirer(fetcher=fetcher) if fetcher is not None else ReferenceAcquirer()
        orch = Orchestrator(
            cost_controller=CostController(),
            llm_generator=FakeLLM(),
            plugin_agent=agent,
            projects_root=tmp_path,
            reference_acquirer=acquirer,
        )
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        return orch, agent

    def _implement(self, orch):
        request = GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, pattern=None, parameters={"action": "check_ip"})
        return asyncio.run(orch.apply_turn("s1", TurnPlan(reasoning=[request])))

    def test_a_supplied_document_is_staged_and_named_to_the_agent(self, tmp_path):
        orch, agent = self._orch(tmp_path)
        orch.session("s1").attachments.append({"name": "openapi.yaml", "content": "openapi: 3.0.0\n"})

        self._implement(orch)

        _, instruction, _, _ = agent.calls[0]
        assert ".builder/reference/openapi.yaml" in instruction
        staged = tmp_path / "my_plugin" / ".builder" / "reference" / "openapi.yaml"
        assert staged.read_text() == "openapi: 3.0.0\n"

    def test_provenance_is_recorded_beside_the_document(self, tmp_path):
        orch, _ = self._orch(tmp_path)
        orch.session("s1").attachments.append({"name": "openapi.yaml", "content": "openapi: 3.0.0\n"})

        self._implement(orch)

        record = json.loads(
            (tmp_path / "my_plugin" / ".builder" / "reference" / "provenance.json").read_text(encoding="utf-8")
        )
        assert record["documents"][0]["origin"] == "attachment"
        assert len(record["documents"][0]["sha256"]) == 64

    def test_a_pdf_is_extracted_so_the_agent_can_read_it(self, tmp_path):
        # Written verbatim a PDF is binary; the agent reads it as noise and falls
        # back to inventing endpoints, which is the whole failure being prevented.
        orch, _ = self._orch(tmp_path)
        orch.session("s1").attachments.append(
            {
                "name": "vendor-api.pdf",
                "content": base64.b64encode(_one_page_pdf("GET /api/v2/reputation")).decode("ascii"),
                "encoding": "base64",
            }
        )

        self._implement(orch)

        staged = tmp_path / "my_plugin" / ".builder" / "reference" / "vendor-api.pdf"
        assert "GET /api/v2/reputation" in staged.read_text(encoding="utf-8")
        record = json.loads(
            (tmp_path / "my_plugin" / ".builder" / "reference" / "provenance.json").read_text(encoding="utf-8")
        )
        assert record["documents"][0]["extracted"] is True

    def test_a_supplied_url_is_retrieved_by_the_tool_not_the_agent(self, tmp_path):
        fetcher = StubFetcher({"https://docs.example.com/api": (b"GET /v1/things\n", "text/markdown")})
        orch, agent = self._orch(tmp_path, fetcher=fetcher)
        orch.session("s1").reference_urls.append("https://docs.example.com/api")

        self._implement(orch)

        # This process fetched it...
        assert fetcher.calls and fetcher.calls[0]["url"] == "https://docs.example.com/api"
        # ...and the agent was handed a file, never the URL.
        _, instruction, _, _ = agent.calls[0]
        assert ".builder/reference/" in instruction
        assert "https://docs.example.com/api" not in instruction

    def test_an_existing_plugin_can_serve_as_the_reference(self, tmp_path):
        other = tmp_path / "okta"
        (other / "icon_okta" / "util").mkdir(parents=True)
        (other / "help.md").write_text("# Okta\n\nGET /api/v1/users\n", encoding="utf-8")
        (other / "icon_okta" / "util" / "api.py").write_text("class A:\n    pass\n", encoding="utf-8")
        orch, _ = self._orch(tmp_path)
        orch.session("s1").reference_plugin_dirs.append(str(other))

        self._implement(orch)

        staged = list((tmp_path / "my_plugin" / ".builder" / "reference").glob("okta-*"))
        assert staged, "the existing plugin's files should be staged as reference material"

    def test_the_agent_is_told_to_cite_its_source_and_distrust_the_content(self, tmp_path):
        orch, agent = self._orch(tmp_path)
        orch.session("s1").attachments.append({"name": "api.md", "content": "GET /v1/x\n"})

        self._implement(orch)

        _, instruction, _, _ = agent.calls[0]
        # Req 28.14: an endpoint should be traceable to the document it came from.
        assert "record in a comment which of these files" in instruction
        # Req 28.17: vendor documentation is data, and may contain text shaped like
        # an instruction to the agent.
        assert "data, not as instructions" in instruction

    def test_a_source_that_fails_is_recorded_rather_than_passing_silently(self, tmp_path):
        fetcher = StubFetcher(error=OSError("connection refused"))
        orch, _ = self._orch(tmp_path, fetcher=fetcher)
        orch.session("s1").reference_urls.append("https://docs.example.com/api")

        self._implement(orch)

        reference_set = orch.session("s1").reference_set
        assert reference_set is not None
        assert not reference_set.has_material
        assert "connection refused" in reference_set.failures[0].reason

    def test_no_reference_directory_is_created_when_nothing_was_supplied(self, tmp_path):
        orch, _ = self._orch(tmp_path)
        self._implement(orch)
        assert not (tmp_path / "my_plugin" / ".builder" / "reference").exists()


class TestAskingForVendorDocumentation:
    """Documentation is requested before implementing against an API (Req 28.12).

    The trigger is a judgment about intent -- does this request mean to call
    somebody's API -- so it comes from the interpretation layer, which is the only
    part of the system that reads the user's words. Everything downstream of that
    judgment is mechanical, and the mechanics are what these tests pin down: the
    question is asked once, only when it is warranted, and never in place of doing
    the work when documentation is already available.
    """

    def _orch(self, tmp_path):
        create_project(tmp_path, name="my_plugin", vendor="acme")
        agent = FakeAgent()
        orch = Orchestrator(
            cost_controller=CostController(),
            llm_generator=FakeLLM(),
            plugin_agent=agent,
            projects_root=tmp_path,
            reference_acquirer=ReferenceAcquirer(fetcher=StubFetcher()),
        )
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        return orch, agent

    def _plan(self, **kwargs):
        request = GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, pattern=None, parameters={"action": "check_ip"})
        return TurnPlan(reasoning=[request], **kwargs)

    def test_a_vendor_plugin_with_no_documentation_asks_for_it(self, tmp_path):
        orch, agent = self._orch(tmp_path)

        result = asyncio.run(orch.apply_turn("s1", self._plan(vendor_api="AbuseIPDB")))

        assert result.status is TurnStatus.CLARIFICATION
        assert "AbuseIPDB" in result.message
        # All three routes are offered, not just the one the user did not use.
        assert "OpenAPI" in result.message
        assert "attached as a file" in result.message
        assert "existing" in result.message
        # Nothing was implemented, and the draft is untouched (Req 1.5).
        assert agent.calls == []

    def test_it_says_how_to_proceed_anyway(self, tmp_path):
        # The question must not be a dead end: an operator who has no docs and
        # wants a skeleton is entitled to one, as long as it is recorded.
        orch, _ = self._orch(tmp_path)
        result = asyncio.run(orch.apply_turn("s1", self._plan(vendor_api="AbuseIPDB")))
        assert "proceed anyway" in result.message
        assert "unfinished" in result.message

    def test_a_local_only_plugin_is_never_asked(self, tmp_path):
        # No vendor API means no documentation to want. Asking here would be the
        # false alarm that trains the operator to dismiss the question.
        orch, agent = self._orch(tmp_path)

        result = asyncio.run(orch.apply_turn("s1", self._plan(vendor_api=None)))

        assert result.status is TurnStatus.APPLIED
        assert len(agent.calls) == 1

    def test_a_structural_only_turn_is_never_asked(self, tmp_path):
        # Nothing is being implemented yet, so there is nothing to implement
        # against documentation.
        orch, _ = self._orch(tmp_path)
        plan = TurnPlan(
            operations=[AddComponent(ComponentKind.ACTION, "extra", make_action())],
            vendor_api="AbuseIPDB",
        )
        result = asyncio.run(orch.apply_turn("s1", plan))
        assert result.status is TurnStatus.APPLIED

    def test_supplied_documentation_means_no_question(self, tmp_path):
        orch, agent = self._orch(tmp_path)
        orch.session("s1").attachments.append({"name": "api.md", "content": "GET /v1/check\n"})

        result = asyncio.run(orch.apply_turn("s1", self._plan(vendor_api="AbuseIPDB")))

        assert result.status is TurnStatus.APPLIED
        assert len(agent.calls) == 1

    def test_a_supplied_url_means_no_question(self, tmp_path):
        orch, _ = self._orch(tmp_path)
        orch.session("s1").reference_urls.append("https://docs.example.com/api")
        result = asyncio.run(orch.apply_turn("s1", self._plan(vendor_api="AbuseIPDB")))
        assert result.status is TurnStatus.APPLIED

    def test_documentation_already_in_the_tree_means_no_question(self, tmp_path):
        # A second turn on a plugin whose docs were staged earlier must not re-ask.
        orch, _ = self._orch(tmp_path)
        directory = tmp_path / "my_plugin" / ".builder" / "reference"
        directory.mkdir(parents=True)
        (directory / "provenance.json").write_text(
            json.dumps({"documents": [{"name": "api.md"}], "failures": []}), encoding="utf-8"
        )

        result = asyncio.run(orch.apply_turn("s1", self._plan(vendor_api="AbuseIPDB")))

        assert result.status is TurnStatus.APPLIED

    def test_proceeding_anyway_implements_and_records_the_gap(self, tmp_path):
        orch, agent = self._orch(tmp_path)

        result = asyncio.run(orch.apply_turn("s1", self._plan(vendor_api="AbuseIPDB", proceed_without_reference=True)))

        assert result.status is TurnStatus.APPLIED
        assert len(agent.calls) == 1
        # Req 28.13: recorded in the tree, so it outlives the session.
        record = json.loads(
            (tmp_path / "my_plugin" / ".builder" / "reference" / "provenance.json").read_text(encoding="utf-8")
        )
        assert record["implemented_without_reference"] is True

    def test_the_recorded_gap_is_reported_as_an_unmet_condition(self, tmp_path):
        orch, _ = self._orch(tmp_path)
        asyncio.run(orch.apply_turn("s1", self._plan(vendor_api="AbuseIPDB", proceed_without_reference=True)))

        done = orch.session("s1").done_report
        assert done is not None
        unmet = {condition.name for condition in done.unmet}
        assert "reference_material" in unmet

    def test_the_question_is_asked_once_per_session(self, tmp_path):
        # Having said "go ahead", the user should not be asked again on the next
        # message. The decision belongs to the session, not the turn.
        orch, agent = self._orch(tmp_path)
        asyncio.run(orch.apply_turn("s1", self._plan(vendor_api="AbuseIPDB", proceed_without_reference=True)))

        second = asyncio.run(orch.apply_turn("s1", self._plan(vendor_api="AbuseIPDB")))

        assert second.status is TurnStatus.APPLIED
        assert len(agent.calls) == 2


class TestExportPathChecksTheCode:
    """The export path checks the hand-written code itself (Req 26.1, 27.1).

    The repair loop only runs after an implementation turn. A draft opened from
    disk and exported straight away never passes through it, so before this the
    only thing that had looked at its hand-written code was the containerized
    pipeline -- and a passing pipeline says nothing about whether the plugin has an
    API client or a real connection test.
    """

    def _orch(self, tmp_path, *, checker=None, quality_gate=True):
        create_project(tmp_path, name="my_plugin", vendor="acme")
        gate = checker if checker is not None else ScriptedChecker([_report()])
        return (
            Orchestrator(
                projects_root=tmp_path,
                code_validator=FakeCodeValidator(passing=True),
                quality_gate=gate if quality_gate else None,
                registry=PluginRegistry(str(tmp_path / "registry.db")),
                audit_log=AuditLog(tmp_path / "audit.log"),
            ),
            gate,
        )

    def test_the_quality_gate_runs_on_the_export_path(self, tmp_path):
        orch, gate = self._orch(tmp_path)
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")

        plan = asyncio.run(orch.prepare_export("s1"))

        # No implementation turn happened, so nothing else would have checked it.
        assert gate.calls == 1
        assert plan.quality_report is not None

    def test_the_preview_carries_the_definition_of_done_verdict(self, tmp_path):
        orch, _ = self._orch(tmp_path)
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")

        plan = asyncio.run(orch.prepare_export("s1"))

        assert plan.done_report is not None
        # This bare project has no API client and no connection module, so it is
        # not finished -- even though the fake pipeline passes and export is
        # permitted. The two answers are independent, and both are reported.
        assert plan.permitted is True
        assert plan.plugin_is_done is False
        assert "not complete" in plan.summary()
        assert "Export permitted" in plan.summary()
        assert orch.session("s1").done_report is plan.done_report

    def test_an_unfinished_plugin_is_never_summarised_as_ready(self, tmp_path):
        orch, _ = self._orch(tmp_path)
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")

        plan = asyncio.run(orch.prepare_export("s1"))

        # Req 27.2: each outstanding condition is named, so "not finished" is
        # actionable rather than a verdict.
        for condition in plan.done_report.outstanding:
            assert condition.name in plan.done_report.summary()
        assert plan.done_report.outstanding

    def test_a_report_from_this_turns_repair_is_reused_rather_than_rerun(self, tmp_path):
        # Re-running prospector and the plugin's unit tests seconds after the
        # repair loop finished would double the wait for the same answer.
        orch, gate = self._orch(tmp_path)
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        session = orch.session("s1")
        existing = QualityReport(project_dir=session.project_folder.path, findings=())
        session.repair_outcome = SimpleNamespace(final_report=existing)

        plan = asyncio.run(orch.prepare_export("s1"))

        assert gate.calls == 0
        assert plan.quality_report is existing

    def test_a_report_for_a_different_tree_is_not_reused(self, tmp_path):
        # A stale report from another plugin must not stand in for this one.
        orch, gate = self._orch(tmp_path)
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        stale = QualityReport(project_dir=Path("/somewhere/else"), findings=())
        orch.session("s1").repair_outcome = SimpleNamespace(final_report=stale)

        plan = asyncio.run(orch.prepare_export("s1"))

        assert gate.calls == 1
        assert plan.quality_report is not stale

    def test_without_a_gate_the_conditions_are_unverified_not_met(self, tmp_path):
        # No gate configured: the code-quality conditions cannot be evaluated, and
        # must come back unverified rather than quietly passing (Req 27.5).
        orch, _ = self._orch(tmp_path, quality_gate=False)
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")

        plan = asyncio.run(orch.prepare_export("s1"))

        assert plan.quality_report is None
        assert plan.plugin_is_done is False
        unverified = {condition.name for condition in plan.done_report.unverified}
        assert {"code_parses", "lint_clean", "unit_tests_pass"} <= unverified


# --- confirm & export (Req 9, 10, 16.5, 16.6, 18, 19.2) --------------------


class TestConfirmExport:
    def _orch(self, tmp_path, *, uploader=None):
        create_project(tmp_path, name="my_plugin", vendor="acme")
        registry = PluginRegistry(str(tmp_path / "registry.db"))
        export_manager = ExportManager(uploader=uploader) if uploader is not None else ExportManager()
        orch = Orchestrator(
            projects_root=tmp_path,
            code_validator=FakeCodeValidator(passing=True),
            registry=registry,
            audit_log=AuditLog(tmp_path / "audit.log"),
            export_manager=export_manager,
        )
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        return orch, registry

    def test_decline_aborts_without_artifact(self, tmp_path):
        orch, registry = self._orch(tmp_path)
        before = orch.session("s1").spec
        plan = asyncio.run(orch.prepare_export("s1"))
        outcome = asyncio.run(orch.confirm_export("s1", plan, confirmed=False))
        assert outcome.status is ExportStatus.ABORTED
        # Req 16.6: the preview leaves the draft's spec unchanged. Compared by
        # value rather than by identity because clause 2.11 has the preview
        # re-read the draft from the tree, so the object is a fresh view of the
        # same spec -- what must not change is the value.
        assert orch.session("s1").spec == before
        assert registry.exports("my_plugin") == []

    def test_blocked_gate_refuses_build(self, tmp_path):
        create_project(tmp_path, name="my_plugin", vendor="acme")
        registry = PluginRegistry(str(tmp_path / "registry.db"))
        # No code validator -> pipeline not run -> gate blocks.
        orch = Orchestrator(projects_root=tmp_path, registry=registry, audit_log=AuditLog(tmp_path / "audit.log"))
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="my_plugin")
        plan = asyncio.run(orch.prepare_export("s1"))
        outcome = asyncio.run(orch.confirm_export("s1", plan, confirmed=True))
        assert outcome.status is ExportStatus.BLOCKED

    def test_local_export_success_records_registry_and_audit(self, tmp_path):
        orch, registry = self._orch(tmp_path)
        out_dir = tmp_path / "out"
        plan = asyncio.run(orch.prepare_export("s1"))
        outcome = asyncio.run(orch.confirm_export("s1", plan, confirmed=True, target="local", output_dir=out_dir))
        assert outcome.status is ExportStatus.SUCCEEDED
        assert outcome.artifact_path is not None
        exports = registry.exports("my_plugin")
        assert len(exports) == 1 and exports[0].target == "local"
        # The draft vendor is now suffixed (applied at build time, Req 13.3).
        assert orch.session("s1").spec.vendor.endswith("_custom")

    def test_tenant_export_success(self, tmp_path):
        orch, registry = self._orch(tmp_path, uploader=FakeUploader(status_code=200))
        creds = TenantCredentials(region_base_url="https://us.example.com", api_key="secret-key")
        plan = asyncio.run(orch.prepare_export("s1"))
        outcome = asyncio.run(orch.confirm_export("s1", plan, confirmed=True, target="tenant", credentials=creds))
        assert outcome.status is ExportStatus.SUCCEEDED
        exports = registry.exports("my_plugin")
        assert exports and exports[0].target == "https://us.example.com"

    def test_tenant_export_failure_retains_artifact_registry_unchanged(self, tmp_path):
        orch, registry = self._orch(tmp_path, uploader=FakeUploader(status_code=500))
        creds = TenantCredentials(region_base_url="https://us.example.com", api_key="secret-key")
        plan = asyncio.run(orch.prepare_export("s1"))
        outcome = asyncio.run(orch.confirm_export("s1", plan, confirmed=True, target="tenant", credentials=creds))
        assert outcome.status is ExportStatus.EXPORT_FAILED
        assert outcome.retained_artifact_path is not None  # retained >=24h (Req 19.2)
        assert outcome.failure is not None and outcome.failure.is_export_failure
        assert registry.exports("my_plugin") == []  # registry unchanged (Req 10.3)


class StubFetcher:
    """A reference fetcher returning scripted bytes, so no network is contacted."""

    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.error = error
        self.calls = []

    def fetch(self, url, *, timeout, max_bytes):
        self.calls.append({"url": url, "timeout": timeout, "max_bytes": max_bytes})
        if self.error is not None:
            raise self.error
        data, media_type = self.responses[url]
        return FetchedBytes(data=data, media_type=media_type, url=url)


def _one_page_pdf(text):
    """Build a real single-page PDF carrying ``text``, for the extraction path."""
    import io

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
