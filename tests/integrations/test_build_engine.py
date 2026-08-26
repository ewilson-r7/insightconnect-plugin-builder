"""Unit tests for the Build_Engine PLG packaging (task 15.5; Req 9.1, 9.2, 9.4, 9.5).

These cover the deterministic filesystem behavior of ``BuildEngine.package`` and
``list_plugin_files`` using temporary directories: creating a ``.plg``,
extracting it to verify the round trip, confirming the gzip format, the
validation gate, exclusion of ``.builder/`` metadata, and packaging atomicity
(no partial artifact, sources unchanged) on failure.

The PLG round-trip *property* (Req 2.1, 9.2) and the preview/package consistency
*property* (Req 16.1, 16.2) are covered separately by tasks 15.6 and 15.8.
"""

import gzip
import json
import tarfile
from pathlib import Path

import pytest

# Packaging drives `docker build` and `docker save` now, and none of these tests are
# about Docker. The stub answers both from a real executable, so the production argv
# and file handling are still exercised -- only the daemon is absent.
from tests.docker_stub import stub_docker  # noqa: E402

from icplugin_builder.integrations.build_engine import (
    BUILDER_METADATA_DIR,
    PLG_SUFFIX,
    BuildEngine,
    ExportPreview,
    PackagingError,
    PlgArtifact,
    ValidationNotPassedError,
    list_plugin_files,
    preview_export_files,
)


def make_project(root: Path) -> dict:
    """Create a small plugin working tree under ``root`` and return its files.

    Includes a ``.builder/`` metadata subtree and a ``__pycache__`` directory
    that must be excluded from the packaged artifact.
    """
    files = {
        "plugin.spec.yaml": (
            # A vendor is required: the artifact is an image tagged
            # <vendor>/<name>:<version>, and Docker refuses a repository component
            # that starts with the "_" an absent vendor would leave behind.
            "plugin_spec_version: v2\nname: my_plugin\nversion: 1.0.0\nvendor: rapid7\n"
        ),
        "icon_my_plugin/actions/run/action.py": "def run():\n    return 1\n",
        "icon_my_plugin/connection/connection.py": "conn = True\n",
        "help.md": "# My Plugin\n",
        "Dockerfile": "FROM python:3.11\n",
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    # Tool-only metadata + transient dirs that must NOT be packaged.
    builder = root / BUILDER_METADATA_DIR
    (builder / "history" / "1.0.0").mkdir(parents=True, exist_ok=True)
    (builder / "project.json").write_text('{"plugin_name": "my_plugin"}', encoding="utf-8")
    (builder / "history" / "1.0.0" / "plugin.spec.yaml").write_text("snapshot\n", encoding="utf-8")
    pycache = root / "icon_my_plugin" / "__pycache__"
    pycache.mkdir(parents=True, exist_ok=True)
    (pycache / "action.cpython-311.pyc").write_bytes(b"\x00\x01")
    return files


class TestListPluginFiles:
    def test_lists_only_plugin_files_excluding_metadata(self, tmp_path):
        files = make_project(tmp_path)
        listed = list_plugin_files(tmp_path)

        assert listed == sorted(files)
        assert all(not path.startswith(BUILDER_METADATA_DIR) for path in listed)
        assert all("__pycache__" not in path for path in listed)

    def test_missing_project_dir_raises(self, tmp_path):
        with pytest.raises(PackagingError):
            list_plugin_files(tmp_path / "does-not-exist")


class TestPreviewExportFiles:
    """The export preview file list (task 15.7; Req 16.2)."""

    def test_preview_lists_exactly_the_plugin_files(self, tmp_path):
        files = make_project(tmp_path)
        preview = preview_export_files(tmp_path)

        assert isinstance(preview, ExportPreview)
        assert preview.files == tuple(sorted(files))
        assert preview.count == len(files)
        assert not preview.is_empty

    def test_preview_excludes_metadata_and_transient_dirs(self, tmp_path):
        make_project(tmp_path)
        preview = preview_export_files(tmp_path)

        assert all(not path.startswith(BUILDER_METADATA_DIR) for path in preview)
        assert all("__pycache__" not in path for path in preview)

    def test_preview_matches_list_plugin_files_source_of_truth(self, tmp_path):
        make_project(tmp_path)

        # The preview is derived from the same source of truth the packager uses.
        assert list(preview_export_files(tmp_path)) == list_plugin_files(tmp_path)

    def test_preview_equals_packaged_contents(self, tmp_path):
        source = tmp_path / "project"
        make_project(source)

        preview = preview_export_files(source)
        artifact = BuildEngine(docker_executable=stub_docker(tmp_path)).package(
            source, validation_passed=True, output_dir=tmp_path / "out"
        )

        # Property 30 asserted the previewed file list equalled the archive's members.
        # True of a source tarball, false of an image archive -- the archive's members
        # are `oci-layout`, `index.json`, `manifest.json` and layer blobs, and the
        # plugin's files are inside the layers, admitted by its `.dockerignore`.
        #
        # What survives is the half that was actually load-bearing: the preview and the
        # artifact agree about which plugin files went in, computed from one source.
        assert preview.files == artifact.files

        with tarfile.open(artifact.path, mode="r:gz") as archive:
            roots = {name.split("/", 1)[0] for name in archive.getnames()}
        assert "oci-layout" in roots, f"the archive's root members are {sorted(roots)!r}"
        assert not (set(preview.files) & roots), (
            "the archive's members still look like the plugin's own files, so the artifact "
            "is a source tree rather than an image"
        )

    def test_preview_contains_membership_check(self, tmp_path):
        make_project(tmp_path)
        preview = preview_export_files(tmp_path)

        assert "plugin.spec.yaml" in preview
        assert f"{BUILDER_METADATA_DIR}/project.json" not in preview

    def test_preview_missing_project_dir_raises(self, tmp_path):
        with pytest.raises(PackagingError):
            preview_export_files(tmp_path / "does-not-exist")


class TestPackageRoundTrip:
    def test_package_then_extract_round_trips(self, tmp_path):
        source = tmp_path / "project"
        files = make_project(source)
        out = tmp_path / "out"

        artifact = BuildEngine(docker_executable=stub_docker(tmp_path)).package(
            source, validation_passed=True, output_dir=out
        )

        """The archive is an image, and reports the files the image was built from.

        This asserted that extracting the artifact yielded the plugin's files with
        identical contents -- design Property 6, a round trip. That was true of a
        source tarball and is false of an image archive: extracting yields
        `oci-layout`, `index.json`, `manifest.json` and layer blobs, and the plugin's
        files are inside the layers, admitted by its `.dockerignore` rather than by us.

        The claim worth keeping is that nothing is lost track of, so the two halves are
        asserted separately: the archive is an image carrying the expected identity, and
        the artifact still reports which plugin files it was built from.
        """
        assert isinstance(artifact, PlgArtifact)
        assert artifact.path.exists()
        assert artifact.path.suffix == PLG_SUFFIX
        assert set(artifact.files) == set(files), "the artifact no longer reports what it was built from"

        with tarfile.open(artifact.path, mode="r:gz") as archive:
            roots = {name.split("/", 1)[0] for name in archive.getnames()}
            handle = archive.extractfile("manifest.json")
            assert handle is not None
            declared = json.loads(handle.read().decode("utf-8"))[0]["RepoTags"]

        assert {
            "oci-layout",
            "index.json",
            "manifest.json",
        } <= roots, f"the archive's root members are {sorted(roots)!r}, which is not an image archive"
        assert declared == [
            "rapid7_custom/my_plugin:1.0.0"
        ], f"the archive declares {declared!r}; a tenant reads the plugin's identity from this"

    def test_artifact_is_gzip_format(self, tmp_path):
        source = tmp_path / "project"
        make_project(source)

        artifact = BuildEngine(docker_executable=stub_docker(tmp_path)).package(
            source, validation_passed=True, output_dir=tmp_path / "out"
        )

        # gzip magic bytes and decompressibility both confirm the format (Req 9.2).
        with artifact.path.open("rb") as handle:
            assert handle.read(2) == b"\x1f\x8b"
        with gzip.open(artifact.path, "rb") as handle:
            assert handle.read(1) != b""

    def test_default_artifact_name_and_location(self, tmp_path):
        source = tmp_path / "my_plugin"
        make_project(source)

        artifact = BuildEngine(docker_executable=stub_docker(tmp_path)).package(source, validation_passed=True)

        # Was `<root name>.plg`. The toolchain writes `<vendor>_<name>_<version>.plg`
        # and there is no reason to differ from it (`bugfix.md` 1.2).
        assert artifact.name == "rapid7_custom_my_plugin_1.0.0.plg"
        assert artifact.path.parent == (source / BUILDER_METADATA_DIR / "artifacts").resolve()

    def test_custom_artifact_name_gets_plg_suffix(self, tmp_path):
        source = tmp_path / "project"
        make_project(source)

        artifact = BuildEngine(docker_executable=stub_docker(tmp_path)).package(
            source, validation_passed=True, output_dir=tmp_path / "out", artifact_name="my_plugin-1.0.0"
        )
        assert artifact.name == "my_plugin-1.0.0.plg"


class TestValidationGate:
    def test_package_rejected_when_validation_not_passed(self, tmp_path):
        source = tmp_path / "project"
        make_project(source)
        out = tmp_path / "out"

        with pytest.raises(ValidationNotPassedError):
            BuildEngine(docker_executable=stub_docker(tmp_path)).package(
                source, validation_passed=False, output_dir=out
            )

        # No artifact produced (Req 9.4).
        assert not out.exists() or not any(out.iterdir())


class TestPackagingAtomicity:
    def test_failure_leaves_no_partial_artifact_and_sources_unchanged(self, tmp_path, monkeypatch):
        source = tmp_path / "project"
        files = make_project(source)
        out = tmp_path / "out"

        # Force the write to fail partway through. The archive used to be written with
        # `tarfile`; it is now `docker save` followed by gzip compression, so the
        # equivalent injury is made to the compression step.
        import icplugin_builder.integrations.build_engine as be

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(be.gzip, "open", boom)

        with pytest.raises(PackagingError):
            BuildEngine(docker_executable=stub_docker(tmp_path)).package(source, validation_passed=True, output_dir=out)

        # No partial artifact: no leftover .plg or temp files in the output dir (Req 9.5).
        leftovers = [p.name for p in out.iterdir()] if out.exists() else []
        assert not any(name.endswith(PLG_SUFFIX) for name in leftovers)
        assert leftovers == []

        # Sources are byte-identical (Req 9.5).
        for rel, content in files.items():
            assert (source / rel).read_text(encoding="utf-8") == content

    def test_missing_project_dir_raises_packaging_error(self, tmp_path):
        with pytest.raises(PackagingError):
            BuildEngine(docker_executable=stub_docker(tmp_path)).package(
                tmp_path / "nope", validation_passed=True, output_dir=tmp_path / "out"
            )
