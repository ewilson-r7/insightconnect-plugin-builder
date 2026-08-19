"""Unit tests for the three plugin-file predicates (bugfix task 6.1, clause 2.6).

Table-driven, because the point of moving these into one module is that three
subsystems consume one answer. A table makes the three predicates' *disagreements*
legible, and the disagreements are the whole reason they are three functions:

* ``unit_test/`` is lint-excluded and **not** generated, so the tests stay
  compiled, formatted and run (`bugfix.md` 3.7);
* a generated ``schema.py`` is lint-excluded and **still packaged**, because the
  plugin cannot run without it;
* ``.builder/`` is all three, being neither the plugin's code nor its content.
"""

from pathlib import Path

import pytest

from icplugin_builder.core.plugin_files import (
    GENERATED_DIR_NAMES,
    GENERATED_FILE_NAMES,
    PACKAGING_EXCLUDED_DIR_NAMES,
    UNIT_TEST_DIR,
    hand_written_python,
    is_generated,
    is_lint_excluded,
    is_packaging_excluded,
)

#: ``(path, generated, lint_excluded, packaging_excluded)``. One row per named file
#: and directory, plus the paths whose three answers differ.
CASES = (
    # Hand-written plugin code: judged by everything, packaged.
    ("icon_x/util/api.py", False, False, False),
    ("icon_x/connection/connection.py", False, False, False),
    ("icon_x/actions/get_thing/action.py", False, False, False),
    ("plugin.spec.yaml", False, False, False),
    ("requirements.txt", False, False, False),
    # Generated files: not linted, still packaged.
    ("icon_x/actions/get_thing/schema.py", True, True, False),
    ("icon_x/__init__.py", True, True, False),
    ("setup.py", True, True, False),
    ("Dockerfile", True, True, False),
    ("Makefile", True, True, False),
    ("help.md", True, True, False),
    (".CHECKSUM", True, True, False),
    # Generated directories. `bin/` holds the plugin's entry point and is packaged;
    # `build/` and `dist/` are setuptools *output* -- copies of code packaged from its
    # real location, plus built archives -- so change 8 keeps them out of the .plg.
    ("bin/icon_x", True, True, False),
    ("build/lib/icon_x/api.py", True, True, True),
    ("dist/thing.tar.gz", True, True, True),
    # The unit tests: lint-excluded, NOT generated, and packaged.
    ("unit_test/test_action.py", False, True, False),
    ("unit_test/helpers/fixtures.py", False, True, False),
    ("unit_test/responses/thing.json", False, True, False),
    # The tool's own metadata: excluded everywhere.
    (".builder/reference/swagger.yaml", True, True, True),
    (".builder/provenance.json", True, True, True),
    # Transient directories: not packaged. `.git` and `__pycache__` are also
    # generated-or-vendored; the cache directories are packaging-only.
    (".git/config", True, True, True),
    ("icon_x/__pycache__/api.cpython-313.pyc", True, True, True),
    (".pytest_cache/v/cache/lastfailed", False, False, True),
    (".mypy_cache/3.13/icon_x/api.data.json", False, False, True),
    ("icon_x/util/.mypy_cache/x.json", False, False, True),
)


@pytest.mark.parametrize("path,generated,lint_excluded,packaging_excluded", CASES, ids=[case[0] for case in CASES])
def test_the_three_predicates_agree_with_the_table(path, generated, lint_excluded, packaging_excluded):
    assert is_generated(path) is generated, f"is_generated({path!r})"
    assert is_lint_excluded(path) is lint_excluded, f"is_lint_excluded({path!r})"
    assert is_packaging_excluded(path) is packaging_excluded, f"is_packaging_excluded({path!r})"


def test_every_named_generated_file_is_generated_at_any_depth():
    """A plugin can put these at the root or several levels down."""
    for name in GENERATED_FILE_NAMES:
        assert is_generated(name)
        assert is_generated(f"icon_x/{name}")
        assert is_generated(f"icon_x/actions/get_thing/{name}")


def test_every_named_generated_directory_is_generated_at_any_depth():
    for name in GENERATED_DIR_NAMES:
        assert is_generated(f"{name}/thing.py")
        assert is_generated(f"icon_x/{name}/thing.py")


def test_every_packaging_excluded_directory_is_excluded_at_any_depth():
    for name in PACKAGING_EXCLUDED_DIR_NAMES:
        assert is_packaging_excluded(f"{name}/thing")
        assert is_packaging_excluded(f"icon_x/deep/{name}/thing")


def test_byproduct_files_are_excluded_from_packaging():
    """Change 8's addition: the file half a directory-name set could not express.

    Wave 8 left this predicate directory-only on purpose, so that adding the
    byproduct patterns would read as a change rather than as a test quietly starting
    to pass. This is that change: coverage data at any depth, coverage.py's
    per-process files under parallel mode, loose bytecode beside its source, the
    setuptools build and egg-info trees, and the tarball the generated Makefile
    writes.
    """
    for path in (
        ".coverage",
        "unit_test/.coverage",
        "icon_x/util/.coverage",
        ".coverage.host.12345.678901",
        "icon_x/util/api.pyc",
        "icon_x/util/api.pyo",
        "build/lib/icon_x/util/api.py",
        "jumpcloud_rapid7_plugin.egg-info/PKG-INFO",
        "rapid7-jumpcloud-1.0.0.tar.gz",
        "thing.tgz",
    ):
        assert is_packaging_excluded(path), path


def test_a_file_named_build_is_still_packaged():
    """``build`` is excluded at directory positions only.

    Matching it anywhere would drop a plugin file that happened to be called
    ``build``, which is a different thing from the directory ``setup.py build``
    writes.
    """
    assert not is_packaging_excluded("build")
    assert not is_packaging_excluded("icon_x/build")
    assert is_packaging_excluded("build/lib/thing.py")


def test_hand_written_and_generated_plugin_files_are_still_packaged():
    """The other half of 3.2: the exclusion must not reach anything the plugin needs."""
    for path in (
        "plugin.spec.yaml",
        "requirements.txt",
        "icon_x/util/api.py",
        "unit_test/test_api.py",
        "setup.py",
        "Dockerfile",
        "help.md",
        ".CHECKSUM",
        "bin/icon_x",
    ):
        assert not is_packaging_excluded(path), path


def test_a_unit_test_path_is_lint_excluded_but_not_generated():
    """The distinction 3.7 turns on, asserted on its own so it cannot erode."""
    path = f"{UNIT_TEST_DIR}/test_action.py"
    assert is_lint_excluded(path)
    assert not is_generated(path)
    assert not is_packaging_excluded(path)


def test_an_empty_path_is_not_generated():
    assert not is_generated("")
    assert not is_lint_excluded("")
    assert not is_packaging_excluded("")


class TestHandWrittenPython:
    def test_lists_only_hand_written_python_sorted(self, tmp_path):
        for relative in (
            "icon_x/util/api.py",
            "icon_x/connection/connection.py",
            "icon_x/actions/get_thing/schema.py",
            "icon_x/actions/get_thing/action.py",
            "setup.py",
            "unit_test/test_action.py",
            "help.md",
        ):
            target = tmp_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x = 1\n", encoding="utf-8")

        assert hand_written_python(tmp_path) == (
            "icon_x/actions/get_thing/action.py",
            "icon_x/connection/connection.py",
            "icon_x/util/api.py",
            "unit_test/test_action.py",
        )

    def test_a_missing_directory_yields_nothing(self, tmp_path):
        assert hand_written_python(tmp_path / "absent") == ()

    def test_a_file_rather_than_a_directory_yields_nothing(self, tmp_path):
        target = tmp_path / "api.py"
        target.write_text("x = 1\n", encoding="utf-8")
        assert hand_written_python(target) == ()

    def test_the_unit_tests_are_hand_written_python(self, tmp_path):
        """They are format-checked and compiled, so they have to appear here (3.7)."""
        target = Path(tmp_path) / UNIT_TEST_DIR / "test_action.py"
        target.parent.mkdir(parents=True)
        target.write_text("x = 1\n", encoding="utf-8")
        assert hand_written_python(tmp_path) == (f"{UNIT_TEST_DIR}/test_action.py",)
