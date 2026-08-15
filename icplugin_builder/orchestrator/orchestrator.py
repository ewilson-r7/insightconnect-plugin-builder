"""The Orchestrator: the single component that sequences the end-to-end flow.

The ``Orchestrator`` is deliberately the only component that mutates the
in-session draft and sequences side effects, so the workflow invariants live in
one exhaustively testable place (design "Orchestrator Responsibilities"). It
wires together the already-built pieces -- the :class:`Draft`, the deterministic
``insight-plugin`` CLI wrapper and its :class:`RefreshCoordinator`, the
generation classifier + :class:`TemplateLibrary`, the :class:`LLMGenerator`, the
:class:`SpecValidator` and :class:`CodeValidator`, the export gate, the
:class:`BuildEngine`, the :class:`ExportManager`, the :class:`PluginRegistry`,
:class:`AuditLog`, and :class:`ProjectFolder`, and the :class:`CostController` --
and enforces the ordering guarantees across them:

* **Entry mode** is established at session start (net-new / iterate / enhance)
  and routed accordingly, recording a :class:`ProvenanceRecord` for the draft
  regardless of mode (Req 24.1-24.5).
* **Draft custody** -- the session holds one draft; every turn transforms it
  through the atomic apply wrapper so a failing step leaves it byte-identical
  (Req 1.7, 15.1-15.4).
* **Deterministic/LLM boundary** -- each requested reasoning artifact is
  classified; a template match renders deterministically (zero LLM calls, Req
  3.3) while an unmatched reasoning kind is dispatched to the cost-gated
  :class:`LLMGenerator` (Req 3.2, 3.4).
* **Refresh after structural edit** -- when a turn changes the spec's structural
  surface, ``insight-plugin refresh`` regenerates derived files (Req 22.3).
* **Validate-before-export / version-bump-before-build** -- an export is
  previewed only after the ``_custom`` vendor suffix is applied (Req 13.3), the
  registry is read for prior versions (aborting on read failure, Req 12.8), the
  version is bumped (Req 12), and the spec + code are validated; the gate
  permits export iff the spec is valid and all four code stages pass (Req 7.4,
  8.6, 8.7). Preview, file list, and prior-version diff are surfaced and
  explicit confirmation is required before the build (Req 16).
* **Audit** -- build and export emit audit records (Req 18.2, 18.3, 18.6).

Everything costly or non-deterministic (the CLI, Docker, the LLM, the tenant
API, git remotes) is injected, so the orchestrator is fully mockable under test.
Collaborators are optional where a flow can run without them (e.g. an in-memory
net-new draft with no project folder), which keeps the object usable in slices
while the API/UI layers are built on top (tasks 22-24).
"""

from __future__ import annotations

import copy
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from ..core.atomic import atomic_apply
from ..core.cost_controller import CostController
from ..core.diff import diff_file_trees
from ..core.draft import ComponentNotFoundError, Draft, DraftError
from ..core.generation import (
    CODE_ARTIFACT_KINDS,
    ArtifactKind,
    GenerationRequest,
    TemplateLibrary,
    classify_request,
    default_template_library,
)
from ..core.input_validation import validate_conversation_input
from ..core.spec_completeness import check_completeness, with_sdk_version
from ..core.spec_model import PluginSpec, SemVer
from ..core.spec_validator import SpecValidator, ValidationReport
from ..core.vendor import apply_custom_vendor_suffix
from ..core.version_bump import apply_version_bump, bump_for_export
from ..core.yaml_codec import dump_plugin_spec, load_plugin_spec
from ..integrations.build_engine import BuildEngine, BuildEngineError
from ..integrations.build_prep import resolve_sdk_version
from ..integrations.build_export_failure import (
    classify_export_failure,
    retain_failed_export_artifact,
)
from ..integrations.code_validator import CodeValidator, PipelineReport
from ..integrations.export_gate import decide_export
from ..integrations.export_manager import ExportManager, TenantCredentials
from ..integrations.insight_plugin_cli import InsightPluginCli, InsightPluginCliError
from ..integrations.llm_generator import CostLimitError, LLMGenerator, LLMGeneratorError
from ..integrations.plugin_agent import AgentRunResult, PluginAgent
from ..integrations.quality_gate import QualityReport
from ..integrations.refresh_coordinator import RefreshCoordinator, detect_structural_change
from ..persistence.audit_log import AuditLog
from ..persistence.project_folder import (
    DEFAULT_PACKAGE_PREFIX,
    ENTRY_MODE_CREATE_NEW,
    ENTRY_MODE_ENHANCE_PRODUCTION,
    ENTRY_MODE_ITERATE_CUSTOM,
    ProjectFolder,
    ProjectFolderError,
    ProvenanceRecord,
)
from ..persistence.registry import PluginRegistry, RegistryError
from .operations import DraftOperation
from .repair_loop import Fixer, RepairLoop, RepairOutcome
from .session import (
    ExportOutcome,
    ExportPlan,
    ExportStatus,
    GeneratedArtifact,
    SessionState,
    TurnResult,
    TurnStatus,
)

__all__ = [
    "OrchestratorError",
    "SessionNotFoundError",
    "EntryModeError",
    "RegistryAccessError",
    "TurnPlan",
    "Orchestrator",
]

logger = logging.getLogger(__name__)

#: Directory names never included in the export file tree / preview.
_EXCLUDED_TREE_NAMES = frozenset({".builder", ".git", "__pycache__", ".pytest_cache", ".mypy_cache"})

#: The spec filename written into a materialized build directory.
_SPEC_FILENAME = "plugin.spec.yaml"


class OrchestratorError(Exception):
    """Base class for orchestration failures."""


class SessionNotFoundError(OrchestratorError):
    """Raised when an operation references an unknown session id."""


class EntryModeError(OrchestratorError):
    """Raised when an entry mode is unknown or its required inputs are missing."""


class RegistryAccessError(OrchestratorError):
    """Raised when the Plugin_Registry cannot be read to determine prior versions (Req 12.8).

    Export is aborted without building and the draft's version is left
    unchanged, per Req 12.8.
    """


class TurnPlan:
    """The deterministic outcome of interpreting one conversation turn.

    Interpreting free-form natural language into concrete edits is the job of the
    upstream planner/LLM and is out of scope for the orchestrator; a
    :class:`TurnPlan` is the structured instruction that interpretation produces
    and the orchestrator applies. A plan carries the ordered draft
    :class:`~icplugin_builder.orchestrator.operations.DraftOperation` edits and
    the reasoning :class:`GenerationRequest` artifacts a turn needs, or -- when
    the request was ambiguous -- a ``clarification`` string that leaves the draft
    untouched (Req 1.5, 22.5).
    """

    def __init__(
        self,
        operations: Optional[Sequence[DraftOperation]] = None,
        reasoning: Optional[Sequence[GenerationRequest]] = None,
        *,
        clarification: Optional[str] = None,
    ) -> None:
        """Build a turn plan.

        Args:
            operations: the ordered draft edits to apply.
            reasoning: the reasoning artifacts (action logic / field description /
                help text) to produce across the deterministic/LLM boundary.
            clarification: when set, the turn is ambiguous; no edit is applied and
                this text is surfaced to the user (Req 1.5, 22.5).
        """
        self.operations: List[DraftOperation] = list(operations or [])
        self.reasoning: List[GenerationRequest] = list(reasoning or [])
        self.clarification = clarification

    @property
    def is_ambiguous(self) -> bool:
        """Return ``True`` iff the turn requested clarification."""
        return self.clarification is not None


class Orchestrator:
    """Sequences conversation, generation, validation, build, and export.

    All collaborators are injected. Only the ones a given flow needs must be
    supplied: an in-memory net-new draft needs none of the integration
    collaborators, while an export flow needs the validators, the build engine,
    the export manager, and the registry.
    """

    def __init__(
        self,
        *,
        cost_controller: Optional[CostController] = None,
        llm_generator: Optional[LLMGenerator] = None,
        plugin_agent: Optional[PluginAgent] = None,
        scaffolder: Optional[InsightPluginCli] = None,
        repair_loop: Optional[RepairLoop] = None,
        sdk_readme: Optional[Union[str, Path]] = None,
        template_library: Optional[TemplateLibrary] = None,
        refresh_coordinator: Optional[RefreshCoordinator] = None,
        spec_validator: Optional[SpecValidator] = None,
        code_validator: Optional[CodeValidator] = None,
        build_engine: Optional[BuildEngine] = None,
        export_manager: Optional[ExportManager] = None,
        registry: Optional[PluginRegistry] = None,
        audit_log: Optional[AuditLog] = None,
        source_provider: Optional[Any] = None,
        projects_root: Optional[Union[str, Path]] = None,
    ) -> None:
        """Configure the orchestrator with its collaborators.

        Args:
            cost_controller: gates and records LLM usage; also the source of the
                cumulative session token total (Req 3.6, 4).
            llm_generator: the cost-gated Kiro CLI dispatcher for prose reasoning
                artifacts -- field descriptions and help text (Req 3.2).
            plugin_agent: the delegated Kiro CLI *agent* that implements plugin
                code in place (the API client, connection, action bodies, and
                unit tests). Code implementation is delegated rather than
                prompted for and spliced, so it requires a project folder on
                disk to work in.
            scaffolder: the ``insight-plugin`` CLI wrapper used to scaffold a
                net-new plugin tree deterministically (Req 3.1). Without it a
                net-new draft gets a bare project folder populated by ``refresh``,
                which yields the legacy ``komand_`` package prefix.
            repair_loop: checks the implemented code and delegates repairs until
                progress stops. Without it, findings are neither collected nor
                acted on -- which is the behavior that let unusable plugins
                through.
            sdk_readme: path to the InsightConnect SDK repository's ``README.md``,
                whose changelog is the source of the SDK version stamped into a
                new plugin's spec. ``None`` uses the conventional clone location.
            template_library: templates consulted before dispatching to the LLM;
                defaults to :func:`default_template_library` (Req 3.3).
            refresh_coordinator: runs ``insight-plugin refresh`` after structural
                edits (Req 22.3).
            spec_validator: structural + semver validation (Req 7); defaults to a
                fresh :class:`SpecValidator`.
            code_validator: the four-stage code pipeline (Req 8).
            build_engine: packages a validated project into a ``.plg`` (Req 9);
                defaults to a fresh :class:`BuildEngine`.
            export_manager: local/tenant export (Req 9.3, 10); defaults to a fresh
                :class:`ExportManager`.
            registry: the plugin/export registry (Req 11, 12).
            audit_log: the append-only audit log (Req 18).
            source_provider: the ``Plugin_Source_Provider`` for the enhance entry
                mode (Req 25).
            projects_root: the root under which project folders live; enables
                iterate-mode loading and lazy project-folder creation on export.
        """
        self._cost_controller = cost_controller
        self._llm = llm_generator
        self._agent = plugin_agent
        self._scaffolder = scaffolder
        self._repair_loop = repair_loop
        self._sdk_readme = sdk_readme
        self._template_library = template_library if template_library is not None else default_template_library()
        self._refresh = refresh_coordinator
        self._spec_validator = spec_validator if spec_validator is not None else SpecValidator()
        self._code_validator = code_validator
        self._build_engine = build_engine if build_engine is not None else BuildEngine()
        self._export_manager = export_manager if export_manager is not None else ExportManager()
        self._registry = registry
        self._audit = audit_log
        self._source_provider = source_provider
        self._projects_root = Path(projects_root) if projects_root is not None else None
        self._sessions: Dict[str, SessionState] = {}

    # -- session access -----------------------------------------------------

    def session(self, session_id: str) -> SessionState:
        """Return the :class:`SessionState` for ``session_id`` or raise."""
        state = self._sessions.get(session_id)
        if state is None:
            raise SessionNotFoundError(f"no active session {session_id!r}")
        return state

    def token_total(self, session_id: str) -> int:
        """Return the cumulative session token total (Req 3.6)."""
        if self._cost_controller is None:
            return 0
        return self._cost_controller.session_total(session_id)

    # -- entry-mode routing (Req 24) ----------------------------------------

    def start_session(
        self,
        entry_mode: str,
        *,
        session_id: str,
        user_id: str,
        plugin_name: Optional[str] = None,
        source: Optional[Any] = None,
        production_plugin: Optional[str] = None,
        initial_spec: Optional[PluginSpec] = None,
    ) -> SessionState:
        """Establish the entry mode and load the starting draft (Req 24.1-24.5).

        Net-new begins with an empty (or supplied) draft (Req 24.2); iterate loads
        a previously created plugin from its project folder (Req 24.3); enhance
        read-only-imports a production plugin via the ``Plugin_Source_Provider``
        (Req 24.4). A :class:`ProvenanceRecord` identifying the entry mode is
        recorded for the resulting draft in every mode (Req 24.5).

        Args:
            entry_mode: one of ``ENTRY_MODE_CREATE_NEW``, ``ENTRY_MODE_ITERATE_CUSTOM``,
                or ``ENTRY_MODE_ENHANCE_PRODUCTION``.
            session_id: the session identifier to register.
            user_id: the operator identifier (governs the request rate limit).
            plugin_name: the plugin to load for iterate mode.
            source: the production source (id or config) for enhance mode.
            production_plugin: the plugin name within ``source`` for enhance mode.
            initial_spec: an optional starting spec for net-new mode.

        Returns:
            The registered :class:`SessionState`.

        Raises:
            EntryModeError: if the mode is unknown or its required inputs are
                missing or cannot be loaded.
        """
        if entry_mode == ENTRY_MODE_CREATE_NEW:
            state = self._start_net_new(session_id, user_id, initial_spec)
        elif entry_mode == ENTRY_MODE_ITERATE_CUSTOM:
            state = self._start_iterate(session_id, user_id, plugin_name)
        elif entry_mode == ENTRY_MODE_ENHANCE_PRODUCTION:
            state = self._start_enhance(session_id, user_id, source, production_plugin)
        else:
            raise EntryModeError(f"unknown entry mode: {entry_mode!r}")

        self._sessions[session_id] = state
        return state

    def _start_net_new(self, session_id: str, user_id: str, initial_spec: Optional[PluginSpec]) -> SessionState:
        """Begin a net-new draft from an empty (or supplied) spec (Req 24.2)."""
        draft = Draft(spec=copy.deepcopy(initial_spec) if initial_spec is not None else PluginSpec())
        return SessionState(
            session_id=session_id,
            user_id=user_id,
            entry_mode=ENTRY_MODE_CREATE_NEW,
            provenance=ProvenanceRecord.net_new(_now_iso()),
            draft=draft,
            baseline_spec=copy.deepcopy(draft.spec),
        )

    def _start_iterate(self, session_id: str, user_id: str, plugin_name: Optional[str]) -> SessionState:
        """Load a previously created custom plugin into an editable draft (Req 24.3)."""
        if not plugin_name:
            raise EntryModeError("iterate mode requires a plugin_name")
        if self._projects_root is None:
            raise EntryModeError("iterate mode requires a configured projects_root")
        try:
            folder = ProjectFolder.open(self._projects_root, plugin_name)
            spec = load_plugin_spec(folder.spec_path.read_text(encoding="utf-8"))
        except (ProjectFolderError, OSError, ValueError) as error:
            # Req 21.6: report the specific missing/unreadable content, no partial draft.
            raise EntryModeError(f"could not load plugin {plugin_name!r}: {error}") from error

        draft = Draft(spec=spec, code_files=_read_dir_tree(folder.path))
        return SessionState(
            session_id=session_id,
            user_id=user_id,
            entry_mode=ENTRY_MODE_ITERATE_CUSTOM,
            provenance=ProvenanceRecord(entry_mode=ENTRY_MODE_ITERATE_CUSTOM, created_utc=_now_iso()),
            draft=draft,
            project_folder=folder,
            baseline_spec=copy.deepcopy(spec),
            last_exported_spec=copy.deepcopy(spec),
        )

    def _start_enhance(
        self,
        session_id: str,
        user_id: str,
        source: Optional[Any],
        production_plugin: Optional[str],
    ) -> SessionState:
        """Read-only-import a production plugin into a new fork draft (Req 24.4, 25)."""
        if self._source_provider is None:
            raise EntryModeError("enhance mode requires a configured source_provider")
        if source is None or not production_plugin:
            raise EntryModeError("enhance mode requires a source and a production_plugin")
        try:
            result = self._source_provider.import_plugin(source, production_plugin)
            folder = result.project_folder
            spec = load_plugin_spec(folder.spec_path.read_text(encoding="utf-8"))
        except Exception as error:  # noqa: BLE001 -- surface any import failure as an entry-mode error.
            raise EntryModeError(f"could not import production plugin {production_plugin!r}: {error}") from error

        draft = Draft(spec=spec, code_files=_read_dir_tree(folder.path))
        return SessionState(
            session_id=session_id,
            user_id=user_id,
            entry_mode=ENTRY_MODE_ENHANCE_PRODUCTION,
            provenance=result.provenance,
            draft=draft,
            project_folder=folder,
            baseline_spec=copy.deepcopy(spec),
            private_source_notice=result.private_source_notice,
        )

    # -- conversation turns (Req 1, 3, 15, 22) ------------------------------

    async def submit_message(self, session_id: str, text: str, plan: Optional[TurnPlan] = None) -> TurnResult:
        """Validate a message and apply its interpreted turn plan.

        Enforces the input gate first: empty/whitespace-only or over-long input is
        rejected leaving the draft unchanged (Req 1.1, 1.6). An ambiguous plan
        surfaces clarification without touching the draft (Req 1.5, 22.5).
        Otherwise the plan's operations and reasoning are applied via
        :meth:`apply_turn`.

        Args:
            session_id: the session to act on.
            text: the raw user message.
            plan: the interpreted :class:`TurnPlan`; when ``None`` the message
                cannot be acted on and clarification is requested (interpretation
                is upstream of the orchestrator).

        Returns:
            A :class:`TurnResult` describing the outcome.
        """
        session = self.session(session_id)

        gate = validate_conversation_input(text)
        if not gate.accepted:
            return TurnResult(
                status=TurnStatus.REJECTED_INPUT,
                message=gate.message or "input rejected",
                spec=session.spec,
                token_total=self.token_total(session_id),
            )

        if plan is None or plan.is_ambiguous:
            message = (
                plan.clarification
                if (plan is not None and plan.clarification)
                else (
                    "The request could not be interpreted as a specific plugin change; "
                    "please describe the component or change you want."
                )
            )
            return TurnResult(
                status=TurnStatus.CLARIFICATION,
                message=message,
                spec=session.spec,
                token_total=self.token_total(session_id),
            )

        return await self.apply_turn(session_id, plan)

    async def apply_turn(self, session_id: str, plan: TurnPlan) -> TurnResult:
        """Apply a turn's operations and reasoning, refreshing on structural change.

        The draft operations run against an isolated copy through the atomic apply
        wrapper, so a not-found or invalid operation leaves the draft byte-
        identical (Req 1.7, 15.4). Reasoning artifacts are then produced across
        the deterministic/LLM boundary; a cost block or LLM failure aborts the
        turn without committing (Req 1.7, 3.7, 4.2). On success the new draft is
        committed and, when the spec's structural surface changed, an
        ``insight-plugin refresh`` regenerates the derived files (Req 22.3).

        Args:
            session_id: the session to act on.
            plan: the interpreted turn plan (assumed non-ambiguous).

        Returns:
            A :class:`TurnResult` describing the outcome.
        """
        session = self.session(session_id)

        if plan.is_ambiguous:
            return TurnResult(
                status=TurnStatus.CLARIFICATION,
                message=plan.clarification or "clarification required",
                spec=session.spec,
                token_total=self.token_total(session_id),
            )

        # 1. Apply the draft edits atomically (commit-on-success, Req 1.7).
        try:
            new_draft = atomic_apply(session.draft, lambda draft: _apply_operations(draft, plan.operations))
        except ComponentNotFoundError as error:
            return TurnResult(
                status=TurnStatus.NOT_FOUND,
                message=str(error),
                spec=session.spec,
                token_total=self.token_total(session_id),
            )
        except (DraftError, ValueError) as error:
            return TurnResult(
                status=TurnStatus.FAILED,
                message=str(error),
                spec=session.spec,
                token_total=self.token_total(session_id),
            )

        # 2. Produce the prose reasoning artifacts (field descriptions, help
        #    text). These are pure text and touch no files, so they can run
        #    before the working tree exists. Code implementation is *not* done
        #    here -- it is delegated in step 5, after the scaffold exists.
        prose_requests, code_requests = _partition_reasoning(plan.reasoning)
        try:
            generated = await self._dispatch_reasoning(session, prose_requests)
        except (CostLimitError, LLMGeneratorError) as error:
            # Halt the step; nothing is committed, so the draft is unchanged (Req 1.7, 3.7).
            return TurnResult(
                status=TurnStatus.FAILED,
                message=str(error),
                spec=session.spec,
                token_total=self.token_total(session_id),
            )

        # 3. Detect a structural change and refresh derived files (Req 22.3).
        change = detect_structural_change(session.baseline_spec, new_draft.spec)
        refreshed = False

        # Stamp the resolved SDK version onto the draft itself before anything is
        # written, so the persisted spec, the scaffold, and the refreshed derived
        # files all agree. Stamping only the copy handed to the scaffolder would
        # be undone by the save below.
        if change.is_structural:
            stamped = self._with_resolved_sdk(new_draft.spec)
            if stamped is not new_draft.spec:
                new_draft = Draft(spec=stamped, code_files=dict(new_draft.code_files))

        # Lazily materialize a project folder for net-new sessions on the first
        # structural change, scaffolding it deterministically when possible.
        if change.is_structural and session.project_folder is None and self._projects_root is not None:
            session.project_folder = await self._materialize_project_folder(session, new_draft.spec)

        if change.is_structural and session.project_folder is not None and self._refresh is not None:
            session.project_folder.save(new_draft.spec, generated_files=_as_generated(new_draft.code_files))
            await self._refresh.refresh_if_structural(
                session.baseline_spec, new_draft.spec, session.project_folder.path
            )
            session.baseline_spec = copy.deepcopy(new_draft.spec)
            refreshed = True

        # 4. Commit the new draft and record generated artifacts.
        session.draft = new_draft
        session.generated.extend(generated)

        # 5. Delegate code implementation to the Kiro agent, in the project
        #    folder, now that the scaffold exists. The agent edits the files
        #    itself; nothing is spliced back from its output.
        message = ""
        if code_requests:
            try:
                run = await self._delegate_implementation(session, code_requests)
            except (CostLimitError, LLMGeneratorError) as error:
                # The draft edits are already committed and the scaffold is on
                # disk; only the implementation failed. Report it as a failed
                # turn carrying the current spec so the user can retry the
                # implementation without redoing the structural edit.
                return TurnResult(
                    status=TurnStatus.FAILED,
                    message=str(error),
                    spec=new_draft.spec,
                    refreshed=refreshed,
                    structural_reasons=tuple(change.reasons),
                    token_total=self.token_total(session_id),
                )
            if run is not None:
                _record_credits(session, run)
                generated.append(
                    GeneratedArtifact(
                        kind=ArtifactKind.ACTION_LOGIC,
                        content=run.summary,
                        from_llm=True,
                        tokens=run.tokens,
                        name="implementation",
                    )
                )
                session.generated.append(generated[-1])
                message = run.summary

            # 6. Check the implemented code and repair what is wrong. Reporting
            #    failures without acting on them is what let broken plugins
            #    through, so the loop runs here rather than leaving the findings
            #    for someone to read later.
            repair = await self._repair(session)
            if repair is not None:
                session.repair_outcome = repair
                message = f"{message}\n\n{repair.summary()}".strip() if message else repair.summary()

        return TurnResult(
            status=TurnStatus.APPLIED,
            message=message,
            spec=new_draft.spec,
            generated=tuple(generated),
            refreshed=refreshed,
            structural_reasons=tuple(change.reasons),
            token_total=self.token_total(session_id),
        )

    async def _repair(self, session: SessionState) -> Optional[RepairOutcome]:
        """Check the implemented code and delegate repairs until progress stops.

        The fixer is the same delegated agent that wrote the code: it is given the
        findings, with their file and line, and asked to fix exactly those. The
        decision about whether it succeeded is not its to make -- the loop
        re-runs the check and compares finding keys.

        Returns:
            The :class:`RepairOutcome`, or ``None`` when no repair loop is
            configured or the draft has no working tree to check.
        """
        if self._repair_loop is None or session.project_folder is None:
            return None

        fixer: Optional[Fixer] = self._agent_fixer(session) if self._agent is not None else None

        try:
            return await self._repair_loop.run(session.project_folder.path, fixer)
        except (CostLimitError, LLMGeneratorError) as error:
            # The budget ran out or a repair run failed. The implemented code
            # stands; report how far the repair got rather than failing the turn.
            logger.warning("repair loop stopped early: %s", error)
            return None

    def _agent_fixer(self, session: SessionState) -> Fixer:
        """Return a :data:`Fixer` that asks the delegated agent to fix findings."""
        agent = self._agent
        session_id = session.session_id
        user_id = session.user_id

        async def fix(root: Path, report: QualityReport) -> object:
            assert agent is not None  # guarded by the caller
            run = await agent.implement(
                root,
                _repair_instruction(report),
                session_id=session_id,
                user_id=user_id,
            )
            _record_credits(session, run)
            return run

        return fix

    async def _materialize_project_folder(self, session: SessionState, spec: PluginSpec) -> ProjectFolder:
        """Create the on-disk working tree for a net-new draft.

        Prefers deterministic scaffolding: ``insight-plugin create`` produces the
        full tree -- package directory, action/connection stubs, ``Dockerfile``,
        ``setup.py``, ``bin/``, ``unit_test/`` -- using the **current** ``icon_``
        package prefix. Falling back to creating a bare folder and letting
        ``refresh`` populate it is not equivalent: from an identical spec,
        ``refresh`` against a directory holding only a spec emits the legacy
        ``komand_`` prefix. Every plugin this tool produced before scaffolding was
        wired carried that legacy prefix as a result.

        The order is forced by the CLI: it will not create over an existing
        directory, so the scaffold is produced first and the tool's
        ``.builder/`` metadata is adopted into it afterwards, recording the
        prefix actually observed on disk.

        When the scaffolder is unavailable or fails, this falls back to a bare
        project folder so the turn still completes; a subsequent ``refresh`` will
        populate it. The failure is logged rather than raised because the draft
        edit itself succeeded.
        """
        name = spec.name or session.session_id
        root = Path(self._projects_root) / name  # type: ignore[arg-type]

        if self._scaffolder is not None and not root.exists():
            try:
                tree = await self._scaffolder.create(spec, self._projects_root)
                prefix = tree.package_prefix() or DEFAULT_PACKAGE_PREFIX
                return ProjectFolder.adopt(root, name, spec, package_prefix=prefix)
            except (InsightPluginCliError, ProjectFolderError) as error:
                logger.warning(
                    "deterministic scaffolding failed for %r (%s); "
                    "falling back to a bare project folder populated by refresh",
                    name,
                    error,
                )

        if root.exists():
            return ProjectFolder.adopt(root, name, spec)
        return ProjectFolder.create(self._projects_root, name, spec)

    def _with_resolved_sdk(self, spec: PluginSpec) -> PluginSpec:
        """Stamp the resolved SDK version into ``spec`` when it carries none.

        `insight-plugin validate` requires an ``sdk`` block, and every plugin this
        tool produced before this step shipped without one. The version is read
        from the SDK's own changelog rather than hardcoded, and an ``sdk`` block
        that is already populated is left untouched.

        Resolution failure is not fatal: the spec is returned unchanged and the
        completeness check will report the missing field, which is a better
        outcome than blocking the turn over a missing SDK checkout.
        """
        if _sdk_block_present(spec):
            return spec
        resolved = resolve_sdk_version(self._sdk_readme)
        if not resolved.resolved or resolved.version is None:
            logger.warning("could not resolve an SDK version (%s); leaving sdk unset", resolved.detail)
            return spec
        return with_sdk_version(spec, resolved.version)

    async def _delegate_implementation(
        self,
        session: SessionState,
        requests: Sequence[GenerationRequest],
    ) -> Optional[AgentRunResult]:
        """Delegate plugin code implementation to the Kiro agent (one run per turn).

        The agent is given the project directory and a task naming the components
        to implement; it reads the spec, writes the API client, connection, action
        bodies, and unit tests, and runs the toolchain to verify itself. The
        standing rules and the definition of done live in the agent config, and
        the plugin conventions live in the skills that config references, so the
        instruction assembled here stays a task rather than a restated rulebook.

        Returns:
            The :class:`AgentRunResult`, or ``None`` when delegation could not be
            attempted because no agent or project folder is available.

        Raises:
            CostLimitError: if the Cost_Controller blocks the run.
            PluginAgentError: if the agent run fails; carries the CLI's stderr.
        """
        if self._agent is None or session.project_folder is None:
            return None

        actions = [
            str(request.parameters.get("action"))
            for request in requests
            if request.kind is ArtifactKind.ACTION_LOGIC and request.parameters.get("action")
        ]
        references = _write_reference_files(session)
        return await self._agent.implement(
            session.project_folder.path,
            _implementation_instruction(session.spec, actions, references),
            session_id=session.session_id,
            user_id=session.user_id,
        )

    async def _dispatch_reasoning(
        self,
        session: SessionState,
        requests: Sequence[GenerationRequest],
    ) -> List[GeneratedArtifact]:
        """Classify and produce each *prose* reasoning artifact (Req 3.2, 3.3, 3.4).

        A request that matches a template is rendered deterministically with zero
        LLM calls; otherwise it is dispatched to the cost-gated
        :class:`LLMGenerator`.

        Code artifacts do not come through here. Implementing plugin code means
        writing several interdependent files and running the toolchain to check
        them, which is delegated to the Kiro agent in
        :meth:`_delegate_implementation` rather than requested as a text
        completion per artifact.
        """
        produced: List[GeneratedArtifact] = []
        for request in requests:
            classification = classify_request(request, self._template_library)
            if classification.requires_llm:
                if self._llm is None:
                    raise LLMGeneratorError("an LLM_Generator is required to produce reasoning content")
                scoped = _prose_context(session, request)
                result = await self._llm.generate(
                    classification.kind,
                    scoped,
                    session_id=session.session_id,
                    user_id=session.user_id,
                )
                produced.append(
                    GeneratedArtifact(
                        kind=classification.kind,
                        content=result.content,
                        from_llm=True,
                        tokens=result.tokens,
                        name=request.parameters.get("action") or request.name,
                    )
                )
            else:
                content = classification.template.render(request.parameters) if classification.template else ""
                produced.append(GeneratedArtifact(kind=classification.kind, content=content, from_llm=False, tokens=0))
        return produced

    # -- export sequencing (Req 12, 13, 16, 7, 8) ---------------------------

    async def prepare_export(self, session_id: str) -> ExportPlan:
        """Compute the reviewable export preview without exporting (Req 12, 13, 16, 7, 8).

        Sequences the pre-export decisions in order: read the registry for prior
        versions (aborting on a read failure, Req 12.8); apply the ``_custom``
        vendor suffix (Req 13.3); bump the version relative to prior exports (Req
        12), recording the previous->new display when it changed (Req 12.6);
        validate the spec and, when a code validator and project folder are
        present, the code (Req 7, 8); and compute the export-gating decision (Req
        7.4, 8.6, 8.7). Finally build the preview: the spec, the exact file list
        that would be packaged, and the added/removed/modified diff against the
        prior exported version (Req 16.1-16.4).

        The session draft is **not** mutated here, so declining the preview leaves
        it unchanged (Req 16.6); the vendor-suffixed, version-bumped spec is
        carried on the returned :class:`ExportPlan` and applied only at
        :meth:`confirm_export` time.

        Args:
            session_id: the session to prepare an export for.

        Returns:
            An :class:`ExportPlan` with the gate decision, spec preview, file
            list, diff, and version display.

        Raises:
            RegistryAccessError: if the registry cannot be read (Req 12.8).
        """
        session = self.session(session_id)

        # Req 12.8: read prior versions first; abort (version unchanged) on failure.
        try:
            prior_versions = self._prior_versions(session.plugin_name)
        except RegistryError as error:
            raise RegistryAccessError(
                f"could not read the plugin registry to determine prior versions: {error}"
            ) from error

        # Req 13.3: apply the _custom vendor suffix before build/export begins.
        suffixed = atomic_apply(session.draft, _suffix_vendor)

        # Req 12: bump the version relative to prior exports.
        bump = bump_for_export(suffixed.spec, session.last_exported_spec, prior_versions)
        if bump.changed:
            update = apply_version_bump(suffixed.spec, bump)
            export_spec = update.spec
            version_display = update.display  # Req 12.6 previous -> new
        else:
            export_spec = suffixed.spec
            version_display = ""

        # Req 7, 8: validate the spec and (when possible) the code.
        spec_report: ValidationReport = self._spec_validator.validate(export_spec)
        session.spec_report = spec_report

        # Completeness is reported alongside structural validity: a spec can be
        # well-formed and still be rejected by `insight-plugin validate` for a
        # missing sdk block, version_history, or output examples.
        completeness = check_completeness(export_spec)
        pipeline_report: Optional[PipelineReport] = None
        if self._code_validator is not None and session.project_folder is not None:
            pipeline_report = await self._code_validator.run_pipeline(session.project_folder.path)
            session.pipeline_report = pipeline_report

        # Req 7.4, 8.6, 8.7: the gate permits iff spec valid AND all stages passed.
        decision = decide_export(spec_report, pipeline_report)

        # Req 16.1-16.4: preview file list and prior-version diff.
        file_tree = self._file_tree(session, export_spec)
        file_list = tuple(sorted(file_tree))
        diff = diff_file_trees(session.prior_file_tree, file_tree)

        return ExportPlan(
            decision=decision,
            spec_preview=export_spec,
            file_list=file_list,
            diff=diff,
            version_bump=bump,
            version_display=version_display,
            spec_report=spec_report,
            pipeline_report=pipeline_report,
            completeness=completeness,
        )

    async def confirm_export(
        self,
        session_id: str,
        plan: ExportPlan,
        *,
        confirmed: bool,
        target: str = "local",
        credentials: Optional[TenantCredentials] = None,
        output_dir: Optional[Union[str, Path]] = None,
    ) -> ExportOutcome:
        """Confirm the preview and run the build + export (Req 16.5, 16.6, 9, 10, 18, 19).

        Requires explicit confirmation (Req 16.5): a declined confirmation aborts
        the export, produces no artifact, and leaves the draft unchanged (Req
        16.6). A blocked gate refuses to build (Req 7.4, 8.6). Otherwise the
        vendor-suffixed, version-bumped spec is committed, the project is packaged
        into a ``.plg`` (Req 9), the build is audited (Req 18.2), and the artifact
        is exported locally or uploaded to the tenant (Req 9.3, 10). A successful
        export is recorded in the registry and audit log (Req 10.2, 11.2, 18.6); a
        failed tenant upload retains the artifact for retry, leaves the registry
        unchanged, and classifies the failure (Req 10.3, 19.2, 19.4).

        Args:
            session_id: the session to export.
            plan: the :class:`ExportPlan` from :meth:`prepare_export`.
            confirmed: whether the user confirmed the preview (Req 16.5).
            target: ``"local"`` or ``"tenant"``.
            credentials: the tenant credentials, required for ``target="tenant"``.
            output_dir: a user-accessible directory for a local export's ``.plg``.

        Returns:
            An :class:`ExportOutcome` describing the result.
        """
        session = self.session(session_id)

        # Req 16.5, 16.6: explicit confirmation is required; a decline aborts.
        if not confirmed:
            return ExportOutcome(
                status=ExportStatus.ABORTED,
                message="Export cancelled at preview; the draft is unchanged and no artifact was produced.",
            )

        # Req 7.4, 8.6: a blocked gate refuses to build/export (unless force is set).
        if not plan.permitted and not getattr(plan, "_force", False):
            return ExportOutcome(status=ExportStatus.BLOCKED, message=plan.decision.summary())

        export_spec = plan.spec_preview
        version = str(export_spec.version)
        plugin = export_spec.name

        # Commit the vendor-suffixed, version-bumped spec now the build begins (Req 13.3, 12).
        session.draft = Draft(spec=copy.deepcopy(export_spec), code_files=dict(session.draft.code_files))
        session.baseline_spec = copy.deepcopy(export_spec)

        build_dir, artifact_store = self._build_dir(session, export_spec)

        # Req 9: package the validated project into a single .plg.
        try:
            artifact = self._build_engine.package(
                build_dir,
                validation_passed=True,
                output_dir=output_dir if (target == "local" and output_dir is not None) else None,
                artifact_name=f"{plugin}-{version}.plg",
            )
        except BuildEngineError as error:
            return ExportOutcome(
                status=ExportStatus.BUILD_FAILED,
                message=str(error),
                version=version,
                target=target,
            )

        session.last_artifact = artifact
        self._audit_build(plugin, version)  # Req 18.2

        if target == "local":
            return self._finish_local_export(session, plugin, version, export_spec, artifact, artifact_store)
        return self._finish_tenant_export(session, plugin, version, export_spec, artifact, artifact_store, credentials)

    def _finish_local_export(
        self,
        session: SessionState,
        plugin: str,
        version: str,
        export_spec: PluginSpec,
        artifact: Any,
        artifact_store: Optional[ProjectFolder],
    ) -> ExportOutcome:
        """Record a successful local export in the registry/audit log (Req 9.3, 11.2, 18.6)."""
        self._record_export(plugin, version, export_spec.vendor, target="local")
        self._record_history(session, version, export_spec, target="local", result="success")
        session.last_exported_spec = copy.deepcopy(export_spec)
        session.prior_file_tree = self._file_tree(session, export_spec)
        return ExportOutcome(
            status=ExportStatus.SUCCEEDED,
            message=f"Local export succeeded; artifact written to {artifact.path}.",
            artifact_path=str(artifact.path),
            version=version,
            target="local",
        )

    def _finish_tenant_export(
        self,
        session: SessionState,
        plugin: str,
        version: str,
        export_spec: PluginSpec,
        artifact: Any,
        artifact_store: Optional[ProjectFolder],
        credentials: Optional[TenantCredentials],
    ) -> ExportOutcome:
        """Upload to the tenant and record success/failure (Req 10, 18.3, 18.6, 19.2)."""
        if credentials is None:
            raise OrchestratorError("tenant export requires credentials")

        # Req 18.3: record the credential use (masked) before contacting the tenant.
        if self._audit is not None:
            self._audit.record_credential_use(credentials.api_key, target=credentials.region_base_url)

        result = self._export_manager.export_tenant(artifact, credentials)

        if result.success:
            self._record_export(plugin, version, export_spec.vendor, target=result.region_base_url)
            self._record_history(session, version, export_spec, target=result.region_base_url, result="success")
            session.last_exported_spec = copy.deepcopy(export_spec)
            session.prior_file_tree = self._file_tree(session, export_spec)
            return ExportOutcome(
                status=ExportStatus.SUCCEEDED,
                message=f"Tenant export succeeded to {result.region_base_url}.",
                artifact_path=str(artifact.path),
                version=version,
                target=result.region_base_url,
            )

        # Req 19.2: retain the artifact >=24h; registry left unchanged (no export record).
        retained_path: Optional[str] = None
        if artifact_store is not None:
            try:
                retained = retain_failed_export_artifact(
                    artifact_store, Path(artifact.path).name, Path(artifact.path).read_bytes()
                )
                retained_path = str(retained.path)
            except (OSError, ValueError):  # pragma: no cover - best-effort retention
                retained_path = None

        self._record_history(session, version, export_spec, target=result.region_base_url, result="failed")
        failure = classify_export_failure("tenant upload", result.error or "tenant upload failed")
        return ExportOutcome(
            status=ExportStatus.EXPORT_FAILED,
            message=result.error or "tenant upload failed",
            artifact_path=str(artifact.path),
            version=version,
            target=result.region_base_url,
            failure=failure,
            retained_artifact_path=retained_path,
        )

    # -- registry / audit / history helpers ---------------------------------

    def _prior_versions(self, plugin_name: str) -> List[SemVer]:
        """Return every previously exported version for ``plugin_name`` (Req 12.1).

        Raises:
            RegistryError: if the registry cannot be read (surfaced as Req 12.8).
        """
        if self._registry is None or not plugin_name:
            return []
        versions: List[SemVer] = []
        for record in self._registry.exports(plugin_name):
            try:
                versions.append(SemVer.parse(record.version))
            except (ValueError, AttributeError):
                continue
        return versions

    def _record_export(self, plugin: str, version: str, vendor: str, *, target: str) -> None:
        """Record a plugin creation (upsert) and export in the registry (Req 11.1, 11.2)."""
        if self._registry is None:
            return
        self._registry.record_creation(plugin, vendor, version)
        self._registry.record_export(plugin, version, target=target, result="success")

    def _audit_build(self, plugin: str, version: str) -> None:
        """Record a build event in the audit log (Req 18.2)."""
        if self._audit is not None:
            self._audit.record_build(plugin, version)

    def _record_history(
        self,
        session: SessionState,
        version: str,
        export_spec: PluginSpec,
        *,
        target: str,
        result: str,
    ) -> None:
        """Snapshot the exported version's spec and outcome in the project folder (Req 21.3)."""
        if session.project_folder is None:
            return
        outcome = {
            "target": target,
            "timestamp_utc": _now_iso(),
            "result": result,
        }
        try:
            session.project_folder.record_version(version, export_spec, export_outcome=outcome)
        except ProjectFolderError:  # pragma: no cover - history is best-effort
            pass

    # -- build directory / file tree ----------------------------------------

    def _build_dir(self, session: SessionState, export_spec: PluginSpec) -> tuple:
        """Resolve a directory to package from and its artifact store.

        Prefers the session's :class:`ProjectFolder`, persisting the spec and
        hand-written code into it (Req 21.2). When no project folder exists a new
        one is created under ``projects_root`` if configured, otherwise a
        temporary directory is materialized from the draft so an in-memory net-new
        plugin can still be packaged. Returns ``(directory, artifact_store)`` where
        ``artifact_store`` is the project folder (for failed-export retention) or
        ``None`` for the temporary directory.
        """
        if session.project_folder is not None:
            session.project_folder.save(export_spec, generated_files=_as_generated(session.draft.code_files))
            return session.project_folder.path, session.project_folder

        if self._projects_root is not None and export_spec.name:
            try:
                folder = ProjectFolder.open(self._projects_root, export_spec.name)
            except ProjectFolderError:
                folder = ProjectFolder.create(self._projects_root, export_spec.name, export_spec)
            folder.save(export_spec, generated_files=_as_generated(session.draft.code_files))
            session.project_folder = folder
            return folder.path, folder

        # Materialize a temporary build tree from the draft (no persistent store).
        temp_dir = Path(tempfile.mkdtemp(prefix="icpb-build-"))
        (temp_dir / _SPEC_FILENAME).write_text(dump_plugin_spec(export_spec), encoding="utf-8")
        for path, content in session.draft.code_files.items():
            target = temp_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(str(content), encoding="utf-8")
        return temp_dir, None

    def _file_tree(self, session: SessionState, export_spec: PluginSpec) -> Dict[str, Any]:
        """Return the file tree that would be packaged (for the preview/diff).

        Reads the project folder tree (excluding tool-only ``.builder/`` metadata)
        when one exists; otherwise derives the tree from the in-memory draft's
        hand-written code plus the serialized spec, so a net-new draft still has a
        non-empty preview file list (Req 16.2).
        """
        if session.project_folder is not None and session.project_folder.path.is_dir():
            return _read_dir_tree(session.project_folder.path)

        tree: Dict[str, Any] = {_SPEC_FILENAME: dump_plugin_spec(export_spec)}
        for path, content in session.draft.code_files.items():
            tree[path] = content
        return tree


# --- module helpers --------------------------------------------------------


def _apply_operations(draft: Draft, operations: Sequence[DraftOperation]) -> Draft:
    """Apply each operation in order, returning the final draft.

    Each operation is non-mutating; when run under the atomic apply wrapper a
    failure partway through leaves the caller's draft byte-identical (Req 1.7).
    """
    current = draft
    for operation in operations:
        current = operation.apply(current)
    return current


def _suffix_vendor(draft: Draft) -> Draft:
    """Return a draft whose spec vendor carries the ``_custom`` suffix (Req 13.3)."""
    new_spec = copy.deepcopy(draft.spec)
    new_spec.vendor = apply_custom_vendor_suffix(new_spec.vendor)
    return Draft(spec=new_spec, code_files=dict(draft.code_files))


def _as_generated(code_files: Mapping[str, Any]) -> Dict[str, Any]:
    """Coerce a draft's code-file mapping to the ``generated_files`` shape."""
    return {str(path): content for path, content in code_files.items()}


def _record_credits(session: SessionState, run: Any) -> None:
    """Accumulate a delegated run's reported credits onto the session.

    Credits are the only usage figure the Kiro CLI measures, so they are the
    session's real cost. A run that reported none is skipped rather than counted
    as zero, and ``credits_reported`` records whether any figure was ever seen so
    "nothing spent" stays distinguishable from "spend unknown".
    """
    credits = getattr(run, "credits", None)
    if credits is None:
        return
    session.credits_spent += float(credits)
    session.credits_reported = True


def _sdk_block_present(spec: PluginSpec) -> bool:
    """Return ``True`` iff the spec already carries a populated ``sdk.version``.

    ``sdk`` is not a modeled field, so it is carried verbatim in
    :attr:`PluginSpec.extra`.
    """
    sdk = spec.extra.get("sdk")
    return isinstance(sdk, Mapping) and bool(sdk.get("version"))


def _partition_reasoning(
    requests: Sequence[GenerationRequest],
) -> Tuple[List[GenerationRequest], List[GenerationRequest]]:
    """Split reasoning requests into (prose, code).

    Prose artifacts -- field descriptions and help text -- are single pieces of
    text with no dependency on the working tree, so they are generated directly.
    Code artifacts are delegated to the Kiro agent as one implementation task,
    because writing an API client, a connection, action bodies and their tests
    is a set of interdependent edits that has to be verified by running the
    toolchain, not a set of independent completions.
    """
    prose: List[GenerationRequest] = []
    code: List[GenerationRequest] = []
    for request in requests:
        if request.kind in CODE_ARTIFACT_KINDS:
            code.append(request)
        else:
            prose.append(request)
    return prose, code


def _prose_context(session: SessionState, request: GenerationRequest) -> Dict[str, Any]:
    """Build the scoped context for a prose artifact (field description, help text).

    Scoped on purpose (design "Prompt scoping"): the plugin's identity and its
    connection contract are enough to describe a field or write help prose, and
    the whole project tree is never sent.
    """
    context: Dict[str, Any] = dict(request.parameters)
    spec = session.spec

    context["plugin_name"] = spec.name
    context["plugin_title"] = spec.title
    context["plugin_description"] = spec.description
    if spec.connection:
        context["connection_fields"] = {
            name: {"type": fs.type, "required": fs.required, "description": fs.description}
            for name, fs in spec.connection.items()
        }

    action_name = request.parameters.get("action", "")
    if action_name and action_name in (spec.actions or {}):
        action = spec.actions[action_name]
        context["action_title"] = action.title
        context["action_description"] = action.description

    return context


def _repair_instruction(report: QualityReport) -> str:
    """Build the task for one repair round.

    The findings are named with file and line so the fixer does not have to
    re-derive them, and the instruction is bounded to fixing them. Generated
    files are already excluded upstream, but the constraint is restated because
    the obvious way to silence a finding in ``schema.py`` is to edit it, and the
    fix must not be to edit a file ``insight-plugin refresh`` will overwrite.
    """
    return (
        "The checks below found problems in this plugin's hand-written code.\n"
        "Fix exactly these, then verify by re-running the checks yourself.\n\n"
        f"{report.render()}\n\n"
        "Constraints:\n"
        "- Do not edit generated files (schema.py, __init__.py, setup.py, "
        "Dockerfile, Makefile, help.md, .CHECKSUM, bin/). If a finding appears to "
        "require it, change plugin.spec.yaml and run insight-plugin refresh instead.\n"
        "- Do not silence a finding by deleting the code it refers to unless the "
        "code is genuinely unused.\n"
        "- Keep the plugin's behavior the same; this is a repair pass, not a rewrite.\n"
    )


#: Where user-supplied reference material is staged for the agent. Inside
#: ``.builder/`` so it is tool-owned metadata and never reaches the ``.plg``.
_REFERENCE_DIR = "reference"


def _write_reference_files(session: SessionState) -> Tuple[str, ...]:
    """Stage the session's attachments where the delegated agent can read them.

    An attached OpenAPI spec or vendor API document is written verbatim into the
    project's ``.builder/reference/`` and the agent is pointed at the directory.
    This replaces parsing the spec here and passing an extract along in a prompt:
    the agent reads the real document, so nothing is lost to a summariser, and
    there is no second representation to drift.

    ``.builder/`` is tool-owned and excluded from the packaged artifact, so
    reference material cannot leak into a published plugin.

    Returns:
        The staged filenames, relative to the project root, for naming in the
        instruction. Empty when there is nothing to stage or the write failed --
        a reference file is an aid, not a precondition, so a failure here degrades
        rather than aborting the turn.
    """
    if not session.attachments or session.project_folder is None:
        return ()

    directory = session.project_folder.path / ".builder" / _REFERENCE_DIR
    written: List[str] = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for attachment in session.attachments:
            name = Path(str(attachment.get("name") or "reference")).name
            if not name:
                continue
            content = str(attachment.get("content") or "")
            (directory / name).write_text(content, encoding="utf-8")
            written.append(f".builder/{_REFERENCE_DIR}/{name}")
    except OSError as error:
        logger.warning("could not stage reference files for the agent: %s", error)
        return ()
    return tuple(written)


def _implementation_instruction(
    spec: PluginSpec,
    actions: Sequence[str],
    references: Sequence[str] = (),
) -> str:
    """Build the task handed to the delegated agent.

    Task only. The standing rules and the definition of done live in the agent
    config, and the plugin conventions live in the skills that config loads, so
    nothing here restates them -- a second copy of those rules maintained in this
    codebase would drift from the real ones and then contradict them.

    The spec itself is not inlined: it is on disk as ``plugin.spec.yaml`` in the
    directory the agent is working in, and the agent can read it. Passing a
    serialized copy in the prompt would risk it diverging from the file the
    toolchain actually reads.
    """
    lines = [
        f"Implement the InsightConnect plugin '{spec.name}' in this directory.",
        "",
        "Read plugin.spec.yaml for what to build.",
    ]
    if references:
        listed = "\n".join(f"  {name}" for name in references)
        lines.extend(
            [
                "",
                "Reference material supplied for this plugin -- read it before",
                "implementing, and use it for endpoint paths, HTTP methods, request",
                "and response shapes, authentication, pagination and error formats",
                "rather than inferring them:",
                listed,
            ]
        )
    if actions:
        listed = ", ".join(sorted(set(actions)))
        lines.extend(
            [
                "",
                f"Actions to implement in this pass: {listed}.",
                "Also implement whatever shared code they need (API client, connection,",
                "constants) and unit tests covering them.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Implement the connection, the API client, every action defined in the",
                "spec, and unit tests covering them.",
            ]
        )
    return "\n".join(lines) + "\n"


def _read_dir_tree(root: Union[str, Path]) -> Dict[str, Any]:
    """Read every file under ``root`` into a POSIX-relative path -> content map.

    UTF-8-decodable files are stored as ``str`` and everything else as ``bytes``
    so binary resources compare intact. VCS/build noise and the tool-only
    ``.builder/`` subtree are skipped at any depth so they never appear in the
    export preview.
    """
    base = Path(root)
    tree: Dict[str, Any] = {}
    if not base.is_dir():
        return tree
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(base)
        if any(part in _EXCLUDED_TREE_NAMES for part in relative.parts):
            continue
        raw = path.read_bytes()
        try:
            tree[relative.as_posix()] = raw.decode("utf-8")
        except UnicodeDecodeError:
            tree[relative.as_posix()] = raw
    return tree


def _now_iso() -> str:
    """Return the current time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat()
