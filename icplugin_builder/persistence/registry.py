"""Plugin_Registry: the persistent record of created plugins and their exports.

The registry is the authoritative *local* record of "what plugins exist and
what versions have been exported" (design Data Models -> Plugin_Registry). It
backs version-bump monotonicity (Req 12.5) and export history (Req 11) and must
survive application restarts (Req 11.3), so it is stored in a file-backed SQLite
database rather than in memory.

Two tables mirror the design schema:

* ``plugins`` -- one row per plugin: its name (primary key), vendor, current
  version, and creation timestamp in UTC (Req 11.1).
* ``exports`` -- one row per recorded export: the plugin it belongs to, the
  exported version, the export target (``"local"`` or a tenant region base
  URL), the export timestamp in UTC, and a pass/fail result (Req 11.2). An
  autoincrement ``id`` gives a stable insertion order used to break timestamp
  ties.

All timestamps are stored as ISO-8601 strings in UTC. Callers may pass a
timezone-aware :class:`~datetime.datetime` or an ISO string; naive datetimes are
assumed to be UTC. A history query (:meth:`PluginRegistry.history`) returns the
plugin's creation entry plus every export event, ordered from most recent to
oldest timestamp (Req 11.4); a plugin with no recorded entries yields an empty
history rather than an error (Req 11.5).

Writes run inside a transaction and roll back on failure, raising
:class:`RegistryError` while leaving previously recorded history unchanged
(Req 11.6).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Union

from icplugin_builder.core.spec_model import SemVer

__all__ = [
    "RegistryError",
    "PluginRecord",
    "ExportRecord",
    "HistoryEntry",
    "PluginRegistry",
]

#: Accepted timestamp inputs: a timezone-aware/naive datetime or an ISO string.
TimestampInput = Union[datetime, str, None]

#: Accepted version inputs: a :class:`SemVer` or a plain string.
VersionInput = Union[SemVer, str]

_CREATED_KIND = "created"
_EXPORT_KIND = "export"


class RegistryError(Exception):
    """Raised when a registry read or write cannot be completed.

    On a write failure the underlying transaction is rolled back, so any
    previously recorded history is preserved unchanged (Req 11.6).
    """


@dataclass(frozen=True)
class PluginRecord:
    """A row from the ``plugins`` table (Req 11.1).

    Attributes:
        plugin_name: the plugin's snake_case name (primary key).
        vendor: the vendor value as recorded (``_custom``-suffixed on export).
        version: the plugin's current recorded version.
        created_utc: ISO-8601 UTC creation timestamp.
    """

    plugin_name: str
    vendor: str
    version: str
    created_utc: str


@dataclass(frozen=True)
class ExportRecord:
    """A row from the ``exports`` table (Req 11.2).

    Attributes:
        plugin_name: the plugin this export belongs to.
        version: the exported semantic version.
        target: ``"local"`` or the tenant region base URL.
        export_utc: ISO-8601 UTC export timestamp.
        result: ``"success"`` or ``"failed"``.
    """

    plugin_name: str
    version: str
    target: str
    export_utc: str
    result: str


@dataclass(frozen=True)
class HistoryEntry:
    """A single entry in a plugin's combined history (Req 11.4).

    A history is the plugin's creation entry plus one entry per recorded export,
    ordered from most recent to oldest timestamp.

    Attributes:
        kind: ``"created"`` for the creation entry, ``"export"`` for an export.
        version: the version associated with the entry.
        timestamp: ISO-8601 UTC timestamp of the entry.
        target: the export target for an export entry; ``None`` for creation.
        result: the export result for an export entry; ``None`` for creation.
    """

    kind: str
    version: str
    timestamp: str
    target: Optional[str] = None
    result: Optional[str] = None


class PluginRegistry:
    """A file-backed SQLite store of plugin metadata and export history.

    Construct with a database path; the schema is created on first use and the
    same file reopened later returns the previously recorded data (Req 11.3).
    The instance owns a single connection and commits after each successful
    write. Use as a context manager, or call :meth:`close`, to release it.
    """

    def __init__(self, db_path: Union[str, Path]) -> None:
        """Open (or create) the registry database at ``db_path``.

        Args:
            db_path: filesystem path to the SQLite database. The special value
                ``":memory:"`` creates a transient in-memory database that does
                not persist across instances.
        """
        self._db_path = db_path if db_path == ":memory:" else str(Path(db_path))
        # isolation_level=None would put us in autocommit mode; we keep the
        # default deferred transactions so a failed write rolls back cleanly.
        # check_same_thread=False lets the single owning connection be reached
        # from the API server's event-loop/worker threads (the tool is a
        # single local operator, so access is serialized and never concurrent).
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def _create_schema(self) -> None:
        """Create the ``plugins`` and ``exports`` tables if they do not exist."""
        try:
            with self._conn:
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS plugins (
                        plugin_name TEXT PRIMARY KEY,
                        vendor TEXT NOT NULL,
                        current_version TEXT NOT NULL,
                        created_utc TEXT NOT NULL
                    )
                    """)
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS exports (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        plugin_name TEXT NOT NULL REFERENCES plugins(plugin_name),
                        version TEXT NOT NULL,
                        target TEXT NOT NULL,
                        export_utc TEXT NOT NULL,
                        result TEXT NOT NULL
                    )
                    """)
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_exports_plugin ON exports(plugin_name)")
        except sqlite3.Error as error:
            raise RegistryError(f"failed to initialize plugin registry schema: {error}") from error

    # --- Writes ------------------------------------------------------------

    def record_creation(
        self,
        plugin_name: str,
        vendor: str,
        version: VersionInput,
        created_utc: TimestampInput = None,
    ) -> PluginRecord:
        """Record the creation of a plugin (Req 11.1).

        Records the plugin name, vendor, version, and a UTC creation timestamp.
        If the plugin already exists, its vendor and current version are updated
        while its original creation timestamp is preserved.

        Args:
            plugin_name: the plugin's snake_case name.
            vendor: the vendor value to record.
            version: the plugin's current version (a :class:`SemVer` or string).
            created_utc: creation timestamp; defaults to the current UTC time.

        Returns:
            The stored :class:`PluginRecord`.

        Raises:
            RegistryError: if the write fails; prior history is left unchanged.
        """
        version_str = _version_to_str(version)
        timestamp = _to_iso(created_utc)
        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO plugins (plugin_name, vendor, current_version, created_utc)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(plugin_name) DO UPDATE SET
                        vendor = excluded.vendor,
                        current_version = excluded.current_version
                    """,
                    (plugin_name, vendor, version_str, timestamp),
                )
        except sqlite3.Error as error:
            raise RegistryError(f"failed to record creation of plugin {plugin_name!r}: {error}") from error

        stored = self.get_plugin(plugin_name)
        assert stored is not None  # just written
        return stored

    def record_export(
        self,
        plugin_name: str,
        version: VersionInput,
        target: str,
        export_utc: TimestampInput = None,
        result: str = "success",
    ) -> ExportRecord:
        """Record an export of a plugin (Req 11.2).

        Records the exported version, the export target, and a UTC timestamp.

        Args:
            plugin_name: the plugin being exported. It must already have a
                creation record.
            version: the exported version (a :class:`SemVer` or string).
            target: ``"local"`` or the tenant region base URL.
            export_utc: export timestamp; defaults to the current UTC time.
            result: ``"success"`` (default) or ``"failed"``.

        Returns:
            The stored :class:`ExportRecord`.

        Raises:
            RegistryError: if the write fails (for example, if the plugin has no
                creation record); prior history is left unchanged.
        """
        version_str = _version_to_str(version)
        timestamp = _to_iso(export_utc)
        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO exports (plugin_name, version, target, export_utc, result)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (plugin_name, version_str, target, timestamp, result),
                )
        except sqlite3.Error as error:
            raise RegistryError(f"failed to record export of plugin {plugin_name!r}: {error}") from error

        return ExportRecord(
            plugin_name=plugin_name,
            version=version_str,
            target=target,
            export_utc=timestamp,
            result=result,
        )

    # --- Reads -------------------------------------------------------------

    def get_plugin(self, plugin_name: str) -> Optional[PluginRecord]:
        """Return the :class:`PluginRecord` for ``plugin_name``, or ``None``.

        Args:
            plugin_name: the plugin to look up.

        Returns:
            The stored record, or ``None`` if the plugin is not recorded.

        Raises:
            RegistryError: if the read fails.
        """
        try:
            row = self._conn.execute(
                "SELECT plugin_name, vendor, current_version, created_utc FROM plugins WHERE plugin_name = ?",
                (plugin_name,),
            ).fetchone()
        except sqlite3.Error as error:
            raise RegistryError(f"failed to read plugin {plugin_name!r}: {error}") from error
        if row is None:
            return None
        return PluginRecord(
            plugin_name=row["plugin_name"],
            vendor=row["vendor"],
            version=row["current_version"],
            created_utc=row["created_utc"],
        )

    def list_plugins(self) -> List[PluginRecord]:
        """Return every recorded plugin, ordered by creation timestamp descending.

        Raises:
            RegistryError: if the read fails.
        """
        try:
            rows = self._conn.execute(
                "SELECT plugin_name, vendor, current_version, created_utc FROM plugins"
            ).fetchall()
        except sqlite3.Error as error:
            raise RegistryError(f"failed to list plugins: {error}") from error
        records = [
            PluginRecord(
                plugin_name=row["plugin_name"],
                vendor=row["vendor"],
                version=row["current_version"],
                created_utc=row["created_utc"],
            )
            for row in rows
        ]
        records.sort(key=lambda record: _sort_key(record.created_utc), reverse=True)
        return records

    def exports(self, plugin_name: str) -> List[ExportRecord]:
        """Return every export recorded for ``plugin_name``, most-recent-first.

        Args:
            plugin_name: the plugin whose exports to list.

        Returns:
            The export records ordered from most recent to oldest timestamp; an
            empty list if the plugin has no recorded exports.

        Raises:
            RegistryError: if the read fails.
        """
        try:
            rows = self._conn.execute(
                """
                SELECT plugin_name, version, target, export_utc, result, id
                FROM exports WHERE plugin_name = ?
                """,
                (plugin_name,),
            ).fetchall()
        except sqlite3.Error as error:
            raise RegistryError(f"failed to read exports for plugin {plugin_name!r}: {error}") from error
        records = [
            ExportRecord(
                plugin_name=row["plugin_name"],
                version=row["version"],
                target=row["target"],
                export_utc=row["export_utc"],
                result=row["result"],
            )
            for row in rows
        ]
        # Most-recent-first: newer timestamp first; equal timestamps break on
        # the autoincrement id (later insert = more recent).
        order = {id(record): row["id"] for record, row in zip(records, rows)}
        records.sort(key=lambda record: (_sort_key(record.export_utc), order[id(record)]), reverse=True)
        return records

    def history(self, plugin_name: str) -> List[HistoryEntry]:
        """Return the combined version + export history for a plugin (Req 11.4).

        The history is the plugin's creation entry plus one entry per recorded
        export, ordered from most recent to oldest timestamp. A plugin with no
        recorded entries yields an empty list rather than raising (Req 11.5).

        Args:
            plugin_name: the plugin whose history to return.

        Returns:
            The history entries ordered most-recent-first.

        Raises:
            RegistryError: if the read fails.
        """
        plugin = self.get_plugin(plugin_name)
        if plugin is None:
            return []

        # (sort_timestamp, tiebreak, entry) tuples. Creation uses tiebreak -1 so
        # that, at an identical timestamp, exports (tiebreak = their id >= 1)
        # rank as more recent than the creation that logically preceded them.
        ordered: List[tuple] = [
            (
                _sort_key(plugin.created_utc),
                -1,
                HistoryEntry(
                    kind=_CREATED_KIND,
                    version=plugin.version,
                    timestamp=plugin.created_utc,
                ),
            )
        ]
        try:
            rows = self._conn.execute(
                """
                SELECT version, target, export_utc, result, id
                FROM exports WHERE plugin_name = ?
                """,
                (plugin_name,),
            ).fetchall()
        except sqlite3.Error as error:
            raise RegistryError(f"failed to read history for plugin {plugin_name!r}: {error}") from error
        for row in rows:
            ordered.append(
                (
                    _sort_key(row["export_utc"]),
                    row["id"],
                    HistoryEntry(
                        kind=_EXPORT_KIND,
                        version=row["version"],
                        timestamp=row["export_utc"],
                        target=row["target"],
                        result=row["result"],
                    ),
                )
            )
        ordered.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [entry for _, _, entry in ordered]

    # --- Lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()

    def __enter__(self) -> "PluginRegistry":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()


def _version_to_str(version: VersionInput) -> str:
    """Normalize a version input to its string form."""
    if isinstance(version, SemVer):
        return str(version)
    return str(version)


def _to_iso(value: TimestampInput) -> str:
    """Normalize a timestamp input to an ISO-8601 UTC string.

    A ``None`` value uses the current UTC time. A naive datetime is assumed to
    be UTC; an aware datetime is converted to UTC. A string is stored verbatim
    (the caller is trusted to provide an ISO-8601 value).
    """
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _sort_key(timestamp: str) -> datetime:
    """Parse an ISO-8601 timestamp into a comparable aware datetime.

    Values produced by :func:`_to_iso` always parse; a caller-supplied string
    that cannot be parsed falls back to the minimum datetime so it sorts as the
    oldest rather than crashing the ordering.
    """
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
