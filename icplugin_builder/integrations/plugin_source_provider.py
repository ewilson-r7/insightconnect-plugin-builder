"""Plugin_Source_Provider: read-only import of production plugins (Req 24.4, 25).

The "enhance an existing production plugin" entry mode forks a plugin from a
configured :class:`~icplugin_builder.api.config.ProductionSourceConfig` -- the
public ``rapid7/insightconnect-plugins`` or the private ``komand-plugins``
repository -- into a fresh custom lineage without ever writing back to the
source (design "Plugin_Source_Provider"; Req 25.3, Property 47).

Each source is resolved **local clone first, remote GitHub fallback second**
(Req 25.1, 25.2). Enumerating and fetching plugins from a remote is delegated to
a pluggable :class:`RemotePluginFetcher` so the network/git surface is mockable
in tests; the local-clone path reads directly from the filesystem.

:meth:`PluginSourceProvider.import_plugin` performs the fork:

* copies the selected plugin directory into a **new** ``Project_Folder`` and
  never modifies the source (read-only invariant, Req 25.3);
* applies the ``_custom`` vendor suffix while retaining the original plugin name
  (Req 25.4);
* records a :class:`~icplugin_builder.persistence.project_folder.ProvenanceRecord`
  capturing the entry mode, source repository, original name, and original
  version (Req 24.5, 25.4);
* preserves the original license/attribution references in ``resources``
  (Req 25.5) -- these survive because the whole plugin tree is copied and the
  spec's unmodeled keys round-trip through the typed model;
* detects and records the package prefix, accepting both the current ``icon_``
  and legacy ``komand_`` prefixes (Req 25.7);
* stores an immutable ``.builder/baseline/`` snapshot of the imported production
  spec and code for later fork-baseline diffs (Req 25.8); and
* flags a private-source usage-restriction notice when the source is the private
  repository (Req 25.6).

If a required git credential for the private repository is missing on a remote
fetch, the fetch is rejected before any network call with a
:class:`GitCredentialRequiredError` (Req 25.9). If the selected plugin cannot be
read or does not conform to the plugin-spec schema, a :class:`PluginImportError`
is raised and no partial draft is created (Req 25.10).
"""

from __future__ import annotations

import copy
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

from icplugin_builder.api.config import ProductionSourceConfig
from icplugin_builder.core.diff import FileTreeDiff, diff_file_trees
from icplugin_builder.core.spec_model import PluginSpec
from icplugin_builder.core.vendor import apply_custom_vendor_suffix
from icplugin_builder.core.yaml_codec import load_plugin_spec
from icplugin_builder.persistence.credential_store import CredentialStore, CredentialStoreError
from icplugin_builder.persistence.project_folder import (
    BUILDER_DIRNAME,
    ENTRY_MODE_ENHANCE_PRODUCTION,
    SPEC_FILENAME,
    ProjectFolder,
    ProjectFolderError,
    ProvenanceRecord,
)

try:  # Python 3.8+ typing.Protocol; runtime-checkable for isinstance-free duck typing.
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover - Protocol is in 3.8+, project targets 3.11
    from typing_extensions import Protocol, runtime_checkable  # type: ignore

__all__ = [
    "PluginSourceError",
    "SourceNotFoundError",
    "PluginNotFoundError",
    "GitCredentialRequiredError",
    "PluginImportError",
    "BaselineNotFoundError",
    "RemotePluginFetcher",
    "SourceAvailability",
    "ProductionPluginRef",
    "ImportResult",
    "PluginSourceProvider",
    "baseline_diff",
    "ENHANCE_PRODUCTION_ENTRY_MODE",
    "PRIVATE_SOURCE_NOTICE",
    "BASELINE_DIRNAME",
]

#: The provenance entry mode recorded for a production fork (design Provenance_Record).
#: Aliased to the shared model constant so the value has a single source of truth.
ENHANCE_PRODUCTION_ENTRY_MODE = ENTRY_MODE_ENHANCE_PRODUCTION

#: Subdirectory under ``.builder/`` holding the immutable production-fork baseline.
BASELINE_DIRNAME = "baseline"

#: The usage-restriction notice surfaced when a plugin is imported from the
#: private production repository (Req 25.6).
PRIVATE_SOURCE_NOTICE = (
    "This plugin was imported from a private source repository and is subject to "
    "that repository's usage restrictions."
)

#: The recognized production package prefixes (current ``icon_``, legacy ``komand_``).
_PACKAGE_PREFIXES = ("icon", "komand")

#: Directory names never copied from a source (VCS/build noise).
_SKIP_TREE_NAMES = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache", BUILDER_DIRNAME})


class PluginSourceError(Exception):
    """Base class for production-source resolution and import failures."""


class SourceNotFoundError(PluginSourceError):
    """Raised when a requested production source id is not configured."""


class PluginNotFoundError(PluginSourceError):
    """Raised when a requested plugin is absent from a source (local and remote)."""


class GitCredentialRequiredError(PluginSourceError):
    """Raised when a private-repo remote fetch is attempted without a git credential (Req 25.9)."""


class PluginImportError(PluginSourceError):
    """Raised when a selected plugin cannot be read or does not conform to the schema (Req 25.10)."""


class BaselineNotFoundError(PluginSourceError):
    """Raised when a fork baseline diff is requested but no ``.builder/baseline/`` snapshot exists.

    Only production forks store a baseline (Req 25.8); a net-new or
    custom-iteration draft has nothing to diff against.
    """


@runtime_checkable
class RemotePluginFetcher(Protocol):
    """Resolves plugins from a remote production source (the GitHub fallback).

    Implementations perform the git/sparse-fetch work for the large monorepo.
    Both methods receive the resolved git credential (or ``None`` for a public
    source) so the provider owns the credential lookup and the "missing
    credential" rejection (Req 25.2, 25.9).
    """

    def list_plugins(self, source: ProductionSourceConfig, *, credential: Optional[str]) -> Sequence[str]:
        """Return the plugin directory names available in the remote ``source``."""
        ...

    def fetch_plugin(
        self,
        source: ProductionSourceConfig,
        name: str,
        destination: Path,
        *,
        credential: Optional[str],
    ) -> None:
        """Fetch plugin ``name`` from the remote ``source`` into ``destination``.

        The plugin's files (``plugin.spec.yaml`` and the ``<prefix>_<name>/``
        package tree) are written directly under ``destination``.
        """
        ...


@dataclass(frozen=True)
class SourceAvailability:
    """A configured production source and how it can be resolved (Req 25.1)."""

    source: ProductionSourceConfig
    local_available: bool
    remote_available: bool

    @property
    def id(self) -> str:
        """The source's configured id."""
        return self.source.id


@dataclass(frozen=True)
class ProductionPluginRef:
    """A plugin selectable from a production source."""

    name: str
    package_prefix: Optional[str] = None


@dataclass(frozen=True)
class ImportResult:
    """The outcome of importing (forking) a production plugin."""

    project_folder: ProjectFolder
    provenance: ProvenanceRecord
    package_prefix: str
    source_location: str
    private_source_notice: Optional[str] = None


class PluginSourceProvider:
    """Lists and read-only-imports production plugins for the enhance entry mode.

    Construct with the configured production sources, the projects root where
    forked ``Project_Folder``s are created, and -- optionally -- a
    :class:`~icplugin_builder.persistence.credential_store.CredentialStore` for
    private-repo git credentials and a :class:`RemotePluginFetcher` for the
    remote GitHub fallback. When no fetcher is supplied, only local-clone
    resolution is available and any remote fallback raises.
    """

    def __init__(
        self,
        sources: Sequence[ProductionSourceConfig],
        projects_root: Union[str, Path],
        *,
        credential_store: Optional[CredentialStore] = None,
        remote_fetcher: Optional[RemotePluginFetcher] = None,
    ) -> None:
        """Configure the provider.

        Args:
            sources: The configured production sources (from ``AppConfig``).
            projects_root: Directory under which forked ``Project_Folder``s live.
            credential_store: Store holding the private-repo git credential; only
                consulted for a private source's remote fallback (Req 25.2).
            remote_fetcher: Resolver for the remote GitHub fallback; when absent,
                only local clones can be read.
        """
        self._sources: Dict[str, ProductionSourceConfig] = {source.id: source for source in sources}
        self._projects_root = Path(projects_root)
        self._credential_store = credential_store
        self._remote_fetcher = remote_fetcher

    # -- source/plugin discovery -------------------------------------------

    def list_sources(self) -> List[SourceAvailability]:
        """Return the configured sources with local/remote availability (Req 25.1)."""
        result: List[SourceAvailability] = []
        for source in self._sources.values():
            local_dir = self._local_clone_dir(source)
            result.append(
                SourceAvailability(
                    source=source,
                    local_available=local_dir is not None and local_dir.is_dir(),
                    remote_available=bool(source.remote_url) and self._remote_fetcher is not None,
                )
            )
        return result

    def list_plugins(self, source: Union[str, ProductionSourceConfig]) -> List[ProductionPluginRef]:
        """Enumerate the plugins available in ``source`` (Req 25.1, 25.2).

        Reads the configured local clone when reachable; otherwise falls back to
        the remote source via the :class:`RemotePluginFetcher`, using the stored
        git credential for a private repository.

        Args:
            source: A source id or a :class:`ProductionSourceConfig`.

        Returns:
            The plugins in the source, each with its detected package prefix
            (local clone) or name only (remote).

        Raises:
            SourceNotFoundError: if ``source`` names an unconfigured source.
            GitCredentialRequiredError: if a private remote lookup lacks its
                git credential (Req 25.9).
            PluginSourceError: if no local clone is reachable and no remote
                fetcher is configured.
        """
        resolved = self._resolve_source(source)

        local_dir = self._local_clone_dir(resolved)
        if local_dir is not None and local_dir.is_dir():
            return self._list_local_plugins(local_dir)

        credential = self._require_remote_credential(resolved)
        fetcher = self._require_fetcher(resolved)
        names = fetcher.list_plugins(resolved, credential=credential)
        return [ProductionPluginRef(name=name) for name in names]

    # -- import (fork) ------------------------------------------------------

    def import_plugin(self, source: Union[str, ProductionSourceConfig], name: str) -> ImportResult:
        """Fork the production plugin ``name`` into a new ``Project_Folder`` (Req 25.3-25.8).

        Resolves the plugin local-clone-first / remote-fallback-second, copies it
        into a new ``Project_Folder`` without touching the source, applies the
        ``_custom`` vendor suffix (retaining the original name), records
        provenance, preserves the license/attribution in ``resources``, detects
        and records the package prefix, and stores a read-only
        ``.builder/baseline/`` snapshot for later fork-baseline diffs.

        Args:
            source: A source id or a :class:`ProductionSourceConfig`.
            name: The plugin directory name to import from the source.

        Returns:
            An :class:`ImportResult` describing the created fork.

        Raises:
            SourceNotFoundError: if ``source`` is not configured.
            PluginNotFoundError: if the plugin is absent locally and remotely.
            GitCredentialRequiredError: if a private remote fetch lacks its
                git credential (Req 25.9).
            PluginImportError: if the plugin is unreadable or non-conforming; no
                partial draft is created (Req 25.10).
        """
        resolved = self._resolve_source(source)

        temp_dir: Optional[str] = None
        try:
            plugin_dir, location, temp_dir = self._resolve_plugin_dir(resolved, name)
            return self._fork_from_directory(resolved, plugin_dir, location)
        finally:
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def baseline_diff(self, project: ProjectFolder) -> FileTreeDiff:
        """Diff the current draft against its stored production baseline (Req 25.8).

        Thin instance-level wrapper over the module-level :func:`baseline_diff`;
        see it for the full contract. Requires ``project`` to be a production
        fork carrying a ``.builder/baseline/`` snapshot.
        """
        return baseline_diff(project)

    # -- plugin-dir resolution ---------------------------------------------

    def _resolve_plugin_dir(
        self,
        source: ProductionSourceConfig,
        name: str,
    ) -> tuple[Path, str, Optional[str]]:
        """Resolve the on-disk directory of plugin ``name`` for ``source``.

        Returns ``(plugin_dir, source_location, temp_dir_to_clean)``. When the
        plugin is fetched from the remote, ``temp_dir_to_clean`` is the temporary
        directory the caller must remove; it is ``None`` for a local clone.
        """
        local_dir = self._local_clone_dir(source)
        if local_dir is not None and local_dir.is_dir():
            candidate = local_dir / name
            if candidate.is_dir():
                return candidate, "local_clone", None

        # No reachable local clone, or the plugin is absent locally: fall back to
        # the remote source (Req 25.2).
        credential = self._require_remote_credential(source)
        fetcher = self._require_fetcher(source)

        temp_dir = tempfile.mkdtemp(prefix="icpb-import-")
        destination = Path(temp_dir) / name
        destination.mkdir(parents=True, exist_ok=True)
        try:
            fetcher.fetch_plugin(source, name, destination, credential=credential)
        except PluginSourceError:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        except Exception as error:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise PluginNotFoundError(
                f"could not fetch plugin {name!r} from remote source {source.id!r}: {error}"
            ) from error

        if not any(destination.iterdir()):
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise PluginNotFoundError(f"plugin {name!r} not found in source {source.id!r}")
        return destination, "remote_github", temp_dir

    # -- fork construction --------------------------------------------------

    def _fork_from_directory(
        self,
        source: ProductionSourceConfig,
        plugin_dir: Path,
        location: str,
    ) -> ImportResult:
        """Create the forked ``Project_Folder`` from a resolved plugin directory.

        Parsing/validation happens before the folder is created so a
        non-conforming plugin never leaves a partial draft (Req 25.10).
        """
        original_spec = self._read_spec(plugin_dir)
        package_prefix = self._detect_package_prefix(plugin_dir)

        # The fork keeps the original name; the Project_Folder is named by it.
        plugin_name = original_spec.name or plugin_dir.name
        original_version = str(original_spec.version)

        provenance = ProvenanceRecord(
            entry_mode=ENHANCE_PRODUCTION_ENTRY_MODE,
            created_utc=_now_iso(),
            source_repo=source.repo,
            source_visibility=source.visibility,
            source_location=location,
            original_plugin_name=plugin_name,
            original_version=original_version,
        )

        # Apply the _custom vendor suffix while retaining the original name (Req 25.4).
        fork_spec = copy.deepcopy(original_spec)
        fork_spec.vendor = apply_custom_vendor_suffix(original_spec.vendor)

        try:
            folder = ProjectFolder.create(
                self._projects_root,
                plugin_name,
                fork_spec,
                provenance=provenance,
                package_prefix=package_prefix,
            )
        except ProjectFolderError as error:
            raise PluginImportError(f"could not create project folder for {plugin_name!r}: {error}") from error

        # From here on a failure would leave a partial draft, so clean up the
        # created folder if copying the tree or baseline fails (Req 25.10).
        try:
            # Copy the whole plugin tree (code, docs, resources, tests, license)
            # except the spec, which the Project_Folder already wrote with the
            # _custom vendor applied. Copying the tree preserves the license and
            # attribution files/resources verbatim (Req 25.5).
            self._copy_tree(plugin_dir, folder.path, skip_top_level={SPEC_FILENAME})

            # Store an immutable read-only baseline snapshot (original spec +
            # code) for later fork-baseline diffs (Req 25.8).
            baseline_dir = folder.path / BUILDER_DIRNAME / BASELINE_DIRNAME
            self._copy_tree(plugin_dir, baseline_dir, skip_top_level=frozenset())
        except OSError as error:
            shutil.rmtree(folder.path, ignore_errors=True)
            raise PluginImportError(f"failed to copy plugin {plugin_name!r} into project folder: {error}") from error

        notice = PRIVATE_SOURCE_NOTICE if source.visibility == "private" else None
        return ImportResult(
            project_folder=folder,
            provenance=provenance,
            package_prefix=package_prefix,
            source_location=location,
            private_source_notice=notice,
        )

    # -- helpers ------------------------------------------------------------

    def _resolve_source(self, source: Union[str, ProductionSourceConfig]) -> ProductionSourceConfig:
        """Return the configured :class:`ProductionSourceConfig` for ``source``."""
        if isinstance(source, ProductionSourceConfig):
            return source
        resolved = self._sources.get(source)
        if resolved is None:
            raise SourceNotFoundError(f"no configured production source with id {source!r}")
        return resolved

    def _local_clone_dir(self, source: ProductionSourceConfig) -> Optional[Path]:
        """Return the expanded local-clone path for ``source``, or ``None`` when unset."""
        if not source.local_path:
            return None
        return Path(os.path.expanduser(source.local_path))

    def _require_fetcher(self, source: ProductionSourceConfig) -> RemotePluginFetcher:
        """Return the remote fetcher, or raise when the remote fallback is unavailable."""
        if self._remote_fetcher is None:
            raise PluginSourceError(
                f"source {source.id!r} has no reachable local clone and no remote fetcher is configured"
            )
        return self._remote_fetcher

    def _require_remote_credential(self, source: ProductionSourceConfig) -> Optional[str]:
        """Resolve the git credential for a remote fetch, enforcing Req 25.9.

        A public source needs no credential. A private source must have a
        credential available in the ``Credential_Store``; when it is missing the
        fetch is rejected before any network call (Req 25.9).
        """
        if source.visibility != "private":
            return self._lookup_credential(source)

        credential = self._lookup_credential(source)
        if not credential:
            raise GitCredentialRequiredError(f"a git credential is required to fetch the private source {source.id!r}")
        return credential

    def _lookup_credential(self, source: ProductionSourceConfig) -> Optional[str]:
        """Return the stored git credential for ``source``, or ``None`` when absent."""
        if not source.git_credential_id or self._credential_store is None:
            return None
        if not self._credential_store.has(source.git_credential_id):
            return None
        try:
            return self._credential_store.retrieve(source.git_credential_id)
        except CredentialStoreError:
            return None

    def _list_local_plugins(self, clone_dir: Path) -> List[ProductionPluginRef]:
        """List plugin directories under a local clone (those with a spec file)."""
        refs: List[ProductionPluginRef] = []
        for child in sorted(clone_dir.iterdir()):
            if not child.is_dir() or child.name in _SKIP_TREE_NAMES:
                continue
            if not (child / SPEC_FILENAME).is_file():
                continue
            prefix = self._safe_detect_prefix(child)
            refs.append(ProductionPluginRef(name=child.name, package_prefix=prefix))
        return refs

    def _read_spec(self, plugin_dir: Path) -> PluginSpec:
        """Read and parse ``plugin.spec.yaml`` from ``plugin_dir`` (Req 25.10)."""
        spec_path = plugin_dir / SPEC_FILENAME
        try:
            text = spec_path.read_text(encoding="utf-8")
        except OSError as error:
            raise PluginImportError(f"could not read {SPEC_FILENAME} in {plugin_dir}: {error}") from error
        try:
            return load_plugin_spec(text)
        except ValueError as error:
            raise PluginImportError(
                f"plugin at {plugin_dir} does not conform to the plugin spec schema: {error}"
            ) from error

    def _detect_package_prefix(self, plugin_dir: Path) -> str:
        """Detect ``icon``/``komand`` package prefix, raising if absent (Req 25.7, 25.10)."""
        prefix = self._safe_detect_prefix(plugin_dir)
        if prefix is None:
            raise PluginImportError(
                f"plugin at {plugin_dir} has no recognized package directory "
                f"(expected an 'icon_' or 'komand_' prefix)"
            )
        return prefix

    @staticmethod
    def _safe_detect_prefix(plugin_dir: Path) -> Optional[str]:
        """Return the package prefix (``icon``/``komand``) of ``plugin_dir`` or ``None``."""
        for child in sorted(plugin_dir.iterdir()):
            if not child.is_dir():
                continue
            for prefix in _PACKAGE_PREFIXES:
                if child.name.startswith(f"{prefix}_"):
                    return prefix
        return None

    @staticmethod
    def _copy_tree(source: Path, destination: Path, *, skip_top_level: frozenset) -> None:
        """Copy ``source``'s contents into ``destination`` (read-only on ``source``).

        Skips VCS/build-noise directories everywhere and the named top-level
        entries in ``skip_top_level``. ``destination`` is created if needed. The
        source is only read, never written (read-only invariant, Req 25.3).
        """
        destination.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            if child.name in _SKIP_TREE_NAMES or child.name in skip_top_level:
                continue
            target = destination / child.name
            if child.is_dir():
                shutil.copytree(
                    child,
                    target,
                    ignore=shutil.ignore_patterns(*_SKIP_TREE_NAMES),
                    dirs_exist_ok=True,
                )
            else:
                shutil.copy2(child, target)


def baseline_diff(project: ProjectFolder) -> FileTreeDiff:
    """Diff a fork's current draft against its stored production baseline (Req 25.8).

    Computes the added/removed/modified file partition between the current draft
    working tree and the immutable ``.builder/baseline/`` snapshot captured when
    the production plugin was forked. This is deliberately independent of the
    prior-*exported*-version diff of Req 16 (which compares against the version
    recorded in the ``Plugin_Registry``/``history/``): the baseline stays fixed
    at the original production import for the lifetime of the fork, so the diff
    always reflects everything the user has changed since the fork.

    The tool-owned ``.builder/`` subtree (which holds the baseline itself, the
    project metadata, history, and artifacts) is excluded from the draft tree so
    it never appears as spurious additions.

    Args:
        project: the forked :class:`ProjectFolder` to diff.

    Returns:
        A :class:`~icplugin_builder.core.diff.FileTreeDiff` whose ``added``,
        ``removed``, and ``modified`` sets are the set-difference of the draft
        tree against the baseline tree.

    Raises:
        BaselineNotFoundError: if ``project`` has no ``.builder/baseline/``
            snapshot (i.e. it is not a production fork).
    """
    baseline_dir = project.path / BUILDER_DIRNAME / BASELINE_DIRNAME
    if not baseline_dir.is_dir():
        raise BaselineNotFoundError(
            f"no baseline snapshot for {project.plugin_name!r}; baseline diff applies only to production forks"
        )

    baseline_tree = _read_file_tree(baseline_dir)
    draft_tree = _read_file_tree(project.path)
    return diff_file_trees(baseline_tree, draft_tree)


def _read_file_tree(root: Path) -> Dict[str, Union[str, bytes]]:
    """Read every file under ``root`` into a path -> content mapping.

    Keys are POSIX-relative paths; values are UTF-8 ``str`` when decodable and
    raw ``bytes`` otherwise so binary resources compare intact. VCS/build noise
    and the tool-owned ``.builder/`` subtree (both in ``_SKIP_TREE_NAMES``) are
    skipped at any depth, matching the read-only import's copy semantics.
    """
    tree: Dict[str, Union[str, bytes]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _SKIP_TREE_NAMES for part in relative.parts):
            continue
        raw = path.read_bytes()
        try:
            tree[relative.as_posix()] = raw.decode("utf-8")
        except UnicodeDecodeError:
            tree[relative.as_posix()] = raw
    return tree


def _now_iso() -> str:
    """Return the current time as an ISO-8601 UTC string."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
