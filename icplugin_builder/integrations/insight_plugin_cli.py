"""Wrapper over the ``insight-plugin`` CLI (the Deterministic_Scaffolder backend).

The ``insight-plugin`` CLI is the *deterministic* half of the generation split:
it scaffolds a plugin's directory structure and regenerates the derived files
(``schema.py``, ``__init__.py``, ``Dockerfile``, ``Makefile``, ``setup.py``,
``help.md``, ``.CHECKSUM``) mechanically from ``plugin.spec.yaml`` with **zero**
LLM involvement (design "Generation_Engine -> Deterministic_Scaffolder"; Req
3.1, 22.3). This module wraps two operations:

* :meth:`InsightPluginCli.create` -> ``insight-plugin create`` -- scaffold a new
  plugin working tree from a :class:`~icplugin_builder.core.spec_model.PluginSpec`.
* :meth:`InsightPluginCli.refresh` -> ``insight-plugin refresh`` -- regenerate the
  derived files after a structural spec change.

Both run the external binary through :func:`asyncio.create_subprocess_exec` so
the caller's event loop is never blocked, and both return a :class:`ProjectTree`
snapshot of the resulting on-disk files. Neither path performs any LLM call --
the wrapper only shells out to the deterministic CLI and reads the filesystem.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Optional, Sequence, Union

from icplugin_builder.core.spec_model import PluginSpec
from icplugin_builder.core.yaml_codec import dump_plugin_spec

__all__ = [
    "InsightPluginCliError",
    "CommandResult",
    "ProjectTree",
    "InsightPluginCli",
    "DERIVED_FILE_NAMES",
    "DEFAULT_EXECUTABLE",
    "DEFAULT_SPEC_FILENAME",
]

#: The default ``insight-plugin`` executable name (resolved on ``PATH``).
DEFAULT_EXECUTABLE = "insight-plugin"

#: The conventional spec filename the CLI reads and writes.
DEFAULT_SPEC_FILENAME = "plugin.spec.yaml"

#: The derived files ``insight-plugin refresh`` regenerates from the spec. These
#: are machine-generated and must never be hand-edited (Req 22.3).
DERIVED_FILE_NAMES = frozenset(
    {
        "schema.py",
        "__init__.py",
        "Dockerfile",
        "Makefile",
        "setup.py",
        "help.md",
        ".CHECKSUM",
    }
)

#: Directory names skipped when snapshotting a project tree.
_IGNORED_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache"})

#: Accepted path inputs.
PathInput = Union[str, Path]


class InsightPluginCliError(Exception):
    """Raised when an ``insight-plugin`` invocation fails.

    Carries the argument vector, exit code, and captured output so callers can
    surface the failing step and its complete error output (Req 19.1).
    """

    def __init__(
        self,
        message: str,
        *,
        command: Optional[Sequence[str]] = None,
        returncode: Optional[int] = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.command = tuple(command) if command is not None else ()
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@dataclass(frozen=True)
class CommandResult:
    """The captured outcome of one ``insight-plugin`` subprocess invocation."""

    command: tuple
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Return ``True`` iff the process exited with status ``0``."""
        return self.returncode == 0


@dataclass(frozen=True)
class ProjectTree:
    """A snapshot of a scaffolded plugin's on-disk files.

    ``files`` maps each POSIX-style path *relative to* :attr:`root` to its
    content (``str`` for text files, ``bytes`` for undecodable/binary files),
    which lets it feed the file-tree diff engine directly.
    """

    root: Path
    files: Dict[str, Union[str, bytes]]

    def derived_files(self) -> Dict[str, Union[str, bytes]]:
        """Return only the files the CLI regenerates deterministically (Req 3.1)."""
        return {path: content for path, content in self.files.items() if PurePosixPath(path).name in DERIVED_FILE_NAMES}

    def has_derived_file(self, name: str) -> bool:
        """Return ``True`` iff a derived file named ``name`` is present anywhere in the tree."""
        return any(PurePosixPath(path).name == name for path in self.files)


class InsightPluginCli:
    """Async wrapper over the ``insight-plugin`` CLI create/refresh operations.

    The wrapper is deliberately thin: it builds the argument vector, runs the
    external binary via :func:`asyncio.create_subprocess_exec`, and reads the
    resulting working tree. It performs no LLM calls; all scaffolding is produced
    by the deterministic CLI (Req 3.1).
    """

    def __init__(
        self,
        executable: PathInput = DEFAULT_EXECUTABLE,
        *,
        spec_filename: str = DEFAULT_SPEC_FILENAME,
    ) -> None:
        """Configure the wrapper.

        Args:
            executable: the ``insight-plugin`` binary name or path.
            spec_filename: the spec filename written/read in the working tree.
        """
        self._executable = str(executable)
        self._spec_filename = spec_filename

    @property
    def executable(self) -> str:
        """The configured ``insight-plugin`` executable."""
        return self._executable

    async def create(self, spec: PluginSpec, target_dir: PathInput) -> ProjectTree:
        """Scaffold a new plugin working tree via ``insight-plugin create`` (Req 3.1).

        Writes ``spec`` to ``<target_dir>/<spec_filename>`` and runs
        ``insight-plugin create <spec_filename>`` with ``target_dir`` as the
        working directory, producing the directory structure and derived files
        (``schema.py``, ``__init__.py``, ``Dockerfile``, ``Makefile``,
        ``setup.py``, ``help.md``, ``.CHECKSUM``) with zero LLM calls.

        Args:
            spec: the :class:`PluginSpec` to scaffold from.
            target_dir: the directory the plugin tree is created under; it is
                created if it does not already exist.

        Returns:
            A :class:`ProjectTree` snapshot of ``target_dir`` after creation.

        Raises:
            InsightPluginCliError: if the spec cannot be written or the CLI exits
                with a non-zero status or is not found.
        """
        root = Path(target_dir)
        try:
            root.mkdir(parents=True, exist_ok=True)
            (root / self._spec_filename).write_text(dump_plugin_spec(spec), encoding="utf-8")
        except OSError as error:
            raise InsightPluginCliError(f"failed to stage spec in {root}: {error}") from error

        await self._run(["create", self._spec_filename], cwd=root)
        return snapshot_tree(root)

    async def refresh(self, project_dir: PathInput) -> ProjectTree:
        """Regenerate derived files via ``insight-plugin refresh`` (Req 3.1, 22.3).

        Runs ``insight-plugin refresh`` with ``project_dir`` as the working
        directory so the CLI regenerates ``schema.py``, ``__init__.py``,
        ``Dockerfile``, ``Makefile``, ``setup.py``, ``help.md``, and ``.CHECKSUM``
        from the current spec. No file is hand-edited and no LLM call is made.

        Args:
            project_dir: the existing plugin working tree to refresh.

        Returns:
            A :class:`ProjectTree` snapshot of ``project_dir`` after refresh.

        Raises:
            InsightPluginCliError: if ``project_dir`` is missing, or the CLI exits
                with a non-zero status or is not found.
        """
        root = Path(project_dir)
        if not root.is_dir():
            raise InsightPluginCliError(f"project directory does not exist: {root}")

        await self._run(["refresh"], cwd=root)
        return snapshot_tree(root)

    async def _run(self, args: Sequence[str], *, cwd: Path) -> CommandResult:
        """Run ``insight-plugin`` with ``args`` in ``cwd`` and capture its output.

        Raises:
            InsightPluginCliError: if the binary is not found or exits non-zero.
        """
        command = [self._executable, *args]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise InsightPluginCliError(
                f"insight-plugin executable not found: {self._executable!r}; "
                "install the insight-plugin CLI and ensure it is on PATH",
                command=command,
            ) from error

        stdout_bytes, stderr_bytes = await process.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        returncode = process.returncode if process.returncode is not None else -1

        result = CommandResult(command=tuple(command), returncode=returncode, stdout=stdout, stderr=stderr)
        if not result.ok:
            raise InsightPluginCliError(
                f"insight-plugin {' '.join(args)} failed with exit code {returncode}",
                command=command,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )
        return result


def snapshot_tree(root: PathInput) -> ProjectTree:
    """Read every file under ``root`` into a :class:`ProjectTree`.

    Directories in :data:`_IGNORED_DIRS` (``.git``, ``__pycache__``, ...) are
    skipped. Files decode as UTF-8 text when possible; undecodable files are
    retained as ``bytes`` so binary resources survive intact.

    Args:
        root: the plugin working-tree directory to snapshot.

    Returns:
        A :class:`ProjectTree` whose ``files`` maps relative POSIX paths to content.
    """
    base = Path(root)
    files: Dict[str, Union[str, bytes]] = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(base)
        if any(part in _IGNORED_DIRS for part in relative.parts):
            continue
        key = relative.as_posix()
        raw = path.read_bytes()
        try:
            files[key] = raw.decode("utf-8")
        except UnicodeDecodeError:
            files[key] = raw
    return ProjectTree(root=base, files=files)
