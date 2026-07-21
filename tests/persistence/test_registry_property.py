"""Property-based test for Plugin_Registry persistence and ordering (task 8.2).

The unit tests in ``test_registry.py`` pin specific round-trip and ordering
examples; this module covers design Property 22 with Hypothesis: across
arbitrary sequences of plugin-creation and export events written to a
file-backed registry, reopening the store returns those records unchanged, and
history/export queries return entries ordered from most recent to oldest
timestamp (Req 11.1-11.4).
"""

from datetime import datetime, timezone
from typing import List, Tuple

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.persistence.registry import (
    ExportRecord,
    PluginRecord,
    PluginRegistry,
)
from tests.strategies import semvers, snake_case_names, vendors

#: A creation event: (plugin_name, vendor, version_str, created_utc_iso).
CreationEvent = Tuple[str, str, str, str]
#: An export event: (plugin_name, version_str, target, export_utc_iso, result).
ExportEvent = Tuple[str, str, str, str, str]

_TARGETS = (
    "local",
    "https://us.api.insight.rapid7.com",
    "https://eu.api.insight.rapid7.com",
    "https://ca.api.insight.rapid7.com",
)


def _utc_datetimes() -> st.SearchStrategy[datetime]:
    """Generate timezone-aware UTC datetimes across a wide, comparable range."""
    return st.datetimes(
        min_value=datetime(2000, 1, 1),
        max_value=datetime(2035, 12, 31),
        timezones=st.just(timezone.utc),
    )


@st.composite
def registry_events(draw: st.DrawFn) -> Tuple[List[CreationEvent], List[ExportEvent]]:
    """Generate a batch of creation events plus exports referencing them.

    Plugin names are unique so every creation is an independent row (avoiding
    the recreate-updates-metadata path, which the unit tests cover). Each export
    targets one of the created plugins, carries its own UTC timestamp drawn
    independently of the creation timestamp, and may record success or failure.
    Empty export lists are included to exercise creation-only histories.
    """
    names = draw(st.lists(snake_case_names(), min_size=1, max_size=5, unique=True))

    creations: List[CreationEvent] = []
    for name in names:
        vendor = draw(vendors())
        version = draw(semvers())
        created = draw(_utc_datetimes())
        creations.append((name, vendor, str(version), created.isoformat()))

    export_strategy = st.builds(
        lambda name, version, target, ts, result: (name, str(version), target, ts.isoformat(), result),
        st.sampled_from(names),
        semvers(),
        st.sampled_from(_TARGETS),
        _utc_datetimes(),
        st.sampled_from(("success", "failed")),
    )
    exports = draw(st.lists(export_strategy, max_size=20))
    return creations, exports


def _non_increasing(timestamps: List[str]) -> bool:
    """Return whether ISO-8601 timestamps are ordered most-recent-first."""
    parsed = [datetime.fromisoformat(ts) for ts in timestamps]
    return parsed == sorted(parsed, reverse=True)


# Feature: insightconnect-plugin-builder, Property 22: Registry persistence round trip and ordering
@settings(max_examples=100)
@given(events=registry_events())
def test_registry_round_trip_and_ordering(events, tmp_path_factory):
    """Records survive a reopen and history is ordered most-recent-first.

    Writes every creation and export through one registry instance backed by a
    temp-file database, closes it, then opens a fresh instance on the same path.
    The reopened store must return each plugin's creation record unchanged and
    the same set of export records (round trip), and both ``exports`` and
    ``history`` must be ordered from most recent to oldest timestamp.

    **Validates: Requirements 11.1, 11.2, 11.3, 11.4**
    """
    creations, exports = events
    db_path = tmp_path_factory.mktemp("registry") / "registry.db"

    # Write through the first instance, then discard it to simulate a restart.
    writer = PluginRegistry(db_path)
    try:
        for name, vendor, version, created in creations:
            writer.record_creation(name, vendor, version, created)
        for name, version, target, exported, result in exports:
            writer.record_export(name, version, target, exported, result)
    finally:
        writer.close()

    # Reference model of what the reopened store must contain.
    expected_plugins = {
        name: PluginRecord(plugin_name=name, vendor=vendor, version=version, created_utc=created)
        for name, vendor, version, created in creations
    }
    expected_exports: dict = {name: [] for name, *_ in creations}
    for name, version, target, exported, result in exports:
        expected_exports[name].append(
            ExportRecord(
                plugin_name=name,
                version=version,
                target=target,
                export_utc=exported,
                result=result,
            )
        )

    reopened = PluginRegistry(db_path)
    try:
        for name, expected_record in expected_plugins.items():
            # Round trip: creation record is returned unchanged after reopen.
            assert reopened.get_plugin(name) == expected_record

            got_exports = reopened.exports(name)
            # Round trip: the same set of export records is returned.
            assert sorted(map(_export_key, got_exports)) == sorted(map(_export_key, expected_exports[name]))
            # Ordering: exports are most-recent-first.
            assert _non_increasing([e.export_utc for e in got_exports])

            history = reopened.history(name)
            # History holds the creation entry plus one entry per export.
            assert len(history) == len(expected_exports[name]) + 1
            assert sum(1 for e in history if e.kind == "created") == 1
            # Ordering: combined history is most-recent-first.
            assert _non_increasing([e.timestamp for e in history])
    finally:
        reopened.close()


def _export_key(record: ExportRecord) -> Tuple[str, str, str, str, str]:
    """Order-independent identity of an export record for set comparison."""
    return (record.plugin_name, record.version, record.target, record.export_utc, record.result)
