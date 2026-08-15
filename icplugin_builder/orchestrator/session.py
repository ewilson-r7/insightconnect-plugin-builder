"""Session state and turn/export result types for the Orchestrator (task 20.3).

The :class:`SessionState` is the orchestrator's single mutable holder for one
conversation session: the entry mode and its :class:`ProvenanceRecord`, the
in-session :class:`Draft` (the current ``Plugin_Spec`` plus its hand-written
code), the on-disk :class:`ProjectFolder` (when the draft is backed by one), and
the bookkeeping the export sequencing needs -- the last-refreshed structural
baseline, the most recently exported spec and file tree, and the latest
validation reports and built artifact.

The result dataclasses (:class:`TurnResult`, :class:`GeneratedArtifact`,
:class:`ExportPlan`, :class:`ExportOutcome`) are the values the orchestrator
returns to the presentation layer. They are deliberately plain, immutable
snapshots so the API/UI can render them without reaching back into live session
state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from ..core.diff import FileTreeDiff
from ..core.draft import Draft
from ..core.generation import ArtifactKind
from ..core.spec_completeness import CompletenessReport
from ..core.spec_model import PluginSpec
from ..core.spec_validator import ValidationReport
from ..core.version_bump import VersionBump
from ..integrations.build_engine import PlgArtifact
from ..integrations.code_validator import PipelineReport
from ..integrations.definition_of_done import DoneReport
from ..integrations.export_gate import ExportDecision
from ..integrations.quality_gate import QualityReport
from ..persistence.project_folder import ProjectFolder, ProvenanceRecord

__all__ = [
    "TurnStatus",
    "GeneratedArtifact",
    "TurnResult",
    "ExportPlan",
    "ExportStatus",
    "ExportOutcome",
    "SessionState",
]


class TurnStatus(str, Enum):
    """The outcome category of a conversation turn."""

    #: The turn's operations and reasoning were applied to the draft.
    APPLIED = "applied"
    #: The turn was rejected by the input gate (empty/whitespace or too long).
    REJECTED_INPUT = "rejected_input"
    #: The turn targeted a component that does not exist (Req 15.4).
    NOT_FOUND = "not_found"
    #: The turn was ambiguous and clarification is requested (Req 1.5, 22.5).
    CLARIFICATION = "clarification"
    #: A generation step failed; the prior draft is preserved (Req 1.7).
    FAILED = "failed"


class ExportStatus(str, Enum):
    """The outcome category of an export attempt."""

    #: Local ``.plg`` produced / tenant upload succeeded.
    SUCCEEDED = "succeeded"
    #: The user declined the preview confirmation (Req 16.6).
    ABORTED = "aborted"
    #: Export blocked by the validation conjunction gate (Req 7.4, 8.6).
    BLOCKED = "blocked"
    #: Packaging (build) failed (Req 19.4).
    BUILD_FAILED = "build_failed"
    #: The tenant upload failed or timed out (Req 19.2, 19.4).
    EXPORT_FAILED = "export_failed"


@dataclass(frozen=True)
class GeneratedArtifact:
    """One artifact produced during a turn across the deterministic/LLM boundary.

    Attributes:
        kind: the reasoning :class:`ArtifactKind` requested.
        content: the produced text (template render or LLM completion).
        from_llm: ``True`` when the Kiro CLI produced it; ``False`` when it was
            rendered deterministically from a template (Req 3.3).
        tokens: tokens consumed (``0`` for a deterministic render).
        name: optional name tag (e.g. the action name for action_logic artifacts).
    """

    kind: ArtifactKind
    content: str
    from_llm: bool
    tokens: int = 0
    name: Optional[str] = None


@dataclass(frozen=True)
class TurnResult:
    """The result of submitting one conversation turn.

    Attributes:
        status: the :class:`TurnStatus` category.
        message: a user-facing message (rejection reason, clarification text, or
            a short confirmation); empty when not applicable.
        spec: the current draft spec after the turn (unchanged from before the
            turn for any non-:attr:`TurnStatus.APPLIED` result) (Req 1.4).
        generated: the artifacts produced across the deterministic/LLM boundary.
        refreshed: ``True`` iff an ``insight-plugin refresh`` ran after a
            structural spec change this turn (Req 22.3).
        structural_reasons: human-readable reasons a refresh was triggered.
        token_total: the cumulative session token total after the turn (Req 3.6).
    """

    status: TurnStatus
    message: str = ""
    spec: Optional[PluginSpec] = None
    generated: Tuple[GeneratedArtifact, ...] = ()
    refreshed: bool = False
    structural_reasons: Tuple[str, ...] = ()
    token_total: int = 0

    @property
    def applied(self) -> bool:
        """Return ``True`` iff the turn mutated the draft."""
        return self.status is TurnStatus.APPLIED

    @property
    def needs_clarification(self) -> bool:
        """Return ``True`` iff the turn requested clarification (Req 1.5, 22.5)."""
        return self.status is TurnStatus.CLARIFICATION


@dataclass(frozen=True)
class ExportPlan:
    """The reviewable preview computed before an export is confirmed (Req 12, 16).

    Attributes:
        decision: the export-gating decision -- permitted iff the spec is valid
            and all four code stages passed (Req 7.4, 8.6, 8.7).
        spec_preview: the vendor-suffixed, version-bumped spec that would be
            exported (Req 16.1).
        file_list: the exact files that would be included in the ``.plg`` (Req 16.2).
        diff: the added/removed/modified partition versus the prior exported
            version, or a first-version diff when none exists (Req 16.3, 16.4).
        version_bump: the version-bump decision (Req 12).
        version_display: ``"<previous> -> <new>"`` shown before the build when the
            version changed; empty when unchanged (Req 12.6).
        spec_report: the spec-validation report backing ``decision``.
        pipeline_report: the code-validation report backing ``decision``.
        completeness: the spec-completeness report -- the fields and conventions
            ``insight-plugin validate`` requires, which are checked separately
            from structural validity because a well-formed spec can still be
            rejected for a missing ``sdk`` block or output examples.
        quality_report: the located findings against the hand-written code,
            checked on the export path so a draft that was never implemented in
            this session cannot reach the preview unexamined (Req 26.1).
        done_report: whether the plugin meets every definition-of-done condition
            (Req 27.1). Reported alongside :attr:`decision` rather than folded
            into it: the export gate is the four-stage conjunction by definition
            (Req 8.7, design "Property 17"), while this answers the different and
            larger question of whether the plugin is finished.
    """

    decision: ExportDecision
    spec_preview: PluginSpec
    file_list: Tuple[str, ...]
    diff: FileTreeDiff
    version_bump: VersionBump
    version_display: str = ""
    spec_report: Optional[ValidationReport] = None
    pipeline_report: Optional[PipelineReport] = None
    completeness: Optional[CompletenessReport] = None
    quality_report: Optional[QualityReport] = None
    done_report: Optional[DoneReport] = None

    @property
    def permitted(self) -> bool:
        """Return ``True`` iff export is permitted by the gate."""
        return self.decision.permitted

    @property
    def plugin_is_done(self) -> Optional[bool]:
        """Whether the plugin meets every definition-of-done condition.

        ``None`` when the definition of done was not evaluated, which is distinct
        from ``False``: the first means nothing was checked, the second that
        something specific is outstanding.
        """
        return None if self.done_report is None else self.done_report.complete

    def summary(self) -> str:
        """Return the preview's headline: can this be exported, and is it finished.

        Two separate answers, and both are said out loud. "Export permitted"
        speaks only for the four stages the gate weighs, so on its own it would
        let a plugin with no API client and a stubbed connection test read as
        ready (Req 27.3).
        """
        lines = [self.decision.summary()]
        if self.done_report is not None and not self.done_report.complete:
            lines.append(self.done_report.summary())
        return " ".join(lines)


@dataclass(frozen=True)
class ExportOutcome:
    """The result of confirming and running an export.

    Attributes:
        status: the :class:`ExportStatus` category.
        message: a user-facing summary.
        artifact_path: the produced ``.plg`` path on success (local or the
            uploaded artifact), else ``None``.
        version: the version the plugin was exported at.
        target: ``"local"`` or the tenant region base URL.
        failure: the classified build/export failure indication, when failed.
        retained_artifact_path: a failed tenant export's retained ``.plg`` path
            (kept >=24h for retry), when applicable (Req 19.2).
    """

    status: ExportStatus
    message: str = ""
    artifact_path: Optional[str] = None
    version: Optional[str] = None
    target: Optional[str] = None
    failure: Optional[Any] = None
    retained_artifact_path: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        """Return ``True`` iff the export completed successfully."""
        return self.status is ExportStatus.SUCCEEDED


@dataclass
class SessionState:
    """The orchestrator's mutable holder for one conversation session.

    Attributes:
        session_id: the session identifier (governs the token budget/total).
        user_id: the user identifier (governs the request rate limit).
        entry_mode: one of the ``ENTRY_MODE_*`` constants.
        provenance: the :class:`ProvenanceRecord` recorded for the draft (Req 24.5).
        draft: the in-session :class:`Draft` (current spec + hand-written code).
        project_folder: the on-disk :class:`ProjectFolder`, or ``None`` for an
            in-memory net-new draft not yet persisted.
        baseline_spec: the last spec whose derived files are on disk/refreshed;
            used to detect structural changes needing a refresh (Req 22.3).
        last_exported_spec: the most recently exported spec, for breaking-change
            classification and version bumping (Req 12.1, 12.2).
        prior_file_tree: the file tree of the most recently exported version, for
            the export diff; ``None`` marks a first version (Req 16.3, 16.4).
        spec_report / pipeline_report: the latest validation reports (Req 7, 8).
        last_artifact: the most recently built ``.plg`` artifact, if any.
        private_source_notice: the usage-restriction notice when the draft was
            forked from the private production repository (Req 25.6).
        repair_outcome: the most recent
            :class:`~icplugin_builder.orchestrator.repair_loop.RepairOutcome`.
            Typed loosely to keep this module free of an import cycle; the
            orchestrator owns the concrete type.
        done_report: the most recent
            :class:`~icplugin_builder.integrations.definition_of_done.DoneReport`.
            This is the one place that answers "is this plugin finished";
            ``repair_outcome`` only says how far the repair loop got, which is a
            different question.
        credits_spent: cumulative credits reported by the delegated agent across
            every run in this session -- implementation and each repair round.
            Credits are the only usage figure the Kiro CLI measures, so this is
            the session's real cost; the token total is an estimate.
        credits_reported: whether any run actually reported a credits figure.
            Distinguishes "nothing spent yet" from "spend unknown", which a bare
            ``0.0`` cannot.
        attachments: reference files the user supplied (an OpenAPI spec, vendor
            API documentation), as ``{"name": ..., "content": ...}``. Written into
            the project's ``.builder/reference/`` before implementation so the
            delegated agent can read them directly, rather than being parsed here
            and passed along as a lossy summary.
    """

    session_id: str
    user_id: str
    entry_mode: str
    provenance: ProvenanceRecord
    draft: Draft
    project_folder: Optional[ProjectFolder] = None
    baseline_spec: Optional[PluginSpec] = None
    last_exported_spec: Optional[PluginSpec] = None
    prior_file_tree: Optional[Dict[str, Any]] = None
    spec_report: Optional[ValidationReport] = None
    pipeline_report: Optional[PipelineReport] = None
    last_artifact: Optional[PlgArtifact] = None
    private_source_notice: Optional[str] = None
    generated: List[GeneratedArtifact] = field(default_factory=list)
    repair_outcome: Optional[Any] = None
    done_report: Optional[DoneReport] = None
    credits_spent: float = 0.0
    credits_reported: bool = False
    attachments: List[Dict[str, str]] = field(default_factory=list)

    @property
    def plugin_name(self) -> str:
        """The current plugin name (from the draft spec)."""
        return self.draft.spec.name

    @property
    def spec(self) -> PluginSpec:
        """The current draft spec."""
        return self.draft.spec
