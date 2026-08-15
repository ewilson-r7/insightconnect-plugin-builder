"""Unit tests for the ``insight-plugin`` CLI wrapper (task 13.1; Req 3.1, 22.3).

These cover the deterministic scaffolding wrapper: staging the spec, invoking
``insight-plugin create``/``refresh`` through a *mocked* subprocess (the real
binary is never required), snapshotting the resulting working tree, filtering
the regenerated derived files, and the failure paths (non-zero exit, missing
binary, missing project directory). The zero-LLM-call and structural-refresh
*properties* are covered separately by the property tests (tasks 13.3, 13.6).

The subprocess is mocked by monkeypatching ``asyncio.create_subprocess_exec``;
tests drive the coroutines with ``asyncio.run`` so no async test plugin is
required.
"""

import asyncio
from pathlib import Path

import pytest

from icplugin_builder.core.spec_model import PluginSpec, SemVer
from icplugin_builder.integrations import insight_plugin_cli as ipc
from icplugin_builder.integrations.insight_plugin_cli import (
    DERIVED_FILE_NAMES,
    CommandResult,
    InsightPluginCli,
    InsightPluginCliError,
    ProjectTree,
    snapshot_tree,
)


def make_spec(name="my_plugin", version=SemVer(1, 0, 0), vendor="acme_custom"):
    return PluginSpec(
        name=name,
        title="My Plugin",
        description="A test plugin.",
        version=version,
        vendor=vendor,
    )


class FakeProcess:
    """A stand-in for the object returned by ``create_subprocess_exec``."""

    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


def install_fake_exec(monkeypatch, *, returncode=0, stdout=b"", stderr=b"", side_effect=None):
    """Patch ``asyncio.create_subprocess_exec`` and record each invocation.

    ``side_effect`` (if given) is called with ``(command, cwd)`` before the fake
    process is returned, letting a test simulate the CLI writing files to disk.
    Returns the list that collects ``(command, cwd)`` tuples.
    """
    calls = []

    async def fake_exec(*command, cwd=None, stdout=None, stderr=None):
        calls.append((list(command), cwd))
        if side_effect is not None:
            side_effect(list(command), cwd)
        return FakeProcess(returncode=returncode, stdout=out, stderr=err)

    out, err = stdout, stderr
    monkeypatch.setattr(ipc.asyncio, "create_subprocess_exec", fake_exec)
    return calls


def scaffold_into(root):
    """Simulate ``insight-plugin create`` producing a plugin tree at ``root``."""

    def side_effect(command, cwd):
        (root / "icon_my_plugin").mkdir(parents=True, exist_ok=True)
        (root / "icon_my_plugin" / "schema.py").write_text("# schema\n", encoding="utf-8")
        (root / "icon_my_plugin" / "__init__.py").write_text("", encoding="utf-8")
        (root / "Dockerfile").write_text("FROM python:3.11\n", encoding="utf-8")
        (root / "Makefile").write_text("build:\n", encoding="utf-8")
        (root / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")
        (root / "help.md").write_text("# Help\n", encoding="utf-8")
        (root / ".CHECKSUM").write_text("{}\n", encoding="utf-8")

    return side_effect


class TestCreate:
    """``create`` scaffolds ``<projects_root>/<name>``, not ``projects_root`` itself.

    ``insight-plugin create`` is invoked from the *parent* of the plugin and
    always creates a subdirectory named after the plugin (the ``cd plugins/`` then
    ``insight-plugin create`` step of the documented workflow). The argument is
    therefore the projects root, and the resulting tree lives one level below it.
    """

    def test_stages_spec_out_of_tree_and_invokes_create_from_the_parent(self, tmp_path, monkeypatch):
        projects_root = tmp_path / "projects"
        target = projects_root / "my_plugin"
        calls = install_fake_exec(monkeypatch, side_effect=scaffold_into(target))

        tree = asyncio.run(InsightPluginCli().create(make_spec(), projects_root))

        assert len(calls) == 1
        command, cwd = calls[0]
        # Invoked from the parent so the CLI creates the plugin subdirectory.
        assert cwd == str(projects_root)
        assert command[:2] == ["insight-plugin", "create"]

        # The spec is staged outside the tree and passed by absolute path, so the
        # projects root gains only the plugin directory -- no stray spec file.
        staged = Path(command[2])
        assert staged.is_absolute()
        assert staged.name == "plugin.spec.yaml"
        assert not (projects_root / "plugin.spec.yaml").exists()
        # The staging directory is temporary and cleaned up after the run.
        assert not staged.exists()

        # The returned tree is rooted at the created plugin directory.
        assert isinstance(tree, ProjectTree)
        assert tree.root == target
        assert "icon_my_plugin/schema.py" in tree.files

    def test_returns_tree_with_generated_derived_files(self, tmp_path, monkeypatch):
        projects_root = tmp_path / "projects"
        target = projects_root / "my_plugin"
        install_fake_exec(monkeypatch, side_effect=scaffold_into(target))

        tree = asyncio.run(InsightPluginCli().create(make_spec(), projects_root))

        derived_names = {name.rsplit("/", 1)[-1] for name in tree.derived_files()}
        assert derived_names == set(DERIVED_FILE_NAMES)
        assert tree.files["Dockerfile"] == "FROM python:3.11\n"
        assert tree.files["icon_my_plugin/schema.py"] == "# schema\n"

    def test_detects_the_package_prefix_actually_used(self, tmp_path, monkeypatch):
        projects_root = tmp_path / "projects"
        target = projects_root / "my_plugin"
        install_fake_exec(monkeypatch, side_effect=scaffold_into(target))
        tree = asyncio.run(InsightPluginCli().create(make_spec(), projects_root))
        assert tree.package_prefix() == "icon"

    def test_refuses_to_scaffold_over_an_existing_directory(self, tmp_path, monkeypatch):
        # The CLI declines in this case while still exiting 0, so guarding here
        # is what stops a silent no-op being reported as a successful scaffold.
        projects_root = tmp_path / "projects"
        (projects_root / "my_plugin").mkdir(parents=True)
        calls = install_fake_exec(monkeypatch)

        with pytest.raises(InsightPluginCliError) as excinfo:
            asyncio.run(InsightPluginCli().create(make_spec(), projects_root))

        assert "existing directory" in str(excinfo.value)
        assert calls == []  # rejected before launching the CLI

    def test_zero_exit_that_produced_no_tree_is_an_error(self, tmp_path, monkeypatch):
        # Guards the same silent-no-op path from the other side: exit 0 is not
        # sufficient evidence that scaffolding happened.
        projects_root = tmp_path / "projects"
        install_fake_exec(monkeypatch, stdout=b"WARNING: directory exists")

        with pytest.raises(InsightPluginCliError) as excinfo:
            asyncio.run(InsightPluginCli().create(make_spec(), projects_root))
        assert "did not produce" in str(excinfo.value)

    def test_rejects_a_spec_without_a_name(self, tmp_path, monkeypatch):
        calls = install_fake_exec(monkeypatch)
        with pytest.raises(InsightPluginCliError) as excinfo:
            asyncio.run(InsightPluginCli().create(make_spec(name=""), tmp_path / "projects"))
        assert "no name" in str(excinfo.value)
        assert calls == []

    def test_nonzero_exit_raises_with_output(self, tmp_path, monkeypatch):
        install_fake_exec(monkeypatch, returncode=2, stderr=b"boom")
        with pytest.raises(InsightPluginCliError) as excinfo:
            asyncio.run(InsightPluginCli().create(make_spec(), tmp_path / "projects"))
        error = excinfo.value
        assert error.returncode == 2
        assert error.stderr == "boom"
        assert error.command[:2] == ("insight-plugin", "create")

    def test_missing_executable_raises_actionable_error(self, tmp_path, monkeypatch):
        async def fake_exec(*command, cwd=None, stdout=None, stderr=None):
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(ipc.asyncio, "create_subprocess_exec", fake_exec)

        with pytest.raises(InsightPluginCliError) as excinfo:
            asyncio.run(InsightPluginCli(executable="does-not-exist").create(make_spec(), tmp_path / "w"))
        assert "not found" in str(excinfo.value)


class TestRefresh:
    def test_invokes_refresh_in_project_dir(self, tmp_path, monkeypatch):
        project = tmp_path / "plugin"
        project.mkdir()
        calls = install_fake_exec(monkeypatch)

        tree = asyncio.run(InsightPluginCli().refresh(project))

        assert len(calls) == 1
        command, cwd = calls[0]
        assert command == ["insight-plugin", "refresh"]
        assert cwd == str(project)
        assert isinstance(tree, ProjectTree)

    def test_regenerates_derived_files(self, tmp_path, monkeypatch):
        project = tmp_path / "plugin"
        project.mkdir()
        (project / "plugin.spec.yaml").write_text("name: p\n", encoding="utf-8")

        def regen(command, cwd):
            (project / "help.md").write_text("# refreshed\n", encoding="utf-8")
            (project / ".CHECKSUM").write_text("{}\n", encoding="utf-8")

        install_fake_exec(monkeypatch, side_effect=regen)

        tree = asyncio.run(InsightPluginCli().refresh(project))
        assert tree.has_derived_file("help.md")
        assert tree.files["help.md"] == "# refreshed\n"

    def test_missing_project_dir_raises(self, tmp_path, monkeypatch):
        install_fake_exec(monkeypatch)
        with pytest.raises(InsightPluginCliError):
            asyncio.run(InsightPluginCli().refresh(tmp_path / "nope"))

    def test_nonzero_exit_raises(self, tmp_path, monkeypatch):
        project = tmp_path / "plugin"
        project.mkdir()
        install_fake_exec(monkeypatch, returncode=1, stderr=b"refresh failed")
        with pytest.raises(InsightPluginCliError) as excinfo:
            asyncio.run(InsightPluginCli().refresh(project))
        assert excinfo.value.stderr == "refresh failed"


class TestSnapshotTree:
    def test_reads_files_relative_to_root(self, tmp_path):
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "a.py").write_text("a\n", encoding="utf-8")
        (tmp_path / "Dockerfile").write_text("FROM x\n", encoding="utf-8")

        tree = snapshot_tree(tmp_path)
        assert tree.files == {"Dockerfile": "FROM x\n", "pkg/a.py": "a\n"}

    def test_skips_ignored_directories(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("x\n", encoding="utf-8")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "m.pyc").write_bytes(b"\x00\x01")
        (tmp_path / "keep.py").write_text("keep\n", encoding="utf-8")

        tree = snapshot_tree(tmp_path)
        assert tree.files == {"keep.py": "keep\n"}

    def test_retains_binary_as_bytes(self, tmp_path):
        (tmp_path / "icon.png").write_bytes(b"\x89PNG\xff\xfe")
        tree = snapshot_tree(tmp_path)
        assert tree.files["icon.png"] == b"\x89PNG\xff\xfe"

    def test_derived_files_filters_by_name(self, tmp_path):
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "schema.py").write_text("s\n", encoding="utf-8")
        (tmp_path / "pkg" / "action.py").write_text("logic\n", encoding="utf-8")
        (tmp_path / "help.md").write_text("h\n", encoding="utf-8")

        tree = snapshot_tree(tmp_path)
        assert set(tree.derived_files()) == {"pkg/schema.py", "help.md"}


class TestCommandResult:
    def test_ok_reflects_returncode(self):
        assert CommandResult(("insight-plugin",), 0, "", "").ok is True
        assert CommandResult(("insight-plugin",), 1, "", "").ok is False
