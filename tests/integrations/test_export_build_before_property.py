"""Property test for build-before-export (task 16.3).

# Feature: insightconnect-plugin-builder, Property 21: Export requires a built artifact

Property 21 states that whenever no built ``.plg`` artifact exists, a tenant
export attempt is rejected with an error telling the user the plugin must be
built first (design "Property 21" / "Export_Manager"; Req 10.5).

The guard under test is :meth:`ExportManager.export_tenant`, which must perform
this rejection **before any network call**. This test drives it across the
"no artifact" input space -- the ``None`` case, a bare non-existent filesystem
path, and a :class:`PlgArtifact` whose ``path`` points at a file that was never
written -- while always supplying *valid* (non-empty) tenant credentials so the
only reason to reject is the missing artifact. For every draw it asserts that
:class:`ArtifactNotBuiltError` is raised and that a spy uploader is never
invoked, proving the rejection short-circuits ahead of any upload.

**Validates: Requirements 10.5**
"""

import tempfile
from pathlib import Path
from typing import List, Optional

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.integrations.build_engine import PlgArtifact
from icplugin_builder.integrations.export_manager import (
    ArtifactNotBuiltError,
    ExportManager,
    TenantCredentials,
    UploadResponse,
)


class SpyUploader:
    """A :class:`TenantUploader` that records calls; it must never be invoked here."""

    def __init__(self) -> None:
        self.calls: List[dict] = []

    def upload(self, *, region_base_url, api_key, artifact_path, timeout) -> UploadResponse:
        self.calls.append(
            {
                "region_base_url": region_base_url,
                "api_key": api_key,
                "artifact_path": Path(artifact_path),
                "timeout": timeout,
            }
        )
        return UploadResponse(status_code=200, body="ok")


# Non-empty, printable credential values so the credential guard (Req 10.4) always
# passes and the *only* reason an export can be rejected is the missing artifact.
_nonempty_text = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=40,
)


@st.composite
def valid_credentials(draw: st.DrawFn) -> TenantCredentials:
    """Draw a :class:`TenantCredentials` with a non-empty region base URL and API key."""
    region = draw(_nonempty_text)
    api_key = draw(_nonempty_text)
    return TenantCredentials(region_base_url=region, api_key=api_key)


# A single filesystem path segment that is a valid, non-empty file name and never
# collides with a special directory entry.
_path_segment = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="._-"),
    min_size=1,
    max_size=20,
).filter(lambda seg: seg not in {"", ".", ".."})


@st.composite
def unbuilt_artifacts(draw: st.DrawFn):
    """Draw a description of an artifact input for which no built ``.plg`` exists.

    Returns either ``None`` (nothing built) or a tuple ``(kind, segments)`` where
    ``kind`` selects a bare path or a :class:`PlgArtifact` wrapper and ``segments``
    are joined under the test's temp base to form a path that is never created.
    """
    kind = draw(st.sampled_from(("none", "path", "artifact")))
    if kind == "none":
        return None
    segments = draw(st.lists(_path_segment, min_size=1, max_size=4))
    suffix = draw(st.sampled_from(("", ".plg", ".txt", ".tar.gz")))
    return kind, segments, suffix


def _materialize(base: Path, spec) -> Optional[object]:
    """Turn a drawn ``unbuilt_artifacts`` spec into an ``export_tenant`` argument.

    The produced path lives under ``base`` (a fresh temp dir) and is never
    written, so it is guaranteed not to exist on disk.
    """
    if spec is None:
        return None
    kind, segments, suffix = spec
    path = base.joinpath(*segments)
    path = path.with_name(path.name + suffix)
    # Defensive: these paths are never created, but assert the invariant the
    # property depends on so a surprising collision fails loudly rather than
    # silently weakening the test.
    assert not path.exists()
    if kind == "path":
        return path
    return PlgArtifact(path=path, files=())


@settings(max_examples=100)
@given(spec=unbuilt_artifacts(), credentials=valid_credentials())
def test_export_rejected_when_no_built_artifact(spec, credentials):
    """Property 21: with valid creds but no built artifact, export is rejected pre-network.

    For any "no artifact" input (``None``, a non-existent path, or a
    :class:`PlgArtifact` pointing at a missing file) and any valid credential
    pair, :meth:`ExportManager.export_tenant` raises
    :class:`ArtifactNotBuiltError` and the spy uploader is never called.

    **Validates: Requirements 10.5**
    """
    uploader = SpyUploader()
    manager = ExportManager(uploader=uploader)
    with tempfile.TemporaryDirectory() as base:
        artifact = _materialize(Path(base), spec)

        with pytest.raises(ArtifactNotBuiltError) as excinfo:
            manager.export_tenant(artifact, credentials)

        # The error tells the user the plugin must be built first (Req 10.5).
        assert "built" in str(excinfo.value).lower()
        # No network call was made: the guard short-circuits before any upload.
        assert uploader.calls == []
