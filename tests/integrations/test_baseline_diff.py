"""Unit tests for the fork baseline diff (task 17.5; Req 25.8).

``baseline_diff`` compares a production fork's current draft working tree against
the immutable ``.builder/baseline/`` snapshot captured at import time, reporting
the added/removed/modified files. These tests build a real fork via the
read-only import (local clone, no network) and then edit the draft to exercise
each partition, plus the tool-metadata exclusion and the not-a-fork rejection.
"""

from pathlib import Path

import pytest

from icplugin_builder.api.config import ProductionSourceConfig
from icplugin_builder.core.spec_model import PluginSpec
from icplugin_builder.integrations.plugin_source_provider import (
    BaselineNotFoundError,
    PluginSourceProvider,
    baseline_diff,
)
from icplugin_builder.persistence.project_folder import ProjectFolder


def write_production_plugin(clone_dir, name, *, prefix="icon", vendor="rapid7"):
    """Create a minimal production plugin directory under ``clone_dir``."""
    plugin_dir = Path(clone_dir) / name
    package_dir = plugin_dir / f"{prefix}_{name}"
    package_dir.mkdir(parents=True)

    spec_text = (
        "plugin_spec_version: v2\n"
        f"name: {name}\n"
        f"title: {name.title()}\n"
        "description: A production plugin.\n"
        "version: 2.3.4\n"
        f"vendor: {vendor}\n"
        "actions:\n"
        "  do_thing:\n"
        "    title: Do Thing\n"
        "    description: Does a thing.\n"
        "    input:\n"
        "      host:\n"
        "        type: string\n"
        "        required: true\n"
    )
    (plugin_dir / "plugin.spec.yaml").write_text(spec_text, encoding="utf-8")
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "action.py").write_text("# hand-written logic\n", encoding="utf-8")
    (plugin_dir / "help.md").write_text("# Help\n", encoding="utf-8")
    return plugin_dir


def public_source(local_path):
    return ProductionSourceConfig(
        id="rapid7_public",
        repo="rapid7/insightconnect-plugins",
        visibility="public",
        local_path=str(local_path),
        remote_url="https://github.com/rapid7/insightconnect-plugins.git",
    )


@pytest.fixture
def projects_root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture
def public_clone(tmp_path):
    clone = tmp_path / "public_clone"
    clone.mkdir()
    write_production_plugin(clone, "jira", prefix="icon", vendor="rapid7")
    return clone


@pytest.fixture
def fork(public_clone, projects_root):
    """A freshly imported production fork with an unmodified draft."""
    provider = PluginSourceProvider([public_source(public_clone)], projects_root)
    return provider.import_plugin("rapid7_public", "jira")


class TestBaselineDiff:
    def test_no_edits_yields_only_spec_change(self, fork):
        """A fresh fork differs from baseline only by the ``_custom`` vendor spec.

        The import rewrites ``plugin.spec.yaml`` with the ``_custom`` vendor while
        the baseline keeps the original vendor, so the spec is the sole change and
        nothing is added or removed.
        """
        diff = baseline_diff(fork.project_folder)

        assert diff.added == frozenset()
        assert diff.removed == frozenset()
        assert diff.modified == frozenset({"plugin.spec.yaml"})
        assert not diff.first_version

    def test_added_file_is_reported(self, fork):
        folder = fork.project_folder
        (folder.path / "icon_jira" / "new_action.py").write_text("# new\n", encoding="utf-8")

        diff = baseline_diff(folder)

        assert "icon_jira/new_action.py" in diff.added
        assert "icon_jira/new_action.py" not in diff.modified

    def test_modified_file_is_reported(self, fork):
        folder = fork.project_folder
        (folder.path / "icon_jira" / "action.py").write_text("# edited logic\n", encoding="utf-8")

        diff = baseline_diff(folder)

        assert "icon_jira/action.py" in diff.modified
        assert "icon_jira/action.py" not in diff.added

    def test_removed_file_is_reported(self, fork):
        folder = fork.project_folder
        (folder.path / "help.md").unlink()

        diff = baseline_diff(folder)

        assert "help.md" in diff.removed

    def test_builder_metadata_excluded_from_diff(self, fork):
        """The tool-owned ``.builder/`` subtree never appears in the diff."""
        diff = baseline_diff(fork.project_folder)

        all_paths = diff.added | diff.removed | diff.modified
        assert not any(path.startswith(".builder/") for path in all_paths)

    def test_provider_method_matches_module_function(self, public_clone, projects_root):
        provider = PluginSourceProvider([public_source(public_clone)], projects_root)
        result = provider.import_plugin("rapid7_public", "jira")

        via_method = provider.baseline_diff(result.project_folder)
        via_function = baseline_diff(result.project_folder)

        assert via_method == via_function

    def test_missing_baseline_raises(self, projects_root):
        """A non-fork draft (no baseline snapshot) is rejected."""
        folder = ProjectFolder.create(
            projects_root,
            "net_new",
            PluginSpec(name="net_new", vendor="acme"),
        )

        with pytest.raises(BaselineNotFoundError):
            baseline_diff(folder)
