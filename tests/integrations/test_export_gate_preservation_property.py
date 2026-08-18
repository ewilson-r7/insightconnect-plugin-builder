"""Property 75 -- preservation over the generated axes (spec task 2.2).

# Feature: export-gate-and-preview-fidelity, Property 75: Preservation -- nothing else changes

**This test must PASS on unfixed code**, and no production file is edited by this
task. Property 75 says: *for any tree or session satisfying none of*
``isBugCondition_1``, ``isBugCondition_2``, ``isBugCondition_3``, *F′ produces the
same stage verdicts, findings, condition statuses, export decision and packaged
contents as F*, up to the byproducts 2.15 removes and the one stage message
`bugfix.md` records as knowingly changed.

## What this property can assert today, and what it cannot

**F′ does not exist yet.** No fix has landed, so a test that literally compares F
to F′ is unrunnable. What this module does instead is the runnable half of the
same statement:

1. it **quantifies over the four axes** task 2.1 names -- tool presence,
   generated-versus-hand-written defect placement, spec completeness, test outcome
   -- generating trees along those dimensions;
2. it **records the verdict tuple** for every axis point in one fixture,
   ``tests/fixtures/preservation_baseline/property_75_axis_table.json``, so the
   *same test* re-run after each fix (tasks 5.5, 7.5, 9.6) compares F′ against
   what F produced rather than against an expectation written after the change;
3. it **excludes the bug-condition trees**, because Property 75 is scoped to trees
   satisfying *none* of the three conditions, and it excludes them by construction
   with the construction verified per example rather than asserted in prose.

Assertable now: the quality-gate finding set, the twelve definition-of-done
condition statuses, the completeness findings, the packaged member set, and the
export decision as a function of the four stage verdicts. Not assertable until a
fix lands: that F′ agrees with any of it -- that is what tasks 5.5, 7.5 and 9.6
run this file for. Also **not** asserted here: the four stage verdicts as
*observed*, see "Why the pipeline is not run" below.

## How the "satisfies none of the three conditions" set is constructed

Task 1 measured that ``isBugCondition_1`` and ``isBugCondition_2`` hold for
essentially every real plugin tree today -- the ``test`` stage fails for every
plugin (task 1.1) and the ``lint`` stage fails on generated files for every
scaffolded plugin (task 1.2). So the set Property 75 quantifies over is genuinely
narrow, and it is reached here by three deliberate exclusions:

* ``isBugCondition_1`` = ``hostUnitTestsPass ∧ dockerignoreExcludes(unit_test) ∧
  ¬imageHasPytest``. Every tree here falsifies the **second** conjunct: its
  ``.dockerignore`` is :data:`DOCKERIGNORE_KEEPING_THE_TESTS`, which does not
  exclude ``unit_test/``. That is checked per example, not assumed. **The limit,
  stated plainly**: a real scaffolded tree *does* exclude ``unit_test/``, so the
  admissible set here is not the set of trees the tool actually produces. The tree
  that is excluded is exercised on its own by
  :class:`TestTheExcludedTreesReallyAreBugConditionTrees`, and by tasks 1.1 and
  2.1's axis 1 against the real JumpCloud tree.
* ``isBugCondition_2`` = ``findings ≠ ∅ ∧ every finding is in a generated file``.
  The placement axis therefore offers ``no_defect``, ``hand_written`` and
  ``generated_and_hand_written`` and **withholds ``generated_only``**, which is
  precisely a C₂ tree. Falsification is verified per example by running ``flake8``
  -- the linter the ``lint`` stage actually uses (task 1.2's correction to
  `bugfix.md` 1.4) -- over the tree and checking that its findings are not all in
  generated paths. Note the asymmetry this exposes and pins: the ``Quality_Gate``
  filters generated paths out through
  :func:`~icplugin_builder.integrations.quality_gate.is_lint_excluded`, so a
  generated defect is invisible to it, while ``flake8`` reports it. That is why
  the ``generated_and_hand_written`` verdict equals the ``hand_written`` verdict
  in the recorded table: a recorded fact about F, not a defect in the generator.
* ``isBugCondition_3`` = ``implementationDelegated ∧ diskSpec ≠ draftSpec``. It is
  **vacuously false** for everything here: this module builds trees, never
  sessions, and nothing delegates an implementation turn. The session half of
  Property 75 is consequently *not* covered here. It is covered by task 2.1's
  orchestrator-layer axes 7-9 (``tests/orchestrator/test_preservation_baseline.py``)
  and, for the fixed system, by Properties 63 and 64 in tasks 4.8 and 4.9. The
  spec axis below is the part of a session's report that *is* reachable without a
  session: the completeness findings the preview reports, which change 2 re-reads
  from disk and must not otherwise alter.

## Why the pipeline is not run, and what replaces it

A real :meth:`CodeValidator.run_pipeline` costs a ``docker build`` plus an
``insight-plugin validate`` per tree -- minutes each, warm. At 100+ examples that
is not a test, so the observed four-stage verdicts stay where task 2.1 pinned them
(axis 1, against one tree, with Docker really running). What is quantified here
instead is the **gate's mapping from stage verdicts to the export decision**: the
stage tuple is an axis, :func:`decide_export` is called for real, and its decision
is recorded. That keeps "the same export decision" inside the property while
leaving "the same stage verdicts" an example-based baseline, and it is stated here
so the gap is visible rather than papered over.

## Cost, and how 100+ examples is afforded honestly

The verdict for an axis point is a *function* of that point, so it is computed
once and memoised: 36 axis points reduce to 18 ``Quality_Gate`` runs (the spec axis
needs no subprocess) plus 9 ``flake8`` runs, all of them during the module-scoped
fixture. Every Hypothesis example after the first is a dictionary lookup against
the recorded table, which is what makes :data:`MAX_EXAMPLES` -- 250, well past the
100 task 2 requires -- affordable rather than a reason to quietly run five.
Measured wall clock on the reproduction host: about 22 seconds inside the fixture
and about 28 seconds for the whole module, examples included. The exact figure of
the recording run rides on the fixture as ``measured.observation_seconds``.

**Every generated source is kept under 79 columns.** Task 2.1 hit the reason:
``flake8``'s default ``pycodestyle`` runs at 79 while the plugin bar is 120, so a
line between the two fails the ``lint`` stage today and passes after change 5 --
which would put a verdict the fix is *meant* to change into a preservation
baseline. The prospector profile is pinned repository-sourced for the same class of
reason (task 2.1's :class:`TestTheProfileSourceChangesTheConditionStatus`).

**Host dependence, recorded rather than hidden.** The resolved interpreter here has
no ``pytest-cov``, so ``coverage_threshold`` reads ``unverified`` and a coverage
skip note appears throughout. Those are verdicts about verifiability (Req 26.4,
27.5) so they are compared, not merely noted -- which does mean a host that gains
``pytest-cov`` will need the table re-recorded. The fixture's provenance block says
which host it was taken on.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12**
"""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from icplugin_builder.core.spec_completeness import check_completeness
from icplugin_builder.core.spec_model import PluginSpec
from icplugin_builder.integrations.build_engine import list_plugin_files
from icplugin_builder.integrations.code_validator import (
    PipelineReport,
    StageName,
    StageResult,
    StageStatus,
)
from icplugin_builder.integrations.definition_of_done import evaluate_done
from icplugin_builder.integrations.export_gate import decide_export
from icplugin_builder.core.plugin_files import is_generated
from icplugin_builder.integrations.quality_gate import SOURCE_TESTS, QualityReport

from tests.core.test_spec_completeness import complete_mapping
from tests.integrations.test_export_gate_preservation import (
    DEFECTIVE_CLIENT,
    FAILING_UNIT_TEST,
    SOUND_CLIENT,
    UNIT_TEST_DIR,
    _bin_without_prospector,
    _condition_rows,
    _finding_rows,
    _incomplete_spec,
    _run_gate,
    _valid_spec_report,
    _write_tree,
)
from tests.preservation_baseline import load, pin, recording, toolchain_path

#: The fixture this module records F into, and compares F′ against.
TABLE_NAME = "property_75_axis_table"

# ---------------------------------------------------------------------------
# The axes
# ---------------------------------------------------------------------------

#: Axis 1, tool presence. ``prospector`` absent is how `bugfix.md` 3.6's
#: unverified-is-not-met distinction is exercised (Req 26.4, 27.5).
TOOL_AXIS: Tuple[str, ...] = ("prospector_present", "prospector_absent")

#: Axis 2, defect placement. ``generated_only`` is deliberately **absent**: it is
#: exactly ``isBugCondition_2``, so it lies outside Property 75's scope. It is
#: named here so its absence reads as a decision rather than an oversight, and
#: :class:`TestTheExcludedTreesReallyAreBugConditionTrees` demonstrates why.
PLACEMENT_AXIS: Tuple[str, ...] = ("no_defect", "hand_written", "generated_and_hand_written")
EXCLUDED_PLACEMENT = "generated_only"

#: Axis 3, spec completeness. Feeds the ``spec_complete`` condition and the
#: completeness findings the export preview reports.
SPEC_AXIS: Tuple[str, ...] = ("complete", "incomplete")

#: Axis 4, test outcome. All three states matter: ``no_tests`` is its own finding,
#: ``failing`` is `bugfix.md` 3.1's genuinely failing suite, and ``passing`` is the
#: tree change 7 must start passing without turning a failing suite green.
TEST_AXIS: Tuple[str, ...] = ("no_tests", "failing_tests", "passing_tests")

#: Hypothesis budget. Task 2's parent text requires at least 100 examples; the
#: memoised verdict table makes a larger number nearly free, so the budget is spent
#: on covering the 36 x 16 product of tree points and stage tuples rather than
#: pared back.
MAX_EXAMPLES = 250

#: A ``.dockerignore`` that does **not** exclude the unit tests, which is how every
#: tree here falsifies ``isBugCondition_1``'s second conjunct. The file is still
#: present, so the packaged member set is the same shape as a real tree's.
DOCKERIGNORE_KEEPING_THE_TESTS = ".git/**/*\n.builder/**/*\n"

#: The defect `bugfix.md` 1.4 found in the real tree's **generated** files: an
#: unused re-export in ``__init__.py``, which ``flake8`` reports as ``F401`` and
#: the ``Quality_Gate`` never sees, because ``__init__.py`` is generated and the
#: steering forbids editing it.
GENERATED_INIT_WITH_UNUSED_IMPORT = '''"""Generated package init carrying an unused re-export (F401)."""

from .util.api import ExampleApi
'''

#: A unit test that genuinely passes, so the test-outcome axis has a positive
#: state. Every line stays inside 79 columns for the reason in the module
#: docstring.
PASSING_UNIT_TEST = '''"""A unit test that genuinely passes."""

import unittest

from icon_example.util.api import ExampleApi


class TestSuspendUser(unittest.TestCase):
    """The plugin's own test of suspend_user, which passes."""

    def test_suspend_user(self):
        client = ExampleApi("api-key")
        expected = {"method": "PUT", "path": "systemusers/u1"}
        self.assertEqual(client.suspend_user("u1"), expected)
'''

#: File names whose presence in a ``.plg`` is a byproduct 2.15 removes, so they are
#: partitioned out of the compared member set rather than asserted either way.
_BYPRODUCT_SUFFIXES = (".pyc",)
_BYPRODUCT_PREFIXES = (".coverage",)

#: One axis point: ``(tools, placement, spec, tests)``.
AxisPoint = Tuple[str, str, str, str]

#: All 36 admissible axis points, in a deterministic order.
ADMISSIBLE_POINTS: Tuple[AxisPoint, ...] = tuple(itertools.product(TOOL_AXIS, PLACEMENT_AXIS, SPEC_AXIS, TEST_AXIS))

#: All 16 four-stage verdict tuples, for the export-decision half of the property.
STAGE_TUPLES: Tuple[Tuple[bool, ...], ...] = tuple(itertools.product((True, False), repeat=len(StageName.ORDER)))

#: Whether ``flake8`` -- the linter the ``lint`` stage really runs today -- can be
#: resolved. Its absence weakens the C₂ falsification evidence to the
#: ``Quality_Gate``'s own findings, and is recorded rather than silently tolerated.
FLAKE8 = shutil.which("flake8", path=toolchain_path())


def point_key(point: AxisPoint) -> str:
    """Return the table key for ``point``: the axis values, pipe-separated."""
    return "|".join(point)


def stage_key(flags: Tuple[bool, ...]) -> str:
    """Return the table key for one four-stage verdict tuple."""
    return "|".join(f"{name}={int(flag)}" for name, flag in zip(StageName.ORDER, flags))


# ---------------------------------------------------------------------------
# Building and observing one tree
# ---------------------------------------------------------------------------


def _tree_sources(placement: str) -> Dict[str, str]:
    """Return the ``client`` and ``package_init`` sources for a placement.

    Three placements, and the third is the one that matters: a defect in a
    generated file *and* one in a hand-written file is admissible under Property 75
    because not every finding is generated, while a defect in a generated file
    alone is ``isBugCondition_2`` and is withheld from the axis.
    """
    if placement == "no_defect":
        return {"client": SOUND_CLIENT, "package_init": ""}
    if placement == "hand_written":
        return {"client": DEFECTIVE_CLIENT, "package_init": ""}
    if placement == "generated_and_hand_written":
        return {"client": DEFECTIVE_CLIENT, "package_init": GENERATED_INIT_WITH_UNUSED_IMPORT}
    if placement == EXCLUDED_PLACEMENT:
        return {"client": SOUND_CLIENT, "package_init": GENERATED_INIT_WITH_UNUSED_IMPORT}
    raise AssertionError(f"unknown placement {placement!r}")  # pragma: no cover - guards the axis constant


def _unit_test_source(tests: str) -> Optional[str]:
    """Return the ``unit_test/`` source for a test-outcome axis value."""
    if tests == "no_tests":
        return None
    if tests == "failing_tests":
        return FAILING_UNIT_TEST
    if tests == "passing_tests":
        return PASSING_UNIT_TEST
    raise AssertionError(f"unknown test outcome {tests!r}")  # pragma: no cover - guards the axis constant


def _spec_for(completeness: str) -> PluginSpec:
    """Return a completeness-clean or genuinely incomplete spec.

    The incomplete one is task 2.1's own ``_incomplete_spec`` -- missing ``sdk``,
    missing an output example -- so the two modules disagree about nothing. The
    complete one is the mapping ``tests/core/test_spec_completeness.py`` already
    maintains as the shape that passes every check.
    """
    if completeness == "complete":
        return PluginSpec.from_mapping(complete_mapping())
    if completeness == "incomplete":
        return _incomplete_spec()
    raise AssertionError(f"unknown spec axis value {completeness!r}")  # pragma: no cover - guards the constant


def _flake8_rows(root: Path) -> Optional[List[Dict[str, str]]]:
    """Return ``flake8``'s findings over ``root``, or ``None`` if it is absent.

    Run directly rather than through the ``lint`` stage on purpose: this is the
    evidence that a tree is **not** a ``isBugCondition_2`` tree, which is a
    property of the tree and must stay measurable after change 5 replaces the
    stage's linter. ``flake8``'s bare defaults are what task 1.2 measured, so they
    are what the falsification is checked against.
    """
    if FLAKE8 is None:
        return None
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [FLAKE8, "--format=%(path)s|%(code)s", "."],
        cwd=str(root),
        capture_output=True,
        timeout=300.0,
        check=False,
        env={**os.environ, "PATH": toolchain_path()},
    )
    rows: List[Dict[str, str]] = []
    for line in completed.stdout.decode("utf-8", errors="replace").splitlines():
        raw_path, _, code = line.partition("|")
        path = PurePosixPath(raw_path.strip().lstrip("./")).as_posix()
        if not path or not code:
            continue
        rows.append({"path": path, "code": code.strip()})
    return sorted(rows, key=lambda row: (row["path"], row["code"]))


def _dockerignore_excludes_tests(root: Path) -> bool:
    """Return ``True`` iff ``.dockerignore`` keeps ``unit_test/`` out of the image."""
    path = root / ".dockerignore"
    if not path.is_file():
        return False
    return any(UNIT_TEST_DIR in line for line in path.read_text(encoding="utf-8").splitlines())


def _is_byproduct(member: str) -> bool:
    """Return ``True`` iff ``member`` is a local build or test byproduct (2.15)."""
    name = PurePosixPath(member).name
    return name.startswith(_BYPRODUCT_PREFIXES) or name.endswith(_BYPRODUCT_SUFFIXES)


def _packaged_members(root: Path) -> Tuple[List[str], List[str]]:
    """Return ``(plugin_files, byproducts)`` as :func:`list_plugin_files` sees them.

    The archive itself is not built: ``list_plugin_files`` *is* the definition of
    what a ``.plg`` carries (task 2.1's axis 6 read both and found them the same
    set), and building one per axis point would add an ``insight-plugin`` run to
    every example for no extra evidence.
    """
    members = sorted(list_plugin_files(root))
    return (
        [member for member in members if not _is_byproduct(member)],
        [member for member in members if _is_byproduct(member)],
    )


def _pipeline(root: Path, flags: Tuple[bool, ...]) -> PipelineReport:
    """Build a four-stage report with the given pass/fail verdicts.

    Constructed rather than observed, for the reason in the module docstring: a
    real run costs a Docker build per example. Task 2.1's axis 5 states the same
    premise the same way.
    """
    return PipelineReport(
        project_dir=root,
        stages=tuple(
            StageResult(
                name=name,
                status=StageStatus.PASSED if flag else StageStatus.FAILED,
                returncode=0 if flag else 1,
                stdout="",
                stderr="",
                duration_seconds=0.0,
                message="" if flag else f"{name} stage failed with exit code 1",
            )
            for name, flag in zip(StageName.ORDER, flags)
        ),
        docker_available=True,
    )


class _Observatory:
    """Observes F once per distinct axis point and remembers what it saw.

    The memoisation is not an optimisation detail, it is what makes the property
    affordable: a verdict is a function of its axis point, the point space is
    finite, and so 250 Hypothesis examples cost 18 ``Quality_Gate`` runs rather
    than 250.
    """

    def __init__(self, work: Path) -> None:
        self._work = work
        self._trees: Dict[Tuple[str, str], Path] = {}
        self._quality: Dict[Tuple[str, str, str], QualityReport] = {}
        self._flake8: Dict[Tuple[str, str], Optional[List[Dict[str, str]]]] = {}
        self._verdicts: Dict[str, Dict[str, Any]] = {}
        self._sanitised_bin: Optional[str] = None

    # -- inputs ------------------------------------------------------------

    def tree(self, placement: str, tests: str) -> Path:
        """Return the tree for one ``(placement, tests)`` pair, building it once."""
        cached = self._trees.get((placement, tests))
        if cached is not None:
            return cached
        root = self._work / f"tree-{placement}-{tests}"
        _write_tree(
            root,
            unit_test=_unit_test_source(tests),
            dockerignore=DOCKERIGNORE_KEEPING_THE_TESTS,
            **_tree_sources(placement),
        )
        self._trees[(placement, tests)] = root
        return root

    def _bin_without_prospector(self) -> str:
        """A ``PATH`` with the toolchain's other binaries but no ``prospector``."""
        if self._sanitised_bin is None:
            self._sanitised_bin = _bin_without_prospector(self._work / "bin")
        return self._sanitised_bin

    # -- observations ------------------------------------------------------

    def quality(self, tools: str, placement: str, tests: str) -> QualityReport:
        """Run the ``Quality_Gate`` once per ``(tools, placement, tests)``."""
        cached = self._quality.get((tools, placement, tests))
        if cached is not None:
            return cached
        root = self.tree(placement, tests)
        path = None if tools == "prospector_present" else self._bin_without_prospector()
        report = _run_gate(root, path=path, run_tests=True)
        self._quality[(tools, placement, tests)] = report
        return report

    def flake8(self, placement: str, tests: str) -> Optional[List[Dict[str, str]]]:
        """Run ``flake8`` once per tree, for the C₂ falsification evidence."""
        if (placement, tests) not in self._flake8:
            self._flake8[(placement, tests)] = _flake8_rows(self.tree(placement, tests))
        return self._flake8[(placement, tests)]

    def verdict(self, point: AxisPoint) -> Dict[str, Any]:
        """Return the compared verdict tuple for ``point``, observing it once.

        The payload is verdicts only: finding identities, condition statuses,
        completeness keys, the packaged member set. No message text, no timing, no
        absolute path -- message text is what `bugfix.md`'s recorded exception is
        about, so comparing it would make this brittle exactly where the design
        says not to compare.
        """
        key = point_key(point)
        cached = self._verdicts.get(key)
        if cached is not None:
            return cached
        tools, placement, completeness, tests = point
        root = self.tree(placement, tests)
        quality = self.quality(tools, placement, tests)
        spec = _spec_for(completeness)
        done = evaluate_done(root, spec=spec, quality_report=quality)
        completeness_report = check_completeness(spec)
        plugin_files, byproducts = _packaged_members(root)
        verdict = {
            "quality_finding_rows": _finding_rows(quality),
            "quality_checked_files": list(quality.checked_files),
            "quality_skipped": list(quality.skipped),
            "quality_clean": quality.clean,
            "coverage_was_measured": quality.coverage_percent is not None,
            "conditions": _condition_rows(done),
            "outstanding_condition_names": sorted(condition.name for condition in done.outstanding),
            "done_complete": done.complete,
            "completeness_keys": list(completeness_report.keys()),
            "completeness_is_complete": completeness_report.is_complete,
            "packaged_plugin_files": plugin_files,
            "packaged_byproducts": byproducts,
        }
        self._verdicts[key] = verdict
        return verdict

    def bug_condition_evidence(self, point: AxisPoint) -> Dict[str, Any]:
        """Return what makes ``point`` fall outside all three bug conditions.

        Evidence, not assertion: the two host-checkable conjuncts of C₁, the
        ``flake8`` partition that decides C₂, and the structural fact that decides
        C₃. The tests below assert on this rather than trusting the generator.
        """
        tools, placement, _, tests = point
        root = self.tree(placement, tests)
        quality = self.quality(tools, placement, tests)
        rows = self.flake8(placement, tests)
        return {
            "dockerignore_excludes_unit_tests": _dockerignore_excludes_tests(root),
            "host_unit_tests_pass": not quality.by_source(SOURCE_TESTS),
            "flake8_rows": rows,
            "flake8_findings_all_generated": (
                None if rows is None else bool(rows) and all(is_generated(row["path"]) for row in rows)
            ),
            "implementation_delegated": False,
        }


# ---------------------------------------------------------------------------
# The recorded table
# ---------------------------------------------------------------------------


class _Recording:
    """The recorded table, wrapped so a failing example does not print it whole.

    Hypothesis reprs the arguments of a failing example, fixtures included, and the
    table is roughly 70 kB of JSON -- which it warns about, and which buries the one
    axis point that actually moved. The wrapper's :meth:`__repr__` is a one-line
    summary, and the row that differs is named by the assertion instead.
    """

    def __init__(self, document: Any) -> None:
        assert isinstance(document, dict), f"the recorded baseline is not a table: {type(document)}"
        self.trees: Dict[str, Any] = document["trees"]
        self.export_decisions: Dict[str, Any] = document["export_decisions"]

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<recorded {TABLE_NAME}: {len(self.trees)} tree rows, {len(self.export_decisions)} decision rows>"


@pytest.fixture(scope="module")
def observatory(tmp_path_factory) -> _Observatory:
    """One observatory for the module, so each axis point is observed once."""
    return _Observatory(tmp_path_factory.mktemp("property75"))


@pytest.fixture(scope="module")
def axis_table(observatory: _Observatory) -> Dict[str, Any]:
    """Observe F at every admissible axis point and every stage tuple.

    This is the expensive fixture -- roughly two minutes on the reproduction host
    -- and it is where all of the module's subprocess work happens. Everything
    after it reads the memoised result.
    """
    started = time.monotonic()
    trees = {point_key(point): observatory.verdict(point) for point in ADMISSIBLE_POINTS}
    evidence = {point_key(point): observatory.bug_condition_evidence(point) for point in ADMISSIBLE_POINTS}
    decisions = {}
    scratch = observatory.tree("no_defect", "no_tests")
    for flags in STAGE_TUPLES:
        decision = decide_export(_valid_spec_report(), _pipeline(scratch, flags))
        decisions[stage_key(flags)] = {
            "permitted": decision.permitted,
            "spec_valid": decision.spec_valid,
            "code_passed": decision.code_passed,
            "failed_stage_names": [stage.name for stage in decision.failed_stages],
            "block_reason_count": len(decision.reasons),
        }
    return {
        "trees": trees,
        "export_decisions": decisions,
        "bug_condition_evidence": evidence,
        "seconds": round(time.monotonic() - started, 1),
    }


@pytest.fixture(scope="module")
def recorded_table(axis_table: Dict[str, Any]) -> _Recording:
    """Return the recorded table: written in record mode, loaded otherwise.

    Deliberately **not** compared wholesale here. A single assertion over
    thirty-six rows reports all of them when one moved, and reports it as a fixture
    error rather than as a failing example, so the comparison is done row by row by
    the tests below and :func:`tests.preservation_baseline.load` is what reads the
    recording. ``bug_condition_evidence`` and the timing are recorded but never
    compared: the evidence carries ``flake8`` codes, which change 5 stops
    consulting, and the timing is a property of the host.
    """
    if not recording():
        return _Recording(load(TABLE_NAME).observed)
    baseline = pin(
        TABLE_NAME,
        {"trees": axis_table["trees"], "export_decisions": axis_table["export_decisions"]},
        description=(
            "Property 75's verdict table. One row per admissible axis point -- tool presence x defect "
            "placement x spec completeness x test outcome, 36 in all -- carrying the quality-gate finding "
            "identities, the twelve definition-of-done statuses, the completeness findings and the packaged "
            "member set F produces for it; plus one row per four-stage verdict tuple carrying the export "
            "decision F reaches. Trees satisfying any of the three bug conditions are outside Property 75 "
            "and are not in this table: every tree here falsifies isBugCondition_1 by not excluding "
            "unit_test/ from its .dockerignore, falsifies isBugCondition_2 by having no finding at all or at "
            "least one hand-written finding, and falsifies isBugCondition_3 vacuously by being a tree rather "
            "than a delegated session."
        ),
        requirements=(
            "3.1",
            "3.2",
            "3.3",
            "3.4",
            "3.5",
            "3.6",
            "3.7",
            "3.8",
            "3.9",
            "3.10",
            "3.11",
            "3.12",
        ),
        measured={
            "observation_seconds": axis_table["seconds"],
            "flake8_resolved": FLAKE8,
            "bug_condition_evidence": axis_table["bug_condition_evidence"],
            "note": (
                "recorded, not asserted. The flake8 codes are the evidence that each tree falsifies "
                "isBugCondition_2 under the linter the lint stage runs today; change 5 replaces that linter, "
                "so the codes are expected to stop being the tool's business while the falsification stays "
                "true of the trees. coverage reads unverified throughout because the resolved interpreter on "
                "the recording host has no pytest-cov"
            ),
        },
    )
    return _Recording(baseline.observed)


# ---------------------------------------------------------------------------
# Property 75
# ---------------------------------------------------------------------------


class TestPropertyEndSeventyFivePreservation:
    """Property 75 -- for a tree satisfying none of the three bug conditions, the
    verdicts are the ones recorded from F.

    Every example re-observes the axis point it draws (through the memoised
    observatory) and compares against the recorded table, so this class is what
    tasks 5.5, 7.5 and 9.6 re-run: after a fix, a verdict that moved shows up here
    as a named axis point with both payloads printed.
    """

    def test_the_table_covers_every_admissible_point(self, axis_table, recorded_table: _Recording):
        """A generator that stopped covering an axis must fail loudly, not quietly."""
        assert set(axis_table["trees"]) == {point_key(point) for point in ADMISSIBLE_POINTS}
        assert set(recorded_table.trees) == {point_key(point) for point in ADMISSIBLE_POINTS}, (
            "the recorded table does not cover the admissible axis points; re-record with "
            "ICPB_RECORD_PRESERVATION_BASELINE=1 and commit the fixture"
        )
        assert set(recorded_table.export_decisions) == {stage_key(flags) for flags in STAGE_TUPLES}
        assert len(recorded_table.trees) == len(TOOL_AXIS) * len(PLACEMENT_AXIS) * len(SPEC_AXIS) * len(TEST_AXIS)

    def test_every_recorded_axis_point_is_reproduced(self, axis_table, recorded_table: _Recording):
        """The property's coverage guarantee, since Hypothesis gives a sample.

        The drawn examples below are a sample of a finite space, so completeness is
        stated separately and exhaustively here: every one of the thirty-six
        admissible points, and every one of the sixteen stage tuples, reproduces
        what was recorded. The failure names the point, which is what a wholesale
        comparison of the whole table could not do.
        """
        differing = [key for key, verdict in axis_table["trees"].items() if verdict != recorded_table.trees.get(key)]
        assert not differing, (
            f"{len(differing)} axis point(s) no longer reproduce F: {differing}\n"
            f"first difference recorded: {recorded_table.trees.get(differing[0])}\n"
            f"first difference observed: {axis_table['trees'][differing[0]]}"
        )
        assert axis_table["export_decisions"] == recorded_table.export_decisions

    # Feature: export-gate-and-preview-fidelity, Property 75: for any tree or
    # session satisfying none of isBugCondition_1, isBugCondition_2,
    # isBugCondition_3, F' produces the same stage verdicts, findings, condition
    # statuses, export decision and packaged contents as F, up to the byproducts
    # 2.15 removes and the one stage message recorded in task 2.1.
    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        point=st.tuples(
            st.sampled_from(TOOL_AXIS),
            st.sampled_from(PLACEMENT_AXIS),
            st.sampled_from(SPEC_AXIS),
            st.sampled_from(TEST_AXIS),
        ),
        flags=st.tuples(*[st.booleans()] * len(StageName.ORDER)),
    )
    def test_the_verdicts_are_the_ones_recorded_from_f(
        self,
        observatory: _Observatory,
        recorded_table: _Recording,
        point: AxisPoint,
        flags: Tuple[bool, ...],
    ):
        """The property itself, in three parts.

        First the scope: the drawn tree satisfies none of the three bug
        conditions, checked against evidence rather than asserted. Then the
        findings, the condition statuses, the completeness findings and the
        packaged member set equal what was recorded from F. Then the export
        decision for the drawn four-stage verdict tuple equals what was recorded,
        which is the gate's conjunction and nothing else (design Property 17).
        """
        evidence = observatory.bug_condition_evidence(point)
        assert not evidence["dockerignore_excludes_unit_tests"], (
            f"{point_key(point)} excludes unit_test/ from its build context, so with passing host tests it "
            "would satisfy isBugCondition_1 and fall outside Property 75's scope"
        )
        if evidence["flake8_findings_all_generated"] is not None:
            assert not evidence["flake8_findings_all_generated"], (
                f"{point_key(point)} has findings and every one of them is in a generated file, which is "
                f"isBugCondition_2: {evidence['flake8_rows']}"
            )
        assert not evidence["implementation_delegated"], "a tree cannot have delegated an implementation turn"

        observed = observatory.verdict(point)
        expected = recorded_table.trees[point_key(point)]
        assert observed == expected, (
            f"the verdicts for axis point {point_key(point)} differ from the recording of F.\n"
            f"recorded: {expected}\nobserved: {observed}\n"
            "Preservation says F' reports what F reported for a tree satisfying none of the three bug "
            "conditions. A difference here is either a regression or an intended change that belongs in the "
            "spec's recorded exception list."
        )

        decision = decide_export(_valid_spec_report(), _pipeline(observatory.tree(point[1], point[3]), flags))
        recorded_decision = recorded_table.export_decisions[stage_key(flags)]
        assert {
            "permitted": decision.permitted,
            "spec_valid": decision.spec_valid,
            "code_passed": decision.code_passed,
            "failed_stage_names": [stage.name for stage in decision.failed_stages],
            "block_reason_count": len(decision.reasons),
        } == recorded_decision, (
            f"the export decision for stage tuple {stage_key(flags)} differs from the recording of F: "
            f"recorded {recorded_decision}"
        )

    def test_the_packaged_member_set_carries_no_reference_material(self, axis_table):
        """3.2 and 3.10, over every generated tree rather than one.

        Cheap to state and worth stating separately: whatever else moves, the
        member set never gains ``.builder/`` or a reference document.
        """
        for key, verdict in axis_table["trees"].items():
            leaked = [
                member
                for member in verdict["packaged_plugin_files"]
                if ".builder" in PurePosixPath(member).parts
                or (
                    PurePosixPath(member).suffix.lower() in (".yaml", ".yml", ".json", ".pdf")
                    and PurePosixPath(member).name != "plugin.spec.yaml"
                )
            ]
            assert not leaked, f"{key} would package tool-only or reference material: {leaked}"

    def test_a_missing_linter_never_reads_as_a_clean_lint(self, axis_table):
        """3.6 over the tool axis: ``unverified`` is not ``met`` at any point.

        Stated over the whole table rather than one tree, because change 5 rewires
        the lint path for all of them at once (Req 26.4, 27.5).
        """
        for point in ADMISSIBLE_POINTS:
            if point[0] != "prospector_absent":
                continue
            verdict = axis_table["trees"][point_key(point)]
            assert verdict["conditions"]["lint_clean"] == "unverified", (
                f"{point_key(point)} ran without prospector and reported lint_clean as "
                f"{verdict['conditions']['lint_clean']}; a check that could not run must be unverified"
            )
            assert any("prospector" in note for note in verdict["quality_skipped"])

    def test_the_unit_tests_are_still_compiled_formatted_and_run(self, axis_table):
        """3.7 -- the lint exclusion is lint-only, and stays that way.

        ``unit_test/`` is :func:`is_lint_excluded` and **not** :func:`is_generated`,
        so it is in the compile and format file set and its failures are findings.
        A generator that treated "excluded" as two-valued would have missed this.
        """
        for point in ADMISSIBLE_POINTS:
            verdict = axis_table["trees"][point_key(point)]
            checked = verdict["quality_checked_files"]
            if point[3] == "no_tests":
                assert not any(name.startswith(f"{UNIT_TEST_DIR}/") for name in checked)
                assert any(
                    row["code"] == "no-tests" for row in verdict["quality_finding_rows"]
                ), f"{point_key(point)} has no unit_test/ and no no-tests finding: {verdict['quality_finding_rows']}"
                continue
            assert any(name.startswith(f"{UNIT_TEST_DIR}/") for name in checked), (
                f"{point_key(point)} carries unit tests that were never compiled or format-checked: {checked}. "
                "unit_test/ is excluded from the linter only (bugfix.md 3.7)"
            )
            if point[3] == "failing_tests":
                assert any(
                    "test_suspend_user" in row["key"] for row in verdict["quality_finding_rows"]
                ), f"{point_key(point)} has a failing suite that no finding names"


class TestTheExcludedTreesReallyAreBugConditionTrees:
    """Why the axes withhold what they withhold, demonstrated rather than claimed.

    Property 75 is scoped to trees satisfying none of the three conditions, and
    task 1 measured that essentially every real tree satisfies C₁ and C₂. So the
    two exclusions above carry the weight of the property's honesty, and each is
    shown to be an exclusion of a genuine bug-condition tree rather than a
    convenient narrowing.
    """

    def test_a_generated_only_defect_is_a_bug_condition_two_tree(self, tmp_path):
        """``findings ≠ ∅ ∧ every finding generated`` -- exactly ``isBugCondition_2``.

        Measured with ``flake8`` because that is what the ``lint`` stage runs
        (task 1.2). This is the placement the axis withholds, and this is why.
        """
        if FLAKE8 is None:
            pytest.skip("flake8 is not resolvable, so isBugCondition_2 cannot be measured as the stage measures it")
        root = _write_tree(
            tmp_path / "generated-only",
            dockerignore=DOCKERIGNORE_KEEPING_THE_TESTS,
            **_tree_sources(EXCLUDED_PLACEMENT),
        )
        rows = _flake8_rows(root)
        assert rows, "the withheld placement produced no findings at all, so it is not a C2 tree either"
        assert all(is_generated(row["path"]) for row in rows), (
            "the withheld placement produced a hand-written finding, so it would have been admissible after "
            f"all: {rows}"
        )

    def test_the_quality_gate_cannot_see_that_defect_at_all(self, tmp_path):
        """The asymmetry that makes the table's two defect placements agree.

        The ``Quality_Gate`` filters generated paths out
        (:func:`is_lint_excluded`), so the defect ``flake8`` reports above is
        invisible to it -- which is why ``generated_and_hand_written`` records the
        same verdict as ``hand_written``. Recorded as an observation of F: change 5
        makes the ``lint`` stage agree with the gate, and this is the shape it must
        agree with.
        """
        root = _write_tree(
            tmp_path / "generated-only-gate",
            dockerignore=DOCKERIGNORE_KEEPING_THE_TESTS,
            **_tree_sources(EXCLUDED_PLACEMENT),
        )
        quality = _run_gate(root, run_tests=False)
        if any(note.startswith("prospector (") for note in quality.skipped):
            pytest.skip(f"prospector did not run ({quality.skipped}); nothing to observe here")
        assert quality.clean, (
            "the quality gate reported a finding against a tree whose only defect is in a generated file: "
            f"{quality.summary()}"
        )

    def test_a_generated_dockerignore_with_passing_tests_is_a_bug_condition_one_tree(self, tmp_path):
        """Two of C₁'s three conjuncts, on the host, for the tree the axes avoid.

        The third -- ``¬imageHasPytest`` -- is a property of the
        ``rapid7/insightconnect-python-3-slim-plugin`` runtime image, measured
        against the real tree by task 1.1 rather than re-measured here with a
        Docker build. What this test shows is that a tree with the **generated**
        ``.dockerignore`` and a passing suite differs from the admissible trees in
        exactly the conjunct the axes falsify.
        """
        root = _write_tree(tmp_path / "generated-dockerignore", unit_test=PASSING_UNIT_TEST)
        assert _dockerignore_excludes_tests(root), "the generated .dockerignore no longer excludes unit_test/"
        quality = _run_gate(root, run_tests=True)
        if any(note.startswith("tests (") for note in quality.skipped):
            pytest.skip(f"the plugin's tests could not be run on this host: {quality.skipped}")
        assert not quality.by_source(SOURCE_TESTS), (
            "the passing-test fixture did not pass, so this tree does not satisfy isBugCondition_1's first "
            f"conjunct: {quality.summary()}"
        )
        assert not _dockerignore_excludes_tests(
            _write_tree(
                tmp_path / "admissible", unit_test=PASSING_UNIT_TEST, dockerignore=DOCKERIGNORE_KEEPING_THE_TESTS
            )
        ), "the admissible tree excludes unit_test/ after all, so the axes do not falsify C1's second conjunct"
