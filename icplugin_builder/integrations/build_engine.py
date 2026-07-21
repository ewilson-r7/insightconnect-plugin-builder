"""Packages a validated plugin project into a ``.plg`` artifact (the Build_Engine).

Req 9 (design "Build_Engine": ``package(project) -> PlgArtifact``) requires that
a plugin be packaged into a single ``.plg`` file that is a **gzipped tarball**
(Req 9.2) and that packaging happens **only when validation has passed** (Req
9.1, 9.4). The operation is atomic: on any packaging failure no partial artifact
is produced and the source plugin files are left unchanged (Req 9.5, design
Property 2).

Design notes:

* **Validation gating (Req 9.1, 9.4).** :meth:`BuildEngine.package` requires the
  caller to pass ``validation_passed=True``. When validation has not passed it
  raises :class:`ValidationNotPassedError` and produces no artifact. The full
  export-gating decision (spec-valid *and* all four code stages passed) lives in
  the orchestration layer (task 15.3); the Build_Engine enforces the local
  "validation passed" precondition at the packaging boundary.
* **Deterministic file set.** :func:`list_plugin_files` computes the exact set of
  files that will be included in the ``.plg`` -- the plugin working tree minus
  tool-only metadata (the ``.builder/`` subtree, which must never leak into the
  artifact, Req 14.3) and transient directories (``.git``, ``__pycache__`` ...).
  It is exposed separately so the export-preview file list (task 15.7) can be
  computed from the same source of truth the packager uses, guaranteeing the
  preview equals the packaged contents (design Property 30).
* **Atomicity (Req 9.5).** The tarball is written to a temporary file in the
  output directory and only ``os.replace``-d into its final path once it has
  been fully and successfully written. If anything fails mid-way the temporary
  file is removed, so no partial ``.plg`` is ever observable, and because the
  packager only *reads* the source tree the sources are untouched.
* **Round trip (design Property 6).** Members are stored under their relative
  POSIX paths, so extracting the ``.plg`` yields the same set of plugin files
  with identical contents.
"""

from __future__ import annotations

import os
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Union

__all__ = [
    "BuildEngineError",
    "ValidationNotPassedError",
    "PackagingError",
    "PlgArtifact",
    "BuildEngine",
    "ExportPreview",
    "list_plugin_files",
    "preview_export_files",
    "PLG_SUFFIX",
    "BUILDER_METADATA_DIR",
    "DEFAULT_ARTIFACT_SUBDIR",
    "VALIDATION_NOT_PASSED_MESSAGE",
]

#: The artifact file extension for a packaged plugin.
PLG_SUFFIX = ".plg"

#: The tool-only metadata directory that must never be packaged into a ``.plg``
#: (design "Project_Folder" ``.builder/``; Req 14.3).
BUILDER_METADATA_DIR = ".builder"

#: Where a produced artifact is placed by default, relative to the project root.
DEFAULT_ARTIFACT_SUBDIR = Path(BUILDER_METADATA_DIR) / "artifacts"

#: The error surfaced when packaging is requested before validation passed
#: (Req 9.4).
VALIDATION_NOT_PASSED_MESSAGE = "validation has not passed; the plugin cannot be packaged until validation succeeds"

#: Directory names skipped when collecting plugin files, mirroring the CLI
#: wrapper's snapshot behavior plus the ``.builder/`` metadata subtree.
_EXCLUDED_DIRS = frozenset({BUILDER_METADATA_DIR, ".git", "__pycache__", ".pytest_cache", ".mypy_cache"})

#: Accepted path inputs.
PathInput = Union[str, Path]


class BuildEngineError(Exception):
    """Base class for Build_Engine failures."""


class ValidationNotPassedError(BuildEngineError):
    """Raised when packaging is requested but validation has not passed (Req 9.4)."""


class PackagingError(BuildEngineError):
    """Raised when packaging fails after validation passed (Req 9.5).

    When this is raised no partial ``.plg`` exists and the source plugin files
    are unchanged.
    """


@dataclass(frozen=True)
class PlgArtifact:
    """A produced ``.plg`` artifact.

    Attributes:
        path: The absolute path of the written gzipped-tarball ``.plg``.
        files: The relative POSIX member paths included in the archive, sorted.
    """

    path: Path
    files: Tuple[str, ...]

    @property
    def name(self) -> str:
        """The artifact file name (e.g. ``my_plugin-1.0.0.plg``)."""
        return self.path.name


def _resolve_root(project: object) -> Path:
    """Resolve ``project`` to a working-tree path.

    Accepts a path-like value directly, or any object exposing a ``root``
    attribute (e.g. a
    :class:`~icplugin_builder.integrations.insight_plugin_cli.ProjectTree`), so
    the Build_Engine composes with the CLI wrapper output. Path-likes are checked
    first because :class:`pathlib.Path` exposes an unrelated ``root`` anchor.
    """
    if isinstance(project, (str, os.PathLike)):
        return Path(project)
    root = getattr(project, "root", project)
    return Path(root)


def list_plugin_files(project: object) -> List[str]:
    """Return the exact set of files that will be included in the ``.plg``.

    Walks ``project``'s working tree and returns every regular file as a
    relative POSIX path, excluding the tool-only ``.builder/`` metadata subtree
    (Req 14.3) and transient directories (``.git``, ``__pycache__`` ...). The
    result is sorted for determinism.

    This is the single source of truth for "what goes into the artifact"; the
    packager and the export preview (task 15.7) both consume it so the preview
    equals the packaged contents (design Property 30).

    Args:
        project: A plugin working tree -- a path, or an object with a ``root``
            attribute.

    Returns:
        Sorted relative POSIX file paths destined for the ``.plg``.

    Raises:
        PackagingError: If the project directory does not exist.
    """
    root = _resolve_root(project)
    if not root.is_dir():
        raise PackagingError(f"project directory does not exist: {root}")

    files: List[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _EXCLUDED_DIRS for part in relative.parts):
            continue
        files.append(relative.as_posix())
    return sorted(files)


@dataclass(frozen=True)
class ExportPreview:
    """The file-list preview shown to the user before an export (Req 16.2).

    The preview names exactly the files that will be included in the ``.plg`` if
    the export proceeds, so the author can confirm the contents before the
    plugin leaves the tool. Because :attr:`files` is derived from the same
    :func:`list_plugin_files` source of truth the packager consumes, the preview
    equals the packaged contents (design Property 30).

    Attributes:
        files: The relative POSIX member paths destined for the ``.plg``, sorted
            for a stable, deterministic display. This matches
            :attr:`PlgArtifact.files` for the same project.
    """

    files: Tuple[str, ...]

    @property
    def count(self) -> int:
        """The number of files that will be included in the ``.plg``."""
        return len(self.files)

    @property
    def is_empty(self) -> bool:
        """Return ``True`` iff no files would be packaged."""
        return not self.files

    def __iter__(self):
        """Iterate the previewed member paths in sorted order."""
        return iter(self.files)

    def __contains__(self, path: object) -> bool:
        """Return ``True`` iff ``path`` is among the previewed member paths."""
        return path in self.files


def preview_export_files(project: object) -> ExportPreview:
    """Return the export preview file list for ``project`` (Req 16.2).

    Computes the exact set of files that will be included in the ``.plg`` by
    reusing :func:`list_plugin_files` -- the single source of truth the packager
    itself consumes -- so the preview list is guaranteed to equal the packaged
    contents (design Property 30). This never writes an artifact or mutates the
    working tree; it only reads the source tree to enumerate its members.

    Args:
        project: A plugin working tree -- a path, or an object with a ``root``
            attribute.

    Returns:
        An :class:`ExportPreview` naming, in sorted order, the files that would
        be packaged into the ``.plg``.

    Raises:
        PackagingError: If the project directory does not exist.
    """
    return ExportPreview(files=tuple(list_plugin_files(project)))


class BuildEngine:
    """Packages a validated plugin project into a gzipped-tarball ``.plg`` (Req 9).

    The engine is read-only with respect to the source tree: it only reads the
    plugin files and writes the artifact, so a failing packaging run leaves the
    sources unchanged (Req 9.5).
    """

    def package(
        self,
        project: object,
        *,
        validation_passed: bool,
        output_dir: Optional[PathInput] = None,
        artifact_name: Optional[str] = None,
    ) -> PlgArtifact:
        """Package ``project`` into a single ``.plg`` gzipped tarball (Req 9.1, 9.2).

        Packaging proceeds only when ``validation_passed`` is ``True`` (Req 9.1,
        9.4). The archive is written atomically: it is built in a temporary file
        and only moved into place on full success, so on any failure no partial
        artifact exists and the sources are untouched (Req 9.5).

        Args:
            project: A plugin working tree -- a path, or an object with a
                ``root`` attribute.
            validation_passed: Whether validation succeeded for this plugin. When
                ``False`` no artifact is produced.
            output_dir: Directory to write the ``.plg`` into. Defaults to
                ``<root>/.builder/artifacts`` (excluded from the archive itself).
            artifact_name: The artifact file name. Defaults to ``<root name>.plg``;
                a ``.plg`` suffix is appended when absent.

        Returns:
            A :class:`PlgArtifact` describing the written artifact and its members.

        Raises:
            ValidationNotPassedError: If ``validation_passed`` is ``False`` (Req 9.4).
            PackagingError: If packaging fails; no partial artifact is left and
                the sources are unchanged (Req 9.5).
        """
        if not validation_passed:
            raise ValidationNotPassedError(VALIDATION_NOT_PASSED_MESSAGE)

        root = _resolve_root(project)
        # Compute the member set first; this also validates the project exists.
        members = list_plugin_files(root)

        destination_dir = Path(output_dir) if output_dir is not None else root / DEFAULT_ARTIFACT_SUBDIR
        final_name = artifact_name if artifact_name else root.name + PLG_SUFFIX
        if not final_name.endswith(PLG_SUFFIX):
            final_name += PLG_SUFFIX
        final_path = destination_dir / final_name

        try:
            destination_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise PackagingError(f"failed to prepare output directory {destination_dir}: {error}") from error

        temp_path = self._write_archive(root, members, destination_dir)
        try:
            os.replace(temp_path, final_path)
        except OSError as error:
            _safe_unlink(temp_path)
            raise PackagingError(f"failed to finalize artifact at {final_path}: {error}") from error

        return PlgArtifact(path=final_path.resolve(), files=tuple(members))

    def _write_archive(self, root: Path, members: List[str], destination_dir: Path) -> Path:
        """Write ``members`` of ``root`` into a temp gzipped tarball; return its path.

        On any failure the partially written temporary file is removed and a
        :class:`PackagingError` is raised, so no observable partial artifact
        remains (Req 9.5). The temporary file is created in ``destination_dir``
        so the subsequent :func:`os.replace` is an atomic same-filesystem rename.
        """
        fd, temp_name = tempfile.mkstemp(prefix=".plg-", suffix=".tmp", dir=str(destination_dir))
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            with tarfile.open(temp_path, mode="w:gz") as archive:
                for member in members:
                    archive.add(str(root / member), arcname=member, recursive=False)
        except Exception as error:  # noqa: BLE001 -- any failure must leave no partial artifact.
            _safe_unlink(temp_path)
            raise PackagingError(f"failed to package plugin at {root}: {error}") from error
        return temp_path


def _safe_unlink(path: Path) -> None:
    """Remove ``path`` if present, ignoring a missing file (best-effort cleanup)."""
    try:
        path.unlink()
    except FileNotFoundError:  # pragma: no cover - already gone
        pass
    except OSError:  # pragma: no cover - best-effort cleanup
        pass
