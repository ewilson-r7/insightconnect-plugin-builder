"""File-tree diff engine for export preview and prior-version comparison.

A *file tree* is modeled as a mapping of path -> content (``Mapping[str, Any]``);
content is compared with ``==`` so it may be ``str`` or ``bytes``. Given a prior
tree and a current tree, :func:`diff_file_trees` partitions the union of their
paths into three disjoint sets:

* **added** -- present in the current tree but not the prior tree.
* **removed** -- present in the prior tree but not the current tree.
* **modified** -- present in both trees but with differing content.

Paths present in both trees with identical content are *unchanged* and appear in
none of the three sets. When no prior tree exists (``prior is None``) every file
in the current tree is reported as an addition and the result is flagged as the
first version (design Property 31; Requirements 16.3, 16.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

__all__ = ["FileTreeDiff", "diff_file_trees"]


@dataclass(frozen=True)
class FileTreeDiff:
    """The partition of two file trees into added/removed/modified paths.

    The three path sets are pairwise disjoint. ``first_version`` is ``True``
    only when there was no prior tree at all, in which case every current file
    appears in :attr:`added` and both :attr:`removed` and :attr:`modified` are
    empty (Requirement 16.4).
    """

    added: frozenset[str]
    removed: frozenset[str]
    modified: frozenset[str]
    first_version: bool = False

    @property
    def has_changes(self) -> bool:
        """Return ``True`` iff any file was added, removed, or modified."""
        return bool(self.added or self.removed or self.modified)


def diff_file_trees(
    prior: Optional[Mapping[str, Any]],
    current: Mapping[str, Any],
) -> FileTreeDiff:
    """Partition ``prior`` and ``current`` file trees into added/removed/modified.

    Args:
        prior: The prior version's file tree, or ``None`` when no prior version
            exists. An empty mapping is treated as an existing (but empty) tree,
            whereas ``None`` marks a genuine first version.
        current: The current draft's file tree.

    Returns:
        A :class:`FileTreeDiff`. When ``prior`` is ``None``, every path in
        ``current`` is reported as an addition and ``first_version`` is ``True``.
    """
    if prior is None:
        return FileTreeDiff(
            added=frozenset(current),
            removed=frozenset(),
            modified=frozenset(),
            first_version=True,
        )

    prior_paths = set(prior)
    current_paths = set(current)

    added = current_paths - prior_paths
    removed = prior_paths - current_paths
    modified = {path for path in prior_paths & current_paths if prior[path] != current[path]}

    return FileTreeDiff(
        added=frozenset(added),
        removed=frozenset(removed),
        modified=frozenset(modified),
        first_version=False,
    )
