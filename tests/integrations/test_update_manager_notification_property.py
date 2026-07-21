"""Property test for update-notification correctness (task 18.3).

The unit tests in ``test_update_manager.py`` pin the snapshot/check/apply
behaviour with specific examples; this module covers the universal property
behind Req 23.6: the Update_Manager notifies the user of a Managed_Tooling
component **iff** the latest available version is strictly newer than the
installed one, and every such notification carries the component, the installed
version, the available version, and a reference to that version's changelog.

``UpdateManager.notify_available_updates`` performs the installed-vs-available
comparison and emits one notification per component with a newer version. Every
collaborator is injected (the installed probe supplies the snapshot, a recording
notifier captures emitted notifications), so the test is fully deterministic --
no real binaries, network, or wall clock are involved.
"""

from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.api.config import UpdatesConfig
from icplugin_builder.core.spec_model import SemVer
from icplugin_builder.integrations.update_manager import (
    MANAGED_TOOLING_COMPONENTS,
    NOTIFY_UPDATE_AVAILABLE,
    UpdateManager,
)


class RecordingNotifier:
    """A notifier that captures every emitted notification."""

    def __init__(self):
        self.notifications = []

    def __call__(self, notification):
        self.notifications.append(notification)


def _semver_strings():
    """Generate strict ``MAJOR.MINOR.PATCH`` version strings with small parts.

    Small components keep the installed/available spaces overlapping so the
    generated inputs land on both sides of the "strictly newer" boundary --
    exercising both the notify and the no-notify branches.
    """
    part = st.integers(min_value=0, max_value=4)
    return st.builds(lambda a, b, c: f"{a}.{b}.{c}", part, part, part)


def _version_maps():
    """Generate a paired ``(installed, available)`` version map over shared components.

    Both maps are keyed by the same generated subset of Managed_Tooling
    components, so every available version has an installed counterpart to be
    compared against.
    """
    components = st.lists(
        st.sampled_from(list(MANAGED_TOOLING_COMPONENTS)),
        min_size=1,
        max_size=len(MANAGED_TOOLING_COMPONENTS),
        unique=True,
    )

    @st.composite
    def _maps(draw):
        chosen = draw(components)
        installed = {component: draw(_semver_strings()) for component in chosen}
        available = {component: draw(_semver_strings()) for component in chosen}
        return installed, available

    return _maps()


def _make_manager(installed, notifier):
    """Build an UpdateManager whose snapshot is ``installed`` and record via ``notifier``."""
    manager = UpdateManager(
        UpdatesConfig(offline_mode=False, cache_ttl_hours=24),
        installed_probe=lambda: dict(installed),
        upstream_source=lambda: {},
        clock=lambda: datetime(2024, 1, 1, tzinfo=timezone.utc),
        notifier=notifier,
    )
    manager.snapshot_installed()
    return manager


# Feature: insightconnect-plugin-builder, Property 43: Update notification iff newer version available
@settings(max_examples=200)
@given(maps=_version_maps())
def test_notify_iff_newer_version_available(maps):
    """Notify iff available is strictly newer than installed, with all fields populated.

    For every component, a notification is emitted exactly when the available
    version is strictly newer than the installed one (by semantic-version
    ordering). Each emitted notification carries the component, the installed
    version, the available version, and the changelog reference for that
    version; components whose available version is equal to or older than the
    installed one produce no notification.

    **Validates: Requirements 23.6**
    """
    installed, available = maps
    changelogs = {
        component: f"https://changelog.example/{component}/{version}" for component, version in available.items()
    }

    notifier = RecordingNotifier()
    manager = _make_manager(installed, notifier)

    notifications = manager.notify_available_updates(available, changelogs=changelogs)

    # The oracle: a component should be notified exactly when available > installed.
    expected_components = {
        component for component in available if SemVer.parse(available[component]) > SemVer.parse(installed[component])
    }

    # The manager returns and delivers the same notifications, one per newer component.
    assert notifications == tuple(notifier.notifications)
    notified_components = {notification.component for notification in notifications}
    assert notified_components == expected_components
    # Exactly one notification per newer component (no duplicates, none for non-newer).
    assert len(notifications) == len(expected_components)

    for notification in notifications:
        component = notification.component
        # Only strictly-newer components are notified.
        assert SemVer.parse(available[component]) > SemVer.parse(installed[component])
        # Every required field (Req 23.6) is populated correctly.
        assert notification.kind == NOTIFY_UPDATE_AVAILABLE
        assert notification.installed_version == installed[component]
        assert notification.available_version == available[component]
        assert notification.changelog_url == changelogs[component]
