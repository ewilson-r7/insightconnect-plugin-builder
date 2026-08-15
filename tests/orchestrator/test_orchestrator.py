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
from pathlib import Path
from types import SimpleNamespace

import pytest

from icplugin_builder.core.cost_controller import CostController
from icplugin_builder.core.draft import ComponentKind
from icplugin_builder.core.generation import ArtifactKind, GenerationRequest
from icplugin_builder.core.spec_model import Component, PluginSpec, SemVer
from icplugin_builder.core.yaml_codec import load_plugin_spec
from icplugin_builder.integrations.code_validator import (
    PipelineReport,
    StageName,
    StageResult,
    StageStatus,
)
from icplugin_builder.integrations.export_manager import ExportManager, TenantCredentials, UploadResponse
from icplugin_builder.integrations.insight_plugin_cli import (
    InsightPluginCliError,
    ProjectTree,
    snapshot_tree,
)
from icplugin_builder.integrations.refresh_coordinator import RefreshCoordinator
from icplugin_builder.orchestrator import (
    AddComponent,
    EntryModeError,
    ModifyComponent,
    Orchestrator,
    RegistryAccessError,
    TurnPlan,
    TurnStatus,
)
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

    def __init__(self, summary="Implemented 1 action; insight-plugin validate passed.", tokens=31):
        self.summary = summary
        self.tokens = tokens
        self.calls = []

    async def implement(self, project_dir, instruction, *, session_id, user_id):
        self.calls.append((str(project_dir), instruction, session_id, user_id))
        return SimpleNamespace(
            succeeded=True,
            summary=self.summary,
            transcript=self.summary,
            changed_files=("icon_x/util/api.py",),
            credits=0.2,
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
        # The turn reports the agent's own account of what it did.
        assert result.message == agent.summary

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
        assert orch.session("s1").spec is before  # unchanged (Req 16.6)
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
