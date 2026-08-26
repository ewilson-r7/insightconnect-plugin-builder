"""A stand-in ``docker`` for tests that must not build a real image.

Packaging a plugin now produces a container image archive, so ``BuildEngine.package``
drives ``docker build`` and ``docker save``. Most tests are not about Docker: they are
about validation gating, atomicity, naming, or the export path's reporting. A property
test in particular would build an image per generated example, which is unusable.

The stub is a real executable answering the two subcommands the engine uses, so the
production code path — argv, exit codes, the file the engine expects to find — is
exercised unchanged. Only the daemon is absent.

Tests that genuinely need a real image (the bug conditions, and the integration test over
the JumpCloud tree) use the real ``docker`` and skip when no daemon is reachable.
"""

from __future__ import annotations

import json
import tarfile
import tempfile
from pathlib import Path

#: What a real image archive has at its root, and therefore what the stub produces.
#: Read from the archive that imported successfully -- see
#: `.kiro/specs/plg-artifact-is-an-image/bugfix.md` 1.1.
IMAGE_ARCHIVE_ROOT_MEMBERS = ("oci-layout", "index.json", "manifest.json")


def write_image_archive(path: Path, *, image_tag: str, payload_bytes: int = 2_048) -> None:
    """Write a tar at ``path`` shaped like ``docker save`` output.

    Not a real image: the layer blob is filler. What it reproduces faithfully is the
    structure the artifact is asserted on -- the root members and the ``RepoTags`` that
    carry the plugin's identity -- so a test can check the engine produced an image
    archive with the right identity without a daemon.

    ``payload_bytes`` pads the fake layer so the archive is not suspiciously tiny, since
    one of the defect's symptoms was an artifact far too small to hold an image.
    """
    blob = "blobs/sha256/" + "0" * 64
    manifest = [{"Config": blob, "RepoTags": [image_tag], "Layers": [blob]}]
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [{"annotations": {"io.containerd.image.name": f"docker.io/{image_tag}"}}],
    }
    with tempfile.TemporaryDirectory() as staging_name:
        staging = Path(staging_name)
        (staging / "oci-layout").write_text(json.dumps({"imageLayoutVersion": "1.0.0"}), encoding="utf-8")
        (staging / "index.json").write_text(json.dumps(index), encoding="utf-8")
        (staging / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        layer = staging / blob
        layer.parent.mkdir(parents=True, exist_ok=True)
        layer.write_bytes(b"\0" * payload_bytes)

        with tarfile.open(path, mode="w") as archive:
            for member in (*IMAGE_ARCHIVE_ROOT_MEMBERS, blob):
                archive.add(str(staging / member), arcname=member)


def stub_docker(directory: Path, *, build_exit: int = 0, save_exit: int = 0) -> str:
    """Write a stand-in ``docker`` into ``directory`` and return its path.

    Answers ``build`` by succeeding, and ``save -o <path>`` by writing an
    image-archive-shaped tar at ``<path>`` tagged with whatever ``-t``/positional tag it
    was handed. Any other subcommand exits non-zero, so a test that starts depending on
    one finds out rather than silently passing.

    Args:
        directory: where to write the executable.
        build_exit: exit status for ``docker build``, so a failing build can be tested.
        save_exit: exit status for ``docker save``.
    """
    script = directory / "docker"
    helper = Path(__file__).resolve()
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.path.insert(0, {parent!r})\n"
        "from docker_stub import write_image_archive\n"
        "from pathlib import Path\n"
        "argv = sys.argv[1:]\n"
        "sub = argv[0] if argv else ''\n"
        "if sub == 'build':\n"
        f"    sys.exit({int(build_exit)})\n"
        "if sub == 'save':\n"
        f"    code = {int(save_exit)}\n"
        "    if code:\n"
        "        sys.stderr.write('stub docker: save refused\\n')\n"
        "        sys.exit(code)\n"
        "    tag = argv[1] if len(argv) > 1 else 'unknown/unknown:0'\n"
        "    out = argv[argv.index('-o') + 1] if '-o' in argv else None\n"
        "    if out is None:\n"
        "        sys.stderr.write('stub docker: save without -o\\n')\n"
        "        sys.exit(2)\n"
        "    write_image_archive(Path(out), image_tag=tag)\n"
        "    sys.exit(0)\n"
        "sys.stderr.write('stub docker: unsupported subcommand %r\\n' % sub)\n"
        "sys.exit(64)\n".format(parent=str(helper.parent)),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script)
