"""Unit tests for export outcome recording (task 16.4; Req 10.2, 10.3, 19.2).

These cover the durable side-effects of a finished tenant-export attempt as
performed by
:func:`icplugin_builder.integrations.export_outcome.record_export_outcome`:

* a successful upload records the export in the ``Plugin_Registry`` with the
  target region base URL and the upload timestamp (Req 10.2) and appends a
  success entry to the ``Audit_Log`` (Req 18.6); and
* a failed/timed-out upload leaves the ``Plugin_Registry`` unchanged, appends a
  failed-attempt entry to the ``Audit_Log`` (Req 10.3), and retains the built
  ``.plg`` for at least 24 hours (Req 19.2).

Real collaborators are used (an in-memory registry and a file-backed audit log)
with a small in-memory artifact store; no network is contacted.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from icplugin_builder.integrations.build_export_failure import MINIMUM_EXPORT_RETENTION
from icplugin_builder.integrations.export_manager import ExportResult
from icplugin_builder.integrations.export_outcome import record_export_outcome
from icplugin_builder.persistence.audit_log import AuditEvent, AuditLog
from icplugin_builder.persistence.registry import PluginRegistry

PLUGIN = "okta"
VERSION = "1.2.3"
REGION = "https://us.api.insight.rapid7.com"
UPLOAD_TS = "2024-01-02T03:04:05+00:00"


class FakeArtifactStore:
    """A minimal :class:`ArtifactStore` that writes under a temp directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.saved: dict = {}

    def save_artifact(self, filename: str, content: bytes) -> Path:
        path = self.root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.saved[filename] = content
        return path


@pytest.fixture
def registry():
    reg = PluginRegistry(":memory:")
    reg.record_creation(PLUGIN, "rapid7_custom", VERSION)
    try:
        yield reg
    finally:
        reg.close()


@pytest.fixture
def audit_log(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


def _success_result(artifact_path: Path) -> ExportResult:
    return ExportResult(
        success=True,
        region_base_url=REGION,
        artifact_path=artifact_path,
        uploaded_utc=UPLOAD_TS,
        status_code=201,
    )


def _failure_result(artifact_path: Path, *, timed_out: bool = False, error: str = "boom") -> ExportResult:
    return ExportResult(
        success=False,
        region_base_url=REGION,
        artifact_path=artifact_path,
        uploaded_utc=UPLOAD_TS,
        status_code=None if timed_out else 500,
        timed_out=timed_out,
        error=error,
    )


class TestSuccessRecording:
    def test_records_export_in_registry_with_region_and_timestamp(self, registry, audit_log, tmp_path):
        artifact = tmp_path / "okta-1.2.3.plg"
        artifact.write_bytes(b"plg")

        outcome = record_export_outcome(
            _success_result(artifact),
            plugin_name=PLUGIN,
            version=VERSION,
            registry=registry,
            audit_log=audit_log,
        )

        assert outcome.success is True
        assert outcome.registry_updated is True
        exports = registry.exports(PLUGIN)
        assert len(exports) == 1
        assert exports[0].target == REGION
        assert exports[0].export_utc == UPLOAD_TS
        assert exports[0].result == "success"
        assert exports[0].version == VERSION

    def test_appends_success_entry_to_audit_log(self, registry, audit_log, tmp_path):
        artifact = tmp_path / "okta-1.2.3.plg"
        artifact.write_bytes(b"plg")

        outcome = record_export_outcome(
            _success_result(artifact),
            plugin_name=PLUGIN,
            version=VERSION,
            registry=registry,
            audit_log=audit_log,
        )

        records = audit_log.records()
        assert len(records) == 1
        assert records[0].event == AuditEvent.EXPORT
        assert records[0].plugin_name == PLUGIN
        assert records[0].version == VERSION
        assert records[0].target == REGION
        # A successful export carries no failure reason.
        assert records[0].reason is None
        assert outcome.audit_record.reason is None
        assert outcome.retained_artifact is None

    def test_success_does_not_retain_an_artifact(self, registry, audit_log, tmp_path):
        artifact = tmp_path / "okta-1.2.3.plg"
        artifact.write_bytes(b"plg")
        store = FakeArtifactStore(tmp_path / "retained")

        outcome = record_export_outcome(
            _success_result(artifact),
            plugin_name=PLUGIN,
            version=VERSION,
            registry=registry,
            audit_log=audit_log,
            artifact_store=store,
        )

        assert outcome.retained_artifact is None
        assert store.saved == {}


class TestFailureRecording:
    def test_leaves_registry_unchanged(self, registry, audit_log, tmp_path):
        artifact = tmp_path / "okta-1.2.3.plg"
        artifact.write_bytes(b"plg")

        outcome = record_export_outcome(
            _failure_result(artifact),
            plugin_name=PLUGIN,
            version=VERSION,
            registry=registry,
            audit_log=audit_log,
        )

        assert outcome.success is False
        assert outcome.registry_updated is False
        assert outcome.export_record is None
        assert registry.exports(PLUGIN) == []

    def test_appends_failed_attempt_entry_to_audit_log(self, registry, audit_log, tmp_path):
        artifact = tmp_path / "okta-1.2.3.plg"
        artifact.write_bytes(b"plg")

        record_export_outcome(
            _failure_result(artifact, error="tenant upload failed with status 500"),
            plugin_name=PLUGIN,
            version=VERSION,
            registry=registry,
            audit_log=audit_log,
        )

        records = audit_log.records()
        assert len(records) == 1
        assert records[0].event == AuditEvent.EXPORT
        assert records[0].plugin_name == PLUGIN
        assert records[0].target == REGION
        # A failed attempt is distinguished by carrying a failure reason.
        assert records[0].reason == "tenant upload failed with status 500"

    def test_timeout_is_recorded_as_a_failed_attempt(self, registry, audit_log, tmp_path):
        artifact = tmp_path / "okta-1.2.3.plg"
        artifact.write_bytes(b"plg")

        outcome = record_export_outcome(
            _failure_result(artifact, timed_out=True, error="tenant upload timed out after 60s"),
            plugin_name=PLUGIN,
            version=VERSION,
            registry=registry,
            audit_log=audit_log,
        )

        assert outcome.registry_updated is False
        assert registry.exports(PLUGIN) == []
        assert "timed out" in outcome.audit_record.reason

    def test_retains_artifact_for_at_least_24h(self, registry, audit_log, tmp_path):
        artifact = tmp_path / "okta-1.2.3.plg"
        artifact.write_bytes(b"the-built-plg")
        store = FakeArtifactStore(tmp_path / "retained")
        retained_at = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

        outcome = record_export_outcome(
            _failure_result(artifact),
            plugin_name=PLUGIN,
            version=VERSION,
            registry=registry,
            audit_log=audit_log,
            artifact_store=store,
            recorded_utc=retained_at,
        )

        retained = outcome.retained_artifact
        assert retained is not None
        assert retained.filename == "okta-1.2.3.plg"
        assert store.saved["okta-1.2.3.plg"] == b"the-built-plg"
        # Retention is guaranteed for the full >=24h window.
        assert retained.retain_until - retained.retained_at >= MINIMUM_EXPORT_RETENTION
        assert retained.is_guaranteed_retained_at(retained_at + timedelta(hours=23, minutes=59))
        assert not retained.is_guaranteed_retained_at(retained_at + timedelta(hours=24, minutes=1))

    def test_retains_bytes_read_from_result_artifact_path(self, registry, audit_log, tmp_path):
        artifact = tmp_path / "okta-1.2.3.plg"
        artifact.write_bytes(b"bytes-on-disk")
        store = FakeArtifactStore(tmp_path / "retained")

        outcome = record_export_outcome(
            _failure_result(artifact),
            plugin_name=PLUGIN,
            version=VERSION,
            registry=registry,
            audit_log=audit_log,
            artifact_store=store,
        )

        assert outcome.retained_artifact is not None
        assert store.saved["okta-1.2.3.plg"] == b"bytes-on-disk"

    def test_no_artifact_store_means_no_retention_but_still_audits(self, registry, audit_log, tmp_path):
        artifact = tmp_path / "okta-1.2.3.plg"
        artifact.write_bytes(b"plg")

        outcome = record_export_outcome(
            _failure_result(artifact),
            plugin_name=PLUGIN,
            version=VERSION,
            registry=registry,
            audit_log=audit_log,
        )

        assert outcome.retained_artifact is None
        assert len(audit_log.records()) == 1

    def test_missing_artifact_file_skips_retention_without_raising(self, registry, audit_log, tmp_path):
        missing = tmp_path / "never-built.plg"
        store = FakeArtifactStore(tmp_path / "retained")

        outcome = record_export_outcome(
            _failure_result(missing),
            plugin_name=PLUGIN,
            version=VERSION,
            registry=registry,
            audit_log=audit_log,
            artifact_store=store,
        )

        assert outcome.retained_artifact is None
        assert store.saved == {}
        assert len(audit_log.records()) == 1
