"""Record the outcome of a tenant export in the registry/audit log (task 16.4).

This coordinates the persistence side-effects of a tenant export once the
:class:`~icplugin_builder.integrations.export_manager.ExportManager` has produced
an :class:`~icplugin_builder.integrations.export_manager.ExportResult`
(design "Export_Manager"; Flow 4). It is the single place that turns "the upload
attempt finished" into durable records, so the success and failure paths stay
symmetric and testable:

* **On a successful upload** (Req 10.2) the export is recorded in the
  ``Plugin_Registry`` with the target tenant region base URL and the upload
  timestamp, and a matching success entry is appended to the ``Audit_Log``
  (Req 18.6).
* **On a failure or timeout** (Req 10.3) a failed-attempt entry is appended to
  the ``Audit_Log``, the ``Plugin_Registry`` is left **unchanged** (no export
  row is written), and the already-built ``.plg`` is retained for at least 24
  hours so the user can retry the export or download the artifact (Req 19.2).

The result classification (success vs failure/timeout) is read straight off the
:class:`ExportResult`; this function performs no network I/O. Every collaborator
(registry, audit log, artifact store) is injected so the outcome recording is
fully deterministic under test.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from icplugin_builder.integrations.build_export_failure import (
    ArtifactStore,
    RetainedArtifact,
    retain_failed_export_artifact,
)
from icplugin_builder.integrations.export_manager import ExportResult
from icplugin_builder.persistence.audit_log import AuditLog, AuditRecord
from icplugin_builder.persistence.project_folder import TimestampInput
from icplugin_builder.persistence.registry import ExportRecord, PluginRegistry

__all__ = [
    "ExportOutcome",
    "record_export_outcome",
    "REGISTRY_RESULT_SUCCESS",
]

#: The ``result`` value written to the registry for a recorded successful export.
REGISTRY_RESULT_SUCCESS = "success"


@dataclass(frozen=True)
class ExportOutcome:
    """What was recorded for a single tenant-export attempt (Req 10.2, 10.3).

    Attributes:
        success: ``True`` iff the upload succeeded; mirrors
            :attr:`ExportResult.success`.
        export_record: The registry export row written on success; ``None`` on a
            failure/timeout (the registry is left unchanged, Req 10.3).
        audit_record: The audit-log record appended for the attempt -- a success
            export record on success, or a failed-attempt record on failure. An
            attempt always produces exactly one audit record.
        retained_artifact: The failed export's retained ``.plg`` and its
            guaranteed >=24h retention window (Req 19.2); ``None`` on success or
            when no artifact store was supplied to retain into.
    """

    success: bool
    audit_record: AuditRecord
    export_record: Optional[ExportRecord] = None
    retained_artifact: Optional[RetainedArtifact] = None

    @property
    def registry_updated(self) -> bool:
        """Return ``True`` iff an export row was written to the registry."""
        return self.export_record is not None


def record_export_outcome(
    result: ExportResult,
    *,
    plugin_name: str,
    version: str,
    registry: PluginRegistry,
    audit_log: AuditLog,
    artifact_store: Optional[ArtifactStore] = None,
    artifact_content: Optional[bytes] = None,
    artifact_filename: Optional[str] = None,
    recorded_utc: TimestampInput = None,
) -> ExportOutcome:
    """Record the outcome of a tenant export attempt (Req 10.2, 10.3, 19.2).

    Reads the success/failure classification off ``result`` and performs the
    matching durable side-effects:

    * **Success** (``result.success``): records the export in ``registry`` with
      the target region base URL and the upload timestamp (Req 10.2), and
      appends a success export record to ``audit_log`` (Req 18.6).
    * **Failure/timeout** (``result.failed``): appends a failed-attempt record to
      ``audit_log`` (Req 10.3), leaves ``registry`` unchanged, and -- when an
      ``artifact_store`` is supplied -- retains the built ``.plg`` for >=24 hours
      for retry (Req 19.2).

    Args:
        result: The :class:`ExportResult` from
            :meth:`ExportManager.export_tenant`.
        plugin_name: The plugin the export belongs to; it must already have a
            creation record in ``registry`` for the success path to record an
            export against it.
        version: The exported plugin version.
        registry: The :class:`PluginRegistry` to record a successful export in.
        audit_log: The :class:`AuditLog` to append the success or failed-attempt
            record to.
        artifact_store: Optional store used to retain the built ``.plg`` on
            failure (Req 19.2); when omitted, no artifact is retained.
        artifact_content: Optional built ``.plg`` bytes to retain on failure;
            when omitted, the bytes are read from ``result.artifact_path``.
        artifact_filename: Optional retained-artifact filename; defaults to
            ``<plugin_name>-<version>.plg``.
        recorded_utc: Optional retention start instant for the retained artifact;
            defaults to the current UTC time.

    Returns:
        An :class:`ExportOutcome` describing exactly what was recorded.
    """
    region = result.region_base_url
    if result.success:
        export_record = registry.record_export(
            plugin_name,
            version,
            region,
            export_utc=result.uploaded_utc,
            result=REGISTRY_RESULT_SUCCESS,
        )
        audit_record = audit_log.record_export(plugin_name, version, target=region)
        return ExportOutcome(
            success=True,
            audit_record=audit_record,
            export_record=export_record,
            retained_artifact=None,
        )

    # Failure/timeout: leave the registry unchanged, audit the failed attempt,
    # and retain the built artifact for retry (Req 10.3, 19.2).
    audit_record = audit_log.record_export_failure(
        plugin_name,
        version,
        reason=result.error or "tenant export failed",
        target=region,
    )
    retained = _retain_failed_artifact(
        result,
        plugin_name=plugin_name,
        version=version,
        artifact_store=artifact_store,
        artifact_content=artifact_content,
        artifact_filename=artifact_filename,
        recorded_utc=recorded_utc,
    )
    return ExportOutcome(
        success=False,
        audit_record=audit_record,
        export_record=None,
        retained_artifact=retained,
    )


def _retain_failed_artifact(
    result: ExportResult,
    *,
    plugin_name: str,
    version: str,
    artifact_store: Optional[ArtifactStore],
    artifact_content: Optional[bytes],
    artifact_filename: Optional[str],
    recorded_utc: TimestampInput,
) -> Optional[RetainedArtifact]:
    """Retain the failed export's ``.plg`` for >=24h, or return ``None`` if it can't.

    Retention needs both somewhere to store the artifact (``artifact_store``) and
    the artifact bytes. The bytes are taken from ``artifact_content`` when given,
    otherwise read from ``result.artifact_path`` (which still exists after a
    failed upload). When no store is supplied, or the bytes cannot be obtained,
    nothing is retained and ``None`` is returned rather than raising -- recording
    the failed attempt in the audit log is the primary obligation (Req 10.3).
    """
    if artifact_store is None:
        return None

    content = artifact_content
    if content is None:
        source = Path(result.artifact_path)
        if not source.is_file():
            return None
        content = source.read_bytes()

    filename = artifact_filename or f"{plugin_name}-{version}.plg"
    return retain_failed_export_artifact(
        artifact_store,
        filename,
        content,
        retained_utc=recorded_utc,
    )
