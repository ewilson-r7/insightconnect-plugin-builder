"""Unit tests for the Plugin_Registry storage and queries (task 8.1; Req 11.1-11.4).

These cover specific examples and edge cases for recording plugin creation and
export events, reading them back, history ordering (most-recent-first), and
persistence across a reopen of a file-backed database. The universal
round-trip/ordering property is covered separately by the property test
(task 8.2, Property 22); empty-history and write-failure behavior is covered by
the task 8.3 unit tests.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from icplugin_builder.core.spec_model import SemVer
from icplugin_builder.persistence.registry import (
    ExportRecord,
    HistoryEntry,
    PluginRecord,
    PluginRegistry,
    RegistryError,
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "registry.db"


class TestRecordCreation:
    def test_records_name_vendor_version_and_utc(self, db_path):
        # Req 11.1: creation records name, vendor, version, and a UTC timestamp.
        created = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        with PluginRegistry(db_path) as registry:
            record = registry.record_creation("my_plugin", "acme_custom", SemVer(1, 0, 0), created)
        assert record == PluginRecord(
            plugin_name="my_plugin",
            vendor="acme_custom",
            version="1.0.0",
            created_utc=created.isoformat(),
        )

    def test_accepts_semver_or_string_version(self, db_path):
        with PluginRegistry(db_path) as registry:
            registry.record_creation("a", "v_custom", "2.3.4")
            assert registry.get_plugin("a").version == "2.3.4"

    def test_defaults_timestamp_to_now_utc(self, db_path):
        before = datetime.now(timezone.utc)
        with PluginRegistry(db_path) as registry:
            record = registry.record_creation("a", "v_custom", SemVer(1, 0, 0))
        stored = datetime.fromisoformat(record.created_utc)
        assert stored.tzinfo is not None
        assert stored >= before - timedelta(seconds=5)

    def test_naive_datetime_treated_as_utc(self, db_path):
        naive = datetime(2024, 6, 1, 8, 30, 0)
        with PluginRegistry(db_path) as registry:
            record = registry.record_creation("a", "v_custom", SemVer(1, 0, 0), naive)
        stored = datetime.fromisoformat(record.created_utc)
        assert stored.utcoffset() == timedelta(0)

    def test_recreate_updates_metadata_but_keeps_created_utc(self, db_path):
        first = datetime(2024, 1, 1, tzinfo=timezone.utc)
        with PluginRegistry(db_path) as registry:
            registry.record_creation("a", "v_custom", SemVer(1, 0, 0), first)
            registry.record_creation("a", "w_custom", SemVer(2, 0, 0), datetime(2025, 1, 1, tzinfo=timezone.utc))
            record = registry.get_plugin("a")
        assert record.vendor == "w_custom"
        assert record.version == "2.0.0"
        assert record.created_utc == first.isoformat()


class TestRecordExport:
    def test_records_version_target_and_utc(self, db_path):
        # Req 11.2: export records version, target, and a UTC timestamp.
        exported = datetime(2024, 2, 2, 9, 0, 0, tzinfo=timezone.utc)
        with PluginRegistry(db_path) as registry:
            registry.record_creation("a", "v_custom", SemVer(1, 0, 0))
            record = registry.record_export("a", SemVer(1, 0, 0), "https://us.api.insight.rapid7.com", exported)
        assert record == ExportRecord(
            plugin_name="a",
            version="1.0.0",
            target="https://us.api.insight.rapid7.com",
            export_utc=exported.isoformat(),
            result="success",
        )

    def test_multiple_exports_all_returned_most_recent_first(self, db_path):
        with PluginRegistry(db_path) as registry:
            registry.record_creation("a", "v_custom", SemVer(1, 0, 0))
            registry.record_export("a", "1.0.0", "local", datetime(2024, 1, 1, tzinfo=timezone.utc))
            registry.record_export("a", "1.0.1", "local", datetime(2024, 3, 1, tzinfo=timezone.utc))
            registry.record_export("a", "1.0.2", "local", datetime(2024, 2, 1, tzinfo=timezone.utc))
            versions = [e.version for e in registry.exports("a")]
        assert versions == ["1.0.1", "1.0.2", "1.0.0"]


class TestListPlugins:
    def test_lists_all_recorded_plugins(self, db_path):
        with PluginRegistry(db_path) as registry:
            registry.record_creation("a", "va_custom", SemVer(1, 0, 0), datetime(2024, 1, 1, tzinfo=timezone.utc))
            registry.record_creation("b", "vb_custom", SemVer(2, 0, 0), datetime(2024, 2, 1, tzinfo=timezone.utc))
            names = [p.plugin_name for p in registry.list_plugins()]
        # Most-recently created first.
        assert names == ["b", "a"]

    def test_get_missing_plugin_returns_none(self, db_path):
        with PluginRegistry(db_path) as registry:
            assert registry.get_plugin("nope") is None


class TestHistoryOrdering:
    def test_history_returns_creation_and_exports_most_recent_first(self, db_path):
        # Req 11.4: versions + export events ordered most-recent-first.
        with PluginRegistry(db_path) as registry:
            registry.record_creation("a", "v_custom", SemVer(1, 0, 0), datetime(2024, 1, 1, tzinfo=timezone.utc))
            registry.record_export("a", "1.0.0", "local", datetime(2024, 2, 1, tzinfo=timezone.utc))
            registry.record_export("a", "1.0.1", "https://tenant", datetime(2024, 3, 1, tzinfo=timezone.utc))
            history = registry.history("a")

        assert [(e.kind, e.version) for e in history] == [
            ("export", "1.0.1"),
            ("export", "1.0.0"),
            ("created", "1.0.0"),
        ]
        # Export entries carry their target; creation entry does not.
        assert history[0].target == "https://tenant"
        assert history[-1].target is None
        # Timestamps are non-increasing.
        stamps = [datetime.fromisoformat(e.timestamp) for e in history]
        assert stamps == sorted(stamps, reverse=True)

    def test_history_creation_ranks_oldest_on_timestamp_tie(self, db_path):
        # When an export shares the creation timestamp, the export (which
        # logically follows) is the more recent entry.
        same = datetime(2024, 5, 5, 5, 5, 5, tzinfo=timezone.utc)
        with PluginRegistry(db_path) as registry:
            registry.record_creation("a", "v_custom", SemVer(1, 0, 0), same)
            registry.record_export("a", "1.0.0", "local", same)
            history = registry.history("a")
        assert [e.kind for e in history] == ["export", "created"]

    def test_history_only_creation_when_no_exports(self, db_path):
        with PluginRegistry(db_path) as registry:
            registry.record_creation("a", "v_custom", SemVer(1, 0, 0))
            history = registry.history("a")
        assert len(history) == 1
        assert history[0] == HistoryEntry(
            kind="created",
            version="1.0.0",
            timestamp=history[0].timestamp,
        )


class TestPersistenceAcrossRestart:
    def test_records_survive_reopen(self, db_path):
        # Req 11.3: metadata and history persist across application restarts.
        created = datetime(2024, 1, 1, tzinfo=timezone.utc)
        exported = datetime(2024, 2, 1, tzinfo=timezone.utc)
        first = PluginRegistry(db_path)
        first.record_creation("a", "v_custom", SemVer(1, 2, 3), created)
        first.record_export("a", "1.2.3", "local", exported)
        first.close()

        reopened = PluginRegistry(db_path)
        try:
            plugin = reopened.get_plugin("a")
            history = reopened.history("a")
        finally:
            reopened.close()

        assert plugin == PluginRecord("a", "v_custom", "1.2.3", created.isoformat())
        assert [(e.kind, e.version, e.timestamp) for e in history] == [
            ("export", "1.2.3", exported.isoformat()),
            ("created", "1.2.3", created.isoformat()),
        ]


class TestEmptyHistory:
    """Task 8.3, Req 11.5: an unrecorded plugin yields an empty result, no error."""

    def test_history_of_unrecorded_plugin_is_empty_without_error(self, db_path):
        with PluginRegistry(db_path) as registry:
            assert registry.history("never_created") == []

    def test_history_is_empty_on_a_fresh_registry(self, db_path):
        # A brand-new database has no plugins at all; a query still returns [].
        with PluginRegistry(db_path) as registry:
            assert registry.history("anything") == []
            assert registry.list_plugins() == []

    def test_exports_of_unrecorded_plugin_is_empty_without_error(self, db_path):
        with PluginRegistry(db_path) as registry:
            assert registry.exports("never_created") == []

    def test_empty_history_does_not_create_the_plugin(self, db_path):
        # Querying an absent plugin must be a pure read: it stays absent.
        with PluginRegistry(db_path) as registry:
            registry.history("ghost")
            assert registry.get_plugin("ghost") is None


class TestWriteFailurePreservesHistory:
    """Task 8.3, Req 11.6: a failed write raises RegistryError and leaves prior history intact."""

    def _recorded_history(self, registry, plugin_name):
        return [(e.kind, e.version, e.timestamp, e.target, e.result) for e in registry.history(plugin_name)]

    def test_export_for_unknown_plugin_raises_and_preserves_history(self, db_path):
        # A foreign-key violation (export for a plugin with no creation record)
        # is a real failing write: it must roll back and raise RegistryError
        # while leaving the already-recorded plugin's history untouched.
        with PluginRegistry(db_path) as registry:
            registry.record_creation("a", "v_custom", SemVer(1, 0, 0), datetime(2024, 1, 1, tzinfo=timezone.utc))
            registry.record_export("a", "1.0.0", "local", datetime(2024, 2, 1, tzinfo=timezone.utc))
            before = self._recorded_history(registry, "a")

            with pytest.raises(RegistryError):
                registry.record_export("missing", "9.9.9", "local", datetime(2024, 3, 1, tzinfo=timezone.utc))

            # The unrelated plugin's history is byte-for-byte unchanged, and the
            # failed export left no orphan row anywhere.
            assert self._recorded_history(registry, "a") == before
            assert registry.exports("missing") == []
            assert registry.get_plugin("missing") is None

    def test_failed_creation_write_raises_and_preserves_history(self, db_path, monkeypatch):
        # Simulate a mid-write database failure: the transaction rolls back, a
        # RegistryError surfaces to the caller, and previously recorded history
        # survives unchanged (Req 11.6).
        with PluginRegistry(db_path) as registry:
            registry.record_creation("a", "v_custom", SemVer(1, 0, 0), datetime(2024, 1, 1, tzinfo=timezone.utc))
            registry.record_export("a", "1.0.0", "local", datetime(2024, 2, 1, tzinfo=timezone.utc))
            before = self._recorded_history(registry, "a")

            real_conn = registry._conn

            class _FailingConn:
                """Wraps the real connection but fails every write via execute()."""

                def __enter__(self):
                    return real_conn.__enter__()

                def __exit__(self, *exc_info):
                    return real_conn.__exit__(*exc_info)

                def execute(self, *args, **kwargs):
                    raise sqlite3.OperationalError("simulated write failure")

            monkeypatch.setattr(registry, "_conn", _FailingConn())

            with pytest.raises(RegistryError):
                registry.record_creation("b", "w_custom", SemVer(2, 0, 0))

            # Restore the real connection and confirm nothing partial persisted.
            monkeypatch.setattr(registry, "_conn", real_conn)
            assert self._recorded_history(registry, "a") == before
            assert registry.get_plugin("b") is None
