"""Recorded baselines of F, for the preservation half of the export-gate bugfix.

Spec task 2.1. The preservation property is stated as **F′ reproducing F**
(`bugfix.md` "Preservation, for all three"; design Property 75), so F's behaviour
has to be *recorded* before anything changes. This module is the recording
mechanism: it writes one JSON document per preservation axis under
``tests/fixtures/preservation_baseline/`` and, on every later run, compares a
fresh observation against the file rather than against an inline literal. That is
what makes the post-fix checks at tasks 5.5, 7.5, 9.6 and 2.2 comparisons against
**recorded fact** instead of re-derived expectation.

Three commitments, each of them a reaction to a way this kind of baseline goes
wrong:

* **Verdicts, not reports.** The compared payload holds stage statuses, finding
  keys, condition statuses, the export decision and the packaged member set --
  never message text. One message differs by design: for a tree with failing
  tests on a host with no Docker, F reports the ``test`` stage failed with the
  Docker-unavailable message and F′ reports it failed with the pytest failures.
  Same verdict, better message. Anything whose text or figure is *expected* to
  change goes in :attr:`Baseline.measured`, which is recorded and reported but
  never asserted.
* **Provenance travels with the measurement.** Every fixture carries the host, the
  interpreter, the resolved tool versions and the trees that were available when
  it was taken, plus what was **absent**. A baseline recorded on a host with no
  Docker and no plugin trees is a weaker document than one recorded on a complete
  host, and it says so rather than looking identical.
* **Recording is explicit.** Fixtures are only written when
  :data:`RECORD_ENV` is set, so a comparison can never quietly "fix" itself by
  overwriting the baseline it failed against.

Usage::

    baseline = pin("axis_2_hand_written_defect", observed, description=...)

In comparison mode (the default) that asserts ``observed`` equals the recorded
payload. In record mode it writes the file and returns it. To re-record::

    ICPB_RECORD_PRESERVATION_BASELINE=1 .venv/bin/python -m pytest \\
        tests/integrations/test_export_gate_preservation.py

_Requirements: 3.1 through 3.12_
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import pytest

__all__ = [
    "FIXTURE_DIR",
    "RECORD_ENV",
    "TOOLCHAIN_PATH_ENTRIES",
    "PROJECTS_ROOT",
    "Baseline",
    "environment",
    "load",
    "pin",
    "recording",
    "toolchain_path",
    "tree",
]

#: Where the recorded baselines live. One JSON document per axis, sorted keys and
#: two-space indent so a re-recording produces a readable diff.
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "preservation_baseline"

#: Set this to re-record. Deliberately not a pytest flag: recording rewrites
#: committed fixtures, so it should be an obvious, deliberate act at the shell.
RECORD_ENV = "ICPB_RECORD_PRESERVATION_BASELINE"

#: Directories holding the real toolchain on the reproduction host, per
#: `bugfix.md` "Reproduction Environment". Neither is on a non-login shell
#: ``PATH``, so measurements prepend them rather than assuming them.
TOOLCHAIN_PATH_ENTRIES: Tuple[str, ...] = (
    str(Path("~/Library/Python/3.9/bin").expanduser()),
    "/Applications/Docker.app/Contents/Resources/bin",
)

#: Where this tool keeps a user's plugin projects. **Read-only here.** Every
#: measurement that needs a real tree works on a ``shutil.copytree`` copy, because
#: packaging and test runs write, and leaving byproducts in a user's project
#: directory would be this suite committing the defect it measures.
PROJECTS_ROOT = Path("~/.icplugin-builder/projects").expanduser()

#: The tools whose presence changes what can be measured at all.
_PROBED_TOOLS: Tuple[str, ...] = ("docker", "insight-plugin", "prospector", "black", "flake8")

#: The plugin trees the reproduction run left behind, in the order `bugfix.md`
#: names them.
_PROBED_TREES: Tuple[str, ...] = ("jumpcloud", "abuseipdb", "rapid7_velociraptor")


def recording() -> bool:
    """Return ``True`` iff this run should rewrite the baselines."""
    return os.environ.get(RECORD_ENV, "").strip() not in ("", "0", "false", "no")


def toolchain_path() -> str:
    """Return ``PATH`` with :data:`TOOLCHAIN_PATH_ENTRIES` prepended."""
    existing = os.environ.get("PATH", "")
    entries = [*TOOLCHAIN_PATH_ENTRIES, existing] if existing else list(TOOLCHAIN_PATH_ENTRIES)
    return os.pathsep.join(entries)


def _version_of(tool: str) -> Optional[str]:
    """Return ``tool``'s first self-reported version line, or ``None`` if absent.

    Recorded so a baseline names the bar that produced it. A finding is only
    attributable to a version of a linter that was actually installed.
    """
    resolved = shutil.which(tool, path=toolchain_path())
    if resolved is None:
        return None
    for flag in ("--version", "version"):
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [resolved, flag],
                capture_output=True,
                timeout=60.0,
                check=False,
                env={**os.environ, "PATH": toolchain_path()},
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
        text = (completed.stdout + completed.stderr).decode("utf-8", errors="replace").strip()
        if text:
            return text.splitlines()[0].strip()
    return resolved


@lru_cache(maxsize=1)
def environment() -> Dict[str, Any]:
    """Probe the host once: what is installed, what trees exist, what is absent.

    This is the part of a baseline that keeps an incomplete recording from
    passing itself off as a complete one. ``absent`` is the operative field: an
    axis that could not be measured leaves its tool or its tree named here.
    """
    tools = {name: _version_of(name) for name in _PROBED_TOOLS}
    trees = {name: (PROJECTS_ROOT / name).is_dir() for name in _PROBED_TREES}
    absent = sorted(
        [f"tool:{name}" for name, version in tools.items() if version is None]
        + [f"tree:{name}" for name, present in trees.items() if not present]
    )
    return {
        "recorded_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "tools": tools,
        "trees": trees,
        "absent": absent,
        "complete_host": not absent,
    }


def tree(name: str) -> Path:
    """Return the real project tree ``name``, skipping when it is not present.

    A synthesised substitute would not carry the same evidence -- the figures in
    `bugfix.md` are about the plugin the 2026-08-17 run produced -- so absence is
    a skip, and :func:`environment` records it under ``absent``.
    """
    root = PROJECTS_ROOT / name
    if not root.is_dir():
        pytest.skip(
            f"the {name} tree is not present at {root}; this baseline is about that concrete tree and a "
            "synthesised one would not carry the same evidence"
        )
    return root


def _normalise(value: Any) -> Any:
    """Round-trip ``value`` through JSON so tuples, paths and sets compare equal.

    Observations are built from dataclasses and tuples; the recorded file holds
    lists and strings. Normalising both sides means a comparison failure is a
    behaviour difference rather than a Python type difference.
    """
    return json.loads(json.dumps(value, sort_keys=True, default=str))


class Baseline:
    """One recorded axis: what was compared, what was only noted, and by whom.

    Attributes:
        name: the axis identifier, which is also the fixture's file name.
        path: the fixture on disk.
        document: the whole recorded document, provenance included.
    """

    def __init__(self, name: str, path: Path, document: Mapping[str, Any]) -> None:
        self.name = name
        self.path = path
        self.document = dict(document)

    @property
    def observed(self) -> Any:
        """The compared payload: verdicts F produced, and F′ must reproduce."""
        return self.document.get("observed")

    @property
    def measured(self) -> Any:
        """Figures recorded but **not** asserted, because the fix changes them."""
        return self.document.get("measured")

    @property
    def provenance(self) -> Any:
        """The host, tools and trees the recording was taken on."""
        return self.document.get("provenance")

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Baseline({self.name!r}, path={self.path.name!r})"


def pin(
    name: str,
    observed: Any,
    *,
    description: str,
    requirements: Sequence[str] = (),
    measured: Optional[Mapping[str, Any]] = None,
) -> Baseline:
    """Compare ``observed`` against the recorded baseline ``name``, or record it.

    Args:
        name: the axis identifier; the fixture is ``FIXTURE_DIR / f"{name}.json"``.
        observed: the verdicts F produced. Compared exactly, after a JSON
            round-trip, so this must contain nothing host-dependent and no
            message text.
        description: what this axis is and why it is preserved, written into the
            fixture so the file explains itself without the test beside it.
        requirements: the `bugfix.md` clauses the axis preserves.
        measured: figures to record without asserting -- the ones the fix is
            *expected* to change, and the ones that are properties of this host
            rather than of the tool.

    Returns:
        The :class:`Baseline`, so a caller can read ``measured`` back for a
        diagnostic message.

    Raises:
        AssertionError: when the fresh observation differs from the recording.
    """
    path = FIXTURE_DIR / f"{name}.json"
    payload = _normalise(observed)
    document = {
        "axis": name,
        "description": description,
        "requirements": list(requirements),
        "provenance": environment(),
        "observed": payload,
        "measured": _normalise(dict(measured or {})),
    }

    if recording():
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return Baseline(name, path, document)

    recorded = load(name).document
    assert payload == recorded.get("observed"), (
        f"the verdicts for {name!r} differ from the baseline recorded in {path.name}.\n"
        f"recorded provenance: {json.dumps(recorded.get('provenance', {}).get('absent', []))} absent, "
        f"taken {recorded.get('provenance', {}).get('recorded_utc')}\n"
        f"recorded: {json.dumps(recorded.get('observed'), indent=2, sort_keys=True)}\n"
        f"observed: {json.dumps(payload, indent=2, sort_keys=True)}\n"
        "Preservation says F' reports what F reported. If this difference is intended, it belongs in the "
        "spec's recorded exception list, not in a re-recorded fixture."
    )
    return Baseline(name, path, recorded)


def load(name: str) -> Baseline:
    """Return the recorded baseline ``name`` without comparing anything to it.

    :func:`pin` compares a whole payload in one assertion, which is right for an
    axis whose payload is one tree's verdicts. It is wrong for a payload that is a
    *table* -- spec task 2.2's Property 75 records one row per axis point -- because
    a single wholesale comparison reports thirty-six rows when one of them moved,
    and reports it as a fixture error rather than as a failing example. Such a
    caller loads the recording here and compares row by row, so a failure names the
    axis point that changed.

    Args:
        name: the axis identifier; the fixture is ``FIXTURE_DIR / f"{name}.json"``.

    Returns:
        The recorded :class:`Baseline`.

    Raises:
        AssertionError: when nothing has been recorded for ``name`` yet. Absence is
            an assertion failure rather than a file error because a comparison with
            no baseline to compare against establishes nothing.
    """
    path = FIXTURE_DIR / f"{name}.json"
    if not path.is_file():
        raise AssertionError(
            f"no recorded baseline for {name!r} at {path}. This axis has never been observed on this "
            f"checkout; re-record with {RECORD_ENV}=1 and commit the fixture, so the post-fix comparison "
            "is against recorded fact rather than an expectation written after the change"
        )
    return Baseline(name, path, json.loads(path.read_text(encoding="utf-8")))
