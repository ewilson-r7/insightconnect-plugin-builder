"""Property test for no-upgrade-without-approval (task 18.5).

The unit tests in ``test_update_manager.py`` pin a single not-approved example;
this module covers the universal property across generated inputs: for any
installed snapshot and any requested update, calling
:meth:`UpdateManager.apply_update` with ``approved=False`` must never install
anything and must leave every installed version exactly as it was before the
call (Req 23.7).

The installer and smoke test are injected spies that record every call, so the
absence of any install or smoke-test invocation is observed directly with no
real binaries, network, or wall clock involved.
"""

from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.api.config import UpdatesConfig
from icplugin_builder.integrations.update_manager import (
    APPLY_NOT_APPROVED,
    MANAGED_TOOLING_COMPONENTS,
    SmokeTestResult,
    UpdateManager,
)


class SpyInstaller:
    """An installer spy that records every (component, version) call."""

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, component, version) -> None:
        self.calls.append((component, version))


class SpySmokeTest:
    """A smoke-test spy that records every (component, version) call."""

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, component, version) -> SmokeTestResult:
        self.calls.append((component, version))
        return SmokeTestResult(passed=True, detail="spy")


# A version string generator constrained to plausible semantic versions.
versions = st.builds(
    lambda a, b, c: f"{a}.{b}.{c}",
    st.integers(min_value=0, max_value=20),
    st.integers(min_value=0, max_value=20),
    st.integers(min_value=0, max_value=20),
)

# Installed snapshots: a subset of the Managed_Tooling components mapped to a version.
installed_maps = st.dictionaries(
    keys=st.sampled_from(MANAGED_TOOLING_COMPONENTS),
    values=versions,
    max_size=len(MANAGED_TOOLING_COMPONENTS),
)


# Feature: insightconnect-plugin-builder, Property 44: No upgrade without approval
@settings(max_examples=200)
@given(
    installed=installed_maps,
    component=st.sampled_from(MANAGED_TOOLING_COMPONENTS),
    requested_version=versions,
)
def test_no_upgrade_without_approval_leaves_versions_unchanged(installed, component, requested_version):
    """Without approval, nothing installs and every installed version is unchanged.

    For any installed snapshot and any requested (component, version) update,
    ``apply_update(..., approved=False)`` must return ``APPLY_NOT_APPROVED``,
    must never invoke the installer or the smoke test, and must leave the full
    installed snapshot byte-for-byte identical to what it was before the call.

    **Validates: Requirements 23.7**
    """
    installer = SpyInstaller()
    smoke_test = SpySmokeTest()
    manager = UpdateManager(
        UpdatesConfig(offline_mode=False, cache_ttl_hours=24),
        installed_probe=lambda: dict(installed),
        upstream_source=lambda: {},
        clock=lambda: datetime(2024, 1, 1, tzinfo=timezone.utc),
        installer=installer,
        smoke_test=smoke_test,
    )
    manager.snapshot_installed()

    before = dict(manager.installed_versions.versions)

    result = manager.apply_update(component, requested_version, approved=False)

    # The update was refused for want of approval.
    assert result.status == APPLY_NOT_APPROVED
    assert result.applied is False

    # No component was ever installed and no smoke test ever ran.
    assert installer.calls == []
    assert smoke_test.calls == []

    # Every installed version is exactly what it was before the call.
    after = dict(manager.installed_versions.versions)
    assert after == before
