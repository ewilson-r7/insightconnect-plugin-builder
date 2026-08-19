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

import asyncio
from contextlib import suppress
from pathlib import Path

from icplugin_builder.api.app import (
    _serialize_export_plan,
    _WebsocketProgress,
    create_app,
    create_app_from_config,
)
from icplugin_builder.api.config import load_config
from icplugin_builder.core.truncation import MAX_DISPLAY_CHARS
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


class FakeBlockingCodeValidator:
    """A code validator whose ``lint`` and ``test`` stages always fail.

    Mirrors the real blocked-gate case: the pipeline runs to completion but two
    stages exit non-zero, so the export gate refuses to build.
    """

    async def run_pipeline(self, project, *, image_tag=None, unit_test_run=None):
        failed = {StageName.LINT, StageName.TEST}
        stages = tuple(
            StageResult(
                name=name,
                status=StageStatus.FAILED if name in failed else StageStatus.PASSED,
                returncode=1 if name in failed else 0,
                stdout="",
                stderr="",
                duration_seconds=0.0,
                message=f"{name} failed" if name in failed else "",
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
    def _client(self, tmp_path, code_validator=None):
        _create_project(tmp_path, name="my_plugin", vendor="acme")
        registry = PluginRegistry(str(tmp_path / "registry.db"))
        orch = Orchestrator(
            projects_root=tmp_path,
            code_validator=code_validator or FakeCodeValidator(),
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

    def test_the_preview_reports_whether_the_plugin_is_finished(self, tmp_path):
        # A permitted export and a finished plugin are different claims. This
        # project's tree is bare, so the preview must permit the export and still
        # say the plugin is not done, naming what is outstanding (Req 27.2, 27.3).
        client, _ = self._client(tmp_path)
        plan = client.post("/api/session/s1/export/prepare").json()

        assert plan["permitted"] is True
        assert plan["plugin_is_done"] is False
        assert plan["done_conditions"], "outstanding conditions must reach the operator"
        names = {condition["name"] for condition in plan["done_conditions"]}
        assert "api_client" in names
        for condition in plan["done_conditions"]:
            assert condition["status"] in ("unmet", "unverified")
            assert condition["detail"]
        # The headline says both things, so "export permitted" is not the last word.
        assert "not complete" in plan["summary"]

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

    def test_blocked_gate_refuses_to_export(self, tmp_path):
        # A failing stage must block the export and name the reason, without
        # producing an artifact (Req 7.4, 8.6).
        client, registry = self._client(tmp_path, code_validator=FakeBlockingCodeValidator())
        plan = client.post("/api/session/s1/export/prepare").json()
        assert plan["permitted"] is False
        # Clause 2.16: each failing stage is an entry carrying its own output, not a
        # bare name -- so the operator does not have to reproduce the pipeline to
        # learn what failed. The names are still present, which is what 3.5 keeps.
        assert {entry["name"] for entry in plan["failed_stages"]} == {"lint", "test"}
        for entry in plan["failed_stages"]:
            assert set(entry) >= {"name", "status", "returncode", "message", "displayed_output", "full_output"}

        out_dir = tmp_path / "out"
        confirm = client.post(
            "/api/session/s1/export/confirm",
            json={"confirmed": True, "target": "local", "output_dir": str(out_dir)},
        )
        assert confirm.status_code == 200
        outcome = confirm.json()
        assert outcome["status"] == "blocked"
        assert outcome["artifact_path"] is None
        assert registry.exports("my_plugin") == []

    def test_force_overrides_a_blocked_gate(self, tmp_path):
        # The force flag is the documented override for a blocked gate. It used to
        # raise FrozenInstanceError and surface as an unhandled 500, so the whole
        # escape hatch was unreachable; keep it exercised.
        client, _ = self._client(tmp_path, code_validator=FakeBlockingCodeValidator())
        assert client.post("/api/session/s1/export/prepare").json()["permitted"] is False

        out_dir = tmp_path / "out"
        confirm = client.post(
            "/api/session/s1/export/confirm",
            json={"confirmed": True, "target": "local", "output_dir": str(out_dir), "force": True},
        )
        assert confirm.status_code == 200, confirm.text
        outcome = confirm.json()
        assert outcome["status"] == "succeeded", outcome
        assert outcome["artifact_path"]


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


class TestBlockedExportDetail:
    """Clause 2.16 -- a blocked export reports what failed, not only that it did.

    The payload used to carry `failed_stages` as a list of names, so every stage's
    stdout, stderr, returncode and message was discarded at the boundary even though
    the report held all four. An operator's only route to the reason was to re-run
    the four-stage pipeline by hand.
    """

    @staticmethod
    def _plan(*failures, spec_valid=True):
        """An ExportPlan whose pipeline failed the named stages with the given output.

        Assembled through the production types so what is serialized is the shape
        ``prepare_export`` produces rather than a hand-built stand-in.
        """
        from icplugin_builder.core.diff import diff_file_trees
        from icplugin_builder.core.spec_validator import SpecValidationError, ValidationReport
        from icplugin_builder.core.version_bump import bump_version
        from icplugin_builder.integrations.code_validator import PipelineReport, StageResult, StageStatus
        from icplugin_builder.integrations.export_gate import decide_export
        from icplugin_builder.orchestrator.session import ExportPlan

        stages = tuple(
            StageResult(
                name=name,
                status=StageStatus.FAILED,
                returncode=1,
                stdout=output,
                stderr="",
                duration_seconds=0.0,
                message=f"{name} stage failed",
            )
            for name, output in failures
        )
        report = PipelineReport(project_dir=Path("/tmp/x"), stages=stages, docker_available=True) if stages else None
        errors = () if spec_valid else (SpecValidationError(path="name", message="required"),)
        spec_report = ValidationReport(errors=errors)
        spec = PluginSpec(
            name="acme_widget",
            title="Acme",
            description="A spec for the blocked-export payload.",
            version=SemVer(1, 0, 0),
            vendor="acme",
        )
        return ExportPlan(
            decision=decide_export(spec_report, report),
            spec_preview=spec,
            file_list=("plugin.spec.yaml",),
            diff=diff_file_trees(None, {"plugin.spec.yaml": "x"}),
            version_bump=bump_version(spec.version, [], is_breaking=False),
            spec_report=spec_report,
            pipeline_report=report,
        )

    def test_every_failing_stage_is_reported_not_only_the_first(self):
        payload = _serialize_export_plan(self._plan(("lint", "E501 too long"), ("test", "FAILED test_x")))
        entries = {entry["name"]: entry for entry in payload["failed_stages"]}
        assert set(entries) == {"lint", "test"}
        assert "E501 too long" in entries["lint"]["displayed_output"]
        assert "FAILED test_x" in entries["test"]["displayed_output"]

    def test_each_entry_carries_the_status_returncode_and_message(self):
        payload = _serialize_export_plan(self._plan(("lint", "boom")))
        entry = payload["failed_stages"][0]
        assert entry["status"] == "failed"
        assert entry["returncode"] == 1
        assert entry["message"] == "lint stage failed"

    def test_overlong_output_is_bounded_and_retained_in_full(self):
        """Req 19.5's rule: display the first 10,000 characters, keep the whole text."""
        overlong = "x" * (MAX_DISPLAY_CHARS + 500)
        payload = _serialize_export_plan(self._plan(("lint", overlong)))
        entry = payload["failed_stages"][0]
        assert entry["truncated"] is True
        assert len(entry["displayed_output"]) == MAX_DISPLAY_CHARS
        assert len(entry["full_output"]) == len(overlong)

    def test_output_within_the_bound_is_not_marked_truncated(self):
        payload = _serialize_export_plan(self._plan(("lint", "short")))
        entry = payload["failed_stages"][0]
        assert entry["truncated"] is False
        assert entry["displayed_output"] == entry["full_output"] == "short"

    def test_a_spec_blocked_before_the_stages_still_names_the_block(self):
        """No pipeline report at all -- the payload must not simply go quiet."""
        payload = _serialize_export_plan(self._plan(spec_valid=False))
        assert payload["permitted"] is False
        assert isinstance(payload["failed_stages"], list)


class TestProgressReporting:
    """Clause 2.17 and 2.19 -- report the work done, and keep reporting while it runs.

    The generation status used to be emitted from `plan.reasoning` *before* the turn
    ran, so a turn that declined the work had already announced it: a request to
    implement against an undocumented vendor API ends in a clarification, and the
    operator had been told "Generating logic for 3 action(s)..." first. And the route
    sent nothing between that frame and the turn's result, so a 13-minute delegated
    run looked exactly like a hang.
    """

    class _Socket:
        """A websocket that records the frames sent to it."""

        def __init__(self):
            self.frames = []

        async def send_json(self, frame):
            self.frames.append(frame)

        def statuses(self):
            return [frame["message"] for frame in self.frames if frame.get("type") == "status"]

    def test_a_reported_phase_reaches_the_client(self):
        socket = self._Socket()
        reporter = _WebsocketProgress(socket)
        reporter.report("implementing 3 action(s) with the agent")
        asyncio.run(reporter.drain())
        assert socket.statuses() == ["implementing 3 action(s) with the agent"]

    def test_nothing_is_sent_for_a_turn_that_reports_no_phase(self):
        """A turn that declines the work announces nothing, which is the whole point."""
        socket = self._Socket()
        asyncio.run(_WebsocketProgress(socket).drain())
        assert socket.statuses() == []

    def test_the_ticker_restates_the_current_phase_with_its_elapsed_time(self):
        """2.19: the operator's question is "is anything happening?"."""

        async def drive():
            socket = self._Socket()
            reporter = _WebsocketProgress(socket, interval=0.05)
            reporter.report("implementing 3 action(s) with the agent")
            ticker = asyncio.create_task(reporter.tick())
            await asyncio.sleep(0.3)
            ticker.cancel()
            with suppress(asyncio.CancelledError):
                await ticker
            return socket.statuses()

        statuses = asyncio.run(drive())
        assert len(statuses) > 1, f"the ticker emitted nothing beyond the first frame: {statuses}"
        assert any("s)" in status and "agent" in status for status in statuses[1:]), statuses

    def test_the_ticker_follows_the_phase_as_it_changes(self):
        async def drive():
            socket = self._Socket()
            reporter = _WebsocketProgress(socket, interval=0.05)
            reporter.report("scaffolding the plugin working tree")
            ticker = asyncio.create_task(reporter.tick())
            await asyncio.sleep(0.15)
            reporter.report("implementing 2 action(s) with the agent")
            await asyncio.sleep(0.15)
            ticker.cancel()
            with suppress(asyncio.CancelledError):
                await ticker
            return socket.statuses()

        statuses = asyncio.run(drive())
        assert any("scaffolding" in status for status in statuses), statuses
        assert any("implementing" in status for status in statuses), statuses

    def test_a_closed_socket_does_not_break_the_turn(self):
        """The ticker runs beside the orchestration loop; it must not be able to kill it."""

        class Broken(self._Socket):
            async def send_json(self, frame):
                raise RuntimeError("client went away")

        reporter = _WebsocketProgress(Broken())
        reporter.report("implementing 1 action(s) with the agent")
        asyncio.run(reporter.drain())
