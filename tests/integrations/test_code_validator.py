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

from icplugin_builder.integrations import code_validator as cv
from icplugin_builder.integrations.code_validator import (
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


def all_pass_router(command):
    """A router where every command (docker probe + all stages) succeeds."""
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

        report = asyncio.run(CodeValidator().run_pipeline(tmp_path))

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
            if command[0] == "flake8":
                return FakeProcess(returncode=1, stdout=b"E501 line too long")
            return FakeProcess(returncode=0)

        install_router(monkeypatch, router)
        report = asyncio.run(CodeValidator().run_pipeline(tmp_path))

        lint = report.stage(StageName.LINT)
        assert lint.status is StageStatus.FAILED
        assert lint.returncode == 1
        assert "E501" in lint.stdout
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
            if command[0] == "flake8":
                return FakeProcess(returncode=0, stdout=b"lint ok")
            # No other command should be invoked when Docker is unavailable.
            raise AssertionError(f"unexpected command executed: {command}")

        install_router(monkeypatch, router)
        report = asyncio.run(CodeValidator().run_pipeline(tmp_path))

        assert report.docker_available is False
        assert report.docker_message == DOCKER_UNAVAILABLE_MESSAGE
        assert report.stage(StageName.LINT).status is StageStatus.PASSED
        for name in (StageName.BUILD, StageName.TEST, StageName.VALIDATE):
            stage = report.stage(name)
            assert stage.status is StageStatus.FAILED
            assert stage.message == DOCKER_UNAVAILABLE_MESSAGE
        assert report.passed is False


class TestCodeRetention:
    def test_pipeline_does_not_modify_the_working_tree(self, tmp_path, monkeypatch):
        # Seed a source file, then run a failing pipeline; the file is untouched (Req 8.6).
        source = tmp_path / "action.py"
        source.write_text("def run():\n    return 1\n", encoding="utf-8")
        before = source.read_text(encoding="utf-8")

        def router(command):
            if command[0] == "flake8":
                return FakeProcess(returncode=1, stdout=b"lint failed")
            return FakeProcess(returncode=0)

        install_router(monkeypatch, router)
        report = asyncio.run(CodeValidator().run_pipeline(tmp_path))

        assert report.passed is False
        assert source.read_text(encoding="utf-8") == before


class TestReportHelpers:
    def test_stage_lookup_returns_none_for_unknown(self, tmp_path, monkeypatch):
        install_router(monkeypatch, all_pass_router)
        report = asyncio.run(CodeValidator().run_pipeline(tmp_path))
        assert report.stage("does-not-exist") is None

    def test_custom_test_command_substitutes_image_tag(self, tmp_path, monkeypatch):
        record = []
        install_router(monkeypatch, all_pass_router, record=record)

        validator = CodeValidator(test_command=("docker", "run", "{image}", "pytest"))
        asyncio.run(validator.run_pipeline(tmp_path, image_tag="myplugin:1.0"))

        test_calls = [cmd for cmd, _ in record if cmd[:2] == ["docker", "run"]]
        assert test_calls == [["docker", "run", "myplugin:1.0", "pytest"]]
