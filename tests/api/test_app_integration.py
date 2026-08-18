"""Integration tests for API endpoints, WebSocket, and startup (task 22.2).

Task 22.1 wired the FastAPI app and gave it a first pass of endpoint tests
(``tests/api/test_app.py``). This module adds the deeper integration coverage
called for by task 22.2: it drives the *real* :class:`Orchestrator` (with the
real pure-logic and persistence components) behind the HTTP/WebSocket surface
via FastAPI's ``TestClient`` and exercises the routes and startup paths that
touch collaborators task 22.1 left lightly covered, always with the costly
externals mocked (the tenant uploader, the production-source provider, and the
update manager are fakes; no network, Docker, Kiro CLI, or git remote is
contacted).

Covered here (Req 20.1 self-contained startup + core routes/WebSocket wiring):

* tenant export wired through prepare/confirm, success **and** failure/timeout
  paths, with a fake uploader standing in for the InsightConnect tenant API
  (Req 10.1, 10.2, 10.3, 19.2, 19.4);
* tenant export credential guards surfaced at the API boundary (Req 10.4);
* the export gate blocking a build when validation fails (Req 7.4, 8.6);
* the read-only production-source routes and the ``enhance_production`` entry
  mode wired through a fake ``Plugin_Source_Provider`` (Req 24.4, 25.1, 25.6);
* the managed-tooling update route wired through a fake ``Update_Manager``
  (Req 23.3, 23.4);
* graceful behaviour when optional collaborators are absent (registry, source
  provider, update manager);
* the WebSocket channel's bad-frame handling and access-protection rejection
  (Req 5.3, 17.2);
* a self-contained app built from config that serves pre-built UI static assets
  and starts on loopback (Req 20.1).
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from icplugin_builder.api.app import create_app, create_app_from_config
from icplugin_builder.api.config import AccessConfig, NetworkConfig, load_config
from icplugin_builder.core.spec_model import PluginSpec, SemVer
from icplugin_builder.integrations.code_validator import (
    PipelineReport,
    StageName,
    StageResult,
    StageStatus,
)
from icplugin_builder.integrations.export_manager import ExportManager, UploadResponse
from icplugin_builder.orchestrator.orchestrator import Orchestrator
from icplugin_builder.persistence.audit_log import AuditLog
from icplugin_builder.persistence.project_folder import (
    ENTRY_MODE_CREATE_NEW,
    ENTRY_MODE_ENHANCE_PRODUCTION,
    ProjectFolder,
    ProvenanceRecord,
)
from icplugin_builder.persistence.registry import PluginRegistry
from icplugin_builder.security.access_controller import AccessController, hash_passphrase

# --- fakes -----------------------------------------------------------------


class FakeCodeValidator:
    """A code validator whose four-stage pipeline always passes."""

    async def run_pipeline(self, project, *, image_tag=None, unit_test_run=None):
        stages = tuple(
            StageResult(
                name=name,
                status=StageStatus.PASSED,
                returncode=0,
                stdout="",
                stderr="",
                duration_seconds=0.0,
                message="",
            )
            for name in StageName.ORDER
        )
        return PipelineReport(project_dir=project, stages=stages, docker_available=True)


class FakeUploader:
    """A tenant uploader that records calls and returns a canned response.

    Standing in for the InsightConnect tenant API keeps the export flow
    deterministic and offline (design "costly externals are mocked").
    """

    def __init__(self, *, status_code=200, raise_error=None):
        self.status_code = status_code
        self.raise_error = raise_error
        self.calls = []

    def upload(self, *, region_base_url, api_key, artifact_path, timeout):
        self.calls.append(
            {"region_base_url": region_base_url, "api_key": api_key, "artifact_path": artifact_path, "timeout": timeout}
        )
        if self.raise_error is not None:
            raise self.raise_error
        return UploadResponse(status_code=self.status_code)


class FakeSourceProvider:
    """A read-only production-source provider backed by real project folders."""

    def __init__(self, projects_root):
        self._root = projects_root
        self.imported = []

    def list_sources(self):
        return [
            SimpleNamespace(id="rapid7_public", available=True),
            SimpleNamespace(id="komand_private", available=False),
        ]

    def list_plugins(self, source_id):
        return [
            SimpleNamespace(name="base64", version="1.0.0"),
            SimpleNamespace(name="regex", version="2.1.0"),
        ]

    def import_plugin(self, source, name):
        self.imported.append((source, name))
        spec = _make_spec(name=name, vendor="rapid7")
        folder = ProjectFolder.create(self._root, name, spec)
        folder.save(spec, generated_files={"README.md": "imported\n"})
        provenance = ProvenanceRecord(entry_mode=ENTRY_MODE_ENHANCE_PRODUCTION, created_utc=_now())
        notice = "This plugin comes from a private source and is subject to its usage restrictions."
        return SimpleNamespace(
            project_folder=folder,
            provenance=provenance,
            private_source_notice=notice if source == "komand_private" else None,
        )


class FakeUpdateManager:
    """An update manager returning a canned, non-blocking check result."""

    def __init__(self, *, performed=True, from_cache=False, skipped=False):
        self._result = SimpleNamespace(performed=performed, from_cache=from_cache, skipped=skipped)
        self.checks = 0

    def check_upstream(self):
        self.checks += 1
        return self._result


# --- helpers ---------------------------------------------------------------


def _now():
    return datetime.now(timezone.utc).isoformat()


def _make_spec(name="my_plugin", vendor="acme"):
    return PluginSpec(
        name=name,
        title="My Plugin",
        description="A test plugin.",
        version=SemVer(1, 0, 0),
        vendor=vendor,
    )


def _create_project(projects_root, name="my_plugin", vendor="acme"):
    spec = _make_spec(name=name, vendor=vendor)
    folder = ProjectFolder.create(projects_root, name, spec)
    folder.save(spec, generated_files={"README.md": "hello\n"})
    return folder


def _iterate_client(tmp_path, *, uploader=None, name="my_plugin"):
    """Build a client whose session iterates a real project, ready to export."""
    _create_project(tmp_path, name=name, vendor="acme")
    registry = PluginRegistry(str(tmp_path / "registry.db"))
    export_manager = ExportManager(uploader=uploader) if uploader is not None else None
    orch = Orchestrator(
        projects_root=tmp_path,
        code_validator=FakeCodeValidator(),
        registry=registry,
        audit_log=AuditLog(tmp_path / "audit.log"),
        export_manager=export_manager,
    )
    app = create_app(orchestrator=orch, registry=registry)
    client = TestClient(app)
    client.post(
        "/api/session",
        json={"entry_mode": "iterate_custom", "session_id": "s1", "user_id": "u1", "plugin_name": name},
    )
    return client, registry


# --- tenant export (Req 10, 19) --------------------------------------------


class TestTenantExport:
    def test_prepare_then_confirm_tenant_upload_records_export(self, tmp_path):
        uploader = FakeUploader(status_code=200)
        client, registry = _iterate_client(tmp_path, uploader=uploader)

        assert client.post("/api/session/s1/export/prepare").json()["permitted"] is True
        confirm = client.post(
            "/api/session/s1/export/confirm",
            json={
                "confirmed": True,
                "target": "tenant",
                "region_base_url": "https://us.api.insight.rapid7.com",
                "api_key": "secret-key",
            },
        )
        assert confirm.status_code == 200
        outcome = confirm.json()
        assert outcome["status"] == "succeeded"
        assert outcome["target"] == "https://us.api.insight.rapid7.com"

        # The upload actually went through the (fake) tenant API.
        assert len(uploader.calls) == 1
        assert uploader.calls[0]["region_base_url"] == "https://us.api.insight.rapid7.com"

        # A successful upload is recorded in the registry (Req 10.2).
        exports = registry.exports("my_plugin")
        assert any(rec.target == "https://us.api.insight.rapid7.com" for rec in exports)

    def test_tenant_upload_failure_retains_artifact_and_leaves_registry_unchanged(self, tmp_path):
        uploader = FakeUploader(status_code=500)
        client, registry = _iterate_client(tmp_path, uploader=uploader)

        client.post("/api/session/s1/export/prepare")
        outcome = client.post(
            "/api/session/s1/export/confirm",
            json={
                "confirmed": True,
                "target": "tenant",
                "region_base_url": "https://us.api.insight.rapid7.com",
                "api_key": "secret-key",
            },
        ).json()

        # Req 19.4: the failure is classified as an export failure (not a build failure).
        assert outcome["status"] == "export_failed"
        # Req 19.2: the built artifact is retained for retry.
        assert outcome["retained_artifact_path"]
        # Req 10.3: a failed upload leaves the registry export history unchanged.
        assert registry.exports("my_plugin") == []

    def test_tenant_export_without_credentials_is_400(self, tmp_path):
        client, _ = _iterate_client(tmp_path, uploader=FakeUploader())
        client.post("/api/session/s1/export/prepare")
        resp = client.post(
            "/api/session/s1/export/confirm",
            json={"confirmed": True, "target": "tenant"},
        )
        # Req 10.4: the API rejects a tenant export missing credentials before any upload.
        assert resp.status_code == 400


# --- export gating (Req 7.4, 8.6) ------------------------------------------


class TestExportGating:
    def test_blocked_export_when_spec_invalid(self):
        # A net-new empty draft has an incomplete spec that fails validation.
        client = TestClient(create_app(orchestrator=Orchestrator()))
        client.post("/api/session", json={"entry_mode": ENTRY_MODE_CREATE_NEW, "session_id": "s1", "user_id": "u1"})

        plan = client.post("/api/session/s1/export/prepare").json()
        assert plan["permitted"] is False
        assert plan["spec_errors"]  # every validation error is surfaced with a path

        outcome = client.post(
            "/api/session/s1/export/confirm",
            json={"confirmed": True, "target": "local"},
        ).json()
        assert outcome["status"] == "blocked"


# --- production sources + enhance entry mode (Req 24.4, 25) ----------------


class TestProductionSources:
    def _client(self, tmp_path):
        provider = FakeSourceProvider(tmp_path)
        orch = Orchestrator(projects_root=tmp_path, source_provider=provider)
        app = create_app(orchestrator=orch, source_provider=provider)
        return TestClient(app), provider

    def test_list_sources_reports_availability(self, tmp_path):
        client, _ = self._client(tmp_path)
        body = client.get("/api/sources").json()
        ids = {s["id"]: s["available"] for s in body["sources"]}
        assert ids == {"rapid7_public": True, "komand_private": False}

    def test_list_source_plugins(self, tmp_path):
        client, _ = self._client(tmp_path)
        body = client.get("/api/sources/rapid7_public/plugins").json()
        names = {p["name"] for p in body["plugins"]}
        assert names == {"base64", "regex"}

    def test_enhance_entry_mode_imports_and_carries_private_notice(self, tmp_path):
        client, provider = self._client(tmp_path)
        resp = client.post(
            "/api/session",
            json={
                "entry_mode": ENTRY_MODE_ENHANCE_PRODUCTION,
                "session_id": "s1",
                "user_id": "u1",
                "source": "komand_private",
                "production_plugin": "base64",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["entry_mode"] == ENTRY_MODE_ENHANCE_PRODUCTION
        assert body["plugin_name"] == "base64"
        # Req 25.6: a private-source import surfaces the usage-restriction notice.
        assert body["private_source_notice"]
        assert provider.imported == [("komand_private", "base64")]

    def test_source_routes_without_provider(self):
        client = TestClient(create_app(orchestrator=Orchestrator()))
        assert client.get("/api/sources").json() == {"sources": []}
        # With no provider configured, listing a source's plugins is a 404.
        assert client.get("/api/sources/any/plugins").status_code == 404


# --- managed-tooling updates (Req 23) --------------------------------------


class TestUpdatesRoute:
    def test_updates_route_reports_check_result(self):
        manager = FakeUpdateManager(performed=True, from_cache=False, skipped=False)
        app = create_app(orchestrator=Orchestrator(), update_manager=manager)
        body = TestClient(app).get("/api/updates").json()
        assert body == {"checked": True, "performed": True, "from_cache": False, "skipped": False}
        assert manager.checks == 1

    def test_updates_route_without_manager(self):
        client = TestClient(create_app(orchestrator=Orchestrator()))
        assert client.get("/api/updates").json() == {"available": False, "checked": False}


# --- config route without config -------------------------------------------


class TestConfigWithoutConfig:
    def test_config_route_reports_unconfigured(self):
        client = TestClient(create_app(orchestrator=Orchestrator()))
        assert client.get("/api/config").json() == {"configured": False}


# --- session read routes ---------------------------------------------------


class TestSessionReadRoutes:
    def test_get_session_returns_full_state(self):
        client = TestClient(create_app(orchestrator=Orchestrator()))
        client.post("/api/session", json={"entry_mode": ENTRY_MODE_CREATE_NEW, "session_id": "s1", "user_id": "u1"})
        body = client.get("/api/session/s1").json()
        assert body["session_id"] == "s1"
        assert body["entry_mode"] == ENTRY_MODE_CREATE_NEW
        assert body["visualization"]["state"] == "empty"
        assert body["token_total"] == 0


# --- WebSocket edge cases (Req 5.3, 17.2) ----------------------------------


class TestWebSocketEdges:
    def test_non_submit_frame_is_rejected(self):
        client = TestClient(create_app(orchestrator=Orchestrator()))
        client.post("/api/session", json={"entry_mode": ENTRY_MODE_CREATE_NEW, "session_id": "s1", "user_id": "u1"})
        with client.websocket_connect("/ws/s1") as ws:
            assert ws.receive_json()["type"] == "state"
            ws.send_json({"type": "not_a_submit"})
            reply = ws.receive_json()
            assert reply["type"] == "error"
            assert "submit_message" in reply["detail"]

    def test_websocket_denied_without_passphrase(self):
        passphrase = "correct horse battery staple"
        access = AccessConfig(protection_enabled=True, passphrase_hash=hash_passphrase(passphrase))
        controller = AccessController(access, network_config=NetworkConfig())
        orch = Orchestrator()
        app = create_app(orchestrator=orch, access_controller=controller)
        client = TestClient(app)
        # The session must exist; start it with the passphrase header.
        client.post(
            "/api/session",
            json={"entry_mode": ENTRY_MODE_CREATE_NEW, "session_id": "s1", "user_id": "u1"},
            headers={"X-Access-Passphrase": passphrase},
        )
        # Connecting without the passphrase query param is rejected before the
        # channel opens: the server closes with a policy-violation code (Req 17.2).
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/ws/s1"):
                pass
        assert excinfo.value.code == 1008

    def test_websocket_permitted_with_passphrase(self):
        passphrase = "correct horse battery staple"
        access = AccessConfig(protection_enabled=True, passphrase_hash=hash_passphrase(passphrase))
        controller = AccessController(access, network_config=NetworkConfig())
        app = create_app(orchestrator=Orchestrator(), access_controller=controller)
        client = TestClient(app)
        client.post(
            "/api/session",
            json={"entry_mode": ENTRY_MODE_CREATE_NEW, "session_id": "s1", "user_id": "u1"},
            headers={"X-Access-Passphrase": passphrase},
        )
        with client.websocket_connect(f"/ws/s1?passphrase={passphrase.replace(' ', '%20')}") as ws:
            assert ws.receive_json()["type"] == "state"


# --- self-contained startup with static UI (Req 20.1) ----------------------


class TestStartupServesStaticUI:
    def test_app_from_config_serves_prebuilt_ui(self, tmp_path):
        ui_dir = tmp_path / "ui"
        ui_dir.mkdir()
        (ui_dir / "index.html").write_text("<!doctype html><title>ICPB</title>", encoding="utf-8")

        config = load_config(
            {
                "llm": {"provider": "kiro_cli", "kiro_cli_path": "/bin/kiro"},
                "paths": {"config_root": str(tmp_path / "cfg"), "projects_root": str(tmp_path / "projects")},
            }
        )
        app = create_app_from_config(config, static_dir=ui_dir)
        client = TestClient(app)

        # API still runs alongside the static mount.
        assert client.get("/api/health").json()["status"] == "ok"
        # The pre-built UI is served at the root (Req 20.1).
        root = client.get("/")
        assert root.status_code == 200
        assert "ICPB" in root.text
