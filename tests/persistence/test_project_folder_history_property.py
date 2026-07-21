"""Property-based test for per-version history retention (task 11.3).

The unit tests in ``test_project_folder.py`` pin specific snapshot/tooling
examples; this module covers design Property 39 with Hypothesis: for any
sequence of exported versions of a plugin, each version's ``Plugin_Spec``
snapshot and export outcome must be independently retrievable from the
``Project_Folder`` and equal to what was recorded at that version (Req 21.3).
"""

from __future__ import annotations

import json
from typing import List, Optional, Tuple

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.spec_model import PluginSpec, SemVer
from icplugin_builder.core.yaml_codec import dump_plugin_spec, load_plugin_spec
from icplugin_builder.persistence.project_folder import (
    EXPORT_OUTCOME_FILE,
    HISTORY_DIRNAME,
    SPEC_FILENAME,
    BUILDER_DIRNAME,
    ProjectFolder,
)
from tests.strategies import plugin_specs, semvers

#: One recorded version: (version, spec-at-that-version, optional export outcome).
VersionSnapshot = Tuple[SemVer, PluginSpec, Optional[dict]]

_TARGETS = (
    "local",
    "https://us.api.insight.rapid7.com",
    "https://eu.api.insight.rapid7.com",
)


def _export_outcomes() -> st.SearchStrategy[dict]:
    """Generate JSON-round-trippable ``export_outcome`` dicts.

    Every value is a JSON-native scalar so a ``json.dumps``/``json.loads``
    round trip returns an equal dict, isolating the test from serialization
    quirks and keeping the focus on history retention.
    """
    return st.fixed_dictionaries(
        {
            "target": st.sampled_from(_TARGETS),
            "timestamp_utc": st.datetimes().map(lambda dt: dt.isoformat()),
            "result": st.sampled_from(("success", "failed")),
            "message": st.text(
                alphabet=st.characters(min_codepoint=32, max_codepoint=126),
                max_size=40,
            ),
        }
    )


@st.composite
def version_histories(draw: st.DrawFn) -> List[VersionSnapshot]:
    """Generate a sequence of versions, each with its own spec and outcome.

    Versions are unique by their string form so every ``record_version`` call
    targets a distinct ``history/<version>/`` directory (the store keys history
    by version string). Each version gets an independently generated spec
    snapshot and, most of the time, an export outcome; ``None`` outcomes are
    included to exercise the snapshot-only path.
    """
    versions = draw(st.lists(semvers(), min_size=1, max_size=6, unique_by=str))
    snapshots: List[VersionSnapshot] = []
    for version in versions:
        spec = draw(plugin_specs())
        outcome = draw(st.one_of(st.none(), _export_outcomes()))
        snapshots.append((version, spec, outcome))
    return snapshots


# Feature: insightconnect-plugin-builder, Property 39: Per-version history retention
@settings(max_examples=100)
@given(history=version_histories())
def test_per_version_history_is_independently_retrievable(history, tmp_path_factory):
    """Each recorded version's spec and outcome survive independently and unchanged.

    Records a sequence of versions, each with a distinct spec snapshot and
    export outcome, into one ``Project_Folder``. Afterwards every version must
    be listed, and for each version the retained ``Plugin_Spec`` snapshot and
    export outcome must be independently retrievable and equal to exactly what
    was recorded for that version, with no cross-contamination between versions.

    **Validates: Requirements 21.3**
    """
    projects_root = tmp_path_factory.mktemp("projects")
    folder = ProjectFolder.create(projects_root, "my_plugin", history[0][1])

    # Record every version's snapshot and (optional) export outcome.
    for version, spec, outcome in history:
        folder.record_version(version, spec, export_outcome=outcome)

    history_root = folder.path / BUILDER_DIRNAME / HISTORY_DIRNAME
    expected_versions = sorted(str(version) for version, _spec, _outcome in history)

    # Every recorded version is listed exactly once.
    assert sorted(folder.list_versions()) == expected_versions

    for version, spec, outcome in history:
        version_str = str(version)
        version_dir = history_root / version_str

        # The snapshot is retrievable from this version's own directory ...
        snapshot_text = (version_dir / SPEC_FILENAME).read_text(encoding="utf-8")
        # ... byte-for-byte equal to what was exported at that version ...
        assert snapshot_text == dump_plugin_spec(spec)
        # ... and re-parses to a spec equal to the one recorded.
        assert load_plugin_spec(snapshot_text) == spec

        outcome_path = version_dir / EXPORT_OUTCOME_FILE
        if outcome is None:
            # No outcome recorded -> no outcome file for this version.
            assert not outcome_path.exists()
        else:
            stored = json.loads(outcome_path.read_text(encoding="utf-8"))
            # The export outcome is retrievable and equal to what was recorded.
            assert stored == outcome
