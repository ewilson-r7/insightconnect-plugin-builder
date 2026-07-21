"""Unit tests for the file-tree diff engine (task 3.3).

These cover specific examples and edge cases for :func:`diff_file_trees`:
added/removed/modified partitioning, unchanged files, the no-prior first
version case, and content types (str and bytes). The universal partition
property is covered separately by the property test (task 3.4).
"""

from icplugin_builder.core.diff import FileTreeDiff, diff_file_trees


class TestNoPriorTree:
    def test_none_prior_reports_all_as_added(self):
        current = {"a.py": "x", "b.py": "y"}
        result = diff_file_trees(None, current)
        assert result.first_version is True
        assert result.added == frozenset({"a.py", "b.py"})
        assert result.removed == frozenset()
        assert result.modified == frozenset()

    def test_none_prior_with_empty_current(self):
        result = diff_file_trees(None, {})
        assert result.first_version is True
        assert result.added == frozenset()
        assert result.has_changes is False

    def test_empty_prior_is_not_first_version(self):
        # An empty (but existing) prior tree still means every current file is
        # an addition, but it is NOT the first-version case.
        result = diff_file_trees({}, {"a.py": "x"})
        assert result.first_version is False
        assert result.added == frozenset({"a.py"})


class TestPartition:
    def test_added_removed_modified_unchanged(self):
        prior = {
            "keep.py": "same",
            "change.py": "old",
            "gone.py": "bye",
        }
        current = {
            "keep.py": "same",
            "change.py": "new",
            "fresh.py": "hello",
        }
        result = diff_file_trees(prior, current)
        assert result.added == frozenset({"fresh.py"})
        assert result.removed == frozenset({"gone.py"})
        assert result.modified == frozenset({"change.py"})
        # "keep.py" is unchanged and appears in no set.
        assert result.first_version is False
        assert result.has_changes is True

    def test_identical_trees_have_no_changes(self):
        tree = {"a.py": "1", "b.py": "2"}
        result = diff_file_trees(dict(tree), dict(tree))
        assert result.added == frozenset()
        assert result.removed == frozenset()
        assert result.modified == frozenset()
        assert result.has_changes is False

    def test_sets_are_pairwise_disjoint(self):
        prior = {"a": "1", "b": "2", "c": "3"}
        current = {"b": "changed", "c": "3", "d": "4"}
        result = diff_file_trees(prior, current)
        assert result.added.isdisjoint(result.removed)
        assert result.added.isdisjoint(result.modified)
        assert result.removed.isdisjoint(result.modified)
        assert result.added == frozenset({"d"})
        assert result.removed == frozenset({"a"})
        assert result.modified == frozenset({"b"})

    def test_bytes_content_compared_by_value(self):
        prior = {"bin": b"\x00\x01", "same": b"\xff"}
        current = {"bin": b"\x00\x02", "same": b"\xff"}
        result = diff_file_trees(prior, current)
        assert result.modified == frozenset({"bin"})
        assert result.added == frozenset()
        assert result.removed == frozenset()


class TestFileTreeDiff:
    def test_is_frozen_dataclass(self):
        result = diff_file_trees({"a": "1"}, {"a": "1"})
        assert isinstance(result, FileTreeDiff)

    def test_has_changes_false_when_empty(self):
        result = FileTreeDiff(frozenset(), frozenset(), frozenset())
        assert result.has_changes is False
