"""Unit tests for Plugin_Source_Provider import error paths (task 17.7; Req 25.9, 25.10).

These exercise the failure branches of
:class:`~icplugin_builder.integrations.plugin_source_provider.PluginSourceProvider`:

* a private-source remote fetch attempted without a git credential is rejected
  *before* any network/fetch call with
  :class:`GitCredentialRequiredError` (Req 25.9); and
* importing an unreadable or non-conforming production plugin (missing spec,
  non-conforming spec, or no recognized package prefix) raises
  :class:`PluginImportError` with a specific message and leaves **no** partial
  draft behind in the projects root (Req 25.10).

The remote fetcher is mocked (no network, no real git) and all filesystem work
uses pytest ``tmp_path`` temp dirs.
"""

from pathlib import Path

import pytest

from icplugin_builder.api.config import ProductionSourceConfig
from icplugin_builder.integrations.plugin_source_provider import (
    GitCredentialRequiredError,
    PluginImportError,
    PluginSourceProvider,
)
from icplugin_builder.persistence.credential_store import CredentialStore

PUBLIC_REPO = "rapid7/insightconnect-plugins"
PRIVATE_REPO = "komand-plugins"


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


class RecordingRemoteFetcher:
    """A remote fetcher that records calls and fails if anything is fetched.

    Used to prove the credential rejection happens before any network/fetch
    call: any invocation of ``list_plugins``/``fetch_plugin`` is recorded, so an
    empty call log demonstrates the fetch was rejected up front.
    """

    def __init__(self):
        self.list_calls = []
        self.fetch_calls = []

    def list_plugins(self, source, *, credential):
        self.list_calls.append((source.id, credential))
        return []

    def fetch_plugin(self, source, name, destination, *, credential):
        self.fetch_calls.append((source.id, name, credential))


def write_plugin_dir(clone_dir, name, *, spec_text=None, prefix="icon", version="2.3.4"):
    """Create a production-style plugin directory under a local clone.

    Args are chosen so individual conformance failures can be induced:
    pass ``spec_text=None`` to omit the spec file, a malformed ``spec_text`` for
    a non-conforming spec, or ``prefix=None`` to omit the package directory.
    """
    plugin_dir = Path(clone_dir) / name
    plugin_dir.mkdir(parents=True)

    if prefix is not None:
        package_dir = plugin_dir / f"{prefix}_{name}"
        package_dir.mkdir()
        (package_dir / "__init__.py").write_text("", encoding="utf-8")
        (package_dir / "action.py").write_text("# logic\n", encoding="utf-8")

    if spec_text is None:
        default_spec = (
            "plugin_spec_version: v2\n"
            f"name: {name}\n"
            f"title: {name.title()}\n"
            "description: A production plugin.\n"
            f"version: {version}\n"
            "vendor: rapid7\n"
        )
        spec_text = default_spec
    if spec_text is not False:  # False means "write no spec file at all"
        (plugin_dir / "plugin.spec.yaml").write_text(spec_text, encoding="utf-8")
    return plugin_dir


@pytest.fixture
def projects_root(tmp_path):
    return tmp_path / "projects"


def assert_no_partial_draft(projects_root, name):
    """Assert the failed import left no ``Project_Folder`` behind (Req 25.10)."""
    draft = Path(projects_root) / name
    assert not draft.exists(), f"a partial draft was left behind at {draft}"


class TestMissingGitCredentialRejectedBeforeFetch:
    """Req 25.9: a private remote fetch without a credential is rejected up front."""

    def test_import_rejected_before_any_fetch_call(self, projects_root):
        fetcher = RecordingRemoteFetcher()
        provider = PluginSourceProvider(
            [private_source(local_path=None)],
            projects_root,
            remote_fetcher=fetcher,
        )

        with pytest.raises(GitCredentialRequiredError):
            provider.import_plugin("komand_private", "jira")

        # Rejected before any network/fetch call was made (Req 25.9).
        assert fetcher.fetch_calls == []
        assert fetcher.list_calls == []
        assert_no_partial_draft(projects_root, "jira")

    def test_credential_store_present_but_empty_is_rejected(self, tmp_path, projects_root):
        # A store exists but holds no credential for the configured id: still a
        # missing credential, so the fetch is rejected before contacting remote.
        store = CredentialStore(tmp_path / "creds.enc", master_secret="master")
        fetcher = RecordingRemoteFetcher()
        provider = PluginSourceProvider(
            [private_source(local_path=None)],
            projects_root,
            credential_store=store,
            remote_fetcher=fetcher,
        )

        with pytest.raises(GitCredentialRequiredError):
            provider.import_plugin("komand_private", "jira")

        assert fetcher.fetch_calls == []
        assert_no_partial_draft(projects_root, "jira")


class TestUnreadableOrNonConformingImportRaises:
    """Req 25.10: unreadable/non-conforming plugins raise PluginImportError, no draft."""

    def test_missing_spec_file_raises_read_error(self, tmp_path, projects_root):
        clone = tmp_path / "clone"
        clone.mkdir()
        write_plugin_dir(clone, "nospec", spec_text=False, prefix="icon")
        provider = PluginSourceProvider([public_source(clone)], projects_root)

        with pytest.raises(PluginImportError) as excinfo:
            provider.import_plugin("rapid7_public", "nospec")

        assert "plugin.spec.yaml" in str(excinfo.value)
        assert_no_partial_draft(projects_root, "nospec")

    def test_non_conforming_spec_raises_schema_error(self, tmp_path, projects_root):
        clone = tmp_path / "clone"
        clone.mkdir()
        # A YAML list is not a valid plugin spec mapping.
        write_plugin_dir(clone, "badspec", spec_text="- not\n- a\n- mapping\n", prefix="icon")
        provider = PluginSourceProvider([public_source(clone)], projects_root)

        with pytest.raises(PluginImportError) as excinfo:
            provider.import_plugin("rapid7_public", "badspec")

        assert "does not conform" in str(excinfo.value)
        assert_no_partial_draft(projects_root, "badspec")

    def test_invalid_version_spec_raises_schema_error(self, tmp_path, projects_root):
        clone = tmp_path / "clone"
        clone.mkdir()
        bad_version_spec = (
            "plugin_spec_version: v2\n"
            "name: badver\n"
            "title: Badver\n"
            "description: A production plugin.\n"
            "version: not-a-semver\n"
            "vendor: rapid7\n"
        )
        write_plugin_dir(clone, "badver", spec_text=bad_version_spec, prefix="icon")
        provider = PluginSourceProvider([public_source(clone)], projects_root)

        with pytest.raises(PluginImportError) as excinfo:
            provider.import_plugin("rapid7_public", "badver")

        assert "does not conform" in str(excinfo.value)
        assert_no_partial_draft(projects_root, "badver")

    def test_no_recognized_package_prefix_raises(self, tmp_path, projects_root):
        clone = tmp_path / "clone"
        clone.mkdir()
        # Valid spec, but no icon_/komand_ package directory present.
        write_plugin_dir(clone, "noprefix", prefix=None)
        provider = PluginSourceProvider([public_source(clone)], projects_root)

        with pytest.raises(PluginImportError) as excinfo:
            provider.import_plugin("rapid7_public", "noprefix")

        message = str(excinfo.value)
        assert "no recognized package directory" in message
        assert "icon_" in message and "komand_" in message
        assert_no_partial_draft(projects_root, "noprefix")

    def test_non_conforming_remote_fetch_raises_and_cleans_up(self, tmp_path, projects_root):
        # Non-conforming plugin arriving via the mocked remote fetcher: the
        # import must still raise PluginImportError with no partial draft.
        backing = tmp_path / "remote_backing"
        backing.mkdir()
        write_plugin_dir(backing, "badspec", spec_text="- not\n- a\n- mapping\n", prefix="icon")

        class FetchFromBacking(RecordingRemoteFetcher):
            def fetch_plugin(self, source, name, destination, *, credential):
                super().fetch_plugin(source, name, destination, credential=credential)
                src = backing / name
                for child in src.iterdir():
                    target = Path(destination) / child.name
                    if child.is_dir():
                        import shutil

                        shutil.copytree(child, target)
                    else:
                        target.write_bytes(child.read_bytes())

        store = CredentialStore(tmp_path / "creds.enc", master_secret="master")
        store.store("komand_git", "ghp_token_value")
        fetcher = FetchFromBacking()
        provider = PluginSourceProvider(
            [private_source(local_path=None)],
            projects_root,
            credential_store=store,
            remote_fetcher=fetcher,
        )

        with pytest.raises(PluginImportError):
            provider.import_plugin("komand_private", "badspec")

        assert fetcher.fetch_calls == [("komand_private", "badspec", "ghp_token_value")]
        assert_no_partial_draft(projects_root, "badspec")
