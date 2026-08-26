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

from icplugin_builder.integrations import build_engine
from icplugin_builder.integrations import build_prep

from tests.docker_stub import stub_docker


@pytest.fixture(scope="session", autouse=True)
def _stub_docker_for_the_whole_session(tmp_path_factory):
    """Keep packaging away from a real Docker daemon for the entire session.

    A `.plg` is a gzipped `docker save` of the plugin image, so `BuildEngine.package`
    drives `docker build` and `docker save`. Almost no test is about Docker -- they are
    about validation gating, atomicity, naming, or the export path's reporting -- and
    left unguarded they each build a real image. Measured: the orchestrator and
    integrations suites went from about four minutes to twenty-five.

    Session-scoped on purpose. Several preservation observations are built in
    **module-scoped** fixtures, which run before any function-scoped fixture and so
    cannot be reached by one; a function-scoped guard let real builds straight through
    and reported `'docker' was not found on PATH` from inside a scrubbed environment.

    Substituted at the module constant rather than at each construction site, because
    there are dozens of them and a missed one silently reintroduces a real build. The
    constant is read at call time by `BuildEngine.__init__`, which is what makes this
    reach engines the tests construct themselves.
    """
    patcher = pytest.MonkeyPatch()
    patcher.setattr(build_engine, "DEFAULT_DOCKER_EXECUTABLE", stub_docker(tmp_path_factory.mktemp("docker-stub")))
    yield
    patcher.undo()


@pytest.fixture(autouse=True)
def _real_docker_when_a_test_asks(request, monkeypatch):
    """Give back the real `docker` to a test marked `builds_a_real_image`.

    The inverse of the session guard: stubbed by default, real on request. Such a test
    must also skip itself when no daemon is reachable, because a missing daemon is a fact
    about the host and not about the tool.
    """
    if request.node.get_closest_marker("builds_a_real_image"):
        monkeypatch.setattr(build_engine, "DEFAULT_DOCKER_EXECUTABLE", "docker")


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
