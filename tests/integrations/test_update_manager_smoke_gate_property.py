"""Property test for smoke-test-gated recording/rollback (task 18.6).

When the user approves an update, :meth:`UpdateManager.apply_update` installs the
selected version and runs a smoke test against a known-good sample plugin. This
module covers the universal property across generated inputs: the recorded
installed version becomes the newly-requested version **iff** the smoke test
passes (Req 23.8); otherwise the component is rolled back to its pre-update
version and the result carries a reason explaining why the update was not
applied (Req 23.9).

The smoke test is an injected spy whose pass/fail outcome is generated, and the
installer is a spy that records every ``(component, version)`` install so both
the initial install and any rollback reinstall are observed directly -- no real
binaries, network, or wall clock are involved.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.api.config import UpdatesConfig
from icplugin_builder.integrations.update_manager import (
    APPLY_APPLIED,
    APPLY_ROLLED_BACK,
    COMPONENT_INSIGHTCONNECT_SDK,
    SmokeTestResult,
    UpdateManager,
)

version_strategy = st.from_regex(r"\A[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,2}\Z", fullmatch=True)


class RecordingInstaller:
    """An installer spy that records every ``(component, version)`` install."""

    def __init__(self) -> None:
        self.installs = []

    def __call__(self, component: str, version: str) -> None:
        self.installs.append((component, version))


class SmokeTestSpy:
    """A smoke test spy returning a fixed pass/fail outcome and recording calls."""

    def __init__(self, passed: bool) -> None:
        self.passed = passed
        self.calls = []

    def __call__(self, component: str, version: str) -> SmokeTestResult:
        self.calls.append((component, version))
        detail = "known-good sample validated" if self.passed else "known-good sample failed to validate"
        return SmokeTestResult(passed=self.passed, detail=detail)


# Feature: insightconnect-plugin-builder, Property 45: Approved update records version iff smoke test passes; rollback otherwise  # noqa: E501
@settings(max_examples=200)
@given(
    installed_version=version_strategy,
    requested_version=version_strategy,
    smoke_passes=st.booleans(),
)
def test_approved_update_records_iff_smoke_passes(installed_version, requested_version, smoke_passes):
    """Approved update records the new version iff the smoke test passes.

    Starting from a snapshotted installed version, an approved update is applied
    with a smoke test whose outcome is generated. When the smoke test passes, the
    recorded installed version must become the requested version and the result
    must report success. When it fails, the recorded installed version must
    revert to the pre-update version, the installer must be asked to reinstall
    that pre-update version (a rollback), and the result must carry a non-empty
    reason explaining that the update was not applied.

    **Validates: Requirements 23.8, 23.9**
    """
    component = COMPONENT_INSIGHTCONNECT_SDK
    installer = RecordingInstaller()
    smoke_test = SmokeTestSpy(passed=smoke_passes)
    manager = UpdateManager(
        UpdatesConfig(offline_mode=False, cache_ttl_hours=24),
        installed_probe=lambda: {component: installed_version},
        upstream_source=lambda: {},
        installer=installer,
        smoke_test=smoke_test,
    )

    # Establish the pre-update baseline the rollback must restore.
    manager.snapshot_installed()
    assert manager.installed_versions.get(component) == installed_version

    result = manager.apply_update(component, requested_version, approved=True)

    # The requested version is always installed first, and the smoke test runs.
    assert installer.installs[0] == (component, requested_version)
    assert smoke_test.calls == [(component, requested_version)]

    if smoke_passes:
        # Smoke test passed: the new version is recorded as installed (Req 23.8).
        assert result.status == APPLY_APPLIED
        assert result.applied is True
        assert result.installed_version == requested_version
        assert manager.installed_versions.get(component) == requested_version
    else:
        # Smoke test failed: roll back to the pre-update version with a reason (Req 23.9).
        assert result.status == APPLY_ROLLED_BACK
        assert result.applied is False
        assert result.installed_version == installed_version
        assert manager.installed_versions.get(component) == installed_version
        assert result.reason.strip() != ""
        # The rollback reinstalls the pre-update version.
        assert installer.installs[-1] == (component, installed_version)

    # The recorded version is the new one exactly when the smoke test passed.
    recorded_is_new = manager.installed_versions.get(component) == requested_version
    # When the requested version equals the installed version the two coincide,
    # so the strict biconditional only holds when they differ.
    if requested_version != installed_version:
        assert recorded_is_new == smoke_passes
