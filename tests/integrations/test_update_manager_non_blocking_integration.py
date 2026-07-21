"""Integration test: the startup/interval update check does not block (task 18.9).

Requirement 23.3 states that, at startup and at a configurable interval
thereafter, the Update_Manager checks upstream sources for the latest available
version of each Managed_Tooling component *without blocking the
Conversation_Interface*. :meth:`UpdateManager.check_upstream_async` satisfies
this by dispatching the potentially-blocking check via :func:`asyncio.to_thread`
so it runs off the event loop.

Unlike the fast, fully-synchronous collaborators used by the unit tests in
``test_update_manager.py`` (which assert *what* the check returns), this module
drives ``check_upstream_async`` on a real event loop against a **genuinely
blocking** upstream source. The source parks on a :class:`threading.Event` that
is only released from a *concurrently-running coroutine*. If the check ran on the
event loop instead of a worker thread, that releasing coroutine would never get
to run, the source would never be released, and the test would time out --
so the test passing is direct evidence the check does not block the loop.

The offline path (Req 23.5) is also exercised end-to-end through the async entry
point: with offline mode enabled the upstream source is never contacted, and the
manager continues to operate on the installed versions.
"""

import asyncio
import threading
import time
from datetime import datetime, timezone

import pytest

from icplugin_builder.api.config import UpdatesConfig
from icplugin_builder.integrations.update_manager import (
    CHECK_PERFORMED,
    CHECK_SKIPPED_OFFLINE,
    COMPONENT_INSIGHT_PLUGIN_CLI,
    COMPONENT_INSIGHTCONNECT_SDK,
    COMPONENT_KIRO_CLI,
    COMPONENT_PLUGIN_SPEC_SCHEMA,
    UpdateManager,
    UpstreamCheckResult,
)

# A safety valve so a regression that reintroduces blocking fails fast instead of
# hanging the suite. Generous relative to the sub-millisecond loop iterations.
_TIMEOUT_SECONDS = 5.0

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


def _make_manager(upstream_source, *, offline_mode=False):
    """Build an UpdateManager with an injected (blocking) upstream source."""
    return UpdateManager(
        UpdatesConfig(offline_mode=offline_mode, cache_ttl_hours=24),
        installed_probe=lambda: dict(INSTALLED),
        upstream_source=upstream_source,
        clock=lambda: datetime(2024, 1, 1, tzinfo=timezone.utc),
        network_probe=lambda: True,
    )


class EventGatedSource:
    """An upstream source that blocks on an event until released elsewhere.

    Calling the source signals :attr:`started` (so a coroutine can observe that
    the check is really running on its worker thread) and then parks on
    :attr:`release` until another party sets it. This makes "does the check block
    the event loop?" observable: only a *free* loop can run the coroutine that
    releases it.
    """

    def __init__(self, versions):
        self.versions = dict(versions)
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self):
        self.calls += 1
        self.started.set()
        if not self.release.wait(timeout=_TIMEOUT_SECONDS):
            raise TimeoutError("upstream source was never released by the event loop")
        return dict(self.versions)


class BlockingCalledError(AssertionError):
    """Raised if an upstream source is contacted when it should have been skipped."""


class TestNonBlockingUpstreamCheck:
    def test_async_check_does_not_block_the_event_loop(self):
        # A blocking source proves non-blocking dispatch: it is released only from
        # a coroutine, which can only run if the loop was never blocked (Req 23.3).
        source = EventGatedSource(UPSTREAM)
        manager = _make_manager(source)

        async def scenario():
            loop_iterations = 0
            check_task = asyncio.create_task(manager.check_upstream_async())

            # Wait for the source to actually begin blocking on its worker thread.
            while not source.started.is_set():
                await asyncio.sleep(0.001)
                loop_iterations += 1

            # The source is now parked. Keep driving the loop to show it is free,
            # then release the source from within the running coroutine.
            for _ in range(5):
                await asyncio.sleep(0.001)
                loop_iterations += 1
            source.release.set()

            result = await asyncio.wait_for(check_task, timeout=_TIMEOUT_SECONDS)
            return result, loop_iterations

        result, loop_iterations = asyncio.run(scenario())

        assert isinstance(result, UpstreamCheckResult)
        assert result.status == CHECK_PERFORMED
        assert result.available[COMPONENT_INSIGHT_PLUGIN_CLI] == "1.3.0"
        assert source.calls == 1
        # The loop kept iterating while the upstream source was parked.
        assert loop_iterations > 0

    def test_other_coroutines_progress_while_check_runs(self):
        # A slow (sleeping) source must not starve concurrently-scheduled work.
        def slow_source():
            time.sleep(0.2)
            return dict(UPSTREAM)

        manager = _make_manager(slow_source)

        async def scenario():
            progress = 0

            async def companion():
                nonlocal progress
                # Should complete its many quick iterations well before the
                # 0.2s upstream sleep finishes if the check is truly off-loop.
                for _ in range(20):
                    await asyncio.sleep(0.001)
                    progress += 1
                return "companion_done"

            check_result, companion_result = await asyncio.wait_for(
                asyncio.gather(manager.check_upstream_async(), companion()),
                timeout=_TIMEOUT_SECONDS,
            )
            return check_result, companion_result, progress

        check_result, companion_result, progress = asyncio.run(scenario())

        assert check_result.status == CHECK_PERFORMED
        assert companion_result == "companion_done"
        assert progress == 20

    def test_offline_mode_skips_upstream_source_via_async_entry(self):
        # Req 23.5: offline mode skips the upstream check; the source that would
        # otherwise block forever is never contacted, and the call returns at once.
        def must_not_be_called():
            raise BlockingCalledError("upstream source contacted while offline")

        manager = _make_manager(must_not_be_called, offline_mode=True)

        result = asyncio.run(asyncio.wait_for(manager.check_upstream_async(), timeout=_TIMEOUT_SECONDS))

        assert result.status == CHECK_SKIPPED_OFFLINE
        assert result.skipped is True
        assert result.available is None
        # Installed versions remain usable even though the check was skipped.
        assert manager.snapshot_installed()[COMPONENT_KIRO_CLI] == "0.9.0"


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    raise SystemExit(pytest.main([__file__, "-ra"]))
