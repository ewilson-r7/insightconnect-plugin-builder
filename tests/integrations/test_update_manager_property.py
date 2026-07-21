"""Property test for update-check caching (task 18.2).

The unit tests in ``test_update_manager.py`` pin specific TTL examples; this
module covers the universal property across generated inputs: once an upstream
check has been performed, :meth:`UpdateManager.check_upstream` must return the
cached result -- contacting the upstream source zero additional times -- for
every call issued while still within the configured cache TTL, and must perform
exactly one fresh upstream check the moment a call lands at or past the TTL
(Req 23.4).

The clock is an injected :class:`MutableClock` advanced by generated deltas and
the upstream source is a :class:`CountingSource` that records every call, so the
number of real upstream checks is observed directly with no network, real
binaries, or wall clock involved.
"""

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.api.config import UpdatesConfig
from icplugin_builder.integrations.update_manager import (
    CHECK_CACHED,
    CHECK_PERFORMED,
    COMPONENT_INSIGHTCONNECT_SDK,
    UpdateManager,
)

UPSTREAM = {COMPONENT_INSIGHTCONNECT_SDK: "6.2.0"}


class MutableClock:
    """A deterministic, advanceable clock returning timezone-aware times."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class CountingSource:
    """An upstream source that counts calls and returns fixed versions."""

    def __init__(self, versions) -> None:
        self.versions = dict(versions)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return dict(self.versions)


# Feature: insightconnect-plugin-builder, Property 42: Update-check caching honored
@settings(max_examples=200)
@given(
    ttl_hours=st.integers(min_value=1, max_value=72),
    advance_minutes=st.lists(st.integers(min_value=0, max_value=6000), min_size=1, max_size=25),
)
def test_no_upstream_check_within_cache_ttl(ttl_hours, advance_minutes):
    """No new upstream check occurs within the TTL; a call past it rechecks.

    After a first performed check, the clock is advanced by a generated sequence
    of deltas. For each subsequent call, if the elapsed time since the last
    performed check is still under the TTL the result must be served from cache
    (source call count unchanged, ``checked_at`` pinned to the cached check), and
    if it is at or past the TTL exactly one fresh upstream check must occur
    (source call count incremented by one), which resets the caching window.

    **Validates: Requirements 23.4**
    """
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    clock = MutableClock(start)
    source = CountingSource(UPSTREAM)
    ttl = timedelta(hours=ttl_hours)
    manager = UpdateManager(
        UpdatesConfig(offline_mode=False, cache_ttl_hours=ttl_hours),
        installed_probe=lambda: {},
        upstream_source=source,
        clock=clock,
        network_probe=lambda: True,
    )

    # The first check is always performed and seeds the cache.
    first = manager.check_upstream()
    assert first.status == CHECK_PERFORMED
    assert source.calls == 1

    last_checked_at = first.checked_at
    expected_calls = 1

    for minutes in advance_minutes:
        clock.advance(timedelta(minutes=minutes))
        now = clock.now
        result = manager.check_upstream()

        if now - last_checked_at >= ttl:
            # Past the TTL: exactly one fresh upstream check, window resets.
            expected_calls += 1
            assert result.status == CHECK_PERFORMED
            assert result.checked_at == now
            last_checked_at = now
        else:
            # Within the TTL: served from cache, no new upstream check.
            assert result.status == CHECK_CACHED
            assert result.from_cache is True
            assert result.checked_at == last_checked_at

        assert source.calls == expected_calls
