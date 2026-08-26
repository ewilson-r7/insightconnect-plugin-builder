"""Bug conditions: the exported `.plg` is not an artifact a tenant can import.

Read `.kiro/specs/plg-artifact-is-an-image/bugfix.md` first. **Every test in the first
two classes is expected to FAIL against current code**, and each one names what was
measured rather than what the format is assumed to be.

The measurement, taken on one plugin at one version minutes apart:

* the tool's artifact -- 12,637 bytes, 37 members, rooted at `Dockerfile`,
  `icon_jumpcloud/`, `bin/` -- **rejected on import**;
* `insight-plugin export`'s artifact -- 81,305,076 bytes, 26 entries, rooted at
  `oci-layout`, `index.json`, `manifest.json`, `blobs/`, carrying
  `RepoTags == ["rapid7_custom/jumpcloud:1.0.1"]` -- **imported cleanly**.

So a `.plg` is a gzipped `docker save` of the plugin image. The code and the spec live
inside its layers; a tenant loads the image.

The third class pins what must keep working. The export path's *reporting* -- what is
recorded, what is refused, what a failure says -- is not what is wrong here, and a
change this deep is exactly where such behaviour gets lost by accident.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from icplugin_builder.core.vendor import apply_custom_vendor_suffix
from icplugin_builder.integrations.build_engine import (
    BuildEngine,
    PackagingError,
    ValidationNotPassedError,
)

#: What the archive that actually imported had at its root. Recorded from the
#: measurement, not from a reading of the OCI specification -- the claim being tested is
#: "the shape a tenant accepted", and that is an observation.
IMAGE_ARCHIVE_ROOT_MEMBERS = {"oci-layout", "index.json", "manifest.json"}

VENDOR = "rapid7"
PLUGIN_NAME = "jumpcloud"
VERSION = "1.0.1"


def _plugin_tree(root: Path) -> Path:
    """A minimal plugin tree with a buildable Dockerfile.

    The Dockerfile is real and tiny -- `docker build` has to be able to complete for the
    fixed code to produce anything, and a fixture that cannot build would make these
    tests unable to distinguish "not implemented" from "cannot work here".
    """
    (root / f"icon_{PLUGIN_NAME}" / "actions" / "create_user").mkdir(parents=True, exist_ok=True)
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "plugin.spec.yaml").write_text(
        "plugin_spec_version: v2\n"
        f"name: {PLUGIN_NAME}\n"
        f"version: {VERSION}\n"
        f"vendor: {VENDOR}\n"
        "title: JumpCloud\n"
        "description: Automate JumpCloud\n",
        encoding="utf-8",
    )
    (root / "Dockerfile").write_text('FROM alpine:3.19\nCOPY . /workspace\nENTRYPOINT ["/bin/sh"]\n', encoding="utf-8")
    (root / f"icon_{PLUGIN_NAME}" / "actions" / "create_user" / "action.py").write_text(
        "def run():\n    return {}\n", encoding="utf-8"
    )
    (root / "bin" / f"icon_{PLUGIN_NAME}").write_text("#!/usr/bin/env python\n", encoding="utf-8")
    (root / "help.md").write_text("# JumpCloud\n", encoding="utf-8")
    return root


def _docker_available() -> bool:
    """Whether a Docker daemon can actually be reached.

    Checked rather than assumed: producing an image archive needs a daemon, and a test
    that fails for want of one would be reporting on the host, not the tool.
    """
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - platform dependent
        return False
    return probe.returncode == 0


requires_docker = pytest.mark.skipif(
    not _docker_available(), reason="no Docker daemon reachable, so no image archive can be produced"
)


def _root_members(archive: Path) -> set:
    """The distinct first path segments inside a gzipped tarball."""
    with tarfile.open(archive, mode="r:gz") as opened:
        return {name.split("/", 1)[0] for name in opened.getnames()}


def _repo_tags(archive: Path) -> list:
    """The `RepoTags` an image archive declares, or `[]` when it is not one."""
    with tarfile.open(archive, mode="r:gz") as opened:
        try:
            handle = opened.extractfile("manifest.json")
        except KeyError:
            return []
        if handle is None:  # pragma: no cover - defensive
            return []
        entries = json.loads(handle.read().decode("utf-8"))
    return list(entries[0].get("RepoTags", [])) if entries else []


@pytest.mark.builds_a_real_image
@requires_docker
class TestTheArtifactIsNotAnImageArchive:
    """`bugfix.md` 1.1. **Expected to FAIL now.**

    `BuildEngine.package` tars the working tree. The artifact's root members are the
    plugin's own files, where an importable archive's are `oci-layout`, `index.json`
    and `manifest.json`.
    """

    def test_the_artifact_is_an_image_archive(self, tmp_path):
        root = _plugin_tree(tmp_path / PLUGIN_NAME)

        artifact = BuildEngine().package(root, validation_passed=True)

        members = _root_members(artifact.path)
        assert IMAGE_ARCHIVE_ROOT_MEMBERS <= members, (
            f"the artifact's root members are {sorted(members)!r}. An archive a tenant accepted has "
            f"{sorted(IMAGE_ARCHIVE_ROOT_MEMBERS)!r} -- this is the plugin's source tree, which "
            "InsightConnect has no way to interpret"
        )

    def test_the_artifact_declares_the_plugin_image_identity(self, tmp_path):
        """The identity is `<vendor>/<name>:<version>`, vendor `_custom`-suffixed."""
        root = _plugin_tree(tmp_path / PLUGIN_NAME)
        expected = f"{apply_custom_vendor_suffix(VENDOR)}/{PLUGIN_NAME}:{VERSION}"

        artifact = BuildEngine().package(root, validation_passed=True)

        assert _repo_tags(artifact.path) == [expected], (
            f"the artifact declares {_repo_tags(artifact.path)!r}, not [{expected!r}]. A tenant "
            "identifies the plugin by the image tag, so the published identity has to be on it -- "
            "the build stage's own icplugin-validate/<name>:latest tag will not do"
        )

    def test_the_artifact_is_large_enough_to_contain_an_image(self, tmp_path):
        """A crude check that catches the whole defect on its own.

        A source tarball for this plugin is kilobytes; an image archive is tens of
        megabytes. Kept because it fails loudly for the right reason even if the
        structural assertions above are ever weakened.
        """
        root = _plugin_tree(tmp_path / PLUGIN_NAME)

        artifact = BuildEngine().package(root, validation_passed=True)
        size = artifact.path.stat().st_size

        assert size > 1_000_000, (
            f"the artifact is {size:,} bytes. The measured source tarball was 12,637 and the "
            "importable image archive was 81,305,076 -- nothing this small carries a container image"
        )


@pytest.mark.builds_a_real_image
@requires_docker
class TestTheArtifactIsMisnamed:
    """`bugfix.md` 1.2. **Expected to FAIL now.**

    The toolchain writes `<vendor>_<name>_<version>.plg`; the orchestrator asks for
    `<name>-<version>.plg`. Whether a tenant parses the filename is unverified
    (`bugfix.md` 1.4), so this is asserted as consistency with the toolchain rather
    than as a tenant requirement.
    """

    def test_the_default_artifact_name_follows_the_toolchain(self, tmp_path):
        root = _plugin_tree(tmp_path / PLUGIN_NAME)
        expected = f"{apply_custom_vendor_suffix(VENDOR)}_{PLUGIN_NAME}_{VERSION}.plg"

        artifact = BuildEngine().package(root, validation_passed=True)

        assert (
            artifact.path.name == expected
        ), f"the artifact is named {artifact.path.name!r}; the toolchain produces {expected!r}"


class TestWhatMustKeepWorking:
    """Preservation (`tasks.md` 1.3). These pass now and must keep passing.

    None of this is what is wrong. It is recorded because replacing the packaging
    mechanism is exactly the kind of change that drops a refusal or a failure mode
    without anyone noticing.
    """

    def test_packaging_is_refused_when_validation_did_not_pass(self, tmp_path):
        """Req 9.4 -- an unvalidated plugin produces no artifact at all."""
        root = _plugin_tree(tmp_path / PLUGIN_NAME)

        with pytest.raises(ValidationNotPassedError):
            BuildEngine().package(root, validation_passed=False)

        assert list((root / ".builder" / "artifacts").glob("*.plg")) == [], "an artifact was produced anyway"

    def test_a_refused_packaging_leaves_the_sources_untouched(self, tmp_path):
        """Req 9.5 -- the engine is read-only with respect to the plugin tree."""
        root = _plugin_tree(tmp_path / PLUGIN_NAME)
        before = {path: path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}

        with pytest.raises(ValidationNotPassedError):
            BuildEngine().package(root, validation_passed=False)

        after = {path: path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}
        assert after == before

    def test_no_partial_artifact_survives_a_failure(self, tmp_path):
        """Req 9.5 -- a failure leaves nothing observable behind.

        Provoked by making the output directory unwritable, which is the failure the
        atomic-write path exists for. The new mechanism has to keep this property: a
        half-written 80 MB archive is worse than none.
        """
        root = _plugin_tree(tmp_path / PLUGIN_NAME)
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")

        with pytest.raises(Exception):
            BuildEngine().package(root, validation_passed=True, output_dir=blocked / "inner")

        assert not (blocked / "inner").exists()


class TestAFailedImageBuildSaysWhatToDo:
    """Task 6 -- packaging needs Docker, so its absence must not read as a plugin defect.

    Every case here says nothing about the plugin's code. Reporting them as "packaging
    failed" would send the operator to look at a plugin that is fine, which is precisely
    the class of misdirection this whole bugfix exists to remove.
    """

    @staticmethod
    def _tree(root: Path) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / "plugin.spec.yaml").write_text(
            f"plugin_spec_version: v2\nname: {PLUGIN_NAME}\nversion: {VERSION}\nvendor: {VENDOR}\n",
            encoding="utf-8",
        )
        (root / "Dockerfile").write_text("FROM alpine:3.19\n", encoding="utf-8")
        return root

    def test_an_absent_docker_names_itself_and_why_it_is_needed(self, tmp_path):
        root = self._tree(tmp_path / PLUGIN_NAME)

        with pytest.raises(PackagingError) as caught:
            BuildEngine(docker_executable="/nonexistent/docker").package(root, validation_passed=True)

        message = str(caught.value)
        assert "/nonexistent/docker" in message, "the message does not name what it looked for"
        assert "container image" in message, (
            "the message does not say why Docker is needed. An operator who thinks packaging is "
            "just archiving has no reason to expect a daemon."
        )

    def test_an_unreachable_daemon_is_distinguished_from_a_build_failure(self, tmp_path):
        """The most common failure, and the one that says least about the plugin."""
        root = self._tree(tmp_path / PLUGIN_NAME)
        refusing = tmp_path / "refuse" / "docker"
        refusing.parent.mkdir(parents=True, exist_ok=True)
        refusing.write_text(
            "#!/bin/sh\n" "echo 'Cannot connect to the Docker daemon at unix:///var/run/docker.sock.' >&2\n" "exit 1\n",
            encoding="utf-8",
        )
        refusing.chmod(0o755)

        with pytest.raises(PackagingError) as caught:
            BuildEngine(docker_executable=str(refusing)).package(root, validation_passed=True)

        message = str(caught.value)
        assert "daemon is not reachable" in message, f"reported as a generic build failure: {message}"
        assert (
            "nothing about the plugin needs changing" in message
        ), "the message does not tell the operator their plugin is not the problem"

    def test_a_silent_build_failure_says_how_to_see_why(self, tmp_path):
        """Docker occasionally fails saying nothing. A bare exit code is not actionable."""
        root = self._tree(tmp_path / PLUGIN_NAME)
        silent = tmp_path / "silent" / "docker"
        silent.parent.mkdir(parents=True, exist_ok=True)
        silent.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
        silent.chmod(0o755)

        with pytest.raises(PackagingError) as caught:
            BuildEngine(docker_executable=str(silent)).package(root, validation_passed=True)

        message = str(caught.value)
        assert "printed nothing" in message, f"a silent failure was reported as if it explained itself: {message}"
        assert "docker build" in message, "the message does not give the command to run by hand"

    def test_a_failed_build_leaves_no_artifact_behind(self, tmp_path):
        """Fail closed: no artifact, not even a partial one (Req 9.5)."""
        root = self._tree(tmp_path / PLUGIN_NAME)
        out = tmp_path / "out"
        failing = tmp_path / "fail" / "docker"
        failing.parent.mkdir(parents=True, exist_ok=True)
        failing.write_text("#!/bin/sh\necho boom >&2\nexit 1\n", encoding="utf-8")
        failing.chmod(0o755)

        with pytest.raises(PackagingError):
            BuildEngine(docker_executable=str(failing)).package(root, validation_passed=True, output_dir=out)

        leftovers = sorted(path.name for path in out.iterdir()) if out.exists() else []
        assert leftovers == [], f"a failed packaging left {leftovers} behind"

    def test_a_failed_build_leaves_the_plugin_tree_untouched(self, tmp_path):
        """The image is built from a staged copy, so nothing in the tree moves (Req 9.5)."""
        root = self._tree(tmp_path / PLUGIN_NAME)
        (root / "previous_1.0.0.plg").write_bytes(b"a previous release")
        before = {path: path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}
        failing = tmp_path / "fail2" / "docker"
        failing.parent.mkdir(parents=True, exist_ok=True)
        failing.write_text("#!/bin/sh\necho boom >&2\nexit 1\n", encoding="utf-8")
        failing.chmod(0o755)

        with pytest.raises(PackagingError):
            BuildEngine(docker_executable=str(failing)).package(root, validation_passed=True)

        after = {path: path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}
        assert after == before, "packaging modified the plugin tree on its way to failing"
