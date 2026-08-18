"""End-to-end integration test for the fully wired tool (task 24.2).

Task 24.1 wired every component together -- the Orchestrator, persistence
(registry/audit/project folders), the integrations (``insight-plugin`` CLI +
refresh coordinator, the Kiro-CLI-backed LLM_Generator, the four-stage
Docker Code_Validator, the Build_Engine, and the Export_Manager), the cost
controls, and the FastAPI app that serves the built UI -- so the operator
launches a single process. This module is the end-to-end proof that the *real*
stack works together, from ``create`` through ``generate`` -> ``validate`` ->
``build`` -> ``export`` (both local **and** tenant), with only the genuine
externals mocked (Req 20.1, 20.4).

Where the earlier suites lean on component-level fakes -- ``test_orchestration
_flows_integration`` (task 20.4) and ``test_app_integration`` (task 22.2) both
inject a ``FakeCodeValidator``/``FakeLLM``/``FakeCli`` -- this test wires the
**real** ``LLMGenerator``, ``RefreshCoordinator``/``InsightPluginCli``,
``CodeValidator``, ``BuildEngine`` and ``ExportManager`` and mocks only at the
true process/network boundary:

* the **Kiro CLI**, the **``insight-plugin`` CLI**, and the **Docker engine** are
  mocked by patching :func:`asyncio.create_subprocess_exec` with a single
  command-dispatching harness, so no real subprocess is ever launched; and
* the **InsightConnect tenant API** is mocked by injecting a fake uploader into a
  real :class:`ExportManager`, so no network is contacted.

The generation turns are applied directly on the Orchestrator because
free-form-text interpretation into concrete edits is upstream of it (the API's
``submit_message`` deliberately asks for clarification without a planner). The
validate/build/export half is then driven through the **real FastAPI app** via
``TestClient`` -- the same HTTP surface the packaged UI calls -- so the wiring
from task 24.1 is exercised for real. Coroutines that are driven outside the app
use ``asyncio.run`` so no async test plugin is required.

**Validates: Requirements 20.1, 20.4**
"""

import asyncio
import tarfile
from pathlib import Path

from fastapi.testclient import TestClient

from icplugin_builder.api.app import create_app
from icplugin_builder.core.cost_controller import CostController
from icplugin_builder.core.draft import ComponentKind
from icplugin_builder.core.generation import ArtifactKind, GenerationRequest
from icplugin_builder.core.spec_model import Component, FieldSchema, PluginSpec, SemVer
from icplugin_builder.integrations.build_engine import BuildEngine
from icplugin_builder.integrations.code_validator import CodeValidator, StageName
from icplugin_builder.integrations.export_manager import ExportManager, UploadResponse
from icplugin_builder.integrations.insight_plugin_cli import InsightPluginCli
from icplugin_builder.integrations.llm_generator import LLMGenerator
from icplugin_builder.integrations.plugin_agent import PluginAgent
from icplugin_builder.integrations.refresh_coordinator import RefreshCoordinator
from icplugin_builder.orchestrator import (
    AddComponent,
    Orchestrator,
    TurnPlan,
    TurnStatus,
)
from icplugin_builder.persistence.audit_log import AuditEvent, AuditLog
from icplugin_builder.persistence.project_folder import (
    ENTRY_MODE_ITERATE_CUSTOM,
    ProjectFolder,
)
from icplugin_builder.persistence.registry import PluginRegistry

# --- mocked externals (Kiro CLI / insight-plugin / Docker via subprocess) --

#: Token figure the mocked Kiro CLI reports for each reasoning invocation.
_KIRO_REPORTED_TOKENS = 37

#: The action-logic body the mocked Kiro CLI "generates".
_KIRO_CONTENT = "def run(self, params={}):\n    return {}"

#: The transcript the mocked Kiro CLI *agent* run emits. Shaped like a real
#: agentic run: narration of the tool calls it made, a ``> ``-prefixed closing
#: report, and the credits footer. Nothing is parsed out of this as code -- the
#: real agent writes files itself -- so the test asserts on the delegation
#: happening and being accounted for, not on recovering a payload from stdout.
_AGENT_TRANSCRIPT = (
    "I'll create the following file: icon_acme_widget/util/api.py (using tool: write)\n"
    "I will run the following command: insight-plugin validate (using tool: shell)\n"
    "> Implemented list_widgets with an API client and unit tests. "
    "insight-plugin validate and prospector both pass.\n"
    " \u25b8 Credits: 0.42 \u2022 Time: 51s\n"
)


class _FakeProcess:
    """A stand-in for the object returned by ``create_subprocess_exec``.

    Completes immediately with a scripted return code and captured output so the
    real Kiro/insight-plugin/Docker binaries are never required.
    """

    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.stdin_received = None

    async def communicate(self, stdin=None):
        self.stdin_received = stdin
        return self._stdout, self._stderr

    def kill(self):  # pragma: no cover - only used on the timeout path
        self.returncode = -9

    async def wait(self):  # pragma: no cover - only used on the timeout path
        return self.returncode


class MockedExternalProcesses:
    """One harness mocking every external *process* the wired tool shells out to.

    Patching :func:`asyncio.create_subprocess_exec` intercepts every subprocess
    the real integration stack launches and dispatches it by command:

    * ``kiro --kind <kind>`` -- the Kiro CLI (LLM provider, Req 20.3): returns the
      generated content plus a machine-readable ``total_tokens`` figure;
    * ``insight-plugin create`` / ``refresh`` / ``validate`` -- the deterministic
      scaffolder / refresh / validate operations;
    * ``docker version`` / ``build`` / ``run`` -- the Docker probe and the
      build/test stages;
    * ``prospector`` -- the offline lint stage, under the resolved profile.

    Every launched ``(argv, cwd)`` is recorded on :attr:`dispatched` so a test can
    assert exactly what reached each external and that nothing else did.
    """

    def __init__(self):
        self.dispatched = []

    @staticmethod
    def classify(argv):
        """Return a stable label for a launched command (or ``"unknown"``)."""
        if not argv:
            return "unknown"
        if argv[0] == "kiro":
            return "kiro"
        if argv[0] == "kiro-cli":
            return "agent"
        if argv[0] == "prospector":
            return StageName.LINT
        # Clause 2.1: the plugin's unit tests are a host run now, not a `docker run`
        # against the built image.
        if "pytest" in argv:
            return StageName.TEST
        if argv[:2] == ["docker", "version"]:
            return "docker_probe"
        if argv[:2] == ["docker", "build"]:
            return StageName.BUILD
        if argv[:2] == ["docker", "run"]:
            return StageName.TEST
        if argv[0] == "insight-plugin":
            sub = argv[1] if len(argv) > 1 else ""
            return f"insight-plugin:{sub}"
        # The validate stage drives icon_validator's validator list under the
        # toolchain's interpreter rather than shelling `insight-plugin validate`,
        # so the one validator that needs a plugins-repo git clone can be skipped
        # instead of crashing the run and suppressing every failure with it.
        if argv[1:2] == ["-c"] and "icon_validator" in argv[2]:
            return StageName.VALIDATE
        return "unknown"

    def install(self, monkeypatch):
        """Patch ``asyncio.create_subprocess_exec`` to this harness."""
        harness = self

        async def fake_exec(*command, stdin=None, stdout=None, stderr=None, cwd=None, **kwargs):
            argv = [str(part) for part in command]
            harness.dispatched.append((argv, cwd))
            return harness._respond(argv)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        return self

    def _respond(self, argv):
        """Return a scripted fake process for ``argv`` (all succeed)."""
        kind = self.classify(argv)
        if kind == "kiro":
            stdout = f"{_KIRO_CONTENT}\ntotal_tokens: {_KIRO_REPORTED_TOKENS}\n".encode("utf-8")
            return _FakeProcess(returncode=0, stdout=stdout)
        if kind == "agent":
            return _FakeProcess(returncode=0, stdout=_AGENT_TRANSCRIPT.encode("utf-8"))
        if kind == "docker_probe":
            return _FakeProcess(returncode=0, stdout=b"Docker version 25.0.0")
        # Every insight-plugin / docker / prospector stage passes cleanly.
        if kind == StageName.LINT:
            # Prospector JSON, because the lint stage reads its verdict from the
            # findings rather than from the exit code.
            return _FakeProcess(returncode=0, stdout=b'{"messages": []}')
        return _FakeProcess(returncode=0, stdout=f"{kind} ok".encode("utf-8"))

    # -- assertion helpers --------------------------------------------------

    def kinds(self):
        """The classified kinds of every dispatched command, in launch order."""
        return [self.classify(argv) for argv, _ in self.dispatched]

    def count(self, kind):
        """How many dispatched commands classified as ``kind``."""
        return self.kinds().count(kind)


class FakeTenantUploader:
    """A fake InsightConnect tenant API standing in for the real upload.

    Injected into a real :class:`ExportManager` so :meth:`export_tenant` runs its
    genuine pre-network guards and result classification without contacting any
    network. Records each upload so a test can assert the artifact reached the
    (mocked) tenant.
    """

    def __init__(self, *, status_code=200):
        self.status_code = status_code
        self.calls = []

    def upload(self, *, region_base_url, api_key, artifact_path, timeout):
        self.calls.append(
            {
                "region_base_url": region_base_url,
                "api_key": api_key,
                "artifact_path": Path(artifact_path),
                "timeout": timeout,
            }
        )
        return UploadResponse(status_code=self.status_code, body="ok")


# --- spec / project helpers ------------------------------------------------


def _make_spec(name="acme_widget", vendor="acme", version=SemVer(1, 0, 0), actions=None):
    """A minimal spec that passes the real Spec_Validator."""
    return PluginSpec(
        name=name,
        title="Acme Widget",
        description="A plugin used by the end-to-end integration test.",
        version=version,
        vendor=vendor,
        connection={"api_key": FieldSchema(type="string", required=True, title="API Key")},
        actions=actions or {},
    )


def _make_action(title="List widgets"):
    return Component(title=title, description="Lists widgets.", input={}, output={})


def _seed_project(projects_root, *, name="acme_widget", vendor="acme"):
    """Create a Project_Folder on disk so iterate mode can load it (the 'create')."""
    spec = _make_spec(name=name, vendor=vendor)
    folder = ProjectFolder.create(projects_root, name, spec)
    # Persist a couple of files so the working tree is non-empty for packaging, and
    # a unit test so the host test stage has something to run: the stage fails closed
    # on a tree with no tests (clause 2.3), which is asserted on its own elsewhere.
    folder.save(
        spec,
        generated_files={
            "README.md": "hello\n",
            "plugin.py": "# plugin\n",
            "unit_test/test_plugin.py": "def test_ok():\n    assert True\n",
        },
    )
    return folder


def _wire_real_stack(projects_root, *, uploader):
    """Wire an Orchestrator from the REAL components with externals mocked.

    Only the process/network boundary is faked (via the patched subprocess and
    the injected ``uploader``); every collaborator here is the production class,
    so this is a true end-to-end assembly rather than a stub graph.
    """
    cost = CostController()
    orch = Orchestrator(
        cost_controller=cost,
        llm_generator=LLMGenerator(cost, executable="kiro"),
        plugin_agent=PluginAgent(cost, executable="kiro-cli"),
        refresh_coordinator=RefreshCoordinator(cli=InsightPluginCli("insight-plugin")),
        code_validator=CodeValidator(
            prospector_executable="prospector",
            docker_executable="docker",
            insight_plugin_executable="insight-plugin",
        ),
        build_engine=BuildEngine(),
        export_manager=ExportManager(uploader=uploader),
        registry=PluginRegistry(str(projects_root / "registry.db")),
        audit_log=AuditLog(projects_root / "audit.log"),
        projects_root=projects_root,
    )
    return orch, cost


def _add_action_turn(name, *, with_llm_logic=True):
    """A turn that adds an action and (optionally) requests LLM action logic."""
    reasoning = []
    if with_llm_logic:
        reasoning.append(GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, pattern=None, parameters={"action": name}))
    return TurnPlan(
        operations=[AddComponent(ComponentKind.ACTION, name, _make_action())],
        reasoning=reasoning,
    )


# --- the end-to-end scenario ----------------------------------------------


class TestEndToEndCreateGenerateValidateBuildExport:
    """create -> generate -> validate -> build -> export (local and tenant).

    The whole flow runs against the real stack with the Kiro CLI, the
    ``insight-plugin`` CLI, Docker, and the tenant API mocked (Req 20.1, 20.4).
    """

    def test_full_flow_local_then_tenant_export_with_all_externals_mocked(self, tmp_path, monkeypatch):
        externals = MockedExternalProcesses().install(monkeypatch)
        uploader = FakeTenantUploader(status_code=200)

        # -- create: a Project_Folder exists on disk; open it in iterate mode.
        _seed_project(tmp_path, name="acme_widget", vendor="acme")
        orch, cost = _wire_real_stack(tmp_path, uploader=uploader)
        orch.start_session(
            ENTRY_MODE_ITERATE_CUSTOM,
            session_id="s1",
            user_id="u1",
            plugin_name="acme_widget",
        )

        # -- generate: a structural turn drives the REAL refresh coordinator
        # (insight-plugin refresh, mocked) and the REAL PluginAgent (kiro-cli,
        # mocked), gated and recorded by the real CostController.
        result = asyncio.run(orch.apply_turn("s1", _add_action_turn("list_widgets")))
        assert result.status is TurnStatus.APPLIED
        assert result.refreshed is True  # structural edit -> insight-plugin refresh (Req 22.3)
        assert externals.count("insight-plugin:refresh") == 1

        # Implementation was delegated to the Kiro CLI run as an agent: exactly
        # one agent run, in the plugin's own project folder, with the prompt on
        # stdin rather than in argv.
        assert externals.count("agent") == 1
        agent_argv, agent_cwd = next(
            (argv, cwd) for argv, cwd in externals.dispatched if MockedExternalProcesses.classify(argv) == "agent"
        )
        assert "--no-interactive" in agent_argv
        assert any(part.startswith("--trust-tools=") for part in agent_argv)
        assert not any("list_widgets" in part for part in agent_argv)  # task is on stdin, not argv
        assert str(agent_cwd).endswith("acme_widget")

        # The turn reports the agent's own closing summary, and the run was
        # recorded on the cumulative session total (Req 3.5, 3.6).
        assert "insight-plugin validate" in result.message
        assert result.token_total > 0
        # No code was requested from the single-shot LLM path.
        assert externals.count("kiro") == 0

        # -- validate + build + export are driven through the REAL FastAPI app,
        # the same HTTP surface the packaged UI calls (Req 20.1).
        client = TestClient(create_app(orchestrator=orch, registry=orch._registry))

        # prepare_export runs the real Spec_Validator plus the real four-stage
        # Code_Validator (prospector + docker build/run + insight-plugin validate,
        # all mocked) and the real Build_Engine preview.
        plan = client.post("/api/session/s1/export/prepare").json()
        assert plan["permitted"] is True  # spec valid AND all four stages passed (Req 7.4, 8.6, 8.7)
        assert "plugin.spec.yaml" in plan["file_list"]
        assert plan["diff"]["first_version"] is True  # no prior export yet (Req 16.4)
        # The four validation stages each ran exactly once.
        assert externals.count(StageName.LINT) == 1
        assert externals.count(StageName.BUILD) == 1
        assert externals.count(StageName.TEST) == 1
        assert externals.count(StageName.VALIDATE) == 1

        # -- export locally: the real Build_Engine packages a genuine .plg and no
        # tenant upload is required (Req 20.4, 9).
        out_dir = tmp_path / "out"
        local = client.post(
            "/api/session/s1/export/confirm",
            json={"confirmed": True, "target": "local", "output_dir": str(out_dir)},
        ).json()
        assert local["status"] == "succeeded"
        assert local["version"] == "1.0.0"
        artifact_path = Path(local["artifact_path"])
        assert artifact_path.is_file()
        # The produced artifact is a real gzipped tarball carrying the spec.
        assert tarfile.is_tarfile(artifact_path)
        with tarfile.open(artifact_path, "r:gz") as tar:
            names = tar.getnames()
        assert "plugin.spec.yaml" in names
        # The _custom vendor suffix was applied at export (Req 13.3).
        assert client.get("/api/session/s1").json()["spec"]["vendor"] == "acme_custom"
        # No tenant upload happened on the local path.
        assert uploader.calls == []

        # -- iterate again, then export to the (mocked) tenant. A non-breaking
        # addition bumps the patch version against the recorded 1.0.0 export.
        result2 = asyncio.run(orch.apply_turn("s1", _add_action_turn("get_widget", with_llm_logic=False)))
        assert result2.status is TurnStatus.APPLIED

        plan2 = client.post("/api/session/s1/export/prepare").json()
        assert plan2["permitted"] is True
        assert plan2["version_display"] == "1.0.0 -> 1.0.1"  # patch bump (Req 12.4)
        assert plan2["diff"]["first_version"] is False

        tenant = client.post(
            "/api/session/s1/export/confirm",
            json={
                "confirmed": True,
                "target": "tenant",
                "region_base_url": "https://us.api.insight.rapid7.com",
                "api_key": "secret-key",
            },
        ).json()
        assert tenant["status"] == "succeeded"
        assert tenant["target"] == "https://us.api.insight.rapid7.com"
        assert tenant["version"] == "1.0.1"

        # The upload reached the (mocked) tenant API exactly once, with the built
        # artifact and the supplied region -- no real network was contacted.
        assert len(uploader.calls) == 1
        assert uploader.calls[0]["region_base_url"] == "https://us.api.insight.rapid7.com"
        assert uploader.calls[0]["artifact_path"].suffix == ".plg"

        # -- persistence accumulated both exports, most-recent-first (Req 11.4).
        history = client.get("/api/plugins/acme_widget/history").json()["history"]
        exported = [h["version"] for h in history if h["kind"] == "export"]
        assert exported == ["1.0.1", "1.0.0"]

        # The audit log recorded both builds, the tenant credential use, and its
        # hash chain verifies intact (Req 18).
        audit = AuditLog(tmp_path / "audit.log")
        events = [r.event for r in audit.records()]
        assert events.count(AuditEvent.BUILD) == 2
        assert AuditEvent.CREDENTIAL_USE in events
        assert audit.verify().valid is True

    def test_tenant_upload_failure_is_classified_and_registry_unchanged(self, tmp_path, monkeypatch):
        """A failing (mocked) tenant API yields an export failure, artifact retained.

        Exercises the failure half of the export path end-to-end (Req 19.2, 19.4,
        10.3): the real Export_Manager classifies the mocked 500 as an *export*
        (not build) failure, retains the built ``.plg`` for retry, and leaves the
        registry export history unchanged.
        """
        MockedExternalProcesses().install(monkeypatch)
        uploader = FakeTenantUploader(status_code=500)

        _seed_project(tmp_path, name="acme_widget", vendor="acme")
        orch, _ = _wire_real_stack(tmp_path, uploader=uploader)
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="acme_widget")
        asyncio.run(orch.apply_turn("s1", _add_action_turn("list_widgets", with_llm_logic=False)))

        client = TestClient(create_app(orchestrator=orch, registry=orch._registry))
        assert client.post("/api/session/s1/export/prepare").json()["permitted"] is True

        outcome = client.post(
            "/api/session/s1/export/confirm",
            json={
                "confirmed": True,
                "target": "tenant",
                "region_base_url": "https://us.api.insight.rapid7.com",
                "api_key": "secret-key",
            },
        ).json()

        assert outcome["status"] == "export_failed"  # export failure, not a build failure (Req 19.4)
        assert outcome["retained_artifact_path"]  # artifact retained for retry (Req 19.2)
        assert Path(outcome["retained_artifact_path"]).is_file()
        # A failed upload leaves the registry export history unchanged (Req 10.3).
        assert orch._registry.exports("acme_widget") == []
