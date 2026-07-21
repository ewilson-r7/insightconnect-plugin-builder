"""Property test for missing-credential rejection (task 16.2).

# Feature: insightconnect-plugin-builder, Property 20: Missing credentials rejected before any network call

Property 20 states that *for any* tenant credential pair in which the region
base URL or the API key is empty (or whitespace-only), the export is rejected
**before** contacting the InsightConnect API, with an error naming the missing
credential (design "Property 20"; Req 10.4).

The guard under test is
:meth:`~icplugin_builder.integrations.export_manager.ExportManager.export_tenant`
together with :class:`TenantCredentials`. This test drives it across the empty-
credential input space by drawing which credential is missing (region only, API
key only, or both), rendering the missing value as an empty or whitespace-only
string and the present value (if any) as a genuinely non-empty one, always
supplying a **real built ``.plg`` artifact** so any rejection is attributable to
the credentials rather than a missing artifact. A spy uploader records every
call; the assertions confirm ``export_tenant`` raises
:class:`MissingCredentialError` naming the missing credential and that the
uploader was never invoked -- i.e. no network call happened.

**Validates: Requirements 10.4**
"""

import functools
import tempfile
from pathlib import Path
from typing import List

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.integrations.build_engine import BuildEngine, PlgArtifact
from icplugin_builder.integrations.export_manager import (
    ExportManager,
    MissingCredentialError,
    TenantCredentials,
    UploadResponse,
)

# Whitespace characters that ``str.strip()`` treats as blank; a value drawn
# solely from these (including the empty string) is "empty" for Req 10.4.
_BLANK_ALPHABET = " \t\n\r\x0b\x0c"


class SpyUploader:
    """A :class:`TenantUploader` that records calls so we can prove none happened.

    If :meth:`upload` is ever invoked the property has been violated (a network
    call was attempted despite a missing credential), so it records the call and
    returns a benign success response rather than performing any real upload.
    """

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


@functools.lru_cache(maxsize=1)
def _built_artifact() -> PlgArtifact:
    """Build one real ``.plg`` once and reuse it across all examples.

    The artifact only needs to exist on disk for the built-artifact guard; the
    credential guard runs first, so a genuinely built (rather than fabricated)
    artifact keeps the test faithful without rebuilding per example.
    """
    workdir = Path(tempfile.mkdtemp(prefix="prop20_"))
    source = workdir / "project"
    files = {
        "plugin.spec.yaml": "plugin_spec_version: v2\nname: my_plugin\nversion: 1.0.0\n",
        "icon_my_plugin/actions/run/action.py": "def run():\n    return 1\n",
        "help.md": "# My Plugin\n",
        "Dockerfile": "FROM python:3.11\n",
    }
    for rel, content in files.items():
        path = source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return BuildEngine().package(source, validation_passed=True, output_dir=workdir / "artifacts")


def _blank() -> st.SearchStrategy[str]:
    """Draw an empty or whitespace-only string (an "empty" credential)."""
    return st.text(alphabet=_BLANK_ALPHABET, min_size=0, max_size=8)


def _present() -> st.SearchStrategy[str]:
    """Draw a genuinely non-empty credential (non-blank once stripped)."""
    return st.text(min_size=1, max_size=32).filter(lambda value: value.strip() != "")


@st.composite
def missing_credential_cases(draw: st.DrawFn):
    """Draw ``(credentials, expects_region_missing)`` where a credential is empty.

    Covers the three empty-credential shapes: region empty only, API key empty
    only, and both empty. ``expects_region_missing`` records whether the region
    is the first-missing credential, since the guard validates the region before
    the API key (design: region checked first).
    """
    scenario = draw(st.sampled_from(("region_blank", "key_blank", "both_blank")))
    if scenario == "region_blank":
        region, api_key, region_missing = draw(_blank()), draw(_present()), True
    elif scenario == "key_blank":
        region, api_key, region_missing = draw(_present()), draw(_blank()), False
    else:
        region, api_key, region_missing = draw(_blank()), draw(_blank()), True
    return TenantCredentials(region_base_url=region, api_key=api_key), region_missing


@settings(max_examples=200)
@given(case=missing_credential_cases())
def test_missing_credentials_rejected_before_any_network_call(case):
    """Property 20: an empty region URL or API key is rejected pre-network.

    For any credential pair with an empty (or whitespace-only) region base URL
    and/or API key, ``export_tenant`` raises :class:`MissingCredentialError`
    naming the first missing credential, and the injected uploader is never
    called -- proving the rejection happens before any network call.

    **Validates: Requirements 10.4**
    """
    credentials, region_missing = case
    uploader = SpyUploader()
    artifact = _built_artifact()

    with pytest.raises(MissingCredentialError) as excinfo:
        ExportManager(uploader=uploader).export_tenant(artifact, credentials)

    message = str(excinfo.value).lower()
    if region_missing:
        assert "region" in message
    else:
        assert "api key" in message

    # No network call was attempted: the uploader was never invoked (Req 10.4).
    assert uploader.calls == []
