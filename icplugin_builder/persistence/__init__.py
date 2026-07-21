"""Persistence layer.

Plugin registry (SQLite), encrypted credential store (Fernet), append-only
hash-chained audit log, and per-plugin project folders with version history.
"""

from icplugin_builder.persistence.audit_log import (
    GENESIS_HASH,
    RETENTION_MIN_DAYS,
    AuditEvent,
    AuditLog,
    AuditLogError,
    AuditRecord,
    VerificationResult,
)
from icplugin_builder.persistence.project_folder import (
    ProjectFolder,
    ProjectFolderError,
    ProjectListing,
    ProjectMetadata,
    ProvenanceRecord,
    ToolingStamp,
    list_projects,
)
from icplugin_builder.persistence.registry import (
    ExportRecord,
    HistoryEntry,
    PluginRecord,
    PluginRegistry,
    RegistryError,
)

__all__ = [
    "ProjectFolder",
    "ProjectFolderError",
    "ProjectListing",
    "ProjectMetadata",
    "ProvenanceRecord",
    "ToolingStamp",
    "list_projects",
    "AuditEvent",
    "AuditLog",
    "AuditLogError",
    "AuditRecord",
    "VerificationResult",
    "GENESIS_HASH",
    "RETENTION_MIN_DAYS",
    "ExportRecord",
    "HistoryEntry",
    "PluginRecord",
    "PluginRegistry",
    "RegistryError",
]
