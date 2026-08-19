"""
# Feature: export-gate-and-preview-fidelity, Property 66: One definition of the unit test run, in both directions

Property 66 states that the ``test`` stage's verdict equals whether the plugin's
unit tests pass under the resolved interpreter -- passing when they pass and failing
when they fail -- that the ``Quality_Gate``'s test findings derive from the same
execution definition and the same interpreter, and that the two never report
contradictory outcomes for one tree.

The defect this closes is the contradiction itself. The stage ran
``docker run --rm <image> python -m pytest -q`` against an image carrying neither
the tests (the generated ``.dockerignore`` excludes ``unit_test/**/*``) nor pytest
(the runtime image correctly declares no test dependencies), so it failed for every
plugin ever built -- while the gate ran the same tests on the host and reported them
passing. One tree, two subsystems, opposite answers, and no way for an operator to
tell which was lying.

**Both directions matter, and the second is the one that bites.** A stage hard-wired
to pass would satisfy "passes when the tests pass" and be worthless. So each example
is generated with a *known* expected outcome and the verdict is asserted against
that, not merely against the other subsystem's agreement.

pytest is driven for real against tiny generated trees, because the parsing under
test is of pytest's own output and a fake emitting what we imagine pytest prints
would only test the fake. The interpreter is this process's own, passed explicitly:
that keeps the property about the *agreement* rather than about whether the host
happens to have a qualifying interpreter, which is clause 2.3's separate concern.

**Validates: Requirements 2.1, 2.2, 2.4**
"""

import asyncio
import sys
from pathlib import Path
from typing import Tuple

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from icplugin_builder.core.plugin_files import UNIT_TEST_DIR
from icplugin_builder.integrations.code_validator import CodeValidator, StageName, StageStatus
from icplugin_builder.integrations.plugin_tests import run_unit_tests
from icplugin_builder.integrations.quality_gate import SOURCE_TESTS, QualityGate

PACKAGE = "icon_agreement"

#: This interpreter has pytest by definition -- it is running these tests.
INTERPRETER = sys.executable

_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


def _module(passing: Tuple[bool, ...]) -> str:
    """A test module whose *n*-th test passes or fails as ``passing[n]`` says."""
    lines = []
    for index, ok in enumerate(passing):
        lines.append(f"def test_case_{index}():")
        lines.append(f"    assert {ok!r}")
        lines.append("")
    return "\n".join(lines) or "# no tests here\n"


def _tree(root: Path, passing: Tuple[bool, ...]) -> Path:
    """Build a plugin tree whose suite passes iff every entry of ``passing`` is true."""
    (root / PACKAGE).mkdir(parents=True, exist_ok=True)
    (root / PACKAGE / "api.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / UNIT_TEST_DIR).mkdir(parents=True, exist_ok=True)
    (root / UNIT_TEST_DIR / "test_api.py").write_text(_module(passing), encoding="utf-8")
    return root


def _clean_linters(root: Path) -> Tuple[str, str]:
    """Stub prospector and black that report nothing.

    The property is about the *test* verdict, and a real prospector run per example
    would dominate the cost of a hundred of them without bearing on the claim. The
    lint and format bars have their own property suite.
    """
    prospector = root / "stub-prospector"
    prospector.write_text("#!/bin/sh\nprintf '{\"messages\": []}\\n'\n", encoding="utf-8")
    prospector.chmod(0o755)
    black = root / "stub-black"
    black.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    black.chmod(0o755)
    return str(prospector), str(black)


def _gate(root: Path) -> QualityGate:
    """A gate whose linters are stubbed, so only the test run costs anything."""
    prospector, black = _clean_linters(root)
    return QualityGate(python_executable=INTERPRETER, prospector_executable=prospector, black_executable=black)


def _stage(root: Path) -> StageStatus:
    """The ``test`` stage's verdict for ``root``, reached the way the pipeline reaches it."""
    validator = CodeValidator(test_python_executable=INTERPRETER)
    spec = next(item for item in validator._stage_specs("tag", root) if item.name == StageName.TEST)
    return asyncio.run(validator._run_stage(spec, root))


#: At least one test, so "no tests collected" -- a distinct outcome with its own
#: assertions in the example-based suite -- is not folded into this property.
_SUITES = st.lists(st.booleans(), min_size=1, max_size=4).map(tuple)


@given(passing=_SUITES)
@_SETTINGS
def test_the_stage_verdict_equals_whether_the_tests_pass(tmp_path_factory, passing):
    """Both directions: the stage passes iff the plugin's own suite passes."""
    root = _tree(tmp_path_factory.mktemp("agree"), passing)
    expected_pass = all(passing)

    stage = _stage(root)

    assert stage.passed is expected_pass, (
        f"the suite {'passes' if expected_pass else 'fails'} and the stage says "
        f"{stage.status.value}: {stage.message}"
    )
    assert stage.status is (StageStatus.PASSED if expected_pass else StageStatus.FAILED)


@given(passing=_SUITES)
@_SETTINGS
def test_the_gate_and_the_stage_never_contradict_each_other(tmp_path_factory, passing):
    """One definition, so no tree can make the two disagree.

    This is the shape of the original defect, and it is invisible from either side
    alone: each subsystem was individually self-consistent.
    """
    root = _tree(tmp_path_factory.mktemp("agree"), passing)

    gate_report = asyncio.run(_gate(root).run(root))
    gate_says_pass = not gate_report.by_source(SOURCE_TESTS)
    stage = _stage(root)

    assert gate_says_pass is stage.passed, (
        f"the Quality_Gate reports the tests passing={gate_says_pass} "
        f"(findings={[f.key for f in gate_report.by_source(SOURCE_TESTS)]}) while the test stage says "
        f"passed={stage.passed}: {stage.message}"
    )
    assert gate_says_pass is all(passing), "the gate's own verdict does not match the generated suite"


@given(passing=_SUITES)
@_SETTINGS
def test_both_subsystems_name_the_same_interpreter(tmp_path_factory, passing):
    """Clause 2.4 -- one interpreter, so neither verdict is about a different environment."""
    root = _tree(tmp_path_factory.mktemp("agree"), passing)

    gate_report = asyncio.run(_gate(root).run(root))
    stage = _stage(root)

    assert gate_report.unit_test_run is not None
    assert gate_report.unit_test_run.interpreter == INTERPRETER
    assert INTERPRETER in stage.message, f"the stage does not name the interpreter it used: {stage.message!r}"


@given(passing=_SUITES)
@_SETTINGS
def test_a_precomputed_run_reaches_the_same_verdict_as_running_it_again(tmp_path_factory, passing):
    """Clause 2.4's other half: reusing the gate's run cannot change the answer.

    ``prepare_export`` hands the gate's ``UnitTestRun`` to the stage so a preview
    executes the suite once rather than twice. That is only sound if the reused run
    yields the verdict a fresh one would -- otherwise the saving would buy a
    different answer.
    """
    root = _tree(tmp_path_factory.mktemp("agree"), passing)

    supplied = asyncio.run(run_unit_tests(root, python_executable=INTERPRETER, measure_coverage=False))
    validator = CodeValidator(test_python_executable=INTERPRETER)
    spec = next(item for item in validator._stage_specs("tag", root) if item.name == StageName.TEST)

    reused = asyncio.run(validator._run_stage(spec, root, unit_test_run=supplied))
    fresh = asyncio.run(validator._run_stage(spec, root))

    assert reused.status is fresh.status, (
        f"reusing the run gave {reused.status.value} where running it again gave {fresh.status.value}: "
        f"{reused.message!r} vs {fresh.message!r}"
    )
    assert reused.passed is all(passing)


@given(passing=_SUITES)
@_SETTINGS
def test_the_stage_asks_nothing_of_docker(tmp_path_factory, passing):
    """Clause 2.1 -- a host-run check cannot be gated on the engine, for any tree."""
    root = _tree(tmp_path_factory.mktemp("agree"), passing)
    spec = next(
        item
        for item in CodeValidator(test_python_executable=INTERPRETER)._stage_specs("some/image:tag", root)
        if item.name == StageName.TEST
    )
    assert spec.requires_docker is False
    assert "docker" not in spec.command
    assert "some/image:tag" not in spec.command
