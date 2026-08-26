"""Packages a validated plugin project into a ``.plg`` artifact (the Build_Engine).

A ``.plg`` is a gzipped ``docker save`` of the plugin's **image**, tagged
``<vendor>/<name>:<version>``. A tenant loads that image on import; the plugin's code
and its ``plugin.spec.yaml`` travel inside the layers. Packaging therefore needs a
Docker daemon, and happens **only when validation has passed** (Req 9.1, 9.4). The
operation is atomic: on any failure no partial artifact is produced and the plugin's
source files are left unchanged (Req 9.5, design Property 2).

This module previously produced a gzipped tarball of the plugin's *source tree*. It
read Requirement 9.2's "a gzipped tarball containing the built plugin" as "containing
the plugin's files", which is a coherent reading and the wrong one -- no artifact this
tool produced could be imported. The clause now says which. See
``.kiro/specs/plg-artifact-is-an-image/bugfix.md`` for the measurements.

Design notes:

* **Validation gating (Req 9.1, 9.4).** :meth:`BuildEngine.package` requires the
  caller to pass ``validation_passed=True``. When validation has not passed it
  raises :class:`ValidationNotPassedError` and produces no artifact. The full
  export-gating decision (spec-valid *and* all four code stages passed) lives in
  the orchestration layer; the Build_Engine enforces the local "validation passed"
  precondition at the packaging boundary.
* **Identity comes from disk (Req 13).** :func:`read_plugin_identity` reads the vendor,
  name and version from the tree's own ``plugin.spec.yaml`` and applies the ``_custom``
  vendor suffix idempotently. Read rather than passed in, so the tag on the artifact and
  the spec shipping inside it cannot disagree. A missing or unparseable spec fails
  packaging: a guessed tag would produce an artifact that imports as the wrong plugin.
* **Built here, not by the toolchain.** The engine drives ``docker build`` and
  ``docker save`` itself. ``insight-plugin export`` does the same job but treats any
  stdout from a successful build as a failure, so it cannot run on a host without
  working buildx -- ``bugfix.md`` 1.5 and 2.2 record the finding and the tradeoff.
* **Atomicity (Req 9.5).** The archive is compressed into a temporary file in the output
  directory and only ``os.replace``-d into its final path once fully written. If
  anything fails mid-way the temporary file and the intermediate tar are removed, so no
  partial ``.plg`` is ever observable. The engine only reads the plugin's sources, so
  they are untouched: the image is built from a staged copy of the packaged file set, not
  from the plugin's directory, so nothing there is added, moved or removed.
* **What went into the image.** :func:`list_plugin_files` still reports the plugin's own
  files, and :class:`PlgArtifact` still carries them, because the operator wants to know
  what the image was built from. It is no longer the archive's member list: what the
  image admits is governed by the plugin's ``.dockerignore``.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import subprocess  # nosec B404 - used to drive the local docker CLI, never with shell=True
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

from ..core.plugin_files import is_packaging_excluded
from ..core.vendor import apply_custom_vendor_suffix
from ..core.yaml_codec import load_plugin_spec

__all__ = [
    "BuildEngineError",
    "ValidationNotPassedError",
    "PackagingError",
    "PlgArtifact",
    "PluginIdentity",
    "BuildEngine",
    "ExportPreview",
    "list_plugin_files",
    "preview_export_files",
    "read_plugin_identity",
    "PLG_SUFFIX",
    "BUILDER_METADATA_DIR",
    "DEFAULT_ARTIFACT_SUBDIR",
    "DEFAULT_DOCKER_EXECUTABLE",
    "VALIDATION_NOT_PASSED_MESSAGE",
]

#: The artifact file extension for a packaged plugin.
PLG_SUFFIX = ".plg"

#: The ``docker`` binary. The artifact is an image archive, so packaging needs a
#: daemon -- see ``.kiro/specs/plg-artifact-is-an-image/bugfix.md``.
DEFAULT_DOCKER_EXECUTABLE = "docker"

#: How long a plugin image build may take. Generous: a cold build pulls the SDK base
#: image and installs the plugin's dependencies.
DEFAULT_BUILD_TIMEOUT_SECONDS = 1800.0

#: How long ``docker save`` may take. The archive is tens of megabytes, so this is
#: bounded by local disk rather than by the network.
DEFAULT_SAVE_TIMEOUT_SECONDS = 600.0

#: The tool-only metadata directory that must never be packaged into a ``.plg``
#: (design "Project_Folder" ``.builder/``; Req 14.3).
BUILDER_METADATA_DIR = ".builder"

#: Where a produced artifact is placed by default, relative to the project root.
DEFAULT_ARTIFACT_SUBDIR = Path(BUILDER_METADATA_DIR) / "artifacts"

#: The error surfaced when packaging is requested before validation passed
#: (Req 9.4).
VALIDATION_NOT_PASSED_MESSAGE = "validation has not passed; the plugin cannot be packaged until validation succeeds"

#: Accepted path inputs.
PathInput = Union[str, Path]


#: How Docker says its daemon cannot be reached. Detected because it is the most common
#: packaging failure and the one that says least about the plugin -- reporting it as
#: "docker build failed" sends the operator to look at their code.
_DAEMON_UNREACHABLE_PHRASES = (
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "docker daemon is not running",
    "error during connect",
)


def _explain_docker_failure(what: str, tag: str, returncode: int, output: str) -> str:
    """Turn a failed docker command into something an operator can act on.

    Three cases, because they call for three different actions and "packaging failed"
    serves none of them: the daemon is not running, the command said nothing at all, or
    the command explained itself and the explanation should be passed through.
    """
    lowered = output.lower()
    if any(phrase in lowered for phrase in _DAEMON_UNREACHABLE_PHRASES):
        return (
            f"cannot {what} {tag}: the Docker daemon is not reachable. Packaging a plugin produces a "
            "container image, so Docker must be running for export as well as for the build stage. "
            "Start Docker and try again -- nothing about the plugin needs changing."
        )
    detail = _tail(output)
    if not detail:
        return (
            f"docker {what} failed for {tag} (exit {returncode}) and printed nothing. Try the command "
            f"by hand to see why: docker {what} {tag}"
        )
    return f"docker {what} failed for {tag} (exit {returncode}). {detail}"


@dataclass(frozen=True)
class PluginIdentity:
    """How a plugin identifies itself to a tenant.

    A tenant loads the image out of the ``.plg`` and reads the plugin's identity from
    the image tag, so this is the one thing the artifact must get right. Measured from
    the archive that imported successfully: ``rapid7_custom/jumpcloud:1.0.1``
    (``.kiro/specs/plg-artifact-is-an-image/bugfix.md`` 1.1).

    Attributes:
        vendor: the vendor, carrying the ``_custom`` suffix (Req 13).
        name: the plugin's snake_case name.
        version: the plugin's semantic version.
    """

    vendor: str
    name: str
    version: str

    @property
    def image_tag(self) -> str:
        """The published image tag, ``<vendor>/<name>:<version>``."""
        return f"{self.vendor}/{self.name}:{self.version}"

    @property
    def artifact_name(self) -> str:
        """The artifact filename, ``<vendor>_<name>_<version>.plg``.

        Matches what ``insight-plugin export`` writes. Whether a tenant parses the
        filename is unverified (``bugfix.md`` 1.8); matching the toolchain costs
        nothing and removes the question.
        """
        return f"{self.vendor}_{self.name}_{self.version}{PLG_SUFFIX}"


#: A Docker repository path component: lowercase alphanumeric runs, optionally joined by
#: a single separator. Docker rejects anything else with "invalid reference format", so
#: the tag is checked here rather than letting a malformed one reach the daemon.
_REFERENCE_COMPONENT = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


def read_plugin_identity(root: PathInput) -> PluginIdentity:
    """Read the plugin's published identity from the spec in ``root``.

    Read from disk rather than accepted from a caller, so the artifact's identity and
    the spec that will ship inside it cannot disagree -- the same reason the export
    preview reads the spec from disk. The vendor suffix is applied here and is
    idempotent, so a tree whose spec is already vendor-suffixed (the export path
    commits one before packaging, Req 13.3) is unaffected.

    Raises:
        PackagingError: if the spec is missing, unreadable, unparseable, missing a field
            the identity needs, or names a vendor or plugin that cannot form a valid
            Docker reference. Packaging cannot proceed without a tag, and a guessed tag
            would produce an artifact that imports as the wrong plugin.
    """
    spec_path = Path(root) / "plugin.spec.yaml"
    try:
        text = spec_path.read_text(encoding="utf-8")
    except OSError as error:
        raise PackagingError(f"cannot read the plugin spec at {spec_path}: {error}") from error
    try:
        spec = load_plugin_spec(text)
    except Exception as error:  # noqa: BLE001 - the codec raises a range of parse errors
        raise PackagingError(f"cannot parse the plugin spec at {spec_path}: {error}") from error

    name = (getattr(spec, "name", "") or "").strip()
    version = str(getattr(spec, "version", "") or "").strip()
    if not name:
        raise PackagingError(f"the plugin spec at {spec_path} has no name, so the image cannot be tagged")
    if not version:
        raise PackagingError(f"the plugin spec at {spec_path} has no version, so the image cannot be tagged")

    identity = PluginIdentity(
        vendor=apply_custom_vendor_suffix(getattr(spec, "vendor", None)),
        name=name,
        version=version,
    )
    _require_taggable(identity, spec_path)
    return identity


def _require_taggable(identity: PluginIdentity, spec_path: Path) -> None:
    """Reject an identity that cannot become a Docker tag, saying which part is wrong.

    Worth checking here rather than at the daemon. Requirement 13.4 turns an absent
    vendor into exactly ``_custom``, and ``_custom/<name>`` is an *invalid reference
    format* to Docker -- a repository component may not begin with a separator. So a
    spec that is legal by Req 13.4 can describe a plugin that cannot be published. Real
    plugins always carry a vendor (``insight-plugin validate`` requires one), which is
    why this is unreachable in practice, but failing closed with the reason named beats
    surfacing Docker's exit 125.

    The tag is refused rather than repaired: lowercasing or trimming would silently
    publish the plugin under an identity its author did not choose, which is worse than
    stopping.
    """
    for label, component in (("vendor", identity.vendor), ("name", identity.name)):
        if not _REFERENCE_COMPONENT.match(component):
            raise PackagingError(
                f"the plugin's {label} {component!r} cannot form a Docker image tag "
                f"(from {spec_path}). A tag component must be lowercase alphanumeric, "
                f"optionally separated by '.', '_' or '-', and must start and end with a "
                f"letter or digit -- so {identity.image_tag!r} would be refused as an "
                "invalid reference format."
            )


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
        path: The absolute path of the written ``.plg`` -- a gzipped ``docker save`` of
            the plugin image.
        files: The plugin files the image was built from, sorted. Retained because the
            operator still wants to know what went in, even though the archive no longer
            contains them one by one -- what the image admits is governed by the
            plugin's ``.dockerignore``.
        image_tag: The published identity the artifact declares, which is what a tenant
            reads to know which plugin this is.
    """

    path: Path
    files: Tuple[str, ...]
    image_tag: str = ""

    @property
    def name(self) -> str:
        """The artifact file name (e.g. ``rapid7_custom_jumpcloud_1.0.1.plg``)."""
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
        if is_packaging_excluded(relative.as_posix()):
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
    """Packages a validated plugin project into an image-archive ``.plg`` (Req 9).

    A ``.plg`` is a gzipped ``docker save`` of the plugin's image, tagged
    ``<vendor>/<name>:<version>``. A tenant loads that image; the plugin's code and its
    ``plugin.spec.yaml`` travel inside the layers. Packaging therefore requires a Docker
    daemon, and the engine drives ``docker build`` and ``docker save`` directly rather
    than calling ``insight-plugin export`` -- see
    ``.kiro/specs/plg-artifact-is-an-image/bugfix.md`` 2.2 for why.

    The engine is read-only with respect to the plugin's source files: it reads them,
    builds from them, and writes the artifact, so a failing run leaves the sources
    unchanged (Req 9.5). It does remove a **stale ``.plg``** from the plugin directory
    from a staged copy of the packaged file set, so byproducts this tool excludes really
    are absent from the image -- the plugin's generated ``.dockerignore`` excludes none of
    them.
    """

    def __init__(
        self,
        *,
        docker_executable: Optional[str] = None,
        build_timeout_seconds: float = DEFAULT_BUILD_TIMEOUT_SECONDS,
        save_timeout_seconds: float = DEFAULT_SAVE_TIMEOUT_SECONDS,
    ) -> None:
        """Configure the engine.

        Args:
            docker_executable: the ``docker`` binary. ``None`` reads
                :data:`DEFAULT_DOCKER_EXECUTABLE` at call time rather than at import, so
                a caller -- or the test suite's own guard against building real images --
                can substitute a stand-in without reaching into every construction site.
            build_timeout_seconds: ceiling on ``docker build``.
            save_timeout_seconds: ceiling on ``docker save``.
        """
        self._docker = docker_executable or DEFAULT_DOCKER_EXECUTABLE
        self._build_timeout = build_timeout_seconds
        self._save_timeout = save_timeout_seconds

    def package(
        self,
        project: object,
        *,
        validation_passed: bool,
        output_dir: Optional[PathInput] = None,
        artifact_name: Optional[str] = None,
    ) -> PlgArtifact:
        """Package ``project`` into a single image-archive ``.plg`` (Req 9.1, 9.2).

        Packaging proceeds only when ``validation_passed`` is ``True`` (Req 9.1, 9.4).
        The image is built and tagged with the plugin's published identity, saved, and
        gzipped into a temporary file that is only moved into place on full success, so
        on any failure no partial artifact exists and the sources are untouched (Req 9.5).

        Args:
            project: A plugin working tree -- a path, or an object with a ``root``
                attribute.
            validation_passed: Whether validation succeeded for this plugin. When
                ``False`` no artifact is produced.
            output_dir: Directory to write the ``.plg`` into. Defaults to
                ``<root>/.builder/artifacts``.
            artifact_name: Override the artifact file name. Defaults to the identity's
                own ``<vendor>_<name>_<version>.plg``, which is what the toolchain
                writes; an override still gets a ``.plg`` suffix if it lacks one.

        Returns:
            A :class:`PlgArtifact` describing the written artifact.

        Raises:
            ValidationNotPassedError: If ``validation_passed`` is ``False`` (Req 9.4).
            PackagingError: If the identity cannot be read, Docker is unavailable, the
                build fails, or the save fails. No partial artifact is left and the
                sources are unchanged (Req 9.5).
        """
        if not validation_passed:
            raise ValidationNotPassedError(VALIDATION_NOT_PASSED_MESSAGE)

        root = _resolve_root(project)
        identity = read_plugin_identity(root)
        # Recorded for the report even though the archive no longer contains them one
        # by one: the operator still wants to know what the image was built from.
        members = list_plugin_files(root)

        destination_dir = Path(output_dir) if output_dir is not None else root / DEFAULT_ARTIFACT_SUBDIR
        final_name = artifact_name if artifact_name else identity.artifact_name
        if not final_name.endswith(PLG_SUFFIX):
            final_name += PLG_SUFFIX
        final_path = destination_dir / final_name

        try:
            destination_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise PackagingError(f"failed to prepare output directory {destination_dir}: {error}") from error

        with tempfile.TemporaryDirectory(prefix="icpb-context-") as staging:
            context = self._stage_build_context(root, members, Path(staging))
            self._build_image(context, identity)
            temp_path = self._save_image(identity, destination_dir)
        try:
            os.replace(temp_path, final_path)
        except OSError as error:
            _safe_unlink(temp_path)
            raise PackagingError(f"failed to finalize artifact at {final_path}: {error}") from error

        return PlgArtifact(path=final_path.resolve(), files=tuple(members), image_tag=identity.image_tag)

    def _stage_build_context(self, root: Path, members: List[str], parent: Path) -> Path:
        """Copy exactly ``members`` into a fresh directory and return it.

        The image is built from this staging directory rather than from the plugin's own
        tree, which matters for three reasons.

        **The byproduct exclusions become real again.** ``list_plugin_files`` drops
        ``.coverage``, ``*.pyc``, ``build/`` and ``*.egg-info`` -- but the plugin's
        generated ``.dockerignore`` excludes none of them, and the generated Dockerfile
        does ``ADD . /workspace``. Building from the tree would copy a coverage database,
        full of absolute paths from this machine, into a customer-facing image. Building
        from the staged set means the file list this tool reports *is* what the image
        contains.

        **The engine goes back to being read-only.** An earlier attempt moved a stale
        ``.plg`` out of the operator's directory to keep it out of the build context.
        Staging removes the need to touch their tree at all, which is the better property
        -- packaging should not rearrange the thing it is packaging.

        **What is reported and what ships cannot drift.** One list drives the preview,
        the artifact's ``files``, and the build context.
        """
        context = parent / "context"
        for relative in members:
            source = root / relative
            target = context / relative
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            except OSError as error:
                raise PackagingError(f"failed to stage {relative} for the image build: {error}") from error
        return context

    def _build_image(self, root: Path, identity: PluginIdentity) -> None:
        """Build the plugin image and tag it with its published identity.

        ``docker build -t`` tags as it builds, so this is the tag step too. The build
        stage's own ``icplugin-validate/<name>:latest`` tag is left alone -- it belongs
        to validation and has a different job.
        """
        completed = self._run(
            (self._docker, "build", "-t", identity.image_tag, "."),
            cwd=root,
            timeout=self._build_timeout,
            what=f"build the plugin image {identity.image_tag}",
        )
        if completed.returncode != 0:
            raise PackagingError(
                _explain_docker_failure(
                    "build",
                    identity.image_tag,
                    completed.returncode,
                    completed.stderr or completed.stdout or "",
                )
            )

    def _save_image(self, identity: PluginIdentity, destination_dir: Path) -> Path:
        """Save the image and gzip it into a temp file in ``destination_dir``.

        Written beside the final path so the subsequent :func:`os.replace` is an atomic
        same-filesystem rename, and compressed from an intermediate tar rather than
        streamed so that a failed save cannot leave a truncated archive that looks
        complete.
        """
        fd, temp_name = tempfile.mkstemp(prefix=".plg-", suffix=".tmp", dir=str(destination_dir))
        os.close(fd)
        temp_path = Path(temp_name)
        tar_path = Path(temp_name + ".tar")
        try:
            completed = self._run(
                (self._docker, "save", identity.image_tag, "-o", str(tar_path)),
                cwd=destination_dir,
                timeout=self._save_timeout,
                what=f"save the plugin image {identity.image_tag}",
            )
            if completed.returncode != 0:
                raise PackagingError(
                    _explain_docker_failure(
                        "save",
                        identity.image_tag,
                        completed.returncode,
                        completed.stderr or completed.stdout or "",
                    )
                )
            with open(tar_path, "rb") as source, gzip.open(temp_path, "wb") as target:
                shutil.copyfileobj(source, target)
        except PackagingError:
            _safe_unlink(temp_path)
            _safe_unlink(tar_path)
            raise
        except OSError as error:
            _safe_unlink(temp_path)
            _safe_unlink(tar_path)
            raise PackagingError(f"failed to compress the image archive: {error}") from error
        _safe_unlink(tar_path)
        return temp_path

    def _run(self, command: Sequence[str], *, cwd: Path, timeout: float, what: str) -> "subprocess.CompletedProcess":
        """Run one docker command, turning every way it can fail into a clear message.

        Docker being absent or its daemon being down are the two most common causes and
        say nothing about the plugin, so they are reported as themselves rather than as
        "packaging failed".
        """
        try:
            return subprocess.run(  # nosec B603 - fixed argv, no shell
                list(command),
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as error:
            raise PackagingError(
                f"cannot {what}: {self._docker!r} was not found on PATH. Packaging a plugin "
                "produces a container image, so Docker is required for export as well as build."
            ) from error
        except subprocess.TimeoutExpired as error:
            raise PackagingError(f"cannot {what}: timed out after {timeout:.0f}s") from error
        except OSError as error:
            raise PackagingError(f"cannot {what}: {error}") from error


def _tail(output: Optional[str], *, limit: int = 600) -> str:
    """The last of ``output``, bounded, for a failure message.

    Docker's failures are verbose and the useful part is at the end. Bounded so a
    pathological build log cannot become the whole error.
    """
    text = (output or "").strip()
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def _safe_unlink(path: Path) -> None:
    """Remove ``path`` if present, ignoring a missing file (best-effort cleanup)."""
    try:
        path.unlink()
    except FileNotFoundError:  # pragma: no cover - already gone
        pass
    except OSError:  # pragma: no cover - best-effort cleanup
        pass
