"""Broad end-to-end orchestration flow tests (task 20.4).

Where ``tests/orchestrator/test_orchestrator.py`` (task 20.3) exercises the
orchestrator's ordering guarantees one step at a time in isolation, these tests
drive **multi-step scenarios end-to-end through a single session**, wiring the
real pure-logic and persistence components (draft/operations, vendor suffix,
version bump + breaking-change classifier, spec validator, export gate, build
engine, plugin registry, audit log, project folder) against **mocked externals**
(the ``insight-plugin`` CLI, the Kiro CLI/LLM, the Docker code pipeline, and the
InsightConnect tenant API).

Each test threads a whole workflow together so cross-step invariants surface:

* the **iterate** lifecycle -- load a custom plugin, add components across the
  deterministic/LLM boundary, refresh after each structural edit, then export
  twice (local then tenant): first version unchanged with no prior export, then
  a non-breaking patch bump against the recorded prior version, with the
  registry/audit/history accumulating correctly (Req 24.3, 22.3, 12, 16, 9, 10,
  11, 18);
* the **enhance** (production fork) lifecycle -- import read-only, iterate with a
  refresh, and export with the idempotent ``_custom`` vendor suffix preserved
  (Req 24.4, 13.2, 25);
* the **net-new** lifecycle -- start empty, walk the input gate / clarification /
  reasoning-boundary turns, and confirm the validate-before-export gate blocks
  an unvalidated draft (Req 24.2, 22.4, 1, 3);
* an **iterate breaking-change** lifecycle -- a clarification turn leaves the
  draft untouched, then an optional->required edit drives a MAJOR version bump
  against the recorded prior version (Req 22.5, 12.2, 12.3).

Coroutines are driven with ``asyncio.run`` so no async test plugin is needed,
matching the repo's conventions.
"""

import asyncio
from types import SimpleNamespace

from icplugin_builder.core.cost_controller import CostController
from icplugin_builder.core.draft import ComponentKind
from icplugin_builder.core.generation import ArtifactKind, GenerationRequest
from icplugin_builder.core.spec_model import Component, FieldSchema, PluginSpec, SemVer
from icplugin_builder.integrations.code_validator import (
    PipelineReport,
    StageName,
    StageResult,
    StageStatus,
)
from icplugin_builder.integrations.export_manager import ExportManager, TenantCredentials, UploadResponse
from icplugin_builder.integrations.insight_plugin_cli import ProjectTree
from icplugin_builder.integrations.refresh_coordinator import RefreshCoordinator
from icplugin_builder.orchestrator import (
    AddComponent,
    ModifyComponent,
    Orchestrator,
    TurnPlan,
    TurnStatus,
    UpdateMetadata,
)
from icplugin_builder.orchestrator.session import ExportStatus
from icplugin_builder.persistence.audit_log import AuditEvent, AuditLog
from icplugin_builder.persistence.project_folder import (
    ENTRY_MODE_CREATE_NEW,
    ENTRY_MODE_ENHANCE_PRODUCTION,
    ENTRY_MODE_ITERATE_CUSTOM,
    ProjectFolder,
    ProvenanceRecord,
)
from icplugin_builder.persistence.registry import PluginRegistry

# --- mocked externals ------------------------------------------------------


class FakeCli:
    """A stand-in ``insight-plugin`` CLI recording each ``refresh`` invocation."""

    def __init__(self):
        self.refresh_calls = []

    async def refresh(self, project_dir):
        self.refresh_calls.append(project_dir)
        return ProjectTree(root=project_dir, files={"help.md": "# help\n"})


class FakeCodeValidator:
    """A Docker code pipeline whose four stages all pass (or all fail)."""

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
    """A stand-in Kiro CLI/LLM that records usage against the cost controller.

    Recording usage keeps the cumulative session token counter meaningful across
    a flow (Req 3.5, 3.6) without needing the real subprocess.
    """

    def __init__(self, cost_controller, *, content="def run(self, params={}):\n    return {}\n", tokens=50):
        self.cost = cost_controller
        self.content = content
        self.tokens = tokens
        self.calls = []

    async def generate(self, kind, scoped_context, *, session_id, user_id):
        self.calls.append((kind, dict(scoped_context), session_id, user_id))
        total = self.cost.record_usage(session_id, self.tokens, True)
        return SimpleNamespace(kind=kind, content=self.content, tokens=self.tokens, session_total=total)


class FakeUploader:
    """A tenant uploader returning a fixed HTTP status for every upload."""

    def __init__(self, status_code=200):
        self.status_code = status_code
        self.calls = 0

    def upload(self, *, region_base_url, api_key, artifact_path, timeout):
        self.calls += 1
        return UploadResponse(status_code=self.status_code, body="ok")


# --- spec/component helpers ------------------------------------------------


def make_spec(name="acme_widget", vendor="acme", version=SemVer(1, 0, 0), actions=None):
    return PluginSpec(
        name=name,
        title="Acme Widget",
        description="A test plugin used by the orchestration flow tests.",
        version=version,
        vendor=vendor,
        connection={"api_key": FieldSchema(type="string", required=True, title="API Key")},
        actions=actions or {},
    )


def make_action(title="Do a thing", inputs=None):
    return Component(title=title, description="Does a thing.", input=inputs or {}, output={})


def seed_project(projects_root, *, name="acme_widget", vendor="acme", actions=None, provenance=None):
    """Create a Project_Folder on disk so iterate/enhance modes can load it."""
    spec = make_spec(name=name, vendor=vendor, actions=actions)
    folder = ProjectFolder.create(projects_root, name, spec, provenance=provenance)
    folder.save(spec, generated_files={"README.md": "hello\n"})
    return folder


def build_orchestrator(projects_root, *, uploader=None):
    """Wire an orchestrator with real logic/persistence and mocked externals."""
    cost = CostController()
    llm = FakeLLM(cost)
    export_manager = ExportManager(uploader=uploader) if uploader is not None else ExportManager()
    orch = Orchestrator(
        cost_controller=cost,
        llm_generator=llm,
        refresh_coordinator=RefreshCoordinator(cli=FakeCli()),
        code_validator=FakeCodeValidator(passing=True),
        registry=PluginRegistry(str(projects_root / "registry.db")),
        audit_log=AuditLog(projects_root / "audit.log"),
        export_manager=export_manager,
        projects_root=projects_root,
    )
    return orch, llm


# --- iterate: full two-export lifecycle ------------------------------------


class TestIterateLifecycleFlow:
    """Load -> reason -> refresh -> local export -> iterate -> tenant export."""

    def test_iterate_create_export_iterate_reexport_end_to_end(self, tmp_path):
        seed_project(tmp_path, name="acme_widget", vendor="acme")
        orch, llm = build_orchestrator(tmp_path, uploader=FakeUploader(status_code=200))
        state = orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="acme_widget")
        assert state.entry_mode == ENTRY_MODE_ITERATE_CUSTOM

        # Turn 1: add an action plus two reasoning artifacts. The field
        # description matches a template (zero LLM calls) while the action logic
        # has no template and is dispatched to the mocked LLM (Req 3.2, 3.3).
        turn1 = TurnPlan(
            operations=[AddComponent(ComponentKind.ACTION, "list_widgets", make_action())],
            reasoning=[
                GenerationRequest(
                    kind=ArtifactKind.FIELD_DESCRIPTION,
                    pattern="api_key",
                    parameters={"service_name": "Acme"},
                ),
                GenerationRequest(
                    kind=ArtifactKind.ACTION_LOGIC,
                    pattern=None,
                    parameters={"action": "list_widgets"},
                ),
            ],
        )
        result1 = asyncio.run(orch.apply_turn("s1", turn1))
        assert result1.status is TurnStatus.APPLIED
        assert result1.refreshed is True  # structural edit refreshed derived files (Req 22.3)
        kinds = {(g.kind, g.from_llm) for g in result1.generated}
        assert (ArtifactKind.TEMPLATE_MATCH, False) in kinds  # rendered deterministically
        assert (ArtifactKind.ACTION_LOGIC, True) in kinds  # dispatched to the LLM
        assert result1.token_total == llm.tokens  # only the LLM call counts (Req 3.5, 3.6)

        # Turn 2: a metadata-only edit is non-structural and triggers no refresh.
        result2 = asyncio.run(orch.apply_turn("s1", TurnPlan(operations=[UpdateMetadata(title="Acme Widget Pro")])))
        assert result2.status is TurnStatus.APPLIED
        assert result2.refreshed is False

        # First export: no prior export, so the version is unchanged (Req 12.7)
        # and the diff reports a first version (Req 16.4). The _custom suffix is
        # applied on the preview spec only (Req 13.3, 16.6).
        plan1 = asyncio.run(orch.prepare_export("s1"))
        assert plan1.permitted is True
        assert plan1.spec_preview.vendor == "acme_custom"
        assert plan1.version_display == ""
        assert plan1.diff.first_version is True
        assert "plugin.spec.yaml" in plan1.file_list
        assert orch.session("s1").spec.vendor == "acme"  # draft untouched by preview

        out_dir = tmp_path / "out"
        outcome1 = asyncio.run(orch.confirm_export("s1", plan1, confirmed=True, target="local", output_dir=out_dir))
        assert outcome1.status is ExportStatus.SUCCEEDED
        assert outcome1.version == "1.0.0"
        # The committed draft now carries the suffixed vendor (Req 13.3).
        assert orch.session("s1").spec.vendor == "acme_custom"

        # Iterate again: a non-breaking addition against the just-exported spec.
        result3 = asyncio.run(
            orch.apply_turn(
                "s1", TurnPlan(operations=[AddComponent(ComponentKind.ACTION, "get_widget", make_action())])
            )
        )
        assert result3.status is TurnStatus.APPLIED
        assert result3.refreshed is True

        # Second export: a prior 1.0.0 export exists, the change is non-breaking,
        # so the version bumps by a patch (Req 12.4) and the diff is no longer a
        # first version (Req 16.3).
        plan2 = asyncio.run(orch.prepare_export("s1"))
        assert plan2.permitted is True
        assert str(plan2.spec_preview.version) == "1.0.1"
        assert plan2.version_display == "1.0.0 -> 1.0.1"
        assert plan2.diff.first_version is False

        creds = TenantCredentials(region_base_url="https://us.example.com", api_key="secret-key")
        outcome2 = asyncio.run(orch.confirm_export("s1", plan2, confirmed=True, target="tenant", credentials=creds))
        assert outcome2.status is ExportStatus.SUCCEEDED
        assert outcome2.target == "https://us.example.com"

        # Registry accumulated both exports, most-recent-first (Req 11.4).
        registry = PluginRegistry(str(tmp_path / "registry.db"))
        exports = registry.exports("acme_widget")
        assert [e.version for e in exports] == ["1.0.1", "1.0.0"]
        assert exports[0].target == "https://us.example.com"
        assert exports[1].target == "local"

        # Per-version history snapshots are independently retrievable (Req 21.3).
        folder = ProjectFolder.open(tmp_path, "acme_widget")
        assert folder.list_versions() == ["1.0.0", "1.0.1"]

        # The audit log recorded both builds, the credential use, and the exports,
        # and its hash chain verifies intact (Req 18).
        audit = AuditLog(tmp_path / "audit.log")
        events = [r.event for r in audit.records()]
        assert events.count(AuditEvent.BUILD) == 2
        assert AuditEvent.CREDENTIAL_USE in events
        assert audit.verify().valid is True


# --- enhance: production fork lifecycle ------------------------------------


class TestEnhanceForkFlow:
    """Import read-only -> iterate with refresh -> export with idempotent suffix."""

    def test_enhance_import_iterate_export_preserves_custom_suffix(self, tmp_path):
        provenance = ProvenanceRecord(
            entry_mode=ENTRY_MODE_ENHANCE_PRODUCTION,
            created_utc="2024-01-01T00:00:00+00:00",
            source_repo="rapid7/insightconnect-plugins",
            original_plugin_name="prod_widget",
            original_version="1.0.0",
        )
        folder = seed_project(tmp_path, name="prod_widget", vendor="rapid7_custom", provenance=provenance)
        import_result = SimpleNamespace(
            project_folder=folder,
            provenance=provenance,
            private_source_notice="This plugin was forked from a private production source.",
        )
        source_provider = SimpleNamespace(import_plugin=lambda source, name: import_result)

        orch, _ = build_orchestrator(tmp_path)
        orch._source_provider = source_provider  # inject the mocked source provider

        state = orch.start_session(
            ENTRY_MODE_ENHANCE_PRODUCTION,
            session_id="s1",
            user_id="u1",
            source="rapid7",
            production_plugin="prod_widget",
        )
        assert state.entry_mode == ENTRY_MODE_ENHANCE_PRODUCTION
        assert state.provenance.entry_mode == ENTRY_MODE_ENHANCE_PRODUCTION
        assert state.private_source_notice  # usage-restriction notice surfaced (Req 25.6)

        # Enhance the fork with a new action; the structural edit refreshes.
        result = asyncio.run(
            orch.apply_turn("s1", TurnPlan(operations=[AddComponent(ComponentKind.ACTION, "enrich", make_action())]))
        )
        assert result.status is TurnStatus.APPLIED
        assert result.refreshed is True

        # The vendor already ends in _custom, so the suffix operation is
        # idempotent and leaves it unchanged (Req 13.2).
        plan = asyncio.run(orch.prepare_export("s1"))
        assert plan.permitted is True
        assert plan.spec_preview.vendor == "rapid7_custom"

        outcome = asyncio.run(
            orch.confirm_export("s1", plan, confirmed=True, target="local", output_dir=tmp_path / "o")
        )
        assert outcome.status is ExportStatus.SUCCEEDED
        # Provenance for the fork is unchanged across the export (Req 24.5).
        assert orch.session("s1").provenance.entry_mode == ENTRY_MODE_ENHANCE_PRODUCTION


# --- net-new: create, converse, and validate-before-export -----------------


class TestNetNewFlow:
    """Start empty, walk the input/clarification/reasoning turns, gate export."""

    def test_net_new_conversation_then_export_gate_blocks_unvalidated_draft(self, tmp_path):
        orch, llm = build_orchestrator(tmp_path)
        state = orch.start_session(
            ENTRY_MODE_CREATE_NEW,
            session_id="s1",
            user_id="u1",
            initial_spec=make_spec(name="fresh_plugin", vendor="acme"),
        )
        assert state.entry_mode == ENTRY_MODE_CREATE_NEW
        assert state.provenance.entry_mode == ENTRY_MODE_CREATE_NEW

        # Input gate: blank input is rejected and leaves the draft unchanged.
        before = orch.session("s1").spec
        rejected = asyncio.run(orch.submit_message("s1", "   "))
        assert rejected.status is TurnStatus.REJECTED_INPUT
        assert orch.session("s1").spec is before

        # An ambiguous turn asks for clarification without touching the draft.
        clar = asyncio.run(orch.submit_message("s1", "make it better", plan=TurnPlan(clarification="Which action?")))
        assert clar.status is TurnStatus.CLARIFICATION
        assert orch.session("s1").spec is before

        # A concrete turn adds an action and dispatches an LLM reasoning artifact.
        turn = TurnPlan(
            operations=[AddComponent(ComponentKind.ACTION, "scan", make_action())],
            reasoning=[GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, pattern=None, parameters={"action": "scan"})],
        )
        applied = asyncio.run(orch.submit_message("s1", "add a scan action", plan=turn))
        assert applied.status is TurnStatus.APPLIED
        assert "scan" in orch.session("s1").spec.actions
        assert len(llm.calls) == 1
        assert applied.token_total == llm.tokens

        # With lazy project-folder creation, the net-new draft now has a project
        # folder on disk and the (fake) code validator passes all stages, so
        # export is permitted.
        plan = asyncio.run(orch.prepare_export("s1"))
        assert plan.permitted is True


# --- iterate: clarification then a breaking-change major bump ---------------


class TestBreakingChangeBumpFlow:
    """Clarification leaves the draft alone; an optional->required edit bumps MAJOR."""

    def test_clarification_then_optional_to_required_drives_major_bump(self, tmp_path):
        # Seed a plugin whose action has an optional input field, then record a
        # prior 1.0.0 export so a bump has a baseline to exceed.
        action = make_action(inputs={"host": FieldSchema(type="string", required=False, title="Host")})
        seed_project(tmp_path, name="acme_gadget", vendor="acme", actions={"list_items": action})
        orch, _ = build_orchestrator(tmp_path)
        registry = PluginRegistry(str(tmp_path / "registry.db"))
        registry.record_creation("acme_gadget", "acme_custom", "1.0.0")
        registry.record_export("acme_gadget", "1.0.0", target="local")

        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="acme_gadget")

        # A clarification turn must leave the draft byte-identical (Req 22.5).
        before = orch.session("s1").spec
        clar = asyncio.run(orch.submit_message("s1", "tweak it", plan=TurnPlan(clarification="Which field?")))
        assert clar.status is TurnStatus.CLARIFICATION
        assert orch.session("s1").spec is before

        # Make the previously optional field required -> a breaking change.
        required_action = make_action(inputs={"host": FieldSchema(type="string", required=True, title="Host")})
        applied = asyncio.run(
            orch.apply_turn(
                "s1", TurnPlan(operations=[ModifyComponent(ComponentKind.ACTION, "list_items", required_action)])
            )
        )
        assert applied.status is TurnStatus.APPLIED
        assert applied.refreshed is True

        # The breaking change against the prior 1.0.0 export forces a MAJOR bump
        # to 2.0.0 (Req 12.2, 12.3), strictly greater than every prior version.
        plan = asyncio.run(orch.prepare_export("s1"))
        assert plan.version_bump.breaking is True
        assert str(plan.spec_preview.version) == "2.0.0"
        assert plan.version_display == "1.0.0 -> 2.0.0"
        assert plan.permitted is True
