"""Suite-wide guards.

The one thing enforced here is that **no test contacts the network**. This codebase
already leans that way by design -- the ``Fetcher`` protocol exists so reference
retrieval can be faked, and its docstring says as much -- but the SDK version
resolution added a second outbound path, reached indirectly from
``Orchestrator.apply_turn`` rather than from an obvious call site. A test that
silently talks to PyPI is slow, fails on a plane, and passes for the wrong reason
when the index happens to agree with the fixture.

Rather than trusting every future test to remember, the lookup is disabled here for
the whole suite. A test that wants to exercise it injects a stub fetcher and passes
``consult_pypi=True`` explicitly, which is also how it stays readable at the call
site.
"""

from __future__ import annotations

import pytest

from icplugin_builder.integrations import build_prep


@pytest.fixture(autouse=True)
def _no_package_index_lookups(request, monkeypatch):
    """Make the PyPI SDK-version lookup fail fast instead of reaching the network.

    Fails rather than returning a version, so the fallback chain (local changelog,
    then installed distribution) is what tests exercise unless they opt in. The
    detail string names this fixture, so a surprising ``detail`` in a failure points
    straight here instead of looking like a real outage.

    A test that exercises the lookup itself marks itself
    ``@pytest.mark.consults_package_index`` and injects a stub fetcher -- it still
    contacts no network, it just needs the real resolution logic left in place.
    """
    if request.node.get_closest_marker("consults_package_index"):
        return
    monkeypatch.setattr(
        build_prep,
        "_pypi_sdk_version",
        lambda **_kwargs: (None, "the package index is not consulted from the test suite (tests/conftest.py)"),
    )
