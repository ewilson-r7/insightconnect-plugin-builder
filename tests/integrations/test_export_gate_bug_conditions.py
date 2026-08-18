"""Bug-condition exploration tests for the export gate (spec task 1).

**These tests are expected to FAIL on unfixed code.** The failure is the point:
each one encodes the behavior `bugfix.md` requires and, until the fix lands,
reports the counterexample that proves the defect exists. Do not "repair" them by
weakening an assertion -- they become the fix's acceptance check at tasks 5.4,
7.5 and 9.6.

Scope of this module: the **gate and reporting layer** only. Nothing here touches
code generation, which the run verified as correct (`bugfix.md` 3.1).

Task 1.1 -- ``isBugCondition_1``, the ``test`` stage can never pass::

    RETURN hostUnitTestsPass(X)
       AND dockerignoreExcludes(X, 'unit_test')
       AND NOT imageHasPytest(X)

and the property required of the fixed tool::

    FOR ALL X WHERE isBugCondition_1(X) DO
      report <- runPipeline'(X)
      ASSERT stage(report, 'test').passed
      ASSERT stage(report, 'test').passed = hostUnitTestsPass(X)
    END FOR

**Scoped rather than generated.** The three bug conditions are deterministic
against one concrete tree -- the JumpCloud plugin at
``~/.icplugin-builder/projects/jumpcloud/`` that the 2026-08-17 run produced -- so
these tests are scoped to it instead of generating working trees. The
generated-tree generalization arrives with Properties 66, 68 and 69 in later
tasks. The properties that *are* generated here quantify over image tags (task
1.1) and over line widths (task 1.2), where generation is free and the claim is
genuinely universal.

**Environment.** ``insight-plugin`` and ``prospector`` live in
``~/Library/Python/3.9/bin`` and ``docker`` in
``/Applications/Docker.app/Contents/Resources/bin``; neither is on a non-login
shell ``PATH``, so :func:`_tool_path` prepends both. A stage that fails because a
tool is missing is an environmental artifact, not a finding, so every check that
needs the real toolchain **skips** rather than failing when it is absent -- an
unverified measurement is recorded as unverified (parent Req 26.4, 27.5).

Later subtasks append their own classes to this module: one class per
bug-condition clause. Task 1.10's two integrations-layer counterexamples -- what a
blocked export reports (1.11) and which credential types the toolchain defines
(1.17) -- are the last section, below task 1.9's.

_Requirements: 1.1, 1.2, 1.3_
"""

import ast
import asyncio
import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Dict, NamedTuple, Optional, Tuple

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Task 1.10 reads the operator-facing export payload, which is built here. The
# claim under test (`bugfix.md` 1.11) is about what reaches the operator, and this
# serializer is the boundary at which the stage output is dropped.
from icplugin_builder.api.app import _serialize_export_plan
from icplugin_builder.core import spec_completeness
from icplugin_builder.core.diff import diff_file_trees
from icplugin_builder.core.spec_completeness import VALID_CREDENTIAL_TYPES, check_completeness
from icplugin_builder.core.spec_model import PluginSpec, SemVer
from icplugin_builder.core.truncation import MAX_DISPLAY_CHARS, truncate_error_output
from icplugin_builder.core.version_bump import bump_version
from icplugin_builder.integrations.build_engine import (
    BUILDER_METADATA_DIR,
    BuildEngine,
    _EXCLUDED_DIRS,
    list_plugin_files,
    preview_export_files,
)
from icplugin_builder.integrations.build_prep import (
    DEFAULT_LINT_PROFILE,
    FALLBACK_LINT_PROFILE,
    LINT_PROFILE_SOURCE_FALLBACK,
    LINT_PROFILE_SOURCE_REPOSITORY,
    LINT_TOOLS,
    PLUGIN_LINE_LENGTH,
    REQUIRED_TOOLS,
    LintProfile,
    TargetPython,
    resolve_lint_profile,
    resolve_target_python,
)
from icplugin_builder.integrations.build_export_failure import classify_build_failure
from icplugin_builder.integrations.code_validator import (
    CodeValidator,
    PipelineReport,
    StageName,
    StageResult,
    StageStatus,
    _derive_image_tag,
)
from icplugin_builder.integrations.export_gate import decide_export
from icplugin_builder.integrations.definition_of_done import (
    CONDITION_API_CLIENT,
    CONDITION_UNIT_TESTS,
    ConditionResult,
    ConditionStatus,
    _defined_names,
    evaluate_done,
)
from icplugin_builder.integrations.quality_gate import (
    GENERATED_FILE_NAMES,
    SOURCE_FORMAT,
    SOURCE_PROSPECTOR,
    SOURCE_TESTS,
    CodeFinding,
    QualityGate,
    QualityReport,
    hand_written_python,
    is_generated,
    is_lint_excluded,
    package_dir,
)
from icplugin_builder.orchestrator.session import ExportPlan

#: The tree the reproduction run produced. Every concrete-tree assertion below is
#: about this plugin and no other.
JUMPCLOUD_TREE = Path("~/.icplugin-builder/projects/jumpcloud").expanduser()

#: Directories holding the real toolchain on the reproduction host, per
#: `bugfix.md` "Reproduction Environment". Prepended rather than replaced.
TOOLCHAIN_PATH_ENTRIES: Tuple[str, ...] = (
    str(Path("~/Library/Python/3.9/bin").expanduser()),
    "/Applications/Docker.app/Contents/Resources/bin",
)

#: The unit-test directory name the generated ``.dockerignore`` excludes.
UNIT_TEST_DIR = "unit_test"


def _tool_path() -> str:
    """Return ``PATH`` with the toolchain directories prepended."""
    existing = os.environ.get("PATH", "")
    return os.pathsep.join([*TOOLCHAIN_PATH_ENTRIES, existing]) if existing else os.pathsep.join(TOOLCHAIN_PATH_ENTRIES)


@pytest.fixture(autouse=True)
def toolchain_on_path(monkeypatch):
    """Put the real toolchain on ``PATH`` for every test in this module."""
    monkeypatch.setenv("PATH", _tool_path())


def _require_tree() -> None:
    """Skip when the concrete tree is absent; it cannot be substituted."""
    if not JUMPCLOUD_TREE.is_dir():
        pytest.skip(
            f"the JumpCloud tree is not present at {JUMPCLOUD_TREE}; these assertions are about that "
            "concrete tree and a synthesised one would not carry the same evidence"
        )


def _capture(
    command,
    *,
    cwd: Optional[Path] = None,
    timeout: float = 900.0,
    path: Optional[str] = None,
    environment: Optional[Dict[str, str]] = None,
) -> Optional[Tuple[int, str]]:
    """Run ``command``, returning ``(returncode, output)`` or ``None`` if it could not run.

    Args:
        command: the argv to run. Fixed, never a shell string.
        cwd: the working directory, or ``None`` for this process's.
        timeout: seconds before the run is abandoned.
        path: the ``PATH`` the child sees. Defaults to :func:`_tool_path`, which is
            what every measurement before task 1.4 wants. Task 1.4 needs a run with
            a *sanitised* ``PATH``, so it is a parameter rather than a constant --
            added additively, so no caller above changes behaviour.
        environment: extra environment variables, applied after ``PATH``. Task 1.4
            uses it to redirect ``HOME``, which is how the repository profile is
            hidden from a child without moving anybody's files.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            list(command),
            cwd=None if cwd is None else str(cwd),
            capture_output=True,
            timeout=timeout,
            check=False,
            env={
                **os.environ,
                "PATH": _tool_path() if path is None else path,
                **(environment or {}),
            },
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    output = completed.stdout.decode("utf-8", errors="replace") + completed.stderr.decode("utf-8", errors="replace")
    return completed.returncode, output


@lru_cache(maxsize=1)
def _docker_available() -> bool:
    """Return ``True`` iff a Docker engine answers ``docker version``."""
    result = _capture(["docker", "version"], timeout=60.0)
    return result is not None and result[0] == 0


def _require_docker() -> None:
    """Skip when Docker is unavailable, so a missing engine is never a finding."""
    if not _docker_available():
        pytest.skip("no Docker engine answered `docker version`; the in-image measurement cannot be taken")


@lru_cache(maxsize=1)
def _target_python() -> TargetPython:
    """The interpreter the tool resolves for a plugin's tests."""
    return resolve_target_python()


@lru_cache(maxsize=1)
def _host_unit_test_run() -> Optional[Tuple[int, str]]:
    """``hostUnitTestsPass(X)``: run the tree's own suite under the resolved interpreter."""
    interpreter = _target_python().executable
    if interpreter is None:
        return None
    return _capture(
        [interpreter, "-m", "pytest", UNIT_TEST_DIR, "-q", "--no-header"],
        cwd=JUMPCLOUD_TREE,
        timeout=600.0,
    )


def _host_unit_tests_pass() -> bool:
    """Return ``True`` iff the plugin's unit tests pass on the host."""
    run = _host_unit_test_run()
    if run is None:
        pytest.skip("no interpreter resolved for the plugin's tests; hostUnitTestsPass could not be measured")
    return run[0] == 0


def _dockerignore_patterns() -> Tuple[str, ...]:
    """The generated ``.dockerignore``'s patterns, comments and blanks dropped."""
    path = JUMPCLOUD_TREE / ".dockerignore"
    if not path.is_file():
        return ()
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return tuple(line.strip() for line in lines if line.strip() and not line.strip().startswith("#"))


def _dockerignore_excludes(name: str) -> bool:
    """``dockerignoreExcludes(X, name)``: is ``name`` kept out of the build context?"""
    return any(pattern == name or pattern.startswith(f"{name}/") for pattern in _dockerignore_patterns())


@lru_cache(maxsize=1)
def _image_tag() -> str:
    """The image tag the validator derives for this tree."""
    # Deliberately the validator's own derivation rather than a literal, so the
    # image these tests probe is the image the stage would run.
    return _derive_image_tag(JUMPCLOUD_TREE)


@lru_cache(maxsize=1)
def _image_built() -> Optional[Tuple[int, str]]:
    """Build the tree's image exactly as the ``build`` stage does."""
    return _capture(["docker", "build", "-t", _image_tag(), "."], cwd=JUMPCLOUD_TREE, timeout=1800.0)


def _require_image() -> None:
    """Skip when the image could not be built; an unbuilt image measures nothing."""
    _require_docker()
    built = _image_built()
    if built is None or built[0] != 0:
        detail = "the build did not complete" if built is None else built[1].strip().splitlines()[-3:]
        pytest.skip(f"could not build {_image_tag()} from {JUMPCLOUD_TREE}: {detail}")


def _in_image(shell_command: str) -> Tuple[int, str]:
    """Run ``shell_command`` inside the built image, bypassing its entrypoint."""
    _require_image()
    result = _capture(
        ["docker", "run", "--rm", "--entrypoint", "sh", _image_tag(), "-c", shell_command],
        timeout=300.0,
    )
    if result is None:
        pytest.skip(f"could not run a probe inside {_image_tag()}")
    return result


@lru_cache(maxsize=1)
def _image_has_pytest() -> bool:
    """``imageHasPytest(X)``: can the runtime image import ``pytest``?"""
    return _in_image("python -c 'import pytest'")[0] == 0


@lru_cache(maxsize=1)
def _image_has_unit_tests() -> bool:
    """Does the built image carry the plugin's ``unit_test/`` directory at all?"""
    return _in_image(f"test -e /python/src/{UNIT_TEST_DIR}")[0] == 0


@lru_cache(maxsize=1)
def _pipeline_report() -> PipelineReport:
    """One four-stage pipeline run over the concrete tree, as the tool wires it."""
    _require_docker()
    validator = CodeValidator(validate_python_executable=_target_python().executable or "python3")
    return asyncio.run(validator.run_pipeline(JUMPCLOUD_TREE))


@lru_cache(maxsize=1)
def _quality_report() -> QualityReport:
    """One ``Quality_Gate`` run over the same tree, under the same interpreter."""
    interpreter = _target_python().executable
    if interpreter is None:
        pytest.skip("no interpreter resolved; the Quality_Gate cannot run the plugin's tests")
    return asyncio.run(QualityGate(python_executable=interpreter).run(JUMPCLOUD_TREE))


def _stage_detail(name: str) -> str:
    """Everything the pipeline recorded for one stage, for an assertion message."""
    stage = _pipeline_report().stage(name)
    if stage is None:
        return f"no {name} stage was recorded"
    return (
        f"{name}: status={stage.status.value} returncode={stage.returncode!r} "
        f"message={stage.message!r} stdout={stage.stdout.strip()[-600:]!r} stderr={stage.stderr.strip()[-600:]!r}"
    )


class TestBugConditionOneHoldsForTheJumpCloudTree:
    """``isBugCondition_1`` -- the three conjuncts, measured rather than assumed.

    These are the *witnesses*. They are expected to pass both before and after the
    fix: the chosen fix runs the tests on the host (`bugfix.md` 2.1) and changes
    neither the generated ``.dockerignore`` nor the runtime image, so all three
    facts survive it. What changes is that the stage stops depending on them.
    """

    def test_the_plugins_own_unit_tests_pass_on_the_host(self):
        """``hostUnitTestsPass(X)`` -- the first conjunct."""
        _require_tree()
        run = _host_unit_test_run()
        assert run is not None, "the plugin's unit tests could not be run at all"
        returncode, output = run
        assert returncode == 0, (
            f"the plugin's unit tests do not pass under {_target_python().executable!r}, so this tree is not "
            f"an instance of isBugCondition_1: {output.strip()[-800:]}"
        )

    def test_the_generated_dockerignore_excludes_the_unit_tests(self):
        """``dockerignoreExcludes(X, 'unit_test')`` -- the second conjunct."""
        _require_tree()
        assert _dockerignore_excludes(UNIT_TEST_DIR), (
            f"{UNIT_TEST_DIR} is not excluded by the generated .dockerignore, whose patterns are "
            f"{_dockerignore_patterns()}"
        )

    def test_the_runtime_image_has_no_pytest(self):
        """``NOT imageHasPytest(X)`` -- the third conjunct."""
        _require_tree()
        assert not _image_has_pytest(), (
            f"{_image_tag()} can import pytest, so this tree is not an instance of isBugCondition_1: "
            f"{_in_image('python -c \"import pytest\"')[1].strip()[-400:]}"
        )

    def test_the_built_image_carries_no_unit_tests(self):
        """The consequence of the second conjunct, measured in the built image."""
        _require_tree()
        assert not _image_has_unit_tests(), (
            f"/python/src/{UNIT_TEST_DIR} exists in {_image_tag()}, so the in-image test command would have "
            "something to run"
        )


class TestTheTestStageCanNeverPass:
    """`bugfix.md` 1.1 / 2.2 -- the stage's verdict must be the plugin's own.

    Expected to FAIL on unfixed code: the stage shells
    ``docker run --rm <image> python -m pytest -q`` at an image that carries
    neither the tests nor ``pytest``.
    """

    def test_the_test_stage_passes_for_a_plugin_whose_tests_pass(self):
        """``ASSERT stage(report, 'test').passed`` for a tree satisfying the condition."""
        _require_tree()
        _require_image()
        stage = _pipeline_report().stage(StageName.TEST)
        assert stage is not None, "the pipeline recorded no test stage"
        assert stage.passed, (
            "the plugin's unit tests pass on the host, so the test stage must pass. "
            f"It did not -- {_stage_detail(StageName.TEST)}"
        )

    def test_the_test_stage_verdict_equals_the_host_result(self):
        """``ASSERT stage(report, 'test').passed = hostUnitTestsPass(X)`` -- both directions."""
        _require_tree()
        _require_image()
        stage = _pipeline_report().stage(StageName.TEST)
        assert stage is not None, "the pipeline recorded no test stage"
        assert stage.passed == _host_unit_tests_pass(), (
            f"the test stage says passed={stage.passed} while the plugin's unit tests on the host say "
            f"passed={_host_unit_tests_pass()} -- {_stage_detail(StageName.TEST)}"
        )

    def test_the_export_gate_permits_a_plugin_whose_tests_pass(self):
        """``ASSERT decideExport'(specReport(X), report).permitted`` -- the gate's conjunction."""
        _require_tree()
        _require_image()
        report = _pipeline_report()
        failed = tuple(stage.name for stage in report.failed_stages)
        assert StageName.TEST not in failed, (
            "a plugin whose unit tests pass must not need `force` to export on account of the test stage; "
            f"failed stages were {failed} -- {_stage_detail(StageName.TEST)}"
        )

    def test_the_test_stage_names_the_interpreter_it_used(self):
        """`bugfix.md` 2.3 -- an unrunnable run must say what it tried to run with."""
        _require_tree()
        _require_image()
        stage = _pipeline_report().stage(StageName.TEST)
        assert stage is not None, "the pipeline recorded no test stage"
        interpreter = _target_python().executable or ""
        recorded = f"{stage.message}\n{stage.stdout}\n{stage.stderr}"
        assert interpreter and interpreter in recorded, (
            f"the test stage's result never names the interpreter {interpreter!r} it ran the plugin's tests "
            "with, so an operator cannot tell which environment produced the verdict -- "
            f"{_stage_detail(StageName.TEST)}"
        )


class TestTheGateAndThePipelineDisagree:
    """`bugfix.md` 1.2 / 2.4 -- two subsystems, one tree, opposite answers.

    Expected to FAIL on unfixed code: the ``Quality_Gate`` runs the tests on the
    host and reports ``unit_tests_pass`` met, while the pipeline's ``test`` stage
    runs them in the image and fails.
    """

    def test_the_quality_gate_and_the_test_stage_agree_about_the_unit_tests(self):
        """One definition of the unit test run, so the two cannot contradict each other."""
        _require_tree()
        _require_image()
        quality = _quality_report()
        gate_says_tests_pass = not quality.by_source(SOURCE_TESTS)
        stage = _pipeline_report().stage(StageName.TEST)
        assert stage is not None, "the pipeline recorded no test stage"
        assert gate_says_tests_pass == stage.passed, (
            f"the Quality_Gate reports the unit tests passing={gate_says_tests_pass} "
            f"(findings={quality.by_source(SOURCE_TESTS)}, coverage={quality.coverage_percent}) while the "
            f"pipeline reports the test stage passed={stage.passed} -- {_stage_detail(StageName.TEST)}"
        )

    def test_the_done_condition_and_the_test_stage_agree(self):
        """The same contradiction as the operator sees it: a met condition beside a failed stage."""
        _require_tree()
        _require_image()
        done = evaluate_done(JUMPCLOUD_TREE, quality_report=_quality_report())
        condition = next(c for c in done.conditions if c.name == CONDITION_UNIT_TESTS)
        stage = _pipeline_report().stage(StageName.TEST)
        assert stage is not None, "the pipeline recorded no test stage"
        assert condition.met == stage.passed, (
            f"the Definition_Of_Done reports {CONDITION_UNIT_TESTS}={condition.status.value} while the "
            f"pipeline reports the test stage passed={stage.passed} -- {_stage_detail(StageName.TEST)}"
        )


class TestSplitInterpretersAreNotDetected:
    """`bugfix.md` 2.3, edge case -- the SDK in one interpreter, ``pytest`` in another.

    This is the reproduction host's own configuration and the case 2.3 exists for.
    Two fake interpreters stand in for it, so the case is exercised on any host.
    """

    @staticmethod
    def _fake_interpreter(directory: Path, name: str, *, has_sdk: bool, has_pytest: bool) -> Path:
        """Write an interpreter that succeeds on ``python -c 'import X'`` selectively."""
        importable = [module for module, present in (("sdk", has_sdk), ("pytest", has_pytest)) if present]
        script = directory / name
        script.write_text(
            "#!/bin/sh\n"
            "# A stand-in interpreter: it can import some modules and not others.\n"
            'case "$*" in\n'
            + "".join(f"  *import\\ {module}*) exit 0 ;;\n" for module in importable)
            + "  *import*) exit 1 ;;\n"
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    def test_neither_candidate_satisfies_both_imports(self, tmp_path):
        """The witness: a split host has no single interpreter that can run the tests."""
        sdk_only = self._fake_interpreter(tmp_path, "python-sdk", has_sdk=True, has_pytest=False)
        pytest_only = self._fake_interpreter(tmp_path, "python-pytest", has_sdk=False, has_pytest=True)

        def can_import(interpreter: Path, module: str) -> bool:
            result = _capture([str(interpreter), "-c", f"import {module}"], timeout=60.0)
            return result is not None and result[0] == 0

        capability = {
            str(candidate): (can_import(candidate, "sdk"), can_import(candidate, "pytest"))
            for candidate in (sdk_only, pytest_only)
        }
        assert capability[str(sdk_only)] == (True, False), capability
        assert capability[str(pytest_only)] == (False, True), capability
        assert not any(
            sdk and has_pytest for sdk, has_pytest in capability.values()
        ), f"one of these candidates can import both, so this is not the split-interpreter case: {capability}"

    def test_a_resolver_reports_why_each_candidate_was_rejected(self, tmp_path):
        """`bugfix.md` 2.3 -- the tool must be able to say which of the two imports failed.

        Expected to FAIL on unfixed code: ``resolve_target_python()`` returns the
        first plausible interpreter without probing either import, and
        :class:`TargetPython` has nowhere to record that it cannot run the tests.
        """
        import icplugin_builder.integrations.build_prep as build_prep

        resolver = getattr(build_prep, "resolve_test_interpreter", None)
        assert resolver is not None, (
            "nothing resolves an interpreter that can import both the SDK and pytest, so on a host where "
            "they are split nothing reports which import failed. "
            f"resolve_target_python() offers only {resolve_target_python()!r}, whose fields carry no "
            "notion of whether the plugin's tests can be run with it"
        )

        sdk_only = self._fake_interpreter(tmp_path, "python-sdk", has_sdk=True, has_pytest=False)
        pytest_only = self._fake_interpreter(tmp_path, "python-pytest", has_sdk=False, has_pytest=True)
        resolution = resolver(candidates=(str(sdk_only), str(pytest_only)))
        assert not getattr(
            resolution, "resolved", False
        ), f"neither candidate can import both the SDK and pytest, so the resolution must not succeed: {resolution!r}"
        rejections = str(getattr(resolution, "detail", ""))
        assert (
            "pytest" in rejections and str(sdk_only) in rejections
        ), f"the resolution does not say that {sdk_only} was rejected for pytest: {rejections!r}"
        assert (
            str(pytest_only) in rejections
        ), f"the resolution does not say why {pytest_only} was rejected: {rejections!r}"


class TestTheTestStageDoesNotAskThePluginImage:
    """`bugfix.md` 2.1 -- the stage must not run the tests inside the built image.

    The image provably carries neither the tests nor ``pytest``
    (:class:`TestBugConditionOneHoldsForTheJumpCloudTree`), so a command aimed at
    it cannot report anything about the plugin's tests for **any** tag. Generation
    is free here and the claim is genuinely universal over tags, which is why this
    one clause is property-based while the rest are scoped to the concrete tree.
    """

    # Feature: export-gate-and-preview-fidelity, Property 1: Bug Condition
    @settings(max_examples=100, deadline=None)
    @given(
        tag=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-._", min_size=1, max_size=24).map(
            lambda name: f"icplugin-validate/{name}:latest"
        )
    )
    def test_no_image_tag_makes_the_test_stage_run_in_the_image(self, tag):
        """**Validates: Requirements 1.1, 1.2, 1.3**"""
        specs = CodeValidator()._stage_specs(tag, JUMPCLOUD_TREE)
        test_spec = next(spec for spec in specs if spec.name == StageName.TEST)
        assert tag not in test_spec.command, (
            "the test stage runs the plugin's tests inside the built image, which carries neither "
            f"{UNIT_TEST_DIR}/ nor pytest, so its verdict is independent of the plugin: {test_spec.command}"
        )

    def test_the_stage_command_is_not_a_docker_run(self):
        """The same claim, stated over the wired default rather than a generated tag."""
        specs = CodeValidator()._stage_specs(_image_tag(), JUMPCLOUD_TREE)
        test_spec = next(spec for spec in specs if spec.name == StageName.TEST)
        assert test_spec.command[:2] != (
            "docker",
            "run",
        ), f"the test stage is a `docker run` against the plugin image: {test_spec.command}"

    def test_the_test_stage_does_not_require_docker(self):
        """`bugfix.md` 2.1 -- a host-run check has no reason to depend on the engine."""
        specs = CodeValidator()._stage_specs(_image_tag(), JUMPCLOUD_TREE)
        test_spec = next(spec for spec in specs if spec.name == StageName.TEST)
        assert not test_spec.requires_docker, (
            "the test stage is gated on Docker, so on a host without the engine it reports the "
            "Docker-unavailable message instead of anything about the plugin's tests"
        )


def test_the_toolchain_is_on_path():
    """Guard: record the toolchain each measurement above depended on.

    An assertion that fails because ``docker`` or ``insight-plugin`` was absent is
    an environmental artifact and not a finding, so the environment is stated
    rather than assumed (`bugfix.md` "Reproduction Environment").
    """
    resolved = {name: shutil.which(name) for name in ("docker", "insight-plugin", "prospector", "black")}
    missing = sorted(name for name, path in resolved.items() if path is None)
    if missing:
        pytest.skip(f"toolchain incomplete on PATH: missing {missing}; resolved {resolved}")
    assert resolved["docker"] is not None


# ---------------------------------------------------------------------------
# Task 1.2 -- ``isBugCondition_2``, the ``lint`` stage measured as the tool runs it
# ---------------------------------------------------------------------------
#
# ``bugfix.md`` states the condition as::
#
#     FUNCTION isBugCondition_2(X)
#       RETURN findings(X) <> EMPTY
#          AND FOR ALL f IN findings(X): isGenerated(path(f))
#
# and its own note says the 14 messages in 1.4 were taken with **bare** prospector
# rather than through the stage. The design went further and found that the stage
# does not run prospector at all: ``CodeValidator.__init__`` defaults
# ``lint_command`` to ``("flake8", ".")`` (``code_validator.py:203``) and
# ``api/app.py:767`` passes the same literal. So this task re-measures the stage's
# own finding set and partitions it, because the universal quantifier above is a
# claim about *every* finding and one hand-written finding refutes it.
#
# The partition has **three** buckets, not two, and the third is the one that
# matters: ``unit_test/`` is :func:`is_lint_excluded` but **not**
# :func:`is_generated` (`bugfix.md` 3.7 -- the tests stay compiled, formatted and
# run), so a finding there is in a hand-written file that the linter has no remit
# over. Reporting only "generated versus hand-written" would hide it.
#
# _Requirements: 1.4, 1.6, 2.7_

#: The three buckets a lint finding can land in, in report order.
BUCKET_GENERATED = "generated"
BUCKET_LINT_EXCLUDED_HAND_WRITTEN = "lint-excluded but hand-written"
BUCKET_PLAIN_HAND_WRITTEN = "hand-written"
BUCKET_ORDER: Tuple[str, ...] = (
    BUCKET_GENERATED,
    BUCKET_LINT_EXCLUDED_HAND_WRITTEN,
    BUCKET_PLAIN_HAND_WRITTEN,
)

#: ``path:line:col: CODE message`` -- flake8's default report line.
_FLAKE8_FINDING = re.compile(r"^(?P<path>.+?):(?P<line>\d+):(?P<column>\d+): (?P<code>[A-Z]+\d+) (?P<message>.*)$")

#: ``line too long (117 > 79 characters)`` -- the width and the bar that judged it.
_E501_WIDTH = re.compile(r"\((?P<width>\d+) > (?P<limit>\d+) characters\)")


class LintFinding(NamedTuple):
    """One finding from the ``lint`` stage, with the bucket it belongs to."""

    path: str
    line: int
    code: str
    message: str

    @property
    def bucket(self) -> str:
        """Which of the three buckets this finding's path lands in."""
        return _bucket(self.path)

    @property
    def width(self) -> Optional[int]:
        """The offending line's width when this is a width complaint, else ``None``."""
        match = _E501_WIDTH.search(self.message)
        return int(match.group("width")) if match else None


def _bucket(relative_path: str) -> str:
    """Partition ``relative_path`` into one of :data:`BUCKET_ORDER`.

    Deliberately built from the tool's **own** predicates rather than a local
    re-implementation, so the partition is the one the fix will consume.
    """
    if is_generated(relative_path):
        return BUCKET_GENERATED
    if is_lint_excluded(relative_path):
        return BUCKET_LINT_EXCLUDED_HAND_WRITTEN
    return BUCKET_PLAIN_HAND_WRITTEN


def _partition(findings: Tuple[LintFinding, ...]) -> Dict[str, Tuple[LintFinding, ...]]:
    """Group ``findings`` by bucket, with every bucket present even when empty."""
    grouped: Dict[str, list] = {bucket: [] for bucket in BUCKET_ORDER}
    for finding in findings:
        grouped[finding.bucket].append(finding)
    return {bucket: tuple(items) for bucket, items in grouped.items()}


def _partition_summary(findings: Tuple[LintFinding, ...]) -> str:
    """A one-line-per-bucket rendering, for an assertion message that is evidence."""
    grouped = _partition(findings)
    lines = [f"{len(findings)} finding(s) in total"]
    for bucket in BUCKET_ORDER:
        items = grouped[bucket]
        codes = dict(collections.Counter(item.code for item in items))
        files = dict(collections.Counter(item.path for item in items))
        lines.append(f"  {bucket}: {len(items)} {codes} across {files}")
    return "\n".join(lines)


def _parse_flake8(output: str) -> Tuple[LintFinding, ...]:
    """Parse flake8's default report, normalising the leading ``./``."""
    findings = []
    for line in output.splitlines():
        match = _FLAKE8_FINDING.match(line.strip())
        if match is None:
            continue
        path = match.group("path")
        findings.append(
            LintFinding(
                path=path[2:] if path.startswith("./") else path,
                line=int(match.group("line")),
                code=match.group("code"),
                message=match.group("message"),
            )
        )
    return tuple(findings)


def _lint_stage_command() -> Tuple[str, ...]:
    """The ``lint`` stage's command, taken from the validator rather than a literal."""
    specs = CodeValidator()._stage_specs(_image_tag(), JUMPCLOUD_TREE)
    return next(spec for spec in specs if spec.name == StageName.LINT).command


def _require_lint_tool() -> str:
    """Skip when the stage's linter is not installed; an absent tool measures nothing.

    That this skip is *reachable at all* is part of the finding: the executable is
    not in :data:`REQUIRED_TOOLS`, so nothing warns an operator before the stage
    fails on it.
    """
    executable = _lint_stage_command()[0]
    resolved = shutil.which(executable)
    if resolved is None:
        pytest.skip(
            f"the lint stage's own executable {executable!r} is not on PATH, so the stage's finding set "
            f"cannot be measured here. It is not in REQUIRED_TOOLS {REQUIRED_TOOLS}, which is why nothing "
            "reported that before the stage failed"
        )
    return resolved


@lru_cache(maxsize=1)
def _lint_stage_result() -> StageResult:
    """Run the ``lint`` stage exactly as the pipeline runs it, and nothing else.

    ``_run_stage`` is the pipeline's own runner, so the command, the working
    directory and the verdict are the stage's rather than a re-creation. It is
    called directly instead of through :func:`_pipeline_report` because the lint
    stage needs no Docker (``requires_docker=False``) and a full pipeline run costs
    an image build.
    """
    _require_tree()
    _require_lint_tool()
    validator = CodeValidator()
    spec = next(spec for spec in validator._stage_specs(_image_tag(), JUMPCLOUD_TREE) if spec.name == StageName.LINT)
    return asyncio.run(validator._run_stage(spec, JUMPCLOUD_TREE))


@lru_cache(maxsize=1)
def _lint_stage_findings() -> Tuple[LintFinding, ...]:
    """``findings(X)`` as the ``lint`` stage produces them for the JumpCloud tree."""
    result = _lint_stage_result()
    return _parse_flake8(result.stdout + result.stderr)


@lru_cache(maxsize=1)
def _repository_bar_findings() -> Tuple[LintFinding, ...]:
    """The same tree under the bar the plugins repository actually applies.

    Prospector with the resolved profile and :data:`LINT_TOOLS`, which is what the
    ``Quality_Gate`` runs -- ``pycodestyle`` deliberately excluded, because the
    repository never runs it (see the note on :data:`LINT_TOOLS`). Unfiltered here:
    the bucketing is done by the assertions, so the raw message set is visible.
    """
    _require_tree()
    profile = resolve_lint_profile()
    command = ["prospector", "--output-format", "json"]
    if profile.resolved:
        command.extend(["--profile", str(profile.path)])
    for tool in LINT_TOOLS:
        command.extend(["--tool", tool])
    result = _capture(command, cwd=JUMPCLOUD_TREE, timeout=600.0)
    if result is None:
        pytest.skip("prospector could not be run; the repository's own bar cannot be measured")
    _, output = result
    start = output.find("{")
    if start < 0:
        pytest.skip(f"prospector produced no JSON object: {output.strip()[-400:]}")
    try:
        payload = json.loads(output[start:])
    except json.JSONDecodeError as error:  # pragma: no cover - defensive
        pytest.skip(f"prospector's JSON could not be parsed: {error}")
    findings = []
    for message in payload.get("messages") or []:
        location = message.get("location") or {}
        raw_line = location.get("line")
        findings.append(
            LintFinding(
                path=str(location.get("path", "")).strip(),
                line=int(raw_line) if isinstance(raw_line, int) and raw_line > 0 else 0,
                code=str(message.get("code") or "unknown"),
                message=str(message.get("message") or "").strip(),
            )
        )
    return tuple(findings)


@lru_cache(maxsize=1)
def _widths_the_lint_stage_flags() -> frozenset:
    """Which line widths within the plugin's own line length the stage complains about.

    One synthetic hand-written module carrying a line of every width from 80 to
    :data:`PLUGIN_LINE_LENGTH`, linted by the stage's own command in a throwaway
    tree with no configuration above it -- so the bar measured is the stage's
    default and not this repository's ``.flake8``.
    """
    _require_lint_tool()
    root = Path(tempfile.mkdtemp(prefix="icpb-lint-width-"))
    module = root / "icon_x" / "util" / "api.py"
    module.parent.mkdir(parents=True)
    assert not is_generated(module.relative_to(root).as_posix()), "the probe module must be hand-written"
    # Three words rather than two: pycodestyle exempts a comment that is a single
    # long token (its long-URL special case), which would make the probe measure
    # that exemption instead of the width bar.
    widths = range(80, PLUGIN_LINE_LENGTH + 1)
    module.write_text("".join(f"# note {'x' * (width - 7)}\n" for width in widths), encoding="utf-8")
    result = _capture(_lint_stage_command(), cwd=root, timeout=300.0)
    if result is None:
        pytest.skip("the lint stage's command could not be run over the width probe")
    flagged = {finding.width for finding in _parse_flake8(result[1]) if finding.width is not None}
    return frozenset(flagged)


class TestTheLintStageRunsFlake8AndNotProspector:
    """The wiring, measured -- `bugfix.md` 1.4's premise is wrong about the tool.

    Witnesses. These pass before the fix and are expected to *stop* passing once
    task 7.1 replaces the linter; they exist so the refutation of 1.4 is recorded
    as evidence rather than asserted in prose.
    """

    def test_the_stage_command_is_flake8(self):
        """``code_validator.py:203`` -- the constructor's default, as the stage sees it."""
        assert _lint_stage_command() == ("flake8", "."), (
            "the lint stage no longer runs `flake8 .`, so the measurement this task exists to take is "
            f"obsolete and the partition below describes a different tool: {_lint_stage_command()}"
        )

    def test_the_api_wires_the_same_literal(self):
        """``api/app.py:767`` -- the running server passes the default explicitly."""
        import icplugin_builder.api.app as app_module

        source = Path(app_module.__file__).read_text(encoding="utf-8")
        assert 'lint_command=("flake8", ".")' in source, (
            "api/app.py no longer wires `flake8 .` into the CodeValidator; the stage's linter has changed "
            "and this measurement needs retaking"
        )

    def test_the_stages_linter_is_not_a_declared_required_tool(self):
        """`bugfix.md` 1.6, design finding 1 -- nothing checks for the stage's own tool."""
        executable = _lint_stage_command()[0]
        assert executable not in REQUIRED_TOOLS, (
            f"{executable!r} is now declared in REQUIRED_TOOLS {REQUIRED_TOOLS}, so its absence would be "
            "reported before the stage failed on it -- this witness is closed"
        )

    def test_where_the_stages_linter_resolves_on_this_host(self):
        """Record the ``PATH`` fact the stage's verdict silently depends on."""
        executable = _lint_stage_command()[0]
        with_toolchain = shutil.which(executable)
        bare = shutil.which(executable, path="/usr/bin:/bin:/usr/sbin:/sbin")
        if with_toolchain is None:
            pytest.skip(
                f"{executable!r} is absent even with the toolchain prepended, so on this host the lint "
                f"stage fails for want of an undeclared tool: PATH entries {TOOLCHAIN_PATH_ENTRIES}"
            )
        if bare is not None:
            pytest.skip(
                f"{executable!r} resolves on a bare PATH at {bare}, so on this host the undeclared "
                "dependency is not at risk of being absent"
            )
        assert any(entry in with_toolchain for entry in TOOLCHAIN_PATH_ENTRIES), (
            f"{executable!r} resolves at {with_toolchain}, which is outside the toolchain directories "
            f"{TOOLCHAIN_PATH_ENTRIES} and outside a bare PATH -- record where it came from before relying "
            "on this measurement"
        )


class TestTheLintStageFindingsPartitionIntoThreeBuckets:
    """The measurement itself -- `bugfix.md` 1.4 re-taken through the stage.

    Witnesses, expected to pass on unfixed code. Each one records a fact the fix's
    size depends on, so a change in any of them means task 7 needs re-sizing
    rather than that a test broke.
    """

    def test_the_stage_reports_findings_at_all(self):
        """``findings(X) <> EMPTY`` -- the first conjunct of ``isBugCondition_2``."""
        findings = _lint_stage_findings()
        assert findings, (
            "the lint stage reports nothing for this tree, so it is not an instance of isBugCondition_2 "
            f"and 1.4 is closed by measurement: {_lint_stage_result().status.value}, "
            f"returncode={_lint_stage_result().returncode!r}"
        )

    def test_the_stage_reports_findings_in_generated_files(self):
        """The part of 1.4 that survives re-measurement: findings the author cannot fix."""
        generated = _partition(_lint_stage_findings())[BUCKET_GENERATED]
        assert generated, f"no finding lies in a generated file:\n{_partition_summary(_lint_stage_findings())}"

    def test_the_partition_has_three_buckets_and_the_third_is_populated(self):
        """``unit_test/`` is lint-excluded and **not** generated, so two buckets would hide it."""
        findings = _lint_stage_findings()
        grouped = _partition(findings)
        excluded = grouped[BUCKET_LINT_EXCLUDED_HAND_WRITTEN]
        assert excluded, f"nothing landed in the lint-excluded bucket:\n{_partition_summary(findings)}"
        for finding in excluded:
            assert is_lint_excluded(finding.path) and not is_generated(finding.path), (
                f"{finding.path} is not the third case, so the partition really is two buckets: "
                f"is_generated={is_generated(finding.path)} is_lint_excluded={is_lint_excluded(finding.path)}"
            )

    def test_the_stage_reports_width_complaints_on_hand_written_plugin_code(self):
        """**The decisive measurement.** ``E501`` on files the author both owns and must fix.

        If this holds, excluding generated files is *necessary but not sufficient*
        for 2.7 and task 7.1's replacement of the linter is part of the fix.
        """
        findings = _lint_stage_findings()
        hand_written = _partition(findings)[BUCKET_PLAIN_HAND_WRITTEN]
        assert hand_written, (
            "every finding lies outside hand-written plugin code, so excluding generated and lint-excluded "
            f"files would be sufficient and task 7 is the smaller change:\n{_partition_summary(findings)}"
        )
        widths = tuple(finding.code for finding in hand_written)
        assert set(widths) == {"E501"}, (
            "the hand-written bucket carries codes other than width complaints, so the fix cannot be a "
            f"question of line length alone:\n{_partition_summary(findings)}"
        )

    def test_every_hand_written_width_complaint_is_within_the_plugins_own_line_length(self):
        """The complaints are about the *bar*, not the code: 120 columns, judged at 79."""
        findings = _lint_stage_findings()
        hand_written = _partition(findings)[BUCKET_PLAIN_HAND_WRITTEN]
        measured = tuple((finding.path, finding.line, finding.width) for finding in hand_written)
        assert measured, f"nothing to measure:\n{_partition_summary(findings)}"
        over = tuple(item for item in measured if item[2] is None or item[2] > PLUGIN_LINE_LENGTH)
        assert not over, (
            f"these hand-written lines exceed the plugin line length of {PLUGIN_LINE_LENGTH}, so they are "
            f"genuine width defects rather than artefacts of the stage's bar: {over}"
        )
        limits = {
            int(_E501_WIDTH.search(finding.message).group("limit"))
            for finding in hand_written
            if _E501_WIDTH.search(finding.message)
        }
        assert limits and max(limits) < PLUGIN_LINE_LENGTH, (
            f"the stage judged these lines at {sorted(limits)} columns; if it applied "
            f"{PLUGIN_LINE_LENGTH} they would not be findings at all"
        )


class TestBugConditionTwoAsLiterallyStatedDoesNotHoldForTheStage:
    """The refutation, recorded rather than worked around.

    ``isBugCondition_2`` says *every* finding is in a generated file. Measured
    through the stage that actually runs, that is false -- which is the outcome
    task 1.2 exists to establish, and the reason the fix is not "exclude the
    generated files". The restatement that *does* hold is the second test: under
    the bar the plugins repository applies, no finding lies in hand-written plugin
    code, so 2.7's "a plugin a human reviewer would call clean" is a fair
    description of this tree.
    """

    def test_not_every_stage_finding_is_in_a_generated_file(self):
        """The universal quantifier fails, and one counterexample is enough."""
        findings = _lint_stage_findings()
        outside = tuple(finding for finding in findings if not is_generated(finding.path))
        assert outside, (
            "every finding is in a generated file after all, so isBugCondition_2 holds as literally "
            f"stated and task 7 need only exclude them:\n{_partition_summary(findings)}"
        )

    def test_under_the_repository_bar_no_finding_lies_in_hand_written_plugin_code(self):
        """Prospector, resolved profile, ``LINT_TOOLS`` -- what a reviewer would see.

        The evidence that replacing the linter *reaches* 2.7 rather than merely
        moving the failure: the tree is clean under the bar the repository applies,
        so a stage judged by that bar passes.
        """
        findings = _repository_bar_findings()
        hand_written = _partition(findings)[BUCKET_PLAIN_HAND_WRITTEN]
        assert not hand_written, (
            "the repository's own bar reports findings in hand-written plugin code, so replacing the "
            "linter alone does not make this tree pass and task 7 is larger still:\n"
            f"{_partition_summary(findings)}"
        )


class TestTheLintStageJudgesHandWrittenCodeAtTheRepositoryBar:
    """`bugfix.md` 2.6, 2.7 -- the expected behaviour. **Expected to FAIL now.**

    Each assertion here is the fixed tool's promise: the stage judges only files
    the plugin author may edit, at the width the repository formats to, with the
    same linter the ``Quality_Gate`` uses, and it passes for a tree a reviewer
    would call clean. These become task 7.5's acceptance check.
    """

    def test_the_stage_reports_no_finding_in_a_generated_file(self):
        """2.6 -- generated files are outside the remit, by one shared definition."""
        findings = _lint_stage_findings()
        generated = _partition(findings)[BUCKET_GENERATED]
        assert not generated, (
            "the lint stage reports findings against files the Agent_Rulebook forbids editing, so the "
            f"failure is real, correctly located, and unfixable by its audience:\n"
            f"{_partition_summary(findings)}"
        )

    def test_the_stage_reports_no_finding_outside_its_remit(self):
        """2.6 with 3.7 -- ``unit_test/`` stays compiled and run, but is not linted."""
        findings = _lint_stage_findings()
        outside = tuple(finding for finding in findings if is_lint_excluded(finding.path))
        assert not outside, (
            "the lint stage reports findings the plugins repository's own static-analysis job filters out "
            f"before running, so the tool holds a plugin to a bar its source repository does not:\n"
            f"{_partition_summary(findings)}"
        )

    def test_the_stage_applies_the_plugins_own_line_length(self):
        """2.7 -- 120 columns, which is what the repository formats to."""
        findings = _lint_stage_findings()
        judged_narrower = tuple(
            (finding.path, finding.line, finding.width, _E501_WIDTH.search(finding.message).group("limit"))
            for finding in findings
            if _E501_WIDTH.search(finding.message)
            and int(_E501_WIDTH.search(finding.message).group("limit")) < PLUGIN_LINE_LENGTH
        )
        assert not judged_narrower, (
            f"the lint stage judges line width at a narrower bar than the plugin line length of "
            f"{PLUGIN_LINE_LENGTH}, so correctly formatted code reports as a defect: {judged_narrower}"
        )

    def test_the_stage_uses_the_same_linter_as_the_quality_gate(self):
        """2.6 -- one linter and one profile, so the two subsystems cannot disagree."""
        command = _lint_stage_command()
        assert command[0] == "prospector", (
            "the lint stage and the Quality_Gate judge the same code with different linters under "
            f"different rules, which is the same two-subsystems-disagree shape as 2.4: stage runs "
            f"{command}, the gate runs prospector under {resolve_lint_profile().source!r} with "
            f"tools {LINT_TOOLS}"
        )

    def test_the_stages_linter_is_declared_so_its_absence_is_reported(self):
        """`bugfix.md` 1.6 -- a stage must not fail for want of a tool nobody checks for."""
        executable = _lint_stage_command()[0]
        assert executable in REQUIRED_TOOLS, (
            f"the lint stage runs {executable!r}, which is not in REQUIRED_TOOLS {REQUIRED_TOOLS}, so "
            "check_tooling reports a complete toolchain on a host where the stage cannot run"
        )

    def test_the_stage_passes_for_this_tree(self):
        """2.7 -- the plugin is correct, so the stage that judges it must say so."""
        result = _lint_stage_result()
        assert result.passed, (
            "a plugin whose every finding lies outside hand-written plugin code at the repository's bar "
            f"fails the lint stage, so export needs `force`. status={result.status.value} "
            f"returncode={result.returncode!r}\n{_partition_summary(_lint_stage_findings())}"
        )

    def test_the_stage_names_the_bar_that_produced_its_verdict(self):
        """2.8 -- a finding is attributable to the profile and width that raised it."""
        result = _lint_stage_result()
        recorded = f"{result.message}\n{result.stdout}\n{result.stderr}"
        profile = resolve_lint_profile()
        assert str(profile.path) in recorded and str(PLUGIN_LINE_LENGTH) in recorded, (
            "the lint stage's result names neither the profile it applied nor the line length, so a "
            f"finding cannot be attributed to the bar that produced it. Resolved profile was "
            f"{profile.source!r} at {profile.path}"
        )


class TestNoHandWrittenLineWithinThePluginsLineLengthIsAFinding:
    """2.7 as a claim about every admissible width, not only the ones this tree has.

    Generation is cheap and the claim is genuinely universal here: the stage's
    width bar is a property of the stage, so it can be measured once over a
    synthetic module and then quantified over. The concrete-tree measurements above
    show *that* ``E501`` fires on hand-written code; this shows it fires on every
    width the plugin is allowed to use, which is why the repair is the bar and not
    the code.
    """

    # Feature: export-gate-and-preview-fidelity, Property 1: Bug Condition
    @settings(max_examples=100, deadline=None)
    @given(width=st.integers(min_value=80, max_value=PLUGIN_LINE_LENGTH))
    def test_a_hand_written_line_within_the_plugin_line_length_is_not_a_finding(self, width):
        """**Validates: Requirements 1.4, 2.7**"""
        flagged = _widths_the_lint_stage_flags()
        assert width not in flagged, (
            f"the lint stage reports a hand-written line of {width} characters as too long, though the "
            f"plugin line length is {PLUGIN_LINE_LENGTH}; it flags every width in "
            f"{min(flagged)}..{max(flagged)}, so no amount of file exclusion makes correctly formatted "
            "plugin code pass"
        )


def test_the_lint_stages_own_tool_is_recorded():
    """Guard: state the linter, its resolution, and the profile every measurement above used.

    A lint measurement that silently used a different tool, a different profile, or
    a different width is not evidence, so the inputs are recorded rather than
    assumed (`bugfix.md` 2.8).
    """
    command = _lint_stage_command()
    resolved = shutil.which(command[0])
    if resolved is None:
        pytest.skip(f"the lint stage's executable {command[0]!r} is not on PATH; nothing was measured")
    profile = resolve_lint_profile()
    assert profile.detail, "resolve_lint_profile reported no provenance at all"
    assert SOURCE_PROSPECTOR == "prospector", "the gate's prospector source name changed; update the partition"


# ---------------------------------------------------------------------------
# Task 1.3 -- the format check, re-measured **through ``QualityGate.run()``**
# ---------------------------------------------------------------------------
#
# ``bugfix.md`` 1.5 reports the ``formatted`` condition unmet "on the strength of
# 5 files ``black --check`` would reformat: four generated ``schema.py`` and a
# generated ``setup.py``", and calls it systemic because "generated ``setup.py``
# also fails ``black`` in the pre-existing ``abuseipdb`` and
# ``rapid7_velociraptor`` projects".
#
# ``QualityGate._check_format`` is handed ``hand_written_python(root)``, which
# drops every name in :data:`GENERATED_FILE_NAMES` -- ``schema.py`` and
# ``setup.py`` among them -- so **no generated path can reach it**. That makes 1.5
# a measurement taken with bare ``black`` over the whole tree, outside the tool.
# Task 1.3 exists to establish that by measurement rather than by argument,
# because it is what decides the size of task 7.2.
#
# The measurement is therefore *two* runs and their difference, which is the
# thing 1.5 got wrong:
#
#   bare   ``black --check --line-length 120 .``   over the whole tree
#   tool   ``QualityGate.run()``                   over ``hand_written_python``
#
# Both are taken here, over every project tree present, and every reported path is
# bucketed with task 1.2's three-way partition -- because the third bucket is again
# the one that decides something. ``unit_test/`` is :func:`is_lint_excluded` but
# **not** :func:`is_generated`, and 3.7 keeps it format-checked, so a format
# finding there is a genuine finding its author can and must fix. It is not an
# instance of ``isBugCondition_2``.
#
# **A nil code change is an acceptable outcome for task 7.2 and the exclusion half
# of it is nil.** What the measurement did surface is separate and recorded in
# :class:`TestTheFormatFindingCannotBeAttributedToAFile`: the finding the gate
# produces carries no path at all.
#
# _Requirements: 1.5, 2.7_

#: Where the tool keeps project trees.
PROJECTS_ROOT = Path("~/.icplugin-builder/projects").expanduser()

#: The three trees `bugfix.md` 1.5 names, JumpCloud first. ``abuseipdb`` and
#: ``rapid7_velociraptor`` carry the "systemic rather than JumpCloud-specific"
#: half of the claim, so they are measured rather than taken on trust.
TREE_NAMES: Tuple[str, ...] = ("jumpcloud", "abuseipdb", "rapid7_velociraptor")

#: ``would reformat <path>`` -- black's own report line, one per file.
_WOULD_REFORMAT = re.compile(r"^would reformat (?P<path>.+?)\s*$")


def _tree(name: str) -> Path:
    """The project tree called ``name``, whether or not it exists."""
    return JUMPCLOUD_TREE if name == "jumpcloud" else PROJECTS_ROOT / name


def _require_named_tree(name: str) -> Path:
    """Skip when the named tree is absent; a synthesised one carries no evidence."""
    root = _tree(name)
    if not root.is_dir():
        pytest.skip(
            f"the {name} tree is not present at {root}; 1.5's claim about it can neither be confirmed "
            "nor refuted here, and a fabricated tree would not be the plugin the claim is about"
        )
    return root


def _require_black() -> str:
    """Skip when ``black`` is absent, and return where it resolved."""
    resolved = shutil.which("black")
    if resolved is None:
        pytest.skip(
            f"black is not on PATH even with {TOOLCHAIN_PATH_ENTRIES} prepended, so neither the bare "
            "measurement nor the gate's own format check can be taken"
        )
    return resolved


def _parse_would_reformat(output: str, root: Path) -> Tuple[str, ...]:
    """The paths black said it would reformat, relative to ``root`` and sorted.

    black prints absolute paths when handed a directory and relative ones when
    handed files, so both are normalised here rather than assuming either.
    """
    paths = set()
    for line in output.splitlines():
        match = _WOULD_REFORMAT.match(line.strip())
        if match is None:
            continue
        raw = Path(match.group("path"))
        try:
            relative = raw.relative_to(root)
        except ValueError:
            relative = raw
        paths.add(relative.as_posix())
    return tuple(sorted(paths))


@lru_cache(maxsize=len(TREE_NAMES))
def _bare_black_at_plugin_width(name: str) -> Tuple[str, ...]:
    """``black --check --line-length 120 .`` over the whole tree -- 1.5's instrument.

    Deliberately *bare*: the whole tree, no file set, no exclusion. This is the
    measurement 1.5 must have been taken with, reproduced at the width the tool
    applies so that the only difference from the gate's run is **which files are
    judged**.
    """
    root = _require_named_tree(name)
    _require_black()
    result = _capture(["black", "--check", f"--line-length={PLUGIN_LINE_LENGTH}", "."], cwd=root, timeout=600.0)
    if result is None:
        pytest.skip(f"bare black could not be run over {root}")
    return _parse_would_reformat(result[1], root)


@lru_cache(maxsize=len(TREE_NAMES))
def _bare_black_at_blacks_own_width(name: str) -> Tuple[str, ...]:
    """The same bare run with **no** ``--line-length``, so black applies its default.

    Kept as a separate measurement because it is the one that reproduces 1.5's
    generated files: a plugin is formatted to 120 columns and black's own default
    is narrower, so at the default width correctly formatted generated code reports
    as needing reformatting.
    """
    root = _require_named_tree(name)
    _require_black()
    result = _capture(["black", "--check", "."], cwd=root, timeout=600.0)
    if result is None:
        pytest.skip(f"bare black could not be run over {root}")
    return _parse_would_reformat(result[1], root)


@lru_cache(maxsize=len(TREE_NAMES))
def _quality_report_for(name: str) -> QualityReport:
    """One ``QualityGate.run()`` over the named tree, cached for the session.

    JumpCloud delegates to :func:`_quality_report` rather than running a second
    time: the gate runs the plugin's tests and rewrites ``.coverage`` in the tree
    (`bugfix.md` 1.10), so one execution per tree per session is both cheaper and
    less intrusive.
    """
    root = _require_named_tree(name)
    if root == JUMPCLOUD_TREE:
        return _quality_report()
    interpreter = _target_python().executable
    if interpreter is None:
        pytest.skip("no interpreter resolved; the Quality_Gate cannot run over this tree")
    return asyncio.run(QualityGate(python_executable=interpreter).run(root))


def _gate_format_findings(name: str) -> Tuple[CodeFinding, ...]:
    """The format findings ``QualityGate.run()`` produced for the named tree."""
    return _quality_report_for(name).by_source(SOURCE_FORMAT)


def _format_summary(name: str) -> str:
    """Both measurements side by side, bucketed -- the difference *is* the finding."""
    bare = _bare_black_at_plugin_width(name)
    findings = _gate_format_findings(name)
    file_set = hand_written_python(_tree(name))
    lines = [
        f"{name}: bare `black --check --line-length {PLUGIN_LINE_LENGTH} .` reports {len(bare)} path(s); "
        f"QualityGate.run() reports {len(findings)} format finding(s) over "
        f"{len(file_set)} hand-written file(s)"
    ]
    for path in bare:
        lines.append(f"  bare  {path} [{_bucket(path)}]{'' if path in file_set else '  (outside the gate file set)'}")
    for finding in findings:
        lines.append(f"  gate  {finding.path} [{_bucket(finding.path)}] {finding.code}: {finding.message}")
    return "\n".join(lines)


@pytest.mark.parametrize("name", TREE_NAMES)
class TestTheFiveFilesAreABareMeasurementTakenOutsideTheTool:
    """`bugfix.md` 1.5 re-measured -- witnesses, expected to pass before the fix.

    Each records a fact task 7.2's size depends on. The claim under test is 1.5's
    own: that the ``formatted`` condition is unmet because of generated
    ``schema.py`` and ``setup.py`` files. Measured through the tool, no generated
    file is judged at all, so the claim cannot be about the gate.
    """

    def test_the_generated_paths_bare_black_reports_are_outside_the_gates_file_set(self, name):
        """**The measurement task 1.3 exists to take.**

        Every generated path bare black names is absent from
        ``hand_written_python(root)``, which is precisely the list
        ``_check_format`` is handed -- so none of them can become a format finding.
        """
        root = _require_named_tree(name)
        bare = _bare_black_at_plugin_width(name)
        file_set = set(hand_written_python(root))
        generated = tuple(path for path in bare if _bucket(path) == BUCKET_GENERATED)
        leaked = tuple(path for path in generated if path in file_set)
        assert not leaked, (
            "a generated path reached the gate's own format file set, so the exclusion is not doing what "
            f"reading it suggests and task 7.2 needs a code change after all: {leaked}\n"
            f"{_format_summary(name)}"
        )

    def test_the_gates_format_findings_name_no_generated_path(self, name):
        """2.7 -- the consequence, measured on the report rather than on the file set."""
        findings = _gate_format_findings(name)
        generated = tuple(finding for finding in findings if _bucket(finding.path) == BUCKET_GENERATED)
        assert not generated, (
            "QualityGate.run() reports a format finding against a file the Agent_Rulebook forbids editing, "
            f"so 1.5 stands as written and task 7.2 is an edit:\n{_format_summary(name)}"
        )

    def test_the_only_paths_the_gate_drops_from_the_bare_run_are_generated(self, name):
        """The difference between the two runs, and what accounts for all of it.

        The substantive claim, since 2.6 says the checks judge *only* hand-written
        files and 3.7 says ``unit_test/`` stays among them: every path bare black
        reports that the gate does not judge is generated, and every path it does
        judge is not.
        """
        root = _require_named_tree(name)
        bare = set(_bare_black_at_plugin_width(name))
        file_set = set(hand_written_python(root))
        assert bare, (
            f"bare black reports nothing for this tree at {PLUGIN_LINE_LENGTH} columns, so there is no "
            f"difference between the two runs to attribute:\n{_format_summary(name)}"
        )
        dropped = {path: _bucket(path) for path in bare - file_set}
        kept = {path: _bucket(path) for path in bare & file_set}
        assert all(bucket == BUCKET_GENERATED for bucket in dropped.values()), (
            "the gate drops a path from the format check for some reason other than its being generated, "
            f"which 2.6 does not license: {dropped}\n{_format_summary(name)}"
        )
        assert all(
            bucket != BUCKET_GENERATED for bucket in kept.values()
        ), f"a generated path is inside the gate's format file set: {kept}\n{_format_summary(name)}"


class TestWhereTheFiveFilesCameFrom:
    """1.5's figure reproduced, so the discrepancy is evidence and not a guess.

    A plugin is formatted to :data:`PLUGIN_LINE_LENGTH`; black's own default is
    narrower. Run bare at the default width, the generated files report as needing
    reformatting; run at the width the tool applies, they do not. So 1.5's five
    files are a measurement of the **bar**, taken outside the tool, and not of the
    plugin.
    """

    def test_generated_files_report_at_blacks_default_width(self):
        """The default-width run names generated files -- 1.5's instrument, reproduced."""
        _require_named_tree("jumpcloud")
        at_default = _bare_black_at_blacks_own_width("jumpcloud")
        generated = tuple(path for path in at_default if _bucket(path) == BUCKET_GENERATED)
        assert generated, (
            "bare black at its own default width reports no generated file either, so 1.5's five files are "
            f"not a width artefact and their origin is still unaccounted for: {at_default}"
        )

    def test_no_generated_file_reports_at_the_plugins_own_width(self):
        """And at 120 they are clean, which is the half 1.5 leaves out."""
        _require_named_tree("jumpcloud")
        at_plugin_width = _bare_black_at_plugin_width("jumpcloud")
        generated = tuple(path for path in at_plugin_width if _bucket(path) == BUCKET_GENERATED)
        assert not generated, (
            f"generated files fail black even at the plugin's own line length of {PLUGIN_LINE_LENGTH}, so "
            f"the width is not the explanation: {generated}\n{_format_summary('jumpcloud')}"
        )

    def test_setup_py_is_clean_at_the_plugins_own_width_in_every_tree(self):
        """1.5's systemic claim, taken at the tool's width across every tree present.

        ``setup.py`` failing black in ``abuseipdb`` and ``rapid7_velociraptor`` is
        what makes 1.5 systemic. At 120 columns it fails in none of them.
        """
        measured = {}
        for name in TREE_NAMES:
            if not _tree(name).is_dir():
                continue
            measured[name] = tuple(
                path for path in _bare_black_at_plugin_width(name) if PurePosixPath(path).name == "setup.py"
            )
        if not measured:
            pytest.skip(f"no project tree is present under {PROJECTS_ROOT}")
        offenders = {name: paths for name, paths in measured.items() if paths}
        assert not offenders, (
            f"setup.py fails black at {PLUGIN_LINE_LENGTH} columns in {offenders}, so 1.5's systemic claim "
            "holds at the tool's own width and is not a default-width artefact"
        )


class TestTheGateFormatCheckIsCleanWhereItsFileSetIsClean:
    """2.7 -- the expected behaviour, and on ``abuseipdb`` it already holds.

    ``abuseipdb`` is the cleanest case in `bugfix.md` 1.5's systemic claim: bare
    black at 120 names four generated ``schema.py`` files and nothing else, and the
    gate reports **no** format finding for the tree. That is 1.5 closed by
    measurement rather than by edit, on the very tree cited to show the defect was
    not JumpCloud-specific.
    """

    def test_abuseipdb_reports_generated_files_bare_and_nothing_through_the_gate(self):
        """The whole of task 1.3 in one tree: four bare, zero through the tool."""
        _require_named_tree("abuseipdb")
        bare = _bare_black_at_plugin_width("abuseipdb")
        assert (
            bare
        ), f"bare black reports nothing for abuseipdb, so it no longer illustrates 1.5:\n{_format_summary('abuseipdb')}"
        assert all(_bucket(path) == BUCKET_GENERATED for path in bare), (
            "bare black names a file outside the generated bucket in abuseipdb, so the tree is no longer "
            f"the clean illustration of 1.5 it was:\n{_format_summary('abuseipdb')}"
        )
        assert not _gate_format_findings("abuseipdb"), (
            "the gate reports a format finding for a tree whose only unformatted files are generated, so "
            f"the exclusion is not working and task 7.2 is an edit:\n{_format_summary('abuseipdb')}"
        )


class TestAFormatFindingInTheUnitTestsIsGenuine:
    """3.7 with 2.6 -- the third bucket, and why it is not ``isBugCondition_2``.

    ``unit_test/`` is :func:`is_lint_excluded` and **not** :func:`is_generated`, and
    3.7 keeps it compiled, format-checked and run. So a format finding there is in
    a hand-written file its author may edit: a genuine finding, which the fix must
    keep reporting rather than exclude. Recorded as its own class because the
    exclusion in 2.6 is easy to over-apply in exactly this spot.
    """

    def test_the_unit_tests_are_inside_the_format_check_file_set(self):
        """The tests are judged, which is what 3.7 requires."""
        root = _require_named_tree("jumpcloud")
        file_set = hand_written_python(root)
        under_test = tuple(path for path in file_set if UNIT_TEST_DIR in PurePosixPath(path).parts)
        assert under_test, (
            f"no {UNIT_TEST_DIR}/ file is in the format check's file set, so 3.7 is already violated: " f"{file_set}"
        )
        for path in under_test:
            assert _bucket(path) == BUCKET_LINT_EXCLUDED_HAND_WRITTEN, (
                f"{path} is not the third case, so the partition this measurement relies on has changed: "
                f"is_generated={is_generated(path)} is_lint_excluded={is_lint_excluded(path)}"
            )

    def test_every_path_bare_black_reports_for_this_tree_is_hand_written(self):
        """**The answer to task 1.3's own question, for JumpCloud.**

        Both reported paths are hand-written -- one plain, one in ``unit_test/`` --
        so on this tree the format check's non-empty result is a genuine finding
        its author can fix, and *not* an instance of ``isBugCondition_2``.
        """
        _require_named_tree("jumpcloud")
        bare = _bare_black_at_plugin_width("jumpcloud")
        assert bare, f"nothing to attribute:\n{_format_summary('jumpcloud')}"
        buckets = {path: _bucket(path) for path in bare}
        assert all(bucket != BUCKET_GENERATED for bucket in buckets.values()), (
            "a generated path is among the reported files, so part of this tree's format result is "
            f"unfixable by its author after all: {buckets}\n{_format_summary('jumpcloud')}"
        )


class TestTheFormatFindingCannotBeAttributedToAFile:
    """**The counterexample this measurement surfaced.** Expected to FAIL now.

    Not the shortfall 1.5 describes and not what task 7.2 was written to fix, so it
    is recorded as its own class: ``_check_format`` passes ``--quiet``, which
    suppresses the very ``would reformat <path>`` lines it then parses, so ``named``
    is always empty and every unformatted tree yields the one tree-level finding
    ``format:.:-:would-reformat``.

    Two consequences, both about repairability rather than about the verdict:

    * the finding names no file, so 2.8's "a finding is attributable to the bar
      that produced it" fails at the more basic level of *which file*;
    * its key is identical however many files are unformatted and wherever they
      are, so fixing one of two leaves the key unchanged and the repair loop's
      finding-key arithmetic (3.8) reads a stall while progress is being made.

    This is why task 1.1 observed **1** format finding on a tree bare black reports
    **2** paths for. It is a measurement result, not an edit: no production file is
    touched by this task.
    """

    def test_quiet_is_why_the_path_is_lost(self):
        """The witness, measured rather than argued: ``--quiet`` emits no names."""
        root = _require_named_tree("jumpcloud")
        _require_black()
        unformatted = _bare_black_at_plugin_width("jumpcloud")
        if not unformatted:
            pytest.skip("this tree is already black-clean at the plugin width; there is no name to lose")
        quiet = _capture(
            ["black", "--check", "--quiet", f"--line-length={PLUGIN_LINE_LENGTH}", *unformatted],
            cwd=root,
            timeout=300.0,
        )
        loud = _capture(
            ["black", "--check", f"--line-length={PLUGIN_LINE_LENGTH}", *unformatted],
            cwd=root,
            timeout=300.0,
        )
        assert quiet is not None and loud is not None, "black could not be run over the reported files"
        assert quiet[0] == loud[0] == 1, f"black's verdict differs between the two runs: {quiet[0]} vs {loud[0]}"
        assert not _parse_would_reformat(quiet[1], root), (
            "black names the files it would reformat even under --quiet, so the flag is not what costs the "
            f"finding its path: {quiet[1].strip()[-400:]}"
        )
        assert _parse_would_reformat(loud[1], root) == unformatted, (
            "without --quiet black names exactly the reported files, which is the information the gate "
            f"discards: {loud[1].strip()[-400:]}"
        )

    def test_the_format_finding_names_the_file_black_would_reformat(self):
        """2.8 at its most basic -- a finding without a path cannot be acted on."""
        _require_named_tree("jumpcloud")
        findings = _gate_format_findings("jumpcloud")
        if not findings:
            pytest.skip("the gate reports no format finding for this tree; there is no attribution to check")
        pathless = tuple(finding for finding in findings if finding.path in (".", "", None))
        assert not pathless, (
            "the format check reports that black would reformat something without saying what, so neither "
            f"the operator nor the repair loop can act on it:\n{_format_summary('jumpcloud')}"
        )

    def test_the_gate_reports_one_finding_per_unformatted_file(self):
        """3.8 -- one key per file, so fixing one of several is visible as progress."""
        _require_named_tree("jumpcloud")
        bare = _bare_black_at_plugin_width("jumpcloud")
        if not bare:
            pytest.skip("this tree is black-clean at the plugin width; there is nothing to count")
        reported = tuple(finding.path for finding in _gate_format_findings("jumpcloud"))
        assert sorted(reported) == sorted(bare), (
            f"black would reformat {len(bare)} file(s) and the gate reports {len(reported)} finding(s) at "
            f"{reported}, so the finding count and the finding keys are both independent of how many files "
            f"are unformatted:\n{_format_summary('jumpcloud')}"
        )


class TestNoGeneratedFileCanReachTheFormatCheck:
    """1.5's five files, as a claim about every generated name at every depth.

    The concrete measurements above show that the generated files *this* tree
    carries are outside the format check's file set. This shows it for every name
    in :data:`GENERATED_FILE_NAMES` at every depth a plugin could put one, which is
    the general reason 1.5 cannot be a statement about the gate. Pure path logic,
    no I/O, so generation is cheap and the claim is genuinely universal.
    """

    # Feature: export-gate-and-preview-fidelity, Property 1: Bug Condition
    @settings(max_examples=100, deadline=None)
    @given(
        name=st.sampled_from(sorted(filename for filename in GENERATED_FILE_NAMES if filename.endswith(".py"))),
        directories=st.lists(
            st.sampled_from(["icon_thing", "actions", "create_user", "util", "connection", UNIT_TEST_DIR]),
            min_size=0,
            max_size=4,
        ),
    )
    def test_a_generated_python_file_is_never_in_the_format_checks_file_set(self, name, directories):
        """**Validates: Requirements 1.5, 2.7**"""
        root = Path(tempfile.mkdtemp(prefix="icpb-generated-reach-"))
        try:
            relative = PurePosixPath(*directories, name)
            target = root / Path(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            # Deliberately written unformatted, so that anything which did judge
            # it would certainly report it.
            target.write_text("x = {  'a' :1,\n  'b'  : 2 }\n", encoding="utf-8")
            assert relative.as_posix() not in hand_written_python(root), (
                f"{relative} is in the format check's file set, so a generated file can be reported as "
                "needing reformatting and 1.5's shortfall is reachable through the tool after all"
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)


def test_the_format_checks_black_is_recorded():
    """Guard: state the binary, the version, and the width every measurement used.

    Two ``black`` installations on the reproduction host disagree at
    :data:`PLUGIN_LINE_LENGTH` -- the toolchain's 25.11.0 reports two files for the
    JumpCloud tree and this repository's own 26.5.1 reports none -- so a format
    measurement that does not say which binary produced it is not evidence.
    ``QualityGate`` takes ``black`` from ``PATH``, so what is on ``PATH`` here is
    part of the result.
    """
    resolved = _require_black()
    version = _capture(["black", "--version"], timeout=60.0)
    assert version is not None, f"black at {resolved} would not report its version"
    assert PLUGIN_LINE_LENGTH == 120, (
        f"the plugin line length is {PLUGIN_LINE_LENGTH}, not the 120 these measurements were taken at; "
        "retake them before relying on any figure above"
    )
    assert (
        QualityGate()._black == "black"
    ), "the gate no longer takes black from PATH, so the binary recorded here is not the one it would run"


# ---------------------------------------------------------------------------
# Task 1.4 -- the pre-existing failure is a missing tool, not a divergent profile
# ---------------------------------------------------------------------------
#
# ``bugfix.md`` 1.6 says
# ``test_quality_gate.py::TestFindingsTheRepositoryWouldNotRaise::test_a_real_defect_is_still_reported``
# "already fails on this host before any change (it expects a prospector
# ``undefined-variable`` and gets an empty set)", and attributes it to
# ``resolve_lint_profile`` preferring the plugins checkout over the vendored
# fallback -- "the bar a plugin is held to is a function of the developer's home
# directory". The design contradicted that and said the two profiles are
# **byte-identical** on this host and the test **passes** with ``prospector`` on
# ``PATH``.
#
# Both documents are partly wrong, and this task's job is to say which part by
# measurement rather than by argument. What is measured here:
#
# 1. **The two profiles.** Absolute paths and SHA-256 of each, recorded rather
#    than described. They are **not** byte-identical -- the vendored copy carries a
#    17-line provenance header -- but every rule line is equal, so the two apply
#    the same bar. `bugfix.md` 1.6's *cause* is refuted; the design's *wording* is
#    too strong and is corrected here rather than repeated.
# 2. **The outcome with and without the tool.** The same test id, run as a
#    subprocess twice, differing only in whether ``prospector`` resolves.
# 3. **That the difference is the tool and nothing else.** Removing every
#    directory where ``prospector`` resolves also removes other executables, so a
#    third run restores ``prospector`` alone through a symlink and nothing else --
#    if that run passes, the collateral removals are not the cause.
# 4. **That the profile source does not decide the outcome.** A fourth run with
#    ``HOME`` redirected resolves the *fallback* profile, which is the exact
#    condition 1.6 blames. If the test still passes there, the divergence
#    hypothesis is refuted behaviourally as well as by hash.
#
# **Nothing here edits production code and nothing here edits the failing test.**
# The repair belongs to task 7.4 and, per the corrected diagnosis, it is pinning
# the profile (2.8) **plus** guarding on the tool (2.9).
#
# _Requirements: 1.6, 2.8, 2.9_

#: This repository's root, derived from this file rather than from ``os.getcwd()``,
#: because the child pytest runs below are launched with an explicit ``cwd``.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The pre-existing failure `bugfix.md` 1.6 names, as a pytest node id.
PRE_EXISTING_TEST_ID = (
    "tests/integrations/test_quality_gate.py::TestFindingsTheRepositoryWouldNotRaise"
    "::test_a_real_defect_is_still_reported"
)

#: The file and class that node id lives in, for the source-level assertion in
#: :class:`TestASkippedLinterIsIndistinguishableFromAPassingOne`.
PRE_EXISTING_TEST_FILE = REPO_ROOT / "tests" / "integrations" / "test_quality_gate.py"
PRE_EXISTING_TEST_CLASS = "TestFindingsTheRepositoryWouldNotRaise"

#: The finding the pre-existing test asserts is present.
PRE_EXISTING_EXPECTED_CODE = "undefined-variable"

#: ``N passed`` / ``N failed`` / ``N skipped`` from pytest's own summary line.
_PYTEST_COUNT = re.compile(r"(?P<count>\d+) (?P<outcome>passed|failed|skipped|error|errors)")


class ProfileEvidence(NamedTuple):
    """Both prospector profiles, with the hashes that make the comparison evidence."""

    repository_path: str
    repository_sha256: str
    fallback_path: str
    fallback_sha256: str
    bytes_identical: bool
    rules_identical: bool
    fallback_only_lines: Tuple[str, ...]

    def render(self) -> str:
        """Everything a reader needs to check the comparison themselves."""
        return (
            f"repository profile: {self.repository_path}\n"
            f"  sha256 {self.repository_sha256}\n"
            f"vendored fallback:  {self.fallback_path}\n"
            f"  sha256 {self.fallback_sha256}\n"
            f"bytes identical: {self.bytes_identical}; rule lines identical: {self.rules_identical}\n"
            f"lines present only in the fallback: {len(self.fallback_only_lines)}"
        )


def _sha256(path: Path) -> str:
    """The SHA-256 of ``path``'s contents, so a claim about it can be re-checked."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rule_lines(text: str) -> Tuple[str, ...]:
    """The lines of a prospector profile that carry rules: no comments, no blanks.

    A profile's *content* for the purpose of "which findings does this raise" is
    its rule lines. A provenance header changes the bytes and no finding, so the
    comparison that matters is this one -- and the hashes above are recorded beside
    it so the byte-level difference is visible rather than defined away.
    """
    return tuple(line.rstrip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#"))


@lru_cache(maxsize=1)
def _profile_evidence() -> ProfileEvidence:
    """Locate both profiles and record them. Skips when either is absent."""
    repository = Path(DEFAULT_LINT_PROFILE).expanduser()
    fallback = Path(FALLBACK_LINT_PROFILE)
    if not repository.is_file():
        pytest.skip(
            f"no plugins checkout profile at {repository}, so the two profiles cannot be compared on this "
            "host and 1.6's divergence claim can be neither confirmed nor refuted here"
        )
    if not fallback.is_file():
        pytest.skip(f"the vendored fallback profile is missing at {fallback}; there is nothing to compare against")
    repository_text = repository.read_text(encoding="utf-8", errors="replace")
    fallback_text = fallback.read_text(encoding="utf-8", errors="replace")
    repository_rules = _rule_lines(repository_text)
    fallback_rules = _rule_lines(fallback_text)
    return ProfileEvidence(
        repository_path=str(repository),
        repository_sha256=_sha256(repository),
        fallback_path=str(fallback),
        fallback_sha256=_sha256(fallback),
        bytes_identical=repository.read_bytes() == fallback.read_bytes(),
        rules_identical=repository_rules == fallback_rules,
        fallback_only_lines=tuple(
            line for line in fallback_text.splitlines() if line.rstrip() not in set(repository_text.splitlines())
        ),
    )


@lru_cache(maxsize=1)
def _prospector_directories() -> Tuple[str, ...]:
    """Every directory on the measurement ``PATH`` in which ``prospector`` resolves.

    Enumerated rather than assumed: on the reproduction host it is two -- the
    toolchain's ``~/Library/Python/3.9/bin`` and a pyenv shim directory -- and
    dropping only the first would leave the tool resolvable and measure nothing.
    """
    seen = []
    for directory in _tool_path().split(os.pathsep):
        if directory and directory not in seen and shutil.which("prospector", path=directory):
            seen.append(directory)
    return tuple(seen)


@lru_cache(maxsize=1)
def _path_without_prospector() -> str:
    """The measurement ``PATH`` with every ``prospector``-bearing directory removed."""
    excluded = set(_prospector_directories())
    kept = [
        directory
        for directory in dict.fromkeys(_tool_path().split(os.pathsep))
        if directory and directory not in excluded
    ]
    sanitised = os.pathsep.join(kept)
    if shutil.which("prospector", path=sanitised) is not None:
        pytest.skip(
            f"prospector still resolves under the sanitised PATH at "
            f"{shutil.which('prospector', path=sanitised)}, so the without-the-tool run would not be one"
        )
    return sanitised


@lru_cache(maxsize=1)
def _prospector_only_directory() -> str:
    """A directory holding a symlink to ``prospector`` and nothing else.

    Prepended to the sanitised ``PATH`` it restores exactly one executable, which
    is what makes the comparison attributable to the linter rather than to whatever
    else lived beside it.
    """
    resolved = _require_prospector()
    shim = Path(tempfile.mkdtemp(prefix="icpb-prospector-only-"))
    (shim / "prospector").symlink_to(resolved)
    return str(shim)


def _require_prospector() -> str:
    """Skip when ``prospector`` is absent; without it there is no contrast to draw."""
    resolved = shutil.which("prospector")
    if resolved is None:
        pytest.skip(
            f"prospector is not on PATH even with {TOOLCHAIN_PATH_ENTRIES} prepended, so the with-the-tool "
            "half of this measurement cannot be taken"
        )
    return resolved


class PytestOutcome(NamedTuple):
    """One child pytest run of :data:`PRE_EXISTING_TEST_ID`."""

    label: str
    returncode: int
    output: str

    @property
    def summary_line(self) -> str:
        """pytest's own last line -- ``1 passed in 1.5s`` and the like."""
        lines = [line.strip() for line in self.output.splitlines() if line.strip()]
        return lines[-1] if lines else ""

    @property
    def counts(self) -> Dict[str, int]:
        """The outcome counts pytest reported, parsed from that line."""
        return {
            match.group("outcome"): int(match.group("count")) for match in _PYTEST_COUNT.finditer(self.summary_line)
        }

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and self.counts.get("passed", 0) == 1

    @property
    def failed(self) -> bool:
        return self.counts.get("failed", 0) >= 1

    @property
    def was_skipped(self) -> bool:
        return not self.failed and self.counts.get("skipped", 0) >= 1

    @property
    def assertion_message(self) -> str:
        """The assertion lines pytest printed, which are what an operator reads."""
        return "\n".join(line for line in self.output.splitlines() if line.lstrip().startswith(("E ", "E\t", "E   ")))

    def render(self) -> str:
        """The run, its verdict, and its message -- an assertion message that is evidence."""
        return (
            f"[{self.label}] exit={self.returncode} summary={self.summary_line!r}\n"
            f"{self.assertion_message or '  (no assertion output)'}"
        )


def _require_runnable_pytest(path_value: str, label: str) -> None:
    """Guard: a child that cannot run pytest measures the invocation, not the linter.

    ``sys.executable`` is absolute, so pytest is reachable whatever ``PATH`` says --
    but that is a claim worth checking rather than assuming, because a sanitised
    ``PATH`` is exactly the kind of change that breaks an interpreter quietly.
    """
    probe = _capture([sys.executable, "-m", "pytest", "--version"], cwd=REPO_ROOT, timeout=120.0, path=path_value)
    if probe is None or probe[0] != 0:
        pytest.skip(
            f"pytest cannot run under the {label} PATH ({probe!r}), so any outcome measured there would be "
            "a broken invocation rather than an absent linter"
        )


def _run_pre_existing_test(
    label: str, path_value: str, environment: Optional[Tuple[Tuple[str, str], ...]] = None
) -> PytestOutcome:
    """Run :data:`PRE_EXISTING_TEST_ID` as a child, under ``path_value``."""
    _require_runnable_pytest(path_value, label)
    result = _capture(
        [sys.executable, "-m", "pytest", PRE_EXISTING_TEST_ID, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        timeout=600.0,
        path=path_value,
        environment=dict(environment) if environment else None,
    )
    if result is None:
        pytest.skip(f"the {label} run of {PRE_EXISTING_TEST_ID} could not be started")
    return PytestOutcome(label=label, returncode=result[0], output=result[1])


@lru_cache(maxsize=1)
def _outcome_with_prospector() -> PytestOutcome:
    """The test with the tool present -- the design's claim, measured."""
    _require_prospector()
    return _run_pre_existing_test("prospector present", _tool_path())


@lru_cache(maxsize=1)
def _outcome_without_prospector() -> PytestOutcome:
    """The same test with every ``prospector`` directory removed from ``PATH``."""
    _require_prospector()
    return _run_pre_existing_test("prospector absent", _path_without_prospector())


@lru_cache(maxsize=1)
def _outcome_with_only_prospector_restored() -> PytestOutcome:
    """The sanitised ``PATH`` plus a directory containing only ``prospector``."""
    _require_prospector()
    return _run_pre_existing_test(
        "prospector restored alone",
        os.pathsep.join([_prospector_only_directory(), _path_without_prospector()]),
    )


def _prospector_path_working_under_home(home: str) -> str:
    """A ``PATH`` whose ``prospector`` still runs when ``HOME`` is ``home``.

    **The trap this exists for, found by falling into it.** Redirecting ``HOME``
    hides the plugins checkout, which is the point -- but it also breaks a
    ``pip install --user`` prospector, whose ``site-packages`` is resolved from
    ``HOME``: ``~/Library/Python/3.9/bin/prospector`` then dies with
    ``ModuleNotFoundError: No module named 'prospector'``. A run like that measures
    a broken tool and would look exactly like a profile that changed the outcome.

    This host happens to carry a second, HOME-independent installation under
    ``~/.pyenv``, so a working candidate exists; each is probed rather than assumed,
    and when none survives the redirection the measurement is skipped instead of
    reported.
    """
    for directory in _prospector_directories():
        candidate = os.pathsep.join([directory, _path_without_prospector()])
        probe = _capture(["prospector", "--version"], timeout=120.0, path=candidate, environment={"HOME": home})
        if probe is not None and probe[0] == 0:
            return candidate
    pytest.skip(
        "every prospector on this host stops working when HOME is redirected (a --user installation "
        f"resolves its site-packages from HOME), so 1.6's condition cannot be reproduced this way: "
        f"{_prospector_directories()}"
    )
    raise AssertionError("unreachable")  # pragma: no cover - pytest.skip raises


@lru_cache(maxsize=1)
def _repository_profile_hidden_run() -> Tuple[str, str, PytestOutcome]:
    """The tool present, ``HOME`` redirected, so the **fallback** profile is resolved.

    This is 1.6's own condition -- the bar decided by the developer's home
    directory -- reproduced without moving anybody's files.

    Returns:
        The redirected ``HOME``, the ``PATH`` used, and the outcome.
    """
    _require_prospector()
    home = tempfile.mkdtemp(prefix="icpb-home-without-plugins-")
    path_value = _prospector_path_working_under_home(home)
    return home, path_value, _run_pre_existing_test("repository profile hidden", path_value, (("HOME", home),))


@lru_cache(maxsize=8)
def _resolved_profile_under(path_value: str, home: Optional[str] = None) -> Dict[str, Optional[str]]:
    """What ``resolve_lint_profile()`` resolves in a child under those conditions.

    Measured in the child rather than in this process, because the claim under test
    is about what the *child pytest run* was judged by.
    """
    snippet = (
        "import hashlib, json;"
        "from icplugin_builder.integrations.build_prep import resolve_lint_profile;"
        "p = resolve_lint_profile();"
        "print(json.dumps({'path': p.path, 'source': p.source, "
        "'sha256': hashlib.sha256(open(p.path, 'rb').read()).hexdigest() if p.path else None}))"
    )
    result = _capture(
        [sys.executable, "-c", snippet],
        cwd=REPO_ROOT,
        timeout=120.0,
        path=path_value,
        environment={"HOME": home} if home else None,
    )
    if result is None or result[0] != 0:
        pytest.skip(f"could not resolve the lint profile in a child process: {result!r}")
    start = result[1].find("{")
    return json.loads(result[1][start:])


class TestBothProfilesApplyTheSameRules:
    """`bugfix.md` 1.6's stated cause, refuted with hashes rather than with prose.

    Witnesses: expected to pass before and after the fix. They establish that the
    two profiles cannot explain a difference in findings, which is what makes the
    tool-presence measurement below the explanation rather than a second guess.

    **A correction to the design, recorded rather than smoothed over.** The design
    says the two files are *byte-identical*. On this host they are not: the vendored
    copy carries a provenance header, so the hashes differ. Every **rule** line is
    equal, which is what "the same bar" means, and that is what these tests assert.
    """

    def test_the_tool_resolves_the_repository_profile_on_this_host(self):
        """The premise of 1.6: the checkout wins when it is present."""
        evidence = _profile_evidence()
        profile = resolve_lint_profile()
        assert profile.resolved, f"no profile resolved at all: {profile.detail}"
        assert profile.source == LINT_PROFILE_SOURCE_REPOSITORY, (
            "the repository profile exists but was not preferred, so the resolution order 1.6 describes no "
            f"longer holds: resolved {profile.source!r} at {profile.path}\n{evidence.render()}"
        )
        assert str(Path(profile.path)) == evidence.repository_path, (
            f"resolve_lint_profile chose {profile.path}, not the checkout profile this comparison is "
            f"about:\n{evidence.render()}"
        )

    def test_both_profiles_are_recorded_with_their_hashes(self):
        """The evidence itself: two absolute paths, two SHA-256s, both stated."""
        evidence = _profile_evidence()
        assert evidence.repository_sha256 and evidence.fallback_sha256, evidence.render()
        assert evidence.repository_path != evidence.fallback_path, (
            "the repository profile and the vendored fallback are the same file, so there are not two "
            f"profiles to diverge:\n{evidence.render()}"
        )

    def test_the_two_profiles_apply_the_same_rules(self):
        """**The refutation of 1.6's stated cause.** Same rules, so the same findings.

        If this ever fails, the divergence hypothesis is live again and task 7.4's
        remedy has to be reconsidered -- which is why it asserts rule equality and
        reports both hashes instead of asserting a hash.
        """
        evidence = _profile_evidence()
        assert evidence.rules_identical, (
            "the repository profile and the vendored fallback disagree about the rules, so which one "
            "resolved really can change a finding and `bugfix.md` 1.6's cause is live after all:\n"
            f"{evidence.render()}"
        )

    def test_the_byte_difference_is_provenance_and_not_rules(self):
        """The design's "byte-identical" corrected: the bytes differ, by comments only."""
        evidence = _profile_evidence()
        substantive = tuple(
            line for line in evidence.fallback_only_lines if line.strip() and not line.lstrip().startswith("#")
        )
        assert not substantive, (
            "the vendored fallback carries rule lines the repository profile does not, so the difference "
            f"between them is more than provenance: {substantive}\n{evidence.render()}"
        )
        if evidence.bytes_identical:
            pytest.skip(
                "the two profiles are byte-identical on this host, which is what the design claimed; there "
                f"is no header difference left to attribute:\n{evidence.render()}"
            )
        assert evidence.fallback_only_lines, (
            "the bytes differ but no line is unique to the fallback, so the difference is whitespace or "
            f"line endings and should be recorded as such rather than as a header:\n{evidence.render()}"
        )


class TestThePreExistingFailureFollowsTheToolAndNotTheProfile:
    """The measurement task 1.4 exists to take: the same test, two ``PATH``s.

    Witnesses. On unfixed code the two outcomes differ, and the difference is
    attributable to ``prospector``'s presence -- not to which profile resolved, and
    not to the other executables that share its directories.

    **A host fact worth recording, because it nearly produced a wrong answer**:
    there are two prospector installations here. The toolchain's
    ``~/Library/Python/3.9/bin/prospector`` is a ``--user`` install and stops
    importing when ``HOME`` moves; the one under ``~/.pyenv`` does not. So which
    prospector a ``PATH`` finds decides whether the HOME-redirected probe measures a
    fallback profile or a dead linter. :func:`_prospector_path_working_under_home`
    picks one that survives and skips when none does.
    """

    def test_the_test_passes_with_prospector_on_path(self):
        """The design's claim, measured: 1 passed, exit 0."""
        outcome = _outcome_with_prospector()
        assert outcome.passed, (
            "the pre-existing test does not pass even with prospector on PATH, so its failure has a cause "
            f"neither `bugfix.md` 1.6 nor the design has identified:\n{outcome.render()}"
        )

    def test_the_test_fails_with_prospector_absent(self):
        """`bugfix.md` 1.6's symptom, reproduced by removing the tool and nothing else."""
        outcome = _outcome_without_prospector()
        assert outcome.failed, (
            "the pre-existing test does not fail with prospector removed from PATH, so the missing-tool "
            f"diagnosis does not reproduce here:\n{outcome.render()}"
            f"\nremoved directories: {_prospector_directories()}"
        )
        assert PRE_EXISTING_EXPECTED_CODE in outcome.assertion_message and "set()" in outcome.assertion_message, (
            "the failure is not the empty finding set 1.6 describes, so it is a different failure and the "
            f"comparison below is not about the same thing:\n{outcome.render()}"
        )

    def test_the_two_outcomes_differ(self):
        """The task's own instruction: run it both ways and assert the outcomes differ."""
        present = _outcome_with_prospector()
        absent = _outcome_without_prospector()
        assert present.passed and absent.failed, (
            "the outcome is the same with and without prospector, so the tool's presence is not what "
            f"decides it:\n{present.render()}\n{absent.render()}"
        )
        assert present.returncode != absent.returncode, (
            f"both runs exited {present.returncode}, so nothing distinguishes them:\n"
            f"{present.render()}\n{absent.render()}"
        )

    def test_restoring_only_prospector_restores_the_pass(self):
        """The confound, closed: the other tools in those directories are not the cause.

        Sanitising ``PATH`` removes whole directories, so it removes
        ``insight-plugin`` and friends along with the linter. Putting back a symlink
        to ``prospector`` alone -- and nothing else -- makes the test pass again,
        which attributes the failure to the linter specifically.
        """
        restored = _outcome_with_only_prospector_restored()
        assert restored.passed, (
            "restoring prospector alone did not restore the pass, so something else the sanitised PATH "
            f"removed is implicated and the diagnosis is incomplete:\n{restored.render()}\n"
            f"removed directories: {_prospector_directories()}\n"
            f"restored via: {_prospector_only_directory()}"
        )

    def test_the_same_profile_is_resolved_in_both_runs(self):
        """Nothing about the profile changed between the two runs, so it explains nothing."""
        present = _resolved_profile_under(_tool_path())
        absent = _resolved_profile_under(_path_without_prospector())
        assert present == absent, (
            "the two runs resolved different profiles, so the difference in outcome is not attributable to "
            f"the tool alone: present={present} absent={absent}"
        )

    def test_the_test_still_passes_when_the_fallback_profile_is_the_one_resolved(self):
        """**1.6's condition, reproduced -- and the outcome does not change.**

        ``HOME`` is redirected, so no plugins checkout is visible and
        ``resolve_lint_profile`` returns the vendored copy. That is precisely "the
        bar a plugin is held to is a function of the developer's home directory",
        and the test passes under it, so the home directory does not decide this
        outcome.

        The ``PATH`` comes from :func:`_prospector_path_working_under_home` because a
        redirected ``HOME`` breaks a ``--user`` prospector, and a broken linter here
        would masquerade as a profile that mattered.
        """
        home, path_value, outcome = _repository_profile_hidden_run()
        resolved = _resolved_profile_under(path_value, home)
        assert resolved["source"] == LINT_PROFILE_SOURCE_FALLBACK, (
            "redirecting HOME did not make the fallback profile the resolved one, so this run does not "
            f"reproduce 1.6's condition: {resolved}"
        )
        assert resolved["sha256"] == _profile_evidence().fallback_sha256, (
            "the profile resolved under the redirected HOME is not the vendored copy this comparison "
            f"hashed: {resolved}\n{_profile_evidence().render()}"
        )
        assert outcome.passed, (
            "the pre-existing test fails when the vendored fallback is the profile in force, so which "
            f"profile resolved does change the outcome after all:\n{outcome.render()}\nresolved {resolved}"
        )


class TestASkippedLinterIsIndistinguishableFromAPassingOne:
    """**The counterexample.** The assertion cannot tell silence from absence.

    `bugfix.md` 3.6 and parent Requirements 26.4 / 27.5 require a skipped check to
    stay distinguishable from a passing one, and parent design Property 58 makes
    that distinction. Here the same distinction fails one level up, inside a *test
    assertion*: ``_check_prospector`` records the skip on ``QualityReport.skipped``
    and returns no findings, and the assertion reads only ``by_source``.

    The first test is a witness -- it records the mechanism and passes now. The
    other two are the expected behaviour and are **expected to FAIL** until task
    7.4 lands: the check must skip when the tool is absent (2.9), and it must pin
    the profile it depends on rather than discovering it (2.8).
    """

    @staticmethod
    def _plugin_with_a_real_defect(root: Path) -> Path:
        """The pre-existing test's own fixture: ``requests`` used and never imported."""
        package = root / "icon_x"
        package.mkdir()
        (package / "api.py").write_text("def fetch(url):\n    return requests.get(url)\n", encoding="utf-8")
        return root

    def test_an_absent_linter_produces_the_same_finding_set_as_a_clean_one(self, tmp_path):
        """The mechanism, measured in process: no findings, a skip note, an empty message."""
        root = self._plugin_with_a_real_defect(tmp_path)
        report = asyncio.run(QualityGate(run_tests=False, prospector_executable="prospector-not-installed").run(root))
        assert report.by_source(SOURCE_PROSPECTOR) == (), (
            "a prospector that does not exist produced findings, so this is not the case the pre-existing "
            f"test trips over: {report.by_source(SOURCE_PROSPECTOR)}"
        )
        assert any("prospector" in note for note in report.skipped), (
            f"the gate did not record that prospector was skipped, so the information the assertion needs "
            f"is not merely unread -- it is absent: {report.skipped}"
        )
        assert PRE_EXISTING_EXPECTED_CODE not in report.render(), (
            "the rendered report names the defect after all, so the assertion would have passed: "
            f"{report.render()!r}"
        )
        assert not report.render().strip(), (
            "the message the failing assertion prints is `report.render()`; if it carried the skip note an "
            f"operator could tell the two cases apart from the failure alone: {report.render()!r}"
        )

    def test_the_check_is_skipped_rather_than_failed_when_the_linter_is_absent(self):
        """2.9 -- **expected to FAIL now.** A missing tool is a skip, not a false failure.

        This is the distinction parent design Property 58 makes, asked of the test
        rather than of the report: with ``prospector`` off ``PATH`` the run must
        report a skip, because "the linter said nothing" and "the linter never ran"
        are different outcomes and only one of them is a defect in the plugin.
        """
        outcome = _outcome_without_prospector()
        assert outcome.was_skipped, (
            "with prospector absent the content-dependent check fails instead of skipping, so a host "
            "without the linter reports a defect in this tool rather than an unrunnable check "
            f"(parent Req 26.4, 27.5):\n{outcome.render()}\nremoved directories: {_prospector_directories()}"
        )

    def test_the_content_dependent_check_pins_the_profile_it_depends_on(self):
        """2.8 -- **expected to FAIL now.** The expected finding is a function of the profile.

        ``QualityGate`` already accepts ``lint_profile``; the check does not pass
        one, so its expectation is judged by whichever profile the host happens to
        offer. The two profiles agree today (:class:`TestBothProfilesApplyTheSameRules`),
        which is why this is a latent dependency rather than the current failure --
        and why pinning it is a separate half of the repair from the tool guard.
        """
        source = PRE_EXISTING_TEST_FILE.read_text(encoding="utf-8")
        start = source.find(f"class {PRE_EXISTING_TEST_CLASS}")
        assert (
            start >= 0
        ), f"{PRE_EXISTING_TEST_CLASS} is no longer in {PRE_EXISTING_TEST_FILE}; retake this measurement"
        following = source.find("\nclass ", start + 1)
        body = source[start : following if following > 0 else len(source)]
        assert "lint_profile=" in body, (
            f"{PRE_EXISTING_TEST_CLASS} constructs its QualityGate without an explicit lint_profile, so the "
            "profile that decides its expected finding is whatever `resolve_lint_profile()` discovers on "
            f"the host -- which is what 2.8 requires pinned. Resolved here: {resolve_lint_profile().source!r} "
            f"at {resolve_lint_profile().path}"
        )


def test_the_pre_existing_failures_inputs_are_recorded():
    """Guard: state the linter, the profiles, and the two ``PATH``s used above.

    A tool-presence measurement that does not say where the tool was, which
    profiles were compared, or what was removed from ``PATH`` is not evidence
    (`bugfix.md` 2.8).
    """
    resolved = _require_prospector()
    version = _capture(["prospector", "--version"], timeout=120.0)
    assert version is not None, f"prospector at {resolved} would not report its version"
    evidence = _profile_evidence()
    assert evidence.repository_sha256 != "" and evidence.fallback_sha256 != "", evidence.render()
    assert _prospector_directories(), "no directory on the measurement PATH holds prospector, yet it resolved"
    assert PRE_EXISTING_TEST_FILE.is_file(), f"the pre-existing test's file is missing at {PRE_EXISTING_TEST_FILE}"


# ---------------------------------------------------------------------------
# Task 1.5 -- the bar is discovered per run and never reported
# ---------------------------------------------------------------------------
#
# ``bugfix.md`` 2.8 requires that a lint or format result "report which prospector
# profile was resolved, from which source, and which line length was applied, so
# that a finding is attributable to the bar that produced it". The design's fifth
# finding states the defect: ``resolve_lint_profile`` records ``source`` and
# ``detail`` on the :class:`LintProfile`, and the gate surfaces that detail **only**
# when the profile is non-authoritative or unresolved.
#
# So the measurement is two runs of the same gate over the same tree, differing
# only in which profile resolves:
#
#   authoritative  the plugins checkout present  -> ``source='repository'``
#   fallback       the checkout hidden           -> ``source='fallback'``
#
# **"Hidden" means ``HOME`` redirected, never a file moved.**
# ``~/Documents/GitHub/insightconnect-plugins/`` belongs to somebody else and is
# read-only as far as this suite is concerned; ``resolve_lint_profile`` expands
# ``~``, so a child with ``HOME`` pointing at an empty temporary directory sees no
# checkout and falls back. That is task 1.4's technique, reused rather than
# reinvented -- including :func:`_prospector_path_working_under_home`, because
# redirecting ``HOME`` also breaks the ``pip install --user`` prospector whose
# ``site-packages`` resolves from ``HOME``, and a dead linter in the hidden run
# would look exactly like a profile that changed the outcome.
#
# One addition of this task's own, for a reason worth recording: the hidden run
# substitutes **only** ``prospector``, through a symlink in a directory of its own
# (:func:`_prospector_shim_under_home`), and keeps the rest of :func:`_tool_path`
# intact. Two ``black`` installations on this host disagree at
# :data:`PLUGIN_LINE_LENGTH` -- the guard in task 1.3 records it -- so letting the
# hidden run resolve a different ``black`` would change the format finding for a
# reason that has nothing to do with the profile.
#
# What the two runs measure, and what they found (`bugfix.md` 1.6, 2.8):
#
#   * the **authoritative** run's report says nothing whatever about the profile:
#     ``skipped`` carries only the caller's own note, and neither the path, the
#     word ``repository``, nor the width appears in ``summary()`` or ``render()``;
#   * the **fallback** run's report carries the full provenance detail, naming the
#     absent checkout and the vendored copy;
#   * both runs produce the *same* findings over the *same* file set, so the only
#     difference between the two reports is the disclosure itself.
#
# **The line length is reported by neither**, which is the half of 2.8 the task
# text names explicitly. Two widths are in play in one tool: ``_check_format``
# passes ``--line-length 120`` (:data:`PLUGIN_LINE_LENGTH`), while task 1.2
# measured the ``lint`` stage's ``flake8`` judging the same tree at pycodestyle's
# 79. Neither figure reaches any report. Task 1.2 already asserts that the *stage*
# names its bar; the *gate*'s width is this task's, and task 1.3's ``--quiet``
# finding is the same requirement failing at the more basic level of which file.
#
# **Nothing here edits production code.** The repair is task 7.3.
#
# _Requirements: 1.6, 2.8_

#: Printed by the child before its JSON payload, so a warning on stdout cannot be
#: mistaken for the measurement.
_DISCLOSURE_MARKER = "__PROFILE_DISCLOSURE__"

#: Run the gate in a child and report everything it discloses about the bar.
#:
#: ``run_tests=False`` deliberately: the plugin's unit tests say nothing about
#: which profile judged it, and running them rewrites ``.coverage`` in the tree
#: (`bugfix.md` 1.10). The full run *is* measured, once, by
#: :func:`_quality_report` in
#: :meth:`TestTheReportStatesWhichBarJudgedThePlugin.test_the_real_gate_run_names_the_bar`,
#: so the claim is not an artefact of the switch.
_DISCLOSURE_SNIPPET = (
    "import asyncio, hashlib, json, sys\n"
    "from pathlib import Path\n"
    "from icplugin_builder.integrations.build_prep import PLUGIN_LINE_LENGTH, resolve_lint_profile\n"
    "from icplugin_builder.integrations.quality_gate import QualityGate\n"
    "profile = resolve_lint_profile()\n"
    "report = asyncio.run(QualityGate(run_tests=False, python_executable=sys.argv[2]).run(Path(sys.argv[1])))\n"
    "print('" + _DISCLOSURE_MARKER + "' + json.dumps({\n"
    "    'profile_path': profile.path,\n"
    "    'profile_source': profile.source,\n"
    "    'profile_sha256': (\n"
    "        hashlib.sha256(Path(profile.path).read_bytes()).hexdigest() if profile.path else None\n"
    "    ),\n"
    "    'skipped': list(report.skipped),\n"
    "    'summary': report.summary(),\n"
    "    'rendered': report.render(),\n"
    "    'finding_keys': list(report.keys()),\n"
    "    'checked_files': list(report.checked_files),\n"
    "    'line_length': PLUGIN_LINE_LENGTH,\n"
    "}))\n"
)


class GateDisclosure(NamedTuple):
    """One ``QualityGate.run()`` and everything it told an operator about the bar.

    Attributes:
        label: which of the two runs this is, for assertion messages.
        home: the ``HOME`` the child saw. The real one for the authoritative run.
        profile_path / profile_source / profile_sha256: what
            ``resolve_lint_profile()`` resolved *in the child*, since the claim
            under test is about the bar that judged that run.
        skipped / summary / rendered: the three operator-facing surfaces
            :class:`QualityReport` offers. There is no fourth: the report carries
            no field for the profile or the width (see
            :meth:`TestTheReportStatesWhichBarJudgedThePlugin.test_the_report_carries_the_bar_as_structured_data`).
        finding_keys / checked_files: recorded so the two runs can be shown to have
            judged the same code and reached the same verdict.
        line_length: the width the gate applied to ``black``, read from the tool
            rather than assumed.
    """

    label: str
    home: str
    profile_path: Optional[str]
    profile_source: Optional[str]
    profile_sha256: Optional[str]
    skipped: Tuple[str, ...]
    summary: str
    rendered: str
    finding_keys: Tuple[str, ...]
    checked_files: Tuple[str, ...]
    line_length: int

    @property
    def surface(self) -> str:
        """Everything the report puts in front of an operator, concatenated.

        The disclosure is looked for here rather than in one field, so a fix that
        reports the bar through ``summary()`` or a skip note passes just as one that
        adds a field -- the requirement is that the operator can read it, not where.
        """
        return "\n".join((self.summary, self.rendered, *self.skipped))

    def mentions(self, needle: str) -> bool:
        """Return ``True`` iff ``needle`` appears anywhere an operator would see."""
        return needle in self.surface

    def render(self) -> str:
        """The run and its whole disclosure surface -- an assertion message that is evidence."""
        return (
            f"[{self.label}] HOME={self.home}\n"
            f"  resolved profile: {self.profile_source!r} at {self.profile_path}\n"
            f"  sha256 {self.profile_sha256}\n"
            f"  applied line length: {self.line_length}\n"
            f"  summary: {self.summary!r}\n"
            f"  rendered: {self.rendered!r}\n"
            f"  skipped: {self.skipped}\n"
            f"  finding keys: {self.finding_keys}\n"
            f"  files judged: {len(self.checked_files)}"
        )


def _gate_disclosure(label: str, path_value: str, home: Optional[str] = None) -> GateDisclosure:
    """Run the gate over the JumpCloud tree in a child and collect its disclosure.

    A child rather than this process because ``HOME`` decides which profile
    resolves, and changing this process's ``HOME`` would change it for every other
    measurement in the module.
    """
    _require_tree()
    interpreter = _target_python().executable or sys.executable
    result = _capture(
        [sys.executable, "-c", _DISCLOSURE_SNIPPET, str(JUMPCLOUD_TREE), interpreter],
        cwd=REPO_ROOT,
        timeout=900.0,
        path=path_value,
        environment={"HOME": home} if home else None,
    )
    if result is None:
        pytest.skip(f"the {label} gate run could not be started")
    returncode, output = result
    marker = output.find(_DISCLOSURE_MARKER)
    if returncode != 0 or marker < 0:
        pytest.skip(f"the {label} gate run did not report a disclosure (exit {returncode}): {output.strip()[-600:]}")
    payload = json.loads(output[marker + len(_DISCLOSURE_MARKER) :].splitlines()[0])
    return GateDisclosure(
        label=label,
        home=home or os.path.expanduser("~"),
        profile_path=payload["profile_path"],
        profile_source=payload["profile_source"],
        profile_sha256=payload["profile_sha256"],
        skipped=tuple(payload["skipped"]),
        summary=payload["summary"],
        rendered=payload["rendered"],
        finding_keys=tuple(payload["finding_keys"]),
        checked_files=tuple(payload["checked_files"]),
        line_length=int(payload["line_length"]),
    )


def _prospector_shim_under_home(home: str) -> str:
    """A directory holding a symlink to a ``prospector`` that works under ``home``.

    :func:`_prospector_path_working_under_home` (task 1.4) finds *which* directory
    holds a prospector that survives the ``HOME`` redirection -- the trap being that
    a ``--user`` installation resolves its ``site-packages`` from ``HOME`` and dies
    with ``ModuleNotFoundError`` once it moves. That helper returns a whole
    sanitised ``PATH``, which also drops ``black`` and ``insight-plugin`` because
    they share a directory with the broken prospector.

    Substituting one executable instead keeps every other tool identical between
    the two runs, which matters here: task 1.3's guard records two ``black``
    installations on this host that disagree at :data:`PLUGIN_LINE_LENGTH`, so a
    hidden run that resolved a different ``black`` would differ in its format
    finding for a reason unrelated to the profile.
    """
    working = _prospector_path_working_under_home(home)
    resolved = shutil.which("prospector", path=working)
    if resolved is None:  # pragma: no cover - the helper only returns a working PATH
        pytest.skip(f"no prospector resolved under the HOME-redirected PATH {working}")
    shim = Path(tempfile.mkdtemp(prefix="icpb-prospector-under-home-"))
    (shim / "prospector").symlink_to(resolved)
    return str(shim)


@lru_cache(maxsize=1)
def _authoritative_disclosure() -> GateDisclosure:
    """The gate run with the plugins checkout present, so the repository profile wins."""
    _profile_evidence()  # skips when there is no checkout, in which case this run is not authoritative
    return _gate_disclosure("checkout present", _tool_path())


@lru_cache(maxsize=1)
def _fallback_disclosure() -> GateDisclosure:
    """The same run with the checkout hidden behind a redirected ``HOME``."""
    _profile_evidence()
    home = tempfile.mkdtemp(prefix="icpb-home-no-plugins-checkout-")
    path_value = os.pathsep.join([_prospector_shim_under_home(home), _tool_path()])
    return _gate_disclosure("checkout hidden", path_value, home)


def _width_disclosure_surface(disclosure: GateDisclosure) -> str:
    """The disclosure surface with the two incidental paths masked out.

    The question is whether the report *states* the width it judged at, and a
    temporary ``HOME`` or a profile filename that happens to contain the digits
    ``120`` would answer it by accident. Only those two paths are masked, so a
    report that names the width in any wording still satisfies the assertions
    either way.
    """
    text = disclosure.surface.replace(disclosure.home, "<HOME>")
    if disclosure.profile_path:
        text = text.replace(disclosure.profile_path, "<PROFILE>")
    return text


@lru_cache(maxsize=1)
def _quality_gate_source() -> str:
    """The gate's own source, for asserting where the disclosure is gated."""
    import icplugin_builder.integrations.quality_gate as quality_gate_module

    return Path(quality_gate_module.__file__).read_text(encoding="utf-8")


class TestTheTwoRunsDifferOnlyInTheProfileTheyResolved:
    """The measurement's premise -- witnesses, expected to pass before and after.

    Unless the two runs really are the same gate over the same tree under two
    different profiles, nothing below is attributable to the profile. So the
    premise is measured rather than assumed: which profile each run resolved, by
    path and by hash, and that both judged the same files to the same verdict.
    """

    def test_the_authoritative_run_resolves_the_repository_profile(self):
        """With the checkout present, the bar is the repository's own."""
        disclosure = _authoritative_disclosure()
        evidence = _profile_evidence()
        assert disclosure.profile_source == LINT_PROFILE_SOURCE_REPOSITORY, (
            "the run with the checkout present did not resolve the repository profile, so it is not the "
            f"authoritative case this comparison needs:\n{disclosure.render()}\n{evidence.render()}"
        )
        assert disclosure.profile_sha256 == evidence.repository_sha256, (
            f"the profile the run resolved is not the checkout profile that was hashed:\n"
            f"{disclosure.render()}\n{evidence.render()}"
        )

    def test_the_hidden_run_resolves_the_vendored_fallback(self):
        """With ``HOME`` redirected, no checkout is visible and the vendored copy wins.

        This is `bugfix.md` 1.6's own condition -- "the bar a plugin is held to is a
        function of the developer's home directory" -- reproduced without moving
        anybody's files.
        """
        disclosure = _fallback_disclosure()
        evidence = _profile_evidence()
        assert disclosure.profile_source == LINT_PROFILE_SOURCE_FALLBACK, (
            "redirecting HOME did not make the vendored copy the resolved profile, so the checkout is not "
            f"hidden from this run:\n{disclosure.render()}"
        )
        assert disclosure.profile_sha256 == evidence.fallback_sha256, (
            f"the hidden run resolved something other than the vendored copy:\n"
            f"{disclosure.render()}\n{evidence.render()}"
        )
        assert Path(_authoritative_disclosure().profile_path or "").is_file(), (
            "the checkout profile is not on disk, so this run did not hide anything -- retake the "
            f"measurement:\n{evidence.render()}"
        )

    def test_the_two_runs_judged_the_same_code_to_the_same_verdict(self):
        """So the only difference between the two reports is what each says about the bar.

        Rule-identical profiles (task 1.4) produce identical findings, which is
        exactly why the disclosure matters: an operator cannot infer the bar from
        the findings, because the findings do not vary with it.
        """
        authoritative = _authoritative_disclosure()
        fallback = _fallback_disclosure()
        assert authoritative.checked_files == fallback.checked_files, (
            f"the two runs judged different file sets, so their reports are not comparable:\n"
            f"{authoritative.render()}\n{fallback.render()}"
        )
        assert authoritative.finding_keys == fallback.finding_keys, (
            "the two runs produced different findings, so which profile resolved does change the verdict "
            f"and task 1.4's rule-equality finding needs retaking:\n"
            f"{authoritative.render()}\n{fallback.render()}"
        )


class TestOnlyANonAuthoritativeProfileIsDisclosed:
    """**The counterexample.** The report speaks up only when the bar is second-best.

    The first two tests are the measurement in both directions and the third and
    fourth are the code path that produces it. All four pass on unfixed code, and
    the second is expected to **stop** passing once task 7.3 lands -- that is the
    fix, and this class is where the before-state is recorded.
    """

    def test_the_hidden_run_discloses_the_profile_it_fell_back_to(self):
        """The disclosure that does exist: provenance, when the profile is not authoritative."""
        disclosure = _fallback_disclosure()
        evidence = _profile_evidence()
        assert disclosure.mentions(evidence.fallback_path), (
            "the fallback run's report does not name the vendored profile either, so the profile is never "
            f"disclosed at all and the gating below is moot:\n{disclosure.render()}"
        )
        assert any("profile" in note for note in disclosure.skipped), (
            f"the provenance reaches the operator through some surface other than a skip note; record which "
            f"before relying on this measurement:\n{disclosure.render()}"
        )

    def test_the_authoritative_run_discloses_nothing_about_the_profile(self):
        """**The finding.** Under the authoritative bar the operator learns nothing.

        Not the path, not the source, not even the word: a run judged by the
        repository's own profile is indistinguishable, from its report alone, from
        a run judged by anything else.
        """
        disclosure = _authoritative_disclosure()
        evidence = _profile_evidence()
        disclosed = {
            "the profile path": disclosure.mentions(evidence.repository_path),
            "the source label": disclosure.mentions(LINT_PROFILE_SOURCE_REPOSITORY),
            "the word 'profile'": disclosure.mentions("profile"),
            "the word 'prospector'": disclosure.mentions("prospector"),
        }
        assert not any(disclosed.values()), (
            "the authoritative run does disclose the bar after all, so 2.8 is closed by measurement for the "
            f"profile half and task 7.3 shrinks: {disclosed}\n{disclosure.render()}"
        )

    def test_the_disclosure_is_gated_on_the_profile_being_non_authoritative(self):
        """The code path, so the claim is about the gate and not about this one tree.

        ``_check_prospector`` appends the detail to ``skipped`` inside
        ``if not profile.is_authoritative`` and in the unresolved branch. There is
        no unconditional path from :class:`LintProfile` to the report.
        """
        source = _quality_gate_source()
        start = source.find("async def _check_prospector")
        assert start >= 0, "quality_gate no longer has a _check_prospector; retake this measurement"
        following = source.find("\n    async def ", start + 1)
        body = source[start : following if following > 0 else len(source)]
        assert "if not profile.is_authoritative:" in body, (
            "the disclosure is no longer gated on the profile being non-authoritative; retake this "
            f"measurement against the current code:\n{body}"
        )
        carrying = tuple(line.strip() for line in body.splitlines() if "profile.detail" in line)
        assert carrying and all(line.startswith("skipped.append(") for line in carrying), (
            "the profile detail reaches the report by some route other than a skip note, so this "
            f"measurement of how provenance is surfaced needs retaking: {carrying}"
        )

    def test_no_other_layer_consumes_the_resolved_profile(self):
        """There is no second route to the operator: one production caller, and it is the gate.

        Task 7.3 requires the profile carried "on ``QualityReport`` and on the
        ``lint`` stage's result, and serialized into the export payload". Today
        nothing outside the gate so much as reads it, which is why the export
        preview cannot state the bar even when the gate knows it.
        """
        package = REPO_ROOT / "icplugin_builder"
        readers = sorted(
            path.relative_to(package).as_posix()
            for path in package.rglob("*.py")
            if path.name != "build_prep.py" and "resolve_lint_profile" in path.read_text(encoding="utf-8")
        )
        assert readers == ["integrations/quality_gate.py"], (
            "the set of modules consuming the resolved lint profile has changed, so this measurement of "
            f"where provenance can reach an operator needs retaking: {readers}"
        )


class TestNoReportNamesTheWidthItJudgedAt:
    """The line length half of 2.8, which the task text names explicitly.

    Witnesses. The gate formats at :data:`PLUGIN_LINE_LENGTH` and says so nowhere.
    Two widths are in play in one tool: task 1.2 measured the ``lint`` stage's
    ``flake8`` judging the same tree at pycodestyle's 79, while ``_check_format``
    passes 120 -- so "at what line length" has two answers for one plugin and the
    reports give neither. The stage's half is asserted by task 1.2; this is the
    gate's.
    """

    def test_the_gate_judges_formatting_at_the_plugins_own_line_length(self):
        """The width is applied -- the complaint is that it is not reported."""
        assert QualityGate()._line_length == PLUGIN_LINE_LENGTH, (
            f"the gate no longer formats at {PLUGIN_LINE_LENGTH}, so the width this measurement is about "
            f"has changed: {QualityGate()._line_length}"
        )
        command_line = "--line-length={self._line_length}"
        assert command_line in _quality_gate_source(), (
            "the format check no longer passes an explicit --line-length; retake this measurement against "
            "the current code"
        )

    def test_neither_run_names_the_width_it_judged_at(self):
        """**The finding.** Not in the summary, not in a finding, not in a skip note."""
        for disclosure in (_authoritative_disclosure(), _fallback_disclosure()):
            stated = _width_disclosure_surface(disclosure)
            assert str(disclosure.line_length) not in stated, (
                f"the {disclosure.label} run does name the width after all, so the line-length half of 2.8 "
                f"is closed by measurement:\n{disclosure.render()}"
            )
            assert "line length" not in stated and "line-length" not in stated, (
                f"the {disclosure.label} run's report mentions the line length in words; record how before "
                f"relying on this measurement:\n{disclosure.render()}"
            )


class TestTheReportStatesWhichBarJudgedThePlugin:
    """`bugfix.md` 2.8 -- the expected behaviour. **Expected to FAIL now.**

    Each assertion is the fixed tool's promise: whichever profile judged the
    plugin, the report says which one, where it came from, and at what width, so a
    finding is attributable to the bar that produced it. These become part of task
    7.5's acceptance check.

    The attribution failure task 1.3 surfaced is the same requirement one level
    down -- ``_check_format``'s ``--quiet`` costs a format finding its *path* -- so
    it is asserted there and cross-referenced rather than repeated here.
    """

    def test_every_report_names_the_profile_it_applied(self):
        """The path, in both directions: an authoritative bar is as worth naming as a stale one."""
        undisclosed = tuple(
            disclosure
            for disclosure in (_authoritative_disclosure(), _fallback_disclosure())
            if not disclosure.mentions(str(disclosure.profile_path))
        )
        assert not undisclosed, (
            "a lint result does not name the prospector profile that produced it, so an operator holding "
            "two differing reports cannot tell whether the plugin or the bar changed:\n"
            + "\n".join(disclosure.render() for disclosure in undisclosed)
        )

    def test_every_report_names_the_source_the_profile_came_from(self):
        """``repository`` or ``fallback`` -- 2.8 asks for the source, not only the path."""
        undisclosed = tuple(
            disclosure
            for disclosure in (_authoritative_disclosure(), _fallback_disclosure())
            if not disclosure.mentions(str(disclosure.profile_source))
        )
        assert not undisclosed, (
            "a lint result does not say whether the profile came from the plugins repository or from the "
            "vendored copy, so the report cannot say that two operators with different checkouts are "
            "being held to different bars -- which is the tradeoff 2.8 states rather than removes:\n"
            + "\n".join(disclosure.render() for disclosure in undisclosed)
        )

    def test_every_report_names_the_line_length_it_applied(self):
        """The width, which the task text names explicitly and no report carries."""
        undisclosed = tuple(
            disclosure
            for disclosure in (_authoritative_disclosure(), _fallback_disclosure())
            if str(disclosure.line_length) not in _width_disclosure_surface(disclosure)
        )
        assert not undisclosed, (
            f"a format result does not state that it judged the plugin at {PLUGIN_LINE_LENGTH} columns, so "
            "a `would-reformat` finding is indistinguishable from one raised at black's own narrower "
            "default -- and task 1.2 measured the lint stage applying 79 to the same tree, so the operator "
            "sees two widths and is told neither:\n" + "\n".join(disclosure.render() for disclosure in undisclosed)
        )

    def test_the_report_carries_the_bar_as_structured_data(self):
        """Design change 3 -- carried *on* ``QualityReport``, so a serializer can pass it on.

        Prose in a skip note cannot be put in the export payload, which is where
        2.8's "any check whose expected outcome depends on profile content" and
        task 12.1's "the profile and interpreter named" both need it.
        """
        fields = tuple(QualityReport.__dataclass_fields__)
        # Deliberately a claim about what is carried, not about field names: a
        # report holding a whole LintProfile satisfies 2.8 as well as one holding
        # its path and source separately.
        missing = tuple(
            what
            for what, token in (("the profile", "profile"), ("the line length", "line_length"))
            if not any(token in field for field in fields)
        )
        assert not missing, (
            f"QualityReport carries nothing for {missing}, so the bar cannot be serialized into the export "
            f"payload however the report words it -- and task 12.1 asks the preview to name the profile. "
            f"Its fields are {fields}"
        )

    def test_the_real_gate_run_names_the_bar(self):
        """The same claim of the full run, so it is not an artefact of ``run_tests=False``.

        Every measurement above switches the plugin's unit tests off, for speed and
        to avoid rewriting ``.coverage`` in the tree. This one is the gate exactly
        as the tool runs it.
        """
        _require_tree()
        report = _quality_report()
        profile = resolve_lint_profile()
        surface = "\n".join((report.summary(), report.render(), *report.skipped))
        assert str(profile.path) in surface and str(PLUGIN_LINE_LENGTH) in surface, (
            "the gate's own report over this tree names neither the profile it applied nor the width it "
            f"judged at. Resolved profile was {profile.source!r} at {profile.path}; the report says:\n"
            f"summary={report.summary()!r}\nskipped={report.skipped}\nrendered={report.render()!r}"
        )


class TestEveryProfileTheGateAppliesIsNamedInItsReport:
    """2.8 as a claim about every profile the gate could apply, not only this host's two.

    The concrete runs above show the disclosure is a function of ``source``. This
    shows it for every source and every profile path, which is why the repair is
    reporting the bar unconditionally rather than widening the condition. Cheap and
    hermetic: a stub ``prospector`` that reports no messages, an empty tree, and a
    pinned :class:`LintProfile`, so no real toolchain and no plugin tree is needed
    and the claim is genuinely universal.
    """

    @staticmethod
    def _prospector_stub(directory: Path) -> Path:
        """A ``prospector`` that reports an empty message set and exits 0."""
        stub = directory / "prospector-stub"
        stub.write_text("#!/bin/sh\nprintf '{\"messages\": []}\\n'\n", encoding="utf-8")
        stub.chmod(0o755)
        return stub

    # Feature: export-gate-and-preview-fidelity, Property 1: Bug Condition
    @settings(max_examples=100, deadline=None)
    @given(
        source=st.sampled_from([LINT_PROFILE_SOURCE_REPOSITORY, LINT_PROFILE_SOURCE_FALLBACK]),
        name=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=20),
    )
    def test_the_report_names_the_profile_whatever_its_source(self, source, name):
        """**Validates: Requirements 1.6, 2.8**"""
        root = Path(tempfile.mkdtemp(prefix="icpb-profile-disclosure-"))
        try:
            profile_path = root / f"{name}.yaml"
            profile_path.write_text("strictness: veryhigh\n", encoding="utf-8")
            tree = root / "tree"
            tree.mkdir()
            gate = QualityGate(
                run_tests=False,
                prospector_executable=str(self._prospector_stub(root)),
                lint_profile=LintProfile(path=str(profile_path), source=source, detail=f"pinned at {profile_path}"),
            )
            report = asyncio.run(gate.run(tree))
            surface = "\n".join((report.summary(), report.render(), *report.skipped))
            assert str(profile_path) in surface, (
                f"a run judged by a {source!r} profile at {profile_path} produces a report that does not "
                f"name it, so which bar judged the plugin is disclosed only when the bar is second-best: "
                f"summary={report.summary()!r} skipped={report.skipped} rendered={report.render()!r}"
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)


def test_the_profile_provenance_measurements_inputs_are_recorded():
    """Guard: state the two profiles, the two ``HOME``s, and the prospector each run used.

    A provenance measurement that does not say which profiles were compared, which
    ``HOME`` hid the checkout, or which prospector survived the redirection is not
    evidence -- and the last of those is the trap task 1.4 fell into first
    (`bugfix.md` 2.8).
    """
    evidence = _profile_evidence()
    assert evidence.repository_path != evidence.fallback_path, evidence.render()
    authoritative = _authoritative_disclosure()
    fallback = _fallback_disclosure()
    assert authoritative.home != fallback.home, (
        "both runs saw the same HOME, so the checkout was never hidden:\n"
        f"{authoritative.render()}\n{fallback.render()}"
    )
    assert (
        Path(fallback.home).is_dir() and not (Path(fallback.home) / "Documents").exists()
    ), f"the redirected HOME at {fallback.home} is not the empty directory this measurement assumed"
    assert authoritative.line_length == fallback.line_length == PLUGIN_LINE_LENGTH, (
        f"the two runs applied different widths ({authoritative.line_length} and {fallback.line_length}), "
        "so a difference between their reports is not attributable to the profile alone"
    )


# ---------------------------------------------------------------------------
# Task 1.8 -- the ``api_client`` detector rejects the prescribed pattern
# ---------------------------------------------------------------------------
#
# ``bugfix.md`` 1.9 states the defect: the ``api_client`` condition is reported
# unmet with the detail ``icon_jumpcloud/util/api.py: no HTTP_ERROR_MAP`` for a
# plugin whose map is defined in ``util/constants.py`` and imported into
# ``util/api.py``, "even though import-from-``constants`` is the pattern
# ``~/.kiro/steering/implementation.md`` prescribes".
#
# **That claim was checked against the steering rather than taken from the bug
# report**, because ``~/.kiro/`` is the authoritative rulebook for plugin
# structure and this module is asserting that a detector contradicts it. Two
# sections of ``implementation.md`` say it, and they say it in two halves:
#
#   "## Constants Pattern (util/constants.py)
#      - `HTTP_ERROR_MAP` dict mapping status codes to `{cause, assistance}` pairs
#      - Cover at minimum: 400, 401, 403, 404, 429, 500, 503"
#
#   "## API Client Pattern (util/api.py)
#      - `_handle_status()` or `_raise_for_status()` maps HTTP codes via
#        `HTTP_ERROR_MAP` dict"
#
# So the map is **defined** under the constants heading and **used** under the
# client heading, and the steering names no other home for it. `bugfix.md` 1.9 is
# accurate: the prescribed placement is a definition in ``util/constants.py`` that
# ``util/api.py`` imports. Nothing in ``implementation.md`` asks for a second
# literal copy in ``api.py``, which is what ``_api_client_condition`` requires --
# it builds ``assigned`` from the ``ast.Assign`` / ``ast.AnnAssign`` targets of
# ``api.py`` alone (:func:`definition_of_done._defined_names`), and an
# ``ast.ImportFrom`` is neither.
#
# The concrete tree is in hand, so this is measured and not only reasoned:
# ``~/.icplugin-builder/projects/jumpcloud/`` defines the map in
# ``icon_jumpcloud/util/constants.py`` and imports it into
# ``icon_jumpcloud/util/api.py``. One detail the bug report does not record and
# that matters to the fix: the real tree uses the **absolute in-package** form,
# ``from icon_jumpcloud.util.constants import (..., HTTP_ERROR_MAP)``, not the
# relative one -- so a fix that handled only ``level > 0`` would leave the very
# tree that produced the report unmet. Design change 3 covers both, and the cases
# below enumerate both.
#
# **Six placements, enumerated rather than generated**, because they are the
# before-state task 5.1 needs, one per unit test that task names:
#
#   defined in ``api.py``            met today       -- the control
#   relative in-package import       unmet today     -- the prescribed pattern
#   absolute in-package import       unmet today     -- what JumpCloud does
#   import from outside the package  unmet today     -- and stays unmet (change 3)
#   neither defined nor imported     unmet today     -- and stays unmet
#   dangling relative import         unmet today     -- deliberately left alone
#
# The dangling case is recorded, not asserted into a new behavior: design change 3
# leaves an import from a module that does not define the name to the linter and
# the compile check, which report it with a location, "duplicating that judgment
# here would report one defect twice". What this module measures is therefore that
# today the detector cannot tell it from the sound import -- same status, same
# detail -- so after the fix both read met and the claim that nothing was
# double-reported is checkable rather than asserted.
#
# **Property 65 is covered example-based**, per task 5.2's recorded decision: this
# is behavior at a source-parsing boundary where enumerated import forms are more
# informative than generated ones. The one generated property below is deliberately
# not a substitute for that enumeration -- it quantifies over the *arrangements* of
# an in-package import (relative or absolute, which module, how many sibling names,
# where in the list, parenthesised or not, and the package's own name), which is
# genuinely combinatorial and where a hand-written case list would be arbitrary.
#
# **Nothing here edits production code.** The repair is task 5.
#
# _Requirements: 1.9_

#: The detail ``bugfix.md`` 1.9 records for the real tree. Pinned as a literal so
#: the counterexample this task exists to produce is compared against the reported
#: string rather than against a re-derivation of it.
JUMPCLOUD_API_CLIENT_DETAIL = "icon_jumpcloud/util/api.py: no HTTP_ERROR_MAP"

#: The package name the synthetic fixtures use. ``package_dir`` recognises
#: ``icon_*``, which is what ``insight-plugin create`` emits.
FIXTURE_PACKAGE = "icon_fixture"

#: A module outside the plugin package that also defines the map, for the one
#: placement the fix deliberately does not reach.
OUTSIDE_MODULE = "shared_errors"

#: The map itself, shaped as ``implementation.md`` prescribes: status codes to
#: ``{cause, assistance}`` pairs, covering the codes it names as the minimum.
_ERROR_MAP_LITERAL = (
    "HTTP_ERROR_MAP = {\n"
    '    400: {"cause": "Bad request.", "assistance": "Verify the request body."},\n'
    '    401: {"cause": "Invalid credentials.", "assistance": "Verify the API key."},\n'
    '    403: {"cause": "Forbidden.", "assistance": "Verify the key\'s permissions."},\n'
    '    404: {"cause": "Not found.", "assistance": "Verify the identifier."},\n'
    '    429: {"cause": "Rate limited.", "assistance": "Retry after a delay."},\n'
    '    500: {"cause": "Server error.", "assistance": "Retry, then contact support."},\n'
    '    503: {"cause": "Service unavailable.", "assistance": "Retry after a delay."},\n'
    "}\n"
)


class ErrorMapPlacement(NamedTuple):
    """One way a plugin can put ``HTTP_ERROR_MAP`` within reach of its client.

    Attributes:
        label: the case's name, used in parametrised ids and assertion messages.
        api_prelude: the lines that go into ``util/api.py`` above the client class
            -- an import, a literal assignment, or nothing at all.
        constants_defines_map: whether ``util/constants.py`` defines the map. False
            with an importing prelude is the **dangling** case.
        writes_outside_module: whether a module outside the package defines it.
        reachable_in_package: whether the map is genuinely reachable from inside
            the package. This is the predicate change 3 keys on, so it is stated
            per case rather than inferred from the prelude's text.
        prescribed: whether ``implementation.md`` prescribes this placement.
        expected_met_after_fix: what 2.13 requires of the fixed detector. Recorded
            on the case so the before-state and the after-state are read from one
            table.
    """

    label: str
    api_prelude: str
    constants_defines_map: bool
    writes_outside_module: bool
    reachable_in_package: bool
    prescribed: bool
    expected_met_after_fix: bool


#: The six placements task 5.1 enumerates, in the order it enumerates them.
ERROR_MAP_PLACEMENTS: Tuple[ErrorMapPlacement, ...] = (
    ErrorMapPlacement(
        label="defined_in_api",
        api_prelude=_ERROR_MAP_LITERAL,
        constants_defines_map=False,
        writes_outside_module=False,
        reachable_in_package=True,
        prescribed=False,
        expected_met_after_fix=True,
    ),
    ErrorMapPlacement(
        label="relative_in_package_import",
        api_prelude="from .constants import BASE_URL, TIMEOUT, HTTP_ERROR_MAP\n",
        constants_defines_map=True,
        writes_outside_module=False,
        reachable_in_package=True,
        prescribed=True,
        expected_met_after_fix=True,
    ),
    ErrorMapPlacement(
        label="absolute_in_package_import",
        api_prelude=f"from {FIXTURE_PACKAGE}.util.constants import BASE_URL, TIMEOUT, HTTP_ERROR_MAP\n",
        constants_defines_map=True,
        writes_outside_module=False,
        reachable_in_package=True,
        prescribed=True,
        expected_met_after_fix=True,
    ),
    ErrorMapPlacement(
        label="outside_package_import",
        api_prelude=f"from {OUTSIDE_MODULE} import HTTP_ERROR_MAP\nfrom .constants import BASE_URL, TIMEOUT\n",
        constants_defines_map=False,
        writes_outside_module=True,
        reachable_in_package=False,
        prescribed=False,
        expected_met_after_fix=False,
    ),
    ErrorMapPlacement(
        label="neither_defined_nor_imported",
        api_prelude="from .constants import BASE_URL, TIMEOUT\n",
        constants_defines_map=False,
        writes_outside_module=False,
        reachable_in_package=False,
        prescribed=False,
        expected_met_after_fix=False,
    ),
    ErrorMapPlacement(
        label="dangling_relative_import",
        api_prelude="from .constants import BASE_URL, TIMEOUT, HTTP_ERROR_MAP\n",
        constants_defines_map=False,
        writes_outside_module=False,
        reachable_in_package=False,
        prescribed=False,
        # Change 3 leaves this to the linter and the compile check, so the
        # detector's own verdict is whatever the import form gives it -- met, and
        # deliberately not distinguished. Recorded, not asserted: the assertions
        # below are about indistinguishability, not about this value.
        expected_met_after_fix=True,
    ),
)

#: The placements that are in-package, which is where 2.13 requires ``met``.
IN_PACKAGE_PLACEMENTS: Tuple[ErrorMapPlacement, ...] = tuple(
    placement for placement in ERROR_MAP_PLACEMENTS if placement.reachable_in_package
)

#: The placements 2.13 requires to stay unmet: "neither defined nor imported"
#: there, which includes an import from outside the plugin package.
OUT_OF_SCOPE_PLACEMENTS: Tuple[ErrorMapPlacement, ...] = tuple(
    placement
    for placement in ERROR_MAP_PLACEMENTS
    if not placement.expected_met_after_fix  # the fix leaves these alone
)


def _fixture_api_source(prelude: str, *, make_request: bool, domain_method: bool) -> str:
    """Return a ``util/api.py`` shaped the way ``implementation.md`` prescribes.

    ``make_request`` and ``domain_method`` exist because the condition has three
    halves and change 3 touches one of them. Being able to withhold each of the
    other two is what makes "unchanged" measurable rather than assumed.
    """
    lines = [
        "import requests",
        "from insightconnect_plugin_runtime.exceptions import PluginException",
        "",
        prelude.rstrip("\n"),
        "",
        "",
        "class FixtureApiClient:",
        '    """A client with the shape the rulebook names."""',
        "",
        "    def __init__(self, api_key):",
        "        self._api_key = api_key",
        "",
        "    def _headers(self):",
        '        return {"x-api-key": self._api_key}',
    ]
    if make_request:
        lines += [
            "",
            "    def _make_request(self, method, endpoint, **kwargs):",
            "        response = requests.request(",
            '            method, f"{BASE_URL}{endpoint}", headers=self._headers(), timeout=TIMEOUT, **kwargs',
            "        )",
            "        error = HTTP_ERROR_MAP.get(response.status_code)",
            "        if error is not None:",
            "            raise PluginException(**error)",
            "        return response.json()",
        ]
    if domain_method:
        lines += [
            "",
            "    def create_user(self, email, username):",
            '        return self._make_request("POST", "/systemusers", json={"email": email, "username": username})',
        ]
    return "\n".join(lines) + "\n"


def _write_fixture_plugin(
    root: Path,
    placement: ErrorMapPlacement,
    *,
    package: str = FIXTURE_PACKAGE,
    constants_module: str = "constants",
    make_request: bool = True,
    domain_method: bool = True,
) -> Path:
    """Write a minimal plugin tree carrying ``placement``, and return its root.

    Minimal on purpose: ``_api_client_condition`` reads ``<package>/util/api.py``
    and nothing else, so a tree with a package, a ``util`` module and those two
    files is the whole input. Nothing here is executed -- every assertion in this
    section goes through :mod:`ast` -- so the imports need not resolve.
    """
    package_root = root / package
    util = package_root / "util"
    util.mkdir(parents=True, exist_ok=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (util / "__init__.py").write_text("", encoding="utf-8")

    constants = ['BASE_URL = "https://console.example.com"', "TIMEOUT = 60", ""]
    if placement.constants_defines_map:
        constants.append(_ERROR_MAP_LITERAL)
    (util / f"{constants_module}.py").write_text("\n".join(constants), encoding="utf-8")

    if placement.writes_outside_module:
        (root / f"{OUTSIDE_MODULE}.py").write_text(_ERROR_MAP_LITERAL, encoding="utf-8")

    (util / "api.py").write_text(
        _fixture_api_source(placement.api_prelude, make_request=make_request, domain_method=domain_method),
        encoding="utf-8",
    )
    return root


def _api_client_result(root: Path) -> ConditionResult:
    """Return the ``api_client`` condition as the tool reports it for ``root``.

    Through :func:`evaluate_done` rather than by calling ``_api_client_condition``
    directly, so what is measured is the condition an operator is shown. No
    quality or pipeline report is supplied: the structural conditions are read
    straight off the tree and need neither.
    """
    condition = evaluate_done(root).condition(CONDITION_API_CLIENT)
    assert condition is not None, f"no {CONDITION_API_CLIENT} condition was evaluated for {root}"
    return condition


def _fixture_api_client_result(root: Path, placement: ErrorMapPlacement, **kwargs) -> ConditionResult:
    """Write ``placement`` under ``root`` and report its ``api_client`` condition."""
    return _api_client_result(_write_fixture_plugin(root, placement, **kwargs))


@lru_cache(maxsize=1)
def _jumpcloud_api_client() -> ConditionResult:
    """The ``api_client`` condition for the real tree the run produced."""
    _require_tree()
    return _api_client_result(JUMPCLOUD_TREE)


@lru_cache(maxsize=1)
def _jumpcloud_api_module() -> ast.Module:
    """The real tree's ``util/api.py``, parsed."""
    _require_tree()
    path = JUMPCLOUD_TREE / "icon_jumpcloud" / "util" / "api.py"
    if not path.is_file():
        pytest.skip(f"the JumpCloud tree at {JUMPCLOUD_TREE} has no client module at {path}")
    return ast.parse(path.read_text(encoding="utf-8"))


def _error_map_imports(tree: ast.Module) -> Tuple[Tuple[Optional[str], int], ...]:
    """Return ``(module, level)`` for every import of the map in ``tree``."""
    return tuple(
        (node.module, node.level)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and any(alias.name == "HTTP_ERROR_MAP" for alias in node.names)
    )


class TestTheRealTreeImportsTheMapRatherThanAssigningIt:
    """The mechanism, measured on the tree that produced the report.

    These are the *witnesses*: facts about where the map lives and how ``ast``
    sees it. They hold before and after the fix, because change 3 changes what the
    detector accepts and not what the plugin does.
    """

    def test_the_constants_module_defines_the_error_map(self):
        """The definition is where ``implementation.md`` puts it."""
        _require_tree()
        constants = JUMPCLOUD_TREE / "icon_jumpcloud" / "util" / "constants.py"
        if not constants.is_file():
            pytest.skip(f"the JumpCloud tree has no constants module at {constants}")
        _, assigned = _defined_names(ast.parse(constants.read_text(encoding="utf-8")))
        assert "HTTP_ERROR_MAP" in assigned, (
            f"{constants} does not define HTTP_ERROR_MAP, so this tree is not an instance of the "
            "placement bugfix.md 1.9 is about"
        )

    def test_the_client_module_imports_the_error_map(self):
        """And the client reaches it by import, which is the other half."""
        imports = _error_map_imports(_jumpcloud_api_module())
        assert imports, (
            "the JumpCloud client module does not import HTTP_ERROR_MAP at all, so the counterexample "
            "bugfix.md 1.9 records cannot be about an imported map"
        )

    def test_the_client_module_never_assigns_the_error_map(self):
        """The mechanism of the defect: ``_defined_names`` sees no assignment."""
        _, assigned = _defined_names(_jumpcloud_api_module())
        assert "HTTP_ERROR_MAP" not in assigned, (
            "the JumpCloud client module assigns HTTP_ERROR_MAP after all, which would make the reported "
            "detail a different defect from the one the design diagnoses"
        )

    def test_the_import_is_the_absolute_in_package_form(self):
        """Recorded because it decides how much of change 3 is required.

        `bugfix.md` 1.9 and design change 3 both mention the relative form first.
        The tree that produced the report uses the absolute in-package form, so a
        fix keyed on ``level > 0`` alone would leave this very plugin unmet.
        """
        imports = _error_map_imports(_jumpcloud_api_module())
        absolute_in_package = [
            (module, level) for module, level in imports if level == 0 and (module or "").startswith("icon_jumpcloud")
        ]
        assert absolute_in_package, (
            f"the JumpCloud client imports HTTP_ERROR_MAP as {imports}, not by an absolute in-package "
            "path; the note that change 3 must cover both forms was measured on this tree and should be "
            "re-measured rather than trusted"
        )


class TestTheApiClientDetectorRejectsThePrescribedPattern:
    """``api_client`` is unmet for a plugin that follows the rulebook.

    **Expected to FAIL on unfixed code.** Each test asserts what 2.13 requires:
    a map that ``util/api.py`` imports from within the plugin package satisfies the
    condition. Today ``_api_client_condition`` builds ``assigned`` from the
    ``ast.Assign`` / ``ast.AnnAssign`` targets of ``api.py`` alone, so every one of
    them reports ``unmet`` with ``<pkg>/util/api.py: no HTTP_ERROR_MAP``.
    """

    def test_a_map_defined_in_the_client_module_is_accepted_today(self, tmp_path):
        """The control. Without this the claim below is not about imports.

        Passes before and after the fix: change 3 adds to the assigned-name check
        rather than replacing it.
        """
        placement = ERROR_MAP_PLACEMENTS[0]
        condition = _fixture_api_client_result(tmp_path, placement)
        assert condition.met, (
            f"the {placement.label} placement is already unmet, so an unmet verdict for an imported map "
            f"would not be evidence about imports: {condition}"
        )

    @pytest.mark.parametrize("placement", IN_PACKAGE_PLACEMENTS, ids=lambda placement: placement.label)
    def test_an_in_package_error_map_satisfies_the_condition(self, tmp_path, placement):
        """**Validates: Requirements 1.9, 2.13**

        The prescribed pattern and its absolute-path variant, which is what the
        real tree uses.
        """
        condition = _fixture_api_client_result(tmp_path, placement)
        assert condition.met, (
            f"a plugin whose HTTP_ERROR_MAP is reachable in-package via the {placement.label} placement "
            f"reports api_client {condition.status.value} with detail {condition.detail!r}; "
            f"implementation.md defines the map under 'Constants Pattern (util/constants.py)' and has "
            f"api.py map codes 'via HTTP_ERROR_MAP dict', so this is the prescribed shape being rejected "
            f"(prescribed={placement.prescribed})"
        )

    def test_the_real_jumpcloud_tree_meets_the_api_client_condition(self):
        """**Validates: Requirements 1.9, 2.13**

        The concrete counterexample: the plugin the 2026-08-17 run produced, whose
        every API call was hand-checked against the supplied specs (`bugfix.md`
        introduction), reported as having no error map.
        """
        condition = _jumpcloud_api_client()
        assert condition.met, (
            f"the JumpCloud plugin at {JUMPCLOUD_TREE} reports api_client {condition.status.value} "
            f"with detail {condition.detail!r}, while its map is defined in "
            f"icon_jumpcloud/util/constants.py and imported into icon_jumpcloud/util/api.py"
        )

    def test_the_condition_is_unmet_rather_than_unverified(self):
        """A verdict, not a gap. Holds before the fix and is vacuous after it.

        Worth separating because ``unverified`` would make this a reporting
        shortfall of a different kind -- 27.5's "could not be checked" -- and the
        fix for that would be elsewhere. The client module parses, so the detector
        checked it and decided against it.
        """
        condition = _jumpcloud_api_client()
        assert condition.status is not ConditionStatus.UNVERIFIED, (
            "the api_client condition for the JumpCloud tree is unverified rather than unmet, so the "
            f"defect is not the one bugfix.md 1.9 describes: {condition}"
        )


class TestTheFormsTheFixDeliberatelyDoesNotReach:
    """A map that is genuinely out of reach stays unmet.

    Passes before **and** after the fix. 2.13 requires the condition be reported
    unmet "only when the map is neither defined nor imported there", and change 3
    scopes acceptance to imports from within the plugin package -- so these two
    cases are the boundary of the fix and their before-state is what makes the
    boundary checkable.
    """

    @pytest.mark.parametrize("placement", OUT_OF_SCOPE_PLACEMENTS, ids=lambda placement: placement.label)
    def test_a_map_out_of_the_packages_reach_leaves_the_condition_unmet(self, tmp_path, placement):
        """**Validates: Requirements 1.9, 2.13**"""
        condition = _fixture_api_client_result(tmp_path, placement)
        assert condition.status is ConditionStatus.UNMET, (
            f"the {placement.label} placement reports api_client {condition.status.value}; the fix is "
            f"scoped to in-package imports, so this case must stay unmet: {condition}"
        )
        assert "HTTP_ERROR_MAP" in condition.detail, (
            f"the {placement.label} placement is unmet but its detail does not name the missing map, so "
            f"the operator cannot tell which half of the condition failed: {condition.detail!r}"
        )


class TestADanglingImportIsNotDistinguishableToday:
    """An import from a module that does not define the map is left to the linter.

    Design change 3: "A dangling import -- imported from a module that does not
    define it -- is left to the linter and the compile check, which report it with
    a location; duplicating that judgment here would report one defect twice."

    So this class records rather than asserts. What it asserts is the *indistin-
    guishability*: today the detector gives the dangling import and the sound
    import the same status and the same detail, and no condition in the whole
    report mentions the dangling name. Both claims hold after the fix too -- both
    placements will read met -- which is exactly what "left alone" means.
    """

    @staticmethod
    def _sound_and_dangling(root: Path) -> Tuple[ConditionResult, ConditionResult]:
        sound = next(p for p in ERROR_MAP_PLACEMENTS if p.label == "relative_in_package_import")
        dangling = next(p for p in ERROR_MAP_PLACEMENTS if p.label == "dangling_relative_import")
        return (
            _fixture_api_client_result(root / "sound", sound),
            _fixture_api_client_result(root / "dangling", dangling),
        )

    def test_the_two_placements_write_the_same_client_module(self, tmp_path):
        """The premise: they differ only in whether ``constants.py`` defines it."""
        sound, dangling = (
            _write_fixture_plugin(tmp_path / "sound", ERROR_MAP_PLACEMENTS[1]),
            _write_fixture_plugin(tmp_path / "dangling", ERROR_MAP_PLACEMENTS[5]),
        )
        sound_api = (sound / FIXTURE_PACKAGE / "util" / "api.py").read_text(encoding="utf-8")
        dangling_api = (dangling / FIXTURE_PACKAGE / "util" / "api.py").read_text(encoding="utf-8")
        assert sound_api == dangling_api, (
            "the two fixtures' client modules differ, so a difference in the detector's verdict would not "
            "be attributable to the dangling target alone"
        )
        sound_constants = (sound / FIXTURE_PACKAGE / "util" / "constants.py").read_text(encoding="utf-8")
        dangling_constants = (dangling / FIXTURE_PACKAGE / "util" / "constants.py").read_text(encoding="utf-8")
        assert (
            "HTTP_ERROR_MAP" in sound_constants and "HTTP_ERROR_MAP" not in dangling_constants
        ), "the dangling fixture's constants module defines the map after all, so it is not dangling"

    def test_the_detector_gives_both_the_same_verdict(self, tmp_path):
        """Recorded, not corrected: one defect is not reported twice."""
        sound, dangling = self._sound_and_dangling(tmp_path)
        assert (sound.status, sound.detail) == (dangling.status, dangling.detail), (
            "the detector already distinguishes a dangling import from a sound one:\n"
            f"  sound:    {sound}\n  dangling: {dangling}\n"
            "design change 3 assumes it does not, and leaves the dangling case to the linter and the "
            "compile check on that basis"
        )

    def test_no_condition_in_the_report_names_the_dangling_target(self, tmp_path):
        """Nothing in the twelve conditions reports the dangling import as such."""
        root = _write_fixture_plugin(tmp_path, ERROR_MAP_PLACEMENTS[5])
        report = evaluate_done(root)
        naming = [c for c in report.conditions if "constants" in c.detail and "HTTP_ERROR_MAP" in c.detail]
        assert not naming, (
            "a definition-of-done condition already reports the dangling import by naming both the map "
            f"and the module it came from, so change 3's 'left to the linter' would double-report it: "
            f"{[str(c) for c in naming]}"
        )


class TestTheOtherTwoHalvesOfTheConditionAreUnaffected:
    """``_make_request`` and "at least one public domain method" are unchanged.

    Change 3 says so in one line; this class makes it measurable. Everything here
    passes before and after the fix, which is the point -- after task 5 these are
    the tests that show the other two halves still bite.
    """

    @pytest.mark.parametrize("placement", ERROR_MAP_PLACEMENTS, ids=lambda placement: placement.label)
    def test_a_complete_client_is_never_faulted_for_the_other_two_halves(self, tmp_path, placement):
        """Whatever the map's placement, the other two halves report nothing."""
        condition = _fixture_api_client_result(tmp_path, placement)
        assert "_make_request" not in condition.detail, (
            f"the {placement.label} placement is faulted for its request helper although it defines one: "
            f"{condition.detail!r}"
        )
        assert "domain method" not in condition.detail, (
            f"the {placement.label} placement is faulted for having no domain method although it defines "
            f"create_user: {condition.detail!r}"
        )

    def test_a_missing_request_helper_is_still_reported(self, tmp_path):
        """Withhold ``_make_request`` from the prescribed placement."""
        condition = _fixture_api_client_result(tmp_path, ERROR_MAP_PLACEMENTS[1], make_request=False)
        assert condition.status is ConditionStatus.UNMET, f"a client with no _make_request() reports {condition}"
        assert (
            "no central _make_request()" in condition.detail
        ), f"a client with no central request helper is unmet for another reason: {condition.detail!r}"

    def test_a_client_with_no_public_domain_method_is_still_reported(self, tmp_path):
        """Withhold the domain method the same way."""
        condition = _fixture_api_client_result(tmp_path, ERROR_MAP_PLACEMENTS[1], domain_method=False)
        assert condition.status is ConditionStatus.UNMET, f"a client with no domain method reports {condition}"
        assert (
            "no public domain method" in condition.detail
        ), f"a client with nothing for an action to call is unmet for another reason: {condition.detail!r}"

    def test_the_real_trees_only_shortfall_is_the_error_map(self):
        """The JumpCloud client is faulted for the map and nothing else.

        Which is what makes 1.9 a detector defect rather than a plugin defect: the
        run verified that ``_make_request`` is central (`bugfix.md` introduction),
        and the detector agrees about that half.
        """
        condition = _jumpcloud_api_client()
        assert "_make_request" not in condition.detail and "domain method" not in condition.detail, (
            "the JumpCloud client is faulted for more than its error map, so 1.9 is not the whole story "
            f"for this tree: {condition.detail!r}"
        )


class TestNoArrangementOfAnInPackageImportSatisfiesTheDetector:
    """The generated half, and only where generation adds something.

    Property 65 is covered **example-based** by the enumeration above, per task
    5.2's recorded decision. This property is not a substitute for it: it
    quantifies over the *arrangements* of an in-package import -- relative or
    absolute, which module holds the map, how many other names ride the same
    statement, where in that list the map sits, whether the list is parenthesised,
    and what the package is called -- which is combinatorial in a way a
    hand-written case list would have to sample arbitrarily.

    It asserts the **fixed** behavior (2.13), so it fails today for every example,
    and the counterexample Hypothesis shrinks to is the simplest arrangement the
    detector rejects. Asserting today's behavior instead would have produced a
    test that passes now and has to be deleted at task 5, which is not a
    regression test of anything.
    """

    @staticmethod
    def _import_statement(style: str, package: str, module: str, siblings: Tuple[str, ...], index: int) -> str:
        """Build one in-package ``from ... import ...`` naming the map."""
        source = {
            "relative": f".{module}",
            "relative_parent": f"..util.{module}",
            "absolute": f"{package}.util.{module}",
        }[style]
        names = list(siblings)
        names.insert(index, "HTTP_ERROR_MAP")
        return f"from {source} import " + ", ".join(names) + "\n"

    # Feature: export-gate-and-preview-fidelity, Property 1: Bug Condition
    @settings(max_examples=100, deadline=None)
    @given(
        style=st.sampled_from(("relative", "relative_parent", "absolute")),
        suffix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=10),
        module=st.sampled_from(("constants", "errors", "http_errors")),
        siblings=st.lists(st.sampled_from(("BASE_URL", "TIMEOUT", "V1_API_PATH", "V2_API_PATH")), unique=True),
        index=st.integers(min_value=0, max_value=4),
    )
    def test_an_in_package_import_of_the_map_satisfies_the_condition(self, style, suffix, module, siblings, index):
        """**Validates: Requirements 1.9, 2.13**"""
        package = f"icon_{suffix}"
        root = Path(tempfile.mkdtemp(prefix="icpb-api-client-import-"))
        try:
            placement = ErrorMapPlacement(
                label=f"{style}:{module}",
                api_prelude=self._import_statement(
                    style, package, module, tuple(siblings), index % (len(siblings) + 1)
                ),
                constants_defines_map=True,
                writes_outside_module=False,
                reachable_in_package=True,
                prescribed=(style != "relative_parent" and module == "constants"),
                expected_met_after_fix=True,
            )
            condition = _fixture_api_client_result(root, placement, package=package, constants_module=module)
            assert condition.met, (
                f"{package}/util/api.py reaches HTTP_ERROR_MAP by "
                f"{placement.api_prelude.strip()!r} and still reports api_client "
                f"{condition.status.value}: {condition.detail!r}"
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)


def test_the_api_client_detectors_inputs_are_recorded():
    """Guard: state the tree, the detail, and the enumeration this task measured.

    A detector measurement that does not say which tree it read, what the detector
    said about it, or which placements were enumerated is not evidence. The detail
    is compared against the literal `bugfix.md` 1.9 records, and the comparison is
    written so it survives the fix: after task 5 the condition is met and there is
    no detail to pin.
    """
    condition = _jumpcloud_api_client()
    assert condition.met or condition.detail == JUMPCLOUD_API_CLIENT_DETAIL, (
        f"the detector reports {condition.status.value} for {JUMPCLOUD_TREE} with detail "
        f"{condition.detail!r}, which is neither met nor the counterexample bugfix.md 1.9 records "
        f"({JUMPCLOUD_API_CLIENT_DETAIL!r}); the reported string should be corrected in whichever "
        "document is wrong rather than worked around here"
    )
    labels = tuple(placement.label for placement in ERROR_MAP_PLACEMENTS)
    assert len(set(labels)) == len(labels) == 6, f"the enumeration task 5.1 needs is not the six cases: {labels}"
    assert len(IN_PACKAGE_PLACEMENTS) == 3 and len(OUT_OF_SCOPE_PLACEMENTS) == 2, (
        "the placements no longer partition into three in-package cases and two the fix leaves alone: "
        f"in-package={[p.label for p in IN_PACKAGE_PLACEMENTS]} "
        f"out-of-scope={[p.label for p in OUT_OF_SCOPE_PLACEMENTS]}"
    )
    steering = Path("~/.kiro/steering/implementation.md").expanduser()
    if not steering.is_file():
        pytest.skip(f"the authoritative rulebook is not present at {steering}; the prescription is unverifiable")
    text = steering.read_text(encoding="utf-8")
    assert "## Constants Pattern (util/constants.py)" in text and "`HTTP_ERROR_MAP` dict" in text, (
        f"{steering} no longer prescribes HTTP_ERROR_MAP under the constants heading, so the premise of "
        "this task -- that the detector rejects the prescribed pattern -- must be re-read before it is "
        "asserted"
    )


# ---------------------------------------------------------------------------
# Task 1.9 -- byproducts reach the ``.plg``
# ---------------------------------------------------------------------------
#
# ``bugfix.md`` 1.10: "WHEN a plugin is packaged after a local test run, THEN the
# system includes ``.coverage`` and ``unit_test/.coverage`` in the ``.plg``", and
# 2.15 requires the fixed tool to "exclude local build and test byproducts,
# including ``.coverage`` at any depth".
#
# **The mechanism, read before it was measured.**
# :data:`~icplugin_builder.integrations.build_engine._EXCLUDED_DIRS` is a set of
# *directory names* and :func:`list_plugin_files` consumes it as::
#
#     if any(part in _EXCLUDED_DIRS for part in relative.parts):
#         continue
#
# ``relative.parts`` includes the file name, so the filter is capable of matching
# a file -- but only against a name in that set, and every name in it is a
# directory (``.builder``, ``.git``, ``__pycache__``, ``.pytest_cache``,
# ``.mypy_cache``). There is therefore nothing in the packager that knows what a
# byproduct *file* is, at any depth. That is the whole defect, and it is why
# design change 8 replaces the set with a predicate,
# ``core/plugin_files.is_packaging_excluded``, rather than adding another name to
# it.
#
# **Two measurements, because they answer different questions.**
#
#   1. A copy of the JumpCloud tree with every byproduct stripped, the plugin's
#      own tests then run in it, and the result packaged -- so a ``.coverage`` in
#      the archive was demonstrably *produced by that run* rather than inherited
#      from the tree. This is 1.10 exactly. :func:`packaged_after_a_test_run`.
#   2. A second copy with the full byproduct table planted, packaged -- so the
#      partition task 10's predicate has to encode (which byproducts the
#      directory-name set already catches, and which it cannot) is measured rather
#      than argued. :func:`packaged_with_every_byproduct`.
#
# Both work on ``shutil.copytree`` copies. Nothing here writes into
# ``~/.icplugin-builder/projects/``; the one measurement taken against the real
# tree is read-only and is in
# :func:`test_the_byproduct_measurements_inputs_are_recorded`.
#
# **The archive is opened, not trusted.** Task 1.7 established the ``.plg`` is a
# gzipped tarball (Req 9.1) and reads members out of one; its helpers live in
# ``tests/orchestrator/test_preview_fidelity_bug_conditions.py`` and so cannot be
# imported here, but the technique is the same one -- :func:`_archive_members`
# below opens the produced artifact with :mod:`tarfile` rather than believing
# :attr:`PlgArtifact.files`. Believing the returned tuple would test
# ``list_plugin_files`` against itself.
#
# _Requirements: 1.10_

#: The coverage data file name coverage.py writes, and the prefix of the
#: per-process files it writes under ``--cov`` in parallel mode.
COVERAGE_FILE = ".coverage"

#: What ``bugfix.md`` 1.10 names literally. Pinned so the claim under test is the
#: claim the document makes.
BUGFIX_1_10_MEMBERS: Tuple[str, ...] = (COVERAGE_FILE, f"{UNIT_TEST_DIR}/{COVERAGE_FILE}")

#: The member count the run recorded for this tree ("39 entries, no ``.builder/``,
#: no swagger") and the count 3.2 requires afterwards -- "the 39-entry baseline
#: less the byproducts named in 2.15", which is those two ``.coverage`` files.
BASELINE_MEMBER_COUNT = 39
BASELINE_LESS_BYPRODUCTS = BASELINE_MEMBER_COUNT - len(BUGFIX_1_10_MEMBERS)

#: Byproduct kinds, so the report says *why* something is a byproduct rather than
#: only that it is one. ``vcs`` and ``metadata`` are not byproducts of a build;
#: they are in the table because 3.2 requires they stay excluded.
KIND_COVERAGE = "coverage data"
KIND_CACHE = "interpreter or tool cache"
KIND_BUILD = "packaging byproduct"
KIND_METADATA = "tool-only metadata"
KIND_VCS = "version control"


class ByproductCase(NamedTuple):
    """One file the fixed packager must keep out of the ``.plg`` (2.15, 3.2).

    Attributes:
        label: short name for the parametrised test id.
        relative: the POSIX path planted in the copied tree.
        kind: one of the ``KIND_`` constants, for the partition report.
        excluded_today: whether ``_EXCLUDED_DIRS`` already catches it. Recorded so
            the partition is legible in one place; the assertion itself does not
            branch on it, because the requirement is the same for every row.
        reason: why this file is a byproduct, or why it must stay excluded.
    """

    label: str
    relative: str
    kind: str
    excluded_today: bool
    reason: str


#: Every byproduct observed in, or producible in, these trees. The rows with
#: ``excluded_today=False`` are exactly the surface change 8's predicate has to
#: add; the rows with ``True`` are the surface it must not lose.
BYPRODUCT_CASES: Tuple[ByproductCase, ...] = (
    ByproductCase(
        label="root-coverage",
        relative=COVERAGE_FILE,
        kind=KIND_COVERAGE,
        excluded_today=False,
        reason="written by `pytest --cov` run from the plugin root; `bugfix.md` 1.10 names it",
    ),
    ByproductCase(
        label="unit-test-coverage",
        relative=f"{UNIT_TEST_DIR}/{COVERAGE_FILE}",
        kind=KIND_COVERAGE,
        excluded_today=False,
        reason="written by the same run invoked from inside unit_test/; `bugfix.md` 1.10 names it",
    ),
    ByproductCase(
        label="coverage-at-depth",
        relative="icon_jumpcloud/util/.coverage",
        kind=KIND_COVERAGE,
        excluded_today=False,
        reason="2.15 says `.coverage` at *any* depth, so depth is part of the requirement",
    ),
    ByproductCase(
        label="coverage-parallel-suffix",
        relative=".coverage.host.12345.678901",
        kind=KIND_COVERAGE,
        excluded_today=False,
        reason="coverage.py's per-process file under `--cov` parallel mode; change 8 names `.coverage.*`",
    ),
    ByproductCase(
        label="pycache-pyc",
        relative="__pycache__/setup.cpython-313.pyc",
        kind=KIND_CACHE,
        excluded_today=True,
        reason="`__pycache__` is in `_EXCLUDED_DIRS`, so everything under it is already dropped",
    ),
    ByproductCase(
        label="pycache-pyc-at-depth",
        relative=f"{UNIT_TEST_DIR}/__pycache__/util.cpython-313.pyc",
        kind=KIND_CACHE,
        excluded_today=True,
        reason="the filter tests every path part, so a nested `__pycache__` is caught too",
    ),
    ByproductCase(
        label="pytest-cache",
        relative=".pytest_cache/CACHEDIR.TAG",
        kind=KIND_CACHE,
        excluded_today=True,
        reason=(
            "`.pytest_cache` **is** in `_EXCLUDED_DIRS`, unlike `GENERATED_DIR_NAMES` where task 1.3 "
            "found it absent -- the two lists differ and packaging consumes this one"
        ),
    ),
    ByproductCase(
        label="mypy-cache",
        relative=".mypy_cache/missing_stubs.txt",
        kind=KIND_CACHE,
        excluded_today=True,
        reason="`.mypy_cache` is in `_EXCLUDED_DIRS`; no tree here produces one, so it is planted",
    ),
    ByproductCase(
        label="bare-pyc",
        relative="icon_jumpcloud/util/api.pyc",
        kind=KIND_CACHE,
        excluded_today=False,
        reason=(
            "a `.pyc` beside its source rather than under `__pycache__`; nothing in the directory-name "
            "set can reach it, which is the same hole `.coverage` falls through"
        ),
    ),
    ByproductCase(
        label="setuptools-build",
        relative="build/lib/icon_jumpcloud/util/api.py",
        kind=KIND_BUILD,
        excluded_today=False,
        reason="`setup.py build` copies the package into `build/lib/`; task 1.1 saw `build` in the image",
    ),
    ByproductCase(
        label="egg-info",
        relative="jumpcloud_rapid7_plugin.egg-info/PKG-INFO",
        kind=KIND_BUILD,
        excluded_today=False,
        reason="`setup.py egg_info` writes it; task 1.1 saw `jumpcloud_rapid7_plugin.egg-info` in the image",
    ),
    ByproductCase(
        label="make-tarball",
        relative="rapid7-jumpcloud-1.0.0.tar.gz",
        kind=KIND_BUILD,
        excluded_today=False,
        reason="the generated Makefile's `tarball` target writes `$(VENDOR)-$(NAME)-$(VERSION).tar.gz` at the root",
    ),
    ByproductCase(
        label="builder-reference",
        relative=f"{BUILDER_METADATA_DIR}/reference/jumpcloud_v1_swagger.yaml",
        kind=KIND_METADATA,
        excluded_today=True,
        reason="3.2 requires `.builder/` and all reference material stay out; this row guards that",
    ),
    ByproductCase(
        label="git-config",
        relative=".git/config",
        kind=KIND_VCS,
        excluded_today=True,
        reason="`.git` is in `_EXCLUDED_DIRS`; change 8's predicate must keep covering VCS directories",
    ),
)


def _is_byproduct(relative: str) -> bool:
    """Is ``relative`` a build or test byproduct, per 2.15 and change 8's list?

    Deliberately a *test-side* classifier, not an import of the predicate change 8
    will add -- that predicate does not exist yet, and once it does, a test that
    imported it would be asserting the production code against itself. It covers
    ``.builder/`` and VCS/cache directories too, because those are the surface the
    same predicate has to keep.
    """
    parts = PurePosixPath(relative).parts
    if any(part in _EXCLUDED_DIRS for part in parts):
        return True
    if any(part == "build" or part.endswith(".egg-info") for part in parts[:-1]):
        return True
    name = parts[-1]
    return (
        name == COVERAGE_FILE
        or name.startswith(f"{COVERAGE_FILE}.")
        or name.endswith((".pyc", ".pyo"))
        or name.endswith((".tar.gz", ".tgz"))
    )


def _byproducts_on_disk(root: Path) -> Tuple[str, ...]:
    """Every byproduct present under ``root``, as sorted relative POSIX paths."""
    found = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if _is_byproduct(relative):
            found.add(relative)
    return tuple(sorted(found))


#: Cache directories removed wholesale from a copy before a measurement, so a
#: ``__pycache__`` left by an earlier run is not mistaken for this one's output.
#: ``.builder/`` and ``.git/`` are deliberately **not** here: they are already
#: excluded from packaging and 3.2 requires they stay that way, so removing them
#: would make the preservation assertions vacuous.
_STRIPPED_CACHE_DIRS: Tuple[str, ...] = ("__pycache__", ".pytest_cache", ".mypy_cache")


def _strip_byproducts(root: Path) -> Tuple[str, ...]:
    """Remove the byproducts under ``root`` that packaging would otherwise pick up.

    So that anything observed afterwards was produced by the run under
    measurement, rather than inherited from the tree the copy was taken from. Only
    files :func:`list_plugin_files` would currently package are unlinked, plus the
    cache directories in :data:`_STRIPPED_CACHE_DIRS`; the tree's own
    ``.builder/`` is left where it is.
    """
    removed = [member for member in list_plugin_files(root) if _is_byproduct(member)]
    for relative in removed:
        (root / relative).unlink(missing_ok=True)
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir() and path.name in _STRIPPED_CACHE_DIRS:
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path.relative_to(root).as_posix() + "/")
    return tuple(sorted(removed))


def _copy_of_the_tree(label: str) -> Path:
    """A ``shutil.copytree`` copy of the JumpCloud tree, for a destructive measurement.

    The real tree is never packaged or written to by these tests: packaging runs
    the plugin's suite and rewrites ``.coverage`` (which is how 1.10 arises in the
    first place), and a test that produced the artifact it is measuring inside a
    user's project directory would be leaving byproducts of its own.
    """
    _require_tree()
    work = Path(tempfile.mkdtemp(prefix=f"icpb-byproduct-{label}-"))
    root = work / JUMPCLOUD_TREE.name
    shutil.copytree(JUMPCLOUD_TREE, root, symlinks=True)
    return root


def _archive_members(artifact: Path) -> Tuple[str, ...]:
    """The member names inside a produced ``.plg``, read out of the archive itself.

    Mirrors task 1.7's technique against ``tests/orchestrator`` rather than
    importing it: the ``.plg`` is a gzipped tarball (Req 9.1), so the members are
    read with :mod:`tarfile`. Reading them from the archive rather than from
    :attr:`PlgArtifact.files` is the point -- the returned tuple is
    ``list_plugin_files``'s own output, so trusting it would leave the archive
    itself unmeasured.
    """
    with tarfile.open(artifact, "r:gz") as archive:
        return tuple(sorted(member.name for member in archive.getmembers() if member.isfile()))


def _package_copy(root: Path) -> Tuple[str, ...]:
    """Package ``root`` into a ``.plg`` outside the tree; return its real members.

    The artifact is written outside the working tree rather than to the default
    ``.builder/artifacts/``, so that "the archive does not contain itself" is a
    property of the output directory and not of the exclusion under test.
    """
    artifact = BuildEngine().package(root, validation_passed=True, output_dir=root.parent / "artifacts")
    return _archive_members(artifact.path)


class ByproductRun(NamedTuple):
    """Everything one packaging measurement recorded, so a failure can be read."""

    root: Path
    stripped: Tuple[str, ...]
    baseline: Tuple[str, ...]
    test_runs: Tuple[Tuple[str, int], ...]
    produced: Tuple[str, ...]
    members: Tuple[str, ...]
    preview: Tuple[str, ...]

    @property
    def summary(self) -> str:
        """The measurement, laid out for an assertion message."""
        lines = [
            f"tree={self.root}",
            f"stripped {len(self.stripped)} byproduct(s) before the run",
            f"baseline after the strip: {len(self.baseline)} member(s)",
            "test runs: " + ", ".join(f"{label} -> rc={code}" for label, code in self.test_runs),
            f"byproducts on disk afterwards: {list(self.produced)}",
            f"archive members: {len(self.members)}",
        ]
        packaged = [member for member in self.members if _is_byproduct(member)]
        lines.append(f"byproducts *inside* the .plg: {packaged}")
        return "\n".join(lines)


def _resolved_interpreter_with_pytest() -> str:
    """The interpreter the tool resolves, skipping unless it can run the suite.

    A host whose resolved interpreter has no ``pytest`` cannot produce a
    ``.coverage`` at all, so it cannot measure 1.10 -- that is an unverified
    measurement and it is recorded as a skip rather than as a finding (Req 26.4).
    """
    interpreter = _target_python().executable
    if interpreter is None:
        pytest.skip("no interpreter resolved for the plugin's tests; a coverage file cannot be produced")
    probe = _capture([interpreter, "-c", "import pytest, pytest_cov"], timeout=120.0)
    if probe is None or probe[0] != 0:
        pytest.skip(
            f"the resolved interpreter {interpreter} cannot import pytest and pytest-cov, so no "
            "`.coverage` would be written and 1.10 cannot be reproduced here"
        )
    return interpreter


@pytest.fixture(scope="module")
def packaged_after_a_test_run():
    """`bugfix.md` 1.10 reproduced end to end: strip, run the tests, package, open the ``.plg``.

    Module-scoped because it copies a tree, runs a suite twice and writes an
    archive; every assertion below reads the same measurement rather than taking
    it again.
    """
    interpreter = _resolved_interpreter_with_pytest()
    root = _copy_of_the_tree("run")
    try:
        stripped = _strip_byproducts(root)
        baseline = tuple(list_plugin_files(root))
        package = package_dir(root)
        coverage_args = [f"--cov={package}", "--cov-report=term-missing"] if package else []
        # Two invocations, because 1.10 names two files and they come from two
        # working directories: coverage.py writes `.coverage` beside the process's
        # cwd, so the root file and the `unit_test/` file are not one run's output.
        runs = []
        for label, cwd, target in (
            ("root", root, UNIT_TEST_DIR),
            (UNIT_TEST_DIR, root / UNIT_TEST_DIR, "."),
        ):
            result = _capture(
                [interpreter, "-m", "pytest", target, "-q", "--no-header", *coverage_args],
                cwd=cwd,
                timeout=600.0,
            )
            if result is None:
                pytest.skip(f"the plugin's suite could not be run from {cwd}; 1.10 was not reproduced")
            runs.append((label, result[0]))
        yield ByproductRun(
            root=root,
            stripped=stripped,
            baseline=baseline,
            test_runs=tuple(runs),
            produced=_byproducts_on_disk(root),
            members=_package_copy(root),
            preview=tuple(preview_export_files(root).files),
        )
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


@pytest.fixture(scope="module")
def packaged_with_every_byproduct():
    """The same tree with every :data:`BYPRODUCT_CASES` row planted, then packaged.

    No test run here: planting is what makes the *partition* measurable, including
    the rows this host's tooling does not happen to produce (a ``.mypy_cache``, a
    bare ``.pyc``, a ``make tarball`` archive). Task 10's predicate has to encode
    that partition, so it is measured rather than reasoned about.
    """
    root = _copy_of_the_tree("planted")
    try:
        _strip_byproducts(root)
        baseline = tuple(list_plugin_files(root))
        for case in BYPRODUCT_CASES:
            target = root / case.relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"planted byproduct for {case.label}\n", encoding="utf-8")
        yield ByproductRun(
            root=root,
            stripped=(),
            baseline=baseline,
            test_runs=(),
            produced=_byproducts_on_disk(root),
            members=_package_copy(root),
            preview=tuple(preview_export_files(root).files),
        )
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


class TestTheTestRunProducesTheTwoFilesBugfixNames:
    """Witnesses for 1.10's premise -- expected to pass before *and* after the fix.

    The fix is to the packager, not to coverage.py: running the plugin's tests will
    go on writing ``.coverage``, and that is correct. These assertions establish
    that the files in the archive below were produced by *this* run, which is what
    makes the next class's failures about packaging rather than about a stale tree.
    """

    def test_the_strip_left_no_byproduct_behind(self, packaged_after_a_test_run):
        """Nothing observed later was inherited from the tree the copy was taken from."""
        run = packaged_after_a_test_run
        assert not [member for member in run.baseline if _is_byproduct(member)], (
            "the copied tree still carried byproducts after the strip, so a `.coverage` in the archive "
            f"would not prove the test run produced it:\n{run.summary}"
        )

    def test_the_plugins_own_tests_pass_in_the_copy(self, packaged_after_a_test_run):
        """A failing suite would make the measurement about a broken plugin instead."""
        run = packaged_after_a_test_run
        failed = [label for label, code in run.test_runs if code != 0]
        assert not failed, (
            f"the plugin's suite did not pass in the copy for {failed}; 1.10 is about packaging *after a "
            f"local test run*, so a red suite measures something else:\n{run.summary}"
        )

    @pytest.mark.parametrize("relative", BUGFIX_1_10_MEMBERS)
    def test_the_run_wrote_the_coverage_file(self, packaged_after_a_test_run, relative):
        """Both files `bugfix.md` 1.10 names exist on disk after the run."""
        run = packaged_after_a_test_run
        assert (run.root / relative).is_file(), (
            f"{relative} was not written by the plugin's own test run, so `bugfix.md` 1.10's premise "
            f"does not hold on this host and the claim below cannot be attributed to packaging:\n{run.summary}"
        )


class TestByproductsReachThePlg:
    """`bugfix.md` 1.10 -- **expected to FAIL until task 10 lands.**

    Each assertion is the fixed behavior 2.15 requires, so the failure message is
    the counterexample: the byproduct, named, inside a real gzipped tarball that
    was opened rather than described.
    """

    @pytest.mark.parametrize("relative", BUGFIX_1_10_MEMBERS)
    def test_the_coverage_file_is_not_a_member_of_the_plg(self, packaged_after_a_test_run, relative):
        """**Validates: Requirements 1.10, 2.15**"""
        run = packaged_after_a_test_run
        assert relative not in run.members, (
            f"{relative} is a member of the produced .plg. `_EXCLUDED_DIRS` is a set of directory names "
            f"({sorted(_EXCLUDED_DIRS)}) and `list_plugin_files` drops a path only when one of its parts "
            f"is in that set, so no byproduct *file* has anything excluding it:\n{run.summary}"
        )

    def test_no_coverage_file_at_any_depth_is_a_member(self, packaged_after_a_test_run):
        """2.15's general form -- ``.coverage`` at *any* depth.

        **Validates: Requirements 2.15**
        """
        run = packaged_after_a_test_run
        coverage_members = [
            member
            for member in run.members
            if PurePosixPath(member).name == COVERAGE_FILE or PurePosixPath(member).name.startswith(f"{COVERAGE_FILE}.")
        ]
        assert not coverage_members, (
            f"the .plg carries coverage data at {coverage_members}; 2.15 requires `.coverage` at any depth "
            f"be excluded, which a directory-name set cannot express:\n{run.summary}"
        )

    def test_the_member_set_is_the_baseline_with_nothing_added(self, packaged_after_a_test_run):
        """3.2's two halves at once: byproducts gone, every needed file still there.

        This is the assertion task 10.1 re-runs. It fails today on the *additions*
        only, which is the point -- stating it as an equality means a fix that
        excluded a byproduct by also dropping a file the plugin needs would fail
        here rather than pass.

        **Validates: Requirements 1.10, 2.15, 3.2**
        """
        run = packaged_after_a_test_run
        added = [member for member in run.members if member not in run.baseline]
        missing = [member for member in run.baseline if member not in run.members]
        assert (added, missing) == ([], []), (
            f"the .plg is not the stripped tree: it adds {added} and is missing {missing}. The additions "
            f"are the byproducts of the test run; the baseline is {len(run.baseline)} member(s) and the "
            f"archive is {len(run.members)}:\n{run.summary}"
        )

    def test_the_archive_carries_the_baseline_less_the_two_byproducts(self, packaged_after_a_test_run):
        """The count 3.2 states in so many words -- the 39-entry baseline less 2.15's byproducts.

        **Validates: Requirements 2.15, 3.2**
        """
        run = packaged_after_a_test_run
        assert len(run.baseline) == BASELINE_LESS_BYPRODUCTS, (
            f"the stripped tree holds {len(run.baseline)} file(s), not the {BASELINE_LESS_BYPRODUCTS} that "
            f"3.2's '{BASELINE_MEMBER_COUNT}-entry baseline less the byproducts named in 2.15' implies; the "
            f"baseline has moved and the figure should be re-recorded rather than worked around:\n{run.summary}"
        )
        assert len(run.members) == BASELINE_LESS_BYPRODUCTS, (
            f"the .plg holds {len(run.members)} member(s) rather than {BASELINE_LESS_BYPRODUCTS}; the "
            f"surplus is {[member for member in run.members if member not in run.baseline]}:\n{run.summary}"
        )


class TestThePartitionTaskTensPredicateHasToEncode:
    """Every byproduct, one row each -- **the passes and failures *are* the partition.**

    One assertion, applied uniformly: after task 10 no row is a member. Which rows
    fail today is exactly which rows ``_EXCLUDED_DIRS`` cannot reach, so this class
    reports the partition change 8's ``is_packaging_excluded`` has to encode without
    a second test that asserts today's behavior and would have to be deleted when
    the fix lands.

    The rows with ``excluded_today=True`` pass now and must keep passing: two of
    them (``.builder/reference/...`` and ``.git/config``) are 3.2's preservation
    constraint rather than 2.15's requirement.
    """

    @pytest.mark.parametrize("case", BYPRODUCT_CASES, ids=lambda case: case.label)
    def test_the_byproduct_is_not_a_member_of_the_plg(self, packaged_with_every_byproduct, case):
        """**Validates: Requirements 1.10, 2.15, 3.2**"""
        run = packaged_with_every_byproduct
        assert case.relative not in run.members, (
            f"{case.relative} ({case.kind}) is a member of the produced .plg. {case.reason}. "
            f"`_EXCLUDED_DIRS` {'does' if case.excluded_today else 'does not'} already reach it, and the "
            f"set is {sorted(_EXCLUDED_DIRS)}:\n{run.summary}"
        )

    def test_planting_the_table_added_nothing_but_byproducts(self, packaged_with_every_byproduct):
        """Guard: the planted rows are the only difference from the stripped tree.

        Without this a row that accidentally overwrote a real plugin file would
        make the class above pass for the wrong reason.
        """
        run = packaged_with_every_byproduct
        planted = {case.relative for case in BYPRODUCT_CASES}
        unexpected = [member for member in run.members if member not in run.baseline and member not in planted]
        assert not unexpected, f"packaging the planted tree produced members from nowhere: {unexpected}"
        assert not planted & set(run.baseline), (
            "a planted byproduct path collides with a real file in the plugin tree, so the row would be "
            f"measuring the wrong thing: {sorted(planted & set(run.baseline))}"
        )


class TestPreservationTheArtifactStillCarriesThePlugin:
    """3.2 -- witnesses that pass today and must go on passing after change 8.

    Change 8 moves ``list_plugin_files`` onto a new predicate. The risk in that
    move is not that it excludes too little but that it excludes too much, so what
    it must keep is asserted here rather than left implied by the equality above.
    """

    def test_the_builder_subtree_is_absent_from_the_archive(self, packaged_with_every_byproduct):
        """`bugfix.md` 3.2 and Req 14.3 -- ``.builder/`` and its reference material stay out."""
        run = packaged_with_every_byproduct
        leaked = [member for member in run.members if PurePosixPath(member).parts[0] == BUILDER_METADATA_DIR]
        assert not leaked, f"tool-only metadata reached the .plg: {leaked}\n{run.summary}"

    def test_no_reference_document_reaches_the_archive(self, packaged_with_every_byproduct):
        """The supplied Swagger specs are the thing 3.2 is most concerned to keep out."""
        run = packaged_with_every_byproduct
        suspects = [
            member
            for member in run.members
            if "reference" in PurePosixPath(member).parts or "swagger" in PurePosixPath(member).name.lower()
        ]
        assert not suspects, f"reference material reached the .plg: {suspects}\n{run.summary}"

    def test_every_hand_written_and_generated_plugin_file_is_present(self, packaged_after_a_test_run):
        """Everything the plugin needs is still a member, byproducts aside (3.2)."""
        run = packaged_after_a_test_run
        missing = [member for member in run.baseline if member not in run.members]
        assert not missing, f"the .plg is missing plugin files: {missing}\n{run.summary}"
        for required in ("plugin.spec.yaml", "setup.py", "requirements.txt", "Dockerfile"):
            assert required in run.members, f"{required} is not in the .plg\n{run.summary}"

    def test_the_export_preview_equals_the_archive(self, packaged_after_a_test_run):
        """Property 30 -- one source of truth, so the preview changes with the packager.

        Recorded here because change 8 edits ``list_plugin_files``, which both
        consume. If the two ever diverged, the fix would silently stop being
        visible in the preview.
        """
        run = packaged_after_a_test_run
        assert run.preview == run.members, (
            "the export preview and the produced archive disagree; they are meant to be the same "
            f"`list_plugin_files` call.\npreview only: {[p for p in run.preview if p not in run.members]}\n"
            f"archive only: {[m for m in run.members if m not in run.preview]}"
        )


#: Directory names the property below must not generate. A byproduct sitting in an
#: already-excluded directory would be excluded for the wrong reason, and its
#: innocent sibling would be excluded with it -- so an example built from one of
#: these names would prove nothing either way.
_RESERVED_DIR_NAMES = frozenset(_EXCLUDED_DIRS) | {"build"}


class TestNoCoverageFileIsPackagedWhereverItSits:
    """The one genuinely universal claim here: **depth and surrounding names do not matter.**

    Everything above is scoped to one concrete tree, per task 1's recorded
    approach. This property is generated because the claim it makes is about the
    packager's *filter*, not about a plugin: 2.15 says "``.coverage`` at any
    depth", and "any depth" is a quantifier. What varies is how deep the file sits,
    what the intervening directories are called, and which of coverage.py's file
    names it is. All of them are excluded from the ``.plg`` after the fix, and none
    of them is today.

    **This is not Property 69 and does not stand in for it.** Property 69 (task
    10.2) quantifies over whole generated trees and carries the "and contains every
    other file present in the tree" half. This one holds one byproduct fixed and
    varies its location, which is the part that isolates the directory-name-set
    mechanism.

    It asserts the fixed behavior, so it fails for every example today and becomes
    change 8's regression test unchanged. Asserting today's behavior instead would
    produce a test that has to be deleted at task 10, which regresses nothing.
    """

    # Feature: export-gate-and-preview-fidelity, Property 1: Bug Condition
    @settings(max_examples=100, deadline=None)
    @given(
        directories=st.lists(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=8).filter(
                lambda name: name not in _RESERVED_DIR_NAMES
            ),
            max_size=4,
        ),
        name=st.sampled_from((COVERAGE_FILE, f"{COVERAGE_FILE}.host.1.2", f"{COVERAGE_FILE}.local.99.1")),
        sibling=st.sampled_from(("action.py", "connection.py", "util.py")),
    )
    def test_a_coverage_file_at_any_depth_is_excluded_from_the_packaged_set(self, directories, name, sibling):
        """**Validates: Requirements 1.10, 2.15**

        Measured through ``list_plugin_files`` rather than by writing an archive,
        because that function is the single source of truth the packager consumes
        (Property 30, asserted directly in
        :meth:`TestPreservationTheArtifactStillCarriesThePlugin.test_the_export_preview_equals_the_archive`),
        and writing a hundred tarballs would measure ``tarfile``.
        """
        assert not set(directories) & _RESERVED_DIR_NAMES, "the generator's own guard did not hold"
        root = Path(tempfile.mkdtemp(prefix="icpb-coverage-depth-"))
        try:
            directory = root.joinpath(*directories)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / sibling).write_text("# a file the plugin needs\n", encoding="utf-8")
            (directory / name).write_text("coverage data\n", encoding="utf-8")
            relative = PurePosixPath(*directories, name).as_posix()
            packaged = list_plugin_files(root)
            assert relative not in packaged, (
                f"{relative} is packaged; `list_plugin_files` drops a path only when one of its parts is in "
                f"_EXCLUDED_DIRS {sorted(_EXCLUDED_DIRS)}, and no byproduct file name is in that set"
            )
            assert (
                PurePosixPath(*directories, sibling).as_posix() in packaged
            ), "the plugin file beside the byproduct was dropped too, so the exclusion is too broad (3.2)"
        finally:
            shutil.rmtree(root, ignore_errors=True)


def test_the_byproduct_measurements_inputs_are_recorded():
    """Guard: state the mechanism, the real tree's baseline, and the two lists that differ.

    Read-only against ``~/.icplugin-builder/projects/``: it enumerates what would
    be packaged and never packages, so it leaves no artifact and writes no
    ``.coverage``. Written so it survives the fix -- the baseline is checked as
    "either 1.10's 39 with the byproducts or 3.2's 37 without them", so the figure
    is pinned in both directions rather than only before.
    """
    assert _EXCLUDED_DIRS == frozenset({BUILDER_METADATA_DIR, ".git", "__pycache__", ".pytest_cache", ".mypy_cache"}), (
        "`_EXCLUDED_DIRS` is no longer the five directory names task 1.9 measured, so the mechanism this "
        f"task describes must be re-read before change 8 replaces it: {sorted(_EXCLUDED_DIRS)}"
    )
    assert not [name for name in _EXCLUDED_DIRS if PurePosixPath(name).suffix and name != ".coverage"], (
        "a file-shaped name has appeared in `_EXCLUDED_DIRS`; the claim that it 'covers directories only' "
        f"needs re-measuring: {sorted(_EXCLUDED_DIRS)}"
    )
    assert COVERAGE_FILE not in _EXCLUDED_DIRS, (
        "`.coverage` is in `_EXCLUDED_DIRS`, so 1.10's root cause has already changed and task 10 should "
        "be re-scoped rather than implemented as designed"
    )

    # The two lists are different lists, and packaging consumes this one. Task 1.3
    # recorded `.pytest_cache` as absent from `GENERATED_DIR_NAMES`; that is true
    # and it is *not* true of the packaging set, which is the distinction change 8
    # has to preserve when the two are drawn together into `core/plugin_files`.
    assert ".pytest_cache" in _EXCLUDED_DIRS, "packaging no longer excludes `.pytest_cache`"
    assert "__pycache__" in _EXCLUDED_DIRS, "packaging no longer excludes `__pycache__`"

    _require_tree()
    packaged = tuple(list_plugin_files(JUMPCLOUD_TREE))
    byproducts = tuple(member for member in packaged if _is_byproduct(member))
    assert len(packaged) in (BASELINE_MEMBER_COUNT, BASELINE_LESS_BYPRODUCTS), (
        f"{JUMPCLOUD_TREE} would package {len(packaged)} file(s), which is neither the "
        f"{BASELINE_MEMBER_COUNT} the run recorded nor the {BASELINE_LESS_BYPRODUCTS} that 3.2 requires "
        f"afterwards; the tree has changed and the baseline should be re-recorded. Byproducts: "
        f"{list(byproducts)}"
    )
    assert byproducts in ((), BUGFIX_1_10_MEMBERS), (
        f"the byproducts {JUMPCLOUD_TREE} would package are {list(byproducts)}, not the "
        f"{list(BUGFIX_1_10_MEMBERS)} `bugfix.md` 1.10 names; whichever document is wrong should be "
        "corrected rather than worked around here"
    )
    labels = tuple(case.label for case in BYPRODUCT_CASES)
    assert len(set(labels)) == len(labels), f"the byproduct table has duplicate labels: {labels}"
    assert {case.kind for case in BYPRODUCT_CASES} == {
        KIND_COVERAGE,
        KIND_CACHE,
        KIND_BUILD,
        KIND_METADATA,
        KIND_VCS,
    }, "the byproduct table no longer covers all five kinds change 8's predicate has to encode"


# ---------------------------------------------------------------------------
# Task 1.10, integrations half -- what a blocked export reports (1.11) and which
# credential types the toolchain defines (1.17)
# ---------------------------------------------------------------------------
#
# Task 1.10 lists seven counterexamples across two layers. The five that live in
# the orchestrator -- progress, token accounting, interpreter truncation, and the
# export preview's ``version_display`` -- are in
# ``tests/orchestrator/test_preview_fidelity_bug_conditions.py``. The two here are
# the integrations-layer pair:
#
#   * **1.11 / 2.16** -- "WHEN an export is blocked, THEN the system reports only
#     stage names (``\"failed code stages: lint, test\"``) and carries no
#     prospector or pytest output, so the operator has to reproduce all four
#     stages by hand to learn what failed."
#   * **1.17 / 2.22** -- ``credential_token`` is rejected by
#     :data:`~icplugin_builder.core.spec_completeness.VALID_CREDENTIAL_TYPES`
#     though the installed toolchain defines it.
#
# **Why the API serializer is read from an integrations test.** 1.11 is a claim
# about what reaches the operator, and the only place a stage result becomes an
# operator-facing payload is ``api/app.py``'s ``_serialize_export_plan``. The data
# it drops is produced here -- :class:`StageResult` carries ``stdout``,
# ``stderr``, ``returncode`` and ``message``, and
# :attr:`PipelineReport.failed_stages` hands every failing one to
# :func:`decide_export`. Asserting only against the :class:`ExportDecision` would
# measure half the path and could not tell "the gate dropped it" from "the
# serializer dropped it", so both are measured and the boundary between them is
# named. Task 11.1 puts the fix in the serializer, which is what makes this the
# right thing to pin.
#
# **Two pipeline reports, because they answer different questions.**
#
#   1. A **constructed** report whose failing stages carry recognisable prospector
#      and pytest text (:func:`_blocked_report`). Deterministic, needs no Docker,
#      and lets the claim "no stage output reaches the payload" be stated over
#      output whose exact bytes are known. It also carries the >10,000-character
#      case, which the real tree does not produce.
#   2. The **real** four-stage run over the JumpCloud tree (``_pipeline_report``,
#      shared with task 1.1), so the claim is also taken against the output the
#      reproduction run actually saw. Skips without Docker.
#
# **1.17 is measured against the installed package, not against the document.**
# `bugfix.md` records ``insight_plugin/features/common/schema_util.py`` defining
# ``credential_token`` with shape ``{token, domain}`` at version 1.9.20. That is
# read out of the installed toolchain here rather than trusted, because the whole
# point of Property 74 is that the valid set comes from the toolchain's own schema
# instead of from a hand-maintained tuple -- and a test that trusted the document
# would reproduce exactly the failure mode it exists to prevent.
#
# Nothing here edits production code, and nothing here writes into
# ``~/.icplugin-builder/projects/``: the constructed reports name a throwaway
# directory and the real report is the read-only one task 1.1 already runs.
#
# _Requirements: 1.11, 1.17_

#: The blocked-export summary `bugfix.md` 1.11 quotes. Pinned so the claim under
#: test is the document's own claim, and so a reworded summary is visible.
RECORDED_BLOCKED_SUMMARY_FRAGMENT = "failed code stages: lint, test"

#: The two stages that failed in the reproduction run, in pipeline order.
RECORDED_FAILED_STAGES: Tuple[str, ...] = (StageName.LINT, StageName.TEST)

#: A prospector-shaped line and a pytest-shaped line, standing in for the output
#: 2.16 requires reach the operator. Their content is immaterial; what matters is
#: that they are long enough to recognise and could not appear by accident.
_PROSPECTOR_OUTPUT_LINE = "icon_jumpcloud/util/api.py:12:1: F401 'requests' imported but unused"
_PYTEST_OUTPUT_LINE = (
    "FAILED unit_test/test_suspend_user.py::TestSuspendUser::test_suspend - AssertionError: 401 != 200"
)

#: A stage output past :data:`MAX_DISPLAY_CHARS`, so the "bounded display, full
#: text retained" half of 2.16 is measurable rather than only the presence half.
_OVERLONG_STAGE_OUTPUT = "E501 line too long\n" * 900

#: The credential type `bugfix.md` 1.17 is about.
CREDENTIAL_TOKEN = "credential_token"

#: Its shape as 1.17 records it: a required ``token`` and an optional ``domain``.
RECORDED_CREDENTIAL_TOKEN_REQUIRED: Tuple[str, ...] = ("token",)
RECORDED_CREDENTIAL_TOKEN_PROPERTIES: Tuple[str, ...] = ("domain", "token")

#: The version `bugfix.md` "Reproduction Environment" records for the CLI on
#: ``PATH``, and the line ``design.md`` cites for the definition.
RECORDED_INSIGHT_PLUGIN_VERSION = "1.9.20"
RECORDED_SCHEMA_UTIL_LINE = 109

#: The finding task 1.6 measured in situ -- one of the diverged session's 16.
RECORDED_IN_SITU_CREDENTIAL_FINDING = "invalid_credential_type:connection.api_key.type"

#: Printed by :func:`_toolchain_credential_probe` and parsed back as JSON, so the
#: toolchain's schema can be read from an interpreter that is not this one.
_CREDENTIAL_PROBE = (
    "import json;"
    "from insight_plugin.features.common.schema_util import SchemaUtil;"
    "print(json.dumps({name: definition for name, definition in SchemaUtil.BASE_TYPES.items()"
    " if name.startswith('credential')}))"
)


def _blocked_report(
    project_dir: Path,
    *,
    lint_stdout: str = _PROSPECTOR_OUTPUT_LINE,
    test_stdout: str = _PYTEST_OUTPUT_LINE,
) -> PipelineReport:
    """A four-stage report whose ``lint`` and ``test`` stages failed with output.

    Mirrors the reproduction run's shape -- ``build`` and ``validate`` passed,
    ``lint`` and ``test`` did not -- and, unlike the run, carries output whose
    exact bytes this module chose, so "the payload does not contain it" is a
    statement about a known string.

    ``message`` is left empty on purpose. :class:`StageResult` documents it as "a
    human-readable note explaining a non-pass outcome (timeout, missing
    executable, or the actionable Docker error)", so a stage that simply exited
    non-zero carries none -- which is why the gate's reason line for such a stage
    falls back to ``"<name> stage did not pass"`` and says nothing about what
    failed.
    """
    stages = tuple(
        StageResult(
            name=name,
            status=StageStatus.FAILED if name in RECORDED_FAILED_STAGES else StageStatus.PASSED,
            returncode=1 if name in RECORDED_FAILED_STAGES else 0,
            stdout=(lint_stdout if name == StageName.LINT else test_stdout) if name in RECORDED_FAILED_STAGES else "",
            stderr="",
            duration_seconds=0.5,
            message="",
        )
        for name in StageName.ORDER
    )
    return PipelineReport(project_dir=project_dir, stages=stages, docker_available=True)


def _plan_for(report: PipelineReport) -> ExportPlan:
    """The :class:`ExportPlan` a blocked preview would carry for ``report``.

    Assembled through the production types -- :func:`decide_export` for the
    decision, :func:`diff_file_trees` for the first-version diff,
    :func:`bump_version` for the no-prior-export bump -- so what is serialized
    below is the shape ``prepare_export`` produces and not a hand-built stand-in.
    ``spec_report`` is left ``None``: a preview whose spec has not been validated
    is blocked for that reason too, which is visible in the summary and does not
    affect what the failing stages carry.
    """
    spec = PluginSpec(
        name="jumpcloud",
        title="JumpCloud",
        description="A test spec for the blocked-export payload.",
        version=SemVer(1, 0, 0),
        vendor="rapid7",
    )
    return ExportPlan(
        decision=decide_export(None, report),
        spec_preview=spec,
        file_list=("plugin.spec.yaml",),
        diff=diff_file_trees(None, {"plugin.spec.yaml": "x"}),
        version_bump=bump_version(spec.version, [], is_breaking=False),
        pipeline_report=report,
    )


def _payload_text(plan: ExportPlan) -> str:
    """The blocked preview as the operator's client receives it, as one string.

    Serialized with :func:`json.dumps` rather than inspected field by field
    because the claim is a *negative* one -- no stage output is present anywhere
    in the payload -- and a field-by-field check could only refute it for the
    fields it thought to look at.
    """
    return json.dumps(_serialize_export_plan(plan), sort_keys=True, default=str)


def _failing_stage_entries(plan: ExportPlan) -> Tuple[Any, ...]:
    """Whatever the payload carries per failing stage, in payload order.

    Today that is a tuple of bare stage-name strings. Written to read the payload
    rather than the report so it keeps measuring the operator-facing shape after
    task 11.1 replaces those strings with objects.
    """
    return tuple(_serialize_export_plan(plan)["failed_stages"])


def _stage_output_in(payload: str, stage_output: str) -> bool:
    """Is ``stage_output``'s first line present in ``payload``?

    The first line rather than the whole text so a bounded display (2.16 allows
    truncation at :data:`MAX_DISPLAY_CHARS`) still counts as present.
    """
    first_line = stage_output.strip().splitlines()[0]
    return first_line in payload


@lru_cache(maxsize=1)
def _toolchain_credential_types() -> Optional[Dict[str, Any]]:
    """The credential types the installed ``insight-plugin`` defines, or ``None``.

    Read from ``SchemaUtil.BASE_TYPES`` in this interpreter when the toolchain is
    importable here, and otherwise out of the interpreter this tool *resolves* for
    the plugin toolchain (:func:`resolve_target_python`), through a subprocess.

    The second path is not belt-and-braces. On the reproduction host this
    repository's own virtualenv has no ``insight_plugin`` at all while the
    resolved target interpreter has it, so a cross-check written as "skip when
    ``insight_plugin`` is not importable" -- which is how task 11.6 states it --
    would skip silently in exactly the environment where the drift it guards
    against would go unnoticed. Recorded here as a note for that task.
    """
    try:
        from insight_plugin.features.common.schema_util import SchemaUtil  # noqa: PLC0415 - probe, not a dependency

        return {name: definition for name, definition in SchemaUtil.BASE_TYPES.items() if name.startswith("credential")}
    except ImportError:
        pass

    interpreter = _target_python().executable
    if interpreter is None:
        return None
    probed = _capture([interpreter, "-c", _CREDENTIAL_PROBE], timeout=120.0)
    if probed is None or probed[0] != 0:
        return None
    # The toolchain's own imports emit urllib3 warnings on this host, so the JSON
    # is the last line rather than the whole of stdout.
    for line in reversed(probed[1].strip().splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        return parsed if isinstance(parsed, dict) else None
    return None


def _require_toolchain_credential_types() -> Dict[str, Any]:
    """The toolchain's credential types, or a skip when no interpreter has them."""
    types = _toolchain_credential_types()
    if types is None:
        pytest.skip(
            "no interpreter available to this test has insight_plugin installed, so the toolchain's own "
            "credential schema cannot be read; a hardcoded expectation here would be the very thing "
            "Property 74 rejects"
        )
    return types


def _credential_finding_keys(declared: str) -> Tuple[str, ...]:
    """The completeness findings for a connection declaring ``declared``.

    A minimal mapping rather than a whole spec: :func:`check_completeness` reads
    ``connection`` independently of everything else, and the eleven absent
    top-level fields a real draft would also report are noise for this claim.
    """
    mapping = {"connection": {"api_key": {"type": declared, "required": True, "title": "API Key"}}}
    return tuple(
        finding.key for finding in check_completeness(mapping).findings if finding.code == "invalid_credential_type"
    )


class TestABlockedExportReportsOnlyStageNames:
    """`bugfix.md` 1.11 / 2.16 -- the operator must not have to re-run the pipeline.

    Expected to FAIL on unfixed code. ``_serialize_export_plan`` emits
    ``failed_stages`` as ``[stage.name for stage in plan.decision.failed_stages]``,
    so every stage's ``stdout``, ``stderr``, ``returncode`` and ``message`` is
    discarded at the boundary even though the report carries all four.
    """

    def test_the_payload_carries_each_failing_stages_output(self, tmp_path):
        """2.16's requirement, stated over output whose bytes are known."""
        payload = _payload_text(_plan_for(_blocked_report(tmp_path)))
        absent = [
            label
            for label, text in (("lint (prospector)", _PROSPECTOR_OUTPUT_LINE), ("test (pytest)", _PYTEST_OUTPUT_LINE))
            if not _stage_output_in(payload, text)
        ]
        assert not absent, (
            f"the blocked preview carries no output for {absent}. Its whole account of the failure is "
            f"failed_stages={list(_failing_stage_entries(_plan_for(_blocked_report(tmp_path))))!r} -- stage "
            "names and nothing else -- so the operator has to reproduce the pipeline by hand to learn what "
            f"failed. The report the payload was built from carries {len(_PROSPECTOR_OUTPUT_LINE)} and "
            f"{len(_PYTEST_OUTPUT_LINE)} characters of stdout for those two stages"
        )

    def test_each_failing_stage_is_reported_with_its_returncode_and_status(self, tmp_path):
        """The rest of the entry task 11.1 specifies: name, status, returncode, message.

        Asserted as "the entry is not just a name" rather than against a field
        list, so it holds for any shape that actually carries the four values.
        """
        entries = _failing_stage_entries(_plan_for(_blocked_report(tmp_path)))
        bare = tuple(entry for entry in entries if isinstance(entry, str))
        assert not bare, (
            f"{len(bare)} of the {len(entries)} failing-stage entries are bare strings {bare}, so the "
            "payload carries no status, returncode, message or output for them. Every one of those four is "
            "on the StageResult the report already holds"
        )

    def test_every_failing_stage_is_reported_and_not_only_the_first(self, tmp_path):
        """Two stages failed, so two must be accounted for.

        ``classify_build_failure`` -- the existing path from a report to an
        operator-facing failure -- takes ``failed[0]`` and stops, which is why
        2.16 says "for each stage that did not pass" and task 11.1 says "every
        failing stage, not just the first". The measurement below records both:
        that the classifier stops at one, and that the payload carries the output
        of neither.
        """
        report = _blocked_report(tmp_path)
        assert tuple(stage.name for stage in report.failed_stages) == RECORDED_FAILED_STAGES
        classified = classify_build_failure(report)
        assert _PROSPECTOR_OUTPUT_LINE in classified.displayed_output, (
            "the existing classifier does not even describe the first failing stage's output, so the "
            f"premise of this measurement is wrong: {classified.displayed_output[:200]!r}"
        )
        assert _PYTEST_OUTPUT_LINE not in classified.displayed_output, (
            "classify_build_failure now describes more than the first failing stage; task 11.1's "
            "'every failing stage, not just the first' should be re-scoped against what it actually does"
        )
        payload = _payload_text(_plan_for(report))
        carried = tuple(
            name
            for name, text in ((StageName.LINT, _PROSPECTOR_OUTPUT_LINE), (StageName.TEST, _PYTEST_OUTPUT_LINE))
            if _stage_output_in(payload, text)
        )
        assert carried == RECORDED_FAILED_STAGES, (
            f"the payload carries output for {carried or 'no'} failing stage(s) where both failed. The "
            "classifier stops at the first and the serializer keeps neither, so nothing on the export path "
            "reports the second"
        )

    def test_overlong_stage_output_is_bounded_and_retained_in_full(self, tmp_path):
        """The truncation half of 2.16: displayed bounded, full text kept.

        Req 19.5's rule already exists as :func:`truncate_error_output`, so this
        asserts the payload uses it rather than that it works. Expected to FAIL on
        the first clause -- there is no output in the payload to bound.
        """
        report = _blocked_report(tmp_path, lint_stdout=_OVERLONG_STAGE_OUTPUT)
        payload = _payload_text(_plan_for(report))
        bounded = truncate_error_output(_OVERLONG_STAGE_OUTPUT)
        assert len(_OVERLONG_STAGE_OUTPUT) > MAX_DISPLAY_CHARS, "the overlong fixture is not overlong"
        assert _stage_output_in(payload, _OVERLONG_STAGE_OUTPUT), (
            f"a {len(_OVERLONG_STAGE_OUTPUT)}-character lint failure reaches the operator as the single "
            f"word {StageName.LINT!r}. Req 19.5's rule would display its first {MAX_DISPLAY_CHARS} "
            f"characters ({len(bounded.displayed)} here) and retain {len(bounded.full)}"
        )

    def test_the_real_blocked_preview_carries_no_prospector_or_pytest_output(self):
        """The same claim against the concrete tree's own four-stage run.

        Skips without Docker -- a stage that failed because the engine is absent
        would be measuring the host. Expected to FAIL when it runs: whatever the
        real stages printed, the payload carries their names.
        """
        _require_tree()
        report = _pipeline_report()
        failing = tuple(stage.name for stage in report.failed_stages)
        if not failing:
            pytest.skip(
                "the four-stage pipeline passed for this tree, so there is no blocked export to inspect; "
                "1.11 is a claim about what a blocked one reports"
            )
        payload = _payload_text(_plan_for(report))
        unreported = tuple(
            stage.name
            for stage in report.failed_stages
            if (stage.stdout.strip() or stage.stderr.strip())
            and not _stage_output_in(payload, stage.stdout.strip() or stage.stderr.strip())
        )
        assert not unreported, (
            f"the real pipeline failed {list(failing)} and the preview reports "
            f"{list(_failing_stage_entries(_plan_for(report)))}. Output produced but not reported: "
            f"{list(unreported)}. First line of each: "
            + "; ".join(
                f"{stage.name}: {(stage.stdout.strip() or stage.stderr.strip()).splitlines()[0][:160]!r}"
                for stage in report.failed_stages
                if stage.stdout.strip() or stage.stderr.strip()
            )
        )


class TestPreservationTheBlockedGateStillNamesItsStages:
    """What must not change when the detail is added (3.5).

    Expected to pass before and after. The gate stays the four-stage conjunction
    and the summary still names the stages that failed; 2.16 adds their output
    beside that, it does not replace it.
    """

    def test_the_recorded_summary_still_reproduces(self, tmp_path):
        """``"failed code stages: lint, test"`` -- the string 1.11 quotes."""
        plan = _plan_for(_blocked_report(tmp_path))
        assert RECORDED_BLOCKED_SUMMARY_FRAGMENT in plan.decision.summary(), (
            f"the blocked summary no longer contains {RECORDED_BLOCKED_SUMMARY_FRAGMENT!r}, so `bugfix.md` "
            f"1.11's quotation is stale: {plan.decision.summary()!r}"
        )

    def test_the_export_stays_blocked_and_the_stage_names_stay_present(self, tmp_path):
        """The verdict and the names, which the added detail must not displace."""
        plan = _plan_for(_blocked_report(tmp_path))
        payload = _serialize_export_plan(plan)
        assert plan.permitted is False, "a report with two failing stages permitted the export"
        assert payload["permitted"] is False
        named = tuple(entry if isinstance(entry, str) else entry.get("name") for entry in payload["failed_stages"])
        assert named == RECORDED_FAILED_STAGES, (
            f"the payload accounts for {named} where the failing stages are {RECORDED_FAILED_STAGES}; "
            "whatever shape the entries take, both stages have to appear"
        )


class TestTheToolchainDefinesCredentialToken:
    """`bugfix.md` 1.17's premise, read out of the installed package.

    Expected to pass before and after the fix: these are facts about the
    toolchain, not about this tool. They are asserted because 1.17's claim is
    exactly that the toolchain defines a type this repository calls invalid, and
    that claim is only as good as the package it is read from.
    """

    def test_the_toolchain_defines_credential_token(self):
        """``SchemaUtil.BASE_TYPES`` carries it."""
        types = _require_toolchain_credential_types()
        assert CREDENTIAL_TOKEN in types, (
            f"the installed toolchain defines {sorted(types)} and not {CREDENTIAL_TOKEN!r}, so 1.17's "
            "premise does not hold for this installation and the requirement should be re-checked before "
            "task 11.6 adds the type"
        )

    def test_credential_token_has_the_recorded_token_and_domain_shape(self):
        """The ``{token, domain}`` shape 1.17 and 2.22 name, with ``token`` required."""
        definition = _require_toolchain_credential_types()[CREDENTIAL_TOKEN]
        properties = tuple(sorted(definition.get("properties", {})))
        required = tuple(definition.get("required", ()))
        assert properties == RECORDED_CREDENTIAL_TOKEN_PROPERTIES, (
            f"{CREDENTIAL_TOKEN} carries properties {properties}, not the "
            f"{RECORDED_CREDENTIAL_TOKEN_PROPERTIES} `bugfix.md` 1.17 records"
        )
        assert required == RECORDED_CREDENTIAL_TOKEN_REQUIRED, (
            f"{CREDENTIAL_TOKEN} requires {required}, not {RECORDED_CREDENTIAL_TOKEN_REQUIRED}; the steering "
            "correction 2.22 asks for describes a required token and an optional domain"
        )
        token = definition["properties"]["token"]
        assert token.get("format") == "password", (
            "the toolchain's token field is not password-formatted, so 11.6's description of the shape is "
            f"wrong: {token}"
        )


class TestCredentialTokenIsRejectedByTheCompletenessCheck:
    """`bugfix.md` 1.17 / 2.22 and design Property 74. **Expected to FAIL now.**

    :data:`VALID_CREDENTIAL_TYPES` is a hand-maintained tuple of three, and the
    comment above it offers ``credential_token`` as its example of a type "the
    platform does not define". The toolchain defines four. So a spec that the
    toolchain would accept reads as a defect, which is what task 1.6 already
    observed in situ as one of the diverged session's sixteen findings.
    """

    def test_the_valid_set_matches_the_toolchains_own_schema(self):
        """Property 74's requirement: the set comes from the installed schema."""
        toolchain = tuple(sorted(_require_toolchain_credential_types()))
        missing = tuple(name for name in toolchain if name not in VALID_CREDENTIAL_TYPES)
        invented = tuple(name for name in VALID_CREDENTIAL_TYPES if name not in toolchain)
        assert not missing and not invented, (
            f"the toolchain defines {toolchain} and this repository accepts "
            f"{tuple(sorted(VALID_CREDENTIAL_TYPES))}. Defined but rejected: {missing}. Accepted but not "
            f"defined: {invented}. The tuple is maintained by hand, so the two can and did drift"
        )

    def test_a_connection_declaring_credential_token_reports_no_finding(self):
        """2.22's requirement, over the type the run's spec actually declared."""
        _require_toolchain_credential_types()
        keys = _credential_finding_keys(CREDENTIAL_TOKEN)
        assert not keys, (
            f"a connection field declaring {CREDENTIAL_TOKEN!r} -- which the installed toolchain defines -- "
            f"is reported as {list(keys)}. Task 1.6 measured this same key in situ as one of the diverged "
            f"session's 16 findings: {RECORDED_IN_SITU_CREDENTIAL_FINDING}"
        )

    def test_the_in_situ_finding_key_still_reproduces(self):
        """The key task 1.6 observed, re-taken here in isolation.

        Fails on unfixed code by *reproducing* rather than by contradiction, so
        the recorded key is checked instead of trusted. Inverts when task 11.6
        lands.
        """
        keys = _credential_finding_keys(CREDENTIAL_TOKEN)
        assert RECORDED_IN_SITU_CREDENTIAL_FINDING not in keys, (
            f"{RECORDED_IN_SITU_CREDENTIAL_FINDING} reproduces exactly, against a credential type the "
            "installed toolchain defines. This assertion is written to fail while the bug is present; when "
            "task 11.6 lands it passes"
        )

    def test_the_comment_no_longer_offers_credential_token_as_undefined(self):
        """The other half of task 11.6, which is a documentation defect in code.

        The comment above :data:`VALID_CREDENTIAL_TYPES` reads "A plugin using
        anything else (``credential_token``, say) will not bind its credential at
        runtime". That is the reasoning the tuple encodes, so leaving it in place
        while adding the type would leave the next reader with a contradiction.
        Expected to FAIL now.
        """
        source = Path(spec_completeness.__file__).read_text(encoding="utf-8")
        marker = "VALID_CREDENTIAL_TYPES: Tuple[str, ...]"
        assert marker in source, f"{spec_completeness.__file__} no longer declares {marker}"
        comment = source.split(marker)[0].rsplit("\n\n", 1)[-1]
        assert CREDENTIAL_TOKEN not in comment, (
            f"the comment introducing VALID_CREDENTIAL_TYPES still names {CREDENTIAL_TOKEN!r} as its "
            f"example of a type the platform does not define, though it does: {comment.strip()!r}"
        )


class TestPreservationAnInventedCredentialTypeIsStillReported:
    """3.3 -- widening the set to the toolchain's own must not empty it.

    Expected to pass before and after. If it ever fails after task 11.6, the fix
    stopped checking credential types rather than correcting which ones it knows.
    """

    @pytest.mark.parametrize("declared", ("credential_bearer_token", "credential_", "credential_oauth2"))
    def test_a_type_the_toolchain_does_not_define_is_reported(self, declared: str):
        keys = _credential_finding_keys(declared)
        assert keys == (RECORDED_IN_SITU_CREDENTIAL_FINDING,), (
            f"{declared!r} is defined by no toolchain schema and produced {list(keys)}; a type nobody "
            "defines has to stay reported"
        )

    def test_a_non_credential_type_is_not_judged_as_one(self):
        """The check keys off the ``credential`` prefix, which stays as it is."""
        assert not _credential_finding_keys("string")
        assert not _credential_finding_keys("password")


def test_the_reporting_and_credential_measurements_inputs_are_recorded():
    """Guard: state the toolchain installation, the version, and the two sets.

    Read-only. Written to survive the fix: the version is checked as "either the
    1.9.20 `bugfix.md` records or whatever is installed, and the figure is
    reported either way", and the accepted set is checked as "either today's three
    or the toolchain's four", so a re-measurement is visible rather than absorbed.
    """
    types = _require_toolchain_credential_types()
    toolchain = tuple(sorted(types))
    assert len(toolchain) == 4, (
        f"the installed toolchain defines {len(toolchain)} credential types {toolchain}, not the four "
        "`bugfix.md` 1.17 and design 2.22 were written against; re-read the schema before task 11.6"
    )
    assert tuple(sorted(VALID_CREDENTIAL_TYPES)) in (
        ("credential_asymmetric_key", "credential_secret_key", "credential_username_password"),
        toolchain,
    ), (
        f"VALID_CREDENTIAL_TYPES is {tuple(sorted(VALID_CREDENTIAL_TYPES))}, which is neither the three "
        f"this task measured nor the toolchain's {toolchain}; whichever it is now should be re-recorded"
    )

    # The CLI on PATH and the package importable to a Python are two installations
    # and can differ, which is worth stating rather than discovering: on the
    # reproduction host the CLI is 1.9.20 and the importable package is 1.11.0.
    # Both define the same four types at the same lines, so 1.17 does not turn on
    # which one is read -- but a future divergence would, and this is where it
    # would show up.
    reported = _capture(["insight-plugin", "--version"], timeout=120.0)
    cli_version = "not on PATH" if reported is None or reported[0] != 0 else reported[1].strip().splitlines()[-1]
    assert cli_version != "", "insight-plugin reported an empty version"
    if cli_version != RECORDED_INSIGHT_PLUGIN_VERSION:
        # Not a failure: 1.17 is about what the schema says, not about a version.
        # Recorded so a figure taken here can be attributed to an installation.
        print(  # noqa: T201 - this is the record
            f"note: insight-plugin on PATH reports {cli_version!r}, not the "
            f"{RECORDED_INSIGHT_PLUGIN_VERSION!r} `bugfix.md` records"
        )

    assert MAX_DISPLAY_CHARS == 10_000, (
        f"the display bound is {MAX_DISPLAY_CHARS}, not the 10,000 Req 19.5 and task 11.1 name; the "
        "truncation measurement above should be retaken"
    )
    assert StageName.ORDER == (StageName.LINT, StageName.BUILD, StageName.TEST, StageName.VALIDATE), (
        f"the four stages are no longer {RECORDED_FAILED_STAGES}-inclusive in the recorded order: " f"{StageName.ORDER}"
    )
