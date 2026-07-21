"""Property test for fork baseline-diff correctness (task 17.6; Req 25.8).

The example-based tests in ``test_baseline_diff.py`` pin single cases of each
partition (added/removed/modified, the ``.builder/`` exclusion, the not-a-fork
rejection). This module covers the universal property across arbitrary sequences
of edits to a real production fork's draft working tree:
:func:`~icplugin_builder.integrations.plugin_source_provider.baseline_diff`
always equals the added/removed/modified set-difference between the current draft
file tree and the stored ``.builder/baseline/`` snapshot (with the tool-owned
``.builder/`` subtree excluded).

Each example builds a distinct production plugin in a throwaway local clone,
forks it with the real (un-mocked) read-only import (no network/git), applies a
generated sequence of add/remove/modify edits to the draft, and compares
``baseline_diff`` against an **independent** oracle that walks the baseline and
draft trees itself and computes the three sets with plain Python set operations.
"""

import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from icplugin_builder.api.config import ProductionSourceConfig
from icplugin_builder.integrations.plugin_source_provider import (
    PluginSourceProvider,
    baseline_diff,
)
from icplugin_builder.persistence.project_folder import BUILDER_DIRNAME, ProjectFolder

# Package prefixes a production plugin package directory may carry (Req 25.7).
_PACKAGE_PREFIXES = ("icon", "komand")

# Directory/entry names never copied by the import and never diffed. Defined
# independently of the module under test so the oracle stays a true oracle; the
# ``.builder/`` subtree (which holds the baseline itself) must be excluded.
_ORACLE_SKIP = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache", BUILDER_DIRNAME})

# File suffixes for generated edits (a mix of code/docs/binary-ish resources).
_SUFFIXES = (".py", ".md", ".txt", ".json", ".bin")


def _spec_text(name: str) -> str:
    """Render a minimal schema-conforming ``plugin.spec.yaml`` body for ``name``."""
    return (
        "plugin_spec_version: v2\n"
        f"name: {name}\n"
        f"title: {name.title()}\n"
        "description: A production plugin.\n"
        "version: 2.3.4\n"
        "vendor: rapid7\n"
        "actions:\n"
        "  do_thing:\n"
        "    title: Do Thing\n"
        "    description: Does a thing.\n"
        "    input:\n"
        "      host:\n"
        "        type: string\n"
        "        required: true\n"
    )


def _write_production_plugin(clone_dir: Path, name: str, prefix: str) -> None:
    """Materialize a minimal production plugin directory under ``clone_dir``."""
    plugin_dir = clone_dir / name
    package_dir = plugin_dir / f"{prefix}_{name}"
    package_dir.mkdir(parents=True)
    (plugin_dir / "plugin.spec.yaml").write_text(_spec_text(name), encoding="utf-8")
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "action.py").write_text("# hand-written logic\n", encoding="utf-8")
    (plugin_dir / "help.md").write_text("# Help\n", encoding="utf-8")


def _snake() -> st.SearchStrategy[str]:
    """Generate short snake_case path segments that never collide with skip names."""
    first = st.sampled_from("abcdefghijklmnopqrstuvwxyz")
    rest = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=0, max_size=6)
    return st.builds(lambda h, t: (h + t).strip("_") or "x", first, rest)


def _rel_paths() -> st.SearchStrategy[str]:
    """Generate a POSIX-relative file path of 1-3 segments with a file suffix."""
    dirs = st.lists(_snake(), min_size=0, max_size=2)
    return st.builds(
        lambda parts, leaf, suffix: "/".join([*parts, leaf + suffix]),
        dirs,
        _snake(),
        st.sampled_from(_SUFFIXES),
    )


def _contents() -> st.SearchStrategy[bytes]:
    """Generate file content as raw bytes (text and non-utf-8 binary both appear)."""
    text = st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=40).map(
        lambda s: s.encode("utf-8")
    )
    return st.one_of(text, st.binary(max_size=40))


# An edit is (kind, rel_path, content, selector). ``rel_path``/``content`` drive
# "add"; ``selector`` picks an existing draft file for "modify"/"remove".
_EDIT = st.tuples(
    st.sampled_from(("add", "modify", "remove")),
    _rel_paths(),
    _contents(),
    st.integers(min_value=0, max_value=10_000),
)


def _current_draft_files(root: Path) -> List[Path]:
    """Return the draft's diffable files (sorted), excluding the skip subtrees."""
    files: List[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _ORACLE_SKIP for part in relative.parts):
            continue
        files.append(path)
    return files


def _apply_edit(root: Path, edit: Tuple[str, str, bytes, int]) -> None:
    """Apply one generated edit to the draft working tree at ``root`` in place."""
    kind, rel_path, content, selector = edit

    if kind == "add":
        target = root / rel_path
        # Never write into (or over) a skip subtree, and skip when any path
        # component is already a non-directory (can't nest under a file).
        rel = Path(rel_path)
        if any(part in _ORACLE_SKIP for part in rel.parts):
            return
        if target.is_dir():
            return
        parent = target.parent
        for ancestor in [parent, *parent.parents]:
            if ancestor == root:
                break
            if ancestor.exists() and not ancestor.is_dir():
                return
        parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return

    existing = _current_draft_files(root)
    if not existing:
        return
    chosen = existing[selector % len(existing)]

    if kind == "modify":
        chosen.write_bytes(content)
    else:  # remove
        chosen.unlink()


def _read_tree(root: Path) -> Dict[str, bytes]:
    """Independent oracle: map POSIX-relative path -> raw bytes, skipping noise.

    Comparing raw bytes is equivalent to the module's decode-then-compare because
    two byte strings are equal iff their utf-8 decodings (when both decodable)
    are equal, so the modified-set determination matches.
    """
    tree: Dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _ORACLE_SKIP for part in relative.parts):
            continue
        tree[relative.as_posix()] = path.read_bytes()
    return tree


# Feature: insightconnect-plugin-builder, Property 50: Baseline diff correctness for forks
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    name=_snake(),
    prefix=st.sampled_from(_PACKAGE_PREFIXES),
    edits=st.lists(_EDIT, max_size=8),
)
def test_baseline_diff_equals_set_difference(name, prefix, edits):
    """``baseline_diff`` equals the draft-vs-baseline set-difference (Req 25.8).

    For an arbitrary production fork and any sequence of subsequent draft edits,
    the reported added/removed/modified sets must equal the set-difference of the
    current draft file tree against the stored ``.builder/baseline/`` snapshot,
    with the tool-owned ``.builder/`` subtree excluded.

    **Validates: Requirements 25.8**
    """
    with tempfile.TemporaryDirectory(prefix="icpb-baseline-") as workspace:
        workspace_path = Path(workspace)
        clone_dir = workspace_path / "clone"
        clone_dir.mkdir()
        projects_root = workspace_path / "projects"

        _write_production_plugin(clone_dir, name, prefix)

        source = ProductionSourceConfig(
            id="prod_source",
            repo="rapid7/insightconnect-plugins",
            visibility="public",
            local_path=str(clone_dir),
            remote_url="https://github.com/rapid7/insightconnect-plugins.git",
        )
        provider = PluginSourceProvider([source], projects_root)
        folder: ProjectFolder = provider.import_plugin("prod_source", name).project_folder

        for edit in edits:
            _apply_edit(folder.path, edit)

        # Independent oracle: read the baseline and current draft trees ourselves.
        baseline_dir = folder.path / BUILDER_DIRNAME / "baseline"
        baseline_tree = _read_tree(baseline_dir)
        draft_tree = _read_tree(folder.path)

        baseline_paths = set(baseline_tree)
        draft_paths = set(draft_tree)
        expected_added = draft_paths - baseline_paths
        expected_removed = baseline_paths - draft_paths
        expected_modified = {p for p in baseline_paths & draft_paths if baseline_tree[p] != draft_tree[p]}

        diff = baseline_diff(folder)

    assert set(diff.added) == expected_added
    assert set(diff.removed) == expected_removed
    assert set(diff.modified) == expected_modified
    # The three partitions are pairwise disjoint and never mention .builder/.
    assert diff.added.isdisjoint(diff.removed)
    assert diff.added.isdisjoint(diff.modified)
    assert diff.removed.isdisjoint(diff.modified)
    assert not any(p.startswith(f"{BUILDER_DIRNAME}/") for p in diff.added | diff.removed | diff.modified)
    assert not diff.first_version
