"""Managed-tooling version snapshotting and cached upstream update checks.

The ``Update_Manager`` tracks the versions of the external tooling the
Plugin_Builder depends on -- the **Managed_Tooling** set (design "Update_Manager";
Req 23). This module implements the first slice of that component (task 18.1):

* :meth:`UpdateManager.snapshot_installed` records the currently-installed version
  of every :data:`MANAGED_TOOLING_COMPONENTS` component at startup (Req 23.1).
* :meth:`UpdateManager.check_upstream` asks an upstream source for the latest
  available version of each component, **caching** the result for a configurable
  duration so no new upstream check is performed until the cache expires
  (Req 23.3, 23.4), and **skipping** the check entirely when offline mode is
  enabled or the network is unavailable, continuing to operate on the installed
  versions (Req 23.5).
* :meth:`UpdateManager.check_upstream_async` runs the (potentially blocking)
  upstream check off the event loop so the Conversation_Interface is never
  blocked (Req 23.3).

Everything costly or non-deterministic is injected: an ``installed_probe`` that
reports installed versions, an ``upstream_source`` that reports upstream
versions, a ``clock`` supplying the current time, and a ``network_probe``
reporting network reachability. Injecting these keeps the manager fully
deterministic under test -- no real binaries, network, or wall clock are
required.

Applying approved updates with smoke-test + rollback (Req 23.7-23.9), stamping
per-build tool versions into the Project_Folder (Req 23.2), and the SDK-bump
offer (Req 23.10) build on this foundation (task 18.4):

* :meth:`UpdateManager.apply_update` installs a selected version **only when the
  user approves** (Req 23.7), runs a smoke test against a known-good sample
  plugin, records the new installed version **only if the smoke test passes**
  (Req 23.8), and otherwise rolls back to the previously-installed version and
  reports why (Req 23.9).
* :meth:`UpdateManager.stamp_build` writes the ``insight-plugin`` CLI and
  InsightConnect SDK versions actually used for a build into the plugin's
  Project_Folder (Req 23.2).
* :meth:`UpdateManager.offer_sdk_bump` offers to advance a loaded plugin's pinned
  InsightConnect SDK version when it is behind the latest known-good SDK, leaving
  the pin unchanged unless the user approves the bump (Req 23.10).

Notification of newer versions (Req 23.6) is covered by the check flow and its
property test. Everything costly or non-deterministic remains injected: the
installer that installs/rolls back a component, the smoke test that validates a
known-good sample, and a notifier that surfaces outcomes to the user are all
collaborators, keeping the manager fully deterministic under test.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Callable, Dict, List, Mapping, Optional, Tuple, Union

from ..api.config import UpdatesConfig
from ..core.spec_model import SemVer
from ..persistence.project_folder import ProjectFolder, ToolingStamp

__all__ = [
    "COMPONENT_INSIGHT_PLUGIN_CLI",
    "COMPONENT_INSIGHTCONNECT_SDK",
    "COMPONENT_KIRO_CLI",
    "COMPONENT_PLUGIN_SPEC_SCHEMA",
    "MANAGED_TOOLING_COMPONENTS",
    "CHECK_PERFORMED",
    "CHECK_CACHED",
    "CHECK_SKIPPED_OFFLINE",
    "CHECK_SKIPPED_NO_NETWORK",
    "CHECK_FAILED",
    "APPLY_APPLIED",
    "APPLY_ROLLED_BACK",
    "APPLY_INSTALL_FAILED",
    "APPLY_NOT_APPROVED",
    "NOTIFY_UPDATE_APPLIED",
    "NOTIFY_UPDATE_ROLLED_BACK",
    "NOTIFY_UPDATE_NOT_APPROVED",
    "NOTIFY_SDK_BUMP_OFFER",
    "NOTIFY_UPDATE_AVAILABLE",
    "ToolingVersions",
    "UpstreamCheckResult",
    "SmokeTestResult",
    "ApplyResult",
    "SdkBumpOffer",
    "UpdateNotification",
    "InstalledProbe",
    "UpstreamSource",
    "Clock",
    "NetworkProbe",
    "Installer",
    "SmokeTest",
    "Notifier",
    "UpdateManager",
]

#: The ``insight-plugin`` CLI used to scaffold, refresh, and validate plugins.
COMPONENT_INSIGHT_PLUGIN_CLI = "insight_plugin_cli"

#: The InsightConnect SDK version pinned in each Plugin_Spec.
COMPONENT_INSIGHTCONNECT_SDK = "insightconnect_sdk"

#: The Kiro CLI used as the primary LLM provider.
COMPONENT_KIRO_CLI = "kiro_cli"

#: The plugin specification schema version.
COMPONENT_PLUGIN_SPEC_SCHEMA = "plugin_spec_schema"

#: The canonical, stable-ordered set of Managed_Tooling components (Req 23,
#: glossary "Managed_Tooling").
MANAGED_TOOLING_COMPONENTS = (
    COMPONENT_INSIGHT_PLUGIN_CLI,
    COMPONENT_INSIGHTCONNECT_SDK,
    COMPONENT_KIRO_CLI,
    COMPONENT_PLUGIN_SPEC_SCHEMA,
)

#: An upstream check was performed and its result is fresh (Req 23.3).
CHECK_PERFORMED = "checked"

#: A cached result was returned; no new upstream check was performed (Req 23.4).
CHECK_CACHED = "cached"

#: The check was skipped because offline mode is enabled (Req 23.5).
CHECK_SKIPPED_OFFLINE = "skipped_offline"

#: The check was skipped because the network is unavailable (Req 23.5).
CHECK_SKIPPED_NO_NETWORK = "skipped_no_network"

#: An upstream check was attempted but the source raised; the prior cache (if
#: any) is preserved and operation continues on installed versions (Req 23.5).
CHECK_FAILED = "check_failed"

#: An approved update installed cleanly and its smoke test passed; the new
#: version is now recorded as installed (Req 23.8).
APPLY_APPLIED = "applied"

#: An approved update installed but its smoke test failed, so it was rolled back
#: to the previously-installed version with a reason (Req 23.9).
APPLY_ROLLED_BACK = "rolled_back"

#: An approved update could not be installed; the installer raised before the
#: smoke test, so the previously-installed version is retained (Req 23.9).
APPLY_INSTALL_FAILED = "install_failed"

#: No update was applied because the user did not approve it (Req 23.7).
APPLY_NOT_APPROVED = "not_approved"

#: Notification kind: an approved update was applied after a passing smoke test.
NOTIFY_UPDATE_APPLIED = "update_applied"

#: Notification kind: an approved update was rolled back after a failing smoke test.
NOTIFY_UPDATE_ROLLED_BACK = "update_rolled_back"

#: Notification kind: an update was requested without approval and not applied.
NOTIFY_UPDATE_NOT_APPROVED = "update_not_approved"

#: Notification kind: a plugin's pinned SDK is behind and a bump is offered.
NOTIFY_SDK_BUMP_OFFER = "sdk_bump_offer"

#: Notification kind: a newer version of a Managed_Tooling component is available
#: upstream (Req 23.6).
NOTIFY_UPDATE_AVAILABLE = "update_available"


@dataclass(frozen=True)
class ToolingVersions:
    """An immutable snapshot of ``component -> version`` for Managed_Tooling.

    Used both for the installed snapshot (Req 23.1) and for the latest available
    versions reported by an upstream check (Req 23.3). Missing components simply
    have no entry, so a partial probe (for example when a binary is absent)
    still produces a valid snapshot.

    Attributes:
        versions: A read-only mapping from component identifier (one of
            :data:`MANAGED_TOOLING_COMPONENTS`) to its version string.
    """

    versions: Mapping[str, str]

    def __post_init__(self) -> None:
        # Defensively copy into a read-only mapping so the snapshot is immutable
        # regardless of what the caller passed in.
        object.__setattr__(self, "versions", MappingProxyType(dict(self.versions)))

    def get(self, component: str) -> Optional[str]:
        """Return the version recorded for ``component``, or ``None`` if absent."""
        return self.versions.get(component)

    def __contains__(self, component: object) -> bool:
        return component in self.versions

    def __getitem__(self, component: str) -> str:
        return self.versions[component]


@dataclass(frozen=True)
class UpstreamCheckResult:
    """The outcome of a call to :meth:`UpdateManager.check_upstream`.

    Attributes:
        status: One of :data:`CHECK_PERFORMED`, :data:`CHECK_CACHED`,
            :data:`CHECK_SKIPPED_OFFLINE`, :data:`CHECK_SKIPPED_NO_NETWORK`, or
            :data:`CHECK_FAILED`.
        available: The latest available versions reported upstream, or ``None``
            when the check was skipped or failed (Req 23.5).
        checked_at: When the underlying upstream check that produced
            :attr:`available` ran; ``None`` when no check has ever succeeded.
        detail: A short human-readable description of the outcome.
    """

    status: str
    available: Optional[ToolingVersions] = None
    checked_at: Optional[datetime] = None
    detail: str = ""

    @property
    def performed(self) -> bool:
        """Return ``True`` iff a fresh upstream check was performed."""
        return self.status == CHECK_PERFORMED

    @property
    def from_cache(self) -> bool:
        """Return ``True`` iff a cached result was returned without rechecking."""
        return self.status == CHECK_CACHED

    @property
    def skipped(self) -> bool:
        """Return ``True`` iff the check was skipped (offline or no network)."""
        return self.status in (CHECK_SKIPPED_OFFLINE, CHECK_SKIPPED_NO_NETWORK)


@dataclass(frozen=True)
class SmokeTestResult:
    """The outcome of the post-install smoke test (Req 23.8, 23.9).

    Attributes:
        passed: ``True`` iff the known-good sample plugin validated with the
            updated tooling.
        detail: A short human-readable description of the smoke-test outcome;
            used as the rollback reason when :attr:`passed` is ``False``.
    """

    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class ApplyResult:
    """The outcome of :meth:`UpdateManager.apply_update` (Req 23.7-23.9).

    Attributes:
        component: The Managed_Tooling component the update targeted.
        requested_version: The version the user asked to install.
        installed_version: The version installed **after** this call: the new
            version on success, or the pre-update version after a rollback,
            not-approved, or install failure.
        status: One of :data:`APPLY_APPLIED`, :data:`APPLY_ROLLED_BACK`,
            :data:`APPLY_INSTALL_FAILED`, or :data:`APPLY_NOT_APPROVED`.
        reason: A short human-readable explanation, always populated when the
            update was not applied (Req 23.9).
    """

    component: str
    requested_version: str
    installed_version: Optional[str]
    status: str
    reason: str = ""

    @property
    def applied(self) -> bool:
        """Return ``True`` iff the requested version is now installed (Req 23.8)."""
        return self.status == APPLY_APPLIED

    @property
    def rolled_back(self) -> bool:
        """Return ``True`` iff the update was rolled back after a smoke-test failure."""
        return self.status == APPLY_ROLLED_BACK


@dataclass(frozen=True)
class SdkBumpOffer:
    """The outcome of :meth:`UpdateManager.offer_sdk_bump` (Req 23.10).

    Attributes:
        offered: ``True`` iff the pinned SDK is behind the latest known-good SDK
            and a bump is therefore offered on the next refresh.
        pinned_version: The plugin's pinned SDK version **after** this call: the
            latest known-good version only when the user approved, otherwise the
            unchanged original pin.
        latest_known_good: The latest known-good SDK version considered.
        applied: ``True`` iff the user approved and the pin was advanced.
        message: A short human-readable description of the offer or its result.
    """

    offered: bool
    pinned_version: str
    latest_known_good: Optional[str]
    applied: bool = False
    message: str = ""


@dataclass(frozen=True)
class UpdateNotification:
    """A user-facing notification emitted by the Update_Manager.

    Attributes:
        kind: One of the ``NOTIFY_*`` constants.
        component: The Managed_Tooling component the notification concerns.
        message: The human-readable notification text.
        installed_version: The relevant installed/pinned version, when applicable.
        available_version: The relevant available/target version, when applicable.
        changelog_url: A reference to the available version's changelog, populated
            for update-available notifications when a reference is known (Req 23.6).
    """

    kind: str
    component: str
    message: str
    installed_version: Optional[str] = None
    available_version: Optional[str] = None
    changelog_url: Optional[str] = None


#: A callable returning the currently-installed Managed_Tooling versions.
InstalledProbe = Callable[[], Mapping[str, str]]

#: A callable returning the latest available Managed_Tooling versions upstream.
#: May raise to signal a transient upstream/network failure.
UpstreamSource = Callable[[], Mapping[str, str]]

#: A callable returning the current time as a timezone-aware ``datetime``.
Clock = Callable[[], datetime]

#: A callable returning whether network access is currently available.
NetworkProbe = Callable[[], bool]

#: A callable that installs ``version`` of ``component`` (or reinstalls a prior
#: version on rollback). May raise to signal an install failure.
Installer = Callable[[str, str], None]

#: A callable that runs the post-install smoke test for ``component`` at
#: ``version`` against a known-good sample plugin, returning a
#: :class:`SmokeTestResult`. May raise, which is treated as a failed smoke test.
SmokeTest = Callable[[str, str], SmokeTestResult]

#: A callable that surfaces an :class:`UpdateNotification` to the user.
Notifier = Callable[["UpdateNotification"], None]


def _utc_now() -> datetime:
    """Return the current UTC time as a timezone-aware ``datetime``."""
    return datetime.now(timezone.utc)


def _is_behind(pinned: str, latest: str) -> bool:
    """Return ``True`` iff ``pinned`` is behind ``latest`` (Req 23.10).

    When both values parse as ``MAJOR.MINOR.PATCH`` semantic versions they are
    compared by their total ordering, so a bump is offered only when the pin is
    strictly older. When either value is not a semantic version the two cannot be
    ordered numerically, so a bump is offered whenever they differ, treating any
    mismatch with the latest known-good version as behind.
    """
    if SemVer.is_valid(pinned) and SemVer.is_valid(latest):
        return SemVer.parse(pinned) < SemVer.parse(latest)
    return pinned.strip() != latest.strip()


@dataclass
class _CacheEntry:
    """The last successfully-performed upstream check, used to honor the TTL."""

    versions: ToolingVersions
    checked_at: datetime = field(default_factory=_utc_now)


class UpdateManager:
    """Snapshots installed tooling and performs cached, skippable upstream checks.

    The manager is constructed with the update settings (offline mode and cache
    TTL) plus injected probes. It records installed versions at startup
    (Req 23.1) and answers upstream checks while enforcing the caching and
    offline/no-network rules (Req 23.3-23.5). It performs no version comparison
    or notification itself; callers read :attr:`installed_versions` and the
    returned :class:`UpstreamCheckResult` to decide what to surface (Req 23.6,
    implemented separately).
    """

    def __init__(
        self,
        config: UpdatesConfig,
        *,
        installed_probe: InstalledProbe,
        upstream_source: UpstreamSource,
        clock: Clock = _utc_now,
        network_probe: NetworkProbe = lambda: True,
        installer: Optional[Installer] = None,
        smoke_test: Optional[SmokeTest] = None,
        notifier: Optional[Notifier] = None,
    ) -> None:
        """Configure the manager.

        Args:
            config: The update settings (``offline_mode``, ``cache_ttl_hours``).
            installed_probe: Reports the installed version of each component.
            upstream_source: Reports the latest available version of each
                component; may raise to signal a transient failure.
            clock: Supplies the current time; injected for deterministic tests.
            network_probe: Reports whether the network is reachable; defaults to
                assuming it is, so real absence surfaces via ``upstream_source``.
            installer: Installs (and, on rollback, reinstalls) a component
                version; required by :meth:`apply_update`. May raise to signal an
                install failure.
            smoke_test: Runs the post-install smoke test against a known-good
                sample plugin; required by :meth:`apply_update`.
            notifier: Optional sink for user-facing :class:`UpdateNotification`
                messages; defaults to a no-op.
        """
        self._config = config
        self._installed_probe = installed_probe
        self._upstream_source = upstream_source
        self._clock = clock
        self._network_probe = network_probe
        self._installer = installer
        self._smoke_test = smoke_test
        self._notifier = notifier

        self._installed: Optional[ToolingVersions] = None
        self._cache: Optional[_CacheEntry] = None

    @property
    def config(self) -> UpdatesConfig:
        """The update settings governing offline mode and cache lifetime."""
        return self._config

    @property
    def installed_versions(self) -> Optional[ToolingVersions]:
        """The most recent installed snapshot, or ``None`` before startup snapshot."""
        return self._installed

    @property
    def cache_ttl(self) -> timedelta:
        """The configured cache lifetime as a :class:`~datetime.timedelta` (Req 23.4)."""
        return timedelta(hours=self._config.cache_ttl_hours)

    def snapshot_installed(self) -> ToolingVersions:
        """Record and return the currently-installed Managed_Tooling versions.

        Called at startup (Req 23.1). The installed probe is invoked and its
        result cached as :attr:`installed_versions`, so later steps (upstream
        comparison, SDK-bump offers) can read the recorded baseline without
        re-probing.

        Returns:
            The installed :class:`ToolingVersions` snapshot.
        """
        snapshot = ToolingVersions(self._installed_probe())
        self._installed = snapshot
        return snapshot

    def check_upstream(self) -> UpstreamCheckResult:
        """Check upstream for the latest versions, honoring caching and skips.

        The check is skipped -- returning without contacting the upstream source
        -- when offline mode is enabled or the network is unavailable, so the
        tool continues operating on the installed versions (Req 23.5). When a
        prior successful check is still within the cache TTL, the cached result
        is returned and no new upstream check is performed (Req 23.4). Otherwise
        the upstream source is consulted; a raised error is reported as a failed
        check that preserves any prior cache and continues operation (Req 23.5).

        Returns:
            An :class:`UpstreamCheckResult` describing the outcome and, when
            available, the latest upstream versions.
        """
        if self._config.offline_mode:
            return UpstreamCheckResult(
                status=CHECK_SKIPPED_OFFLINE,
                detail="offline mode is enabled; upstream update check skipped",
            )

        if not self._network_probe():
            return UpstreamCheckResult(
                status=CHECK_SKIPPED_NO_NETWORK,
                detail="network unavailable; upstream update check skipped",
            )

        now = self._clock()
        if self._cache is not None and not self._is_cache_expired(now):
            return UpstreamCheckResult(
                status=CHECK_CACHED,
                available=self._cache.versions,
                checked_at=self._cache.checked_at,
                detail="returned cached upstream result; cache has not expired",
            )

        try:
            available = ToolingVersions(self._upstream_source())
        except Exception as error:  # noqa: BLE001 - surfaced as a skippable failure
            preserved = self._cache
            return UpstreamCheckResult(
                status=CHECK_FAILED,
                available=preserved.versions if preserved is not None else None,
                checked_at=preserved.checked_at if preserved is not None else None,
                detail=f"upstream update check failed: {error}",
            )

        self._cache = _CacheEntry(versions=available, checked_at=now)
        return UpstreamCheckResult(
            status=CHECK_PERFORMED,
            available=available,
            checked_at=now,
            detail="performed a fresh upstream update check",
        )

    def apply_update(self, component: str, version: str, *, approved: bool = False) -> ApplyResult:
        """Apply an approved update with smoke-test gating and rollback.

        No component is ever upgraded without explicit approval: when ``approved``
        is ``False`` the installer is never invoked and every installed version is
        left unchanged (Req 23.7). When approved, the selected version is
        installed and a smoke test validates a known-good sample plugin with the
        updated tooling; the new version is recorded as installed **only if the
        smoke test passes** (Req 23.8). If the smoke test fails (or raises), the
        component is rolled back to its previously-installed version and the
        result reports why (Req 23.9). If the install itself fails, the
        previously-installed version is retained.

        Args:
            component: The Managed_Tooling component to update.
            version: The version to install.
            approved: Whether the user explicitly approved this update.

        Returns:
            An :class:`ApplyResult` describing what is installed after the call.

        Raises:
            ValueError: if no installer or smoke test was configured, so an
                approved update cannot be attempted.
        """
        previous = self._installed_version(component)

        if not approved:
            reason = f"update to {component} {version} was not applied because it was not approved"
            self._notify(
                UpdateNotification(
                    kind=NOTIFY_UPDATE_NOT_APPROVED,
                    component=component,
                    message=reason,
                    installed_version=previous,
                    available_version=version,
                )
            )
            return ApplyResult(
                component=component,
                requested_version=version,
                installed_version=previous,
                status=APPLY_NOT_APPROVED,
                reason=reason,
            )

        if self._installer is None or self._smoke_test is None:
            raise ValueError("apply_update requires an installer and a smoke_test collaborator")

        try:
            self._installer(component, version)
        except Exception as error:  # noqa: BLE001 - surfaced as an install failure
            reason = f"failed to install {component} {version}: {error}"
            self._notify(
                UpdateNotification(
                    kind=NOTIFY_UPDATE_ROLLED_BACK,
                    component=component,
                    message=reason,
                    installed_version=previous,
                    available_version=version,
                )
            )
            return ApplyResult(
                component=component,
                requested_version=version,
                installed_version=previous,
                status=APPLY_INSTALL_FAILED,
                reason=reason,
            )

        try:
            outcome = self._smoke_test(component, version)
        except Exception as error:  # noqa: BLE001 - a raising smoke test counts as failure
            outcome = SmokeTestResult(passed=False, detail=f"smoke test raised: {error}")

        if outcome.passed:
            self._record_installed(component, version)
            message = f"updated {component} to {version}; smoke test passed"
            self._notify(
                UpdateNotification(
                    kind=NOTIFY_UPDATE_APPLIED,
                    component=component,
                    message=message,
                    installed_version=version,
                    available_version=version,
                )
            )
            return ApplyResult(
                component=component,
                requested_version=version,
                installed_version=version,
                status=APPLY_APPLIED,
                reason=message,
            )

        # Smoke test failed: roll back to the previously-installed version.
        rollback_note = ""
        if previous is not None:
            try:
                self._installer(component, previous)
            except Exception as error:  # noqa: BLE001 - report but keep pre-update version recorded
                rollback_note = f" (rollback reinstall reported: {error})"
        else:
            rollback_note = " (no prior version to reinstall)"

        detail = outcome.detail or "smoke test failed"
        reason = f"update to {component} {version} was not applied: {detail}; rolled back to {previous}{rollback_note}"
        self._notify(
            UpdateNotification(
                kind=NOTIFY_UPDATE_ROLLED_BACK,
                component=component,
                message=reason,
                installed_version=previous,
                available_version=version,
            )
        )
        return ApplyResult(
            component=component,
            requested_version=version,
            installed_version=previous,
            status=APPLY_ROLLED_BACK,
            reason=reason,
        )

    def stamp_build(
        self,
        project: ProjectFolder,
        version: str,
        *,
        used_versions: Optional[Mapping[str, str]] = None,
    ) -> ToolingStamp:
        """Stamp the tool versions used for a build into the Project_Folder (Req 23.2).

        Records the ``insight-plugin`` CLI and InsightConnect SDK versions (plus
        the Kiro CLI and plugin-spec schema versions) actually used for the build
        of ``version`` into the plugin's ``.builder/tooling.json``. The stamped
        versions come from the manager's installed snapshot unless
        ``used_versions`` overrides them, so the stamp always reflects the tooling
        actually in effect for the build.

        Args:
            project: The plugin's Project_Folder to stamp.
            version: The version this build produced.
            used_versions: Optional explicit ``component -> version`` mapping of
                the tooling used; defaults to the installed snapshot.

        Returns:
            The :class:`ToolingStamp` that was recorded.
        """
        source = dict(used_versions) if used_versions is not None else dict(self._ensure_installed().versions)
        stamp = ToolingStamp(
            insight_plugin_cli=source.get(COMPONENT_INSIGHT_PLUGIN_CLI),
            sdk_version=source.get(COMPONENT_INSIGHTCONNECT_SDK),
            kiro_cli=source.get(COMPONENT_KIRO_CLI),
            spec_schema=source.get(COMPONENT_PLUGIN_SPEC_SCHEMA),
        )
        project.stamp_build_tooling(version, stamp)
        return stamp

    def offer_sdk_bump(
        self,
        pinned_sdk: str,
        *,
        latest_known_good: Optional[str] = None,
        approved: bool = False,
    ) -> SdkBumpOffer:
        """Offer to advance a plugin's pinned SDK when it is behind (Req 23.10).

        When a loaded plugin's pinned InsightConnect SDK version is behind the
        latest known-good SDK version, this offers a bump on the next refresh. The
        pinned version is left unchanged unless the user approves the bump; only
        an approved offer advances the pin to the latest known-good version.

        Args:
            pinned_sdk: The plugin's currently-pinned SDK version.
            latest_known_good: The latest known-good SDK version to compare
                against; defaults to the installed InsightConnect SDK version
                recorded in the snapshot.
            approved: Whether the user approved advancing the pin.

        Returns:
            An :class:`SdkBumpOffer` describing whether a bump is offered and the
            resulting pinned version.
        """
        latest = latest_known_good
        if latest is None:
            installed = self._installed
            latest = installed.get(COMPONENT_INSIGHTCONNECT_SDK) if installed is not None else None

        if latest is None or not _is_behind(pinned_sdk, latest):
            return SdkBumpOffer(
                offered=False,
                pinned_version=pinned_sdk,
                latest_known_good=latest,
                applied=False,
                message=f"pinned SDK {pinned_sdk} is current; no bump offered",
            )

        if approved:
            message = f"pinned SDK bumped from {pinned_sdk} to {latest}"
            self._notify(
                UpdateNotification(
                    kind=NOTIFY_SDK_BUMP_OFFER,
                    component=COMPONENT_INSIGHTCONNECT_SDK,
                    message=message,
                    installed_version=pinned_sdk,
                    available_version=latest,
                )
            )
            return SdkBumpOffer(
                offered=True,
                pinned_version=latest,
                latest_known_good=latest,
                applied=True,
                message=message,
            )

        message = f"SDK {latest} is available; pinned SDK {pinned_sdk} left unchanged pending approval"
        self._notify(
            UpdateNotification(
                kind=NOTIFY_SDK_BUMP_OFFER,
                component=COMPONENT_INSIGHTCONNECT_SDK,
                message=message,
                installed_version=pinned_sdk,
                available_version=latest,
            )
        )
        return SdkBumpOffer(
            offered=True,
            pinned_version=pinned_sdk,
            latest_known_good=latest,
            applied=False,
            message=message,
        )

    def _installed_version(self, component: str) -> Optional[str]:
        """Return the currently-installed version of ``component`` (snapshotting first)."""
        return self._ensure_installed().get(component)

    def _ensure_installed(self) -> ToolingVersions:
        """Return the installed snapshot, taking it now if it has not been taken."""
        if self._installed is None:
            return self.snapshot_installed()
        return self._installed

    def _record_installed(self, component: str, version: str) -> None:
        """Record ``version`` as the newly-installed version of ``component``."""
        current: Dict[str, str] = dict(self._ensure_installed().versions)
        current[component] = version
        self._installed = ToolingVersions(current)

    def _notify(self, notification: UpdateNotification) -> None:
        """Deliver ``notification`` to the injected notifier, if any."""
        if self._notifier is not None:
            self._notifier(notification)

    async def check_upstream_async(self) -> UpstreamCheckResult:
        """Run :meth:`check_upstream` off the event loop (Req 23.3).

        The upstream check may block on network I/O, so it is dispatched to a
        worker thread. This keeps the Conversation_Interface responsive while the
        check runs.

        Returns:
            The :class:`UpstreamCheckResult` produced by :meth:`check_upstream`.
        """
        return await asyncio.to_thread(self.check_upstream)

    def _is_cache_expired(self, now: datetime) -> bool:
        """Return ``True`` iff the cached result is at or past its TTL (Req 23.4)."""
        if self._cache is None:
            return True
        return now - self._cache.checked_at >= self.cache_ttl

    def notify_available_updates(
        self,
        available: Union[ToolingVersions, Mapping[str, str]],
        *,
        changelogs: Optional[Mapping[str, str]] = None,
    ) -> Tuple[UpdateNotification, ...]:
        """Notify the user of every component whose available version is newer (Req 23.6).

        Compares the installed snapshot against the latest ``available`` versions
        and emits exactly one :class:`UpdateNotification` -- carrying the
        component, its installed version, the available version, and a reference
        to that version's changelog -- for each component whose available version
        is strictly newer than the installed one. Components whose available
        version is equal to or older than the installed one produce no
        notification, so the user is notified **iff** a newer version exists.

        The installed snapshot is taken now if it has not been taken yet, so the
        comparison always has a baseline. Notifications are delivered to the
        injected notifier (if any) in stable component order.

        Args:
            available: The latest available ``component -> version`` versions, as
                a :class:`ToolingVersions` or a plain mapping.
            changelogs: Optional ``component -> changelog reference`` mapping used
                to populate each notification's :attr:`UpdateNotification.changelog_url`.

        Returns:
            The tuple of emitted :class:`UpdateNotification` objects, one per
            component with a newer version, in stable component order.
        """
        available_versions = available if isinstance(available, ToolingVersions) else ToolingVersions(available)
        installed = self._ensure_installed()
        changelog_refs = dict(changelogs or {})

        notifications: List[UpdateNotification] = []
        for component in sorted(available_versions.versions):
            installed_version = installed.get(component)
            available_version = available_versions.get(component)
            if installed_version is None or available_version is None:
                continue
            if not _is_behind(installed_version, available_version):
                continue
            changelog_url = changelog_refs.get(component)
            message = f"newer {component} available: {available_version} (installed {installed_version})"
            notification = UpdateNotification(
                kind=NOTIFY_UPDATE_AVAILABLE,
                component=component,
                message=message,
                installed_version=installed_version,
                available_version=available_version,
                changelog_url=changelog_url,
            )
            self._notify(notification)
            notifications.append(notification)
        return tuple(notifications)
