"""Integration tests over the whole export preview (bugfix task 12).

Each of the four changes this bugfix made was accepted against its own unit and
property tests. These exercise the *assembled* preview instead: a real orchestrator,
a real ``Quality_Gate`` running the real prospector and black, a real host test run,
and the real ``export/prepare`` path -- with only the process boundaries that need a
Docker daemon or a paid model substituted.

The reason for testing the assembly separately is that every defect this bugfix
closed lived in the seams. The preview judged a spec the agent had superseded; the
lint stage and the gate judged the same code by different linters; the test stage
asked an image that could not answer; the payload dropped what the stages printed.
None of those is visible from inside one component.

What is faked, and why each is a process boundary rather than a behaviour:

* the Docker daemon -- the ``build`` stage and the ``insight-plugin`` validators;
* the delegated agent -- a stand-in that writes a spec, because the alternative is a
  paid model call;
* the interpreter, in the websocket test, since the route's own decision about what
  to announce is what is being measured.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

from icplugin_builder.api.app import _serialize_export_plan, create_app
from icplugin_builder.core.plugin_files import UNIT_TEST_DIR
from icplugin_builder.core.spec_completeness import check_completeness
from icplugin_builder.core.yaml_codec import load_plugin_spec
from icplugin_builder.integrations import code_validator as cv
from icplugin_builder.integrations.build_prep import (
    PLUGIN_LINE_LENGTH,
    SDK_IMPORT_MODULE,
    TEST_RUNNER_MODULE,
    resolve_lint_profile,
    resolve_test_interpreter,
)
from icplugin_builder.integrations.code_validator import CodeValidator, StageName, StageStatus
from icplugin_builder.integrations.definition_of_done import CONDITION_UNIT_TESTS, ConditionStatus, evaluate_done
from icplugin_builder.integrations.plugin_agent import AgentRunResult
from icplugin_builder.integrations.quality_gate import QualityGate
from icplugin_builder.orchestrator import Orchestrator, TurnPlan, TurnStatus
from icplugin_builder.persistence.project_folder import ENTRY_MODE_ITERATE_CUSTOM, ProjectFolder
from icplugin_builder.persistence.registry import PluginRegistry

# ---------------------------------------------------------------------------
# Fakes at the process boundaries
# ---------------------------------------------------------------------------


class _FakeProcess:
    """A stand-in for what ``create_subprocess_exec`` returns."""

    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> Tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:  # pragma: no cover - only reached on a timeout
        self.returncode = -9

    async def wait(self) -> int:  # pragma: no cover - only reached on a timeout
        return self.returncode


def _mock_docker_only(monkeypatch, *, dispatched: Optional[List[List[str]]] = None) -> None:
    """Fake the Docker daemon and the toolchain validators, and nothing else.

    prospector, black and pytest run for real: they are the bar this bugfix moved,
    and a fake would be asserting our idea of what they say. Only the two stages that
    need an engine or the plugin toolchain installed are substituted, which is what
    lets this run on a host with neither.
    """
    real_exec = cv.asyncio.create_subprocess_exec

    async def fake_exec(*command, cwd=None, stdout=None, stderr=None):
        argv = [str(part) for part in command]
        if dispatched is not None:
            dispatched.append(argv)
        if argv[:1] == ["docker"]:
            return _FakeProcess(returncode=0, stdout=b"Docker version 25.0.0")
        if argv[1:2] == ["-c"] and "icon_validator" in argv[2]:
            return _FakeProcess(returncode=0, stdout=b"validators: 20 ran of 21 available")
        return await real_exec(*command, cwd=cwd, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(cv.asyncio, "create_subprocess_exec", fake_exec)


class _SpecWritingAgent:
    """The delegated agent, replaced by the one effect this preview is about.

    The real agent writes the tree; what the preview reads from it is
    ``plugin.spec.yaml``. Substituting the process rather than the result is what
    keeps this an integration test of the preview instead of a test of a mock.
    """

    def __init__(self, spec_text: str, *, block_seconds: float = 0.0) -> None:
        self._spec_text = spec_text
        self._block_seconds = block_seconds
        self.calls = 0
        self.entered_at: Optional[float] = None
        self.left_at: Optional[float] = None

    async def implement(self, root, instruction, *, session_id: str, user_id: str) -> AgentRunResult:
        self.entered_at = time.monotonic()
        self.calls += 1
        (Path(root) / "plugin.spec.yaml").write_text(self._spec_text, encoding="utf-8")
        if self._block_seconds:
            await asyncio.sleep(self._block_seconds)
        self.left_at = time.monotonic()
        return AgentRunResult(succeeded=True, summary="implemented the actions and their tests", transcript="")


class _StubInterpreter:
    """Returns a prepared plan, so the route's own announcements are what is measured."""

    def __init__(self, plan: TurnPlan) -> None:
        self._plan = plan
        self.calls = 0

    async def interpret(self, text, spec, *, attachments=None, session_id=None, user_id=None) -> TurnPlan:
        self.calls += 1
        return self._plan


# ---------------------------------------------------------------------------
# Tree fixtures
# ---------------------------------------------------------------------------

PACKAGE = "icon_acme"

_COMPLETE_SPEC = """plugin_spec_version: v2
extension: plugin
products: [insightconnect]
name: acme
title: Acme
description: Automate Acme.
version: 1.0.0
cloud_ready: false
vendor: rapid7
support: rapid7
status: []
supported_versions: ["2026-01-01"]
sdk:
  type: full
  version: 6.6.0
  user: nobody
key_features: ["Look things up"]
requirements: ["An API key"]
version_history:
  - 1.0.0 - Initial plugin release
resources:
  source_url: https://example.invalid/src
  license_url: https://example.invalid/license
  vendor_url: https://example.invalid
links: ["[Acme](https://example.invalid)"]
references: ["[API](https://example.invalid/api)"]
tags: [acme]
hub_tags:
  use_cases: [threat_detection_and_response]
  keywords: [acme]
  features: []
connection:
  api_key:
    title: API Key
    type: credential_secret_key
    required: true
actions:
  get_thing:
    title: Get Thing
    description: Retrieve a thing.
    input:
      thing_id:
        title: Thing ID
        type: string
        example: "42"
        required: true
    output:
      thing:
        title: Thing
        type: object
        example: {"id": "42"}
        required: false
"""

_CLEAN_CLIENT = '''import requests
from .constants import HTTP_ERROR_MAP


class AcmeApi:
    """A client with the shape the rulebook names."""

    def __init__(self, api_key):
        self._api_key = api_key

    def get_thing(self, thing_id):
        """One domain method per action."""
        return self._make_request("GET", f"things/{thing_id}")

    def _make_request(self, method, path, **kwargs):
        response = requests.request(method, f"https://api.example.invalid/{path}", timeout=60, **kwargs)
        if response.status_code in HTTP_ERROR_MAP:
            raise ValueError(HTTP_ERROR_MAP[response.status_code])
        return response.json()
'''

#: The defect the plugins repository's own profile reports: `requests` used, never
#: imported. A plugin shipped this way dies with a NameError on first run.
_DEFECTIVE_CLIENT = '''from .constants import HTTP_ERROR_MAP


class AcmeApi:
    """A client that reaches for requests without importing it."""

    def get_thing(self, thing_id):
        return self._make_request("GET", f"things/{thing_id}")

    def _make_request(self, method, path, **kwargs):
        response = requests.request(method, f"https://api.example.invalid/{path}", **kwargs)
        if response.status_code in HTTP_ERROR_MAP:
            raise ValueError(HTTP_ERROR_MAP[response.status_code])
        return response.json()
'''


def _plugin_tree(root: Path, *, client: str = _CLEAN_CLIENT, tests_pass: bool = True) -> Path:
    """Materialize a plugin tree the preview can be run over.

    Created through :class:`ProjectFolder` rather than by hand, because that is what
    puts the ``.builder/`` metadata there and iterate mode opens a project folder.
    """
    spec = load_plugin_spec(_COMPLETE_SPEC)
    ProjectFolder.create(root.parent, root.name, spec)
    package = root / PACKAGE
    (package / "util").mkdir(parents=True, exist_ok=True)
    (package / "actions" / "get_thing").mkdir(parents=True, exist_ok=True)
    (package / "connection").mkdir(parents=True, exist_ok=True)
    (root / UNIT_TEST_DIR).mkdir(parents=True, exist_ok=True)

    # Generated by `insight-plugin create` in a real tree; without them a relative
    # import is genuinely beyond the top-level package and prospector says so.
    for init in (
        package / "__init__.py",
        package / "util" / "__init__.py",
        package / "actions" / "__init__.py",
        package / "actions" / "get_thing" / "__init__.py",
        package / "connection" / "__init__.py",
    ):
        init.write_text("", encoding="utf-8")

    (root / "plugin.spec.yaml").write_text(_COMPLETE_SPEC, encoding="utf-8")
    (root / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    (package / "util" / "api.py").write_text(client, encoding="utf-8")
    (package / "util" / "constants.py").write_text(
        'HTTP_ERROR_MAP = {401: "The API key is invalid.", 404: "Not found."}\n', encoding="utf-8"
    )
    (package / "connection" / "connection.py").write_text(
        "class Connection:\n"
        "    def connect(self, params):\n"
        '        self.key = params.get("api_key")\n'
        "\n"
        "    def test(self):\n"
        '        self.client.get_thing("1")\n'
        '        return {"success": True}\n',
        encoding="utf-8",
    )
    (package / "actions" / "get_thing" / "action.py").write_text(
        "class GetThing:\n"
        "    def run(self, params):\n"
        '        return self.connection.client.get_thing(params["thing_id"])\n',
        encoding="utf-8",
    )
    assertion = "assert True" if tests_pass else "assert False"
    (root / UNIT_TEST_DIR / "test_get_thing.py").write_text(
        f"def test_get_thing():\n    {assertion}\n", encoding="utf-8"
    )
    return root


def _orchestrator(root: Path, *, agent=None, test_python: Optional[str] = None) -> Orchestrator:
    """Wire an orchestrator over ``root`` the way ``api/app.py`` wires one."""
    interpreter = test_python or resolve_test_interpreter().executable
    if interpreter is None:
        pytest.skip(
            f"no interpreter available here can import both {SDK_IMPORT_MODULE} and {TEST_RUNNER_MODULE}, "
            "so the preview's test stage cannot report on the plugin rather than on the host"
        )
    return Orchestrator(
        projects_root=root.parent,
        plugin_agent=agent,
        registry=PluginRegistry(str(root.parent / "registry.db")),
        quality_gate=QualityGate(python_executable=interpreter),
        code_validator=CodeValidator(
            prospector_executable="prospector",
            test_python_executable=interpreter,
            validate_python_executable=interpreter,
        ),
    )


def _require_linter() -> None:
    """Skip when prospector is absent; an absent linter measures nothing (2.9)."""
    import shutil

    if shutil.which("prospector") is None:
        pytest.skip("prospector is not on PATH, so the lint stage cannot report on this plugin")


# ---------------------------------------------------------------------------
# 12.1 -- the whole preview, for a plugin that is actually finished
# ---------------------------------------------------------------------------


class TestTheWholePreviewForAFinishedPlugin:
    """Task 12.1 -- a correct plugin clears the gate, and the report says on what.

    This is the case the originating run got wrong in three independent ways at once:
    the preview judged a superseded spec, the lint stage failed on files the author
    may not edit, and the test stage asked an image that carried neither the tests nor
    pytest. All three had to be right simultaneously for an export to be permitted,
    which is why they are asserted together here rather than only apart.
    """

    def test_the_preview_permits_the_export_and_names_the_bar_it_applied(self, tmp_path, monkeypatch):
        _require_linter()
        root = _plugin_tree(tmp_path / "acme")
        agent = _SpecWritingAgent(_COMPLETE_SPEC)
        orch = _orchestrator(root, agent=agent)
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="acme")
        _mock_docker_only(monkeypatch)

        from icplugin_builder.core.generation import ArtifactKind, GenerationRequest

        turn = asyncio.run(
            orch.submit_message(
                "s1",
                "Implement get_thing.",
                TurnPlan(
                    reasoning=[GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, parameters={"action": "get_thing"})]
                ),
            )
        )
        assert turn.status is TurnStatus.APPLIED, turn.message
        assert agent.calls == 1, "the turn did not delegate, so this is not a delegated preview"

        plan = asyncio.run(orch.prepare_export("s1"))
        payload = _serialize_export_plan(plan)

        # 2.2: every stage passed, so no `force` is needed.
        assert plan.permitted is True, (
            f"a finished plugin needs force to export. Stages: "
            f"{[(s.name, s.status.value, s.message) for s in plan.pipeline_report.stages]}"
        )
        assert payload["failed_stages"] == []

        # 2.12: the preview describes the spec on disk, not the session's draft.
        on_disk = load_plugin_spec((root / "plugin.spec.yaml").read_text(encoding="utf-8"))
        assert plan.spec_preview.name == on_disk.name
        assert sorted(plan.spec_preview.actions) == sorted(on_disk.actions)
        assert not plan.completeness.findings, [f.message for f in plan.completeness.findings]
        assert not check_completeness(plan.spec_preview).findings

        # 2.8: the profile and the width that produced the verdict are reported.
        profile = resolve_lint_profile()
        assert payload["lint_bar"]["profile_path"] == str(profile.path)
        assert payload["lint_bar"]["line_length"] == PLUGIN_LINE_LENGTH

        # 2.3: and the interpreter the tests ran under is named.
        test_stage = plan.pipeline_report.stage(StageName.TEST)
        assert test_stage.status is StageStatus.PASSED, test_stage.message
        assert str(orch._quality_gate._python) in test_stage.message

    def test_the_tests_are_executed_once_for_the_whole_preview(self, tmp_path, monkeypatch):
        """2.4 -- one execution, one interpreter, one verdict."""
        _require_linter()
        root = _plugin_tree(tmp_path / "acme")
        orch = _orchestrator(root)
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="acme")
        dispatched: List[List[str]] = []
        _mock_docker_only(monkeypatch, dispatched=dispatched)

        asyncio.run(orch.prepare_export("s1"))

        pytest_runs = [argv for argv in dispatched if "pytest" in argv and "-m" in argv]
        assert len(pytest_runs) == 1, f"the suite ran {len(pytest_runs)} times for one preview: {pytest_runs}"


# ---------------------------------------------------------------------------
# 12.2 -- a blocked preview says what failed
# ---------------------------------------------------------------------------


class TestABlockedPreviewReportsEveryFailure:
    """Task 12.2 -- two genuine defects, both reported with their output (2.16).

    The defects are real rather than injected into a report: `requests` used and never
    imported, which is what the plugins repository's own profile reports and what a
    plugin actually shipped; and a unit test that fails.
    """

    def test_both_failing_stages_appear_with_their_output(self, tmp_path, monkeypatch):
        _require_linter()
        root = _plugin_tree(tmp_path / "acme", client=_DEFECTIVE_CLIENT, tests_pass=False)
        orch = _orchestrator(root)
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="acme")
        _mock_docker_only(monkeypatch)

        plan = asyncio.run(orch.prepare_export("s1"))
        payload = _serialize_export_plan(plan)
        entries = {entry["name"]: entry for entry in payload["failed_stages"]}

        assert plan.permitted is False
        assert {StageName.LINT, StageName.TEST} <= set(
            entries
        ), f"a tree with a hand-written lint defect and a failing test reports {sorted(entries)} as failing"
        assert "undefined-variable" in entries[StageName.LINT]["displayed_output"], entries[StageName.LINT]
        assert "test_get_thing" in (
            entries[StageName.TEST]["displayed_output"] + entries[StageName.TEST]["message"]
        ), entries[StageName.TEST]

    def test_the_stages_that_pass_are_not_reported_as_failing(self, tmp_path, monkeypatch):
        """Nothing that actually passed is dragged into needing `force`."""
        _require_linter()
        root = _plugin_tree(tmp_path / "acme", client=_DEFECTIVE_CLIENT, tests_pass=False)
        orch = _orchestrator(root)
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="acme")
        _mock_docker_only(monkeypatch)

        plan = asyncio.run(orch.prepare_export("s1"))
        failing = {entry["name"] for entry in _serialize_export_plan(plan)["failed_stages"]}
        assert StageName.BUILD not in failing
        assert StageName.VALIDATE not in failing


# ---------------------------------------------------------------------------
# 12.3 -- the split-interpreter host
# ---------------------------------------------------------------------------


class TestASplitInterpreterHostFailsClosed:
    """Task 12.3 -- the one Bug 1 case that needs the SDK and pytest separated.

    On the reproduction host the SDK lived in the Command Line Tools 3.9 with no
    pytest, and the project virtualenv had pytest and no SDK. Neither can run a
    plugin's unit tests, and a tool that assumed one interpreter would report the
    host's problem as the plugin's. This is why clause 2.3 exists, and it completes
    Property 67's coverage, which task 9.8 left to this test.
    """

    @staticmethod
    def _interpreter(directory: Path, name: str, *, importable: Tuple[str, ...]) -> str:
        """A stand-in interpreter that can import only ``importable``."""
        script = directory / name
        runs_pytest = TEST_RUNNER_MODULE in importable
        script.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            + "".join(f"  *import\\ {module}*) exit 0 ;;\n" for module in importable)
            + "  *import*) exit 1 ;;\n"
            # Asked to *run* pytest, an interpreter without it says so and exits
            # non-zero, exactly as a real one does. Without this the stand-in would
            # look like an interpreter whose suite passed.
            + (
                "  *-m\\ pytest*) echo 'no tests ran' ; exit 5 ;;\n"
                if runs_pytest
                else "  *-m\\ pytest*) echo 'No module named pytest' >&2 ; exit 1 ;;\n"
            )
            + "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return str(script)

    def test_neither_split_candidate_qualifies(self, tmp_path):
        """The premise, measured rather than assumed."""
        sdk_only = self._interpreter(tmp_path, "python-sdk", importable=(SDK_IMPORT_MODULE,))
        pytest_only = self._interpreter(tmp_path, "python-pytest", importable=(TEST_RUNNER_MODULE,))

        resolution = resolve_test_interpreter(candidates=(sdk_only, pytest_only))

        assert not resolution.resolved
        missing = {rejection.executable: rejection.missing for rejection in resolution.rejections}
        assert missing == {sdk_only: (TEST_RUNNER_MODULE,), pytest_only: (SDK_IMPORT_MODULE,)}

    def test_the_stage_fails_closed_and_names_the_interpreter(self, tmp_path):
        """2.3 -- never a quiet pass, and the message says what was tried."""
        sdk_only = self._interpreter(tmp_path, "python-sdk", importable=(SDK_IMPORT_MODULE,))
        root = _plugin_tree(tmp_path / "acme")

        validator = CodeValidator(test_python_executable=sdk_only)
        spec = next(item for item in validator._stage_specs("tag", root) if item.name == StageName.TEST)
        stage = asyncio.run(validator._run_stage(spec, root))

        assert stage.status is StageStatus.FAILED, "an unrunnable test run must never read as a pass"
        assert sdk_only in stage.message
        assert TEST_RUNNER_MODULE in stage.message

    def test_the_definition_of_done_reports_unverified_at_the_same_time(self, tmp_path):
        """3.6 -- the gate fails closed while the advisory report stays honest.

        Two different questions, and the split host is where they visibly differ: the
        four-stage gate has no third state, so it must fail; the Definition_Of_Done
        can say "could not be checked", so it must.
        """
        # An interpreter with the SDK and no pytest: it *cannot run* the tests, which
        # is unverifiable. One with pytest but no tests to collect is a different and
        # genuinely unmet condition, so it would not exercise this pairing.
        sdk_only = self._interpreter(tmp_path, "python-sdk", importable=(SDK_IMPORT_MODULE,))
        root = _plugin_tree(tmp_path / "acme")

        report = asyncio.run(QualityGate(python_executable=sdk_only, run_tests=True).run(root))
        done = evaluate_done(root, quality_report=report)
        condition = next(item for item in done.conditions if item.name == CONDITION_UNIT_TESTS)

        validator = CodeValidator(test_python_executable=sdk_only)
        spec = next(item for item in validator._stage_specs("tag", root) if item.name == StageName.TEST)
        stage = asyncio.run(validator._run_stage(spec, root))

        assert stage.status is StageStatus.FAILED
        assert condition.status is ConditionStatus.UNVERIFIED, (
            f"{CONDITION_UNIT_TESTS} reads {condition.status.value} where the tests could not be run; "
            "an unverifiable condition must not read as met (27.5) nor as a defect"
        )

    def test_pytest_is_never_installed_to_make_the_run_possible(self, tmp_path):
        """SCOPE-12 -- the absence is reported with remediation, not remedied."""
        recorder = tmp_path / "python-recorder"
        log = tmp_path / "argv.log"
        # Records what it was asked to do, and answers as an interpreter without
        # pytest answers -- so the stage reaches its remediation path rather than
        # reading the run as a plugin whose tests failed.
        recorder.write_text(
            f'#!/bin/sh\necho "$@" >> {log}\necho "No module named pytest" >&2\nexit 1\n',
            encoding="utf-8",
        )
        recorder.chmod(0o755)
        root = _plugin_tree(tmp_path / "acme")

        validator = CodeValidator(test_python_executable=str(recorder))
        spec = next(item for item in validator._stage_specs("tag", root) if item.name == StageName.TEST)
        stage = asyncio.run(validator._run_stage(spec, root))

        recorded = log.read_text(encoding="utf-8") if log.exists() else ""
        assert "install" not in recorded and "pip" not in recorded, recorded
        assert "Install pytest" in stage.message, stage.message


# ---------------------------------------------------------------------------
# 12.4 -- progress over the real websocket route
# ---------------------------------------------------------------------------


class TestProgressOverTheWebsocket:
    """Task 12.4 -- the operator can tell a long run from a hang, and is not misled.

    Driven through the production route with a real websocket client. The delegated
    run is held open for a known interval so the claim is about frames arriving
    *during* it rather than around it.
    """

    BLOCK_SECONDS = 3.0

    def _drive(
        self, orchestrator: Orchestrator, plan: TurnPlan, *, text: str
    ) -> Tuple[List[Tuple[float, Dict[str, Any]]], float]:
        app = create_app(orchestrator=orchestrator, interpreter=_StubInterpreter(plan))
        entries: List[Tuple[float, Dict[str, Any]]] = []
        with TestClient(app) as client:
            with client.websocket_connect("/ws/s1") as websocket:
                assert websocket.receive_json().get("type") == "state"
                started = time.monotonic()
                websocket.send_json({"type": "submit_message", "text": text})
                for _ in range(60):
                    frame = websocket.receive_json()
                    entries.append((time.monotonic() - started, frame))
                    if frame.get("type") in ("visualization", "error"):
                        break
        return entries, started

    @staticmethod
    def _statuses(entries) -> List[str]:
        return [frame["message"] for _, frame in entries if frame.get("type") == "status"]

    def test_frames_arrive_while_the_delegated_run_is_in_flight(self, tmp_path, monkeypatch):
        """2.19 -- the operator's question is whether anything is happening."""
        root = _plugin_tree(tmp_path / "acme")
        agent = _SpecWritingAgent(_COMPLETE_SPEC, block_seconds=self.BLOCK_SECONDS)
        orch = _orchestrator(root, agent=agent)
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="acme")
        _mock_docker_only(monkeypatch)

        from icplugin_builder.core.generation import ArtifactKind, GenerationRequest

        entries, started = self._drive(
            orch,
            TurnPlan(reasoning=[GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, parameters={"action": "get_thing"})]),
            text="Implement get_thing.",
        )

        assert agent.calls == 1
        during = [
            frame["message"]
            for elapsed, frame in entries
            if frame.get("type") == "status" and agent.entered_at + 0.25 <= started + elapsed <= agent.left_at - 0.25
        ]
        assert during, f"nothing arrived during a {self.BLOCK_SECONDS}s delegated run: {self._statuses(entries)}"

    def test_no_silence_exceeds_the_reporting_interval_by_much(self, tmp_path, monkeypatch):
        """The same claim as a bound on the widest gap rather than as presence."""
        root = _plugin_tree(tmp_path / "acme")
        agent = _SpecWritingAgent(_COMPLETE_SPEC, block_seconds=self.BLOCK_SECONDS)
        orch = _orchestrator(root, agent=agent)
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="acme")
        _mock_docker_only(monkeypatch)

        from icplugin_builder.core.generation import ArtifactKind, GenerationRequest

        entries, _ = self._drive(
            orch,
            TurnPlan(reasoning=[GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, parameters={"action": "get_thing"})]),
            text="Implement get_thing.",
        )

        gaps = [second - first for (first, _), (second, _) in zip(entries, entries[1:])]
        assert gaps, "only one frame arrived"
        assert max(gaps) < self.BLOCK_SECONDS, (
            f"the widest silence was {max(gaps):.2f}s across a {self.BLOCK_SECONDS}s delegated run: "
            f"{self._statuses(entries)}"
        )

    def test_every_status_frame_names_a_step(self, tmp_path, monkeypatch):
        """2.19 asks for the current step, not merely a heartbeat."""
        root = _plugin_tree(tmp_path / "acme")
        orch = _orchestrator(root, agent=_SpecWritingAgent(_COMPLETE_SPEC, block_seconds=1.0))
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="acme")
        _mock_docker_only(monkeypatch)

        from icplugin_builder.core.generation import ArtifactKind, GenerationRequest

        entries, _ = self._drive(
            orch,
            TurnPlan(reasoning=[GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, parameters={"action": "get_thing"})]),
            text="Implement get_thing.",
        )

        statuses = self._statuses(entries)
        assert statuses, "no status frame at all"
        assert all(status.strip() for status in statuses), statuses
        assert any("implementing" in status for status in statuses), statuses

    def test_a_turn_that_ends_in_a_clarification_announces_no_generation(self, tmp_path, monkeypatch):
        """2.17 -- the defect this closed: work announced that the turn declined."""
        root = _plugin_tree(tmp_path / "acme")
        orch = _orchestrator(root, agent=_SpecWritingAgent(_COMPLETE_SPEC))
        orch.start_session(ENTRY_MODE_ITERATE_CUSTOM, session_id="s1", user_id="u1", plugin_name="acme")
        _mock_docker_only(monkeypatch)

        from icplugin_builder.core.generation import ArtifactKind, GenerationRequest

        entries, _ = self._drive(
            orch,
            TurnPlan(
                reasoning=[GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, parameters={"action": "get_thing"})],
                vendor_api="Acme Cloud",
            ),
            text="Implement get_thing against the Acme API.",
        )

        turn = next((frame["result"] for _, frame in entries if frame.get("type") == "turn"), None)
        assert turn is not None, self._statuses(entries)
        assert turn["status"] == TurnStatus.CLARIFICATION.value, turn["message"][:200]
        assert not any("implementing" in status for status in self._statuses(entries)), self._statuses(entries)
        assert not any("Generating logic" in status for status in self._statuses(entries))
