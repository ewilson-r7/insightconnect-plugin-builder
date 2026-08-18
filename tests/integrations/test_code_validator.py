"""Unit tests for the four-stage Code_Validator pipeline (task 15.1; Req 8.1-8.6, 8.8).

These cover the deterministic behavior of ``CodeValidator.run_pipeline`` and its
Docker probe using a *mocked* subprocess layer (no real Docker daemon or plugin
toolchain is required). We monkeypatch ``asyncio.create_subprocess_exec`` with a
router that returns per-command fake processes, and drive the coroutines with
``asyncio.run`` so no async test plugin is needed.

The failing-stage-identification *property* (Req 8.5) and the mocked pipeline
integration test are covered separately by tasks 15.2 and 15.11.
"""

import asyncio
import json

from icplugin_builder.integrations import code_validator as cv
from icplugin_builder.integrations.build_prep import LINT_TOOLS, PLUGIN_LINE_LENGTH, LintProfile
from icplugin_builder.integrations.plugin_tests import UnitTestFailure, UnitTestRun
from icplugin_builder.integrations.code_validator import (
    DEFAULT_STAGE_TIMEOUT_SECONDS,
    DOCKER_UNAVAILABLE_MESSAGE,
    CodeValidator,
    PipelineReport,
    StageName,
    StageStatus,
)


class FakeProcess:
    """A stand-in for the object returned by ``create_subprocess_exec``.

    When ``delay`` is set, :meth:`communicate` sleeps so ``asyncio.wait_for`` in
    the validator times out (exercising the 600s abort path, Req 8.8).
    """

    def __init__(self, returncode=0, stdout=b"", stderr=b"", delay=0.0):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._delay = delay
        self.killed = False

    async def communicate(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


def install_router(monkeypatch, router, *, record=None):
    """Patch ``create_subprocess_exec`` to dispatch to ``router(command)``.

    ``router`` maps a command argv (list) to a :class:`FakeProcess`, or raises
    ``FileNotFoundError`` to simulate a missing executable. Each invocation's
    ``(command, cwd)`` is appended to ``record`` when provided.
    """

    async def fake_exec(*command, cwd=None, stdout=None, stderr=None):
        if record is not None:
            record.append((list(command), cwd))
        return router(list(command))

    monkeypatch.setattr(cv.asyncio, "create_subprocess_exec", fake_exec)


#: What prospector prints when it has nothing to report. The ``lint`` stage reads
#: its verdict from this JSON rather than from the exit code, because prospector
#: exits 0 even when it *does* report messages -- so a fake that only sets a
#: returncode would let every tree pass.
PROSPECTOR_CLEAN = b'{"messages": []}'


def prospector_findings(*messages) -> bytes:
    """Prospector JSON reporting ``(path, line, code)`` triples."""
    payload = {
        "messages": [
            {"location": {"path": path, "line": line}, "code": code, "message": f"{code} at {path}:{line}"}
            for path, line, code in messages
        ]
    }
    return json.dumps(payload).encode("utf-8")


def plugin_tree(root):
    """Give ``root`` the unit_test/ directory the host test stage needs.

    The stage fails closed on a tree with no tests (clause 2.3), which is correct
    and is asserted on its own below -- so a fixture meant to exercise the
    *all-stages-pass* path has to carry one.
    """
    (root / "unit_test").mkdir(parents=True, exist_ok=True)
    (root / "unit_test" / "test_it.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    return root


def all_pass_router(command):
    """A router where every command (docker probe + all stages) succeeds."""
    if command[0] == "prospector":
        return FakeProcess(returncode=0, stdout=PROSPECTOR_CLEAN)
    return FakeProcess(returncode=0, stdout=b"ok")


class TestDockerProbe:
    def test_available_when_docker_version_succeeds(self, monkeypatch):
        install_router(monkeypatch, lambda cmd: FakeProcess(returncode=0, stdout=b"Docker 25.0"))
        probe = asyncio.run(CodeValidator().probe_docker())
        assert probe.available is True
        assert probe.message == ""

    def test_unavailable_when_binary_missing(self, monkeypatch):
        def router(cmd):
            raise FileNotFoundError(2, "No such file or directory")

        install_router(monkeypatch, router)
        probe = asyncio.run(CodeValidator().probe_docker())
        assert probe.available is False
        assert probe.message == DOCKER_UNAVAILABLE_MESSAGE

    def test_unavailable_when_daemon_not_running(self, monkeypatch):
        install_router(monkeypatch, lambda cmd: FakeProcess(returncode=1, stderr=b"Cannot connect to the daemon"))
        probe = asyncio.run(CodeValidator().probe_docker())
        assert probe.available is False
        assert "daemon" in probe.detail


class TestRunPipelineAllPass:
    def test_reports_four_stages_in_order_and_passes(self, tmp_path, monkeypatch):
        record = []
        install_router(monkeypatch, all_pass_router, record=record)

        report = asyncio.run(CodeValidator().run_pipeline(plugin_tree(tmp_path)))

        assert isinstance(report, PipelineReport)
        assert [stage.name for stage in report.stages] == list(StageName.ORDER)
        assert all(stage.status is StageStatus.PASSED for stage in report.stages)
        assert report.passed is True
        assert report.failed_stages == ()
        assert report.docker_available is True

    def test_accepts_project_tree_like_object(self, tmp_path, monkeypatch):
        install_router(monkeypatch, all_pass_router)

        class TreeLike:
            root = tmp_path

        plugin_tree(tmp_path)
        report = asyncio.run(CodeValidator().run_pipeline(TreeLike()))
        assert report.project_dir == tmp_path
        assert report.passed is True

    def test_runs_stages_in_project_directory(self, tmp_path, monkeypatch):
        record = []
        install_router(monkeypatch, all_pass_router, record=record)

        asyncio.run(CodeValidator().run_pipeline(tmp_path))

        # The probe runs with no cwd; every stage runs inside the project dir.
        stage_cwds = [cwd for _, cwd in record if cwd is not None]
        assert stage_cwds and all(cwd == str(tmp_path) for cwd in stage_cwds)


class TestRunPipelineFailures:
    def test_nonzero_stage_records_fail_with_output(self, tmp_path, monkeypatch):
        def router(command):
            if command[0] == "prospector":
                return FakeProcess(
                    returncode=0,
                    stdout=prospector_findings(("icon_x/util/api.py", 12, "undefined-variable")),
                )
            return FakeProcess(returncode=0)

        install_router(monkeypatch, router)
        report = asyncio.run(CodeValidator().run_pipeline(plugin_tree(tmp_path)))

        lint = report.stage(StageName.LINT)
        assert lint.status is StageStatus.FAILED
        assert "undefined-variable" in lint.stdout
        assert "icon_x/util/api.py" in lint.stdout
        assert report.passed is False
        assert [s.name for s in report.failed_stages] == [StageName.LINT]

    def test_missing_stage_executable_is_a_fail(self, tmp_path, monkeypatch):
        # The validate stage runs icon_validator under the toolchain's
        # interpreter, so the executable that can go missing is that interpreter.
        def router(command):
            if command[0] == "no-such-interpreter":
                raise FileNotFoundError(2, "No such file or directory")
            return FakeProcess(returncode=0)

        install_router(monkeypatch, router)
        validator = CodeValidator(validate_python_executable="no-such-interpreter")
        report = asyncio.run(validator.run_pipeline(tmp_path))

        validate = report.stage(StageName.VALIDATE)
        assert validate.status is StageStatus.FAILED
        assert "no-such-interpreter" in validate.message
        assert report.passed is False

    def test_build_stage_timeout_aborts_with_timeout_fail(self, tmp_path, monkeypatch):
        # Build sleeps past the (tiny) stage timeout; everything else succeeds.
        def router(command):
            if command[0] == "docker" and len(command) > 1 and command[1] == "build":
                return FakeProcess(delay=10.0)
            return FakeProcess(returncode=0)

        install_router(monkeypatch, router)
        validator = CodeValidator(stage_timeout_seconds=0.05)
        report = asyncio.run(validator.run_pipeline(tmp_path))

        build = report.stage(StageName.BUILD)
        assert build.status is StageStatus.TIMED_OUT
        assert build.timed_out is True
        assert "aborted" in build.message
        assert report.passed is False


class TestDockerUnavailable:
    def test_docker_stages_fail_but_lint_still_runs(self, tmp_path, monkeypatch):
        def router(command):
            if command[0] == "docker" and command[1:] == ["version"]:
                raise FileNotFoundError(2, "no docker")
            if command[0] == "prospector":
                return FakeProcess(returncode=0, stdout=PROSPECTOR_CLEAN)
            # The test stage runs the plugin's tests on the host, so a pytest
            # invocation here is expected rather than a surprise.
            if "pytest" in command:
                return FakeProcess(returncode=5, stdout=b"no tests ran")
            raise AssertionError(f"unexpected command executed: {command}")

        install_router(monkeypatch, router)
        report = asyncio.run(CodeValidator().run_pipeline(tmp_path))

        assert report.docker_available is False
        assert report.docker_message == DOCKER_UNAVAILABLE_MESSAGE
        assert report.stage(StageName.LINT).status is StageStatus.PASSED
        # Clause 2.1's recorded consequence: lint *and* test now yield real results
        # on a host with no engine, so an operator gets more partial feedback
        # offline. The gate's conjunction is untouched -- build and validate still
        # cannot run, so export is still blocked.
        for name in (StageName.BUILD, StageName.VALIDATE):
            stage = report.stage(name)
            assert stage.status is StageStatus.FAILED
            assert stage.message == DOCKER_UNAVAILABLE_MESSAGE
        test_stage = report.stage(StageName.TEST)
        assert test_stage.message != DOCKER_UNAVAILABLE_MESSAGE
        assert report.passed is False


class TestCodeRetention:
    def test_pipeline_does_not_modify_the_working_tree(self, tmp_path, monkeypatch):
        # Seed a source file, then run a failing pipeline; the file is untouched (Req 8.6).
        source = tmp_path / "action.py"
        source.write_text("def run():\n    return 1\n", encoding="utf-8")
        before = source.read_text(encoding="utf-8")

        def router(command):
            if command[0] == "prospector":
                return FakeProcess(
                    returncode=0,
                    stdout=prospector_findings(("action.py", 2, "undefined-variable")),
                )
            return FakeProcess(returncode=0)

        install_router(monkeypatch, router)
        report = asyncio.run(CodeValidator().run_pipeline(tmp_path))

        assert report.passed is False
        assert source.read_text(encoding="utf-8") == before


class TestTheLintStageJudgesHandWrittenCodeAtTheStatedBar:
    """The ``lint`` stage's verdict comes from findings, not an exit code (2.6-2.8).

    Prospector exits ``0`` even when it reports messages, so the exit code cannot
    carry the verdict -- and the findings are what let the stage ignore files the
    plugin's author is forbidden to edit. In the run that motivated this, fourteen
    messages blocked an export and every one of them was in a generated
    ``__init__.py`` or ``schema.py``: real, correctly located, and unfixable by
    their audience.
    """

    def _tree(self, tmp_path):
        package = tmp_path / "icon_x"
        (package / "util").mkdir(parents=True)
        (package / "util" / "api.py").write_text("x = 1\n", encoding="utf-8")
        (package / "util" / "schema.py").write_text("y = 2\n", encoding="utf-8")
        (tmp_path / "unit_test").mkdir()
        (tmp_path / "unit_test" / "test_api.py").write_text("z = 3\n", encoding="utf-8")
        return tmp_path

    def _lint(self, tmp_path, monkeypatch, stdout, returncode=0):
        def router(command):
            if command[0] == "prospector":
                return FakeProcess(returncode=returncode, stdout=stdout)
            return FakeProcess(returncode=0)

        install_router(monkeypatch, router)
        report = asyncio.run(CodeValidator().run_pipeline(self._tree(tmp_path)))
        return report.stage(StageName.LINT)

    def test_it_runs_prospector_under_the_resolved_profile_and_the_plugin_width(self, tmp_path, monkeypatch):
        record = []
        install_router(monkeypatch, all_pass_router, record=record)
        asyncio.run(CodeValidator().run_pipeline(tmp_path))

        command = next(cmd for cmd, _ in record if cmd[0] == "prospector")
        assert command[command.index("--max-line-length") + 1] == str(PLUGIN_LINE_LENGTH)
        for tool in LINT_TOOLS:
            assert ["--tool", tool] == command[command.index(tool) - 1 : command.index(tool) + 1]

    def test_a_finding_in_a_generated_file_does_not_fail_the_stage(self, tmp_path, monkeypatch):
        stage = self._lint(
            tmp_path,
            monkeypatch,
            prospector_findings(("icon_x/util/schema.py", 4, "bad-super-call")),
        )
        assert stage.status is StageStatus.PASSED
        assert "1 finding(s) in generated or excluded files ignored" in stage.message

    def test_a_finding_in_the_unit_tests_does_not_fail_the_stage(self, tmp_path, monkeypatch):
        # unit_test/ is lint-excluded and still compiled, formatted and run (3.7).
        stage = self._lint(
            tmp_path,
            monkeypatch,
            prospector_findings(("unit_test/test_api.py", 9, "protected-access")),
        )
        assert stage.status is StageStatus.PASSED

    def test_a_finding_in_hand_written_code_fails_the_stage(self, tmp_path, monkeypatch):
        stage = self._lint(
            tmp_path,
            monkeypatch,
            prospector_findings(("icon_x/util/api.py", 7, "undefined-variable")),
        )
        assert stage.status is StageStatus.FAILED
        assert "icon_x/util/api.py:7: undefined-variable" in stage.stdout

    def test_a_zero_exit_with_findings_still_fails(self, tmp_path, monkeypatch):
        """Prospector exits 0 even when it reports; the exit code cannot be the verdict."""
        stage = self._lint(
            tmp_path,
            monkeypatch,
            prospector_findings(("icon_x/util/api.py", 7, "undefined-variable")),
            returncode=0,
        )
        assert stage.returncode == 0
        assert stage.status is StageStatus.FAILED

    def test_a_nonzero_exit_with_no_finding_still_passes(self, tmp_path, monkeypatch):
        """The mirror image: prospector's exit code says nothing about the plugin."""
        stage = self._lint(tmp_path, monkeypatch, PROSPECTOR_CLEAN, returncode=1)
        assert stage.returncode == 1
        assert stage.status is StageStatus.PASSED

    def test_unparseable_output_fails_rather_than_passing_quietly(self, tmp_path, monkeypatch):
        """The gate has no third state, so a stage that read nothing establishes nothing."""
        stage = self._lint(tmp_path, monkeypatch, b"Traceback (most recent call last): boom")
        assert stage.status is StageStatus.FAILED
        assert "no parseable JSON" in stage.message

    def test_the_result_names_the_profile_the_source_and_the_width(self, tmp_path, monkeypatch):
        stage = self._lint(tmp_path, monkeypatch, PROSPECTOR_CLEAN)
        assert stage.line_length == PLUGIN_LINE_LENGTH
        assert stage.lint_profile is not None
        assert str(PLUGIN_LINE_LENGTH) in stage.message
        assert str(stage.lint_profile.path) in stage.message
        assert (stage.lint_profile.source or "unresolved") in stage.message

    def test_a_pinned_profile_is_used_as_given(self, tmp_path, monkeypatch):
        record = []
        install_router(monkeypatch, all_pass_router, record=record)
        profile = LintProfile(path="/tmp/pinned.yaml", source="repository", detail="pinned")
        asyncio.run(CodeValidator(lint_profile=profile).run_pipeline(tmp_path))

        command = next(cmd for cmd, _ in record if cmd[0] == "prospector")
        assert command[command.index("--profile") + 1] == "/tmp/pinned.yaml"

    def test_a_tree_with_no_hand_written_python_passes_naming_zero_files(self, tmp_path, monkeypatch):
        """The tradeoff, recorded rather than hidden: the stage has no third state.

        The honest report of such a tree lives in the ``Definition_Of_Done``, where
        ``code_parses`` comes back unverified.
        """
        install_router(monkeypatch, all_pass_router)
        report = asyncio.run(CodeValidator().run_pipeline(tmp_path))
        stage = report.stage(StageName.LINT)
        assert stage.status is StageStatus.PASSED
        assert "0 hand-written file(s) judged" in stage.message

    def test_a_missing_linter_fails_the_stage_naming_it(self, tmp_path, monkeypatch):
        def router(command):
            if command[0] == "prospector":
                raise FileNotFoundError(2, "No such file or directory")
            return FakeProcess(returncode=0)

        install_router(monkeypatch, router)
        report = asyncio.run(CodeValidator().run_pipeline(tmp_path))
        stage = report.stage(StageName.LINT)
        assert stage.status is StageStatus.FAILED
        assert "prospector" in stage.message


class TestReportHelpers:
    def test_stage_lookup_returns_none_for_unknown(self, tmp_path, monkeypatch):
        install_router(monkeypatch, all_pass_router)
        report = asyncio.run(CodeValidator().run_pipeline(tmp_path))
        assert report.stage("does-not-exist") is None

    def test_no_docker_run_is_dispatched_for_the_tests(self, tmp_path, monkeypatch):
        """Clause 2.1: the tests are a host run, so nothing is aimed at the image.

        Replaces a check that the in-container command substituted the image tag.
        That override existed only to point the stage at a container, and the
        container provably carries neither the tests nor pytest -- so its verdict was
        independent of the plugin for every tag.
        """
        record = []
        install_router(monkeypatch, all_pass_router, record=record)

        asyncio.run(CodeValidator().run_pipeline(tmp_path, image_tag="myplugin:1.0"))

        assert [cmd for cmd, _ in record if cmd[:2] == ["docker", "run"]] == []
        assert [cmd for cmd, _ in record if cmd[:2] == ["docker", "build"]] == [
            ["docker", "build", "-t", "myplugin:1.0", "."]
        ]


class TestTheTestStageRunsOnTheHost:
    """Clause 2.1-2.3 -- the stage's verdict is the plugin's own, or an honest fail.

    The stage used to run ``docker run --rm <image> python -m pytest -q`` against an
    image carrying neither the tests (the generated ``.dockerignore`` excludes
    ``unit_test/**/*``) nor pytest (the runtime image correctly declares no test
    dependencies), so it failed for **every** plugin ever built and the only edits
    that would have fixed it are to two files the Agent_Rulebook forbids touching.

    Option (a) was taken deliberately: the stage no longer establishes that the
    tests pass *in the shipping environment*. That intent is unreachable without
    those forbidden edits, and the Quality_Gate already ran them on the host
    successfully -- so the change removes a state in which two subsystems
    contradicted each other about one plugin.
    """

    def _tree(self, root, *, tests="def test_ok():\n    assert True\n"):
        (root / "unit_test").mkdir(parents=True, exist_ok=True)
        (root / "unit_test" / "test_it.py").write_text(tests, encoding="utf-8")
        (root / "icon_x").mkdir(parents=True, exist_ok=True)
        return root

    def _stage(self, root, monkeypatch, *, pytest_process=None):
        def router(command):
            if command[0] == "prospector":
                return FakeProcess(returncode=0, stdout=PROSPECTOR_CLEAN)
            if "pytest" in command and pytest_process is not None:
                return pytest_process
            return FakeProcess(returncode=0, stdout=b"ok")

        install_router(monkeypatch, router)
        report = asyncio.run(CodeValidator(test_python_executable="host-python").run_pipeline(root))
        return report.stage(StageName.TEST)

    def test_a_passing_suite_passes_the_stage(self, tmp_path):
        run = UnitTestRun(interpreter="host-python", ran=True, returncode=0, output="1 passed")
        stage = CodeValidator(test_python_executable="host-python")._judge_tests(run)
        assert stage.status is StageStatus.PASSED
        assert "host-python" in stage.message
        assert stage.stdout == "1 passed"

    def test_a_failing_suite_fails_the_stage_and_names_the_tests(self, tmp_path):
        run = UnitTestRun(
            interpreter="host-python",
            ran=True,
            returncode=1,
            output="FAILED unit_test/test_it.py::test_bad",
            failures=(UnitTestFailure(path="unit_test/test_it.py", line=2, name="test_bad"),),
        )
        stage = CodeValidator(test_python_executable="host-python")._judge_tests(run)
        assert stage.status is StageStatus.FAILED
        assert "test_bad" in stage.message
        assert "host-python" in stage.message

    def test_a_timeout_is_recorded_as_a_timeout_not_a_failure(self, tmp_path):
        run = UnitTestRun(interpreter="host-python", timed_out=True)
        validator = CodeValidator(test_python_executable="host-python", stage_timeout_seconds=600.0)
        stage = validator._judge_tests(run)
        assert stage.status is StageStatus.TIMED_OUT
        assert stage.timed_out is True
        assert "600s" in stage.message and "aborted" in stage.message

    def test_the_600s_threshold_is_what_the_stage_asks_for(self, tmp_path):
        """Req 8.8 still applies, now to the host run."""
        assert DEFAULT_STAGE_TIMEOUT_SECONDS == 600.0
        spec = next(
            s
            for s in CodeValidator(test_python_executable="host-python")._stage_specs("tag", tmp_path)
            if s.name == StageName.TEST
        )
        assert spec.timeout_seconds == 600.0

    def test_no_unit_test_directory_fails_closed(self, tmp_path):
        run = UnitTestRun(interpreter="host-python", no_tests=True, message="no unit_test/ directory")
        stage = CodeValidator(test_python_executable="host-python")._judge_tests(run)
        assert stage.status is StageStatus.FAILED, "an unrunnable check must never pass quietly"
        assert "unit_test/" in stage.message
        assert "host-python" in stage.message

    def test_no_qualifying_interpreter_fails_closed_naming_the_reason(self, tmp_path):
        run = UnitTestRun(
            interpreter=None,
            skipped=("tests (no interpreter can import both insightconnect_plugin_runtime and pytest)",),
            message="the plugin's unit tests could not be run: no interpreter can import both",
        )
        stage = CodeValidator()._judge_tests(run)
        assert stage.status is StageStatus.FAILED
        assert "could not be run" in stage.message

    def test_pytest_absent_is_reported_with_remediation_and_never_installed(self, tmp_path):
        """SCOPE-12 -- the absence is reported, not remedied."""
        run = UnitTestRun(interpreter="host-python", message="host-python -m pytest is not available")
        stage = CodeValidator(test_python_executable="host-python")._judge_tests(run)
        assert stage.status is StageStatus.FAILED
        assert "Install pytest" in stage.message
        assert "host-python" in stage.message

    def test_a_collection_error_fails_closed(self, tmp_path):
        run = UnitTestRun(interpreter="host-python", ran=True, returncode=2, output="ERROR collecting")
        stage = CodeValidator(test_python_executable="host-python")._judge_tests(run)
        assert stage.status is StageStatus.FAILED
        assert "exited 2" in stage.message

    def test_the_stage_runs_with_docker_absent(self, tmp_path, monkeypatch):
        """A host-run check has no reason to depend on the engine."""

        def router(command):
            if command[:2] == ["docker", "version"]:
                raise FileNotFoundError(2, "no docker")
            if command[0] == "prospector":
                return FakeProcess(returncode=0, stdout=PROSPECTOR_CLEAN)
            if "pytest" in command:
                return FakeProcess(returncode=0, stdout=b"1 passed")
            raise AssertionError(f"unexpected command executed: {command}")

        install_router(monkeypatch, router)
        report = asyncio.run(CodeValidator(test_python_executable="host-python").run_pipeline(self._tree(tmp_path)))
        stage = report.stage(StageName.TEST)
        assert stage.status is StageStatus.PASSED
        assert report.docker_available is False
        # The gate's conjunction is untouched: build and validate still cannot run.
        assert report.passed is False

    def test_a_precomputed_run_is_reused_rather_than_running_the_suite_twice(self, tmp_path, monkeypatch):
        """Clause 2.4 -- one execution per export, one interpreter, one verdict."""
        record = []
        install_router(monkeypatch, all_pass_router, record=record)
        supplied = UnitTestRun(interpreter="already-ran", ran=True, returncode=0, output="1 passed")

        report = asyncio.run(
            CodeValidator(test_python_executable="host-python").run_pipeline(
                self._tree(tmp_path), unit_test_run=supplied
            )
        )

        stage = report.stage(StageName.TEST)
        assert stage.status is StageStatus.PASSED
        assert "already-ran" in stage.message
        assert [cmd for cmd, _ in record if "pytest" in cmd] == [], "the suite was executed a second time"

    def test_without_a_precomputed_run_the_stage_runs_its_own(self, tmp_path, monkeypatch):
        """A caller holding only the validator still gets a verdict."""
        record = []

        def router(command):
            record.append(command)
            if command[0] == "prospector":
                return FakeProcess(returncode=0, stdout=PROSPECTOR_CLEAN)
            if "pytest" in command:
                return FakeProcess(returncode=0, stdout=b"1 passed")
            return FakeProcess(returncode=0, stdout=b"ok")

        install_router(monkeypatch, router)
        report = asyncio.run(CodeValidator(test_python_executable="host-python").run_pipeline(self._tree(tmp_path)))
        assert report.stage(StageName.TEST).status is StageStatus.PASSED
        assert [cmd for cmd in record if "pytest" in cmd], "the stage never ran the suite"
