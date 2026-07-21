"""Orchestration layer.

Sequences entry-mode routing, draft management, the deterministic/LLM
generation boundary, refresh-after-structural-edit, validate-before-export,
version-bump-before-build, preview/diff/confirm, and audit emission.

The :class:`~icplugin_builder.orchestrator.orchestrator.Orchestrator` is the
single component that mutates the in-session draft and sequences side effects,
so the workflow invariants live in one place. See its module docstring for the
full set of ordering guarantees it enforces.
"""

from .operations import (
    AddComponent,
    DraftOperation,
    ModifyComponent,
    RemoveComponent,
    SetConnection,
    UpdateMetadata,
)
from .orchestrator import (
    EntryModeError,
    Orchestrator,
    OrchestratorError,
    RegistryAccessError,
    SessionNotFoundError,
    TurnPlan,
)
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
    "Orchestrator",
    "OrchestratorError",
    "SessionNotFoundError",
    "EntryModeError",
    "RegistryAccessError",
    "TurnPlan",
    "DraftOperation",
    "AddComponent",
    "ModifyComponent",
    "RemoveComponent",
    "SetConnection",
    "UpdateMetadata",
    "SessionState",
    "TurnResult",
    "TurnStatus",
    "GeneratedArtifact",
    "ExportPlan",
    "ExportStatus",
    "ExportOutcome",
]
