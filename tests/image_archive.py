"""Reading a ``.plg``, which is a gzipped ``docker save`` of the plugin image.

Two levels of question, and keeping them apart is the point of this module.

**The identity question** — is this an image archive, and does it declare the plugin it
should? Answerable from the archive's own metadata, so it works against the stub docker
in ``tests/docker_stub.py`` and costs nothing. Most tests want only this.

**The contents question** — are the plugin's files actually inside, and is anything in
there that should not be? Answerable only by reading the layer blobs of a *real* image,
so a test asking it must build one (``@pytest.mark.builds_a_real_image``). One test does,
because the answer is worth having and twenty copies of it are not.

The distinction is what the old tests lost. They asserted membership against a source
tarball's file list -- `plugin.spec.yaml` is a member, no reference document is a member
-- which conflated the two questions because for a source tarball they were the same
question. For an image archive they are not: what the image admits is governed by the
plugin's ``.dockerignore``, not by our packaging.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from typing import Dict, List, Set

#: What ``docker save`` puts at the root of its archive. Read from the artifact that
#: imported successfully -- see `.kiro/specs/plg-artifact-is-an-image/bugfix.md` 1.1.
IMAGE_ARCHIVE_ROOT_MEMBERS = frozenset({"oci-layout", "index.json", "manifest.json"})


def root_members(archive: Path) -> Set[str]:
    """The distinct first path segments inside the gzipped archive."""
    with tarfile.open(archive, mode="r:gz") as opened:
        return {name.split("/", 1)[0] for name in opened.getnames()}


def _manifest(archive: Path) -> List[Dict]:
    """The docker-style ``manifest.json`` from inside the archive."""
    with tarfile.open(archive, mode="r:gz") as opened:
        handle = opened.extractfile("manifest.json")
        if handle is None:  # pragma: no cover - defensive
            raise AssertionError(f"{archive} has no readable manifest.json")
        return json.loads(handle.read().decode("utf-8"))


def repo_tags(archive: Path) -> List[str]:
    """The image identity the archive declares, or ``[]`` when it declares none."""
    try:
        entries = _manifest(archive)
    except (KeyError, tarfile.TarError):
        return []
    return list(entries[0].get("RepoTags", [])) if entries else []


def assert_is_image_archive(archive: Path, *, expected_tag: str) -> None:
    """Assert the artifact is an image archive declaring ``expected_tag``.

    The narrow claim: enough to catch the defect this replaced -- a source tarball named
    ``.plg`` -- without needing a real image. Failure messages name what was found,
    because "assert False" on a packaging question tells nobody anything.
    """
    found = root_members(archive)
    assert IMAGE_ARCHIVE_ROOT_MEMBERS <= found, (
        f"{archive.name} has root members {sorted(found)!r}. An importable .plg has "
        f"{sorted(IMAGE_ARCHIVE_ROOT_MEMBERS)!r} -- this looks like a source tree."
    )
    declared = repo_tags(archive)
    assert declared == [expected_tag], (
        f"{archive.name} declares {declared!r}, not [{expected_tag!r}]. A tenant reads the "
        "plugin's identity from the image tag."
    )


def image_file_names(archive: Path) -> Set[str]:
    """Every path present in the image's layers, as the container would see them.

    Reads each layer blob named by ``manifest.json`` and unions their members. Whiteouts
    (``.wh.`` entries, a deleted file in a later layer) are not resolved: this reports
    what any layer *added*, which is the right question for "did this leak into the
    image" and close enough for "is the plugin's code in here".

    Only meaningful for a real image. Against the stub the layer blob is filler, so this
    returns nothing useful -- which is why the one caller builds a real image.
    """
    present: Set[str] = set()
    with tarfile.open(archive, mode="r:gz") as opened:
        entries = json.loads(opened.extractfile("manifest.json").read().decode("utf-8"))
        for layer in entries[0].get("Layers", []):
            handle = opened.extractfile(layer)
            if handle is None:  # pragma: no cover - defensive
                continue
            try:
                with tarfile.open(fileobj=handle, mode="r|*") as blob:
                    for member in blob:
                        present.add(member.name.lstrip("./"))
            except tarfile.TarError:  # pragma: no cover - a non-tar layer
                continue
    return present


def image_contains(archive: Path, needle: str) -> bool:
    """Whether ``needle`` appears anywhere in the image's layers.

    Matched on a path suffix, because the plugin's files live under the image's working
    directory (``/workspace``) rather than at the root the plugin tree used.
    """
    needle = needle.lstrip("./")
    return any(name == needle or name.endswith("/" + needle) for name in image_file_names(archive))


def image_member_text(archive: Path, member: str) -> str:
    """The text of ``member`` as the image carries it.

    Searches the layers newest-last, so a file rewritten in a later layer wins -- which is
    what the container would see. Raises with the member named rather than returning
    empty, because "the artifact does not carry this at all" and "it carries it empty" are
    different failures.
    """
    member = member.lstrip("./")
    found = None
    with tarfile.open(archive, mode="r:gz") as opened:
        entries = json.loads(opened.extractfile("manifest.json").read().decode("utf-8"))
        for layer in entries[0].get("Layers", []):
            handle = opened.extractfile(layer)
            if handle is None:  # pragma: no cover - defensive
                continue
            try:
                with tarfile.open(fileobj=handle, mode="r|*") as blob:
                    for entry in blob:
                        name = entry.name.lstrip("./")
                        if entry.isfile() and (name == member or name.endswith("/" + member)):
                            payload = blob.extractfile(entry)
                            if payload is not None:
                                found = payload.read().decode("utf-8", errors="replace")
            except tarfile.TarError:  # pragma: no cover - a non-tar layer
                continue
    if found is None:
        raise AssertionError(f"the image in {archive.name} carries no {member!r}")
    return found
