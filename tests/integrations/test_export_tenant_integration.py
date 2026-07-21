"""Integration test for the wired tenant-export flow (task 16.6; Req 10.1, 10.2, 10.3).

Unlike the unit tests in ``test_export_manager.py`` (which exercise
:class:`ExportManager` in isolation) and ``test_export_outcome.py`` (which feed a
hand-built :class:`ExportResult` into
:func:`record_export_outcome`), this test wires the two together end to end:

    real .plg  ->  ExportManager.export_tenant (fake uploader)  ->  ExportResult
              ->  record_export_outcome (real registry + audit + artifact store)

That is, the :class:`ExportResult` under test is the *actual* result the
Export_Manager produces from a mocked InsightConnect tenant API, not one
constructed by hand. Only the network boundary is faked (an injected
:class:`TenantUploader`); the registry, audit log, and artifact store are all
real collaborators and no network is contacted (Req 10.1).

* **Success path** (Req 10.1, 10.2): a 2xx tenant response yields
  ``ExportResult.success`` and the outcome recording writes an export row to the
  ``Plugin_Registry`` (target region + upload timestamp) plus a success entry to
  the ``Audit_Log``; no artifact is retained.
* **Failure/timeout paths** (Req 10.3): a non-2xx response, an upload exception,
  and a :class:`TimeoutError` each yield ``ExportResult.failed``; the outcome
  recording leaves the ``Plugin_Registry`` unchanged, appends a failed-attempt
  entry to the ``Audit_Log``, and retains the already-built ``.plg`` for retry.
"""

from pathlib import Path
from typing import List, Optional

import pytest

from icplugin_builder.integrations.build_engine import PLG_SUFFIX, BuildEngine, PlgArtifact
from icplugin_builder.integrations.export_manager import (
    ExportManager,
    TenantCredentials,
    UploadResponse,
)
from icplugin_builder.integrations.export_outcome import record_export_outcome
from icplugin_builder.persistence.audit_log import AuditEvent, AuditLog
from icplugin_builder.persistence.registry import PluginRegistry

PLUGIN = "my_plugin"
VENDOR = "rapid7_custom"
VERSION = "1.0.0"
REGION = "https://us.api.insight.rapid7.com"
API_KEY = "tenant-api-key"


def make_project(root: Path) -> None:
    """Create a small but real plugin working tree under ``root``."""
    files = {
        "plugin.spec.yaml": "plugin_spec_version: v2\nname: my_plugin\nversion: 1.0.0\n",
        "icon_my_plugin/actions/run/action.py": "def run():\n    return 1\n",
        "help.md": "# My Plugin\n",
        "Dockerfile": "FROM python:3.11\n",
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class FakeTenantUploader:
    """A fake InsightConnect tenant API: records calls, returns/raises a script.

    Stands in for the real network boundary so the wired flow stays deterministic
    and contacts no tenant (Req 10.1).
    """

    def __init__(self, *, response: Optional[UploadResponse] = None, exc: Optional[BaseException] = None) -> None:
        self.response = response if response is not None else UploadResponse(status_code=200, body="ok")
        self.exc = exc
        self.calls: List[dict] = []

    def upload(self, *, region_base_url, api_key, artifact_path, timeout) -> UploadResponse:
        self.calls.append(
            {
                "region_base_url": region_base_url,
                "api_key": api_key,
                "artifact_path": Path(artifact_path),
                "timeout": timeout,
            }
        )
        if self.exc is not None:
            raise self.exc
        return self.response


class FakeArtifactStore:
    """A minimal real :class:`ArtifactStore` writing under a temp directory."""

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
def built_artifact(tmp_path) -> PlgArtifact:
    """Package a real ``.plg`` the same way a tenant export would upload it."""
    source = tmp_path / "project"
    make_project(source)
    return BuildEngine().package(source, validation_passed=True, output_dir=tmp_path / "artifacts")


@pytest.fixture
def registry():
    reg = PluginRegistry(":memory:")
    reg.record_creation(PLUGIN, VENDOR, VERSION)
    try:
        yield reg
    finally:
        reg.close()


@pytest.fixture
def audit_log(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


def _run_export(uploader: FakeTenantUploader, artifact: PlgArtifact, registry, audit_log, store):
    """Drive the full wired flow: export_tenant -> record_export_outcome."""
    result = ExportManager(uploader=uploader).export_tenant(
        artifact, TenantCredentials(region_base_url=REGION, api_key=API_KEY)
    )
    outcome = record_export_outcome(
        result,
        plugin_name=PLUGIN,
        version=VERSION,
        registry=registry,
        audit_log=audit_log,
        artifact_store=store,
    )
    return result, outcome


class TestWiredSuccessPath:
    def test_success_records_export_and_audits_without_retaining(self, built_artifact, registry, audit_log, tmp_path):
        uploader = FakeTenantUploader(response=UploadResponse(status_code=201, body="created"))
        store = FakeArtifactStore(tmp_path / "retained")

        result, outcome = _run_export(uploader, built_artifact, registry, audit_log, store)

        # The tenant API was actually contacted through the uploader with the 60s budget.
        assert len(uploader.calls) == 1
        assert uploader.calls[0]["api_key"] == API_KEY
        assert uploader.calls[0]["timeout"] == 60.0
        assert uploader.calls[0]["artifact_path"] == built_artifact.path

        # The result is the one the Export_Manager produced from the 2xx response.
        assert result.success is True
        assert outcome.success is True

        # Registry records the export with the target region and the upload timestamp (Req 10.2).
        exports = registry.exports(PLUGIN)
        assert len(exports) == 1
        assert exports[0].target == REGION
        assert exports[0].export_utc == result.uploaded_utc
        assert exports[0].result == "success"
        assert exports[0].version == VERSION

        # Audit log gets exactly one success entry (no failure reason).
        records = audit_log.records()
        assert len(records) == 1
        assert records[0].event == AuditEvent.EXPORT
        assert records[0].plugin_name == PLUGIN
        assert records[0].target == REGION
        assert records[0].reason is None

        # A success retains nothing.
        assert outcome.retained_artifact is None
        assert store.saved == {}


class TestWiredFailurePaths:
    @pytest.mark.parametrize(
        "uploader, expect_timeout",
        [
            (FakeTenantUploader(response=UploadResponse(status_code=500, body="error")), False),
            (FakeTenantUploader(response=UploadResponse(status_code=409, body="conflict")), False),
            (FakeTenantUploader(exc=ConnectionError("connection refused")), False),
            (FakeTenantUploader(exc=TimeoutError("deadline exceeded")), True),
        ],
        ids=["http-500", "http-409", "connection-error", "timeout"],
    )
    def test_failure_leaves_registry_unchanged_audits_and_retains(
        self, uploader, expect_timeout, built_artifact, registry, audit_log, tmp_path
    ):
        store = FakeArtifactStore(tmp_path / "retained")

        result, outcome = _run_export(uploader, built_artifact, registry, audit_log, store)

        # The upload was attempted (past the pre-network guards) but did not succeed.
        assert len(uploader.calls) == 1
        assert result.success is False
        assert result.timed_out is expect_timeout
        assert outcome.success is False

        # Registry is left unchanged: no export row is written (Req 10.3).
        assert outcome.registry_updated is False
        assert outcome.export_record is None
        assert registry.exports(PLUGIN) == []

        # Exactly one failed-attempt entry is appended to the audit log (Req 10.3).
        records = audit_log.records()
        assert len(records) == 1
        assert records[0].event == AuditEvent.EXPORT
        assert records[0].plugin_name == PLUGIN
        assert records[0].target == REGION
        assert records[0].reason  # a failed attempt carries a non-empty reason
        assert records[0].reason == result.error

        # The already-built .plg is retained for retry and still exists on disk.
        retained = outcome.retained_artifact
        assert retained is not None
        assert retained.filename == f"{PLUGIN}-{VERSION}.plg"
        assert store.saved[retained.filename] == built_artifact.path.read_bytes()
        assert built_artifact.path.suffix == PLG_SUFFIX
        assert built_artifact.path.is_file()
