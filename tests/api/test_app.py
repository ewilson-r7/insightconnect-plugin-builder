"""Integration tests for the FastAPI app, routes, and WebSocket (task 22.1).

These drive the real :class:`Orchestrator` (with the real pure-logic and
persistence components) behind the HTTP/WebSocket surface using FastAPI's
``TestClient``. Costly externals are either unused (net-new drafts need none) or
mocked (a passing code validator stands in for the Docker pipeline), matching
the repo's convention of exercising real dispatch paths with mocked externals.

Covered here (basic endpoint + startup coverage; deeper coverage is task 22.2):

* health/config routes and the loopback/access posture (Req 17.4, 20.1);
* session lifecycle: start (net-new/iterate), get, token counter, visualization
  (Req 1.4, 3.6, 5);
* message submission wiring through to the orchestrator (Req 1);
* export prepare/confirm wiring (Req 12, 16, 9);
* registry-backed plugin list/history (Req 11);
* the WebSocket channel streaming state + turn + tokens + visualization (Req 5.3);
* optional access protection denying/permitting routes and the WebSocket (Req 17);
* a self-contained app built from config starting on loopback (Req 20.1).
"""

from fastapi.testclient import TestClient

from icplugin_builder.api.app import create_app, create_app_from_config
from icplugin_builder.api.config import load_config
from icplugin_builder.core.spec_model import PluginSpec, SemVer
from icplugin_builder.integrations.code_validator import (
    PipelineReport,
    StageName,
    StageResult,
    StageStatus,
)
from icplugin_builder.orchestrator.orchestrator import Orchestrator
from icplugin_builder.persistence.audit_log import AuditLog
from icplugin_builder.persistence.project_folder import ENTRY_MODE_CREATE_NEW, ProjectFolder
from icplugin_builder.persistence.registry import PluginRegistry
from icplugin_builder.security.access_controller import AccessController, hash_passphrase
from icplugin_builder.api.config import AccessConfig, NetworkConfig

# --- fakes -----------------------------------------------------------------


class FakeCodeValidator:
    """A code validator whose four-stage pipeline always passes."""

    async def run_pipeline(self, project, *, image_tag=None):
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


# --- health & config -------------------------------------------------------


class TestHealthAndConfig:
    def test_health_reports_loopback_by_default(self):
        app = create_app(orchestrator=Orchestrator())
        client = TestClient(app)
        resp = client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["bind_address"] == "127.0.0.1"
        assert body["access_protection"] is False

    def test_config_summary_has_no_secrets(self, tmp_path):
        config = load_config({"llm": {"provider": "kiro_cli", "kiro_cli_path": "/bin/kiro"}})
        app = create_app(orchestrator=Orchestrator(), config=config)
        client = TestClient(app)
        body = client.get("/api/config").json()
        assert body["configured"] is True
        assert body["bind_address"] == "127.0.0.1"
        assert body["llm_provider"] == "kiro_cli"
        # No passphrase/secret leaks into the summary.
        assert "passphrase" not in body
        assert "api_key" not in body


# --- session lifecycle -----------------------------------------------------


class TestSessionRoutes:
    def _client(self):
        return TestClient(create_app(orchestrator=Orchestrator()))

    def test_start_net_new_returns_initial_state(self):
        client = self._client()
        resp = client.post(
            "/api/session",
            json={"entry_mode": ENTRY_MODE_CREATE_NEW, "session_id": "s1", "user_id": "u1"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == "s1"
        assert body["entry_mode"] == ENTRY_MODE_CREATE_NEW
        assert body["token_total"] == 0
        assert body["visualization"]["state"] == "empty"

    def test_unknown_entry_mode_is_400(self):
        client = self._client()
        resp = client.post("/api/session", json={"entry_mode": "bogus", "session_id": "s1", "user_id": "u1"})
        assert resp.status_code == 400

    def test_get_missing_session_is_404(self):
        client = self._client()
        assert client.get("/api/session/ghost").status_code == 404

    def test_tokens_and_visualization(self):
        client = self._client()
        client.post("/api/session", json={"entry_mode": ENTRY_MODE_CREATE_NEW, "session_id": "s1", "user_id": "u1"})
        assert client.get("/api/session/s1/tokens").json()["token_total"] == 0
        viz = client.get("/api/session/s1/visualization").json()
        assert viz["state"] == "empty"
        assert viz["nodes"]  # connection node always present

    def test_submit_message_wires_to_orchestrator(self):
        client = self._client()
        client.post("/api/session", json={"entry_mode": ENTRY_MODE_CREATE_NEW, "session_id": "s1", "user_id": "u1"})
        # A blank message is rejected by the input gate (Req 1.6).
        resp = client.post("/api/session/s1/message", json={"text": "   "})
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected_input"


# --- export ----------------------------------------------------------------


class TestExportRoutes:
    def _client(self, tmp_path):
        _create_project(tmp_path, name="my_plugin", vendor="acme")
        registry = PluginRegistry(str(tmp_path / "registry.db"))
        orch = Orchestrator(
            projects_root=tmp_path,
            code_validator=FakeCodeValidator(),
            registry=registry,
            audit_log=AuditLog(tmp_path / "audit.log"),
        )
        app = create_app(orchestrator=orch, registry=registry)
        client = TestClient(app)
        client.post(
            "/api/session",
            json={"entry_mode": "iterate_custom", "session_id": "s1", "user_id": "u1", "plugin_name": "my_plugin"},
        )
        return client, registry

    def test_prepare_then_confirm_local_export(self, tmp_path):
        client, registry = self._client(tmp_path)
        plan = client.post("/api/session/s1/export/prepare").json()
        assert plan["permitted"] is True
        assert plan["spec_preview"]["vendor"].endswith("_custom")
        assert plan["diff"]["first_version"] is True

        out_dir = tmp_path / "out"
        confirm = client.post(
            "/api/session/s1/export/confirm",
            json={"confirmed": True, "target": "local", "output_dir": str(out_dir)},
        )
        assert confirm.status_code == 200
        outcome = confirm.json()
        assert outcome["status"] == "succeeded"
        assert outcome["artifact_path"]

        plugins = client.get("/api/plugins").json()["plugins"]
        assert any(p["name"] == "my_plugin" for p in plugins)
        history = client.get("/api/plugins/my_plugin/history").json()["history"]
        assert history  # at least the creation + export entries

    def test_confirm_without_prepare_is_409(self, tmp_path):
        client, _ = self._client(tmp_path)
        resp = client.post("/api/session/s1/export/confirm", json={"confirmed": True, "target": "local"})
        assert resp.status_code == 409

    def test_decline_aborts(self, tmp_path):
        client, registry = self._client(tmp_path)
        client.post("/api/session/s1/export/prepare")
        outcome = client.post("/api/session/s1/export/confirm", json={"confirmed": False}).json()
        assert outcome["status"] == "aborted"
        assert registry.exports("my_plugin") == []


# --- WebSocket -------------------------------------------------------------


class TestWebSocket:
    def test_channel_streams_state_and_turn(self):
        app = create_app(orchestrator=Orchestrator())
        client = TestClient(app)
        client.post("/api/session", json={"entry_mode": ENTRY_MODE_CREATE_NEW, "session_id": "s1", "user_id": "u1"})
        with client.websocket_connect("/ws/s1") as ws:
            first = ws.receive_json()
            assert first["type"] == "state"
            assert first["state"]["session_id"] == "s1"
            ws.send_json({"type": "submit_message", "text": "   "})
            turn = ws.receive_json()
            assert turn["type"] == "turn"
            assert turn["result"]["status"] == "rejected_input"
            tokens = ws.receive_json()
            assert tokens["type"] == "tokens"
            viz = ws.receive_json()
            assert viz["type"] == "visualization"

    def test_unknown_session_channel_closed(self):
        app = create_app(orchestrator=Orchestrator())
        client = TestClient(app)
        with client.websocket_connect("/ws/ghost") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "error"


# --- access protection (Req 17) --------------------------------------------


class TestAccessProtection:
    def _protected_app(self):
        passphrase = "correct horse battery staple"
        access = AccessConfig(protection_enabled=True, passphrase_hash=hash_passphrase(passphrase))
        controller = AccessController(access, network_config=NetworkConfig())
        app = create_app(orchestrator=Orchestrator(), access_controller=controller)
        return app, passphrase

    def test_denies_without_passphrase(self):
        app, _ = self._protected_app()
        client = TestClient(app)
        # Health is unprotected; config is protected.
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/config").status_code == 401

    def test_permits_with_correct_passphrase(self):
        app, passphrase = self._protected_app()
        client = TestClient(app)
        resp = client.post(
            "/api/session",
            json={"entry_mode": ENTRY_MODE_CREATE_NEW, "session_id": "s1", "user_id": "u1"},
            headers={"X-Access-Passphrase": passphrase},
        )
        assert resp.status_code == 200

    def test_wrong_passphrase_denied(self):
        app, _ = self._protected_app()
        client = TestClient(app)
        resp = client.post(
            "/api/session",
            json={"entry_mode": ENTRY_MODE_CREATE_NEW, "session_id": "s1", "user_id": "u1"},
            headers={"X-Access-Passphrase": "wrong"},
        )
        assert resp.status_code == 401


# --- self-contained startup (Req 20.1) -------------------------------------


class TestStartupFromConfig:
    def test_app_starts_self_contained_on_loopback(self, tmp_path):
        config = load_config(
            {
                "llm": {"provider": "kiro_cli", "kiro_cli_path": "/bin/kiro"},
                "paths": {"config_root": str(tmp_path / "cfg"), "projects_root": str(tmp_path / "projects")},
            }
        )
        app = create_app_from_config(config)
        client = TestClient(app)
        health = client.get("/api/health").json()
        assert health["status"] == "ok"
        assert health["bind_address"] == "127.0.0.1"
        # Registry-backed list route responds even with no plugins yet.
        assert client.get("/api/plugins").json() == {"plugins": []}
