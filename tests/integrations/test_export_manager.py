"""Unit tests for the Export_Manager local + tenant export (task 16.1; Req 9.3, 10.1, 10.4, 10.5).

These cover the focused behavior of :meth:`ExportManager.export_local` (writing a
``.plg`` to a user-accessible location and reporting the path) and
:meth:`ExportManager.export_tenant` (the pre-network credential and built-artifact
guards, a successful upload, and failure/timeout classification) using temporary
directories and an injected fake uploader -- no real network is contacted.

The missing-credential *property* (Req 10.4), the build-before-export *property*
(Req 10.5), export-recording semantics (Req 10.2, 10.3), and the mocked tenant
integration test (Req 10.1) are covered separately by tasks 16.2-16.6.
"""

import gzip
from pathlib import Path
from typing import List, Optional

import pytest

# Packaging drives `docker build` and `docker save` now, and none of these tests are
# about Docker. The stub answers both from a real executable, so the production argv
# and file handling are still exercised -- only the daemon is absent.
from tests.docker_stub import stub_docker  # noqa: E402

from icplugin_builder.integrations.build_engine import PLG_SUFFIX, BuildEngine, PlgArtifact
from icplugin_builder.integrations.export_manager import (
    ArtifactNotBuiltError,
    ExportManager,
    ExportResult,
    MissingCredentialError,
    TenantCredentials,
    UploadResponse,
)


def make_project(root: Path) -> dict:
    """Create a small plugin working tree under ``root`` and return its files."""
    files = {
        "plugin.spec.yaml": "plugin_spec_version: v2\nname: my_plugin\nversion: 1.0.0\nvendor: rapid7\n",
        "icon_my_plugin/actions/run/action.py": "def run():\n    return 1\n",
        "help.md": "# My Plugin\n",
        "Dockerfile": "FROM python:3.11\n",
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return files


class RecordingUploader:
    """A fake :class:`TenantUploader` that records calls and returns a scripted result."""

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


def _package(source: Path, out: Path) -> PlgArtifact:
    from icplugin_builder.integrations.build_engine import BuildEngine

    make_project(source)
    return BuildEngine(docker_executable=stub_docker(out.parent)).package(
        source, validation_passed=True, output_dir=out
    )


class TestExportLocal:
    def test_writes_plg_to_output_dir_and_reports_path(self, tmp_path):
        source = tmp_path / "project"
        make_project(source)
        out = tmp_path / "downloads"

        manager = ExportManager(build_engine=BuildEngine(docker_executable=stub_docker(tmp_path)))
        path = manager.export_local(source, output_dir=out)

        assert path.suffix == PLG_SUFFIX
        assert path.parent == out.resolve()
        assert path.is_file()
        # The reported artifact is a real gzipped tarball.
        with open(path, "rb") as handle:
            assert handle.read(2) == b"\x1f\x8b"  # gzip magic
        with gzip.open(path):
            pass

    def test_defaults_to_current_working_directory(self, tmp_path, monkeypatch):
        source = tmp_path / "project"
        make_project(source)
        workdir = tmp_path / "cwd"
        workdir.mkdir()
        monkeypatch.chdir(workdir)

        manager = ExportManager(build_engine=BuildEngine(docker_executable=stub_docker(tmp_path)))
        path = manager.export_local(source)

        assert path.parent == workdir.resolve()
        assert path.is_file()


class TestExportTenantGuards:
    def test_empty_region_rejected_before_network(self, tmp_path):
        uploader = RecordingUploader()
        artifact = _package(tmp_path / "project", tmp_path / "artifacts")

        with pytest.raises(MissingCredentialError) as excinfo:
            ExportManager(uploader=uploader).export_tenant(
                artifact, TenantCredentials(region_base_url="  ", api_key="key")
            )

        assert "region" in str(excinfo.value).lower()
        assert uploader.calls == []  # no network call was made

    def test_empty_api_key_rejected_before_network(self, tmp_path):
        uploader = RecordingUploader()
        artifact = _package(tmp_path / "project", tmp_path / "artifacts")

        with pytest.raises(MissingCredentialError) as excinfo:
            ExportManager(uploader=uploader).export_tenant(
                artifact, TenantCredentials(region_base_url="https://us.example", api_key="")
            )

        assert "api key" in str(excinfo.value).lower()
        assert uploader.calls == []

    def test_missing_artifact_rejected_before_network(self, tmp_path):
        uploader = RecordingUploader()

        with pytest.raises(ArtifactNotBuiltError):
            ExportManager(uploader=uploader).export_tenant(
                None, TenantCredentials(region_base_url="https://us.example", api_key="key")
            )

        assert uploader.calls == []

    def test_nonexistent_artifact_path_rejected_before_network(self, tmp_path):
        uploader = RecordingUploader()
        missing = tmp_path / "never-built.plg"

        with pytest.raises(ArtifactNotBuiltError):
            ExportManager(uploader=uploader).export_tenant(
                missing, TenantCredentials(region_base_url="https://us.example", api_key="key")
            )

        assert uploader.calls == []

    def test_credentials_checked_before_artifact(self, tmp_path):
        # With both a missing credential and a missing artifact, the credential
        # guard fires first (design: "validate creds first").
        uploader = RecordingUploader()

        with pytest.raises(MissingCredentialError):
            ExportManager(uploader=uploader).export_tenant(None, TenantCredentials(region_base_url="", api_key=""))

        assert uploader.calls == []


class TestExportTenantUpload:
    def test_successful_upload_returns_success_result(self, tmp_path):
        artifact = _package(tmp_path / "project", tmp_path / "artifacts")
        uploader = RecordingUploader(response=UploadResponse(status_code=201, body="created"))

        result = ExportManager(uploader=uploader).export_tenant(
            artifact, TenantCredentials(region_base_url="  https://us.example  ", api_key="secret")
        )

        assert isinstance(result, ExportResult)
        assert result.success is True
        assert result.failed is False
        assert result.status_code == 201
        # Surrounding whitespace is stripped from the recorded region.
        assert result.region_base_url == "https://us.example"
        assert result.artifact_path == artifact.path
        assert result.uploaded_utc  # stamped
        # Upload was invoked with the stripped credentials and the 60s default timeout.
        assert len(uploader.calls) == 1
        call = uploader.calls[0]
        assert call["region_base_url"] == "https://us.example"
        assert call["api_key"] == "secret"
        assert call["timeout"] == 60.0
        assert call["artifact_path"] == artifact.path

    def test_accepts_artifact_given_as_path(self, tmp_path):
        artifact = _package(tmp_path / "project", tmp_path / "artifacts")
        uploader = RecordingUploader()

        result = ExportManager(uploader=uploader).export_tenant(
            str(artifact.path), TenantCredentials(region_base_url="https://us.example", api_key="k")
        )

        assert result.success is True
        assert uploader.calls[0]["artifact_path"] == artifact.path

    def test_non_2xx_response_is_a_failure(self, tmp_path):
        artifact = _package(tmp_path / "project", tmp_path / "artifacts")
        uploader = RecordingUploader(response=UploadResponse(status_code=409, body="conflict"))

        result = ExportManager(uploader=uploader).export_tenant(
            artifact, TenantCredentials(region_base_url="https://us.example", api_key="k")
        )

        assert result.success is False
        assert result.failed is True
        assert result.status_code == 409
        assert "409" in result.error

    def test_timeout_is_classified_as_timeout_failure(self, tmp_path):
        artifact = _package(tmp_path / "project", tmp_path / "artifacts")
        uploader = RecordingUploader(exc=TimeoutError("deadline exceeded"))

        result = ExportManager(uploader=uploader).export_tenant(
            artifact,
            TenantCredentials(region_base_url="https://us.example", api_key="k"),
            timeout=60.0,
        )

        assert result.success is False
        assert result.timed_out is True
        assert "timed out" in result.error.lower()

    def test_upload_exception_is_reported_not_raised(self, tmp_path):
        artifact = _package(tmp_path / "project", tmp_path / "artifacts")
        uploader = RecordingUploader(exc=ConnectionError("connection refused"))

        result = ExportManager(uploader=uploader).export_tenant(
            artifact, TenantCredentials(region_base_url="https://us.example", api_key="k")
        )

        assert result.success is False
        assert result.timed_out is False
        assert "failed" in result.error.lower()

    def test_custom_timeout_is_forwarded_to_uploader(self, tmp_path):
        artifact = _package(tmp_path / "project", tmp_path / "artifacts")
        uploader = RecordingUploader()

        ExportManager(uploader=uploader).export_tenant(
            artifact,
            TenantCredentials(region_base_url="https://us.example", api_key="k"),
            timeout=10.0,
        )

        assert uploader.calls[0]["timeout"] == 10.0
