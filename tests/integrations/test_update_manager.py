"""Unit tests for version snapshotting and cached upstream checks (task 18.1).

These cover :class:`UpdateManager` from
:mod:`icplugin_builder.integrations.update_manager`:

* recording installed Managed_Tooling versions at startup (Req 23.1),
* caching an upstream check result for the configured TTL and not rechecking
  until it expires (Req 23.3, 23.4),
* skipping the check in offline mode or when the network is unavailable, while
  continuing to operate on the installed versions (Req 23.5), and
* dispatching the check off the event loop so it does not block (Req 23.3).

Every collaborator (installed probe, upstream source, clock, network probe) is
injected, so the tests are fully deterministic with no real binaries, network,
or wall clock. The caching *property* (no new check within the TTL) is covered
separately by the property test (task 18.2).
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from icplugin_builder.api.config import UpdatesConfig
from icplugin_builder.core.spec_model import PluginSpec, SemVer
from icplugin_builder.integrations.update_manager import (
    APPLY_APPLIED,
    APPLY_INSTALL_FAILED,
    APPLY_NOT_APPROVED,
    APPLY_ROLLED_BACK,
    CHECK_CACHED,
    CHECK_FAILED,
    CHECK_PERFORMED,
    CHECK_SKIPPED_NO_NETWORK,
    CHECK_SKIPPED_OFFLINE,
    COMPONENT_INSIGHT_PLUGIN_CLI,
    COMPONENT_INSIGHTCONNECT_SDK,
    COMPONENT_KIRO_CLI,
    COMPONENT_PLUGIN_SPEC_SCHEMA,
    MANAGED_TOOLING_COMPONENTS,
    NOTIFY_SDK_BUMP_OFFER,
    NOTIFY_UPDATE_APPLIED,
    NOTIFY_UPDATE_NOT_APPROVED,
    NOTIFY_UPDATE_ROLLED_BACK,
    SmokeTestResult,
    ToolingVersions,
    UpdateManager,
    UpstreamCheckResult,
)
from icplugin_builder.persistence.project_folder import ProjectFolder


class MutableClock:
    """A deterministic, advanceable clock for TTL tests."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class CountingSource:
    """An upstream source that counts calls and returns fixed versions."""

    def __init__(self, versions):
        self.versions = dict(versions)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return dict(self.versions)


INSTALLED = {
    COMPONENT_INSIGHT_PLUGIN_CLI: "1.2.3",
    COMPONENT_INSIGHTCONNECT_SDK: "6.1.0",
    COMPONENT_KIRO_CLI: "0.9.0",
    COMPONENT_PLUGIN_SPEC_SCHEMA: "v2",
}

UPSTREAM = {
    COMPONENT_INSIGHT_PLUGIN_CLI: "1.3.0",
    COMPONENT_INSIGHTCONNECT_SDK: "6.2.0",
    COMPONENT_KIRO_CLI: "0.9.0",
    COMPONENT_PLUGIN_SPEC_SCHEMA: "v2",
}


def make_manager(**overrides):
    config = overrides.pop("config", UpdatesConfig(offline_mode=False, cache_ttl_hours=24))
    installed_probe = overrides.pop("installed_probe", lambda: dict(INSTALLED))
    upstream_source = overrides.pop("upstream_source", CountingSource(UPSTREAM))
    clock = overrides.pop("clock", MutableClock(datetime(2024, 1, 1, tzinfo=timezone.utc)))
    network_probe = overrides.pop("network_probe", lambda: True)
    manager = UpdateManager(
        config,
        installed_probe=installed_probe,
        upstream_source=upstream_source,
        clock=clock,
        network_probe=network_probe,
    )
    return manager, upstream_source, clock


class TestToolingVersions:
    def test_is_immutable_and_copies_input(self):
        source = {COMPONENT_KIRO_CLI: "1.0.0"}
        versions = ToolingVersions(source)
        # Mutating the original dict does not leak into the snapshot.
        source[COMPONENT_KIRO_CLI] = "9.9.9"
        assert versions[COMPONENT_KIRO_CLI] == "1.0.0"

    def test_get_and_contains(self):
        versions = ToolingVersions({COMPONENT_KIRO_CLI: "1.0.0"})
        assert versions.get(COMPONENT_KIRO_CLI) == "1.0.0"
        assert versions.get(COMPONENT_INSIGHT_PLUGIN_CLI) is None
        assert COMPONENT_KIRO_CLI in versions
        assert COMPONENT_INSIGHT_PLUGIN_CLI not in versions


class TestSnapshotInstalled:
    def test_records_installed_versions_at_startup(self):
        manager, _, _ = make_manager()
        assert manager.installed_versions is None

        snapshot = manager.snapshot_installed()

        assert isinstance(snapshot, ToolingVersions)
        for component in MANAGED_TOOLING_COMPONENTS:
            assert snapshot[component] == INSTALLED[component]
        # The snapshot is retained for later reads.
        assert manager.installed_versions is snapshot

    def test_partial_probe_still_snapshots(self):
        partial = {COMPONENT_KIRO_CLI: "0.9.0"}
        manager, _, _ = make_manager(installed_probe=lambda: partial)

        snapshot = manager.snapshot_installed()

        assert snapshot.get(COMPONENT_KIRO_CLI) == "0.9.0"
        assert snapshot.get(COMPONENT_INSIGHT_PLUGIN_CLI) is None


class TestCheckUpstream:
    def test_performs_check_and_returns_available(self):
        manager, source, _ = make_manager()

        result = manager.check_upstream()

        assert result.status == CHECK_PERFORMED
        assert result.performed is True
        assert source.calls == 1
        assert result.available[COMPONENT_INSIGHT_PLUGIN_CLI] == "1.3.0"
        assert result.checked_at == datetime(2024, 1, 1, tzinfo=timezone.utc)

    def test_second_check_within_ttl_uses_cache(self):
        manager, source, clock = make_manager()

        first = manager.check_upstream()
        clock.advance(timedelta(hours=23, minutes=59))
        second = manager.check_upstream()

        assert first.status == CHECK_PERFORMED
        assert second.status == CHECK_CACHED
        assert second.from_cache is True
        # No new upstream check was performed within the TTL (Req 23.4).
        assert source.calls == 1
        assert second.available[COMPONENT_INSIGHTCONNECT_SDK] == "6.2.0"
        # Cached result keeps the original check time.
        assert second.checked_at == first.checked_at

    def test_check_after_ttl_expiry_rechecks(self):
        manager, source, clock = make_manager()

        manager.check_upstream()
        clock.advance(timedelta(hours=24))
        result = manager.check_upstream()

        assert result.status == CHECK_PERFORMED
        assert source.calls == 2
        assert result.checked_at == datetime(2024, 1, 2, tzinfo=timezone.utc)

    def test_offline_mode_skips_check(self):
        config = UpdatesConfig(offline_mode=True, cache_ttl_hours=24)
        manager, source, _ = make_manager(config=config)

        result = manager.check_upstream()

        assert result.status == CHECK_SKIPPED_OFFLINE
        assert result.skipped is True
        assert result.available is None
        assert source.calls == 0

    def test_no_network_skips_check(self):
        manager, source, _ = make_manager(network_probe=lambda: False)

        result = manager.check_upstream()

        assert result.status == CHECK_SKIPPED_NO_NETWORK
        assert result.skipped is True
        assert result.available is None
        # The upstream source is never contacted without a network (Req 23.5).
        assert source.calls == 0

    def test_upstream_failure_reported_and_prior_cache_preserved(self):
        state = {"fail": False}

        def flaky_source():
            if state["fail"]:
                raise ConnectionError("boom")
            return dict(UPSTREAM)

        manager, _, clock = make_manager(upstream_source=flaky_source)

        first = manager.check_upstream()
        assert first.status == CHECK_PERFORMED

        # Force expiry so the next call attempts a real check, then make it fail.
        clock.advance(timedelta(hours=25))
        state["fail"] = True
        result = manager.check_upstream()

        assert result.status == CHECK_FAILED
        # Prior cached versions are preserved so the tool keeps operating.
        assert result.available[COMPONENT_INSIGHT_PLUGIN_CLI] == "1.3.0"
        assert "boom" in result.detail

    def test_installed_versions_available_independent_of_check(self):
        manager, _, _ = make_manager(network_probe=lambda: False)
        manager.snapshot_installed()

        result = manager.check_upstream()

        # Even when checks are skipped, installed versions remain usable.
        assert result.skipped is True
        assert manager.installed_versions[COMPONENT_KIRO_CLI] == "0.9.0"


class TestCheckUpstreamAsync:
    def test_async_check_returns_result_without_blocking(self):
        manager, source, _ = make_manager()

        result = asyncio.run(manager.check_upstream_async())

        assert isinstance(result, UpstreamCheckResult)
        assert result.status == CHECK_PERFORMED
        assert source.calls == 1


class RecordingInstaller:
    """An installer that records each (component, version) install call."""

    def __init__(self, fail_versions=None):
        self.calls = []
        self.fail_versions = set(fail_versions or ())

    def __call__(self, component, version):
        self.calls.append((component, version))
        if version in self.fail_versions:
            raise RuntimeError(f"install of {component} {version} failed")


class RecordingNotifier:
    """A notifier that captures every emitted notification."""

    def __init__(self):
        self.notifications = []

    def __call__(self, notification):
        self.notifications.append(notification)


def make_update_manager(*, smoke_passes=True, smoke_detail="", installer=None, notifier=None):
    """Build an UpdateManager wired with deterministic apply collaborators."""
    installer = installer if installer is not None else RecordingInstaller()
    notifier = notifier if notifier is not None else RecordingNotifier()

    def smoke_test(component, version):
        return SmokeTestResult(passed=smoke_passes, detail=smoke_detail)

    manager = UpdateManager(
        UpdatesConfig(offline_mode=False, cache_ttl_hours=24),
        installed_probe=lambda: dict(INSTALLED),
        upstream_source=CountingSource(UPSTREAM),
        clock=MutableClock(datetime(2024, 1, 1, tzinfo=timezone.utc)),
        installer=installer,
        smoke_test=smoke_test,
        notifier=notifier,
    )
    manager.snapshot_installed()
    return manager, installer, notifier


class TestApplyUpdate:
    def test_no_approval_leaves_installed_unchanged(self):
        # Req 23.7: nothing is upgraded without explicit approval.
        manager, installer, notifier = make_update_manager()

        result = manager.apply_update(COMPONENT_INSIGHT_PLUGIN_CLI, "1.3.0", approved=False)

        assert result.status == APPLY_NOT_APPROVED
        assert result.applied is False
        assert installer.calls == []
        assert manager.installed_versions[COMPONENT_INSIGHT_PLUGIN_CLI] == "1.2.3"
        assert notifier.notifications[-1].kind == NOTIFY_UPDATE_NOT_APPROVED

    def test_approved_update_records_new_version_when_smoke_passes(self):
        # Req 23.8: install + passing smoke test records the new version.
        manager, installer, notifier = make_update_manager(smoke_passes=True)

        result = manager.apply_update(COMPONENT_INSIGHT_PLUGIN_CLI, "1.3.0", approved=True)

        assert result.status == APPLY_APPLIED
        assert result.applied is True
        assert result.installed_version == "1.3.0"
        assert installer.calls == [(COMPONENT_INSIGHT_PLUGIN_CLI, "1.3.0")]
        assert manager.installed_versions[COMPONENT_INSIGHT_PLUGIN_CLI] == "1.3.0"
        assert notifier.notifications[-1].kind == NOTIFY_UPDATE_APPLIED

    def test_failing_smoke_test_rolls_back_with_reason(self):
        # Req 23.9: a failed smoke test rolls back and reports why.
        manager, installer, notifier = make_update_manager(
            smoke_passes=False, smoke_detail="sample plugin failed to validate"
        )

        result = manager.apply_update(COMPONENT_INSIGHT_PLUGIN_CLI, "1.3.0", approved=True)

        assert result.status == APPLY_ROLLED_BACK
        assert result.rolled_back is True
        # Recorded version is the pre-update version.
        assert result.installed_version == "1.2.3"
        assert manager.installed_versions[COMPONENT_INSIGHT_PLUGIN_CLI] == "1.2.3"
        # The install and the rollback reinstall were both attempted.
        assert installer.calls == [
            (COMPONENT_INSIGHT_PLUGIN_CLI, "1.3.0"),
            (COMPONENT_INSIGHT_PLUGIN_CLI, "1.2.3"),
        ]
        assert "sample plugin failed to validate" in result.reason
        assert notifier.notifications[-1].kind == NOTIFY_UPDATE_ROLLED_BACK

    def test_raising_smoke_test_counts_as_failure(self):
        installer = RecordingInstaller()
        notifier = RecordingNotifier()

        def raising_smoke_test(component, version):
            raise ConnectionError("boom")

        manager = UpdateManager(
            UpdatesConfig(offline_mode=False, cache_ttl_hours=24),
            installed_probe=lambda: dict(INSTALLED),
            upstream_source=CountingSource(UPSTREAM),
            installer=installer,
            smoke_test=raising_smoke_test,
            notifier=notifier,
        )
        manager.snapshot_installed()

        result = manager.apply_update(COMPONENT_INSIGHTCONNECT_SDK, "6.2.0", approved=True)

        assert result.status == APPLY_ROLLED_BACK
        assert "boom" in result.reason
        assert manager.installed_versions[COMPONENT_INSIGHTCONNECT_SDK] == "6.1.0"

    def test_install_failure_retains_previous_version(self):
        installer = RecordingInstaller(fail_versions={"1.3.0"})
        manager, installer, notifier = make_update_manager(installer=installer)

        result = manager.apply_update(COMPONENT_INSIGHT_PLUGIN_CLI, "1.3.0", approved=True)

        assert result.status == APPLY_INSTALL_FAILED
        assert result.installed_version == "1.2.3"
        assert manager.installed_versions[COMPONENT_INSIGHT_PLUGIN_CLI] == "1.2.3"

    def test_requires_collaborators_when_approved(self):
        manager = UpdateManager(
            UpdatesConfig(offline_mode=False, cache_ttl_hours=24),
            installed_probe=lambda: dict(INSTALLED),
            upstream_source=CountingSource(UPSTREAM),
        )
        manager.snapshot_installed()

        with pytest.raises(ValueError):
            manager.apply_update(COMPONENT_KIRO_CLI, "1.0.0", approved=True)


class TestStampBuild:
    def test_stamps_installed_versions_into_project_folder(self, tmp_path):
        # Req 23.2: the CLI/SDK versions used for the build are stored per build.
        manager, _, _ = make_update_manager()
        spec = PluginSpec(name="my_plugin", title="My Plugin", version=SemVer(1, 0, 0), vendor="acme_custom")
        folder = ProjectFolder.create(tmp_path, "my_plugin", spec)

        stamp = manager.stamp_build(folder, "1.0.0")

        assert stamp.insight_plugin_cli == "1.2.3"
        assert stamp.sdk_version == "6.1.0"
        recorded = folder.tooling()["1.0.0"]
        assert recorded.insight_plugin_cli == "1.2.3"
        assert recorded.sdk_version == "6.1.0"
        assert recorded.kiro_cli == "0.9.0"
        assert recorded.spec_schema == "v2"

    def test_explicit_used_versions_override_snapshot(self, tmp_path):
        manager, _, _ = make_update_manager()
        spec = PluginSpec(name="my_plugin", title="My Plugin", version=SemVer(2, 0, 0), vendor="acme_custom")
        folder = ProjectFolder.create(tmp_path, "my_plugin", spec)

        used = {
            COMPONENT_INSIGHT_PLUGIN_CLI: "1.4.0",
            COMPONENT_INSIGHTCONNECT_SDK: "6.3.0",
        }
        stamp = manager.stamp_build(folder, "2.0.0", used_versions=used)

        assert stamp.insight_plugin_cli == "1.4.0"
        assert stamp.sdk_version == "6.3.0"
        assert folder.tooling()["2.0.0"].sdk_version == "6.3.0"


class TestOfferSdkBump:
    def test_offers_bump_when_pin_behind_and_leaves_pin_unless_approved(self):
        # Req 23.10: offer a bump when behind; pin unchanged without approval.
        manager, _, notifier = make_update_manager()

        offer = manager.offer_sdk_bump("6.0.0", latest_known_good="6.2.0", approved=False)

        assert offer.offered is True
        assert offer.applied is False
        assert offer.pinned_version == "6.0.0"
        assert offer.latest_known_good == "6.2.0"
        assert notifier.notifications[-1].kind == NOTIFY_SDK_BUMP_OFFER

    def test_approved_bump_advances_the_pin(self):
        manager, _, _ = make_update_manager()

        offer = manager.offer_sdk_bump("6.0.0", latest_known_good="6.2.0", approved=True)

        assert offer.offered is True
        assert offer.applied is True
        assert offer.pinned_version == "6.2.0"

    def test_no_offer_when_pin_current(self):
        manager, _, notifier = make_update_manager()

        offer = manager.offer_sdk_bump("6.2.0", latest_known_good="6.2.0")

        assert offer.offered is False
        assert offer.pinned_version == "6.2.0"
        assert notifier.notifications == []

    def test_no_offer_when_pin_ahead(self):
        manager, _, _ = make_update_manager()

        offer = manager.offer_sdk_bump("7.0.0", latest_known_good="6.2.0")

        assert offer.offered is False
        assert offer.pinned_version == "7.0.0"

    def test_defaults_latest_to_installed_sdk_snapshot(self):
        # The installed InsightConnect SDK (6.1.0) is used as the known-good default.
        manager, _, _ = make_update_manager()

        offer = manager.offer_sdk_bump("5.9.0")

        assert offer.offered is True
        assert offer.latest_known_good == "6.1.0"
