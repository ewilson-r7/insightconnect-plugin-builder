"""Unit tests for the Project_Folder layout and metadata (task 11.1; Req 21.1, 21.2, 21.4).

These cover specific examples and edge cases for creating a per-plugin folder,
saving the current spec/code/docs/artifacts, recording per-version history and
tooling stamps, and listing previously created plugins with name, current
version, and last-modification timestamp. The universal save/list fidelity and
history-retention properties are covered separately by the property tests
(tasks 11.2/11.3); the missing-content error path is covered by task 11.4.
"""

from datetime import datetime, timedelta, timezone

import pytest

from icplugin_builder.core.spec_model import PluginSpec, SemVer
from icplugin_builder.core.yaml_codec import load_plugin_spec
from icplugin_builder.persistence.project_folder import (
    ENTRY_MODE_CREATE_NEW,
    ENTRY_MODE_ENHANCE_PRODUCTION,
    ENTRY_MODE_ITERATE_CUSTOM,
    VALID_ENTRY_MODES,
    ProjectFolder,
    ProjectFolderError,
    ProjectListing,
    ProvenanceRecord,
    ToolingStamp,
    list_projects,
)


def make_spec(name="my_plugin", version=SemVer(1, 0, 0), vendor="acme_custom"):
    return PluginSpec(
        name=name,
        title="My Plugin",
        description="A test plugin.",
        version=version,
        vendor=vendor,
    )


@pytest.fixture
def projects_root(tmp_path):
    return tmp_path / "projects"


class TestCreate:
    def test_creates_folder_spec_and_metadata(self, projects_root):
        # Req 21.1: creating a plugin creates a Project_Folder on the filesystem.
        created = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        folder = ProjectFolder.create(projects_root, "my_plugin", make_spec(), created_utc=created)

        assert folder.path == projects_root / "my_plugin"
        assert folder.spec_path.exists()
        assert folder.metadata_path.exists()

        metadata = folder.metadata()
        assert metadata.plugin_name == "my_plugin"
        assert metadata.current_version == "1.0.0"
        assert metadata.created_utc == created.isoformat()
        assert metadata.last_modified_utc == created.isoformat()
        assert metadata.package_prefix == "icon"

    def test_persists_provenance(self, projects_root):
        provenance = ProvenanceRecord(
            entry_mode=ENTRY_MODE_ENHANCE_PRODUCTION,
            created_utc="2024-01-01T00:00:00+00:00",
            source_repo="rapid7/insightconnect-plugins",
            source_visibility="public",
            source_location="local_clone",
            original_plugin_name="my_plugin",
            original_version="3.2.1",
        )
        ProjectFolder.create(projects_root, "my_plugin", make_spec(), provenance=provenance)

        reopened = ProjectFolder.open(projects_root, "my_plugin").metadata()
        assert reopened.provenance == provenance

    def test_records_net_new_provenance_by_default(self, projects_root):
        # Req 24.5: every created draft carries a Provenance_Record; a draft
        # created without an explicit one defaults to net-new.
        created = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        ProjectFolder.create(projects_root, "my_plugin", make_spec(), created_utc=created)

        provenance = ProjectFolder.open(projects_root, "my_plugin").metadata().provenance
        assert provenance is not None
        assert provenance.entry_mode == ENTRY_MODE_CREATE_NEW
        assert provenance.created_utc == created.isoformat()
        # Fork fields stay unset for a net-new draft.
        assert provenance.source_repo is None
        assert provenance.original_plugin_name is None

    def test_persists_iterate_custom_provenance(self, projects_root):
        # Req 24.5: an explicit custom-iteration provenance is persisted verbatim.
        provenance = ProvenanceRecord(
            entry_mode=ENTRY_MODE_ITERATE_CUSTOM,
            created_utc="2024-01-01T00:00:00+00:00",
        )
        ProjectFolder.create(projects_root, "my_plugin", make_spec(), provenance=provenance)

        reopened = ProjectFolder.open(projects_root, "my_plugin").metadata()
        assert reopened.provenance == provenance
        assert reopened.provenance.entry_mode in VALID_ENTRY_MODES

    def test_net_new_factory_sets_entry_mode(self):
        record = ProvenanceRecord.net_new("2024-05-05T00:00:00+00:00")
        assert record.entry_mode == ENTRY_MODE_CREATE_NEW
        assert record.created_utc == "2024-05-05T00:00:00+00:00"
        assert record.source_repo is None

    def test_written_spec_round_trips(self, projects_root):
        folder = ProjectFolder.create(projects_root, "my_plugin", make_spec(version=SemVer(2, 1, 0)))
        loaded = load_plugin_spec(folder.spec_path.read_text(encoding="utf-8"))
        assert loaded.name == "my_plugin"
        assert str(loaded.version) == "2.1.0"

    def test_rejects_empty_name(self, projects_root):
        with pytest.raises(ProjectFolderError):
            ProjectFolder.create(projects_root, "   ", make_spec())

    def test_rejects_invalid_prefix(self, projects_root):
        with pytest.raises(ProjectFolderError):
            ProjectFolder.create(projects_root, "p", make_spec(), package_prefix="bogus")

    def test_rejects_existing_folder(self, projects_root):
        ProjectFolder.create(projects_root, "my_plugin", make_spec())
        with pytest.raises(ProjectFolderError):
            ProjectFolder.create(projects_root, "my_plugin", make_spec())

    def test_komand_prefix_accepted(self, projects_root):
        folder = ProjectFolder.create(
            projects_root, "legacy_plugin", make_spec(name="legacy_plugin"), package_prefix="komand"
        )
        assert folder.package_dir() == folder.path / "komand_legacy_plugin"


class TestSave:
    def test_saves_code_docs_and_artifacts(self, projects_root, tmp_path):
        # Req 21.2: on generate/build/export, store spec, code, docs, and artifacts.
        folder = ProjectFolder.create(projects_root, "my_plugin", make_spec())

        package_source = tmp_path / "pkg"
        (package_source / "actions" / "run").mkdir(parents=True)
        (package_source / "actions" / "run" / "action.py").write_text("# logic\n", encoding="utf-8")

        metadata = folder.save(
            make_spec(version=SemVer(1, 1, 0)),
            package_source=package_source,
            help_md="# Help\n",
            generated_files={"Dockerfile": "FROM python:3.11\n"},
            artifacts={"my_plugin-1.1.0.plg": b"\x1f\x8bartifact"},
            modified_utc=datetime(2024, 2, 1, tzinfo=timezone.utc),
        )

        assert (folder.package_dir() / "actions" / "run" / "action.py").read_text() == "# logic\n"
        assert (folder.path / "help.md").read_text() == "# Help\n"
        assert (folder.path / "Dockerfile").read_text() == "FROM python:3.11\n"
        artifact = folder.path / ".builder" / "artifacts" / "my_plugin-1.1.0.plg"
        assert artifact.read_bytes() == b"\x1f\x8bartifact"

        assert metadata.current_version == "1.1.0"
        assert metadata.last_modified_utc == "2024-02-01T00:00:00+00:00"

    def test_save_preserves_created_timestamp(self, projects_root):
        created = datetime(2024, 1, 1, tzinfo=timezone.utc)
        folder = ProjectFolder.create(projects_root, "p", make_spec(name="p"), created_utc=created)
        folder.save(
            make_spec(name="p", version=SemVer(1, 2, 0)), modified_utc=datetime(2024, 3, 1, tzinfo=timezone.utc)
        )
        metadata = folder.metadata()
        assert metadata.created_utc == created.isoformat()
        assert metadata.last_modified_utc == "2024-03-01T00:00:00+00:00"

    def test_save_replaces_prior_package_contents(self, projects_root, tmp_path):
        folder = ProjectFolder.create(projects_root, "p", make_spec(name="p"))

        first = tmp_path / "first"
        first.mkdir()
        (first / "old.py").write_text("old\n", encoding="utf-8")
        folder.save(make_spec(name="p"), package_source=first)

        second = tmp_path / "second"
        second.mkdir()
        (second / "new.py").write_text("new\n", encoding="utf-8")
        folder.save(make_spec(name="p"), package_source=second)

        assert (folder.package_dir() / "new.py").exists()
        assert not (folder.package_dir() / "old.py").exists()

    def test_save_artifact_returns_path(self, projects_root):
        folder = ProjectFolder.create(projects_root, "p", make_spec(name="p"))
        path = folder.save_artifact("p-1.0.0.plg", b"data")
        assert path.read_bytes() == b"data"


class TestRecordVersion:
    def test_snapshots_spec_and_export_outcome(self, projects_root):
        # Req 21.3 storage: each version retains its spec snapshot and export outcome.
        folder = ProjectFolder.create(projects_root, "p", make_spec(name="p"))
        outcome = {
            "target": "local",
            "timestamp_utc": "2024-01-01T00:00:00+00:00",
            "result": "success",
            "message": "ok",
        }

        version_dir = folder.record_version(SemVer(1, 0, 0), make_spec(name="p"), export_outcome=outcome)

        snapshot = load_plugin_spec((version_dir / "plugin.spec.yaml").read_text(encoding="utf-8"))
        assert snapshot.name == "p"
        import json

        stored = json.loads((version_dir / "export_outcome.json").read_text(encoding="utf-8"))
        assert stored == outcome

    def test_stamps_tooling_per_version(self, projects_root):
        folder = ProjectFolder.create(projects_root, "p", make_spec(name="p"))
        stamp = ToolingStamp(insight_plugin_cli="1.2.3", sdk_version="6.1.0", kiro_cli="0.9")
        folder.record_version(SemVer(1, 0, 0), make_spec(name="p"), tooling=stamp)

        tooling = folder.tooling()
        assert tooling["1.0.0"] == stamp

    def test_list_versions_sorted_numerically(self, projects_root):
        folder = ProjectFolder.create(projects_root, "p", make_spec(name="p"))
        for version in (SemVer(1, 0, 0), SemVer(2, 0, 0), SemVer(1, 10, 0)):
            folder.record_version(version, make_spec(name="p"))
        assert folder.list_versions() == ["1.0.0", "1.10.0", "2.0.0"]


class TestListProjects:
    def test_lists_name_version_and_last_modified(self, projects_root):
        # Req 21.4: listing returns each plugin's name, current version, last-modified.
        ProjectFolder.create(
            projects_root,
            "alpha",
            make_spec(name="alpha"),
            created_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        beta = ProjectFolder.create(
            projects_root,
            "beta",
            make_spec(name="beta"),
            created_utc=datetime(2024, 1, 2, tzinfo=timezone.utc),
        )
        beta.save(
            make_spec(name="beta", version=SemVer(1, 5, 0)), modified_utc=datetime(2024, 6, 1, tzinfo=timezone.utc)
        )

        listings = list_projects(projects_root)
        # Most-recently-modified first: beta (2024-06) before alpha (2024-01).
        assert listings == [
            ProjectListing("beta", "1.5.0", "2024-06-01T00:00:00+00:00"),
            ProjectListing("alpha", "1.0.0", "2024-01-01T00:00:00+00:00"),
        ]

    def test_missing_root_returns_empty(self, projects_root):
        assert list_projects(projects_root / "does_not_exist") == []

    def test_skips_dirs_without_metadata(self, projects_root):
        ProjectFolder.create(projects_root, "real", make_spec(name="real"))
        (projects_root / "not_a_plugin").mkdir()
        listings = list_projects(projects_root)
        assert [entry.plugin_name for entry in listings] == ["real"]


class TestOpen:
    def test_open_missing_raises(self, projects_root):
        projects_root.mkdir(parents=True)
        with pytest.raises(ProjectFolderError):
            ProjectFolder.open(projects_root, "nope")

    def test_defaults_timestamp_to_now(self, projects_root):
        before = datetime.now(timezone.utc)
        folder = ProjectFolder.create(projects_root, "p", make_spec(name="p"))
        stored = datetime.fromisoformat(folder.metadata().created_utc)
        assert stored >= before - timedelta(seconds=5)


class TestMissingContent:
    """Req 21.6: loading a plugin whose Project_Folder is missing or has unreadable
    required content reports the specific problem and never yields a partial draft."""

    def test_open_reports_missing_metadata_content(self, projects_root):
        # A plugin directory exists on disk but its .builder/project.json was
        # never written, so there is no required content to load.
        (projects_root / "orphan").mkdir(parents=True)
        with pytest.raises(ProjectFolderError) as excinfo:
            ProjectFolder.open(projects_root, "orphan")
        assert "orphan" in str(excinfo.value)

    def test_metadata_reports_missing_project_json(self, projects_root):
        folder = ProjectFolder.create(projects_root, "my_plugin", make_spec())
        folder.metadata_path.unlink()

        with pytest.raises(ProjectFolderError) as excinfo:
            folder.metadata()
        message = str(excinfo.value)
        assert "my_plugin" in message
        assert "missing" in message.lower() or "unreadable" in message.lower()

    def test_metadata_reports_corrupt_project_json(self, projects_root):
        folder = ProjectFolder.create(projects_root, "my_plugin", make_spec())
        folder.metadata_path.write_text("{ not valid json", encoding="utf-8")

        with pytest.raises(ProjectFolderError) as excinfo:
            folder.metadata()
        message = str(excinfo.value)
        assert "my_plugin" in message
        assert "corrupt" in message.lower()

    def test_listing_reports_missing_content(self, projects_root):
        # listing() loads metadata; a missing project.json must surface the error
        # rather than returning a partial listing row.
        folder = ProjectFolder.create(projects_root, "my_plugin", make_spec())
        folder.metadata_path.unlink()

        with pytest.raises(ProjectFolderError):
            folder.listing()
