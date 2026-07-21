"""Property test for preview/package file-list consistency (task 15.8).

# Feature: insightconnect-plugin-builder, Property 30: Preview file list matches packaged contents

Property 30 states that *for any* plugin working tree, the export-preview file
list shown to the user before an export equals the actual set of files placed
inside the produced ``.plg`` artifact. The preview must therefore neither
promise a file that packaging omits nor hide a file that packaging includes --
including the requirement that tool-only metadata (the ``.builder/`` subtree)
and transient directories (``__pycache__`` ...) are excluded from *both* the
preview and the artifact (Req 16.2, and Req 14.3 for the ``.builder/``
exclusion).

The export preview is computed by
:func:`~icplugin_builder.integrations.build_engine.preview_export_files` and the
artifact is produced by
:meth:`~icplugin_builder.integrations.build_engine.BuildEngine.package`. This
test generates arbitrary plugin working trees on disk -- varied relative paths,
file counts, and text/binary contents -- and deliberately seeds each tree with
``.builder/`` and ``__pycache__`` (and other transient) noise files that must be
excluded. It then computes the preview, packages the tree, extracts the produced
``.plg``, and asserts:

* the preview file list equals the set of members actually inside the ``.plg``;
* that shared set equals exactly the non-excluded files written to the tree; and
* none of the seeded excluded-directory noise appears in either place.

No real plugin toolchain or Docker daemon is required; the working tree is
synthesized directly on disk.

**Validates: Requirements 16.1, 16.2**
"""

import tarfile
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.integrations.build_engine import (
    BuildEngine,
    ExportPreview,
    preview_export_files,
)

# Directory-name segments the packager (and preview) must exclude from artifacts.
# Mirrors ``build_engine._EXCLUDED_DIRS``.
_EXCLUDED_SEGMENTS: Tuple[str, ...] = (
    ".builder",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
)
_EXCLUDED_SET = frozenset(_EXCLUDED_SEGMENTS)


def _path_segments() -> st.SearchStrategy[str]:
    """Generate a single ordinary (non-excluded) path segment.

    Segments are lowercase snake_case tokens (letters/digits/underscore). Using
    only lowercase avoids collisions on case-insensitive filesystems (e.g.
    macOS) and mirrors plugin file-naming conventions. Excluded metadata names
    are filtered out so an ordinary segment never accidentally names an excluded
    directory.
    """
    return st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=8).filter(
        lambda seg: seg not in _EXCLUDED_SET
    )


def _included_paths() -> st.SearchStrategy[str]:
    """Generate a relative POSIX path (1-3 ordinary segments) destined for the ``.plg``."""
    return st.lists(_path_segments(), min_size=1, max_size=3).map(lambda parts: "/".join(parts))


def _excluded_paths() -> st.SearchStrategy[str]:
    """Generate a relative POSIX path nested under an excluded directory.

    The first segment is always an excluded directory name (e.g. ``.builder`` or
    ``__pycache__``) so the file must be omitted from both the preview and the
    artifact. Because ordinary segments never equal an excluded name, these
    paths can never collide with an included path.
    """
    excluded_root = st.sampled_from(_EXCLUDED_SEGMENTS)
    tail = st.lists(_path_segments(), min_size=1, max_size=2)
    return st.builds(lambda head, rest: "/".join([head, *rest]), excluded_root, tail)


def _file_contents() -> st.SearchStrategy[bytes]:
    """Generate file contents as raw bytes, covering both text and binary.

    Text content is UTF-8 encoded so the tree exercises human-readable files;
    binary content exercises arbitrary byte payloads. The preview/package
    comparison is over file *paths*, so contents only vary the on-disk tree.
    """
    text_bytes = st.text(max_size=200).map(lambda s: s.encode("utf-8"))
    binary_bytes = st.binary(max_size=200)
    return st.one_of(text_bytes, binary_bytes)


def _is_ancestor(candidate: List[str], other: List[str]) -> bool:
    """Return whether ``candidate`` names a directory prefix of ``other``."""
    return len(candidate) < len(other) and other[: len(candidate)] == candidate


@st.composite
def plugin_trees_with_noise(draw: st.DrawFn) -> Tuple[Dict[str, bytes], Dict[str, bytes]]:
    """Generate ``(included, excluded)`` maps of ``{relative_posix_path: bytes}``.

    ``included`` holds at least one ordinary file destined for the ``.plg``;
    ``excluded`` holds ``.builder/``/``__pycache__``/... noise files that must be
    omitted from both the preview and the artifact. Paths that would collide on
    the filesystem -- where one path names a directory that is an ancestor of
    another (e.g. ``a`` as a file and ``a/b`` as a file) -- are dropped, so the
    combined set can always be materialized as a real directory tree.
    """
    included_raw = draw(st.dictionaries(_included_paths(), _file_contents(), min_size=1, max_size=8))
    excluded_raw = draw(st.dictionaries(_excluded_paths(), _file_contents(), min_size=0, max_size=5))

    included: Dict[str, bytes] = {}
    excluded: Dict[str, bytes] = {}
    kept_parts: List[List[str]] = []
    # Shorter paths first so ancestor directories are detected deterministically;
    # included paths are considered before excluded ones so the tree always keeps
    # at least one included file (they never share a prefix, so order is safe).
    combined = [(p, c, True) for p, c in included_raw.items()] + [(p, c, False) for p, c in excluded_raw.items()]
    for path, content, is_included in sorted(combined, key=lambda item: (len(item[0].split("/")), item[0])):
        parts = path.split("/")
        if any(_is_ancestor(parts, k) or _is_ancestor(k, parts) for k in kept_parts):
            continue
        (included if is_included else excluded)[path] = content
        kept_parts.append(parts)

    # The shortest included path is always retained (no included/excluded path
    # shares a prefix), so ``included`` has at least one file.
    return included, excluded


def _materialize(tree: Dict[str, bytes], root: Path) -> None:
    """Write ``tree`` into ``root`` as a real directory tree."""
    for relative, content in tree.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


@settings(max_examples=100, deadline=None)
@given(trees=plugin_trees_with_noise())
def test_preview_file_list_matches_packaged_contents(trees):
    """Property 30: the export preview names exactly the files inside the ``.plg``.

    For any generated working tree (with excluded-directory noise), the preview
    file list equals the set of members actually contained in the produced
    ``.plg``; that shared set is exactly the non-excluded files, and none of the
    seeded ``.builder/``/``__pycache__``/... noise appears in either place.

    **Validates: Requirements 16.1, 16.2**
    """
    included, excluded = trees
    with tempfile.TemporaryDirectory() as workdir:
        base = Path(workdir)
        source = base / "project"
        source.mkdir()
        _materialize(included, source)
        _materialize(excluded, source)

        # The preview the user sees before confirming the export.
        preview = preview_export_files(source)
        assert isinstance(preview, ExportPreview)
        preview_files = set(preview.files)

        # The artifact produced if the export proceeds. Write it outside the
        # source tree so it cannot perturb the tree being enumerated.
        artifact = BuildEngine().package(source, validation_passed=True, output_dir=base / "out")

        # The actual members inside the produced ``.plg``.
        with tarfile.open(artifact.path, mode="r:gz") as archive:
            packaged_files = set(archive.getnames())

        # Core property: the preview equals the actual packaged contents.
        assert preview_files == packaged_files

        # That shared set is exactly the non-excluded files that were written...
        assert preview_files == set(included)
        # ...and the seeded excluded-directory noise leaks into neither view.
        for noise_path in excluded:
            assert noise_path not in preview_files
            assert noise_path not in packaged_files
        assert not any(part in _EXCLUDED_SET for member in packaged_files for part in member.split("/"))
