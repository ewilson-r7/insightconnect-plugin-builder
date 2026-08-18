"""The single gate that answers whether a generated plugin is actually finished.

Everything else in this package answers a *part* of that question. The
:class:`~icplugin_builder.integrations.quality_gate.QualityGate` knows whether the
code parses and the tests pass. :func:`~icplugin_builder.core.spec_completeness.check_completeness`
knows whether the spec carries the fields the toolchain needs. The
:class:`~icplugin_builder.integrations.code_validator.CodeValidator` knows whether
``insight-plugin validate`` passed. Nobody reported the conjunction, and that gap
is not academic: the first version of this tool reported success on plugins with
unparseable Python, no API client, and ``pass`` in ``connection.test()``, because
each component had faithfully reported its own slice and no component was asked
the whole question (Req 27).

This module asks the whole question. It evaluates every ``Definition_Of_Done``
condition, names each one that is unmet, and -- the part that keeps it honest --
distinguishes a condition that was *checked and failed* from one that *could not
be checked at all*. The second is reported as **unverified**, never as met
(Req 27.5). A missing linter must not look like a clean lint.

Three design commitments, each of them a reaction to a way this has gone wrong:

* **Conditions are executed, not asserted.** Nothing here reads a Plugin_Agent's
  account of its own work (Req 27.4). Every condition traces back to a tool's
  output or to this module parsing the tree itself.
* **The structural conditions are checked with ``ast``, not with substring
  searches.** The real defect this catches looks like ``response =
  requests.get(url)`` in an action module *with no import of ``requests`` at
  all* -- so an import-based check would pass it. Usage is what matters.
* **Anything unknown fails closed.** :attr:`DoneReport.complete` requires every
  condition to be present and met, so a report assembled from partial inputs
  reports incomplete rather than done.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from ..core.spec_completeness import check_completeness
from ..core.spec_model import PluginSpec
from .code_validator import PipelineReport, StageName
from .quality_gate import (
    DEFAULT_COVERAGE_THRESHOLD,
    SOURCE_COMPILE,
    SOURCE_COVERAGE,
    SOURCE_FORMAT,
    SOURCE_PROSPECTOR,
    SOURCE_TESTS,
    QualityReport,
    is_generated,
    package_dir,
)
from .reference_material import read_reference_state

__all__ = [
    "CONDITION_CODE_PARSES",
    "CONDITION_FORMATTED",
    "CONDITION_LINT_CLEAN",
    "CONDITION_UNIT_TESTS",
    "CONDITION_COVERAGE",
    "CONDITION_API_CLIENT",
    "CONDITION_ACTIONS_USE_CLIENT",
    "CONDITION_CONNECTION",
    "CONDITION_DEPENDENCY_MANIFEST",
    "CONDITION_SPEC_COMPLETE",
    "CONDITION_REFERENCE_MATERIAL",
    "CONDITION_TOOLCHAIN_VALIDATE",
    "CONDITION_ORDER",
    "CONDITION_DESCRIPTIONS",
    "ConditionStatus",
    "ConditionResult",
    "DoneReport",
    "evaluate_done",
]

#: Every hand-written Python file parses (Req 27.1).
CONDITION_CODE_PARSES = "code_parses"
#: Formatting matches what ``insight-plugin refresh`` itself applies.
CONDITION_FORMATTED = "formatted"
#: The rulebook's linter reports nothing against hand-written code (Req 27.1).
CONDITION_LINT_CLEAN = "lint_clean"
#: Unit tests covering the actions pass (Req 27.1).
CONDITION_UNIT_TESTS = "unit_tests_pass"
#: Statement coverage of the plugin package meets the configured minimum (Req 27.1).
CONDITION_COVERAGE = "coverage_threshold"
#: An API client with centralized request handling and per-action methods (Req 27.1).
CONDITION_API_CLIENT = "api_client"
#: Component code goes through that client instead of doing its own HTTP.
CONDITION_ACTIONS_USE_CLIENT = "actions_use_api_client"
#: The connection's ``connect`` and ``test`` are implemented, not stubbed (Req 27.1).
CONDITION_CONNECTION = "connection_implemented"
#: A dependency manifest exists, with anything it lists pinned exactly (Req 27.1).
CONDITION_DEPENDENCY_MANIFEST = "dependency_manifest"
#: The spec carries every field the toolchain requires (Req 30).
CONDITION_SPEC_COMPLETE = "spec_complete"
#: The plugin was implemented against real vendor documentation (Req 28.13).
CONDITION_REFERENCE_MATERIAL = "reference_material"
#: ``insight-plugin validate`` passes (Req 27.1).
CONDITION_TOOLCHAIN_VALIDATE = "toolchain_validate"

#: Every condition, in reporting order. A :class:`DoneReport` missing any of
#: these is not a report that the plugin is done.
CONDITION_ORDER: Tuple[str, ...] = (
    CONDITION_CODE_PARSES,
    CONDITION_FORMATTED,
    CONDITION_LINT_CLEAN,
    CONDITION_API_CLIENT,
    CONDITION_ACTIONS_USE_CLIENT,
    CONDITION_CONNECTION,
    CONDITION_UNIT_TESTS,
    CONDITION_COVERAGE,
    CONDITION_DEPENDENCY_MANIFEST,
    CONDITION_SPEC_COMPLETE,
    CONDITION_REFERENCE_MATERIAL,
    CONDITION_TOOLCHAIN_VALIDATE,
)

#: What each condition means, phrased so naming an unmet one is self-explanatory.
CONDITION_DESCRIPTIONS: Dict[str, str] = {
    CONDITION_CODE_PARSES: "every hand-written Python file parses",
    CONDITION_FORMATTED: "hand-written code matches the formatter",
    CONDITION_LINT_CLEAN: "the linter reports nothing against hand-written code",
    CONDITION_API_CLIENT: "an API client centralizes requests and exposes a method per action",
    CONDITION_ACTIONS_USE_CLIENT: "component code calls the API client instead of making its own requests",
    CONDITION_CONNECTION: "the connection's connect() and test() are implemented, not stubbed",
    CONDITION_UNIT_TESTS: "the plugin's unit tests pass",
    CONDITION_COVERAGE: "statement coverage meets the configured minimum",
    CONDITION_DEPENDENCY_MANIFEST: "a dependency manifest exists with exact pins",
    CONDITION_SPEC_COMPLETE: "plugin.spec.yaml carries every field the toolchain needs",
    CONDITION_REFERENCE_MATERIAL: "the implementation was based on real vendor documentation",
    CONDITION_TOOLCHAIN_VALIDATE: "insight-plugin validate passes",
}

#: Where the API client belongs, relative to the plugin package.
_API_CLIENT_PATH = ("util", "api.py")

#: The centralized request helper the rulebook names.
_MAKE_REQUEST = "_make_request"

#: The status-to-exception mapping the rulebook names.
_ERROR_MAP = "HTTP_ERROR_MAP"

#: Component sections whose modules must not make their own HTTP requests.
_COMPONENT_SECTIONS = ("actions", "triggers", "tasks")

#: Modules whose use signals a component is doing its own HTTP.
_HTTP_MODULES = frozenset({"requests", "httpx", "urllib", "urllib2", "urllib3", "http", "aiohttp"})

#: Markers of a URL built in component code rather than owned by the client.
_URL_MARKERS = ("http://", "https://")

#: How many offending items to name in a condition's detail before summarising.
_DETAIL_LIMIT = 3


class ConditionStatus(Enum):
    """Whether one definition-of-done condition holds.

    The three-way split is the point. A two-way pass/fail would have to fold "the
    linter is not installed" into one of the two, and folding it into *pass* is
    how a plugin gets called finished on the strength of a check that never ran
    (Req 27.5).
    """

    #: Checked, and it holds.
    MET = "met"
    #: Checked, and it does not hold.
    UNMET = "unmet"
    #: Could not be checked, so nothing is known. Never counts as met.
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class ConditionResult:
    """The outcome of one definition-of-done condition.

    Attributes:
        name: the stable identifier, one of :data:`CONDITION_ORDER`.
        status: :class:`ConditionStatus`.
        description: what the condition requires, in plain words.
        detail: why it is unmet, or why it could not be evaluated. Empty when met.
    """

    name: str
    status: ConditionStatus
    description: str = ""
    detail: str = ""

    @property
    def met(self) -> bool:
        """Return ``True`` iff this condition was checked and holds."""
        return self.status is ConditionStatus.MET

    def __str__(self) -> str:  # pragma: no cover - convenience only
        label = f"[{self.status.value}] {self.name}: {self.description}"
        return f"{label} -- {self.detail}" if self.detail else label


@dataclass(frozen=True)
class DoneReport:
    """Whether a plugin meets every definition-of-done condition.

    Attributes:
        project_dir: the plugin working tree the conditions were evaluated against.
        conditions: one :class:`ConditionResult` per condition, in
            :data:`CONDITION_ORDER`.
        coverage_threshold: the minimum coverage that was in force, for reporting.
    """

    project_dir: Path
    conditions: Tuple[ConditionResult, ...] = ()
    coverage_threshold: float = DEFAULT_COVERAGE_THRESHOLD

    @property
    def complete(self) -> bool:
        """Return ``True`` iff every condition was evaluated and every one is met.

        Deliberately strict on two counts. An unverified condition does not count
        as met, so a plugin is never called finished on the strength of a check
        that could not run (Req 27.5). And a report that is missing a condition
        entirely is not complete either -- a partially assembled report reporting
        "done" would be the same defect this gate exists to close (Req 27.3).
        """
        if self.missing_conditions:
            return False
        return bool(self.conditions) and all(condition.met for condition in self.conditions)

    @property
    def missing_conditions(self) -> Tuple[str, ...]:
        """Conditions that :data:`CONDITION_ORDER` names but this report lacks."""
        present = {condition.name for condition in self.conditions}
        return tuple(name for name in CONDITION_ORDER if name not in present)

    @property
    def unmet(self) -> Tuple[ConditionResult, ...]:
        """The conditions that were checked and do not hold (Req 27.2)."""
        return tuple(c for c in self.conditions if c.status is ConditionStatus.UNMET)

    @property
    def unverified(self) -> Tuple[ConditionResult, ...]:
        """The conditions that could not be checked (Req 27.5)."""
        return tuple(c for c in self.conditions if c.status is ConditionStatus.UNVERIFIED)

    @property
    def outstanding(self) -> Tuple[ConditionResult, ...]:
        """Everything standing between this plugin and done, unmet first."""
        return self.unmet + self.unverified

    def condition(self, name: str) -> Optional[ConditionResult]:
        """Return the result for ``name``, or ``None`` when it was not evaluated."""
        for condition in self.conditions:
            if condition.name == name:
                return condition
        return None

    def summary(self) -> str:
        """Return one line stating whether the plugin is done, and what is missing.

        Nothing in this string calls an incomplete plugin complete, ready, or
        successful (Req 27.3), and every outstanding condition is named (Req 27.2).
        """
        if self.complete:
            return f"Plugin is complete: all {len(self.conditions)} definition-of-done conditions are met."

        parts: List[str] = []
        if self.unmet:
            parts.append(f"{len(self.unmet)} unmet ({', '.join(c.name for c in self.unmet)})")
        if self.unverified:
            parts.append(f"{len(self.unverified)} unverified ({', '.join(c.name for c in self.unverified)})")
        if self.missing_conditions:
            parts.append(f"{len(self.missing_conditions)} not evaluated ({', '.join(self.missing_conditions)})")
        return "Plugin is not complete -- " + "; ".join(parts) + "."

    def render(self) -> str:
        """Render every condition, one per line, with the reason for each shortfall."""
        return "\n".join(str(condition) for condition in self.conditions)


def evaluate_done(
    project_dir: Union[str, Path],
    *,
    spec: Optional[PluginSpec] = None,
    quality_report: Optional[QualityReport] = None,
    pipeline_report: Optional[PipelineReport] = None,
    coverage_threshold: float = DEFAULT_COVERAGE_THRESHOLD,
) -> DoneReport:
    """Evaluate every definition-of-done condition for one plugin (Req 27).

    Pure aggregation plus direct inspection of the tree: the checks that need a
    subprocess have already been run by their owners and arrive here as reports,
    while the structural conditions are read straight off the source with
    :mod:`ast`. Nothing is taken from an agent's account of its own work
    (Req 27.4).

    An input that was not supplied does not make a condition pass. It makes the
    conditions that depend on it *unverified*, which never counts as met
    (Req 27.5) -- so calling this with no reports at all yields a report that
    honestly says almost nothing is known.

    Args:
        project_dir: the plugin working tree.
        spec: the spec to check for completeness; ``None`` leaves that condition
            unverified.
        quality_report: the :class:`~icplugin_builder.integrations.quality_gate.QualityReport`
            covering parse, format, lint, tests, and coverage. ``None`` leaves all
            five unverified.
        pipeline_report: the four-stage
            :class:`~icplugin_builder.integrations.code_validator.PipelineReport`,
            for the ``insight-plugin validate`` condition. ``None`` leaves it
            unverified.
        coverage_threshold: the minimum statement coverage to require. Pass the
            same value the quality gate ran with.

    Returns:
        A :class:`DoneReport` carrying one result per condition in
        :data:`CONDITION_ORDER`.
    """
    root = Path(project_dir)

    results: Dict[str, ConditionResult] = {}
    results.update(_from_quality_report(quality_report, coverage_threshold))
    results.update(_from_structure(root))
    results[CONDITION_SPEC_COMPLETE] = _spec_condition(spec)
    results[CONDITION_REFERENCE_MATERIAL] = _reference_condition(root)
    results[CONDITION_TOOLCHAIN_VALIDATE] = _validate_condition(pipeline_report)

    ordered = tuple(results[name] for name in CONDITION_ORDER if name in results)
    return DoneReport(project_dir=root, conditions=ordered, coverage_threshold=coverage_threshold)


def _met(name: str) -> ConditionResult:
    """Build a met result for ``name``."""
    return ConditionResult(name=name, status=ConditionStatus.MET, description=CONDITION_DESCRIPTIONS[name])


def _unmet(name: str, detail: str) -> ConditionResult:
    """Build an unmet result for ``name``, carrying why."""
    return ConditionResult(
        name=name,
        status=ConditionStatus.UNMET,
        description=CONDITION_DESCRIPTIONS[name],
        detail=detail,
    )


def _unverified(name: str, detail: str) -> ConditionResult:
    """Build an unverified result for ``name``, carrying what prevented the check."""
    return ConditionResult(
        name=name,
        status=ConditionStatus.UNVERIFIED,
        description=CONDITION_DESCRIPTIONS[name],
        detail=detail,
    )


def _join(items: Sequence[str]) -> str:
    """Join up to :data:`_DETAIL_LIMIT` items, stating how many were left out."""
    shown = list(items[:_DETAIL_LIMIT])
    if len(items) > _DETAIL_LIMIT:
        shown.append(f"and {len(items) - _DETAIL_LIMIT} more")
    return "; ".join(shown)


# --------------------------------------------------------------------------- #
# Conditions derived from the quality gate's report
# --------------------------------------------------------------------------- #


def _was_skipped(report: QualityReport, source: str) -> Optional[str]:
    """Return the note explaining why ``source`` did not run, or ``None``.

    The quality gate records these as free text prefixed with the check's name,
    which is what makes "clean" distinguishable from "never ran" (Req 26.4).
    """
    for note in report.skipped:
        if note == source or note.startswith(f"{source} "):
            return note
    return None


def _from_quality_report(
    report: Optional[QualityReport],
    coverage_threshold: float,
) -> Dict[str, ConditionResult]:
    """Derive the five code-quality conditions from a quality-gate report."""
    pairs = (
        (CONDITION_CODE_PARSES, SOURCE_COMPILE),
        (CONDITION_FORMATTED, SOURCE_FORMAT),
        (CONDITION_LINT_CLEAN, SOURCE_PROSPECTOR),
        (CONDITION_UNIT_TESTS, SOURCE_TESTS),
    )
    if report is None:
        absent = "the quality gate has not run against this plugin"
        results = {name: _unverified(name, absent) for name, _ in pairs}
        results[CONDITION_COVERAGE] = _unverified(CONDITION_COVERAGE, absent)
        return results

    results = {}
    for name, source in pairs:
        results[name] = _quality_condition(report, name, source)
    results[CONDITION_COVERAGE] = _coverage_condition(report, coverage_threshold)
    return results


def _quality_condition(report: QualityReport, name: str, source: str) -> ConditionResult:
    """Turn one check's findings into a condition result."""
    findings = report.by_source(source)
    if findings:
        return _unmet(name, _join([str(finding) for finding in findings]))

    note = _was_skipped(report, source)
    if note is not None:
        return _unverified(name, f"the check did not run: {note}")

    # The compile and format checks are only run when there is something to run
    # them on, and they record no note when there is not. An empty tree would
    # otherwise report as parsing and formatting perfectly.
    if source in (SOURCE_COMPILE, SOURCE_FORMAT) and not report.checked_files:
        return _unverified(name, f"no hand-written Python files were found under {report.project_dir}")

    return _met(name)


def _coverage_condition(report: QualityReport, threshold: float) -> ConditionResult:
    """Turn the measured coverage figure into a condition result.

    Reads the measured percentage rather than the absence of a finding: no
    coverage finding is equally true of a plugin that cleared the threshold and
    one whose coverage was never measured, and only the first is done.
    """
    findings = report.by_source(SOURCE_COVERAGE)
    if findings:
        return _unmet(CONDITION_COVERAGE, _join([str(finding) for finding in findings]))

    if report.coverage_percent is None:
        note = _was_skipped(report, SOURCE_COVERAGE) or _was_skipped(report, SOURCE_TESTS)
        reason = f"coverage was not measured: {note}" if note else "coverage was not measured"
        return _unverified(CONDITION_COVERAGE, reason)

    if report.coverage_percent < threshold:
        return _unmet(
            CONDITION_COVERAGE,
            f"statement coverage is {report.coverage_percent:.0f}%, below the {threshold:.0f}% minimum",
        )
    return _met(CONDITION_COVERAGE)


# --------------------------------------------------------------------------- #
# Conditions read directly off the source tree
# --------------------------------------------------------------------------- #


def _from_structure(root: Path) -> Dict[str, ConditionResult]:
    """Check the conditions that are properties of the code's shape."""
    results = {CONDITION_DEPENDENCY_MANIFEST: _manifest_condition(root)}

    package = package_dir(root) if root.is_dir() else None
    if package is None:
        reason = f"no plugin package directory (icon_* or komand_*) was found under {root}"
        for name in (CONDITION_API_CLIENT, CONDITION_ACTIONS_USE_CLIENT, CONDITION_CONNECTION):
            results[name] = _unverified(name, reason)
        return results

    package_root = root / package
    results[CONDITION_API_CLIENT] = _api_client_condition(package_root, package)
    results[CONDITION_ACTIONS_USE_CLIENT] = _component_http_condition(root, package_root)
    results[CONDITION_CONNECTION] = _connection_condition(package_root, package)
    return results


def _parse(path: Path) -> Optional[ast.Module]:
    """Parse ``path``, returning ``None`` when it cannot be read or parsed.

    A file that does not parse leaves the structural conditions unknowable rather
    than violated -- the parse failure is its own condition, and reporting it
    twice would double-count one defect.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _defined_names(tree: ast.Module) -> Tuple[frozenset, frozenset]:
    """Return ``(function_names, assigned_names)`` defined anywhere in ``tree``."""
    functions = set()
    assigned = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigned.add(node.target.id)
    return frozenset(functions), frozenset(assigned)


def _imported_from_package(tree: ast.Module, name: str, package: str) -> bool:
    """Return ``True`` iff ``tree`` imports ``name`` from within ``package``.

    Two forms count, because both are in use: a relative import (``level > 0``,
    which cannot leave the package) and an absolute one whose module is the
    package or a submodule of it. An import from anywhere else does not count --
    a map living outside the plugin is not shipped with the plugin, so a client
    that reaches for it is broken on import in the tenant.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not any(alias.name == name for alias in node.names):
            continue
        if node.level and node.level > 0:
            return True
        module = node.module or ""
        if module == package or module.startswith(f"{package}."):
            return True
    return False


def _api_client_condition(package_root: Path, package: str) -> ConditionResult:
    """Check that a real API client exists where the rulebook puts it.

    Verifies the three things the rulebook names by name -- the module, the
    central request helper, the status-to-exception map -- plus at least one
    public domain method, since a client with nothing but ``_make_request`` gives
    the actions nothing to call.

    The map counts as present when ``api.py`` either defines it or imports it from
    within the plugin package (clause 2.13). Requiring a literal definition in
    ``api.py`` contradicted the rulebook, which puts the map in
    ``util/constants.py`` and has ``api.py`` map codes through it: the condition
    was unmet for exactly the shape the rulebook prescribes.

    A **dangling** import -- one naming a module that does not define the map -- is
    deliberately accepted here. The linter and the compile check already report it,
    with a file and a line; judging it again in this condition would report one
    defect twice.
    """
    api_path = package_root.joinpath(*_API_CLIENT_PATH)
    relative = f"{package}/{'/'.join(_API_CLIENT_PATH)}"
    if not api_path.is_file():
        return _unmet(CONDITION_API_CLIENT, f"{relative} does not exist; actions have no client to call")

    tree = _parse(api_path)
    if tree is None:
        return _unverified(CONDITION_API_CLIENT, f"{relative} could not be parsed, so its contents are unknown")

    functions, assigned = _defined_names(tree)
    domain_methods = {name for name in functions if not name.startswith("_") and name not in ("connect", "test", "run")}

    missing: List[str] = []
    if _MAKE_REQUEST not in functions:
        missing.append(f"no central {_MAKE_REQUEST}()")
    if _ERROR_MAP not in assigned and not _imported_from_package(tree, _ERROR_MAP, package):
        missing.append(f"no {_ERROR_MAP} defined in or imported into this module")
    if not domain_methods:
        missing.append("no public domain method for actions to call")

    if missing:
        return _unmet(CONDITION_API_CLIENT, f"{relative}: {'; '.join(missing)}")
    return _met(CONDITION_API_CLIENT)


def _component_modules(root: Path, package_root: Path) -> List[Path]:
    """Return the hand-written action, trigger, and task modules."""
    modules: List[Path] = []
    for section in _COMPONENT_SECTIONS:
        section_dir = package_root / section
        if not section_dir.is_dir():
            continue
        for path in sorted(section_dir.rglob("*.py")):
            if not path.is_file():
                continue
            if is_generated(path.relative_to(root).as_posix()):
                continue
            modules.append(path)
    return modules


def _http_usage(tree: ast.Module) -> bool:
    """Return ``True`` iff ``tree`` reaches for an HTTP library itself.

    Usage rather than imports, because the real defect looks like
    ``requests.get(url)`` in a module that never imported ``requests`` -- the
    plugin fails at runtime with a ``NameError`` and an import-based check waves
    it through.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] in _HTTP_MODULES for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in _HTTP_MODULES:
                return True
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in _HTTP_MODULES:
                return True
    return False


def _builds_url(tree: ast.Module) -> bool:
    """Return ``True`` iff ``tree`` contains a URL literal outside a docstring.

    Endpoint paths belong to the client. A component assembling its own absolute
    URL is the shape that put one vendor's base URL in every action this tool
    generated.
    """
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if node in docstrings:
            continue
        lowered = node.value.lower()
        if any(marker in lowered for marker in _URL_MARKERS):
            return True
    return False


def _component_http_condition(root: Path, package_root: Path) -> ConditionResult:
    """Check that component code delegates HTTP to the client instead of doing it."""
    modules = _component_modules(root, package_root)
    if not modules:
        return _unverified(
            CONDITION_ACTIONS_USE_CLIENT,
            f"no action, trigger, or task modules were found under {package_root.name}",
        )

    offenders: List[str] = []
    unparseable: List[str] = []
    for path in modules:
        relative = path.relative_to(root).as_posix()
        tree = _parse(path)
        if tree is None:
            unparseable.append(relative)
            continue
        faults = []
        if _http_usage(tree):
            faults.append("makes its own HTTP request")
        if _builds_url(tree):
            faults.append("builds a URL")
        if faults:
            offenders.append(f"{relative} {' and '.join(faults)}")

    if offenders:
        return _unmet(CONDITION_ACTIONS_USE_CLIENT, _join(offenders))
    if unparseable:
        return _unverified(CONDITION_ACTIONS_USE_CLIENT, f"could not be parsed: {_join(unparseable)}")
    return _met(CONDITION_ACTIONS_USE_CLIENT)


def _is_stub(node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> bool:
    """Return ``True`` iff ``node`` has a body that does nothing.

    Counts an unfinished marker as a stub too: the scaffolder emits ``test()`` as
    a ``# TODO`` comment above a bare ``pass``, and a plugin whose connection test
    is that comment cannot verify a connection.
    """
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]

    if not body:
        return True
    for statement in body:
        if isinstance(statement, ast.Pass):
            continue
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and statement.value.value is ...
        ):
            continue
        if isinstance(statement, ast.Raise):
            exception = statement.exc
            name = exception.func if isinstance(exception, ast.Call) else exception
            if isinstance(name, ast.Name) and name.id == "NotImplementedError":
                continue
        return False
    return True


def _has_todo(source: str, node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> bool:
    """Return ``True`` iff ``node``'s source carries an unfinished marker.

    Read off the raw lines rather than the tree, because ``ast`` discards
    comments and the marker the scaffolder leaves behind is a comment.
    """
    lines = source.splitlines()
    end = getattr(node, "end_lineno", None) or node.lineno
    segment = "\n".join(lines[node.lineno - 1 : end]).upper()
    return "TODO" in segment or "FIXME" in segment


def _connection_condition(package_root: Path, package: str) -> ConditionResult:
    """Check that ``connect()`` and ``test()`` are implemented rather than stubbed."""
    path = package_root / "connection" / "connection.py"
    relative = f"{package}/connection/connection.py"
    if not path.is_file():
        return _unmet(CONDITION_CONNECTION, f"{relative} does not exist")

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return _unverified(CONDITION_CONNECTION, f"{relative} could not be read: {error}")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _unverified(CONDITION_CONNECTION, f"{relative} could not be parsed, so its contents are unknown")

    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ("connect", "test")
    }

    problems: List[str] = []
    for name in ("connect", "test"):
        node = methods.get(name)
        if node is None:
            problems.append(f"{name}() is not defined")
        elif _is_stub(node):
            problems.append(f"{name}() is a stub")
        elif _has_todo(source, node):
            problems.append(f"{name}() still carries an unfinished marker")

    if problems:
        return _unmet(CONDITION_CONNECTION, f"{relative}: {'; '.join(problems)}")
    return _met(CONDITION_CONNECTION)


def _manifest_condition(root: Path) -> ConditionResult:
    """Check that ``requirements.txt`` exists and pins whatever it lists.

    A manifest of nothing but comments is the scaffolded default and counts as
    met: a plugin with no third-party dependencies has nothing to pin. A
    dependency named without ``==`` does not count, because a floating version is
    how a plugin that built today fails to build later.
    """
    path = root / "requirements.txt"
    if not path.is_file():
        return _unmet(CONDITION_DEPENDENCY_MANIFEST, "requirements.txt does not exist")

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        return _unverified(CONDITION_DEPENDENCY_MANIFEST, f"requirements.txt could not be read: {error}")

    unpinned = [
        line.strip() for line in lines if line.strip() and not line.strip().startswith(("#", "-")) and "==" not in line
    ]
    if unpinned:
        return _unmet(CONDITION_DEPENDENCY_MANIFEST, f"not pinned with '==': {_join(unpinned)}")
    return _met(CONDITION_DEPENDENCY_MANIFEST)


# --------------------------------------------------------------------------- #
# Conditions delegated to the spec check and the containerized pipeline
# --------------------------------------------------------------------------- #


def _spec_condition(spec: Optional[PluginSpec]) -> ConditionResult:
    """Check the spec for the fields the toolchain requires (Req 30)."""
    if spec is None:
        return _unverified(CONDITION_SPEC_COMPLETE, "no spec was supplied to check")

    report = check_completeness(spec)
    if report.is_complete:
        return _met(CONDITION_SPEC_COMPLETE)
    return _unmet(CONDITION_SPEC_COMPLETE, _join([f"{f.path}: {f.message}" for f in report.errors]))


def _reference_condition(root: Path) -> ConditionResult:
    """Check whether the implementation had real vendor documentation (Req 28.13).

    Three outcomes rather than two, because the absence of documentation is only a
    defect for a plugin that calls somebody's API. A plugin that encodes base64
    needs none, and reporting it as unmet would be a false alarm that teaches the
    operator to ignore this condition.

    So: met when documentation was stored, unmet only when the tree records that
    implementation deliberately went ahead without it, and unverified when nothing
    was recorded either way.
    """
    state = read_reference_state(root)
    if state.has_material:
        return _met(CONDITION_REFERENCE_MATERIAL)
    if state.without_reference:
        return _unmet(
            CONDITION_REFERENCE_MATERIAL,
            state.detail
            or (
                "implementation proceeded with no vendor documentation, so endpoints and "
                "payload shapes were inferred rather than sourced"
            ),
        )
    return _unverified(
        CONDITION_REFERENCE_MATERIAL,
        "no record of vendor documentation either way; a plugin that calls no external API needs none",
    )


def _validate_condition(pipeline_report: Optional[PipelineReport]) -> ConditionResult:
    """Check whether ``insight-plugin validate`` passed.

    A stage that never ran a process -- Docker absent, the CLI missing, or the
    stage killed on its timeout -- is unverified rather than unmet (Req 27.5). Only
    a real non-zero exit is the toolchain rejecting the plugin.
    """
    if pipeline_report is None:
        return _unverified(CONDITION_TOOLCHAIN_VALIDATE, "the validation pipeline has not run against this plugin")

    stage = pipeline_report.stage(StageName.VALIDATE)
    if stage is None:
        return _unverified(CONDITION_TOOLCHAIN_VALIDATE, "the pipeline recorded no validate stage")
    if stage.passed:
        return _met(CONDITION_TOOLCHAIN_VALIDATE)
    if stage.timed_out or stage.returncode is None:
        reason = stage.message or "the validate stage did not run to completion"
        return _unverified(CONDITION_TOOLCHAIN_VALIDATE, reason)

    detail = stage.message or _last_line(stage.stderr) or _last_line(stage.stdout) or "see the stage output"
    return _unmet(CONDITION_TOOLCHAIN_VALIDATE, f"insight-plugin validate exited {stage.returncode}: {detail}")


def _last_line(text: str) -> str:
    """Return the last non-blank line of ``text``, or an empty string."""
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""
