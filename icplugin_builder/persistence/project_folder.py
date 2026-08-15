"""Project_Folder: the on-disk working tree and history store for one plugin.

Each plugin the tool creates gets its own directory on the local filesystem
(design Data Models -> Project_Folder, Req 21). That directory is the durable
home for the plugin's source of truth (``plugin.spec.yaml``), its generated code
tree, its documentation, its build artifacts, and a tool-owned ``.builder/``
metadata subtree that never leaks into the plugin or its ``.plg`` package.

Layout (mirrors the design):

.. code-block:: text

    <projects_root>/<plugin_name>/
      plugin.spec.yaml                 # source of truth (current draft)
      <prefix>_<plugin_name>/          # package tree (icon_ or komand_)
      help.md                          # generated docs
      Dockerfile Makefile setup.py ... # other generated files
      .builder/                        # tool-owned metadata (never packaged)
        project.json                   # plugin_name, current_version, timestamps,
                                       #   package_prefix, provenance
        tooling.json                   # per-build tool version stamps
        history/<version>/             # per-version spec snapshot + export outcome
        artifacts/<name>-<version>.plg # retained built artifacts
        baseline/                      # production-fork baseline (set elsewhere)

This module implements the layout, the metadata files, per-version history
snapshots, and a listing of previously created plugins with their name, current
version, and last-modification timestamp (Req 21.1, 21.2, 21.4). All timestamps
are ISO-8601 strings in UTC.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Union

from icplugin_builder.core.spec_model import PluginSpec, SemVer
from icplugin_builder.core.yaml_codec import dump_plugin_spec

__all__ = [
    "ProjectFolderError",
    "ProvenanceRecord",
    "ToolingStamp",
    "ProjectMetadata",
    "ProjectListing",
    "ProjectFolder",
    "list_projects",
    "ENTRY_MODE_CREATE_NEW",
    "ENTRY_MODE_ITERATE_CUSTOM",
    "ENTRY_MODE_ENHANCE_PRODUCTION",
    "VALID_ENTRY_MODES",
]

#: Directory name for the tool-owned metadata subtree.
BUILDER_DIRNAME = ".builder"
#: File holding the plugin's project metadata.
PROJECT_METADATA_FILE = "project.json"
#: File holding per-build tooling version stamps.
TOOLING_FILE = "tooling.json"
#: Subdirectory holding per-version snapshots.
HISTORY_DIRNAME = "history"
#: Subdirectory holding retained build artifacts.
ARTIFACTS_DIRNAME = "artifacts"
#: The plugin spec filename (source of truth).
SPEC_FILENAME = "plugin.spec.yaml"
#: The generated documentation filename.
HELP_FILENAME = "help.md"
#: The per-version export-outcome filename.
EXPORT_OUTCOME_FILE = "export_outcome.json"

#: Accepted SDK-era package prefixes (design: ``icon_`` current, ``komand_`` legacy).
_VALID_PREFIXES = ("icon", "komand")
#: Default package prefix for net-new plugins.
DEFAULT_PACKAGE_PREFIX = "icon"

#: Entry mode for a net-new plugin started from an empty draft (design
#: Provenance_Record / Req 24.1, 24.2).
ENTRY_MODE_CREATE_NEW = "create_new"
#: Entry mode for iterating on a previously created custom plugin (Req 24.1, 24.3).
ENTRY_MODE_ITERATE_CUSTOM = "iterate_custom"
#: Entry mode for a read-only fork of a production plugin (Req 24.1, 24.4, 25).
ENTRY_MODE_ENHANCE_PRODUCTION = "enhance_production"
#: The three recognized entry modes a Provenance_Record may record (Req 24.1).
VALID_ENTRY_MODES = (
    ENTRY_MODE_CREATE_NEW,
    ENTRY_MODE_ITERATE_CUSTOM,
    ENTRY_MODE_ENHANCE_PRODUCTION,
)

#: Accepted version inputs: a :class:`SemVer` or a plain string.
VersionInput = Union[SemVer, str]
#: Accepted timestamp inputs: a datetime or an ISO string; ``None`` means "now".
TimestampInput = Union[datetime, str, None]


class ProjectFolderError(Exception):
    """Raised when a project folder cannot be created, written, or read."""


@dataclass(frozen=True)
class ProvenanceRecord:
    """How a draft originated (design Data Models -> Provenance_Record, Req 24.5).

    Recorded for **every** created draft regardless of entry mode: a net-new
    draft, a custom-iteration draft, or a production fork all carry a
    :class:`ProvenanceRecord` so each draft has an auditable lineage. The
    ``entry_mode`` is one of :data:`VALID_ENTRY_MODES`; the fork fields
    (``source_*``, ``original_*``) are populated only when ``entry_mode`` is
    :data:`ENTRY_MODE_ENHANCE_PRODUCTION`.
    """

    entry_mode: str
    created_utc: str
    source_repo: Optional[str] = None
    source_visibility: Optional[str] = None
    source_location: Optional[str] = None
    original_plugin_name: Optional[str] = None
    original_version: Optional[str] = None

    @classmethod
    def net_new(cls, created_utc: str) -> "ProvenanceRecord":
        """Return the provenance for a net-new draft started from an empty spec.

        This is the default provenance recorded when a draft is created without
        an explicit one (design Provenance_Record; Req 24.1, 24.2, 24.5).
        """
        return cls(entry_mode=ENTRY_MODE_CREATE_NEW, created_utc=created_utc)

    def to_dict(self) -> Dict[str, object]:
        """Serialize to a JSON-ready dict, omitting unset fork fields."""
        data: Dict[str, object] = {
            "entry_mode": self.entry_mode,
            "created_utc": self.created_utc,
        }
        for key in (
            "source_repo",
            "source_visibility",
            "source_location",
            "original_plugin_name",
            "original_version",
        ):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ProvenanceRecord":
        """Rebuild a :class:`ProvenanceRecord` from its serialized form."""
        return cls(
            entry_mode=str(data.get("entry_mode", "")),
            created_utc=str(data.get("created_utc", "")),
            source_repo=_opt_str(data.get("source_repo")),
            source_visibility=_opt_str(data.get("source_visibility")),
            source_location=_opt_str(data.get("source_location")),
            original_plugin_name=_opt_str(data.get("original_plugin_name")),
            original_version=_opt_str(data.get("original_version")),
        )


@dataclass(frozen=True)
class ToolingStamp:
    """The tool versions used for one build (design Data Models -> tooling.json)."""

    insight_plugin_cli: Optional[str] = None
    sdk_version: Optional[str] = None
    kiro_cli: Optional[str] = None
    spec_schema: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        """Serialize to a JSON-ready dict, omitting unset fields."""
        data: Dict[str, object] = {}
        for key in ("insight_plugin_cli", "sdk_version", "kiro_cli", "spec_schema"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ToolingStamp":
        """Rebuild a :class:`ToolingStamp` from its serialized form."""
        return cls(
            insight_plugin_cli=_opt_str(data.get("insight_plugin_cli")),
            sdk_version=_opt_str(data.get("sdk_version")),
            kiro_cli=_opt_str(data.get("kiro_cli")),
            spec_schema=_opt_str(data.get("spec_schema")),
        )


@dataclass(frozen=True)
class ProjectMetadata:
    """The contents of a project's ``.builder/project.json`` (Req 21.1, 21.2)."""

    plugin_name: str
    current_version: str
    created_utc: str
    last_modified_utc: str
    package_prefix: str
    provenance: Optional[ProvenanceRecord] = None

    def to_dict(self) -> Dict[str, object]:
        """Serialize to a JSON-ready dict."""
        data: Dict[str, object] = {
            "plugin_name": self.plugin_name,
            "current_version": self.current_version,
            "created_utc": self.created_utc,
            "last_modified_utc": self.last_modified_utc,
            "package_prefix": self.package_prefix,
        }
        if self.provenance is not None:
            data["provenance"] = self.provenance.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ProjectMetadata":
        """Rebuild :class:`ProjectMetadata` from its serialized form."""
        provenance_raw = data.get("provenance")
        provenance = ProvenanceRecord.from_dict(provenance_raw) if isinstance(provenance_raw, Mapping) else None
        return cls(
            plugin_name=str(data.get("plugin_name", "")),
            current_version=str(data.get("current_version", "")),
            created_utc=str(data.get("created_utc", "")),
            last_modified_utc=str(data.get("last_modified_utc", "")),
            package_prefix=str(data.get("package_prefix", DEFAULT_PACKAGE_PREFIX)),
            provenance=provenance,
        )


@dataclass(frozen=True)
class ProjectListing:
    """A single row returned when listing previously created plugins (Req 21.4)."""

    plugin_name: str
    current_version: str
    last_modified_utc: str


class ProjectFolder:
    """A per-plugin on-disk working tree and history store (Req 21).

    Construct via :meth:`create` for a new plugin or :meth:`open` for an existing
    one. The instance is a thin handle over the directory; every mutating call
    writes straight to disk and refreshes the ``last_modified_utc`` stamp.
    """

    def __init__(self, root: Union[str, Path]) -> None:
        """Wrap the directory at ``root`` (``<projects_root>/<plugin_name>``)."""
        self._root = Path(root)

    # --- Construction ------------------------------------------------------

    @classmethod
    def create(
        cls,
        projects_root: Union[str, Path],
        plugin_name: str,
        spec: PluginSpec,
        *,
        provenance: Optional[ProvenanceRecord] = None,
        package_prefix: str = DEFAULT_PACKAGE_PREFIX,
        created_utc: TimestampInput = None,
    ) -> "ProjectFolder":
        """Create a new Project_Folder and write its initial spec + metadata (Req 21.1).

        Args:
            projects_root: the directory under which per-plugin folders live.
            plugin_name: the plugin's snake_case name; also the directory name.
            spec: the initial :class:`PluginSpec` to persist as the source of truth.
            provenance: how the draft originated; stored in ``project.json``.
                When omitted, a net-new :class:`ProvenanceRecord` is recorded so
                every created draft carries provenance (Req 24.5).
            package_prefix: ``"icon"`` (current) or ``"komand"`` (legacy).
            created_utc: creation timestamp; defaults to the current UTC time.

        Returns:
            The created :class:`ProjectFolder`.

        Raises:
            ProjectFolderError: if ``plugin_name`` is empty, the prefix is
                invalid, the folder already exists, or the write fails.
        """
        if not plugin_name or not plugin_name.strip():
            raise ProjectFolderError("plugin_name must be a non-empty name")
        prefix = _validate_prefix(package_prefix)

        root = Path(projects_root) / plugin_name
        if root.exists():
            raise ProjectFolderError(f"project folder already exists: {root}")

        folder = cls(root)
        timestamp = _to_iso(created_utc)
        # Every created draft carries provenance; default to net-new when the
        # caller supplies none (Req 24.5). Iterate/enhance callers pass an
        # explicit record identifying their entry mode.
        recorded_provenance = provenance if provenance is not None else ProvenanceRecord.net_new(timestamp)
        try:
            folder._builder_dir().mkdir(parents=True, exist_ok=True)
            metadata = ProjectMetadata(
                plugin_name=plugin_name,
                current_version=str(_version_to_str(spec.version)),
                created_utc=timestamp,
                last_modified_utc=timestamp,
                package_prefix=prefix,
                provenance=recorded_provenance,
            )
            folder._write_metadata(metadata)
            folder._write_spec(spec)
        except OSError as error:
            raise ProjectFolderError(f"failed to create project folder {root}: {error}") from error
        return folder

    @classmethod
    def adopt(
        cls,
        root: Union[str, Path],
        plugin_name: str,
        spec: PluginSpec,
        *,
        provenance: Optional[ProvenanceRecord] = None,
        package_prefix: str = DEFAULT_PACKAGE_PREFIX,
        created_utc: TimestampInput = None,
    ) -> "ProjectFolder":
        """Adopt an already-scaffolded plugin directory as a Project_Folder.

        The counterpart to :meth:`create` for the deterministic-scaffolding path.
        ``insight-plugin create`` refuses to run if the plugin directory already
        exists, so the tree has to be scaffolded *first* and the tool's own
        ``.builder/`` metadata written into it afterwards -- which is what this
        does. :meth:`create` remains the entry point when there is no scaffold to
        adopt (an in-memory draft being persisted for the first time).

        The plugin's own files are left untouched: only ``.builder/`` metadata is
        written. The existing ``plugin.spec.yaml`` is rewritten from ``spec`` so
        the stored source of truth matches the draft the scaffold came from.

        Args:
            root: the existing plugin directory (``<projects_root>/<plugin_name>``).
            plugin_name: the plugin's snake_case name.
            spec: the :class:`PluginSpec` to persist as the source of truth.
            provenance: how the draft originated; defaults to a net-new record.
            package_prefix: the prefix the scaffold actually used. Pass the value
                detected from the tree rather than a default, so the recorded
                metadata cannot contradict what is on disk.
            created_utc: creation timestamp; defaults to the current UTC time.

        Returns:
            The adopted :class:`ProjectFolder`.

        Raises:
            ProjectFolderError: if ``plugin_name`` is empty, the prefix is
                invalid, ``root`` is not an existing directory, or a write fails.
        """
        if not plugin_name or not plugin_name.strip():
            raise ProjectFolderError("plugin_name must be a non-empty name")
        prefix = _validate_prefix(package_prefix)

        path = Path(root)
        if not path.is_dir():
            raise ProjectFolderError(f"cannot adopt a directory that does not exist: {path}")

        folder = cls(path)
        timestamp = _to_iso(created_utc)
        recorded_provenance = provenance if provenance is not None else ProvenanceRecord.net_new(timestamp)
        try:
            folder._builder_dir().mkdir(parents=True, exist_ok=True)
            metadata = ProjectMetadata(
                plugin_name=plugin_name,
                current_version=str(_version_to_str(spec.version)),
                created_utc=timestamp,
                last_modified_utc=timestamp,
                package_prefix=prefix,
                provenance=recorded_provenance,
            )
            folder._write_metadata(metadata)
            folder._write_spec(spec)
        except OSError as error:
            raise ProjectFolderError(f"failed to adopt project folder {path}: {error}") from error
        return folder

    @classmethod
    def open(cls, projects_root: Union[str, Path], plugin_name: str) -> "ProjectFolder":
        """Open an existing Project_Folder.

        Raises:
            ProjectFolderError: if the folder or its ``project.json`` is missing.
        """
        root = Path(projects_root) / plugin_name
        folder = cls(root)
        if not folder.metadata_path.exists():
            raise ProjectFolderError(f"no project folder for {plugin_name!r} at {root}")
        return folder

    # --- Paths -------------------------------------------------------------

    @property
    def path(self) -> Path:
        """The plugin's working-tree directory."""
        return self._root

    @property
    def plugin_name(self) -> str:
        """The plugin name (the working-tree directory's name)."""
        return self._root.name

    @property
    def spec_path(self) -> Path:
        """Path to ``plugin.spec.yaml`` (the source of truth)."""
        return self._root / SPEC_FILENAME

    @property
    def metadata_path(self) -> Path:
        """Path to ``.builder/project.json``."""
        return self._builder_dir() / PROJECT_METADATA_FILE

    @property
    def tooling_path(self) -> Path:
        """Path to ``.builder/tooling.json``."""
        return self._builder_dir() / TOOLING_FILE

    def _builder_dir(self) -> Path:
        return self._root / BUILDER_DIRNAME

    def package_dir(self) -> Path:
        """Path to the plugin's package tree (``<prefix>_<name>/``)."""
        metadata = self.metadata()
        return self._root / f"{metadata.package_prefix}_{self.plugin_name}"

    # --- Saving the current draft (Req 21.2) --------------------------------

    def save(
        self,
        spec: PluginSpec,
        *,
        package_source: Optional[Union[str, Path]] = None,
        help_md: Optional[str] = None,
        generated_files: Optional[Mapping[str, Union[str, bytes]]] = None,
        artifacts: Optional[Mapping[str, bytes]] = None,
        modified_utc: TimestampInput = None,
    ) -> ProjectMetadata:
        """Store the current spec, code, docs, and artifacts (Req 21.2).

        Persists the current :class:`PluginSpec`, and optionally the generated
        code tree, documentation, other generated files, and build artifacts,
        then updates the current version and the last-modification timestamp.

        Args:
            spec: the current draft spec to persist as the source of truth.
            package_source: optional directory whose contents are copied into the
                plugin's package tree (``<prefix>_<name>/``), replacing any prior
                contents.
            help_md: optional ``help.md`` documentation text.
            generated_files: optional mapping of repo-relative path -> content for
                other generated files (``Dockerfile``, ``Makefile``, ``setup.py``,
                ``.CHECKSUM``, ...).
            artifacts: optional mapping of filename -> bytes stored under
                ``.builder/artifacts/``.
            modified_utc: modification timestamp; defaults to the current UTC time.

        Returns:
            The updated :class:`ProjectMetadata`.

        Raises:
            ProjectFolderError: if any write fails; see the message for details.
        """
        try:
            self._builder_dir().mkdir(parents=True, exist_ok=True)
            self._write_spec(spec)

            if package_source is not None:
                self._copy_package(Path(package_source))
            if help_md is not None:
                (self._root / HELP_FILENAME).write_text(help_md, encoding="utf-8")
            if generated_files:
                self._write_generated(generated_files)
            if artifacts:
                self._write_artifacts(artifacts)

            metadata = self.metadata()
            updated = ProjectMetadata(
                plugin_name=metadata.plugin_name,
                current_version=str(_version_to_str(spec.version)),
                created_utc=metadata.created_utc,
                last_modified_utc=_to_iso(modified_utc),
                package_prefix=metadata.package_prefix,
                provenance=metadata.provenance,
            )
            self._write_metadata(updated)
        except OSError as error:
            raise ProjectFolderError(f"failed to save project {self.plugin_name!r}: {error}") from error
        return updated

    def save_artifact(self, filename: str, content: bytes) -> Path:
        """Store one build artifact under ``.builder/artifacts/`` and return its path.

        Raises:
            ProjectFolderError: if the write fails.
        """
        try:
            self._write_artifacts({filename: content})
        except OSError as error:
            raise ProjectFolderError(f"failed to store artifact {filename!r}: {error}") from error
        return self._artifacts_dir() / filename

    # --- Per-version history (Req 21.3 storage) -----------------------------

    def record_version(
        self,
        version: VersionInput,
        spec: PluginSpec,
        *,
        export_outcome: Optional[Mapping[str, object]] = None,
        tooling: Optional[ToolingStamp] = None,
    ) -> Path:
        """Snapshot a version's spec and export outcome under ``.builder/history/``.

        Writes ``history/<version>/plugin.spec.yaml`` (the exported/built spec at
        this version) and, when provided, ``export_outcome.json``. When a
        :class:`ToolingStamp` is given it is recorded in ``tooling.json`` keyed by
        the version.

        Args:
            version: the version this snapshot represents.
            spec: the spec as of this version.
            export_outcome: optional ``{target, timestamp_utc, result, message}``.
            tooling: optional per-build tool version stamp for this version.

        Returns:
            The ``history/<version>/`` directory path.

        Raises:
            ProjectFolderError: if any write fails.
        """
        version_str = _version_to_str(version)
        version_dir = self._history_dir() / version_str
        try:
            version_dir.mkdir(parents=True, exist_ok=True)
            (version_dir / SPEC_FILENAME).write_text(dump_plugin_spec(spec), encoding="utf-8")
            if export_outcome is not None:
                (version_dir / EXPORT_OUTCOME_FILE).write_text(
                    json.dumps(dict(export_outcome), indent=2, sort_keys=True),
                    encoding="utf-8",
                )
            if tooling is not None:
                self._stamp_tooling(version_str, tooling)
        except OSError as error:
            raise ProjectFolderError(
                f"failed to record version {version_str} for {self.plugin_name!r}: {error}"
            ) from error
        return version_dir

    def stamp_build_tooling(self, version: VersionInput, tooling: ToolingStamp) -> None:
        """Record the tool versions used for one build of ``version`` (Req 23.2).

        Writes the given :class:`ToolingStamp` into ``.builder/tooling.json`` keyed
        by the build version, without requiring a full history snapshot. This is
        the per-build stamp the Update_Manager writes so each build records the
        ``insight-plugin`` CLI and InsightConnect SDK versions actually used.

        Args:
            version: the version this build produced.
            tooling: the tool versions used for the build.

        Raises:
            ProjectFolderError: if the write fails.
        """
        version_str = _version_to_str(version)
        try:
            self._builder_dir().mkdir(parents=True, exist_ok=True)
            self._stamp_tooling(version_str, tooling)
        except OSError as error:
            raise ProjectFolderError(
                f"failed to stamp tooling for version {version_str} of {self.plugin_name!r}: {error}"
            ) from error

    def list_versions(self) -> List[str]:
        """Return every recorded history version, sorted oldest-to-newest."""
        history = self._history_dir()
        if not history.is_dir():
            return []
        versions = [child.name for child in history.iterdir() if child.is_dir()]
        versions.sort(key=_version_sort_key)
        return versions

    # --- Reads -------------------------------------------------------------

    def metadata(self) -> ProjectMetadata:
        """Read and return the project's :class:`ProjectMetadata`.

        Raises:
            ProjectFolderError: if ``project.json`` is missing or unreadable.
        """
        try:
            raw = self.metadata_path.read_text(encoding="utf-8")
        except OSError as error:
            raise ProjectFolderError(
                f"missing or unreadable project metadata for {self.plugin_name!r}: {error}"
            ) from error
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ProjectFolderError(f"corrupt project metadata for {self.plugin_name!r}: {error}") from error
        return ProjectMetadata.from_dict(data)

    def tooling(self) -> Dict[str, ToolingStamp]:
        """Return the per-version tooling stamps recorded in ``tooling.json``."""
        if not self.tooling_path.exists():
            return {}
        try:
            data = json.loads(self.tooling_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ProjectFolderError(f"unreadable tooling metadata for {self.plugin_name!r}: {error}") from error
        return {version: ToolingStamp.from_dict(stamp) for version, stamp in data.items() if isinstance(stamp, Mapping)}

    def listing(self) -> ProjectListing:
        """Return this plugin's listing row (name, version, last-modified) (Req 21.4)."""
        metadata = self.metadata()
        return ProjectListing(
            plugin_name=metadata.plugin_name,
            current_version=metadata.current_version,
            last_modified_utc=metadata.last_modified_utc,
        )

    # --- Internal write helpers -------------------------------------------

    def _write_spec(self, spec: PluginSpec) -> None:
        self.spec_path.write_text(dump_plugin_spec(spec), encoding="utf-8")

    def _write_metadata(self, metadata: ProjectMetadata) -> None:
        self.metadata_path.write_text(json.dumps(metadata.to_dict(), indent=2, sort_keys=True), encoding="utf-8")

    def _stamp_tooling(self, version: str, tooling: ToolingStamp) -> None:
        existing: Dict[str, object] = {}
        if self.tooling_path.exists():
            try:
                existing = json.loads(self.tooling_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = {}
        existing[version] = tooling.to_dict()
        self.tooling_path.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")

    def _copy_package(self, source: Path) -> None:
        if not source.is_dir():
            raise ProjectFolderError(f"package source is not a directory: {source}")
        destination = self._root / f"{self.metadata().package_prefix}_{self.plugin_name}"
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)

    def _write_generated(self, files: Mapping[str, Union[str, bytes]]) -> None:
        for relative_path, content in files.items():
            target = self._root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")

    def _write_artifacts(self, artifacts: Mapping[str, bytes]) -> None:
        artifacts_dir = self._artifacts_dir()
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in artifacts.items():
            (artifacts_dir / filename).write_bytes(content)

    def _artifacts_dir(self) -> Path:
        return self._builder_dir() / ARTIFACTS_DIRNAME

    def _history_dir(self) -> Path:
        return self._builder_dir() / HISTORY_DIRNAME


def list_projects(projects_root: Union[str, Path]) -> List[ProjectListing]:
    """List previously created plugins with name, version, last-modified (Req 21.4).

    Scans ``projects_root`` for per-plugin folders that carry a readable
    ``.builder/project.json`` and returns one :class:`ProjectListing` per plugin,
    ordered from most recently modified to least. A non-existent root yields an
    empty list; directories without valid metadata are skipped.

    Args:
        projects_root: the directory under which per-plugin folders live.

    Returns:
        The listings ordered most-recently-modified first.
    """
    root = Path(projects_root)
    if not root.is_dir():
        return []

    listings: List[ProjectListing] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        metadata_file = child / BUILDER_DIRNAME / PROJECT_METADATA_FILE
        if not metadata_file.exists():
            continue
        try:
            metadata = ProjectFolder(child).metadata()
        except ProjectFolderError:
            continue
        listings.append(
            ProjectListing(
                plugin_name=metadata.plugin_name,
                current_version=metadata.current_version,
                last_modified_utc=metadata.last_modified_utc,
            )
        )

    listings.sort(key=lambda item: _sort_key(item.last_modified_utc), reverse=True)
    return listings


# --- Module helpers --------------------------------------------------------


def _validate_prefix(prefix: str) -> str:
    """Return ``prefix`` if it is a recognized package prefix, else raise."""
    if prefix not in _VALID_PREFIXES:
        raise ProjectFolderError(f"package_prefix must be one of {_VALID_PREFIXES}, got {prefix!r}")
    return prefix


def _opt_str(value: object) -> Optional[str]:
    """Coerce a JSON value to ``Optional[str]`` (``None`` stays ``None``)."""
    return None if value is None else str(value)


def _version_to_str(version: VersionInput) -> str:
    """Normalize a version input to its string form."""
    return str(version)


def _version_sort_key(version: str) -> tuple:
    """Sort key that orders valid semver numerically and others lexically last."""
    try:
        semver = SemVer.parse(version)
        return (0, (semver.major, semver.minor, semver.patch), "")
    except (ValueError, AttributeError):
        return (1, (0, 0, 0), version)


def _to_iso(value: TimestampInput) -> str:
    """Normalize a timestamp input to an ISO-8601 UTC string.

    ``None`` uses the current UTC time; a naive datetime is assumed UTC; an aware
    datetime is converted to UTC; a string is stored verbatim.
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

    An unparseable value sorts as the oldest rather than crashing the ordering.
    """
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
