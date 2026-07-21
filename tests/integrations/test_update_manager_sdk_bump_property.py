"""Property test for the SDK-bump offer (task 18.7).

The unit tests in ``test_update_manager.py`` pin specific SDK-bump examples;
this module covers the universal property across generated inputs: for any
pinned InsightConnect SDK version and any latest-known-good SDK version,
:meth:`UpdateManager.offer_sdk_bump` must offer a bump **iff** the pinned
version is strictly behind the latest, and must leave the pin unchanged unless
the user approves -- an approved bump (and only an approved bump) advances the
pin to the latest known-good version (Req 23.10).

Both the pinned and latest versions are generated as ``MAJOR.MINOR.PATCH``
SemVer strings and compared with :class:`SemVer`'s total ordering, so the
"behind" relation the offer is checked against is computed independently of the
manager. No network, real binaries, or wall clock are involved.
"""

from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.api.config import UpdatesConfig
from icplugin_builder.core.spec_model import SemVer
from icplugin_builder.integrations.update_manager import UpdateManager

# SemVer strings with small, comparable components so "behind" is exercised in
# both directions (behind, equal, and ahead) across generated pairs.
semver_strings = st.builds(
    lambda a, b, c: f"{a}.{b}.{c}",
    st.integers(min_value=0, max_value=8),
    st.integers(min_value=0, max_value=8),
    st.integers(min_value=0, max_value=8),
)


def _make_manager():
    """Build an UpdateManager with no external tooling required by this call."""
    return UpdateManager(
        UpdatesConfig(offline_mode=False, cache_ttl_hours=24),
        installed_probe=lambda: {},
        upstream_source=lambda: {},
        clock=lambda: datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


# Feature: insightconnect-plugin-builder, Property 46: SDK bump offered but not applied without approval
@settings(max_examples=200)
@given(
    pinned=semver_strings,
    latest=semver_strings,
    approved=st.booleans(),
)
def test_sdk_bump_offered_iff_behind_and_applied_only_when_approved(pinned, latest, approved):
    """A bump is offered iff the pin is behind; the pin only advances on approval.

    For any pinned SDK version and any latest-known-good SDK version:

    * ``offer.offered`` is ``True`` iff the pinned version is strictly behind the
      latest (computed independently via :class:`SemVer`'s ordering);
    * when the pin is **not** behind, no bump is offered and the pin is unchanged;
    * when the pin **is** behind but the user did not approve, the bump is offered
      yet the pin is left unchanged and ``applied`` is ``False``;
    * when the pin is behind and the user approved, the pin advances exactly to
      the latest known-good version and ``applied`` is ``True``.

    **Validates: Requirements 23.10**
    """
    behind = SemVer.parse(pinned) < SemVer.parse(latest)

    manager = _make_manager()
    offer = manager.offer_sdk_bump(pinned, latest_known_good=latest, approved=approved)

    # The latest considered is always reported back unchanged.
    assert offer.latest_known_good == latest

    # A bump is offered iff the pinned version is strictly behind the latest.
    assert offer.offered is behind

    if not behind:
        # Nothing to offer: the pin is untouched regardless of approval.
        assert offer.applied is False
        assert offer.pinned_version == pinned
    elif approved:
        # Approved bump advances the pin exactly to the latest known-good version.
        assert offer.applied is True
        assert offer.pinned_version == latest
    else:
        # Offered but not approved: the pin is left unchanged.
        assert offer.applied is False
        assert offer.pinned_version == pinned
