"""Integration test for the wired four-stage Code_Validator pipeline (task 15.11).

Where :mod:`test_code_validator` unit-tests each seam of
``CodeValidator.run_pipeline`` in isolation, this module exercises the *wired
multi-stage flow* end-to-end against a **mocked Docker engine** and a **mocked
``insight-plugin`` CLI**. A single ``MockDockerEngine`` harness patches
``asyncio.create_subprocess_exec`` and dispatches by command, so the real Docker
daemon and plugin toolchain are never required; every coroutine is driven with
``asyncio.run`` so no async test plugin is needed.

The scenarios cover the full pipeline the user drives with one build request:

* All four stages -- lint (Req 8.1), build (Req 8.2), test (Req 8.3), and
  ``insight-plugin validate`` (Req 8.4) -- run in order against the same project
  directory, dispatching the expected ``docker`` / ``insight-plugin`` argv and
  building/reusing a single image tag, and the aggregate report passes.
* The 600-second abort (Req 8.8): a build (or test) stage that runs past the
  configured threshold is aborted with a timeout fail carrying the ``600s``
  message, while the lint and validate stages are *not* subject to that abort.

**Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.8**
"""

import asyncio

from icplugin_builder.integrations import code_validator as cv
from icplugin_builder.integrations.code_validator import (
    DEFAULT_STAGE_TIMEOUT_SECONDS,
    CodeValidator,
    StageName,
    StageStatus,
)


class _FakeProcess:
    """A stand-in for the object returned by ``create_subprocess_exec``.

    Instances complete immediately; ``_SlowProcess`` overrides
    :meth:`communicate` to block so an outer ``wait_for`` aborts it.
    """

    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class _SlowProcess(_FakeProcess):
    """A process that never finishes on its own, forcing the stage's abort path."""

    async def communicate(self):
        await asyncio.sleep(3600)
        return self._stdout, self._stderr


class MockDockerEngine:
    """A mock Docker engine + ``insight-plugin`` CLI for the wired pipeline.

    The harness classifies each launched command (the ``docker version`` probe,
    ``flake8`` lint, ``docker build``, ``docker run`` test, and
    ``insight-plugin validate``) and returns a scripted fake process for it,
    recording every ``(argv, cwd)`` on :attr:`dispatched`. Commands named in
    ``slow`` return a blocking process so the validator's ``wait_for`` guard
    aborts them (exercising the 600s timeout, Req 8.8).
    """

    def __init__(self, *, slow=()):
        self.dispatched = []
        self.timeouts = []
        self._slow = set(slow)

    @staticmethod
    def classify(command):
        if command[:2] == ["docker", "version"]:
            return "probe"
        if command[0] == "flake8":
            return StageName.LINT
        if command[:2] == ["docker", "build"]:
            return StageName.BUILD
        if command[:2] == ["docker", "run"]:
            return StageName.TEST
        # The validate stage runs icon_validator's own validator list under the
        # toolchain's interpreter rather than shelling `insight-plugin validate`,
        # so that the one validator needing a plugins-repo git clone can be
        # skipped instead of crashing the run.
        if command[1:2] == ["-c"] and "icon_validator" in command[2]:
            return StageName.VALIDATE
        return "unknown"

    def install(self, monkeypatch):
        """Patch the subprocess + timeout seams onto the ``code_validator`` module."""
        engine = self
        real_wait_for = asyncio.wait_for

        async def fake_exec(*command, cwd=None, stdout=None, stderr=None):
            argv = list(command)
            engine.dispatched.append((argv, cwd))
            kind = engine.classify(argv)
            if kind == "probe":
                return _FakeProcess(returncode=0, stdout=b"Docker version 25.0.0")
            if kind in engine._slow:
                return _SlowProcess()
            return _FakeProcess(returncode=0, stdout=f"{kind} ok".encode())

        async def fake_wait_for(awaitable, timeout):
            # Record the threshold the validator asked for (600s for build/test,
            # the shorter probe timeout for the probe), but enforce a tiny real
            # bound so a blocking slow process aborts fast instead of hanging.
            engine.timeouts.append(timeout)
            return await real_wait_for(awaitable, 0.05)

        monkeypatch.setattr(cv.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(cv.asyncio, "wait_for", fake_wait_for)
        return self

    def argv_for(self, kind):
        """Return the argv dispatched for stage ``kind`` (or ``None``)."""
        for argv, _ in self.dispatched:
            if self.classify(argv) == kind:
                return argv
        return None

    def stage_kinds(self):
        """The classified kinds of every dispatched command, in launch order."""
        return [self.classify(argv) for argv, _ in self.dispatched]


class TestWiredFourStagePipeline:
    """One build request drives lint -> build -> test -> validate end-to-end."""

    def test_all_four_stages_run_in_order_and_report_passes(self, tmp_path, monkeypatch):
        """Req 8.1-8.4: every stage runs, in order, and the aggregate passes."""
        engine = MockDockerEngine().install(monkeypatch)

        report = asyncio.run(CodeValidator().run_pipeline(tmp_path, image_tag="myplugin:1.0"))

        # The report carries one passing result per stage, in pipeline order.
        assert [stage.name for stage in report.stages] == list(StageName.ORDER)
        assert all(stage.status is StageStatus.PASSED for stage in report.stages)
        assert report.passed is True
        assert report.failed_stages == ()
        assert report.docker_available is True

        # The wired dispatch: a probe, then each stage exactly once, in order.
        assert engine.stage_kinds() == [
            "probe",
            StageName.LINT,
            StageName.BUILD,
            StageName.TEST,
            StageName.VALIDATE,
        ]

    def test_dispatches_expected_docker_and_insight_plugin_commands(self, tmp_path, monkeypatch):
        """Req 8.2-8.4: build/test go to Docker and validate to the toolchain, one image tag."""
        engine = MockDockerEngine().install(monkeypatch)

        asyncio.run(
            CodeValidator(validate_python_executable="toolchain-python").run_pipeline(
                tmp_path, image_tag="myplugin:1.0"
            )
        )

        assert engine.argv_for(StageName.LINT) == ["flake8", "."]
        assert engine.argv_for(StageName.BUILD) == ["docker", "build", "-t", "myplugin:1.0", "."]
        # The test stage runs against the *same* image the build produced.
        assert engine.argv_for(StageName.TEST) == [
            "docker",
            "run",
            "--rm",
            "myplugin:1.0",
            "python",
            "-m",
            "pytest",
            "-q",
        ]
        # Validate runs under the toolchain's interpreter (icon_validator lives
        # there, not in this tool's environment), against the project directory,
        # with the repo-dependent validator named for exclusion.
        validate_argv = engine.argv_for(StageName.VALIDATE)
        assert validate_argv[0] == "toolchain-python"
        assert validate_argv[1] == "-c"
        assert str(tmp_path) in validate_argv
        assert "Version Increment Validator" in validate_argv[-1]

    def test_every_stage_runs_inside_the_project_directory(self, tmp_path, monkeypatch):
        """The four stages run in the plugin working tree; only the probe is cwd-less."""
        engine = MockDockerEngine().install(monkeypatch)

        asyncio.run(CodeValidator().run_pipeline(tmp_path, image_tag="myplugin:1.0"))

        for argv, cwd in engine.dispatched:
            if MockDockerEngine.classify(argv) == "probe":
                assert cwd is None
            else:
                assert cwd == str(tmp_path)


class TestWiredTimeoutAbort:
    """The 600s abort applies to build and test but not to lint or validate (Req 8.8)."""

    def test_build_stage_aborts_at_the_600s_threshold(self, tmp_path, monkeypatch):
        """A build that overruns is aborted with a 600s timeout fail."""
        engine = MockDockerEngine(slow=[StageName.BUILD]).install(monkeypatch)

        # A default validator uses the real 600s build/test threshold.
        report = asyncio.run(CodeValidator().run_pipeline(tmp_path, image_tag="myplugin:1.0"))

        build = report.stage(StageName.BUILD)
        assert build.status is StageStatus.TIMED_OUT
        assert build.timed_out is True
        assert "600s" in build.message and "aborted" in build.message
        assert report.passed is False
        assert [stage.name for stage in report.failed_stages] == [StageName.BUILD]

        # The threshold the validator requested for build/test is exactly 600s,
        # and that guard is applied only to those two stages (not lint/validate).
        assert DEFAULT_STAGE_TIMEOUT_SECONDS == 600.0
        assert engine.timeouts.count(600.0) == 2

    def test_test_stage_aborts_at_the_600s_threshold(self, tmp_path, monkeypatch):
        """A test stage that overruns is likewise aborted, after a passing build."""
        MockDockerEngine(slow=[StageName.TEST]).install(monkeypatch)

        report = asyncio.run(CodeValidator().run_pipeline(tmp_path, image_tag="myplugin:1.0"))

        assert report.stage(StageName.BUILD).status is StageStatus.PASSED
        test = report.stage(StageName.TEST)
        assert test.status is StageStatus.TIMED_OUT
        assert "600s" in test.message
        # Lint and validate still complete normally -- they are not timeout-guarded.
        assert report.stage(StageName.LINT).status is StageStatus.PASSED
        assert report.stage(StageName.VALIDATE).status is StageStatus.PASSED
        assert report.passed is False
