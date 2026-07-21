"""Property test for the PLG artifact round trip (task 15.6).

# Feature: insightconnect-plugin-builder, Property 6: PLG artifact round trip

Property 6 states that *for any* validated plugin working tree, packaging it
into a ``.plg`` and then extracting that artifact yields the same set of files
with byte-identical contents, and that the produced artifact carries the gzip
format (Req 2.1, 9.2).

The Build_Engine (:meth:`~icplugin_builder.integrations.build_engine.BuildEngine.package`)
packages a validated project into a single gzipped-tarball ``.plg``. This test
generates arbitrary plugin working trees on disk -- varied relative paths, file
counts, and both text and binary contents -- writes them into a fresh temporary
directory, packages with ``validation_passed=True``, then extracts the ``.plg``
and asserts:

* the extracted file set equals the packaged file set
  (:attr:`PlgArtifact.files`), which -- because the generated trees contain no
  excluded metadata -- is exactly the set of files that was written;
* every extracted file's bytes are identical to what was written; and
* the artifact is gzip format (gzip magic bytes plus decompressibility).

No real plugin toolchain or Docker daemon is required; the working tree is
synthesized directly on disk.

**Validates: Requirements 2.1, 9.2**
"""

import gzip
import tarfile
import tempfile
from pathlib import Path
from typing import Dict, List

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.integrations.build_engine import (
    PLG_SUFFIX,
    BuildEngine,
    PlgArtifact,
    list_plugin_files,
)

# Directory/file name segments the packager excludes from artifacts; generated
# trees must avoid them so the packaged set equals the written set exactly.
_EXCLUDED_SEGMENTS = frozenset({".builder", ".git", "__pycache__", ".pytest_cache", ".mypy_cache"})


def _path_segments() -> st.SearchStrategy[str]:
    """Generate a single path segment.

    Segments are lowercase snake_case tokens (letters/digits/underscore). Using
    only lowercase avoids collisions on case-insensitive filesystems (e.g.
    macOS) and mirrors plugin file-naming conventions. Excluded metadata names
    are filtered out so every generated file is destined for the artifact.
    """
    return st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=8).filter(
        lambda seg: seg not in _EXCLUDED_SEGMENTS
    )


def _relative_paths() -> st.SearchStrategy[str]:
    """Generate a relative POSIX path of one to three segments."""
    return st.lists(_path_segments(), min_size=1, max_size=3).map(lambda parts: "/".join(parts))


def _file_contents() -> st.SearchStrategy[bytes]:
    """Generate file contents as raw bytes, covering both text and binary.

    Text content is UTF-8 encoded so the tree exercises human-readable files;
    binary content exercises arbitrary byte payloads (e.g. compiled resources,
    images). Comparing raw bytes on both sides makes the round-trip assertion
    encoding-agnostic.
    """
    text_bytes = st.text(max_size=200).map(lambda s: s.encode("utf-8"))
    binary_bytes = st.binary(max_size=200)
    return st.one_of(text_bytes, binary_bytes)


def _is_ancestor(candidate: List[str], other: List[str]) -> bool:
    """Return whether ``candidate`` names a directory prefix of ``other``."""
    return len(candidate) < len(other) and other[: len(candidate)] == candidate


@st.composite
def plugin_working_trees(draw: st.DrawFn) -> Dict[str, bytes]:
    """Generate a non-empty map of ``{relative_posix_path: content_bytes}``.

    Paths that would collide on the filesystem -- where one path names a
    directory that is an ancestor of another path (e.g. ``a`` as a file and
    ``a/b`` as a file) -- are dropped, keeping a set that can be materialized as
    a real directory tree. At least one file is always retained.
    """
    raw = draw(st.dictionaries(_relative_paths(), _file_contents(), min_size=1, max_size=8))

    kept: Dict[str, bytes] = {}
    kept_parts: List[List[str]] = []
    # Shorter paths first so ancestor directories are detected deterministically.
    for path in sorted(raw, key=lambda p: (len(p.split("/")), p)):
        parts = path.split("/")
        if any(_is_ancestor(parts, k) or _is_ancestor(k, parts) for k in kept_parts):
            continue
        kept[path] = raw[path]
        kept_parts.append(parts)

    # The prefix filter can never remove everything: the first (shortest) path
    # is always retained, so the tree has at least one file.
    return kept


def _materialize(tree: Dict[str, bytes], root: Path) -> None:
    """Write ``tree`` into ``root`` as a real directory tree."""
    for relative, content in tree.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


@settings(max_examples=100, deadline=None)
@given(tree=plugin_working_trees())
def test_plg_package_extract_round_trips_and_is_gzip(tree):
    """Property 6: package then extract yields identical files; artifact is gzip.

    For any generated working tree, packaging with ``validation_passed=True``
    and extracting the resulting ``.plg`` reproduces exactly the packaged file
    set with byte-identical contents, and the artifact is gzip format.

    **Validates: Requirements 2.1, 9.2**
    """
    with tempfile.TemporaryDirectory() as workdir:
        base = Path(workdir)
        source = base / "project"
        source.mkdir()
        _materialize(tree, source)

        output_dir = base / "out"
        artifact = BuildEngine().package(source, validation_passed=True, output_dir=output_dir)

        assert isinstance(artifact, PlgArtifact)
        assert artifact.path.exists()
        assert artifact.path.suffix == PLG_SUFFIX

        # Since the generated tree contains no excluded metadata, the packaged
        # member set equals both the written files and the deterministic listing.
        assert set(artifact.files) == set(tree)
        assert list(artifact.files) == list_plugin_files(source)

        # Artifact carries gzip format: gzip magic bytes and decompressibility.
        with artifact.path.open("rb") as handle:
            assert handle.read(2) == b"\x1f\x8b"
        with gzip.open(artifact.path, "rb") as handle:
            handle.read(1)  # decompresses without error

        # Round trip: extracting reproduces the same files with identical bytes.
        extracted = base / "extracted"
        with tarfile.open(artifact.path, mode="r:gz") as archive:
            member_names = sorted(archive.getnames())
            archive.extractall(extracted, filter="data")

        assert member_names == sorted(tree)
        for relative, content in tree.items():
            assert (extracted / relative).read_bytes() == content
