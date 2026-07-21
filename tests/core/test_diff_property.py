"""Property-based test for the file-tree diff engine (task 3.4).

Covers design Property 31 with Hypothesis: across arbitrary prior/current file
trees the added/removed/modified partition is correct and pairwise disjoint,
and the no-prior (``None``) case reports every current file as an addition.
"""

from typing import Any, Dict, Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.diff import diff_file_trees


def _paths() -> st.SearchStrategy[str]:
    """Generate short, non-empty file paths used as tree keys."""
    return st.text(alphabet="abcde/._", min_size=1, max_size=6)


def _contents() -> st.SearchStrategy[Any]:
    """Generate file contents as either text or bytes (compared by value)."""
    return st.one_of(st.text(max_size=8), st.binary(max_size=8))


def file_trees() -> st.SearchStrategy[Dict[str, Any]]:
    """Generate a file tree modeled as a ``{path: content}`` mapping."""
    return st.dictionaries(_paths(), _contents(), max_size=6)


def prior_trees() -> st.SearchStrategy[Optional[Dict[str, Any]]]:
    """Generate a prior tree, including the ``None`` (no-prior) first-version case."""
    return st.one_of(st.none(), file_trees())


# Feature: insightconnect-plugin-builder, Property 31: Diff correctness against prior version
@settings(max_examples=200)
@given(prior=prior_trees(), current=file_trees())
def test_diff_partition_is_correct_and_disjoint(prior: Optional[Dict[str, Any]], current: Dict[str, Any]):
    """The diff partitions changed files correctly; no-prior yields all additions.

    **Validates: Requirements 16.3, 16.4**
    """
    result = diff_file_trees(prior, current)

    if prior is None:
        # No prior version: every current file is an addition, nothing else.
        assert result.first_version is True
        assert result.added == frozenset(current)
        assert result.removed == frozenset()
        assert result.modified == frozenset()
        return

    assert result.first_version is False

    prior_paths = set(prior)
    current_paths = set(current)

    expected_added = current_paths - prior_paths
    expected_removed = prior_paths - current_paths
    expected_modified = {p for p in prior_paths & current_paths if prior[p] != current[p]}

    assert result.added == frozenset(expected_added)
    assert result.removed == frozenset(expected_removed)
    assert result.modified == frozenset(expected_modified)

    # The three sets are pairwise disjoint.
    assert result.added.isdisjoint(result.removed)
    assert result.added.isdisjoint(result.modified)
    assert result.removed.isdisjoint(result.modified)

    # Unchanged files (present in both, identical content) appear in no set.
    unchanged = {p for p in prior_paths & current_paths if prior[p] == current[p]}
    reported = result.added | result.removed | result.modified
    assert reported.isdisjoint(unchanged)

    # Every path in either tree is either reported or unchanged (full coverage).
    assert reported | unchanged == prior_paths | current_paths
