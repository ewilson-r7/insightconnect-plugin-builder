"""Unit tests for the Plugin_Source_Provider read-only import (task 17.1; Req 24.5, 25.1-25.7).

These cover source listing with local/remote availability, plugin enumeration
from a local clone and via a mocked remote fetcher, the private-repo missing
credential rejection, and the read-only fork import: ``_custom`` vendor with the
original name retained, provenance recording, license/attribution preservation,
``icon_``/``komand_`` prefix detection, and the ``.builder/baseline/`` snapshot.

The read-only invariant, fork identity, package-prefix, and baseline-diff
*properties* (tasks 17.2-17.6) and the import error paths (task 17.7) are covered
separately; git and remote sources are mocked here (no network, no real git).
"""

from pathlib import Path

import pytest

from icplugin_builder.api.config import ProductionSourceConfig
from icplugin_builder.core.yaml_codec import load_plugin_spec
from icplugin_builder.integrations.plugin_source_provider import (
    ENHANCE_PRODUCTION_ENTRY_MODE,
    PRIVATE_SOURCE_NOTICE,
    GitCredentialRequiredError,
    PluginSourceProvider,
    ProductionPluginRef,
    SourceNotFoundError,
)
from icplugin_builder.persistence.credential_store import CredentialStore

PUBLIC_REPO = "rapid7/insightconnect-plugins"
PRIVATE_REPO = "komand-plugins"


def write_production_plugin(clone_dir, name, *, prefix="icon", vendor="rapid7", version="2.3.4"):
    """Create a minimal production plugin directory under ``clone_dir``.

    The plugin carries a spec with license/attribution ``resources`` and a
    package directory using ``prefix`` (``icon`` or ``komand``), plus a LICENSE
    file, so the copy/preserve/prefix behavior can be asserted.
    """
    plugin_dir = Path(clone_dir) / name
    package_dir = plugin_dir / f"{prefix}_{name}"
    package_dir.mkdir(parents=True)

    spec_text = (
        "plugin_spec_version: v2\n"
        f"name: {name}\n"
        f"title: {name.title()}\n"
        "description: A production plugin.\n"
        f"version: {version}\n"
        f"vendor: {vendor}\n"
        "resources:\n"
        "  source_url: https://github.com/rapid7/insightconnect-plugins\n"
        "  license_url: https://www.apache.org/licenses/LICENSE-2.0\n"
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
    (plugin_dir / "LICENSE").write_text("Apache 2.0\n", encoding="utf-8")
    return plugin_dir


@pytest.fixture
def projects_root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture
def public_clone(tmp_path):
    clone = tmp_path / "public_clone"
    clone.mkdir()
    write_production_plugin(clone, "jira", prefix="icon", vendor="rapid7")
    write_production_plugin(clone, "legacy_tool", prefix="komand", vendor="komand")
    # A non-plugin directory (no spec) must be ignored by listing.
    (clone / "not_a_plugin").mkdir()
    return clone


def public_source(local_path=None):
    return ProductionSourceConfig(
        id="rapid7_public",
        repo=PUBLIC_REPO,
        visibility="public",
        local_path=str(local_path) if local_path else None,
        remote_url="https://github.com/rapid7/insightconnect-plugins.git",
    )


def private_source(local_path=None, git_credential_id="komand_git"):
    return ProductionSourceConfig(
        id="komand_private",
        repo=PRIVATE_REPO,
        visibility="private",
        local_path=str(local_path) if local_path else None,
        remote_url="https://github.com/komand-plugins.git",
        git_credential_id=git_credential_id,
    )


class FakeRemoteFetcher:
    """A stand-in remote source backed by an on-disk fixture clone."""

    def __init__(self, backing_clone):
        self.backing_clone = Path(backing_clone)
        self.list_calls = []
        self.fetch_calls = []

    def list_plugins(self, source, *, credential):
        self.list_calls.append((source.id, credential))
        return sorted(child.name for child in self.backing_clone.iterdir() if (child / "plugin.spec.yaml").is_file())

    def fetch_plugin(self, source, name, destination, *, credential):
        self.fetch_calls.append((source.id, name, credential))
        src = self.backing_clone / name
        for child in src.iterdir():
            target = Path(destination) / child.name
            if child.is_dir():
                import shutil

                shutil.copytree(child, target)
            else:
                target.write_bytes(child.read_bytes())


class TestListSources:
    def test_reports_local_and_remote_availability(self, public_clone, projects_root, tmp_path):
        fetcher = FakeRemoteFetcher(public_clone)
        provider = PluginSourceProvider(
            [public_source(public_clone), private_source(local_path=None)],
            projects_root,
            remote_fetcher=fetcher,
        )
        by_id = {s.id: s for s in provider.list_sources()}

        assert by_id["rapid7_public"].local_available is True
        assert by_id["rapid7_public"].remote_available is True
        # Private source has no local clone but a remote_url + fetcher.
        assert by_id["komand_private"].local_available is False
        assert by_id["komand_private"].remote_available is True

    def test_remote_unavailable_without_fetcher(self, projects_root):
        provider = PluginSourceProvider([public_source(local_path=None)], projects_root)
        (avail,) = provider.list_sources()
        assert avail.local_available is False
        assert avail.remote_available is False


class TestListPlugins:
    def test_lists_local_plugins_with_prefix(self, public_clone, projects_root):
        provider = PluginSourceProvider([public_source(public_clone)], projects_root)
        plugins = provider.list_plugins("rapid7_public")

        assert ProductionPluginRef(name="jira", package_prefix="icon") in plugins
        assert ProductionPluginRef(name="legacy_tool", package_prefix="komand") in plugins
        assert all(ref.name != "not_a_plugin" for ref in plugins)

    def test_falls_back_to_remote_when_no_local_clone(self, public_clone, projects_root):
        fetcher = FakeRemoteFetcher(public_clone)
        provider = PluginSourceProvider([public_source(local_path=None)], projects_root, remote_fetcher=fetcher)
        names = {ref.name for ref in provider.list_plugins("rapid7_public")}

        assert {"jira", "legacy_tool"} <= names
        assert fetcher.list_calls == [("rapid7_public", None)]

    def test_unknown_source_raises(self, projects_root):
        provider = PluginSourceProvider([public_source(local_path=None)], projects_root)
        with pytest.raises(SourceNotFoundError):
            provider.list_plugins("nope")

    def test_private_remote_without_credential_rejected(self, public_clone, projects_root):
        fetcher = FakeRemoteFetcher(public_clone)
        provider = PluginSourceProvider([private_source(local_path=None)], projects_root, remote_fetcher=fetcher)
        with pytest.raises(GitCredentialRequiredError):
            provider.list_plugins("komand_private")
        assert fetcher.list_calls == []  # rejected before any remote call


class TestImportPlugin:
    def test_forks_into_new_project_folder_with_custom_vendor(self, public_clone, projects_root):
        provider = PluginSourceProvider([public_source(public_clone)], projects_root)
        result = provider.import_plugin("rapid7_public", "jira")

        folder = result.project_folder
        # Original name retained; new folder created under projects_root.
        assert folder.plugin_name == "jira"
        assert folder.path == projects_root / "jira"

        spec = load_plugin_spec(folder.spec_path.read_text(encoding="utf-8"))
        assert spec.name == "jira"  # original name retained (Req 25.4)
        assert spec.vendor == "rapid7_custom"  # _custom applied (Req 25.4)

    def test_records_provenance(self, public_clone, projects_root):
        provider = PluginSourceProvider([public_source(public_clone)], projects_root)
        result = provider.import_plugin("rapid7_public", "jira")

        prov = result.provenance
        assert prov.entry_mode == ENHANCE_PRODUCTION_ENTRY_MODE
        assert prov.source_repo == PUBLIC_REPO
        assert prov.source_visibility == "public"
        assert prov.source_location == "local_clone"
        assert prov.original_plugin_name == "jira"
        assert prov.original_version == "2.3.4"

        # Provenance and prefix persist in the project metadata.
        metadata = result.project_folder.metadata()
        assert metadata.provenance == prov
        assert metadata.package_prefix == "icon"

    def test_does_not_modify_source(self, public_clone, projects_root):
        before = {p: p.read_bytes() for p in (public_clone / "jira").rglob("*") if p.is_file()}
        provider = PluginSourceProvider([public_source(public_clone)], projects_root)
        provider.import_plugin("rapid7_public", "jira")
        after = {p: p.read_bytes() for p in (public_clone / "jira").rglob("*") if p.is_file()}
        assert before == after  # read-only invariant (Req 25.3)

    def test_copies_code_and_preserves_license(self, public_clone, projects_root):
        provider = PluginSourceProvider([public_source(public_clone)], projects_root)
        result = provider.import_plugin("rapid7_public", "jira")
        root = result.project_folder.path

        assert (root / "icon_jira" / "action.py").read_text(encoding="utf-8") == "# hand-written logic\n"
        assert (root / "LICENSE").read_text(encoding="utf-8") == "Apache 2.0\n"
        # Attribution/license references in resources survive the round trip (Req 25.5).
        spec = load_plugin_spec((root / "plugin.spec.yaml").read_text(encoding="utf-8"))
        assert spec.extra["resources"]["source_url"].startswith("https://github.com/rapid7")
        assert "license_url" in spec.extra["resources"]

    def test_stores_readonly_baseline_snapshot(self, public_clone, projects_root):
        provider = PluginSourceProvider([public_source(public_clone)], projects_root)
        result = provider.import_plugin("rapid7_public", "jira")
        baseline = result.project_folder.path / ".builder" / "baseline"

        assert (baseline / "plugin.spec.yaml").is_file()
        assert (baseline / "icon_jira" / "action.py").is_file()
        # The baseline keeps the ORIGINAL (unmodified) vendor for fork diffs.
        baseline_spec = load_plugin_spec((baseline / "plugin.spec.yaml").read_text(encoding="utf-8"))
        assert baseline_spec.vendor == "rapid7"

    def test_detects_komand_prefix(self, public_clone, projects_root):
        provider = PluginSourceProvider([public_source(public_clone)], projects_root)
        result = provider.import_plugin("rapid7_public", "legacy_tool")

        assert result.package_prefix == "komand"
        assert (result.project_folder.path / "komand_legacy_tool" / "action.py").is_file()

    def test_no_private_notice_for_public_source(self, public_clone, projects_root):
        provider = PluginSourceProvider([public_source(public_clone)], projects_root)
        result = provider.import_plugin("rapid7_public", "jira")
        assert result.private_source_notice is None

    def test_private_source_flags_notice(self, tmp_path, projects_root):
        private_clone = tmp_path / "private_clone"
        private_clone.mkdir()
        write_production_plugin(private_clone, "secret_tool", prefix="icon", vendor="komand")

        provider = PluginSourceProvider([private_source(local_path=private_clone)], projects_root)
        result = provider.import_plugin("komand_private", "secret_tool")

        assert result.private_source_notice == PRIVATE_SOURCE_NOTICE
        assert result.provenance.source_visibility == "private"

    def test_imports_from_remote_with_credential(self, public_clone, tmp_path, projects_root):
        # Private source, no local clone: fetch via the mocked remote using the
        # stored git credential (Req 25.2).
        store = CredentialStore(tmp_path / "creds.enc", master_secret="master")
        store.store("komand_git", "ghp_token_value")
        fetcher = FakeRemoteFetcher(public_clone)

        provider = PluginSourceProvider(
            [private_source(local_path=None)],
            projects_root,
            credential_store=store,
            remote_fetcher=fetcher,
        )
        result = provider.import_plugin("komand_private", "jira")

        assert result.source_location == "remote_github"
        assert fetcher.fetch_calls == [("komand_private", "jira", "ghp_token_value")]
        assert (result.project_folder.path / "icon_jira" / "action.py").is_file()
        assert result.private_source_notice == PRIVATE_SOURCE_NOTICE
