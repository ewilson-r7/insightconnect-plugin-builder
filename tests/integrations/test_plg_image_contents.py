"""The one test that reads a real plugin image and checks what is inside it.

Everything else asserts the *narrow* claim: the artifact is an image archive declaring
the plugin's identity. That is cheap, needs no daemon, and catches the defect this
bugfix exists for -- a source tarball named ``.plg``.

It does not catch the next defect along: an image that is well-formed and correctly
tagged and does not contain the plugin, or contains something it should not. Answering
that means reading the layer blobs of a real image, which is slow. So it is answered
once, here, and the narrow tests elsewhere lean on this one -- their docstrings say so.

This is also the check whose absence let the original defect ship. A leak check ran over
the old artifact on 2026-08-17 and passed: 39 entries, no ``.builder/``, no vendor
swagger. It inspected the contents of a source tarball very carefully and never asked
whether a tarball was the right artifact. Nothing had ever tried to *load* what the tool
produced.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 - drives the local docker CLI, never with shell=True
from pathlib import Path

import pytest

from icplugin_builder.core.vendor import apply_custom_vendor_suffix
from icplugin_builder.integrations.build_engine import BuildEngine, list_plugin_files

from tests.image_archive import assert_is_image_archive, image_contains, image_file_names

#: The tree the 2026-08-17 end-to-end run produced. Read-only: it is copied before use,
#: because it is the operator's own project and packaging must not disturb it.
JUMPCLOUD_TREE = Path("~/.icplugin-builder/projects/jumpcloud").expanduser()

#: Byproducts and tool-only files that must not reach a customer-facing image. Each is
#: seeded into the copied tree, so their absence is a measurement rather than an
#: assumption about what happened to be lying around.
SEEDED_BYPRODUCTS = (
    ".coverage",
    ".DS_Store",
    "unit_test/.coverage",
    "icon_jumpcloud/stray.pyc",
    "previous_release_1.0.0.plg",
)


def _docker_available() -> bool:
    """Whether a Docker daemon can be reached, so a skip reports the host not the tool."""
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(  # nosec B603 - fixed argv
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - platform dependent
        return False
    return probe.returncode == 0


pytestmark = [
    pytest.mark.builds_a_real_image,
    pytest.mark.skipif(not _docker_available(), reason="no Docker daemon reachable, so no image can be built"),
    pytest.mark.skipif(not JUMPCLOUD_TREE.is_dir(), reason=f"no plugin tree at {JUMPCLOUD_TREE}"),
]


@pytest.fixture(scope="module")
def packaged_image(tmp_path_factory):
    """Package a copy of the real plugin tree, seeded with things that must not ship.

    Module-scoped: one real image build, reused by every assertion below.

    The docker executable is named **explicitly** rather than relying on this module's
    ``builds_a_real_image`` marker. That marker is honoured by a function-scoped fixture
    in ``tests/conftest.py``, which cannot run before a module-scoped one -- so a fixture
    like this would silently get the stub, produce an image with filler layers, and fail
    reporting that the plugin is missing from its own image. It did exactly that once.
    """
    work = tmp_path_factory.mktemp("plg_contents")
    root = work / JUMPCLOUD_TREE.name
    shutil.copytree(JUMPCLOUD_TREE, root, symlinks=True)

    for relative in SEEDED_BYPRODUCTS:
        seeded = root / relative
        seeded.parent.mkdir(parents=True, exist_ok=True)
        seeded.write_bytes(b"byproduct that must not ship")

    artifact = BuildEngine(docker_executable="docker").package(root, validation_passed=True, output_dir=work / "out")
    return root, artifact


class TestTheImageCarriesThePlugin:
    """What a tenant loads has to actually be the plugin."""

    def test_the_artifact_is_an_image_declaring_this_plugin(self, packaged_image):
        root, artifact = packaged_image
        expected = f"{apply_custom_vendor_suffix('rapid7')}/{root.name}:1.0.1"

        assert_is_image_archive(artifact.path, expected_tag=expected)

    def test_the_spec_is_inside_the_image(self, packaged_image):
        """A tenant reads the spec out of the image, so it has to be in there."""
        _root, artifact = packaged_image

        assert image_contains(artifact.path, "plugin.spec.yaml"), (
            "the image carries no plugin.spec.yaml, so a tenant has nothing to read the "
            "plugin's actions and connection from"
        )

    def test_the_hand_written_code_is_inside_the_image(self, packaged_image):
        """The API client, connection and actions -- the plugin's actual behaviour."""
        _root, artifact = packaged_image

        for member in (
            "icon_jumpcloud/util/api.py",
            "icon_jumpcloud/connection/connection.py",
            "icon_jumpcloud/actions/create_user/action.py",
        ):
            assert image_contains(artifact.path, member), f"the image carries no {member}"

    def test_the_reported_list_is_the_packaged_set(self, packaged_image):
        """The artifact reports exactly what was staged into the build context.

        Deliberately *not* asserting containment in either direction against the image,
        because neither holds and each failure taught that:

        * the image is not a superset -- the plugin's generated ``.dockerignore`` excludes
          ``unit_test/**/*``, so the staged tests correctly never ship;
        * the image is not a subset either -- the Dockerfile is multi-stage and copies a
          built ``/workspace``, so pip output and build products are in there that were
          never staged. That is what a build *is*.

        What is true, and what the narrow assertions elsewhere rest on, is that the
        reported list is the staged set. A byproduct that never enters the context cannot
        enter the image, which is why measuring the list is sound for the leak claims --
        and the leak claims themselves are measured directly below, against the image.
        """
        root, artifact = packaged_image

        assert set(artifact.files) == set(list_plugin_files(root)), "the reported list is not the packaged set"

    def test_the_unit_tests_are_excluded_by_the_plugins_own_dockerignore(self, packaged_image):
        """Recorded because it is the reason the staged set and the image differ.

        Pinned so that if a future change starts shipping the tests, it is a deliberate
        decision rather than a silent one.
        """
        _root, artifact = packaged_image

        assert "unit_test/util.py" in set(artifact.files), "the unit tests are no longer staged"
        assert not image_contains(artifact.path, "unit_test/util.py"), (
            "the plugin's unit tests are now inside the shipped image; the generated "
            ".dockerignore excludes unit_test/**/*, so something has changed"
        )


class TestTheImageCarriesNothingItShouldNot:
    """The 2026-08-17 leak check's question, asked of the right artifact this time."""

    def test_no_seeded_byproduct_reaches_the_image(self, packaged_image):
        """Coverage data, Finder droppings, loose bytecode, and a previous release.

        Seeded deliberately. The plugin's generated ``.dockerignore`` excludes none of
        these, so before the build context was staged from the packaged file set they
        would all have been copied in -- a coverage database carries absolute paths from
        the build machine, and a stale ``.plg`` is 80 MB of the previous release.
        """
        _root, artifact = packaged_image

        leaked = [relative for relative in SEEDED_BYPRODUCTS if image_contains(artifact.path, relative)]

        assert leaked == [], f"byproducts reached a customer-facing image: {leaked}"

    def test_no_tool_metadata_reaches_the_image(self, packaged_image):
        """``.builder/`` holds this tool's own history, drafts and vendor reference material."""
        _root, artifact = packaged_image

        leaked = sorted(name for name in image_file_names(artifact.path) if ".builder/" in name)

        assert leaked == [], f"the tool's own metadata reached the image: {leaked[:8]}"

    def test_the_operators_tree_is_untouched_by_packaging(self, packaged_image):
        """Packaging reads the plugin; it does not rearrange it.

        Asserted because an earlier attempt moved a stale ``.plg`` out of the operator's
        directory to keep it out of the build context. Staging the context removed the
        need, and this pins that nothing goes back to touching their files.
        """
        root, _artifact = packaged_image

        for relative in SEEDED_BYPRODUCTS:
            assert (root / relative).exists(), f"packaging removed {relative} from the plugin tree"
