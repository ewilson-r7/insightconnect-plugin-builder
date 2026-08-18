"""Preservation baselines for the gate and reporting layer (spec task 2.1).

**These tests must PASS on unfixed code.** That is the whole point of them: the
preservation property is stated as **F′ reproducing F** (`bugfix.md`
"Preservation, for all three"; design Property 75), so what F does has to be
observed and written down *before* anything changes. Nothing here fixes anything,
and no production file is edited by this task.

Six of task 2.1's nine axes live in this module because they are integrations-layer
concerns -- the ``Code_Validator``, the ``Quality_Gate``, the
``Definition_Of_Done``, the export gate's conjunction, and the packager:

1. **Genuinely failing test** -- a tree whose ``test_suspend_user`` fails.
2. **Genuine hand-written defect** -- ``util/api.py`` using ``requests`` without
   importing it.
3. **Genuinely incomplete on-disk spec** -- missing ``sdk``, missing output
   examples.
4. **Missing tool** -- prospector absent: skipped, ``lint_clean`` *unverified*.
5. **Advisory boundary** -- four stages cleared with outstanding
   definition-of-done conditions: ``permitted: true``, conditions presented.
6. **Packaged contents** -- ``.builder/`` and every reference document absent,
   every plugin file present.

The remaining three -- forced export, the repair loop, and delegation isolation --
are orchestrator-layer and live in ``tests/orchestrator/test_preservation_baseline.py``.

**The comparison is against a file, not a literal.** Each axis observes F, then
hands the verdicts to :func:`tests.preservation_baseline.pin`, which asserts them
against ``tests/fixtures/preservation_baseline/<axis>.json``. That is what makes
tasks 5.5, 7.5, 9.6 and 2.2 comparisons against recorded fact rather than against
an expectation someone wrote after the change.

**Verdicts, not messages.** Stage statuses, finding keys, condition statuses, the
export decision and the packaged member set are compared. Message text is not --
one message differs by design (`bugfix.md`'s recorded exception: for a tree with
failing tests on a host with no Docker, F blames Docker and F′ blames the pytest
failures). Figures the fix is *meant* to change go into the fixture's ``measured``
section, which is recorded and reported but never asserted.

**Three corrections to task 2.1's own axis text, found by measuring.** They are
asserted here as *observations*, not as the axis text hoped:

* **Axis 1's "the failure names the test" is not true of F.** The pipeline's
  ``test`` stage is ``docker run --rm <image> python -m pytest -q``, so its verdict
  is independent of the plugin's tests. It is the ``Quality_Gate`` -- not the
  stage -- that names ``test_suspend_user`` today.
* **Axis 6's baseline is 37 plugin files, not 39.** Task 1.9 measured
  ``list_plugin_files`` returning 39 = 37 plugin files + ``.coverage`` +
  ``unit_test/.coverage``, so `bugfix.md`'s "39 entries" already counted two
  byproducts and 3.2's "39-entry baseline less the byproducts" is 37. Both figures
  are recorded.
* **Axis 5's two outstanding conditions are a measurement, not an invariant.**
  Task 1.6 measured the ``iterate_custom`` control at exactly ``formatted`` and
  ``api_client``; both close when tasks 5 and 7 land. The invariant this axis
  preserves is the *advisory* status of the definition of done, which is why the
  compared payload is the gate's independence from it rather than a count.

**Environment.** ``insight-plugin`` and ``prospector`` live in
``~/Library/Python/3.9/bin`` and ``docker`` in
``/Applications/Docker.app/Contents/Resources/bin``, neither on a non-login shell
``PATH``, so :func:`tests.preservation_baseline.toolchain_path` prepends both. A
tool or tree that is absent is a **skip**, never a finding, and the fixture's
provenance names it, so a baseline taken on an incomplete host cannot be mistaken
for a complete one. Nothing here writes into ``~/.icplugin-builder/projects/``:
the one axis that needs a real tree works on a ``shutil.copytree`` copy.

_Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12_
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from icplugin_builder.core.spec_completeness import CompletenessReport, check_completeness
from icplugin_builder.core.spec_model import Component, FieldSchema, PluginSpec, SemVer
from icplugin_builder.core.spec_validator import ValidationReport
from icplugin_builder.integrations.build_engine import (
    BUILDER_METADATA_DIR,
    BuildEngine,
    list_plugin_files,
)
from icplugin_builder.integrations.build_prep import (
    FALLBACK_LINT_PROFILE,
    LINT_PROFILE_SOURCE_FALLBACK,
    LINT_PROFILE_SOURCE_REPOSITORY,
    LintProfile,
)
from icplugin_builder.integrations.code_validator import (
    CodeValidator,
    PipelineReport,
    StageName,
    StageResult,
    StageStatus,
)
from icplugin_builder.integrations.definition_of_done import (
    CONDITION_API_CLIENT,
    CONDITION_FORMATTED,
    CONDITION_LINT_CLEAN,
    CONDITION_ORDER,
    ConditionStatus,
    DoneReport,
    evaluate_done,
)
from icplugin_builder.integrations.export_gate import decide_export
from icplugin_builder.integrations.quality_gate import (
    SOURCE_PROSPECTOR,
    SOURCE_TESTS,
    QualityGate,
    QualityReport,
)

from tests.preservation_baseline import environment, pin, toolchain_path, tree

# ---------------------------------------------------------------------------
# Pinned inputs
# ---------------------------------------------------------------------------
#
# Task 1.4's finding, carried rather than re-derived: a content-dependent lint
# assertion that resolves its profile at runtime varies with the developer's home
# directory, and a *missing* prospector reads as an empty finding set. So the
# profile is pinned to the vendored fallback -- which task 1.4 measured as
# byte-identical to the repository's copy on this host -- and the absence of the
# tool is handled as its own axis (4) rather than as noise in the others.

#: Why the profile was pinned. It rides on the :class:`LintProfile`, and the gate
#: surfaces it as a skip note for a non-authoritative profile, so it is fixed text
#: rather than something that varies per run.
PINNED_PROFILE_DETAIL = "pinned by the preservation baseline (spec task 2.1)"

#: The plugin package name used by every synthesised tree here.
PACKAGE = "icon_example"

#: The unit-test directory name, matching what the generated ``.dockerignore``
#: excludes.
UNIT_TEST_DIR = "unit_test"

#: The tree the reproduction run produced. Axis 6 is about this plugin and no
#: other; `bugfix.md`'s member figures are measurements of it.
RECORDED_TREE = "jumpcloud"

#: What `bugfix.md` 3.2 calls "the 39-entry baseline", and what task 1.9 found it
#: actually decomposes into. Recorded as constants so the correction is legible in
#: one place: the document's 39 already counted two byproducts, so "the 39-entry
#: baseline less the byproducts named in 2.15" is 37.
BUGFIX_MEMBER_COUNT = 39
BYPRODUCT_MEMBERS_IN_THE_BASELINE: Tuple[str, ...] = (".coverage", f"{UNIT_TEST_DIR}/.coverage")
PLUGIN_FILE_COUNT = BUGFIX_MEMBER_COUNT - len(BYPRODUCT_MEMBERS_IN_THE_BASELINE)


# ---------------------------------------------------------------------------
# Fixture plugin sources
# ---------------------------------------------------------------------------
#
# Each is ``black --line-length 120`` clean, so the only findings a tree produces
# are the ones its axis is about. A stray ``would-reformat`` would put a second
# finding in the compared payload and make the axis about formatting instead.

#: A sound API client: a central ``_make_request``, an ``HTTP_ERROR_MAP``, and one
#: domain method per action.
#:
#: The exploded ``HTTP_ERROR_MAP`` carries a magic trailing comma so ``black`` at
#: 120 columns keeps it multi-line while every line stays inside ``flake8``'s
#: default 79. That is not cosmetic: without it the two bars disagree, and a
#: ``pycodestyle`` ``E501`` would fail the ``lint`` stage today and pass it after
#: change 5 -- putting a verdict the fix is *meant* to change into a preservation
#: baseline. Task 1.2 measured exactly that collision.
SOUND_CLIENT = '''"""A hand-written API client of the shape the rulebook prescribes."""

HTTP_ERROR_MAP = {
    401: "The API key is invalid.",
    404: "The resource was not found.",
}


class ExampleApi:
    """Centralises every request the plugin makes."""

    def __init__(self, api_key):
        self.api_key = api_key

    def suspend_user(self, user_id):
        """One domain method per action."""
        return self._make_request("PUT", "systemusers/" + user_id)

    def get_status(self):
        return self._make_request("GET", "status")

    def _make_request(self, method, path):
        return {"method": method, "path": path}
'''

#: The genuine hand-written defect axis 2 is about: ``requests`` is *used* and
#: never imported. An import-based check passes this file; the plugin then dies
#: with a ``NameError`` on its first call, which is why the checks read usage.
DEFECTIVE_CLIENT = '''"""A hand-written client that uses requests without importing it."""

HTTP_ERROR_MAP = {
    401: "The API key is invalid.",
}


class ExampleApi:
    """A real defect, in a hand-written file, and the author's to fix."""

    def __init__(self, api_key):
        self.api_key = api_key

    def get_status(self):
        return self._make_request("GET", "status")

    def _make_request(self, method, path):
        response = requests.request(method, "https://example.test/" + path)
        return response.json()
'''

#: A real connection: state in ``connect()``, a genuine ``test()``.
SOUND_CONNECTION = '''"""A connection whose connect() holds state and whose test() really tests."""

from ..util.api import ExampleApi


class Connection:
    """Not a stub: no TODO, no bare pass."""

    def __init__(self):
        self.client = None

    def connect(self, params):
        self.client = ExampleApi(params.get("api_key"))

    def test(self):
        self.client.get_status()
        return {"success": True}
'''

#: The failing unit test axis 1 is about. It fails for a real reason -- the client
#: returns a different shape -- rather than by raising outright, so the failure is
#: a genuine assertion failure of the kind a plugin author would see.
FAILING_UNIT_TEST = '''"""A unit test that genuinely fails, named so the failure is identifiable."""

import unittest

from icon_example.util.api import ExampleApi


class TestSuspendUser(unittest.TestCase):
    """The plugin's own test of suspend_user, which does not pass."""

    def test_suspend_user(self):
        client = ExampleApi("api-key")
        self.assertEqual(client.suspend_user("u1"), {"suspended": True})
'''

#: A dependency manifest with an exact pin, so that condition is not incidentally
#: unmet in the trees below.
PINNED_MANIFEST = "requests==2.31.0\n"


#: What the generated ``.dockerignore`` carries, and the second conjunct of
#: ``isBugCondition_1``: the tests are excluded from the build context, so the
#: in-image test command has nothing to run.
GENERATED_DOCKERIGNORE = f"{UNIT_TEST_DIR}/**/*\n"


def _write_tree(
    root: Path,
    *,
    client: str = SOUND_CLIENT,
    connection: str = SOUND_CONNECTION,
    unit_test: Optional[str] = None,
    package_init: str = "",
    dockerignore: str = GENERATED_DOCKERIGNORE,
) -> Path:
    """Build a minimal plugin working tree at ``root``.

    Deliberately minimal: only the files the axes read. A fuller scaffold would
    bring generated ``schema.py`` and ``setup.py`` with it, which are exactly the
    files Bug 2 is about, and their findings would drown the axis under
    measurement.

    Args:
        root: where to build the tree.
        client: the contents of ``<package>/util/api.py``.
        connection: the contents of ``<package>/connection/connection.py``.
        unit_test: the contents of ``unit_test/test_suspend_user.py``, or ``None``
            to leave the tree without a ``unit_test/`` directory at all.
        package_init: the contents of the generated ``<package>/__init__.py``.
            Empty by default; task 2.2 passes the unused re-export
            `bugfix.md` 1.4 found in the real tree, so a defect can be placed in a
            *generated* file without disturbing any hand-written one.
        dockerignore: the contents of ``.dockerignore``. Defaults to
            :data:`GENERATED_DOCKERIGNORE`. Task 2.2 overrides it because a tree
            that both excludes ``unit_test/`` and has passing host tests satisfies
            ``isBugCondition_1``, which Property 75 is explicitly scoped away from.
    """
    package = root / PACKAGE
    (package / "util").mkdir(parents=True)
    (package / "connection").mkdir(parents=True)
    (package / "__init__.py").write_text(package_init, encoding="utf-8")
    (package / "util" / "__init__.py").write_text("", encoding="utf-8")
    (package / "connection" / "__init__.py").write_text("", encoding="utf-8")
    (package / "util" / "api.py").write_text(client, encoding="utf-8")
    (package / "connection" / "connection.py").write_text(connection, encoding="utf-8")
    (root / "requirements.txt").write_text(PINNED_MANIFEST, encoding="utf-8")
    (root / ".dockerignore").write_text(dockerignore, encoding="utf-8")
    if unit_test is not None:
        tests = root / UNIT_TEST_DIR
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / "test_suspend_user.py").write_text(unit_test, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Running F
# ---------------------------------------------------------------------------


def _pinned_profile() -> LintProfile:
    """The prospector profile every lint measurement here is judged against.

    Declared :data:`LINT_PROFILE_SOURCE_REPOSITORY` and pointed at the vendored
    copy, which is what design change 5 step 4 prescribes ("an explicit
    ``LintProfile`` pointing at a fixture copy") and what task 1.4 justifies: it
    measured the repository profile and the vendored fallback as **byte-identical**
    on this host, so the content is the repository's bar and the declaration is
    accurate about content rather than about provenance.

    The source matters for a reason found while recording this baseline, not
    guessed: a profile declared ``fallback`` makes the gate append the skip note
    ``"prospector profile (...)"``, and ``definition_of_done._was_skipped`` matches
    any note beginning with the check's name -- so a *non-authoritative profile*
    renders ``lint_clean`` **unverified** even though prospector ran and reported
    nothing. That is recorded on its own as
    :class:`TestTheProfileSourceChangesTheConditionStatus`; here it is avoided, so
    that axis 4's ``unverified`` means "the linter was absent" and nothing else.
    """
    return LintProfile(
        path=str(FALLBACK_LINT_PROFILE),
        source=LINT_PROFILE_SOURCE_REPOSITORY,
        detail=PINNED_PROFILE_DETAIL,
    )


def _fallback_sourced_profile() -> LintProfile:
    """The same profile content, declared non-authoritative.

    Used only by :class:`TestTheProfileSourceChangesTheConditionStatus`, which
    records what F does with it.
    """
    return LintProfile(
        path=str(FALLBACK_LINT_PROFILE),
        source=LINT_PROFILE_SOURCE_FALLBACK,
        detail=PINNED_PROFILE_DETAIL,
    )


@contextlib.contextmanager
def _with_path(path: str) -> Iterator[None]:
    """Run the block with ``PATH`` set to ``path``, then restore it.

    The gate and the pipeline shell out with :func:`asyncio.create_subprocess_exec`,
    which inherits :data:`os.environ`, so ``PATH`` is how "prospector is absent"
    is expressed to them. Module-scoped fixtures cannot use ``monkeypatch``, hence
    a context manager rather than the usual fixture.
    """
    previous = os.environ.get("PATH")
    os.environ["PATH"] = path
    try:
        yield
    finally:
        if previous is None:  # pragma: no cover - PATH is always set in practice
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous


def _run_gate(
    root: Path,
    *,
    path: Optional[str] = None,
    run_tests: bool = True,
    profile: Optional[LintProfile] = None,
) -> QualityReport:
    """Run the ``Quality_Gate`` over ``root`` under the pinned profile."""
    gate = QualityGate(
        python_executable=sys.executable,
        run_tests=run_tests,
        lint_profile=_pinned_profile() if profile is None else profile,
    )
    with _with_path(toolchain_path() if path is None else path):
        return asyncio.run(gate.run(root))


def _run_pipeline(root: Path) -> PipelineReport:
    """Run the four-stage pipeline over ``root`` exactly as the tool wires it."""
    validator = CodeValidator(validate_python_executable=sys.executable)
    with _with_path(toolchain_path()):
        return asyncio.run(validator.run_pipeline(root))


def _finding_rows(report: QualityReport) -> List[Dict[str, str]]:
    """The findings as compared verdicts: source, path, code, and stable key.

    No message and no raw line number. The key already carries the bucketed line
    (:attr:`CodeFinding.key`), and message text is what the recorded exception is
    about, so including it would make the baseline brittle in exactly the place
    the design says not to compare.
    """
    return sorted(
        (
            {"source": finding.source, "path": finding.path, "code": finding.code, "key": finding.key}
            for finding in report.findings
        ),
        key=lambda row: row["key"],
    )


def _condition_rows(done: DoneReport) -> Dict[str, str]:
    """Every definition-of-done condition and its status, in reporting order."""
    return {name: _status_of(done, name) for name in CONDITION_ORDER}


def _status_of(done: DoneReport, name: str) -> str:
    """One condition's status value, or ``"not_evaluated"`` when it is absent."""
    condition = done.condition(name)
    return "not_evaluated" if condition is None else condition.status.value


def _stage_rows(report: PipelineReport) -> Dict[str, str]:
    """Every stage and its status. Statuses only -- the messages are host-dependent."""
    return {stage.name: stage.status.value for stage in report.stages}


# ---------------------------------------------------------------------------
# Axis 1 -- a genuinely failing test
# ---------------------------------------------------------------------------


class FailingTestObservation:
    """What F reports for a tree whose ``test_suspend_user`` genuinely fails."""

    def __init__(self, root: Path, quality: QualityReport, done: DoneReport, pipeline: PipelineReport) -> None:
        self.root = root
        self.quality = quality
        self.done = done
        self.pipeline = pipeline

    @property
    def test_findings(self) -> Tuple[Any, ...]:
        """The findings the test run produced."""
        return self.quality.by_source(SOURCE_TESTS)

    @property
    def stage(self) -> Optional[StageResult]:
        """The pipeline's ``test`` stage result."""
        return self.pipeline.stage(StageName.TEST)

    @property
    def stage_text(self) -> str:
        """Everything the ``test`` stage recorded, for the naming measurement."""
        stage = self.stage
        if stage is None:  # pragma: no cover - the pipeline always records four
            return ""
        return f"{stage.message}\n{stage.stdout}\n{stage.stderr}"


@pytest.fixture(scope="module")
def failing_test_tree(tmp_path_factory) -> FailingTestObservation:
    """One tree with a genuinely failing test, observed once through F."""
    root = _write_tree(tmp_path_factory.mktemp("axis1") / "tree", unit_test=FAILING_UNIT_TEST)
    quality = _run_gate(root)
    done = evaluate_done(root, quality_report=quality)
    pipeline = _run_pipeline(root)
    return FailingTestObservation(root, quality, done, pipeline)


class TestAxisOneAGenuinelyFailingTestIsStillReported:
    """Axis 1 (`bugfix.md` 3.1, 3.5) -- a real test failure keeps failing the gate.

    Passes on unfixed code, and must keep passing: change 7 moves the ``test``
    stage onto the host, which is precisely the change that could turn a failing
    suite into a passing stage if it were done carelessly.
    """

    def test_the_premise_holds_the_suite_really_fails(self, failing_test_tree: FailingTestObservation):
        """The witness. Without this the rest of the axis measures nothing."""
        findings = failing_test_tree.test_findings
        if not findings and any(note.startswith("tests") for note in failing_test_tree.quality.skipped):
            pytest.skip(
                "the plugin's tests could not be run under "
                f"{sys.executable}: {failing_test_tree.quality.skipped}; the failing-test axis cannot be "
                "measured on this host"
            )
        assert findings, (
            "no test finding was produced for a tree whose test_suspend_user fails, so this tree is not an "
            f"instance of the axis: {failing_test_tree.quality.summary()}"
        )

    def test_the_recorded_verdicts_are_unchanged(self, failing_test_tree: FailingTestObservation):
        """Pin the whole axis: finding keys, condition statuses, stage statuses.

        The ``test`` stage's *status* is compared and its *message* is not, which
        is the recorded exception in force: on a host with no Docker F blames the
        engine and F′ blames the pytest failures, and both are ``failed``.
        """
        observed = {
            "quality_finding_rows": _finding_rows(failing_test_tree.quality),
            "test_source_finding_count": len(failing_test_tree.test_findings),
            "conditions": _condition_rows(failing_test_tree.done),
            "stage_statuses": _stage_rows(failing_test_tree.pipeline),
            "export_permitted": decide_export(_valid_spec_report(), failing_test_tree.pipeline).permitted,
        }
        baseline = pin(
            "axis_1_failing_test",
            observed,
            description=(
                "A tree whose test_suspend_user genuinely fails. F fails the test stage and reports the "
                "failure as a Quality_Gate finding whose key names the test; unit_tests_pass is unmet and "
                "export is not permitted. Stage messages are excluded from the comparison because the "
                "recorded exception is about exactly that text."
            ),
            requirements=("3.1", "3.5", "3.7"),
            measured={
                "test_stage_returncode": (
                    None if failing_test_tree.stage is None else failing_test_tree.stage.returncode
                ),
                "test_stage_message": ("" if failing_test_tree.stage is None else failing_test_tree.stage.message),
                "docker_available": failing_test_tree.pipeline.docker_available,
                "note": (
                    "returncode and message are recorded, not asserted: change 7 replaces the in-image "
                    "docker run with a host run, so both are expected to change while the verdict does not"
                ),
            },
        )
        assert baseline.observed is not None

    def test_the_quality_gate_names_the_failing_test(self, failing_test_tree: FailingTestObservation):
        """The naming half of the axis, located where it actually happens.

        Axis 1's text says "the failure names the test". It does -- in the
        ``Quality_Gate``'s finding key, not in the pipeline stage. Asserted here so
        the fix cannot lose the identification while moving the stage.
        """
        keys = [finding.key for finding in failing_test_tree.test_findings]
        if not keys:
            pytest.skip("no test findings on this host; see the premise test")
        assert any(
            "test_suspend_user" in key for key in keys
        ), f"no test finding names test_suspend_user, so the failure is not identifiable from the keys: {keys}"

    def test_the_pipeline_stage_does_not_name_the_test_on_unfixed_code(self, failing_test_tree: FailingTestObservation):
        """A **correction to axis 2.1's text**, recorded rather than asserted as hope.

        The task text says "stage fails, and the failure names the test". The
        second half is not true of F: the stage is
        ``docker run --rm <image> python -m pytest -q``, so its output is about an
        image, never about ``test_suspend_user``. Task 1.1 measured the same thing
        against the real tree, where a built image's ``ENTRYPOINT`` swallows the
        arguments and argparse exits 2 for a passing suite and a failing one
        alike.

        This is recorded as an observation of F. It is the one axis-1 expectation
        the fix is *meant* to change, and change 7 is where it changes.
        """
        stage = failing_test_tree.stage
        assert stage is not None, "the pipeline recorded no test stage"
        assert not stage.passed, (
            "the test stage passed for a tree whose tests fail, which would break the axis before the fix "
            f"even starts: {stage.status.value} rc={stage.returncode}"
        )
        names_the_test = "test_suspend_user" in failing_test_tree.stage_text
        baseline = pin(
            "axis_1_stage_naming",
            {"stage_status": stage.status.value, "stage_names_the_failing_test": names_the_test},
            description=(
                "The correction to task 2.1's axis-1 text. F's test stage fails for a tree with a failing "
                "test, but its recorded output never names the test, because the stage runs pytest inside "
                "the built plugin image rather than against the plugin's own suite. Recorded so the fix's "
                "verification compares against what F did, not against what the axis text hoped."
            ),
            requirements=("3.1",),
            measured={
                "stage_command_is_docker_run": True,
                "note": (
                    "change 7 is expected to flip stage_names_the_failing_test to true; that is the "
                    "improvement, and it is why the preservation property is stated over verdicts"
                ),
            },
        )
        assert baseline.observed["stage_status"] == stage.status.value


# ---------------------------------------------------------------------------
# Axis 2 -- a genuine hand-written defect
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def hand_written_defect(tmp_path_factory) -> Tuple[QualityReport, DoneReport]:
    """A tree whose ``util/api.py`` uses ``requests`` without importing it."""
    root = _write_tree(tmp_path_factory.mktemp("axis2") / "tree", client=DEFECTIVE_CLIENT)
    quality = _run_gate(root, run_tests=False)
    return quality, evaluate_done(root, quality_report=quality)


class TestAxisTwoAGenuineHandWrittenDefectIsStillReported:
    """Axis 2 (`bugfix.md` 3.3, 2.9) -- the exclusion must not lower the bar.

    This is the axis that keeps change 5 honest. Excluding generated files is
    correct; excluding *findings* would be the defect task 37's standard forbids,
    so the ``undefined-variable`` in a hand-written client is pinned with its
    path, its code and its key.
    """

    def test_the_premise_holds_prospector_ran(self, hand_written_defect):
        """The witness, and the distinction task 1.4 found this suite could not make."""
        quality, _ = hand_written_defect
        if any(note.startswith("prospector (") for note in quality.skipped):
            pytest.skip(
                f"prospector did not run ({quality.skipped}); an empty finding set from a linter that never "
                "ran is not evidence of anything, which is what axis 4 is about"
            )
        assert quality.by_source(SOURCE_PROSPECTOR), (
            "prospector ran and reported nothing against a file that uses `requests` without importing it: "
            f"{quality.summary()}"
        )

    def test_the_recorded_verdicts_are_unchanged(self, hand_written_defect):
        """Pin the finding's identity and the conditions it drives."""
        quality, done = hand_written_defect
        observed = {
            "finding_rows": _finding_rows(quality),
            "checked_files": list(quality.checked_files),
            "skipped": list(quality.skipped),
            "conditions": _condition_rows(done),
            "lint_clean_status": _status_of(done, CONDITION_LINT_CLEAN),
        }
        pin(
            "axis_2_hand_written_defect",
            observed,
            description=(
                "A hand-written util/api.py that uses `requests` and never imports it. F reports one "
                "prospector undefined-variable finding, located in the hand-written file, and lint_clean "
                "is unmet. The exclusion of generated files must not remove this finding."
            ),
            requirements=("3.1", "3.3", "3.7"),
            measured={
                "note": (
                    "the profile is pinned to the vendored fallback, so this finding set does not vary with "
                    "the developer's home directory (task 1.4's correction to bugfix.md 1.6)"
                )
            },
        )

    def test_the_defect_is_located_in_the_hand_written_client(self, hand_written_defect):
        """The located half: a finding the author cannot act on is not a finding."""
        quality, _ = hand_written_defect
        prospector = quality.by_source(SOURCE_PROSPECTOR)
        if not prospector:
            pytest.skip("prospector did not run; see the premise test")
        paths = {finding.path for finding in prospector}
        assert (
            f"{PACKAGE}/util/api.py" in paths
        ), f"the defect was reported somewhere other than the hand-written client: {sorted(paths)}"


# ---------------------------------------------------------------------------
# Axis 3 -- a genuinely incomplete on-disk spec
# ---------------------------------------------------------------------------


def _incomplete_spec() -> PluginSpec:
    """A spec missing ``sdk`` and missing an ``example`` on its one output.

    Both shortfalls are genuine and both are what `bugfix.md` names: the ``sdk``
    block is what ``insight-plugin validate`` needs, and an output without an
    example is the convention shortfall Requirement 30 covers. Change 2 makes the
    preview read this from **disk** instead of from the draft; what it must not do
    is stop reporting it.
    """
    return PluginSpec(
        name="example",
        title="Example",
        description="A plugin whose spec is genuinely incomplete.",
        version=SemVer(1, 0, 0),
        vendor="rapid7",
        actions={
            "suspend_user": Component(
                title="Suspend User",
                description="Suspends a user.",
                input={"user_id": FieldSchema(type="string", required=True, title="User ID", example="u1")},
                output={"suspended": FieldSchema(type="boolean", required=True, title="Suspended")},
            )
        },
    )


def _valid_spec_report() -> ValidationReport:
    """A clean ``Spec_Validator`` report, so the gate turns only on the stages.

    Axis 5 is about the conjunction and about the definition of done being
    advisory. Feeding a clean spec report isolates the code half of the
    conjunction, which is the half the two gate defects touch.
    """
    return ValidationReport(errors=[])


class TestAxisThreeAGenuinelyIncompleteSpecIsStillReported:
    """Axis 3 (`bugfix.md` 3.1, 2.12) -- the same findings, later read from disk.

    Change 2 makes ``prepare_export`` derive completeness from the on-disk spec.
    The findings for a spec that is *genuinely* incomplete must be identical
    before and after; only which artifact is read changes.
    """

    def test_the_recorded_findings_are_unchanged(self):
        report: CompletenessReport = check_completeness(_incomplete_spec())
        observed = {
            "keys": list(report.keys()),
            "rows": sorted(
                (
                    {"code": finding.code, "path": finding.path, "severity": finding.severity.value}
                    for finding in report.findings
                ),
                key=lambda row: (row["path"], row["code"]),
            ),
            "error_count": len(report.errors),
            "warning_count": len(report.warnings),
            "is_complete": report.is_complete,
        }
        pin(
            "axis_3_incomplete_spec",
            observed,
            description=(
                "A spec missing the sdk block and missing an example on its one output. F reports these "
                "completeness findings; after change 2 the preview reads the same spec from disk and must "
                "report the same set. Messages are excluded; the stable keys are the identity."
            ),
            requirements=("3.1",),
        )

    def test_the_absent_sdk_block_is_among_them(self):
        """A witness against the fixture rotting into a set of unrelated findings."""
        report = check_completeness(_incomplete_spec())
        assert any(
            "sdk" in finding.path for finding in report.findings
        ), f"no finding refers to the absent sdk block: {report.keys()}"


# ---------------------------------------------------------------------------
# Axis 4 -- a missing tool is skipped, never clean
# ---------------------------------------------------------------------------


def _bin_without_prospector(directory: Path) -> str:
    """A ``PATH`` holding the toolchain's other binaries but not ``prospector``.

    Task 1.4's finding, carried: two prospector installations exist on this host,
    a ``--user`` install in ``~/Library/Python/3.9/bin`` and a HOME-independent
    ``~/.pyenv/shims`` copy, so removing one directory leaves the tool resolvable.
    A single directory of symlinks built here is unambiguous instead: whatever the
    host's layout, ``prospector`` is not on this ``PATH``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for tool in ("black", "python3", "sh", "env"):
        resolved = shutil.which(tool, path=toolchain_path())
        if resolved is not None:
            link = directory / tool
            if not link.exists():
                link.symlink_to(resolved)
    return str(directory)


@pytest.fixture(scope="module")
def prospector_absent(tmp_path_factory) -> Tuple[QualityReport, DoneReport]:
    """The sound tree checked with ``prospector`` off ``PATH``."""
    work = tmp_path_factory.mktemp("axis4")
    root = _write_tree(work / "tree")
    quality = _run_gate(root, path=_bin_without_prospector(work / "bin"), run_tests=False)
    return quality, evaluate_done(root, quality_report=quality)


class TestAxisFourAMissingLinterNeverReadsAsACleanLint:
    """Axis 4 (`bugfix.md` 3.6; parent Req 26.4, 27.5) -- unverified is not met.

    Task 1.4 dissected this: with ``prospector`` off ``PATH``,
    ``_check_prospector`` records a skip on :attr:`QualityReport.skipped` and
    returns no findings, so the report renders as the empty string. The condition
    is therefore reported **unverified** -- and this axis exists because change 5
    rewires the lint path, which is where that distinction is easiest to lose.
    """

    def test_the_skip_is_recorded_rather_than_silent(self, prospector_absent):
        quality, _ = prospector_absent
        assert any(
            "prospector" in note for note in quality.skipped
        ), f"prospector was absent and nothing recorded a skip; notes were {quality.skipped}"

    def test_the_clean_report_renders_as_nothing_at_all(self, prospector_absent):
        """Task 1.4's own measurement: no findings, and ``render()`` is empty.

        This is what makes the distinction necessary. Read on its own the report
        is indistinguishable from a clean one, so the ``skipped`` note and the
        ``unverified`` status are the only things carrying the truth.
        """
        quality, _ = prospector_absent
        assert not quality.by_source(
            SOURCE_PROSPECTOR
        ), f"prospector was absent yet findings were attributed to it: {quality.by_source(SOURCE_PROSPECTOR)}"
        assert quality.render() == "", f"the report rendered {quality.render()!r} rather than nothing"

    def test_the_recorded_verdicts_are_unchanged(self, prospector_absent):
        quality, done = prospector_absent
        observed = {
            "prospector_finding_count": len(quality.by_source(SOURCE_PROSPECTOR)),
            "render_is_empty": quality.render() == "",
            "clean": quality.clean,
            "skipped": list(quality.skipped),
            "conditions": _condition_rows(done),
            "lint_clean_status": _status_of(done, CONDITION_LINT_CLEAN),
        }
        pin(
            "axis_4_missing_tool",
            observed,
            description=(
                "The sound tree checked with prospector off PATH. F records a skip, produces no prospector "
                "findings, renders as the empty string, and reports lint_clean unverified -- never met. A "
                "missing linter must not read as a clean lint (Req 26.4, 27.5)."
            ),
            requirements=("3.6",),
            measured={
                "note": (
                    "black is deliberately left resolvable on the sanitised PATH so this axis is about the "
                    "linter alone; if black were absent too the format condition would also read unverified"
                )
            },
        )

    def test_lint_clean_is_unverified_and_not_met(self, prospector_absent):
        """The clause in its own words, so the failure message says what broke."""
        _, done = prospector_absent
        condition = done.condition(CONDITION_LINT_CLEAN)
        assert condition is not None, "lint_clean was not evaluated at all"
        assert condition.status is ConditionStatus.UNVERIFIED, (
            f"prospector was absent and lint_clean read {condition.status.value}; a check that could not "
            "run must be unverified rather than met or unmet"
        )
        assert not condition.met, "an unverified condition counted as met"


class TestTheProfileSourceChangesTheConditionStatus:
    """An observation of F found while recording axis 4, and task 7's decision on it.

    **What F did.** ``_check_prospector`` appended the note
    ``"prospector profile (<detail>)"`` whenever the resolved profile was not the
    repository's own, and ``definition_of_done._was_skipped`` matches any note
    beginning with the check's name. So a run under a **non-authoritative profile**
    reported ``lint_clean`` *unverified* -- indistinguishable from a run where
    prospector was never installed -- even though the linter ran and reported
    nothing.

    **What task 7 decided.** That overloading is removed. Provenance now rides on
    the report itself (``QualityReport.bar``), where it is disclosed for *every*
    profile rather than only a second-best one, so the skip-note channel is left to
    mean what 26.4 needs it to mean: the check did not run. A resolved fallback
    profile is a bar worth naming, not a check that failed to happen, so
    ``lint_clean`` now reads ``met``.

    The distinction that had to survive does: prospector genuinely absent still
    yields a ``prospector (... not available)`` note and an unverified
    ``lint_clean``, which is what axis 4 proper pins.

    Re-pinned rather than deleted, and the class docstring anticipated this --
    "if F′ changes it the change is visible here instead of surfacing as a
    mysterious axis-4 failure".
    """

    def test_a_non_authoritative_profile_no_longer_reads_as_unverified(self, tmp_path):
        root = _write_tree(tmp_path / "tree")
        quality = _run_gate(root, run_tests=False, profile=_fallback_sourced_profile())
        if any(note.startswith("prospector (") for note in quality.skipped):
            pytest.skip(f"prospector did not run ({quality.skipped}); nothing to observe here")
        done = evaluate_done(root, quality_report=quality)
        pin(
            "axis_4_profile_source_note",
            {
                "prospector_finding_count": len(quality.by_source(SOURCE_PROSPECTOR)),
                "skipped": list(quality.skipped),
                "lint_clean_status": _status_of(done, CONDITION_LINT_CLEAN),
            },
            description=(
                "The same clean tree and the same profile content, declared fallback rather than "
                "repository. F appended a 'prospector profile (...)' skip note, which _was_skipped matches "
                "by prefix, so lint_clean read unverified even though prospector ran and found nothing. "
                "Change 5 moved provenance onto QualityReport.bar, where it is disclosed for every profile, "
                "leaving the skip-note channel to mean only that a check did not run -- so lint_clean now "
                "reads met. Re-pinned deliberately; the false unverified was the defect 2.8 and 26.4 name."
            ),
            requirements=("3.6",),
            measured={
                "note": (
                    "axis 4 proper still pins the genuine missing-tool case, so unverified continues to "
                    "mean 'the linter was absent' and nothing else"
                )
            },
        )
        assert _status_of(done, CONDITION_LINT_CLEAN) == "met", (
            "a resolved fallback profile still reads as an unverified lint_clean, so provenance is being "
            f"reported through the skip-note channel again: {quality.skipped}"
        )
        assert not any(
            "profile" in note for note in quality.skipped
        ), f"the profile detail is back in the skip notes: {quality.skipped}"
        assert (
            str(quality.lint_profile.path) in quality.bar()
        ), f"the report no longer names the profile it judged under: {quality.bar()!r}"


def _all_stages_passed(root: Path) -> PipelineReport:
    """A pipeline report in which all four stages passed.

    Constructed rather than run. Clearing all four stages needs a real image
    build and a real ``insight-plugin`` validate against a full scaffold, and this
    axis is not about whether they can pass -- it is about whether the *gate*
    consults anything besides them. Building the report directly is how the
    existing orchestrator suite states the same premise.
    """
    return PipelineReport(
        project_dir=root,
        stages=tuple(
            StageResult(
                name=name,
                status=StageStatus.PASSED,
                returncode=0,
                stdout="",
                stderr="",
                duration_seconds=0.0,
            )
            for name in StageName.ORDER
        ),
        docker_available=True,
    )


@pytest.fixture(scope="module")
def advisory_boundary(tmp_path_factory):
    """Four stages cleared over a tree with outstanding definition-of-done work."""
    root = _write_tree(tmp_path_factory.mktemp("axis5") / "tree")
    pipeline = _all_stages_passed(root)
    # No quality report and no spec: the conditions that depend on them are
    # unverified, which is itself outstanding work (Req 27.5) and is exactly the
    # state 27.7 says must be presented beside a permitted preview.
    done = evaluate_done(root, pipeline_report=pipeline)
    decision = decide_export(_valid_spec_report(), pipeline)
    return decision, done


class TestAxisFiveTheDefinitionOfDoneStaysAdvisory:
    """Axis 5 (`bugfix.md` 3.5; parent Req 27.6, 27.7) -- permitted, with conditions shown.

    The gate is the four-stage conjunction and **only** that (design Property 17).
    Outstanding definition-of-done conditions are presented alongside a permitted
    preview rather than blocking it. Every change in this bugfix touches one of the
    two reports, so the boundary between them is pinned.
    """

    def test_export_is_permitted_despite_outstanding_conditions(self, advisory_boundary):
        decision, done = advisory_boundary
        assert (
            decision.permitted
        ), f"four stages passed and a valid spec was supplied, yet export was blocked: {decision.summary()}"
        assert done.outstanding, (
            "this tree has no outstanding definition-of-done conditions, so it does not exercise the "
            f"advisory boundary at all: {done.summary()}"
        )
        assert not done.complete, "the definition of done reported complete, so nothing was outstanding"

    def test_the_recorded_verdicts_are_unchanged(self, advisory_boundary):
        decision, done = advisory_boundary
        observed = {
            "permitted": decision.permitted,
            "spec_valid": decision.spec_valid,
            "code_passed": decision.code_passed,
            "failed_stage_names": [stage.name for stage in decision.failed_stages],
            "block_reason_count": len(decision.reasons),
            "conditions": _condition_rows(done),
            "outstanding_condition_names": sorted(condition.name for condition in done.outstanding),
            "done_complete": done.complete,
        }
        pin(
            "axis_5_advisory_boundary",
            observed,
            description=(
                "Four stages cleared and a valid spec, over a tree with outstanding definition-of-done "
                "conditions. F permits the export and presents the conditions beside it. The gate consults "
                "the four stages and nothing else (Req 27.6, 27.7; design Property 17)."
            ),
            requirements=("3.5",),
            measured={
                "iterate_custom_outstanding_count_from_task_1_6": 2,
                "iterate_custom_outstanding_names_from_task_1_6": [CONDITION_API_CLIENT, CONDITION_FORMATTED],
                "note": (
                    "task 1.6 measured the JumpCloud iterate_custom control at exactly two outstanding "
                    "conditions, formatted and api_client. Recorded here from that task rather than "
                    "re-measured, and recorded as a measurement of F rather than as an invariant: both "
                    "close when changes 3 and 5 land, so asserting the figure would make tasks 5.5 and 7.5 "
                    "fail for the right reason at the wrong place"
                ),
            },
        )

    def test_a_failed_stage_is_what_blocks_and_a_condition_is_not(self, advisory_boundary):
        """The boundary from the other side: only the stages move the decision."""
        _, done = advisory_boundary
        root = done.project_dir
        one_failed = PipelineReport(
            project_dir=root,
            stages=tuple(
                StageResult(
                    name=name,
                    status=StageStatus.PASSED if name != StageName.TEST else StageStatus.FAILED,
                    returncode=0 if name != StageName.TEST else 1,
                    stdout="",
                    stderr="",
                    duration_seconds=0.0,
                    message="" if name != StageName.TEST else "test stage failed with exit code 1",
                )
                for name in StageName.ORDER
            ),
            docker_available=True,
        )
        blocked = decide_export(_valid_spec_report(), one_failed)
        assert not blocked.permitted, "a failed stage did not block the export"
        assert [stage.name for stage in blocked.failed_stages] == [StageName.TEST]


# ---------------------------------------------------------------------------
# Axis 6 -- packaged contents
# ---------------------------------------------------------------------------


class PackagedObservation:
    """One packaging run over a copy of the recorded tree."""

    def __init__(self, root: Path, listed: Tuple[str, ...], members: Tuple[str, ...]) -> None:
        self.root = root
        self.listed = listed
        self.members = members

    @property
    def byproducts(self) -> Tuple[str, ...]:
        """The coverage byproducts `bugfix.md` 1.10 names, as packaged today."""
        return tuple(member for member in self.members if member in BYPRODUCT_MEMBERS_IN_THE_BASELINE)

    @property
    def plugin_files(self) -> Tuple[str, ...]:
        """Everything else: the files the plugin actually needs."""
        return tuple(member for member in self.members if member not in BYPRODUCT_MEMBERS_IN_THE_BASELINE)


@pytest.fixture(scope="module")
def packaged_recorded_tree(tmp_path_factory) -> PackagedObservation:
    """Package a copy of the recorded tree and read the archive's own members.

    A ``shutil.copytree`` copy, because packaging writes; and the members are read
    out of the ``.plg`` with :mod:`tarfile` rather than taken from
    :attr:`PlgArtifact.files`, which is ``list_plugin_files``'s own output and
    would leave the archive unmeasured.
    """
    source = tree(RECORDED_TREE)
    work = tmp_path_factory.mktemp("axis6")
    root = work / source.name
    shutil.copytree(source, root, symlinks=True)
    listed = tuple(list_plugin_files(root))
    artifact = BuildEngine().package(root, validation_passed=True, output_dir=work / "artifacts")
    with tarfile.open(artifact.path, "r:gz") as archive:
        members = tuple(sorted(member.name for member in archive.getmembers() if member.isfile()))
    return PackagedObservation(root, listed, members)


class TestAxisSixPackagedContents:
    """Axis 6 (`bugfix.md` 3.2) -- reference material stays out, plugin files stay in.

    **Corrected figure.** Task 1.9 measured ``list_plugin_files`` on this tree
    returning 39 = 37 plugin files + ``.coverage`` + ``unit_test/.coverage``, so
    `bugfix.md`'s "39 entries" already counted two byproducts and 3.2's "39-entry
    baseline less the byproducts" is **37**. Both figures are recorded; the
    compared payload is the 37, because that is the part change 8 must not touch.
    """

    def test_the_builder_metadata_directory_is_absent(self, packaged_recorded_tree: PackagedObservation):
        leaked = [
            member for member in packaged_recorded_tree.members if BUILDER_METADATA_DIR in PurePosixPath(member).parts
        ]
        assert not leaked, f"the .plg carries tool-only metadata: {leaked}"

    def test_no_reference_document_reaches_the_archive(self, packaged_recorded_tree: PackagedObservation):
        """3.10's other half: reference material is stored, and never shipped."""
        reference = [
            member
            for member in packaged_recorded_tree.members
            if PurePosixPath(member).suffix.lower() in (".yaml", ".yml", ".json", ".pdf")
            and PurePosixPath(member).name != "plugin.spec.yaml"
        ]
        assert not reference, f"the .plg carries reference material: {reference}"

    def test_the_recorded_member_set_is_unchanged(self, packaged_recorded_tree: PackagedObservation):
        observed = {
            "plugin_files": list(packaged_recorded_tree.plugin_files),
            "plugin_file_count": len(packaged_recorded_tree.plugin_files),
            "builder_metadata_members": [
                member
                for member in packaged_recorded_tree.members
                if BUILDER_METADATA_DIR in PurePosixPath(member).parts
            ],
        }
        pin(
            "axis_6_packaged_contents",
            observed,
            description=(
                "A copy of the recorded JumpCloud tree, packaged, with the archive's own members read back. "
                "The compared payload is the plugin files -- .builder/ and every reference document absent, "
                "every file the plugin needs present. The two .coverage byproducts are recorded separately "
                "because change 8 removes them by design."
            ),
            requirements=("3.2", "3.10"),
            measured={
                "byproducts_packaged_today": list(packaged_recorded_tree.byproducts),
                "total_members_today": len(packaged_recorded_tree.members),
                "listed_by_list_plugin_files": len(packaged_recorded_tree.listed),
                "bugfix_md_member_count": BUGFIX_MEMBER_COUNT,
                "note": (
                    "task 1.9's correction: bugfix.md's 39 already counted the two .coverage files, so "
                    f"3.2's '39-entry baseline less the byproducts' is {PLUGIN_FILE_COUNT}. Task 1.9 also "
                    "found the byproduct surface is wider than .coverage -- build/lib/**, *.egg-info/**, "
                    "the Makefile's tarball output and a bare .pyc all reach the .plg today -- and its "
                    "BYPRODUCT_CASES table enumerates the full partition"
                ),
            },
        )

    def test_the_corrected_baseline_count_is_thirty_seven(self, packaged_recorded_tree: PackagedObservation):
        """The correction stated as an assertion, so it cannot quietly drift back."""
        assert len(packaged_recorded_tree.plugin_files) == PLUGIN_FILE_COUNT, (
            f"the packaged plugin files number {len(packaged_recorded_tree.plugin_files)}, not "
            f"{PLUGIN_FILE_COUNT}. bugfix.md 3.2's figure of {BUGFIX_MEMBER_COUNT} counted the two "
            f"byproducts {list(BYPRODUCT_MEMBERS_IN_THE_BASELINE)} alongside the plugin's own files"
        )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_the_recording_environment_is_itself_recorded():
    """Pin what was available, so an incomplete baseline is visibly incomplete.

    Not a check that the host is complete -- it often will not be, and that is
    allowed. It writes the probe down. A fixture recorded with ``absent``
    naming ``tool:docker`` describes less of F than one recorded on a host with
    the engine running, and the difference has to be legible without re-running
    anything.
    """
    probe = environment()
    pin(
        "environment",
        {
            "probed_tools": sorted(probe["tools"]),
            "probed_trees": sorted(probe["trees"]),
        },
        description=(
            "Which tools and trees the preservation baselines probe for. The compared payload is the probe's "
            "shape; the values, versions and absences live in every fixture's provenance block, because "
            "those are properties of the host rather than of the tool."
        ),
        requirements=("3.6",),
        measured={
            "tools": probe["tools"],
            "trees": probe["trees"],
            "absent": probe["absent"],
            "complete_host": probe["complete_host"],
        },
    )
