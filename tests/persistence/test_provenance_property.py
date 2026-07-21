"""Property-based test for provenance across entry modes (task 20.2).

Design Property 51 requires that *every* draft created through any of the three
entry modes -- create net-new, iterate on a previously created custom plugin, or
enhance an existing production plugin -- persists a ``Provenance_Record`` in the
project's ``.builder/project.json`` whose entry mode equals the mode used to
create the draft (Req 24.5). This module exercises all three modes with
Hypothesis, plus the default net-new provenance recorded when a draft is created
without an explicit one, and verifies the fork fields survive persistence for a
production fork.
"""

import json
from datetime import datetime, timezone
from typing import NamedTuple, Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.spec_model import PluginSpec
from icplugin_builder.persistence.project_folder import (
    ENTRY_MODE_CREATE_NEW,
    ENTRY_MODE_ENHANCE_PRODUCTION,
    ENTRY_MODE_ITERATE_CUSTOM,
    ProjectFolder,
    ProjectMetadata,
    ProvenanceRecord,
)
from tests.strategies import plugin_specs, snake_case_names, semvers


class ProvenanceCase(NamedTuple):
    """One generated draft-origination scenario to persist and verify.

    Attributes:
        provenance: the record passed to :meth:`ProjectFolder.create`, or
            ``None`` to exercise the default net-new provenance.
        expected_entry_mode: the entry mode the persisted record must carry.
    """

    provenance: Optional[ProvenanceRecord]
    expected_entry_mode: str


def _created_utcs() -> st.SearchStrategy[str]:
    """Generate ISO-8601 UTC creation timestamps for provenance records."""
    return st.datetimes(
        min_value=datetime(2000, 1, 1),
        max_value=datetime(2035, 12, 31),
        timezones=st.just(timezone.utc),
    ).map(lambda moment: moment.isoformat())


@st.composite
def provenance_cases(draw: st.DrawFn) -> ProvenanceCase:
    """Generate a provenance scenario spanning all three entry modes.

    Draws one of four scenarios: an explicit net-new record, an
    iterate-custom record, an enhance-production record with all fork fields
    populated, or ``None`` (which must default to a net-new record). This
    exercises Property 51 across every entry mode plus the default path.
    """
    created_utc = draw(_created_utcs())
    scenario = draw(st.sampled_from(("create_new", "iterate_custom", "enhance_production", "default")))

    if scenario == "default":
        return ProvenanceCase(provenance=None, expected_entry_mode=ENTRY_MODE_CREATE_NEW)

    if scenario == "create_new":
        return ProvenanceCase(
            provenance=ProvenanceRecord(entry_mode=ENTRY_MODE_CREATE_NEW, created_utc=created_utc),
            expected_entry_mode=ENTRY_MODE_CREATE_NEW,
        )

    if scenario == "iterate_custom":
        return ProvenanceCase(
            provenance=ProvenanceRecord(entry_mode=ENTRY_MODE_ITERATE_CUSTOM, created_utc=created_utc),
            expected_entry_mode=ENTRY_MODE_ITERATE_CUSTOM,
        )

    # enhance_production: a production fork carries all fork-identifying fields.
    provenance = ProvenanceRecord(
        entry_mode=ENTRY_MODE_ENHANCE_PRODUCTION,
        created_utc=created_utc,
        source_repo=draw(snake_case_names()),
        source_visibility=draw(st.sampled_from(("public", "private"))),
        source_location=draw(st.sampled_from(("local_clone", "remote_github"))),
        original_plugin_name=draw(snake_case_names()),
        original_version=str(draw(semvers())),
    )
    return ProvenanceCase(provenance=provenance, expected_entry_mode=ENTRY_MODE_ENHANCE_PRODUCTION)


# Feature: insightconnect-plugin-builder, Property 51: Provenance recorded for every entry mode
@settings(max_examples=100)
@given(case=provenance_cases(), spec=plugin_specs(), plugin_name=snake_case_names())
def test_provenance_recorded_for_every_entry_mode(case, spec, plugin_name, tmp_path_factory):
    """Every created draft persists a provenance whose entry mode is the one used.

    Creates a ``Project_Folder`` for each generated entry-mode scenario, then
    reads the persisted ``.builder/project.json`` back from disk and asserts a
    ``Provenance_Record`` is present whose entry mode equals the mode used to
    create the draft. For a production fork the fork-identifying fields
    (source repo/visibility/location, original name/version) also round-trip.

    **Validates: Requirements 24.5**
    """
    assert isinstance(spec, PluginSpec)
    projects_root = tmp_path_factory.mktemp("projects")

    folder = ProjectFolder.create(
        projects_root,
        plugin_name,
        spec,
        provenance=case.provenance,
    )

    # Read the raw persisted metadata straight from disk to confirm the
    # provenance was actually written to .builder/project.json (Req 24.5).
    raw = json.loads(folder.metadata_path.read_text(encoding="utf-8"))
    assert "provenance" in raw, "every created draft must persist a provenance record"

    metadata = ProjectMetadata.from_dict(raw)
    assert metadata.provenance is not None
    assert metadata.provenance.entry_mode == case.expected_entry_mode

    # The folder's own metadata() read agrees with the on-disk JSON.
    assert folder.metadata().provenance == metadata.provenance

    # For a production fork, the fork-identifying fields survive persistence.
    if case.expected_entry_mode == ENTRY_MODE_ENHANCE_PRODUCTION:
        assert case.provenance is not None
        persisted = metadata.provenance
        assert persisted.source_repo == case.provenance.source_repo
        assert persisted.source_visibility == case.provenance.source_visibility
        assert persisted.source_location == case.provenance.source_location
        assert persisted.original_plugin_name == case.provenance.original_plugin_name
        assert persisted.original_version == case.provenance.original_version
